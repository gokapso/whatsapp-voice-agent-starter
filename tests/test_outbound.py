import asyncio
from dataclasses import replace
import json

from aiortc import RTCPeerConnection, RTCSessionDescription
import httpx
import pytest

from conftest import PHONE, TOKEN, KapsoRecorder, ToneTrack, asgi_client, fixture, listen, post_webhook, tool_call, wait_for_event
from kapso_voice_agent.app import create_app
from kapso_voice_agent.outbound import OutboundError, can_start_call, normalize_recipient
from test_call_flow import Harness, close_peer, echo_until

AUTH = {"authorization": f"Bearer {TOKEN}"}
CALL_ID = "wacid.SYNTHETIC-OUTBOUND-1"


def permission(allowed=True, can_request=False):
    return {"permission": {"status": "temporary" if allowed else "no_permission"},
            "actions": [{"action_name": "send_call_permission_request", "can_perform_action": can_request},
                        {"action_name": "start_call", "can_perform_action": allowed}]}


def test_recipient_and_permission_rules():
    assert normalize_recipient("+1 555 010 0002") == "15550100002"
    assert normalize_recipient("US.100000000000000002") == "US.100000000000000002"
    for bad in ("../../calls", "123", "US.12"):
        with pytest.raises(OutboundError):
            normalize_recipient(bad)
    assert can_start_call(permission()) and not can_start_call(permission(False)) and not can_start_call({})


def test_no_permission_never_dials(settings, spec, store):
    async def go():
        async def handler(request):
            return httpx.Response(200, json=permission(False)) if request.method == "GET" else None
        kapso = KapsoRecorder(handler)
        app = create_app(replace(settings, enable_outbound=True), spec, store, kapso_factory=kapso.factory())
        async with asgi_client(app) as client:
            checked = await client.post("/operator/api/outbound/permission", headers=AUTH, json={"recipient": "15550100002"})
            dialed = await client.post("/operator/api/outbound/call", headers=AUTH, json={"recipient": "15550100002"})
        return checked.json(), dialed.status_code, kapso.actions, app.state.manager
    checked, status, actions, manager = asyncio.run(go())
    assert checked == {"permission_status": "no_permission", "can_call": False, "can_request": False}
    assert status == 409 and actions == [] and not manager.jobs and not manager.outbound.dialing


def ask(settings, spec, store, recipient, current, sent=None):
    """POST /outbound/permission-request against a mock Kapso. `current` is the permission state
    Meta reports; `sent` answers the message request (default: Meta's success shape)."""
    async def go():
        paths = []

        async def handler(request):
            paths.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, json=current)
            return sent or httpx.Response(200, json={"messages": [{"id": "wamid.SYNTHETIC-1"}]})
        kapso = KapsoRecorder(handler)
        app = create_app(replace(settings, enable_outbound=True), spec, store, kapso_factory=kapso.factory())
        async with asgi_client(app) as client:
            response = await client.post("/operator/api/outbound/permission-request", headers=AUTH,
                                         json={"recipient": recipient})
        return response, [b for b in kapso.bodies if b], paths, list(app.state.log.events)
    return asyncio.run(go())


@pytest.mark.parametrize("recipient,field,value", [("+1 555 010 0002", "to", "15550100002"),
                                                   ("US.100000000000000002", "recipient", "US.100000000000000002")])
def test_permission_request_sends_one_interactive_message(settings, spec, store, recipient, field, value):
    response, bodies, paths, events = ask(settings, spec, store, recipient, permission(False, can_request=True))
    assert response.status_code == 200 and response.json() == {
        "permission_status": "no_permission", "can_call": False, "can_request": True, "sent": True}
    assert paths == [("GET", f"/meta/whatsapp/v24.0/{PHONE}/call_permissions"),
                     ("POST", f"/meta/whatsapp/v24.0/{PHONE}/messages")]
    assert bodies == [{"messaging_product": "whatsapp", "recipient_type": "individual", field: value,
                       "type": "interactive", "interactive": {
                           "type": "call_permission_request", "action": {"name": "call_permission_request"},
                           "body": {"text": spec.permission_request}}}]
    assert [e["event"] for e in events] == ["permission_request_sent"] and "555" not in json.dumps(events)


def test_permission_request_is_not_sent_when_calls_are_allowed_or_limited(settings, spec, store):
    allowed, bodies, _, _ = ask(settings, spec, store, "15550100002", permission(True, can_request=True))
    assert allowed.json() == {"permission_status": "temporary", "can_call": True, "can_request": True, "sent": False}
    assert bodies == []
    limited, bodies, _, events = ask(settings, spec, store, "15550100002", permission(False, can_request=False))
    assert limited.status_code == 429 and "one call permission request" in limited.json()["detail"]
    assert bodies == [] and [e["event"] for e in events] == ["permission_request_limited"]


def test_permission_request_outside_the_chat_window_is_explained_and_not_retried(settings, spec, store):
    closed = httpx.Response(422, headers={"x-request-id": "req-SYNTHETIC-2"}, json={"error": {
        "message": "Cannot send non-template messages outside the 24-hour window.", "code": 131047}})
    response, bodies, _, events = ask(settings, spec, store, "15550100002", permission(False, can_request=True), closed)
    assert response.status_code == 502 and "within 24 hours" in response.json()["detail"]
    assert len(bodies) == 1
    assert events[-1] == {**events[-1], "event": "permission_request_failed", "http_status": 422, "code": "131047",
                          "request_id": "req-synthetic-2"}


def test_ambiguous_connect_is_not_retried_and_releases_media(settings, spec, store):
    async def go():
        async def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=permission())
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        kapso = KapsoRecorder(handler)
        app = create_app(replace(settings, enable_outbound=True), spec, store, kapso_factory=kapso.factory())
        manager, connections = app.state.manager, []
        original = manager.connection_factory

        def tracking(ice):
            connection = original(ice)
            connections.append(connection)
            return connection
        manager.connection_factory = tracking
        with pytest.raises(OutboundError, match="outcome is unknown"):
            await manager.outbound.start("15550100002")
        return kapso.actions, connections, manager
    actions, connections, manager = asyncio.run(go())
    assert actions == ["connect"] and connections[0].pc.connectionState == "closed"
    assert not manager.jobs and not manager.outbound.dialing


@pytest.mark.parametrize("events", [["connect", "terminate"], ["terminate", "connect"]])
def test_terminate_batched_before_connect_response_wins(settings, spec, store, events):
    async def go():
        app = create_app(replace(settings, enable_outbound=True), spec, store)
        manager = app.state.manager

        async def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=permission())
            payload = fixture("outbound_answer.json")
            row = payload["entry"][0]["changes"][0]["value"]["calls"][0]
            payload["entry"][0]["changes"][0]["value"]["calls"] = [{**row, "event": e} for e in events]
            await manager.receive(payload)
            return httpx.Response(200, json={"calls": [{"id": CALL_ID}]})
        kapso = KapsoRecorder(handler)
        manager.kapso_factory = kapso.factory()
        result = await manager.outbound.start("15550100002")
        return result, kapso.actions, manager
    result, actions, manager = asyncio.run(go())
    assert result["state"] == "terminated" and actions == ["connect"]
    assert not manager.jobs and not manager.sessions and not manager.outbound.pending


@pytest.mark.parametrize("early_accept", [False, True])
def test_outbound_call_waits_for_accepted_then_runs_one_agent(settings, spec, store, early_accept):
    async def go():
        heard, done = asyncio.Event(), {}
        peer, consumers = RTCPeerConnection(), []
        listen(peer, heard, consumers)
        async with Harness(echo_until(heard, done, minimum=15, first={
                "type": "client_tool_call", "client_tool_call": tool_call("my_appointments")})) as agent:
            app = create_app(replace(settings, enable_outbound=True), spec, store, session_factory=agent.factory())
            manager = app.state.manager

            async def handler(request):
                if request.method == "GET":
                    assert request.url.params["user_wa_id"] == "15550100002"
                    return httpx.Response(200, json=permission())
                body = json.loads(request.content)
                if body["action"] != "connect":
                    return None
                assert body["to"] == "15550100002" and "recording" not in body and "transcription" not in body
                await peer.setRemoteDescription(RTCSessionDescription(body["session"]["sdp"], "offer"))
                peer.addTrack(ToneTrack())
                await peer.setLocalDescription(await peer.createAnswer())
                payload = fixture("outbound_answer.json", session={"sdp_type": "answer", "sdp": peer.localDescription.sdp})
                if early_accept:
                    payload["entry"][0]["changes"][0]["value"]["statuses"] = \
                        fixture("outbound_accepted.json")["entry"][0]["changes"][0]["value"]["statuses"]
                async with asgi_client(app) as receiver:  # Meta's answer can beat the HTTP response
                    assert (await post_webhook(receiver, payload)).status_code == 200
                    assert (await post_webhook(receiver, payload)).status_code == 200
                return httpx.Response(200, json={"calls": [{"id": CALL_ID}]})
            kapso = KapsoRecorder(handler)
            manager.kapso_factory = kapso.factory()
            try:
                result = await manager.outbound.start("+1 555 010 0002")
                assert result["state"] == "ringing" and result["ref"].startswith("call-")
                job = manager.jobs[CALL_ID]
                if not early_accept:
                    await wait_for_event(manager.log, "outbound_answer_applied")
                    assert not agent.sessions  # ringing: no agent until the person picks up
                    async with asgi_client(app) as receiver:
                        assert (await post_webhook(receiver, fixture("outbound_accepted.json"))).status_code == 200
                await asyncio.wait_for(job, timeout=25)
                await manager.receive(fixture("outbound_answer.json", session={"sdp_type": "answer", "sdp": "late"}))
            finally:
                await manager.shutdown()
                await close_peer(peer, consumers)
        return kapso, agent, manager, done
    kapso, agent, manager, done = asyncio.run(go())
    assert kapso.actions == ["connect", "terminate"] and len(agent.sessions) == 1
    session = agent.sessions[0]
    assert session.closed and session.bridge.audio_in > 0 and session.bridge.audio_out > 0
    assert session.bridge.tools.caller == manager.caller_key("US.100000000000000002")
    assert done["tool"] == {"ok": True, "appointments": []}
    assert agent.inits[0]["dynamic_variables"]["call_purpose"] == "outbound_appointment_confirmation"
    assert not manager.jobs and not manager.sessions and not manager.outbound.pending
