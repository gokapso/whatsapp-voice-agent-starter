# Deploy and Host a WhatsApp Voice Agent (Kapso) on Railway

![Kapso WhatsApp Voice Agent](https://raw.githubusercontent.com/gokapso/whatsapp-voice-agent-starter/main/assets/kapso-voice-agent-starter.png)

Answer WhatsApp calls with your own AI front desk. Riley, the sample agent, says it is an AI,
answers questions about your business, and books, moves or cancels appointments after a clear
yes. Your number and messages stay in [Kapso](https://kapso.ai). This server carries the call
audio and runs the tools; [Grok Voice](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech)
listens, thinks and speaks.

## About Hosting the WhatsApp Voice Agent

You fill in three values: your xAI API key, your Kapso project API key and the Meta phone number
ID of your Kapso number. Railway generates the server's own secrets.

On its first start, the server registers its Railway URL as your number's webhook in Kapso, if the
number has none, and never replaces an existing one. Then call the number from WhatsApp, and Riley
answers. Open `https://<your-domain>/operator/` and sign in with the `OPERATOR_TOKEN` variable to
see recent calls, the setup and an event log.

Before you deploy, in the Kapso dashboard:

- Enable Calling on a dedicated number.
- Set the number's **Incoming call answering** to **Agent or integration** (no Inbox fallback),
  and do not assign a dashboard voice agent to it.

Test this server with a WhatsApp call. Browser test calls from the console need the server on
your computer or on a server with a public IP.

To have the agent call people, set `ENABLE_OUTBOUND=1` on the service. The console then asks a
person to allow calls with a WhatsApp message, and calls them once they do.

## Common Use Cases

- An AI receptionist that answers WhatsApp calls and books appointments
- A starting point for building calling on Kapso with your own coding agent
- A test bench for voice agent prompts and tools on real WhatsApp calls

## Dependencies for WhatsApp Voice Agent Hosting

- A [Kapso](https://kapso.ai) project with a WhatsApp number that has Calling enabled
- An [xAI](https://console.x.ai) API key for Grok Voice

### Deployment Dependencies

- Source: [gokapso/whatsapp-voice-agent-starter](https://github.com/gokapso/whatsapp-voice-agent-starter)
- Setup, customization and security: [`README.md`](https://github.com/gokapso/whatsapp-voice-agent-starter#readme),
  [`docs/deploy.md`](https://github.com/gokapso/whatsapp-voice-agent-starter/blob/main/docs/deploy.md),
  [`docs/customize.md`](https://github.com/gokapso/whatsapp-voice-agent-starter/blob/main/docs/customize.md)

### Implementation Details

- One service, built from the repository's Dockerfile: Python, Pipecat and WebRTC. It keeps a
  1 GB volume at `/app/data` for the map of which caller owns which booking.
- Call audio leaves the server over UDP and reaches Meta's call servers directly. A public STUN
  server is set in `ICE_SERVERS_JSON`; no TURN relay is needed.
- `CALENDAR=local` is a sample calendar for a first test. For real bookings, set `CAL_API_KEY`
  and `CALENDAR=calcom`, and describe your business in `agent/business.json`.
- To change the agent, eject the template into your own repository and edit `agent/prompt.md`,
  `agent/agent.toml` and `agent/business.json`.
- Each call is a Grok Voice session that xAI bills per minute. WhatsApp calling and Railway usage
  are billed separately.
- One server answers one number. Call state is in memory, so keep one replica.

## Why Deploy the WhatsApp Voice Agent on Railway?

Railway is a singular platform to deploy your infrastructure stack. Railway will host your
infrastructure so you don't have to deal with configuration, while allowing you to vertically and
horizontally scale it.

By deploying the WhatsApp Voice Agent on Railway, you are one step closer to supporting a complete
full-stack application with minimal burden. Host your servers, databases, AI agents, and more on
Railway.
