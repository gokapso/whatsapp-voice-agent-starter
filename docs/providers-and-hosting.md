# Voice providers and hosting

Two choices decide how you run this server: **who runs the conversation** (the voice provider)
and **where this server runs** (the host). `uv run voice-agent setup` asks both and writes the
env file. This page explains the options and what each one was tested with.

The provider does not replace this server. With ElevenLabs or Grok, this server still answers the
WhatsApp call, carries its audio and runs the tools. For options without a server of yours
(Pipecat Cloud, provider SIP), see [`choose-where-calls-run.md`](choose-where-calls-run.md).

## Voice providers

| | ElevenLabs Agents (default) | Grok Voice (`VOICE_PROVIDER=grok`) |
| --- | --- | --- |
| What runs the call | An agent stored at ElevenLabs, created from `agent/` with `voice-agent agent apply` | xAI speech to speech, `grok-voice-3.0`, configured on every call from `agent/` |
| Keys | `ELEVENLABS_API_KEY`, then `ELEVENLABS_AGENT_ID` from `agent apply` | `XAI_API_KEY` only |
| Prompt, greetings, tools | Shared: `agent/prompt.md`, `agent/agent.toml`, the six tools in `tools.py` | Same files; tools are sent as xAI function tools, plus a local `end_call` |
| Voice and model settings | `agent.toml` `[voice]`, `[asr]`, `[turn]` | `agent.toml` `[grok]` (`model`, `voice`); `[asr]` keywords become transcription hints |
| Recording notice | Read back from the stored agent (`record_voice`) | Always "This call is recorded.": xAI keeps API traffic for 30 days unless your team has Zero Data Retention ([xAI security FAQ](https://docs.x.ai/docs/resources/faq-api/security)) |
| Offline stand-in | `voice-agent dev fake-agent` | `voice-agent dev fake-agent --provider grok` |
| Tested here | Unit and real-WebRTC tests with a fake agent; one real handset call on 2026-10-07 (earlier tools) | Unit and real-WebRTC tests with a scripted xAI stand-in; **not yet tested against the real xAI service** (see `docs/testing.md`) |

The Grok bridge follows xAI's [Speech to Speech docs](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech):
`wss://api.x.ai/v1/realtime?model=grok-voice-3.0`, 16 kHz PCM both ways, server VAD, the
greeting as a non-interruptible `force_message`, tool results as `function_call_output` and one
`response.create` after the response ends. `grok-voice-3.0` is pinned on purpose;
`grok-voice-latest` follows xAI's newest model ([release notes](https://docs.x.ai/developers/release-notes)).

## Hosts for this server

The server needs two kinds of network path. **Signaling** (Kapso's webhooks, the console, Kapso's
API) is ordinary HTTPS. **Call audio** is WebRTC: ICE, DTLS and SRTP over UDP between Meta (or your
browser) and this server. An HTTPS-only host can run the server, but audio then needs a TURN relay.

| Host | Signaling | Call audio | Data (`DATA_DIR`) | Status here |
| --- | --- | --- | --- | --- |
| Your computer (`serve`) | `http://127.0.0.1` | Direct to your browser | Local folder | Browser test calls tested |
| Linux VPS with a public IP ([`examples/vps`](../examples/vps)) | Caddy with automatic HTTPS | Direct UDP, `network_mode: host` | Docker volume | Compose and Caddy files validated; not deployed by this repository |
| [Railway](deploy-railway.md) | Railway's HTTPS domain | TURN relay (`TURN_PROVIDER=cloudflare`) because Railway has no inbound UDP | Railway volume at `/app/data` | Image, entrypoint and TURN-over-TCP relay tested in local containers (`scripts/smoke_turn_relay.py`); **not yet deployed to Railway** |
| Serverless or HTTP-only platforms | Possible | No: no long-lived process for the call, and no media path | No persistent disk | Not supported |

Run **one** server per phone number: call state is in memory, and a Railway volume cannot be
shared by replicas.

What each host needs, whichever you pick:

- **HTTPS** for the webhooks, and for the console, because browsers allow the microphone only on
  HTTPS pages or `localhost`.
- **A writable private disk** for `DATA_DIR` (the booking map and any captures). The server makes
  it 0700 and refuses to start if it does not own it.
- **A long-lived process**: a call keeps one WebSocket to the voice provider for its whole length.
- **Secrets in the host's secret store**, not in the image or the repository.

## Other places a call can run

[`choose-where-calls-run.md`](choose-where-calls-run.md) compares this server with Pipecat Cloud
(this starter's code, one session per call) and with ElevenLabs or xAI over SIP (blocked for
WhatsApp today). ElevenLabs' native WhatsApp integration needs the number imported into ElevenLabs,
so the number leaves Kapso. This starter does not use it.
