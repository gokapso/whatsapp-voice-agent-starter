# Architecture and source map

## Data path

```
WhatsApp caller ──(Meta WebRTC audio, UDP)──────────────────────────────┐
      │                                                                 │
      └─ Meta ─(calls webhook)─> Kapso ─(signed HTTPS POST)─> /webhooks/whatsapp
                                  ^                                     │
                                  └─(call actions + SDP via Kapso API)──┤
                                                                        v
                        this server: CallManager ─> SmallWebRTC peer (audio 16 kHz PCM)
                                                         │
                                                ElevenAgentBridge ──(WebSocket)──> ElevenLabs Agents
                                                         │                       (ASR + LLM + TTS)
                                                   ToolRunner ─(HTTPS)─> Cal.com (availability, bookings)
                                                         └─> SQLite (which caller owns which booking)
```

- HTTPS carries webhooks, SDP and call actions. Audio uses its own WebRTC path (ICE; on Railway,
  a STUN server lets it reach Meta's call servers directly).
- The voice provider owns the conversation: ElevenLabs Agents by default, or xAI Grok Voice with
  `VOICE_PROVIDER=grok` (`grok_bridge.py`). Pipecat is only the audio transport and pipeline runner.
- A real inbound WhatsApp call worked end to end on Railway with Grok Voice (2026-10-14), and one
  real outbound call reached a handset with ElevenLabs (2026-10-07, earlier booking tools). The same call code can also run in a Pipecat Cloud session (below).

## Inbound call sequence

1. `POST /webhooks/whatsapp`: verify signature on raw bytes, parse, keep `calls` changes for
   this phone number. Terminate/REJECTED first, then artifact notices, then connects.
2. New `connect` + `USER_INITIATED` + SDP offer: claim the call ID, start `answer()` as a task,
   return 200 right away. Over capacity: send `reject` instead.
3. `answer()`: get the verified recording state (`recording.py`; unknown → `reject`), create the
   peer, apply the offer, make the answer SDP (sha-256 fingerprint only),
   `pre_accept`, `accept` (20 s budget). Only then create the agent session. The bridge opens the
   provider conversation (ElevenLabs) or plays the greeting (Grok) once the call's media connects,
   so the greeting is not spoken into a call whose audio is not flowing yet.
4. The session sends `conversation_initiation_client_data` with per-call variables
   (`opening_message`, `recording_status`, `today`, `weekday`, `timezone`, `business_name`,
   `call_purpose`).
5. End: the agent closes the conversation (end_call) → `terminate`; the caller hangs up →
   Meta `terminate` webhook cancels the task, no action sent; `MAX_SESSION_SECONDS` →
   `terminate`; failure before accept → `reject`.

## Browser test call sequence (operator only)

The console page sends its WebRTC offer to `POST /operator/api/browser-call` with the operator
token. The server checks the recording state, answers the offer, and starts the same agent
session as a WhatsApp call, with a random per-call caller identity and the inbound greeting.
There is no Kapso step.

## Outbound call sequence (operator only)

permission check → local SDP offer → `connect` → Meta's SDP answer (webhook, may arrive before
the HTTP response) → apply → `ACCEPTED` status → start agent → same endings as inbound. A connect
whose outcome is unknown is reported, never retried.

Without permission, **Ask to allow calls** sends Meta's free-form call permission request through
Kapso's `/messages` proxy: an interactive `call_permission_request` with `[outbound]
permission_request` from `agent.toml` as its text. It is sent only when `call_permissions` says
`send_call_permission_request` is allowed (Meta: one a day and two a week per person), once per
click. WhatsApp delivers it only inside the person's 24-hour customer service window (error
`131047` otherwise). The reply webhook (`call_permission_reply`) is not used: the console polls
`call_permissions` until `start_call` is allowed.

## Inbox handoff (experimental, `handoff.py`)

Off unless `HUMAN_HANDOFF=1` here **and** Kapso has enrolled the project in its handoff rollout.
Meta has no transfer action, and an answered call cannot renegotiate its SDP
([Meta Calling FAQ](https://developers.facebook.com/documentation/business-messaging/whatsapp/calling/faq/)).
So the Meta leg never changes. This server answers a second WebRTC peer for the teammate's
browser and moves the caller's audio itself:

```
Meta leg in -> HandoffInput -> agent bridge -> HandoffOutput -> Meta leg out
                    |                               ^
                    v                               |
            teammate leg out                teammate leg in
```

1. Grok Voice calls `transfer_to_human`, or the operator uses `POST /operator/api/calls/{ref}/handoff`
   (both providers). The server sends `POST /platform/v1/whatsapp/call_handoffs` to Kapso with
   `KAPSO_API_KEY`: the Meta call ID, a random request key (a retry returns the same request) and
   the STUN URLs of `HANDOFF_ICE_SERVERS_JSON` (never TURN credentials). The AI keeps talking.
2. A teammate selects **Join call** in Kapso Inbox. The browser asks for the microphone, then
   sends its SDP offer with every candidate to Kapso. This server reads it on its 1-second poll
   (`GET .../{id}`), answers it with a new peer and sends the answer (`POST .../{id}/answer`).
3. When that peer is connected **and** the teammate's microphone audio arrives here, the server
   asks Kapso to commit (`POST .../{id}/commit`). Only on Kapso's yes does it switch: caller
   audio goes only to the teammate, queued AI audio is flushed, the AI provider socket closes,
   teammate audio plays to the caller. The Meta call, its ID and its talk time do not change.
4. After the switch, the call ends when Kapso reports the handoff ended (Hang up in Inbox, the
   caller hanging up) or the teammate's leg closes; this server then sends `terminate`.
   The AI does not come back after the switch. `HANDOFF_MAX_SECONDS` (default 1800) replaces
   `MAX_SESSION_SECONDS` from the switch on.

Before the switch, everything that goes wrong ends only the attempt and the AI keeps the caller:
nobody joins in 60 s, a decline, the project's rollout turned off, no browser media within 25 s,
Kapso refusing the commit. Grok then says a fixed line ("I couldn't reach a teammate right
now..."). With ElevenLabs the stored agent has no transfer tool and is not told; only the operator
command can start a handoff, and the AI continues silently after a failure.

Not supported: outbound calls, browser test calls, Pipecat Cloud sessions, `LOCAL_CAPTURE=1`, and
more than one server process. Kapso never calls this server for a handoff, so it needs no new route.

## Pipecat Cloud session (`cloud.py`, `examples/pipecat-cloud`)

Kapso's dashboard Pipecat provider starts one Pipecat Cloud session per call, with the Meta
webhook in the start body. `cloud.answer_launch` looks the call up through Kapso (exists on this
number, user-initiated, claimed, not over), takes the caller identity from Kapso's saved call, and
passes that one `connect` to a `CallManager`. From there the inbound sequence above applies. There
is no webhook route, console or persistent `DATA_DIR`.

## Source map

| File | Responsibility | Key tests |
| --- | --- | --- |
| `src/kapso_voice_agent/app.py` | HTTP routes: public webhook + health, operator API | `test_http_surface.py` |
| `src/kapso_voice_agent/static/` | Operator console (plain HTML, CSS and JavaScript) | `test_http_surface.py`, a browser |
| `src/kapso_voice_agent/calls.py` | Webhook event rules, inbound lifecycle, browser calls, capacity | `test_http_surface.py`, `test_call_flow.py` |
| `src/kapso_voice_agent/outbound.py` | Outbound call state machine | `test_outbound.py` |
| `src/kapso_voice_agent/bridge.py` | Pipecat pipeline + ElevenLabs WebSocket protocol; picks the bridge for `VOICE_PROVIDER` | `test_call_flow.py` |
| `src/kapso_voice_agent/grok_bridge.py` | xAI Grok Voice realtime protocol in the same pipeline slot | `test_grok_bridge.py` |
| `src/kapso_voice_agent/handoff.py` | Experimental Inbox handoff: audio switch, teammate's leg, Kapso signaling | `test_handoff.py` |
| `src/kapso_voice_agent/cloud.py` | Pipecat Cloud entry: Kapso launch body, Kapso lookup, then `CallManager` | `test_cloud_launch.py` |
| `src/kapso_voice_agent/setup_wizard.py` | `voice-agent setup`, `export-env`, `railway variables` | `test_setup_wizard.py` |
| `src/kapso_voice_agent/kapso.py` | Kapso call-action client | via flow tests |
| `src/kapso_voice_agent/tools.py` | Tool schemas (source of truth) and dispatcher | `test_tools.py` |
| `src/kapso_voice_agent/calcom.py` | Cal.com calendar: availability, bookings, caller-to-booking map | `test_calcom.py` |
| `src/kapso_voice_agent/business.py` | Owner's business facts, spoken day and time phrases | `test_tools.py`, `test_calcom.py` |
| `src/kapso_voice_agent/store.py` | Local development calendar in SQLite (`CALENDAR=local`) | `test_tools.py` |
| `src/kapso_voice_agent/agent_config.py` | agent.toml → provider config, greetings, read-back diff | `test_agent_config.py` |
| `src/kapso_voice_agent/provider.py` | Dry-run/apply for ElevenLabs agent and Kapso webhook; startup webhook registration rule | `test_setup_and_artifacts.py`, `test_http_surface.py` |
| `src/kapso_voice_agent/recording.py` | Recording notice from the provider's stored setting + local capture | `test_privacy_and_retention.py` |
| `src/kapso_voice_agent/private.py` | 0700/0600 data directory and files, local retention pruning | `test_privacy_and_retention.py` |
| `src/kapso_voice_agent/capture.py` | Optional local WAV + turn capture | `test_call_flow.py`, `test_setup_and_artifacts.py` |
| `src/kapso_voice_agent/artifacts.py` | ElevenLabs MP3/JSON download | `test_setup_and_artifacts.py` |
| `src/kapso_voice_agent/events.py` | Redacted in-memory event log, call references, recent-call summary for the console | `test_http_surface.py` |
| `src/kapso_voice_agent/config.py` | Environment settings and validation | `test_setup_and_artifacts.py` |
| `src/kapso_voice_agent/fake_agent.py` | Offline agent stand-in for manual smoke tests | — |
| `src/kapso_voice_agent/cli.py` | `voice-agent` commands | `test_setup_and_artifacts.py`, `test_privacy_and_retention.py` |
| `agent/` | Editable prompt, settings, business facts, development calendar data | `test_agent_config.py` |
| `agent/provider-tests/` | Opt-in ElevenLabs agent tests (LLM-judged, run by the operator) | `test_agent_config.py` (shape only) |
| `scripts/smoke_setup.py`, `smoke_local.py` | Development smoke checks with real processes, offline | run by hand |
| `examples/pipecat-cloud/` | `bot.py`, Dockerfile on Pipecat's base image, `pcc-deploy.toml` | `test_cloud_launch.py`; container check in its README |
| `.railway/railway.ts`, `.railway/template.md`, `docker-entrypoint.sh`, `examples/vps/` | Railway project, Railway template overview, root hand-off for volumes, VPS Compose + Caddy | `test_setup_wizard.py`, a real deploy |

## Contracts

Small, stable interfaces. Change them only together with their tests.

### HTTP

| Route | Auth | Request | Response |
| --- | --- | --- | --- |
| `GET /healthz` | none | — | `{"ok": true}` |
| `POST /webhooks/whatsapp` | `X-Webhook-Signature` = hex HMAC-SHA256(raw body, `WHATSAPP_WEBHOOK_SECRET`) | Meta webhook JSON, ≤ 1 MB | `{"calls": n, "statuses": n, "ignored": n}`; 401 bad signature; 400 not JSON; 413 too large; 503 secret unset, or an inbound connect while the agent is not configured |
| `GET /operator/`, `/operator/app.js`, `/operator/console.css` | none (page has no secrets); 404 if `OPERATOR_TOKEN` unset | — | console and assets |
| `GET /operator/api/state` | `Authorization: Bearer <OPERATOR_TOKEN>` (the console access key) | — | readiness flags, `agent_missing` (setting names), `whatsapp_missing`, `kapso_webhook` (null when `KAPSO_REGISTER_WEBHOOK` is off\|`registered`\|`present`\|`other`\|`failed`\|`missing_settings`), `voice` `{provider: elevenlabs\|grok, label, model, voice, offline_fake_agent, setup_step: null\|api_key\|create_agent, problem: null\|{problem: auth\|billing\|rate_limited\|not_found\|unreachable\|failed, at}}`, `calendar` (`calcom`\|`local`\|null), `recording` `{provider_recording: on\|off\|unknown\|not_checked\|off (offline fake agent), local_capture, agent_toml_record_audio, next_greeting_says_recorded: true\|false\|null, last_check_error}`, active calls `{ref, direction, state}`, `recent_calls` (newest 6 in the event log: `{ref, kind, started_at, connected_at, last_at, media, tools, problem, active}`), events, native artifact notices |
| `POST /operator/api/browser-call` | bearer | `{"sdp": "<offer>"}` | `{"ref", "type": "answer", "sdp"}`; 409 at capacity; 503 agent not ready (lists what is missing) or recording setting unknown |
| `POST /operator/api/calls/{ref}/hangup` | bearer | — | `{"ended": true}`; 404 unknown ref |
| `POST /operator/api/outbound/permission` | bearer | `{"recipient": "<E.164 digits or US.…>"}` | `{"permission_status": no_permission\|temporary\|permanent, "can_call", "can_request"}` |
| `POST /operator/api/outbound/permission-request` | bearer | `{"recipient"}` | the same fields and `"sent"`; nothing is sent when calls are already allowed; 429 when Meta allows no more requests; 502 with the reason when Meta refuses |
| `POST /operator/api/outbound/call` | bearer | `{"recipient"}` (extra fields rejected) | `{"ref", "state": "ringing" \| "terminated"}` |
| `POST /operator/api/calls/{ref}/handoff` | bearer | — | `{"ok", "status": "requested" \| "already_requested"}` or `{"ok": false, "code": "handoff_unavailable"}`; 409 when `handoff.problem` is set; 404 unknown ref. Experimental |

`ref` is `call-<16 hex>` (SHA-256 prefix of the Meta call ID) or `browser-<16 hex>`. Raw Meta call
IDs never leave the process except in Kapso API calls.

### Webhook events handled

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

### Pipecat Cloud launch body

`cloud.answer_launch` takes the body that Kapso's dashboard Pipecat provider sends to
`/v1/public/{agent}/start`: `{"kind": "whatsapp_connect", "webhook": <Meta calls webhook>,
"whatsapp_token", "phone_number_id", "context"}`. It needs exactly one `USER_INITIATED` `connect`
for `WHATSAPP_PHONE_NUMBER_ID`, and Kapso's saved call (`GET .../calls?call_id=...&limit=1`) with
`session_started_at` set and no `ended_at`. The caller identity is Kapso's
`business_scoped_user_id`, else `user_wa_id`; a body that disagrees is refused. Outcomes:
`finished`, `unverified`, `not_started`, `no_inbound_connect`, `not_configured`,
`ignored_other_phone_number`, `ignored_kind`. `whatsapp_token` and `context` are not used.

### Tool results

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
so a retried call cannot book twice. The Cal.com requests behind each tool are below.

### Cal.com requests

| Tool | Cal.com request | Notes |
| --- | --- | --- |
| `business_info` | `GET /v2/event-types/{id}` (cached 5 min) | Facts from `business.json`; service names and lengths from Cal.com |
| `available_slots` | `GET /v2/slots` for one event type, 14 days from the asked day, in your time zone | At most two times per answer; `needs_email` tells Riley to ask for an email |
| `book_appointment` | `POST /v2/bookings`, start in UTC | Attendee name, email (if asked) and your time zone. A note goes into the booking `metadata`. A repeat request for the same service and time first reads `GET /v2/bookings/{uid}`: it is answered from that booking if it is still accepted or pending, and booked again if Cal.com cancelled, rejected or lost it |
| `my_appointments` | `GET /v2/bookings/{uid}` for each of the caller's bookings | Bookings the business cancelled or moved in Cal.com are not listed |
| `reschedule_appointment` | `POST /v2/bookings/{uid}/reschedule` | Cal.com makes a new booking uid; the caller keeps the same short id |
| `cancel_appointment` | `POST /v2/bookings/{uid}/cancel` | |

Each request sends `Authorization: Bearer <CAL_API_KEY>` and the `cal-api-version` value that
Cal.com's API reference requires for that endpoint (`calcom.py` has the values).

When Cal.com says no or does not answer:

- A read that fails (timeout, error status, an unexpected body, an invalid key) returns
  `calendar_unavailable`. Riley then says the booking system is not available right now. It does
  not offer times without a tool result.
- A booking request is sent once and never retried automatically.
  - Cal.com refuses (4xx): the server checks that day's open times again and returns
    `slot_taken` if the time is gone. Otherwise it returns `booking_rejected`.
  - No answer, a dropped connection or a 5xx: the result is `not_confirmed`. The booking may or
    may not exist, so Riley says it could not confirm the booking and does not try again. Check
    your Cal.com bookings list.
- A booking or a move that your event type must confirm first returns `status: pending`. Riley
  says that the booking or the new time is requested, not confirmed.
- A seated event type returns `unsupported_service` before any slot read or write. If Cal.com
  still answers a booking with a `seatUid`, the booking is reported but not stored, so it can
  never be moved or cancelled by phone.

No SQLite transaction stays open during a Cal.com request. Each local write is one statement.

### Events with fields

`rtc_connected` carries `media`: `direct` (host or STUN-mapped address), `relay` (through a relay
server in `ICE_SERVERS_JSON`) or `unknown`. `provider_problem_<code>` is logged with the same codes
as `voice.problem`. `rtc_connect_timed_out`: the call's audio did not connect within 30 seconds;
the session ends and frees its call slot (an inbound call then gets `terminate`).
A failed Kapso call action (`call_failed_kapsoerror`, `reject_failed_kapsoerror`,
`terminate_failed_kapsoerror`) carries `http_status`, the error `code` (Kapso's, or Meta's numeric
code passed through) and Kapso's `request_id`. `409` `answered_elsewhere` or `not_answerable`
means Kapso keeps the call for another handler (the Inbox in hybrid mode, or a dashboard voice
agent); the server then sends no reject, which Kapso would refuse too.
`handoff_<event>` (experimental): `handoff_requested`, `handoff_request_refused code=<Kapso's>`,
`handoff_joining`, `handoff_media_ready media=direct|relay`, `handoff_committed`,
`handoff_fallback reason=<why the AI kept the call>`, `handoff_teammate_left reason=<hung_up|call_ended|human_disconnected>`.
`/operator/api/state` has `handoff: {enabled, problem: null|off|kapso_missing|local_capture|turn_required}`
and active calls a `handoff` state when the call could hand over.
`kapso_webhook_<state>` (one per startup with `KAPSO_REGISTER_WEBHOOK=1`): the same states as
`kapso_webhook` in `/operator/api/state`. `other` means another URL already receives the number's
Meta events; it is left alone.

### xAI realtime WebSocket (Grok Voice, subset used)

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

### ElevenLabs Agents WebSocket (subset used)

Sent: `conversation_initiation_client_data` (dynamic variables), `{"user_audio_chunk": base64 PCM16 16 kHz, 100 ms}`,
`pong`, `client_tool_result`.
Received: `conversation_initiation_metadata` (must be `pcm_16000` both ways), `audio`,
`interruption` (drop audio with `event_id ≤` the interruption), `ping`, `client_tool_call`,
`user_transcript` / `agent_response` (only used for optional local capture), `client_error`.

The signed URL comes from `GET /v1/convai/conversation/get-signed-url` with the server's API key
(`platform_settings.auth.enable_auth = true`), and must be `wss://api.elevenlabs.io`.
