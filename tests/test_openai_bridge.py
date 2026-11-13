"""OpenAI Realtime (VOICE_PROVIDER=openai) offline: a recording socket stands in for OpenAI. Nothing
here reaches OpenAI. These prove the GA session shape, 16 <-> 24 kHz audio, the greeting request
and tools read from response.done; they say nothing about how the model actually talks."""

import asyncio
import base64
from dataclasses import replace
import json

import pytest

from kapso_voice_agent.bridge import agent_bridge, session_context
from kapso_voice_agent.config import load_settings
from kapso_voice_agent.events import EventLog
from kapso_voice_agent.openai_bridge import OpenAIAgentBridge, openai_endpoint, session_update
from kapso_voice_agent.recording import RecordingCheck

SECOND_16K = b"\x10\x00" * 16000


@pytest.fixture
def openai(settings):
    return replace(settings, voice_provider="openai", openai_api_key="sk-test-key-not-real",
                   elevenlabs_api_key="", elevenlabs_agent_id="")


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        pass


def offline_bridge(settings, spec, store):
    recording = type("R", (), {"recorded": True})()
    bridge = OpenAIAgentBridge(session_context(settings, spec, store, "outbound", recording), store, "caller-key",
                               EventLog(), "outbound-0123456789abcdef")
    bridge.websocket = FakeSocket()
    bridge.pushed = []

    async def push(frame, direction=None):
        bridge.pushed.append(frame)
    bridge.push_frame = push
    return bridge


def test_settings_select_the_openai_bridge_and_need_its_key(openai):
    assert agent_bridge(openai) is OpenAIAgentBridge
    settings = load_settings(environ={"VOICE_PROVIDER": "openai", "CALLER_KEY_SECRET": "c" * 64, "CALENDAR": "local"})
    assert settings.agent_missing() == ["OPENAI_API_KEY (or DEV_AGENT_WS_URL)"]
    assert replace(settings, openai_api_key="k").agent_ready


def test_endpoint_pins_the_model_and_keeps_the_key_in_a_header(openai, spec):
    url, headers = asyncio.run(openai_endpoint(openai, spec))
    assert url == f"wss://api.openai.com/v1/realtime?model={spec.openai['model']}"
    assert headers == {"Authorization": "Bearer sk-test-key-not-real"} and "sk-test" not in url


def test_session_update_is_ga_shaped_24k_with_voice_tools_and_finished_instructions(openai, spec, store):
    context = session_context(openai, spec, store, "outbound", type("R", (), {"recorded": True})())
    session = session_update(spec, context["dynamic_variables"])["session"]
    pcm = {"type": "audio/pcm", "rate": 24000}
    assert session["type"] == "realtime" and session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["format"] == session["audio"]["output"]["format"] == pcm
    assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert session["audio"]["output"]["voice"] == spec.openai["voice"]
    assert session["audio"]["input"]["transcription"]["language"] == "en"
    assert "{{" not in session["instructions"] and "end_call" in {tool["name"] for tool in session["tools"]}


def test_handshake_greets_once_with_a_response_request_after_media(openai, spec, store):
    async def go():
        bridge = offline_bridge(openai, spec, store)
        await bridge.handle_event({"type": "session.created", "session": {"id": "sess_1"}})
        assert [m["type"] for m in bridge.websocket.sent] == ["session.update"]
        await bridge.handle_event({"type": "session.updated", "session": {}})
        assert [m["type"] for m in bridge.websocket.sent] == ["session.update"]  # media not connected yet
        await bridge.media_ready()
        await bridge.media_ready()
        return bridge
    bridge = asyncio.run(go())
    greetings = [m for m in bridge.websocket.sent if m["type"] == "response.create"]
    assert len(greetings) == 1 and spec.greeting("outbound", True) in greetings[0]["response"]["instructions"]
    assert bridge.conversation_id == "sess_1"


def test_audio_is_resampled_both_ways(openai, spec, store):
    async def go():
        bridge = offline_bridge(openai, spec, store)
        bridge.ready.set()
        for _ in range(5):
            await bridge.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(SECOND_16K).decode()})
        delta = base64.b64encode(b"\x10\x00" * 24000).decode()
        for _ in range(5):
            await bridge.handle_event({"type": "response.output_audio.delta", "response_id": "r1", "delta": delta})
        return bridge
    bridge = asyncio.run(go())
    sent = sum(len(base64.b64decode(m["audio"])) for m in bridge.websocket.sent)
    received = sum(len(frame.audio) for frame in bridge.pushed)
    # Five seconds each way, give or take the stream resampler's startup window.
    assert 4 * 48000 < sent <= 5 * 48000
    assert 4 * 32000 < received <= 5 * 32000
    assert all(frame.sample_rate == 16000 for frame in bridge.pushed)


def test_tools_run_from_response_done_once_then_one_response_create(openai, spec, store):
    call = {"type": "function_call", "call_id": "call_1", "name": "business_info", "arguments": '{"topic": "hours"}'}

    async def go():
        bridge = offline_bridge(openai, spec, store)
        await bridge.handle_event({"type": "response.created", "response": {"id": "r1"}})
        await bridge.handle_event({"type": "response.function_call_arguments.done", **call})  # ignored
        await bridge.handle_event({"type": "response.done", "response": {"id": "r1", "output": [call]}})
        return bridge
    bridge = asyncio.run(go())
    types = [m["type"] for m in bridge.websocket.sent]
    assert types == ["conversation.item.create", "response.create"]
    assert bridge.websocket.sent[0]["item"]["call_id"] == "call_1"


def test_end_call_from_response_done_closes_after_the_goodbye(openai, spec, store):
    async def go():
        bridge = offline_bridge(openai, spec, store)
        closed = []

        async def close():
            closed.append(True)
        bridge.websocket.close = close
        bye = {"type": "function_call", "call_id": "bye", "name": "end_call", "arguments": "{}"}
        await bridge.handle_event({"type": "response.done", "response": {"id": "r9", "output": [bye]}})
        return bridge, closed
    bridge, closed = asyncio.run(go())
    assert bridge.ending and closed == [True]


def test_recording_notice_is_on_without_reading_elevenlabs(openai, spec):
    async def never(settings):
        raise AssertionError("OpenAI calls must not read an ElevenLabs agent")
    recording = asyncio.run(RecordingCheck(openai, spec, EventLog(), reader=never).current())
    assert recording.recorded and recording.source == "openai_default_retention"
