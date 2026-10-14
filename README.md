# Kapso WhatsApp voice agent starter

Answer WhatsApp calls with your own AI front desk. [Kapso](https://kapso.ai) delivers the call
to this small Python server, which streams the audio to an
[ElevenLabs Agents](https://elevenlabs.io/docs/eleven-agents) conversation or to
[Grok Voice](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech), and runs the
agent's tools on your own server, against your real [Cal.com](https://cal.com) calendar.

```
WhatsApp call → Kapso signed webhook → this server (Pipecat SmallWebRTC)
             ↔ ElevenLabs Agents or Grok Voice (speech, LLM, voice) ↔ your tools ↔ Cal.com
```

The voice provider runs the conversation. This server answers the WhatsApp call and carries its
audio, also with ElevenLabs. Your number and messages stay in Kapso.
[`docs/choose-where-calls-run.md`](docs/choose-where-calls-run.md) compares this with Pipecat Cloud
and with provider-hosted SIP.

The starter includes:

- **Riley**, an AI receptionist for your business. Riley says it is an AI, answers questions
  from your business facts, offers real open times from your calendar, and books, moves or
  cancels appointments after a clear yes.
- Six tools that run on your server: `business_info`, `available_slots`, `my_appointments`,
  `book_appointment`, `reschedule_appointment` and `cancel_appointment`. Bookings go into
  Cal.com, and each caller sees only their own.
- The agent as config: a prompt, a TOML file and your business facts that you plan, apply and
  verify against ElevenLabs from the command line.
- A browser test client that runs the same agent, tools and audio bridge as a WhatsApp call.
- WhatsApp calls through Kapso: signed webhooks, answer and hang up, and optional outbound calls.

## Using coding agents

This project works with coding agents like Claude Code, Cursor and Codex. Point yours at
[`AGENTS.md`](AGENTS.md) and [`docs/customize.md`](docs/customize.md). The guide has a
ready-made request that sets Riley up as the front desk for your business.

To change things by hand, start with [`agent/prompt.md`](agent/prompt.md),
[`agent/agent.toml`](agent/agent.toml), [`agent/business.json`](agent/business.json) and
[`src/kapso_voice_agent/tools.py`](src/kapso_voice_agent/tools.py).

## Talk to Riley in your browser

You need Python 3.12, [uv](https://docs.astral.sh/uv/), a key for one voice provider and a
calendar:

- **Voice:** an [ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys) with Agents
  access, or an [xAI API key](https://console.x.ai) for Grok Voice (`grok-voice-3.0`).
- **Calendar:** a Cal.com account with an event type for each service, or the local development
  calendar to try things first.

```sh
uv sync
uv run voice-agent setup      # asks for voice provider, calendar and host; keys are typed hidden
uv run voice-agent check
# ElevenLabs only, after `calendar check` passes: uv run voice-agent agent apply --yes --save-agent-id
uv run voice-agent serve
```

`setup` writes a private `.env` and tells you what is left: with ElevenLabs, create the agent with
`uv run voice-agent agent apply --yes --save-agent-id`; with Cal.com, fill in `agent/business.json`
([`docs/calendar.md`](docs/calendar.md)) and run `uv run voice-agent calendar check`.

Open <http://127.0.0.1:8080/operator/> and sign in with the **console access key**. It is a
password for this server's console (`OPERATOR_TOKEN` in `.env`), not a provider key.
`uv run voice-agent console-key` copies it to your clipboard. Then select **Start test call** and
allow the microphone. Book an appointment, then move or cancel it.

With ElevenLabs, calls are recorded for 7 days by default and Riley says so; see
[`docs/recordings.md`](docs/recordings.md). Grok Voice calls always say they are recorded, because
xAI keeps API traffic for 30 days.

## Connect WhatsApp through Kapso

You need a Kapso project with a dedicated WhatsApp number that has Calling enabled, and a host
for this server ([`docs/providers-and-hosting.md`](docs/providers-and-hosting.md) compares them:
a VPS with a public IP, or Railway with a TURN relay). To run each call in Pipecat Cloud instead,
see [`examples/pipecat-cloud`](examples/pipecat-cloud). This server needs:

- **Public HTTPS** for the webhooks from Kapso.
- **UDP to Meta** for the call audio, or a TURN server in `ICE_SERVERS_JSON`. An HTTPS tunnel
  alone does not carry audio. See [`docs/deploy.md`](docs/deploy.md#3-networking-signaling-vs-media).

Add `KAPSO_API_KEY` and `WHATSAPP_PHONE_NUMBER_ID` to `.env` and restart the server with
`uv run voice-agent serve`. In another terminal, register its webhook with Kapso:

```sh
uv run voice-agent check
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp         # dry run
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp --yes
```

Call the number from WhatsApp and Riley answers. If the number already has a webhook or a
dashboard voice agent, see [step 6 of the deploy guide](docs/deploy.md#6-connect-the-number-kapso).

## Docs

- [`docs/choose-where-calls-run.md`](docs/choose-where-calls-run.md): this server, Pipecat Cloud or provider SIP; what each needs and what is proven
- [`docs/calendar.md`](docs/calendar.md): Cal.com setup, what each tool does, and a walkthrough to record
- [`docs/providers-and-hosting.md`](docs/providers-and-hosting.md): ElevenLabs or Grok Voice, and where to host this server
- [`docs/deploy.md`](docs/deploy.md): hosting, networking, Kapso setup and costs
- [`docs/deploy-railway.md`](docs/deploy-railway.md): Railway with a TURN relay, and a one-click template
- [`docs/customize.md`](docs/customize.md): your business, greeting, prompt and tools
- [`docs/development.md`](docs/development.md): offline tests and development loop
- [`docs/testing.md`](docs/testing.md): browser, provider and WhatsApp checks
- [`docs/recordings.md`](docs/recordings.md): recordings and transcripts
- [`docs/security.md`](docs/security.md): operator access, caller isolation and logging
- [`docs/architecture.md`](docs/architecture.md) and [`docs/contracts.md`](docs/contracts.md):
  call flow, source map and HTTP contracts
- Examples: [`examples/pipecat-cloud`](examples/pipecat-cloud) (each call in Pipecat Cloud),
  [`examples/elevenlabs-sip`](examples/elevenlabs-sip) and [`examples/xai-sip`](examples/xai-sip)
  (provider SIP; blocked for WhatsApp today), [`examples/vps`](examples/vps)

## License

MIT. See [`LICENSE`](LICENSE).
