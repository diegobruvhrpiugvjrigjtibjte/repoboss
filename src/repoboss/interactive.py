"""Guided terminal interface: menus on top of the regular CLI commands (same code paths)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from repoboss import __version__
from repoboss.agents import AgentConfigError, load_agents
from repoboss.transforms import TRANSFORMS
from repoboss.workspace import Workspace, WorkspaceError

console = Console()


def _invoke(home: Path | None, *args: str) -> None:
    """Run a CLI command in-process; errors are printed, never fatal for the menu."""
    from typer.main import get_command

    from repoboss.cli import app

    argv = (["--home", str(home)] if home else []) + list(args)
    try:
        get_command(app).main(argv, standalone_mode=False)
    except click.exceptions.Exit:
        pass
    except click.ClickException as exc:
        console.print(f"[red]{exc.format_message()}[/red]")
    except SystemExit:
        pass


def _ws(home: Path | None) -> Workspace | None:
    try:
        return Workspace.open(home)
    except WorkspaceError:
        return None


def _pick(title: str, rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> dict[str, Any] | None:
    if not rows:
        console.print(f"[yellow]No {title.lower()} yet.[/yellow]")
        return None
    table = Table(title=title, header_style="bold cyan")
    table.add_column("#", justify="right")
    for _key, label in columns:
        table.add_column(label)
    for i, row in enumerate(rows, 1):
        table.add_row(str(i), *[str(row.get(key) if row.get(key) is not None else "n/a") for key, _ in columns])
    console.print(table)
    choice = IntPrompt.ask("Number (0 = back)", default=0)
    if choice < 1 or choice > len(rows):
        return None
    return rows[choice - 1]


def _header(home: Path | None) -> None:
    ws = _ws(home)
    if ws is None:
        where = f"[yellow]no workspace at {home or Workspace.default_root()}[/yellow] (choose 9 to create it)"
    else:
        where = f"workspace [bold]{ws.root}[/bold] · default runner [bold]{ws.settings().get('runner')}[/bold]"
    console.print(Panel.fit(f"[bold magenta]RepoBoss {__version__}[/bold magenta]\n{where}", border_style="magenta"))


MENU = [
    ("1", "Create challenges from a repository"),
    ("2", "List challenges"),
    ("3", "Run an agent on a challenge"),
    ("4", "Runs: list and inspect"),
    ("5", "Compare agents on a challenge"),
    ("6", "Re-verify a challenge"),
    ("7", "Agents: show configured agents"),
    ("8", "Open the web dashboard"),
    ("9", "Create the workspace here"),
    ("0", "Quit"),
]


def _runner_choice(ws: Workspace) -> str:
    default = str(ws.settings().get("runner", "docker"))
    return Prompt.ask("Runner (docker = isolated, local = NOT isolated)", choices=["docker", "local"], default=default)


def main(home: Path | None = None) -> None:
    while True:
        console.print()
        _header(home)
        for key, label in MENU:
            console.print(f"  [bold cyan]{key}[/bold cyan]  {label}")
        choice = Prompt.ask("Choose", choices=[k for k, _ in MENU], default="0")
        if choice == "0":
            return
        if choice == "9":
            runner = Prompt.ask("Default runner", choices=["docker", "local"], default="docker")
            _invoke(home, "init", "--runner", runner)
            continue
        ws = _ws(home)
        if ws is None:
            console.print("[red]Create the workspace first (option 9).[/red]")
            continue
        if choice == "1":
            repo = Prompt.ask("Repository folder").strip().strip('"')
            if not repo:
                continue
            _invoke(home, "detect", "--repo", repo)
            transforms = Prompt.ask(
                f"Bug types (comma separated: {', '.join(TRANSFORMS)})", default=",".join(TRANSFORMS)
            )
            per = IntPrompt.ask("Challenges per bug type", default=1)
            seed = IntPrompt.ask("Seed", default=0)
            args = [
                "challenge",
                "create",
                "--repo",
                repo,
                "--seed",
                str(seed),
                "--per-transform",
                str(per),
                "--runner",
                _runner_choice(ws),
            ]
            for name in [t.strip() for t in transforms.split(",") if t.strip()]:
                args += ["--transform", name]
            with console.status("Creating and verifying challenges (this runs the test suite several times)..."):
                _invoke(home, *args)
        elif choice == "2":
            _invoke(home, "challenge", "list")
        elif choice == "3":
            ch = _pick("Challenges", ws.list_challenges(), [("id", "ID"), ("name", "Name"), ("status", "Status")])
            if ch is None:
                continue
            try:
                specs = load_agents(ws.agents_path)
            except AgentConfigError as exc:
                console.print(f"[red]{exc}[/red]")
                continue
            names = list(specs)
            for i, name in enumerate(names, 1):
                tag = " [magenta](DEMO)[/magenta]" if specs[name].is_demo else ""
                console.print(f"  {i}. {name}{tag} — {specs[name].description}")
            pick = IntPrompt.ask("Agent number", default=1)
            if not 1 <= pick <= len(names):
                continue
            attempts = IntPrompt.ask("Attempts", default=1)
            with console.status(f"Running {names[pick - 1]} ..."):
                _invoke(
                    home,
                    "run",
                    ch["id"],
                    "--agent",
                    names[pick - 1],
                    "--attempts",
                    str(attempts),
                    "--runner",
                    _runner_choice(ws),
                )
        elif choice == "4":
            run = _pick(
                "Runs",
                ws.list_runs(limit=40),
                [
                    ("id", "Run"),
                    ("challenge_id", "Challenge"),
                    ("agent_name", "Agent"),
                    ("outcome", "Outcome"),
                    ("total_duration_s", "Seconds"),
                ],
            )
            if run is not None:
                _invoke(home, "runs", "show", run["id"])
                if Confirm.ask("Show the patch?", default=True):
                    console.print(run.get("patch") or "(empty patch)", markup=False, highlight=False)
                if Confirm.ask("Show the agent log?", default=False):
                    _invoke(home, "runs", "show", run["id"], "--log")
        elif choice == "5":
            ch = _pick("Challenges", ws.list_challenges(), [("id", "ID"), ("name", "Name")])
            if ch is not None:
                _invoke(home, "compare", "--challenge", ch["id"])
        elif choice == "6":
            ch = _pick("Challenges", ws.list_challenges(), [("id", "ID"), ("name", "Name"), ("status", "Status")])
            if ch is not None:
                args = ["challenge", "verify", ch["id"], "--runner", _runner_choice(ws)]
                if not ch["trusted"] and Confirm.ask("Imported challenge: execute its commands?", default=False):
                    args.append("--trust")
                with console.status("Verifying..."):
                    _invoke(home, *args)
        elif choice == "7":
            console.print(f"Agents are configured in [bold]{ws.agents_path}[/bold] (see docs/agents.md).")
            _invoke(home, "json", "agents")
        elif choice == "8":
            console.print("Dashboard on http://127.0.0.1:8765 — press Ctrl+C to come back.")
            with contextlib.suppress(KeyboardInterrupt):
                _invoke(home, "dashboard")
