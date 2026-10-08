"""Private on-disk state: bookings, local captures and provider downloads.

Directories this server creates are 0700 and its files 0600, whatever the process umask.
Only directories this server owns are tightened: DATA_DIR itself and the directories below it.
Existing parents of DATA_DIR (for example a shared /srv or a home directory) are never changed.
"""

import os
from pathlib import Path
import re
import shutil
import stat
import time

from .config import ConfigError

SQLITE_SIDE_FILES = ("-journal", "-wal", "-shm")
# Call references allowed in capture directory names.
NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# The names this server gives artifact directories: `<UTC stamp>-<call ref>` (capture.py) and
# `vendor-<16 hex>` (artifacts.py). Retention only ever deletes directories with these names.
CAPTURE_DIR = re.compile(r"[0-9]{8}T[0-9]{6}Z-[A-Za-z0-9_-]{1,80}")
VENDOR_DIR = re.compile(r"vendor-[0-9a-f]{16}")
ARTIFACT_ROOTS = {"captures": CAPTURE_DIR, "vendor": VENDOR_DIR}


def private_dir(path):
    """Create `path` (and any missing parents) as 0700, or tighten an existing `path` to 0700."""
    path = Path(path)
    missing = []
    probe = path
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        directory.chmod(0o700)  # mkdir's mode is reduced by the umask
    if path.is_symlink() or not path.is_dir():
        raise ConfigError(f"{path.name} must be a real directory")
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        try:
            path.chmod(0o700)
        except PermissionError:
            raise ConfigError(f"Cannot make {path.name} private (0700); it must belong to the server user") from None
    return path


def private_file(path):
    """Create an empty 0600 file if missing, or tighten an existing one to 0600."""
    path = Path(path)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return path


def private_sqlite(path):
    """Prepare a SQLite file as 0600 before SQLite opens it.

    SQLite creates its rollback journal (and WAL/SHM files) with the database file's permissions,
    so a 0600 database keeps those 0600 too. Side files left by older runs are tightened here.
    A missing parent directory is created 0700; an existing one is left alone.
    """
    path = Path(path)
    if not path.parent.exists():
        private_dir(path.parent)
    private_file(path)
    for suffix in SQLITE_SIDE_FILES:
        side = path.with_name(path.name + suffix)
        if side.exists() and not side.is_symlink():
            private_file(side)
    return path


def artifact_dirs(root, pattern):
    """This server's artifact directories directly in `root`: real directories named by `pattern`.

    A symlinked root is not ours (it points somewhere else), so nothing in it is listed.
    Symlinked children and any other names (operator folders, files) are never listed either.
    """
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return []
    return [d for d in root.iterdir() if pattern.fullmatch(d.name) and not d.is_symlink() and d.is_dir()]


def prune(root, pattern, keep, days, now=None):
    """Delete artifact directories in `root` older than `days`, then the oldest beyond `keep`.

    Bounded: it lists one directory level and removes whole artifact directories only.
    Only directories from artifact_dirs() count toward `keep` or are ever deleted.
    """
    now = now or time.time()
    dirs = sorted(artifact_dirs(root, pattern), key=lambda d: d.stat().st_mtime)
    removed = 0
    for index, directory in enumerate(dirs):
        if now - directory.stat().st_mtime > days * 86400 or index < len(dirs) - keep:
            shutil.rmtree(directory)
            removed += 1
    return removed


def prune_local_artifacts(data_dir, retention_days, max_count, now=None):
    """Best-effort local retention for both private artifact roots (captures and vendor downloads).

    This deletes this server's local copies only. Provider copies (ElevenLabs) follow the
    provider's own retention setting, and Meta-native artifacts follow Meta's.
    """
    return {name: prune(Path(data_dir) / name, pattern, keep=max_count, days=retention_days, now=now)
            for name, pattern in ARTIFACT_ROOTS.items()}
