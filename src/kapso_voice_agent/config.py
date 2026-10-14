"""Runtime settings from the environment (and an optional .env file). No defaults are secrets."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_TOKEN_LENGTH = 32
# Who runs the conversation: ElevenLabs Agents (default) or xAI Grok Voice (speech to speech).
VOICE_PROVIDERS = ("elevenlabs", "grok")


class ConfigError(ValueError):
    pass


def _flag(value, name):
    if value in ("", "0", "false", "no"):
        return False
    if value in ("1", "true", "yes"):
        return True
    raise ConfigError(f"{name} must be 0 or 1")


def _int(value, name, low, high):
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


@dataclass(frozen=True)
class Settings:
    kapso_api_key: str = ""
    phone_number_id: str = ""
    webhook_secret: str = ""
    meta_graph_version: str = "v24.0"
    elevenlabs_api_key: str = ""
    elevenlabs_agent_id: str = ""
    voice_provider: str = "elevenlabs"
    xai_api_key: str = ""
    # Standard Webhooks secret (whsec_...) from POST /v2/phone-numbers; turns on /webhooks/xai-sip.
    xai_sip_webhook_secret: str = ""
    cal_api_key: str = ""
    # "calcom" (real bookings), "local" (offline development calendar) or "" (none: no calls start).
    calendar: str = ""
    operator_token: str = ""
    caller_key_secret: str = ""
    ice_servers_json: str = "[]"
    # "" (ICE_SERVERS_JSON as is) or "cloudflare" (per-call TURN credentials; see turn.py)
    turn_provider: str = ""
    cloudflare_turn_key_id: str = ""
    cloudflare_turn_api_token: str = ""
    turn_transport: str = "tls"
    max_session_seconds: int = 320
    max_concurrent_calls: int = 1
    enable_outbound: bool = False
    local_capture: bool = False
    capture_retention_days: int = 7
    capture_max_count: int = 20
    data_dir: Path = REPO_ROOT / "data"
    agent_config_path: Path = REPO_ROOT / "agent/agent.toml"
    allowed_hosts: tuple = ()
    dev_agent_ws_url: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def answer_ready(self):
        """A call can be answered and its agent started: Kapso call actions and an agent session."""
        return bool(self.kapso_api_key and self.phone_number_id and self.agent_ready)

    @property
    def calls_ready(self):
        """Everything this server needs to receive and answer WhatsApp calls: answer_ready plus the
        webhook secret. The Pipecat Cloud entry point (cloud.py) has no webhook and needs answer_ready only."""
        return bool(self.answer_ready and self.webhook_secret)

    @property
    def agent_ready(self):
        """An agent session can start: an agent to talk to and a key for caller identities."""
        return not self.agent_missing()

    def agent_missing(self):
        missing = []
        if self.voice_provider == "grok":
            if not (self.dev_agent_ws_url or self.xai_api_key):
                missing.append("XAI_API_KEY (or DEV_AGENT_WS_URL)")
        elif not (self.dev_agent_ws_url or (self.elevenlabs_api_key and self.elevenlabs_agent_id)):
            missing.append("ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID (or DEV_AGENT_WS_URL)")
        if not (self.caller_key_secret or self.webhook_secret):
            missing.append("CALLER_KEY_SECRET (or WHATSAPP_WEBHOOK_SECRET)")
        if not self.calendar:
            missing.append("CAL_API_KEY (or CALENDAR=local for offline development)")
        return missing

    @property
    def xai_sip_ready(self):
        """xAI Direct SIP calls can be controlled: the xAI key, a caller key secret and a calendar."""
        return bool(self.xai_api_key and (self.caller_key_secret or self.webhook_secret) and self.calendar)

    @property
    def operator_enabled(self):
        return len(self.operator_token) >= MIN_TOKEN_LENGTH

    def caller_secret(self):
        secret = self.caller_key_secret or self.webhook_secret
        if not secret:
            raise ConfigError("Set CALLER_KEY_SECRET or WHATSAPP_WEBHOOK_SECRET to key caller identities")
        return secret


def load_settings(environ=None, env_file=None):
    """Process environment wins over the .env file. Unknown variables are ignored."""
    values = {}
    path = Path(env_file) if env_file else REPO_ROOT / ".env"
    if environ is None:
        if path.exists():
            values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
        environ = os.environ
    values.update(environ)
    get = lambda name, default="": values.get(name, default).strip()  # noqa: E731

    ice = get("ICE_SERVERS_JSON", "[]")
    try:
        servers = json.loads(ice)
        if not isinstance(servers, list) or not all(isinstance(s, dict) and s.get("urls") for s in servers):
            raise ValueError
    except ValueError:
        raise ConfigError('ICE_SERVERS_JSON must be a JSON list like [{"urls": "stun:stun.example:3478"}]') from None

    dev_url = get("DEV_AGENT_WS_URL")
    if dev_url and urlparse(dev_url).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("DEV_AGENT_WS_URL must point at a loopback address; it is for offline testing only")

    operator_token = get("OPERATOR_TOKEN")
    if operator_token and len(operator_token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"OPERATOR_TOKEN must be at least {MIN_TOKEN_LENGTH} characters, or empty to disable operator routes")

    cal_api_key = get("CAL_API_KEY")
    calendar = get("CALENDAR") or ("calcom" if cal_api_key else "")
    if calendar not in ("", "calcom", "local"):
        raise ConfigError("CALENDAR must be calcom, local (offline development only) or empty")
    if calendar == "calcom" and not cal_api_key:
        raise ConfigError("CALENDAR=calcom needs CAL_API_KEY")

    turn_provider = get("TURN_PROVIDER").lower()
    if turn_provider not in ("", "cloudflare"):
        raise ConfigError("TURN_PROVIDER must be cloudflare or empty")
    if turn_provider and not (get("CLOUDFLARE_TURN_KEY_ID") and get("CLOUDFLARE_TURN_API_TOKEN")):
        raise ConfigError("TURN_PROVIDER=cloudflare needs CLOUDFLARE_TURN_KEY_ID and CLOUDFLARE_TURN_API_TOKEN")
    turn_transport = get("TURN_TRANSPORT").lower() or "tls"
    if turn_transport not in ("tls", "tcp", "udp"):
        raise ConfigError("TURN_TRANSPORT must be tls (default), tcp or udp")

    voice_provider = get("VOICE_PROVIDER").lower() or "elevenlabs"
    if voice_provider not in VOICE_PROVIDERS:
        raise ConfigError("VOICE_PROVIDER must be elevenlabs (default) or grok")

    data_dir = Path(get("DATA_DIR") or REPO_ROOT / "data")
    agent_config = Path(get("AGENT_CONFIG_PATH") or REPO_ROOT / "agent/agent.toml")
    settings = Settings(
        kapso_api_key=get("KAPSO_API_KEY"),
        phone_number_id=get("WHATSAPP_PHONE_NUMBER_ID"),
        webhook_secret=get("WHATSAPP_WEBHOOK_SECRET"),
        meta_graph_version=get("META_GRAPH_VERSION", "v24.0"),
        elevenlabs_api_key=get("ELEVENLABS_API_KEY"),
        elevenlabs_agent_id=get("ELEVENLABS_AGENT_ID"),
        voice_provider=voice_provider,
        xai_api_key=get("XAI_API_KEY"),
        xai_sip_webhook_secret=get("XAI_SIP_WEBHOOK_SECRET"),
        cal_api_key=cal_api_key,
        calendar=calendar,
        operator_token=operator_token,
        caller_key_secret=get("CALLER_KEY_SECRET"),
        ice_servers_json=ice,
        turn_provider=turn_provider,
        cloudflare_turn_key_id=get("CLOUDFLARE_TURN_KEY_ID"),
        cloudflare_turn_api_token=get("CLOUDFLARE_TURN_API_TOKEN"),
        turn_transport=turn_transport,
        max_session_seconds=_int(get("MAX_SESSION_SECONDS", "320"), "MAX_SESSION_SECONDS", 30, 3600),
        max_concurrent_calls=_int(get("MAX_CONCURRENT_CALLS", "1"), "MAX_CONCURRENT_CALLS", 1, 20),
        enable_outbound=_flag(get("ENABLE_OUTBOUND", "0"), "ENABLE_OUTBOUND"),
        local_capture=_flag(get("LOCAL_CAPTURE", "0"), "LOCAL_CAPTURE"),
        capture_retention_days=_int(get("CAPTURE_RETENTION_DAYS", "7"), "CAPTURE_RETENTION_DAYS", 1, 90),
        capture_max_count=_int(get("CAPTURE_MAX_COUNT", "20"), "CAPTURE_MAX_COUNT", 1, 1000),
        data_dir=data_dir if data_dir.is_absolute() else REPO_ROOT / data_dir,
        agent_config_path=agent_config if agent_config.is_absolute() else REPO_ROOT / agent_config,
        allowed_hosts=tuple(h.strip() for h in get("ALLOWED_HOSTS").split(",") if h.strip()),
        dev_agent_ws_url=dev_url,
    )
    with_port = [h for h in settings.allowed_hosts if not h.startswith("[") and ":" in h]
    if with_port:
        # The Host check compares names only (Starlette drops the port), so "host:8080" never matches.
        raise ConfigError("ALLOWED_HOSTS takes host names without ports, e.g. voice.example.com,127.0.0.1 (got "
                          + ", ".join(with_port) + ")")
    if settings.xai_sip_webhook_secret and not settings.xai_sip_webhook_secret.startswith("whsec_"):
        raise ConfigError("XAI_SIP_WEBHOOK_SECRET is the whsec_... signing secret xAI returns when you register the number")
    if settings.meta_graph_version[:1] != "v" or not settings.meta_graph_version[1:].replace(".", "").isdigit():
        raise ConfigError("META_GRAPH_VERSION must look like v24.0")
    if settings.phone_number_id and not settings.phone_number_id.isdigit():
        raise ConfigError("WHATSAPP_PHONE_NUMBER_ID is Meta's numeric phone number ID, not the display number")
    return settings
