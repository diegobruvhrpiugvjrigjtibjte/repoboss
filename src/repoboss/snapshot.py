"""Read-only snapshots of a user repository.

The original repository is only ever read: files are listed (``git ls-files`` when available, with
optional locks disabled so the index is not rewritten) and copied byte for byte into the workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from repoboss.util import (
    copy_files,
    is_link_like,
    is_noise,
    is_secret_like,
    rmtree,
    tree_hash,
    walk_files,
)

MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass
class SnapshotInfo:
    hash: str
    files: list[str]
    skipped: list[tuple[str, str]] = field(default_factory=list)
    root: Path | None = None


def _git_files(repo: Path) -> list[str] | None:
    if not (repo / ".git").exists() or shutil.which("git") is None:
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repo,
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return sorted({p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p})


def list_repo_files(repo: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """Files to include and (path, reason) for everything deliberately left out."""
    skipped: list[tuple[str, str]] = []
    listed = _git_files(repo)
    if listed is None:
        listed, odd = walk_files(repo)
        skipped.extend((p, "symlink or special file") for p in odd)
    files: list[str] = []
    for rel in listed:
        full = repo / rel
        if is_noise(rel):
            continue
        if is_link_like(full):
            skipped.append((rel, "symlink or special file"))
            continue
        if not full.is_file():
            continue  # deleted but still in the index
        if is_secret_like(rel):
            skipped.append((rel, "looks like a credential file"))
            continue
        if full.stat().st_size > MAX_FILE_BYTES:
            skipped.append((rel, f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"))
            continue
        files.append(rel)
    return files, skipped


def create_snapshot(repo: Path, store: Path) -> SnapshotInfo:
    """Copy ``repo`` into ``store/<hash>/tree`` (content addressed) and return its description."""
    repo = repo.resolve()
    files, skipped = list_repo_files(repo)
    if not files:
        raise ValueError(f"{repo}: no files to snapshot")
    staging = store / f".staging-{os.getpid()}"
    rmtree(staging)
    copy_files(repo, staging / "tree", files)
    digest = tree_hash(staging / "tree", files)
    final = store / digest
    if final.exists():
        rmtree(staging)
    else:
        (staging / "files.json").write_text(json.dumps(files, indent=0), encoding="utf-8")
        staging.rename(final)
    return SnapshotInfo(hash=digest, files=files, skipped=skipped, root=final / "tree")


def load_snapshot(store: Path, digest: str) -> SnapshotInfo:
    base = store / digest
    files = json.loads((base / "files.json").read_text(encoding="utf-8"))
    return SnapshotInfo(hash=digest, files=list(files), root=base / "tree")
