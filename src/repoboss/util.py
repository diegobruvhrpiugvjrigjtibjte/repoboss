"""Small filesystem and hashing helpers shared by the whole package."""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

# Directories that never belong in a snapshot, an agent workspace or a transfer.
NOISE_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        ".repoboss",
        ".idea",
        ".vscode",
        ".eggs",
    }
)
NOISE_DIR_PATTERNS = ("*.egg-info",)
NOISE_FILE_PATTERNS = ("*.pyc", "*.pyo", ".coverage", ".DS_Store", "Thumbs.db")

# File names that commonly hold credentials. Excluded from snapshots and exports by default.
SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".pypirc",
    ".npmrc",
    "credentials*.json",
    "*service-account*.json",
    "*.kdbx",
    ".git-credentials",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    import json

    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def matches_any(rel: str, patterns: Sequence[str], *, casefold: bool = False) -> bool:
    """fnmatch-style matching on POSIX relative paths; ``*`` also crosses ``/``.

    ``dir/**`` additionally matches ``dir`` itself.
    """
    path = rel.lower() if casefold else rel
    for pattern in patterns:
        pat = pattern.lower() if casefold else pattern
        if fnmatchcase(path, pat):
            return True
        if pat.endswith("/**") and path == pat[:-3]:
            return True
    return False


def is_noise(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    for part in parts[:-1]:
        if part in NOISE_DIR_NAMES or any(fnmatchcase(part, p) for p in NOISE_DIR_PATTERNS):
            return True
    name = parts[-1] if parts else rel
    if name in NOISE_DIR_NAMES:
        return True
    return any(fnmatchcase(name, p) for p in NOISE_FILE_PATTERNS)


def is_secret_like(rel: str) -> bool:
    name = PurePosixPath(rel).name.lower()
    return any(fnmatchcase(name, p) for p in SECRET_FILE_PATTERNS)


def is_link_like(path: Path) -> bool:
    """True for symlinks and Windows junctions (never followed by RepoBoss)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return os.path.isjunction(path)


def check_relpath(rel: str) -> str:
    """Validate a relative POSIX path coming from configuration or an archive."""
    if not rel or "\\" in rel or "\x00" in rel:
        raise ValueError(f"invalid relative path: {rel!r}")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise ValueError(f"absolute path not allowed: {rel!r}")
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"path must not contain empty, '.' or '..' segments: {rel!r}")
    return rel


def walk_files(root: Path, skip: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
    """Return (regular files, link-like or special entries) as sorted POSIX relpaths.

    Symlinks and junctions are reported, never followed. Noise directories and the relative paths
    in ``skip`` (installed dependency directories) are not entered.
    """
    files: list[str] = []
    odd: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        rel_dir = base.relative_to(root).as_posix()
        keep: list[str] = []
        for d in dirnames:
            rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            if rel in skip or d in NOISE_DIR_NAMES or any(fnmatchcase(d, p) for p in NOISE_DIR_PATTERNS):
                continue
            if is_link_like(base / d):
                odd.append(rel)
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if rel in skip:
                continue
            full = base / name
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                odd.append(rel)
            elif not is_noise(rel):
                files.append(rel)
    return sorted(files), sorted(odd)


def tree_hash(root: Path, files: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(sha256_file(root / rel).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def copy_files(src_root: Path, dst_root: Path, files: Iterable[str]) -> None:
    for rel in files:
        target = dst_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_root / rel, target)


def unlink_link(path: Path) -> None:
    """Remove a symlink or junction itself, never its target."""
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(path)


def rmtree_quiet(path: Path) -> list[str]:
    """Best-effort removal of scratch directories: returns errors instead of raising.

    Some toolchains leave helper processes that keep files open for a while (e.g. on Windows).
    """
    try:
        rmtree(path)
    except OSError as exc:
        return [str(exc)]
    return []


def rmtree(path: Path) -> None:
    """Remove a tree without ever following links or junctions; clears read-only bits (Windows)."""
    if is_link_like(path):
        unlink_link(path)
        return
    if not path.exists():
        return
    for dirpath, dirnames, _files in os.walk(path, topdown=True, followlinks=False):
        for d in list(dirnames):
            child = Path(dirpath) / d
            if is_link_like(child):
                unlink_link(child)
                dirnames.remove(d)

    def _onexc(func: Any, p: str, _exc: BaseException) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=_onexc)


def make_scratch(prefix: str) -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix=prefix))


def read_text_exact(path: Path) -> str:
    """Read UTF-8 text without newline translation (keeps CRLF intact)."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_exact(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
