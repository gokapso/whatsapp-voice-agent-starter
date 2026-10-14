# Contracts

Small, stable interfaces. Change them only together with their tests.

## HTTP

| Route | Auth | Request | Response |
| --- | --- | --- | --- |
| `GET /healthz` | none | — | `{"ok": true}` |
| `POST /webhooks/whatsapp` | `X-Webhook-Signature` = hex HMAC-SHA256(raw body, `WHATSAPP_WEBHOOK_SECRET`) | Meta webhook JSON, ≤ 1 MB | `{"calls": n, "statuses": n, "ignored": n}`; 401 bad signature; 400 not JSON; 413 too large; 503 secret unset, or an inbound connect while the agent is not configured |
| `GET /operator/`, `/operator/app.js`, `/operator/console.css` | none (page has no secrets); 404 if `OPERATOR_TOKEN` unset | — | console and assets |
| `GET /operator/api/state` | `Authorization: Bearer <OPERATOR_TOKEN>` (the console access key) | — | readiness flags, `agent_missing` (setting names), `voice` `{provider: elevenlabs\|grok, label, model, voice, offline_fake_agent, problem: null\|{problem: auth\|rate_limited\|not_found\|unreachable\|failed, at}}`, `calendar` (`calcom`\|`local`\|null), `recording` `{provider_recording: on\|off\|unknown\|not_checked\|off (offline fake agent), local_capture, agent_toml_record_audio, next_greeting_says_recorded: true\|false\|null, last_check_error}`, active calls `{ref, direction, state}`, events, native artifact notices |
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
`pending` (requested; the business confirms it in Cal.com) or `already_booked` (same caller,
service and time, still accepted in Cal.com; a repeat of a pending request stays `pending`).
`reschedule_appointment` returns `status: rescheduled`, or `pending` when the new time still
needs the business to confirm it, and `previous_when`; the booking keeps its `id`. `cancel_appointment` returns the cancelled booking. The local development calendar
(`CALENDAR=local`) returns the same shapes, plus `open`/`closed_reason` for days it has no
slots on.

Codes: `invalid_arguments`, `unknown_tool`, `invalid_date`, `outside_horizon`,
`service_required`, `unknown_service`, `confirmation_required`, `email_required`,
`invalid_email`, `slot_unavailable`, `slot_taken`, `not_found`, `booking_rejected`, `unsupported_service`,
`not_confirmed`, `calendar_unavailable`, `internal_error`. Each message says how to recover,
and the prompt has a rule for each code (`tests/test_tools.py` and `tests/test_calcom.py` check
this). The provider receives `is_error = !ok`. Results are cached per `tool_call_id` (last 100)
so a retried call cannot book twice. The Cal.com requests behind each tool are in
`docs/calendar.md`.

## Events with fields

`rtc_connected` carries `media`: `direct` (host or STUN-mapped address), `relay` (through TURN)
or `unknown`. `provider_problem_<code>` is logged with the same codes as `voice.problem`.
`turn_credentials_failed`: TURN was configured but no credentials came back; no call starts
(browser 503, outbound 503, inbound `reject`).

## Cloudflare TURN (`TURN_PROVIDER=cloudflare`)

Before each call: `POST https://rtc.live.cloudflare.com/v1/turn/keys/{CLOUDFLARE_TURN_KEY_ID}/credentials/generate-ice-servers`
with `Authorization: Bearer {CLOUDFLARE_TURN_API_TOKEN}` and `{"ttl": MAX_SESSION_SECONDS + 600}`,
5 s timeout, no redirects. From the returned `iceServers`, the server keeps the first STUN URL
and exactly one TURN URL for `TURN_TRANSPORT` (`tls`: `turns:…:443?transport=tcp`; `tcp`;
`udp`), because aiortc uses only the first of each.

## xAI realtime WebSocket (Grok Voice, subset used)

`wss://api.x.ai/v1/realtime?model=<agent.toml grok.model>` with `Authorization: Bearer XAI_API_KEY`
from the server only.
Sent: `session.update` (after `conversation.created`: rendered instructions, voice, server VAD,
`audio/pcm` 16000 in and out, transcription language hint and keyterms, the six function tools
plus `end_call`), `conversation.item.create` with a non-interruptible `force_message` greeting,
`input_audio_buffer.append` (base64 PCM16 16 kHz, 100 ms, only after `session.updated`),
`conversation.item.create` `function_call_output`, `response.create` (once per response that
produced tool outputs).
Received: `conversation.created`, `session.updated`, `response.created`,
`response.output_audio.delta` / `response.audio.delta` (dropped for responses the caller talked
over), `input_audio_buffer.speech_started` (barge-in, ignored while the disclosure greeting
plays), `response.function_call_arguments.done`, `response.done`, transcription and transcript
deltas (only for optional local capture), `error` (harmless cancel races ignored; auth, rate
limit, or anything before the session is ready ends the call).

## ElevenLabs Agents WebSocket (subset used)

Sent: `conversation_initiation_client_data` (dynamic variables), `{"user_audio_chunk": base64 PCM16 16 kHz, 100 ms}`,
`pong`, `client_tool_result`.
Received: `conversation_initiation_metadata` (must be `pcm_16000` both ways), `audio`,
`interruption` (drop audio with `event_id ≤` the interruption), `ping`, `client_tool_call`,
`user_transcript` / `agent_response` (only used for optional local capture), `client_error`.

The signed URL comes from `GET /v1/convai/conversation/get-signed-url` with the server's API key
(`platform_settings.auth.enable_auth = true`), and must be `wss://api.elevenlabs.io`.
