"""xAI Direct SIP control service (xai_sip.py), offline: signed webhooks go through the real HTTP
route, and xAI's realtime session is a scripted socket. Nothing here reaches xAI."""

import asyncio
import base64
from dataclasses import replace
import hashlib
import hmac
import json
import time

import pytest

from conftest import asgi_client
from kapso_voice_agent.app import create_app
from kapso_voice_agent.events import EventLog
from kapso_voice_agent.xai_sip import SipCall, caller_identity

KEY = b"synthetic-xai-signing-key-32byte"
SECRET = "whsec_" + base64.b64encode(KEY).decode()
CALL_ID = "rtc_SYNTHETIC_1"


def incoming(identity_headers):
    return {"object": "event", "id": "evt_synthetic", "type": "realtime.call.incoming", "created_at": 1793000000,
            "data": {"call_id": CALL_ID, "sip_headers": identity_headers, "metadata": {}}}


def signed(raw, msg_id="msg_synthetic_1", timestamp=None, key=KEY):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = base64.b64encode(hmac.new(key, f"{msg_id}.{timestamp}.".encode() + raw, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": timestamp, "webhook-signature": f"v1,{signature}",
            "content-type": "application/json"}


@pytest.fixture
def sip_settings(settings):
    return replace(settings, xai_api_key="xai-test-key-not-real", xai_sip_webhook_secret=SECRET,
                   caller_key_secret="test-caller-key-secret")


def test_webhook_needs_a_valid_fresh_signature_and_starts_one_session_per_call(sip_settings, spec, store):
    started = []

    class Recorded:
        def __init__(self, call_id, caller, *rest):
            self.ref = call_id
            started.append((call_id, caller))

        async def run(self):
            pass

    async def go():
        app = create_app(sip_settings, spec, store)
        app.state.sip_calls.call_factory = Recorded
        raw = json.dumps(incoming([{"name": "From", "value": "<sip:+15550100001@wa.meta.vc>;tag=x"}])).encode()
        async with asgi_client(app) as client:
            forged = await client.post("/webhooks/xai-sip", content=raw, headers=signed(raw, key=b"wrong-key"))
            stale = await client.post("/webhooks/xai-sip", content=raw, headers=signed(raw, timestamp=time.time() - 600))
            first = await client.post("/webhooks/xai-sip", content=raw, headers=signed(raw))
            again = await client.post("/webhooks/xai-sip", content=raw, headers=signed(raw, msg_id="msg_synthetic_2"))
        return app, forged, stale, first, again
    app, forged, stale, first, again = asyncio.run(go())
    assert (forged.status_code, stale.status_code) == (401, 401)
    assert first.json() == {"call": "started"} and again.json() == {"call": "duplicate"}
    assert started == [(CALL_ID, app.state.manager.caller_key("+15550100001"))]


def test_webhook_route_is_off_without_its_secret(settings, spec, store):
    async def go():
        async with asgi_client(create_app(settings, spec, store)) as client:
            return await client.post("/webhooks/xai-sip", content=b"{}", headers=signed(b"{}"))
    assert asyncio.run(go()).status_code == 404


def test_caller_identity_comes_from_sip_headers_preferring_metas_user_id():
    assert caller_identity([{"name": "From", "value": "<sip:+15550100001@wa.meta.vc>;tag=a"},
                            {"name": "X-WA-Meta-User-Id", "value": "US.100000000000000001"}]) == "US.100000000000000001"
    assert caller_identity([{"name": "from", "value": "+15550100002"}]) == "+15550100002"
    assert caller_identity([{"name": "To", "value": "+15550100000"}]) is None


class ScriptedSession:
    """xAI's realtime socket for one SIP call: answers each client message from `replies`."""

    def __init__(self, replies):
        self.sent, self.closed, self.queue = [], False, asyncio.Queue()
        self.replies = replies
        self.queue.put_nowait({"type": "session.updated"})

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        for reply in self.replies(message, len(self.sent)):
            self.queue.put_nowait(reply)

    async def recv(self):
        return json.dumps(await self.queue.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def close(self):
        self.closed = True


def test_session_greets_runs_tools_with_strict_confirmation_and_hangs_up_after_goodbye(sip_settings, spec, store):
    def replies(message, count):
        if message.get("item", {}).get("type") == "force_message":
            # The model sends "true" as a string, then says goodbye.
            return [{"type": "response.function_call_arguments.done", "name": "book_appointment", "call_id": "fc_1",
                     "arguments": json.dumps({"slot": "x", "service_id": "y", "name": "Sam", "confirmed": "true"})},
                    {"type": "response.done", "response": {"id": "r1"}}]
        if message.get("type") == "response.create":
            return [{"type": "response.function_call_arguments.done", "name": "end_call", "call_id": "fc_2", "arguments": "{}"},
                    {"type": "response.done", "response": {"id": "r2"}}]
        return []

    async def go():
        socket, hangups, log = ScriptedSession(replies), [], EventLog()

        async def opener():
            return socket

        async def hangup():
            hangups.append(True)

        call = SipCall(CALL_ID, "caller-key-from-sip-headers", sip_settings, spec, store, log,
                       opener=opener, hangup=hangup, sleep=lambda _s: asyncio.sleep(0))
        await asyncio.wait_for(call.run(), timeout=5)
        return call, socket, hangups, log
    call, socket, hangups, log = asyncio.run(go())
    update, greeting = socket.sent[0], socket.sent[1]
    assert update["type"] == "session.update" and "audio" not in update["session"]
    assert greeting["item"]["interruptible"] is False
    assert greeting["item"]["content"][0]["text"] == spec.greeting("inbound", True)
    output = next(m for m in socket.sent if m.get("item", {}).get("type") == "function_call_output")
    assert json.loads(output["item"]["output"])["code"] == "invalid_arguments"  # "true" is not a boolean
    assert call.tools.caller == "caller-key-from-sip-headers"
    assert hangups == [True] and socket.closed
    assert CALL_ID not in json.dumps(list(log.events))
