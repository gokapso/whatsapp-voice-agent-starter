# Make it yours

Riley is one concrete front desk: answer questions from business data, find a time, book it
after a clear yes, and let callers review or cancel their own bookings. Most front desks (a
clinic, a salon, a repair shop, a tutor) fit the same shape, so the quickest path is to keep the
flow and tools and replace the business.

## With an AI coding agent

Give your coding agent a request like this one, with your facts filled in:

```text
Read AGENTS.md and docs/customize.md. Turn Riley into the AI front desk for <business name>,
a <what it is> in <city / time zone>.
Facts: <opening hours>, <closed days and holidays>, <services, each with a short description
and length>, <appointment length and how far ahead people can book>, <policies, e.g. prices
are given in person>, <address and parking>.
The assistant's name is <name>. Keep the five tools, the booking flow and every rule in
AGENTS.md. Update the tests that use bike-shop data. Run `uv run voice-agent check` and
`uv run pytest`, and fix every failure without weakening a test. Then list the browser
checks from docs/testing.md that I should run, with example phrases for this business.
```

Review the diff before you apply the agent. The tests catch broken rules (no AI disclosure,
filler in greetings, tools with identity arguments, an error code without a recovery rule in the
prompt), but only a real conversation shows whether the new prompt sounds right.

## By hand

Change things in this order, and run the command after each step:

| Step | File | Check with |
| --- | --- | --- |
| 1. Business facts | `agent/business.json`: time zone, hours, `slot_starts`, `horizon_days`, `min_notice_minutes`, services, closures, policies | `uv run voice-agent tools call available_slots '{}'` |
| 2. Names and greetings | `agent/agent.toml`: `[agent]` names, `[greetings]`, `[asr] keywords` | `uv run voice-agent check` |
| 3. Prompt | `agent/prompt.md` | `uv run pytest tests/test_agent_config.py tests/test_tools.py` |
| 4. Voice and model | `agent/agent.toml`: `[voice]`, `llm`, `[turn]`, `[limits]` | `uv run voice-agent agent plan` |
| 5. Provider tests | `agent/provider-tests/*.json`: new business words in the chat histories | `uv run pytest tests/test_agent_config.py` |
| 6. Real check | `agent apply --yes`, `agent verify`, a browser call | `docs/testing.md` level 2 |

`agent.name` is the provider-side name. `agent apply --yes` updates an existing agent only when
the stored name matches, so after a rename it refuses; clear `ELEVENLABS_AGENT_ID` to create a
new agent instead.

## The prompt

`agent/prompt.md` has short sections. Keep the headings; the tests read "When a tool says no".

- **Role**: who the assistant is, the business, and the few jobs it does. Keep it short and
  concrete. The `{{today}}`, `{{timezone}}`, `{{recording_status}}` and `{{call_purpose}}`
  variables are filled per call by the server; keep them.
- **How to talk**: phone manners. One question at a time, short turns, no filler before tools,
  say times with the tool's `when` phrase.
- **Facts come from tools**: the assistant states business facts only from `business_info`.
- **Booking an appointment**: the numbered flow. Look up times before offering them, read back
  everything in one sentence, book only after a clear yes, and report the tool's result.
- **The caller's own appointments**: review, cancel, reschedule.
- **When a tool says no**: one recovery rule for every error code. If you add a code, add a rule;
  `tests/test_tools.py` fails otherwise.
- **Limits and closing**: what it cannot do, and how to end the call.

Keep these: the AI disclosure in the greetings, the recording sentence decided by the server, no
"one moment"/"let me check", no performed sounds, and booking only after a clear yes. They are
the rules in `AGENTS.md`, and tests enforce them.

## Tools

The five tools follow a few rules that make them reliable for an LLM on a phone line. Keep them
when you add or change a tool:

- **Read before write.** `available_slots` returns the exact `slot` values that
  `book_appointment` accepts. The model copies values instead of building them, and the store
  rejects anything that is not an open time.
- **Give the model the words to say.** Results carry a `when` phrase ("Tuesday, November 3 at
  10 AM"), so the model never works out a weekday from an ISO date.
- **Errors the model can fix.** Every failure is `{"ok": false, "code": ..., "message": ...}`,
  and the message says what to do next ("Not booked. Another caller just took that time. Say so,
  call available_slots again and offer other times."). Only `ok: true` means something changed.
- **The server enforces the rules that matter.** The caller identity comes from the call, never
  from arguments, so tools have no phone or name parameters. `confirmed` must be the boolean
  `true`. Retries with the same `tool_call_id` return the first result, and the unique slot
  index stops double bookings.
- **Fast and bounded.** Tools answer in well under `response_timeout_secs` (10 s), every query
  has a limit, and no transaction stays open across a network call.

To add a tool: add a Pydantic `Arguments` model and a `TOOLS` entry in `tools.py` (the
description is what the provider LLM reads), handle it in `ToolRunner.run`, add a recovery rule
for any new error code, and add tests. `uv run voice-agent tools schema` shows what the provider
will receive, and `agent verify` reports drift after you apply it.

## Recipe: book into a real calendar

The SQLite store is a working default with fictional bookings. To book into a real calendar,
replace the source of busy times and the booking write, and keep everything else. This is the
same idea as the optional calendar backend in LiveKit's
[frontdesk example](https://github.com/livekit/agents/tree/bcdf90771c38fbbc275fd835a6dafa7ae5200685/examples/frontdesk):
the agent code stays the same and a calendar class does the I/O.

The sketch below is not part of the tested code. `calendar.example.com` stands for your
calendar's API; read its documentation for the real routes, time format and error codes.

```python
# src/kapso_voice_agent/calendar_store.py (sketch)
import hashlib

import httpx

from .store import AppointmentStore, failure


class CalendarStore(AppointmentStore):
    """Busy times and bookings come from a real calendar. Business facts, the booking rules and
    caller ownership stay local: the calendar does not know WhatsApp callers."""

    def __init__(self, path, business_path, api_key, clock=None):
        super().__init__(path, business_path, clock)
        # Well under the tool's response_timeout_secs (10 s), and no redirects.
        self.http = httpx.Client(base_url="https://calendar.example.com/v1", timeout=5, follow_redirects=False,
                                 headers={"Authorization": f"Bearer {api_key}"})

    def taken_slots(self, first, last):
        response = self.http.get("/busy", params={"from": first, "to": last})
        response.raise_for_status()  # any exception reaches the agent as internal_error
        # Return start times in the same ISO format as slot_inventory(), or nothing will match.
        return {busy["start"] for busy in response.json()["busy"]}

    def book(self, caller, slot, name, service_id, confirmed, note=""):
        if problem := self.booking_problem(slot, service_id, confirmed):
            return problem
        response = self.http.post("/events", json={
            "start": slot, "minutes": self.policy["length_minutes"],
            "title": f"{self.services[service_id]['name']} - {name}"},
            # A retried tool call must not create a second event.
            headers={"Idempotency-Key": hashlib.sha256(f"{caller}|{slot}".encode()).hexdigest()})
        if response.status_code == 409:
            return failure("slot_taken", "Not booked. Another caller just took that time. Say so, call "
                                         "available_slots again and offer other times.")
        response.raise_for_status()
        # Keep the local row: it records which caller owns the booking (my_appointments, cancel).
        return super().book(caller, slot, name, service_id, confirmed, note)
```

Then:

1. Store the calendar's event ID with the local row (add an `external_id` column). In `cancel`,
   delete the remote event before the local row. If `super().book` refuses after the calendar
   accepted, delete the event you just created.
2. Choose the store in `create_app` (`app.py`) when a new `CALENDAR_API_KEY` setting is present,
   and add that setting to `config.py` and `.env.example`.
3. Add tests with `httpx.MockTransport`, like the Kapso tests in `tests/conftest.py`: a busy
   time is not offered, a 409 becomes `slot_taken`, a timeout becomes `internal_error`, and a
   retried `tool_call_id` creates one event. Keep real calendar calls out of `uv run pytest`.
4. Put the calendar's own caller data rules in your privacy notice: names and notes now leave
   this server.
