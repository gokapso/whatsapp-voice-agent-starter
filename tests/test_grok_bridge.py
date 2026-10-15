"""Grok Voice (VOICE_PROVIDER=grok) offline: xAI is a local WebSocket speaking the realtime events
in xAI's Speech to Speech docs. Nothing here reaches xAI. These prove the protocol handling, audio
framing, tool round trip and call lifecycle; they say nothing about how Grok actually talks."""

import asyncio
import base64
from dataclasses import replace
import http
import json

from aiortc import RTCPeerConnection, RTCSessionDescription
from pipecat.frames.frames import EndWorkerFrame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from conftest import TOKEN, KapsoRecorder, ToneTrack, asgi_client, listen
from kapso_voice_agent import grok_bridge
from kapso_voice_agent.agent_config import load_spec
from kapso_voice_agent.app import create_app
from kapso_voice_agent.bridge import AgentSession, session_context
from kapso_voice_agent.config import REPO_ROOT, ConfigError, load_settings
from kapso_voice_agent.events import CALL_PROBLEM, EventLog
from kapso_voice_agent.grok_bridge import GrokAgentBridge, GrokRefused, grok_endpoint, grok_tools, session_update
from kapso_voice_agent.recording import RecordingCheck
from kapso_voice_agent.tools import TOOLS

AUTH = {"authorization": f"Bearer {TOKEN}"}
SECOND = b"\x10\x00" * 16000  # one second of 16 kHz PCM16


@pytest.fixture
def grok(settings):
    return replace(settings, voice_provider="grok", xai_api_key="xai-test-key-not-real",
                   elevenlabs_api_key="", elevenlabs_agent_id="")


def b64(audio):
    return base64.b64encode(audio).decode()


class FakeSocket:
    def __init__(self):
        self.sent, self.closed = [], False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def offline_bridge(grok, spec, store, recorded=True):
    """A bridge wired to a recording socket, with frames captured instead of sent down a pipeline."""
    recording = type("R", (), {"recorded": recorded})()
    context = session_context(grok, spec, store, "inbound", recording)
    clock = Clock()
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds
    bridge = GrokAgentBridge(context, store, "caller-key", EventLog(), "browser-0123456789abcdef", clock=clock, sleep=sleep)
    bridge.sleeps = sleeps
    bridge.websocket = FakeSocket()
    bridge.pushed, bridge.interruptions = [], []

    async def push(frame, direction=None):
        bridge.pushed.append(frame)

    async def interrupt():
        bridge.interruptions.append(clock())
    bridge.push_frame, bridge.broadcast_interruption = push, interrupt
    return bridge, clock


class TestSessionConfig:
    def test_session_update_sends_16k_pcm_server_vad_voice_prompt_and_tools(self, grok, spec, store):
        context = session_context(grok, spec, store, "inbound", type("R", (), {"recorded": True})())
        update = session_update(spec, context["dynamic_variables"])
        session = update["session"]
        assert update["type"] == "session.update"
        assert session["audio"]["input"]["format"] == session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 16000}
        assert session["turn_detection"] == {"type": "server_vad"} and session["voice"] == "eve"
        assert session["audio"]["input"]["transcription"] == {"language_hint": "en", "keyterms": ["Riley", "appointment", "reschedule"]}
        # The provider gets finished instructions: this call's date, recording status and business.
        assert "{{" not in session["instructions"] and "Monday, 2026-11-02" in session["instructions"]
        assert spec.recording_status(True) in session["instructions"]

    def test_tools_are_the_same_six_plus_end_call_with_the_same_schemas(self):
        tools = {tool["name"]: tool for tool in grok_tools()}
        assert set(tools) == set(TOOLS) | {"end_call"}
        assert all(tool["type"] == "function" for tool in tools.values())
        book = tools["book_appointment"]["parameters"]
        assert book["properties"]["confirmed"]["type"] == "boolean" and "confirmed" in book["required"]
        assert not any("caller" in name or "phone" in name for tool in tools.values() for name in tool["parameters"]["properties"])

    def test_endpoint_pins_the_model_and_keeps_the_key_in_a_header(self, grok, spec):
        url, headers = asyncio.run(grok_endpoint(grok, spec))
        assert url == "wss://api.x.ai/v1/realtime?model=grok-voice-3.0"
        assert headers == {"Authorization": "Bearer xai-test-key-not-real"} and "xai-test" not in url

    def test_settings_and_agent_toml_validation(self, tmp_path):
        with pytest.raises(ConfigError, match="VOICE_PROVIDER"):
            load_settings(environ={"VOICE_PROVIDER": "openai"})
        settings = load_settings(environ={"VOICE_PROVIDER": "grok", "CALLER_KEY_SECRET": "c" * 64, "CALENDAR": "local"})
        assert settings.agent_missing() == ["XAI_API_KEY (or DEV_AGENT_WS_URL)"]
        assert replace(settings, xai_api_key="k").agent_ready
        agent = tmp_path / "agent"
        agent.mkdir()
        for name in ("prompt.md", "business.json", "dev-calendar.json"):
            (agent / name).symlink_to(REPO_ROOT / "agent" / name)
        toml = (REPO_ROOT / "agent/agent.toml").read_text()
        for bad, message in ((toml.replace('model = "grok-voice-3.0"', 'model = "gpt-4o"'), "grok.model"),
                             (toml.replace('voice = "eve"', 'voice = "eve; drop"'), "grok.voice"),
                             (toml.replace('voice = "eve"', 'voice = "eve"\nspeed = 2'), "unknown keys")):
            (agent / "agent.toml").write_text(bad)
            with pytest.raises(ValueError, match=message):
                load_spec(agent / "agent.toml")

    def test_recording_notice_is_always_on_and_never_reads_elevenlabs(self, grok, spec):
        async def never(settings):
            raise AssertionError("Grok calls must not read an ElevenLabs agent")
        check = RecordingCheck(grok, spec, EventLog(), reader=never)
        recording = asyncio.run(check.current())
        assert recording.recorded and recording.source == "xai_default_retention"
        assert check.status()["provider_recording"] == "on" and check.status()["next_greeting_says_recorded"] is True


class TestProtocol:
    def test_greeting_is_a_non_interruptible_force_message_once_session_and_media_are_ready(self, grok, spec, store):
        async def go():
            bridge, _ = offline_bridge(grok, spec, store)
            await bridge.handle_event({"type": "conversation.created", "conversation": {"id": "conv_1"}})
            assert bridge.websocket.sent[-1]["type"] == "session.update" and not bridge.ready.is_set()
            await bridge.handle_event({"type": "session.updated", "session": {}})
            # The caller's media is not connected yet: the transport would drop the disclosure.
            assert [m["type"] for m in bridge.websocket.sent] == ["session.update"]
            await bridge.media_ready()
            await bridge.handle_event({"type": "session.updated", "session": {}})  # an echo later on never re-greets
            await bridge.media_ready()
            return bridge
        bridge = asyncio.run(go())
        greetings = [m for m in bridge.websocket.sent if m["type"] == "conversation.item.create"]
        assert len(greetings) == 1
        item = greetings[0]["item"]
        assert item["type"] == "force_message" and item["interruptible"] is False
        assert item["content"] == [{"type": "output_text", "text": spec.greeting("inbound", True)}]
        assert "response.create" not in [m["type"] for m in bridge.websocket.sent]  # xAI speaks force_message itself
        assert bridge.conversation_id == "conv_1" and bridge.log.provider_problem is None

    def test_barge_in_drops_queued_agent_audio_but_never_cuts_the_disclosure(self, grok, spec, store):
        async def go():
            bridge, clock = offline_bridge(grok, spec, store)
            await bridge.media_ready()
            await bridge.handle_event({"type": "session.updated"})
            await bridge.handle_event({"type": "response.created", "response": {"id": "greeting"}})
            await bridge.handle_event({"type": "response.output_audio.delta", "response_id": "greeting", "delta": b64(SECOND)})
            clock.now += 0.8
            await bridge.handle_event({"type": "input_audio_buffer.speech_started"})  # caller talks over the disclosure
            assert bridge.interruptions == []
            await bridge.handle_event({"type": "response.done", "response": {"id": "greeting"}})
            clock.now += 1.0
            await bridge.handle_event({"type": "response.created", "response": {"id": "answer"}})
            await bridge.handle_event({"type": "response.output_audio.delta", "response_id": "answer", "delta": b64(SECOND)})
            await bridge.handle_event({"type": "input_audio_buffer.speech_started"})
            await bridge.handle_event({"type": "response.output_audio.delta", "response_id": "answer", "delta": b64(SECOND)})
            await bridge.handle_event({"type": "response.created", "response": {"id": "next"}})
            await bridge.handle_event({"type": "response.audio.delta", "response_id": "next", "delta": b64(SECOND)})
            return bridge
        bridge = asyncio.run(go())
        assert len(bridge.interruptions) == 1
        assert len(bridge.pushed) == 3  # greeting, the answer before the barge-in, the next response; not the stale chunk
        assert all(frame.sample_rate == 16000 and frame.num_channels == 1 for frame in bridge.pushed)
        assert [e["event"] for e in bridge.log.events].count("agent_interrupted") == 1

    def test_disclosure_stays_protected_through_a_pause_in_its_audio(self, grok, spec, store):
        async def go():
            bridge, clock = offline_bridge(grok, spec, store)
            await bridge.handle_event({"type": "session.updated"})
            await bridge.handle_event({"type": "input_audio_buffer.speech_started"})  # before media: protected
            await bridge.media_ready()
            await bridge.handle_event({"type": "response.created", "response": {"id": "greeting"}})
            await bridge.handle_event({"type": "response.output_audio.delta", "response_id": "greeting",
                                       "delta": b64(SECOND[:3200])})
            clock.now += 3  # the provider pauses longer than the audio sent so far
            await bridge.handle_event({"type": "input_audio_buffer.speech_started"})
            await bridge.handle_event({"type": "response.output_audio.delta", "response_id": "greeting",
                                       "delta": b64(SECOND[:3200])})
            return bridge
        bridge = asyncio.run(go())
        assert bridge.interruptions == [] and len(bridge.pushed) == 2 and "greeting" not in bridge.dropped

    def test_tool_results_go_back_once_then_one_response_create(self, grok, spec, store):
        slots = {"type": "response.function_call_arguments.done", "call_id": "call_1", "name": "available_slots",
                 "arguments": "{}"}
        info = {"type": "response.function_call_arguments.done", "call_id": "call_2", "name": "business_info",
                "arguments": '{"topic": "hours"}'}

        async def go():
            bridge, _ = offline_bridge(grok, spec, store)
            await bridge.handle_event({"type": "response.created", "response": {"id": "r1"}})
            await bridge.handle_event(slots)
            await bridge.handle_event(info)  # parallel call in the same response
            sent_before_done = [m["type"] for m in bridge.websocket.sent]
            await bridge.handle_event({"type": "response.done", "response": {"id": "r1"}})
            await bridge.handle_event({"type": "response.done", "response": {"id": "r2"}})  # no new outputs: no new response
            return bridge, sent_before_done
        bridge, sent_before_done = asyncio.run(go())
        assert "response.create" not in sent_before_done
        outputs = [m["item"] for m in bridge.websocket.sent if m["type"] == "conversation.item.create"]
        assert [o["call_id"] for o in outputs] == ["call_1", "call_2"]
        assert all(o["type"] == "function_call_output" for o in outputs)
        assert json.loads(outputs[0]["output"])["ok"] is True and json.loads(outputs[0]["output"])["slots"]
        assert [m["type"] for m in bridge.websocket.sent].count("response.create") == 1
        assert {"tool_available_slots_ok", "tool_business_info_ok"} <= {e["event"] for e in bridge.log.events}

    def test_confirmation_stays_strict_and_retries_never_book_twice(self, grok, spec, store):
        async def go():
            bridge, _ = offline_bridge(grok, spec, store)
            await bridge.handle_event({"type": "response.function_call_arguments.done", "call_id": "c0",
                                       "name": "available_slots", "arguments": '{"day": "2026-11-03"}'})
            slot = json.loads(bridge.websocket.sent[-1]["item"]["output"])["slots"][0]["slot"]
            args = {"slot": slot, "name": "Sam Lee", "service_id": "consultation"}
            for call_id, confirmed in (("c1", "true"), ("c2", True), ("c2", True)):
                await bridge.handle_event({"type": "response.function_call_arguments.done", "call_id": call_id,
                                           "name": "book_appointment", "arguments": json.dumps({**args, "confirmed": confirmed})})
            await bridge.handle_event({"type": "response.function_call_arguments.done", "call_id": "c3",
                                       "name": "book_appointment", "arguments": "not json"})
            return [json.loads(m["item"]["output"]) for m in bridge.websocket.sent if m["type"] == "conversation.item.create"]
        _, string_yes, booked, retried, garbage = asyncio.run(go())
        assert string_yes["code"] == "invalid_arguments"  # "true" is not a clear yes
        assert booked["ok"] is True and retried == booked
        assert garbage["code"] == "invalid_arguments"
        assert len(store.list_own("caller-key")["appointments"]) == 1

    def test_end_call_closes_after_the_goodbye_has_played(self, grok, spec, store):
        async def go():
            bridge, _ = offline_bridge(grok, spec, store)
            await bridge.handle_event({"type": "response.created", "response": {"id": "r9"}})
            await bridge.handle_event({"type": "response.output_audio.delta", "response_id": "r9", "delta": b64(SECOND * 2)})
            await bridge.handle_event({"type": "response.function_call_arguments.done", "call_id": "bye",
                                       "name": "end_call", "arguments": "{}"})
            assert not bridge.websocket.closed
            await bridge.handle_event({"type": "response.done", "response": {"id": "r9"}})
            return bridge
        bridge = asyncio.run(go())
        assert bridge.websocket.closed and "response.create" not in [m["type"] for m in bridge.websocket.sent]
        assert bridge.sleeps == [pytest.approx(2.3)]  # two seconds of goodbye audio, plus a margin, before closing

    @pytest.mark.parametrize("error,problem", [
        ({"code": "rate_limit_exceeded", "message": "Too many requests"}, "rate_limited"),
        ({"code": "invalid_api_key", "message": "Incorrect API key"}, "auth"),
    ])
    def test_auth_and_rate_limit_errors_end_the_session(self, grok, spec, store, error, problem):
        async def go():
            bridge, _ = offline_bridge(grok, spec, store)
            await bridge.handle_event({"type": "session.updated"})
            with pytest.raises(GrokRefused) as refused:
                await bridge.handle_event({"type": "error", "error": error})
            return refused.value.problem
        assert asyncio.run(go()) == problem

    def test_benign_and_recoverable_errors_keep_the_session(self, grok, spec, store):
        async def go():
            bridge, _ = offline_bridge(grok, spec, store)
            await bridge.handle_event({"type": "session.updated"})
            await bridge.handle_event({"type": "error", "error": {"message": "Cancellation failed: no active response found"}})
            await bridge.handle_event({"type": "error", "error": {"code": "invalid_value", "message": "bad <secret> param"}})
            return bridge
        bridge = asyncio.run(go())
        events = [e["event"] for e in bridge.log.events]
        assert events.count("agent_error_invalid_value") == 1 and len([e for e in events if e.startswith("agent_error")]) == 1
        assert "secret" not in json.dumps(list(bridge.log.events))


class FakeXai:
    """Local stand-in for wss://api.x.ai/v1/realtime. `status` refuses the upgrade like xAI would."""

    def __init__(self, status=None):
        self.status, self.updates, self.items, self.headers, self.server = status, [], [], [], None

    async def __aenter__(self):
        self.server = await serve(self.handle, "127.0.0.1", 0, process_request=self.check).__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.server.__aexit__(*exc)

    def check(self, connection, request):
        self.headers.append(dict(request.headers))
        if self.status:
            return connection.respond(self.status, "refused\n")
        return None

    async def endpoint(self, settings, spec):
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}", {"Authorization": "Bearer test"}

    async def handle(self, socket):
        send = lambda message: socket.send(json.dumps(message))  # noqa: E731
        await send({"type": "conversation.created", "conversation": {"id": "conv_test"}})
        self.updates.append(json.loads(await socket.recv()))
        await send({"type": "session.updated", "session": self.updates[-1]["session"]})
        appended, stage = 0, "greeting"
        async for raw in socket:
            message = json.loads(raw)
            if message["type"] == "conversation.item.create":
                self.items.append(message["item"])
                if stage == "greeting":
                    stage = "tool"
                    tone = (b"\x00\x20\x00\xe0" * 4000)
                    await send({"type": "response.created", "response": {"id": "r1"}})
                    await send({"type": "response.output_audio.delta", "response_id": "r1", "delta": b64(tone)})
                    await send({"type": "response.done", "response": {"id": "r1"}})
                    await send({"type": "response.created", "response": {"id": "r2"}})
                    await send({"type": "response.function_call_arguments.done", "call_id": "call_a",
                                "name": "available_slots", "arguments": "{}"})
                    await send({"type": "response.done", "response": {"id": "r2"}})
            elif message["type"] == "response.create" and stage == "tool":
                stage = "talk"
                await send({"type": "response.created", "response": {"id": "r3"}})
            elif message["type"] == "input_audio_buffer.append":
                appended += 1
                assert len(base64.b64decode(message["audio"])) == 3200
                if stage == "talk":
                    await send({"type": "response.output_audio.delta", "response_id": "r3", "delta": message["audio"]})
                if appended == 20:
                    await send({"type": "response.function_call_arguments.done", "call_id": "call_bye",
                                "name": "end_call", "arguments": "{}"})
                    await send({"type": "response.done", "response": {"id": "r3"}})


def grok_factory(xai, sessions):
    def make(*args, **kwargs):
        session = AgentSession(*args, url_factory=xai.endpoint, **kwargs)
        sessions.append(session)
        return session
    return make


async def browser_call(app, heard):
    peer, consumers = RTCPeerConnection(), []
    peer.addTrack(ToneTrack())
    listen(peer, heard, consumers)
    await peer.setLocalDescription(await peer.createOffer())
    async with asgi_client(app) as client:
        response = await client.post("/operator/api/browser-call", json={"sdp": peer.localDescription.sdp}, headers=AUTH)
    assert response.status_code == 200, response.text
    await peer.setRemoteDescription(RTCSessionDescription(response.json()["sdp"], "answer"))
    return peer, consumers


async def close_peer(peer, consumers):
    await peer.close()
    for consumer in consumers:
        consumer.cancel()
    await asyncio.gather(*consumers, return_exceptions=True)


def test_browser_call_runs_greeting_tool_audio_and_end_call_over_real_webrtc(grok, spec, store):
    async def go():
        heard, sessions = asyncio.Event(), []
        async with FakeXai() as xai:
            app = create_app(grok, spec, store, session_factory=grok_factory(xai, sessions),
                             kapso_factory=KapsoRecorder().factory())
            peer, consumers = await browser_call(app, heard)
            try:
                await asyncio.wait_for(sessions[0].task, timeout=20)
                await asyncio.wait_for(heard.wait(), timeout=5)
                async with asgi_client(app) as client:
                    state = (await client.get("/operator/api/state", headers=AUTH)).json()
            finally:
                await app.state.manager.shutdown()
                await close_peer(peer, consumers)
        return xai, sessions[0], state, app
    xai, session, state, app = asyncio.run(go())
    bridge = session.bridge
    assert isinstance(bridge, GrokAgentBridge) and bridge.audio_in > 0 and bridge.audio_out > 0
    assert xai.updates[0]["session"]["voice"] == "eve"
    assert xai.items[0]["type"] == "force_message" and xai.items[0]["content"][0]["text"] == spec.greeting("inbound", True)
    output = next(item for item in xai.items if item["type"] == "function_call_output")
    assert output["call_id"] == "call_a" and json.loads(output["output"])["ok"] is True
    events = [e["event"] for e in app.state.log.events]
    for name in ("agent_socket_connected", "agent_session_ready", "first_audio_in", "first_audio_out",
                 "tool_available_slots_ok", "tool_end_call_ok", "agent_conversation_ended"):
        assert name in events
    assert state["voice"] == {"provider": "grok", "label": "Grok Voice", "model": "grok-voice-3.0", "voice": "eve",
                              "offline_fake_agent": False, "setup_step": None, "problem": None}
    assert "xai-test-key-not-real" not in json.dumps(state)


@pytest.mark.parametrize("status,problem", [(http.HTTPStatus.UNAUTHORIZED, "auth"),
                                            (http.HTTPStatus.PAYMENT_REQUIRED, "billing"),
                                            (http.HTTPStatus.TOO_MANY_REQUESTS, "rate_limited"),
                                            (http.HTTPStatus.NOT_FOUND, "not_found"),
                                            (http.HTTPStatus.BAD_GATEWAY, "unreachable")])
def test_refused_session_ends_the_call_and_tells_the_operator_why(grok, spec, store, status, problem):
    async def go():
        heard, sessions = asyncio.Event(), []
        async with FakeXai(status=status) as xai:
            app = create_app(grok, spec, store, session_factory=grok_factory(xai, sessions),
                             kapso_factory=KapsoRecorder().factory())
            peer, consumers = await browser_call(app, heard)
            try:
                await asyncio.wait_for(sessions[0].task, timeout=20)
                async with asgi_client(app) as client:
                    state = (await client.get("/operator/api/state", headers=AUTH)).json()
            finally:
                await app.state.manager.shutdown()
                await close_peer(peer, consumers)
        return state, sessions[0], xai
    state, session, xai = asyncio.run(go())
    assert state["voice"]["problem"]["problem"] == problem
    assert session.bridge.audio_out == 0 and f"provider_problem_{problem}" in [e["event"] for e in state["events"]]
    assert xai.headers[0]["authorization"] == "Bearer test"


def test_fake_grok_agent_speaks_the_protocol_the_bridge_uses(grok, spec, store):
    """`voice-agent dev fake-agent --provider grok`: the offline browser check for this provider."""
    from kapso_voice_agent.fake_agent import grok_conversation

    async def go():
        heard, sessions = asyncio.Event(), []
        async with serve(grok_conversation, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            dev = replace(grok, xai_api_key="", dev_agent_ws_url=f"ws://127.0.0.1:{port}")
            app = create_app(dev, spec, store, kapso_factory=KapsoRecorder().factory(),
                             session_factory=lambda *a, **k: sessions.append(AgentSession(*a, **k)) or sessions[-1])
            peer, consumers = await browser_call(app, heard)
            try:
                await asyncio.wait_for(heard.wait(), timeout=10)
                async def tool_ran():
                    while "tool_available_slots_ok" not in [e["event"] for e in app.state.log.events]:
                        await asyncio.sleep(0.02)
                await asyncio.wait_for(tool_ran(), timeout=5)
                async with asgi_client(app) as client:
                    state = (await client.get("/operator/api/state", headers=AUTH)).json()
            finally:
                await app.state.manager.shutdown()
                await close_peer(peer, consumers)
        return state
    state = asyncio.run(go())
    assert state["voice"]["offline_fake_agent"] is True and state["recording"]["provider_recording"] == "off (offline fake agent)"


def test_grok_module_never_imports_without_the_provider(settings):
    from kapso_voice_agent.bridge import ElevenAgentBridge, agent_bridge
    assert agent_bridge(settings) is ElevenAgentBridge
    assert agent_bridge(replace(settings, voice_provider="grok")) is grok_bridge.GrokAgentBridge


def test_allowed_hosts_with_a_port_are_refused_because_they_never_match():
    with pytest.raises(ConfigError, match="without ports"):
        load_settings(environ={"ALLOWED_HOSTS": "voice.example.test,127.0.0.1:8080"})
    assert load_settings(environ={"ALLOWED_HOSTS": "voice.example.test,127.0.0.1,[::1]"}).allowed_hosts == (
        "voice.example.test", "127.0.0.1", "[::1]")


@pytest.mark.parametrize("error,logged", [(ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True), False),
                                          (ConnectionClosedError(None, None), True)])
def test_caller_audio_after_a_clean_agent_close_is_not_a_failure(grok, spec, store, error, logged):
    # end_call closes the agent socket while caller audio is still arriving. That last send is
    # expected to fail cleanly; only an abnormal close is a problem for the operator console.
    async def go():
        bridge, _ = offline_bridge(grok, spec, store)
        bridge.ready.set()

        async def closed(raw):
            raise error
        bridge.websocket.send = closed
        bridge.input_buffer.extend(b"\0" * 3200)
        await GrokAgentBridge.process_frame(bridge, InputAudioRawFrame(b"\0" * 320, 16000, 1), FrameDirection.DOWNSTREAM)
        return bridge
    bridge = asyncio.run(go())
    assert any(CALL_PROBLEM.match(e["event"]) for e in bridge.log.events) is logged
    assert isinstance(bridge.pushed[-1], EndWorkerFrame)
