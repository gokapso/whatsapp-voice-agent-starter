"""Download ElevenLabs' own recording (MP3) and conversation JSON for one conversation.

These are the provider's artifacts, separate from Meta-native recordings/transcripts and from local
captures. They do not appear in Kapso's Calls UI. Output is private (0600 files, 0700 directories)
under DATA_DIR/vendor and pruned by the capture retention settings. Only counts and checksums are
returned: no transcript text, audio, URLs or IDs.
"""

import hashlib
import json
import re
import time

import httpx

from .private import CAPTURE_DIR, VENDOR_DIR, artifact_dirs, private_dir, prune

BASE = "https://api.elevenlabs.io/v1/convai/conversations/"
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_AUDIO_BYTES = 32 * 1024 * 1024
MAX_WAIT_SECONDS = 180


class ArtifactError(Exception):
    pass


def capture_manifest(captures, name=None):
    candidates = sorted(artifact_dirs(captures, CAPTURE_DIR), key=lambda d: d.name)
    if name:
        candidates = [d for d in candidates if d.name == name]
    for directory in reversed(candidates):
        manifest = directory / "manifest.json"
        if manifest.exists():
            data = json.loads(manifest.read_text())
            if data.get("conversation_id"):
                return directory, data
    raise ArtifactError("No local capture manifest with a provider conversation ID")


def bounded_get(client, url, limit, accept):
    with client.stream("GET", url, headers={"accept": accept}) as response:
        if response.is_redirect:
            raise ArtifactError(f"Unexpected redirect HTTP {response.status_code}")
        if response.status_code != 200:
            raise ArtifactError(f"HTTP {response.status_code}")
        declared = int(response.headers.get("content-length") or 0)
        if declared > limit:
            raise ArtifactError("Response exceeds cap")
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ArtifactError("Response exceeds cap")
        return bytes(body), response.headers.get("content-type", "")


def private_write(path, data):
    path.write_bytes(data)
    path.chmod(0o600)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def fetch(conversation_id, agent_id, key, out_root, wait_seconds=0, client=None, sleep=time.sleep,
          retention_days=7, max_count=20):
    if not CONVERSATION_ID.match(conversation_id or ""):
        raise ArtifactError("Invalid conversation ID")
    wait_seconds = min(max(wait_seconds, 0), MAX_WAIT_SECONDS)
    client = client or httpx.Client(timeout=30, follow_redirects=False, headers={"xi-api-key": key})
    deadline = time.monotonic() + wait_seconds
    while True:
        raw, kind = bounded_get(client, BASE + conversation_id, MAX_JSON_BYTES, "application/json")
        if not kind.startswith("application/json"):
            raise ArtifactError("Conversation response is not JSON")
        details = json.loads(raw)
        if details.get("agent_id") != agent_id:
            raise ArtifactError("Conversation belongs to another agent; refused")
        if details.get("status") in ("done", "failed") or time.monotonic() >= deadline:
            break
        sleep(10)
    transcript = details.get("transcript") or []
    report = {"status": details.get("status"), "has_audio": details.get("has_audio"),
              "has_user_audio": details.get("has_user_audio"), "has_response_audio": details.get("has_response_audio"),
              "duration_secs": (details.get("metadata") or {}).get("call_duration_secs"),
              "termination_reason_present": bool((details.get("metadata") or {}).get("termination_reason")),
              "turns": {"user": sum(t.get("role") == "user" for t in transcript),
                        "agent": sum(t.get("role") == "agent" for t in transcript)},
              "files": {}}
    if details.get("status") != "done":
        return report
    private_dir(out_root)
    prune(out_root, VENDOR_DIR, keep=max_count, days=retention_days)
    directory = out_root / ("vendor-" + hashlib.sha256(conversation_id.encode()).hexdigest()[:16])
    directory.mkdir(mode=0o700, exist_ok=True)
    report["files"]["conversation.json"] = private_write(directory / "conversation.json", raw)
    if details.get("has_audio"):
        audio, kind = bounded_get(client, BASE + conversation_id + "/audio", MAX_AUDIO_BYTES, "audio/mpeg")
        if not kind.startswith("audio/"):
            raise ArtifactError("Audio response has an unexpected content type")
        report["files"]["recording.mp3"] = private_write(directory / "recording.mp3", audio)
    report["directory"] = directory.name
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps({"kind": "elevenlabs_vendor_artifacts", "conversation_id": conversation_id,
                                    "retention_days": retention_days, **report}, indent=1))
    manifest.chmod(0o600)
    return report
