"""Collecting an agent's changes and preparing a clean evaluation tree.

Only regular files that differ from the agent's starting point are considered. A change is:

* a **violation** (the run is rejected, nothing is evaluated) when it touches a protected path,
  is a symlink/junction or special file, or exceeds the size limit;
* **ignored** (reported, not transferred) when it lies outside the editable globs;
* **allowed** otherwise, and copied into a fresh tree built from the pristine snapshot.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from repoboss.config import RepoConfig
from repoboss.snapshot import SnapshotInfo
from repoboss.transforms import Candidate, apply_edit
from repoboss.util import copy_files, matches_any, rmtree, walk_files

MAX_CHANGED_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class ChangeSet:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    patch: str = ""
    lines_added: int = 0
    lines_removed: int = 0

    @property
    def files_changed(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


BUILD_OUTPUT_DIRS = frozenset(
    {"bin", "obj", "target", "build", "dist", "out", ".gradle", ".next", "coverage", "TestResults", "cmake-build"}
)


def build_agent_baseline(snapshot: SnapshotInfo, candidate: Candidate, config: RepoConfig, dest: Path) -> list[str]:
    """Buggy tree without hidden files (tests): what the agent starts from. Returns its file list.

    Protected files (build configuration) stay visible; changing them rejects the attempt.
    """
    assert snapshot.root is not None
    visible = [f for f in snapshot.files if not matches_any(f, config.hidden, casefold=True)]
    if candidate.path not in visible or matches_any(candidate.path, config.locked, casefold=True):
        raise ValueError(f"bug location {candidate.path} is hidden or protected; challenge is invalid")
    rmtree(dest)
    copy_files(snapshot.root, dest, visible)
    apply_edit(dest, candidate.edit)
    return visible


def _diff(rel: str, before: bytes | None, after: bytes | None) -> tuple[str, int, int]:
    try:
        a = before.decode("utf-8").splitlines(keepends=True) if before is not None else []
        b = after.decode("utf-8").splitlines(keepends=True) if after is not None else []
    except UnicodeDecodeError:
        return f"Binary file {rel} changed\n", 0, 0
    lines = list(
        difflib.unified_diff(
            a,
            b,
            fromfile=f"a/{rel}" if before is not None else "/dev/null",
            tofile=f"b/{rel}" if after is not None else "/dev/null",
        )
    )
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    text = "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in lines)
    return text, added, removed


def _new_build_output(rel: str, baseline: Path) -> bool:
    """An added file under a build-output directory that did not exist in the baseline."""
    parts = PurePosixPath(rel).parts
    for i, part in enumerate(parts[:-1]):
        if part in BUILD_OUTPUT_DIRS and not (baseline / Path(*parts[: i + 1])).exists():
            return True
    return False


def collect_changes(
    baseline: Path, workspace: Path, config: RepoConfig, extra_violations: list[str] | None = None
) -> ChangeSet:
    skip = frozenset(config.dependency_dirs)
    base_files, _ = walk_files(baseline, skip=skip)
    ws_files, odd = walk_files(workspace, skip=skip)
    cs = ChangeSet()
    cs.violations.extend(f"link or special file: {p}" for p in odd)
    cs.violations.extend(extra_violations or [])
    base_set, ws_set = set(base_files), set(ws_files)
    patches: list[str] = []
    for rel in sorted(base_set | ws_set):
        before = (baseline / rel).read_bytes() if rel in base_set else None
        if rel in ws_set:
            size = (workspace / rel).stat().st_size
            if size > MAX_CHANGED_FILE_BYTES:
                if before is None or size != len(before) or (workspace / rel).read_bytes() != before:
                    cs.violations.append(f"file too large ({size} bytes): {rel}")
                continue
            after: bytes | None = (workspace / rel).read_bytes()
        else:
            after = None
        if before == after:
            continue
        if matches_any(rel, config.hidden, casefold=True):
            cs.violations.append(f"protected path changed: {rel} (hidden test file)")
            continue
        if matches_any(rel, config.protected, casefold=True):
            cs.violations.append(f"protected path changed: {rel}")
            continue
        if before is None and _new_build_output(rel, baseline):
            cs.ignored.append(rel)
            continue
        if not matches_any(rel, config.editable):
            cs.ignored.append(rel)
            continue
        if before is None:
            cs.added.append(rel)
        elif after is None:
            cs.deleted.append(rel)
        else:
            cs.modified.append(rel)
        text, plus, minus = _diff(rel, before, after)
        patches.append(text)
        cs.lines_added += plus
        cs.lines_removed += minus
    cs.patch = "".join(patches)
    return cs


def build_eval_tree(
    snapshot: SnapshotInfo, candidate: Candidate, workspace: Path, changes: ChangeSet, dest: Path
) -> None:
    """Pristine snapshot (with hidden tests) + bug + only the allowed changes."""
    assert snapshot.root is not None
    rmtree(dest)
    copy_files(snapshot.root, dest, snapshot.files)
    apply_edit(dest, candidate.edit)
    for rel in changes.added + changes.modified:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((workspace / rel).read_bytes())
    for rel in changes.deleted:
        (dest / rel).unlink(missing_ok=True)
