# Kapso WhatsApp voice agent starter

![Kapso WhatsApp Voice Agent: gokapso/whatsapp-voice-agent-starter](assets/kapso-voice-agent-starter.png)

Answer WhatsApp calls with your own AI front desk. Riley, the sample agent, says it is an AI
and answers questions about your business. After a clear yes, it books, moves or cancels
appointments in your [Cal.com](https://cal.com) calendar.

## How the parts fit

- **Kapso** owns your WhatsApp number, your messages and the call API.
- **This starter** is one small server. It answers the call through Kapso, carries the audio,
  runs the six calendar tools and gives you a console for test calls.
- **The voice provider** listens, thinks and speaks: [ElevenLabs Agents](https://elevenlabs.io/docs/eleven-agents)
  or [Grok Voice](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech).
  You choose one with `VOICE_PROVIDER`. This choice does not change where the server runs.
- **Railway** is the recommended host for the server. Railway has no inbound UDP, so call audio
  goes through a TURN relay. A Linux server with a public IP also works.

```
WhatsApp caller → Meta → Kapso ─signed webhook→ this server ←→ ElevenLabs or Grok
                                                    └──→ your tools → Cal.com
```

You run one server for one number, with either provider. You do not need a separate Railway
service for each provider. [`docs/deploy.md`](docs/deploy.md#other-ways-to-run-calls) explains
the other options and why a provider cannot answer a Kapso number directly today.

## Get started

### 1. Test in your browser

You need Python 3.12, [uv](https://docs.astral.sh/uv/), and an API key for one provider:
[ElevenLabs](https://elevenlabs.io/app/settings/api-keys) (with Agents access) or
[xAI](https://console.x.ai) for Grok Voice.

```sh
uv sync
uv run voice-agent setup    # choose the provider, the calendar and the host; keys are typed hidden
uv run voice-agent check
uv run voice-agent agent apply --yes --save-agent-id    # ElevenLabs only: creates the agent
uv run voice-agent serve
```

`setup` tells you what is left. For a first test, choose the local development calendar. For
real bookings, fill in `agent/business.json` and run `uv run voice-agent calendar check`
([`docs/customize.md`](docs/customize.md)).

Open <http://127.0.0.1:8080/operator/>. Sign in with the **console access key**:
`uv run voice-agent console-key` copies it to your clipboard. This key (`OPERATOR_TOKEN`) opens
the console of your server only. It is not a Kapso or provider key.

The console shows if you can test and the one next step. Select **Start test call**, allow the
microphone, and book, move or cancel an appointment. Each test call is a real provider session
that the provider bills per minute.

### 2. Host the server on Railway

Follow [`docs/deploy.md`](docs/deploy.md). In short:

1. Get a Cloudflare TURN key and run `uv run voice-agent setup --host railway`.
2. Deploy with the Railway CLI and set the variables with `uv run voice-agent railway variables --yes`.
3. Make a browser test call on the Railway URL.

### 3. Connect your WhatsApp number in Kapso

You need a Kapso project and a dedicated number with Calling enabled. Run
`uv run voice-agent setup --whatsapp` to add `KAPSO_API_KEY` and `WHATSAPP_PHONE_NUMBER_ID` to
your `.env` file. Copy the variables to the server again, then register its webhook:

```sh
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp         # dry run
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp --yes
```

### 4. Call the number from a phone

Call the number from WhatsApp. Riley answers. The console then shows the call under
**Recent calls**. A configured number is not proof: only a phone call shows that WhatsApp audio
reaches your server.

## Change the agent

Point your coding agent (Claude Code, Cursor, Codex) at [`AGENTS.md`](AGENTS.md) and
[`docs/customize.md`](docs/customize.md). The guide has a ready-made request that sets Riley up
for your business. The main files are [`agent/prompt.md`](agent/prompt.md),
[`agent/agent.toml`](agent/agent.toml), [`agent/business.json`](agent/business.json) and
[`src/kapso_voice_agent/tools.py`](src/kapso_voice_agent/tools.py).

## Docs

- [`docs/deploy.md`](docs/deploy.md): Railway, other hosts, Kapso and the phone check
- [`docs/customize.md`](docs/customize.md): your business, Cal.com, the prompt and the tools
- [`docs/testing.md`](docs/testing.md): offline tests, the browser checklist and the phone check
- [`docs/security.md`](docs/security.md): console access, caller isolation, recordings and logs
- [`docs/architecture.md`](docs/architecture.md): call flow, source map and contracts

## License

MIT. See [`LICENSE`](LICENSE).
