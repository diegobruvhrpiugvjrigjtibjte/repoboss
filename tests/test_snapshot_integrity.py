from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repoboss.agents import BUILTIN
from repoboss.challenge import create_challenges
from repoboss.run import run_challenge
from repoboss.runners import LocalRunner
from repoboss.snapshot import create_snapshot
from repoboss.util import sha256_file
from repoboss.workspace import Workspace


def fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (sha256_file(p), st.st_mtime_ns)
    return out


def test_secrets_and_noise_are_excluded(demo_repo: Path, tmp_path: Path) -> None:
    (demo_repo / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (demo_repo / "deploy.pem").write_text("-----BEGIN-----", encoding="utf-8")
    (demo_repo / ".venv" / "lib").mkdir(parents=True)
    (demo_repo / ".venv" / "lib" / "x.py").write_text("", encoding="utf-8")
    (demo_repo / "src" / "shopcalc" / "__pycache__").mkdir(exist_ok=True)
    (demo_repo / "src" / "shopcalc" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    info = create_snapshot(demo_repo, tmp_path / "store")
    assert ".env" not in info.files and "deploy.pem" not in info.files
    assert not any(f.startswith(".venv/") or "__pycache__" in f for f in info.files)
    assert {p for p, _ in info.skipped} >= {".env", "deploy.pem"}


def test_symlinks_are_not_followed(demo_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        os.symlink(outside, demo_repo / "link.txt")
    except OSError:
        pytest.skip("creating symlinks is not permitted on this system")
    info = create_snapshot(demo_repo, tmp_path / "store")
    assert "link.txt" not in info.files
    assert ("link.txt", "symlink or special file") in info.skipped


def test_snapshot_hash_is_content_addressed(demo_repo: Path, tmp_path: Path) -> None:
    a = create_snapshot(demo_repo, tmp_path / "store")
    b = create_snapshot(demo_repo, tmp_path / "store")
    assert a.hash == b.hash
    (demo_repo / "src" / "shopcalc" / "settings.py").write_text("X = 1\n", encoding="utf-8")
    assert create_snapshot(demo_repo, tmp_path / "store").hash != a.hash


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_original_repository_and_git_index_are_untouched(demo_repo: Path, ws: Workspace, runner: LocalRunner) -> None:
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=demo_repo, check=True)
    subprocess.run([*git, "add", "-A"], cwd=demo_repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=demo_repo, check=True)
    (demo_repo / "untracked_note.txt").write_text("work in progress", encoding="utf-8")
    before = fingerprint(demo_repo)

    _, results = create_challenges(ws, demo_repo, runner, transforms=["logic_condition"], seed=0)
    cid = results[0].challenge_id
    assert cid is not None
    run_challenge(ws, cid, BUILTIN["demo-oracle"], runner)

    assert fingerprint(demo_repo) == before  # content and mtimes, including .git/index
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=demo_repo, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == "?? untracked_note.txt"
