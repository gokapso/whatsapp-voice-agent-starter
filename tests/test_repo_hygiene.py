import subprocess
import sys
import tomllib

from kapso_voice_agent.config import REPO_ROOT


def test_publishable_files_pass_the_secret_and_privacy_scan():
    result = subprocess.run([sys.executable, "scripts/secret_scan.py"], cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_private_runtime_paths_are_ignored():
    paths = [".env", ".env.dev", "data/appointments.sqlite3", "data/captures/x/caller.wav", "build/agent-config.json"]
    result = subprocess.run(["git", "check-ignore", *paths], cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.stdout.split() == paths


def test_scanner_rules_catch_planted_samples():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import secret_scan
    samples = {
        "elevenlabs_key": "key = sk_" + "a1" * 15,
        "machine_path": "/" + "Users/someone/project/.env",
        "meta_call_id": "wacid." + "HBgLMTIzNDU2Nzg5MBUCABEYEjA",
        "bsuid": "US." + "4242424242424242",
        "phone_like": "+" + "569" + "12345678",
        "assigned_secret": "WEBHOOK_SECRET=" + "Zq9" * 10,
    }
    for rule, line in samples.items():
        assert secret_scan.RULES[rule].search(line), rule
    for safe in ("15550100001", "wacid.SYNTHETIC-INBOUND-1", "US.100000000000000001", "secret_key: <WHATSAPP_WEBHOOK_SECRET>"):
        assert not any(pattern.search(safe) for pattern in secret_scan.RULES.values()), safe


def test_docker_image_copies_the_root_files_that_init_and_the_package_build_read():
    # `voice-agent init` reads .env.example; the package build reads the readme and license files.
    lines = (REPO_ROOT / "Dockerfile").read_text().splitlines()
    install = next(i for i, line in enumerate(lines) if line.startswith("RUN uv sync") and "--no-install-project" not in line)
    copied = {source for line in lines[:install] if line.startswith("COPY ") and "--from=" not in line for source in line.split()[1:-1]}
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    needed = {".env.example", project["readme"], *project["license-files"]}
    assert needed <= copied, sorted(needed - copied)
