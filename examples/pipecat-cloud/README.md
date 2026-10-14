# Pipecat Cloud: this starter's call code, hosted per call

Pipecat Cloud runs one session of this starter for each WhatsApp call. Kapso keeps the number,
the messages, the webhooks and the call log. You host no server, VPS or Railway service.

```
caller ─> Meta ─> Kapso ─start session─> Pipecat Cloud: bot.py -> kapso_voice_agent.cloud
caller <── WebRTC audio ──> Cloud session <─WebSocket─> ElevenLabs or Grok
                            Cloud session ─Kapso call API─> pre_accept, accept, terminate
                            Cloud session ─> Cal.com
```

## How a call works

1. Meta sends the call to Kapso. Kapso finds the voice agent assigned to the number. With the
   Pipecat provider, Kapso starts a session for your Pipecat Cloud agent. The session body holds
   the Meta webhook (`kind: "whatsapp_connect"`).
2. `cloud.py` looks the call up through Kapso's API with `KAPSO_API_KEY`. The call must be on your
   number, user-initiated, claimed for a voice agent and not over. Otherwise the session answers
   nothing. The body is not signed, so this lookup is the check.
3. The caller identity comes from Kapso's saved call, not from the session body.
4. The session answers like `voice-agent serve`: `pre_accept`, `accept` through Kapso, and then
   the agent starts. The same prompt, greeting, recording notice and six tools apply.
5. The call ends with `terminate` through Kapso. The session then exits.

The session body also has a `whatsapp_token` field. This code never reads it. Call actions go
through Kapso, so Kapso's call log and Inbox state stay correct.

## What is different from a server

- **Bookings across calls.** A Cloud session starts with an empty disk. A caller can move or
  cancel a booking in the same call, but not in a later call. Later calls find nothing, and they
  never see another caller's booking. For repeat callers, use option A on a VPS or Railway.
- **No console and no outbound calls.** Test in the browser with `voice-agent serve` on your
  computer first.
- **No local recordings.** Leave `LOCAL_CAPTURE=0`.

## Secrets

The session reads these from a Pipecat Cloud secret set:

| Secret | Value |
| --- | --- |
| `KAPSO_API_KEY`, `WHATSAPP_PHONE_NUMBER_ID` | Your Kapso project key and Meta's numeric phone number ID |
| `CALLER_KEY_SECRET` | The same value as on any other server for this number |
| `VOICE_PROVIDER` and its keys | `ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID`, or `XAI_API_KEY` |
| `CAL_API_KEY` | Cal.com key. Do not use `CALENDAR=local` here |
| `TURN_PROVIDER`, `CLOUDFLARE_TURN_*` | Optional. Add them if audio does not connect |

The session does not need `OPERATOR_TOKEN` or `WHATSAPP_WEBHOOK_SECRET`. To write the secret set
file from your tested `.env`, run:

```sh
uv run voice-agent export-env --for pipecat-cloud --file build/pipecat-cloud.env
```

The file is 0600 and holds secrets. Upload it, and then delete it.

## Deploy

You need a Pipecat Cloud account and the Pipecat CLI. Install the CLI and sign in as described in
[Pipecat Cloud's docs](https://docs.pipecat.ai/pipecat-cloud/introduction). Pipecat Cloud runs
`linux/arm64` images.

1. Check the code offline:

   ```sh
   uv run pytest tests/test_cloud_launch.py
   ```

2. Build the image from the repository root, and push it to your registry:

   ```sh
   docker buildx build --platform linux/arm64 -f examples/pipecat-cloud/Dockerfile \
     -t YOUR_REGISTRY/kapso-voice-agent-cloud:0.1.0 --push .
   ```

3. Upload the secrets, and then delete the local file:

   ```sh
   pipecat cloud secrets set kapso-voice-secrets --file build/pipecat-cloud.env
   rm build/pipecat-cloud.env
   ```

4. Set `image` in `pcc-deploy.toml`. Then deploy from this directory. Keep the session limit a
   little above `MAX_SESSION_SECONDS` (default 320):

   ```sh
   cd examples/pipecat-cloud
   pipecat cloud deploy --max-session-duration 400
   ```

5. In the Kapso dashboard, create a voice agent with the Pipecat provider. Enter your Pipecat
   public API key and the agent name `kapso-voice-agent`. Assign it to the number.

Before step 5, make sure nothing else answers this number. Remove this server's webhook from the
number, and do not answer its calls in the Inbox.

## Test in this order

1. `uv run pytest tests/test_cloud_launch.py`: launch checks, with real local WebRTC.
2. Local container, no network calls. Start the image and send two bodies it must ignore:

   ```sh
   docker build -f examples/pipecat-cloud/Dockerfile -t kapso-voice-agent-cloud:local .
   docker run --rm -d --name kva-cloud -p 127.0.0.1:18931:8080 \
     -e WHATSAPP_PHONE_NUMBER_ID=100000000000001 -e CALENDAR=local kapso-voice-agent-cloud:local
   curl -s -X POST http://127.0.0.1:18931/bot -H 'content-type: application/json' -d '{"kind":"other"}'
   docker logs kva-cloud | grep kapso-voice-agent     # kapso-voice-agent ignored_kind
   docker rm -f kva-cloud
   ```

3. A browser test call with `voice-agent serve` and the same `.env`. This checks the provider and
   the calendar.
4. One WhatsApp call to the number. Read the session log with `pipecat cloud agent logs`.

## Read the session log

Each session prints one line: `kapso-voice-agent <outcome> <events>`. Events hold no phone numbers,
call IDs or audio.

| Outcome or event | Meaning |
| --- | --- |
| `finished` with `accept_ok`, `rtc_connected`, `agent_session_ready` | The call worked |
| `unverified` | Kapso did not vouch for the call. Check `KAPSO_API_KEY` and `WHATSAPP_PHONE_NUMBER_ID` |
| `ignored_other_phone_number` | The secret set is for another number |
| `not_configured` | A key is missing. `voice-agent check` on your `.env` lists it |
| `accept_ok` but no `rtc_connected` | No media path. Add `TURN_PROVIDER=cloudflare` and its keys |
| `reject_ok` | The session failed before it answered, for example an unknown recording setting |

## Undo

1. In the Kapso dashboard, unassign or turn off the voice agent. Calls then ring as before.
2. Stop or delete the Pipecat Cloud agent, and delete the secret set.

## What was proven

Offline only. The launch tests answer a real local WebRTC call through mocked Kapso. Pipecat's
base image (`dailyco/pipecat-base:0.2.5-py3.12`) loaded this `bot.py` in a local container.
Pipecat documents WhatsApp with SmallWebRTC on Pipecat Cloud, but this example has not been
deployed. No real Kapso launch or WhatsApp call went through it.
