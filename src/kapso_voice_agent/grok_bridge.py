"""Grok Voice bridge: xAI speech to speech runs the conversation (VOICE_PROVIDER=grok).

Same place in the pipeline as bridge.ElevenAgentBridge:
transport.input() -> GrokAgentBridge -> transport.output() [-> local recorder]

xAI's realtime WebSocket (wss://api.x.ai/v1/realtime?model=...) does speech recognition, the LLM
and the voice in one session. There is no stored agent: every call sends the rendered prompt,
voice, tools and audio format in `session.update`. Protocol, per xAI's Speech to Speech docs:
https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech

- Wait for `conversation.created`, send `session.update`, wait for `session.updated`.
- Play the greeting with a non-interruptible `force_message`, so the AI (and recording)
  disclosure plays in full and is not left to the model. It is sent only once the call's media
  is connected (the transport drops audio before that), and barge-in is ignored while it is
  still being generated or played.
- Caller audio goes out as `input_audio_buffer.append` (16 kHz PCM16, 100 ms chunks); the
  server's VAD decides turns. Agent audio arrives as `response.output_audio.delta`.
- `input_audio_buffer.speech_started` while the agent is talking is a barge-in: the server
  stops that response itself; this bridge drops what is still queued for the caller.
- `response.function_call_arguments.done` runs the same tools as ElevenLabs, through the same
  ToolRunner (caller identity, strict confirmation, idempotent per call_id). Each result goes
  back as `function_call_output`; after the response ends, one `response.create` continues.
- `end_call` is a local function: the call ends once the goodbye response has finished and its
  audio has had time to play.
"""

import asyncio
import base64
import json
import re
import time
from urllib.parse import quote

from pipecat.frames.frames import CancelFrame, EndFrame, EndWorkerFrame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from websockets.asyncio.client import connect

from .bridge import CHUNK_BYTES, SAMPLE_RATE, provider_problem
from .tools import TOOLS, ToolRunner, provider_tool_definitions

XAI_REALTIME = "wss://api.x.ai/v1/realtime"
SESSION_READY_SECONDS = 10
BYTES_PER_SECOND = SAMPLE_RATE * 2
PLAYBACK_MARGIN_SECONDS = 0.3
MAX_GOODBYE_WAIT_SECONDS = 20
# Errors xAI reports for harmless races (cancelling a finished response, truncating twice).
BENIGN_ERRORS = ("response_cancel_not_active", "conversation_already_has_active_response")
BENIGN_MESSAGES = ("no active response", "already has an active response", "truncat")
END_CALL = {"type": "function", "name": "end_call",
            "description": "End the call when the caller says goodbye, asks to hang up, or confirms they need "
                           "nothing else. Say one short goodbye first.",
            "parameters": {"type": "object", "properties": {}, "required": []}}
VARIABLE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
SAFE_CODE = re.compile(r"[^a-z0-9_]+")


def render_instructions(prompt, variables):
    """The prompt with this call's {{variables}} filled in. ElevenLabs does this on its side;
    xAI receives finished instructions."""
    return VARIABLE.sub(lambda match: str(variables.get(match.group(1), match.group(0))), prompt)


def grok_tools():
    """The ElevenLabs client tool schemas as xAI function tools, plus end_call."""
    return [{"type": "function", "name": tool["name"], "description": tool["description"],
             "parameters": tool["parameters"]} for tool in provider_tool_definitions()] + [END_CALL]


def session_update(spec, variables):
    raw = spec.raw
    keyterms = [str(word)[:50] for word in raw.get("asr", {}).get("keywords", [])][:100]
    transcription = {"language_hint": raw["agent"]["language"]}
    if keyterms:
        transcription["keyterms"] = keyterms
    pcm = {"type": "audio/pcm", "rate": SAMPLE_RATE}
    return {"type": "session.update", "session": {
        "instructions": render_instructions(spec.prompt, variables),
        "voice": spec.grok["voice"],
        "turn_detection": {"type": "server_vad"},
        "audio": {"input": {"format": pcm, "transcription": transcription}, "output": {"format": pcm}},
        "tools": grok_tools(),
    }}


async def grok_endpoint(settings, spec):
    """WebSocket URL and headers. The API key goes in a header from this process only."""
    if settings.dev_agent_ws_url:
        return settings.dev_agent_ws_url, {}  # loopback-only offline fake, validated in config
    url = f"{XAI_REALTIME}?model={quote(spec.grok['model'], safe='')}"
    return url, {"Authorization": f"Bearer {settings.xai_api_key}"}


class GrokRefused(Exception):
    """xAI reported an error the session cannot continue after."""

    def __init__(self, problem):
        super().__init__(problem)
        self.problem = problem


def error_code(message):
    error = message.get("error") if isinstance(message.get("error"), dict) else {}
    code = SAFE_CODE.sub("_", str(error.get("code") or error.get("type") or "unknown").lower()).strip("_")[:40]
    text = str(error.get("message") or "").lower()
    benign = code in BENIGN_ERRORS or any(fragment in text for fragment in BENIGN_MESSAGES)
    if "rate" in code or "rate limit" in text or "quota" in code:
        problem = "rate_limited"
    elif "auth" in code or "api key" in text or "permission" in code:
        problem = "auth"
    elif "model" in code or "not_found" in code:
        problem = "not_found"
    else:
        problem = None
    return code or "unknown", benign, problem


class GrokAgentBridge(FrameProcessor):
    def __init__(self, context, store, caller, log, ref, url_factory=grok_endpoint, capture=None, clock=time.monotonic,
                 sleep=asyncio.sleep):
        super().__init__()
        self.context, self.store, self.log, self.ref = context, store, log, ref
        self.capture, self.clock, self.sleep = capture, clock, sleep
        self.tools = ToolRunner(store, caller)
        self.url_factory = url_factory
        self.websocket = None
        self.receive_task = None
        self.input_buffer = bytearray()
        self.stopping = False
        self.audio_in = self.audio_out = 0
        self.conversation_id = None
        self.ready = asyncio.Event()
        self.responding = set()          # response ids still producing audio
        self.dropped = set()             # responses the caller talked over
        self.media_connected = False     # set by the session when WebRTC connects
        self.greeting_pending = False    # provider ready, waiting for media
        self.greeting_sent = False
        self.greeting_response = None
        self.greeting_until = 0.0        # local playback end of the disclosure greeting
        self.playback_until = 0.0        # local playback end of everything queued so far
        self.tool_outputs_pending = False
        self.ending = False
        self.agent_text = {}

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            try:
                url, headers = await self.url_factory(self.context["settings"], self.context["spec"])
                self.websocket = await connect(url, additional_headers=headers, open_timeout=15, max_size=4_000_000)
                self.receive_task = self.create_task(self.receive(), name="grok-agent-events")
                self.log.add("agent_socket_connected", self.ref)
            except Exception as error:
                self.log.add("agent_start_failed_" + type(error).__name__, self.ref)
                self.log.provider_failed(provider_problem(error), self.ref)
                await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)
        elif isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if self.websocket is None or not self.ready.is_set():
                return  # xAI applies the 16 kHz format only after session.updated
            if frame.sample_rate != SAMPLE_RATE or frame.num_channels != 1:
                raise ValueError("Agent input must be 16 kHz mono PCM")
            self.audio_in += 1
            if self.audio_in == 1:
                self.log.add("first_audio_in", self.ref)
            self.input_buffer.extend(frame.audio)
            if self.capture:
                await self.push_frame(frame, direction)
            while len(self.input_buffer) >= CHUNK_BYTES:
                chunk = bytes(self.input_buffer[:CHUNK_BYTES])
                del self.input_buffer[:CHUNK_BYTES]
                try:
                    await self.send({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk).decode()})
                except Exception as error:
                    self.log.add("agent_audio_send_failed_" + type(error).__name__, self.ref)
                    await self.stop_socket()
                    await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)
                    return
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.stop_socket()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def send(self, message):
        await self.websocket.send(json.dumps(message))

    async def handle_event(self, message):
        kind = message.get("type")
        if kind == "conversation.created":
            self.conversation_id = str((message.get("conversation") or {}).get("id") or "")[:128] or None
            await self.send(session_update(self.context["spec"], self.context["dynamic_variables"]))
        elif kind == "session.updated":
            if self.ready.is_set():
                return
            self.ready.set()
            self.log.add("agent_session_ready", self.ref)
            self.log.provider_ok()
            self.greeting_pending = True
            await self.greet()
        elif kind == "response.created":
            response_id = str((message.get("response") or {}).get("id") or "")
            self.responding.add(response_id)
            if self.greeting_sent and self.greeting_response is None:
                self.greeting_response = response_id
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            response_id = str(message.get("response_id") or "")
            if response_id in self.dropped:
                return  # generated before the caller interrupted
            audio = base64.b64decode(message["delta"], validate=True)
            if len(audio) % 2:
                raise ValueError("Invalid PCM audio")
            self.playback_until = max(self.playback_until, self.clock()) + len(audio) / BYTES_PER_SECOND
            if response_id == self.greeting_response:
                self.greeting_until = self.playback_until
            self.audio_out += 1
            if self.audio_out == 1:
                self.log.add("first_audio_out", self.ref)
            await self.push_frame(OutputAudioRawFrame(audio, SAMPLE_RATE, 1))
        elif kind == "input_audio_buffer.speech_started":
            if self.greeting_protected():
                return  # the disclosure greeting is still being generated or played to the caller
            if self.audio_out:
                self.dropped.update(self.responding)
                self.playback_until = self.clock()
                self.log.add("agent_interrupted", self.ref)
                await self.broadcast_interruption()
        elif kind == "response.function_call_arguments.done":
            await self.run_tool(message)
        elif kind == "response.output_audio_transcript.delta" and self.capture:
            response_id = str(message.get("response_id") or "")
            if response_id == self.greeting_response:
                return  # captured as sent
            self.agent_text[response_id] = self.agent_text.get(response_id, "") + str(message.get("delta") or "")
        elif kind == "conversation.item.input_audio_transcription.completed" and self.capture:
            self.capture.add_turn("user", message.get("transcript"))
        elif kind == "response.done":
            await self.finish_response(message)
        elif kind == "error":
            code, benign, problem = error_code(message)
            if benign:
                return
            self.log.add("agent_error_" + code, self.ref)
            if problem or not self.ready.is_set():
                # Before the session is configured nothing usable can follow; auth and rate limits never recover.
                # Other errors leave the session connected with its previous settings (xAI docs).
                raise GrokRefused(problem or "failed")

    def greeting_protected(self):
        if self.greeting_pending or (self.greeting_sent and (self.greeting_response is None
                                                             or self.greeting_response in self.responding)):
            return True
        return self.clock() < self.greeting_until + PLAYBACK_MARGIN_SECONDS

    async def media_ready(self):
        """Called by the session when the WebRTC peer connects."""
        self.media_connected = True
        await self.greet()

    async def greet(self):
        if not (self.greeting_pending and self.media_connected and self.websocket):
            return
        self.greeting_pending = False
        self.greeting_sent = True
        opening = self.context["dynamic_variables"]["opening_message"]
        await self.send({"type": "conversation.item.create", "item": {
            "type": "force_message", "role": "assistant", "interruptible": False,
            "content": [{"type": "output_text", "text": opening}]}})
        if self.capture:
            self.capture.add_turn("agent", opening)

    async def run_tool(self, message):
        name, call_id = str(message.get("name") or ""), str(message.get("call_id") or "")[:128]
        if name == "end_call":
            self.ending = True
            self.log.add("tool_end_call_ok", self.ref)
            return
        try:
            parameters = json.loads(message.get("arguments") or "{}")
        except ValueError:
            parameters = None
        if not isinstance(parameters, dict):
            parameters = {"arguments": "not a JSON object"}  # rejected by the tool's model as invalid_arguments
        result = await asyncio.to_thread(self.tools.execute, {"tool_name": name, "tool_call_id": call_id,
                                                              "parameters": parameters})
        logged = name if name in TOOLS else "unknown"
        self.log.add(f"tool_{logged}_{'failed' if result['is_error'] else 'ok'}", self.ref)
        await self.send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": result["result"]}})
        self.tool_outputs_pending = True

    async def finish_response(self, message):
        response = message.get("response") or {}
        response_id = str(response.get("id") or "")
        self.responding.discard(response_id)
        if self.capture and response_id in self.agent_text:
            self.capture.add_turn("agent", self.agent_text.pop(response_id))
        if self.ending:
            # The goodbye is generated but may still be queued for the caller: wait for it to play,
            # then close, which ends the receive loop and the call.
            remaining = self.playback_until - self.clock()
            if remaining > 0:
                await self.sleep(min(remaining + PLAYBACK_MARGIN_SECONDS, MAX_GOODBYE_WAIT_SECONDS))
            await self.websocket.close()
            return
        if self.tool_outputs_pending:
            # One response.create after every output of the response was sent (parallel calls).
            self.tool_outputs_pending = False
            await self.send({"type": "response.create"})

    async def receive(self):
        try:
            async with asyncio.timeout(SESSION_READY_SECONDS):
                while not self.ready.is_set():
                    await self.handle_event(json.loads(await self.websocket.recv()))
            async for raw in self.websocket:
                await self.handle_event(json.loads(raw))
            self.log.add("agent_conversation_ended", self.ref)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.log.add("agent_session_not_ready", self.ref)
            self.log.provider_failed("failed", self.ref)
        except GrokRefused as error:
            self.log.provider_failed(error.problem, self.ref)
        except Exception as error:
            self.log.add("agent_receive_failed_" + type(error).__name__, self.ref)
            if not self.ready.is_set():
                self.log.provider_failed(provider_problem(error), self.ref)
        finally:
            if not self.stopping:
                await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    async def stop_socket(self):
        self.stopping = True
        if self.websocket:
            await self.websocket.close()
            self.websocket = None
        if self.receive_task:
            await self.cancel_task(self.receive_task)
            self.receive_task = None

    async def cleanup(self):
        await self.stop_socket()
        await super().cleanup()
