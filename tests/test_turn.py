"""Per-call TURN credentials (TURN_PROVIDER=cloudflare). Cloudflare is an httpx MockTransport with
the documented generate-ice-servers response; nothing reaches the network."""

import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from conftest import TOKEN, KapsoRecorder, asgi_client
from kapso_voice_agent.app import create_app
from kapso_voice_agent.config import ConfigError, load_settings
from kapso_voice_agent.turn import TurnUnavailable, call_ice_servers

AUTH = {"authorization": f"Bearer {TOKEN}"}
# Shape from https://developers.cloudflare.com/realtime/turn/generate-credentials/ (synthetic values).
CLOUDFLARE = {"iceServers": [
    {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:53?transport=udp",
              "turn:turn.cloudflare.com:3478?transport=tcp", "turn:turn.cloudflare.com:80?transport=tcp",
              "turns:turn.cloudflare.com:5349?transport=tcp", "turns:turn.cloudflare.com:443?transport=tcp"],
     "username": "synthetic-turn-user", "credential": "synthetic-turn-credential"}]}


@pytest.fixture
def turn(settings):
    return replace(settings, turn_provider="cloudflare", cloudflare_turn_key_id="key-id-synthetic",
                   cloudflare_turn_api_token="cf-token-not-real")


def cloudflare(requests, status=201, body=None):
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=CLOUDFLARE if body is None else body)
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("transport,url", [("tls", "turns:turn.cloudflare.com:443?transport=tcp"),
                                           ("tcp", "turn:turn.cloudflare.com:80?transport=tcp"),
                                           ("udp", "turn:turn.cloudflare.com:3478?transport=udp")])
def test_one_turn_url_for_the_chosen_transport_because_aiortc_uses_only_the_first(turn, transport, url):
    requests = []
    servers = json.loads(asyncio.run(call_ice_servers(replace(turn, turn_transport=transport), cloudflare(requests))))
    assert servers == [{"urls": "stun:stun.cloudflare.com:3478"},
                       {"urls": url, "username": "synthetic-turn-user", "credential": "synthetic-turn-credential"}]
    [request] = requests
    assert request.method == "POST" and request.headers["authorization"] == "Bearer cf-token-not-real"
    assert str(request.url) == "https://rtc.live.cloudflare.com/v1/turn/keys/key-id-synthetic/credentials/generate-ice-servers"
    assert json.loads(request.content) == {"ttl": turn.max_session_seconds + 600}


def test_without_a_turn_provider_the_configured_list_is_used_as_is(settings):
    assert asyncio.run(call_ice_servers(replace(settings, ice_servers_json='[{"urls": "stun:a.example:3478"}]'))) \
        == '[{"urls": "stun:a.example:3478"}]'


@pytest.mark.parametrize("status,body", [(401, {"error": "bad token"}), (500, {}), (201, {"iceServers": []}),
                                         (201, {"iceServers": [{"urls": ["turn:x:3478?transport=udp"]}]})])
def test_failures_raise_instead_of_starting_a_call_without_media(turn, status, body):
    with pytest.raises(TurnUnavailable):
        asyncio.run(call_ice_servers(turn, cloudflare([], status, body)))


def test_browser_call_is_refused_with_a_fix_when_turn_fails(turn, spec, store):
    async def failing(settings):
        raise TurnUnavailable("Cloudflare TURN HTTP 401")

    class NeverSession:
        def __init__(self, *args, **kwargs):
            raise AssertionError("no session without a media path")

    async def go():
        app = create_app(turn, spec, store, session_factory=NeverSession, kapso_factory=KapsoRecorder().factory())
        app.state.manager.ice_factory = failing
        async with asgi_client(app) as client:
            response = await client.post("/operator/api/browser-call", json={"sdp": "v=0 synthetic offer"}, headers=AUTH)
            state = (await client.get("/operator/api/state", headers=AUTH)).json()
        return response, state
    response, state = asyncio.run(go())
    assert response.status_code == 503 and "CLOUDFLARE_TURN_KEY_ID" in response.json()["detail"]
    assert "turn_credentials_failed" in [e["event"] for e in state["events"]]
    assert "cf-token-not-real" not in json.dumps(state)


def test_settings_validation():
    base = {"OPERATOR_TOKEN": "o" * 40}
    with pytest.raises(ConfigError, match="CLOUDFLARE_TURN_KEY_ID"):
        load_settings(environ={**base, "TURN_PROVIDER": "cloudflare"})
    with pytest.raises(ConfigError, match="TURN_PROVIDER"):
        load_settings(environ={**base, "TURN_PROVIDER": "twilio"})
    with pytest.raises(ConfigError, match="TURN_TRANSPORT"):
        load_settings(environ={**base, "TURN_TRANSPORT": "quic"})
    settings = load_settings(environ={**base, "TURN_PROVIDER": "Cloudflare", "CLOUDFLARE_TURN_KEY_ID": "k",
                                      "CLOUDFLARE_TURN_API_TOKEN": "t"})
    assert settings.turn_provider == "cloudflare" and settings.turn_transport == "tls"
