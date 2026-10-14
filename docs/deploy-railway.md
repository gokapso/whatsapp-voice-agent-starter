# Deploy on Railway

Railway gives the server an HTTPS domain, a volume and a managed process. It has no inbound UDP
([public networking](https://docs.railway.com/networking/public-networking/specs-and-limits)
lists HTTP, WebSocket and a TCP proxy), so call audio relays through TURN. This guide uses
[Cloudflare Realtime TURN](https://developers.cloudflare.com/realtime/turn/), which the server asks
for short-lived credentials before each call.

What was tested, and what was not:

| Layer | Status |
| --- | --- |
| Image starts as root on a root-owned volume, gives `DATA_DIR` to its user, drops root, listens on `$PORT` | Tested in local Docker (`scripts/smoke_turn_relay.py`) |
| Browser-style call with no direct route, audio only through a TURN relay over TCP | Tested in local Docker with coturn (3 of 3 runs) |
| `.railway/railway.ts` | Typechecks against `railway@3.16.0` and evaluates offline |
| A real Railway deployment, Cloudflare TURN credentials, WhatsApp audio through Railway | **Not tested yet** |

## 1. Prepare the env file

```sh
uv sync
uv run voice-agent setup --host railway     # voice provider, calendar, Cloudflare TURN keys, optional Kapso
uv run voice-agent check
```

Cloudflare TURN: create a TURN key in your Cloudflare account
([TURN docs](https://developers.cloudflare.com/realtime/turn/)) and copy its key ID and API token. `setup` stores them as `CLOUDFLARE_TURN_KEY_ID` and
`CLOUDFLARE_TURN_API_TOKEN` and sets `TURN_PROVIDER=cloudflare`. `TURN_TRANSPORT=tls` (the default)
relays over TCP port 443, which works without any outbound UDP.

With ElevenLabs, create the agent first (`voice-agent agent apply --yes --save-agent-id`), so the
env file has `ELEVENLABS_AGENT_ID`.

## 2. Create the project and deploy (Railway CLI)

This path deploys the code in your directory, including your own `agent/` files and any commits
not yet on GitHub. The commands come from the Railway CLI 5.12 help and were not yet run against a
Railway account; follow the CLI's prompts if they differ. `railway up` reads `.gitignore` and `.railwayignore`, so `.env` and `data/` stay
on your computer.

```sh
railway login
railway init --name whatsapp-voice-agent       # creates the project and links this directory
railway up --detach                            # creates the service from the Dockerfile; the first deploy is not ready yet
railway service                                # select the new service so the next commands target it
railway volume add --mount-path /app/data
uv run voice-agent railway variables           # dry run: lists the variable names it will set
uv run voice-agent railway variables --yes     # sets them; each value goes through stdin, never the command line
railway domain                                 # generates https://<name>.up.railway.app
railway up --detach                            # deploys with the variables and the volume
```

`voice-agent railway variables` also sets `RAILWAY_RUN_UID=0`, which Railway needs so the image can
take ownership of the root-owned volume (the entrypoint drops root before the server starts), and
`ALLOWED_HOSTS=${{RAILWAY_PUBLIC_DOMAIN}},healthcheck.railway.app,127.0.0.1`: Railway's deploy health
check uses the host `healthcheck.railway.app`
([docs](https://docs.railway.com/reference/healthchecks)), and the image's own health check uses
`127.0.0.1`.

Check it:

```sh
curl https://<name>.up.railway.app/healthz      # {"ok": true}
railway logs                                    # the startup banner lists what is missing; never a secret
```

## 3. Test in this order

1. **Console:** open `https://<name>.up.railway.app/operator/` and sign in with the console access
   key. It is the service's `OPERATOR_TOKEN` variable, the same value as in your env file
   (`uv run voice-agent console-key` copies it). The Setup card must show the voice provider as
   ready.
2. **Browser test call over TURN:** select **Start test call**. Open the event log: `rtc_connected`
   must show `media=relay`. This proves the server, the provider and the TURN path. If the call
   says the audio could not connect, check the Cloudflare keys and `TURN_TRANSPORT`.
3. **WhatsApp:** only after step 2 passes. Register the webhook with
   `uv run voice-agent kapso webhook --url https://<name>.up.railway.app/webhooks/whatsapp` (dry run,
   then `--yes`), then call the number from a phone. See `docs/testing.md` level 3. Each call
   rings a real phone and costs money.

## 4. Optional: Infrastructure as Code and a one-click template

`.railway/railway.ts` describes the same project: one service built from the Dockerfile, a 1 GB
volume at `/app/data`, the health check and one replica. It reads the code from
`gokapso/whatsapp-voice-agent-starter` on GitHub (change it to your fork), and keeps every secret
with `preserve()`, so set the variables (step 2) before you apply.

```sh
railway config plan      # preview, changes nothing
railway config apply     # asks before it changes anything
```

To make a **Deploy on Railway** button, deploy the project once, then in Railway open
**Project settings > Generate template from project**. Mark the provider keys as required
variables, and generate the local secrets in the template with `${{secret(43)}}`
(`OPERATOR_TOKEN`) and `${{secret(64, "abcdef0123456789")}}` (`CALLER_KEY_SECRET` and
`WHATSAPP_WEBHOOK_SECRET`). Publishing gives a URL like `https://railway.com/new/template/<code>`
for the button ([docs](https://docs.railway.com/guides/publish-and-share)). A template deploys
from GitHub, so it gets what is on the repository's default branch, not local commits.

## Security notes

- The console is on the public internet on Railway. It needs the console access key (43 random
  characters from `setup`) on every request, but anyone who has the key can start calls. Treat it
  like a password and rotate it (change `OPERATOR_TOKEN`, redeploy) if it leaks.
- The Cloudflare TURN API token stays on the server. Browsers and logs never see it or the
  per-call TURN credentials.
- Railway shows variable values to everyone with access to the project.

## Costs

Railway (compute, volume, egress), Cloudflare TURN (relayed traffic), the voice provider (per
minute) and WhatsApp calling through Kapso are billed separately. Check each provider's pricing.
