"""Kapso's Meta proxy for call actions, with your project API key. No Meta token is used here."""

import re
from urllib.parse import quote, urlencode

import httpx

KAPSO_API = "https://api.kapso.ai"
SAFE_TOKEN = re.compile(r"[^a-z0-9_-]+")
# Kapso keeps the call for another handler (the Inbox, a dashboard voice agent): this server must
# not answer it, and a reject or terminate from here is refused the same way.
HANDLED_ELSEWHERE = ("answered_elsewhere", "not_answerable")


def safe_token(value, limit):
    token = SAFE_TOKEN.sub("_", str(value).lower()).strip("_")[:limit] if isinstance(value, (str, int)) else ""
    return token or None


class KapsoError(Exception):
    """A refused or unreadable Kapso request. The HTTP status, the error `code` (Kapso's, e.g.
    `answered_elsewhere`, or Meta's numeric one) and Kapso's `x-request-id` are safe to log; the
    message can echo request details and is never logged."""

    def __init__(self, message, http_status=None, code=None, request_id=None):
        super().__init__(message)
        self.http_status, self.code, self.request_id = http_status, code, request_id

    def fields(self):
        values = (("http_status", self.http_status), ("code", self.code), ("request_id", self.request_id))
        return {name: value for name, value in values if value}


class KapsoClient:
    def __init__(self, api_key, phone_number_id, graph_version="v24.0", client=None, base_url=KAPSO_API):
        self.key, self.phone_number_id, self.graph_version = api_key, phone_number_id, graph_version
        self.base_url = base_url
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def request(self, method, path, body=None):
        response = await self.client.request(method, self.base_url + path, headers={"X-API-Key": self.key}, json=body)
        request_id = safe_token(response.headers.get("x-request-id"), 64)
        try:
            data = response.json()
        except ValueError:
            raise KapsoError(f"Kapso returned HTTP {response.status_code} without JSON", response.status_code,
                             request_id=request_id) from None
        if response.is_error:
            error = data.get("error", data.get("errors", "request failed")) if isinstance(data, dict) else "request failed"
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            # Kapso: {"error": "...", "code": "answered_elsewhere"}. Meta, passed through: {"error": {"code": 131009}}.
            code = data.get("code") if isinstance(data, dict) else None
            if code is None and isinstance(error, dict):
                code = error.get("code")
            raise KapsoError(f"HTTP {response.status_code}: " + message.replace(self.key, "[redacted]")[:300],
                             response.status_code, safe_token(code, 40), request_id)
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

    async def request_call_permission(self, recipient, text):
        """Meta's free-form call permission request: `text` and a button to allow calls. WhatsApp
        delivers it only inside the person's 24-hour customer service window."""
        target = "recipient" if recipient.startswith("US.") else "to"
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", target: recipient,
                "type": "interactive", "interactive": {"type": "call_permission_request",
                                                       "action": {"name": "call_permission_request"},
                                                       "body": {"text": text}}}
        return await self.request("POST", self.calls_path("messages"), body)

    async def connect(self, recipient, offer_sdp, callback_data="kapso-voice-agent-starter"):
        # No `recording`/`transcription` objects: Meta-native capture is not requested by default.
        target = "recipient" if recipient.startswith("US.") else "to"
        body = {"messaging_product": "whatsapp", "action": "connect", target: recipient,
                "session": {"sdp_type": "offer", "sdp": offer_sdp}, "biz_opaque_callback_data": callback_data}
        return await self.request("POST", self.calls_path(), body)

    # Experimental handoff signaling (handoff.py), on Kapso's Platform API with the same key.
    # Returns (HTTP status, body) so the caller can act on Kapso's refusal code; only transport
    # errors and non-JSON answers raise.

    async def platform(self, method, path, body=None):
        response = await self.client.request(method, self.base_url + "/platform/v1/whatsapp/call_handoffs" + path,
                                             headers={"X-API-Key": self.key}, json=body)
        try:
            data = response.json()
        except ValueError:
            raise KapsoError(f"Kapso returned HTTP {response.status_code} without JSON", response.status_code) from None
        if not isinstance(data, dict):
            raise KapsoError("Kapso returned an unexpected JSON shape")
        return response.status_code, data.get("data", data) if response.is_success else data

    async def request_handoff(self, call_id, request_key, stun_urls):
        return await self.platform("POST", "", {"phone_number_id": self.phone_number_id, "call_id": call_id,
                                                "request_key": request_key, "stun_urls": stun_urls,
                                                "return_to_agent_supported": True})

    async def handoff(self, handoff_id):
        return await self.platform("GET", "/" + quote(handoff_id, safe=""))

    async def answer_handoff(self, handoff_id, sdp):
        return await self.platform("POST", f"/{quote(handoff_id, safe='')}/answer", {"sdp": sdp})

    async def commit_handoff(self, handoff_id):
        return await self.platform("POST", f"/{quote(handoff_id, safe='')}/commit", {})

    async def complete_return(self, handoff_id, request_key, outcome):
        return await self.platform("POST", f"/{quote(handoff_id, safe='')}/complete_return",
                                   {"request_key": request_key, "outcome": outcome})

    async def end_handoff(self, handoff_id, reason):
        return await self.platform("POST", f"/{quote(handoff_id, safe='')}/end", {"reason": reason})

    async def close(self):
        await self.client.aclose()
