"""Experimental: hand a live WhatsApp call from the AI to a teammate in Kapso Inbox (HUMAN_HANDOFF=1).

Meta has no transfer action and an answered call cannot renegotiate its SDP, so the call's Meta
leg never changes. This server answers a second WebRTC peer for the teammate's browser and moves
the caller's audio itself:

    Meta leg in -> HandoffInput -> agent bridge -> HandoffOutput -> Meta leg out
                        |                                ^
                        v                                |
                teammate leg out                 teammate leg in

Signaling goes through Kapso's Platform API with KAPSO_API_KEY; Kapso never calls this server.
request -> poll for the browser's SDP offer -> answer -> wait until the teammate's browser reports
ready (it plays this leg's audio and sends its microphone) AND this leg's media arrives here ->
commit at Kapso -> switch. Received packets alone are not readiness: a muted browser track still
sends silence. Speech is never required. Until the commit the AI keeps the caller, and any failure (nobody
joins, a decline, no media, Kapso refuses) ends only the attempt. After the commit the AI provider
session is closed and does not come back: when the teammate's leg ends, the call ends.

Logs hold event names, reasons and the hashed call reference only: no SDP or audio.
"""

import asyncio
import secrets

import av
import httpx
import numpy as np
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.smallwebrtc.transport import RawAudioTrack

from .bridge import media_path

SAMPLE_RATE = 16000
CHUNK_10MS = SAMPLE_RATE // 100 * 2
REQUEST_SECONDS = 65   # Kapso expires an unanswered request after 60 s
JOIN_SECONDS = 25      # Kapso times out a join after 30 s; this server gives up first
ANSWER_SECONDS = 10
POLL_SECONDS = 1.0
COMMITTED_POLL_SECONDS = 2.0
MAX_POLL_FAILURES = 5
COMMIT_ATTEMPTS = 3
LEG_END_CHECKS = 3
# Kapso ends these before the commit; the AI keeps the caller.
UNAVAILABLE = {"ok": False, "code": "handoff_unavailable",
               "message": "No teammate can join right now. Say so and keep helping the caller."}


class HandoffInput(FrameProcessor):
    """Before the agent bridge: after the switch, caller audio goes to the teammate, never the AI."""

    def __init__(self, switch):
        super().__init__()
        self.switch = switch

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if self.switch.human and isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self.switch.caller_audio(frame.audio)
            return
        await self.push_frame(frame, direction)


class HandoffOutput(FrameProcessor):
    """After the agent bridge: after the switch, AI audio is dropped and teammate audio plays."""

    def __init__(self, switch):
        super().__init__()
        self.switch = switch

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if self.switch.human and isinstance(frame, OutputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            self.switch.dropped_agent_frames += 1
            return
        await self.push_frame(frame, direction)

    async def play(self, audio):
        await self.push_frame(OutputAudioRawFrame(audio, SAMPLE_RATE, 1))


class HandoffSwitch:
    """Who hears the caller and who the caller hears: the AI until `human` is set, then the teammate."""

    def __init__(self):
        self.human = False
        self.leg = None
        self.dropped_agent_frames = 0
        self.input = HandoffInput(self)
        self.output = HandoffOutput(self)

    def caller_audio(self, audio):
        if self.leg:
            self.leg.send(audio)


class TeammateLeg:
    """The teammate's browser peer, answered here. 16 kHz mono PCM both ways; audio from the
    teammate is ignored until `sink` is set at the switch."""

    def __init__(self, connection):
        self.connection = connection
        self.track = RawAudioTrack(sample_rate=SAMPLE_RATE)
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        self.connected, self.heard, self.ended = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.pending = bytearray()
        self.sink = None
        self.reader = None
        self.frames_in = self.frames_out = 0

        @connection.event_handler("connected")
        async def connected(_connection):
            self.connected.set()
            if self.reader is None:
                self.reader = asyncio.create_task(self.read())

        async def gone(_connection):
            self.ended.set()

        for name in ("disconnected", "closed", "failed"):
            connection.event_handler(name)(gone)

    async def answer(self, offer_sdp):
        await self.connection.initialize(sdp=offer_sdp, type="offer")
        self.connection.replace_audio_track(self.track)
        await self.connection.connect()  # report "connected" once ICE/DTLS is up
        return self.connection.get_answer()["sdp"]

    async def read(self):
        try:
            track = self.connection.audio_input_track()
            while track is not None:
                frame = await track.recv()
                for chunk in self.resampler.resample(frame):
                    audio = chunk.to_ndarray().astype(np.int16).tobytes()
                    self.frames_in += 1
                    self.heard.set()
                    if self.sink:
                        await self.sink(audio)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dead track ends the leg
            pass
        finally:
            self.ended.set()

    def send(self, audio):
        """Caller audio to the teammate, in the 10 ms units the output track takes."""
        self.pending.extend(audio)
        usable = len(self.pending) - len(self.pending) % CHUNK_10MS
        if usable:
            self.track.add_audio_bytes(bytes(self.pending[:usable]))
            del self.pending[:usable]
            self.frames_out += 1

    async def close(self):
        self.sink = None
        if self.reader and not self.reader.done():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        try:
            await self.connection.disconnect()
        finally:
            self.ended.set()


class CallHandoff:
    """One inbound call's handoff. The session builds the switch into its pipeline and attaches
    itself; the agent's tool or the operator console calls `request`."""

    def __init__(self, settings, kapso_factory, connection_factory, log, ref, call_id):
        self.settings, self.kapso_factory, self.connection_factory = settings, kapso_factory, connection_factory
        self.log, self.ref, self.call_id = log, ref, call_id
        self.switch = HandoffSwitch()
        self.session = None
        self.state = "idle"  # idle | requested | joining | committed | ended
        self.handoff_id = None
        self.leg = None
        self.task = None
        self.committed_at = None
        self.request_seconds, self.join_seconds = REQUEST_SECONDS, JOIN_SECONDS
        self.poll_seconds, self.committed_poll_seconds = POLL_SECONDS, COMMITTED_POLL_SECONDS

    def attach(self, session):
        self.session = session

    # Request -------------------------------------------------------------------------------

    async def request(self):
        """Ask Kapso for a teammate. Returns a tool-style result at once; the rest runs in the
        background while the AI keeps talking."""
        if self.state in ("requested", "joining"):
            return {"ok": True, "status": "already_requested",
                    "message": "A teammate was already asked to join. Keep helping the caller while they wait."}
        if self.state == "committed" or self.session is None:
            return UNAVAILABLE
        key = "handoff-" + secrets.token_hex(12)
        client = self.kapso_factory()
        try:
            # Kapso replays the same request key, so a lost response is safe to send again.
            for attempt in range(3):
                try:
                    status, data = await client.request_handoff(self.call_id, key, self.settings.handoff_stun_urls())
                    break
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5)
        except Exception as error:  # noqa: BLE001 - the AI keeps the caller either way
            self.log.add("handoff_request_failed_" + type(error).__name__, self.ref)
            return UNAVAILABLE
        finally:
            await client.close()
        if status not in (200, 201) or not isinstance(data.get("id"), str):
            self.log.add("handoff_request_refused", self.ref, code=str(data.get("error", status))[:40])
            return UNAVAILABLE
        self.handoff_id, self.state = data["id"], "requested"
        self.log.add("handoff_requested", self.ref)
        self.task = asyncio.create_task(self.run())
        return {"ok": True, "status": "requested",
                "message": "A teammate was asked to join. Tell the caller you are asking a teammate to join and keep "
                           "helping while they wait. Do not say a person is on the line."}

    # Background ----------------------------------------------------------------------------

    async def run(self):
        client = self.kapso_factory()
        try:
            await self.follow(client)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - before the switch, a failure must not end the call
            self.log.add("handoff_failed_" + type(error).__name__, self.ref)
            if self.state == "committed":
                # The AI is gone and nothing watches the teammate's leg: end the call, not silence.
                await self.leave_teammate(client, "bridge_failed")
            else:
                await self.give_up(client, "bridge_failed")
        finally:
            await client.close()

    async def follow(self, client):
        data = await self.poll_until(client, lambda d: d["state"] != "requested", self.request_seconds)
        if data is None:
            return await self.give_up(client, "cancelled")
        if data["state"] != "joining" or not data.get("offer_sdp"):
            return await self.fall_back(data.get("end_reason") or data["state"])

        self.state = "joining"
        self.log.add("handoff_joining", self.ref)
        self.leg = TeammateLeg(self.connection_factory(self.settings.handoff_ice_json()))
        try:
            answer = await asyncio.wait_for(self.leg.answer(data["offer_sdp"]), ANSWER_SECONDS)
        except Exception:  # noqa: BLE001 - an unusable offer or ICE setup failure
            return await self.give_up(client, "bridge_failed")
        status, data = await client.answer_handoff(self.handoff_id, answer)
        if status != 200:
            return await self.fall_back(data.get("end_reason") or data.get("error", "refused"))

        if not await self.wait_until_ready(client):
            return
        if not await self.commit(client):
            return
        await self.take_over()
        await self.stay_with_teammate(client)

    async def poll_until(self, client, done, seconds):
        """Kapso's view of the handoff until `done` or `seconds` pass (None). Tolerates a few failed reads."""
        loop = asyncio.get_running_loop()
        deadline, failures = loop.time() + seconds, 0
        while loop.time() < deadline:
            data = await self.read(client)
            if data is None:
                failures += 1
                if failures >= MAX_POLL_FAILURES:
                    return None
            elif data["state"] == "ended" or done(data):
                return data
            else:
                failures = 0
            await asyncio.sleep(self.poll_seconds)
        return None

    async def read(self, client):
        try:
            status, data = await client.handoff(self.handoff_id)
        except Exception as error:  # noqa: BLE001
            self.log.add("handoff_poll_failed_" + type(error).__name__, self.ref)
            return None
        if status != 200 or not isinstance(data.get("state"), str):
            self.log.add("handoff_poll_refused", self.ref, http_status=status)
            return None
        return data

    async def wait_until_ready(self, client):
        """Both, in any order: this leg's media arrives here (any RTP, silence included), and the
        browser reported through Kapso that it plays this leg's audio with its microphone on."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.join_seconds
        browser_ready = media_logged = False
        while True:
            if self.leg.heard.is_set() and not media_logged:
                media_logged = True
                self.log.add("handoff_media_ready", self.ref, media=media_path(self.leg.connection))
            if browser_ready and self.leg.heard.is_set():
                return True
            if self.leg.ended.is_set():
                await self.give_up(client, "bridge_failed")
                return False
            if loop.time() >= deadline:
                await self.give_up(client, "join_timeout")
                return False
            data = await self.read(client)
            if data and data["state"] == "ended":
                await self.fall_back(data.get("end_reason") or "ended")
                return False
            if not browser_ready and data and data.get("browser_ready") is True:
                browser_ready = True
                self.log.add("handoff_browser_ready", self.ref)
                continue
            await asyncio.sleep(self.poll_seconds)

    async def commit(self, client):
        """Kapso decides whether the teammate still has the call; only then does audio move."""
        for attempt in range(COMMIT_ATTEMPTS):
            try:
                status, data = await client.commit_handoff(self.handoff_id)
            except httpx.TransportError as error:
                self.log.add("handoff_commit_failed_" + type(error).__name__, self.ref)
                if attempt + 1 < COMMIT_ATTEMPTS:
                    await asyncio.sleep(0.5)
                    continue
                await self.give_up(client, "bridge_failed")
                return False
            if status == 200 and data.get("state") == "committed":
                return True
            await self.fall_back(data.get("end_reason") or data.get("error", "refused"))
            return False
        return False

    async def take_over(self):
        """The switch: caller audio to the teammate only, queued AI audio flushed, provider closed.
        The Meta leg, its call ID and its talk time are untouched. The routing changes first, so a
        failed flush or provider close cannot leave the caller with silence or with the AI."""
        self.switch.leg = self.leg
        self.switch.human = True
        self.leg.sink = self.switch.output.play
        self.state = "committed"
        self.committed_at = asyncio.get_running_loop().time()
        self.log.add("handoff_committed", self.ref)
        for step, action in (("flush", self.switch.output.broadcast_interruption),
                             ("ai_close", self.session.bridge.stop_socket)):
            try:
                await action()
            except Exception as error:  # noqa: BLE001 - the switch above already silences the AI both ways
                self.log.add(f"handoff_{step}_failed_" + type(error).__name__, self.ref)

    async def stay_with_teammate(self, client):
        """Until the teammate's leg ends or Kapso reports the handoff ended (Hang up, call end).
        The AI does not come back: the call then ends."""
        reason = None
        while reason is None:
            try:
                await asyncio.wait_for(self.leg.ended.wait(), self.committed_poll_seconds)
                reason = await self.leg_end_reason(client)
            except TimeoutError:
                data = await self.read(client)
                if data and data["state"] == "ended":
                    reason = data.get("end_reason") or "ended"
        await self.leave_teammate(client, reason)

    async def leave_teammate(self, client, reason):
        """After the switch the AI does not come back: the session ends and the call manager sends
        terminate. Reasons that Kapso did not report itself are reported here."""
        self.log.add("handoff_teammate_left", self.ref, reason=str(reason)[:40])
        if reason in ("human_disconnected", "bridge_failed"):
            await self.report_end(client, reason)
        self.state = "ended"
        await self.leg.close()
        await self.session.runner.cancel()

    async def leg_end_reason(self, client):
        """The teammate's leg closed. A Hang up in Inbox closes it too, and its request may land at
        Kapso a moment later: check briefly before calling it a dropped connection."""
        for _ in range(LEG_END_CHECKS):
            data = await self.read(client)
            if data and data["state"] == "ended":
                return data.get("end_reason") or "ended"
            await asyncio.sleep(self.poll_seconds)
        return "human_disconnected"

    # Endings -------------------------------------------------------------------------------

    async def give_up(self, client, reason):
        await self.report_end(client, reason)
        await self.fall_back(reason)

    async def report_end(self, client, reason):
        try:
            await client.end_handoff(self.handoff_id, reason)
        except Exception as error:  # noqa: BLE001 - Kapso also expires the attempt by itself
            self.log.add("handoff_end_report_failed_" + type(error).__name__, self.ref)

    async def fall_back(self, reason):
        """Before the commit: the AI keeps the caller."""
        self.state = "ended"
        self.log.add("handoff_fallback", self.ref, reason=str(reason)[:40])
        if self.leg:
            await self.leg.close()
        announce = getattr(self.session.bridge, "handoff_unavailable", None) if self.session else None
        if announce and reason != "call_ended":
            await announce()

    async def close(self):
        """The call is ending: stop the attempt and the teammate's leg."""
        if self.task and not self.task.done() and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.state in ("requested", "joining") and self.handoff_id:
            client = self.kapso_factory()
            try:
                await self.report_end(client, "cancelled")
            finally:
                await client.close()
        if self.leg:
            await self.leg.close()
        self.state = "ended" if self.state != "idle" else "idle"
