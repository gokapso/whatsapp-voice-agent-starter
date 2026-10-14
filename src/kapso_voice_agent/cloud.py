"""Pipecat Cloud entry point: Kapso starts one Cloud session per WhatsApp call.

Kapso's dashboard Pipecat provider receives Meta's Calling webhook, claims the call for the voice
agent assigned to the number, and starts a Pipecat Cloud session
(POST /v1/public/{agent}/start, createDailyRoom false) with this body:

    {"kind": "whatsapp_connect", "webhook": <Meta calls webhook>, "whatsapp_token": ...,
     "phone_number_id": ..., "context": {...}}

This module answers that call with the same code as the self-hosted server: CallManager
(accept before the agent, reject/terminate rules), the voice provider bridge and ToolRunner.

Rules it adds (each has a test in tests/test_cloud_launch.py):
- The session body is not signed. Before answering, the call is looked up through Kapso's API
  with KAPSO_API_KEY. It must exist on this number, be user-initiated, be claimed for a voice agent
  (`session_started_at`) and not be over. Otherwise nothing is answered.
- The caller identity comes from Kapso's saved call (BSUID, else phone), not from the body. A body
  whose identity disagrees with Kapso's is refused.
- `whatsapp_token` is never read. Call actions go through Kapso with KAPSO_API_KEY, so Kapso's
  call log and Inbox state stay correct.
- `context` (conversation history, contact notes) is not passed to the agent: the starter's
  prompt and tools do not use it.
"""

import asyncio

from .agent_config import load_spec
from .app import open_calendar
from .bridge import ElevenSession, make_connection
from .calls import CallManager, valid_id
from .config import load_settings
from .events import EventLog, call_ref
from .private import private_dir

LAUNCH_KIND = "whatsapp_connect"


def inbound_connect(webhook, phone_number_id):
    """The single user-initiated `connect` call in a Meta calls webhook for this number, or None."""
    found = []
    for entry in webhook.get("entry") or [] if isinstance(webhook, dict) else []:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict) or change.get("field") != "calls":
                continue
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            if str(metadata.get("phone_number_id", "")) != phone_number_id:
                continue
            for call in value.get("calls") if isinstance(value.get("calls"), list) else []:
                if (isinstance(call, dict) and valid_id(call.get("id")) and call.get("event") == "connect"
                        and call.get("direction") == "USER_INITIATED"):
                    found.append(call)
    return found[0] if len(found) == 1 else None


def verified_identity(call, record):
    """Kapso's identity for the caller, or None if the record does not vouch for this call."""
    if not isinstance(record, dict) or record.get("call_id") != call["id"]:
        return None
    if str(record.get("direction", "")).upper() not in ("USER_INITIATED", "INBOUND"):
        return None
    if not record.get("session_started_at") or record.get("ended_at"):
        return None  # not launched for a voice agent, or already over
    kapso = {"from_user_id": record.get("business_scoped_user_id") or "", "from": record.get("user_wa_id") or ""}
    if not any(kapso.values()):
        return None
    for field, value in kapso.items():
        claimed = call.get(field) or ""
        if claimed and value and claimed != value:
            return None
    return kapso


async def answer_launch(body, settings, spec, store, log, *, kapso_factory=None, session_factory=ElevenSession,
                        connection_factory=make_connection, recording_reader=None):
    """Answer the call in one Kapso launch body. Returns a short outcome name for the session log."""
    if not isinstance(body, dict) or body.get("kind") != LAUNCH_KIND:
        return "ignored_kind"
    if str(body.get("phone_number_id", "")) != settings.phone_number_id:
        return "ignored_other_phone_number"
    if not settings.answer_ready:
        return "not_configured"
    call = inbound_connect(body.get("webhook"), settings.phone_number_id)
    if call is None:
        return "no_inbound_connect"
    ref = call_ref(call["id"])
    manager = CallManager(settings, spec, store, log, session_factory=session_factory,
                          connection_factory=connection_factory, kapso_factory=kapso_factory,
                          recording_reader=recording_reader)
    client = manager.kapso_factory()
    try:
        record = await client.find_call(call["id"])
    except Exception as error:
        log.add("launch_lookup_failed_" + type(error).__name__, ref)
        return "unverified"
    finally:
        await client.close()
    identity = verified_identity(call, record)
    if identity is None:
        log.add("launch_refused_unverified", ref)
        return "unverified"
    call = {key: value for key, value in call.items() if key not in ("from", "from_user_id")} | identity
    payload = {"entry": [{"changes": [{"field": "calls", "value": {
        "metadata": {"phone_number_id": settings.phone_number_id}, "calls": [call]}}]}]}
    try:
        await manager.receive(payload)
        job = manager.jobs.get(call["id"])
        if job is None:
            return "not_started"
        await asyncio.gather(job, return_exceptions=True)
        return "finished"
    finally:
        # A Pipecat Cloud session limit cancels bot(); answer() still sends terminate on its way out.
        await manager.shutdown()


async def run(body):
    """`bot()` body for examples/pipecat-cloud: settings from the session's secrets, one call, then exit."""
    settings = load_settings()
    spec = load_spec(settings.agent_config_path)
    private_dir(settings.data_dir)
    log = EventLog()
    outcome = await answer_launch(body, settings, spec, open_calendar(settings, spec), log)
    # Session logs hold event names and hashed call references only, like the operator log.
    print("kapso-voice-agent", outcome, " ".join(e["event"] for e in log.events), flush=True)
    return outcome
