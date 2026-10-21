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
    cal_api_key: str = ""
    # "calcom" (real bookings), "local" (offline development calendar) or "" (none: no calls start).
    calendar: str = ""
    operator_token: str = ""
    caller_key_secret: str = ""
    ice_servers_json: str = "[]"
    # KAPSO_REGISTER_WEBHOOK=1: at startup, point the number's Meta webhook at this server when the
    # number has none (app.register_webhook). Needs the host's public name: RAILWAY_PUBLIC_DOMAIN.
    register_webhook: bool = False
    public_domain: str = ""
    max_session_seconds: int = 320
    max_concurrent_calls: int = 1
    local_capture: bool = False
    capture_retention_days: int = 7
    capture_max_count: int = 20
    data_dir: Path = REPO_ROOT / "data"
    agent_config_path: Path = REPO_ROOT / "agent/agent.toml"
    allowed_hosts: tuple = ()
    dev_agent_ws_url: str = ""
    # Experimental (handoff.py): an Inbox teammate can take over a live WhatsApp call. Off unless
    # HUMAN_HANDOFF=1, and Kapso enables it per project separately.
    human_handoff: bool = False
    # ICE servers for the teammate's browser leg; defaults to ICE_SERVERS_JSON. A host that cannot
    # receive inbound UDP (Railway) needs a TURN server here.
    handoff_ice_servers_json: str = ""
    handoff_max_seconds: int = 1800
    cloudflare_turn_key_id: str = ""
    cloudflare_turn_api_token: str = field(default="", repr=False)
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

    def handoff_problem(self):
        """Why an Inbox teammate cannot take over calls here, or None when they can."""
        if not self.human_handoff:
            return "off"
        if not (self.kapso_api_key and self.phone_number_id):
            return "kapso_missing"
        if self.local_capture:
            return "local_capture"  # local capture records AI calls only
        if bool(self.cloudflare_turn_key_id) != bool(self.cloudflare_turn_api_token):
            return "cloudflare_turn_missing"
        if self.public_domain and not self.cloudflare_turn_ready and not any(
                url.startswith(("turn:", "turns:")) for url in self.handoff_ice_urls()):
            return "turn_required"  # Railway receives no inbound UDP from browsers
        return None

    @property
    def handoff_ready(self):
        return self.handoff_problem() is None

    @property
    def cloudflare_turn_ready(self):
        return bool(self.cloudflare_turn_key_id and self.cloudflare_turn_api_token)

    def handoff_ice_json(self):
        return self.handoff_ice_servers_json or self.ice_servers_json

    def handoff_ice_urls(self):
        urls = []
        for server in json.loads(self.handoff_ice_json()):
            value = server.get("urls")
            urls.extend(value if isinstance(value, list) else [value])
        return [url for url in urls if isinstance(url, str)]

    def handoff_stun_urls(self):
        """STUN URLs (never credentials) the teammate's browser uses to reach this server's media."""
        urls = [url for url in self.handoff_ice_urls() if url.startswith("stun:")][:2]
        return urls or (["stun:stun.cloudflare.com:3478"] if self.cloudflare_turn_ready else [])

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
    handoff_ice = get("HANDOFF_ICE_SERVERS_JSON")
    for name, value in (("ICE_SERVERS_JSON", ice), ("HANDOFF_ICE_SERVERS_JSON", handoff_ice or "[]")):
        try:
            servers = json.loads(value)
            if not isinstance(servers, list) or not all(isinstance(s, dict) and s.get("urls") for s in servers):
                raise ValueError
        except ValueError:
            raise ConfigError(f'{name} must be a JSON list like [{{"urls": "stun:stun.example:3478"}}]') from None

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
        cal_api_key=cal_api_key,
        calendar=calendar,
        operator_token=operator_token,
        caller_key_secret=get("CALLER_KEY_SECRET"),
        ice_servers_json=ice,
        register_webhook=_flag(get("KAPSO_REGISTER_WEBHOOK", "0"), "KAPSO_REGISTER_WEBHOOK"),
        public_domain=get("RAILWAY_PUBLIC_DOMAIN"),
        max_session_seconds=_int(get("MAX_SESSION_SECONDS", "320"), "MAX_SESSION_SECONDS", 30, 3600),
        max_concurrent_calls=_int(get("MAX_CONCURRENT_CALLS", "1"), "MAX_CONCURRENT_CALLS", 1, 20),
        local_capture=_flag(get("LOCAL_CAPTURE", "0"), "LOCAL_CAPTURE"),
        capture_retention_days=_int(get("CAPTURE_RETENTION_DAYS", "7"), "CAPTURE_RETENTION_DAYS", 1, 90),
        capture_max_count=_int(get("CAPTURE_MAX_COUNT", "20"), "CAPTURE_MAX_COUNT", 1, 1000),
        data_dir=data_dir if data_dir.is_absolute() else REPO_ROOT / data_dir,
        agent_config_path=agent_config if agent_config.is_absolute() else REPO_ROOT / agent_config,
        allowed_hosts=tuple(h.strip() for h in get("ALLOWED_HOSTS").split(",") if h.strip()),
        dev_agent_ws_url=dev_url,
        human_handoff=_flag(get("HUMAN_HANDOFF", "0"), "HUMAN_HANDOFF"),
        handoff_ice_servers_json=handoff_ice,
        cloudflare_turn_key_id=get("CLOUDFLARE_TURN_KEY_ID"),
        cloudflare_turn_api_token=get("CLOUDFLARE_TURN_API_TOKEN"),
        handoff_max_seconds=_int(get("HANDOFF_MAX_SECONDS", "1800"), "HANDOFF_MAX_SECONDS", 60, 14400),
    )
    with_port = [h for h in settings.allowed_hosts if not h.startswith("[") and ":" in h]
    if with_port:
        # The Host check compares names only (Starlette drops the port), so "host:8080" never matches.
        raise ConfigError("ALLOWED_HOSTS takes host names without ports, e.g. voice.example.com,127.0.0.1 (got "
                          + ", ".join(with_port) + ")")
    if settings.meta_graph_version[:1] != "v" or not settings.meta_graph_version[1:].replace(".", "").isdigit():
        raise ConfigError("META_GRAPH_VERSION must look like v24.0")
    if settings.phone_number_id and not settings.phone_number_id.isdigit():
        raise ConfigError("WHATSAPP_PHONE_NUMBER_ID is Meta's numeric phone number ID, not the display number")
    return settings
