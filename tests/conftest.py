from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from repoboss.runners import LocalRunner
from repoboss.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "shopcalc"

FAST_CONFIG = """\
[repoboss]
name = "{name}"
install = []
test = ["python", "-m", "pytest", "-q"]
test_framework = "pytest"
hidden = ["tests/**"]
protected = ["repoboss.toml", "pyproject.toml"]
editable = ["src/**"]
source = ["src/*.py"]
timeout_s = {timeout}
"""


def write_config(repo: Path, name: str = "shopcalc", timeout: float = 60) -> None:
    """Config without install steps: tests use the interpreter running this test suite (has pytest)."""
    (repo / "repoboss.toml").write_text(FAST_CONFIG.format(name=name, timeout=timeout), encoding="utf-8")


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "shopcalc"
    shutil.copytree(DEMO, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    write_config(repo)
    return repo


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    workspace, _ = Workspace.init(tmp_path / "ws", runner="local")
    return workspace


@pytest.fixture
def runner(ws: Workspace) -> LocalRunner:
    return LocalRunner(ws.envs_dir)


def make_repo(root: Path, files: dict[str, str], *, timeout: float = 60) -> Path:
    """Small synthetic repository with pytest configured on src/."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    write_config(root, name=root.name, timeout=timeout)
    return root


PYTHON = sys.executable
