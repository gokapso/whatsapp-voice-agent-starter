"""Experimental Inbox handoff (handoff.py) with real local media. Three aiortc peers and a fake
agent, each with its own tone, so every assertion is about audio that actually arrived:

- caller (Meta leg stand-in) sends 440 Hz,
- the AI agent (local ElevenLabs-shaped WebSocket) sends 1000 Hz,
- the teammate's browser (second peer, answered by this server) sends 700 Hz.

Kapso's handoff API is a small in-memory state machine with Kapso's rules (FakeHandoffs)."""

import asyncio
import base64
from dataclasses import replace
import fractions
import json

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
import av
import httpx
import numpy as np
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from conftest import KapsoRecorder, asgi_client, wait_for_event
from kapso_voice_agent import handoff as handoff_module
from kapso_voice_agent.app import create_app
from kapso_voice_agent.bridge import ElevenSession
from kapso_voice_agent.config import Settings
from kapso_voice_agent.grok_bridge import session_update
from test_call_flow import CALL_ID, apply_answer, inbound_call

CALLER_HZ, AGENT_HZ, TEAMMATE_HZ = 440, 1000, 700


class Tone(MediaStreamTrack):
    kind = "audio"

    def __init__(self, hz):
        super().__init__()
        self.hz, self.timestamp = hz, 0

    async def recv(self):
        await asyncio.sleep(0.02)
        position = np.arange(960) + self.timestamp
        signal = (np.sin(position * 2 * np.pi * self.hz / 48000) * 6000).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(signal.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = 48000, self.timestamp, fractions.Fraction(1, 48000)
        self.timestamp += 960
        return frame


def dominant_hz(frame):
    samples = frame.to_ndarray().astype(np.float64).reshape(-1)
    if frame.layout.name == "stereo":
        samples = samples[::2]
    if np.abs(samples).max() < 400:
        return None
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return round(np.argmax(spectrum) * frame.sample_rate / len(samples))


class Ear:
    """Records the dominant tone of every received frame with its arrival time."""

    def __init__(self, peer):
        self.heard, self.tasks = [], []

        @peer.on("track")
        def track(track):
            async def consume():
                while True:
                    frame = await track.recv()
                    self.heard.append((asyncio.get_running_loop().time(), dominant_hz(frame)))
            self.tasks.append(asyncio.create_task(consume()))

    def count(self, hz, since=0.0, until=float("inf")):
        return sum(1 for at, tone in self.heard if since <= at < until and tone and abs(tone - hz) < 60)

    async def wait_for(self, hz, frames=10, since=0.0, timeout=10):
        async def poll():
            while self.count(hz, since) < frames:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(poll(), timeout)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class ToneAgent:
    """ElevenLabs-shaped agent that talks continuously at 1000 Hz and counts caller audio."""

    def __init__(self, fail_after=None):
        self.user_chunks, self.server, self.closed = [], None, asyncio.Event()
        self.fail_after, self.connections = fail_after, 0

    async def __aenter__(self):
        self.server = await serve(self.handle, "127.0.0.1", 0).__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.server.__aexit__(*exc)

    async def url(self, _settings):
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    def factory(self):
        def make(*args, **kwargs):
            return ElevenSession(*args, url_factory=self.url, **kwargs)
        return make

    async def handle(self, socket):
        self.connections += 1
        await socket.recv()
        if self.fail_after is not None and self.connections > self.fail_after:
            await socket.close()
            return
        await socket.send(json.dumps({"type": "conversation_initiation_metadata", "conversation_initiation_metadata_event": {
            "conversation_id": "fake-conversation-0002", "user_input_audio_format": "pcm_16000",
            "agent_output_audio_format": "pcm_16000"}}))

        async def talk():
            position, event = np.arange(1600), 0
            while True:
                signal = (np.sin(position * 2 * np.pi * AGENT_HZ / 16000) * 6000).astype(np.int16)
                position, event = position + 1600, event + 1
                await socket.send(json.dumps({"type": "audio", "audio_event": {
                    "event_id": event, "audio_base_64": base64.b64encode(signal.tobytes()).decode()}}))
                await asyncio.sleep(0.1)
        talker = asyncio.create_task(talk())
        try:
            async for raw in socket:
                if "user_audio_chunk" in json.loads(raw):
                    self.user_chunks.append(asyncio.get_running_loop().time())
        except ConnectionClosed:
            pass
        finally:
            talker.cancel()
            self.closed.set()


class FakeHandoffs:
    """Kapso's handoff rules in memory: request -> joining (browser offer) -> committed -> ended."""

    def __init__(self, refuse_request=False):
        self.rows, self.requests, self.ends, self.refuse_request = {}, [], [], refuse_request
        self.lose_return_ack = False

    async def __call__(self, request):
        path = request.url.path
        if not path.startswith("/platform/v1/whatsapp/call_handoffs"):
            return None
        body = json.loads(request.content) if request.content else {}
        parts = path.removeprefix("/platform/v1/whatsapp/call_handoffs").strip("/").split("/")
        if parts == [""]:
            self.requests.append(body)
            if self.refuse_request:
                return httpx.Response(403, json={"error": "handoff_unavailable"})
            row = self.rows.setdefault(body["request_key"], {"id": f"handoff-{len(self.rows) + 1}", "state": "requested",
                                                             "end_reason": None, "offer": None, "answer": None,
                                                             "browser_ready": False})
            return self.render(row, 201)
        row = next(r for r in self.rows.values() if r["id"] == parts[0])
        action = parts[1] if len(parts) > 1 else None
        if action == "answer" and row["state"] == "joining":
            row["answer"] = body["sdp"]
        elif action == "commit":
            if row["state"] == "joining" and not row["browser_ready"]:
                return httpx.Response(409, json={"error": "browser_not_ready", "state": row["state"],
                                                 "end_reason": None})
            if row["state"] != "joining" or not row["answer"]:
                return httpx.Response(409, json={"error": "handoff_ended", "state": row["state"],
                                                 "end_reason": row["end_reason"]})
            row["state"] = "committed"
        elif action == "complete_return":
            assert body["request_key"] == row["return_request_key"]
            row["return_pending"] = False
            if body["outcome"] == "ready":
                self.end(row, "returned_to_agent")
                if self.lose_return_ack:
                    self.lose_return_ack = False
                    raise httpx.ReadError("synthetic lost acknowledgement")
        elif action == "end":
            self.ends.append(body["reason"])
            self.end(row, body["reason"])
        return self.render(row)

    def render(self, row, status=200):
        offer = row["offer"] if row["state"] == "joining" and not row["answer"] else None
        return httpx.Response(status, json={"data": {"id": row["id"], "state": row["state"],
                                                     "end_reason": row["end_reason"], "offer_sdp": offer,
                                                     "browser_ready": row["browser_ready"],
                                                     "return_pending": row.get("return_pending", False),
                                                     "return_request_key": row.get("return_request_key")}})

    @property
    def row(self):
        return list(self.rows.values())[-1]

    def end(self, row, reason):
        if row["state"] != "ended":
            row.update(state="ended", end_reason=reason, offer=None, answer=None)

    async def wait_answer(self, timeout=10):
        async def poll():
            while not (self.rows and self.row["answer"]):
                await asyncio.sleep(0.02)
        await asyncio.wait_for(poll(), timeout)
        return self.row["answer"]


@pytest.fixture
def fast_handoff(monkeypatch):
    monkeypatch.setattr(handoff_module, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(handoff_module, "COMMITTED_POLL_SECONDS", 0.1)
    monkeypatch.setattr(handoff_module, "JOIN_SECONDS", 3)


@pytest.fixture
def handoff_settings(settings):
    return replace(settings, human_handoff=True, max_session_seconds=60)


async def start_call(app, kapso, caller):
    caller.addTrack(Tone(CALLER_HZ))
    job = await inbound_call(app, caller)
    await apply_answer(kapso, caller)
    return job


async def teammate_joins(handoffs, teammate, hz=TEAMMATE_HZ, ready=True):
    """The browser: offer with every candidate, Kapso stores it, then the server's answer applies.
    Once connected and playing, the browser reports ready to Kapso (`ready`)."""
    teammate.addTrack(Tone(hz))
    await teammate.setLocalDescription(await teammate.createOffer())
    handoffs.row.update(state="joining", offer=teammate.localDescription.sdp)
    answer = await handoffs.wait_answer()
    await teammate.setRemoteDescription(RTCSessionDescription(answer, "answer"))
    if ready:
        await browser_ready(handoffs, teammate)


async def browser_ready(handoffs, teammate):
    async def connected():
        while teammate.connectionState != "connected":
            await asyncio.sleep(0.02)
    await asyncio.wait_for(connected(), 10)
    handoffs.row["browser_ready"] = True


def run(go):
    return asyncio.run(go())


def test_teammate_takes_over_the_same_call_and_the_ai_stops_both_ways(handoff_settings, spec, store, fast_handoff):
    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent() as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            caller_ear, teammate_ear = Ear(caller), Ear(teammate)
            manager = app.state.manager
            try:
                job = await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                ref = next(call["ref"] for call in manager.snapshot())
                async with asgi_client(app) as client:
                    response = await client.post(f"/operator/api/calls/{ref}/handoff",
                                                 headers={"authorization": "Bearer " + "t" * 40})
                assert response.json()["status"] == "requested"
                # Until a teammate is ready the AI keeps the caller, both ways.
                await asyncio.sleep(0.3)
                loop = asyncio.get_running_loop()
                assert caller_ear.count(AGENT_HZ, since=loop.time() - 0.3) > 5
                await teammate_joins(handoffs, teammate)
                await wait_for_event(app.state.log, "handoff_committed", timeout=10)
                switched = loop.time()
                session = manager.sessions[CALL_ID]

                await caller_ear.wait_for(TEAMMATE_HZ, since=switched)
                await teammate_ear.wait_for(CALLER_HZ, since=switched)
                await asyncio.sleep(1.0)
                after = switched + 0.5
                result = {
                    "caller_hears_teammate": caller_ear.count(TEAMMATE_HZ, since=after),
                    "caller_hears_agent_after": caller_ear.count(AGENT_HZ, since=after),
                    "teammate_hears_caller": teammate_ear.count(CALLER_HZ, since=after),
                    "teammate_heard_before": teammate_ear.count(CALLER_HZ, until=switched),
                    "agent_chunks_after": sum(1 for at in agent.user_chunks if at > switched + 0.3),
                    "agent_closed": agent.closed.is_set(),
                    "meta_leg": session.connection.pc.connectionState,
                    "actions_while_human": list(kapso.actions),
                }
                # The teammate hangs up in Inbox: Kapso ends the handoff, this server ends the call.
                handoffs.end(handoffs.row, "hung_up")
                await asyncio.wait_for(job, timeout=10)
            finally:
                await manager.shutdown()
                for peer, ear in ((caller, caller_ear), (teammate, teammate_ear)):
                    await peer.close()
                    await ear.close()
        return result, kapso, handoffs, app
    result, kapso, handoffs, app = run(go)
    assert result["caller_hears_teammate"] > 20 and result["teammate_hears_caller"] > 20
    assert result["caller_hears_agent_after"] == 0, "AI audio reached the caller after the switch"
    assert result["agent_chunks_after"] == 0, "caller audio reached the AI after the switch"
    assert result["agent_closed"] and result["teammate_heard_before"] == 0
    assert result["meta_leg"] == "connected"
    assert result["actions_while_human"] == ["pre_accept", "accept"]  # same Meta call, no new accept
    assert kapso.actions == ["pre_accept", "accept", "terminate"]
    assert handoffs.requests[0]["call_id"] == CALL_ID and handoffs.requests[0]["stun_urls"] == []
    events = json.dumps(list(app.state.log.events))
    assert "handoff_media_ready" in events and "v=0" not in events and CALL_ID not in events


def test_silent_media_without_the_browser_ready_report_never_switches(handoff_settings, spec, store, fast_handoff):
    """A muted browser track sends valid silent RTP. Only the browser's ready report (after its
    audio plays) lets the switch happen, and then a silent teammate is enough."""
    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent() as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            caller_ear = Ear(caller)
            try:
                await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                session = app.state.manager.sessions[CALL_ID]
                await session.handoff.request()
                await teammate_joins(handoffs, teammate, hz=0, ready=False)
                await wait_for_event(app.state.log, "handoff_media_ready", timeout=10)
                await asyncio.sleep(0.5)
                loop = asyncio.get_running_loop()
                before = {"state": handoffs.row["state"], "agent_closed": agent.closed.is_set(),
                          "switch": session.handoff.switch.human,
                          "caller_hears_agent": caller_ear.count(AGENT_HZ, since=loop.time() - 0.4)}
                await browser_ready(handoffs, teammate)
                await wait_for_event(app.state.log, "handoff_committed", timeout=10)
                await asyncio.wait_for(agent.closed.wait(), 5)
                after = {"state": handoffs.row["state"], "switch": session.handoff.switch.human}
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await teammate.close()
                await caller_ear.close()
        return before, after
    before, after = run(go)
    assert before == {"state": "joining", "agent_closed": False, "switch": False,
                      "caller_hears_agent": before["caller_hears_agent"]}
    assert before["caller_hears_agent"] > 5
    assert after == {"state": "committed", "switch": True}


def test_a_failed_ai_close_at_the_switch_still_connects_the_teammate(
        handoff_settings, spec, store, fast_handoff, monkeypatch):
    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent() as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            caller_ear = Ear(caller)
            manager = app.state.manager
            try:
                job = await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                session = manager.sessions[CALL_ID]
                close_socket, failures = session.bridge.stop_socket, []

                async def failing_once():
                    if not failures:
                        failures.append(1)
                        raise RuntimeError("provider close failed")
                    await close_socket()
                monkeypatch.setattr(session.bridge, "stop_socket", failing_once)
                await session.handoff.request()
                await teammate_joins(handoffs, teammate)
                await wait_for_event(app.state.log, "handoff_committed", timeout=10)
                switched = asyncio.get_running_loop().time()
                await caller_ear.wait_for(TEAMMATE_HZ, since=switched)
                await asyncio.sleep(0.5)
                agent_after = caller_ear.count(AGENT_HZ, since=switched + 0.3)
                handoffs.end(handoffs.row, "hung_up")
                await asyncio.wait_for(job, timeout=10)
            finally:
                await manager.shutdown()
                await caller.close()
                await teammate.close()
                await caller_ear.close()
        return agent_after, kapso, app
    agent_after, kapso, app = run(go)
    assert agent_after == 0, "AI audio reached the caller after the switch"
    assert any(e["event"] == "handoff_ai_close_failed_runtimeerror" for e in app.state.log.events)
    assert kapso.actions == ["pre_accept", "accept", "terminate"]


def test_an_unexpected_failure_after_the_switch_ends_the_call(handoff_settings, spec, store, fast_handoff,
                                                              monkeypatch):
    async def broken(_self, _client):
        raise RuntimeError("watcher failed")
    monkeypatch.setattr(handoff_module.CallHandoff, "stay_with_teammate", broken)

    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent() as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            caller_ear = Ear(caller)
            try:
                job = await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                await app.state.manager.sessions[CALL_ID].handoff.request()
                await teammate_joins(handoffs, teammate)
                await asyncio.wait_for(job, timeout=10)
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await teammate.close()
                await caller_ear.close()
        return kapso, handoffs
    kapso, handoffs = run(go)
    assert handoffs.ends == ["bridge_failed"]
    assert kapso.actions == ["pre_accept", "accept", "terminate"]


@pytest.mark.parametrize("ending", ["declined", "no_media", "ready_without_media"])
def test_the_ai_keeps_the_caller_when_nobody_takes_over(handoff_settings, spec, store, fast_handoff, ending):
    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent() as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            caller_ear = Ear(caller)
            manager = app.state.manager
            try:
                job = await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                session = manager.sessions[CALL_ID]
                assert (await session.handoff.request())["ok"] is True
                if ending == "declined":
                    handoffs.end(handoffs.row, "declined")
                elif ending == "ready_without_media":
                    # A ready report is not enough: this leg's media must arrive here too.
                    await teammate.setLocalDescription(await teammate.createOffer())
                    handoffs.row.update(state="joining", offer=teammate.localDescription.sdp,
                                        browser_ready=True)
                else:
                    # The browser's offer arrives, but its media never connects.
                    await teammate.setLocalDescription(await teammate.createOffer())
                    handoffs.row.update(state="joining", offer=teammate.localDescription.sdp)
                await wait_for_event(app.state.log, "handoff_fallback", timeout=10)
                loop = asyncio.get_running_loop()
                fell_back = loop.time()
                await caller_ear.wait_for(AGENT_HZ, frames=20, since=fell_back)
                result = {"agent_chunks_after": sum(1 for at in agent.user_chunks if at > fell_back),
                          "agent_closed": agent.closed.is_set(), "job_running": not job.done(),
                          "state": session.handoff.state, "switch": session.handoff.switch.human}
            finally:
                await manager.shutdown()
                await caller.close()
                await teammate.close()
                await caller_ear.close()
        return result, kapso, handoffs
    result, kapso, handoffs = run(go)
    assert result["agent_chunks_after"] > 0 and not result["agent_closed"] and result["job_running"]
    assert result["state"] == "ended" and result["switch"] is False
    assert "terminate" not in kapso.actions[:2] and kapso.actions[:2] == ["pre_accept", "accept"]
    assert handoffs.ends == ([] if ending == "declined" else ["join_timeout"])


def test_teammate_leg_dropping_after_the_switch_ends_the_call(handoff_settings, spec, store, fast_handoff):
    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent() as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            caller_ear = Ear(caller)
            manager = app.state.manager
            try:
                job = await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                await manager.sessions[CALL_ID].handoff.request()
                await teammate_joins(handoffs, teammate)
                await wait_for_event(app.state.log, "handoff_committed", timeout=10)
                await teammate.close()  # the browser tab is gone
                await asyncio.wait_for(job, timeout=15)
            finally:
                await manager.shutdown()
                await caller.close()
                await caller_ear.close()
        return kapso, handoffs, app
    kapso, handoffs, app = run(go)
    assert handoffs.ends == ["human_disconnected"]
    assert kapso.actions == ["pre_accept", "accept", "terminate"]


def test_refused_request_and_caller_hangup_leave_the_ai_path_intact(handoff_settings, spec, store, fast_handoff):
    async def go():
        handoffs = FakeHandoffs(refuse_request=True)
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent() as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(),
                             kapso_factory=kapso.factory())
            caller = RTCPeerConnection()
            caller_ear = Ear(caller)
            try:
                await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                result = await app.state.manager.sessions[CALL_ID].handoff.request()
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await caller_ear.close()
        return result, app
    result, app = run(go)
    assert result["ok"] is False and result["code"] == "handoff_unavailable"
    assert any(e["event"] == "handoff_request_refused" and e.get("code") == "handoff_unavailable"
               for e in app.state.log.events)


def test_handoff_off_keeps_the_pipeline_tools_and_console_unchanged(settings, spec, store):
    async def go():
        kapso = KapsoRecorder()
        async with ToneAgent() as agent:
            app = create_app(settings, spec, store, session_factory=agent.factory(), kapso_factory=kapso.factory())
            caller = RTCPeerConnection()
            caller_ear = Ear(caller)
            try:
                await start_call(app, kapso, caller)
                await caller_ear.wait_for(AGENT_HZ)
                session = app.state.manager.sessions[CALL_ID]
                ref = app.state.manager.snapshot()[0]["ref"]
                async with asgi_client(app) as client:
                    response = await client.post(f"/operator/api/calls/{ref}/handoff",
                                                 headers={"authorization": "Bearer " + "t" * 40})
                    state = (await client.get("/operator/api/state",
                                              headers={"authorization": "Bearer " + "t" * 40})).json()
                result = {"handoff": session.handoff, "processors": len(session.worker.pipeline.processors),
                          "status": response.status_code, "state": state["handoff"], "snapshot": state["calls"][0]}
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await caller_ear.close()
        return result
    result = run(go)
    assert result["handoff"] is None and result["status"] == 409
    assert result["state"] == {"enabled": False, "problem": "off"} and "handoff" not in result["snapshot"]
    tools = [tool["name"] for tool in session_update(spec, {"opening_message": "Hi"})["session"]["tools"]]
    assert "transfer_to_human" not in tools
    with_handoff = session_update(spec, {"opening_message": "Hi"}, handoff=True)["session"]
    assert "transfer_to_human" in [tool["name"] for tool in with_handoff["tools"]]
    assert "transfer_to_human" in with_handoff["instructions"]


def test_handoff_readiness_names_what_is_missing():
    base = Settings(kapso_api_key="k", phone_number_id="1", human_handoff=True)
    assert base.handoff_problem() is None
    assert replace(base, human_handoff=False).handoff_problem() == "off"
    assert replace(base, local_capture=True).handoff_problem() == "local_capture"
    assert replace(base, kapso_api_key="").handoff_problem() == "kapso_missing"
    railway = replace(base, public_domain="voice.up.railway.app",
                      ice_servers_json='[{"urls": "stun:stun.example.test:3478"}]')
    assert railway.handoff_problem() == "turn_required"
    relay = replace(railway, handoff_ice_servers_json='[{"urls": ["stun:stun.example.test:3478", '
                                                      '"turn:turn.example.test:3478"], "username": "u", "credential": "c"}]')
    assert relay.handoff_problem() is None
    assert relay.handoff_stun_urls() == ["stun:stun.example.test:3478"]  # never TURN credentials


def test_grok_agent_asks_for_a_teammate_with_its_tool_and_keeps_talking_when_nobody_joins(
        handoff_settings, spec, store, fast_handoff):
    from handoff_fakes import FakeGrokAgent
    from kapso_voice_agent.grok_bridge import HANDOFF_UNAVAILABLE_LINE

    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with FakeGrokAgent(transfer_after=0.5) as agent:
            settings = replace(handoff_settings, voice_provider="grok", dev_agent_ws_url=agent.url)
            app = create_app(settings, spec, store, kapso_factory=kapso.factory())
            caller = RTCPeerConnection()
            caller_ear = Ear(caller)
            try:
                await start_call(app, kapso, caller)
                await wait_for_event(app.state.log, "handoff_requested", timeout=10)
                handoffs.end(handoffs.row, "expired")
                await wait_for_event(app.state.log, "handoff_fallback", timeout=10)
                for _ in range(100):
                    if agent.notices:
                        break
                    await asyncio.sleep(0.05)
                loop = asyncio.get_running_loop()
                await caller_ear.wait_for(AGENT_HZ, frames=10, since=loop.time())
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await caller_ear.close()
        return agent, handoffs, kapso
    agent, handoffs, kapso = run(go)
    assert "transfer_to_human" in agent.tools
    assert agent.tool_outputs == [{"ok": True, "status": "requested", "message": agent.tool_outputs[0]["message"]}]
    assert handoffs.requests[0]["call_id"] == CALL_ID
    assert agent.notices == [HANDOFF_UNAVAILABLE_LINE]
    assert kapso.actions[:2] == ["pre_accept", "accept"] and "terminate" not in kapso.actions[:2]


def test_turn_failure_keeps_the_ai_and_the_same_call(handoff_settings, spec, store, fast_handoff, monkeypatch):
    from kapso_voice_agent.turn import handoff_ice

    async def failed_credentials(settings):
        return await handoff_ice(settings, httpx.MockTransport(lambda request: httpx.Response(503)))

    monkeypatch.setattr(handoff_module, "handoff_ice", failed_credentials)

    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        settings = replace(handoff_settings, cloudflare_turn_key_id="test-turn-key",
                           cloudflare_turn_api_token="test-turn-token")
        async with ToneAgent() as agent:
            app = create_app(settings, spec, store, session_factory=agent.factory(), kapso_factory=kapso.factory())
            caller = RTCPeerConnection()
            ear = Ear(caller)
            try:
                await start_call(app, kapso, caller)
                await ear.wait_for(AGENT_HZ)
                session = app.state.manager.sessions[CALL_ID]
                assert (await session.handoff.request())["ok"]
                handoffs.row.update(state="joining", offer="unused because TURN fails first")
                await wait_for_event(app.state.log, "handoff_fallback", timeout=10)
                await ear.wait_for(AGENT_HZ, since=asyncio.get_running_loop().time())
                assert not agent.closed.is_set()
                assert not session.handoff.switch.human
                assert kapso.actions == ["pre_accept", "accept"]
                assert handoffs.row["end_reason"] == "bridge_failed"
                assert "test-turn-token" not in json.dumps(list(app.state.log.events))
                async with asgi_client(app) as client:
                    state = await client.get("/operator/api/state", headers={"Authorization": "Bearer " + settings.operator_token})
                    assert "test-turn-token" not in state.text
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await ear.close()
    run(go)


@pytest.mark.parametrize("provider", ["elevenlabs", "grok"])
def test_return_reconnects_agent_without_replacing_the_call_and_can_handoff_again(
        handoff_settings, spec, store, fast_handoff, provider):
    from handoff_fakes import FakeGrokAgent

    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        agent = FakeGrokAgent() if provider == "grok" else ToneAgent()
        async with agent:
            settings = replace(handoff_settings, voice_provider=provider)
            kwargs = {"session_factory": agent.factory()} if provider != "grok" else {}
            if provider == "grok":
                settings = replace(settings, dev_agent_ws_url=agent.url)
            app = create_app(settings, spec, store, kapso_factory=kapso.factory(), **kwargs)
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            ear, human_ear = Ear(caller), Ear(teammate)
            try:
                job = await start_call(app, kapso, caller)
                await ear.wait_for(AGENT_HZ)
                session = app.state.manager.sessions[CALL_ID]
                assert (await session.handoff.request())["ok"]
                assert handoffs.requests[0]["return_to_agent_supported"] is True
                await teammate_joins(handoffs, teammate)
                await wait_for_event(app.state.log, "handoff_committed", timeout=10)
                await ear.wait_for(TEAMMATE_HZ)
                await human_ear.wait_for(CALLER_HZ)
                before = asyncio.get_running_loop().time()
                # Readiness is deliberately delayed. Human audio continues throughout preparation.
                prepare = session.bridge.prepare_return
                preparing, release = asyncio.Event(), asyncio.Event()

                async def delayed():
                    preparing.set()
                    await release.wait()
                    await prepare()
                session.bridge.prepare_return = delayed
                handoffs.row.update(return_request_key="return-attempt-0001", return_pending=True)
                await asyncio.wait_for(preparing.wait(), 3)
                await ear.wait_for(TEAMMATE_HZ, since=before)
                assert session.handoff.switch.human
                assert not job.done()
                handoffs.lose_return_ack = True
                release.set()
                await wait_for_event(app.state.log, "handoff_returned_to_agent", timeout=10)
                returned = asyncio.get_running_loop().time()
                await ear.wait_for(AGENT_HZ, since=returned)
                await asyncio.sleep(0.3)
                assert ear.count(TEAMMATE_HZ, since=returned + 0.2) == 0
                assert not session.handoff.switch.human
                assert session.handoff.state == "idle"
                assert app.state.manager.sessions[CALL_ID] is session
                assert not job.done()
                assert kapso.actions == ["pre_accept", "accept"]
                # A second request creates a fresh attempt on this same still-live call.
                assert (await session.handoff.request())["status"] == "requested"
                assert len(handoffs.requests) == 2
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await teammate.close()
                await ear.close()
                await human_ear.close()
    run(go)


def test_failed_agent_return_leaves_human_audio_connected(handoff_settings, spec, store, fast_handoff):
    async def go():
        handoffs = FakeHandoffs()
        kapso = KapsoRecorder(handoffs)
        async with ToneAgent(fail_after=1) as agent:
            app = create_app(handoff_settings, spec, store, session_factory=agent.factory(), kapso_factory=kapso.factory())
            caller, teammate = RTCPeerConnection(), RTCPeerConnection()
            ear = Ear(caller)
            try:
                job = await start_call(app, kapso, caller)
                await ear.wait_for(AGENT_HZ)
                session = app.state.manager.sessions[CALL_ID]
                await session.handoff.request()
                await teammate_joins(handoffs, teammate)
                await wait_for_event(app.state.log, "handoff_committed", timeout=10)

                handoffs.row.update(return_request_key="return-failed-0001", return_pending=True)
                await wait_for_event(app.state.log, "handoff_return_failed_runtimeerror", timeout=5)
                await ear.wait_for(TEAMMATE_HZ, since=asyncio.get_running_loop().time())
                assert session.handoff.switch.human
                assert not handoffs.row["return_pending"]
                assert not job.done()
                assert kapso.actions == ["pre_accept", "accept"]
            finally:
                await app.state.manager.shutdown()
                await caller.close()
                await teammate.close()
                await ear.close()
    run(go)


def test_returned_agent_gets_a_fresh_segment_deadline(handoff_settings):
    from types import SimpleNamespace

    from kapso_voice_agent.calls import CallManager

    async def go():
        loop = asyncio.get_running_loop()
        handoff = SimpleNamespace(committed_at=None, agent_returned_at=None)
        task = asyncio.create_task(asyncio.sleep(0.2))
        session = SimpleNamespace(task=task, handoff=handoff)
        manager = SimpleNamespace(settings=replace(handoff_settings, max_session_seconds=0.15, handoff_max_seconds=0.1))

        async def switches():
            await asyncio.sleep(0.01)
            handoff.committed_at = loop.time()
            await asyncio.sleep(0.07)
            handoff.committed_at = None
            handoff.agent_returned_at = loop.time()
        switching = asyncio.create_task(switches())
        try:
            await CallManager.wait_for_session(manager, session)
            assert task.done() and not task.cancelled()
        finally:
            switching.cancel()
            task.cancel()
            await asyncio.gather(switching, task, return_exceptions=True)
    run(go)
