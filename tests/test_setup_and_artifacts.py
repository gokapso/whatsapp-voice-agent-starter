import asyncio
from dataclasses import replace
import hashlib
import json
import os
import re
import time
import wave

import httpx
from pipecat.frames.frames import EndFrame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner
import pytest

from kapso_voice_agent import artifacts, cli, config, provider
from kapso_voice_agent.agent_config import build_config, load_spec
from kapso_voice_agent.app import create_app
from kapso_voice_agent.capture import LocalCapture
from kapso_voice_agent.config import ConfigError, load_settings
from kapso_voice_agent.private import CAPTURE_DIR, VENDOR_DIR, prune
from kapso_voice_agent.tools import ToolRunner


def refuse_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("dry runs must not create HTTP clients")
    monkeypatch.setattr(httpx, "Client", blocked)
    monkeypatch.setattr(httpx, "AsyncClient", blocked)
    monkeypatch.setattr(provider, "elevenlabs_client", blocked)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("KAPSO_API_KEY=kapso-secret-value\nWHATSAPP_PHONE_NUMBER_ID=100000000000001\n"
                    "WHATSAPP_WEBHOOK_SECRET=webhook-secret-value\nELEVENLABS_API_KEY=eleven-secret-value\n")
    return str(path)


class TestDryRunByDefault:
    def test_agent_plan_and_apply_without_yes_make_no_requests(self, monkeypatch, tmp_path, env_file, capsys):
        for key in list(os.environ):
            if key.startswith(("KAPSO_", "ELEVENLABS_", "WHATSAPP_")):
                monkeypatch.delenv(key)
        refuse_network(monkeypatch)
        cli.main(["--env-file", env_file, "agent", "plan", "--out", str(tmp_path / "plan.json")])
        cli.main(["--env-file", env_file, "agent", "apply"])
        output = capsys.readouterr().out
        assert '"dry_run": true' in output and "eleven-secret-value" not in output
        assert json.loads((tmp_path / "plan.json").read_text())["platform_settings"]["auth"]["enable_auth"] is True

    def test_kapso_webhook_plan_redacts_secret_and_makes_no_requests(self, monkeypatch, env_file, capsys):
        refuse_network(monkeypatch)
        cli.main(["--env-file", env_file, "kapso", "webhook", "--url", "https://voice.example.test/webhooks/whatsapp"])
        output = capsys.readouterr().out
        plan = json.loads(output)
        assert plan["dry_run"] is True and plan["steps"][0]["method"] == "GET"
        assert "webhook-secret-value" not in output and "kapso-secret-value" not in output

    @pytest.mark.parametrize("url", ["http://voice.example.test/webhooks/whatsapp", "https://voice.example.test/other"])
    def test_webhook_url_must_be_public_https_receiver(self, env_file, url):
        with pytest.raises(SystemExit):
            cli.main(["--env-file", env_file, "kapso", "webhook", "--url", url])


class TestApplyGuards:
    def client(self, routes, calls):
        def handler(request):
            calls.append((request.method, request.url.path))
            return routes(request)
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def test_existing_meta_webhook_is_never_replaced(self, settings):
        calls = []
        client = self.client(lambda r: httpx.Response(200, json={"data": [{"id": "w1", "url": "https://old.example/x"}]}), calls)
        with pytest.raises(provider.SetupError, match="already has a Meta webhook"):
            provider.apply_webhook(settings, "https://voice.example.test/webhooks/whatsapp", client)
        assert [m for m, _ in calls] == ["GET"]

    def test_webhook_created_when_none_exists(self, settings):
        calls, bodies = [], []

        def routes(request):
            if request.method == "POST":
                bodies.append(json.loads(request.content))
                return httpx.Response(201, json={"data": {"id": "w2", "active": True}})
            return httpx.Response(200, json={"data": []})
        result = provider.apply_webhook(settings, "https://voice.example.test/webhooks/whatsapp", self.client(routes, calls))
        assert result == {"created": True, "webhook_id": "w2", "active": True}
        assert bodies[0]["whatsapp_webhook"]["kind"] == "meta" and bodies[0]["whatsapp_webhook"]["secret_key"] == settings.webhook_secret

    def test_agent_with_other_name_is_not_modified(self, settings, spec):
        calls = []
        client = self.client(lambda r: httpx.Response(200, json={"name": "Someone else's agent"}), calls)
        with pytest.raises(provider.SetupError, match="different name"):
            provider.apply_agent(settings, build_config(spec, "America/Chicago"), client)
        assert calls == [("GET", "/v1/convai/agents/test-agent")]

    def test_agent_create_reads_back_and_reports_drift(self, settings, spec):
        config = build_config(spec, "America/Chicago")
        stored = json.loads(json.dumps(config))
        stored["conversation_config"]["tts"]["voice_id"] = "changed-by-provider"
        calls = []

        def routes(request):
            if request.method == "POST":
                return httpx.Response(200, json={"agent_id": "new-agent"})
            return httpx.Response(200, json=stored)
        result = provider.apply_agent(replace(settings, elevenlabs_agent_id=""), config, self.client(routes, calls))
        assert result == {"action": "created", "agent_id": "new-agent", "verified": False,
                          "mismatches": ["conversation_config.tts.voice_id"]}
        assert [m for m, _ in calls] == ["POST", "GET"]


class TestSettings:
    @pytest.mark.parametrize("env,message", [
        ({"OPERATOR_TOKEN": "short"}, "at least 32"),
        ({"DEV_AGENT_WS_URL": "wss://agent.example.com"}, "loopback"),
        ({"ICE_SERVERS_JSON": "{}"}, "ICE_SERVERS_JSON"),
        ({"WHATSAPP_PHONE_NUMBER_ID": "+1 555 010 0000"}, "numeric"),
        ({"MAX_CONCURRENT_CALLS": "0"}, "between"),
        ({"ENABLE_OUTBOUND": "maybe"}, "0 or 1")])
    def test_rejects_unsafe_or_malformed_values(self, env, message):
        with pytest.raises(ConfigError, match=message):
            load_settings(environ=env)

    def test_defaults_are_conservative(self):
        settings = load_settings(environ={})
        assert not settings.enable_outbound and not settings.local_capture and not settings.operator_enabled
        assert settings.max_concurrent_calls == 1 and not settings.calls_ready


def clean_provider_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("KAPSO_", "ELEVENLABS_", "WHATSAPP_", "OPERATOR_", "CALLER_", "DEV_AGENT_")):
            monkeypatch.delenv(key)


def mode(path):
    return oct(path.stat().st_mode & 0o777)


class TestInit:
    def test_writes_a_private_env_with_local_secrets_and_empty_provider_keys(self, tmp_path, capsys, monkeypatch):
        clean_provider_env(monkeypatch)
        path = tmp_path / ".env"
        cli.main(["--env-file", str(path), "init"])
        printed = capsys.readouterr().out
        settings = load_settings(env_file=path)
        assert mode(path) == "0o600"
        assert settings.operator_enabled and settings.operator_token in printed
        assert len(settings.caller_key_secret) == len(settings.webhook_secret) == 64
        assert len({settings.operator_token, settings.caller_key_secret, settings.webhook_secret}) == 3
        assert settings.caller_key_secret not in printed and settings.webhook_secret not in printed
        assert not (settings.elevenlabs_api_key or settings.elevenlabs_agent_id or settings.kapso_api_key
                    or settings.phone_number_id or settings.dev_agent_ws_url)
        # Only the ElevenLabs key and agent stand between this file and a browser call.
        assert settings.agent_missing() == ["ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID (or DEV_AGENT_WS_URL)"]
        assert "--save-agent-id" in printed

    def test_never_overwrites_an_existing_file_or_symlink(self, tmp_path, monkeypatch):
        clean_provider_env(monkeypatch)
        path = tmp_path / ".env"
        path.write_text("ELEVENLABS_API_KEY=keep-me\n")
        with pytest.raises(SystemExit):
            cli.main(["--env-file", str(path), "init"])
        assert path.read_text() == "ELEVENLABS_API_KEY=keep-me\n"
        target, link = tmp_path / "elsewhere", tmp_path / "linked.env"
        link.symlink_to(target)
        with pytest.raises(SystemExit):
            cli.main(["--env-file", str(link), "init"])
        assert not target.exists()

    def test_every_generated_value_has_exactly_one_slot_in_the_template(self):
        with pytest.raises(ValueError, match="exactly one"):
            cli.real_env_text("OPERATOR_TOKEN=\nCALLER_KEY_SECRET=\n")
        text, values = cli.real_env_text((cli.REPO_ROOT / ".env.example").read_text())
        assert all(f"{name}={value}" in text.splitlines() for name, value in values.items())
        assert "ELEVENLABS_API_KEY=" in text.splitlines() and "KAPSO_API_KEY=" in text.splitlines()


class TestSaveAgentId:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        clean_provider_env(monkeypatch)
        path = tmp_path / ".env"
        cli.main(["--env-file", str(path), "init"])
        path.write_text(path.read_text().replace("ELEVENLABS_API_KEY=\n", "ELEVENLABS_API_KEY=eleven-secret-value\n"))
        return path

    def provider(self, monkeypatch, agent_id="agent_new123"):
        calls = []

        def routes(request):
            calls.append(request.method)
            if request.method == "POST":
                return httpx.Response(200, json={"agent_id": agent_id})
            return httpx.Response(200, json={})  # read-back; drift is reported, the new ID is still saved
        monkeypatch.setattr(provider, "elevenlabs_client",
                            lambda key: httpx.Client(transport=httpx.MockTransport(routes), follow_redirects=False))
        return calls

    def test_created_agent_id_is_written_and_everything_else_kept(self, env, monkeypatch, capsys):
        before = env.read_text()
        calls = self.provider(monkeypatch)
        cli.main(["--env-file", str(env), "agent", "apply", "--yes", "--save-agent-id"])
        captured = capsys.readouterr()
        after = env.read_text()
        assert calls == ["POST", "GET"] and "Saved ELEVENLABS_AGENT_ID" in captured.err
        assert load_settings(env_file=env).elevenlabs_agent_id == "agent_new123"
        assert after == before.replace("ELEVENLABS_AGENT_ID=\n", "ELEVENLABS_AGENT_ID=agent_new123\n")
        assert mode(env) == "0o600" and sorted(p.name for p in env.parent.iterdir()) == [".env"]
        assert "eleven-secret-value" not in captured.out + captured.err

    def test_without_the_flag_the_file_is_untouched(self, env, monkeypatch, capsys):
        before = env.read_text()
        self.provider(monkeypatch)
        cli.main(["--env-file", str(env), "agent", "apply", "--yes"])
        assert env.read_text() == before and "Set ELEVENLABS_AGENT_ID" in capsys.readouterr().err

    def test_dry_run_names_the_file_and_sends_nothing(self, env, monkeypatch, capsys):
        before = env.read_text()
        refuse_network(monkeypatch)
        cli.main(["--env-file", str(env), "agent", "apply", "--save-agent-id"])
        assert json.loads(capsys.readouterr().out)["would_save_agent_id_to"] == str(env) and env.read_text() == before

    def test_unusable_destination_fails_before_any_request(self, tmp_path, monkeypatch):
        clean_provider_env(monkeypatch)
        refuse_network(monkeypatch)
        monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret-value")
        real = tmp_path / "real.env"
        real.write_text("ELEVENLABS_AGENT_ID=\n")
        link = tmp_path / "link.env"
        link.symlink_to(real)
        for path in (tmp_path / "missing.env", link):
            with pytest.raises(SystemExit):
                cli.main(["--env-file", str(path), "agent", "apply", "--yes", "--save-agent-id"])
        assert real.read_text() == "ELEVENLABS_AGENT_ID=\n"

    def test_quoted_empty_agent_id_counts_as_empty(self, env, monkeypatch):
        env.write_text(env.read_text().replace("ELEVENLABS_AGENT_ID=\n", 'ELEVENLABS_AGENT_ID=""\n'))
        self.provider(monkeypatch)
        cli.main(["--env-file", str(env), "agent", "apply", "--yes", "--save-agent-id"])
        assert load_settings(env_file=env).elevenlabs_agent_id == "agent_new123"

    @pytest.mark.parametrize("second", ["ELEVENLABS_AGENT_ID=\n", 'export ELEVENLABS_AGENT_ID=""\n',
                                        "  ELEVENLABS_AGENT_ID = ''  # again\n", "ELEVENLABS_AGENT_ID\n"])
    def test_a_second_agent_id_assignment_fails_before_any_request(self, env, monkeypatch, capsys, second):
        # python-dotenv uses the last assignment, so saving into either one could leave the ID empty.
        env.write_text(env.read_text() + second)
        before = env.read_bytes()
        calls = self.provider(monkeypatch)
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["--env-file", str(env), "agent", "apply", "--yes", "--save-agent-id"])
        assert exit_info.value.code == 2 and calls == []
        assert "sets ELEVENLABS_AGENT_ID 2 times" in capsys.readouterr().err
        assert env.read_bytes() == before and sorted(p.name for p in env.parent.iterdir()) == [".env"]

    def test_export_and_spaced_assignment_is_replaced_in_place(self, env, monkeypatch):
        env.write_text(env.read_text().replace("ELEVENLABS_AGENT_ID=\n", '\n  export ELEVENLABS_AGENT_ID = ""  # set by apply\n'))
        before = env.read_text()
        self.provider(monkeypatch)
        cli.main(["--env-file", str(env), "agent", "apply", "--yes", "--save-agent-id"])
        after = env.read_text()
        assert load_settings(env_file=env).elevenlabs_agent_id == "agent_new123"
        assert after == before.replace('export ELEVENLABS_AGENT_ID = ""  # set by apply\n', "ELEVENLABS_AGENT_ID=agent_new123\n")
        assert after.count("ELEVENLABS_AGENT_ID") == 1 and mode(env) == "0o600"

    def test_unexpected_agent_id_is_not_written(self, env, monkeypatch):
        before = env.read_text()
        self.provider(monkeypatch, agent_id="bad\nOPERATOR_TOKEN=attacker")
        with pytest.raises(SystemExit):
            cli.main(["--env-file", str(env), "agent", "apply", "--yes", "--save-agent-id"])
        assert env.read_text() == before


class TestAcceptanceGuide:
    """docs/testing.md level 2, line by line: the acceptance env has its own DATA_DIR, and the
    stale-slot commands write to the store of the server started from that env."""

    def test_acceptance_data_dir_is_separate_and_stale_slot_commands_use_the_server_store(self, tmp_path, monkeypatch,
                                                                                          capsys):
        clean_provider_env(monkeypatch)
        for key in ("DATA_DIR", "AGENT_CONFIG_PATH"):
            monkeypatch.delenv(key, raising=False)
        level2 = (cli.REPO_ROOT / "docs/testing.md").read_text().split("## Level 2", 1)[1].split("\n## ", 1)[0]
        [data_dir_line] = re.findall(r"^echo '(DATA_DIR=[^']+)' >> \.env\.acceptance", level2, re.MULTILINE)
        databases = re.findall(r"--db (\S+)", level2)
        # The commands run from the repository root. A temporary root keeps real data/ untouched.
        monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
        (tmp_path / "agent").symlink_to(cli.REPO_ROOT / "agent")
        env = tmp_path / ".env.acceptance"
        cli.main(["--env-file", str(env), "init"])
        with env.open("a") as out:
            out.write(data_dir_line + "\n")
        settings = load_settings(env_file=env)
        assert settings.data_dir == tmp_path / "data/acceptance-riley"
        assert settings.data_dir != load_settings(environ={}).data_dir == tmp_path / "data"

        server_store = create_app(settings, spec=load_spec(cli.REPO_ROOT / "agent/agent.toml")).state.manager.store
        assert len(databases) == 2 and {tmp_path / db for db in databases} == {server_store.path}
        capsys.readouterr()
        monkeypatch.chdir(tmp_path)
        cli.main(["--env-file", str(env), "tools", "call", "available_slots", "{}", "--db", databases[0]])
        slot = json.loads(capsys.readouterr().out)["slots"][0]["slot"]
        cli.main(["--env-file", str(env), "tools", "call", "book_appointment",
                  json.dumps({"slot": slot, "name": "Other", "service_id": "brakes", "confirmed": True}),
                  "--db", databases[1], "--caller", "someone-else"])
        result = ToolRunner(server_store, "browser:caller").execute({
            "tool_name": "book_appointment", "tool_call_id": "stale-1",
            "parameters": {"slot": slot, "name": "Riley Test", "service_id": "brakes", "confirmed": True}})
        assert json.loads(result["result"])["code"] == "slot_taken"
        assert not (tmp_path / "data/appointments.sqlite3").exists()

    @pytest.mark.parametrize("guide", ["README.md", "docs/deploy.md", "docs/testing.md"])
    def test_guides_save_the_agent_id_before_starting_the_server(self, guide):
        # Settings are read once at server start: a server started before the save stays "not ready".
        text = (cli.REPO_ROOT / guide).read_text()
        saved = text.index("agent apply --yes --save-agent-id")
        starts = [text.index(command) for command in ("voice-agent serve", "voice-agent --env-file .env.acceptance serve",
                                                      "docker compose up") if command in text]
        assert starts and all(saved < start for start in starts)


class TestServeBanner:
    def test_points_at_the_console_without_printing_secrets(self, settings):
        lines = cli.serve_banner(settings, "127.0.0.1", 8080, ".env")
        text = "\n".join(lines)
        assert "Operator console: http://127.0.0.1:8080/operator/" in text
        assert "Browser calls: ready" in text and "WhatsApp calls: ready" in text
        for secret in (settings.operator_token, settings.webhook_secret, settings.kapso_api_key, settings.elevenlabs_api_key):
            assert secret not in text

    def test_says_what_is_missing_and_uses_a_local_url_for_wildcard_hosts(self):
        settings = load_settings(environ={"OPERATOR_TOKEN": "o" * 40, "CALLER_KEY_SECRET": "c" * 64})
        text = "\n".join(cli.serve_banner(settings, "0.0.0.0", 9000, ".env"))
        assert "http://127.0.0.1:9000/operator/" in text
        assert "Browser calls: not ready, missing ELEVENLABS_API_KEY" in text
        assert "WhatsApp calls: not ready, missing KAPSO_API_KEY; WHATSAPP_PHONE_NUMBER_ID; WHATSAPP_WEBHOOK_SECRET" in text
        off = "\n".join(cli.serve_banner(load_settings(environ={}), "127.0.0.1", 8080, "process environment only"))
        assert "Operator console: off" in off


def test_offline_tool_workflow_books_and_lists(tmp_path, capsys):
    common = ["--db", str(tmp_path / "tools.sqlite3"), "--now", "2026-11-02T09:00:00-06:00"]
    cli.main(["tools", "call", "available_slots", "{}", *common])
    slot = json.loads(capsys.readouterr().out)["slots"][0]["slot"]
    with pytest.raises(SystemExit):
        cli.main(["tools", "call", "book_appointment", json.dumps({"slot": slot, "name": "Sam", "service_id": "brakes",
                                                                   "confirmed": False}), *common])
    capsys.readouterr()
    cli.main(["tools", "call", "book_appointment", json.dumps({"slot": slot, "name": "Sam", "service_id": "brakes",
                                                               "confirmed": True}), *common])
    assert json.loads(capsys.readouterr().out)["status"] == "booked"
    cli.main(["tools", "call", "my_appointments", "{}", "--caller", "someone-else", *common])
    assert json.loads(capsys.readouterr().out)["appointments"] == []


# Local capture --------------------------------------------------------------------------------

def run_recorder(capture, frames):
    async def go():
        worker = PipelineWorker(Pipeline([capture.recorder]), enable_rtvi=False, idle_timeout_secs=None,
                                params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000))
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        task = asyncio.create_task(runner.run())
        for frame in frames:
            await worker.queue_frame(frame)
            await asyncio.sleep(0.01)
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(task, timeout=10)
        return await capture.finish("conv-test")
    return asyncio.run(go())


def test_capture_cap_bounds_memory_and_marks_truncation(tmp_path):
    capture = LocalCapture(tmp_path / "captures", "browser-0123456789abcdef", max_seconds=0.5)
    second = b"\x10\x00" * 16000
    directory = run_recorder(capture, [InputAudioRawFrame(second, 16000, 1), OutputAudioRawFrame(second, 16000, 1),
                                       InputAudioRawFrame(second, 16000, 1)])
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["truncated"] is True and manifest["seconds"] <= 1.5 and CAPTURE_DIR.fullmatch(directory.name)
    with wave.open(str(directory / "caller.wav")) as caller, wave.open(str(directory / "agent.wav")) as agent:
        assert caller.getnframes() == agent.getnframes() <= 16000 * 1.5


def test_capture_rejects_unsafe_names_and_prunes_by_age_and_count(tmp_path):
    with pytest.raises(ValueError):
        LocalCapture(tmp_path, "../escape", 10)
    root = tmp_path / "captures"
    names = [f"2026010{index + 1}T000000Z-call-{index:016x}" for index in range(4)]
    now = time.time()
    for index, name in enumerate(names):
        (root / name).mkdir(parents=True)
        os.utime(root / name, (now - 60 * (4 - index),) * 2)
    old = now - 9 * 86400
    os.utime(root / names[0], (old, old))
    (root / "c0").mkdir()  # not a generated capture name: never pruned or counted
    os.utime(root / "c0", (old, old))
    assert prune(root, CAPTURE_DIR, keep=2, days=7) == 2
    assert sorted(d.name for d in root.iterdir()) == sorted([*names[2:], "c0"])


def test_capture_manifest_reads_only_generated_capture_directories(tmp_path):
    captures = tmp_path / "captures"
    (captures / "zz-operator-notes").mkdir(parents=True)
    (captures / "zz-operator-notes" / "manifest.json").write_text('{"conversation_id": "not-ours"}')
    ours = captures / "20260101T000000Z-call-0123456789abcdef"
    ours.mkdir()
    (ours / "manifest.json").write_text('{"conversation_id": "conv_12345678"}')
    assert artifacts.capture_manifest(captures) == (ours, {"conversation_id": "conv_12345678"})
    with pytest.raises(artifacts.ArtifactError):
        artifacts.capture_manifest(captures, "zz-operator-notes")
    linked = tmp_path / "linked-captures"
    linked.symlink_to(captures, target_is_directory=True)
    with pytest.raises(artifacts.ArtifactError):
        artifacts.capture_manifest(linked)


# Provider artifacts ---------------------------------------------------------------------------

def conversation(status="done", agent_id="agent-en", has_audio=True):
    return {"agent_id": agent_id, "status": status, "has_audio": has_audio, "has_user_audio": has_audio,
            "has_response_audio": has_audio, "metadata": {"call_duration_secs": 25, "termination_reason": "end_call"},
            "transcript": [{"role": "agent", "message": "secret words"}, {"role": "user", "message": "private words"}]}


def mock_client(routes):
    return httpx.Client(transport=httpx.MockTransport(routes), follow_redirects=False)


def test_fetch_saves_private_files_and_reports_only_counts(tmp_path):
    audio = b"ID3" + b"\x00" * 4000

    def routes(request):
        if request.url.path.endswith("/audio"):
            return httpx.Response(200, content=audio, headers={"content-type": "audio/mpeg"})
        return httpx.Response(200, json=conversation())
    report = artifacts.fetch("conv_12345678", "agent-en", "k", tmp_path / "vendor", client=mock_client(routes))
    assert report["turns"] == {"user": 1, "agent": 1} and "secret words" not in json.dumps(report)
    directory = tmp_path / "vendor" / report["directory"]
    assert VENDOR_DIR.fullmatch(directory.name)
    assert report["files"]["recording.mp3"]["sha256"] == hashlib.sha256(audio).hexdigest()
    assert all(oct(p.stat().st_mode & 0o777) == "0o600" for p in directory.iterdir())


@pytest.mark.parametrize("case", ["other_agent", "redirect", "oversize", "server_error", "bad_audio_type"])
def test_fetch_failures_are_bounded(tmp_path, case):
    def routes(request):
        audio = request.url.path.endswith("/audio")
        if case == "other_agent":
            return httpx.Response(200, json=conversation(agent_id="someone-else"))
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://elsewhere.example/file"})
        if case == "oversize" and audio:
            return httpx.Response(200, content=b"x", headers={"content-type": "audio/mpeg",
                                                             "content-length": str(artifacts.MAX_AUDIO_BYTES + 1)})
        if case == "server_error":
            return httpx.Response(502, text="bad gateway")
        if case == "bad_audio_type" and audio:
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(200, json=conversation())
    with pytest.raises(artifacts.ArtifactError):
        artifacts.fetch("conv_12345678", "agent-en", "k", tmp_path / "vendor", client=mock_client(routes))
    assert not list((tmp_path / "vendor").rglob("recording.mp3"))


def test_fetch_waits_boundedly_and_rejects_bad_ids(tmp_path):
    statuses, sleeps = iter(["processing", "processing", "done"]), []

    def routes(request):
        if request.url.path.endswith("/audio"):
            return httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"})
        return httpx.Response(200, json=conversation(status=next(statuses)))
    report = artifacts.fetch("conv_12345678", "agent-en", "k", tmp_path / "vendor", wait_seconds=999,
                             client=mock_client(routes), sleep=sleeps.append)
    assert report["status"] == "done" and sleeps == [10, 10]
    with pytest.raises(artifacts.ArtifactError):
        artifacts.fetch("../../etc", "agent-en", "k", tmp_path / "vendor", client=mock_client(lambda r: httpx.Response(500)))
