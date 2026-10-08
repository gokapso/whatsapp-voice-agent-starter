# Security model

## Surfaces

| Surface | Who can reach it | Protection |
| --- | --- | --- |
| `POST /webhooks/whatsapp` | The internet | HMAC-SHA256 over the raw body with `WHATSAPP_WEBHOOK_SECRET`, checked before JSON parsing; 1 MB cap; only `calls` changes for the configured phone number ID; invalid events ignored without side effects |
| `GET /healthz` | The internet | Returns `{"ok": true}` only |
| `/operator/*` | Anyone who can reach the port | Absent (404) unless `OPERATOR_TOKEN` (≥ 32 chars) is set; API needs `Authorization: Bearer`; constant-time compare; bearer header is not sent automatically by browsers, so no CSRF; page has a strict CSP and no embedded secrets |
| Outbound calling | Operator only | Also needs `ENABLE_OUTBOUND=1`; permission check before every connect; one attempt, never auto-retried |
| Agent conversation | Server only | Signed URL fetched server-side; agent requires auth; URL host pinned to `wss://api.elevenlabs.io`; `DEV_AGENT_WS_URL` limited to loopback |
| Recordings, bookings | Local filesystem only | No HTTP route serves files; `DATA_DIR` 0700, files 0600 (SQLite journal included) under any umask; size, count and age caps, pruned at startup and hourly |

Recommended: expose only `/webhooks/whatsapp` and `/healthz` publicly at your proxy, and reach
`/operator` over a VPN or SSH tunnel. Set `ALLOWED_HOSTS` to your hostnames.

## Private data on disk

`DATA_DIR` holds the booking store (caller names and notes in plain text, caller keys as HMACs),
local captures and provider downloads. At startup the server creates `DATA_DIR` as 0700 or
tightens it to 0700, and refuses to start if it is not a real directory it owns. The SQLite file
is created 0600 before SQLite opens it, so SQLite's journal files get 0600 too; files left by
older runs are tightened. Existing parents of `DATA_DIR` are never changed. There is no
encryption at rest: protect the host and its backups.

## Caller isolation

- Inbound identity: `from_user_id` (business-scoped user ID) if present, else `from`. Outbound:
  the callee's `to_user_id` or the dialed number. Browser tests: a random per-call identity.
- The store keeps `HMAC-SHA256(CALLER_KEY_SECRET, identity)`, not the phone number or BSUID.
- Tools take no identity arguments; extra arguments are rejected. A person whose calls arrive
  sometimes with a BSUID and sometimes without one may appear as two callers. That is the safe
  failure: they see fewer bookings, never someone else's.

## Prompt injection

Tool results and caller speech can contain instructions. The prompt tells the agent that tool
results are data, and the server enforces the important rules (identity, confirmation flags,
slot validity) itself, so a talked-into-it agent still cannot read or cancel another caller's
booking or book without `confirmed: true`. The confirmation flag still relies on the LLM to ask
first; for real money or irreversible actions, add a server-side two-step confirmation.

## Logs

The event log holds event names, short status strings and hashed call references. It never holds
audio, SDP, transcript text, phone numbers, BSUIDs, raw Meta call IDs, media URLs, file paths or
keys. Kapso error messages are truncated and have the API key redacted. Pipecat and aiortc write
their own debug logs to stderr; `voice-agent serve` and the Docker image default to
`LOGURU_LEVEL=INFO` so connection details stay out of logs. Set `LOGURU_LEVEL=DEBUG` only while
diagnosing media problems.

## What changed from the internal demos this was built from

- Webhook signature now rejects non-hex/missing headers up front; a batch with another phone
  number's change ignores that change instead of failing the whole delivery.
- The operator UI used a per-process token embedded in the page and a host-header allowlist
  bound to localhost. It now needs a configured bearer token that never appears in the page.
- Removed: test-only "reject next call" control, macOS launchctl/phone-profile switching, `say`
  smoke scripts, private snapshot checks, the typing sound, forced pre-tool speech, the "One
  moment." soft-timeout message and office background ambience.
- Inbound: the agent now starts after `accept` (it started before `pre_accept`), matching Kapso's
  guide; failures before accept send `reject`; over-capacity calls are rejected rather than
  answered with HTTP 409.
- Tool results now use `{ok, code, message}`; store reads are index-backed and capped.
