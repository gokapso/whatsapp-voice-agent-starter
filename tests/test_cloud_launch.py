"""Pipecat Cloud entry point (cloud.py): a Kapso launch body is answered with the server's own call
code, only after Kapso vouches for the call. Real local media, mocked Kapso, fake agent."""

import asyncio
from dataclasses import replace
import json

from aiortc import RTCPeerConnection
import httpx
import pytest

from conftest import PHONE, KapsoRecorder, ToneTrack, fixture, listen
from kapso_voice_agent.calls import CallManager
from kapso_voice_agent.cloud import answer_launch
from kapso_voice_agent.events import EventLog
from test_call_flow import CALL_ID, Harness, apply_answer, close_peer, echo_until

TOKEN_IN_BODY = "fake-meta-token-never-used"
RECORD = {"call_id": CALL_ID, "direction": "USER_INITIATED", "session_started_at": "2026-11-02T15:00:01Z",
          "ended_at": None, "business_scoped_user_id": "US.100000000000000001", "user_wa_id": "15550100001"}


def launch_body(sdp, **call_updates):
    webhook = fixture("inbound_connect.json", session={"sdp_type": "offer", "sdp": sdp}, **call_updates)
    return {"kind": "whatsapp_connect", "webhook": webhook, "whatsapp_token": TOKEN_IN_BODY,
            "phone_number_id": PHONE, "context": {"contact": {"profile_name": "Synthetic"}}}


def kapso_with(record):
    async def saved_call(request):
        if request.method == "GET":
            assert request.url.params["call_id"] == CALL_ID
            return httpx.Response(200, json={"data": [record] if record else []})
        return None
    return KapsoRecorder(saved_call)


def test_verified_launch_is_answered_through_kapso_with_kapsos_caller_identity(settings, spec, store):
    # No webhook secret: a Cloud session has no webhook to verify.
    cloud_settings = replace(settings, webhook_secret="", caller_key_secret="test-caller-key-secret")

    async def go():
        heard, done = asyncio.Event(), {}
        kapso, log = kapso_with(RECORD), EventLog()
        async with Harness(echo_until(heard, done)) as agent:
            peer, consumers = RTCPeerConnection(), []
            peer.addTrack(ToneTrack())
            listen(peer, heard, consumers)
            await peer.setLocalDescription(await peer.createOffer())
            launch = asyncio.create_task(answer_launch(launch_body(peer.localDescription.sdp), cloud_settings, spec,
                                                       store, log, kapso_factory=kapso.factory(),
                                                       session_factory=agent.factory(kapso.actions)))
            try:
                await apply_answer(kapso, peer)
                outcome = await asyncio.wait_for(launch, timeout=20)
            finally:
                await close_peer(peer, consumers)
        return outcome, kapso, agent, log
    outcome, kapso, agent, log = asyncio.run(go())
    assert outcome == "finished"
    assert kapso.actions == ["pre_accept", "accept", "terminate"]
    assert agent.connected_after == [["pre_accept", "accept"]]
    session = agent.sessions[0]
    assert session.bridge.audio_in > 0 and session.bridge.audio_out > 0
    expected = CallManager(cloud_settings, spec, store, log).caller_key("US.100000000000000001")
    assert session.bridge.tools.caller == expected
    sent = json.dumps(kapso.bodies) + json.dumps(list(log.events))
    assert TOKEN_IN_BODY not in sent and CALL_ID not in json.dumps(list(log.events))


@pytest.mark.parametrize("record", [
    None,                                                       # Kapso has no such call on this number
    RECORD | {"business_scoped_user_id": "US.100000000000000099"},  # body claims another caller
    RECORD | {"session_started_at": None},                      # not launched for a voice agent
], ids=["unknown_call", "other_caller", "not_claimed"])
def test_launch_kapso_does_not_vouch_for_is_never_answered(settings, spec, store, record):
    async def go():
        kapso, log = kapso_with(record), EventLog()
        sessions = []
        outcome = await answer_launch(launch_body("v=0 synthetic offer"), settings, spec, store, log,
                                      kapso_factory=kapso.factory(), session_factory=lambda *a, **k: sessions.append(a))
        return outcome, kapso, sessions
    outcome, kapso, sessions = asyncio.run(go())
    assert outcome == "unverified"
    assert kapso.actions == [] and sessions == []
