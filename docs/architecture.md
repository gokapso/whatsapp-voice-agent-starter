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
- ElevenLabs owns the conversation. Pipecat is only the audio transport and pipeline runner.
- This bridge and call handling carried one real English outbound WhatsApp call to a handset
  (2026-10-07), with the earlier local booking tools; the Cal.com tools have not been on a real
  call yet. Inbound calls use the same bridge but were not part of that call. It does not use
  Daily rooms or Pipecat Cloud.

## Inbound call sequence

1. `POST /webhooks/whatsapp`: verify signature on raw bytes, parse, keep `calls` changes for
   this phone number. Terminate/REJECTED first, then artifact notices, then connects.
2. New `connect` + `USER_INITIATED` + SDP offer: claim the call ID, start `answer()` as a task,
   return 200 right away. Over capacity: send `reject` instead.
3. `answer()`: get the verified recording state (`recording.py`; unknown → `reject`), create the
   peer, apply the offer, make the answer SDP (sha-256 fingerprint only),
   `pre_accept`, `accept` (20 s budget). Only then create the agent session, so the greeting
   is not spoken into a call that is not connected yet.
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

## Source map

| File | Responsibility | Key tests |
| --- | --- | --- |
| `src/kapso_voice_agent/app.py` | HTTP routes: public webhook + health, operator API/console | `test_http_surface.py` |
| `src/kapso_voice_agent/calls.py` | Webhook event rules, inbound lifecycle, browser calls, capacity | `test_http_surface.py`, `test_call_flow.py` |
| `src/kapso_voice_agent/outbound.py` | Outbound call state machine | `test_outbound.py` |
| `src/kapso_voice_agent/bridge.py` | Pipecat pipeline + ElevenLabs WebSocket protocol | `test_call_flow.py` |
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

## Contracts

See `docs/contracts.md` for HTTP routes, webhook events handled, tool result shape and the
ElevenLabs WebSocket messages used.
