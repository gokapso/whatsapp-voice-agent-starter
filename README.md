# Kapso WhatsApp voice agent starter

Answer WhatsApp calls with your own AI voice agent. [Kapso](https://kapso.ai) delivers the call
to this small Python server, which streams the audio to an
[ElevenLabs Agents](https://elevenlabs.io/docs/eleven-agents) conversation and runs the agent's
tools on your own server and data.

```
WhatsApp call → Kapso signed webhook → this server (Pipecat SmallWebRTC)
             ↔ ElevenLabs Agents (speech, LLM, voice) ↔ your tools (local SQLite)
```

The starter includes:

- **Riley**, the AI front desk of North Loop Bikes, a fictional bike shop. Riley says it is an
  AI, answers questions about hours and services, offers open times, and books or cancels
  repairs after a clear yes.
- Five tools that run on your server: `business_info`, `available_slots`, `my_appointments`,
  `book_appointment` and `cancel_appointment`. Each caller sees only their own bookings.
- The agent as config: a prompt, a TOML file and business data that you plan, apply and
  verify against ElevenLabs from the command line.
- A browser test client that runs the same agent, tools and audio bridge as a WhatsApp call.
- WhatsApp calls through Kapso: signed webhooks, answer and hang up, and optional outbound calls.

## Using coding agents

This project works with coding agents like Claude Code, Cursor and Codex. Point yours at
[`AGENTS.md`](AGENTS.md) and [`docs/customize.md`](docs/customize.md). The guide has a
ready-made request that turns Riley into the front desk for your business.

To change things by hand, start with [`agent/prompt.md`](agent/prompt.md),
[`agent/agent.toml`](agent/agent.toml), [`agent/business.json`](agent/business.json) and
[`src/kapso_voice_agent/tools.py`](src/kapso_voice_agent/tools.py).

## Talk to Riley in your browser

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and an
[ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys) with Agents access.

```sh
uv sync
uv run voice-agent init       # writes a private .env and prints your operator token
# set ELEVENLABS_API_KEY in .env
uv run voice-agent check
uv run voice-agent agent apply --yes --save-agent-id   # creates the agent on ElevenLabs
uv run voice-agent serve
```

Open <http://127.0.0.1:8080/operator/>, paste the operator token, select **Use token**, then
**Start browser call** and allow the microphone. Ask for the shop's hours, book a brake check,
then cancel it.

By default ElevenLabs keeps call audio for 7 days and Riley says the call is recorded. To turn
it off, see [`docs/recordings.md`](docs/recordings.md).

## Connect WhatsApp through Kapso

You need a Kapso project with a dedicated WhatsApp number that has Calling enabled, and a host
for this server:

- **Public HTTPS** for the webhooks from Kapso.
- **UDP to Meta** for the call audio, or a TURN server in `ICE_SERVERS_JSON`. An HTTPS tunnel
  alone does not carry audio. See [`docs/deploy.md`](docs/deploy.md#3-networking-signaling-vs-media).

Add `KAPSO_API_KEY` and `WHATSAPP_PHONE_NUMBER_ID` to `.env`, restart the server, then register
its webhook with Kapso:

```sh
uv run voice-agent check
uv run voice-agent serve
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp         # dry run
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp --yes
```

Call the number from WhatsApp and Riley answers. If the number already has a webhook or a
dashboard voice agent, see [step 6 of the deploy guide](docs/deploy.md#6-connect-the-number-kapso).

## Docs

- [`docs/deploy.md`](docs/deploy.md): hosting, networking, Kapso setup and costs
- [`docs/customize.md`](docs/customize.md): your business, prompt, tools and a real calendar
- [`docs/development.md`](docs/development.md): offline tests and development loop
- [`docs/testing.md`](docs/testing.md): browser, provider and WhatsApp checks
- [`docs/recordings.md`](docs/recordings.md): recordings and transcripts
- [`docs/security.md`](docs/security.md): operator access, caller isolation and logging
- [`docs/architecture.md`](docs/architecture.md) and [`docs/contracts.md`](docs/contracts.md):
  call flow, source map and HTTP contracts

## License

MIT. See [`LICENSE`](LICENSE).
