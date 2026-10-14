"""`voice-agent setup`: a guided env file for one voice provider, calendar and host.

It asks three questions, takes keys with hidden input, writes only the settings it asked about
(an atomic 0600 replace that keeps every other line), validates the result with the same rules as
`serve`, and prints the next steps for that combination. It never prints a key and makes no
network request. `voice-agent export-env --for railway` copies the server's variables for a host.
"""

from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess

from .config import ConfigError, load_settings

PROVIDERS = {
    "elevenlabs": ("ElevenLabs Agents", "ElevenLabs runs speech, the LLM and the voice from an agent you create "
                                        "with `voice-agent agent apply`. This server still carries the call audio "
                                        "and runs the tools. Needs ELEVENLABS_API_KEY."),
    "grok": ("Grok Voice", "xAI grok-voice-3.0 speech to speech, configured per call from this repository. This "
                           "server still carries the call audio and runs the tools. Needs XAI_API_KEY only."),
}
CALENDARS = {
    "calcom": ("Cal.com", "Real availability and bookings. Needs CAL_API_KEY and agent/business.json."),
    "local": ("Local development calendar", "Offline sample calendar in DATA_DIR. Never for real callers."),
}
HOSTS = {
    "local": ("This computer", "Browser test calls only. WhatsApp needs a public host."),
    "vps": ("Linux server with a public IP", "WhatsApp audio reaches the server directly over UDP."),
    "railway": ("Railway", "HTTPS only, no inbound UDP: call audio relays through Cloudflare TURN."),
}
KEY_HELP = {
    "ELEVENLABS_API_KEY": "ElevenLabs API key (elevenlabs.io > Developers > API keys)",
    "XAI_API_KEY": "xAI API key (console.x.ai > API keys)",
    "CAL_API_KEY": "Cal.com API key (Settings > Security > API keys)",
    "KAPSO_API_KEY": "Kapso project API key",
    "CLOUDFLARE_TURN_KEY_ID": "Cloudflare Realtime TURN key ID",
    "CLOUDFLARE_TURN_API_TOKEN": "Cloudflare Realtime TURN key API token",
}
PLAIN = {"WHATSAPP_PHONE_NUMBER_ID": "Meta phone number ID (numeric, not the display number)",
         "CLOUDFLARE_TURN_KEY_ID": KEY_HELP["CLOUDFLARE_TURN_KEY_ID"]}
VALUE = re.compile(r"^[A-Za-z0-9._~+/=:@-]{1,512}$")
CLIPBOARDS = (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip.exe"])
# What a hosted server reads. DATA_DIR comes from the image; DEV_AGENT_WS_URL is loopback-only.
SERVER_KEYS = ("VOICE_PROVIDER", "XAI_API_KEY", "XAI_SIP_WEBHOOK_SECRET", "ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID", "CAL_API_KEY", "CALENDAR",
               "OPERATOR_TOKEN", "CALLER_KEY_SECRET", "KAPSO_API_KEY", "WHATSAPP_PHONE_NUMBER_ID",
               "WHATSAPP_WEBHOOK_SECRET", "META_GRAPH_VERSION", "ICE_SERVERS_JSON", "TURN_PROVIDER",
               "CLOUDFLARE_TURN_KEY_ID", "CLOUDFLARE_TURN_API_TOKEN", "TURN_TRANSPORT", "ENABLE_OUTBOUND",
               "MAX_SESSION_SECONDS", "MAX_CONCURRENT_CALLS", "LOCAL_CAPTURE", "CAPTURE_RETENTION_DAYS",
               "CAPTURE_MAX_COUNT")


class SetupError(Exception):
    pass


@dataclass
class Prompter:
    """Input functions, replaceable in tests. `secret` never echoes."""
    ask: callable = input
    secret: callable = getpass.getpass
    out: callable = print


def choose(prompter, title, options, current, marker="current"):
    keys = list(options)
    prompter.out(f"\n{title}")
    for number, key in enumerate(keys, 1):
        label, detail = options[key]
        prompter.out(f"  {number}. {label}{f'  ({marker})' if key == current else ''}\n     {detail}")
    default = keys.index(current) + 1 if current in keys else 1
    while True:
        answer = prompter.ask(f"Choose 1-{len(keys)} [{default}]: ").strip()
        if not answer:
            return keys[default - 1]
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        if answer in keys:
            return answer
        prompter.out(f"Enter a number from 1 to {len(keys)}.")


def needed_keys(provider, calendar, host, whatsapp):
    keys = ["XAI_API_KEY" if provider == "grok" else "ELEVENLABS_API_KEY"]
    if calendar == "calcom":
        keys.append("CAL_API_KEY")
    if host == "railway":
        keys += ["CLOUDFLARE_TURN_KEY_ID", "CLOUDFLARE_TURN_API_TOKEN"]
    if whatsapp:
        keys += ["KAPSO_API_KEY", "WHATSAPP_PHONE_NUMBER_ID"]
    return keys


def read_env(path):
    from .cli import env_bindings
    return {b.key: (b.value or "") for b in env_bindings(path.read_text()) if b.key}


def update_env(path, values):
    """Set `values` in the env file, keeping every other line. Atomic replace by a new 0600 file."""
    from .cli import env_bindings, write_new_private_file
    if path.is_symlink() or not path.is_file():
        raise SetupError(f"{path} must be a regular file")
    for key, value in values.items():
        if value and not VALUE.match(value):
            raise SetupError(f"The value for {key} has spaces, quotes or other characters an env file line cannot hold")
    bindings = env_bindings(path.read_text())
    parts = [b.original.string for b in bindings]
    for key, value in values.items():
        line = f"{key}={value}\n"
        positions = [i for i, b in enumerate(bindings) if b.key == key]
        if positions:
            # python-dotenv uses the last assignment; keep the blank lines in front of it.
            i = positions[-1]
            parts[i] = parts[i][:len(parts[i]) - len(parts[i].lstrip())] + line
        else:
            parts.append(("\n" if parts and not parts[-1].endswith("\n") else "") + line)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        write_new_private_file(temporary, "".join(parts))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def status_lines(settings, spec, host):
    from .cli import voice_label
    missing = settings.agent_missing()
    voice_missing = [m for m in missing if m.startswith(("XAI_", "ELEVENLABS_"))]
    rows = [("Voice", voice_label(settings, spec), "missing " + voice_missing[0] if voice_missing else "ready")]
    if settings.voice_provider == "elevenlabs" and settings.elevenlabs_api_key and not settings.elevenlabs_agent_id:
        rows[0] = ("Voice", "ElevenLabs Agents", "create the agent: `voice-agent agent apply --yes --save-agent-id`")
    calendar = {"calcom": "Cal.com", "local": "local development calendar"}.get(settings.calendar, "none")
    rows.append(("Calendar", calendar, "ready" if settings.calendar else "missing CAL_API_KEY"))
    rows.append(("Console", "access key set" if settings.operator_enabled else "off",
                 "`voice-agent console-key` copies it" if settings.operator_enabled else "set OPERATOR_TOKEN"))
    whatsapp = [n for n, v in (("KAPSO_API_KEY", settings.kapso_api_key), ("WHATSAPP_PHONE_NUMBER_ID", settings.phone_number_id),
                               ("WHATSAPP_WEBHOOK_SECRET", settings.webhook_secret)) if not v]
    rows.append(("WhatsApp", "Kapso", "ready" if not whatsapp else "not set up (" + ", ".join(whatsapp) + ")"))
    if host == "railway":
        media = (f"Cloudflare TURN ({settings.turn_transport})", "ready") if settings.turn_provider == "cloudflare" else \
            ("TURN relay", "missing CLOUDFLARE_TURN_KEY_ID and CLOUDFLARE_TURN_API_TOKEN")
    elif host == "vps":
        media = ("direct UDP", "open the host firewall to UDP (docs/deploy.md section 3)")
    else:
        media = ("this computer", "ready for browser calls")
    rows.append(("Call audio", *media))
    width = max(len(r[1]) for r in rows)
    return [f"  {name:<11}{value:<{width + 2}}{state}" for name, value, state in rows]


def next_steps(settings, provider, calendar, host, command):
    steps = []
    if calendar == "calcom":
        steps.append("Fill in agent/business.json (docs/calendar.md), then run `" + command + " calendar check`.")
    if provider == "elevenlabs" and not settings.elevenlabs_agent_id:
        steps.append(f"Create the ElevenLabs agent: `{command} agent apply --yes --save-agent-id`.")
    steps.append(f"Check everything offline: `{command} check`.")
    if host == "local":
        steps.append(f"Start the server: `{command} serve`. Open http://127.0.0.1:8080/operator/, sign in with the "
                     f"access key (`{command} console-key` copies it), then select Start test call.")
    elif host == "vps":
        steps.append("Copy the repository and this env file to the server, then follow docs/deploy.md "
                     "(examples/vps has a Compose file with host networking and a Caddy HTTPS proxy).")
    else:
        steps.append("Follow docs/deploy-railway.md. To fill the service's variables, run "
                     f"`{command} export-env --for railway` and paste them into the Raw Editor.")
    if provider == "grok":
        steps.append("Each test call is a real Grok Voice session that xAI bills per minute; calls say they are recorded "
                     "(xAI keeps API traffic for 30 days).")
    return steps


def run(path, command, provider=None, calendar=None, host=None, whatsapp=None, prompter=None, interactive=True):
    """Returns the printed lines' summary data for tests. Raises SetupError on a problem to report."""
    from .agent_config import load_spec
    from .cli import real_env_text, write_new_private_file
    from .config import REPO_ROOT
    prompter = prompter or Prompter()
    created = False
    if not path.exists():
        text, _ = real_env_text((REPO_ROOT / ".env.example").read_text())
        write_new_private_file(path, text)
        created = True
        prompter.out(f"Created {path} (0600) with a new console access key and local secrets.")
    current = read_env(path)
    current_provider = current.get("VOICE_PROVIDER") or "elevenlabs"
    current_calendar = current.get("CALENDAR") or ("calcom" if current.get("CAL_API_KEY") else "local")
    if interactive:
        provider = provider or choose(prompter, "Which voice provider runs the conversation?", PROVIDERS, current_provider)
        calendar = calendar or choose(prompter, "Which calendar should bookings go to?", CALENDARS, current_calendar)
        host = host or choose(prompter, "Where will the server run?", HOSTS, "local", marker="start here")
        if whatsapp is None and host != "local":
            whatsapp = prompter.ask("Connect a WhatsApp number through Kapso now? [y/N]: ").strip().lower() in ("y", "yes")
    # Without a flag, keep what the file already says.
    provider, calendar, host = provider or current_provider, calendar or current_calendar, host or "local"
    updates = {"VOICE_PROVIDER": provider}
    entered = []
    for key in needed_keys(provider, calendar, host, bool(whatsapp)):
        if current.get(key) or not interactive:
            continue
        label = PLAIN.get(key) or KEY_HELP[key]
        reader = prompter.ask if key in PLAIN else prompter.secret
        value = reader(f"{label}{'' if key in PLAIN else ' (hidden)'}, or Enter to skip: ").strip()
        if value:
            updates[key] = value
            entered.append(key)
    # CALENDAR=calcom without a key would make the file invalid; empty means "not chosen yet".
    updates["CALENDAR"] = calendar if calendar == "local" or updates.get("CAL_API_KEY") or current.get("CAL_API_KEY") else ""
    turn_ready = all((updates.get(k) or current.get(k)) for k in ("CLOUDFLARE_TURN_KEY_ID", "CLOUDFLARE_TURN_API_TOKEN"))
    if host == "railway" and turn_ready:
        updates["TURN_PROVIDER"] = "cloudflare"
    update_env(path, updates)
    try:
        settings = load_settings(env_file=path)
        spec = load_spec(settings.agent_config_path)
    except (ConfigError, ValueError) as error:
        raise SetupError(f"{path} is saved but not valid yet: {error}") from None
    overridden = sorted(k for k in updates if k in os.environ and os.environ[k] != updates[k])
    prompter.out(f"\nSaved {path}: " + ", ".join(sorted(updates)) + (" (values hidden)" if entered else ""))
    if overridden:
        prompter.out("Note: your shell sets " + ", ".join(overridden) + ", which wins over the file. Unset it first.")
    prompter.out("")
    for line in status_lines(settings, spec, host):
        prompter.out(line)
    prompter.out("\nNext:")
    for number, step in enumerate(next_steps(settings, provider, calendar, host, command), 1):
        prompter.out(f"  {number}. {step}")
    return {"created": created, "updates": sorted(updates), "entered": entered, "provider": provider,
            "calendar": calendar, "host": host}


def server_variables(path, target):
    """KEY=value lines a hosted server needs, and notes about what will not work there."""
    if not path.is_file():
        raise SetupError(f"{path} does not exist; run `voice-agent setup` first")
    values = read_env(path)
    try:
        load_settings(environ=dict(values))
    except ConfigError as error:
        raise SetupError(f"{path} is not valid: {error}") from None
    keys = SERVER_KEYS
    notes = []
    if target == "pipecat-cloud":
        # No webhook and no console in a Cloud session; Kapso's API vouches for each call (cloud.py).
        keys = tuple(k for k in SERVER_KEYS if k not in ("OPERATOR_TOKEN", "WHATSAPP_WEBHOOK_SECRET", "ENABLE_OUTBOUND",
                                                         "XAI_SIP_WEBHOOK_SECRET"))
        if not values.get("CALLER_KEY_SECRET"):
            raise SetupError("Set CALLER_KEY_SECRET in the env file first. Use the same value everywhere this number's "
                             "calls are answered, or callers lose their bookings")
        missing = [k for k in ("KAPSO_API_KEY", "WHATSAPP_PHONE_NUMBER_ID") if not values.get(k)]
        if missing:
            raise SetupError("Pipecat Cloud answers calls through Kapso: set " + " and ".join(missing) + " first")
        if values.get("CALENDAR") == "local":
            raise SetupError("CALENDAR=local is for development; a Cloud session loses it after each call. Use Cal.com")
        notes.append("Each Cloud session starts with an empty DATA_DIR: callers can change a booking only in the "
                     "call that made it (examples/pipecat-cloud/README.md).")
    lines = [f"{key}={values[key]}" for key in keys if values.get(key)]
    if target == "railway":
        # Railway mounts volumes as root; the entrypoint hands DATA_DIR to the app user and drops root.
        # Host names without ports: Railway's deploy health check and the image's own health check.
        lines += ["RAILWAY_RUN_UID=0", "ALLOWED_HOSTS=${{RAILWAY_PUBLIC_DOMAIN}},healthcheck.railway.app,127.0.0.1"]
        if values.get("TURN_PROVIDER") != "cloudflare" and values.get("ICE_SERVERS_JSON", "[]") in ("", "[]"):
            notes.append("No TURN relay is set: browser and WhatsApp audio will not connect on Railway.")
    if values.get("CALENDAR") == "local":
        notes.append("CALENDAR=local keeps bookings in the server's volume; use Cal.com for real callers.")
    if values.get("DEV_AGENT_WS_URL"):
        notes.append("DEV_AGENT_WS_URL is left out: the offline fake agent runs only on this computer.")
    return lines, notes


def export_env(path, target, show=False, out=print, file=None):
    """Copy (or with show, print; or with file, write to a new 0600 file) the server's variables
    for the host's bulk variable editor or secret store."""
    from .cli import write_new_private_file
    lines, notes = server_variables(path, target)
    text = "\n".join(lines) + "\n"
    if file:
        Path(file).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            write_new_private_file(file, text)
        except FileExistsError:
            raise SetupError(f"{file} already exists; choose a new file name") from None
        out(f"Wrote {len(lines)} variables for {target} to {file} (0600): " + ", ".join(line.split("=", 1)[0] for line in lines))
        out("It holds secrets. Delete it after you upload it.")
    elif show:
        out(text, end="")
    elif copy_to_clipboard(text):
        out(f"Copied {len(lines)} variables for {target} to the clipboard: " + ", ".join(line.split("=", 1)[0] for line in lines))
    else:
        raise SetupError("No clipboard tool found (pbcopy, wl-copy, xclip). Add --show to print them in a private terminal")
    for note in notes:
        out("Note: " + note)
    return lines


def railway_variables(path, yes=False, out=print, runner=subprocess.run):
    """Set the server's variables on the Railway service linked to this directory. Dry run unless
    yes. Each value goes through `railway variable set KEY --stdin`, never the command line."""
    lines, notes = server_variables(path, "railway")
    names = [line.split("=", 1)[0] for line in lines]
    if not shutil.which("railway"):
        raise SetupError("Install the Railway CLI (https://docs.railway.com/cli), then `railway login` and `railway link`")
    if not yes:
        out(f"Dry run: would set {len(names)} variables on the linked Railway service: " + ", ".join(names))
        out("Each value is sent through stdin (`railway variable set KEY --stdin --skip-deploys`). Add --yes to set them.")
        for note in notes:
            out("Note: " + note)
        return names
    for line in lines:
        key, value = line.split("=", 1)
        result = runner(["railway", "variable", "set", key, "--stdin", "--skip-deploys"], input=value.encode(),
                        capture_output=True, timeout=60)
        if result.returncode != 0:
            raise SetupError(f"`railway variable set {key}` failed (exit {result.returncode}). Run `railway link` in this "
                             "directory and pick the voice-agent service, then try again")
    out(f"Set {len(names)} variables on the linked Railway service. Deploy with `railway up` (see docs/deploy-railway.md).")
    for note in notes:
        out("Note: " + note)
    return names


def copy_to_clipboard(text):
    """True when a local clipboard tool took the text. Nothing is printed or logged."""
    for command in CLIPBOARDS:
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text.encode(), check=True, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                continue
            return True
    return False


def default_path(env_file):
    from .config import REPO_ROOT
    return Path(env_file) if env_file else REPO_ROOT / ".env"
