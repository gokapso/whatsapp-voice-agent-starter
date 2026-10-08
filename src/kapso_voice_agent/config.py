"""Runtime settings from the environment (and an optional .env file). No defaults are secrets."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_TOKEN_LENGTH = 32


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
    operator_token: str = ""
    caller_key_secret: str = ""
    ice_servers_json: str = "[]"
    max_session_seconds: int = 200
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
    def calls_ready(self):
        """Everything needed to answer a WhatsApp call."""
        return bool(self.kapso_api_key and self.phone_number_id and self.webhook_secret and self.agent_ready)

    @property
    def agent_ready(self):
        """An agent session can start: an agent to talk to and a key for caller identities."""
        return not self.agent_missing()

    def agent_missing(self):
        missing = []
        if not (self.dev_agent_ws_url or (self.elevenlabs_api_key and self.elevenlabs_agent_id)):
            missing.append("ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID (or DEV_AGENT_WS_URL)")
        if not (self.caller_key_secret or self.webhook_secret):
            missing.append("CALLER_KEY_SECRET (or WHATSAPP_WEBHOOK_SECRET)")
        return missing

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

    data_dir = Path(get("DATA_DIR") or REPO_ROOT / "data")
    agent_config = Path(get("AGENT_CONFIG_PATH") or REPO_ROOT / "agent/agent.toml")
    settings = Settings(
        kapso_api_key=get("KAPSO_API_KEY"),
        phone_number_id=get("WHATSAPP_PHONE_NUMBER_ID"),
        webhook_secret=get("WHATSAPP_WEBHOOK_SECRET"),
        meta_graph_version=get("META_GRAPH_VERSION", "v24.0"),
        elevenlabs_api_key=get("ELEVENLABS_API_KEY"),
        elevenlabs_agent_id=get("ELEVENLABS_AGENT_ID"),
        operator_token=operator_token,
        caller_key_secret=get("CALLER_KEY_SECRET"),
        ice_servers_json=ice,
        max_session_seconds=_int(get("MAX_SESSION_SECONDS", "200"), "MAX_SESSION_SECONDS", 30, 3600),
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
    if settings.meta_graph_version[:1] != "v" or not settings.meta_graph_version[1:].replace(".", "").isdigit():
        raise ConfigError("META_GRAPH_VERSION must look like v24.0")
    if settings.phone_number_id and not settings.phone_number_id.isdigit():
        raise ConfigError("WHATSAPP_PHONE_NUMBER_ID is Meta's numeric phone number ID, not the display number")
    return settings
