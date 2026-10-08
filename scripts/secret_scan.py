#!/usr/bin/env python3
"""Fail if files Git would publish contain credentials, private artifacts or real-looking identifiers.

Scans tracked plus untracked-but-not-ignored files. Standard library only. This is a guard rail, not
a replacement for a dedicated scanner such as gitleaks before publishing.

  python3 scripts/secret_scan.py            # exit 1 on findings; prints file:line and rule only
  python3 scripts/secret_scan.py --extra-values-file PATH   # also fail if any line of PATH appears
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FILES = re.compile(r"(^|/)(\.env(\..*)?|.*\.(sqlite3?|db|wav|mp3|ogg|opus|m4a|pem|key|p12)|data/.*|build/.*)$")
ALLOWED_FILES = {".env.example"}
RULES = {
    "elevenlabs_key": re.compile(r"\bsk_[A-Za-z0-9]{20,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "assigned_secret": re.compile(r"(?i)[a-z_]*(api_?key|secret|token|password)[a-z_]*\s*[=:]\s*['\"]?(?!<|\$|\{|your_|test-|fake|change-me)"
                                  r"[A-Za-z0-9_\-+/]{24,}"),
    "bearer_value": re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.]{30,}"),
    "machine_path": re.compile(r"/(Users|home)/[a-z][a-z0-9_-]+/"),
    "meta_call_id": re.compile(r"\bwacid\.(?!SYNTHETIC|EXAMPLE|test\b)[A-Za-z0-9_=-]{8,}"),
    "bsuid": re.compile(r"\bUS\.(?!1000000)[0-9]{8,}"),
    "uuid": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    "phone_like": re.compile(r"(?<![\w.])\+?(?!1555010|100000000000|200000000000|9000000000000|1793000)[1-9][0-9]{10,14}(?![\w])"),
}
# Public, non-secret values that intentionally appear: ElevenLabs' premade "River" voice ID.
ALLOWED_VALUES = {"SAz9YHcvj6GT2YYXdXww"}


def publishable_files():
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
    return sorted({name.decode() for name in tracked if name and (ROOT / name.decode()).is_file()})


def scan(extra_values=()):
    findings = []
    for name in publishable_files():
        if name not in ALLOWED_FILES and FORBIDDEN_FILES.search(name):
            findings.append((name, 0, "forbidden_file"))
            continue
        try:
            text = (ROOT / name).read_text()
        except UnicodeDecodeError:
            findings.append((name, 0, "binary_file"))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for value in ALLOWED_VALUES:
                line = line.replace(value, "")
            for rule, pattern in RULES.items():
                if pattern.search(line):
                    findings.append((name, number, rule))
            for value in extra_values:
                if value in line:
                    findings.append((name, number, "private_value"))
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra-values-file", help="File with one private value per line that must not appear")
    args = parser.parse_args()
    extra = []
    if args.extra_values_file:
        extra = [v.strip() for v in Path(args.extra_values_file).read_text().splitlines() if len(v.strip()) >= 8]
    findings = scan(extra)
    for name, line, rule in findings:
        print(f"{name}:{line}: {rule}")
    print(f"scanned {len(publishable_files())} files, {len(findings)} findings, {len(extra)} extra values")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
