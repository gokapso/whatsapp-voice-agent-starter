"""One operator-triggered outbound call at a time, through Kapso's Calling API.

Order: permission check -> local SDP offer -> connect -> wait for Meta's SDP answer -> apply it ->
wait for ACCEPTED (the person picked up) -> start the agent. A connect whose outcome is unknown
(timeout, dropped connection) is never retried automatically: the phone may already be ringing.
"""

import asyncio
from collections import OrderedDict
import re

from aiortc import RTCSessionDescription
import httpx

from .bridge import meta_sdp
from .events import call_ref
from .kapso import KapsoError
from .recording import RecordingUnknown

RING_TIMEOUT_SECONDS = 90
EARLY_EVENT_LIMIT = 16


class OutboundError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def normalize_recipient(value):
    """A phone number with country code, or a business-scoped user ID (US.xxxxx)."""
    value = value.strip()
    if re.fullmatch(r"US\.[0-9]{5,30}", value):
        return value
    number = re.sub(r"[ +()-]", "", value)
    if not re.fullmatch(r"[1-9][0-9]{7,14}", number):
        raise OutboundError(422, "Use a WhatsApp number with its country code")
    return number


def can_start_call(permission):
    return any(action.get("action_name") == "start_call" and action.get("can_perform_action") is True
               for action in permission.get("actions", []) if isinstance(action, dict))


class OutboundCalls:
    def __init__(self, manager):
        self.manager = manager
        self.dialing = False
        self.pending = {}
        self.early_answers = OrderedDict()
        self.early_statuses = OrderedDict()

    async def permission(self, recipient):
        recipient = normalize_recipient(recipient)
        client = self.manager.kapso_factory()
        try:
            data = await client.permissions(recipient)
        finally:
            await client.close()
        return {"permission_status": str((data.get("permission") or {}).get("status", "unknown"))[:32],
                "can_call": can_start_call(data)}

    def receive_answer(self, call):
        call_id = call["id"]
        if self.manager.seen.get(call_id) in ("terminated", "closed"):
            return
        session = call.get("session") if isinstance(call.get("session"), dict) else {}
        if session.get("sdp_type") != "answer" or not session.get("sdp"):
            return
        if pending := self.pending.get(call_id):
            if not pending["answer"].done():
                pending["answer"].set_result(call)
        elif self.dialing:
            # Meta can deliver the answer before the connect HTTP response arrives.
            self.early_answers[call_id] = call
            if len(self.early_answers) > EARLY_EVENT_LIMIT:
                self.early_answers.popitem(last=False)

    def receive_status(self, status):
        call_id, name = status["id"], status.get("status")
        if name not in ("RINGING", "ACCEPTED", "REJECTED"):
            return
        if pending := self.pending.get(call_id):
            self.manager.log.add("outbound_" + name.lower(), call_ref(call_id))
            if name == "ACCEPTED" and not pending["accepted"].done():
                pending["accepted"].set_result(True)
        elif self.dialing and name == "ACCEPTED":
            self.early_statuses[call_id] = status
            if len(self.early_statuses) > EARLY_EVENT_LIMIT:
                self.early_statuses.popitem(last=False)

    async def start(self, recipient):
        manager = self.manager
        recipient = normalize_recipient(recipient)
        if not manager.settings.calls_ready:
            raise OutboundError(503, "Configure Kapso, the webhook secret and the ElevenLabs agent first")
        try:
            # Before dialing: the greeting must state the real recording setting.
            recording = await manager.recording.current()
        except RecordingUnknown:
            raise OutboundError(503, "Could not read the agent's recording setting; not dialing. "
                                     "Check ELEVENLABS_API_KEY/ELEVENLABS_AGENT_ID and run `voice-agent agent verify`.") from None
        async with manager.lock:
            if manager.busy():
                raise OutboundError(409, "Already at MAX_CONCURRENT_CALLS")
            self.dialing = True
        client, connection, handed_off = manager.kapso_factory(), None, False
        try:
            if not can_start_call(await client.permissions(recipient)):
                raise OutboundError(409, "WhatsApp has not allowed this call. The person must grant calling permission first.")
            connection = manager.connection_factory(manager.settings.ice_servers_json)
            connection.pc.addTransceiver("audio", direction="sendrecv")
            await connection.pc.setLocalDescription(await connection.pc.createOffer())
            try:
                data = await client.connect(recipient, meta_sdp(connection.pc.localDescription.sdp))
            except (httpx.TransportError, asyncio.TimeoutError, OSError) as error:
                manager.log.add("outbound_connect_outcome_unknown")
                raise OutboundError(502, "The call request outcome is unknown. Check the phone before trying again.") from error
            rows = data.get("calls") or []
            if not rows or not isinstance(rows[0], dict) or not isinstance(rows[0].get("id"), str):
                raise OutboundError(502, "Kapso did not return a call ID. Check the phone before trying again.")
            call_id = rows[0]["id"]
            ref = call_ref(call_id)
            async with manager.lock:
                if manager.seen.get(call_id) == "terminated":
                    return {"ref": ref, "state": "terminated"}
                if call_id in manager.seen:
                    raise OutboundError(502, "Kapso returned a call ID that is already known. Do not dial again automatically.")
                loop = asyncio.get_running_loop()
                pending = {"recipient": recipient, "connection": connection, "recording": recording,
                           "answer": loop.create_future(), "accepted": loop.create_future()}
                self.pending[call_id] = pending
                manager.remember(call_id, "ringing")
                if early := self.early_answers.get(call_id):
                    self.receive_answer(early)
                if early := self.early_statuses.get(call_id):
                    self.receive_status(early)
                job = asyncio.create_task(self.run(call_id, pending, client))
                manager.jobs[call_id] = job
                manager.directions[call_id] = "outbound"
                job.add_done_callback(lambda task: manager.remove_job(call_id, task))
                handed_off = True
            manager.log.add("outbound_ringing", ref)
            return {"ref": ref, "state": "ringing"}
        except KapsoError as error:
            raise OutboundError(502, str(error)) from None
        finally:
            async with manager.lock:
                self.dialing = False
                self.early_answers.clear()
                self.early_statuses.clear()
            if not handed_off:
                try:
                    if connection:
                        await connection.disconnect()
                finally:
                    await client.close()

    async def run(self, call_id, pending, client):
        manager, ref = self.manager, call_ref(call_id)
        connection, session = pending["connection"], None
        try:
            async with asyncio.timeout(RING_TIMEOUT_SECONDS):
                call = await pending["answer"]
                await connection.pc.setRemoteDescription(RTCSessionDescription(call["session"]["sdp"], "answer"))
                manager.log.add("outbound_answer_applied", ref)
                # The SDP answer sets up transport; ACCEPTED means the person picked up.
                await pending["accepted"]
            # Identity comes from this call's callee, never from the conversation.
            caller = manager.caller_key(call.get("to_user_id") or pending["recipient"])
            session = manager.session_factory(connection, manager.settings, manager.spec, manager.store, caller,
                                              manager.log, ref, direction="outbound", recording=pending["recording"])
            manager.sessions[call_id] = session
            await session.start()
            manager.log.add("outbound_agent_started", ref)
            await asyncio.wait_for(asyncio.shield(session.task), timeout=manager.settings.max_session_seconds)
        except asyncio.CancelledError:
            manager.log.add("outbound_hangup", ref)
            raise
        except TimeoutError:
            manager.log.add("outbound_timed_out", ref)
        except Exception as error:
            manager.log.add("outbound_failed_" + type(error).__name__, ref)
        finally:
            try:
                try:
                    if session:
                        await session.close()
                    else:
                        await connection.disconnect()
                finally:
                    if manager.seen.get(call_id) != "terminated":
                        try:
                            await client.action(call_id, "terminate")
                            manager.log.add("outbound_terminate_ok", ref)
                        except Exception as error:
                            manager.log.add("outbound_terminate_failed_" + type(error).__name__, ref)
                        manager.remember(call_id, "closed")
            finally:
                self.pending.pop(call_id, None)
                manager.sessions.pop(call_id, None)
                await client.close()
