"""Challenge creation and verification.

A challenge is accepted only when all three checks hold, each in a fresh copy of the snapshot:

1. the original snapshot passes its tests;
2. the snapshot with the injected bug runs its tests and at least one fails (status ``failed``:
   build errors, collection errors, timeouts and missing dependencies are rejected, not counted as a
   detected bug);
3. applying the inverse patch restores byte-identical content and the tests pass again.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoboss.config import RepoConfig, load_repo_config
from repoboss.runners import EnvInfo, Runner
from repoboss.snapshot import SnapshotInfo, create_snapshot, load_snapshot
from repoboss.testing import FAILED, PASSED, TestReport, run_tests
from repoboss.transforms import TRANSFORMS, Candidate, TransformError, apply_edit, find_candidates
from repoboss.util import (
    copy_files,
    make_scratch,
    matches_any,
    new_id,
    now_iso,
    rmtree,
    rmtree_quiet,
    tree_hash,
)
from repoboss.workspace import Workspace

# Challenge statuses
ACCEPTED = "accepted"
IMPORTED_UNVERIFIED = "imported_unverified"

BROKEN_STATUS_REASON = {
    "collection_error": "bug_breaks_collection",
    "build_error": "bug_breaks_build",
    "no_results": "bug_breaks_build",
    "error": "bug_breaks_run",
}


class ChallengeError(Exception):
    pass


@dataclass
class CandidateCheck:
    candidate: Candidate
    status: str  # accepted | bug_not_detected | bug_breaks_build | timeout | restore_fails | ...
    broken: TestReport | None = None
    restored: TestReport | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": {k: v for k, v in self.candidate.to_dict().items() if k != "edit"},
            "status": self.status,
            "detail": self.detail,
            "broken": self.broken.summary() if self.broken else None,
            "restored": self.restored.summary() if self.restored else None,
        }


@dataclass
class Verification:
    status: str
    detail: str
    original: TestReport | None = None
    check: CandidateCheck | None = None
    env: EnvInfo | None = None
    prepare_log: str = ""
    rejected: list[CandidateCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "original": self.original.summary() if self.original else None,
            "broken": self.check.broken.summary() if self.check and self.check.broken else None,
            "restored": self.check.restored.summary() if self.check and self.check.restored else None,
            "rejected_candidates": [r.to_dict() for r in self.rejected],
            "warnings": self.warnings,
            "verified_at": now_iso(),
        }


def materialize(snapshot: SnapshotInfo, dest: Path, candidate: Candidate | None = None) -> None:
    """Fresh copy of the snapshot at ``dest``, optionally with the bug applied."""
    assert snapshot.root is not None
    rmtree(dest)
    copy_files(snapshot.root, dest, snapshot.files)
    if candidate is not None:
        apply_edit(dest, candidate.edit)


def _status_for_original(report: TestReport) -> str:
    return {PASSED: "ok", FAILED: "original_fails"}.get(report.status, report.status)


def check_original(
    runner: Runner, env: EnvInfo, config: RepoConfig, snapshot: SnapshotInfo, scratch: Path
) -> TestReport:
    tree = scratch / "original"
    materialize(snapshot, tree)
    report = run_tests(runner, env, config, tree, scratch / "out", label="original", known_files=snapshot.files)
    rmtree_quiet(tree)
    return report


def check_candidate(
    runner: Runner, env: EnvInfo, config: RepoConfig, snapshot: SnapshotInfo, candidate: Candidate, scratch: Path
) -> CandidateCheck:
    if matches_any(candidate.path, config.locked, casefold=True):
        return CandidateCheck(candidate, "not_applicable", detail="bug location is hidden or protected")
    tree = scratch / "broken"
    try:
        materialize(snapshot, tree, candidate)
    except TransformError as exc:
        return CandidateCheck(candidate, "not_applicable", detail=str(exc))
    try:
        broken = run_tests(runner, env, config, tree, scratch / "out", label="broken", known_files=snapshot.files)
        if broken.status == PASSED:
            return CandidateCheck(candidate, "bug_not_detected", broken, detail="tests still pass with the bug")
        if broken.status != FAILED:
            reason = BROKEN_STATUS_REASON.get(broken.status, broken.status)
            return CandidateCheck(candidate, reason, broken, detail=f"buggy version ended with {broken.status}")
        # Restore in a fresh copy so build outputs of the buggy run cannot leak into the check.
        materialize(snapshot, tree, candidate)
        try:
            apply_edit(tree, candidate.edit.inverse())
        except TransformError as exc:
            return CandidateCheck(candidate, "restore_fails", broken, detail=str(exc))
        if tree_hash(tree, snapshot.files) != snapshot.hash:
            return CandidateCheck(candidate, "restore_fails", broken, detail="restored tree differs from snapshot")
        restored = run_tests(runner, env, config, tree, scratch / "out", label="restored", known_files=snapshot.files)
        if restored.status != PASSED:
            return CandidateCheck(
                candidate, "restore_fails", broken, restored, detail=f"restored version ended with {restored.status}"
            )
        return CandidateCheck(candidate, ACCEPTED, broken, restored)
    finally:
        rmtree_quiet(tree)


@dataclass
class CreateResult:
    transform: str
    challenge_id: str | None
    verification: Verification


def _store(
    ws: Workspace,
    repo: Path,
    config: RepoConfig,
    snapshot: SnapshotInfo,
    name: str,
    seed: int,
    runner: Runner,
    verification: Verification,
    prep_log: str,
) -> str:
    assert verification.check is not None and verification.env is not None
    cid = new_id("ch")
    ws.insert_challenge(
        {
            "id": cid,
            "name": f"{config.name}:{name}",
            "created_at": now_iso(),
            "source_repo": str(repo.resolve()),
            "snapshot_hash": snapshot.hash,
            "transform": name,
            "seed": seed,
            "description": verification.check.candidate.description,
            "candidate": verification.check.candidate.to_dict(),
            "config": config.to_dict(),
            "status": ACCEPTED,
            "status_detail": verification.detail,
            "verification": verification.to_dict(),
            "env": verification.env.to_dict(),
            "runner": runner.kind,
            "trusted": True,
            "verified_at": now_iso(),
        }
    )
    cdir = ws.challenge_dir(cid)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "prepare.log").write_text(prep_log, encoding="utf-8")
    return cid


def create_challenges(
    ws: Workspace,
    repo: Path,
    runner: Runner,
    *,
    transforms: Sequence[str],
    seed: int,
    max_candidates: int = 8,
    per_transform: int = 1,
    config: RepoConfig | None = None,
) -> tuple[SnapshotInfo, list[CreateResult]]:
    """Snapshot ``repo`` and create up to ``per_transform`` accepted challenges per transformation."""
    for name in transforms:
        if name not in TRANSFORMS:
            raise ChallengeError(f"unknown transformation {name!r}; known: {', '.join(TRANSFORMS)}")
    config = config or load_repo_config(repo)
    snapshot = create_snapshot(repo, ws.root / "snapshots")
    assert snapshot.root is not None
    prep = runner.prepare(snapshot.root, snapshot.files, snapshot.hash, config)
    results: list[CreateResult] = []
    if prep.env is None:
        for name in transforms:
            results.append(
                CreateResult(
                    name, None, Verification(prep.status, "environment preparation failed", prepare_log=prep.log)
                )
            )
        return snapshot, results

    scratch = make_scratch("repoboss-verify-")
    try:
        original = check_original(runner, prep.env, config, snapshot, scratch)
        if original.status != PASSED:
            status = _status_for_original(original)
            for name in transforms:
                results.append(
                    CreateResult(
                        name,
                        None,
                        Verification(
                            status,
                            f"original snapshot: {original.status}",
                            original,
                            env=prep.env,
                            prepare_log=prep.log,
                        ),
                    )
                )
            return snapshot, results

        for name in transforms:
            warnings: list[str] = []
            candidates = find_candidates(
                snapshot.root,
                snapshot.files,
                transform=name,
                seed=seed,
                source=config.source,
                protected=config.locked,
                warnings=warnings,
            )
            if not candidates:
                results.append(
                    CreateResult(
                        name,
                        None,
                        Verification(
                            "not_applicable",
                            f"no site satisfies the preconditions of {name}",
                            original,
                            env=prep.env,
                            warnings=warnings,
                        ),
                    )
                )
                continue
            rejected: list[CandidateCheck] = []
            made = 0
            for cand in candidates[:max_candidates]:
                check = check_candidate(runner, prep.env, config, snapshot, cand, scratch)
                if check.status != ACCEPTED:
                    rejected.append(check)
                    continue
                verification = Verification(
                    ACCEPTED,
                    "original passes, bug fails tests, restored passes",
                    original,
                    check,
                    prep.env,
                    rejected=list(rejected),
                    warnings=warnings,
                )
                cid = _store(ws, repo, config, snapshot, name, seed, runner, verification, prep.log)
                results.append(CreateResult(name, cid, verification))
                made += 1
                if made >= per_transform:
                    break
            if made == 0:
                results.append(
                    CreateResult(
                        name,
                        None,
                        Verification(
                            "no_valid_candidate",
                            f"{len(rejected)} candidate(s) tried, none accepted",
                            original,
                            env=prep.env,
                            rejected=rejected,
                            warnings=warnings,
                        ),
                    )
                )
    finally:
        rmtree_quiet(scratch)
    return snapshot, results


def verify_challenge(ws: Workspace, challenge_id: str, runner: Runner, *, trust: bool = False) -> Verification:
    """Re-run the full acceptance check for an existing (or imported) challenge."""
    row = ws.get_challenge(challenge_id)
    if row is None:
        raise ChallengeError(f"challenge {challenge_id} not found")
    if not row["trusted"] and not trust:
        raise ChallengeError(
            f"{challenge_id} was imported and has never been executed here. Verifying runs its install and "
            "test commands. Inspect it first (`repoboss challenge show`), then pass --trust to proceed."
        )
    config = RepoConfig.from_dict(row["config"])
    snapshot = load_snapshot(ws.root / "snapshots", row["snapshot_hash"])
    assert snapshot.root is not None
    candidate = Candidate.from_dict(row["candidate"])
    prep = runner.prepare(snapshot.root, snapshot.files, snapshot.hash, config)
    if prep.env is None:
        verification = Verification(prep.status, "environment preparation failed", prepare_log=prep.log)
    else:
        scratch = make_scratch("repoboss-verify-")
        try:
            original = check_original(runner, prep.env, config, snapshot, scratch)
            if original.status != PASSED:
                verification = Verification(
                    _status_for_original(original), f"original snapshot: {original.status}", original, env=prep.env
                )
            else:
                check = check_candidate(runner, prep.env, config, snapshot, candidate, scratch)
                detail = (
                    "original passes, bug fails tests, restored passes" if check.status == ACCEPTED else check.detail
                )
                verification = Verification(check.status, detail, original, check, prep.env)
        finally:
            rmtree_quiet(scratch)
    values: dict[str, Any] = {
        "status": verification.status,
        "status_detail": verification.detail,
        "verification": verification.to_dict(),
        "runner": runner.kind,
        "verified_at": now_iso(),
    }
    if verification.env is not None:
        values["env"] = verification.env.to_dict()
    if trust:
        values["trusted"] = True
    ws.update_challenge(challenge_id, values)
    cdir = ws.challenge_dir(challenge_id)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "prepare.log").write_text(prep.log, encoding="utf-8")
    (cdir / "verification.json").write_text(json.dumps(verification.to_dict(), indent=2), encoding="utf-8")
    return verification
