"""`voice-agent sip status` (read-only) and `voice-agent sip plan` (offline dry run).

Meta can send a WhatsApp number's calls to a SIP server instead of the Calling API. Then Meta
sends the call to the provider (ElevenLabs or xAI) over SIP, and the provider handles the audio.
Facts this file relies on, from Meta's SIP guide
(https://developers.facebook.com/documentation/business-messaging/whatsapp/calling/sip):

- With `calling.sip.status` ENABLED the number "exclusively uses SIP for call signaling and will not
  work with Graph APIs": Kapso call actions, Inbox answering, Kapso dashboard agents and this
  server's webhook no longer answer that number's calls. Messages are not affected.
- One SIP server per number, TLS only (default port 5061). Meta's user-initiated INVITE offers
  only Opus (`opus/48000/2`) and telephone-event, with SRTP keys by DTLS (default) or SDES.
- Meta generates the SIP digest password per number and app. This file never asks for it
  (`include_sip_credentials` is never sent) and never prints it.
- With `webhook_delivery` ENABLED, Meta sends `call_created` and `terminate` webhooks (no SDP).

Nothing here changes Meta, Kapso or provider settings. The plan prints the requests to send by
hand after its blockers are cleared, and the request that undoes them.
"""

import json
import re

E164 = re.compile(r"^\+[1-9][0-9]{6,14}$")

PROVIDERS = {
    "elevenlabs": {
        "label": "ElevenLabs Agents (SIP trunk)",
        "hostname": "sip.rtc.elevenlabs.io",
        "srtp": "SDES",
        "blockers": [
            "Codec: Meta offers only Opus on WhatsApp SIP calls. ElevenLabs documents only G.711 (PCMU, PCMA) and "
            "G.722 for SIP trunks, and its API accepts no other codec. Get written confirmation from ElevenLabs that "
            "its SIP trunk answers an Opus-only offer (its WhatsApp FAQ says to contact them about SIP routing).",
            "Media keys: ElevenLabs documents SRTP (media encryption required) but not the key exchange. This plan "
            "switches the number to SDES, because ElevenLabs does not document DTLS-SRTP or ICE. Confirm SDES.",
            "Tools: an agent called over SIP has no client to run this starter's six tools. Use the minimal agent in "
            "examples/elevenlabs-sip, or ElevenLabs' own Cal.com integration (different guarantees).",
        ],
    },
    "xai": {
        "label": "xAI Grok Voice (Direct SIP)",
        "hostname": "sip.voice.x.ai",
        "srtp": None,
        "blockers": [
            "Codec: Meta offers only Opus on WhatsApp SIP calls. xAI's SIP guide lists only G.711 (PCMU, PCMA) "
            "and G.722.",
            "Media encryption: Meta sends SRTP only (DTLS or SDES). xAI's SIP guide does not mention SRTP.",
            "Scope: xAI documents SIP for phone carriers, contact centers and PBXs, not WhatsApp.",
            "Model: xAI ignores the model parameter on SIP sessions, so grok-voice-3.0 cannot be pinned there.",
        ],
    },
}


class SipPlanError(ValueError):
    pass


def describe_mode(calling):
    """Plain description of who answers a number's calls, from Meta's calling settings."""
    calling = calling if isinstance(calling, dict) else {}
    if calling.get("status") != "ENABLED":
        return "Calling is off for this number: nothing answers calls."
    sip = calling.get("sip") if isinstance(calling.get("sip"), dict) else {}
    srtp = calling.get("srtp_key_exchange_protocol") or "DTLS (default)"
    if sip.get("status") != "ENABLED":
        return ("Calling API (not SIP): Kapso receives each call's webhook. Inbox, a Kapso dashboard agent or this "
                f"server can answer. Media keys: {srtp}.")
    servers = [s for s in sip.get("servers") or [] if isinstance(s, dict)]
    targets = ", ".join(f"{s.get('hostname', '?')}:{s.get('port', 5061)}" for s in servers) or "no server"
    return (f"SIP to {targets}. Kapso call actions, Inbox answering, dashboard agents and this server cannot answer "
            f"this number's calls. Media keys: {srtp}.")


def status_lines(calling):
    calling = calling if isinstance(calling, dict) else {}
    sip = calling.get("sip") if isinstance(calling.get("sip"), dict) else {}
    lines = [describe_mode(calling)]
    if sip:
        lines.append(f"SIP call webhooks: {sip.get('webhook_delivery') or 'not reported (Meta default: DISABLED)'}")
    return lines


def plan(provider, number, phone_number_id, graph_version="v24.0", control_url=None):
    """The dry-run text for one provider. Raises SipPlanError for unusable input."""
    if provider not in PROVIDERS:
        raise SipPlanError("Provider must be elevenlabs or xai")
    if not E164.match(number or ""):
        raise SipPlanError("--number must be the WhatsApp business number in E.164 form, for example +15550100000")
    if not (phone_number_id or "").isdigit():
        raise SipPlanError("Set WHATSAPP_PHONE_NUMBER_ID (Meta's numeric phone number ID) in the env file")
    target = PROVIDERS[provider]
    if provider == "xai" and not (control_url or "").startswith("https://"):
        raise SipPlanError("--control-url must be the public https://.../webhooks/xai-sip URL of the control service")
    password = "<Meta's SIP password for this number: type it into the provider, never into a file>"
    calling = {"sip": {"status": "ENABLED", "webhook_delivery": "ENABLED",
                       "servers": [{"hostname": target["hostname"], "port": 5061}]}}
    if target["srtp"]:
        calling["srtp_key_exchange_protocol"] = target["srtp"]
    undo = {"sip": {"status": "DISABLED"}}
    if target["srtp"]:
        undo["srtp_key_exchange_protocol"] = "DTLS"
    settings_url = f"https://api.kapso.ai/meta/whatsapp/{graph_version}/{phone_number_id}/settings"
    if provider == "elevenlabs":
        provider_title = ("ElevenLabs phone number: dashboard Phone numbers > Import number > From SIP Trunk, or "
                          "POST https://api.elevenlabs.io/v1/convai/phone-numbers")
        provider_body = {"provider": "sip_trunk", "label": "WhatsApp via Kapso", "phone_number": number,
                         "agent_id": "<the SIP agent from examples/elevenlabs-sip>",
                         "inbound_trunk_config": {"media_encryption": "required", "remote_domains": ["wa.meta.vc"],
                                                  "credentials": {"username": number[1:], "password": password}}}
    else:
        provider_title = "xAI phone number: POST https://api.x.ai/v2/phone-numbers (returns a signing secret once)"
        provider_body = {"origin": "byo_trunk", "name": "WhatsApp via Kapso", "phone_number": number,
                         "webhook": {"url": control_url},
                         "sip_auth": {"auth_username": number[1:], "auth_password": password}}
    lines = [f"Dry run: route WhatsApp calls for {number} to {target['label']} over SIP. Nothing is sent.", "",
             "Blocked. Do not apply this plan until each item is resolved:"]
    lines += [f"  {i}. {text}" for i, text in enumerate(target["blockers"], 1)]
    lines += ["", "Mode change:",
              "  Current: run `voice-agent sip status` (read-only) and keep its output; you need it to undo.",
              f"  New: SIP only. Meta sends every call on this number to {target['hostname']}:5061 over TLS. Kapso call "
              "actions, Inbox answering, Kapso dashboard agents and this server's webhook stop answering calls on "
              "this number. Messages stay in Kapso. Meta still sends call_created and terminate webhooks to Kapso, "
              "without SDP, recordings or transcripts.", "",
              f"1. {provider_title}", json.dumps(provider_body, indent=2), "",
              "   Meta's SIP password: Meta makes one per number and app. Read it yourself only to type it into the "
              "provider (GET the settings with include_sip_credentials=true). This command never reads it.", "",
              f"2. Meta settings, through Kapso: POST {settings_url} with your X-API-Key header",
              json.dumps({"calling": calling}, indent=2), "",
              "Undo: POST the same URL with this body (calls go back to the Calling API; set "
              "srtp_key_exchange_protocol back to what `sip status` showed if that was not DTLS):",
              json.dumps({"calling": undo}, indent=2)]
    return "\n".join(lines)
