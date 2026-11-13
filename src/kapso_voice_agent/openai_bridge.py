"""OpenAI Realtime bridge: OpenAI speech to speech runs the conversation (VOICE_PROVIDER=openai).

Same place in the pipeline as grok_bridge.GrokAgentBridge, and the same lifecycle: barge-in,
greeting protection, ToolRunner tools, end_call and capture. The protocol differences, per OpenAI's
Realtime docs (https://developers.openai.com/api/docs/guides/realtime-conversations):

- The first server event is `session.created`; the bridge answers with a GA `session.update`
  (`session.type = "realtime"`, audio settings under `audio.input` and `audio.output`).
- OpenAI accepts and produces only 24 kHz PCM16. The call transport runs at 16 kHz, so caller
  audio is resampled 16 -> 24 kHz before `input_audio_buffer.append`, and agent audio
  24 -> 16 kHz before it is queued for the caller.
- There is no non-interruptible "say this" item. The greeting is a `response.create` whose
  instructions ask for the opening text verbatim; barge-in stays ignored while it plays.
- Function calls are read from `response.done` (output items of type `function_call`), which
  carries name, call_id and arguments together.
"""

import base64
from urllib.parse import quote

from pipecat.audio.utils import create_stream_resampler

from .bridge import SAMPLE_RATE
from .grok_bridge import GrokAgentBridge, grok_tools, render_instructions

OPENAI_REALTIME = "wss://api.openai.com/v1/realtime"
OPENAI_RATE = 24000
LANGUAGE = 2  # ISO 639-1 code length accepted by the transcription hint


def session_update(spec, variables):
    pcm = {"type": "audio/pcm", "rate": OPENAI_RATE}
    audio_input = {"format": pcm, "turn_detection": {"type": "server_vad", "interrupt_response": True}}
    language = str(spec.raw["agent"].get("language") or "")
    if len(language) == LANGUAGE:
        audio_input["transcription"] = {"model": spec.openai["transcription_model"], "language": language}
    return {"type": "session.update", "session": {
        "type": "realtime",
        "output_modalities": ["audio"],
        "instructions": render_instructions(spec.prompt, variables),
        "audio": {"input": audio_input, "output": {"format": pcm, "voice": spec.openai["voice"]}},
        "tools": grok_tools(),
        "tool_choice": "auto",
    }}


def greeting_request(opening):
    return {"type": "response.create", "response": {
        "instructions": "Say exactly the following text to the caller, word for word, with nothing before or "
                        "after it, then stop and wait for them:\n" + opening}}


async def openai_endpoint(settings, spec):
    """WebSocket URL and headers. The API key goes in a header from this process only."""
    if settings.dev_agent_ws_url:
        return settings.dev_agent_ws_url, {}  # loopback-only offline fake, validated in config
    url = f"{OPENAI_REALTIME}?model={quote(spec.openai['model'], safe='')}"
    return url, {"Authorization": f"Bearer {settings.openai_api_key}"}


class OpenAIAgentBridge(GrokAgentBridge):
    def __init__(self, *args, url_factory=openai_endpoint, **kwargs):
        super().__init__(*args, url_factory=url_factory, **kwargs)
        self.upsample = create_stream_resampler()
        self.downsample = create_stream_resampler()

    async def send(self, message):
        if message.get("type") == "input_audio_buffer.append":
            audio = await self.upsample.resample(base64.b64decode(message["audio"]), SAMPLE_RATE, OPENAI_RATE)
            if not audio:
                return  # the resampler is still filling its window
            message = {**message, "audio": base64.b64encode(audio).decode()}
        await super().send(message)

    async def handle_event(self, message):
        kind = message.get("type")
        if kind == "session.created":
            self.conversation_id = str((message.get("session") or {}).get("id") or "")[:128] or None
            await self.send(session_update(self.context["spec"], self.context["dynamic_variables"]))
        elif kind == "conversation.created":
            return  # beta-era event; session.created already configured the session
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            audio = await self.downsample.resample(base64.b64decode(message["delta"], validate=True),
                                                   OPENAI_RATE, SAMPLE_RATE)
            if not audio:
                return
            await super().handle_event({**message, "delta": base64.b64encode(audio).decode()})
        elif kind == "response.function_call_arguments.done":
            return  # handled once, from response.done, where the name is always present
        elif kind == "response.done":
            for item in (message.get("response") or {}).get("output") or []:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    await self.run_tool(item)
            await self.finish_response(message)
        else:
            await super().handle_event(message)

    async def greet(self):
        if not (self.greeting_pending and self.media_connected and self.websocket):
            return
        self.greeting_pending = False
        self.greeting_sent = True
        opening = self.context["dynamic_variables"]["opening_message"]
        await self.send(greeting_request(opening))
        if self.capture:
            self.capture.add_turn("agent", opening)
