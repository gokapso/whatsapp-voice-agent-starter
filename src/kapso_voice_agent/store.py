"""Fictional appointment book in SQLite.

Every read is bounded: slot lookups use a range on the unique slot index limited by the booking
horizon, and caller lookups use the (caller, slot) index with LIMIT. Transactions hold one
statement and never wrap network calls. The UNIQUE(slot) constraint, not a read-then-write
check, decides who gets a contested slot.
"""

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import secrets
import sqlite3
from zoneinfo import ZoneInfo

from .private import private_sqlite

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
MAX_OWN_BOOKINGS = 10
MAX_OFFERED_SLOTS = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    slot TEXT NOT NULL UNIQUE,
    caller TEXT NOT NULL,
    name TEXT NOT NULL,
    service_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS appointments_caller_slot ON appointments(caller, slot);
"""


def failure(code, message):
    return {"ok": False, "code": code, "message": message}


def spoken_day(day: date):
    return f"{WEEKDAYS[day.weekday()].capitalize()}, {MONTHS[day.month - 1]} {day.day}"


def spoken(slot):
    """How the agent should say a slot: "Tuesday, November 3 at 10 AM". The model copies this
    phrase, so it never has to work out a weekday from an ISO date."""
    moment = datetime.fromisoformat(slot)
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{spoken_day(moment.date())} at {moment.hour % 12 or 12}{minutes} {'AM' if moment.hour < 12 else 'PM'}"


def offered(slot):
    """A slot as tools return it: the exact value to book with, and the phrase to say."""
    return {"slot": slot, "when": spoken(slot)}


class AppointmentStore:
    def __init__(self, path, business_path, clock=None):
        # Names and notes are personal data: the file (and its journal) is 0600 under any umask.
        self.path = private_sqlite(path)
        self.business = json.loads(Path(business_path).read_text())
        self.timezone = ZoneInfo(self.business["timezone"])
        self.services = {service["id"]: service for service in self.business["services"]}
        self.policy = self.business["appointments"]
        self.clock = clock or (lambda: datetime.now(self.timezone))
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def now(self):
        return self.clock().astimezone(self.timezone)

    # Business facts -------------------------------------------------------------------------

    def info(self, topic="all"):
        b = self.business
        sections = {
            "services": {"services": [{k: s[k] for k in ("id", "name", "minutes", "description")} for s in b["services"]],
                         "appointments": {k: self.policy[k] for k in ("kind", "length_minutes", "explanation")}},
            "hours": {"weekly_hours": {day: [f"{a}-{z}" for a, z in ranges] for day, ranges in b["weekly_hours"].items()},
                      "timezone": b["timezone"]},
            "closures": {"upcoming_closures": [c for c in b["closures"] if c["end"] >= self.now().date().isoformat()]},
            "location": {"location": b["location"], "contact": b["contact"]},
            "policies": {"policies": b["policies"]},
        }
        if topic == "all":
            merged = {"ok": True, "business_name": b["business_name"], "description": b["description"]}
            for section in sections.values():
                merged.update(section)
            return merged
        return {"ok": True, **sections[topic]}

    def closed_reason(self, day: date):
        iso = day.isoformat()
        for closure in self.business["closures"]:
            if closure["start"] <= iso <= closure["end"]:
                return closure["reason"]
        if not self.business["weekly_hours"][WEEKDAYS[day.weekday()]]:
            return f"Closed on {WEEKDAYS[day.weekday()].capitalize()}s"
        return ""

    def slot_inventory(self):
        """Every bookable start time in the horizon, ignoring existing bookings. Bounded by policy."""
        now = self.now()
        earliest = now + timedelta(minutes=self.policy["min_notice_minutes"])
        slots = []
        for offset in range(self.policy["horizon_days"]):
            day = now.date() + timedelta(days=offset)
            if self.closed_reason(day):
                continue
            for start in self.policy["slot_starts"].get(WEEKDAYS[day.weekday()], []):
                slot = datetime.combine(day, time.fromisoformat(start), self.timezone)
                if slot > earliest:
                    slots.append(slot.isoformat())
        return slots

    def taken_slots(self, first, last):
        with self.connect() as db:
            rows = db.execute("SELECT slot FROM appointments WHERE slot >= ? AND slot <= ?", (first, last))
            return {row[0] for row in rows}

    # Tools ----------------------------------------------------------------------------------

    def availability(self, day="", after=""):
        result = {"ok": True, "timezone": self.business["timezone"], "slots": []}
        if day:
            try:
                wanted = date.fromisoformat(day)
            except ValueError:
                return failure("invalid_date", "Use the local date as YYYY-MM-DD, worked out from today's date.")
            days_ahead = (wanted - self.now().date()).days
            if not 0 <= days_ahead < self.policy["horizon_days"]:
                last = self.now().date() + timedelta(days=self.policy["horizon_days"] - 1)
                return failure("outside_horizon", f"Appointments can be checked only from today through {spoken_day(last)}. "
                                                  "Ask the caller for a date in that range.")
            result.update(day=day, day_spoken=spoken_day(wanted))
            if reason := self.closed_reason(wanted):
                upcoming = next((s[:10] for s in self.slot_inventory() if s[:10] > day), None)
                return {**result, "open": False, "closed_reason": reason, "next_open_day": upcoming,
                        "next_open_day_spoken": spoken_day(date.fromisoformat(upcoming)) if upcoming else None}
            result["open"] = True
        inventory = self.slot_inventory()
        if not inventory:
            return result
        taken = self.taken_slots(inventory[0], inventory[-1])
        free = [s for s in inventory if s not in taken and (not after or s > after)]
        wanted_free = [s for s in free if not day or s.startswith(day)]
        result["slots"] = [offered(s) for s in wanted_free[:MAX_OFFERED_SLOTS]]
        result["has_more"] = len(wanted_free) > MAX_OFFERED_SLOTS
        if day and not wanted_free:
            # Open but fully booked (or no later times): point at the soonest free time after that day.
            later = next((s for s in free if s[:10] > day), None)
            result["next_available"] = offered(later) if later else None
        return result

    def booking_problem(self, slot, service_id, confirmed):
        """Why a booking may not be made, or None. A store backed by a real calendar reuses these
        checks before it calls the calendar (docs/customize.md)."""
        if confirmed is not True:
            return failure("confirmation_required", "Not booked. Read back the service, day, time and name, and call again "
                                                    "with confirmed=true only after the caller clearly says yes.")
        if service_id not in self.services:
            return failure("unknown_service", "Not booked. service_id must be one of: " + ", ".join(self.services) + ".")
        if slot not in self.slot_inventory():
            return failure("slot_unavailable", "Not booked. That is not an open appointment time. Call available_slots and "
                                               "use an exact slot value from its result.")
        return None

    def book(self, caller, slot, name, service_id, confirmed, note=""):
        if problem := self.booking_problem(slot, service_id, confirmed):
            return problem
        booking_id = secrets.token_hex(4).upper()
        name = name.strip()
        try:
            with self.connect() as db:
                db.execute("INSERT INTO appointments (id, slot, caller, name, service_id, note, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (booking_id, slot, caller, name, service_id, note.strip(), self.now().isoformat()))
        except sqlite3.IntegrityError:
            with self.connect() as db:
                own = db.execute("SELECT id, slot, name, service_id, note FROM appointments WHERE slot = ? AND caller = ?",
                                 (slot, caller)).fetchone()
            if own:
                return {"ok": True, "status": "already_booked", **self.describe(own)}
            return failure("slot_taken", "Not booked. Another caller just took that time. Say so, call available_slots "
                                         "again and offer other times.")
        return {"ok": True, "status": "booked", "id": booking_id, "slot": slot, "when": spoken(slot),
                "timezone": self.business["timezone"], "name": name, "service_id": service_id,
                "service_name": self.services[service_id]["name"], "fictional": True}

    def list_own(self, caller):
        with self.connect() as db:
            rows = db.execute("SELECT id, slot, name, service_id, note FROM appointments "
                              "WHERE caller = ? AND slot >= ? ORDER BY slot, id LIMIT ?",
                              (caller, self.now().isoformat(), MAX_OWN_BOOKINGS)).fetchall()
        return {"ok": True, "appointments": [self.describe(row) for row in rows]}

    def cancel(self, caller, booking_id, confirmed):
        if confirmed is not True:
            return failure("confirmation_required", "Not cancelled. Confirm with the caller which appointment to cancel, "
                                                    "and call again with confirmed=true only after a clear yes.")
        booking_id = booking_id.strip().upper()
        with self.connect() as db:
            # One statement: delete only this caller's row and return what was removed.
            # fetchall finishes the statement before the commit; id is the primary key, so at most one row.
            rows = db.execute("DELETE FROM appointments WHERE id = ? AND caller = ? RETURNING id, slot, name, service_id, note",
                              (booking_id, caller)).fetchall()
        if not rows:
            # Same answer whether the ID is unknown or belongs to someone else.
            return failure("not_found", "Nothing cancelled. No appointment with that id for this caller. "
                                        "Call my_appointments and use an id from it.")
        return {"ok": True, "status": "cancelled", **self.describe(rows[0])}

    def describe(self, row):
        booking = dict(row)
        service = self.services.get(booking["service_id"])
        booking["service_name"] = service["name"] if service else booking["service_id"]
        booking["when"] = spoken(booking["slot"])
        return booking
