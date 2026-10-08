"""Which recording notice is true for a call.

The greeting and the agent's `recording_status` must describe what is actually configured, not
what agent.toml intends. So before any real agent session starts, the server reads the stored
ElevenLabs agent back (read-only GET) and uses its `platform_settings.privacy.record_voice`,
plus LOCAL_CAPTURE on this server:

- provider recording on, or local capture on  -> "This call is recorded."
- both off                                   -> no recording sentence
- provider setting unknown (read failed, field missing or not a boolean) -> no session starts.
  Inbound calls are declined with `reject`, browser and outbound calls get HTTP 503. The server
  never guesses "recorded" or "not recorded".

A successful read is reused for CHECK_TTL_SECONDS, then read again; a failed read is never
reused. A provider-side change (for example `agent apply --yes` from another shell) therefore
reaches the greeting within that time, or at once after a restart.

The offline fake agent (DEV_AGENT_WS_URL) records nothing and is never read over the network.
"Recording on" means the setting is enabled. It does not prove that bytes were saved.
"""

import asyncio
from dataclasses import dataclass
import time
from urllib.parse import quote

import httpx

from .provider import ELEVENLABS_AGENTS

READ_TIMEOUT_SECONDS = 5
CHECK_TTL_SECONDS = 60


class RecordingUnknown(Exception):
    """The provider's recording setting could not be established; no agent session may start."""


@dataclass(frozen=True)
class Recording:
    provider: bool      # ElevenLabs record_voice as read back; False for the offline fake agent
    local: bool         # LOCAL_CAPTURE on this server
    source: str         # "provider_readback" or "offline_fake_agent"

    @property
    def recorded(self):
        return self.provider or self.local


async def read_provider_recording(settings, transport=None):
    """Read-only: GET the stored agent and return its record_voice flag."""
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_SECONDS, follow_redirects=False, transport=transport) as client:
        response = await client.get(f"{ELEVENLABS_AGENTS}/{quote(settings.elevenlabs_agent_id, safe='')}",
                                    headers={"xi-api-key": settings.elevenlabs_api_key})
    if response.status_code != 200:
        raise RecordingUnknown(f"Agent read HTTP {response.status_code}")
    privacy = (response.json().get("platform_settings") or {}).get("privacy") or {}
    value = privacy.get("record_voice")
    if not isinstance(value, bool):
        raise RecordingUnknown("The stored agent has no boolean platform_settings.privacy.record_voice")
    return value


class RecordingCheck:
    """The single owner of the recording decision for this process."""

    def __init__(self, settings, spec, log, reader=None, clock=time.monotonic):
        self.settings, self.spec, self.log = settings, spec, log
        self.reader, self.clock = reader, clock
        self.lock = asyncio.Lock()
        self.cached = None
        self.checked_at = None
        self.last_error = None

    async def current(self):
        """The recording state for a session about to start. Raises RecordingUnknown."""
        settings = self.settings
        if settings.dev_agent_ws_url:
            return Recording(provider=False, local=settings.local_capture, source="offline_fake_agent")
        async with self.lock:
            if self.cached and self.clock() - self.checked_at < CHECK_TTL_SECONDS:
                return self.cached
            self.cached = None
            reader = self.reader or read_provider_recording
            try:
                provider = await asyncio.wait_for(reader(settings), timeout=READ_TIMEOUT_SECONDS + 1)
                if not isinstance(provider, bool):
                    raise RecordingUnknown("Recording setting is not a boolean")
            except Exception as error:
                self.last_error = type(error).__name__
                self.log.add("recording_check_failed_" + type(error).__name__.lower())
                raise RecordingUnknown("Could not read the agent's recording setting") from None
            if provider != self.spec.record_audio:
                # The stored agent differs from agent.toml. The notice follows the stored agent.
                self.log.add("provider_recording_differs_from_agent_toml", provider_record_voice=provider)
            self.last_error = None
            self.cached = Recording(provider=provider, local=settings.local_capture, source="provider_readback")
            self.checked_at = self.clock()
            return self.cached

    async def warm(self):
        """Startup read so the operator status is known before the first call. Never raises."""
        try:
            await self.current()
        except RecordingUnknown:
            pass

    def status(self):
        """Operator view, without network access."""
        settings = self.settings
        if settings.dev_agent_ws_url:
            provider = "off (offline fake agent)"
        elif self.cached and self.clock() - self.checked_at < CHECK_TTL_SECONDS:
            provider = "on" if self.cached.provider else "off"
        elif self.last_error:
            provider = "unknown"
        else:
            provider = "not_checked"
        if provider.startswith("off"):
            notice = settings.local_capture
        elif provider == "on":
            notice = True
        else:
            notice = None  # decided by the next read; no session starts while it is unknown
        return {"provider_recording": provider, "local_capture": settings.local_capture,
                "agent_toml_record_audio": self.spec.record_audio, "next_greeting_says_recorded": notice,
                "last_check_error": self.last_error}
