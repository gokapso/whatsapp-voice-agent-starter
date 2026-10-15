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


# Why the voice provider last refused a session, for the operator console. A short code only;
# never the provider's message, which can echo request details.
PROVIDER_PROBLEMS = ("auth", "billing", "rate_limited", "not_found", "unreachable", "failed")
# Events that mean a call did not go as planned. A failed tool result (for example slot_taken) is
# a normal answer, not a problem with the call.
CALL_PROBLEM = re.compile(r"^(provider_problem_|agent_session_not_ready|agent_(start|receive|audio_send)_failed_|call_failed_"
                          r"|answer_timed_out|rtc_connect_timed_out|outbound_failed_|outbound_connect_outcome_unknown|reject_|inbound_rejected_busy)")


class EventLog:
    def __init__(self, size=200):
        self.events = deque(maxlen=size)
        self.provider_problem = None

    def provider_failed(self, problem, ref=None):
        problem = problem if problem in PROVIDER_PROBLEMS else "failed"
        self.provider_problem = {"problem": problem, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.add("provider_problem_" + problem, ref)

    def provider_ok(self):
        self.provider_problem = None

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

    def recent_calls(self, active=(), limit=6):
        """The calls this log still holds (its last 200 events), newest first. Built from event
        names only: no call history is stored anywhere."""
        calls = {}
        for entry in self.events:
            ref, name = entry.get("ref"), entry["event"]
            if not ref or name == "native_artifact_event":
                continue
            call = calls.setdefault(ref, {"ref": ref, "kind": "browser" if ref.startswith("browser-") else "whatsapp_inbound",
                                          "started_at": entry["at"], "connected_at": None, "last_at": entry["at"],
                                          "media": None, "tools": 0, "problem": False})
            call["last_at"] = entry["at"]
            if name.startswith("outbound_"):
                call["kind"] = "whatsapp_outbound"
            if name == "rtc_connected" and not call["connected_at"]:
                call["connected_at"], call["media"] = entry["at"], entry.get("media")
            elif name.startswith("tool_") and name != "tool_end_call_ok":
                call["tools"] += 1
            elif CALL_PROBLEM.match(name):
                call["problem"] = True
        newest = list(calls.values())[::-1][:limit]  # the log is in time order, so first seen = started first
        for call in newest:
            call["active"] = call["ref"] in active
        return newest
