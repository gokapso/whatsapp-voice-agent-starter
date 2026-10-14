# xAI Grok Voice over SIP: control service (blocked for WhatsApp)

**Status: blocked for Kapso WhatsApp numbers.** xAI documents Direct SIP for phone carriers,
contact centers and PBXs. Its guide does not mention WhatsApp. This folder explains the control
service that this starter now includes (`/webhooks/xai-sip`). It is tested offline only.

With xAI SIP, Grok is not serverless. xAI handles the audio, but you host a small service. It
receives xAI's signed incoming-call webhook, joins the call's session over WebSocket, configures
the agent and runs the tools.

```
caller ─> SIP trunk ─SIP (TLS)─> sip.voice.x.ai           audio: trunk <──> xAI
          xAI ─signed realtime.call.incoming─> this server: POST /webhooks/xai-sip
          this server ─wss://api.x.ai/v1/realtime?call_id=...─> session.update, greeting, tools
          this server ─POST /v1/realtime/calls/{call_id}/hangup─> xAI
```

## Why WhatsApp does not work yet

For a WhatsApp number, Meta is the "SIP trunk". Meta's documents and xAI's documents do not match:

1. **Codec.** Meta's WhatsApp SIP calls offer only `opus/48000/2`. xAI's SIP guide lists G.711
   (PCMU, PCMA) and G.722 only.
2. **Media encryption.** Meta sends SRTP only (DTLS or SDES keys). xAI's SIP guide does not mention
   SRTP.
3. **Scope.** xAI does not document WhatsApp as a SIP source.
4. **Model.** On SIP sessions xAI ignores the `model` parameter. You cannot pin `grok-voice-3.0`.

Also, SIP replaces the Calling API for that number. Kapso call actions, Inbox answering and Kapso
dashboard agents stop answering its calls. Messages stay in Kapso.

`uv run voice-agent sip plan --provider xai --number +15550100000 --control-url https://voice.example.com/webhooks/xai-sip`
prints these blockers with the xAI registration body, the Meta settings body and the undo body.
It sends nothing.

## What the control service does

- It checks the Standard Webhooks signature (`webhook-id`, `webhook-timestamp`,
  `webhook-signature`) on the raw body before it parses the body. Signatures older than five minutes
  fail.
- It starts one session per `call_id`. It ignores repeated deliveries. Over `MAX_CONCURRENT_CALLS`,
  it hangs up the call.
- It takes the caller from the call's SIP headers: `x-wa-meta-user-id` if present, else the From
  user. Tool arguments never choose the caller. It keys the caller with `CALLER_KEY_SECRET`, as for
  every other call.
- It sends this starter's prompt, voice and six tools, then the greeting as a non-interruptible
  message. The greeting always says that the call is recorded, because xAI keeps API traffic for
  30 days by default.
- It runs the tools with the same `ToolRunner`: the caller-owned booking map, and `confirmed: true`
  as a strict boolean before any change.
- It hangs up after the agent's goodbye (`end_call`), at `MAX_SESSION_SECONDS`, or when the session
  fails. xAI plays the audio, so the server cannot see when the goodbye ends. It waits four
  seconds.

## Hosting

The control service is this starter's server (`voice-agent serve`). It needs:

- public HTTPS for the webhook,
- a long-lived process, for one WebSocket per call,
- a private disk for the booking map (`DATA_DIR`).

It needs no UDP and no TURN relay. Railway works without the TURN settings; a VPS also works.
Serverless platforms do not, because a call keeps a WebSocket open for its whole length.

## Settings and secrets

| Setting | Value |
| --- | --- |
| `XAI_API_KEY` | xAI API key. It opens the call's session and hangs up |
| `XAI_SIP_WEBHOOK_SECRET` | The `whsec_...` signing secret. xAI returns it once, when you register the number |
| `CALLER_KEY_SECRET`, `CAL_API_KEY` | As for any call |

With `XAI_SIP_WEBHOOK_SECRET` empty, `/webhooks/xai-sip` returns 404.

## Test order (for a phone trunk, or for WhatsApp after the blockers are resolved)

1. `uv run pytest tests/test_xai_sip.py`: signature, one session per call, caller identity,
   strict confirmation, hang-up.
2. Deploy this server, and check `https://<host>/healthz`.
3. Register the number with `POST https://api.x.ai/v2/phone-numbers` (the plan shows the body).
   Store the signing secret as `XAI_SIP_WEBHOOK_SECRET`, then restart the server.
4. Point the trunk at `sip:{number}@sip.voice.x.ai;transport=tls`.
5. Make one call. The operator event log shows `sip_session_connected`, `agent_session_ready`,
   `tool_..._ok` and `sip_hangup_ok`.

## Undo

Delete the number at xAI (`DELETE https://api.x.ai/v2/phone-numbers/{phone_number_id}`), and remove
`XAI_SIP_WEBHOOK_SECRET`. For a WhatsApp number, also send the plan's undo body to Meta through Kapso.
