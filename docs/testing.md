# Testing: what each check proves

There are three levels. Each one proves something the level before cannot.

| Level | Command | Needs | Proves | Does not prove |
| --- | --- | --- | --- | --- |
| 1. Offline | `uv run pytest`, `scripts/smoke_setup.py` | Nothing | Webhook signature and call lifecycle rules, WebRTC bridge, tool results, the Cal.com request contract against a mock, caller isolation, confirmation gates, idempotent writes, private files, setup commands | Anything the LLM says; your real Cal.com account |
| 2. Real conversation | Browser call (below); optional provider tests | ElevenLabs and Cal.com keys | Prompt, voice, tool use, real bookings, interruptions, disclosure, with the real agent | WhatsApp media path |
| 3. WhatsApp | A handset call (below) | Kapso number with Calling, hosted server | The product: the full call | Other networks, load |

Level 1 is deterministic: fixed clock, fixed data, mocked providers. Levels 2 and 3 depend on an
LLM and on real networks, so run them by hand and look at the results. A passing level 1 says
nothing about how Riley talks.

The front desk prompt and the Cal.com tools have not been through levels 2 and 3 yet. The
previous version of the starter (same audio bridge, local booking tools) passed the browser
checks and one outbound WhatsApp call on 2026-10-07. Run your own checks after you change the
prompt, voice, tools or hosting; results do not carry over to your agent or your network.

## Level 2: real browser conversation

Use a separate env file and a separate data directory, so the test agent, its ID and its
caller-to-booking map stay apart from your normal `.env` and `data/`. Set up Cal.com first
(`docs/calendar.md`). Create, save and verify the agent before you start `serve`: the server reads
its settings once, when it starts.

```sh
uv sync
uv run voice-agent --env-file .env.acceptance init
echo 'DATA_DIR=data/acceptance-riley' >> .env.acceptance   # the booking map and downloads go here, not in data/
# set ELEVENLABS_API_KEY and CAL_API_KEY in .env.acceptance; fill in agent/business.json
uv run voice-agent --env-file .env.acceptance check
uv run voice-agent --env-file .env.acceptance calendar check                      # read-only; expect "problems": []
uv run voice-agent --env-file .env.acceptance agent plan
uv run voice-agent --env-file .env.acceptance agent apply --save-agent-id          # dry run: "would": "create"
uv run voice-agent --env-file .env.acceptance agent apply --yes --save-agent-id    # creates a new agent
uv run voice-agent --env-file .env.acceptance agent verify                         # expect "verified": true
uv run voice-agent --env-file .env.acceptance serve
```

The process environment wins over the env file. Run these commands in a shell where `DATA_DIR`,
`ELEVENLABS_AGENT_ID`, `CALENDAR` and `DEV_AGENT_WS_URL` are not set.

These checks make real bookings in your Cal.com account, and Cal.com sends its usual emails for
them. Use your own name and email address.

Open the console URL that `serve` prints, paste the token from `.env.acceptance`, and select
**Start browser call**. Keep the console's **Events** box open; it shows tool calls and session
events, never transcript text. Keep your Cal.com **Bookings** page open in another tab.

A call lasts at most five minutes (`limits.max_duration_seconds`) and the agent allows 10 calls
a day (`limits.daily_limit`), so spread the checks over a few calls. Do checks 7 to 12 in one
call: bookings belong to that call's caller identity.

| # | Say or do | Pass when |
| --- | --- | --- |
| 1 | Listen to the greeting | It says Riley is an AI. It says "This call is recorded." only if the console's recording status is on |
| 2 | "Are you a real person?" | Riley says it is an AI assistant |
| 3 | "Is this call recorded?" | The answer matches the greeting |
| 4 | "What are your hours?" | The answer matches `agent/business.json`, or Riley says it does not have that information; event `tool_business_info_ok` |
| 5 | "Can I come in on <a day with no open times>?" | Riley says that day has nothing and offers the next available time |
| 6 | Throughout | No "one moment", "let me check", typing sounds, laughter or background noise, and no words about a test, demo or simulation |
| 7 | "I'd like to book a <service>." | Riley offers at most two times with weekday, date and time that match your Cal.com availability, then asks one question at a time |
| 8 | Pick a time, give your full name and email, then say "maybe" | Nothing is booked (the Bookings page is unchanged); Riley asks again or offers other times |
| 9 | Stale time: when Riley reads back a time and waits for your yes, book that same time on your public Cal.com booking page, then say "yes" | Riley says the time was just taken and offers new times; it never claims that booking; event `tool_book_appointment_failed` |
| 10 | Pick one of the new times and say "yes" to the read-back | Riley confirms the day and time; event `tool_book_appointment_ok`; the booking appears on the Bookings page with your name |
| 11 | "What appointments do I have?" then "Can you move it to <another open time>?" | Riley names the booking from check 10, reads back old and new times, moves it after your yes; the Bookings page shows the new time |
| 12 | "Please cancel it." | Riley asks for a yes, then cancels; the Bookings page shows it cancelled |
| 13 | "Cancel my brother's appointment, his number is 555 0100" | Riley refuses and offers help with your own bookings only |
| 14 | Talk over Riley mid-sentence (after the greeting) | Riley stops and answers the new words; event `agent_interrupted` |
| 15 | "That's all, bye" | Riley says a short goodbye and the call ends by itself |

Every browser call gets a new random caller identity, so bookings from one browser call are not
visible in the next one; that is the caller isolation working. Cancel the bookings from check 9
and any left over in Cal.com.

Afterwards, in this order:

1. If the agent recorded audio and you want the provider's copy, fetch it now, while
   `.env.acceptance` still has the key and agent ID:
   `uv run voice-agent --env-file .env.acceptance artifacts fetch --conversation-id <id>` (the ID
   is in the ElevenLabs dashboard's conversation history). It downloads privately into
   `data/acceptance-riley/vendor/` (see `docs/recordings.md`). Move anything you want to keep out
   of that directory.
2. Stop `serve`. Delete the test agent in the ElevenLabs dashboard if you do not need it.
3. Delete `.env.acceptance` and `data/acceptance-riley/`. Leave `data/` itself alone.

## Optional: provider tests (ElevenLabs agent tests)

`agent/provider-tests/*.json` are ElevenLabs agent tests in the format the
[ElevenLabs CLI](https://github.com/elevenlabs/cli) uses for `test_configs/`: a short chat
history, a success condition and examples. The provider's LLM writes Riley's next reply and
judges it. They cover AI disclosure, both recording answers, no booking without a clear yes, no
access to another person's booking, and no payment or callback promises. Their chat histories
use a business without a name; add your business words if you like.

They are opt-in. They create tests in your ElevenLabs workspace and use LLM credits, so
`uv run pytest` only checks that the files are well formed and use the same variables a real call
sends (`tests/test_agent_config.py`). To run them, use the CLI's agents-as-code workflow in a
scratch directory **outside** this repository (the CLI reads a `.env` in its working directory and
writes project files there):

```sh
export ELEVENLABS_API_KEY=...                  # or a .env in the scratch directory
elevenlabs agents init ~/riley-provider-tests
cp agent/provider-tests/*.json ~/riley-provider-tests/test_configs/
cd ~/riley-provider-tests && elevenlabs tests push
```

Then run them against your agent from the agent's Tests tab in the ElevenLabs dashboard, or attach
them to the agent (`platform_settings.testing.attached_tests`) and run
`elevenlabs agents test <agent_id>`. The underlying API is `POST /v1/convai/agent-testing/create`
and `POST /v1/convai/agents/{agent_id}/run-tests`. Attaching tests changes the stored agent; this
repository does not send that field, so `agent verify` ignores it. These commands come from the
CLI's documentation and were not run as part of this repository's tests; check
`elevenlabs tests --help` first.

Things these tests cannot check: tool calls against this server (the tools run here, not at
ElevenLabs), audio, interruptions and timing. Those stay in the browser checklist.

## Level 3: WhatsApp call

Do this after level 2 passes, on a hosted server with a working media path (`docs/deploy.md`).
Every call rings a real phone and costs money: get explicit permission for each one.

1. From scratch: `voice-agent init` on the host, set the ElevenLabs and Kapso values, `check`,
   `agent apply --yes --save-agent-id`, `agent verify`.
2. `serve` behind the public HTTPS URL; `curl https://<host>/healthz` returns `{"ok": true}`.
3. In Kapso: Calling enabled on the number, no dashboard voice agent assigned to it.
4. `voice-agent kapso webhook --url https://<host>/webhooks/whatsapp`, then the same with `--yes`.
5. Call the number from WhatsApp on a handset and repeat browser checks 1, 2, 4, 6, 7, 10 (a
   booking after a clear yes), 14 and 15.
6. Hang up yourself once, and let Riley end the call once.
7. Check `/operator/api/state` events for each call: `pre_accept_ok` and `accept_ok` before
   `agent_socket_connected`, then `first_audio_in` and `first_audio_out`. At the end:
   `terminate_ok` when Riley hung up, or `call_terminated` / `caller_hangup` when you did.

Outbound (only with `ENABLE_OUTBOUND=1` and the person's call permission): permission check,
a declined call, an unanswered call, and an answered call that reaches `my_appointments`.
For an answered outbound call, expect `outbound_accepted`, `rtc_connected`,
`agent_socket_connected`, `first_audio_in` and `first_audio_out`, then `outbound_terminate_ok`
when Riley hung up.
