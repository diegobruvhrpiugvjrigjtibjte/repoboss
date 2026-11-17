"""The JSON API used by the desktop apps, the Tkinter app and the interactive terminal menu."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoboss.cli import app

cli = CliRunner()


def invoke(home: Path, *args: str):
    return cli.invoke(app, ["--home", str(home), *args])


@pytest.fixture
def populated(demo_repo: Path, tmp_path: Path) -> tuple[Path, str]:
    home = tmp_path / "home"
    invoke(home, "init", "--runner", "local")
    out = invoke(home, "challenge", "create", "--repo", str(demo_repo), "--transform", "logic_condition").output
    cid = re.search(r"(ch-[0-9a-f]{8})", out).group(1)
    invoke(home, "run", cid, "--agent", "demo-oracle")
    return home, cid


def test_json_api(populated: tuple[Path, str]) -> None:
    home, cid = populated
    status = json.loads(invoke(home, "json", "status").output)
    assert status["initialised"] is True and status["runner"] == "local"
    challenges = json.loads(invoke(home, "json", "challenges").output)
    assert [c["id"] for c in challenges] == [cid]
    assert challenges[0]["language"] == "python" and "candidate" not in challenges[0]  # bug not exposed by default
    revealed = json.loads(invoke(home, "json", "challenge", cid, "--reveal").output)
    assert revealed["bug"]["description"]
    runs = json.loads(invoke(home, "json", "runs").output)
    assert len(runs) == 1 and "patch" not in runs[0]
    run = json.loads(invoke(home, "json", "run", runs[0]["id"]).output)
    assert run["patch"] and "DEMO ORACLE" in run["agent_log"]
    agents = json.loads(invoke(home, "json", "agents").output)
    assert {a["name"] for a in agents} >= {"demo-oracle", "demo-noop"}
    stats = json.loads(invoke(home, "json", "stats").output)
    assert stats[0]["agent"] == "demo-oracle" and stats[0]["success_rate"] == 1.0 and stats[0]["note"]


def test_json_errors_exit_non_zero(tmp_path: Path) -> None:
    res = invoke(tmp_path / "missing", "json", "challenges")
    assert res.exit_code == 1


def test_tk_app_renders_every_page(populated: tuple[Path, str]) -> None:
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    from repoboss.gui import App

    home, _ = populated
    try:
        app_ = App(root, home)
        for page in app_.pages:
            app_.show(page)
            root.update()
            assert app_.title.cget("text") == page
    finally:
        root.destroy()


def test_interactive_menu_lists_and_quits(
    populated: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from repoboss import interactive

    home, cid = populated
    answers = iter(["2", "5", "1", "0"])  # list challenges, compare (pick #1), quit
    monkeypatch.setattr(interactive.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(interactive.IntPrompt, "ask", staticmethod(lambda *a, **k: int(next(answers))))
    interactive.main(home)
    out = capsys.readouterr().out
    assert cid in out and "not a general ranking" in out
