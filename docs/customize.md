# Make it yours

Riley is a front desk for any business that takes appointments: a clinic, a salon, a studio, a
repair shop. It answers questions from your business facts and finds real open times in your
Cal.com calendar. It books after a clear yes, and callers can check, move or cancel their own
bookings. You bring the facts and the calendar. The flow and the tools stay the same.

## 1. Connect Cal.com

Cal.com decides which times are open (schedules, buffers, minimum notice, existing events).
Every booking Riley makes is a normal Cal.com booking. The integration is one file,
`src/kapso_voice_agent/calcom.py`, on the [Cal.com API v2](https://cal.com/docs/api-reference/v2/introduction).

1. In Cal.com, create one event type for each service that callers can book. Riley says its
   title and length, and offers its availability. Do not use seated event types: Riley refuses
   them, because a change to one seat changes every attendee.
2. Create an API key under **Settings > Security**. `uv run voice-agent setup` stores it as
   `CAL_API_KEY`.
3. Fill in `agent/business.json`:

   ```json
   {
     "business_name": "Your business",
     "timezone": "America/New_York",
     "description": "One sentence about what you do.",
     "hours": "Monday to Friday, 9 AM to 5 PM.",
     "address": "Street, city",
     "directions": "",
     "website": "",
     "policies": ["Please arrive five minutes early."],
     "services": [
       {"id": "consultation", "cal_event_type_id": 123456, "notes": "For new clients."}
     ],
     "booking": {"horizon_days": 30, "ask_for_email": true}
   }
   ```

   - `timezone` is the time zone Riley speaks in. Use the time zone of your Cal.com profile.
   - `services[].id` is a short name for the agent. `cal_event_type_id` is the event type it
     books. `notes` (optional) says when to suggest it.
   - Leave a fact empty if Riley must not state it. Riley then says it does not have it.
   - `ask_for_email`: Cal.com event types need an email by default, so Riley asks for it and
     spells it back. Set `false` only if your event types do not need one.

4. Check the setup. This command only reads from Cal.com:

   ```sh
   uv run voice-agent calendar check
   ```

   It lists your event types with their IDs. It also lists each problem: a missing time zone, an
   unknown or seated event type, or a time zone that is not the one in Cal.com.

5. Try the tools. Reads are safe. `tools call` refuses to book, move or cancel without `--yes`,
   because these change your real calendar.

   ```sh
   uv run voice-agent tools call available_slots '{"service_id": "consultation"}'
   ```

Restart the server after you change `.env` or `business.json`.

Cal.com sends its usual emails for each booking. The caller's name, email and note go to
Cal.com. Say so in your privacy notice.

`CALENDAR=local` is an offline development calendar (`agent/dev-calendar.json`, `store.py`).
Its bookings are not in any real calendar. Never use it for real callers.

## 2. Names, greeting and voice

In `agent/agent.toml`:

- `[agent] name` is the agent name at the provider. `assistant_name` is what callers hear.
- `[greetings]` are the first words of each call. `{at_business}` adds " at <business_name>".
  `voice-agent check` refuses a greeting without "AI".
- `[asr] keywords`: your business name, service names and other words callers say.
- `[voice]`, `llm`, `[turn]`, `[limits]`: the ElevenLabs voice, model and call limits.
  `[grok]`: the Grok Voice model and voice.

With ElevenLabs, `uv run voice-agent agent apply --yes` sends the changes. It updates an agent
only when the stored name matches. `uv run voice-agent agent verify` reads the agent back and
reports drift. Grok Voice reads these files on every call, so there is nothing to apply.

## 3. The prompt

`agent/prompt.md` has short sections. Keep the headings: the tests read "When a tool says no".
Keep the `{{business_name}}`, `{{today}}`, `{{weekday}}`, `{{timezone}}`, `{{recording_status}}`
and `{{call_purpose}}` variables. The server fills them for each call.

Keep these rules. `AGENTS.md` lists them, and tests enforce them:

- The greetings say the caller talks to an AI.
- The server decides the recording sentence.
- No "one moment" or "let me check", and no performed sounds.
- Book, move or cancel only after a clear yes.
- Each tool error code has a recovery rule in "When a tool says no".

After a prompt change, listen to a real call ([`testing.md`](testing.md)). Only a conversation
shows if it sounds right.

## With an AI coding agent

Give your coding agent a request like this one, with your facts:

```text
Read AGENTS.md and docs/customize.md. Set Riley up as the AI front desk for <business name>,
a <what it is> in <time zone>.
Facts: <opening hours>, <address and directions>, <policies, for example how to cancel>.
Services and their Cal.com event type IDs: <service: event type ID>, ...
Ask for the caller's email: <yes/no>.
Fill in agent/business.json, add the business words to [asr] keywords in agent/agent.toml, and
keep the tools, the booking flow and every rule in AGENTS.md. Run `uv run voice-agent check`
and `uv run pytest`, and fix every failure without weakening a test. Then list the browser
checks from docs/testing.md that I should run, with example phrases for this business.
```

Review the diff before you apply it.

## Tools

The six tools are `business_info`, `available_slots`, `my_appointments`, `book_appointment`,
`reschedule_appointment` and `cancel_appointment`. They run on your server. Keep these rules
when you change a tool:

- **Read before write.** `available_slots` returns exact `slot` values. The model copies them
  into `book_appointment`, and Cal.com refuses a time that is not open.
- **Give the model the words to say.** Results carry a `when` phrase ("Tuesday, November 3 at
  10 AM"), so the model never works out a weekday.
- **Errors the model can fix.** A failure is `{"ok": false, "code": ..., "message": ...}`, and
  the message says what to do next. Only `ok: true` means something changed.
- **The server enforces the important rules.** The caller identity comes from the call, never
  from arguments. `confirmed` must be the boolean `true`. A retried `tool_call_id` returns the
  first result. A booking request is sent once, and an unknown result is `not_confirmed`.
- **Fast and bounded.** Calendar requests have short timeouts, every query has a limit, and no
  transaction stays open during a network request.

To add a tool:

1. Add a Pydantic `Arguments` model and a `TOOLS` entry in `tools.py`.
2. Handle it in `ToolRunner.run` and in both calendars (`calcom.py`, `store.py`).
3. Add a recovery rule in the prompt for each new error code, and add tests.
4. Run `uv run voice-agent tools schema` to see what the provider gets.

To use another calendar service, write a class with the methods of `CalComCalendar`. Choose it
in `open_calendar` (`app.py`). Test it with an `httpx.MockTransport`, as `tests/test_calcom.py`
does.
