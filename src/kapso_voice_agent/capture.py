"""Optional private local capture (LOCAL_CAPTURE=1): two aligned audio tracks plus provider turn text.

Audio comes from Pipecat's AudioBufferProcessor placed after transport.output(), so agent audio is
recorded as it is played and caller audio as it arrives. Turn text is the provider's own
user_transcript/agent_response events; it is not an independent diarized transcript and there is
no second ASR. Files are 0600 in 0700 directories, capped by duration, and pruned by age and count
(before each new capture, at server start and hourly; see private.py). Nothing here is served over HTTP.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
import wave

from pipecat.audio.utils import interleave_stereo_audio
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

from .private import CAPTURE_DIR, NAME, private_dir, prune

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
MAX_TURNS = 200
MAX_TURN_CHARS = 2000


class CappedAudioBuffer(AudioBufferProcessor):
    """Stops recording once either track reaches the cap; memory stays bounded."""

    def __init__(self, max_seconds, **kwargs):
        super().__init__(sample_rate=SAMPLE_RATE, num_channels=2, buffer_size=0,
                         auto_start_recording=True, **kwargs)
        self.max_bytes = int(max_seconds * BYTES_PER_SECOND)
        self.truncated = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        # Pinned pipecat-ai 1.12.0 buffer attributes; tests cover the cap.
        if self._recording and max(len(self._user_audio_buffer), len(self._bot_audio_buffer)) >= self.max_bytes:
            self.truncated = True
            await self.stop_recording()


def write_wav(path, pcm, channels=1):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)
    path.chmod(0o600)


class LocalCapture:
    def __init__(self, root, call_id, max_seconds, retention_days=7, max_count=20):
        if not NAME.match(call_id):
            raise ValueError("Invalid capture name")
        self.root, self.call_id = Path(root), call_id
        self.retention_days, self.max_count = retention_days, max_count
        self.recorder = CappedAudioBuffer(max_seconds)
        self.started = time.monotonic()
        self.turns = []
        self.dropped_turns = 0
        self.tracks = None
        self.ready = asyncio.Event()
        self.written = None

        @self.recorder.event_handler("on_track_audio_data")
        async def on_tracks(processor, user, bot, sample_rate, channels):
            # Only the first (complete) delivery is kept; buffer_size=0 means one delivery.
            if self.tracks is None:
                self.tracks = (bytes(user[:self.recorder.max_bytes]), bytes(bot[:self.recorder.max_bytes]))
            self.ready.set()

    def add_turn(self, role, text):
        if not isinstance(text, str) or not text.strip():
            return
        if len(self.turns) >= MAX_TURNS:
            self.dropped_turns += 1
            return
        self.turns.append({"role": role, "offset_secs": round(time.monotonic() - self.started, 2),
                           "text": text[:MAX_TURN_CHARS], "truncated": len(text) > MAX_TURN_CHARS})

    async def finish(self, conversation_id=None, wait=3.0):
        if self.written is not None:
            return self.written
        recorder = self.recorder
        pending = recorder._recording and bool(len(recorder._user_audio_buffer) or len(recorder._bot_audio_buffer))
        await recorder.stop_recording()
        if pending or (recorder.truncated and self.tracks is None):
            try:
                await asyncio.wait_for(self.ready.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
        self.written = await asyncio.to_thread(self.write, conversation_id)
        return self.written

    def write(self, conversation_id):
        private_dir(self.root)
        prune(self.root, CAPTURE_DIR, keep=self.max_count - 1, days=self.retention_days)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = self.root / f"{stamp}-{self.call_id}"
        directory.mkdir(mode=0o700)
        user, bot = self.tracks or (b"", b"")
        length = max(len(user), len(bot))
        user, bot = user.ljust(length, b"\x00"), bot.ljust(length, b"\x00")
        files = {}
        if length:
            write_wav(directory / "caller.wav", user)
            write_wav(directory / "agent.wav", bot)
            write_wav(directory / "stereo-caller-left-agent-right.wav", interleave_stereo_audio(user, bot), channels=2)
        turns = directory / "turns.json"
        turns.write_text(json.dumps({"source": "elevenlabs_client_events", "turns": self.turns,
                                     "dropped_turns": self.dropped_turns}, ensure_ascii=False, indent=1))
        turns.chmod(0o600)
        for path in sorted(directory.iterdir()):
            files[path.name] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest = {"kind": "local_pipecat_capture", "call_ref": self.call_id, "conversation_id": conversation_id,
                    "sample_rate": SAMPLE_RATE, "seconds": round(length / BYTES_PER_SECOND, 2),
                    "truncated": self.recorder.truncated, "turn_count": len(self.turns),
                    "turns_source": "provider client events, not an independent transcript",
                    "dropped_turns": self.dropped_turns,
                    "retention_days": self.retention_days, "files": files,
                    "expires_after": (datetime.now(timezone.utc) + timedelta(days=self.retention_days)).isoformat()}
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest, indent=1))
        path.chmod(0o600)
        return directory
