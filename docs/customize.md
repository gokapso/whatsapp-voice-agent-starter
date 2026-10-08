# Make it yours

Riley is a front desk for any business that takes appointments: a clinic, a salon, a studio, a
consultant, a repair shop. It answers questions from your business facts, finds real open times
in your Cal.com calendar, books after a clear yes, and lets callers check, move or cancel their
own bookings. You bring the facts and the calendar; the flow and the tools stay the same.

## 1. Your business and calendar

Fill in `agent/business.json` and connect Cal.com as described in
[`docs/calendar.md`](calendar.md). That is all the business data Riley uses:

- Facts (`business_name`, `description`, `hours`, `address`, `directions`, `website`,
  `policies`). Leave out anything you do not want said; Riley says it does not have it.
- Services: one Cal.com event type each. The event type's title, length and availability come
  from Cal.com.
- `timezone`: the time zone Riley speaks in.

Then run `uv run voice-agent calendar check` and fix what it reports.

## 2. Names, greeting and voice

In `agent/agent.toml`:

- `[agent] name` is the provider-side agent name. `assistant_name` is what the agent is called.
- `[greetings]`: the first words of each call. `{at_business}` adds " at <business_name>".
  Keep the AI disclosure; `voice-agent check` refuses a greeting without "AI".
- `[asr] keywords`: your business name, service names and other words callers say.
- `[voice]`, `llm`, `[turn]`, `[limits]`: voice, model and call limits.

`agent apply --yes` updates an existing agent only when the stored name matches. After a rename
it refuses; clear `ELEVENLABS_AGENT_ID` to create a new agent instead.

## 3. The prompt

`agent/prompt.md` has short sections. Keep the headings; the tests read "When a tool says no".

- **Role**: who the assistant is and the jobs it does. The server fills `{{business_name}}`,
  `{{today}}`, `{{weekday}}`, `{{timezone}}`, `{{recording_status}}` and `{{call_purpose}}` per
  call; keep them.
- **How to talk**: warm, short turns, one question at a time, no filler before tools, times said
  with the tool's `when` phrase.
- **Facts come from tools**: the assistant states business facts only from `business_info`.
- **Booking an appointment**: understand the need, find real times, get name (and email), read
  back, book only after a clear yes, confirm, ask if there is anything else.
- **The caller's own appointments**: review, move, cancel.
- **When a tool says no**: one recovery rule for every error code. If you add a code, add a rule;
  the tests fail otherwise.
- **Limits and closing**: what it cannot do, and how to end the call.

Keep these: the AI disclosure in the greetings, the recording sentence decided by the server, no
"one moment"/"let me check", no performed sounds, and booking only after a clear yes. They are
the rules in `AGENTS.md`, and tests enforce them. After any prompt change, listen to a real call
(`docs/testing.md`): only a conversation shows whether it sounds right.

## With an AI coding agent

Give your coding agent a request like this one, with your facts filled in:

```text
Read AGENTS.md, docs/customize.md and docs/calendar.md. Set Riley up as the AI front desk for
<business name>, a <what it is> in <time zone>.
Facts: <opening hours>, <address and directions>, <policies, e.g. how to cancel>.
Services and their Cal.com event type IDs: <service: event type ID>, ...
Ask for the caller's email: <yes/no>.
Fill in agent/business.json, add the business words to [asr] keywords in agent/agent.toml, and
keep the tools, the booking flow and every rule in AGENTS.md. Run `uv run voice-agent check`
and `uv run pytest`, and fix every failure without weakening a test. Then list the browser
checks from docs/testing.md that I should run, with example phrases for this business.
```

Review the diff before you apply the agent.

## Tools

The six tools follow a few rules that make them reliable for an LLM on a phone line. Keep them
when you add or change a tool:

- **Read before write.** `available_slots` returns the exact `slot` values that
  `book_appointment` and `reschedule_appointment` accept. The model copies values instead of
  building them, and Cal.com rejects anything that is not open.
- **Give the model the words to say.** Results carry a `when` phrase ("Tuesday, November 3 at
  10 AM"), so the model never works out a weekday from an ISO date.
- **Errors the model can fix.** Every failure is `{"ok": false, "code": ..., "message": ...}`,
  and the message says what to do next. Only `ok: true` means something changed.
- **The server enforces the rules that matter.** The caller identity comes from the call, never
  from arguments, so tools have no phone, name or email lookup. `confirmed` must be the boolean
  `true`. Retries with the same `tool_call_id` return the first result.
- **Fast, bounded and honest.** Calendar requests have short timeouts inside the tool's
  `response_timeout_secs` (15 s), every query has a limit, no transaction stays open across a
  network call, and a write whose result is unknown is reported as unknown.

To add a tool: add a Pydantic `Arguments` model and a `TOOLS` entry in `tools.py` (the
description is what the provider LLM reads), handle it in `ToolRunner.run` and in both calendars
(`calcom.py`, `store.py`), add a recovery rule for any new error code, and add tests.
`uv run voice-agent tools schema` shows what the provider will receive, and `agent verify`
reports drift after you apply it.

To use another calendar service, write a class with the same methods as `CalComCalendar`
(`info`, `availability`, `book`, `list_own`, `reschedule`, `cancel`, `now`, `timezone`,
`needs_email`), choose it in `open_calendar` (`app.py`), and test it with an
`httpx.MockTransport` the way `tests/test_calcom.py` does.
