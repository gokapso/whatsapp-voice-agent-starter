"""In-memory operator event log. Names and opaque references only: no audio, SDP, transcripts,
phone numbers, raw Meta call IDs or credentials."""

from collections import deque
from datetime import datetime, timezone
import hashlib
import re

SAFE_EVENT = re.compile(r"^[a-z0-9_]{1,64}$")


def call_ref(call_id):
    """Short stable reference for a Meta call ID, safe to show and log."""
    if call_id.startswith("browser-"):
        return call_id
    return "call-" + hashlib.sha256(call_id.encode()).hexdigest()[:16]


class EventLog:
    def __init__(self, size=200):
        self.events = deque(maxlen=size)

    def add(self, event, ref=None, **fields):
        event = event.lower()
        if not SAFE_EVENT.match(event):
            event = "invalid_event_name"
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": event}
        if ref:
            entry["ref"] = ref
        for key, value in fields.items():
            # Only small scalar metadata (status names, durations, counts).
            if isinstance(value, (int, float, bool)) or (isinstance(value, str) and len(value) <= 64):
                entry[key] = value
        self.events.append(entry)
