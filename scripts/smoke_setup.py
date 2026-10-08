#!/usr/bin/env python3
"""Development check: the setup commands in a clean environment, as real processes, fully offline.

Nothing from the caller's environment or any .env file reaches the server: the processes get only
PATH, a temporary HOME and DATA_DIR, and the env files written by the commands under test.

1. README step 1 before the ElevenLabs key: `voice-agent init` writes a private .env, `check`
   reports only the ElevenLabs agent as missing, and `serve` prints the console URL (never a
   secret) and refuses a browser call with a clear reason.
2. docs/development.md: `dev init-env` + the offline fake agent + `serve`, then a browser-path
   call over real HTTP/WebRTC that hears the fake agent's tone and runs one tool, hangup, and
   private data permissions.

The fake agent plays a tone and echoes audio. This proves the media path and setup commands, not
a conversation; the real conversation check is in docs/testing.md. Prints one JSON summary.

  uv run python scripts/smoke_setup.py
"""

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_local import browser_call, free_port, wait_http  # noqa: E402

CLI = [sys.executable, "-m", "kapso_voice_agent"]


def mode(path):
    return oct(stat.S_IMODE(path.stat().st_mode))


def env_value(path, name):
    return next(line.split("=", 1)[1] for line in path.read_text().splitlines() if line.startswith(name + "="))


def run(args, env, cwd):
    return subprocess.run([*CLI, *args], env=env, cwd=cwd, capture_output=True, text=True)


def serve(env_file, port, env, cwd):
    return subprocess.Popen([*CLI, "--env-file", str(env_file), "serve", "--port", str(port)], env=env, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)


def stop(processes):
    for process in processes:
        process.terminate()
    output = []
    for process in processes:
        out, _ = process.communicate(timeout=10)
        output.append(out or "")
    return output


def before_elevenlabs_key(temp, env):
    env_file = temp / ".env"
    init = run(["--env-file", str(env_file), "init"], env, temp)
    again = run(["--env-file", str(env_file), "init"], env, temp)
    check = run(["--env-file", str(env_file), "check"], env, temp)
    token = env_value(env_file, "OPERATOR_TOKEN")
    secrets = [token, env_value(env_file, "CALLER_KEY_SECRET"), env_value(env_file, "WHATSAPP_WEBHOOK_SECRET")]
    port = free_port()
    server = serve(env_file, port, env, temp)
    try:
        base = f"http://127.0.0.1:{port}"
        wait_http(base + "/healthz")
        headers = {"authorization": f"Bearer {token}"}
        state = httpx.get(base + "/operator/api/state", headers=headers).json()
        refused = httpx.post(base + "/operator/api/browser-call", headers=headers, json={"sdp": "v=0 synthetic offer"})
    finally:
        [banner] = stop([server])
    report = json.loads(check.stdout)
    return {
        "init_exit": init.returncode, "init_printed_token": token in init.stdout,
        "init_refuses_existing_file": again.returncode != 0, "env_file_mode": mode(env_file),
        "check_exit": check.returncode, "check_missing": report["agent_missing"],
        "banner": banner.strip().splitlines(), "banner_has_no_secret": not any(s in banner for s in secrets),
        "agent_ready": state["agent_ready"], "browser_call_status": refused.status_code,
        "browser_call_detail": refused.json().get("detail", ""),
    }


def fake_agent_browser_call(temp, env):
    agent_port, port = free_port(), free_port()
    env_file = temp / ".env.dev"
    init = run(["dev", "init-env", "--path", str(env_file), "--port", str(agent_port)], env, temp)
    token = env_value(env_file, "OPERATOR_TOKEN")
    processes = [subprocess.Popen([*CLI, "dev", "fake-agent", "--port", str(agent_port)], env=env, cwd=temp,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                 serve(env_file, port, env, temp)]
    try:
        base = f"http://127.0.0.1:{port}"
        wait_http(base + "/healthz")
        state = httpx.get(base + "/operator/api/state", headers={"authorization": f"Bearer {token}"}).json()
        call = asyncio.run(browser_call(base, token))
    finally:
        _, banner = stop(processes)
    return {
        "token_printed_by_init_env": token in init.stdout, "env_file_mode": mode(env_file),
        "banner_says_fake_agent": "offline fake agent" in banner, "banner_has_no_secret": token not in banner,
        "agent_ready": state["agent_ready"], "calls_ready": state["calls_ready"], "recording": state["recording"],
        "browser_call": call, "data_dir_mode": mode(temp / "data"), "store_mode": mode(temp / "data" / "appointments.sqlite3"),
    }


def main():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(temp), "DATA_DIR": str(temp / "data"),
               "LOGURU_LEVEL": "WARNING"}
        real = before_elevenlabs_key(temp, env)
        dev = fake_agent_browser_call(temp, env)
    real_ok = (real["init_exit"] == 0 and real["init_printed_token"] and real["init_refuses_existing_file"]
               and real["env_file_mode"] == "0o600" and real["check_exit"] == 0
               and real["check_missing"] == ["ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID (or DEV_AGENT_WS_URL)"]
               and any(line.startswith("Operator console: http://127.0.0.1:") for line in real["banner"])
               and real["banner_has_no_secret"] and real["agent_ready"] is False
               and real["browser_call_status"] == 503 and "ELEVENLABS_API_KEY" in real["browser_call_detail"])
    call = dev["browser_call"]
    dev_ok = (dev["token_printed_by_init_env"] and dev["env_file_mode"] == "0o600" and dev["banner_says_fake_agent"]
              and dev["banner_has_no_secret"] and dev["agent_ready"] and not dev["calls_ready"]
              and dev["recording"]["provider_recording"] == "off (offline fake agent)"
              and dev["recording"]["next_greeting_says_recorded"] is False
              and call["hangup_status"] == 200 and call["tool_ran"]
              and dev["data_dir_mode"] == "0o700" and dev["store_mode"] == "0o600")
    print(json.dumps({"ok": real_ok and dev_ok, "init_before_elevenlabs_key": real, "fake_agent_browser_call": dev}, indent=2))
    return 0 if real_ok and dev_ok else 1


if __name__ == "__main__":
    sys.exit(main())
