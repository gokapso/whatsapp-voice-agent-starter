# Calendar: Cal.com

Riley books into your real [Cal.com](https://cal.com) calendar. Cal.com decides which times are
open (your schedules, buffers, minimum notice and existing events), and every booking Riley
makes is a normal Cal.com booking: it shows up in your Cal.com bookings list and in any calendar
you connected to Cal.com.

The integration is one file, `src/kapso_voice_agent/calcom.py`, and uses the
[Cal.com API v2](https://cal.com/docs/api-reference/v2/introduction).

## Set it up

1. In Cal.com, create one event type per service callers can book (for example "Consultation",
   30 minutes). Its title, length and description are what Riley says. Its availability is what
   Riley offers.
2. Create an API key under **Settings > Security** and put it in `.env` as `CAL_API_KEY`.
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

   - `timezone` is the time zone Riley speaks in. Use your Cal.com profile's time zone.
   - `services[].id` is a short name the agent uses (lowercase, `-` or `_`).
     `cal_event_type_id` is the Cal.com event type it books. `notes` is optional: when to suggest
     it.
   - Leave a fact empty if you do not want Riley to state it. Riley then says it does not have
     that information.
   - `ask_for_email`: Cal.com event types ask for the attendee's email by default, so Riley asks
     for it and spells it back. Set it to `false` only if your event types do not need an email.

4. Check the setup. This command only reads from Cal.com:

   ```sh
   uv run voice-agent calendar check
   ```

   It prints your Cal.com username and time zone and every event type with its ID. It also lists
   the problems it finds: a missing time zone, an event type ID that is not in your account, or a
   time zone that is different from your Cal.com profile.

5. Try the tools against your calendar. Reads are safe:

   ```sh
   uv run voice-agent tools call business_info '{"topic": "services"}'
   uv run voice-agent tools call available_slots '{"service_id": "consultation"}'
   ```

   `book_appointment`, `reschedule_appointment` and `cancel_appointment` change your real
   calendar, so `tools call` refuses them unless you add `--yes`.

Restart `serve` after you change `.env` or `business.json`. The server reads them at startup.

## What happens on a call

| Tool | Cal.com request | Notes |
| --- | --- | --- |
| `business_info` | `GET /v2/event-types/{id}` (cached 5 min) | Facts from `business.json`; service names and lengths from Cal.com |
| `available_slots` | `GET /v2/slots` for one event type, 14 days from the asked day, in your time zone | At most two times per answer; `needs_email` tells Riley to ask for an email |
| `book_appointment` | `POST /v2/bookings`, start in UTC | Attendee name, email (if asked) and your time zone. A note goes into the booking `metadata` |
| `my_appointments` | `GET /v2/bookings/{uid}` for each of the caller's bookings | Bookings the business cancelled or moved in Cal.com are not listed |
| `reschedule_appointment` | `POST /v2/bookings/{uid}/reschedule` | Cal.com makes a new booking uid; the caller keeps the same short id |
| `cancel_appointment` | `POST /v2/bookings/{uid}/cancel` | |

Each request sends `Authorization: Bearer <CAL_API_KEY>` and the `cal-api-version` value that
Cal.com's API reference requires for that endpoint (`calcom.py` has the values).

Who owns a booking: the server keeps a small private table, `DATA_DIR/calendar.sqlite3`. It maps
each booking Riley made to the call's caller key (an HMAC of the WhatsApp identity; a browser
call gets a random one) and to a short id. Callers can see, move and cancel only those bookings.
The model never sees Cal.com uids, and it cannot look anything up by phone number, name or email.
Bookings made in Cal.com directly, or by other callers, are not visible on the phone.

## When Cal.com says no or does not answer

- A read that fails (timeout, error status, an unexpected body, an invalid key) returns
  `calendar_unavailable`. Riley then says the booking system is not available right now. It does
  not offer times without a tool result.
- A booking request is sent once and never retried automatically.
  - Cal.com refuses (4xx): the server checks that day's open times again and returns
    `slot_taken` if the time is gone. Otherwise it returns `booking_rejected`.
  - No answer, a dropped connection or a 5xx: the result is `not_confirmed`. The booking may or
    may not exist, so Riley says it could not confirm the booking and does not try again. Check
    your Cal.com bookings list.
- A booking that your event type must confirm first returns `status: pending`. Riley says that
  the booking is requested, not confirmed.

No SQLite transaction stays open during a Cal.com request. Each local write is one statement.

## Emails and personal data

Cal.com handles a booking made by Riley like any other booking. It sends the emails and runs the
workflows that you configured, for example a confirmation to the attendee's email address. The
caller's name, email and optional note leave this server and go to Cal.com. Say so in your
privacy notice.

## Record a walkthrough

A short screen recording that shows the real integration. Use your own Cal.com account and your
own email address as the caller:

1. Open your Cal.com **Bookings** page (Upcoming) in one window.
2. Start a browser call (README) or call your WhatsApp number.
3. Ask for an appointment ("Hi, I'd like to book a consultation this week"). Riley offers two
   real open times from your calendar.
4. Pick one, and give your name and email. Riley reads them back. Say "yes".
5. Refresh the Bookings page: the new booking is there, with the name and time you gave.
6. Ask "What appointments do I have?", then "Can you move it to the next day?" or "Please cancel
   it". After your yes, refresh the Bookings page again to show the change.

## Development without Cal.com

`CALENDAR=local` uses a local appointment book in SQLite (`agent/dev-calendar.json`,
`src/kapso_voice_agent/store.py`) for offline development and the test suite. Its bookings are
not in any real calendar, and `serve` prints this. Never use it to answer real callers.
