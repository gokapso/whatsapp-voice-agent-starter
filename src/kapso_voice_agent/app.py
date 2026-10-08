"""HTTP surface.

Public (expose through your HTTPS ingress):
  GET  /healthz              -> {"ok": true}; no configuration details
  POST /webhooks/whatsapp    -> Kapso-signed Meta Calling webhook (raw-body HMAC, 1 MB cap)

Operator (bearer OPERATOR_TOKEN; returns 404 when the token is unset):
  GET  /operator/                         console page (contains no secrets)
  GET  /operator/api/state                readiness, recording state, active calls, event log, artifact notices
  POST /operator/api/browser-call         {sdp} -> SDP answer; test the agent from a browser
  POST /operator/api/calls/{ref}/hangup   end a browser or outbound call
  POST /operator/api/outbound/permission  {recipient} (needs ENABLE_OUTBOUND=1)
  POST /operator/api/outbound/call        {recipient} (needs ENABLE_OUTBOUND=1)
"""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .agent_config import load_spec
from .bridge import ElevenSession, make_connection
from .calls import CallManager, NotReady
from .config import load_settings
from .events import EventLog
from .outbound import OutboundError
from .private import private_dir, prune_local_artifacts
from .recording import RecordingUnknown

STATIC = Path(__file__).parent / "static"
MAX_WEBHOOK_BYTES = 1_000_000
PRUNE_INTERVAL_SECONDS = 3600
SIGNATURE = re.compile(r"^[0-9a-fA-F]{64}$")
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; media-src 'self' blob: mediastream:; connect-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Offer(Strict):
    sdp: str = Field(min_length=10, max_length=100_000)


class Recipient(Strict):
    recipient: str = Field(min_length=5, max_length=40)


def verify_signature(secret, raw, header):
    if not SIGNATURE.match(header or ""):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.lower())


def prune_once(settings, log):
    try:
        removed = prune_local_artifacts(settings.data_dir, settings.capture_retention_days, settings.capture_max_count)
    except OSError as error:
        log.add("local_prune_failed_" + type(error).__name__)
        return
    if any(removed.values()):
        log.add("local_artifacts_pruned", captures=removed["captures"], vendor=removed["vendor"])


async def prune_periodically(settings, log, interval=PRUNE_INTERVAL_SECONDS):
    """Local retention without waiting for the next call: at startup, then every hour."""
    while True:
        await asyncio.to_thread(prune_once, settings, log)
        await asyncio.sleep(interval)


def open_calendar(settings, spec):
    """The tools' calendar: Cal.com for real bookings, the local SQLite book only when CALENDAR=local
    (development). None when neither is configured; then no call starts (Settings.agent_missing)."""
    if settings.calendar == "calcom":
        from .calcom import CalComCalendar
        return CalComCalendar(settings.data_dir / "calendar.sqlite3", spec.business, settings.cal_api_key)
    if settings.calendar == "local":
        from .store import AppointmentStore
        return AppointmentStore(settings.data_dir / "appointments.sqlite3", spec.business, spec.dev_calendar_path)
    return None


def create_app(settings=None, spec=None, store=None, session_factory=ElevenSession,
               connection_factory=make_connection, kapso_factory=None, recording_reader=None):
    settings = settings or load_settings()
    spec = spec or load_spec(settings.agent_config_path)
    # DATA_DIR holds bookings, captures and provider downloads: 0700 before anything is written.
    private_dir(settings.data_dir)
    store = store or open_calendar(settings, spec)
    log = EventLog()
    manager = CallManager(settings, spec, store, log, session_factory=session_factory,
                          connection_factory=connection_factory, kapso_factory=kapso_factory,
                          recording_reader=recording_reader)

    @asynccontextmanager
    async def lifespan(app):
        pruner = asyncio.create_task(prune_periodically(settings, log))
        if settings.agent_ready:
            manager.spawn(manager.recording.warm())
        try:
            yield
        finally:
            pruner.cancel()
            await asyncio.gather(pruner, return_exceptions=True)
            await manager.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager, app.state.log, app.state.settings = manager, log, settings
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.post("/webhooks/whatsapp")
    async def webhook(request: Request):
        if not settings.webhook_secret:
            raise HTTPException(503, "Webhook secret is not configured")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Webhook too large")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "Webhook too large")
        # Verify the exact bytes before parsing anything.
        if not verify_signature(settings.webhook_secret, bytes(raw), request.headers.get("x-webhook-signature")):
            raise HTTPException(401, "Invalid signature")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "Body is not JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("entry", []), list):
            raise HTTPException(400, "Unexpected webhook shape")
        try:
            counts = await manager.receive(payload)
        except NotReady:
            # Kapso will retry; the call keeps ringing until then or until the caller gives up.
            raise HTTPException(503, "Server is not configured to answer calls") from None
        return counts

    def operator(request: Request):
        if not settings.operator_enabled:
            raise HTTPException(404)
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), settings.operator_token.encode()):
            raise HTTPException(401, "Operator token required", headers={"WWW-Authenticate": "Bearer"})

    def operator_page_enabled():
        if not settings.operator_enabled:
            raise HTTPException(404)

    page = APIRouter(prefix="/operator", dependencies=[Depends(operator_page_enabled)])

    @page.get("/")
    async def console():
        return FileResponse(STATIC / "index.html", headers=PAGE_HEADERS)

    @page.get("/app.js")
    async def console_script():
        return FileResponse(STATIC / "app.js", media_type="text/javascript", headers=PAGE_HEADERS)

    api = APIRouter(prefix="/operator/api", dependencies=[Depends(operator)])

    @api.get("/state")
    async def state():
        return {"calls_ready": settings.calls_ready, "agent_ready": settings.agent_ready,
                "agent_missing": settings.agent_missing(),  # setting names only, never values
                "outbound_enabled": settings.enable_outbound, "local_capture": settings.local_capture,
                "recording": manager.recording.status(),
                "max_concurrent_calls": settings.max_concurrent_calls, "busy": manager.busy(),
                "calls": manager.snapshot(), "events": list(log.events),
                "native_artifact_events": list(manager.artifacts)}

    @api.post("/browser-call")
    async def browser_call(offer: Offer):
        if not settings.agent_ready:
            raise HTTPException(503, "Missing: " + ", ".join(settings.agent_missing()))
        try:
            ref, answer = await manager.start_browser_call(offer.sdp)
        except RecordingUnknown:
            raise HTTPException(503, "Could not read the agent's recording setting; no call started") from None
        except RuntimeError as error:
            if str(error) == "busy":
                raise HTTPException(409, "Already at MAX_CONCURRENT_CALLS") from None
            raise HTTPException(502, "Could not start the voice connection") from None
        except Exception:
            raise HTTPException(502, "Could not start the voice connection") from None
        return {"ref": ref, "type": answer["type"], "sdp": answer["sdp"]}

    @api.post("/calls/{ref}/hangup")
    async def hangup(ref: str):
        if not re.fullmatch(r"(browser|call)-[0-9a-f]{16}", ref):
            raise HTTPException(422, "Invalid call reference")
        if not await manager.hang_up(ref):
            raise HTTPException(404, "No active call with that reference")
        return {"ended": True}

    def outbound_enabled():
        if not settings.enable_outbound:
            raise HTTPException(403, "Outbound calling is off (ENABLE_OUTBOUND=0)")

    @api.post("/outbound/permission", dependencies=[Depends(outbound_enabled)])
    async def permission(body: Recipient):
        try:
            return await manager.outbound.permission(body.recipient)
        except OutboundError as error:
            raise HTTPException(error.status, str(error)) from None
        except Exception as error:
            raise HTTPException(502, f"Permission check failed: {type(error).__name__}") from None

    @api.post("/outbound/call", dependencies=[Depends(outbound_enabled)])
    async def outbound_call(body: Recipient):
        try:
            return await manager.outbound.start(body.recipient)
        except OutboundError as error:
            raise HTTPException(error.status, str(error)) from None

    app.include_router(page)
    app.include_router(api)

    @app.exception_handler(asyncio.TimeoutError)
    async def timeout(request, error):
        return JSONResponse({"detail": "Timed out"}, status_code=504)

    return app
