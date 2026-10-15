# Deploy and connect WhatsApp

This guide puts the server on Railway, points your Kapso number at it and checks a real phone
call. To hear the agent before you deploy, make a browser test call on your computer first
(README step 2).

Nothing here runs by itself. Each command that changes a provider is a dry run until you add
`--yes`.

## What the server needs

| Traffic | Path | What you provide |
| --- | --- | --- |
| Kapso webhooks and the console | HTTPS to `/webhooks/whatsapp` and `/operator/` | A public HTTPS URL |
| Call actions, provider, Cal.com | Outbound HTTPS and WebSocket | Normal internet access |
| **Call audio** | WebRTC (UDP) between Meta and the server | Railway or a server with a public IP |

The server also needs a long-lived process and a private disk for `DATA_DIR` (the map of which
caller owns which booking).

Run **one** server for each phone number. Call state is in memory, so the webhooks must reach
the server that carries the call audio.

## Railway (recommended)

Railway gives the server an HTTPS domain, a volume and a managed process. Railway receives no
inbound UDP, but WhatsApp call audio still connects: the server finds its public address with a
STUN server and sends its audio out to Meta's call servers.

### 1. Prepare the env file

```sh
uv run voice-agent setup --host railway --whatsapp   # the provider key, your Kapso API key and phone number ID
uv run voice-agent check
uv run voice-agent agent apply --yes --save-agent-id  # ElevenLabs only, if you have not created the agent
```

### 2. Deploy with the Railway CLI

`railway up` uploads this directory. It reads `.gitignore` and `.railwayignore`, so `.env` and
`data/` stay on your computer.

```sh
railway login
railway init --name whatsapp-voice-agent       # creates the project and links this directory
railway add --service voice-agent              # an empty service
railway service link voice-agent
railway volume add --mount-path /app/data
uv run voice-agent railway variables           # dry run: lists the variable names
uv run voice-agent railway variables --yes     # sets them; each value goes through stdin
railway domain                                 # gives https://<name>.up.railway.app
railway up --detach                            # builds the Dockerfile and deploys
```

`railway variables` also sets three Railway values:

- `RAILWAY_RUN_UID=0`: the image starts as root, gives the root-owned volume to its own user,
  then drops root before the server starts.
- `ALLOWED_HOSTS=${{RAILWAY_PUBLIC_DOMAIN}},healthcheck.railway.app,127.0.0.1`: Railway's health
  check uses the host `healthcheck.railway.app`, and the image's own health check uses `127.0.0.1`.
- `ICE_SERVERS_JSON` with a public STUN server, unless your `.env` file sets its own.
- `KAPSO_REGISTER_WEBHOOK=1`: at startup the server registers
  `https://$RAILWAY_PUBLIC_DOMAIN/webhooks/whatsapp` as the number's Meta webhook in Kapso, with
  `WHATSAPP_WEBHOOK_SECRET`. It does this only when the number has no Meta webhook, and never
  replaces one.

These commands were run with Railway CLI 5.12. If your CLI asks something else, follow its
prompts.

### 3. Check the server

1. `curl https://<name>.up.railway.app/healthz` returns `{"ok": true}`. `railway logs` shows the
   startup banner, which lists what is missing and never a secret.
2. Open `https://<name>.up.railway.app/operator/` and sign in. The console access key is the
   `OPERATOR_TOKEN` variable of the service. `uv run voice-agent console-key` copies the same
   value from your `.env` file.

Test this server with a WhatsApp call (below). A browser test call cannot connect its audio to
a server on Railway. Browser test calls work when the server runs on your computer or on a server
with a public IP.

### Railway notes

- The console is on the public internet. It needs the console access key on every request, but
  anyone with the key can start calls. Rotate the key if it leaks: change `OPERATOR_TOKEN` and
  redeploy.
- Railway shows variable values to everyone with access to the project.
- `.railway/railway.ts` describes the same project as code (one service, a 1 GB volume, the health
  check, one replica). `railway config plan` previews it. It keeps every secret with `preserve()`,
  so set the variables first.
- There is no published one-click Railway template for this starter yet. To make your own,
  deploy once, then use **Project settings > Generate template from project**
  ([docs](https://docs.railway.com/guides/publish-and-share)). A template deploys from GitHub,
  not from your local commits.

## Connect the number in Kapso

1. In the Kapso dashboard, enable Calling on a dedicated number (**Phone numbers > Enable
   calls**). Kapso's messaging sandbox is not a Calling number.
2. Make sure nothing else answers the calls: no dashboard voice agent on the number, and no
   answering from the Inbox.
3. Point the number's Meta webhook at the server. On Railway the server does it at startup
   (`KAPSO_REGISTER_WEBHOOK=1`); the console's WhatsApp row shows the result. On other hosts:

   ```sh
   uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp          # dry run
   uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp --yes    # stops if one exists
   ```

A number has only one Meta webhook. If one exists, neither the command nor the server replaces
it, and the console says so. Change it on purpose in the dashboard, because it also sends other
Meta events, such as messages, to this URL. Then restart the server. This server uses only
`calls` changes for `WHATSAPP_PHONE_NUMBER_ID` and ignores the rest.

On a call, Kapso signs the Meta event with `WHATSAPP_WEBHOOK_SECRET` and posts it to the server.
The server reads the recording setting, answers with `pre_accept` and `accept` through Kapso, and
starts the agent only after `accept`. The greeting waits until the call audio connects.

## Check with a phone

Every phone call rings a real phone and costs money. Get permission for each call.

1. Call the number from WhatsApp. Riley answers.
2. Book an appointment after a clear yes. Then hang up once yourself, and let Riley end one call.
3. In the console, **Recent calls** shows the call. In the event log, look for `pre_accept_ok` and
   `accept_ok`, then `rtc_connected` with `media=direct`, `first_audio_in` and `first_audio_out`.
   If `accept_ok` comes but `rtc_connected` does not, the call audio could not connect: the
   server ends the call after 30 seconds and logs `rtc_connect_timed_out`.

[`docs/testing.md`](testing.md#phone-check) has the full checklist.

## Other hosts

**A Linux server with a public IP.** Call audio reaches the server directly over UDP, and browser
test calls work too. [`examples/vps`](../examples/vps) has a Compose file with host networking and
Caddy for HTTPS. Choose `--host vps` in `setup`. Open the firewall to UDP: aiortc uses random
UDP ports. Keep `/operator` private (VPN or SSH tunnel) if you can.

```sh
docker compose up --build    # read compose.yaml first
```

**Pipecat Cloud (advanced).** Kapso's dashboard Pipecat provider can start one Pipecat Cloud
session for each call. The session runs this starter's call code, so you host no server. A
session starts with an empty disk, so a caller can change a booking only in the call that made
it. There is no console. See [`examples/pipecat-cloud`](../examples/pipecat-cloud). It is tested
offline only.

**Not supported:** serverless or HTTP-only platforms. A call needs a long-lived process and an
audio path.

## Other ways to run calls

- **The provider's own WhatsApp integration.** ElevenLabs can answer WhatsApp calls itself, but
  you import the number into ElevenLabs. Then the number is not in Kapso. This starter does not
  use that path.
- **Provider SIP.** Meta can send a number's calls over SIP to ElevenLabs or xAI. The number and
  its messages stay in Kapso, but SIP replaces the Calling API for that number, so Kapso call
  actions and Inbox answering stop. Today it is blocked: Meta's WhatsApp SIP offers only the Opus
  codec, and ElevenLabs and xAI document only G.711 and G.722. ElevenLabs says to contact them
  about SIP through another provider. Do not switch a number until a provider confirms Opus.

## What is proven

- Offline tests cover the webhook, the call lifecycle, real local WebRTC, both providers (with
  stand-ins) and the tools.
- On 2026-10-14 a real inbound WhatsApp call reached this server on Railway, with Grok Voice and
  the local calendar. The audio connected directly (`media=direct`) with only a STUN server.
  Riley answered a question, listed free times, booked an appointment after a clear yes and
  ended the call. A browser test call to the same server did not connect its audio.
- One real outbound WhatsApp call reached a handset on 2026-10-07 with ElevenLabs and earlier
  tools.
- Not yet on a real call: ElevenLabs on Railway, the Cal.com tools and outbound calls from
  Railway. No Pipecat Cloud deployment was made for this repository.

## Costs

These are separate bills: WhatsApp calling through Kapso, the voice provider (per minute),
hosting and bandwidth, and storage for recordings. Check each provider's
pricing. This repository does not state rates.
