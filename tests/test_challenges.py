from __future__ import annotations

from pathlib import Path

from repoboss.challenge import ACCEPTED, create_challenges, verify_challenge
from repoboss.runners import LocalRunner
from repoboss.workspace import Workspace

from .conftest import make_repo

ALL = ["config_value", "path_reference", "logic_condition"]


def test_demo_repository_yields_three_accepted_challenges(demo_repo: Path, ws: Workspace, runner: LocalRunner) -> None:
    snapshot, results = create_challenges(ws, demo_repo, runner, transforms=ALL, seed=0)
    assert [r.transform for r in results] == ALL
    assert all(r.challenge_id for r in results), [r.verification.to_dict() for r in results]
    for res in results:
        assert res.challenge_id is not None
        row = ws.get_challenge(res.challenge_id)
        assert row is not None
        assert row["status"] == ACCEPTED
        assert row["seed"] == 0
        assert row["snapshot_hash"] == snapshot.hash
        assert row["env"]["runner"] == "local" and row["env"]["python_version"]
        v = row["verification"]
        assert v["original"]["status"] == "passed"
        assert v["broken"]["status"] == "failed" and v["broken"]["failing"]
        assert v["restored"]["status"] == "passed"
        assert row["config"]["test"] == ["python", "-m", "pytest", "-q"]


def test_verify_is_repeatable(demo_repo: Path, ws: Workspace, runner: LocalRunner) -> None:
    _, results = create_challenges(ws, demo_repo, runner, transforms=["config_value"], seed=3)
    cid = results[0].challenge_id
    assert cid is not None
    first = ws.get_challenge(cid)
    v = verify_challenge(ws, cid, runner)
    assert v.status == ACCEPTED
    assert v.check is not None and v.check.broken is not None and first is not None
    assert v.check.broken.failing == first["verification"]["broken"]["failing"]


def test_transformation_not_applicable(tmp_path: Path, ws: Workspace, runner: LocalRunner) -> None:
    repo = make_repo(
        tmp_path / "noconst",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": "def double(x):\n    return x * 2\n",
            "tests/test_core.py": "from pkg.core import double\n\ndef test_double():\n    assert double(2) == 4\n",
        },
    )
    _, results = create_challenges(ws, repo, runner, transforms=["config_value"], seed=0)
    assert results[0].challenge_id is None
    assert results[0].verification.status == "not_applicable"
    assert ws.list_challenges() == []


def test_bug_not_detected_is_rejected(tmp_path: Path, ws: Workspace, runner: LocalRunner) -> None:
    repo = make_repo(
        tmp_path / "untested",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": "RETRIES = 3\n\ndef double(x):\n    return x * 2\n",
            "tests/test_core.py": "from pkg.core import double\n\ndef test_double():\n    assert double(2) == 4\n",
        },
    )
    _, results = create_challenges(ws, repo, runner, transforms=["config_value"], seed=0)
    v = results[0].verification
    assert results[0].challenge_id is None
    assert v.status == "no_valid_candidate"
    assert [r.status for r in v.rejected] == ["bug_not_detected"]


def test_original_failing_is_rejected(tmp_path: Path, ws: Workspace, runner: LocalRunner) -> None:
    repo = make_repo(
        tmp_path / "red",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": "LIMIT = 3\n",
            "tests/test_core.py": "def test_red():\n    assert 1 == 2\n",
        },
    )
    _, results = create_challenges(ws, repo, runner, transforms=["config_value"], seed=0)
    assert results[0].verification.status == "original_fails"


def test_missing_dependency_is_classified(tmp_path: Path, ws: Workspace, runner: LocalRunner) -> None:
    repo = make_repo(
        tmp_path / "deps",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": "LIMIT = 3\n",
            "tests/test_core.py": "import surely_not_installed_pkg_rb\n\ndef test_x():\n    assert True\n",
        },
    )
    _, results = create_challenges(ws, repo, runner, transforms=["config_value"], seed=0)
    v = results[0].verification
    assert v.status == "dependency_missing"
    assert v.original is not None and v.original.missing_module == "surely_not_installed_pkg_rb"


def test_timeout_is_classified(tmp_path: Path, ws: Workspace, runner: LocalRunner) -> None:
    repo = make_repo(
        tmp_path / "slow",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": "LIMIT = 3\n",
            "tests/test_core.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n",
        },
        timeout=3,
    )
    _, results = create_challenges(ws, repo, runner, transforms=["config_value"], seed=0)
    v = results[0].verification
    assert v.status == "timeout"
    assert v.original is not None and v.original.duration_s < 30


def test_bug_breaking_collection_is_not_accepted(tmp_path: Path, ws: Workspace, runner: LocalRunner) -> None:
    repo = make_repo(
        tmp_path / "importtime",
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": ("from pathlib import Path\nTEXT = (Path(__file__).parent / 'words.txt').read_text()\n"),
            "src/pkg/words.txt": "hello",
            "tests/test_core.py": "from pkg.core import TEXT\n\ndef test_text():\n    assert TEXT == 'hello'\n",
        },
    )
    _, results = create_challenges(ws, repo, runner, transforms=["path_reference"], seed=0)
    v = results[0].verification
    assert results[0].challenge_id is None
    assert [r.status for r in v.rejected] == ["bug_breaks_collection"]
