# Security, privacy and recordings

## Surfaces

| Surface | Who can reach it | Protection |
| --- | --- | --- |
| `POST /webhooks/whatsapp` | The internet | HMAC-SHA256 over the raw body with `WHATSAPP_WEBHOOK_SECRET`, checked before JSON parsing; 1 MB cap; only `calls` changes for the configured phone number ID; invalid events ignored without side effects |
| `GET /healthz` | The internet | Returns `{"ok": true}` only |
| `/operator/*` | Anyone who can reach the port | Absent (404) unless `OPERATOR_TOKEN` (32 or more characters, the "console access key") is set; the API needs `Authorization: Bearer` with a constant-time compare; browsers do not send that header by themselves, so there is no CSRF; the page has a strict CSP and no secrets; the console keeps the key in `sessionStorage` for one tab, never in a URL or `localStorage` |
| Outbound calling | Operator only | Also needs `ENABLE_OUTBOUND=1`; a permission check before every connect; one attempt, never retried |
| Voice provider | Server only | ElevenLabs: signed URL fetched by the server, host pinned to `wss://api.elevenlabs.io`. Grok Voice: `XAI_API_KEY` sent only as a header to `wss://api.x.ai/v1/realtime`. `DEV_AGENT_WS_URL` is loopback only |
| Calendar | Server only | `CAL_API_KEY` stays on the server; fixed host `https://api.cal.com`, no redirects, short timeouts; the model never sees Cal.com uids |
| Bookings, recordings | Local disk only | No HTTP route serves files; `DATA_DIR` 0700 and files 0600 (SQLite journal included) under any umask |

Pipecat Cloud sessions (`cloud.py`) have no public route. Their start body is not signed, so each
call is looked up through Kapso's API first, and the caller identity comes from Kapso's saved
call. The body's `whatsapp_token` is never read.

Expose only `/webhooks/whatsapp` and `/healthz` publicly if you can, and reach `/operator` over a
VPN or SSH tunnel (`examples/vps` does this). Set `ALLOWED_HOSTS` to your host names. On Railway
the console is on the internet: keep the generated 43-character key, and rotate it if it leaks.

`voice-agent init` and `setup` never print the console access key. `voice-agent console-key`
copies it; `--show` prints it on request. `setup` reads provider keys with hidden input and
writes them only to the 0600 env file. `railway variables --yes` passes each value on stdin,
never as a command-line argument.

## Caller isolation

- The identity is the caller's `from_user_id` (business-scoped user ID) or `from` number. For an
  outbound call, it is the callee. A browser test call gets a random identity.
- The store keeps `HMAC-SHA256(CALLER_KEY_SECRET, identity)`, not the phone number or BSUID.
- A caller can list, move or cancel only bookings in the local map under their own key. The
  caller's email goes into a new booking. It is never used to look a booking up.
- Tools take no identity arguments, and extra arguments are refused. A person who calls with and
  without a BSUID can show as two callers. That is the safe failure: they see fewer bookings,
  never someone else's.

Tool results and caller speech can contain instructions. The server enforces the important
rules itself: identity, the confirmation flag and open times. A confused agent still cannot read
another caller's booking or book without `confirmed: true`. The agent asks for the yes. For real
money or actions that cannot be undone, add a server-side two-step confirmation.

## Private data on disk

`DATA_DIR` holds the caller-to-booking map (HMAC keys, short ids, Cal.com uids, start times).
It also holds the local development calendar, local captures and provider downloads. Names,
emails and notes are in Cal.com. The server makes `DATA_DIR` 0700 at start and refuses to start
if it does not own it. There is no encryption at rest: protect the host and its backups.

## Logs

The event log holds event names, short status values and hashed call references. It never holds
audio, SDP, transcript text, phone numbers, BSUIDs, raw Meta call IDs, media URLs, file paths or
keys. The console's **Recent calls** list is built from this log in memory. It is lost when the
server restarts. Pipecat and aiortc write their own logs to stderr at `LOGURU_LEVEL=INFO`. Set
`DEBUG` only while you look for an audio problem.

## Recordings

Three systems can keep call audio or text. They are separate.

| Source | Default here | What it keeps | Retention |
| --- | --- | --- | --- |
| ElevenLabs | On (`privacy.record_audio = true`) | Call audio and the conversation transcript | `privacy.retention_days` (7) at ElevenLabs. `voice-agent artifacts fetch` downloads a private copy |
| xAI (Grok Voice) | Always | API requests and responses | 30 days at xAI, unless your team has Zero Data Retention ([FAQ](https://docs.x.ai/docs/resources/faq-api/security)) |
| Local capture | Off (`LOCAL_CAPTURE=0`) | WAV files and the provider's turn text in `DATA_DIR/captures/` | `CAPTURE_RETENTION_DAYS`, `CAPTURE_MAX_COUNT` |
| Meta native | Not requested | Meta recording or transcript | About 7 days at Meta |

The greeting says "This call is recorded." from the settings in effect, not from `agent.toml`:

- With ElevenLabs, the server reads the stored agent's `record_voice` before a session starts
  (read-only, kept for 60 seconds). If the read fails, no session starts: an inbound call gets
  `reject`, and a browser or outbound call gets HTTP 503.
- With Grok Voice, there is no stored agent, so the greeting always says the call is recorded.
- With `LOCAL_CAPTURE=1`, the greeting always says the call is recorded.
- The offline fake agent records nothing.

ElevenLabs keeps the transcript for the retention period even when audio recording is off. Check
which notice your jurisdiction needs. The starter's wording is a minimum, not legal advice.

Local copies under `DATA_DIR/captures` and `DATA_DIR/vendor` are deleted at server start, every
hour, before each new capture, and by `voice-agent artifacts prune`. Only directories with the
names this server makes are deleted, and symlinks are never followed. If the server is often
stopped, schedule `voice-agent artifacts prune`.

Meta records or transcribes only when the `accept` or `connect` request asks for it. This
starter does not ask. After such a call, the server only notes the `call_recording_available`
event; fetch the file through Kapso's API. Kapso's Calls view shows Meta artifacts only, not
ElevenLabs or local ones.
