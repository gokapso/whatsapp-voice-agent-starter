"""xAI Direct SIP control service: POST /webhooks/xai-sip (on when XAI_SIP_WEBHOOK_SECRET is set).

With xAI Direct SIP, a SIP trunk sends the call to sip.voice.x.ai and xAI handles the audio. xAI
then sends a signed `realtime.call.incoming` webhook here, and this server joins the call's
session over wss://api.x.ai/v1/realtime?call_id=... to configure it and run the tools. No WebRTC
and no UDP on this server: it needs public HTTPS and a long-lived outbound WebSocket.
Protocol: https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech/sip

xAI documents this for phone carriers, contact centers and PBXs. It is NOT a working path for
Kapso WhatsApp numbers: Meta offers only Opus over SRTP, and xAI's SIP guide lists only G.711 and
G.722 and does not mention SRTP (docs/choose-where-calls-run.md).

Rules (each has a test in tests/test_xai_sip.py):
- The webhook is verified as a Standard Webhooks v1 signature (HMAC-SHA256 over
  `webhook-id.webhook-timestamp.body`, timestamp within 5 minutes) before it is parsed.
- One control session per call_id; repeated deliveries are ignored. Over capacity, the call is hung up.
- The caller identity comes from the call's SIP headers (`x-wa-meta-user-id`, else the From user),
  keyed with CALLER_KEY_SECRET like every other call. Tool arguments never choose it.
- Same prompt, tools, strict confirmation and greeting as the Grok bridge. The greeting is a
  non-interruptible `force_message` and always says the call is recorded (xAI keeps API traffic
  for 30 days by default).
- The call ends with POST /v1/realtime/calls/{call_id}/hangup: after end_call's goodbye, at
  MAX_SESSION_SECONDS, or when the session fails.
"""

import asyncio
import base64
import binascii
from collections import OrderedDict
import hashlib
import hmac
import json
import re
import time
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect

from .bridge import session_context
from .events import call_ref
from .grok_bridge import END_CALL, error_code, grok_tools, render_instructions
from .recording import Recording
from .tools import TOOLS, ToolRunner

XAI_REALTIME = "wss://api.x.ai/v1/realtime"
XAI_CALLS = "https://api.x.ai/v1/realtime/calls"
TOLERANCE_SECONDS = 300
SESSION_READY_SECONDS = 10
# xAI plays the audio, so this server cannot see when the goodbye has finished playing.
GOODBYE_SECONDS = 4
MAX_ID_LENGTH = 256
SEEN_LIMIT = 512
SIP_USER = re.compile(r"sip:([^@;>]+)@")
# Not verified with xAI: its incoming webhook may not carry Meta's custom headers.
IDENTITY_HEADER = "x-wa-meta-user-id"


def verify_standard_webhook(secret, headers, raw, now=time.time):
    """True when one `v1,` signature in `webhook-signature` matches. Secret: `whsec_` + base64."""
    msg_id, timestamp = headers.get("webhook-id", ""), headers.get("webhook-timestamp", "")
    signatures = headers.get("webhook-signature", "")
    if not (msg_id and "." not in msg_id and timestamp.isdigit() and signatures):
        return False
    if abs(now() - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError):
        return False
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + raw, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value.encode(), expected.encode()):
            return True
    return False


def caller_identity(sip_headers):
    """The caller's identity from the call's own SIP headers, or None."""
    headers = {}
    for header in sip_headers if isinstance(sip_headers, list) else []:
        if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(header.get("value"), str):
            headers[header["name"].strip().lower()] = header["value"].strip()
    if headers.get(IDENTITY_HEADER):
        return headers[IDENTITY_HEADER][:MAX_ID_LENGTH]
    sender = headers.get("from", "")
    match = SIP_USER.search(sender)
    identity = (match.group(1) if match else sender).strip()
    return identity[:MAX_ID_LENGTH] or None


def incoming_call(payload):
    """(call_id, caller identity) from a realtime.call.incoming event, or None."""
    if not isinstance(payload, dict) or payload.get("type") != "realtime.call.incoming":
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    call_id, identity = data.get("call_id"), caller_identity(data.get("sip_headers"))
    if not (isinstance(call_id, str) and 1 <= len(call_id) <= MAX_ID_LENGTH and identity):
        return None
    return call_id, identity


def sip_session_update(spec, variables):
    """Like grok_bridge.session_update, without an audio format: on SIP, xAI handles the audio."""
    return {"type": "session.update", "session": {
        "instructions": render_instructions(spec.prompt, variables), "voice": spec.grok["voice"],
        "turn_detection": {"type": "server_vad"}, "tools": grok_tools()}}


class SipCall:
    """One SIP call's control session: configure, greet, run tools, hang up."""

    def __init__(self, call_id, caller, settings, spec, store, log, opener=None, hangup=None, sleep=asyncio.sleep):
        self.call_id, self.settings, self.spec, self.log = call_id, settings, spec, log
        self.ref = call_ref(call_id)
        self.tools = ToolRunner(store, caller)
        recording = Recording(provider=True, local=False, source="xai_default_retention")
        self.variables = session_context(settings, spec, store, "inbound", recording)["dynamic_variables"]
        self.opener, self.hangup_request, self.sleep = opener or self.open, hangup or self.post_hangup, sleep
        self.websocket = None
        self.ready = False
        self.ending = False
        self.tool_outputs_pending = False
        self.hung_up = False

    async def open(self):
        url = f"{XAI_REALTIME}?call_id={quote(self.call_id, safe='')}"
        return await connect(url, additional_headers={"Authorization": f"Bearer {self.settings.xai_api_key}"},
                             open_timeout=15, max_size=4_000_000)

    async def post_hangup(self):
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(f"{XAI_CALLS}/{quote(self.call_id, safe='')}/hangup",
                                         headers={"Authorization": f"Bearer {self.settings.xai_api_key}"})
        if response.is_error:
            raise RuntimeError(f"xAI hangup HTTP {response.status_code}")

    async def send(self, message):
        await self.websocket.send(json.dumps(message))

    async def run(self):
        try:
            async with asyncio.timeout(self.settings.max_session_seconds):
                self.websocket = await self.opener()
                self.log.add("sip_session_connected", self.ref)
                await self.send(sip_session_update(self.spec, self.variables))
                async with asyncio.timeout(SESSION_READY_SECONDS):
                    while not self.ready:
                        await self.handle(json.loads(await self.websocket.recv()))
                async for raw in self.websocket:
                    if await self.handle(json.loads(raw)) == "end":
                        break
            self.log.add("sip_session_finished", self.ref)
        except TimeoutError:
            self.log.add("sip_session_timed_out" if self.ready else "sip_session_not_ready", self.ref)
        except Exception as error:
            self.log.add("sip_session_failed_" + type(error).__name__, self.ref)
        finally:
            if self.websocket:
                await self.websocket.close()
            await self.hang_up()

    async def hang_up(self):
        if self.hung_up:
            return
        self.hung_up = True
        try:
            await self.hangup_request()
            self.log.add("sip_hangup_ok", self.ref)
        except Exception as error:  # the caller may have hung up already
            self.log.add("sip_hangup_failed_" + type(error).__name__, self.ref)

    async def handle(self, message):
        kind = message.get("type")
        if kind == "session.updated" and not self.ready:
            self.ready = True
            self.log.add("agent_session_ready", self.ref)
            await self.send({"type": "conversation.item.create", "item": {
                "type": "force_message", "role": "assistant", "interruptible": False,
                "content": [{"type": "output_text", "text": self.variables["opening_message"]}]}})
        elif kind == "response.function_call_arguments.done":
            await self.run_tool(message)
        elif kind == "response.done":
            if self.ending:
                await self.sleep(GOODBYE_SECONDS)
                return "end"
            if self.tool_outputs_pending:
                self.tool_outputs_pending = False
                await self.send({"type": "response.create"})
        elif kind == "error":
            code, benign, problem = error_code(message)
            if not benign:
                self.log.add("agent_error_" + code, self.ref)
                if problem or not self.ready:
                    raise RuntimeError(problem or "failed")
        return None

    async def run_tool(self, message):
        name, tool_call_id = str(message.get("name") or ""), str(message.get("call_id") or "")[:128]
        if name == END_CALL["name"]:
            self.ending = True
            self.log.add("tool_end_call_ok", self.ref)
            return
        try:
            parameters = json.loads(message.get("arguments") or "{}")
        except ValueError:
            parameters = None
        if not isinstance(parameters, dict):
            parameters = {"arguments": "not a JSON object"}
        result = await asyncio.to_thread(self.tools.execute, {"tool_name": name, "tool_call_id": tool_call_id,
                                                              "parameters": parameters})
        self.log.add(f"tool_{name if name in TOOLS else 'unknown'}_{'failed' if result['is_error'] else 'ok'}", self.ref)
        await self.send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": tool_call_id, "output": result["result"]}})
        self.tool_outputs_pending = True


class SipCalls:
    """The process's xAI SIP calls: one control session per call_id, within MAX_CONCURRENT_CALLS."""

    def __init__(self, settings, spec, store, log, call_factory=SipCall):
        self.settings, self.spec, self.store, self.log = settings, spec, store, log
        self.call_factory = call_factory
        self.jobs, self.seen, self.background = {}, OrderedDict(), set()

    def start(self, call_id, identity, caller_key):
        if call_id in self.seen:
            return "duplicate"
        self.seen[call_id] = True
        while len(self.seen) > SEEN_LIMIT:
            self.seen.popitem(last=False)
        call = self.call_factory(call_id, caller_key(identity), self.settings, self.spec, self.store, self.log)
        if len(self.jobs) >= self.settings.max_concurrent_calls:
            self.log.add("sip_rejected_busy", call.ref)
            task = asyncio.create_task(call.hang_up())
            self.background.add(task)
            task.add_done_callback(self.background.discard)
            return "busy"
        job = asyncio.create_task(call.run())
        self.jobs[call_id] = job
        job.add_done_callback(lambda _task: self.jobs.pop(call_id, None))
        return "started"

    async def shutdown(self):
        jobs = list(self.jobs.values()) + list(self.background)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
