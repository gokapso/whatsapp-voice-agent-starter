# Development

Everything on this page runs offline: no accounts, no network, no phone. These are development
checks. They exercise the code paths (webhooks, call lifecycle, WebRTC, tools, storage), not the
agent's conversation. For that, see `docs/testing.md`.

## Test suite

```sh
uv sync
uv run pytest                     # ~1.5 min; real local WebRTC, mocked Kapso and ElevenLabs
uv run pytest tests/test_tools.py # one file
uvx ruff check src tests scripts  # lint (rules in pyproject.toml)
python3 scripts/secret_scan.py    # run before every commit
```

How the tests stand in for the outside world:

- Kapso is an `httpx.MockTransport` (`tests/conftest.py:KapsoRecorder`).
- ElevenLabs is a local WebSocket (`tests/test_call_flow.py:Harness`). Its read-back of the
  stored agent's recording setting is a fixture (`provider_recording_readback`).
- The caller is a local aiortc peer that sends a tone. The real SmallWebRTC transport and Pipecat
  pipeline run in between.
- The store clock is pinned to Monday 2026-11-02 09:00 America/Chicago. Monday is closed.

## Run the tools by hand

```sh
uv run voice-agent tools schema                        # definitions the provider receives
uv run voice-agent tools call business_info '{"topic": "services"}'
uv run voice-agent tools call available_slots '{"day": "2026-11-03"}' --now 2026-11-02T09:00:00-06:00
uv run voice-agent tools call book_appointment \
  '{"slot": "2026-11-03T10:00:00-06:00", "name": "Sam", "service_id": "brakes", "confirmed": true}' \
  --now 2026-11-02T09:00:00-06:00
uv run voice-agent tools call my_appointments --now 2026-11-02T09:00:00-06:00
uv run voice-agent tools call my_appointments --caller someone-else --now 2026-11-02T09:00:00-06:00   # empty
```

These use `data/offline-tools.sqlite3` and the caller label `demo` unless you pass `--db` and
`--caller`. The results are the exact JSON the agent receives.

## Browser call with the offline fake agent

The fake agent stands in for ElevenLabs. It plays a short tone as its "greeting", runs one
`available_slots` call and echoes your microphone back. It is **not a conversation**; use it to
check the media path and the console without spending provider minutes.

```sh
uv run voice-agent dev init-env              # writes .env.dev (0600): operator token, caller key, fake agent URL
                                             # and prints the operator token
uv run voice-agent dev fake-agent &          # ws://127.0.0.1:8765
uv run voice-agent --env-file .env.dev serve # prints the console URL
# open http://127.0.0.1:8080/operator/ and paste the token (grep OPERATOR_TOKEN .env.dev shows it again)
```

`DEV_AGENT_WS_URL` accepts loopback addresses only. The fake agent records nothing and is never
read over the network, so its greeting has no recording sentence unless `LOCAL_CAPTURE=1`. Stop
the background fake agent with `kill %1` when you are done.

## Smoke scripts

Both start real server processes on free loopback ports with temporary data, and stop them at
the end.

```sh
uv run python scripts/smoke_setup.py   # init, check and serve in a clean environment; then a fake-agent browser call
uv run python scripts/smoke_local.py   # health, webhook signature, operator auth, fake-agent browser call
```

`smoke_setup.py` gives the processes only `PATH`, a temporary `HOME` and `DATA_DIR`, and the env
files the commands under test write. It checks that `init` writes a private `.env` and refuses to
overwrite it, that `check` reports only the ElevenLabs agent as missing, that `serve` prints the
console URL and no secret, and that a browser call is refused with a clear reason until the
agent is set up.

## Provider config without a provider

```sh
uv run voice-agent agent plan                          # build/agent-config.json
uv run voice-agent agent plan --schema openapi.json    # optional structural check against a downloaded ElevenLabs OpenAPI file
ELEVENLABS_OPENAPI=openapi.json uv run pytest tests/test_agent_config.py
```
