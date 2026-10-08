"""The owner's business facts (agent/business.json) and the phrases tools give the model to say.

Both calendars (Cal.com in calcom.py, the local development calendar in store.py) answer
business_info from these facts. Empty values are allowed: the tool reports them as not
provided, so the agent says it does not have that information instead of guessing.
"""

from datetime import date, datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
SERVICE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
NOT_PROVIDED = "Not provided. Say you don't have that information; do not guess."
FACT_TOPICS = ("hours", "location", "policies")


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


class Business:
    """Facts from agent/business.json. Nothing here is invented: missing facts stay missing."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())

    @property
    def name(self):
        return str(self.data.get("business_name") or "").strip()

    @property
    def timezone_name(self):
        return str(self.data.get("timezone") or "").strip()

    @property
    def services(self):
        """Configured services: {"id", "cal_event_type_id", "notes"}. Entries with an empty id are
        template rows and are skipped."""
        return [s for s in self.data.get("services", []) if str(s.get("id") or "").strip()]

    @property
    def booking(self):
        return {"horizon_days": 30, "ask_for_email": True, **self.data.get("booking", {})}

    def text(self, key):
        value = self.data.get(key)
        if isinstance(value, list):
            value = [str(v).strip() for v in value if str(v).strip()]
            return value or NOT_PROVIDED
        return str(value or "").strip() or NOT_PROVIDED

    def facts(self, topic):
        sections = {
            "hours": {"hours": self.text("hours")},
            "location": {"address": self.text("address"), "directions": self.text("directions"),
                         "website": self.text("website")},
            "policies": {"policies": self.text("policies")},
        }
        if topic == "all":
            merged = {"business_name": self.name or NOT_PROVIDED, "description": self.text("description")}
            for section in sections.values():
                merged.update(section)
            return merged
        return sections[topic]

    def calcom_problems(self):
        """What must be filled in before the Cal.com calendar can take bookings."""
        problems = []
        try:
            ZoneInfo(self.timezone_name) if self.timezone_name else None
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(f"timezone {self.timezone_name!r} is not an IANA time zone such as America/New_York")
        if not self.timezone_name:
            problems.append("set timezone (IANA name, e.g. America/New_York; use your Cal.com profile time zone)")
        if not self.services:
            problems.append("add at least one service with an id and your Cal.com cal_event_type_id")
        ids = [s["id"] for s in self.services]
        for service in self.services:
            if not SERVICE_ID.fullmatch(service["id"]):
                problems.append(f"service id {service['id']!r} must be lowercase letters, digits, - or _ (max 40)")
            event_type = service.get("cal_event_type_id")
            if not isinstance(event_type, int) or isinstance(event_type, bool) or event_type <= 0:
                problems.append(f"service {service['id']!r} needs cal_event_type_id: the numeric ID of a Cal.com event type")
        if len(set(ids)) != len(ids):
            problems.append("service ids must be unique")
        horizon = self.booking["horizon_days"]
        if not isinstance(horizon, int) or not 1 <= horizon <= 90:
            problems.append("booking.horizon_days must be a whole number from 1 to 90")
        return problems
