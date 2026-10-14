"""`voice-agent setup` and `export-env`: offline, no prompts reach a terminal, no key is printed."""

import os

import pytest

from kapso_voice_agent import cli, setup_wizard
from kapso_voice_agent.config import load_settings
from kapso_voice_agent.setup_wizard import Prompter, SetupError, export_env, run, update_env

XAI_KEY, CF_TOKEN = "xai-wizard-key-not-real", "cf-wizard-token-not-real"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("KAPSO_", "ELEVENLABS_", "WHATSAPP_", "OPERATOR_", "CALLER_", "DEV_AGENT_", "CAL_", "CALENDAR",
                           "VOICE_", "XAI_", "TURN_", "CLOUDFLARE_", "AGENT_CONFIG_PATH", "DATA_DIR")):
            monkeypatch.delenv(key)


def scripted(answers, secrets_):
    printed = []
    answers, secrets_ = iter(answers), iter(secrets_)
    prompter = Prompter(ask=lambda prompt: (printed.append(prompt), next(answers))[1],
                        secret=lambda prompt: (printed.append(prompt), next(secrets_))[1],
                        out=lambda *parts, **_: printed.append(" ".join(str(p) for p in parts)))
    return prompter, printed


def mode(path):
    return oct(path.stat().st_mode & 0o777)


def test_grok_on_railway_writes_a_valid_private_file_and_never_prints_keys(tmp_path):
    path = tmp_path / ".env"
    prompter, printed = scripted(["2", "2", "3", "n", "cf-key-id-1"], [XAI_KEY, CF_TOKEN])
    result = run(path, "voice-agent", prompter=prompter)
    settings = load_settings(env_file=path)
    text = "\n".join(printed)
    assert result["created"] and mode(path) == "0o600"
    assert settings.voice_provider == "grok" and settings.xai_api_key == XAI_KEY and settings.calendar == "local"
    assert settings.turn_provider == "cloudflare" and settings.cloudflare_turn_api_token == CF_TOKEN
    assert settings.agent_ready and settings.operator_enabled
    assert XAI_KEY not in text and CF_TOKEN not in text and settings.operator_token not in text
    assert "Grok Voice (grok-voice-3.0, voice eve)" in text and "docs/deploy-railway.md" in text
    assert "Cloudflare TURN (tls)" in text and "xAI bills per minute" in text


def test_existing_file_keeps_its_other_lines_and_skipped_keys_stay_missing(tmp_path):
    path = tmp_path / ".env"
    cli.main(["--env-file", str(path), "init"])
    before = load_settings(env_file=path)
    path.write_text(path.read_text() + "# my note\nLOCAL_CAPTURE=0\n")
    prompter, printed = scripted(["1", "1", "1"], ["", ""])  # ElevenLabs + Cal.com, both keys skipped
    run(path, "voice-agent", prompter=prompter)
    after = load_settings(env_file=path)
    assert after.operator_token == before.operator_token and "# my note" in path.read_text()
    # CALENDAR=calcom without a key would not load; it stays unchosen until the key is there.
    assert after.calendar == "" and after.voice_provider == "elevenlabs"
    text = "\n".join(printed)
    assert "missing ELEVENLABS_API_KEY" in text and "missing CAL_API_KEY" in text and "calendar check" in text


def test_no_input_uses_flags_and_asks_nothing(tmp_path):
    path = tmp_path / ".env"
    prompter = Prompter(ask=lambda p: pytest.fail("asked"), secret=lambda p: pytest.fail("asked"), out=lambda *a, **k: None)
    run(path, "voice-agent", provider="grok", calendar="local", host="local", prompter=prompter, interactive=False)
    settings = load_settings(env_file=path)
    assert settings.voice_provider == "grok" and settings.agent_missing() == ["XAI_API_KEY (or DEV_AGENT_WS_URL)"]


def test_values_that_an_env_line_cannot_hold_are_refused(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n")
    with pytest.raises(SetupError, match="XAI_API_KEY"):
        update_env(path, {"XAI_API_KEY": "has space"})
    link = tmp_path / "link.env"
    link.symlink_to(path)
    with pytest.raises(SetupError):
        update_env(link, {"A": "2"})
    assert path.read_text() == "A=1\n"


def test_export_for_railway_adds_host_settings_and_leaves_out_local_only_ones(tmp_path):
    path = tmp_path / ".env"
    cli.main(["--env-file", str(path), "init"])
    update_env(path, {"VOICE_PROVIDER": "grok", "XAI_API_KEY": XAI_KEY, "CALENDAR": "local",
                      "DEV_AGENT_WS_URL": "ws://127.0.0.1:8765", "DATA_DIR": "data/x"})
    printed = []
    lines = export_env(path, "railway", show=True, out=lambda *parts, **_: printed.append(" ".join(parts)))
    keys = [line.split("=", 1)[0] for line in lines]
    assert "XAI_API_KEY" in keys and "OPERATOR_TOKEN" in keys and "RAILWAY_RUN_UID" in keys
    assert "ALLOWED_HOSTS=${{RAILWAY_PUBLIC_DOMAIN}}" in lines
    assert "DEV_AGENT_WS_URL" not in keys and "DATA_DIR" not in keys
    notes = "\n".join(printed)
    assert "No TURN relay" in notes and "CALENDAR=local" in notes


def test_export_copies_instead_of_printing_by_default(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    cli.main(["--env-file", str(path), "init"])
    copied, printed = [], []
    monkeypatch.setattr(setup_wizard, "copy_to_clipboard", lambda text: copied.append(text) or True)
    export_env(path, "vps", out=lambda *parts, **_: printed.append(" ".join(parts)))
    token = load_settings(env_file=path).operator_token
    assert token in copied[0] and token not in "\n".join(printed)


def test_console_key_copies_without_printing_and_shows_only_on_request(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    cli.main(["--env-file", str(path), "init"])
    capsys.readouterr()
    token = load_settings(env_file=path).operator_token
    copied = []
    monkeypatch.setattr(setup_wizard, "copy_to_clipboard", lambda text: copied.append(text) or True)
    cli.main(["--env-file", str(path), "console-key"])
    out = capsys.readouterr().out
    assert copied == [token] and token not in out and "not a" not in out
    cli.main(["--env-file", str(path), "console-key", "--show"])
    assert capsys.readouterr().out.strip() == token
    monkeypatch.setattr(setup_wizard, "copy_to_clipboard", lambda text: False)
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(path), "console-key"])


def test_railway_variables_is_a_dry_run_and_then_sends_values_only_through_stdin(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    cli.main(["--env-file", str(path), "init"])
    update_env(path, {"VOICE_PROVIDER": "grok", "XAI_API_KEY": XAI_KEY, "CALENDAR": "local"})
    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "/usr/local/bin/" + name)
    calls, printed = [], []

    def runner(command, input, **kwargs):
        calls.append((command, input))
        return type("Done", (), {"returncode": 0})()
    names = setup_wizard.railway_variables(path, out=printed.append, runner=runner)
    assert calls == [] and "Dry run" in printed[0] and "XAI_API_KEY" in names
    setup_wizard.railway_variables(path, yes=True, out=printed.append, runner=runner)
    assert all(command[:3] == ["railway", "variable", "set"] and command[4:] == ["--stdin", "--skip-deploys"]
               for command, _ in calls)
    assert not any(XAI_KEY in part for command, _ in calls for part in command)
    assert (["railway", "variable", "set", "XAI_API_KEY", "--stdin", "--skip-deploys"], XAI_KEY.encode()) in calls
    assert XAI_KEY not in "\n".join(printed)


def test_no_input_without_flags_keeps_the_files_choices(tmp_path):
    path = tmp_path / ".env"
    cli.main(["--env-file", str(path), "init"])
    update_env(path, {"VOICE_PROVIDER": "grok", "CAL_API_KEY": "cal_live_not_real", "CALENDAR": "calcom"})
    quiet = Prompter(ask=lambda p: pytest.fail("asked"), secret=lambda p: pytest.fail("asked"), out=lambda *a, **k: None)
    run(path, "voice-agent", prompter=quiet, interactive=False)
    settings = load_settings(env_file=path)
    assert settings.voice_provider == "grok" and settings.calendar == "calcom"
