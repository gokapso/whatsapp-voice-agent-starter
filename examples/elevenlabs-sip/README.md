# ElevenLabs over SIP: the provider answers the call (blocked)

**Status: blocked. Do not switch a number yet.** Meta's WhatsApp SIP calls offer only the Opus
codec. ElevenLabs documents only G.711 and G.722 for SIP trunks. This folder prepares the setup and
shows each step as a dry run. Nothing here changes Meta, Kapso or ElevenLabs.

```
caller ─> Meta ─SIP INVITE (TLS, port 5061)─> sip.rtc.elevenlabs.io ─> your ElevenLabs agent
          Meta <────────────── SRTP audio ──────────────> ElevenLabs
          Meta ─call_created, terminate webhooks─> Kapso (no SDP, no audio)
```

## What runs where

- **Audio and conversation:** ElevenLabs. There is no media server or bridge of yours.
- **You host:** nothing for the minimal agent. Railway and a VPS are not needed.
- **Kapso keeps:** the number and its messages. Meta still sends `call_created` and `terminate`
  webhooks to Kapso.
- **Kapso stops:** answering the calls. With SIP on, the number "will not work with Graph APIs"
  (Meta's SIP guide). Kapso call actions, Inbox answering, Kapso dashboard agents and this
  starter's server do not answer its calls.

## Blockers

1. **Codec.** Meta's user-initiated INVITE offers `opus/48000/2` and `telephone-event` only. The
   ElevenLabs SIP guide and API list `G722/8000`, `PCMU/8000` and `PCMA/8000` only. ElevenLabs'
   [WhatsApp FAQ](https://elevenlabs.io/docs/eleven-agents/whatsapp/troubleshooting) describes
   routing calls through another provider's SIP, and says to contact them. Get written
   confirmation that their SIP trunk answers an Opus-only offer.
2. **Media keys.** Meta uses DTLS-SRTP by default, or SDES. ElevenLabs documents SRTP but not its
   key exchange, and does not document DTLS or ICE. The plan switches the number to SDES. Confirm
   SDES with ElevenLabs.
3. **Tools.** This starter's six tools are client tools. A client runs them, and on SIP there is no
   client. Use the minimal agent below, or ElevenLabs' own Cal.com integration.

## The agent

Create a new agent in the ElevenLabs dashboard. Do not reuse the agent that
`voice-agent agent apply` made: its tools would wait for a client that is not there.

- Use [`prompt.md`](prompt.md). It answers questions from facts in the prompt and does not book.
- Pick the first message that matches the agent's "Record voice" setting. The greeting must say
  the caller talks to an AI.
- Turn on the end-call system tool, so the agent can hang up after its goodbye.

### Optional: ElevenLabs' Cal.com integration

ElevenLabs has a [native Cal.com integration](https://elevenlabs.io/docs/eleven-agents/customization/integrations/calcom).
It checks availability, books, cancels, reschedules and looks up bookings with your Cal.com API
key. ElevenLabs runs it; you host nothing. Its rules are not the rules of this starter:

| | This starter's tools | ElevenLabs' Cal.com integration |
| --- | --- | --- |
| Who can see or change a booking | Only the caller who made it, by the call's identity | Not documented. Lookups use details the caller says, such as an email |
| Confirmation | `confirmed: true` (a strict boolean) before any change | Prompt instructions only |
| Failed or unknown writes | Reported as not done, never retried | Not documented |

Use it only if these differences are acceptable for your business. This starter does not set it up.

### Custom tools

ElevenLabs webhook tools call an HTTPS endpoint that you host, on Railway or any HTTPS host. This
starter does not provide that endpoint. If you build one, take the caller from the call. Never take
it from a value that the model fills in. ElevenLabs turns inbound `X-` SIP headers into dynamic
variables. By that rule, Meta's `x-wa-meta-user-id` becomes `{{sip_wa_meta_user_id}}`. This mapping
is not tested.

## Settings and secrets

| Where | What |
| --- | --- |
| `.env` (for `sip status` and `sip plan`) | `KAPSO_API_KEY`, `WHATSAPP_PHONE_NUMBER_ID` |
| ElevenLabs phone number | The number in E.164 form, digest username (the number without `+`), Meta's SIP password |
| Meta settings (through Kapso) | `calling.sip` server `sip.rtc.elevenlabs.io:5061`, `webhook_delivery`, `srtp_key_exchange_protocol` |

Meta makes the SIP password, one for each number and app. Read it only to type it into the
ElevenLabs dashboard. The commands in this repository never ask Meta for it and never print it.

## Dry run

```sh
uv run voice-agent sip status                                          # read-only
uv run voice-agent sip plan --provider elevenlabs --number +15550100000
```

`sip status` says who answers the number's calls now. `sip plan` prints the blockers, the mode
change, the ElevenLabs phone number body, the Meta settings body and the undo body.

## Test order, after the blockers are resolved

1. Run `sip status`. Keep its output, because you need it to undo.
2. Create the agent, then import the number as a SIP trunk with the plan's values.
3. Check the TLS certificate the way Meta's guide does:
   `openssl s_client -quiet -verify_hostname sip.rtc.elevenlabs.io -connect sip.rtc.elevenlabs.io:5061`
4. Save the plan's Meta settings body as `build/sip-apply.json`. Put `KAPSO_API_KEY` and
   `WHATSAPP_PHONE_NUMBER_ID` in your shell environment. Then send the body through Kapso:

   ```sh
   printf 'X-API-Key: %s\n' "$KAPSO_API_KEY" | curl -sS -H @- -H 'Content-Type: application/json' \
     --data @build/sip-apply.json "https://api.kapso.ai/meta/whatsapp/v24.0/$WHATSAPP_PHONE_NUMBER_ID/settings"
   ```

   The key goes through stdin, so it is not in the process list.
5. Run `sip status` again. It must show SIP to `sip.rtc.elevenlabs.io:5061`.
6. Make one call from WhatsApp. Check the conversation in ElevenLabs' call history.
7. Undo (below) if anything fails.

| What you see | Likely cause |
| --- | --- |
| No call in ElevenLabs | TLS or hostname problem. Run step 3 again |
| The call fails at once | Codec (blocker 1) |
| The call connects with no audio | Media keys (blocker 2), or Meta waits for the first SRTP packet |
| Repeated authentication failures | Digest username or password does not match Meta's |

## Undo

Send the plan's undo body the same way as step 4. Then run `sip status`. It must show the Calling
API again. With SIP `DISABLED`, Meta keeps the server entry but does not use it. Then remove the
number from ElevenLabs.
