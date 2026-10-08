"""Offline stand-in for the ElevenLabs Agents WebSocket, for local tests and browser smoke checks.

It speaks the subset of the protocol the bridge uses: conversation metadata, a short tone as the
"greeting", one available_slots tool call, an echo of caller audio, and pings. It is not a
conversational agent. Point DEV_AGENT_WS_URL at it (loopback only).
"""

import asyncio
import base64
import json
import math
import struct

from websockets.asyncio.server import serve

SAMPLE_RATE = 16000


def tone(seconds=0.4, hertz=330, amplitude=3000):
    count = int(SAMPLE_RATE * seconds)
    return b"".join(struct.pack("<h", int(math.sin(2 * math.pi * hertz * i / SAMPLE_RATE) * amplitude))
                    for i in range(count))


async def conversation(socket, echo=True):
    init = json.loads(await socket.recv())
    opening = init.get("dynamic_variables", {}).get("opening_message", "")
    await socket.send(json.dumps({"type": "conversation_initiation_metadata", "conversation_initiation_metadata_event": {
        "conversation_id": "fake-conversation-0001", "user_input_audio_format": "pcm_16000",
        "agent_output_audio_format": "pcm_16000"}}))
    await socket.send(json.dumps({"type": "agent_response", "agent_response_event": {"agent_response": opening}}))
    await socket.send(json.dumps({"type": "audio", "audio_event": {"event_id": 1,
                                                                    "audio_base_64": base64.b64encode(tone()).decode()}}))
    await socket.send(json.dumps({"type": "client_tool_call", "client_tool_call": {
        "tool_name": "available_slots", "tool_call_id": "fake-tool-1", "parameters": {}}}))
    await socket.send(json.dumps({"type": "ping", "ping_event": {"event_id": 1}}))
    event_id = 1
    async for raw in socket:
        message = json.loads(raw)
        if echo and "user_audio_chunk" in message:
            event_id += 1
            await socket.send(json.dumps({"type": "audio", "audio_event": {
                "event_id": event_id, "audio_base_64": message["user_audio_chunk"]}}))


async def run(host="127.0.0.1", port=8765):
    async with serve(conversation, host, port) as server:
        print(f"Fake agent on ws://{host}:{port} (set DEV_AGENT_WS_URL to this)")
        await server.serve_forever()


def main(port=8765):
    asyncio.run(run(port=port))
