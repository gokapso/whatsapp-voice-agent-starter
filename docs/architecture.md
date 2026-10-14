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

- HTTPS carries webhooks, SDP and call actions. Audio uses its own WebRTC path (ICE, maybe TURN).
- The voice provider owns the conversation: ElevenLabs Agents by default, or xAI Grok Voice with
  `VOICE_PROVIDER=grok` (`grok_bridge.py`). Pipecat is only the audio transport and pipeline runner.
- This bridge and call handling carried one real English outbound WhatsApp call to a handset
  (2026-10-07), with the earlier local booking tools; the Cal.com tools have not been on a real
  call yet. Inbound calls use the same bridge but were not part of that call. It does not use
  Daily rooms. The same call code can also run in a Pipecat Cloud session (below).

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
There is no Kapso step. This is the README's step 1 and `docs/testing.md` level 2.

## Outbound call sequence (operator only, `ENABLE_OUTBOUND=1`)

permission check → local SDP offer → `connect` → Meta's SDP answer (webhook, may arrive before
the HTTP response) → apply → `ACCEPTED` status → start agent → same endings as inbound. A connect
whose outcome is unknown is reported, never retried.

## Pipecat Cloud session (`cloud.py`, `examples/pipecat-cloud`)

Kapso's dashboard Pipecat provider starts one Pipecat Cloud session per call, with the Meta
webhook in the start body. `cloud.answer_launch` looks the call up through Kapso (exists on this
number, user-initiated, claimed, not over), takes the caller identity from Kapso's saved call, and
passes that one `connect` to a `CallManager`. From there the inbound sequence above applies. There
is no webhook route, console or persistent `DATA_DIR`.

## Provider SIP (`sip.py`, `xai_sip.py`)

`voice-agent sip status` reads who answers the number's calls; `voice-agent sip plan` prints the
SIP requests without sending them. Meta's SIP mode replaces the Calling API for that number.
`xai_sip.py` is the control service for xAI Direct SIP: signed webhook in, one realtime session
per `call_id` for instructions, greeting and tools, then hangup. xAI handles the audio. Both SIP
paths are blocked for WhatsApp today (`docs/choose-where-calls-run.md`).

## Source map

| File | Responsibility | Key tests |
| --- | --- | --- |
| `src/kapso_voice_agent/app.py` | HTTP routes: public webhook + health, operator API/console | `test_http_surface.py` |
| `src/kapso_voice_agent/calls.py` | Webhook event rules, inbound lifecycle, browser calls, capacity | `test_http_surface.py`, `test_call_flow.py` |
| `src/kapso_voice_agent/outbound.py` | Outbound call state machine | `test_outbound.py` |
| `src/kapso_voice_agent/bridge.py` | Pipecat pipeline + ElevenLabs WebSocket protocol; picks the bridge for `VOICE_PROVIDER` | `test_call_flow.py` |
| `src/kapso_voice_agent/grok_bridge.py` | xAI Grok Voice realtime protocol in the same pipeline slot | `test_grok_bridge.py` |
| `src/kapso_voice_agent/cloud.py` | Pipecat Cloud entry: Kapso launch body, Kapso lookup, then `CallManager` | `test_cloud_launch.py` |
| `src/kapso_voice_agent/sip.py` | `sip status` (read-only) and `sip plan` (dry run) for provider SIP | via `voice-agent sip plan` |
| `src/kapso_voice_agent/xai_sip.py` | xAI Direct SIP: Standard Webhooks check, session control, tools, hangup | `test_xai_sip.py` |
| `src/kapso_voice_agent/turn.py` | Per-call ICE servers; Cloudflare TURN credentials | `test_turn.py` |
| `src/kapso_voice_agent/setup_wizard.py` | `voice-agent setup`, `export-env`, `railway variables` | `test_setup_wizard.py` |
| `src/kapso_voice_agent/kapso.py` | Kapso call-action client | via flow tests |
| `src/kapso_voice_agent/tools.py` | Tool schemas (source of truth) and dispatcher | `test_tools.py` |
| `src/kapso_voice_agent/calcom.py` | Cal.com calendar: availability, bookings, caller-to-booking map | `test_calcom.py` |
| `src/kapso_voice_agent/business.py` | Owner's business facts, spoken day and time phrases | `test_tools.py`, `test_calcom.py` |
| `src/kapso_voice_agent/store.py` | Local development calendar in SQLite (`CALENDAR=local`) | `test_tools.py` |
| `src/kapso_voice_agent/agent_config.py` | agent.toml → provider config, greetings, read-back diff | `test_agent_config.py` |
| `src/kapso_voice_agent/provider.py` | Dry-run/apply for ElevenLabs agent and Kapso webhook | `test_setup_and_artifacts.py` |
| `src/kapso_voice_agent/recording.py` | Recording notice from the provider's stored setting + local capture | `test_privacy_and_retention.py` |
| `src/kapso_voice_agent/private.py` | 0700/0600 data directory and files, local retention pruning | `test_privacy_and_retention.py` |
| `src/kapso_voice_agent/capture.py` | Optional local WAV + turn capture | `test_call_flow.py`, `test_setup_and_artifacts.py` |
| `src/kapso_voice_agent/artifacts.py` | ElevenLabs MP3/JSON download | `test_setup_and_artifacts.py` |
| `src/kapso_voice_agent/events.py` | Redacted in-memory event log, call references | all |
| `src/kapso_voice_agent/config.py` | Environment settings and validation | `test_setup_and_artifacts.py` |
| `src/kapso_voice_agent/fake_agent.py` | Offline agent stand-in for manual smoke tests | — |
| `src/kapso_voice_agent/cli.py` | `voice-agent` commands | `test_setup_and_artifacts.py`, `test_privacy_and_retention.py` |
| `agent/` | Editable prompt, settings, business facts, development calendar data | `test_agent_config.py` |
| `agent/provider-tests/` | Opt-in ElevenLabs agent tests (LLM-judged, run by the operator) | `test_agent_config.py` (shape only) |
| `scripts/smoke_setup.py`, `smoke_local.py` | Development smoke checks with real processes, offline | run by hand |
| `scripts/smoke_turn_relay.py` | Docker: image as Railway runs it, audio only through a coturn relay | run by hand |
| `examples/pipecat-cloud/` | `bot.py`, Dockerfile on Pipecat's base image, `pcc-deploy.toml` | `test_cloud_launch.py`; container check in its README |
| `examples/elevenlabs-sip/`, `examples/xai-sip/` | SIP setup guides; minimal no-tools ElevenLabs prompt | — |
| `.railway/railway.ts`, `docker-entrypoint.sh`, `examples/vps/` | Railway project, root hand-off for volumes, VPS Compose + Caddy | `docs/deploy-railway.md` |

## Contracts

See `docs/contracts.md` for HTTP routes, webhook events handled, tool result shape and the
ElevenLabs WebSocket messages used.
