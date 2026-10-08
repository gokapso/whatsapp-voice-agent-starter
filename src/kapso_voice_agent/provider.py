"""Provider setup. Every command is a dry run unless the operator passes --yes.

Dry runs make no network requests: they render what would be sent, with secrets redacted.
Apply paths refuse to overwrite things they did not create:
- ElevenLabs: create a new agent when ELEVENLABS_AGENT_ID is empty; otherwise update it only if
  its stored name equals agent.toml's name. Every apply ends with a read-back comparison.
- Kapso: create a phone-number webhook with kind "meta" only when the number has none. Only one
  Meta webhook is allowed per number, and replacing one would move other Meta events too.
"""

import json
from urllib.parse import quote, urlparse

import httpx

from .agent_config import mismatches

ELEVENLABS_AGENTS = "https://api.elevenlabs.io/v1/convai/agents"
KAPSO_PLATFORM = "https://api.kapso.ai/platform/v1"


class SetupError(Exception):
    pass


# ElevenLabs ---------------------------------------------------------------------------------

def schema_errors(value, schema, schemas, path="$"):
    """Small structural check against ElevenLabs' OpenAPI: unknown keys, enums, types, ranges."""
    while "$ref" in schema:
        schema = schemas[schema["$ref"].rsplit("/", 1)[1]]
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        return min((schema_errors(value, option, schemas, path) for option in options), key=len)
    errors = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors += schema_errors(item, props[key], schemas, f"{path}.{key}")
            elif schema.get("additionalProperties") is False or (props and not schema.get("additionalProperties")):
                errors.append(f"{path}.{key}: unknown field")
        errors += [f"{path}.{key}: required" for key in schema.get("required", []) if key not in value]
    elif kind == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        for index, item in enumerate(value):
            errors += schema_errors(item, schema.get("items", {}), schemas, f"{path}[{index}]")
    elif kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{path}: expected number"]
        if ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            errors.append(f"{path}: out of range")
    elif kind == "string" and not isinstance(value, str):
        errors.append(f"{path}: expected string")
    elif kind == "boolean" and not isinstance(value, bool):
        errors.append(f"{path}: expected boolean")
    return errors


def validate_against_openapi(config, spec_document):
    body = spec_document["paths"]["/v1/convai/agents/create"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    return schema_errors(config, body, spec_document["components"]["schemas"])


def elevenlabs_client(api_key, transport=None):
    return httpx.Client(timeout=30, follow_redirects=False, headers={"xi-api-key": api_key}, transport=transport)


def _read_agent(client, agent_id):
    response = client.get(f"{ELEVENLABS_AGENTS}/{quote(agent_id, safe='')}")
    if response.is_error:
        raise SetupError(f"Agent read HTTP {response.status_code}")
    return response.json()


def apply_agent(settings, config, client):
    """Create or update the agent, then read it back. Returns a summary without secrets."""
    if not settings.elevenlabs_api_key:
        raise SetupError("ELEVENLABS_API_KEY is not set")
    agent_id = settings.elevenlabs_agent_id
    if agent_id:
        current = _read_agent(client, agent_id)
        if current.get("name") != config["name"]:
            raise SetupError("ELEVENLABS_AGENT_ID points at an agent with a different name; refusing to change it")
        response = client.patch(f"{ELEVENLABS_AGENTS}/{quote(agent_id, safe='')}", json=config)
        action = "updated"
    else:
        response = client.post(f"{ELEVENLABS_AGENTS}/create", json=config)
        action = "created"
    if response.is_error:
        raise SetupError(f"Agent {action[:-1]} failed: HTTP {response.status_code}")
    agent_id = agent_id or response.json()["agent_id"]
    different = mismatches(config, _read_agent(client, agent_id))
    return {"action": action, "agent_id": agent_id, "verified": not different, "mismatches": different}


def verify_agent(settings, config, client):
    if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id):
        raise SetupError("Set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID to verify")
    different = mismatches(config, _read_agent(client, settings.elevenlabs_agent_id))
    return {"verified": not different, "mismatches": different}


# Kapso --------------------------------------------------------------------------------------

def check_public_url(url):
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname or not parsed.path.endswith("/webhooks/whatsapp"):
        raise SetupError("Use the public HTTPS URL of this server ending in /webhooks/whatsapp")
    return url


def webhook_plan(settings, url):
    check_public_url(url)
    base = f"{KAPSO_PLATFORM}/whatsapp/phone_numbers/{quote(settings.phone_number_id or '<WHATSAPP_PHONE_NUMBER_ID>', safe='<>_')}/webhooks"
    return {"dry_run": True, "steps": [
        {"method": "GET", "url": base + "?kind=meta", "purpose": "Refuse if this number already has a Meta webhook"},
        {"method": "POST", "url": base, "body": {"whatsapp_webhook": {
            "kind": "meta", "url": url, "secret_key": "<WHATSAPP_WEBHOOK_SECRET>", "active": True}}},
    ], "not_done": ["Enabling Calling on the number (dashboard or calls_enabled PATCH) stays a manual step",
                    "Choosing whether a dashboard-assigned agent or this server answers the number's calls"]}


def apply_webhook(settings, url, client):
    check_public_url(url)
    if not (settings.kapso_api_key and settings.phone_number_id and settings.webhook_secret):
        raise SetupError("Set KAPSO_API_KEY, WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_WEBHOOK_SECRET first")
    base = f"{KAPSO_PLATFORM}/whatsapp/phone_numbers/{quote(settings.phone_number_id, safe='')}/webhooks"
    headers = {"X-API-Key": settings.kapso_api_key}
    existing = client.get(base, params={"kind": "meta"}, headers=headers)
    if existing.is_error:
        raise SetupError(f"Webhook list HTTP {existing.status_code}")
    rows = existing.json().get("data") or []
    if rows:
        hosts = sorted({urlparse(str(row.get("url", ""))).hostname or "?" for row in rows if isinstance(row, dict)})
        raise SetupError(f"This number already has a Meta webhook ({', '.join(hosts)}). Reuse or update it deliberately.")
    created = client.post(base, headers=headers, json={"whatsapp_webhook": {
        "kind": "meta", "url": url, "secret_key": settings.webhook_secret, "active": True}})
    if created.is_error:
        raise SetupError(f"Webhook create HTTP {created.status_code}")
    data = created.json().get("data") or {}
    return {"created": True, "webhook_id": data.get("id"), "active": data.get("active")}


def redact(text, settings):
    for secret in (settings.kapso_api_key, settings.webhook_secret, settings.elevenlabs_api_key,
                   settings.operator_token, settings.caller_key_secret):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def dumps(value, settings):
    return redact(json.dumps(value, indent=2), settings)
