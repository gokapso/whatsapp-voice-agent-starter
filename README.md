# Kapso WhatsApp voice agent starter

Answer WhatsApp calls with your own AI voice agent. [Kapso](https://kapso.ai) delivers the
call; this small Python server answers it, streams the audio to an
[ElevenLabs Agents](https://elevenlabs.io/docs/eleven-agents) conversation, and runs the agent's
tools on your own server and data.

```
WhatsApp call → Kapso signed webhook → this server (WebRTC via Pipecat SmallWebRTC)
             ↔ ElevenLabs Agents (speech recognition, LLM, voice) ↔ your tools (local SQLite here)
```

The example is **Riley**, the AI front desk of **North Loop Bikes**, a fictional bike shop. Riley
says it is an AI, answers from the shop's data, offers open times, and books or cancels
appointments only after the caller clearly says yes. Callers can only see their own bookings.
Riley does not say "one moment" before lookups, play typing sounds or add background noise.

You get there in two steps:

1. **Talk to Riley in your browser.** Needs only an ElevenLabs API key. It runs the same prompt,
   tools and audio bridge that a WhatsApp call uses, so you can tune the agent before you touch
   a phone number.
2. **Connect WhatsApp through Kapso.** The browser is only a test client; the product is the
   WhatsApp call.

> **Status (2026-10-07): developer starter, not a hosted product.**
>
> - **WhatsApp:** this version, set up from a clean copy, handled one real outbound WhatsApp call
>   through Kapso to a phone (voice "River"). Riley said it is an AI and that the call is
>   recorded, looked up appointments, open times and shop details, read back a fictional
>   check-up and booked it only after a clear yes. Then Riley ended the call (about 1.5 minutes).
>   Audio flowed both ways, and the person on the phone confirmed the booking and was happy with
>   the call. Its recordings came from ElevenLabs and this server's local capture, not from Meta,
>   so they do not show in Kapso's Calls view (`docs/recordings.md`).
> - **Also checked:** the offline tests, the 14 browser checks in `docs/testing.md` with a real
>   ElevenLabs agent (synthetic speech, not a person), and a local Docker image (Linux arm64).
> - **Not checked yet:** inbound calls, a caller who hangs up first, declined or unanswered calls,
>   networks that need a TURN relay, a call through the Docker image, amd64 images, and the opt-in
>   provider tests. One call is a sample, not a reliability measure.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/). Dependencies are pinned in `uv.lock`
  (Pipecat 1.12.0, aiortc, FastAPI). No Kapso or ElevenLabs SDK is needed.
- An [ElevenLabs API key](https://elevenlabs.io/app/settings/api-keys) with access to Agents.
  Browser test calls use ElevenLabs Agents minutes.
- A current desktop browser with a microphone, on the same machine as the server.
- For step 2 only: a Kapso project and a WhatsApp number that can use Calling (see below).

## Step 1: talk to Riley in your browser

```sh
uv sync
uv run voice-agent init           # writes .env (0600) with a new operator token and local keys,
                                  # and prints the operator token
# Open .env and set ELEVENLABS_API_KEY=<your key>. Nothing else is needed for this step.

uv run voice-agent check          # offline: config, prompt, greetings, tool settings
uv run voice-agent agent plan     # offline: writes build/agent-config.json for you to review
uv run voice-agent agent apply --yes --save-agent-id   # creates the ElevenLabs agent and saves its ID in .env
uv run voice-agent agent verify   # reads the agent back and compares every setting
uv run voice-agent serve          # prints: Operator console: http://127.0.0.1:8080/operator/
```

Open `http://127.0.0.1:8080/operator/`, paste the operator token (`grep OPERATOR_TOKEN .env`
shows it again), select **Use token**, then **Start browser call** and allow the microphone.

Riley greets you and says it is an AI. Then try:

- "What are your hours?" and "Can I come in Monday?" (Monday is closed).
- "I'd like to book a brake check." Riley offers two times, asks for your first name, reads it
  all back and books only after you say yes.
- "What appointments do I have?", then cancel one.
- Talk over Riley while it speaks (after the greeting, which always plays in full); it stops
  and answers the new words.

The full checklist, with what each check proves, is in `docs/testing.md`.

**Recording.** `agent/agent.toml` asks ElevenLabs to keep call audio for 7 days
(`privacy.record_audio = true`), so the greeting says "This call is recorded." The server reads
the stored agent back before every session and the greeting follows what is really stored. To
turn it off, set `record_audio = false` and run `agent apply --yes` again. See
`docs/recordings.md`.

**What this proves:** your prompt, voice, tools, booking rules, interruptions and the audio
bridge, with a real conversation. **What it does not prove:** WhatsApp media networking, which
is the hard part of step 2.

## Step 2: connect WhatsApp through Kapso

When a caller dials your number, Meta sends a call webhook to Kapso. Kapso signs it and forwards
it to `POST /webhooks/whatsapp` on this server. The server checks the signature, answers through
Kapso's call API (`pre_accept`, then `accept`), and only after `accept` starts the same Riley
session you tested in the browser. When either side hangs up, the call ends on both.

Before you start:

- **A number that can use Calling.** A dedicated WhatsApp Cloud API number in your Kapso project,
  with Calling enabled (Kapso dashboard: Phone numbers → Enable calls). Kapso's messaging sandbox
  cannot take calls.
- **One answerer per number.** If the Kapso dashboard has a voice agent assigned to the number,
  choose: that agent or this server. Not both.
- **One Meta webhook per number.** `voice-agent kapso webhook` registers this server and refuses
  if the number already has one.
- **A media path.** Call audio is WebRTC over UDP between Meta and this server. A public HTTPS
  URL (reverse proxy or tunnel) carries webhooks only, **not audio**. Run the server on a host
  that Meta can reach over UDP, or add a TURN server. See `docs/deploy.md` §3.
- **Outbound calls** (optional, off by default) also need the person's call permission and
  `ENABLE_OUTBOUND=1`. Then use the console's **Outbound WhatsApp call** panel: check
  permission first, then call.

Then:

```sh
# In .env, add KAPSO_API_KEY and WHATSAPP_PHONE_NUMBER_ID (Meta's numeric ID, not the display number).
# init already generated WHATSAPP_WEBHOOK_SECRET; the next command gives it to Kapso.
uv run voice-agent check
# Stop the step 1 server first: settings are read once, when the server starts.
uv run voice-agent serve --host 127.0.0.1 --port 8080        # behind your public HTTPS URL
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp         # dry run
uv run voice-agent kapso webhook --url https://<host>/webhooks/whatsapp --yes   # registers it
```

Call the number from WhatsApp. The step-by-step guide, hosting options and the live call
checklist are in `docs/deploy.md` and `docs/testing.md`.

## Make it yours

| Change | File |
| --- | --- |
| Role, conversation flow, rules | `agent/prompt.md` |
| Greeting, voice, LLM, limits, recording | `agent/agent.toml` |
| Hours, services, closures, policies | `agent/business.json` |
| Tools and their storage | `src/kapso_voice_agent/tools.py`, `store.py` |

Using an AI coding agent? Point it at `AGENTS.md` and `docs/customize.md`. The guide has a
ready-made request for adapting Riley to your business and a recipe for connecting a real
calendar.

## Develop and test

`uv run pytest` runs the full offline suite (about 1.5 minutes, real local WebRTC, no network or
accounts). The development loop, the offline fake agent and the smoke scripts are in
`docs/development.md`. Offline tests check the code paths; they do not prove how the LLM
behaves. `docs/testing.md` lists the real checks.

## Docs

- `AGENTS.md`: commands, where to change things, rules the tests enforce
- `docs/customize.md`: adapt the business, prompt and tools; real calendar recipe
- `docs/testing.md`: offline tests vs real browser, provider and WhatsApp checks
- `docs/development.md`: offline development, fake agent, smoke scripts
- `docs/deploy.md`: hosting, networking, Kapso setup order, costs
- `docs/architecture.md`: call sequences and source map
- `docs/contracts.md`: HTTP routes, webhook events, tool results, provider protocol
- `docs/recordings.md`: ElevenLabs vs local vs Meta-native recordings and transcripts
- `docs/security.md`: public vs operator surfaces, caller isolation, logging

## License

MIT. See `LICENSE`.
