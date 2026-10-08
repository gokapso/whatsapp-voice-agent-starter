"""Recording notice from the provider's stored setting, quick-start readiness, local retention
without new calls, and private file permissions. All offline: the provider read is a mock."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

import httpx
import pytest

from conftest import TOKEN, KapsoRecorder, asgi_client, fixture, post_webhook
from kapso_voice_agent import cli, recording
from kapso_voice_agent.app import create_app
from kapso_voice_agent.bridge import session_context
from kapso_voice_agent.config import ConfigError, load_settings
from kapso_voice_agent.events import EventLog
from kapso_voice_agent.private import ARTIFACT_ROOTS, private_dir, private_sqlite
from kapso_voice_agent.recording import Recording, RecordingCheck, RecordingUnknown
from kapso_voice_agent.store import AppointmentStore
from test_http_surface import NeverSession

AUTH = {"authorization": f"Bearer {TOKEN}"}
# Captured at import, before conftest's autouse fixture swaps in the offline stand-in.
REAL_PROVIDER_READ = recording.read_provider_recording


def mode(path):
    return oct(stat.S_IMODE(Path(path).stat().st_mode))


def reader(*answers):
    """A provider read that returns (or raises) each answer in turn."""
    answers, reads = list(answers), []

    async def read(settings):
        reads.append(1)
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer
    read.reads = reads
    return read


def without_recording(spec):
    raw = {**spec.raw, "privacy": {**spec.raw["privacy"], "record_audio": False}}
    return replace(spec, raw=raw)


# Recording notice ---------------------------------------------------------------------------

class TestRecordingNotice:
    def check(self, settings, spec, read, clock=time.monotonic):
        return RecordingCheck(settings, spec, EventLog(), reader=read, clock=clock)

    def test_provider_recording_on_is_disclosed_even_when_agent_toml_says_off(self, settings, spec, store):
        log = EventLog()
        state = asyncio.run(RecordingCheck(settings, without_recording(spec), log, reader=reader(True)).current())
        context = session_context(settings, spec, store, "inbound", state)["dynamic_variables"]
        assert state == Recording(provider=True, local=False, source="provider_readback")
        assert "This call is recorded." in context["opening_message"]
        assert context["recording_status"].startswith("This call's audio is recorded")
        assert [e["event"] for e in log.events] == ["provider_recording_differs_from_agent_toml"]

    def test_provider_recording_off_is_not_disclosed_even_when_agent_toml_says_on(self, settings, spec, store):
        state = asyncio.run(self.check(settings, spec, reader(False)).current())
        context = session_context(settings, spec, store, "outbound", state)["dynamic_variables"]
        assert "recorded" not in context["opening_message"]
        assert context["recording_status"] == "This call's audio is not recorded."

    def test_local_capture_alone_is_disclosed(self, settings, spec, store):
        state = asyncio.run(self.check(replace(settings, local_capture=True), spec, reader(False)).current())
        assert state.recorded and "recorded" in session_context(settings, spec, store, "inbound", state)["dynamic_variables"]["opening_message"]

    @pytest.mark.parametrize("answer", [RuntimeError("synthetic outage"), None, "true", asyncio.TimeoutError()])
    def test_unknown_provider_state_raises_and_is_not_cached(self, settings, spec, answer):
        read = reader(answer, True)
        check = self.check(settings, spec, read)

        async def go():
            with pytest.raises(RecordingUnknown):
                await check.current()
            unknown = check.status()
            return unknown, await check.current()
        unknown, state = asyncio.run(go())
        assert unknown["provider_recording"] == "unknown" and unknown["next_greeting_says_recorded"] is None
        assert state.provider is True and len(read.reads) == 2

    def test_verification_is_reused_briefly_then_read_again(self, settings, spec):
        now = [0.0]
        read = reader(True, False)
        check = self.check(settings, spec, read, clock=lambda: now[0])

        async def go():
            first = await check.current()
            now[0] = recording.CHECK_TTL_SECONDS - 1
            cached = await check.current()
            now[0] = recording.CHECK_TTL_SECONDS + 1
            return first, cached, await check.current()
        first, cached, changed = asyncio.run(go())
        assert first.provider is True and cached is first and changed.provider is False and len(read.reads) == 2

    def test_fake_agent_never_reads_the_provider_and_records_nothing(self, settings, spec):
        read = reader(RuntimeError("must not be called"))
        dev = replace(settings, dev_agent_ws_url="ws://127.0.0.1:8765", elevenlabs_api_key="", elevenlabs_agent_id="")
        check = self.check(dev, spec, read)
        state = asyncio.run(check.current())
        assert not state.recorded and state.source == "offline_fake_agent" and read.reads == []
        assert check.status()["next_greeting_says_recorded"] is False

    @pytest.mark.parametrize("status,body,expected", [
        (200, {"platform_settings": {"privacy": {"record_voice": True}}}, True),
        (200, {"platform_settings": {"privacy": {"record_voice": False}}}, False),
        (200, {"platform_settings": {}}, RecordingUnknown),
        (404, {"detail": "not found"}, RecordingUnknown)])
    def test_provider_read_is_a_single_get_of_the_stored_agent(self, settings, status, body, expected):
        requests = []

        def handler(request):
            requests.append((request.method, request.url.path, request.headers["xi-api-key"]))
            return httpx.Response(status, json=body)
        call = REAL_PROVIDER_READ(settings, transport=httpx.MockTransport(handler))
        if expected is RecordingUnknown:
            with pytest.raises(RecordingUnknown):
                asyncio.run(call)
        else:
            assert asyncio.run(call) is expected
        assert requests == [("GET", "/v1/convai/agents/test-agent", settings.elevenlabs_api_key)]


class TestNoSessionWhileRecordingIsUnknown:
    def test_inbound_call_is_rejected_before_pre_accept(self, settings, spec, store):
        async def go():
            kapso = KapsoRecorder()
            app = create_app(settings, spec, store, session_factory=NeverSession, kapso_factory=kapso.factory(),
                             recording_reader=reader(RuntimeError("synthetic outage")))
            async with asgi_client(app) as client:
                response = await post_webhook(client, fixture("inbound_connect.json"))
            await asyncio.gather(*app.state.manager.jobs.values(), return_exceptions=True)
            return response.status_code, kapso.actions, app
        status, actions, app = asyncio.run(go())
        # The delivery itself was fine (200, no Kapso retry); the call is declined, never answered.
        assert status == 200 and actions == ["reject"]
        assert "recording_check_failed_runtimeerror" in [e["event"] for e in app.state.log.events]

    def test_browser_and_outbound_calls_get_503_and_never_dial(self, settings, spec, store):
        async def go():
            kapso = KapsoRecorder()
            app = create_app(replace(settings, enable_outbound=True), spec, store, session_factory=NeverSession,
                             kapso_factory=kapso.factory(), recording_reader=reader(RuntimeError("synthetic outage")))
            async with asgi_client(app) as client:
                browser = await client.post("/operator/api/browser-call", headers=AUTH, json={"sdp": "v=0 synthetic offer"})
                outbound = await client.post("/operator/api/outbound/call", headers=AUTH, json={"recipient": "15550100002"})
                state = (await client.get("/operator/api/state", headers=AUTH)).json()
            return browser.status_code, outbound.status_code, kapso.bodies, state
        browser, outbound, bodies, state = asyncio.run(go())
        assert browser == outbound == 503 and bodies == []
        assert state["recording"]["provider_recording"] == "unknown"
        assert state["recording"]["next_greeting_says_recorded"] is None


# Setup readiness --------------------------------------------------------------------------

class TestSetupReadiness:
    def test_agent_is_not_ready_without_a_caller_key(self, settings, spec, store):
        dev = replace(settings, webhook_secret="", caller_key_secret="", elevenlabs_api_key="", elevenlabs_agent_id="",
                      dev_agent_ws_url="ws://127.0.0.1:8765")
        assert not dev.agent_ready and dev.agent_missing() == ["CALLER_KEY_SECRET (or WHATSAPP_WEBHOOK_SECRET)"]

        async def go():
            app = create_app(dev, spec, store, session_factory=NeverSession)
            async with asgi_client(app) as client:
                state = (await client.get("/operator/api/state", headers=AUTH)).json()
                offer = await client.post("/operator/api/browser-call", headers=AUTH, json={"sdp": "v=0 synthetic offer"})
            return state, offer
        state, offer = asyncio.run(go())
        assert state["agent_ready"] is False and offer.status_code == 503 and "CALLER_KEY_SECRET" in offer.json()["detail"]
        assert state["agent_missing"] == ["CALLER_KEY_SECRET (or WHATSAPP_WEBHOOK_SECRET)"]

    def test_init_env_writes_a_private_reusable_file_and_shows_the_token(self, tmp_path, capsys, monkeypatch):
        for key in list(os.environ):
            if key.startswith(("KAPSO_", "ELEVENLABS_", "WHATSAPP_", "OPERATOR_", "CALLER_", "DEV_AGENT_")):
                monkeypatch.delenv(key)
        path = tmp_path / ".env.dev"
        cli.main(["dev", "init-env", "--path", str(path)])
        printed = capsys.readouterr().out
        settings = load_settings(env_file=path)
        assert mode(path) == "0o600"
        assert settings.operator_enabled and settings.agent_ready and settings.dev_agent_ws_url == "ws://127.0.0.1:8765"
        assert settings.operator_token in printed and settings.caller_key_secret not in printed
        assert not settings.kapso_api_key and not settings.elevenlabs_api_key
        with pytest.raises(SystemExit):
            cli.main(["dev", "init-env", "--path", str(path)])
        assert load_settings(env_file=path).operator_token == settings.operator_token


# Local retention without new calls ------------------------------------------------------------

OLD = time.time() - 9 * 86400
# Names exactly as capture.py and artifacts.py generate them.
CANONICAL = {"captures": ("20260101T000000Z-call-0123456789abcdef", "20260102T000000Z-browser-0123456789abcdef"),
             "vendor": ("vendor-00000000000000aa", "vendor-00000000000000bb")}
# Not ours: operator folders, look-alike names and files are never pruned, even when old.
UNRELATED = {"captures": ("operator-notes", "20260101-call-x", "captures-expired"),
             "vendor": ("operator-notes", "vendor-xyz", "vendor-000000000000000g", "vendor-00000000000000dd\n")}
# Child symlinks are never followed, even with a generated name.
LINKS = {"captures": ("linked-old", "20260103T000000Z-call-00000000000000cc"),
         "vendor": ("linked-old", "vendor-00000000000000cc")}


def make_dir(path, mtime=None):
    path.mkdir(parents=True)
    (path / "manifest.json").write_text("{}")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def expired_and_fresh(data_dir):
    for root, (expired, fresh) in CANONICAL.items():
        make_dir(data_dir / root / expired, OLD)
        make_dir(data_dir / root / fresh)
        for name in UNRELATED[root]:
            make_dir(data_dir / root / name, OLD)
        target = make_dir(data_dir.parent / f"elsewhere-{root}", OLD)
        for name in LINKS[root]:
            (data_dir / root / name).symlink_to(target, target_is_directory=True)
    (data_dir / "captures" / "not-an-artifact.txt").write_text("kept")


def remaining(data_dir):
    return sorted(p.name for root in ("captures", "vendor") for p in (data_dir / root).iterdir())


def kept():
    return sorted(["not-an-artifact.txt", *(name for root in ARTIFACT_ROOTS
                                            for name in (CANONICAL[root][1], *UNRELATED[root], *LINKS[root]))])


def linked_roots(tmp_path, linked):
    """DATA_DIR whose `linked` root is a symlink to an outside folder holding old directories."""
    data_dir, outside = tmp_path / "data", tmp_path / "outside"
    make_dir(outside / CANONICAL[linked][0], OLD)
    make_dir(outside / "external-old-backup", OLD)
    for root in ARTIFACT_ROOTS:
        if root == linked:
            data_dir.mkdir(exist_ok=True)
            (data_dir / root).symlink_to(outside, target_is_directory=True)
        else:
            make_dir(data_dir / root / CANONICAL[root][0], OLD)
    return data_dir, outside


def outside_untouched(data_dir, outside, linked):
    assert (data_dir / linked).is_symlink()
    assert sorted(p.name for p in outside.iterdir()) == sorted([CANONICAL[linked][0], "external-old-backup"])
    assert all((outside / name / "manifest.json").exists() for name in (CANONICAL[linked][0], "external-old-backup"))


async def start_and_stop(app):
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if "local_artifacts_pruned" in [e["event"] for e in app.state.log.events]:
                break
            await asyncio.sleep(0.01)
    return list(app.state.log.events)


def run_prune_command(data_dir, tmp_path, capsys, monkeypatch, max_count=20):
    env = tmp_path / "prune.env"
    env.write_text(f"DATA_DIR={data_dir}\nCAPTURE_RETENTION_DAYS=7\nCAPTURE_MAX_COUNT={max_count}\n")
    for key in ("DATA_DIR", "CAPTURE_RETENTION_DAYS", "CAPTURE_MAX_COUNT"):
        monkeypatch.delenv(key, raising=False)
    cli.main(["--env-file", str(env), "artifacts", "prune"])
    return json.loads(capsys.readouterr().out)


class TestRetention:
    def test_server_start_prunes_both_roots_without_a_call(self, settings, spec, store):
        expired_and_fresh(settings.data_dir)
        events = asyncio.run(start_and_stop(create_app(settings, spec, store, session_factory=NeverSession)))
        assert remaining(settings.data_dir) == kept()
        pruned = next(e for e in events if e["event"] == "local_artifacts_pruned")
        assert pruned["captures"] == pruned["vendor"] == 1
        assert all((settings.data_dir.parent / f"elsewhere-{root}" / "manifest.json").exists() for root in CANONICAL)

    def test_prune_command_works_while_the_server_is_stopped(self, tmp_path, capsys, monkeypatch):
        data_dir = tmp_path / "data"
        expired_and_fresh(data_dir)
        report = run_prune_command(data_dir, tmp_path, capsys, monkeypatch)
        assert report["removed"] == {"captures": 1, "vendor": 1} and "local copies only" in report["scope"]
        assert remaining(data_dir) == kept()

    @pytest.mark.parametrize("linked", ["captures", "vendor"])
    def test_server_start_never_prunes_through_a_symlinked_root(self, linked, tmp_path, settings, spec, store):
        data_dir, outside = linked_roots(tmp_path, linked)
        events = asyncio.run(start_and_stop(create_app(replace(settings, data_dir=data_dir), spec, store,
                                                       session_factory=NeverSession)))
        outside_untouched(data_dir, outside, linked)
        pruned = next(e for e in events if e["event"] == "local_artifacts_pruned")
        assert pruned[linked] == 0 and pruned[next(r for r in ARTIFACT_ROOTS if r != linked)] == 1

    @pytest.mark.parametrize("linked", ["captures", "vendor"])
    def test_prune_command_never_prunes_through_a_symlinked_root(self, linked, tmp_path, capsys, monkeypatch):
        data_dir, outside = linked_roots(tmp_path, linked)
        report = run_prune_command(data_dir, tmp_path, capsys, monkeypatch)
        outside_untouched(data_dir, outside, linked)
        assert report["removed"][linked] == 0
        assert report["removed"][next(r for r in ARTIFACT_ROOTS if r != linked)] == 1

    def test_count_cap_counts_and_removes_only_this_servers_directories(self, tmp_path, capsys, monkeypatch):
        data_dir = tmp_path / "data"
        now = time.time()
        for root in ARTIFACT_ROOTS:
            for index, name in enumerate(UNRELATED[root]):
                make_dir(data_dir / root / name, now - 3600 * (10 + index))  # older than every artifact
        for index in range(3):
            make_dir(data_dir / "captures" / f"2026010{index + 1}T000000Z-call-{index:016x}", now - 3600 * (3 - index))
            make_dir(data_dir / "vendor" / f"vendor-{index:016x}", now - 3600 * (3 - index))
        report = run_prune_command(data_dir, tmp_path, capsys, monkeypatch, max_count=2)
        assert report["removed"] == {"captures": 1, "vendor": 1}
        assert sorted(p.name for p in (data_dir / "captures").iterdir()) == sorted(
            ["20260102T000000Z-call-0000000000000001", "20260103T000000Z-call-0000000000000002",
             *UNRELATED["captures"]])
        assert sorted(p.name for p in (data_dir / "vendor").iterdir()) == sorted(
            ["vendor-0000000000000001", "vendor-0000000000000002", *UNRELATED["vendor"]])


# Private files under a normal umask ---------------------------------------------------------------

@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


class TestPrivateFiles:
    def test_fresh_data_dir_store_and_journal_are_private(self, umask_022, tmp_path, settings, spec):
        shared = tmp_path / "shared"
        shared.mkdir(mode=0o755)
        data_dir = shared / "voice" / "data"
        app = create_app(replace(settings, data_dir=data_dir), spec, session_factory=NeverSession)
        database = data_dir / "appointments.sqlite3"
        assert mode(data_dir) == mode(data_dir.parent) == "0o700" and mode(database) == "0o600"
        assert mode(shared) == "0o755"  # an existing parent is never changed
        with app.state.manager.store.connect() as db:
            db.execute("INSERT INTO appointments (id, slot, caller, name, service_id, created_at) "
                       "VALUES ('A1', 'slot', 'caller', 'Sam', 'brakes', 'now')")
            journal = Path(str(database) + "-journal")
            assert journal.exists() and mode(journal) == "0o600"

    def test_existing_readable_store_is_tightened(self, umask_022, tmp_path, spec):
        data_dir = tmp_path / "data"
        data_dir.mkdir(mode=0o755)
        database = data_dir / "appointments.sqlite3"
        sqlite3.connect(database).close()
        journal = Path(str(database) + "-journal")
        journal.write_bytes(b"")  # left by an older run
        database.chmod(0o644)
        private_dir(data_dir)
        private_sqlite(database)  # what AppointmentStore does before SQLite opens the file
        assert mode(data_dir) == "0o700" and mode(database) == "0o600" and mode(journal) == "0o600"
        AppointmentStore(database, spec.business_path)
        assert mode(database) == "0o600"

    def test_capture_and_vendor_roots_are_private_under_umask_022(self, umask_022, tmp_path):
        for root in ("captures", "vendor"):
            assert mode(private_dir(tmp_path / "data" / root)) == "0o700"
        assert mode(tmp_path / "data") == "0o700"

    def test_data_dir_must_be_a_real_directory(self, tmp_path):
        (tmp_path / "file").write_text("x")
        (tmp_path / "link").symlink_to(tmp_path)
        for name in ("file", "link"):
            with pytest.raises(ConfigError):
                private_dir(tmp_path / name)
