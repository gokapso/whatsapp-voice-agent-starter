"""The Cal.com calendar against a mock of Cal.com API v2. The mock follows the documented request
and response shapes (https://cal.com/docs/api-reference/v2) and records every request, so these
tests pin the exact contract: routes, versions, auth, UTC start times, and what happens when
Cal.com refuses, fails or does not answer. Nothing here reaches the network."""

from datetime import datetime
import json
import secrets
import sqlite3
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import NOW, tool_call
from kapso_voice_agent import calcom, cli
from kapso_voice_agent.calcom import MAX_OWN_BOOKINGS, CalComCalendar
from kapso_voice_agent.tools import ToolRunner

KEY = "cal_test_not_real"
VERSIONS = {"slots": "2024-09-04", "bookings": "2026-02-25", "event-types": "2026-06-12"}
EVENT_TYPES = {1001: {"id": 1001, "title": "Consultation", "slug": "consultation", "lengthInMinutes": 30,
                      "description": "A first conversation.", "hidden": False},
               1002: {"id": 1002, "title": "Follow-up", "slug": "follow-up", "lengthInMinutes": 15,
                      "description": "", "hidden": False}}
TUESDAY_10 = "2026-11-03T10:00:00-06:00"


def cal_time(local):
    """How Cal.com writes a time when asked for timeZone=America/Chicago: milliseconds and an offset."""
    return local.replace(":00-06:00", ":00.000-06:00")


class FakeCalCom:
    """Cal.com API v2 stand-in. `free` holds open start times (local ISO) per event type."""

    def __init__(self):
        self.requests, self.bookings, self.overrides = [], {}, {}
        self.free = {1001: [TUESDAY_10, "2026-11-03T11:00:00-06:00", "2026-11-03T14:00:00-06:00",
                            "2026-11-04T10:00:00-06:00"],
                     1002: ["2026-11-05T09:00:00-06:00"]}
        self.booking_status = "accepted"
        self.on_write = None

    def __call__(self, request):
        assert request.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, request.headers.get("cal-api-version"), body,
                              parse_qs(request.url.query.decode())))
        for (method, prefix), override in self.overrides.items():
            if request.method == method and request.url.path.startswith(prefix):
                return override(request)
        if request.method == "POST" and self.on_write:
            self.on_write(request)
        path = request.url.path.removeprefix("/v2/").split("/")
        version = request.headers.get("cal-api-version")
        if path[0] in VERSIONS and version != VERSIONS[path[0]]:
            return httpx.Response(400, json={"status": "error", "error": {"message": "wrong cal-api-version"}})
        match request.method, path:
            case "GET", ["me"]:
                return ok({"id": 7, "username": "studio", "timeZone": "America/Chicago"})
            case "GET", ["event-types"]:
                return ok(list(EVENT_TYPES.values()))
            case "GET", ["event-types", type_id]:
                return ok(EVENT_TYPES[int(type_id)]) if int(type_id) in EVENT_TYPES else missing()
            case "GET", ["slots"]:
                query = parse_qs(request.url.query.decode())
                first, last = (datetime.fromisoformat(query[k][0].replace("Z", "+00:00")) for k in ("start", "end"))
                data = {}
                for start in self.free.get(int(query["eventTypeId"][0]), []):
                    if first <= datetime.fromisoformat(start) < last:
                        data.setdefault(start[:10], []).append({"start": cal_time(start)})
                return ok(data)
            case "POST", ["bookings"]:
                return self.create(body["eventTypeId"], body["start"], body)
            case "GET", ["bookings", uid]:
                return ok(self.bookings[uid]) if uid in self.bookings else missing()
            case "POST", ["bookings", uid, "cancel"]:
                if uid not in self.bookings:
                    return missing()
                self.bookings[uid]["status"] = "cancelled"
                return ok(self.bookings[uid])
            case "POST", ["bookings", uid, "reschedule"]:
                if uid not in self.bookings:
                    return missing()
                old = self.bookings[uid]
                response = self.create(old["eventTypeId"], body["start"], {"attendee": old["attendees"][0]})
                if response.status_code == 201:
                    old["status"] = "cancelled"
                    self.free[old["eventTypeId"]].append(old["local_start"])
                return response
        return missing()

    def create(self, event_type_id, start_utc, body):
        local = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).astimezone(NOW.tzinfo).isoformat()
        if local not in self.free.get(event_type_id, []):
            return httpx.Response(400, json={"status": "error", "error": {
                "message": "User either already has booking at this time or is not available"}})
        self.free[event_type_id].remove(local)
        uid = "uid" + secrets.token_hex(6)
        self.bookings[uid] = {"id": len(self.bookings) + 1, "uid": uid, "status": self.booking_status,
                              "start": start_utc.replace("Z", ".000Z"), "eventTypeId": event_type_id, "local_start": local,
                              "attendees": [body["attendee"]]}
        return httpx.Response(201, json={"status": "success", "data": self.bookings[uid]})

    def calls(self, method=None):
        return [(m, p) for m, p, *_ in self.requests if method in (None, m)]


def ok(data):
    return httpx.Response(200, json={"status": "success", "data": data})


def missing():
    return httpx.Response(404, json={"status": "error", "error": {"message": "not found"}})


@pytest.fixture
def cal():
    return FakeCalCom()


@pytest.fixture
def calendar(tmp_path, owner_spec, cal):
    return CalComCalendar(tmp_path / "calendar.sqlite3", owner_spec.business, KEY, clock=lambda: NOW,
                          transport=httpx.MockTransport(cal))


def run(runner, name, params=None, tool_id=None):
    response = runner.execute(tool_call(name, params, tool_id or secrets.token_hex(4)))
    return json.loads(response["result"]), response["is_error"]


def booking(**changes):
    return {"slot": TUESDAY_10, "service_id": "consultation", "name": "Sam Lee", "email": "sam.lee@example.com",
            "confirmed": True, **changes}


class TestContract:
    def test_every_request_carries_the_key_and_the_version_its_endpoint_requires(self, calendar, cal):
        runner = ToolRunner(calendar, "caller-a")
        run(runner, "business_info", {"topic": "services"})
        run(runner, "available_slots", {"service_id": "consultation"})
        booked, _ = run(runner, "book_appointment", booking())
        run(runner, "my_appointments")
        run(runner, "reschedule_appointment", {"booking_id": booked["id"], "slot": "2026-11-03T11:00:00-06:00", "confirmed": True})
        run(runner, "cancel_appointment", {"booking_id": booked["id"], "confirmed": True})
        seen = {(method, path.split("/")[2], version) for method, path, version, *_ in cal.requests}
        assert seen == {("GET", "event-types", "2026-06-12"), ("GET", "slots", "2024-09-04"), ("POST", "bookings", "2026-02-25"),
                        ("GET", "bookings", "2026-02-25")}

    def test_booking_body_sends_utc_start_event_type_and_attendee(self, calendar, cal):
        data, error = run(ToolRunner(calendar, "caller-a"), "book_appointment", booking(note="First visit"))
        assert not error and data["status"] == "booked" and data["when"] == "Tuesday, November 3 at 10 AM"
        [(_, path, _, body, _)] = [r for r in cal.requests if r[0] == "POST"]
        assert path == "/v2/bookings" and body == {
            "start": "2026-11-03T16:00:00Z", "eventTypeId": 1001,
            "attendee": {"name": "Sam Lee", "timeZone": "America/Chicago", "language": "en", "email": "sam.lee@example.com"},
            "metadata": {"source": "kapso-voice-agent", "note": "First visit"}}
        # The model sees a short local id, never the Cal.com uid.
        assert len(data["id"]) == 8 and not any(uid in json.dumps(data) for uid in cal.bookings)

    def test_slot_query_uses_the_event_type_a_utc_window_and_the_business_time_zone(self, calendar, cal):
        run(ToolRunner(calendar, "a"), "available_slots", {"service_id": "follow_up", "day": "2026-11-05"})
        [query] = [q for _, path, _, _, q in cal.requests if path == "/v2/slots"]
        assert query == {"eventTypeId": ["1002"], "start": ["2026-11-05T06:00:00Z"], "end": ["2026-11-19T06:00:00Z"],
                         "timeZone": ["America/Chicago"]}


class TestAvailability:
    def test_real_times_are_offered_two_at_a_time_with_spoken_phrases(self, calendar):
        data, error = run(ToolRunner(calendar, "a"), "available_slots", {"service_id": "consultation"})
        assert not error and data["service_name"] == "Consultation" and data["needs_email"] is True
        assert data["slots"] == [{"slot": TUESDAY_10, "when": "Tuesday, November 3 at 10 AM"},
                                 {"slot": "2026-11-03T11:00:00-06:00", "when": "Tuesday, November 3 at 11 AM"}]
        assert data["has_more"] is True

    def test_times_in_utc_are_spoken_in_the_business_time_zone(self, calendar, cal):
        cal.overrides[("GET", "/v2/slots")] = lambda r: ok({"2026-11-03": [{"start": "2026-11-03T20:30:00.000Z"}]})
        data, _ = run(ToolRunner(calendar, "a"), "available_slots", {"service_id": "consultation"})
        assert data["slots"] == [{"slot": "2026-11-03T14:30:00-06:00", "when": "Tuesday, November 3 at 2:30 PM"}]

    def test_later_times_page_forward_from_the_last_offer(self, calendar):
        data, _ = run(ToolRunner(calendar, "a"), "available_slots", {"service_id": "consultation",
                                                                      "after": "2026-11-03T11:00:00-06:00"})
        assert [s["when"] for s in data["slots"]] == ["Tuesday, November 3 at 2 PM", "Wednesday, November 4 at 10 AM"]

    def test_a_day_without_times_points_at_the_next_available_one(self, calendar):
        data, error = run(ToolRunner(calendar, "a"), "available_slots", {"service_id": "consultation", "day": "2026-11-02"})
        assert not error and data["slots"] == [] and data["day_spoken"] == "Monday, November 2"
        assert data["next_available"] == {"slot": TUESDAY_10, "when": "Tuesday, November 3 at 10 AM"}

    def test_nothing_open_says_so_instead_of_inventing_times(self, calendar, cal):
        cal.free = {1001: []}
        data, error = run(ToolRunner(calendar, "a"), "available_slots", {"service_id": "consultation"})
        assert not error and data["slots"] == [] and data["next_available"] is None

    @pytest.mark.parametrize("params,code", [({}, "service_required"), ({"service_id": "haircut"}, "unknown_service"),
                                             ({"service_id": "consultation", "day": "tuesday"}, "invalid_date"),
                                             ({"service_id": "consultation", "day": "2026-12-15"}, "outside_horizon"),
                                             ({"service_id": "consultation", "after": "later"}, "invalid_arguments")])
    def test_bad_requests_are_answered_without_calling_cal_com(self, calendar, cal, params, code):
        data, error = run(ToolRunner(calendar, "a"), "available_slots", params)
        assert error and data["code"] == code and cal.requests == []

    def test_a_single_service_needs_no_service_id(self, tmp_path, owner_spec, cal):
        business = owner_spec.business
        business.data["services"] = business.data["services"][:1]
        single = CalComCalendar(tmp_path / "one.sqlite3", business, KEY, clock=lambda: NOW, transport=httpx.MockTransport(cal))
        data, error = run(ToolRunner(single, "a"), "available_slots")
        assert not error and data["service_id"] == "consultation" and data["slots"]


class TestFailClosed:
    @pytest.mark.parametrize("response", [
        lambda r: httpx.Response(401, json={"status": "error"}),
        lambda r: httpx.Response(500, text="oops"),
        lambda r: httpx.Response(200, text="<html>"),
        lambda r: httpx.Response(200, json={"status": "error", "data": {}}),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)),
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)),
    ])
    def test_unreadable_availability_offers_no_times(self, calendar, cal, response):
        cal.overrides[("GET", "/v2/slots")] = response
        data, error = run(ToolRunner(calendar, "a"), "available_slots", {"service_id": "consultation"})
        assert error and data["code"] == "calendar_unavailable" and "slots" not in data

    def test_a_missing_event_type_is_not_papered_over(self, calendar, cal):
        cal.overrides[("GET", "/v2/event-types")] = lambda r: missing()
        data, error = run(ToolRunner(calendar, "a"), "business_info", {"topic": "services"})
        assert error and data["code"] == "calendar_unavailable"

    def test_rejected_key_on_a_write_books_nothing(self, calendar, cal):
        cal.overrides[("POST", "/v2/bookings")] = lambda r: httpx.Response(401, json={"status": "error"})
        data, error = run(ToolRunner(calendar, "a"), "book_appointment", booking())
        assert error and data["code"] == "calendar_unavailable" and calendar.list_own("a")["appointments"] == []

    def test_an_unreachable_calendar_was_never_asked(self, calendar, cal):
        cal.overrides[("POST", "/v2/bookings")] = lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r))
        data, error = run(ToolRunner(calendar, "a"), "book_appointment", booking())
        assert error and data["code"] == "calendar_unavailable"

    @pytest.mark.parametrize("response", [
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("no answer", request=r)),
        lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError("dropped", request=r)),
        lambda r: httpx.Response(502, text="bad gateway"),
        lambda r: httpx.Response(201, json={"status": "success", "data": {}}),
    ])
    def test_an_ambiguous_booking_is_never_reported_or_retried(self, calendar, cal, response):
        cal.overrides[("POST", "/v2/bookings")] = response
        data, error = run(ToolRunner(calendar, "a"), "book_appointment", booking())
        assert error and data["code"] == "not_confirmed" and "do not try it again" in data["message"]
        assert cal.calls("POST") == [("POST", "/v2/bookings")]  # one attempt, no automatic retry
        assert calendar.list_own("a")["appointments"] == []


class TestBooking:
    def test_nothing_is_sent_before_a_clear_yes_and_complete_details(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        for changes, code in [({"confirmed": False}, "confirmation_required"), ({"email": ""}, "email_required"),
                              ({"email": "sam at example"}, "invalid_email"), ({"service_id": "haircut"}, "unknown_service"),
                              ({"slot": "2026-11-02T08:00:00-06:00"}, "slot_unavailable"),
                              ({"slot": "Tuesday at ten"}, "slot_unavailable")]:
            data, error = run(runner, "book_appointment", booking(**changes))
            assert error and data["code"] == code, changes
        data, error = run(runner, "book_appointment", booking(confirmed="true"))
        assert error and data["code"] == "invalid_arguments"
        assert cal.calls("POST") == []

    def test_a_time_taken_since_it_was_offered_is_reported_as_taken(self, calendar, cal):
        cal.free[1001].remove(TUESDAY_10)  # someone booked it on the Cal.com page
        data, error = run(ToolRunner(calendar, "a"), "book_appointment", booking())
        assert error and data["code"] == "slot_taken" and "available_slots" in data["message"]

    def test_a_refusal_for_another_reason_is_not_called_a_conflict(self, calendar, cal):
        cal.overrides[("POST", "/v2/bookings")] = lambda r: httpx.Response(400, json={"status": "error"})
        data, error = run(ToolRunner(calendar, "a"), "book_appointment", booking())
        assert error and data["code"] == "booking_rejected"

    def test_a_repeated_request_never_creates_a_second_booking(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        first = runner.execute(tool_call("book_appointment", booking(), "same-id"))
        assert runner.execute(tool_call("book_appointment", booking(), "same-id")) == first
        [uid] = cal.bookings
        cal.requests.clear()
        read = cal.overrides[("GET", "/v2/bookings")] = lambda r: (no_write_lock(calendar), ok(cal.bookings[uid]))[1]
        again, error = run(runner, "book_appointment", booking())
        assert read and not error and again["status"] == "already_booked" and again["service_id"] == "consultation"
        assert again["id"] == json.loads(first["result"])["id"]
        # Answered from Cal.com's current state (outside any SQLite transaction), not from the local map alone.
        assert cal.calls() == [("GET", f"/v2/bookings/{uid}")]

    def test_a_repeat_of_a_request_the_business_has_not_confirmed_stays_requested(self, calendar, cal):
        cal.booking_status = "pending"
        runner = ToolRunner(calendar, "a")
        first, _ = run(runner, "book_appointment", booking())
        again, error = run(runner, "book_appointment", booking())
        assert not error and again["status"] == "pending" and again["id"] == first["id"]
        assert "not confirmed" in again["message"] and len(cal.calls("POST")) == 1

    @pytest.mark.parametrize("gone", ["cancelled", "rejected", "missing"])
    def test_a_repeat_after_cal_com_dropped_the_booking_books_again(self, calendar, cal, gone):
        runner = ToolRunner(calendar, "a")
        first, _ = run(runner, "book_appointment", booking())
        [uid] = cal.bookings
        if gone == "missing":
            del cal.bookings[uid]
        else:
            cal.bookings[uid]["status"] = gone
        cal.free[1001].append(TUESDAY_10)
        again, error = run(runner, "book_appointment", booking())
        assert not error and again["status"] == "booked" and again["id"] != first["id"]
        assert len(cal.calls("POST")) == 2 and calendar.own("a", first["id"]) is None

    def test_a_repeat_whose_booking_cannot_be_read_is_not_confirmed_or_sent_again(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        run(runner, "book_appointment", booking())
        cal.overrides[("GET", "/v2/bookings")] = lambda r: httpx.Response(503)
        again, error = run(runner, "book_appointment", booking())
        assert error and again["code"] == "calendar_unavailable" and len(cal.calls("POST")) == 1

    def test_another_service_at_the_same_time_is_never_reported_as_booked_from_the_first(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        first, _ = run(runner, "book_appointment", booking())
        cal.free[1001].append(TUESDAY_10)  # the other event type is free then
        cal.free[1002].append(TUESDAY_10)
        other, error = run(runner, "book_appointment", booking(service_id="follow_up"))
        assert not error and other["status"] == "booked" and other["id"] != first["id"]
        assert [r[3]["eventTypeId"] for r in cal.requests if r[:2] == ("POST", "/v2/bookings")] == [1001, 1002]

    def test_another_service_cal_com_refuses_at_that_time_is_not_booked(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        run(runner, "book_appointment", booking())
        other, error = run(runner, "book_appointment", booking(service_id="follow_up"))
        assert error and other["code"] == "slot_taken" and "id" not in other

    def test_a_booking_that_needs_the_business_to_confirm_is_called_requested(self, calendar, cal):
        cal.booking_status = "pending"
        data, error = run(ToolRunner(calendar, "a"), "book_appointment", booking())
        assert not error and data["status"] == "pending" and "not confirmed" in data["message"]

    def test_email_can_be_turned_off_by_the_owner(self, tmp_path, owner_spec, cal):
        business = owner_spec.business
        business.data["booking"]["ask_for_email"] = False
        quiet = CalComCalendar(tmp_path / "x.sqlite3", business, KEY, clock=lambda: NOW, transport=httpx.MockTransport(cal))
        data, error = run(ToolRunner(quiet, "a"), "book_appointment", booking(email=""))
        assert not error and "email" not in cal.requests[-1][3]["attendee"]

    def test_no_sqlite_write_lock_is_held_while_cal_com_is_called(self, calendar, cal):
        def try_write(request):
            db = sqlite3.connect(calendar.path, timeout=0)
            try:
                db.execute("BEGIN IMMEDIATE")  # fails at once if another connection holds a write lock
                db.rollback()
            finally:
                db.close()
        cal.on_write = try_write
        runner = ToolRunner(calendar, "a")
        booked, error = run(runner, "book_appointment", booking())
        moved, _ = run(runner, "reschedule_appointment", {"booking_id": booked["id"], "slot": "2026-11-03T11:00:00-06:00",
                                                          "confirmed": True})
        cancelled, _ = run(runner, "cancel_appointment", {"booking_id": booked["id"], "confirmed": True})
        assert not error and moved["status"] == "rescheduled" and cancelled["status"] == "cancelled"


class TestOwnAppointments:
    def test_callers_see_and_change_only_their_own_bookings(self, calendar, cal):
        own, other = ToolRunner(calendar, "caller-a"), ToolRunner(calendar, "caller-b")
        booked, _ = run(own, "book_appointment", booking())
        before = len(cal.requests)
        listed, _ = run(other, "my_appointments")
        moved, moved_error = run(other, "reschedule_appointment", {"booking_id": booked["id"], "slot": TUESDAY_10, "confirmed": True})
        cancelled, cancel_error = run(other, "cancel_appointment", {"booking_id": booked["id"], "confirmed": True})
        assert listed["appointments"] == [] and moved["code"] == cancelled["code"] == "not_found"
        assert moved_error and cancel_error and len(cal.requests) == before  # nothing about A's booking reached Cal.com
        mine, _ = run(own, "my_appointments")
        assert mine["appointments"] == [{"id": booked["id"], "slot": TUESDAY_10, "when": "Tuesday, November 3 at 10 AM",
                                         "service_id": "consultation", "status": "booked"}]

    def test_a_booking_cancelled_in_cal_com_is_no_longer_listed(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        run(runner, "book_appointment", booking())
        next(iter(cal.bookings.values()))["status"] = "cancelled"
        listed, error = run(runner, "my_appointments")
        assert not error and listed["appointments"] == []

    def test_listing_fails_closed_when_cal_com_cannot_be_read(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        run(runner, "book_appointment", booking())
        cal.overrides[("GET", "/v2/bookings")] = lambda r: httpx.Response(503)
        listed, error = run(runner, "my_appointments")
        assert error and listed["code"] == "calendar_unavailable"

    def test_cancel_after_a_clear_yes(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        booked, _ = run(runner, "book_appointment", booking())
        data, error = run(runner, "cancel_appointment", {"booking_id": booked["id"], "confirmed": False})
        assert error and data["code"] == "confirmation_required" and len(cal.calls("POST")) == 1
        data, error = run(runner, "cancel_appointment", {"booking_id": booked["id"].lower(), "confirmed": True})
        assert not error and data["status"] == "cancelled" and data["when"] == booked["when"]
        [(_, _, _, body, _)] = [r for r in cal.requests if r[1].endswith("/cancel")]
        assert body == {"cancellationReason": "Cancelled by the caller on a phone call."}
        assert run(runner, "my_appointments")[0]["appointments"] == []

    def test_cancelling_a_booking_cal_com_no_longer_has(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        booked, _ = run(runner, "book_appointment", booking())
        cal.bookings.clear()
        data, error = run(runner, "cancel_appointment", {"booking_id": booked["id"], "confirmed": True})
        assert error and data["code"] == "not_found" and calendar.own("a", booked["id"]) is None

    def test_reschedule_moves_the_booking_and_keeps_the_callers_id(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        booked, _ = run(runner, "book_appointment", booking())
        data, error = run(runner, "reschedule_appointment", {"booking_id": booked["id"], "slot": "2026-11-04T10:00:00-06:00",
                                                             "confirmed": True})
        assert not error and data["status"] == "rescheduled" and data["id"] == booked["id"]
        assert data["when"] == "Wednesday, November 4 at 10 AM" and data["previous_when"] == "Tuesday, November 3 at 10 AM"
        [(_, path, _, body, _)] = [r for r in cal.requests if r[1].endswith("/reschedule")]
        assert body == {"start": "2026-11-04T16:00:00Z", "reschedulingReason": "Moved by the caller on a phone call."}
        assert [a["slot"] for a in run(runner, "my_appointments")[0]["appointments"]] == ["2026-11-04T10:00:00-06:00"]

    def test_moving_to_the_same_time_changes_nothing(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        booked, _ = run(runner, "book_appointment", booking())
        data, error = run(runner, "reschedule_appointment", {"booking_id": booked["id"], "slot": TUESDAY_10, "confirmed": True})
        assert not error and data["status"] == "unchanged" and len(cal.calls("POST")) == 1

    def test_a_move_the_business_must_confirm_is_called_requested(self, calendar, cal):
        cal.booking_status = "pending"
        runner = ToolRunner(calendar, "a")
        booked, _ = run(runner, "book_appointment", booking())
        data, error = run(runner, "reschedule_appointment", {"booking_id": booked["id"], "slot": "2026-11-04T10:00:00-06:00",
                                                             "confirmed": True})
        assert not error and data["status"] == "pending" and "not confirmed" in data["message"]
        assert data["id"] == booked["id"] and data["previous_when"] == "Tuesday, November 3 at 10 AM"
        assert run(runner, "my_appointments")[0]["appointments"][0]["status"] == "pending"

    def test_reschedule_to_a_taken_time_keeps_the_old_one(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        booked, _ = run(runner, "book_appointment", booking())
        cal.free[1001].remove("2026-11-04T10:00:00-06:00")
        data, error = run(runner, "reschedule_appointment", {"booking_id": booked["id"], "slot": "2026-11-04T10:00:00-06:00",
                                                             "confirmed": True})
        assert error and data["code"] == "slot_taken"
        assert [a["slot"] for a in run(runner, "my_appointments")[0]["appointments"]] == [TUESDAY_10]


def no_write_lock(calendar):
    """Fails at once if another connection holds a SQLite write lock."""
    db = sqlite3.connect(calendar.path, timeout=0)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()
    finally:
        db.close()


def seated_type(event_type_id):
    """How Cal.com describes an event type with seats (EventTypeOutput_2026_06_12)."""
    return lambda r: ok({**EVENT_TYPES[event_type_id], "seatsPerTimeSlot": 4,
                         "seats": {"seatsPerTimeSlot": 4, "showAttendeeInfo": False, "showAvailabilityCount": True}})


class TestSeatedEventTypes:
    """Cal.com cancels or moves every seat of a seated booking when the owner's key sends no seatUid,
    so the agent never books, moves or cancels on a seated event type."""

    def test_a_seated_service_is_refused_before_any_slot_read_or_booking(self, calendar, cal):
        cal.overrides[("GET", "/v2/event-types/1001")] = seated_type(1001)
        runner = ToolRunner(calendar, "a")
        slots, slots_error = run(runner, "available_slots", {"service_id": "consultation"})
        booked, book_error = run(runner, "book_appointment", booking())
        assert slots_error and book_error and slots["code"] == booked["code"] == "unsupported_service"
        assert cal.calls() == [("GET", "/v2/event-types/1001")]  # cached; no slots, no POST

    def test_disabled_seats_are_an_ordinary_event_type(self, calendar, cal):
        cal.overrides[("GET", "/v2/event-types/1001")] = lambda r: ok({**EVENT_TYPES[1001], "seatsPerTimeSlot": None,
                                                                        "seats": {"seatsPerTimeSlot": 4, "disabled": True}})
        data, error = run(ToolRunner(calendar, "a"), "book_appointment", booking())
        assert not error and data["status"] == "booked"

    def test_a_booking_whose_type_became_seated_is_never_moved_or_cancelled_for_everyone(self, calendar, cal):
        runner = ToolRunner(calendar, "a")
        booked, _ = run(runner, "book_appointment", booking())
        calendar.event_types.clear()
        cal.overrides[("GET", "/v2/event-types/1001")] = seated_type(1001)
        moved, moved_error = run(runner, "reschedule_appointment", {"booking_id": booked["id"],
                                                                    "slot": "2026-11-04T10:00:00-06:00", "confirmed": True})
        cancelled, cancel_error = run(runner, "cancel_appointment", {"booking_id": booked["id"], "confirmed": True})
        assert moved_error and cancel_error and moved["code"] == cancelled["code"] == "unsupported_service"
        assert cal.calls("POST") == [("POST", "/v2/bookings")]

    def test_a_seat_in_a_shared_booking_is_reported_but_never_managed_by_phone(self, calendar, cal):
        # Documented seated create answer: uid is the whole group's booking, seatUid the caller's seat.
        cal.overrides[("POST", "/v2/bookings")] = lambda r: httpx.Response(201, json={"status": "success", "data": {
            "uid": "shared-group-booking", "seatUid": "caller-a-seat", "status": "accepted",
            "start": "2026-11-03T16:00:00.000Z", "attendees": []}})
        runner = ToolRunner(calendar, "a")
        data, error = run(runner, "book_appointment", booking())
        assert not error and data["status"] == "booked" and data["id"] is None
        with calendar.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM calcom_bookings").fetchone()[0] == 0
        assert run(runner, "my_appointments")[0]["appointments"] == []
        assert not [r for r in cal.requests if "shared-group-booking" in r[1]]


class TestBoundedOwnList:
    def test_own_list_is_capped_and_uses_the_caller_index(self, calendar, cal):
        with calendar.connect() as db:
            db.executemany("INSERT INTO calcom_bookings VALUES (?, ?, 'a', ?, 'consultation', 'now')",
                           [(f"ID{i:02d}", f"uid{i}", f"2026-11-{10 + i:02d}T10:00:00-06:00") for i in range(MAX_OWN_BOOKINGS + 4)])
        for i in range(MAX_OWN_BOOKINGS + 4):
            cal.bookings[f"uid{i}"] = {"uid": f"uid{i}", "status": "accepted", "start": f"2026-11-{10 + i:02d}T16:00:00.000Z"}
        listed, _ = run(ToolRunner(calendar, "a"), "my_appointments")
        assert [a["id"] for a in listed["appointments"]] == [f"ID{i:02d}" for i in range(MAX_OWN_BOOKINGS)]
        assert len(cal.calls("GET")) == MAX_OWN_BOOKINGS
        with calendar.connect() as db:
            plan = " ".join(r[3] for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT id, uid, start, service_id FROM calcom_bookings "
                "WHERE caller = ? AND start >= ? ORDER BY start, id LIMIT 5", ("a", "2026")))
            repeat = " ".join(r[3] for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT id, uid FROM calcom_bookings WHERE caller = ? AND start = ? AND service_id = ? "
                "ORDER BY id LIMIT 1", ("a", "2026", "consultation")))
        assert "calcom_bookings_caller_start" in plan and "TEMP B-TREE" not in plan
        assert "calcom_bookings_caller_start" in repeat and "TEMP B-TREE" not in repeat


def test_an_unfinished_business_file_is_refused(tmp_path, spec):
    with pytest.raises(ValueError, match="timezone"):
        CalComCalendar(tmp_path / "x.sqlite3", spec.business, KEY)


def test_every_cal_com_failure_code_has_a_recovery_rule_in_the_prompt(calendar, cal, spec):
    runner = ToolRunner(calendar, "a")
    codes = {run(runner, name, params)[0].get("code") for name, params in [
        ("available_slots", {}), ("book_appointment", booking(email="")), ("book_appointment", booking(email="nope")),
        ("cancel_appointment", {"booking_id": "NOPE", "confirmed": True})]}
    cal.free[1001].remove(TUESDAY_10)
    codes.add(run(runner, "book_appointment", booking())[0]["code"])
    cal.overrides[("POST", "/v2/bookings")] = lambda r: httpx.Response(400, json={"status": "error"})
    codes.add(run(runner, "book_appointment", booking(slot="2026-11-03T11:00:00-06:00"))[0]["code"])
    cal.overrides[("POST", "/v2/bookings")] = lambda r: httpx.Response(504)
    codes.add(run(runner, "book_appointment", booking(slot="2026-11-03T11:00:00-06:00"))[0]["code"])
    cal.overrides[("GET", "/v2/slots")] = lambda r: httpx.Response(500)
    codes.add(run(runner, "available_slots", {"service_id": "consultation"})[0]["code"])
    cal.overrides[("GET", "/v2/event-types/1002")] = seated_type(1002)
    codes.add(run(runner, "available_slots", {"service_id": "follow_up"})[0]["code"])
    assert codes == {"service_required", "email_required", "invalid_email", "not_found", "slot_taken", "booking_rejected",
                     "not_confirmed", "calendar_unavailable", "unsupported_service"}
    recovery = spec.prompt.split("## When a tool says no", 1)[1].split("##", 1)[0]
    assert [code for code in sorted(codes) if code not in recovery] == []


class TestCli:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch, cal, owner_spec):
        for key in ("CALENDAR", "CAL_API_KEY", "DATA_DIR", "AGENT_CONFIG_PATH"):
            monkeypatch.delenv(key, raising=False)
        real = httpx.Client
        monkeypatch.setattr(calcom.httpx, "Client", lambda **kwargs: real(**{**kwargs, "transport": httpx.MockTransport(cal)}))
        path = tmp_path / ".env"
        path.write_text(f"CAL_API_KEY={KEY}\nDATA_DIR={tmp_path / 'data'}\nAGENT_CONFIG_PATH={owner_spec.directory / 'agent.toml'}\n")
        return str(path)

    def test_calendar_check_reads_the_account_and_confirms_the_configuration(self, env, cal, capsys):
        cli.main(["--env-file", env, "calendar", "check"])
        report = json.loads(capsys.readouterr().out)
        assert report["username"] == "studio" and report["problems"] == []
        assert {t["id"] for t in report["event_types"]} == {1001, 1002}
        assert cal.calls() == [("GET", "/v2/me"), ("GET", "/v2/event-types")] and KEY not in json.dumps(report)

    def test_calendar_check_names_event_types_the_account_does_not_have(self, env, cal, capsys):
        cal.overrides[("GET", "/v2/event-types")] = lambda r: ok([EVENT_TYPES[1001]])
        with pytest.raises(SystemExit):
            cli.main(["--env-file", env, "calendar", "check"])
        assert "cal_event_type_id 1002" in capsys.readouterr().out

    def test_calendar_check_names_seated_event_types(self, env, cal, capsys):
        cal.overrides[("GET", "/v2/event-types")] = lambda r: ok([EVENT_TYPES[1001], {**EVENT_TYPES[1002], "seatsPerTimeSlot": 3}])
        with pytest.raises(SystemExit):
            cli.main(["--env-file", env, "calendar", "check"])
        report = json.loads(capsys.readouterr().out)
        assert [p for p in report["problems"] if "seats" in p] == [
            "cal_event_type_id 1002 has seats; seated event types are not supported (callers could cancel other "
            "attendees' seats)"]
        assert [t["seated"] for t in report["event_types"]] == [False, True]

    def test_tools_call_reads_real_times_but_writes_only_with_yes(self, env, cal, capsys):
        cli.main(["--env-file", env, "tools", "call", "available_slots", '{"service_id": "consultation"}'])
        assert json.loads(capsys.readouterr().out)["ok"] is True and ("GET", "/v2/slots") in cal.calls()
        with pytest.raises(SystemExit):
            cli.main(["--env-file", env, "tools", "call", "book_appointment", json.dumps(booking())])
        assert "--yes" in capsys.readouterr().err and cal.calls("POST") == []
