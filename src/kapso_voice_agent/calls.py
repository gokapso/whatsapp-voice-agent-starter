"""Call state for one process: Calling webhook events in, Kapso call actions and agent sessions out.

Rules this file keeps (each has a test):
- Only `calls` changes for the configured phone_number_id are processed; others are ignored.
- A terminate (or REJECTED status) wins even when batched before its connect.
- A repeated or late connect never starts a second session for the same Meta call ID.
- Recording/transcript completion events are metadata only; they never start a session.
- Over capacity, an inbound call is declined with `reject` instead of being left ringing.
- A failure before `accept` sends `reject`; after `accept` it sends `terminate`.
"""

import asyncio
from collections import OrderedDict, deque
import hashlib
import hmac
import secrets

from .bridge import ElevenSession, make_connection, meta_sdp
from .events import call_ref
from .kapso import KapsoClient
from .outbound import OutboundCalls
from .recording import RecordingCheck

ARTIFACT_EVENTS = {"call_recording_available": ("call_recording", "audio"),
                   "call_transcription_available": ("call_transcript", "document"),
                   "call_transcript_available": ("call_transcript", "document")}
MAX_ID_LENGTH = 512
SEEN_LIMIT = 512
ANSWER_TIMEOUT_SECONDS = 20


class NotReady(Exception):
    """An inbound call arrived before the server has credentials to answer it."""


def valid_id(value):
    return isinstance(value, str) and 1 <= len(value) <= MAX_ID_LENGTH


class CallManager:
    def __init__(self, settings, spec, store, log, session_factory=ElevenSession,
                 connection_factory=make_connection, kapso_factory=None, recording_reader=None):
        self.settings, self.spec, self.store, self.log = settings, spec, store, log
        self.session_factory, self.connection_factory = session_factory, connection_factory
        self.kapso_factory = kapso_factory or (lambda: KapsoClient(settings.kapso_api_key, settings.phone_number_id,
                                                                   settings.meta_graph_version))
        self.sessions, self.jobs = {}, {}
        self.directions = {}
        self.seen = OrderedDict()
        self.lock = asyncio.Lock()
        self.outbound = OutboundCalls(self)
        # Decides the recording notice; no agent session starts while it is unknown.
        self.recording = RecordingCheck(settings, spec, log, reader=recording_reader)
        self.artifacts = deque(maxlen=50)
        self.background = set()

    # Identity and bookkeeping --------------------------------------------------------------

    def caller_key(self, identity):
        """Stable per-caller key for the booking store; raw phone numbers and BSUIDs are not stored."""
        return hmac.new(self.settings.caller_secret().encode(), identity.encode(), hashlib.sha256).hexdigest()

    def remember(self, call_id, state):
        self.seen[call_id] = state
        self.seen.move_to_end(call_id)
        while len(self.seen) > SEEN_LIMIT:
            self.seen.popitem(last=False)

    def busy(self):
        # Every session (inbound, outbound, browser) is owned by exactly one job.
        return len(self.jobs) + int(self.outbound.dialing) >= self.settings.max_concurrent_calls

    def remove_job(self, call_id, task):
        if self.jobs.get(call_id) is task:
            self.jobs.pop(call_id)
            self.directions.pop(call_id, None)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    # Webhook -------------------------------------------------------------------------------

    async def receive(self, payload):
        counts = {"calls": 0, "statuses": 0, "ignored": 0}
        calls, statuses = [], []
        for entry in payload.get("entry") or []:
            for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
                value = change.get("value") if isinstance(change, dict) else None
                if not isinstance(value, dict) or change.get("field") != "calls":
                    counts["ignored"] += 1
                    continue
                metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
                if str(metadata.get("phone_number_id", "")) != self.settings.phone_number_id:
                    counts["ignored"] += 1
                    self.log.add("ignored_other_phone_number")
                    continue
                for call in value.get("calls") if isinstance(value.get("calls"), list) else []:
                    if isinstance(call, dict) and valid_id(call.get("id")):
                        calls.append(call)
                    else:
                        counts["ignored"] += 1
                for status in value.get("statuses") if isinstance(value.get("statuses"), list) else []:
                    if isinstance(status, dict) and valid_id(status.get("id")):
                        statuses.append(status)
                    else:
                        counts["ignored"] += 1
        counts["calls"], counts["statuses"] = len(calls), len(statuses)
        async with self.lock:
            # Terminal events first, so a batched terminate wins over its connect.
            for call in calls:
                if call.get("event") == "terminate":
                    self.log.add("call_terminated", call_ref(call["id"]), status=str(call.get("status", ""))[:32])
                    self.end(call["id"])
            for status in statuses:
                if status.get("status") == "REJECTED":
                    self.end(status["id"])
                self.outbound.receive_status(status)
            for call in calls:
                event = call.get("event")
                if event in ARTIFACT_EVENTS:
                    self.note_artifact(call)
                elif event == "connect" and call.get("direction") == "BUSINESS_INITIATED":
                    self.outbound.receive_answer(call)
                elif event == "connect" and call.get("direction") == "USER_INITIATED":
                    self.start_inbound(call)
        return counts

    def end(self, call_id):
        self.remember(call_id, "terminated")
        if job := self.jobs.get(call_id):
            job.cancel()

    def note_artifact(self, call):
        """Meta-native recording/transcript is ready (only if it was requested on accept/connect).
        Keep presence metadata only; never the media URL. Fetch through Kapso's API, not here."""
        container, kind = ARTIFACT_EVENTS[call["event"]]
        holder = call.get(container) if isinstance(call.get(container), dict) else {}
        media = holder.get(kind) if isinstance(holder.get(kind), dict) else {}
        notice = {"ref": call_ref(call["id"]), "event": call["event"], "media_present": isinstance(media.get("id"), str),
                  "mime_type": str(media.get("mime_type", ""))[:64]}
        if notice in self.artifacts:
            return  # redelivery
        self.artifacts.append(notice)
        self.log.add("native_artifact_event", notice["ref"], kind=kind)

    def start_inbound(self, call):
        call_id = call["id"]
        if call_id in self.seen:
            return  # duplicate or late connect for a known call
        session = call.get("session") if isinstance(call.get("session"), dict) else {}
        if session.get("sdp_type") != "offer" or not isinstance(session.get("sdp"), str) or not session["sdp"]:
            self.remember(call_id, "invalid")
            self.log.add("ignored_connect_without_offer", call_ref(call_id))
            return
        if not self.settings.calls_ready:
            raise NotReady()
        if self.busy():
            self.remember(call_id, "rejected")
            self.log.add("inbound_rejected_busy", call_ref(call_id))
            self.spawn(self.decline(call_id))
            return
        self.remember(call_id, "starting")
        job = asyncio.create_task(self.answer(call))
        self.jobs[call_id] = job
        self.directions[call_id] = "inbound"
        job.add_done_callback(lambda task, cid=call_id: self.remove_job(cid, task))

    # Inbound lifecycle ---------------------------------------------------------------------

    async def decline(self, call_id):
        client = self.kapso_factory()
        try:
            await client.action(call_id, "reject")
            self.log.add("reject_ok", call_ref(call_id))
        except Exception as error:
            self.log.add("reject_failed_" + type(error).__name__, call_ref(call_id))
        finally:
            await client.close()

    async def answer(self, call):
        call_id, ref = call["id"], call_ref(call["id"])
        client = self.kapso_factory()
        connection = session = None
        accepted = False
        try:
            async with asyncio.timeout(ANSWER_TIMEOUT_SECONDS):
                # Unknown recording state raises here, before pre_accept, so the call is rejected.
                recording = await self.recording.current()
                connection = self.connection_factory(self.settings.ice_servers_json)
                await connection.initialize(sdp=call["session"]["sdp"], type="offer")
                answer = meta_sdp(connection.get_answer()["sdp"])
                # Media is ready and silent; the agent starts only after accept succeeds.
                await client.action(call_id, "pre_accept", answer)
                self.log.add("pre_accept_ok", ref)
                await client.action(call_id, "accept", answer)
                accepted = True
                self.remember(call_id, "accepted")
                self.log.add("accept_ok", ref)
            caller = self.caller_key(call.get("from_user_id") or call.get("from") or call_id)
            session = self.session_factory(connection, self.settings, self.spec, self.store, caller, self.log, ref,
                                           direction="inbound", recording=recording)
            self.sessions[call_id] = session
            await session.start()
            await asyncio.wait_for(asyncio.shield(session.task), timeout=self.settings.max_session_seconds)
            self.log.add("session_finished", ref)
        except asyncio.CancelledError:
            self.log.add("caller_hangup", ref)
            raise
        except TimeoutError:
            self.log.add("call_timed_out" if accepted else "answer_timed_out", ref)
        except Exception as error:
            self.log.add("call_failed_" + type(error).__name__, ref)
        finally:
            try:
                if session:
                    await session.close()
                elif connection:
                    await connection.disconnect()
                if self.seen.get(call_id) != "terminated":
                    action = "terminate" if accepted else "reject"
                    try:
                        await client.action(call_id, action)
                        self.log.add(action + "_ok", ref)
                    except Exception as error:
                        self.log.add(action + "_failed_" + type(error).__name__, ref)
                    self.remember(call_id, "closed")
            finally:
                await client.close()
                self.sessions.pop(call_id, None)

    # Browser test calls (operator only) ----------------------------------------------------

    async def start_browser_call(self, offer_sdp):
        recording = await self.recording.current()  # raises RecordingUnknown; read outside the lock
        async with self.lock:
            if self.busy():
                raise RuntimeError("busy")
            ref = "browser-" + secrets.token_hex(8)
            connection = self.connection_factory(self.settings.ice_servers_json)
            session = None
            try:
                await connection.initialize(sdp=offer_sdp, type="offer")
                # Each browser call is its own caller: it cannot see phone callers' bookings.
                session = self.session_factory(connection, self.settings, self.spec, self.store,
                                               self.caller_key(ref), self.log, ref, direction="inbound",
                                               recording=recording)
                self.sessions[ref] = session
                self.directions[ref] = "browser"
                await session.start()
            except Exception:
                self.sessions.pop(ref, None)
                self.directions.pop(ref, None)
                if session:
                    await session.close()
                else:
                    await connection.disconnect()
                raise
            job = asyncio.create_task(self.watch_browser_call(ref, session))
            self.jobs[ref] = job
            job.add_done_callback(lambda task: self.remove_job(ref, task))
            return ref, connection.get_answer()

    async def watch_browser_call(self, ref, session):
        try:
            await asyncio.wait_for(asyncio.shield(session.task), timeout=self.settings.max_session_seconds)
        except (TimeoutError, asyncio.CancelledError):
            pass
        finally:
            await session.close()
            self.sessions.pop(ref, None)

    async def hang_up(self, ref):
        """Operator hangup by opaque reference (browser or outbound call)."""
        for call_id, job in list(self.jobs.items()):
            if call_ref(call_id) == ref:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                return True
        return False

    def snapshot(self):
        return [{"ref": call_ref(call_id), "direction": self.directions.get(call_id, "unknown"),
                 "state": self.seen.get(call_id, "active")} for call_id in self.jobs]

    async def shutdown(self):
        jobs = list(self.jobs.values()) + list(self.background)
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await asyncio.gather(*(s.close() for s in list(self.sessions.values())), return_exceptions=True)
