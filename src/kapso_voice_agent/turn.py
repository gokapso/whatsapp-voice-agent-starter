"""ICE servers for one call's WebRTC media.

Default: ICE_SERVERS_JSON as configured (often [] on a host with a public IP).

TURN_PROVIDER=cloudflare: before each call, ask Cloudflare Realtime TURN for short-lived
credentials (https://developers.cloudflare.com/realtime/turn/generate-credentials/) that last a
little longer than MAX_SESSION_SECONDS. This is for hosts that cannot receive UDP, such as
Railway: media then relays through TURN instead of reaching this server directly.

aiortc uses only the first STUN URL and the first TURN URL it understands, so this module picks
exactly one TURN URL by TURN_TRANSPORT:
- tls (default): turns:...:443?transport=tcp. Outbound TCP 443 passes almost every egress filter.
- tcp: turn:...?transport=tcp.
- udp: turn:...:3478?transport=udp. Lowest latency when the host may send UDP.
The API token stays in this process. TURN credentials are never sent to a browser or logged.
"""

import json
import re
from urllib.parse import quote

import httpx

CLOUDFLARE_TURN = "https://rtc.live.cloudflare.com/v1/turn/keys/{key}/credentials/generate-ice-servers"
TIMEOUT_SECONDS = 5
TTL_MARGIN_SECONDS = 600
# turn:host:port?transport=tcp has no "//", so urlparse cannot read its port.
TURN_URL = re.compile(r"^(?P<scheme>turns?):(?P<host>[^?:]+)(?::(?P<port>[0-9]+))?(?:\?transport=(?P<transport>udp|tcp))?$")


class TurnUnavailable(Exception):
    """TURN credentials could not be created; no call may start without its media path."""


def pick(url, transport):
    """Whether one TURN URL matches TURN_TRANSPORT, and how much it is preferred (lower is better)."""
    match = TURN_URL.match(url)
    if not match:
        return None
    scheme, port = match["scheme"], int(match["port"]) if match["port"] else None
    kind = match["transport"] or ("udp" if scheme == "turn" else "tcp")
    if transport == "tls" and scheme == "turns" and kind == "tcp":
        return 0 if port == 443 else 1
    if transport == "tcp" and scheme == "turn" and kind == "tcp":
        return 0 if port == 443 else 1 if port == 80 else 2
    if transport == "udp" and scheme == "turn" and kind == "udp":
        return 0 if port == 3478 else 1
    return None


def choose(ice_servers, transport):
    """STUN first, then the single TURN URL for this transport, from Cloudflare's iceServers."""
    stun, turn = [], []
    for server in ice_servers if isinstance(ice_servers, list) else []:
        urls = server.get("urls") if isinstance(server, dict) else None
        for url in urls if isinstance(urls, list) else [urls] if isinstance(urls, str) else []:
            if url.startswith("stun:") and not stun:
                stun.append({"urls": url})
            elif url.startswith(("turn:", "turns:")) and (rank := pick(url, transport)) is not None:
                turn.append((rank, {"urls": url, "username": server.get("username"),
                                    "credential": server.get("credential")}))
    if not turn or not all(isinstance(turn[0][1][k], str) and turn[0][1][k] for k in ("username", "credential")):
        raise TurnUnavailable(f"Cloudflare returned no {transport} TURN server")
    return stun + [min(turn, key=lambda item: item[0])[1]]


async def call_ice_servers(settings, transport=None):
    """ICE servers JSON for the next call. Raises TurnUnavailable when TURN is configured but fails."""
    if settings.turn_provider != "cloudflare":
        return settings.ice_servers_json
    url = CLOUDFLARE_TURN.format(key=quote(settings.cloudflare_turn_key_id, safe=""))
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport) as client:
            response = await client.post(url, headers={"Authorization": f"Bearer {settings.cloudflare_turn_api_token}"},
                                         json={"ttl": settings.max_session_seconds + TTL_MARGIN_SECONDS})
    except httpx.HTTPError as error:
        raise TurnUnavailable(f"Cloudflare TURN request failed: {type(error).__name__}") from None
    if response.status_code not in (200, 201):
        raise TurnUnavailable(f"Cloudflare TURN HTTP {response.status_code}")
    try:
        servers = response.json().get("iceServers")
    except ValueError:
        raise TurnUnavailable("Cloudflare TURN returned no JSON") from None
    return json.dumps(choose(servers, settings.turn_transport))
