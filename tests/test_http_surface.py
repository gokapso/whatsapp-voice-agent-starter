"""Public webhook vs authenticated operator controls, and webhook event semantics (no media)."""

import asyncio
from dataclasses import replace
import json

import pytest

from conftest import TOKEN, KapsoRecorder, asgi_client, fixture, post_webhook, sign
from kapso_voice_agent.app import create_app

AUTH = {"authorization": f"Bearer {TOKEN}"}


class NeverSession:
    def __init__(self, *args, **kwargs):
        raise AssertionError("no agent session may start in this test")


def make_app(settings, spec, store, kapso=None, session_factory=NeverSession, connection_factory=None):
    kwargs = {"kapso_factory": (kapso or KapsoRecorder()).factory(), "session_factory": session_factory}
    if connection_factory:
        kwargs["connection_factory"] = connection_factory
    return create_app(settings, spec, store, **kwargs)


def run(coroutine):
    return asyncio.run(coroutine)


class TestWebhookAuthentication:
    @pytest.mark.parametrize("header", [None, "", "not-hex", "0" * 64, "zz" * 32])
    def test_rejects_missing_malformed_or_wrong_signatures(self, settings, spec, store, header):
        async def go():
            app = make_app(settings, spec, store)
            raw = json.dumps(fixture("inbound_connect.json")).encode()
            headers = {} if header is None else {"x-webhook-signature": header}
            async with asgi_client(app) as client:
                return (await client.post("/webhooks/whatsapp", content=raw, headers=headers)).status_code
        assert run(go()) == 401

    def test_signature_is_over_exact_bytes(self, settings, spec, store):
        async def go():
            app = make_app(settings, spec, store)
            raw = json.dumps(fixture("inbound_terminate.json")).encode()
            async with asgi_client(app) as client:
                tampered = raw.replace(b"COMPLETED", b"COMPLETEX")
                bad = await client.post("/webhooks/whatsapp", content=tampered, headers={"x-webhook-signature": sign(raw)})
                good = await client.post("/webhooks/whatsapp", content=raw, headers={"x-webhook-signature": sign(raw).upper()})
                wrong_secret = await post_webhook(client, fixture("inbound_terminate.json"), secret="other")
            return bad.status_code, good.status_code, wrong_secret.status_code
        assert run(go()) == (401, 200, 401)

    def test_size_cap_and_missing_secret(self, settings, spec, store):
        async def go():
            async with asgi_client(make_app(settings, spec, store)) as client:
                big = await client.post("/webhooks/whatsapp", content=b"x" * 1_000_001, headers={"x-webhook-signature": "0" * 64})
            async with asgi_client(make_app(replace(settings, webhook_secret=""), spec, store)) as client:
                unset = await post_webhook(client, fixture("inbound_terminate.json"))
            return big.status_code, unset.status_code
        assert run(go()) == (413, 503)

    def test_signed_non_json_is_400(self, settings, spec, store):
        async def go():
            async with asgi_client(make_app(settings, spec, store)) as client:
                raw = b"not json"
                return (await client.post("/webhooks/whatsapp", content=raw, headers={"x-webhook-signature": sign(raw)})).status_code
        assert run(go()) == 400


class TestWebhookEvents:
    def test_other_numbers_and_non_call_fields_are_ignored_without_side_effects(self, settings, spec, store):
        async def go():
            kapso = KapsoRecorder()
            app = make_app(settings, spec, store, kapso)
            other = fixture("inbound_connect.json")
            other["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "100000000000999"
            async with asgi_client(app) as client:
                first = await post_webhook(client, other)
                second = await post_webhook(client, fixture("messages_change.json"))
            return first.json(), second.json(), kapso.actions, app.state.manager.jobs
        first, second, actions, jobs = run(go())
        assert first["ignored"] == 1 and second["ignored"] == 1 and actions == [] and jobs == {}

    @pytest.mark.parametrize("name", ["recording_available.json", "transcript_available.json"])
    @pytest.mark.parametrize("alias", [None, "call_transcription_available"])
    def test_artifact_events_are_metadata_only(self, settings, spec, store, name, alias):
        async def go():
            kapso = KapsoRecorder()
            app = make_app(settings, spec, store, kapso)
            payload = fixture(name, **({"event": alias} if alias and "transcript" in name else {}))
            async with asgi_client(app) as client:
                assert (await post_webhook(client, payload)).status_code == 200
                state = (await client.get("/operator/api/state", headers=AUTH)).json()
            return state, kapso.actions, app.state.manager
        state, actions, manager = run(go())
        assert actions == [] and manager.jobs == {} and manager.sessions == {}
        notice = state["native_artifact_events"][0]
        assert notice["media_present"] is True and notice["ref"].startswith("call-")
        text = json.dumps(state)
        assert "media.example.invalid" not in text and "9000000000000001" not in text and "wacid." not in text

    @pytest.mark.parametrize("payload", [
        {"entry": [{"changes": ["not-an-object"]}]},
        {"entry": [{"changes": [{"field": "calls", "value": {"metadata": "x", "calls": "x"}}]}]},
        {"entry": ["x", {"changes": None}]},
        {"entry": [{"changes": [{"field": "calls", "value": {"metadata": {"phone_number_id": "100000000000001"},
                                                             "calls": [{"id": 5}, "x"], "statuses": [{"id": ""}]}}]}]},
        {"entry": [{"changes": [{"field": "calls", "value": {"metadata": {"phone_number_id": "100000000000001"}, "calls": [
            {"id": "wacid.SYNTHETIC-X", "event": "call_recording_available", "call_recording": "x"},
            {"id": "wacid.SYNTHETIC-Y", "event": "connect", "direction": "USER_INITIATED", "session": "x"},
            {"id": "wacid.SYNTHETIC-Z", "event": "connect", "direction": "BUSINESS_INITIATED", "session": "x"}]}}]}]}])
    def test_malformed_signed_payloads_are_ignored_not_500(self, settings, spec, store, payload):
        async def go():
            kapso = KapsoRecorder()
            app = make_app(settings, spec, store, kapso)
            async with asgi_client(app) as client:
                response = await post_webhook(client, payload)
            return response.status_code, kapso.actions, app.state.manager.jobs
        assert run(go()) == (200, [], {})

    def test_redelivered_artifact_event_is_noted_once(self, settings, spec, store):
        async def go():
            app = make_app(settings, spec, store)
            async with asgi_client(app) as client:
                for _ in range(2):
                    assert (await post_webhook(client, fixture("recording_available.json"))).status_code == 200
            return list(app.state.manager.artifacts)
        assert len(run(go())) == 1

    @pytest.mark.parametrize("order", [["terminate", "connect"], ["connect", "terminate"]])
    def test_batched_terminate_wins_and_late_connect_never_restarts(self, settings, spec, store, order):
        async def go():
            kapso = KapsoRecorder()
            app = make_app(settings, spec, store, kapso)
            payload = fixture("inbound_connect.json")
            connect = payload["entry"][0]["changes"][0]["value"]["calls"][0]
            terminate = fixture("inbound_terminate.json")["entry"][0]["changes"][0]["value"]["calls"][0]
            payload["entry"][0]["changes"][0]["value"]["calls"] = [terminate if e == "terminate" else connect for e in order]
            async with asgi_client(app) as client:
                assert (await post_webhook(client, payload)).status_code == 200
                assert (await post_webhook(client, fixture("inbound_connect.json"))).status_code == 200
            return kapso.actions, app.state.manager.jobs
        actions, jobs = run(go())
        assert actions == [] and jobs == {}

    def test_over_capacity_inbound_is_rejected_not_left_ringing(self, settings, spec, store):
        async def go():
            kapso = KapsoRecorder()
            app = make_app(settings, spec, store, kapso)
            manager = app.state.manager
            manager.jobs["busy"] = asyncio.get_running_loop().create_future()
            async with asgi_client(app) as client:
                assert (await post_webhook(client, fixture("inbound_connect.json"))).status_code == 200
            await asyncio.gather(*manager.background)
            manager.jobs.pop("busy").cancel()
            return kapso.bodies
        bodies = run(go())
        assert [b["action"] for b in bodies] == ["reject"] and bodies[0]["call_id"] == "wacid.SYNTHETIC-INBOUND-1"

    def test_unconfigured_server_returns_503_for_connect_but_accepts_other_events(self, settings, spec, store):
        async def go():
            app = make_app(replace(settings, elevenlabs_api_key=""), spec, store)
            async with asgi_client(app) as client:
                connect = await post_webhook(client, fixture("inbound_connect.json"))
                terminate = await post_webhook(client, fixture("inbound_terminate.json"))
            return connect.status_code, terminate.status_code
        assert run(go()) == (503, 200)

    def test_connect_without_offer_is_ignored(self, settings, spec, store):
        async def go():
            kapso = KapsoRecorder()
            app = make_app(settings, spec, store, kapso)
            async with asgi_client(app) as client:
                response = await post_webhook(client, fixture("inbound_connect.json", session={"sdp_type": "offer"}))
            return response.status_code, kapso.actions
        assert run(go()) == (200, [])


class TestOperatorSurface:
    def test_health_reveals_nothing(self, settings, spec, store):
        async def go():
            async with asgi_client(make_app(settings, spec, store)) as client:
                return (await client.get("/healthz")).json()
        assert run(go()) == {"ok": True}

    @pytest.mark.parametrize("path,method", [("/operator/", "GET"), ("/operator/api/state", "GET"),
                                             ("/operator/api/browser-call", "POST")])
    def test_operator_routes_do_not_exist_without_token(self, settings, spec, store, path, method):
        async def go():
            async with asgi_client(make_app(replace(settings, operator_token=""), spec, store)) as client:
                return (await client.request(method, path, json={"sdp": "x" * 20}, headers=AUTH)).status_code
        assert run(go()) == 404

    @pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x"])
    def test_operator_api_requires_exact_bearer_token(self, settings, spec, store, header):
        async def go():
            async with asgi_client(make_app(settings, spec, store)) as client:
                headers = {"authorization": header} if header else {}
                state = await client.get("/operator/api/state", headers=headers)
                call = await client.post("/operator/api/outbound/call", headers=headers, json={"recipient": "15550100002"})
                offer = await client.post("/operator/api/browser-call", headers=headers, json={"sdp": "x" * 20})
            return state.status_code, call.status_code, offer.status_code
        assert run(go()) == (401, 401, 401)

    def test_console_page_has_no_secrets_and_strict_headers(self, settings, spec, store):
        async def go():
            async with asgi_client(make_app(settings, spec, store)) as client:
                return await client.get("/operator/"), await client.get("/operator/app.js")
        page, script = run(go())
        assert page.status_code == script.status_code == 200
        assert TOKEN not in page.text + script.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"] == "no-store"

    def test_outbound_is_off_unless_enabled_and_never_dials(self, settings, spec, store):
        async def go():
            kapso = KapsoRecorder()
            async with asgi_client(make_app(settings, spec, store, kapso)) as client:
                permission = await client.post("/operator/api/outbound/permission", headers=AUTH, json={"recipient": "15550100002"})
                call = await client.post("/operator/api/outbound/call", headers=AUTH, json={"recipient": "15550100002"})
            return permission.status_code, call.status_code, kapso.bodies
        assert run(go()) == (403, 403, [])

    def test_extra_fields_and_bad_refs_are_rejected(self, settings, spec, store):
        async def go():
            app = make_app(replace(settings, enable_outbound=True), spec, store)
            async with asgi_client(app) as client:
                capture = await client.post("/operator/api/outbound/call", headers=AUTH, json={
                    "recipient": "15550100002", "recording": {"status": "ENABLED"}})
                bad_ref = await client.post("/operator/api/calls/..%2Fetc/hangup", headers=AUTH)
                unknown = await client.post("/operator/api/calls/call-0123456789abcdef/hangup", headers=AUTH)
            return capture.status_code, bad_ref.status_code, unknown.status_code
        capture, bad_ref, unknown = run(go())
        assert capture == 422 and bad_ref in (404, 422) and unknown == 404

    def test_allowed_hosts_blocks_other_host_headers(self, settings, spec, store):
        async def go():
            app = make_app(replace(settings, allowed_hosts=("voice.example.test",)), spec, store)
            async with asgi_client(app) as client:
                ok = await client.get("/healthz")
            async with asgi_client(app, host="attacker.example") as client:
                blocked = await client.get("/healthz")
            return ok.status_code, blocked.status_code
        assert run(go()) == (200, 400)
