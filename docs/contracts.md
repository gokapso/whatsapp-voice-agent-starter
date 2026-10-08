# Contracts

Small, stable interfaces. Change them only together with their tests.

## HTTP

| Route | Auth | Request | Response |
| --- | --- | --- | --- |
| `GET /healthz` | none | — | `{"ok": true}` |
| `POST /webhooks/whatsapp` | `X-Webhook-Signature` = hex HMAC-SHA256(raw body, `WHATSAPP_WEBHOOK_SECRET`) | Meta webhook JSON, ≤ 1 MB | `{"calls": n, "statuses": n, "ignored": n}`; 401 bad signature; 400 not JSON; 413 too large; 503 secret unset, or an inbound connect while the agent is not configured |
| `GET /operator/` , `/operator/app.js` | none (page has no secrets); 404 if `OPERATOR_TOKEN` unset | — | console |
| `GET /operator/api/state` | `Authorization: Bearer <OPERATOR_TOKEN>` | — | readiness flags, `agent_missing` (setting names), `recording` `{provider_recording: on\|off\|unknown\|not_checked\|off (offline fake agent), local_capture, agent_toml_record_audio, next_greeting_says_recorded: true\|false\|null, last_check_error}`, active calls `{ref, direction, state}`, events, native artifact notices |
| `POST /operator/api/browser-call` | bearer | `{"sdp": "<offer>"}` | `{"ref", "type": "answer", "sdp"}`; 409 at capacity; 503 agent not ready (lists what is missing) or recording setting unknown |
| `POST /operator/api/calls/{ref}/hangup` | bearer | — | `{"ended": true}`; 404 unknown ref |
| `POST /operator/api/outbound/permission` | bearer + `ENABLE_OUTBOUND=1` | `{"recipient": "<E.164 digits or US.…>"}` | `{"permission_status", "can_call"}` |
| `POST /operator/api/outbound/call` | bearer + `ENABLE_OUTBOUND=1` | `{"recipient"}` (extra fields rejected) | `{"ref", "state": "ringing" \| "terminated"}` |

`ref` is `call-<16 hex>` (SHA-256 prefix of the Meta call ID) or `browser-<16 hex>`. Raw Meta call
IDs never leave the process except in Kapso API calls.

## Webhook events handled

Only `entry[].changes[]` with `field == "calls"` and `value.metadata.phone_number_id ==
WHATSAPP_PHONE_NUMBER_ID`. Everything else is counted as `ignored`. If this server shares a Meta
webhook with messaging, route `messages` changes (including `interactive.call_permission_reply`)
elsewhere first.

| Event | Action |
| --- | --- |
| `calls[].event = connect`, `direction = USER_INITIATED`, `session.sdp_type = offer` | answer (or `reject` at capacity) |
| `calls[].event = connect`, `direction = BUSINESS_INITIATED`, `session.sdp_type = answer` | apply to the pending outbound call |
| `calls[].event = terminate` | mark terminal, cancel the call task, no action sent |
| `statuses[].status = RINGING / ACCEPTED / REJECTED` | outbound progress; REJECTED is terminal |
| `calls[].event = call_recording_available` / `call_transcription_available` / `call_transcript_available` | store a notice `{ref, event, media_present, mime_type}`; never starts a session |

Deduplication is by Meta call ID (512 most recent kept in memory). `X-Idempotency-Key` is not
used; per-call guards make repeated deliveries harmless in one process.

## Tool results

Every tool returns JSON with `ok`. Times come as an exact `slot` value (local ISO time with
offset) plus a `when` phrase for the agent to say. Examples from the Cal.com calendar:

```json
{"ok": true, "service_id": "consultation", "service_name": "Consultation", "timezone": "America/Chicago", "needs_email": true, "slots": [{"slot": "2026-11-03T10:00:00-06:00", "when": "Tuesday, November 3 at 10 AM"}], "has_more": true}
{"ok": true, "service_id": "consultation", "service_name": "Consultation", "timezone": "America/Chicago", "needs_email": true, "day": "2026-11-02", "day_spoken": "Monday, November 2", "slots": [], "has_more": false, "next_available": {"slot": "2026-11-03T10:00:00-06:00", "when": "Tuesday, November 3 at 10 AM"}}
{"ok": true, "id": "1A2B3C4D", "slot": "2026-11-03T10:00:00-06:00", "when": "Tuesday, November 3 at 10 AM", "service_id": "consultation", "status": "booked", "name": "Sam Lee", "timezone": "America/Chicago"}
{"ok": false, "code": "slot_taken", "message": "Not booked. That time was just taken. Say so, call available_slots again and offer other times."}
```

`available_slots` returns at most two slots; for a day with nothing open, `next_available` is
the soonest open time after it (or null). `book_appointment` returns `status` `booked`,
`pending` (the business confirms it in Cal.com) or `already_booked` (same caller, same time).
`reschedule_appointment` returns `status: rescheduled` and `previous_when`; the booking keeps
its `id`. `cancel_appointment` returns the cancelled booking. The local development calendar
(`CALENDAR=local`) returns the same shapes, plus `open`/`closed_reason` for days it has no
slots on.

Codes: `invalid_arguments`, `unknown_tool`, `invalid_date`, `outside_horizon`,
`service_required`, `unknown_service`, `confirmation_required`, `email_required`,
`invalid_email`, `slot_unavailable`, `slot_taken`, `not_found`, `booking_rejected`,
`not_confirmed`, `calendar_unavailable`, `internal_error`. Each message says how to recover,
and the prompt has a rule for each code (`tests/test_tools.py` and `tests/test_calcom.py` check
this). The provider receives `is_error = !ok`. Results are cached per `tool_call_id` (last 100)
so a retried call cannot book twice. The Cal.com requests behind each tool are in
`docs/calendar.md`.

## ElevenLabs Agents WebSocket (subset used)

Sent: `conversation_initiation_client_data` (dynamic variables), `{"user_audio_chunk": base64 PCM16 16 kHz, 100 ms}`,
`pong`, `client_tool_result`.
Received: `conversation_initiation_metadata` (must be `pcm_16000` both ways), `audio`,
`interruption` (drop audio with `event_id ≤` the interruption), `ping`, `client_tool_call`,
`user_transcript` / `agent_response` (only used for optional local capture), `client_error`.

The signed URL comes from `GET /v1/convai/conversation/get-signed-url` with the server's API key
(`platform_settings.auth.enable_auth = true`), and must be `wss://api.elevenlabs.io`.
