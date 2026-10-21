#!/usr/bin/env python3
"""Local check of the experimental Inbox handoff (handoff.py) against a Kapso app on your computer.

Everything that would touch Meta or a voice provider is a stand-in: Meta's call actions are
answered here (no request leaves the process), the caller is a local WebRTC peer playing 440 Hz,
and the agent is a fake Grok Voice session playing 1000 Hz that calls transfer_to_human after a
few seconds. Only the handoff signaling goes to the Kapso app, which must be on a loopback
address with the project enrolled in its handoff rollout and a live call row for CALL_ID.

Join from that Kapso app's Inbox; this script prints, each second, which tone the caller hears.

  uv run python scripts/handoff_local_demo.py --kapso http://127.0.0.1:3055 \
      --api-key-file ../cientos/tmp/handoff-demo/api-key --phone-number-id 100000000000777 \
      --call-id wacid.SYNTHETIC-DEMO-1
"""

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import sys
import tempfile
from urllib.parse import urlparse

from aiortc import RTCPeerConnection, RTCSessionDescription
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from conftest import asgi_client, post_webhook  # noqa: E402
from handoff_fakes import AGENT_HZ, FakeGrokAgent  # noqa: E402
from kapso_voice_agent.agent_config import load_spec  # noqa: E402
from kapso_voice_agent.app import create_app  # noqa: E402
from kapso_voice_agent.config import Settings  # noqa: E402
from kapso_voice_agent.kapso import KapsoClient  # noqa: E402
from test_handoff import CALLER_HZ, Ear, Tone  # noqa: E402


class LocalKapso(httpx.AsyncBaseTransport):
    """Meta call actions are answered here; handoff signaling goes to the local Kapso app."""

    def __init__(self, accepts):
        self.accepts, self.real = accepts, httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        if request.url.path.startswith("/meta/"):
            body = json.loads(request.content or b"{}")
            if body.get("action") == "accept":
                self.accepts.append(body["session"]["sdp"])
            return httpx.Response(200, json={"success": True})
        return await self.real.handle_async_request(request)


def connect_payload(phone_number_id, call_id, sdp):
    return {"object": "whatsapp_business_account", "entry": [{"id": "0", "changes": [{"field": "calls", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": phone_number_id},
        "calls": [{"id": call_id, "from": "15550100101", "to": "15550100100", "event": "connect",
                   "direction": "USER_INITIATED", "timestamp": "1790000000",
                   "session": {"sdp_type": "offer", "sdp": sdp}}]}}]}]}


async def main(args):
    if urlparse(args.kapso).hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("--kapso must be a loopback URL: this script is for a Kapso app on your computer")
    key = Path(args.api_key_file).read_text().strip()
    accepts = []
    secret = secrets.token_hex(16)
    async with FakeGrokAgent(transfer_after=args.transfer_after) as agent:
        settings = Settings(kapso_api_key=key, phone_number_id=args.phone_number_id, webhook_secret=secret,
                            voice_provider="grok", dev_agent_ws_url=agent.url, calendar="local",
                            operator_token=secrets.token_hex(24), human_handoff=True,
                            data_dir=Path(tempfile.mkdtemp()), max_session_seconds=args.hold + 30)
        app = create_app(settings, load_spec(settings.agent_config_path), kapso_factory=lambda: KapsoClient(
            key, args.phone_number_id, base_url=args.kapso.rstrip("/"),
            client=httpx.AsyncClient(transport=LocalKapso(accepts), timeout=15)))
        caller = RTCPeerConnection()
        caller.addTrack(Tone(CALLER_HZ))
        ear = Ear(caller)
        await caller.setLocalDescription(await caller.createOffer())
        async with app.router.lifespan_context(app):
            async with asgi_client(app) as client:
                payload = connect_payload(args.phone_number_id, args.call_id, caller.localDescription.sdp)
                response = await post_webhook(client, payload, secret)
                assert response.status_code == 200, response.text
            while not accepts:
                await asyncio.sleep(0.05)
            await caller.setRemoteDescription(RTCSessionDescription(accepts[0], "answer"))
            loop = asyncio.get_running_loop()
            started = loop.time()
            while loop.time() - started < args.hold and not app.state.manager.jobs == {}:
                await asyncio.sleep(1)
                now = loop.time()
                session = next(iter(app.state.manager.sessions.values()), None)
                handoff = session.handoff if session else None
                print(json.dumps({"t": round(now - started), "handoff": handoff.state if handoff else None,
                                  "caller_hears_ai": ear.count(AGENT_HZ, since=now - 1),
                                  "caller_hears_teammate": ear.count(args.teammate_hz, since=now - 1),
                                  "teammate_hears_caller_frames": handoff.leg.frames_out if handoff and handoff.leg else 0,
                                  "ai_connected": not agent.closed.is_set()}), flush=True)
            events = [e["event"] for e in app.state.log.events]
            print(json.dumps({"summary": {"tool_outputs": agent.tool_outputs, "notices": agent.notices,
                                          "events": [e for e in events if e.startswith(("handoff", "tool_transfer",
                                                                                        "terminate", "session"))]}}))
            await app.state.manager.shutdown()
        await caller.close()
        await ear.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--kapso", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--phone-number-id", required=True)
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--transfer-after", type=float, default=4)
    parser.add_argument("--hold", type=int, default=120, help="seconds to keep the call")
    parser.add_argument("--teammate-hz", type=int, default=700, help="tone of the browser's fake microphone")
    asyncio.run(main(parser.parse_args()))
