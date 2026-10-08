"""Kapso's Meta proxy for call actions, with your project API key. No Meta token is used here."""

from urllib.parse import quote, urlencode

import httpx

KAPSO_API = "https://api.kapso.ai"


class KapsoError(Exception):
    pass


class KapsoClient:
    def __init__(self, api_key, phone_number_id, graph_version="v24.0", client=None, base_url=KAPSO_API):
        self.key, self.phone_number_id, self.graph_version = api_key, phone_number_id, graph_version
        self.base_url = base_url
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def request(self, method, path, body=None):
        response = await self.client.request(method, self.base_url + path, headers={"X-API-Key": self.key}, json=body)
        try:
            data = response.json()
        except ValueError:
            raise KapsoError(f"Kapso returned HTTP {response.status_code} without JSON") from None
        if response.is_error:
            error = data.get("error", data.get("errors", "request failed")) if isinstance(data, dict) else "request failed"
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise KapsoError(f"HTTP {response.status_code}: " + message.replace(self.key, "[redacted]")[:300])
        if not isinstance(data, dict):
            raise KapsoError("Kapso returned an unexpected JSON shape")
        return data

    def calls_path(self, resource="calls"):
        return f"/meta/whatsapp/{self.graph_version}/{quote(self.phone_number_id, safe='')}/{resource}"

    async def action(self, call_id, action, answer_sdp=None):
        """pre_accept / accept / reject / terminate for one Meta call ID."""
        body = {"messaging_product": "whatsapp", "call_id": call_id, "action": action}
        if answer_sdp is not None:
            body["session"] = {"sdp_type": "answer", "sdp": answer_sdp}
        result = await self.request("POST", self.calls_path(), body)
        if result.get("success") is not True:
            raise KapsoError(f"Kapso did not confirm the {action} action")
        return result

    async def permissions(self, recipient):
        key = "recipient" if recipient.startswith("US.") else "user_wa_id"
        return await self.request("GET", self.calls_path("call_permissions") + "?" + urlencode({key: recipient}))

    async def connect(self, recipient, offer_sdp, callback_data="kapso-voice-agent-starter"):
        # No `recording`/`transcription` objects: Meta-native capture is not requested by default.
        target = "recipient" if recipient.startswith("US.") else "to"
        body = {"messaging_product": "whatsapp", "action": "connect", target: recipient,
                "session": {"sdp_type": "offer", "sdp": offer_sdp}, "biz_opaque_callback_data": callback_data}
        return await self.request("POST", self.calls_path(), body)

    async def close(self):
        await self.client.aclose()
