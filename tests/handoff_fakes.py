"""Offline stand-ins for the handoff tests and scripts/handoff_local_demo.py. Nothing here reaches
the network."""

import asyncio
import base64
import json

import numpy as np
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

AGENT_HZ = 1000


def tone_pcm(hz, start, samples=1600, rate=16000):
    position = np.arange(samples) + start
    return (np.sin(position * 2 * np.pi * hz / rate) * 6000).astype(np.int16).tobytes()


class FakeGrokAgent:
    """xAI realtime-shaped agent: talks continuously at 1000 Hz and, after `transfer_after`
    seconds of connected media, calls transfer_to_human once. Records what the bridge sends."""

    def __init__(self, transfer_after=None):
        self.transfer_after = transfer_after
        self.server, self.closed = None, asyncio.Event()
        self.tools, self.tool_outputs, self.notices, self.audio_in = [], [], [], []

    async def __aenter__(self):
        self.server = await serve(self.handle, "127.0.0.1", 0).__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.server.__aexit__(*exc)

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def handle(self, socket):
        await socket.send(json.dumps({"type": "conversation.created", "conversation": {"id": "conv-fake-1"}}))
        talker = None
        try:
            async for raw in socket:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "session.update":
                    self.tools = [tool["name"] for tool in message["session"]["tools"]]
                    await socket.send(json.dumps({"type": "session.updated"}))
                elif kind == "conversation.item.create":
                    item = message["item"]
                    if item["type"] == "function_call_output":
                        self.tool_outputs.append(json.loads(item["output"]))
                    elif item["type"] == "force_message" and talker is None:
                        talker = asyncio.create_task(self.talk(socket))  # the greeting: media is up
                    elif item["type"] == "force_message":
                        self.notices.append(item["content"][0]["text"])
                elif kind == "input_audio_buffer.append":
                    self.audio_in.append(asyncio.get_running_loop().time())
        except ConnectionClosed:
            pass
        finally:
            if talker:
                talker.cancel()
            self.closed.set()

    async def talk(self, socket):
        loop = asyncio.get_running_loop()
        started, position, asked = loop.time(), 0, False
        while True:
            await socket.send(json.dumps({"type": "response.output_audio.delta", "response_id": "resp-talk",
                                          "delta": base64.b64encode(tone_pcm(AGENT_HZ, position)).decode()}))
            position += 1600
            if self.transfer_after is not None and not asked and loop.time() - started >= self.transfer_after:
                asked = True
                await socket.send(json.dumps({"type": "response.function_call_arguments.done", "name": "transfer_to_human",
                                              "call_id": "call-transfer-1", "arguments": "{}"}))
                await socket.send(json.dumps({"type": "response.done", "response": {"id": "resp-tool"}}))
            await asyncio.sleep(0.1)
