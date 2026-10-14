# Deploy and connect WhatsApp

Do README step 1 first: a browser test call must work on your computer. This guide then hosts
the server on Railway, connects your Kapso number and checks a real phone call.

Nothing here runs by itself. Each command that changes a provider is a dry run until you add
`--yes`.

## What the server needs

| Traffic | Path | What you provide |
| --- | --- | --- |
| Kapso webhooks and the console | HTTPS to `/webhooks/whatsapp` and `/operator/` | A public HTTPS URL |
| Call actions, provider, Cal.com | Outbound HTTPS and WebSocket | Normal internet access |
| **Call audio** | WebRTC (UDP) between Meta and the server | Open UDP, or a TURN relay |

An HTTPS URL alone does not carry call audio. If a call connects and nobody hears anything, the
audio path is almost always the cause. The server also needs a long-lived process and a private
disk for `DATA_DIR` (the map of which caller owns which booking).

Run **one** server for each phone number. Call state is in memory, so the webhooks must reach
the server that carries the call audio.

## Railway (recommended)

Railway gives the server an HTTPS domain, a volume and a managed process. Railway has no inbound
UDP, so call audio goes through [Cloudflare Realtime TURN](https://developers.cloudflare.com/realtime/turn/).
The server asks Cloudflare for short-lived TURN credentials before each call. With
`TURN_TRANSPORT=tls` (the default), the relay uses TCP port 443.

### 1. Prepare the env file

Create a TURN key in Cloudflare and copy its key ID and API token. Then:

```sh
uv run voice-agent setup --host railway --whatsapp   # asks for the TURN key and the Kapso values
uv run voice-agent check
uv run voice-agent agent apply --yes --save-agent-id  # ElevenLabs only, if you did not do it in step 1
```

### 2. Deploy with the Railway CLI

`railway up` uploads this directory. It reads `.gitignore` and `.railwayignore`, so `.env` and
`data/` stay on your computer.

```sh
railway login
railway init --name whatsapp-voice-agent       # creates the project and links this directory
railway up --detach                            # creates the service from the Dockerfile
railway service                                # select the new service
railway volume add --mount-path /app/data
uv run voice-agent railway variables           # dry run: lists the variable names
uv run voice-agent railway variables --yes     # sets them; each value goes through stdin
railway domain                                 # gives https://<name>.up.railway.app
railway up --detach                            # deploys with the variables and the volume
```

`railway variables` also sets two Railway values:

- `RAILWAY_RUN_UID=0`: the image starts as root, gives the root-owned volume to its own user,
  then drops root before the server starts.
- `ALLOWED_HOSTS=${{RAILWAY_PUBLIC_DOMAIN}},healthcheck.railway.app,127.0.0.1`: Railway's health
  check uses the host `healthcheck.railway.app`, and the image's own health check uses `127.0.0.1`.

These commands come from the Railway CLI 5.12 help. They were not run against a Railway account
for this repository. If the CLI asks something else, follow its prompts.

### 3. Test before WhatsApp

1. `curl https://<name>.up.railway.app/healthz` returns `{"ok": true}`. `railway logs` shows the
   startup banner, which lists what is missing and never a secret.
2. Open `https://<name>.up.railway.app/operator/` and sign in. The console access key is the
   `OPERATOR_TOKEN` variable of the service. `uv run voice-agent console-key` copies the same
   value from your `.env` file.
3. Select **Start test call**. In the technical event log, `rtc_connected` must show
   `media=relay`. This proves the provider and the TURN path to your browser.

If the audio does not connect, check the Cloudflare key and `TURN_TRANSPORT`.

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
3. Register the server as the number's Meta webhook:

   ```sh
   uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp          # dry run
   uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp --yes    # stops if one exists
   ```

A number has only one Meta webhook. If one exists, the command stops. Change it on purpose in the
dashboard, because it also sends other Meta events, such as messages, to this URL. This server
uses only `calls` changes for `WHATSAPP_PHONE_NUMBER_ID` and ignores the rest.

On a call, Kapso signs the Meta event with `WHATSAPP_WEBHOOK_SECRET` and posts it to the server.
The server reads the recording setting, answers with `pre_accept` and `accept` through Kapso, and
starts the agent only after `accept`. The greeting waits until the call audio connects.

## Check with a phone

Every phone call rings a real phone and costs money. Get permission for each call.

1. Call the number from WhatsApp. Riley answers.
2. Book an appointment after a clear yes. Then hang up once yourself, and let Riley end one call.
3. In the console, **Recent calls** shows the call. In the event log, look for `pre_accept_ok` and
   `accept_ok`, then `rtc_connected`, `first_audio_in` and `first_audio_out`.

[`docs/testing.md`](testing.md#phone-check) has the full checklist.

## Other hosts

**A Linux server with a public IP.** Call audio reaches the server directly over UDP, so no TURN
relay is needed. [`examples/vps`](../examples/vps) has a Compose file with host networking and
Caddy for HTTPS. Choose `--host vps` in `setup`. Open the firewall to UDP: aiortc uses random
UDP ports. Keep `/operator` private (VPN or SSH tunnel) if you can.

```sh
docker compose up --build    # read compose.yaml first
```

**Behind NAT or in Docker bridge networking.** Use a TURN relay: `TURN_PROVIDER=cloudflare`, or
your own TURN server in `ICE_SERVERS_JSON`. aiortc uses only the first STUN URL and the first
TURN URL, so put the one you want first.

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
  stand-ins), the tools and the TURN relay in local Docker containers.
- One real outbound WhatsApp call reached a handset on 2026-10-07 with ElevenLabs and earlier
  tools. The Cal.com tools, Grok Voice and Railway have not been on a real call yet.
- No Railway, Cloudflare TURN or Pipecat Cloud deployment was made for this repository.

## Costs

These are separate bills: WhatsApp calling through Kapso, the voice provider (per minute),
hosting and bandwidth, the TURN relay, and storage for recordings. Check each provider's
pricing. This repository does not state rates.
