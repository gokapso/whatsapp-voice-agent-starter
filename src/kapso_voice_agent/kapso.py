"""Kapso's Meta proxy for call actions, with your project API key. No Meta token is used here."""

import re
from urllib.parse import quote, urlencode

import httpx

KAPSO_API = "https://api.kapso.ai"
SAFE_CODE = re.compile(r"[^a-z0-9_]+")


class KapsoError(Exception):
    """A refused or unreadable Kapso request. `http_status` and Kapso's short error `code` (for
    example 409 `not_answerable`) are safe to log; the message can echo request details."""

    def __init__(self, message, http_status=None, code=None):
        super().__init__(message)
        self.http_status, self.code = http_status, code

    def fields(self):
        return {name: value for name, value in (("http_status", self.http_status), ("code", self.code)) if value}


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
            raise KapsoError(f"Kapso returned HTTP {response.status_code} without JSON", response.status_code) from None
        if response.is_error:
            error = data.get("error", data.get("errors", "request failed")) if isinstance(data, dict) else "request failed"
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            code = data.get("code") if isinstance(data, dict) else None
            if isinstance(code, str):
                code = SAFE_CODE.sub("_", code.lower()).strip("_")[:40] or None
            else:
                code = None
            raise KapsoError(f"HTTP {response.status_code}: " + message.replace(self.key, "[redacted]")[:300],
                             response.status_code, code)
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

    async def find_call(self, call_id):
        """Kapso's saved record for one Meta call ID on this number, or None. Read-only."""
        result = await self.request("GET", self.calls_path() + "?" + urlencode({"call_id": call_id, "limit": 1}))
        rows = result.get("data")
        if not isinstance(rows, list) or len(rows) > 1:
            raise KapsoError("Kapso returned an unexpected call list")
        return rows[0] if rows and isinstance(rows[0], dict) else None

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
