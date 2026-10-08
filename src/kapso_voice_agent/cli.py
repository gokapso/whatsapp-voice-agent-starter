"""`voice-agent` command line. Run `voice-agent --help`.

Start here: `voice-agent init` writes a private .env with fresh local secrets.

Offline commands (no network, no credentials): init, check, agent plan, tools schema,
tools call (with CALENDAR=local), kapso webhook (without --yes), artifacts prune, dev fake-agent,
dev init-env, secrets.
Network commands (operator only): serve, agent apply --yes, agent verify, kapso webhook --yes,
artifacts fetch, calendar check (read-only), tools call (with Cal.com; writes need --yes).
"""

import argparse
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys

from dotenv.parser import parse_stream

from .agent_config import build_config, load_spec
from .config import REPO_ROOT, ConfigError, load_settings
from .tools import ToolRunner, provider_tool_definitions


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def context(args):
    try:
        settings = load_settings(env_file=args.env_file)
        spec = load_spec(settings.agent_config_path)
    except (ConfigError, ValueError, OSError) as error:
        fail(str(error))
    return settings, spec


def env_path(args):
    """The env file commands read: --env-file, else the repository's .env."""
    return Path(args.env_file) if args.env_file else REPO_ROOT / ".env"


def shown_path(path):
    path = Path(path)
    resolved = path.resolve()
    return str(resolved.relative_to(REPO_ROOT)) if resolved.is_relative_to(REPO_ROOT) else str(path)


def write_new_private_file(path, text):
    """Create `path` readable only by this user (0600). Never overwrites: O_EXCL fails on any
    existing file or symlink."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as out:
        out.write(text)


def business_timezone(settings, spec, required=True):
    """The time zone the agent config is rendered with: business.json's, or the development
    calendar's with CALENDAR=local."""
    if spec.business.timezone_name:
        return spec.business.timezone_name
    if settings.calendar == "local":
        return json.loads(spec.dev_calendar_path.read_text())["timezone"]
    if required:
        fail(f"Set timezone in {shown_path(spec.business_path)} (IANA name, e.g. America/New_York)")
    return "UTC"


def cmd_check(args):
    settings, spec = context(args)
    config = build_config(spec, business_timezone(settings, spec, required=False))
    tools = config["conversation_config"]["agent"]["prompt"]["tools"]
    problems = []
    if settings.max_session_seconds < spec.max_duration_seconds:
        problems.append("MAX_SESSION_SECONDS is below limits.max_duration_seconds; the bridge would cut calls short")
    if any(t.get("pre_tool_speech") != "off" or t.get("tool_call_sound") for t in tools):
        problems.append("A tool has pre-tool speech or a tool call sound")
    if config["conversation_config"]["turn"]["soft_timeout_config"]["timeout_seconds"] != -1:
        problems.append("The soft-timeout filler is enabled")
    report = {
        "agent_name": spec.name, "voice_id": spec.raw["voice"]["voice_id"],
        # Offline, so this is intent only. At call time the server reads the stored agent's
        # record_voice back and picks the greeting from that (see recording.py).
        "recording": {"agent_toml_record_audio": spec.record_audio, "local_capture": settings.local_capture,
                      "greeting_decided": "per call, from the provider's stored record_voice plus LOCAL_CAPTURE"},
        "greetings": {d: {"recorded": spec.greeting(d, True), "not_recorded": spec.greeting(d, False)}
                      for d in ("inbound", "outbound")},
        "tools": [t["name"] for t in tools],
        "calendar": settings.calendar or "none",
        # Filled in by the owner before Cal.com bookings work; `calendar check` reads the account.
        "business_missing": spec.business.calcom_problems() if settings.calendar != "local" else [],
        "ready": {"calls": settings.calls_ready, "agent": settings.agent_ready, "operator_console": settings.operator_enabled,
                  "outbound": settings.enable_outbound, "local_capture": settings.local_capture},
        "agent_missing": settings.agent_missing(),
        "set": sorted(k for k, v in {"CAL_API_KEY": settings.cal_api_key,
                                     "KAPSO_API_KEY": settings.kapso_api_key, "WHATSAPP_PHONE_NUMBER_ID": settings.phone_number_id,
                                     "WHATSAPP_WEBHOOK_SECRET": settings.webhook_secret,
                                     "ELEVENLABS_API_KEY": settings.elevenlabs_api_key,
                                     "ELEVENLABS_AGENT_ID": settings.elevenlabs_agent_id,
                                     "OPERATOR_TOKEN": settings.operator_token,
                                     "CALLER_KEY_SECRET": settings.caller_key_secret}.items() if v),
        "problems": problems,
    }
    print(json.dumps(report, indent=2))
    if problems:
        raise SystemExit(1)


def cmd_agent_plan(args):
    settings, spec = context(args)
    config = build_config(spec, business_timezone(settings, spec))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=2) + "\n")
    shown = out.resolve().relative_to(REPO_ROOT) if out.resolve().is_relative_to(REPO_ROOT) else out
    result = {"dry_run": True, "wrote": str(shown), "would": "update" if settings.elevenlabs_agent_id else "create",
              "network_requests": 0}
    if args.schema:
        from .provider import validate_against_openapi
        errors = validate_against_openapi(config, json.loads(Path(args.schema).read_text()))
        result["schema_errors"] = errors[:20]
        if errors:
            print(json.dumps(result, indent=2))
            raise SystemExit(1)
    print(json.dumps(result, indent=2))


AGENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def env_bindings(text):
    """The env file's statements as python-dotenv parses them (load_settings reads it the same
    way). Joining every `original.string` gives back the file unchanged."""
    return list(parse_stream(io.StringIO(text)))


def agent_id_destination(args, settings):
    """Where --save-agent-id will write, checked before anything is created. None when not needed."""
    if not args.save_agent_id or settings.elevenlabs_agent_id:
        return None
    path = env_path(args)
    if path.is_symlink() or not path.is_file():
        fail(f"--save-agent-id needs an existing env file at {shown_path(path)} (create it with `voice-agent init`)")
    assigned = [b for b in env_bindings(path.read_text()) if b.key == "ELEVENLABS_AGENT_ID"]
    if len(assigned) > 1:
        fail(f"{shown_path(path)} sets ELEVENLABS_AGENT_ID {len(assigned)} times and only the last one counts; "
             "keep one line, then run again")
    if assigned and (assigned[0].value or "").strip():
        fail(f"{shown_path(path)} already sets ELEVENLABS_AGENT_ID, but the environment overrides it with an empty value")
    return path


def save_agent_id(path, agent_id):
    """Set ELEVENLABS_AGENT_ID in the env file, keeping every other line. The file is replaced
    atomically by a new 0600 file, so a crash never leaves half a file."""
    if not AGENT_ID.fullmatch(str(agent_id)):
        fail("The provider returned an unexpected agent_id; not saving it")
    line = f"ELEVENLABS_AGENT_ID={agent_id}\n"
    bindings = env_bindings(path.read_text())
    parts = [b.original.string for b in bindings]
    assigned = [i for i, b in enumerate(bindings) if b.key == "ELEVENLABS_AGENT_ID"]
    if assigned:
        # Replace the assignment python-dotenv uses (the last one), keeping blank lines before it.
        i = assigned[-1]
        parts[i] = parts[i][:len(parts[i]) - len(parts[i].lstrip())] + line
    else:
        parts.append(("\n" if parts and not parts[-1].endswith("\n") else "") + line)
    text = "".join(parts)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        write_new_private_file(temporary, text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cmd_agent_apply(args):
    from .provider import SetupError, apply_agent, dumps, elevenlabs_client
    settings, spec = context(args)
    config = build_config(spec, business_timezone(settings, spec))
    destination = agent_id_destination(args, settings)
    if not args.yes:
        plan = {"dry_run": True, "would": "update" if settings.elevenlabs_agent_id else "create",
                "agent_name": config["name"], "hint": "Review `voice-agent agent plan`, then add --yes"}
        if destination:
            plan["would_save_agent_id_to"] = shown_path(destination)
        print(json.dumps(plan, indent=2))
        return
    with elevenlabs_client(settings.elevenlabs_api_key) as client:
        try:
            result = apply_agent(settings, config, client)
        except SetupError as error:
            fail(str(error))
    print(dumps(result, settings))
    if result["action"] == "created":
        if destination:
            save_agent_id(destination, result["agent_id"])
            print(f"Saved ELEVENLABS_AGENT_ID to {shown_path(destination)}.", file=sys.stderr)
        else:
            print("Set ELEVENLABS_AGENT_ID to the agent_id above (or use --save-agent-id next time).", file=sys.stderr)
        print("A server that is already running does not see the new ID: restart it (recreate a container).",
              file=sys.stderr)
    else:
        print("A running server re-reads the agent's recording setting within 60 s; restart it to apply now.",
              file=sys.stderr)


def cmd_agent_verify(args):
    from .provider import SetupError, dumps, elevenlabs_client, verify_agent
    settings, spec = context(args)
    config = build_config(spec, business_timezone(settings, spec))
    with elevenlabs_client(settings.elevenlabs_api_key) as client:
        try:
            result = verify_agent(settings, config, client)
        except SetupError as error:
            fail(str(error))
    print(dumps(result, settings))
    if not result["verified"]:
        raise SystemExit(1)


def cmd_kapso_webhook(args):
    import httpx

    from .provider import SetupError, apply_webhook, dumps, webhook_plan
    settings, _ = context(args)
    try:
        if not args.yes:
            print(dumps(webhook_plan(settings, args.url), settings))
            return
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            print(dumps(apply_webhook(settings, args.url, client), settings))
    except SetupError as error:
        fail(str(error))


WRITE_TOOLS = ("book_appointment", "reschedule_appointment", "cancel_appointment")


def tools_calendar(args, settings, spec):
    """Cal.com when it is configured (real reads; writes only with --yes), otherwise the local
    development calendar in --db."""
    if settings.calendar == "calcom":
        from .calcom import CalComCalendar
        if args.now or args.db:
            fail("--now and --db apply to the local development calendar only; this env uses Cal.com")
        if args.name in WRITE_TOOLS and not args.yes:
            fail(f"{args.name} changes your real Cal.com calendar (and Cal.com may email the attendee); add --yes")
        try:
            return CalComCalendar(settings.data_dir / "calendar.sqlite3", spec.business, settings.cal_api_key)
        except ValueError as error:
            fail(str(error))
    from .store import AppointmentStore
    clock = None
    if args.now:
        try:
            fixed = datetime.fromisoformat(args.now)
        except ValueError:
            fail("--now must be an ISO datetime with offset, e.g. 2026-11-02T09:00:00-06:00")
        clock = lambda: fixed  # noqa: E731
    return AppointmentStore(Path(args.db or REPO_ROOT / "data/offline-tools.sqlite3"), spec.business,
                            spec.dev_calendar_path, clock=clock)


def cmd_tools_schema(args):
    print(json.dumps(provider_tool_definitions(), indent=2))


def cmd_tools_call(args):
    settings, spec = context(args)
    try:
        parameters = json.loads(args.parameters)
    except ValueError:
        fail("parameters must be JSON, e.g. '{\"day\": \"2026-11-03\"}'")
    runner = ToolRunner(tools_calendar(args, settings, spec), "offline:" + args.caller)
    result = runner.execute({"tool_name": args.name, "tool_call_id": "", "parameters": parameters})
    print(json.dumps(json.loads(result["result"]), indent=2))
    if result["is_error"]:
        raise SystemExit(1)


def cmd_calendar_check(args):
    from .calcom import CalComClient, CalendarUnavailable
    settings, spec = context(args)
    if not settings.cal_api_key:
        fail("Set CAL_API_KEY (create one in Cal.com under Settings > Security > API keys)")
    business = spec.business
    configured = [s.get("cal_event_type_id") for s in business.services]
    try:
        account = CalComClient(settings.cal_api_key).account(configured)
    except CalendarUnavailable as error:
        fail(f"Cal.com could not be read ({error}); check CAL_API_KEY")
    problems = business.calcom_problems() + [f"cal_event_type_id {i} is not one of this account's event types"
                                             for i in account.pop("configured_not_found")]
    if business.timezone_name and account["profile_timezone"] and business.timezone_name != account["profile_timezone"]:
        problems.append(f"business.json timezone {business.timezone_name} differs from the Cal.com profile's "
                        f"{account['profile_timezone']}; times are spoken in business.json's")
    print(json.dumps({**account, "business_file": shown_path(spec.business_path), "problems": problems,
                      "network_requests": "read-only"}, indent=2))
    if problems:
        raise SystemExit(1)


def cmd_artifacts_fetch(args):
    from .artifacts import ArtifactError, capture_manifest, fetch
    settings, _ = context(args)
    if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id):
        fail("Set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID")
    try:
        conversation_id = args.conversation_id
        if not conversation_id:
            _, manifest = capture_manifest(settings.data_dir / "captures", args.capture)
            conversation_id = manifest["conversation_id"]
        report = fetch(conversation_id, settings.elevenlabs_agent_id, settings.elevenlabs_api_key,
                       settings.data_dir / "vendor", args.wait_seconds,
                       retention_days=settings.capture_retention_days, max_count=settings.capture_max_count)
    except ArtifactError as error:
        fail(str(error))
    print(json.dumps(report, indent=2))


def cmd_artifacts_prune(args):
    from .private import prune_local_artifacts
    settings, _ = context(args)
    removed = prune_local_artifacts(settings.data_dir, settings.capture_retention_days, settings.capture_max_count)
    print(json.dumps({"removed": removed, "retention_days": settings.capture_retention_days,
                      "max_count": settings.capture_max_count,
                      "scope": "local copies only; provider and Meta copies follow their own retention"}, indent=2))


def serve_banner(settings, host, port, env_label):
    """What `serve` prints at startup: where to go and what is missing. Never a secret value."""
    local = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "[::1]"}.get(host, f"[{host}]" if ":" in host else host)
    base = f"http://{local}:{port}"
    lines = [f"Voice agent server on {base} (env: {env_label})"]
    if settings.operator_enabled:
        lines.append(f"Operator console: {base}/operator/  (paste OPERATOR_TOKEN from {env_label})")
    else:
        lines.append("Operator console: off (set OPERATOR_TOKEN, or run `voice-agent init`)")
    if settings.dev_agent_ws_url:
        lines.append("Agent: offline fake agent (development only: a tone and an echo, not a conversation)")
    if settings.calendar == "calcom":
        lines.append("Calendar: Cal.com (real availability and bookings)")
    elif settings.calendar == "local":
        lines.append("Calendar: local development calendar (bookings stay in DATA_DIR; not for real callers)")
    missing = settings.agent_missing()
    lines.append("Browser calls: ready" if not missing else "Browser calls: not ready, missing " + "; ".join(missing))
    whatsapp = [name for name, value in (("KAPSO_API_KEY", settings.kapso_api_key),
                                         ("WHATSAPP_PHONE_NUMBER_ID", settings.phone_number_id),
                                         ("WHATSAPP_WEBHOOK_SECRET", settings.webhook_secret)) if not value]
    if settings.calls_ready:
        lines.append("WhatsApp calls: ready at POST /webhooks/whatsapp (behind your public HTTPS URL)")
    else:
        lines.append("WhatsApp calls: not ready, missing " + "; ".join(whatsapp + missing))
    return lines


def cmd_serve(args):
    # Pipecat/aiortc log connection details at DEBUG; keep them out of production logs by default.
    os.environ.setdefault("LOGURU_LEVEL", "INFO")
    import uvicorn

    from .app import create_app
    settings, spec = context(args)
    try:
        app = create_app(settings, spec)
    except ValueError as error:
        fail(str(error))
    path = env_path(args)
    label = shown_path(path) if path.is_file() else "process environment only"
    print("\n".join(serve_banner(settings, args.host, args.port, label)), flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", proxy_headers=False)


def cmd_fake_agent(args):
    from .fake_agent import main
    main(args.port)


DEV_ENV_TEMPLATE = """# Local offline development only (fake agent, no accounts). Never use these values in production.
OPERATOR_TOKEN={operator_token}
CALLER_KEY_SECRET={caller_key_secret}
DEV_AGENT_WS_URL=ws://127.0.0.1:{port}
# Offline development calendar (agent/dev-calendar.json); bookings stay in DATA_DIR only.
CALENDAR=local
"""


def cmd_init_env(args):
    path = Path(args.path)
    values = {"operator_token": secrets.token_urlsafe(32), "caller_key_secret": secrets.token_hex(32), "port": args.port}
    try:
        write_new_private_file(path, DEV_ENV_TEMPLATE.format(**values))
    except FileExistsError:
        fail(f"{path} already exists; reuse it (grep OPERATOR_TOKEN {path}) or delete it first")
    print(f"Wrote {path} (0600).")
    print(f"Operator token (paste it into the console): {values['operator_token']}")
    print(f"Show it again later: grep OPERATOR_TOKEN {path}")


ENV_HEADER = """# Created by `voice-agent init`. Private (0600): never commit or share it.
# Browser call: set ELEVENLABS_API_KEY and CAL_API_KEY, fill in agent/business.json, then
# `voice-agent calendar check` and `voice-agent agent apply --yes --save-agent-id`.
# WhatsApp: also set KAPSO_API_KEY and WHATSAPP_PHONE_NUMBER_ID (see docs/deploy.md).
"""
GENERATED = ("OPERATOR_TOKEN", "CALLER_KEY_SECRET", "WHATSAPP_WEBHOOK_SECRET")


def real_env_text(example):
    """.env.example with fresh local secrets filled in. Provider keys stay empty for the operator."""
    values = {"OPERATOR_TOKEN": secrets.token_urlsafe(32), "CALLER_KEY_SECRET": secrets.token_hex(32),
              "WHATSAPP_WEBHOOK_SECRET": secrets.token_hex(32)}
    lines = [line for line in example.splitlines() if not line.startswith("# Template for .env")]
    for name, value in values.items():
        positions = [index for index, line in enumerate(lines) if line == f"{name}="]
        if len(positions) != 1:
            raise ValueError(f".env.example must contain exactly one empty {name}= line")
        lines[positions[0]] = f"{name}={value}"
    return ENV_HEADER + "\n".join(lines) + "\n", values


def cmd_init(args):
    path = env_path(args)
    try:
        text, values = real_env_text((REPO_ROOT / ".env.example").read_text())
        write_new_private_file(path, text)
    except FileExistsError:
        fail(f"{shown_path(path)} already exists; it was not changed. Edit it, or move it away and run init again")
    except (OSError, ValueError) as error:
        fail(str(error))
    print(f"Wrote {shown_path(path)} (0600) with a new " + ", ".join(GENERATED) + ".")
    print(f"Operator token (paste it into the console): {values['OPERATOR_TOKEN']}")
    print(f"Show it again later: grep OPERATOR_TOKEN {shown_path(path)}")
    print("Next: put your ElevenLabs and Cal.com API keys in ELEVENLABS_API_KEY and CAL_API_KEY, fill in "
          "agent/business.json, run `uv run voice-agent calendar check`, then "
          "`uv run voice-agent agent apply --yes --save-agent-id`.")


def cmd_secrets(args):
    print(f"OPERATOR_TOKEN={secrets.token_urlsafe(32)}")
    print(f"WHATSAPP_WEBHOOK_SECRET={secrets.token_hex(32)}")
    print(f"CALLER_KEY_SECRET={secrets.token_hex(32)}")


def parser():
    root = argparse.ArgumentParser(prog="voice-agent", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("--env-file", default=None, help="Path to a .env file (default: repo .env if present)")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Write a private .env (or --env-file) with fresh local secrets; never overwrites").set_defaults(
        func=cmd_init)
    commands.add_parser("check", help="Validate config, prompt, greetings and tool behavior offline").set_defaults(func=cmd_check)

    serve = commands.add_parser("serve", help="Run the webhook receiver, bridge and operator console")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=cmd_serve)

    agent = commands.add_parser("agent", help="ElevenLabs agent config").add_subparsers(dest="agent_command", required=True)
    plan = agent.add_parser("plan", help="Render build/agent-config.json (offline)")
    plan.add_argument("--out", default=str(REPO_ROOT / "build/agent-config.json"))
    plan.add_argument("--schema", help="Optional ElevenLabs OpenAPI JSON file to validate against")
    plan.set_defaults(func=cmd_agent_plan)
    apply = agent.add_parser("apply", help="Create/update the agent (dry run unless --yes)")
    apply.add_argument("--yes", action="store_true", help="Really send the config to ElevenLabs")
    apply.add_argument("--save-agent-id", action="store_true",
                       help="After creating a new agent, write its ID to the env file (checked before anything is sent)")
    apply.set_defaults(func=cmd_agent_apply)
    agent.add_parser("verify", help="Read the stored agent back and compare (read-only)").set_defaults(func=cmd_agent_verify)

    kapso = commands.add_parser("kapso", help="Kapso setup").add_subparsers(dest="kapso_command", required=True)
    webhook = kapso.add_parser("webhook", help="Create the number's Meta webhook (dry run unless --yes)")
    webhook.add_argument("--url", required=True, help="Public https://.../webhooks/whatsapp URL of this server")
    webhook.add_argument("--yes", action="store_true", help="Really create it (refuses if one exists)")
    webhook.set_defaults(func=cmd_kapso_webhook)

    tools = commands.add_parser("tools", help="Inspect and exercise tools offline").add_subparsers(dest="tools_command", required=True)
    tools.add_parser("schema", help="Print tool definitions sent to the provider").set_defaults(func=cmd_tools_schema)
    call = tools.add_parser("call", help="Run one tool against the configured calendar (Cal.com, or the local development one)")
    call.add_argument("name")
    call.add_argument("parameters", nargs="?", default="{}")
    call.add_argument("--caller", default="cli", help="Caller label (stands in for the call's identity)")
    call.add_argument("--db", help="Local development calendar file (default data/offline-tools.sqlite3)")
    call.add_argument("--now", help="Fixed clock for the local development calendar, ISO datetime with offset")
    call.add_argument("--yes", action="store_true", help="Allow book/reschedule/cancel against the real Cal.com calendar")
    call.set_defaults(func=cmd_tools_call)

    calendar = commands.add_parser("calendar", help="Cal.com setup").add_subparsers(dest="calendar_command", required=True)
    calendar.add_parser("check", help="Read the Cal.com profile and event types and check business.json (read-only)").set_defaults(
        func=cmd_calendar_check)

    artifacts = commands.add_parser("artifacts", help="Provider artifacts").add_subparsers(dest="artifacts_command", required=True)
    fetch = artifacts.add_parser("fetch", help="Download ElevenLabs MP3 + conversation JSON privately")
    fetch.add_argument("--capture", help="Local capture directory name (default: latest with a conversation)")
    fetch.add_argument("--conversation-id")
    fetch.add_argument("--wait-seconds", type=int, default=0)
    fetch.set_defaults(func=cmd_artifacts_fetch)
    artifacts.add_parser("prune", help="Delete expired local captures and provider downloads (offline)").set_defaults(
        func=cmd_artifacts_prune)

    dev = commands.add_parser("dev", help="Offline development helpers").add_subparsers(dest="dev_command", required=True)
    fake = dev.add_parser("fake-agent", help="Run the offline fake agent WebSocket")
    fake.add_argument("--port", type=int, default=8765)
    fake.set_defaults(func=cmd_fake_agent)
    init_env = dev.add_parser("init-env", help="Write a private .env.dev for the offline fake agent (development only)")
    init_env.add_argument("--path", default=".env.dev")
    init_env.add_argument("--port", type=int, default=8765, help="Fake agent port")
    init_env.set_defaults(func=cmd_init_env)

    commands.add_parser("secrets", help="Print fresh random secrets for .env (local only)").set_defaults(func=cmd_secrets)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    args.func(args)

