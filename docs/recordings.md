# Recordings and transcripts: three separate sources

They are produced by different systems, stored in different places and are not interchangeable.

| Source | Default here | What you get | Where it lives | Retention |
| --- | --- | --- | --- | --- |
| **ElevenLabs** (provider) | **On** (`privacy.record_audio = true`) | Original call MP3 + conversation JSON (provider transcript, tool calls) | ElevenLabs; download with `voice-agent artifacts fetch` | `privacy.retention_days` (7) at the provider; local copies pruned by `CAPTURE_RETENTION_DAYS` |
| **Local bridge capture** | Off (`LOCAL_CAPTURE=0`) | `caller.wav`, `agent.wav`, stereo WAV (16 kHz, aligned), `turns.json`, `manifest.json` | `DATA_DIR/captures/` on this server, 0600/0700 | `CAPTURE_RETENTION_DAYS`, `CAPTURE_MAX_COUNT` (local, best effort) |
| **Meta native** | **Not requested** | Meta recording (audio) and/or transcript | Meta; Kapso stores metadata and fetches on request | About 7 days at Meta |

## Disclosure

The greeting is built per call from the **effective** settings, not from `agent.toml` alone:

- Before a real agent session starts, the server reads the stored ElevenLabs agent back
  (read-only) and uses its `platform_settings.privacy.record_voice`. It reuses a good answer for
  60 seconds, then reads again. Restart the server to apply a provider change at once.
- The greeting says "This call is recorded." when that stored setting or local capture is on, and
  leaves it out otherwise. The agent's `recording_status` variable answers "is this recorded?" the
  same way. If the stored agent and `agent.toml` differ, the event log shows
  `provider_recording_differs_from_agent_toml` and the greeting follows the stored agent.
- If the setting cannot be read (network error, HTTP error, field missing), no session starts:
  inbound calls get `reject`, browser and outbound calls get HTTP 503. The server never guesses.
- The offline fake agent records nothing. It is never read over the network.
- `/operator/api/state` → `recording` shows the last read (`on`, `off`, `unknown`,
  `not_checked`). `not_checked` with no `last_check_error` is normal on an idle server: the
  60-second answer has expired and the next call reads again. To confirm the stored setting
  directly, run `voice-agent agent verify`. "On" means recording is enabled at the provider. It
  does not prove that the provider saved audio for a given call; `artifacts fetch` shows what
  the provider actually has.

Note that ElevenLabs keeps the conversation transcript for the retention period even when audio
recording is off. Check what notice your jurisdiction
and use case require; the starter's wording is a minimum, not legal advice.

## ElevenLabs artifacts

- `voice-agent artifacts fetch` reads the conversation ID from the latest local capture manifest,
  or takes `--conversation-id`. It refuses conversations of other agents, redirects, non-200
  responses and oversized bodies (JSON 2 MiB, audio 32 MiB), and waits at most 180 s.
- Output: `DATA_DIR/vendor/vendor-<hash>/` with `conversation.json`, `recording.mp3`,
  `manifest.json`. Only counts and checksums are printed. These are exact provider originals, so
  they contain the transcript text and IDs; they stay private (0600/0700).
- The provider transcript is the provider's ASR output, not Meta's transcript.

## Local capture

- Audio is recorded after `transport.output()`: agent audio as played, caller audio as received.
  Capped at `MAX_SESSION_SECONDS`; when the cap hits, recording stops and `truncated: true` is
  written to the manifest.
- `turns.json` holds the provider's `user_transcript`/`agent_response` events with offsets. It is
  not an independent, diarized transcript.
- Directory names use the hashed call reference, never the raw Meta call ID.

## Local retention

Local copies under `DATA_DIR/captures` and `DATA_DIR/vendor` are deleted when they are older than
`CAPTURE_RETENTION_DAYS`, or when there are more than `CAPTURE_MAX_COUNT`. This runs:

- at server start and every hour while `voice-agent serve` runs,
- before each new capture or download,
- when you run `voice-agent artifacts prune` (works while the server is stopped).

Only directories with the names this server gives them are deleted or counted: captures named
`<UTC time>-<call reference>` (for example `20261007T153000Z-call-0123456789abcdef`) and downloads
named `vendor-<16 hex characters>`. Other files and folders are left alone. Symlinks are never
followed: if `captures` or `vendor` is itself a symlink, nothing in it is pruned.

If the server is stopped for long periods, schedule `voice-agent artifacts prune` yourself (for
example a daily cron or systemd timer). This is best effort on this machine only: it does not
delete ElevenLabs' copy (that follows `privacy.retention_days` at the provider) or Meta's.

## Meta native capture (optional, not wired in)

Meta records/transcribes only when the `accept` (inbound) or `connect` (outbound) request includes
`recording` and/or `transcription` objects. Meta then plays its own announcement to both
people. To enable it you would add those objects in `kapso.py` (`action(..., "accept")` and
`connect`), never on `pre_accept`, and adjust the greeting so it does not repeat the notice.
This was not tested with this code.

After the call, Meta sends `call_recording_available` and `call_transcription_available`
(Kapso also accepts the `call_transcript_available` spelling). This server records only a notice
(`/operator/api/state` → `native_artifact_events`) and never downloads from the webhook's media
URL. Fetch through Kapso's API (call detail and `/artifacts/recording|transcription` on
`app.kapso.ai`), as described in Kapso's recordings guide.

Status as of 2026-10-07, as reported by the Kapso team and not tested by this repository:
downloading and playing native call **recordings** in Kapso works. Native **transcripts** have
not been verified end to end; do not assume a call has one. A call only has native artifacts if
native capture was requested for it, and this starter does not request it.

## Kapso dashboard

Kapso's Calls view shows Meta-native artifacts only. ElevenLabs and local artifacts from this
starter do not appear there, and Kapso has no upload API for them today.
