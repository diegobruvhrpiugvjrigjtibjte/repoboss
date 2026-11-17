"""Attempts, evaluation isolation and result recording (local runner)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repoboss.agents import BUILTIN, AgentSpec, read_usage
from repoboss.challenge import create_challenges
from repoboss.run import RunError, run_challenge
from repoboss.runners import LocalRunner
from repoboss.snapshot import load_snapshot
from repoboss.transforms import Candidate
from repoboss.workspace import Workspace


@pytest.fixture
def challenge(demo_repo: Path, ws: Workspace, runner: LocalRunner) -> str:
    _, results = create_challenges(ws, demo_repo, runner, transforms=["logic_condition"], seed=0)
    cid = results[0].challenge_id
    assert cid is not None
    return cid


def custom(tmp_path: Path, name: str, script: str, **kw: object) -> AgentSpec:
    agent_dir = tmp_path / f"agent-{name}"
    agent_dir.mkdir()
    (agent_dir / "agent.py").write_text(script, encoding="utf-8")
    fields: dict = dict(
        name=name,
        kind="custom",
        is_demo=False,
        description="test agent",
        command=("python", "{agent_dir}/agent.py"),
        agent_dir=agent_dir,
        timeout_s=60.0,
    )
    fields.update(kw)
    return AgentSpec(**fields)


def test_demo_oracle_passes_and_noop_fails(ws: Workspace, runner: LocalRunner, challenge: str) -> None:
    ok = run_challenge(ws, challenge, BUILTIN["demo-oracle"], runner, attempts=2)
    bad = run_challenge(ws, challenge, BUILTIN["demo-noop"], runner)
    assert [r.outcome for r in ok] == ["passed", "passed"]
    assert bad[0].outcome == "failed"
    row = ws.get_run(ok[0].run_id)
    assert row is not None
    assert row["agent_is_demo"] == 1 and row["runner_isolated"] == 0
    assert row["matches_reference"] == 1 and row["files_changed"] == 1
    assert row["tests_failed"] == 0 and row["tests_total"] == 13
    # Unknown usage is recorded as NULL, never as zero.
    assert row["input_tokens"] is None and row["output_tokens"] is None and row["cost_usd"] is None
    assert "demo adapter" in row["notes"] and "NOT isolated" in row["notes"]
    noop = ws.get_run(bad[0].run_id)
    assert noop is not None and noop["patch"] == "" and noop["failing_tests"]
    # Each attempt starts from a clean copy: working copies are removed afterwards.
    assert not (ws.run_dir(ok[0].run_id) / "workspace").exists()


def test_agent_cannot_see_protected_files_or_host_secrets(
    ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RB_TEST_SECRET", "s3cr3t")
    monkeypatch.setenv("RB_FORWARDED", "visible")
    script = (
        "import json, os, pathlib\n"
        "ws = pathlib.Path(os.environ['REPOBOSS_WORKSPACE'])\n"
        "task = json.loads(pathlib.Path(os.environ['REPOBOSS_TASK_FILE']).read_text())\n"
        "out = pathlib.Path(os.environ['REPOBOSS_OUTPUT_DIR'])\n"
        "(out / 'seen.json').write_text(json.dumps({\n"
        "  'tests': (ws / 'tests').exists(), 'config': (ws / 'repoboss.toml').exists(),\n"
        "  'secret': os.environ.get('RB_TEST_SECRET'), 'forwarded': os.environ.get('RB_FORWARDED'),\n"
        "  'failing': task['failing_tests'], 'cwd': os.getcwd() == str(ws)}))\n"
    )
    spec = custom(tmp_path, "peek", script, pass_env=("RB_FORWARDED",))
    res = run_challenge(ws, challenge, spec, runner)[0]
    import json

    seen = json.loads((ws.run_dir(res.run_id) / "agent-out" / "seen.json").read_text())
    # Tests are hidden; build/test configuration is visible but read-only.
    assert seen == {
        "tests": False,
        "config": True,
        "secret": None,
        "forwarded": "visible",
        "failing": seen["failing"],
        "cwd": True,
    }
    assert seen["failing"]


def test_custom_agent_fix_is_transferred_and_evaluated(
    ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path
) -> None:
    row = ws.get_challenge(challenge)
    assert row is not None
    cand = Candidate.from_dict(row["candidate"])
    snap = load_snapshot(ws.root / "snapshots", row["snapshot_hash"])
    assert snap.root is not None
    fixed = (snap.root / cand.path).read_text(encoding="utf-8")
    spec = custom(
        tmp_path,
        "fixer",
        (
            "import os, pathlib, shutil\n"
            "ws = pathlib.Path(os.environ['REPOBOSS_WORKSPACE'])\n"
            "agent = pathlib.Path(os.environ['REPOBOSS_AGENT_DIR'])\n"
            f"shutil.copyfile(agent / 'fixed.py', ws / {cand.path!r})\n"
            "(ws / 'NOTES.md').write_text('outside editable paths')\n"
            "out = pathlib.Path(os.environ['REPOBOSS_OUTPUT_DIR'])\n"
            '(out / \'usage.json\').write_text(\'{"input_tokens": 120, "output_tokens": 30, "source": "test"}\')\n'
            "print('fixed')\n"
        ),
        version="1.2.3",
        version_source="declared",
    )
    assert spec.agent_dir is not None
    (spec.agent_dir / "fixed.py").write_text(fixed, encoding="utf-8")
    res = run_challenge(ws, challenge, spec, runner)[0]
    run = ws.get_run(res.run_id)
    assert run is not None
    assert run["outcome"] == "passed", run
    assert run["ignored"] == ["NOTES.md"]
    assert run["agent_version"] == "1.2.3" and run["agent_version_source"] == "declared"
    assert run["input_tokens"] == 120 and run["output_tokens"] == 30 and run["cost_usd"] is None
    assert run["usage_source"] == "agent-reported (test)"
    assert cand.path in run["patch"]


def test_protected_change_is_rejected_without_evaluation(
    ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path
) -> None:
    spec = custom(
        tmp_path,
        "cheater",
        (
            "import os, pathlib\n"
            "ws = pathlib.Path(os.environ['REPOBOSS_WORKSPACE'])\n"
            "(ws / 'tests').mkdir()\n"
            "(ws / 'tests' / 'test_pricing.py').write_text('def test_ok():\\n    pass\\n')\n"
        ),
    )
    res = run_challenge(ws, challenge, spec, runner)[0]
    run = ws.get_run(res.run_id)
    assert run is not None
    assert run["outcome"] == "rejected" and run["eval_status"] == "rejected"
    assert run["violations"] == ["protected path changed: tests/test_pricing.py (hidden test file)"]
    assert run["tests_total"] is None


def test_read_only_config_change_is_rejected(
    ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path
) -> None:
    spec = custom(
        tmp_path,
        "configcheat",
        (
            "import os, pathlib\n"
            "ws = pathlib.Path(os.environ['REPOBOSS_WORKSPACE'])\n"
            "p = ws / 'pyproject.toml'\n"
            "p.write_text(p.read_text() + '\\n# addopts = -k nothing\\n')\n"
        ),
    )
    run = ws.get_run(run_challenge(ws, challenge, spec, runner)[0].run_id)
    assert run is not None and run["outcome"] == "rejected"
    assert run["violations"] == ["protected path changed: pyproject.toml"]


def test_protected_match_is_case_insensitive(
    ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path
) -> None:
    spec = custom(
        tmp_path,
        "case",
        (
            "import os, pathlib\n"
            "ws = pathlib.Path(os.environ['REPOBOSS_WORKSPACE'])\n"
            "(ws / 'Tests').mkdir()\n"
            "(ws / 'Tests' / 'conftest.py').write_text('')\n"
        ),
    )
    run = ws.get_run(run_challenge(ws, challenge, spec, runner)[0].run_id)
    assert run is not None and run["outcome"] == "rejected"


def test_symlink_in_workspace_is_rejected(ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    try:
        os.symlink(tmp_path, probe, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("creating symlinks is not permitted on this system")
    target = str(tmp_path).replace("\\", "/")
    spec = custom(
        tmp_path,
        "linker",
        (
            "import os, pathlib\n"
            "ws = pathlib.Path(os.environ['REPOBOSS_WORKSPACE'])\n"
            f"os.symlink({target!r}, ws / 'src' / 'escape', target_is_directory=True)\n"
        ),
    )
    run = ws.get_run(run_challenge(ws, challenge, spec, runner)[0].run_id)
    assert run is not None
    assert run["outcome"] == "rejected"
    assert any("link or special file" in v for v in run["violations"])


def test_agent_timeout_kills_the_attempt(ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path) -> None:
    spec = custom(
        tmp_path,
        "sleeper",
        (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "time.sleep(120)\n"
        ),
    )
    res = run_challenge(ws, challenge, spec, runner, timeout_s=2)[0]
    run = ws.get_run(res.run_id)
    assert run is not None
    assert run["attempt_status"] == "timeout"
    assert run["attempt_exit_code"] is None
    assert run["attempt_duration_s"] < 30
    # Nothing changed, so the evaluation still runs and fails on the bug.
    assert run["outcome"] == "failed"


def test_agent_nonzero_exit_is_recorded(ws: Workspace, runner: LocalRunner, challenge: str, tmp_path: Path) -> None:
    spec = custom(tmp_path, "crash", "raise SystemExit(3)\n")
    run = ws.get_run(run_challenge(ws, challenge, spec, runner)[0].run_id)
    assert run is not None
    assert run["attempt_status"] == "agent_error" and run["attempt_exit_code"] == 3


def test_unaccepted_or_untrusted_challenges_are_refused(ws: Workspace, runner: LocalRunner, challenge: str) -> None:
    ws.update_challenge(challenge, {"trusted": False})
    with pytest.raises(RunError, match="untrusted"):
        run_challenge(ws, challenge, BUILTIN["demo-oracle"], runner)
    ws.update_challenge(challenge, {"trusted": True, "status": "restore_fails"})
    with pytest.raises(RunError, match="not accepted"):
        run_challenge(ws, challenge, BUILTIN["demo-oracle"], runner)


def test_usage_file_validation(tmp_path: Path) -> None:
    assert read_usage(tmp_path) == (None, [])
    (tmp_path / "usage.json").write_text('{"input_tokens": -5, "output_tokens": "many", "cost_usd": 0.02}')
    usage, notes = read_usage(tmp_path)
    assert usage is not None and usage.input_tokens is None and usage.output_tokens is None
    assert usage.cost_usd == 0.02 and len(notes) == 2
    (tmp_path / "usage.json").write_text("not json")
    assert read_usage(tmp_path)[0] is None
