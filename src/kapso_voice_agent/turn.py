"""Server-side TURN credentials for one handoff attempt. No keys or credentials are logged."""

import json
from urllib.parse import quote

import httpx


class TurnError(RuntimeError):
    pass


async def handoff_ice(settings, transport=None):
    """Mint fresh Cloudflare credentials at join time, valid for the whole human segment.

    Static HANDOFF_ICE_SERVERS_JSON remains supported when Cloudflare is not configured. Nothing
    is cached or sent to Kapso/the browser; only this server uses the returned credentials.
    """
    if not settings.cloudflare_turn_ready:
        return settings.handoff_ice_json()
    key_id = quote(settings.cloudflare_turn_key_id, safe="")
    url = f"https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
    try:
        async with httpx.AsyncClient(timeout=5, transport=transport) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {settings.cloudflare_turn_api_token}"},
                                         json={"ttl": settings.handoff_max_seconds + 300})
            response.raise_for_status()
            servers = response.json()["iceServers"]
        if not isinstance(servers, list):
            raise ValueError
        result = []
        has_turn = False
        for server in servers:
            urls = server["urls"]
            urls = [urls] if isinstance(urls, str) else urls
            urls = [url for url in urls if url.split("?")[0].rsplit(":", 1)[-1] != "53"]
            if not urls:
                continue
            row = {"urls": urls}
            if any(url.startswith(("turn:", "turns:")) for url in urls):
                if not (isinstance(server.get("username"), str) and server["username"] and
                        isinstance(server.get("credential"), str) and server["credential"]):
                    raise ValueError
                row.update(username=server["username"], credential=server["credential"])
                has_turn = True
            result.append(row)
        if not has_turn:
            raise ValueError
        return json.dumps(result)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        # Provider responses and httpx exceptions can contain credentials. Report only this error.
        raise TurnError("Could not obtain TURN credentials") from None
