# Deploy and connect to Kapso

Nothing in this guide runs by itself. Each step that changes a provider is an explicit
operator command with `--yes`, run after you read its dry-run output.

## 1. Before you start

- Step 1 of the README done: a real browser conversation with your agent works on your machine.
- A Kapso project with a **dedicated Cloud API number** that Meta allows to use Calling. Kapso's
  messaging sandbox is not a Calling test number. Check Kapso's Calling setup guide for Meta's
  current eligibility rules.
- An ElevenLabs account with Agents access, and the voice you want available to that account.
- A Cal.com account with an event type for each service, and `agent/business.json` filled in
  (`docs/calendar.md`).
- A host for this server with a public HTTPS URL **and** a working media path (section 3).

## 2. Configure

On the host (or copy your tested `.env` there; it holds secrets, keep it 0600):

```sh
uv run voice-agent init           # writes .env (0600) with OPERATOR_TOKEN, CALLER_KEY_SECRET, WHATSAPP_WEBHOOK_SECRET
# set ELEVENLABS_API_KEY, CAL_API_KEY, KAPSO_API_KEY and WHATSAPP_PHONE_NUMBER_ID in .env
uv run voice-agent check          # offline; fix every reported problem
uv run voice-agent calendar check # reads your Cal.com account; fix every reported problem
```

`init` never overwrites an existing file. `voice-agent secrets` prints fresh values if you
manage the environment another way (for example a secret store feeding the container).

## 3. Networking: signaling vs media

| Traffic | Path | What you must provide |
| --- | --- | --- |
| Webhooks from Kapso | HTTPS POST to `/webhooks/whatsapp` | Public HTTPS URL (reverse proxy, load balancer or tunnel) to port 8080 |
| Call actions + SDP | This server → `https://api.kapso.ai` | Outbound HTTPS |
| Agent conversation | This server → `wss://api.elevenlabs.io` | Outbound HTTPS/WSS |
| Calendar | This server → `https://api.cal.com` | Outbound HTTPS |
| **Call audio** | WebRTC (ICE/DTLS/SRTP over UDP) between Meta and this server | A reachable address for this server's media, or a TURN relay |

An HTTPS tunnel for port 8080 delivers webhooks but **does not carry audio**. If the call
connects and nobody hears anything, it is almost always the media path.

Options for media:

1. **Host with a public IP, no NAT in the way** (simplest): run directly or with Docker
   `network_mode: host` (Linux). Allow inbound/outbound UDP. aiortc uses random UDP ports;
   it has no port-range setting, so a firewall must allow the ephemeral range.
2. **Behind NAT or in Docker bridge networking:** add a TURN server to `ICE_SERVERS_JSON`, for
   example `[{"urls":"turn:turn.example.com:3478","username":"u","credential":"p"}]`. A STUN
   entry only helps when the NAT is friendly; TURN relays audio when nothing else works.
3. Managed WebRTC (Daily, Pipecat Cloud) is a different integration and was not tested with this
   code.

`ICE_SERVERS_JSON=[]` (the default) worked in the tested handset call, where the server ran
directly on a machine whose network allowed direct media. Do not assume it works on yours.

## 4. Create the agent (ElevenLabs)

Do this before you start the server: the server reads `.env` once, when it starts. Skip `apply`
if you already created the agent in README step 1 and copied its ID into this `.env`.

```sh
uv run voice-agent agent plan                    # writes build/agent-config.json, no network
uv run voice-agent agent apply --save-agent-id   # dry run: says create or update, and where the ID goes
uv run voice-agent agent apply --yes --save-agent-id   # creates; writes ELEVENLABS_AGENT_ID to .env
uv run voice-agent agent verify                  # read-back comparison, exit 1 on drift
```

`--save-agent-id` checks that the env file exists, is a regular file, has no agent ID yet and
sets `ELEVENLABS_AGENT_ID` at most once, before anything is sent. It changes only that line and
keeps the file 0600. Without the flag, copy the printed `agent_id` into `.env` yourself.

`agent verify` compares everything the repository sends: settings, the full prompt text, and each
tool's description, parameters, required fields and response settings. Fields the provider adds
by itself are ignored; an extra or missing tool, an extra parameter, a tool sound or a background
sound counts as drift. The comparison is tested against mocked read-backs only. If the provider
returns tools in another shape (for example only tool IDs), `verify` reports them as missing
rather than passing; check its output the first time you use it.

When the server starts a real call it also reads the stored agent's `record_voice` (read-only) to
pick the recording sentence. If that read fails, the call is not answered. See
`docs/recordings.md`.

`apply --yes` updates an existing agent only if its stored name equals `agent.toml`'s
`agent.name`. Optional: download the provider's OpenAPI JSON and run
`voice-agent agent plan --schema path/to/openapi.json` for a structural check.

If a server is already running when you save or change `ELEVENLABS_AGENT_ID`, it keeps the old
value: restart `serve`, or recreate the container with `docker compose up -d --force-recreate`
(Compose reads `env_file` only when it creates the container).

## 5. Run

```sh
uv run voice-agent serve --host 127.0.0.1 --port 8080        # behind your HTTPS proxy
# or
docker compose up --build                                     # read compose.yaml comments first
```

`serve` prints the operator console URL and what is still missing for browser and WhatsApp
calls; it never prints a secret. The container image is not built by this repository's checks:
build it, run it and check `/healthz` and a browser call before you rely on it.

Build the image from a normal checkout. The image runs as a non-root user and `COPY` keeps file
modes. If you cloned or extracted the source with a strict umask such as `077`, the copied files
are readable only by root and the image fails when it starts. Clone again from a shell with
`umask 022` and rebuild. Do not loosen the mode of `.env`; it is not part of the image.

The server makes `DATA_DIR` private (0700) at startup and needs to own it. It deletes expired
local captures and downloads at startup and hourly; if it is often stopped, also schedule
`voice-agent artifacts prune`.

The process holds call state in memory. Run **one** instance per phone number. Webhooks must
reach the same instance that owns a call's media. To scale out you would need shared call
state and sticky routing by call ID; that is not implemented.

Browser check on the host: open `https://<host>/operator/` (over your VPN or SSH tunnel, see
`docs/security.md`), paste the operator token, select **Start browser call**. Your browser needs
a media path to the server too, so a browser call that works here is a good sign for WhatsApp
media, not a proof.

## 6. Connect the number (Kapso)

1. Enable Calling on the number in the Kapso dashboard (**Phone numbers → Enable calls**) and
   confirm Meta reports it enabled. This starter does not automate that step.
2. In the dashboard, check whether a voice agent is already assigned to the number. Decide which
   one answers: the dashboard agent or this server. Not both.
3. Register this server as the number's Meta webhook:

```sh
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp          # dry run
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp --yes    # refuses if one exists
```

Only one Meta webhook is allowed per number. If one exists, the command stops; update it
deliberately in the dashboard (it also changes where other Meta events go, such as messages).
This server uses only `calls` changes for `WHATSAPP_PHONE_NUMBER_ID` and ignores the rest, so if
the number also receives messages, route those elsewhere first.

### What happens on a call

1. Meta sends the `connect` event to Kapso; Kapso signs it with `WHATSAPP_WEBHOOK_SECRET` and
   posts it to `/webhooks/whatsapp`. Unsigned or wrongly signed requests get 401.
2. The server reads the agent's recording setting (it sends `reject` if it cannot), builds the WebRTC
   answer, and sends `pre_accept`, then `accept`, through Kapso's call API.
3. Only after `accept` does the Riley session start, so the greeting is never spoken into a call
   that is not connected.
4. The call ends when Riley uses `end_call` (the server sends `terminate`), when the caller hangs
   up (Meta's `terminate` webhook), or at `MAX_SESSION_SECONDS`.

Details and the outbound sequence: `docs/architecture.md`. Recording sources (ElevenLabs, local
capture, Meta native): `docs/recordings.md`. This starter does not request Meta-native recording
or transcription, and it does not transfer calls to a person.

## 7. Live verification (manual, needs explicit authorization for each call)

Follow `docs/testing.md` level 3. A greeting alone does not prove a working conversation. Check
`/operator/api/state` events after each call.

## Costs to plan for

Separate bills apply: WhatsApp calling through Kapso (Kapso credits can be used to pay for your
WhatsApp calls), the voice provider (ElevenLabs Agents usage), your hosting and bandwidth, a TURN
service if you use one, and storage for any recordings you keep. Check each provider's current
pricing; this repository does not state rates.
