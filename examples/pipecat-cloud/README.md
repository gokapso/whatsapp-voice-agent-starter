# Pipecat Cloud (advanced)

Pipecat Cloud can run this starter's call code once for each WhatsApp call, so you host no
server. Kapso keeps the number, the messages and the call log. The recommended path is still one
server on Railway ([`docs/deploy.md`](../../docs/deploy.md)).

**Status:** tested offline only. Pipecat's base image (`dailyco/pipecat-base:0.2.5-py3.12`)
loaded `bot.py` in a local container. Nothing was deployed to Pipecat Cloud, and no real Kapso
launch or WhatsApp call went through it.

## How a call works

1. Kapso's dashboard voice agent with the Pipecat provider starts a session for each call. The
   session body holds the Meta webhook.
2. `cloud.py` looks the call up through Kapso's API. The call must be on your number,
   user-initiated, claimed for a voice agent and not over. The body is not signed, so this lookup
   is the check. The caller identity comes from Kapso's saved call.
3. The session answers like `voice-agent serve`: `pre_accept`, `accept`, then the agent starts.
   The prompt, greeting, recording notice and six tools are the same.

The body also has a `whatsapp_token` field. This code never reads it.

## Limits

- **Bookings across calls.** Each session starts with an empty disk, so the map of which caller
  owns which booking is lost after each call. A caller can move or cancel a booking only in the
  call that made it. Later calls find nothing, and never see another caller's booking.
- No console and no outbound calls. Test in the browser with `voice-agent serve` first.
- No local recordings. Leave `LOCAL_CAPTURE=0`.

## Deploy

You need a Pipecat Cloud account and the [Pipecat CLI](https://docs.pipecat.ai/pipecat-cloud/introduction).
Pipecat Cloud runs `linux/arm64` images.

```sh
uv run pytest tests/test_cloud_launch.py
uv run voice-agent export-env --for pipecat-cloud --file build/pipecat-cloud.env   # 0600, holds secrets
docker buildx build --platform linux/arm64 -f examples/pipecat-cloud/Dockerfile \
  -t YOUR_REGISTRY/kapso-voice-agent-cloud:0.1.0 --push .
pipecat cloud secrets set kapso-voice-secrets --file build/pipecat-cloud.env
rm build/pipecat-cloud.env
cd examples/pipecat-cloud && pipecat cloud deploy --max-session-duration 400   # set image in pcc-deploy.toml first
```

The secret set needs `KAPSO_API_KEY`, `WHATSAPP_PHONE_NUMBER_ID`, `CALLER_KEY_SECRET` (the same
value everywhere this number is answered), the provider keys and `CAL_API_KEY`. `export-env`
checks these.

Then, in the Kapso dashboard, create a voice agent with the Pipecat provider. Enter your Pipecat
public API key and the agent name `kapso-voice-agent`, and assign the agent to the number. First remove
this server's webhook from the number, so only one thing answers the calls.

To undo, unassign the voice agent in Kapso, then delete the Pipecat Cloud agent and secret set.

## Read the session log

Each session prints one line: `kapso-voice-agent <outcome> <events>`. It holds no phone numbers,
call IDs or audio.

| Outcome or event | Meaning |
| --- | --- |
| `finished` with `accept_ok`, `rtc_connected` | The call worked |
| `unverified` | Kapso did not vouch for the call. Check `KAPSO_API_KEY` and `WHATSAPP_PHONE_NUMBER_ID` |
| `not_configured` | A key is missing. `voice-agent check` on your `.env` lists it |
| `accept_ok` but no `rtc_connected` | No audio path between Meta and the session |
