"""Cal.com calendar (API v2): real availability, bookings, reschedules and cancellations.

Cal.com is the system of record for times and bookings. This server keeps one small private
SQLite table that maps each booking it made to the caller who made it (the HMAC caller key from
the call). Callers can list, move and cancel only bookings in that table, through a short local
id; Cal.com booking uids and attendee emails are never used to look anything up.

Rules, following the Cal.com API reference (https://cal.com/docs/api-reference/v2):
- Every request sends `Authorization: Bearer <api key>` and the `cal-api-version` each endpoint
  requires; booking start times are sent in UTC.
- Reads fail closed: an error, timeout or unexpected body becomes `calendar_unavailable`; no
  times are ever made up.
- Writes are sent once and never retried. A write that may have reached Cal.com without an answer
  (timeout, dropped connection, 5xx) becomes `not_confirmed`, never success.
- No SQLite transaction is open during a network call. Each transaction is one statement.
- Seated event types are not supported. Cal.com cancels or moves every seat of a seated booking
  when the owner's key sends no `seatUid`, so such services are refused before any write.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
import functools
import re
import secrets
import sqlite3
import threading
import time as clock_time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .business import failure, offered, spoken, spoken_day
from .private import private_sqlite

API = "https://api.cal.com"
# The version each endpoint requires today. Without it Cal.com falls back to an older behavior.
SLOTS_VERSION = "2024-09-04"
BOOKINGS_VERSION = "2026-02-25"
EVENT_TYPES_VERSION = "2026-06-12"
# Reads must finish well inside the tool's response_timeout_secs (tools.py), writes a little later.
READ_TIMEOUT = httpx.Timeout(4.0, connect=3.0)
WRITE_TIMEOUT = httpx.Timeout(9.0, connect=3.0)
SEARCH_DAYS = 14
MAX_SLOTS_READ = 1000
MAX_OFFERED_SLOTS = 2
MAX_OWN_BOOKINGS = 5
EVENT_TYPE_CACHE_SECONDS = 300
EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS calcom_bookings (
    id TEXT PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    caller TEXT NOT NULL,
    start TEXT NOT NULL,
    service_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calcom_bookings_caller_start ON calcom_bookings(caller, start, id);
"""

UNAVAILABLE = ("The calendar is not available right now, so nothing was looked up or changed. Say so briefly "
               "and offer to try once more. Never offer or confirm a time without a tool result.")
NOT_BY_PHONE = ("This service can't be booked by phone, so nothing was looked up or booked. Say so and offer a "
                "different service from business_info; do not try this one again.")
NOT_CHANGED_BY_PHONE = ("This appointment can't be changed by phone, so nothing was changed. Say so and suggest "
                        "the caller contact the business; do not try again.")
REQUESTED = "Requested, not confirmed yet: the business confirms this kind of appointment itself. Say so."


class CalendarUnavailable(Exception):
    pass


def utc(moment):
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value):
    """An ISO time with an offset (Cal.com sends `Z` or `+02:00`, sometimes with milliseconds)."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def seated(event_type):
    """True for a Cal.com event type with seats (several attendees share one booking)."""
    seats = event_type.get("seats")
    if isinstance(seats, dict) and seats.get("seatsPerTimeSlot") and not seats.get("disabled"):
        return True
    return bool(event_type.get("seatsPerTimeSlot"))


class CalComClient:
    """HTTP access to one Cal.com account. Thread-safe: tools run in worker threads."""

    def __init__(self, api_key, transport=None):
        if not api_key:
            raise ValueError("CAL_API_KEY is empty")
        self.http = httpx.Client(base_url=API, transport=transport, follow_redirects=False,
                                 headers={"Authorization": f"Bearer {api_key}"})

    def read(self, path, version=None, params=None):
        """GET and return `data`, None for 404, or raise CalendarUnavailable."""
        try:
            response = self.http.get(path, params=params, timeout=READ_TIMEOUT,
                                     headers={"cal-api-version": version} if version else {})
        except httpx.HTTPError as error:
            raise CalendarUnavailable(type(error).__name__) from None
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise CalendarUnavailable(f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise CalendarUnavailable("not JSON") from None
        if not isinstance(body, dict) or body.get("status") != "success":
            raise CalendarUnavailable("unexpected body")
        return body.get("data")

    def write(self, path, body):
        """POST once, never retried. Returns (outcome, value):
        ("ok", data) | ("refused", status) when Cal.com answered 4xx and changed nothing |
        ("not_sent", None) when the request never left | ("unknown", None) when it may have been applied."""
        try:
            response = self.http.post(path, json=body, timeout=WRITE_TIMEOUT,
                                      headers={"cal-api-version": BOOKINGS_VERSION})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return "not_sent", None
        except httpx.HTTPError:
            return "unknown", None
        if 400 <= response.status_code < 500:
            return "refused", response.status_code
        if not response.is_success:
            return "unknown", None
        try:
            data = response.json().get("data")
        except (ValueError, AttributeError):
            return "unknown", None
        if not isinstance(data, dict) or not data.get("uid"):
            return "unknown", None
        return "ok", data

    def account(self, configured=()):
        """Read-only setup report for `voice-agent calendar check`: the profile, its event types,
        and whether each configured event type ID exists."""
        me = self.read("/v2/me")
        if not isinstance(me, dict) or not me.get("username"):
            raise CalendarUnavailable("unexpected /v2/me body")
        types = self.read("/v2/event-types", EVENT_TYPES_VERSION, params={"username": me["username"]}) or []
        event_types = [{"id": t.get("id"), "slug": t.get("slug"), "title": t.get("title"),
                        "minutes": t.get("lengthInMinutes"), "hidden": t.get("hidden"), "seated": seated(t)}
                       for t in types[:100] if isinstance(t, dict)]
        known = {t["id"] for t in event_types}
        seated_ids = {t["id"] for t in event_types if t["seated"]}
        return {"username": me["username"], "profile_timezone": me.get("timeZone"), "event_types": event_types,
                "configured_not_found": [i for i in configured if i not in known],
                "configured_seated": [i for i in configured if i in seated_ids]}


def calendar_reads(method):
    """A failed calendar read reaches the agent as data it can act on, not as a crash."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except CalendarUnavailable:
            return failure("calendar_unavailable", UNAVAILABLE)
    return wrapper


class CalComCalendar:
    """The tools' calendar when CAL_API_KEY is set. Same methods as the local AppointmentStore."""

    def __init__(self, path, business, api_key, clock=None, transport=None):
        if problems := business.calcom_problems():
            raise ValueError("agent/business.json: " + "; ".join(problems))
        # Names stay in Cal.com; the local map holds the caller key, ids and times: 0600 anyway.
        self.path = private_sqlite(path)
        self.business = business
        self.timezone = ZoneInfo(business.timezone_name)
        self.services = {service["id"]: service for service in business.services}
        self.horizon_days = business.booking["horizon_days"]
        self.needs_email = business.booking["ask_for_email"] is True
        self.client = CalComClient(api_key, transport)
        self.clock = clock or (lambda: datetime.now(self.timezone))
        self.event_types, self.event_types_lock = {}, threading.Lock()
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

    def local(self, moment):
        return moment.astimezone(self.timezone).isoformat()

    # Services -------------------------------------------------------------------------------

    def event_type(self, service):
        """The Cal.com event type behind a service (title, length, description), cached briefly."""
        event_type_id = service["cal_event_type_id"]
        with self.event_types_lock:
            cached = self.event_types.get(event_type_id)
        if cached and clock_time.monotonic() - cached[0] < EVENT_TYPE_CACHE_SECONDS:
            return cached[1]
        data = self.client.read(f"/v2/event-types/{event_type_id}", EVENT_TYPES_VERSION)
        if not isinstance(data, dict) or not data.get("title"):
            raise CalendarUnavailable("event type not found")
        with self.event_types_lock:
            self.event_types[event_type_id] = (clock_time.monotonic(), data)
        return data

    def unsupported(self, service, message):
        """Refuse a seated event type before any slot read or write. `calendar check` reports it too."""
        if seated(self.event_type(service)):
            return failure("unsupported_service", message)
        return None

    def describe_service(self, service):
        event_type = self.event_type(service)
        return {"id": service["id"], "name": event_type["title"], "minutes": event_type.get("lengthInMinutes"),
                "description": str(event_type.get("description") or "")[:300], "notes": service.get("notes") or ""}

    @calendar_reads
    def info(self, topic="all"):
        if topic == "services":
            return {"ok": True, "services": [self.describe_service(s) for s in self.services.values()]}
        facts = self.business.facts(topic)
        if topic == "all":
            facts["services"] = [self.describe_service(s) for s in self.services.values()]
        return {"ok": True, **facts}

    # Availability ---------------------------------------------------------------------------

    def open_times(self, service, first, last):
        """Free start times from Cal.com between two local datetimes, as local ISO strings."""
        data = self.client.read("/v2/slots", SLOTS_VERSION, params={
            "eventTypeId": service["cal_event_type_id"], "start": utc(first), "end": utc(last),
            "timeZone": str(self.timezone)})
        if not isinstance(data, dict):
            raise CalendarUnavailable("unexpected slots body")
        times = set()
        for day_slots in data.values():
            for item in day_slots if isinstance(day_slots, list) else []:
                start = parse_time(item.get("start") if isinstance(item, dict) else item)
                if start and start > self.now():
                    times.add(start.astimezone(self.timezone))
                if len(times) >= MAX_SLOTS_READ:
                    break
        return [moment.isoformat() for moment in sorted(times)]

    def day_bounds(self, first_day, last_day):
        start = datetime.combine(first_day, time.min, self.timezone)
        return max(start, self.now()), datetime.combine(last_day + timedelta(days=1), time.min, self.timezone)

    def last_day(self):
        return self.now().date() + timedelta(days=self.horizon_days - 1)

    def pick_service(self, service_id):
        if not service_id:
            if len(self.services) == 1:
                return next(iter(self.services.values())), None
            return None, failure("service_required", "Ask what the caller needs, match it to a service from business_info, "
                                                     "and call again with its service_id.")
        if service_id not in self.services:
            return None, failure("unknown_service", "service_id must be one of: " + ", ".join(self.services) + ".")
        return self.services[service_id], None

    @calendar_reads
    def availability(self, service_id="", day="", after=""):
        service, problem = self.pick_service(service_id)
        if problem:
            return problem
        first_day = self.now().date()
        if day:
            try:
                first_day = date.fromisoformat(day)
            except ValueError:
                return failure("invalid_date", "Use the local date as YYYY-MM-DD, worked out from today's date.")
            if not self.now().date() <= first_day <= self.last_day():
                return failure("outside_horizon", f"Appointments can be checked only from today through "
                                                  f"{spoken_day(self.last_day())}. Ask the caller for a date in that range.")
        after_time = parse_time(after) if after else None
        if after and not after_time:
            return failure("invalid_arguments", "after must be an exact slot value from available_slots. Leave it empty "
                                                "for the first times.")
        if problem := self.unsupported(service, NOT_BY_PHONE):
            return problem
        first, last = self.day_bounds(first_day, min(first_day + timedelta(days=SEARCH_DAYS - 1), self.last_day()))
        free = [s for s in self.open_times(service, first, last) if not after_time or datetime.fromisoformat(s) > after_time]
        wanted = [s for s in free if not day or s.startswith(day)]
        result = {"ok": True, "service_id": service["id"], "service_name": self.event_type(service)["title"],
                  "timezone": str(self.timezone), "needs_email": self.needs_email,
                  "slots": [offered(s) for s in wanted[:MAX_OFFERED_SLOTS]], "has_more": len(wanted) > MAX_OFFERED_SLOTS}
        if day:
            result.update(day=day, day_spoken=spoken_day(first_day))
        if not wanted:
            # Nothing that day (closed or full): the soonest free time after it, or none in the search window.
            later = next((s for s in free if not day or s[:10] > day), None)
            result["next_available"] = offered(later) if later else None
        return result

    def still_open(self, service, moment):
        local = moment.astimezone(self.timezone)
        first, last = self.day_bounds(local.date(), local.date())
        return local.isoformat() in self.open_times(service, first, last)

    def refusal(self, service, moment, verb):
        """Cal.com said no (4xx) and changed nothing. Find out whether the time was taken."""
        try:
            taken = not self.still_open(service, moment)
        except CalendarUnavailable:
            taken = False
        if taken:
            return failure("slot_taken", f"Not {verb}. That time was just taken. Say so, call available_slots again "
                                         "and offer other times.")
        return failure("booking_rejected", f"Not {verb}. The calendar refused this request. Say so plainly and offer "
                                           "a different time; do not try the same request again.")

    def slot_time(self, slot):
        moment = parse_time(slot)
        if not moment or moment <= self.now() or moment.astimezone(self.timezone).date() > self.last_day():
            return None
        return moment

    # Bookings -------------------------------------------------------------------------------

    def own(self, caller, booking_id):
        with self.connect() as db:
            return db.execute("SELECT id, uid, start, service_id FROM calcom_bookings WHERE id = ? AND caller = ?",
                              (booking_id.strip().upper(), caller)).fetchone()

    def describe(self, booking_id, start, service_id, status="booked"):
        return {"id": booking_id, "slot": start, "when": spoken(start), "service_id": service_id, "status": status}

    @calendar_reads
    def book(self, caller, slot, name, service_id, confirmed, note="", email=""):
        if confirmed is not True:
            return failure("confirmation_required", "Not booked. Read back the service, day, time, name and email, and call "
                                                    "again with confirmed=true only after the caller clearly says yes.")
        if service_id not in self.services:
            return failure("unknown_service", "Not booked. service_id must be one of: " + ", ".join(self.services) + ".")
        email = email.strip()
        if self.needs_email and not email:
            return failure("email_required", "Not booked. This calendar needs the caller's email. Ask for it, spell it back, "
                                             "and call again after they confirm it.")
        if email and not EMAIL.fullmatch(email):
            return failure("invalid_email", "Not booked. That email does not look complete. Ask the caller to say it again, "
                                            "spell it back, and call again.")
        moment = self.slot_time(slot)
        if not moment:
            return failure("slot_unavailable", "Not booked. That is not an open appointment time. Call available_slots and "
                                               "use an exact slot value from its result.")
        start, service = self.local(moment), self.services[service_id]
        if problem := self.unsupported(service, NOT_BY_PHONE):
            return problem
        with self.connect() as db:
            mine = db.execute("SELECT id, uid FROM calcom_bookings WHERE caller = ? AND start = ? AND service_id = ? "
                              "ORDER BY id LIMIT 1", (caller, start, service_id)).fetchone()
        if mine and (repeated := self.repeated(mine, start, service_id, moment)):
            return repeated
        outcome, data = self.client.write("/v2/bookings", {
            "start": utc(moment), "eventTypeId": service["cal_event_type_id"],
            "attendee": {"name": name.strip(), "timeZone": str(self.timezone), "language": "en",
                         **({"email": email} if email else {})},
            "metadata": {"source": "kapso-voice-agent", **({"note": note.strip()} if note.strip() else {})}})
        if outcome == "ok":
            booking_id = secrets.token_hex(4).upper()
            confirmed_start = self.local(parse_time(data.get("start")) or moment)
            if data.get("seatUid"):
                # A seat in a shared booking (the event type became seated). The uid is the whole
                # group's, so it is never stored: no later move or cancel can reach other attendees.
                booking_id = None
            else:
                try:
                    with self.connect() as db:
                        db.execute("INSERT INTO calcom_bookings (id, uid, caller, start, service_id, created_at) "
                                   "VALUES (?, ?, ?, ?, ?, ?)",
                                   (booking_id, str(data["uid"]), caller, confirmed_start, service_id, self.now().isoformat()))
                except sqlite3.Error:
                    # The booking exists in Cal.com, so it must be reported as made. Only managing it by
                    # phone later is lost.
                    booking_id = None
            pending = data.get("status") == "pending"
            result = {"ok": True, **self.describe(booking_id, confirmed_start, service_id, "pending" if pending else "booked"),
                      "name": name.strip(), "timezone": str(self.timezone)}
            if pending:
                result["message"] = REQUESTED
            return result
        return self.write_failure(outcome, data, service, moment, "booked")

    def repeated(self, row, start, service_id, moment):
        """The caller asks again for a booking this server already made for them (same service and
        time). Answer from Cal.com's current state, never from the local map alone: None means it is
        not there anymore and a new booking may be made."""
        booking = self.booking(row["uid"])
        if booking is None or (isinstance(booking, dict) and booking.get("status") in ("cancelled", "rejected")):
            self.forget(row)
            return None
        if not isinstance(booking, dict) or booking.get("status") not in ("accepted", "pending"):
            raise CalendarUnavailable("unexpected booking body")
        if (now_at := parse_time(booking.get("start"))) and now_at != moment:
            return None  # moved in Cal.com: the caller has nothing at this time
        if booking["status"] == "pending":
            return {"ok": True, **self.describe(row["id"], start, service_id, "pending"),
                    "message": "Already requested in this calendar, not confirmed yet: the business confirms it "
                               "itself. Nothing new was booked. Say so."}
        return {"ok": True, **self.describe(row["id"], start, service_id, "already_booked")}

    def booking(self, uid):
        """The booking as Cal.com has it now, or None when Cal.com does not have it."""
        return self.client.read(f"/v2/bookings/{quote(uid, safe='')}", BOOKINGS_VERSION)

    def write_failure(self, outcome, status, service, moment, verb):
        if outcome == "not_sent" or status in (401, 403):
            return failure("calendar_unavailable", UNAVAILABLE)
        if outcome == "unknown":
            return failure("not_confirmed", f"The calendar did not confirm this, so it may or may not be {verb}. Do not say "
                                            "it worked and do not try it again in this call. Tell the caller you could not "
                                            "confirm it.")
        if service is None:
            return failure("booking_rejected", f"Not {verb}. The calendar refused this request. Say so plainly; do not try "
                                               "the same request again.")
        return self.refusal(service, moment, verb)

    @calendar_reads
    def list_own(self, caller):
        with self.connect() as db:
            rows = db.execute("SELECT id, uid, start, service_id FROM calcom_bookings "
                              "WHERE caller = ? AND start >= ? ORDER BY start, id LIMIT ?",
                              (caller, self.now().isoformat(), MAX_OWN_BOOKINGS)).fetchall()
        if not rows:
            return {"ok": True, "appointments": []}
        # The current state comes from Cal.com: a booking the business cancelled or moved is not listed.
        with ThreadPoolExecutor(max_workers=len(rows)) as pool:
            bookings = list(pool.map(lambda row: self.booking(row["uid"]), rows))
        appointments = []
        for row, booking in zip(rows, bookings, strict=True):
            if not isinstance(booking, dict) or booking.get("status") not in ("accepted", "pending"):
                continue
            start = self.local(parse_time(booking.get("start")) or datetime.fromisoformat(row["start"]))
            appointments.append(self.describe(row["id"], start, row["service_id"],
                                              "pending" if booking["status"] == "pending" else "booked"))
        return {"ok": True, "appointments": appointments}

    @calendar_reads
    def reschedule(self, caller, booking_id, slot, confirmed):
        if confirmed is not True:
            return failure("confirmation_required", "Not moved. Read back the old and new day and time, and call again "
                                                    "with confirmed=true only after the caller clearly says yes.")
        row = self.own(caller, booking_id)
        if not row:
            return failure("not_found", "Nothing moved. No appointment with that id for this caller. "
                                        "Call my_appointments and use an id from it.")
        moment = self.slot_time(slot)
        if not moment:
            return failure("slot_unavailable", "Not moved. That is not an open appointment time. Call available_slots and "
                                               "use an exact slot value from its result.")
        if self.local(moment) == row["start"]:
            return {"ok": True, **self.describe(row["id"], row["start"], row["service_id"], "unchanged")}
        service = self.services.get(row["service_id"])
        if service and (problem := self.unsupported(service, NOT_CHANGED_BY_PHONE)):
            return problem
        outcome, data = self.client.write(f"/v2/bookings/{quote(row['uid'], safe='')}/reschedule",
                                          {"start": utc(moment), "reschedulingReason": "Moved by the caller on a phone call."})
        if outcome == "refused" and data == 404:
            self.forget(row)
            return failure("not_found", "Nothing moved: that appointment is no longer in the calendar. "
                                        "Call my_appointments to see what is booked.")
        if outcome != "ok":
            return self.write_failure(outcome, data, service, moment, "moved")
        start = self.local(parse_time(data.get("start")) or moment)
        try:
            if data.get("seatUid"):
                self.forget(row)  # now a seat in a shared booking: never managed by phone again
            else:
                with self.connect() as db:
                    # Cal.com gives the moved booking a new uid; the caller keeps the same short id.
                    db.execute("UPDATE calcom_bookings SET uid = ?, start = ? WHERE id = ? AND caller = ?",
                               (str(data["uid"]), start, row["id"], caller))
        except sqlite3.Error:
            pass  # moved in Cal.com, which is what the caller asked for; only later phone changes are lost
        # A booking the business must confirm stays pending after a move (Cal.com reschedule docs).
        pending = data.get("status") == "pending"
        result = {"ok": True, **self.describe(row["id"], start, row["service_id"], "pending" if pending else "rescheduled"),
                  "previous_when": spoken(row["start"])}
        if pending:
            result["message"] = ("Moved, but the new time is requested, not confirmed yet: the business confirms it "
                                 "itself. Say so.")
        return result

    @calendar_reads
    def cancel(self, caller, booking_id, confirmed):
        if confirmed is not True:
            return failure("confirmation_required", "Not cancelled. Confirm with the caller which appointment to cancel, "
                                                    "and call again with confirmed=true only after a clear yes.")
        row = self.own(caller, booking_id)
        if not row:
            # Same answer whether the ID is unknown or belongs to someone else.
            return failure("not_found", "Nothing cancelled. No appointment with that id for this caller. "
                                        "Call my_appointments and use an id from it.")
        service = self.services.get(row["service_id"])
        if service and (problem := self.unsupported(service, NOT_CHANGED_BY_PHONE)):
            return problem  # without a seatUid this cancel would remove every attendee
        outcome, data = self.client.write(f"/v2/bookings/{quote(row['uid'], safe='')}/cancel",
                                          {"cancellationReason": "Cancelled by the caller on a phone call."})
        if outcome == "refused" and data == 404:
            self.forget(row)
            return failure("not_found", "Nothing cancelled: that appointment is no longer in the calendar. "
                                        "Call my_appointments to see what is booked.")
        if outcome != "ok":
            return self.write_failure(outcome, data, None, None, "cancelled")
        self.forget(row)
        return {"ok": True, **self.describe(row["id"], row["start"], row["service_id"], "cancelled")}

    def forget(self, row):
        with self.connect() as db:
            db.execute("DELETE FROM calcom_bookings WHERE id = ?", (row["id"],))
