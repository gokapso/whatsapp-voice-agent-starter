import json
import sqlite3

import pytest

from conftest import tool_call
from kapso_voice_agent.store import MAX_OWN_BOOKINGS, AppointmentStore
from kapso_voice_agent.tools import TOOLS, ToolRunner, provider_tool_definitions


def result(runner, name, params=None, tool_id="t"):
    response = runner.execute(tool_call(name, params, tool_id))
    return json.loads(response["result"]), response["is_error"]


def first_slot(store):
    return store.availability()["slots"][0]["slot"]


def booking(store, **changes):
    return {"slot": first_slot(store), "name": "Sam", "service_id": "brakes", "confirmed": True, **changes}


class TestBusinessInfo:
    def test_all_topics_come_from_business_data(self, store):
        data, error = result(ToolRunner(store, "a"), "business_info")
        assert not error and data["business_name"] == "North Loop Bikes"
        assert {s["id"] for s in data["services"]} >= {"brakes", "general_checkup"}
        assert data["weekly_hours"]["monday"] == []

    def test_unknown_topic_is_rejected_by_schema(self, store):
        data, error = result(ToolRunner(store, "a"), "business_info", {"topic": "salaries"})
        assert error and data["code"] == "invalid_arguments" and "topic" in data["message"]


class TestAvailability:
    def test_offers_at_most_two_future_slots_and_skips_closed_monday(self, store):
        data, error = result(ToolRunner(store, "a"), "available_slots")
        assert not error and len(data["slots"]) == 2 and data["has_more"] is True
        assert data["slots"][0] == {"slot": "2026-11-03T10:00:00-06:00", "when": "Tuesday, November 3 at 10 AM"}
        assert data["timezone"] == "America/Chicago"

    def test_requested_day_is_echoed_with_its_weekday(self, store):
        data, _ = result(ToolRunner(store, "a"), "available_slots", {"day": "2026-11-07"})
        assert data["open"] is True and data["day_spoken"] == "Saturday, November 7"
        assert [s["when"] for s in data["slots"]] == ["Saturday, November 7 at 10 AM", "Saturday, November 7 at 11 AM"]

    def test_closed_day_gives_reason_and_next_open_day(self, store):
        data, error = result(ToolRunner(store, "a"), "available_slots", {"day": "2026-11-26"})
        assert not error and data["open"] is False and data["closed_reason"] == "Thanksgiving holiday"
        assert data["slots"] == [] and data["day_spoken"] == "Thursday, November 26"
        assert data["next_open_day"] == "2026-11-28" and data["next_open_day_spoken"] == "Saturday, November 28"

    def test_weekly_closed_day_names_the_weekday(self, store):
        data, _ = result(ToolRunner(store, "a"), "available_slots", {"day": "2026-11-08"})
        assert data["open"] is False and data["closed_reason"] == "Closed on Sundays"
        assert data["next_open_day_spoken"] == "Tuesday, November 10"

    def test_fully_booked_day_points_at_the_next_free_time(self, store):
        saturday = [s for s in store.slot_inventory() if s.startswith("2026-11-07")]
        with store.connect() as db:
            db.executemany("INSERT INTO appointments VALUES (?, ?, 'b', 'Ana', 'brakes', '', 'now')",
                           [(f"FULL{i}", slot) for i, slot in enumerate(saturday)])
        data, error = result(ToolRunner(store, "a"), "available_slots", {"day": "2026-11-07"})
        assert not error and data["open"] is True and data["slots"] == [] and data["has_more"] is False
        assert data["next_available"] == {"slot": "2026-11-10T10:00:00-06:00", "when": "Tuesday, November 10 at 10 AM"}

    def test_afternoon_times_are_spoken_in_twelve_hour_form(self, store):
        data, _ = result(ToolRunner(store, "a"), "available_slots", {"day": "2026-11-03", "after": "2026-11-03T13:00:00-06:00"})
        assert data["slots"][0]["when"] == "Tuesday, November 3 at 3 PM"

    @pytest.mark.parametrize("day,code", [("tuesday", "invalid_date"), ("next tuesday", "invalid_arguments"), ("2027-06-01", "outside_horizon"),
                                          ("2026-11-01", "outside_horizon")])
    def test_bad_days_return_deterministic_codes(self, store, day, code):
        data, error = result(ToolRunner(store, "a"), "available_slots", {"day": day})
        assert error and data["code"] == code

    def test_out_of_range_day_says_which_dates_work(self, store):
        data, _ = result(ToolRunner(store, "a"), "available_slots", {"day": "2027-06-01"})
        assert "through Tuesday, December 1" in data["message"]

    def test_booked_slot_is_no_longer_offered_and_after_pages_forward(self, store):
        runner = ToolRunner(store, "a")
        slots = [s["slot"] for s in store.availability()["slots"]]
        result(runner, "book_appointment", booking(store, slot=slots[0]))
        assert store.availability()["slots"][0]["slot"] == slots[1]
        later = [s["slot"] for s in store.availability(after=slots[1])["slots"]]
        assert later and all(s > slots[1] for s in later)


class TestBooking:
    def test_requires_explicit_true_confirmation(self, store):
        runner = ToolRunner(store, "a")
        data, error = result(runner, "book_appointment", booking(store, confirmed=False), "b1")
        assert error and data["code"] == "confirmation_required"
        data, error = result(runner, "book_appointment", booking(store, confirmed="true"), "b2")
        assert error and data["code"] == "invalid_arguments"
        assert store.list_own("a")["appointments"] == []

    @pytest.mark.parametrize("changes,code", [({"service_id": "teleport"}, "unknown_service"),
                                              ({"slot": "2026-11-03T03:00:00-06:00"}, "slot_unavailable"),
                                              ({"slot": "tomorrow at ten"}, "slot_unavailable")])
    def test_invalid_details_are_refused(self, store, changes, code):
        data, error = result(ToolRunner(store, "a"), "book_appointment", booking(store, **changes))
        assert error and data["code"] == code

    def test_retried_tool_call_id_does_not_double_book(self, store):
        runner = ToolRunner(store, "a")
        first = runner.execute(tool_call("book_appointment", booking(store), "same-id"))
        again = runner.execute(tool_call("book_appointment", booking(store), "same-id"))
        assert first == again and len(store.list_own("a")["appointments"]) == 1

    def test_same_caller_rebooking_same_slot_is_idempotent(self, store):
        runner = ToolRunner(store, "a")
        args = booking(store)
        result(runner, "book_appointment", args, "x1")
        data, error = result(runner, "book_appointment", args, "x2")
        assert not error and data["status"] == "already_booked"

    def test_another_caller_cannot_take_a_booked_slot(self, store):
        args = booking(store)
        result(ToolRunner(store, "a"), "book_appointment", args)
        data, error = result(ToolRunner(store, "b"), "book_appointment", args)
        assert error and data["code"] == "slot_taken"

    def test_success_result_says_when_and_is_marked_fictional(self, store):
        data, error = result(ToolRunner(store, "a"), "book_appointment", booking(store))
        assert not error and data["status"] == "booked" and data["fictional"] is True
        assert data["when"] == "Tuesday, November 3 at 10 AM" and data["timezone"] == "America/Chicago"
        assert data["service_name"] == "Brake check and adjustment" and data["name"] == "Sam"

    def test_stale_slot_is_recoverable_with_a_fresh_lookup(self, store):
        """The flow the prompt asks for: slot_taken -> available_slots again -> book a new time."""
        stale = booking(store)
        result(ToolRunner(store, "someone-else"), "book_appointment", stale)
        runner = ToolRunner(store, "a")
        refused, error = result(runner, "book_appointment", stale, "b1")
        assert error and refused["code"] == "slot_taken" and refused["message"].startswith("Not booked.")
        assert "available_slots" in refused["message"]
        fresh, _ = result(runner, "available_slots", {"day": stale["slot"][:10]}, "s1")
        assert stale["slot"] not in [s["slot"] for s in fresh["slots"]]
        booked, error = result(runner, "book_appointment", {**stale, "slot": fresh["slots"][0]["slot"]}, "b2")
        assert not error and booked["status"] == "booked"

    def test_failures_name_how_to_correct_them(self, store):
        runner = ToolRunner(store, "a")
        unknown, _ = result(runner, "book_appointment", booking(store, service_id="Brake check"), "u1")
        assert unknown["code"] == "unknown_service" and "brakes" in unknown["message"] and "general_checkup" in unknown["message"]
        unconfirmed, _ = result(runner, "book_appointment", booking(store, confirmed=False), "u2")
        assert unconfirmed["message"].startswith("Not booked.") and "confirmed=true" in unconfirmed["message"]


class TestCallerIsolation:
    def test_lists_and_cancels_only_own_bookings(self, store):
        own, other = ToolRunner(store, "caller-a"), ToolRunner(store, "caller-b")
        booked, _ = result(own, "book_appointment", booking(store))
        listed, _ = result(other, "my_appointments")
        assert listed["appointments"] == []
        data, error = result(other, "cancel_appointment", {"booking_id": booked["id"], "confirmed": True}, "c1")
        # Same response as an unknown ID: no hint that the booking exists.
        assert error and data["code"] == "not_found"
        data, error = result(own, "cancel_appointment", {"booking_id": booked["id"], "confirmed": False}, "c2")
        assert error and data["code"] == "confirmation_required"
        data, error = result(own, "cancel_appointment", {"booking_id": booked["id"].lower(), "confirmed": True}, "c3")
        assert not error and data["status"] == "cancelled"
        assert data["id"] == booked["id"] and data["when"] == booked["when"] and data["service_name"] == booked["service_name"]
        assert store.list_own("caller-a")["appointments"] == []

    def test_own_appointments_have_spoken_times(self, store):
        own = ToolRunner(store, "caller-a")
        booked, _ = result(own, "book_appointment", booking(store), "b1")
        listed, _ = result(own, "my_appointments", tool_id="l1")
        assert [(a["id"], a["when"]) for a in listed["appointments"]] == [(booked["id"], "Tuesday, November 3 at 10 AM")]

    @pytest.mark.parametrize("params", [{"caller": "someone-else"}, {"phone": "15550100009"}])
    def test_identity_arguments_are_rejected(self, store, params):
        data, error = result(ToolRunner(store, "a"), "my_appointments", params)
        assert error and data["code"] == "invalid_arguments"

    def test_unknown_tools_and_empty_identity(self, store):
        data, error = result(ToolRunner(store, "a"), "transfer_call", {"number": "15550100009"})
        assert error and data["code"] == "unknown_tool"
        with pytest.raises(ValueError):
            ToolRunner(store, "")


def test_store_errors_become_tool_data_not_crashes(store, monkeypatch):
    monkeypatch.setattr(store, "list_own", lambda caller: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    data, error = result(ToolRunner(store, "a"), "my_appointments")
    assert error and data["code"] == "internal_error"


class TestBoundedQueries:
    def test_own_bookings_are_capped(self, store):
        slots = store.slot_inventory()[:MAX_OWN_BOOKINGS + 3]
        with store.connect() as db:
            db.executemany("INSERT INTO appointments VALUES (?, ?, 'a', 'Sam', 'brakes', '', 'now')",
                           [(f"ID{i}", slot) for i, slot in enumerate(slots)])
        rows = store.list_own("a")["appointments"]
        assert len(rows) == MAX_OWN_BOOKINGS and [r["slot"] for r in rows] == slots[:MAX_OWN_BOOKINGS]

    def test_queries_use_indexes(self, store):
        with store.connect() as db:
            own = " ".join(r[3] for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT id, slot, name, service_id, note FROM appointments "
                "WHERE caller = ? AND slot >= ? ORDER BY slot, id LIMIT 10", ("a", "2026")))
            taken = " ".join(r[3] for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT slot FROM appointments WHERE slot >= ? AND slot <= ?", ("a", "b")))
        assert "appointments_caller_slot" in own
        assert "INDEX" in taken and "SCAN appointments" not in taken.replace("COVERING INDEX", "")


def test_provider_tool_definitions_match_runner_and_have_no_filler(spec):
    definitions = provider_tool_definitions()
    assert [d["name"] for d in definitions] == list(TOOLS)
    for definition in definitions:
        assert definition["pre_tool_speech"] == "off" and "tool_call_sound" not in definition
        assert definition["execution_mode"] == "immediate"
        for name in ("caller", "phone", "user_id", "wa_id"):
            assert name not in definition["parameters"]["properties"]


def test_fresh_store_builds_from_business_data(tmp_path, spec):
    store = AppointmentStore(tmp_path / "new.sqlite3", spec.business_path)
    assert store.info("hours")["timezone"] == "America/Chicago"


def test_every_failure_code_the_tools_return_has_a_recovery_rule_in_the_prompt(store, spec, monkeypatch):
    runner = ToolRunner(store, "a")
    taken = booking(store)
    result(ToolRunner(store, "b"), "book_appointment", taken, "seed")
    attempts = [("transfer_call", {}), ("available_slots", {"day": 7}), ("available_slots", {"day": "tuesday"}),
                ("available_slots", {"day": "2027-06-01"}), ("book_appointment", booking(store, confirmed=False)),
                ("book_appointment", booking(store, service_id="teleport")),
                ("book_appointment", booking(store, slot="2026-11-03T03:00:00-06:00")), ("book_appointment", taken),
                ("cancel_appointment", {"booking_id": "NOPE", "confirmed": True})]
    codes = {result(runner, name, params, f"r{i}")[0]["code"] for i, (name, params) in enumerate(attempts)}
    monkeypatch.setattr(store, "info", lambda topic: 1 / 0)
    codes.add(result(runner, "business_info", {}, "boom")[0]["code"])
    assert len(codes) == len(attempts) + 1
    recovery = spec.prompt.split("## When a tool says no", 1)[1].split("##", 1)[0]
    assert [code for code in sorted(codes) if code not in recovery] == []
