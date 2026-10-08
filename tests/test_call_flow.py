"""Real local media: an aiortc peer plays the WhatsApp caller, a local WebSocket plays the agent.
Kapso is mocked. These exercise the actual SmallWebRTC transport, Pipecat pipeline and bridge."""

import asyncio
import base64
from dataclasses import replace
import json
import wave

from aiortc import RTCPeerConnection, RTCSessionDescription
import httpx
import numpy as np
import pytest
from websockets.asyncio.server import serve

from conftest import TOKEN, KapsoRecorder, ToneTrack, asgi_client, fixture, listen, post_webhook, tool_call, wait_for_event
from kapso_voice_agent.app import create_app
from kapso_voice_agent.bridge import ElevenSession

CALL_ID = "wacid.SYNTHETIC-INBOUND-1"


class Harness:
    """Fake agent + session factory bound to it. `script` decides how the agent behaves."""

    def __init__(self, script):
        self.script, self.sessions, self.inits, self.server = script, [], [], None
        self.connected_after = []

    async def __aenter__(self):
        self.server = await serve(self.handle, "127.0.0.1", 0).__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.server.__aexit__(*exc)

    async def handle(self, socket):
        self.inits.append(json.loads(await socket.recv()))
        await socket.send(json.dumps({"type": "conversation_initiation_metadata", "conversation_initiation_metadata_event": {
            "conversation_id": "fake-conversation-0001", "user_input_audio_format": "pcm_16000",
            "agent_output_audio_format": "pcm_16000"}}))
        await self.script(socket)

    async def url(self, _settings):
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    def factory(self, actions=None):
        def make(*args, **kwargs):
            if actions is not None:
                self.connected_after.append(list(actions))
            session = ElevenSession(*args, url_factory=self.url, **kwargs)
            self.sessions.append(session)
            return session
        return make


def echo_until(heard, done, minimum=8, first=None):
    async def script(socket):
        if first:
            await socket.send(json.dumps(first))
        count = 0
        async for raw in socket:
            message = json.loads(raw)
            if message.get("type") == "client_tool_result":
                done["tool"] = json.loads(message["result"])
            if "user_audio_chunk" in message:
                count += 1
                assert len(base64.b64decode(message["user_audio_chunk"])) == 3200
                await socket.send(json.dumps({"type": "audio", "audio_event": {
                    "event_id": count, "audio_base_64": message["user_audio_chunk"]}}))
                if count >= minimum and heard.is_set() and (not first or "tool" in done):
                    await socket.close()  # agent ends the conversation (like end_call)
                    return
    return script


async def inbound_call(app, peer):
    await peer.setLocalDescription(await peer.createOffer())
    payload = fixture("inbound_connect.json", session={"sdp_type": "offer", "sdp": peer.localDescription.sdp})
    async with asgi_client(app) as client:
        assert (await post_webhook(client, payload)).status_code == 200
    return app.state.manager.jobs[CALL_ID]


async def apply_answer(kapso, peer, timeout=10):
    async def accepted():
        while "accept" not in kapso.actions:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(accepted(), timeout)
    sdp = next(b["session"]["sdp"] for b in kapso.bodies if b.get("action") == "accept")
    await peer.setRemoteDescription(RTCSessionDescription(sdp, "answer"))


async def close_peer(peer, consumers):
    await peer.close()
    for consumer in consumers:
        consumer.cancel()
    await asyncio.gather(*consumers, return_exceptions=True)


def test_inbound_call_exchanges_audio_runs_tool_and_terminates_after_agent_ends(settings, spec, store):
    async def go():
        heard, done = asyncio.Event(), {}
        kapso = KapsoRecorder()
        async with Harness(echo_until(heard, done, first={"type": "client_tool_call",
                                                          "client_tool_call": tool_call("available_slots")})) as agent:
            app = create_app(settings, spec, store, session_factory=agent.factory(kapso.actions),
                             kapso_factory=kapso.factory())
            peer, consumers = RTCPeerConnection(), []
            peer.addTrack(ToneTrack())
            listen(peer, heard, consumers)
            try:
                job = await inbound_call(app, peer)
                async with asgi_client(app) as client:  # duplicate delivery: same job, no second session
                    payload = fixture("inbound_connect.json", session={"sdp_type": "offer", "sdp": peer.localDescription.sdp})
                    assert (await post_webhook(client, payload)).status_code == 200
                assert app.state.manager.jobs[CALL_ID] is job
                await apply_answer(kapso, peer)
                await asyncio.wait_for(job, timeout=20)
            finally:
                await app.state.manager.shutdown()
                await close_peer(peer, consumers)
        return kapso, agent, done, app
    kapso, agent, done, app = asyncio.run(go())
    assert kapso.actions == ["pre_accept", "accept", "terminate"]
    # The agent (and its greeting) starts only after accept succeeded.
    assert agent.connected_after == [["pre_accept", "accept"]]
    session = agent.sessions[0]
    assert session.closed and session.bridge.audio_in > 0 and session.bridge.audio_out > 0
    assert done["tool"]["ok"] is True and done["tool"]["slots"]
    variables = agent.inits[0]["dynamic_variables"]
    assert variables["opening_message"] == spec.greeting("inbound", True) and variables["today"] == "2026-11-02"
    assert session.bridge.tools.caller == app.state.manager.caller_key("US.100000000000000001")
    events = json.dumps(list(app.state.log.events))
    assert CALL_ID not in events and "15550100001" not in events and "US.1000" not in events
    accept = next(b for b in kapso.bodies if b["action"] == "accept")
    assert "recording" not in accept and "transcription" not in accept
    assert "a=fingerprint:sha-512" not in accept["session"]["sdp"]


def test_caller_hangup_closes_session_without_sending_terminate(settings, spec, store):
    async def go():
        heard, done = asyncio.Event(), {}
        kapso = KapsoRecorder()
        async with Harness(echo_until(heard, done, minimum=10_000)) as agent:
            app = create_app(settings, spec, store, session_factory=agent.factory(), kapso_factory=kapso.factory())
            peer, consumers = RTCPeerConnection(), []
            peer.addTrack(ToneTrack())
            listen(peer, heard, consumers)
            try:
                job = await inbound_call(app, peer)
                await apply_answer(kapso, peer)
                await asyncio.wait_for(heard.wait(), timeout=15)
                async with asgi_client(app) as client:
                    assert (await post_webhook(client, fixture("inbound_terminate.json"))).status_code == 200
                await asyncio.gather(job, return_exceptions=True)
            finally:
                await app.state.manager.shutdown()
                await close_peer(peer, consumers)
        return kapso, agent, app
    kapso, agent, app = asyncio.run(go())
    assert kapso.actions == ["pre_accept", "accept"]
    assert agent.sessions[0].closed and not app.state.manager.sessions and not app.state.manager.jobs


@pytest.mark.parametrize("failing", ["pre_accept", "accept"])
def test_failure_before_accept_rejects_the_call_and_starts_no_agent(settings, spec, store, failing):
    async def go():
        async def handler(request):
            if json.loads(request.content).get("action") == failing:
                return httpx.Response(500, json={"error": {"message": "synthetic failure"}})
            return None
        kapso = KapsoRecorder(handler)
        async with Harness(echo_until(asyncio.Event(), {})) as agent:
            app = create_app(settings, spec, store, session_factory=agent.factory(), kapso_factory=kapso.factory())
            peer = RTCPeerConnection()
            peer.addTrack(ToneTrack())
            try:
                job = await inbound_call(app, peer)
                await asyncio.wait_for(job, timeout=10)
            finally:
                await app.state.manager.shutdown()
                await peer.close()
        return kapso, agent
    kapso, agent = asyncio.run(go())
    expected = ["pre_accept", "reject"] if failing == "pre_accept" else ["pre_accept", "accept", "reject"]
    assert kapso.actions == expected and agent.sessions == []


def test_agent_start_failure_after_accept_terminates(settings, spec, store):
    async def go():
        kapso = KapsoRecorder()

        async def broken_url(_settings):
            raise RuntimeError("synthetic agent outage")

        def factory(*args, **kwargs):
            return ElevenSession(*args, url_factory=broken_url, **kwargs)

        app = create_app(settings, spec, store, session_factory=factory, kapso_factory=kapso.factory())
        peer = RTCPeerConnection()
        peer.addTrack(ToneTrack())
        try:
            job = await inbound_call(app, peer)
            await apply_answer(kapso, peer)
            await asyncio.wait_for(job, timeout=15)
            await wait_for_event(app.state.log, "agent_start_failed_runtimeerror")
        finally:
            await app.state.manager.shutdown()
            await peer.close()
        return kapso
    assert asyncio.run(go()).actions == ["pre_accept", "accept", "terminate"]


def test_local_capture_is_private_aligned_and_keeps_text_out_of_logs(settings, spec, store):
    async def go():
        heard = asyncio.Event()

        async def script(socket):
            await socket.send(json.dumps({"type": "agent_response", "agent_response_event": {"agent_response": "Hi there."}}))
            await socket.send(json.dumps({"type": "user_transcript", "user_transcription_event": {"user_transcript": "Hello."}}))
            await echo_until(heard, {}, minimum=25)(socket)

        kapso = KapsoRecorder()
        async with Harness(script) as agent:
            app = create_app(replace(settings, local_capture=True), spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            peer, consumers = RTCPeerConnection(), []
            peer.addTrack(ToneTrack())
            listen(peer, heard, consumers)
            try:
                job = await inbound_call(app, peer)
                await apply_answer(kapso, peer)
                await asyncio.wait_for(job, timeout=20)
            finally:
                await app.state.manager.shutdown()
                await close_peer(peer, consumers)
        return agent, app
    agent, app = asyncio.run(go())
    directory = agent.sessions[0].capture_dir
    assert directory is not None and "wacid" not in directory.name
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["conversation_id"] == "fake-conversation-0001" and manifest["truncated"] is False
    for path in directory.iterdir():
        assert oct(path.stat().st_mode & 0o777) == "0o600", path.name
    tracks = {}
    for name in ("caller", "agent"):
        with wave.open(str(directory / f"{name}.wav")) as audio:
            assert audio.getframerate() == 16000 and audio.getnchannels() == 1
            tracks[name] = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
    assert len(tracks["caller"]) == len(tracks["agent"]) > 16000
    assert np.abs(tracks["caller"]).max() > 1000 and np.abs(tracks["agent"]).max() > 1000
    turns = json.loads((directory / "turns.json").read_text())["turns"]
    assert [(t["role"], t["text"]) for t in turns] == [("agent", "Hi there."), ("user", "Hello.")]
    assert agent.inits[0]["dynamic_variables"]["recording_status"].startswith("This call's audio is recorded")
    assert "Hi there." not in json.dumps(list(app.state.log.events))


def test_browser_call_uses_operator_api_and_its_own_caller_identity(settings, spec, store):
    async def go():
        heard = asyncio.Event()
        async with Harness(echo_until(heard, {}, minimum=8)) as agent:
            app = create_app(settings, spec, store, session_factory=agent.factory(), kapso_factory=KapsoRecorder().factory())
            peer, consumers = RTCPeerConnection(), []
            peer.addTrack(ToneTrack())
            peer.createDataChannel("chat")
            listen(peer, heard, consumers)
            try:
                await peer.setLocalDescription(await peer.createOffer())
                async with asgi_client(app) as client:
                    response = await client.post("/operator/api/browser-call", json={"sdp": peer.localDescription.sdp},
                                                 headers={"authorization": f"Bearer {TOKEN}"})
                    assert response.status_code == 200, response.text
                    answer = response.json()
                    await peer.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
                    await asyncio.wait_for(heard.wait(), timeout=15)
                    busy = await client.post("/operator/api/browser-call", json={"sdp": peer.localDescription.sdp},
                                             headers={"authorization": f"Bearer {TOKEN}"})
                    hangup = await client.post(f"/operator/api/calls/{answer['ref']}/hangup",
                                               headers={"authorization": f"Bearer {TOKEN}"})
            finally:
                await app.state.manager.shutdown()
                await close_peer(peer, consumers)
        return agent, app, busy, hangup
    agent, app, busy, hangup = asyncio.run(go())
    assert busy.status_code == 409 and hangup.status_code == 200
    assert agent.sessions[0].closed and not app.state.manager.jobs
    assert agent.sessions[0].bridge.tools.caller != app.state.manager.caller_key("US.100000000000000001")
