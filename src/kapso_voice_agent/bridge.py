"""Audio bridge: Pipecat SmallWebRTC carries audio; ElevenLabs Agents runs the conversation.

Pipeline: transport.input() -> ElevenAgentBridge -> transport.output() [-> local recorder]
The bridge forwards 16 kHz PCM caller audio to the agent WebSocket, plays the agent's PCM back,
honours interruptions, answers pings and runs client tools locally. There is no second LLM, ASR
or TTS here. With VOICE_PROVIDER=grok, grok_bridge.GrokAgentBridge takes the bridge's place.

The agent speaks its greeting as soon as the conversation opens. So the bridge gets the signed URL
when the pipeline starts, but opens the conversation only once the call's WebRTC media is
connected (`media_ready`); otherwise the start of the AI disclosure could play into a call whose
audio is not flowing yet.
"""

import asyncio
import base64
import json
from urllib.parse import urlparse

import httpx
from pipecat.frames.frames import CancelFrame, EndFrame, EndWorkerFrame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK

from .business import WEEKDAYS
from .capture import LocalCapture
from .tools import TOOLS, ToolRunner

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100 ms of 16 kHz mono PCM16
SIGNED_URL_API = "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
# A call whose audio has not connected by then ends (AgentSession.end_if_media_never_connects).
CONNECT_TIMEOUT_SECONDS = 30


class ProviderRejected(Exception):
    """The voice provider answered a session request with an HTTP error status."""

    def __init__(self, status):
        super().__init__(f"Voice provider HTTP {status}")
        self.status = status


def provider_problem(error):
    """Short operator-facing reason a provider session did not start (events.PROVIDER_PROBLEMS)."""
    status = getattr(error, "status", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)  # websockets InvalidStatus
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "billing"
    if status == 429:
        return "rate_limited"
    if status in (400, 404):
        return "not_found"
    if isinstance(status, int) and status >= 500:
        return "unreachable"
    if isinstance(error, (OSError, TimeoutError, httpx.TransportError)):
        return "unreachable"
    return "failed"


def make_connection(ice_json):
    servers = [IceServer(**row) for row in json.loads(ice_json)]
    # Pipecat's own connect timeout cancels its peer-connection close halfway, and every later
    # close then waits forever. AgentSession ends a call that never connects well before this.
    return SmallWebRTCConnection(ice_servers=servers, connection_timeout_secs=CONNECT_TIMEOUT_SECONDS * 4)


def media_path(connection):
    """How the call's audio flows: "relay" (through a relay server), "direct" (host or STUN-mapped address) or
    "unknown". Reads aiortc's selected ICE pair; best effort, for the operator log only."""
    try:
        transceiver = connection.pc.getTransceivers()[0]
        pairs = list(transceiver.sender.transport.transport._connection._nominated.values())
    except (AttributeError, IndexError, TypeError):
        return "unknown"
    if not pairs:
        return "unknown"
    types = {pairs[0].local_candidate.type, pairs[0].remote_candidate.type}
    return "relay" if "relay" in types else "direct"


def meta_sdp(sdp):
    """Meta accepts only the sha-256 DTLS fingerprint; drop the others aiortc offers."""
    return "\r\n".join(line for line in sdp.splitlines()
                       if not line.startswith("a=fingerprint:") or line.startswith("a=fingerprint:sha-256")) + "\r\n"


async def signed_url(settings):
    """Server-side signed conversation URL. The API key never leaves this process."""
    if settings.dev_agent_ws_url:
        return settings.dev_agent_ws_url  # loopback-only offline fake, validated in config
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.get(SIGNED_URL_API, headers={"xi-api-key": settings.elevenlabs_api_key},
                                    params={"agent_id": settings.elevenlabs_agent_id})
    if response.is_error:
        raise ProviderRejected(response.status_code)
    url = response.json()["signed_url"]
    parsed = urlparse(url)
    if parsed.scheme != "wss" or parsed.hostname != "api.elevenlabs.io":
        raise ValueError("Unexpected ElevenLabs WebSocket host")
    return url


class ElevenAgentBridge(FrameProcessor):
    def __init__(self, context, store, caller, log, ref, url_factory=signed_url, capture=None):
        super().__init__()
        self.context, self.store, self.log, self.ref = context, store, log, ref
        self.capture = capture
        self.tools = ToolRunner(store, caller)
        self.url_factory = url_factory
        self.websocket = None
        self.receive_task = None
        self.input_buffer = bytearray()
        self.last_interrupt_id = -1
        self.stopping = False
        self.audio_in = self.audio_out = 0
        self.conversation_id = None
        self.url = None                  # signed URL, fetched when the pipeline starts
        self.media_connected = False     # set by the session when WebRTC connects
        self.opening = False
        self.handoff = None              # handoff.CallHandoff when a teammate may take over

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            try:
                self.url = await self.url_factory(self.context["settings"])
            except Exception as error:
                await self.start_failed(error)
                return
            await self.open_conversation()
        elif isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if self.websocket is None:
                return
            if frame.sample_rate != SAMPLE_RATE or frame.num_channels != 1:
                raise ValueError("Agent input must be 16 kHz mono PCM")
            self.audio_in += 1
            if self.audio_in == 1:
                self.log.add("first_audio_in", self.ref)
            self.input_buffer.extend(frame.audio)
            if self.capture:
                # Caller audio continues to the recorder placed after transport.output().
                await self.push_frame(frame, direction)
            while len(self.input_buffer) >= CHUNK_BYTES:
                chunk = bytes(self.input_buffer[:CHUNK_BYTES])
                del self.input_buffer[:CHUNK_BYTES]
                try:
                    await self.websocket.send(json.dumps({"user_audio_chunk": base64.b64encode(chunk).decode()}))
                except Exception as error:
                    if not isinstance(error, ConnectionClosedOK):  # a clean close (end_call) is not a failure
                        self.log.add("agent_audio_send_failed_" + type(error).__name__, self.ref)
                    await self.stop_socket()
                    await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)
                    return
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.stop_socket()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def media_ready(self):
        """Called by the session when the WebRTC peer connects."""
        self.media_connected = True
        await self.open_conversation()

    async def open_conversation(self):
        """Open the agent conversation once both the signed URL and connected media are there."""
        if not (self.url and self.media_connected) or self.opening or self.stopping:
            return
        self.opening = True
        try:
            self.websocket = await connect(self.url, open_timeout=15, max_size=2_000_000)
            await self.websocket.send(json.dumps({"type": "conversation_initiation_client_data",
                                                  "dynamic_variables": self.context["dynamic_variables"]}))
            self.receive_task = self.create_task(self.receive(), name="eleven-agent-events")
            self.log.add("agent_socket_connected", self.ref)
        except Exception as error:
            await self.start_failed(error)

    async def start_failed(self, error):
        self.log.add("agent_start_failed_" + type(error).__name__, self.ref)
        self.log.provider_failed(provider_problem(error), self.ref)
        await self.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    async def handle_event(self, message):
        kind = message.get("type")
        if kind == "conversation_initiation_metadata":
            data = message["conversation_initiation_metadata_event"]
            if data["agent_output_audio_format"] != "pcm_16000" or data["user_input_audio_format"] != "pcm_16000":
                raise ValueError("Agent audio format is not pcm_16000; fix the agent's tts/asr formats")
            self.conversation_id = data["conversation_id"]
            self.log.add("agent_session_ready", self.ref)
            self.log.provider_ok()
        elif kind == "audio":
            data = message["audio_event"]
            if int(data["event_id"]) <= self.last_interrupt_id:
                return  # audio generated before the caller interrupted
            audio = base64.b64decode(data["audio_base_64"], validate=True)
            if len(audio) % 2:
                raise ValueError("Invalid PCM audio")
            self.audio_out += 1
            if self.audio_out == 1:
                self.log.add("first_audio_out", self.ref)
            await self.push_frame(OutputAudioRawFrame(audio, SAMPLE_RATE, 1))
        elif kind == "interruption":
            self.last_interrupt_id = max(self.last_interrupt_id, int(message["interruption_event"]["event_id"]))
            self.log.add("agent_interrupted", self.ref)
            await self.broadcast_interruption()
        elif kind == "ping":
            await self.websocket.send(json.dumps({"type": "pong", "event_id": message["ping_event"]["event_id"]}))
        elif kind == "client_tool_call":
            call = message["client_tool_call"]
            result = await asyncio.to_thread(self.tools.execute, call)
            name = call.get("tool_name") if call.get("tool_name") in TOOLS else "unknown"
            self.log.add(f"tool_{name}_{'failed' if result['is_error'] else 'ok'}", self.ref)
            if call.get("expects_response", True):
                await self.websocket.send(json.dumps(result))
        elif kind in ("user_transcript", "agent_response"):
            if self.capture:
                text = (message.get("user_transcription_event", {}).get("user_transcript") if kind == "user_transcript"
                        else message.get("agent_response_event", {}).get("agent_response"))
                self.capture.add_turn("user" if kind == "user_transcript" else "agent", text)
        elif kind == "client_error":
            raise RuntimeError("The agent conversation reported an error")

    async def receive(self):
        try:
            async for raw in self.websocket:
                await self.handle_event(json.loads(raw))
            self.log.add("agent_conversation_ended", self.ref)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.log.add("agent_receive_failed_" + type(error).__name__, self.ref)
        finally:
            if not self.stopping:
                # Ask the worker to end so it drains audio and exits its run loop.
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


def session_context(settings, spec, store, direction, recording):
    """Per-call values sent to the agent. `recording` is the verified state from recording.py,
    so the greeting mentions recording only when it really applies."""
    today = store.now().date()
    return {"settings": settings, "spec": spec, "dynamic_variables": {
        "today": today.isoformat(), "weekday": WEEKDAYS[today.weekday()].capitalize(), "timezone": str(store.timezone),
        "business_name": spec.business_label(),
        "call_purpose": "outbound_appointment_confirmation" if direction == "outbound" else "inbound",
        "opening_message": spec.greeting(direction, recording.recorded),
        "recording_status": spec.recording_status(recording.recorded)}}


def agent_bridge(settings):
    """The bridge for VOICE_PROVIDER: ElevenLabs Agents (default) or xAI Grok Voice."""
    if settings.voice_provider == "grok":
        from .grok_bridge import GrokAgentBridge
        return GrokAgentBridge
    return ElevenAgentBridge


class AgentSession:
    """One call: WebRTC transport + agent bridge (+ optional capture), run by a Pipecat worker."""

    def __init__(self, connection, settings, spec, store, caller, log, ref, direction="inbound", *, recording,
                 url_factory=None, handoff=None):
        self.connection, self.log, self.ref = connection, log, ref
        self.closed = False
        self.capture_dir = None
        self.transport = SmallWebRTCTransport(connection, TransportParams(audio_in_enabled=True, audio_out_enabled=True))
        self.capture = None
        if settings.local_capture:
            self.capture = LocalCapture(settings.data_dir / "captures", ref, float(settings.max_session_seconds),
                                        settings.capture_retention_days, settings.capture_max_count)
        context = session_context(settings, spec, store, direction, recording)
        bridge = agent_bridge(settings)
        self.bridge = bridge(context, store, caller, log, ref, capture=self.capture,
                             **({"url_factory": url_factory} if url_factory else {}))
        self.handoff = handoff
        processors = [self.transport.input(), self.bridge, self.transport.output()]
        if handoff:
            # The switch sits around the bridge; without a handoff the pipeline is unchanged.
            processors = [self.transport.input(), handoff.switch.input, self.bridge, handoff.switch.output,
                          self.transport.output()]
            self.bridge.handoff = handoff
            handoff.attach(self)
        if self.capture:
            processors.append(self.capture.recorder)
        self.worker = PipelineWorker(Pipeline(processors),
                                     params=PipelineParams(audio_in_sample_rate=SAMPLE_RATE, audio_out_sample_rate=SAMPLE_RATE),
                                     # The agent owns silence detection. PCM audio alone does not reset
                                     # Pipecat's turn-frame inactivity timer.
                                     enable_rtvi=False, idle_timeout_secs=None)
        self.runner = WorkerRunner(handle_sigint=False)
        self.task = None
        self.media_connected = asyncio.Event()
        self.connect_timeout = CONNECT_TIMEOUT_SECONDS
        self.connect_watch = None

        @self.transport.event_handler("on_client_connected")
        async def connected(transport, client):
            self.media_connected.set()
            log.add("rtc_connected", ref, media=media_path(connection))
            await self.bridge.media_ready()  # the greeting waits for connected media

        @self.transport.event_handler("on_client_disconnected")
        async def disconnected(transport, client):
            log.add("rtc_disconnected", ref)
            # The runner, not only the worker: a worker cancelled before media connected leaves
            # runner.run() waiting, and the call would hold its slot until MAX_SESSION_SECONDS.
            await self.runner.cancel()

    async def start(self):
        await self.runner.add_workers(self.worker)
        self.task = asyncio.create_task(self.runner.run())
        self.connect_watch = asyncio.create_task(self.end_if_media_never_connects())

    async def end_if_media_never_connects(self):
        try:
            await asyncio.wait_for(self.media_connected.wait(), timeout=self.connect_timeout)
        except TimeoutError:
            self.log.add("rtc_connect_timed_out", self.ref)
            await self.runner.cancel()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.connect_watch and not self.connect_watch.done():
            self.connect_watch.cancel()
        await self.runner.cancel()
        if self.task and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=5)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        await self.bridge.stop_socket()
        if self.handoff:
            await self.handoff.close()
        await self.connection.disconnect()
        if self.capture:
            try:
                self.capture_dir = await self.capture.finish(self.bridge.conversation_id)
                self.log.add("local_capture_saved", self.ref, truncated=self.capture.recorder.truncated)
            except Exception as error:
                self.log.add("local_capture_failed_" + type(error).__name__, self.ref)
        self.log.add("session_closed", self.ref)


ElevenSession = AgentSession  # name used before the Grok bridge existed
