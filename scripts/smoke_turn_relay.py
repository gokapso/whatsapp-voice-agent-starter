"""Container smoke check for hosts without inbound UDP (the Railway topology). Needs Docker; offline
apart from pulling the coturn/coturn image once. Nothing reaches a provider, Kapso or Meta.

    docker build -t kapso-voice-starter:dev .
    uv run python scripts/smoke_turn_relay.py

What it starts, on two internal Docker networks with no route between them (plain bridge
networks are routed to each other on some Docker hosts, OrbStack among them):

- server network: this image as the server, started as root with a fresh root-owned volume (as
  Railway does with RAILWAY_RUN_UID=0) and PORT set, and the offline fake Grok agent in the same
  network namespace.
- caller network: the same image as a stand-in caller (an aiortc peer sending a tone).
- on both: coturn, and a TCP-only forwarder to the server's PORT that plays the host's HTTPS edge.

Then a browser-style test call: the caller posts its offer through the forwarder. Audio can only
flow through coturn over TCP, like media to a Railway service through a TURN relay.

Pass: the server drops root and owns a 0700 DATA_DIR, /healthz answers on $PORT, the caller hears
agent audio, the tool runs, and the server logs `rtc_connected` with media `relay`. This proves the
image, the entrypoint and aiortc's TURN-over-TCP path. It does not prove Railway itself, a hosted
TURN service, or Meta's media.
"""

import argparse
import asyncio
import fractions
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

IMAGE = os.environ.get("STARTER_IMAGE", "kapso-voice-starter:dev")
TURN_IMAGE = "coturn/coturn:4.6"
TURN_USER, TURN_PASSWORD = "starter", "relay-test-only"
# TCP-only forwarder: the caller reaches HTTP through it, never the server's addresses.
EDGE = """
import asyncio
async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
async def handle(reader, writer):
    upstream_reader, upstream_writer = await asyncio.open_connection("{server}", {port})
    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer), return_exceptions=True)
async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", {port})
    await server.serve_forever()
asyncio.run(main())
"""


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=180)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def orchestrate():
    tag = secrets.token_hex(3)
    names = {k: f"starter-smoke-{k}-{tag}" for k in ("servernet", "callernet", "turn", "server", "agent", "edge", "data")}
    port, token = free_port(), secrets.token_urlsafe(32)
    turn_url = f"turn:{names['turn']}:3478?transport=tcp"
    report = {}
    try:
        docker("network", "create", "--internal", names["servernet"])
        docker("network", "create", "--internal", names["callernet"])
        docker("volume", "create", names["data"])
        # coturn listens only on the interfaces it sees at start: attach both networks first.
        docker("create", "--name", names["turn"], "--network", names["servernet"], TURN_IMAGE,
               "-n", "--log-file=stdout", "--lt-cred-mech", f"--user={TURN_USER}:{TURN_PASSWORD}",
               "--realm=starter.test", "--listening-port=3478", "--no-tls", "--no-dtls", "--no-cli",
               "--min-port=49160", "--max-port=49260")
        docker("network", "connect", names["callernet"], names["turn"])
        docker("start", names["turn"])
        ice = json.dumps([{"urls": turn_url, "username": TURN_USER, "credential": TURN_PASSWORD}])
        env = {"PORT": str(port), "OPERATOR_TOKEN": token, "CALLER_KEY_SECRET": secrets.token_hex(32),
               "CALENDAR": "local", "VOICE_PROVIDER": "grok", "DEV_AGENT_WS_URL": "ws://127.0.0.1:8765",
               "ICE_SERVERS_JSON": ice, "LOGURU_LEVEL": "WARNING"}
        docker("run", "-d", "--name", names["server"], "--network", names["servernet"], "--user", "0",
               "-v", f"{names['data']}:/app/data",
               *[part for key, value in env.items() for part in ("-e", f"{key}={value}")], IMAGE)
        docker("run", "-d", "--name", names["agent"], "--network", f"container:{names['server']}", IMAGE,
               "voice-agent", "dev", "fake-agent", "--provider", "grok")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if docker("exec", names["server"], "python", "-c",
                      f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/healthz', timeout=2)",
                      check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("server did not become healthy:\n" + docker("logs", names["server"], check=False).stdout[-2000:])
        docker("run", "-d", "--name", names["edge"], "--network", names["servernet"], IMAGE, "python", "-c",
               EDGE.format(port=port, server=names["server"]))
        docker("network", "connect", names["callernet"], names["edge"])
        uid = docker("exec", names["server"], "sh", "-c", "grep '^Uid:' /proc/1/status").stdout.split()
        data = docker("exec", names["server"], "stat", "-c", "%u %a", "/app/data").stdout.split()
        report["server_process_uid"] = uid[1] if len(uid) > 1 else None
        report["data_dir_owner_mode"] = data
        caller = docker("run", "--rm", "--network", names["callernet"], "-v", f"{Path(__file__).resolve().parent}:/smoke:ro",
                        "-e", f"SMOKE_TOKEN={token}", IMAGE,
                        "python", "/smoke/smoke_turn_relay.py", "caller", "--base", f"http://{names['edge']}:{port}",
                        "--turn", turn_url, check=False)
        report["caller"] = json.loads(caller.stdout.strip().splitlines()[-1]) if caller.stdout.strip() else {
            "error": caller.stderr[-2000:]}
        server_log = docker("logs", names["server"], check=False)
        report["server_log_has_token"] = token in server_log.stdout + server_log.stderr
        if not report["caller"].get("media"):
            # Diagnostics only; the relay credentials here are throwaway test values.
            report["turn_log_tail"] = docker("logs", names["turn"], check=False).stdout.splitlines()[-25:]
            report["server_log_tail"] = (server_log.stdout + server_log.stderr).splitlines()[-25:]
    finally:
        for name in ("agent", "edge", "server", "turn"):
            docker("rm", "-f", names[name], check=False)
        docker("volume", "rm", names["data"], check=False)
        for name in ("servernet", "callernet"):
            docker("network", "rm", names[name], check=False)
    call = report.get("caller", {})
    report["ok"] = (report.get("server_process_uid") == "10001" and report.get("data_dir_owner_mode") == ["10001", "700"]
                    and call.get("heard_agent_audio") and call.get("tool_ran") and call.get("media") == "relay"
                    and call.get("hangup_status") == 200 and not report["server_log_has_token"])
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


async def caller(base, turn):
    """Runs inside the caller container: an aiortc peer that can only use the TURN relay."""
    from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
    import av
    import httpx
    import numpy as np

    class Tone(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.timestamp = 0

        async def recv(self):
            await asyncio.sleep(0.02)
            signal = (np.sin((np.arange(960) + self.timestamp) * 2 * np.pi * 440 / 48000) * 5000).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(signal.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate, frame.pts, frame.time_base = 48000, self.timestamp, fractions.Fraction(1, 48000)
            self.timestamp += 960
            return frame

    headers = {"authorization": f"Bearer {os.environ['SMOKE_TOKEN']}"}
    peer = RTCPeerConnection(RTCConfiguration([RTCIceServer(urls=turn, username=TURN_USER, credential=TURN_PASSWORD)]))
    peer.addTrack(Tone())
    heard, consumers = asyncio.Event(), []

    @peer.on("track")
    def track(remote):
        async def consume():
            while True:
                frame = await remote.recv()
                if np.abs(frame.to_ndarray().astype(np.int32)).max() > 500:
                    heard.set()
        consumers.append(asyncio.create_task(consume()))

    result = {}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as client:
        await peer.setLocalDescription(await peer.createOffer())
        answer = await client.post("/operator/api/browser-call", json={"sdp": peer.localDescription.sdp})
        result["browser_call_status"] = answer.status_code
        if answer.status_code == 200:
            body = answer.json()
            await peer.setRemoteDescription(RTCSessionDescription(body["sdp"], "answer"))
            try:
                await asyncio.wait_for(heard.wait(), timeout=30)
            except TimeoutError:
                pass
            await asyncio.sleep(1)
            events = (await client.get("/operator/api/state")).json()["events"]
            result["heard_agent_audio"] = heard.is_set()
            result["tool_ran"] = any(e["event"] == "tool_available_slots_ok" for e in events)
            result["media"] = next((e.get("media") for e in events if e["event"] == "rtc_connected"), None)
            result["hangup_status"] = (await client.post(f"/operator/api/calls/{body['ref']}/hangup")).status_code
    await peer.close()
    for consumer in consumers:
        consumer.cancel()
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="orchestrate", choices=("orchestrate", "caller"))
    parser.add_argument("--base")
    parser.add_argument("--turn")
    args = parser.parse_args()
    if args.mode == "caller":
        asyncio.run(caller(args.base, args.turn))
        return 0
    return orchestrate()


if __name__ == "__main__":
    sys.exit(main())
