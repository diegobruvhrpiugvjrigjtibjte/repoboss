"""Running agents against a challenge and recording verifiable results."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from repoboss.agents import AgentSpec, AttemptContext, AttemptResult, make_adapter
from repoboss.challenge import ACCEPTED
from repoboss.config import RepoConfig
from repoboss.evaluation import ChangeSet, build_agent_baseline, build_eval_tree, collect_changes
from repoboss.runners import Runner
from repoboss.snapshot import load_snapshot
from repoboss.testing import PASSED, TestReport, run_tests
from repoboss.transforms import Candidate
from repoboss.util import copy_files, new_id, now_iso, read_text_exact, rmtree_quiet, walk_files
from repoboss.workspace import Workspace

TESTS_DISCLAIMER = (
    "Passing the protected tests shows the change satisfies those tests; it does not prove the fix is fully correct."
)


class RunError(Exception):
    pass


# Evaluation statuses caused by the agent's code (tests fail, code does not build or run).
AGENT_FAILURES = {"failed", "no_results", "build_error", "collection_error", "error", "no_tests"}


def run_outcome(eval_status: str) -> str:
    """Overall outcome of an attempt: passed / failed / timeout / infra_error (rejected is set earlier)."""
    if eval_status == PASSED:
        return PASSED
    if eval_status in AGENT_FAILURES:
        return "failed"
    if eval_status == "timeout":
        return "timeout"
    return "infra_error"


@dataclass
class RunSummary:
    run_id: str
    outcome: str
    attempt_status: str | None
    eval_status: str | None
    report: TestReport | None
    changes: ChangeSet | None


def _task(challenge: dict[str, Any], config: RepoConfig, network: bool) -> dict[str, Any]:
    verification = challenge.get("verification") or {}
    broken = verification.get("broken") or {}
    return {
        "format": "repoboss-task/1",
        "challenge_id": challenge["id"],
        "instructions": (
            "A bug was introduced into this repository. Some tests that are hidden from you now fail. "
            "Change the source code so that the behaviour is correct again. Test files are not present "
            "here and must not be created or modified; build and test configuration files are read-only. "
            "Such changes cause the attempt to be rejected."
        ),
        "failing_tests": broken.get("failing", []),
        "test_command": list(config.test),
        "editable": list(config.editable),
        "hidden": list(config.hidden),
        "protected": list(config.protected),
        "network": network,
    }


def run_challenge(
    ws: Workspace,
    challenge_id: str,
    spec: AgentSpec,
    runner: Runner,
    *,
    attempts: int = 1,
    network: bool | None = None,
    timeout_s: float | None = None,
    keep_workspaces: bool = False,
) -> list[RunSummary]:
    challenge = ws.get_challenge(challenge_id)
    if challenge is None:
        raise RunError(f"challenge {challenge_id} not found")
    if not challenge["trusted"]:
        raise RunError(
            f"{challenge_id} is imported and untrusted. Inspect it, then run "
            f"`repoboss challenge verify {challenge_id} --trust`."
        )
    if challenge["status"] != ACCEPTED:
        raise RunError(
            f"{challenge_id} is not accepted (status: {challenge['status']}). "
            f"Run `repoboss challenge verify {challenge_id}`."
        )
    if attempts < 1:
        raise RunError("attempts must be at least 1")
    config = RepoConfig.from_dict(challenge["config"])
    candidate = Candidate.from_dict(challenge["candidate"])
    snapshot = load_snapshot(ws.root / "snapshots", challenge["snapshot_hash"])
    use_network = spec.network if network is None else network
    agent_timeout = timeout_s or spec.timeout_s
    batch = new_id("batch")
    out: list[RunSummary] = []
    for attempt in range(1, attempts + 1):
        out.append(
            _one_attempt(
                ws,
                challenge,
                config,
                candidate,
                snapshot,
                spec,
                runner,
                batch,
                attempt,
                use_network,
                agent_timeout,
                keep_workspaces,
            )
        )
    return out


def _one_attempt(
    ws: Workspace,
    challenge: dict[str, Any],
    config: RepoConfig,
    candidate: Candidate,
    snapshot: Any,
    spec: AgentSpec,
    runner: Runner,
    batch: str,
    attempt: int,
    network: bool,
    agent_timeout: float,
    keep: bool,
) -> RunSummary:
    run_id = new_id("run")
    rdir = ws.run_dir(run_id)
    rdir.mkdir(parents=True)
    started = time.monotonic()
    ws.insert_run(
        {
            "id": run_id,
            "challenge_id": challenge["id"],
            "batch_id": batch,
            "attempt": attempt,
            "created_at": now_iso(),
            "agent_name": spec.name,
            "agent_version": spec.version,
            "agent_version_source": spec.version_source,
            "agent_is_demo": spec.is_demo,
            "runner": runner.kind,
            "runner_isolated": runner.isolated,
            "network": network,
            "config": {
                "agent": spec.public(),
                "challenge_config": config.to_dict(),
                "timeout_s": agent_timeout,
                "limits": vars(runner.limits),
            },
            "outcome": "running",
        }
    )

    def finish(values: dict[str, Any]) -> None:
        values.setdefault("finished_at", now_iso())
        values.setdefault("total_duration_s", round(time.monotonic() - started, 3))
        ws.update_run(run_id, values)

    prep = runner.prepare(snapshot.root, snapshot.files, snapshot.hash, config)
    (rdir / "prepare.log").write_text(prep.log, encoding="utf-8")
    if prep.env is None:
        finish({"prepare_status": prep.status, "outcome": prep.status})
        return RunSummary(run_id, prep.status, None, None, None, None)
    env = prep.env

    baseline = rdir / "baseline"
    agent_ws = rdir / "workspace"
    agent_out = rdir / "agent-out"
    agent_out.mkdir()
    visible = build_agent_baseline(snapshot, candidate, config, baseline)
    copy_files(baseline, agent_ws, visible)
    task = _task(challenge, config, network)
    (agent_out / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")

    adapter = make_adapter(spec)
    try:
        result = adapter.attempt(
            AttemptContext(runner, env, agent_ws, agent_out, task, candidate, agent_timeout, network, config)
        )
    except Exception as exc:  # adapter bug: record, never crash the batch
        result = AttemptResult("launch_error", None, 0.0, f"adapter raised {type(exc).__name__}: {exc}\n")
    (rdir / "agent.log").write_text(result.log, encoding="utf-8")
    notes = list(result.notes)
    if result.version is not None:
        ws.update_run(run_id, {"agent_version": result.version, "agent_version_source": result.version_source})
    if not runner.isolated:
        notes.append("local runner: the attempt was NOT isolated (no network, CPU or memory restrictions)")
    if spec.is_demo:
        notes.append("demo adapter: verifies the pipeline only, not a model's ability")

    values: dict[str, Any] = {
        "env": env.to_dict(),
        "prepare_status": "ready",
        "attempt_status": result.status,
        "attempt_exit_code": result.exit_code,
        "attempt_duration_s": round(result.duration_s, 3),
    }
    if result.usage is not None:
        values.update(
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cost_usd=result.usage.cost_usd,
            usage_source=result.usage.source,
        )

    changes = collect_changes(baseline, agent_ws, config, extra_violations=result.violations)
    (rdir / "final.patch").write_text(changes.patch, encoding="utf-8")
    values.update(
        patch=changes.patch,
        files_changed=changes.files_changed,
        lines_added=changes.lines_added,
        lines_removed=changes.lines_removed,
        violations=changes.violations,
        ignored=changes.ignored,
    )
    report: TestReport | None = None
    if changes.violations:
        values.update(eval_status="rejected", outcome="rejected")
    else:
        eval_tree = rdir / "eval"
        build_eval_tree(snapshot, candidate, agent_ws, changes, eval_tree)
        report = run_tests(
            runner,
            env,
            config,
            eval_tree,
            rdir / "eval-out",
            label="eval",
            known_files=walk_files(eval_tree, skip=frozenset(config.dependency_dirs))[0],
        )
        bug_file = eval_tree / candidate.path
        original_file = snapshot.root / candidate.path
        values.update(
            eval_status=report.status,
            eval_exit_code=report.exit_code,
            eval_duration_s=report.duration_s,
            tests_passed=report.passed,
            tests_failed=report.failed,
            tests_errors=report.errors,
            tests_skipped=report.skipped,
            tests_total=report.total,
            failing_tests=report.failing,
            matches_reference=bug_file.is_file() and read_text_exact(bug_file) == read_text_exact(original_file),
            outcome=run_outcome(report.status),
        )
        if report.status == PASSED:
            notes.append(TESTS_DISCLAIMER)
    values["notes"] = "\n".join(notes) or None
    finish(values)
    if not keep:
        for sub in ("baseline", "workspace", "eval", "agent-out/run-scratch"):
            rmtree_quiet(rdir / sub)
    return RunSummary(run_id, values["outcome"], result.status, values.get("eval_status"), report, changes)
