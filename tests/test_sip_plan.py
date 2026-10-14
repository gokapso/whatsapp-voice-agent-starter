"""`voice-agent sip status` and `sip plan`: read-only status without the SIP password, offline plan."""

import asyncio

import httpx

from conftest import KAPSO_KEY, PHONE
from kapso_voice_agent.kapso import KapsoClient
from kapso_voice_agent.sip import describe_mode, plan


def test_status_read_never_asks_meta_for_the_sip_password_and_names_who_answers():
    requests = []

    async def kapso(request):
        requests.append(request)
        return httpx.Response(200, json={"calling": {"status": "ENABLED", "sip": {
            "status": "ENABLED", "servers": [{"hostname": "sip.rtc.elevenlabs.io", "port": 5061}]}}})

    async def go():
        client = KapsoClient(KAPSO_KEY, PHONE, client=httpx.AsyncClient(transport=httpx.MockTransport(kapso)))
        try:
            return await client.calling_settings()
        finally:
            await client.close()
    calling = asyncio.run(go())
    assert [r.method for r in requests] == ["GET"] and "include_sip_credentials" not in str(requests[0].url)
    assert "cannot answer" in describe_mode(calling)
    assert describe_mode({"status": "ENABLED"}).startswith("Calling API")


def test_plan_shows_the_mode_change_and_undo_with_a_password_placeholder():
    text = plan("elevenlabs", "+15550100000", PHONE)
    assert "Blocked" in text and "SIP only" in text and '"status": "DISABLED"' in text
    assert '"password": "<' in text and '"srtp_key_exchange_protocol": "SDES"' in text
