# Choose where your calls run

All four options keep your WhatsApp number and messages in Kapso. They differ in where the call
audio goes and what you must host. Two questions decide the option:

- **Who runs the conversation?** ElevenLabs Agents or Grok Voice. This is the voice provider.
- **What answers the WhatsApp call and moves its audio?** This server, a Pipecat Cloud session,
  or the provider itself over SIP.

| | A. This server (default) | B. Pipecat Cloud | C. ElevenLabs SIP | D. xAI SIP |
| --- | --- | --- | --- | --- |
| Example | README, [`examples/vps`](../examples/vps), [Railway](deploy-railway.md) | [`examples/pipecat-cloud`](../examples/pipecat-cloud) | [`examples/elevenlabs-sip`](../examples/elevenlabs-sip) | [`examples/xai-sip`](../examples/xai-sip) |
| Call audio goes to | This server (WebRTC), then the provider (WebSocket) | A Pipecat Cloud session (WebRTC), then the provider | ElevenLabs (SIP) | xAI (SIP) |
| You host | This server: VPS, Railway with TURN, or your computer for browser tests | A Pipecat Cloud agent. No VPS or Railway | Nothing for a minimal agent | This server, for webhooks and tools only. No UDP or TURN |
| Who answers the call | This server, through Kapso's call API | The Cloud session, through Kapso's call API | Meta sends the call to ElevenLabs | Meta sends the call to xAI |
| Kapso keeps | Number, messages, signed webhooks, call log, Inbox call state | The same as A | Number and messages. Kapso call actions, Inbox answering and dashboard agents stop answering calls | The same as C |
| Tools | The six tools on this server, with the caller-owned booking map | The six tools in the session. The booking map is lost after each call | None, or ElevenLabs' own Cal.com integration | The six tools on this server |
| Status | Works offline and in the browser. One real handset call (2026-10-07, earlier tools) | Offline tests and a local container check. Not deployed | **Blocked**: codec. Dry-run plan only | **Blocked for WhatsApp**: codec and SRTP. Offline tests only |

Run one option per number. Kapso's dashboard voice agent, Inbox answering, this server's webhook
and SIP must not all answer one number.

## A. This server answers the call (default)

```
caller ─WhatsApp─> Meta ─calls webhook─> Kapso ─signed POST─> this server
caller <──── WebRTC audio (UDP, or a TURN relay) ────> this server <─WebSocket─> ElevenLabs or Grok
                                                       this server ─> Cal.com, booking map
```

This server answers through Kapso's call API (`pre_accept`, `accept`, `terminate`). Kapso logs the
call, and the Inbox shows its state. Railway and a VPS are both valid hosts. See
[`providers-and-hosting.md`](providers-and-hosting.md).

**Why ElevenLabs still needs this server.** Kapso gives you the call as a WebRTC offer. An
ElevenLabs agent takes audio over its WebSocket API, not a WhatsApp WebRTC offer. A stored agent
does not change this. Something must answer the WebRTC call, copy the audio both ways and run the
client tools. In option A, that is this server. Grok Voice works the same way.

ElevenLabs' native WhatsApp integration does not need this server. It needs the number imported
into ElevenLabs, so the number leaves Kapso. This starter does not use that path.

## B. Pipecat Cloud runs this server's code for each call

```
caller ─> Meta ─> Kapso ─start session (dashboard Pipecat provider)─> Pipecat Cloud: cloud.py
caller <──── WebRTC audio ────> Pipecat Cloud session <─WebSocket─> ElevenLabs or Grok
                                Pipecat Cloud session ─Kapso call API─> pre_accept, accept, terminate
```

Kapso's dashboard Pipecat provider starts one Cloud session per call. The session runs this
starter's own call code. It checks the call with Kapso first, then answers through Kapso. You
host nothing else. Each session starts with an empty disk, so a caller can change a booking only
in the call that made it. See [`examples/pipecat-cloud`](../examples/pipecat-cloud).

Pipecat Cloud's own `/whatsapp` webhook URL is for direct Meta apps (Meta token and app secret).
It does not take Kapso's webhook, and this example does not use it.

## C and D. The provider answers over SIP

```
caller ─> Meta ─SIP INVITE (TLS)─> ElevenLabs or xAI        audio: Meta <─SRTP─> provider
                                    xAI only: signed webhook ─> this server ─WebSocket─> session control, tools
```

Meta can send a number's calls over SIP instead of the Calling API. Then nothing of yours carries
audio. But SIP replaces the Calling API for that number. Kapso call actions, Inbox answering and
Kapso dashboard agents no longer answer its calls. With `webhook_delivery` on, Kapso still gets
`call_created` and `terminate` webhooks, without SDP, recordings or transcripts.

**Both SIP options are blocked today**, from the providers' own documents:

- Meta's WhatsApp SIP calls offer only the Opus codec. ElevenLabs and xAI document only G.711 and
  G.722 for SIP. An answer in another codec fails.
- xAI's SIP guide does not mention SRTP. Meta sends encrypted media only.
- xAI documents SIP for phone carriers and PBXs, not WhatsApp.

ElevenLabs' WhatsApp FAQ describes SIP routing through another provider, and says to contact them.
Until a provider confirms Opus, use `voice-agent sip plan` as a dry run only. Each SIP example lists
the blockers, the requests and the undo request.

## Railway in each option

| Option | Railway |
| --- | --- |
| A | Optional. One of the hosts for this server. It needs a TURN relay, because Railway has no inbound UDP |
| B | Not needed. Pipecat Cloud is the host |
| C | Not needed. Custom ElevenLabs webhook tools need some HTTPS service, and Railway is one choice |
| D | Optional. The control service needs HTTPS and a long-lived process, but no UDP or TURN |

## What is proven

| Option | Offline tests | Real provider | Deployed host | WhatsApp handset |
| --- | --- | --- | --- | --- |
| A, ElevenLabs | Yes, with real local WebRTC | Earlier tools only | No | One outbound call, 2026-10-07 |
| A, Grok | Yes, scripted xAI | No | No | No |
| B | Yes: launch checks, real local WebRTC, container with Pipecat's base image | No | No | No |
| C | Dry-run plan only | No | Not applicable | No |
| D | Yes: signed webhook, scripted session | No | No | No |

Live checks follow [`testing.md`](testing.md). Each example README lists its own test order.
