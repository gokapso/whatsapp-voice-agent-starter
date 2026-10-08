"""Shared offline test helpers. Nothing here reaches the network: Kapso is an httpx MockTransport,
ElevenLabs is a local WebSocket server, and media is a local aiortc peer."""

import asyncio
from datetime import datetime
import fractions
import hashlib
import hmac
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from aiortc import MediaStreamTrack
import av
import httpx
import numpy as np
import pytest

from kapso_voice_agent import recording
from kapso_voice_agent.agent_config import load_spec
from kapso_voice_agent.config import REPO_ROOT, Settings
from kapso_voice_agent.kapso import KapsoClient
from kapso_voice_agent.store import AppointmentStore

FIXTURES = Path(__file__).parent / "fixtures"
PHONE = "100000000000001"
SECRET = "test-webhook-secret-not-real"
TOKEN = "t" * 40
KAPSO_KEY = "test-kapso-key-not-real"
# Monday 2026-11-02 09:00 in the fictional shop's time zone; Monday is closed.
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("America/Chicago"))


def fixture(name, **call_updates):
    payload = json.loads((FIXTURES / name).read_text())
    value = payload["entry"][0]["changes"][0]["value"]
    for row in value.get("calls", []):
        row.update(call_updates)
    return payload


def sign(raw, secret=SECRET):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def post_webhook(client, payload, secret=SECRET):
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/whatsapp", content=raw,
                       headers={"x-webhook-signature": sign(raw, secret), "content-type": "application/json"})


def tool_call(name, params=None, tool_id="tool-1"):
    return {"tool_name": name, "tool_call_id": tool_id, "parameters": params or {}}


@pytest.fixture(autouse=True)
def provider_recording_readback(monkeypatch):
    """Offline stand-in for the read-only provider privacy read: the stored agent records audio,
    as agent.toml asks. Tests that need another answer pass `recording_reader` to create_app."""
    reads = []

    async def stored_agent_records_audio(settings):
        reads.append(settings.elevenlabs_agent_id)
        return True
    monkeypatch.setattr(recording, "read_provider_recording", stored_agent_records_audio)
    return reads


@pytest.fixture
def spec():
    return load_spec(REPO_ROOT / "agent/agent.toml")


@pytest.fixture
def settings(tmp_path):
    return Settings(kapso_api_key=KAPSO_KEY, phone_number_id=PHONE, webhook_secret=SECRET,
                    elevenlabs_api_key="test-eleven-key-not-real", elevenlabs_agent_id="test-agent",
                    operator_token=TOKEN, data_dir=tmp_path / "data", max_session_seconds=60)


@pytest.fixture
def store(tmp_path, spec):
    return AppointmentStore(tmp_path / "store.sqlite3", spec.business_path, clock=lambda: NOW)


class KapsoRecorder:
    """Mock Kapso proxy: records call actions and answers like the real API's success shape."""

    def __init__(self, handler=None):
        self.actions, self.bodies = [], []
        self.handler = handler

    async def __call__(self, request):
        assert request.headers["x-api-key"] == KAPSO_KEY
        body = json.loads(request.content) if request.content else {}
        self.bodies.append(body)
        if "action" in body:
            self.actions.append(body["action"])
        if self.handler:
            response = await self.handler(request)
            if response is not None:
                return response
        return httpx.Response(200, json={"success": True})

    def factory(self):
        return lambda: KapsoClient(KAPSO_KEY, PHONE, "v24.0",
                                   client=httpx.AsyncClient(transport=httpx.MockTransport(self)))


def asgi_client(app, host="voice.example.test"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{host}")


class ToneTrack(MediaStreamTrack):
    """Caller microphone stand-in: a 440 Hz tone at 48 kHz."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.timestamp = 0

    async def recv(self):
        await asyncio.sleep(0.02)
        position = np.arange(960) + self.timestamp
        signal = (np.sin(position * 2 * np.pi * 440 / 48000) * 5000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(signal.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 48000, self.timestamp, fractions.Fraction(1, 48000)
        self.timestamp += 960
        return frame


def listen(peer, heard, consumers):
    @peer.on("track")
    def track(track):
        async def consume():
            while True:
                frame = await track.recv()
                if np.abs(frame.to_ndarray().astype(np.int32)).max() > 500:
                    heard.set()
        consumers.append(asyncio.create_task(consume()))


async def wait_for_event(log, name, timeout=5):
    async def poll():
        while not any(e["event"] == name for e in log.events):
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout=timeout)

