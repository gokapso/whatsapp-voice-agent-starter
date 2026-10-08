# AGENTS.md

Guide for coding agents (and people) changing this repository. Read this first, then
`docs/architecture.md` for the source map.

## What this is

A self-hosted English voice agent for WhatsApp calls through Kapso. A call arrives as a
Kapso-signed Meta Calling webhook. This server answers it with WebRTC (Pipecat SmallWebRTC,
audio only) and streams the audio to an ElevenLabs Agents conversation, which does speech
recognition, the LLM and text-to-speech. Tools run here, against a local SQLite file. The
operator console can start the same session from a browser, which is how people test the agent
before connecting a number.

The example business is fictional: Riley, front desk of North Loop Bikes.

## Commands

```sh
uv sync                                   # install exactly what uv.lock pins
uv run pytest                             # full offline suite (~1.5 min; real local WebRTC)
uv run voice-agent init                   # private .env with fresh local secrets; never overwrites
uv run voice-agent check                  # validate agent.toml, prompt, greetings, tool behavior
uv run voice-agent agent plan             # render build/agent-config.json (offline)
uv run voice-agent tools schema           # tool definitions the provider receives
uv run voice-agent tools call available_slots '{}' --now 2026-11-02T09:00:00-06:00
uv run voice-agent serve                  # http://127.0.0.1:8080; prints the console URL, never secrets
uv run voice-agent dev init-env           # private .env.dev for the offline fake agent
uv run voice-agent dev fake-agent         # offline agent stand-in (DEV_AGENT_WS_URL): a tone, not a conversation
uv run voice-agent artifacts prune        # delete expired local captures/downloads (offline)
uv run python scripts/smoke_setup.py      # init/check/serve + fake-agent browser call, clean environment, real processes
python3 scripts/secret_scan.py            # run before every commit
uvx ruff check src tests scripts          # lint
```

Commands that touch a provider are dry runs unless `--yes` is given: `agent apply`
(`--save-agent-id` also writes a newly created agent's ID to the env file), `kapso webhook`.
`agent verify` and `artifacts fetch` are read-only network calls. `agent verify` compares every
value the repo sends, including the full prompt and each tool's description, parameters and
response settings; fields the provider adds by itself are ignored.

## Where to change things

| Goal | Edit | Then run |
| --- | --- | --- |
| What the agent says and how | `agent/prompt.md` | `voice-agent check`, `pytest tests/test_agent_config.py tests/test_tools.py` |
| Greeting, voice, model, limits, recording | `agent/agent.toml` | `voice-agent agent plan` |
| Business facts (hours, services, closures) | `agent/business.json` | `pytest tests/test_tools.py` |
| Tool arguments and descriptions | `src/kapso_voice_agent/tools.py` (Pydantic models + `TOOLS`) | `voice-agent tools schema` |
| Tool behavior / storage | `src/kapso_voice_agent/store.py` | `pytest tests/test_tools.py` |
| LLM behavior scenarios (opt-in, provider-judged) | `agent/provider-tests/*.json` | `pytest tests/test_agent_config.py`; run them per `docs/testing.md` |
| Setup commands | `src/kapso_voice_agent/cli.py` | `pytest tests/test_setup_and_artifacts.py`, `scripts/smoke_setup.py` |
| Call handling rules | `src/kapso_voice_agent/calls.py`, `outbound.py` | `pytest tests/test_http_surface.py tests/test_call_flow.py tests/test_outbound.py` |
| Recording notice decision | `src/kapso_voice_agent/recording.py` | `pytest tests/test_privacy_and_retention.py` |
| Private files, retention | `src/kapso_voice_agent/private.py` | `pytest tests/test_privacy_and_retention.py` |

To replace the bike shop with your business, follow `docs/customize.md`: rewrite
`business.json`, the prompt and the greetings; keep or rewrite the five tools. Tool rules: the
model copies exact values from tool results (`slot`, `service_id`, booking `id`) and says the
`when` phrase; failures are `{"ok": false, "code": ..., "message": ...}` with a message that says
how to recover, and every code needs a rule in the prompt's "When a tool says no" section. To call
a real backend from a tool, see the calendar recipe in `docs/customize.md`: keep calls under
`response_timeout_secs` and outside any transaction.

## Rules that must stay true

Tests enforce each of these. Do not weaken a test to make a change pass.

1. **Signed webhook only.** `/webhooks/whatsapp` verifies `X-Webhook-Signature` (hex HMAC-SHA256
   of the raw body) before parsing. Only `calls` changes for `WHATSAPP_PHONE_NUMBER_ID` are used.
2. **Operator controls are separate.** Everything under `/operator` needs `OPERATOR_TOKEN` and
   returns 404 when it is unset. Outbound calling also needs `ENABLE_OUTBOUND=1`. No secret is
   ever sent to the browser.
3. **Caller isolation.** The caller identity comes from the call (BSUID or phone, HMAC-keyed),
   never from tool arguments. Tools have no identifier parameters. Cancels of other callers'
   bookings return the same `not_found` as unknown IDs.
4. **Confirmation gates.** `book_appointment` and `cancel_appointment` need `confirmed: true`
   (strict boolean). Never report a booking the tool did not confirm.
5. **No filler.** Tools use `pre_tool_speech: "off"` and no `tool_call_sound`; the soft-timeout
   filler is disabled (`timeout_seconds: -1`); no background sound; `expressive_mode` off; the
   prompt tells the agent not to say "one moment" or "let me check". Greetings must not contain
   filler and must say the caller is talking to an AI.
6. **Truthful recording notice.** Before a real agent session starts, `recording.py` reads the
   stored ElevenLabs agent back (read-only) and uses its `record_voice`, not `agent.toml`'s
   intent. The greeting says "This call is recorded." only when that is on or `LOCAL_CAPTURE=1`.
   If the setting cannot be read, no session starts (inbound `reject`, browser/outbound 503).
   The fake agent records nothing and is never read over the network. No Meta-native capture
   (`recording`/`transcription`) is requested by default.
7. **Call lifecycle.** Agent starts only after `accept` (inbound) or `ACCEPTED` (outbound).
   Terminate wins over connect in a batch. Repeated connects never start a second session.
   Recording/transcript completion events never start a session. Failure before accept sends
   `reject`; after accept sends `terminate`. Ambiguous outbound connects are never retried.
8. **Private artifacts.** Logs and `/operator/api/state` hold event names and hashed call
   references only: no audio, transcript text, SDP, phone numbers, raw Meta call IDs, media URLs
   or file paths. `DATA_DIR`, the SQLite store (and its journal) and capture/vendor files are
   0600/0700 under any umask (`private.py`). Local copies are pruned at startup, hourly and on
   each write, or with `voice-agent artifacts prune`. Pruning deletes only real directories with
   generated names (`private.CAPTURE_DIR`, `VENDOR_DIR`) and never follows a symlinked root or
   child. No HTTP route serves recordings.
9. **Bounded storage.** SQLite reads are index-backed and capped (`LIMIT`, booking horizon). One
   statement per transaction; never hold a transaction across network calls.
10. **Nothing private in Git.** `.env`, `data/`, `build/`, audio and SQLite files are ignored.
    Fixtures are synthetic (555-01xx numbers, `wacid.SYNTHETIC-*`). `scripts/secret_scan.py` and
    `tests/test_repo_hygiene.py` check this.

## Testing approach

- `uv run pytest` runs offline. Kapso is an `httpx.MockTransport` (`tests/conftest.py:KapsoRecorder`),
  ElevenLabs is a local WebSocket (`tests/test_call_flow.py:Harness`), and the caller is a local
  aiortc peer sending a tone. These tests use the real SmallWebRTC transport and Pipecat pipeline.
- The store takes a `clock`; tests pin it to Monday 2026-11-02 09:00 America/Chicago.
- Offline tests prove code paths, not what the LLM says. Prompt checks in pytest are structural
  (filler, disclosure, a recovery rule per error code). Conversation quality is checked by the
  real browser checklist and the opt-in provider tests in `docs/testing.md`.
- Never add a test that needs real credentials, places a call, sends a message or creates a
  provider resource. Live checks are manual operator steps (`docs/testing.md`).

## Not in scope here

- No second implementation behind a provider abstraction. If you add another voice runtime,
  add a module beside `bridge.py` and choose it in `calls.py`.
- Multi-process scaling: call state is in memory in one process. See `docs/deploy.md`.
