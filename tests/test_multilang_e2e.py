"""End-to-end runs on the bundled non-Python examples, with the real toolchains when installed."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from repoboss.agents import BUILTIN
from repoboss.challenge import create_challenges
from repoboss.config import load_repo_config
from repoboss.run import run_challenge
from repoboss.runners import LocalRunner
from repoboss.workspace import Workspace

from .conftest import ROOT

EXAMPLES = {
    "shopcalc-js": ("node",),
    "shopcalc-rs": ("cargo",),
    "shopcalc-c": ("cmake", "ninja", "gcc"),
    "shopcalc-cs": ("dotnet",),
}


@pytest.mark.parametrize("example", sorted(EXAMPLES))
def test_example_end_to_end(example: str, tmp_path: Path, ws: Workspace, runner: LocalRunner) -> None:
    missing = [tool for tool in EXAMPLES[example] if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"toolchain not installed: {', '.join(missing)}")
    repo = tmp_path / example
    shutil.copytree(ROOT / "examples" / example, repo)
    config = load_repo_config(repo)
    transforms = ["logic_condition"] if example == "shopcalc-cs" else ["config_value", "logic_condition"]
    _, results = create_challenges(ws, repo, runner, transforms=transforms, seed=0, config=config)
    for res in results:
        assert res.challenge_id is not None, (example, res.transform, res.verification.to_dict())
        v = res.verification
        assert v.original is not None and v.original.status == "passed" and (v.original.total or 0) >= 13
        assert v.check is not None and v.check.broken is not None and v.check.broken.status == "failed"
    cid = results[0].challenge_id
    assert cid is not None
    ok = run_challenge(ws, cid, BUILTIN["demo-oracle"], runner)[0]
    bad = run_challenge(ws, cid, BUILTIN["demo-noop"], runner)[0]
    assert (ok.outcome, bad.outcome) == ("passed", "failed")
