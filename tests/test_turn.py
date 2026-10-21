"""Offline Cloudflare protocol checks; real relay connectivity is an operator test."""

import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from kapso_voice_agent.config import Settings, load_settings
from kapso_voice_agent.provider import redact
from kapso_voice_agent.turn import TurnError, handoff_ice


@pytest.fixture
def relay_settings():
    return Settings(human_handoff=True, kapso_api_key="test-kapso", phone_number_id="1",
                    public_domain="voice.up.railway.app", cloudflare_turn_key_id="test-turn-key",
                    cloudflare_turn_api_token="test-turn-token")


def test_each_join_mints_fresh_credentials_for_the_full_human_segment(relay_settings):
    requests = []

    def generate(request):
        requests.append(request)
        return httpx.Response(201, json={"iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478"]},
            {"urls": ["turn:turn.cloudflare.com:53?transport=udp", "turn:turn.cloudflare.com:3478?transport=udp",
                      "turns:turn.cloudflare.com:5349?transport=tcp"],
             "username": "test-user", "credential": "test-credential-" + str(len(requests))}]})

    async def go():
        return [json.loads(await handoff_ice(relay_settings, httpx.MockTransport(generate))) for _ in range(2)]

    first, second = asyncio.run(go())
    assert first[1]["credential"] != second[1]["credential"]
    assert first[1]["urls"] == ["turn:turn.cloudflare.com:3478?transport=udp", "turns:turn.cloudflare.com:5349?transport=tcp"]
    assert all(request.headers["Authorization"] == "Bearer test-turn-token" for request in requests)
    assert all(request.url == "https://rtc.live.cloudflare.com/v1/turn/keys/test-turn-key/credentials/generate-ice-servers"
               for request in requests)
    assert all(json.loads(request.content)["ttl"] == relay_settings.handoff_max_seconds + 300 for request in requests)


@pytest.mark.parametrize("status,body", [(401, {"error": "test-turn-token"}), (201, {"iceServers": {}}),
                                       (201, {"iceServers": [{"urls": "stun:stun.cloudflare.com:3478"}]}),
                                       (201, {"iceServers": [{"urls": "turn:turn.cloudflare.com:3478"}]})])
def test_provider_failures_are_sanitized(relay_settings, status, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    with pytest.raises(TurnError) as error:
        asyncio.run(handoff_ice(relay_settings, transport))
    assert str(error.value) == "Could not obtain TURN credentials"
    assert "test-turn-token" not in repr(error.value)


def test_static_turn_does_not_contact_cloudflare(relay_settings):
    def unexpected(request):
        pytest.fail("Static configuration contacted Cloudflare")
    settings = replace(relay_settings, cloudflare_turn_key_id="", cloudflare_turn_api_token="",
                       handoff_ice_servers_json='[{"urls": "turn:turn.example.test:3478", "username": "u", "credential": "c"}]')
    assert asyncio.run(handoff_ice(settings, httpx.MockTransport(unexpected))) == settings.handoff_ice_servers_json
    assert settings.handoff_problem() is None


def test_cloudflare_readiness_and_secrets_stay_server_side(relay_settings):
    loaded = load_settings(environ={"CLOUDFLARE_TURN_KEY_ID": "test-turn-key", "CLOUDFLARE_TURN_API_TOKEN": "test-turn-token"})
    assert loaded.cloudflare_turn_ready
    assert relay_settings.handoff_problem() is None
    assert replace(relay_settings, cloudflare_turn_api_token="").handoff_problem() == "cloudflare_turn_missing"
    assert replace(relay_settings, human_handoff=False).handoff_problem() == "off"
    assert relay_settings.handoff_stun_urls() == ["stun:stun.cloudflare.com:3478"]
    assert "test-turn-token" not in repr(relay_settings)
    assert redact("test-turn-token", relay_settings) == "[redacted]"
