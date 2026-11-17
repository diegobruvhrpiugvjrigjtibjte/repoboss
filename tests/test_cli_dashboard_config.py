from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from repoboss.agents import AgentConfigError, load_agents
from repoboss.cli import app
from repoboss.config import ConfigError, RepoConfig, load_repo_config
from repoboss.dashboard.app import create_app
from repoboss.workspace import Workspace

cli = CliRunner()


def invoke(home: Path, *args: str):
    return cli.invoke(app, ["--home", str(home), *args])


def test_cli_end_to_end(demo_repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert invoke(home, "init", "--runner", "local").exit_code == 0
    res = invoke(home, "doctor")
    assert "workspace" in res.output and "python" in res.output
    res = invoke(home, "challenge", "create", "--repo", str(demo_repo), "--transform", "config_value")
    assert res.exit_code == 0, res.output
    cid = re.search(r"\[accepted\] (ch-[0-9a-f]{8})", res.output).group(1)
    res = invoke(home, "run", cid, "--agent", "demo-oracle", "--attempts", "2")
    assert res.exit_code == 0, res.output
    assert "DEMO adapter" in res.output and "2/2 attempt(s) passed" in res.output
    assert "too few to draw statistical conclusions" in res.output
    run_ids = re.findall(r"run-[0-9a-f]{8}", res.output)
    res = invoke(home, "compare", *run_ids[:2])
    assert res.exit_code == 0 and "Matches reference fix" in res.output
    res = invoke(home, "compare", "--challenge", cid)
    assert "not a general ranking" in res.output
    res = invoke(home, "runs", "show", run_ids[0], "--patch")
    assert "@@" in res.output
    res = invoke(home, "challenge", "show", cid)
    assert "use --reveal" in res.output
    pkg = tmp_path / "c.zip"
    res = invoke(home, "export", cid, "--out", str(pkg))
    assert res.exit_code == 0 and "snapshot/tests/test_pricing.py" in res.output
    res = invoke(tmp_path / "home2", "init", "--runner", "local")
    res = invoke(tmp_path / "home2", "import", str(pkg))
    assert res.exit_code == 0 and "Nothing was executed" in res.output


def test_cli_errors_are_readable(tmp_path: Path) -> None:
    res = invoke(tmp_path / "nowhere", "challenge", "list")
    assert res.exit_code == 1 and "repoboss init" in res.output
    invoke(tmp_path / "h", "init", "--runner", "local")
    res = invoke(tmp_path / "h", "run", "ch-00000000", "--agent", "nobody")
    assert res.exit_code == 1 and "unknown agent" in res.output
    res = invoke(tmp_path / "h", "run", "ch-00000000", "--agent", "demo-oracle", "--runner", "local")
    assert res.exit_code == 1 and "not found" in res.output


def test_dashboard_pages(demo_repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    invoke(home, "init", "--runner", "local")
    client = TestClient(create_app(Workspace.open(home)))
    page = client.get("/")
    assert page.status_code == 200 and "No challenges yet" in page.text

    out = invoke(home, "challenge", "create", "--repo", str(demo_repo), "--transform", "logic_condition").output
    cid = re.search(r"(ch-[0-9a-f]{8})", out).group(1)
    out = invoke(home, "run", cid, "--agent", "demo-oracle").output
    out += invoke(home, "run", cid, "--agent", "demo-noop").output
    a, b = re.findall(r"run-[0-9a-f]{8}", out)[:2]

    page = client.get("/")
    assert cid in page.text and "DEMO" in page.text and "not isolated" in page.text
    page = client.get(f"/challenges/{cid}")
    assert page.status_code == 200 and "Reveal the injected bug" in page.text and "descriptive" in page.text
    page = client.get(f"/runs/{a}")
    assert page.status_code == 200 and "Demonstration data" in page.text
    assert 'class="add"' in page.text and "does not prove" in page.text
    page = client.get(f"/compare?run={a}&run={b}")
    assert page.status_code == 200 and "Outcome" in page.text
    page = client.get(f"/compare?run={a}")
    assert "Select at least two runs" in page.text
    page = client.get("/runs/run-doesnotexist")
    assert page.status_code == 404 and "does not exist" in page.text
    assert client.get("/static/style.css").status_code == 200


def test_dashboard_escapes_untrusted_text(demo_repo: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    invoke(home, "init", "--runner", "local")
    out = invoke(home, "challenge", "create", "--repo", str(demo_repo), "--transform", "logic_condition").output
    cid = re.search(r"(ch-[0-9a-f]{8})", out).group(1)
    ws = Workspace.open(home)
    ws.update_challenge(cid, {"name": "<script>alert(1)</script>"})
    page = TestClient(create_app(ws)).get("/")
    assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text


@pytest.mark.parametrize(
    "body, message",
    [
        ('test = "pytest -q"', "list of non-empty strings"),
        ('test = ["pytest"]\ninstall = "pip install x"', "install"),
        ('test = ["pytest"]\nprotected = ["../x"]', "relative POSIX glob"),
        ('test = ["pytest"]\nfoo = 1', "unknown keys"),
        ('test = ["pytest"]\ndocker_image = "x; rm -rf /"', "docker_image"),
        ("install = []", "'test' is required"),
    ],
)
def test_invalid_repo_config(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / "repoboss.toml").write_text(f"[repoboss]\n{body}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_repo_config(tmp_path)


def test_config_always_protects_itself(tmp_path: Path) -> None:
    (tmp_path / "repoboss.toml").write_text('[repoboss]\ntest = ["pytest"]\nprotected = ["tests/**"]\n')
    cfg = load_repo_config(tmp_path)
    assert "repoboss.toml" in cfg.protected
    assert RepoConfig.from_dict(cfg.to_dict()) == cfg


@pytest.mark.parametrize(
    "body, message",
    [
        ('[agents.x]\ncommand = "python agent.py"', "argv"),
        ('[agents.x]\ncommand = ["python", "{home}/a.py"]', "placeholder"),
        ('[agents.x]\ncommand = ["a"]\npass_env = ["REPOBOSS_WORKSPACE"]', "REPOBOSS_"),
        ('[agents.demo-oracle]\ncommand = ["a"]', "reserved"),
        ('[agents.x]\ncommand = ["a"]\nagent_dir = "missing-dir"', "not a directory"),
    ],
)
def test_invalid_agent_config(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "agents.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(AgentConfigError, match=re.escape(message)):
        load_agents(path)
