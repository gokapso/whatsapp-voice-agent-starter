#!/usr/bin/env python3
"""End-to-end smoke test of the real server process, fully offline.

Starts `voice-agent dev fake-agent` and `voice-agent serve` as subprocesses on free loopback
ports with throwaway secrets and a temporary DATA_DIR, then over real HTTP/WebRTC:
health, webhook signature rejection/acceptance, operator auth, and a browser-path call whose
audio round-trips through the bridge and the fake agent. Prints one JSON summary.

  uv run python scripts/smoke_local.py
"""

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

from aiortc import RTCPeerConnection, RTCSessionDescription
import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from conftest import ToneTrack  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, deadline=30):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit(f"server did not start: {url}")


async def browser_call(base, token):
    heard = asyncio.Event()
    peer = RTCPeerConnection()
    peer.addTrack(ToneTrack())
    consumers = []

    @peer.on("track")
    def on_track(track):
        async def consume():
            while True:
                frame = await track.recv()
                if np.abs(frame.to_ndarray().astype(np.int32)).max() > 500:
                    heard.set()
        consumers.append(asyncio.create_task(consume()))

    headers = {"authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base, timeout=20) as client:
        await peer.setLocalDescription(await peer.createOffer())
        response = await client.post("/operator/api/browser-call", json={"sdp": peer.localDescription.sdp}, headers=headers)
        response.raise_for_status()
        answer = response.json()
        await peer.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        started = time.monotonic()
        await asyncio.wait_for(heard.wait(), timeout=20)
        first_audio = round(time.monotonic() - started, 2)
        await asyncio.sleep(1)
        state = (await client.get("/operator/api/state", headers=headers)).json()
        hangup = await client.post(f"/operator/api/calls/{answer['ref']}/hangup", headers=headers)
    for consumer in consumers:
        consumer.cancel()
    await peer.close()
    events = [e["event"] for e in state["events"]]
    return {"heard_agent_audio_after_s": first_audio, "hangup_status": hangup.status_code,
            "tool_ran": "tool_available_slots_ok" in events, "events": events}


def main():
    with tempfile.TemporaryDirectory() as data:
        agent_port, port = free_port(), free_port()
        token, secret = secrets.token_urlsafe(32), secrets.token_hex(32)
        env = {**os.environ, "DATA_DIR": data, "OPERATOR_TOKEN": token, "WHATSAPP_WEBHOOK_SECRET": secret,
               "WHATSAPP_PHONE_NUMBER_ID": "100000000000001", "KAPSO_API_KEY": "", "ELEVENLABS_API_KEY": "",
               "ELEVENLABS_AGENT_ID": "", "DEV_AGENT_WS_URL": f"ws://127.0.0.1:{agent_port}", "LOGURU_LEVEL": "WARNING"}
        cli = [sys.executable, "-m", "kapso_voice_agent", "--env-file", os.devnull]
        processes = [subprocess.Popen([*cli, "dev", "fake-agent", "--port", str(agent_port)], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                     subprocess.Popen([*cli, "serve", "--port", str(port)], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
        try:
            base = f"http://127.0.0.1:{port}"
            wait_http(base + "/healthz")
            raw = json.dumps(json.loads((ROOT / "tests/fixtures/inbound_terminate.json").read_text())).encode()
            good = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            results = {
                "healthz": httpx.get(base + "/healthz").json(),
                "webhook_unsigned": httpx.post(base + "/webhooks/whatsapp", content=raw).status_code,
                "webhook_signed": httpx.post(base + "/webhooks/whatsapp", content=raw,
                                             headers={"x-webhook-signature": good}).status_code,
                "operator_without_token": httpx.get(base + "/operator/api/state").status_code,
                "operator_with_token": httpx.get(base + "/operator/api/state",
                                                 headers={"authorization": f"Bearer {token}"}).status_code,
                "console_page": httpx.get(base + "/operator/").status_code,
                "outbound_disabled": httpx.post(base + "/operator/api/outbound/call", json={"recipient": "15550100002"},
                                                headers={"authorization": f"Bearer {token}"}).status_code,
            }
            results["browser_call"] = asyncio.run(browser_call(base, token))
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                process.wait(timeout=10)
    expected = {"healthz": {"ok": True}, "webhook_unsigned": 401, "webhook_signed": 200, "operator_without_token": 401,
                "operator_with_token": 200, "console_page": 200, "outbound_disabled": 403}
    ok = all(results[k] == v for k, v in expected.items()) and results["browser_call"]["hangup_status"] == 200 \
        and results["browser_call"]["tool_ran"]
    print(json.dumps({"ok": ok, **results}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
