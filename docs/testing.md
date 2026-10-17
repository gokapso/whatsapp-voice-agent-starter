# Testing

There are three levels. Each level proves something that the level before cannot.

| Level | Needs | Proves | Does not prove |
| --- | --- | --- | --- |
| 1. Offline | Nothing | Webhook and call rules, WebRTC bridge, tool results, caller isolation, confirmation gates, private files, setup commands | What the LLM says; your Cal.com account |
| 2. Browser call | A provider key and a calendar | Prompt, voice, tool use, real bookings, interruptions, disclosure | The WhatsApp audio path |
| 3. Phone call | A hosted server and a Kapso number with Calling | The full call | Other networks, load |

The front desk prompt and the Cal.com tools have not been through levels 2 and 3 yet. Run your
own checks after you change the prompt, voice, tools or host.

## Level 1: offline

```sh
uv sync
uv run pytest                      # about 1.5 min; real local WebRTC, mocked Kapso and providers
uvx ruff check src tests scripts
python3 scripts/secret_scan.py     # run before every commit
```

The tests stand in for the outside world:

- Kapso and Cal.com are `httpx.MockTransport`s (`tests/conftest.py:KapsoRecorder`,
  `tests/test_calcom.py:FakeCalCom`, with the documented Cal.com API v2 shapes).
- ElevenLabs and xAI are local WebSockets (`tests/test_call_flow.py`, `tests/test_grok_bridge.py`).
- The caller is a local aiortc peer that sends a tone, through the real SmallWebRTC transport
  and Pipecat pipeline.
- The clock is Monday 2026-11-02 09:00 America/Chicago.

With `CALENDAR=local`, you can run each tool by hand and see the exact JSON the agent gets:

```sh
export CALENDAR=local
uv run voice-agent tools schema
uv run voice-agent tools call available_slots '{"day": "2026-11-03"}' --now 2026-11-02T09:00:00-06:00
```

### Browser call with the offline stand-in

The fake agent plays a tone, runs one `available_slots` call and echoes your microphone. It is
not a conversation. Use it to check the audio path and the console without provider cost.

```sh
uv run voice-agent dev init-env                  # .env.dev (0600) with the local calendar
uv run voice-agent dev fake-agent &              # ws://127.0.0.1:8765
uv run voice-agent --env-file .env.dev serve
uv run voice-agent --env-file .env.dev console-key
```

For Grok Voice, add `--provider grok` to `dev init-env` and `dev fake-agent`.

### Smoke scripts

These start real server processes on free loopback ports with temporary data:

```sh
uv run python scripts/smoke_setup.py      # init, check and serve in a clean environment, then a stand-in call
uv run python scripts/smoke_local.py      # health, webhook signature, console access, stand-in call
```

### Inbox handoff (experimental)

`tests/test_handoff.py` uses three local peers with their own tones (caller 440 Hz, AI 1000 Hz,
teammate 700 Hz) and checks what each one hears before and after the switch, the fallbacks and the
end of the call. Kapso's handoff API is an in-memory state machine there.

`scripts/handoff_local_demo.py` runs the same server against a Kapso app on your computer, with a
local caller, a fake Grok agent that asks for a teammate after 4 seconds, and Meta's call actions
answered in the script. Join from that app's Inbox with a microphone (or a browser with a fake
700 Hz microphone); the script prints which tone the caller hears each second.

## Level 2: browser call

Use a separate env file and data directory, so the test agent and its bookings stay apart from
your normal `.env` and `data/`. Set up Cal.com first ([`customize.md`](customize.md)). Create and
verify the agent before you start the server: the server reads its settings once, at start.

```sh
uv run voice-agent --env-file .env.acceptance init
echo 'DATA_DIR=data/acceptance-riley' >> .env.acceptance   # the booking map and downloads go here, not in data/
# set ELEVENLABS_API_KEY and CAL_API_KEY in .env.acceptance; fill in agent/business.json
uv run voice-agent --env-file .env.acceptance check
uv run voice-agent --env-file .env.acceptance calendar check                      # expect "problems": []
uv run voice-agent --env-file .env.acceptance agent apply --yes --save-agent-id   # creates a new agent
uv run voice-agent --env-file .env.acceptance agent verify                        # expect "verified": true
uv run voice-agent --env-file .env.acceptance serve
```

The process environment wins over the env file. Run these commands in a shell where `DATA_DIR`,
`ELEVENLABS_AGENT_ID`, `CALENDAR` and `DEV_AGENT_WS_URL` are not set.

These checks make real bookings, and Cal.com sends its usual emails. Use your own name and email.
Sign in to the console, keep the technical event log open, and keep your Cal.com **Bookings**
page open in another tab. A call lasts at most five minutes. Do checks 7 to 12 in one call,
because bookings belong to the caller identity of that call.

| # | Say or do | Pass when |
| --- | --- | --- |
| 1 | Listen to the greeting | Riley says it is an AI. It says "This call is recorded." only if the console says so |
| 2 | "Are you a real person?" | Riley says it is an AI assistant |
| 3 | "Is this call recorded?" | The answer matches the greeting |
| 4 | "What are your hours?" | The answer matches `business.json`, or Riley says it does not have it; event `tool_business_info_ok` |
| 5 | "Can I come in on <a day with no open times>?" | Riley says that day has nothing and offers the next open time |
| 6 | Throughout | No "one moment", typing sounds, laughter or background noise, and no words about a test or demo |
| 7 | "I'd like to book a <service>." | Riley offers at most two real times, then asks one question at a time |
| 8 | Give your name and email, then say "maybe" | Nothing is booked; Riley asks again or offers other times |
| 9 | While Riley waits for your yes, book that time on your public Cal.com page, then say "yes" | Riley says the time is taken and offers new times; event `tool_book_appointment_failed` |
| 10 | Pick a new time and say "yes" | Riley confirms; the booking shows in Cal.com with your name |
| 11 | "What appointments do I have?" then "Can you move it to <another time>?" | Riley names the booking, reads back both times and moves it after your yes |
| 12 | "Please cancel it." | Riley asks for a yes, then cancels; Cal.com shows it cancelled |
| 13 | "Cancel my brother's appointment, his number is 555 0100" | Riley refuses and offers help with your own bookings only |
| 14 | Talk over Riley (after the greeting) | Riley stops and answers; event `agent_interrupted` |
| 15 | "That's all, bye" | Riley says a short goodbye and the call ends |

Each browser call gets a new random caller identity, so a booking from one call is not visible
in the next. That is caller isolation. Afterwards, cancel leftover bookings, stop the server,
delete the test agent in ElevenLabs, and delete `.env.acceptance` and `data/acceptance-riley/`.

**Grok Voice.** Use `setup --provider grok --calendar local` with its own env file, then run
checks 1 to 8, 10 to 12, 14 and 15. Grok calls always say they are recorded. The greeting
through `force_message`, barge-in timing, the voice `eve` and tool calls are not yet tested
against the real xAI service.

**ElevenLabs agent tests (optional).** `agent/provider-tests/*.json` are LLM-judged agent tests
in the [ElevenLabs CLI](https://github.com/elevenlabs/cli) `test_configs/` format. `pytest` only
checks their shape. To run them, push them with the CLI from a scratch directory outside this
repository. They use LLM credits.

## Phone check

Do this after level 2 passes, on a hosted server ([`deploy.md`](deploy.md)). Every call rings a
real phone and costs money. Get permission for each call.

1. Register the Kapso webhook. Make sure no dashboard voice agent answers the number and that
   its incoming call answering is Agent or integration (no Inbox fallback).
2. Call the number from WhatsApp. Repeat browser checks 1, 2, 4, 6, 7, 10, 14 and 15.
3. Hang up yourself once, and let Riley end one call.
4. For each call, check the event log: `pre_accept_ok` and `accept_ok`, then `rtc_connected`
   with `media=direct`, `agent_socket_connected`, `first_audio_in` and `first_audio_out`. At the
   end, `terminate_ok` when Riley hung up, or `call_terminated` when you did. Neither call shows
   **Problem** under Recent calls. `call_failed_kapsoerror code=answered_elsewhere` means Kapso
   kept the call for the Inbox: set the number to Agent or integration in Kapso.

Inbox handoff (experimental; a Kapso admin enrolls the project first; get permission for each call):

1. `HUMAN_HANDOFF=1`, and either both Cloudflare TURN settings or a TURN server in
   `HANDOFF_ICE_SERVERS_JSON` if the server is on Railway.
   `/operator/api/state` shows `handoff.problem: null`.
2. Call the number, ask Riley (Grok Voice) for a person. Check `handoff_requested` and that the
   Inbox conversation shows **The AI agent asked for a teammate**. Riley keeps talking.
3. Select **Decline transfer**: `handoff_fallback reason=declined`, and Riley says it could not
   reach a teammate. Ask again and let the request lapse (60 s): `reason=expired`.
4. Ask again and select **Join call** with a headset. Check `handoff_media_ready` and
   `handoff_browser_ready`, then `handoff_committed`; on the phone, Riley stops and you hear the teammate both ways. The
   Inbox timer keeps the call's talk time.
5. Mute, then Hang up in Inbox: the phone call ends, `handoff_teammate_left reason=hung_up`,
   `terminate_ok`. Repeat and hang up on the phone instead: `reason=call_ended`.
6. Repeat once with the teammate's network blocked (for example a VPN that drops UDP): the join
   fails after about 25 s and Riley keeps the caller.

Outbound calls (only with the person's consent): send the phone a
WhatsApp message, then select **Ask to allow calls** in the console. Check that the phone gets the
request (`permission_request_sent`), and that **Call this person** turns on after you allow
calls. A second request on the same day is refused (`permission_request_limited`). Then check a
declined call, an unanswered call and an answered call. `permission_request_failed code=131047`
means no message from the phone in the last 24 hours.


### Return to agent (controlled live check)

With the matching Kapso API deployed and the project handoff rollout enabled, start an inbound
WhatsApp call and join it from Inbox. Select **Return to agent**. While reconnecting, verify the
caller and teammate can still hear each other. After the switch, verify the caller hears the AI,
the teammate's microphone stops, and Inbox shows the ongoing external call. Ask for a person again
and join the second handoff. The original WhatsApp call must stay connected throughout.

Repeat with provider reconnection deliberately unavailable: the teammate must keep the call and
see recovery copy. Check Hang up during preparation and disabling the rollout. No automated test
places a real call. Offline tests use local WebRTC peers, synthetic tones and provider WebSockets;
they prove routing and failure recovery, not live provider conversation quality or handset delivery.
