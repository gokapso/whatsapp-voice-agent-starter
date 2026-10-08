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
offset) plus a `when` phrase for the agent to say:

```json
{"ok": true, "slots": [{"slot": "2026-11-03T10:00:00-06:00", "when": "Tuesday, November 3 at 10 AM"}], "has_more": true, "timezone": "America/Chicago"}
{"ok": true, "day": "2026-11-26", "day_spoken": "Thursday, November 26", "open": false, "closed_reason": "Thanksgiving holiday", "next_open_day": "2026-11-28", "next_open_day_spoken": "Saturday, November 28", "slots": []}
{"ok": true, "status": "booked", "id": "1A2B3C4D", "slot": "2026-11-03T10:00:00-06:00", "when": "Tuesday, November 3 at 10 AM", "service_name": "Brake check and adjustment", "fictional": true}
{"ok": false, "code": "slot_taken", "message": "Not booked. Another caller just took that time. Say so, call available_slots again and offer other times."}
```

An open day with no free times returns `slots: []` and `next_available` (the soonest free slot
after that day, or null). `book_appointment` returns `status: booked` or, when the same caller
books the same slot again, `already_booked`. `cancel_appointment` returns the cancelled booking.

Codes: `invalid_arguments`, `unknown_tool`, `invalid_date`, `outside_horizon`,
`confirmation_required`, `unknown_service`, `slot_unavailable`, `slot_taken`, `not_found`,
`internal_error`. Each message says how to recover, and the prompt has a rule for each code
(`tests/test_tools.py` checks this). The provider receives `is_error = !ok`. Results are cached
per `tool_call_id` (last 100) so a retried call cannot double-book.

## ElevenLabs Agents WebSocket (subset used)

Sent: `conversation_initiation_client_data` (dynamic variables), `{"user_audio_chunk": base64 PCM16 16 kHz, 100 ms}`,
`pong`, `client_tool_result`.
Received: `conversation_initiation_metadata` (must be `pcm_16000` both ways), `audio`,
`interruption` (drop audio with `event_id ≤` the interruption), `ping`, `client_tool_call`,
`user_transcript` / `agent_response` (only used for optional local capture), `client_error`.

The signed URL comes from `GET /v1/convai/conversation/get-signed-url` with the server's API key
(`platform_settings.auth.enable_auth = true`), and must be `wss://api.elevenlabs.io`.
