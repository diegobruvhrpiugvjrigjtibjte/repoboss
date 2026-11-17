"""Command line interface."""

from __future__ import annotations

import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from repoboss import __version__
from repoboss.agents import AgentConfigError, load_agents
from repoboss.challenge import ACCEPTED, ChallengeError, create_challenges, verify_challenge
from repoboss.compare import aggregate, comparison_rows, sample_note
from repoboss.config import ConfigError
from repoboss.packaging import PackageError, export_challenge, import_challenge
from repoboss.run import TESTS_DISCLAIMER, RunError, run_challenge
from repoboss.runners import Limits, Runner, docker_available, get_runner
from repoboss.transforms import TRANSFORMS, Candidate, unified_diff
from repoboss.util import read_text_exact
from repoboss.workspace import Workspace, WorkspaceError

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="RepoBoss: verifiable code-repair challenges from your own repositories, in many languages.",
)
challenge_app = typer.Typer(no_args_is_help=True, help="Create, verify and inspect challenges.")
runs_app = typer.Typer(no_args_is_help=True, help="List and inspect runs.")
json_app = typer.Typer(no_args_is_help=True, help="Machine-readable JSON output (used by the desktop apps).")
app.add_typer(challenge_app, name="challenge")
app.add_typer(runs_app, name="runs")
app.add_typer(json_app, name="json")

_state: dict[str, Path | None] = {"home": None}

HomeOpt = Annotated[
    Path | None, typer.Option("--home", help="Workspace directory (default: ./.repoboss or $REPOBOSS_HOME).")
]
RunnerOpt = Annotated[str | None, typer.Option("--runner", help="docker (isolated) or local (NOT isolated).")]


@app.callback()
def _main(home: HomeOpt = None) -> None:
    _state["home"] = home


def _fail(message: str, code: int = 1) -> None:
    typer.secho(f"error: {message}", fg="red", err=True)
    raise typer.Exit(code)


def _ws() -> Workspace:
    try:
        return Workspace.open(_state["home"])
    except WorkspaceError as exc:
        _fail(str(exc))
        raise  # unreachable


def _runner(ws: Workspace, name: str | None) -> Runner:
    settings = ws.settings()
    kind = name or str(settings.get("runner", "docker"))
    try:
        runner = get_runner(kind, ws.envs_dir, Limits.from_settings(settings.get("limits", {})))
    except ValueError as exc:
        _fail(str(exc))
        raise
    if kind == "docker":
        ok, why = runner.available()
        if not ok:
            _fail(
                f"Docker is not available ({why}). Install/start Docker, or pass --runner local "
                "for trusted code only (no isolation)."
            )
    else:
        typer.secho(
            "warning: local runner is NOT isolated: commands run as normal processes on this machine "
            "with your user's permissions and network access.",
            fg="yellow",
            err=True,
        )
    return runner


def _echo_kv(rows: list[tuple[str, Any]]) -> None:
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        typer.echo(f"  {key.ljust(width)}  {value}")


def _table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    typer.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    typer.echo("  ".join("-" * w for w in widths))
    for row in rows:
        typer.echo("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)))


# ---------------------------------------------------------------------------------------------- doctor/init


@app.command()
def doctor() -> None:
    """Check the local toolchain and workspace."""
    checks: list[tuple[str, bool | None, str]] = []
    py_ok = sys.version_info >= (3, 12)
    checks.append(("python", py_ok, f"{platform.python_version()} ({sys.executable})"))
    checks.append(("repoboss", True, __version__))
    checks.append(("sqlite", True, sqlite3.sqlite_version))
    git = shutil.which("git")
    checks.append(("git", git is not None, git or "not found (optional: used to list repository files)"))
    d_ok, d_msg = docker_available()
    checks.append(("docker", d_ok, d_msg if d_ok else f"{d_msg} -> isolated runs unavailable"))
    root = _state["home"] or Workspace.default_root()
    try:
        ws = Workspace.open(_state["home"])
        runner = str(ws.settings().get("runner", "docker"))
        checks.append(("workspace", True, str(ws.root)))
        usable = runner == "local" or d_ok
        checks.append(("default runner", usable, runner + ("" if runner == "docker" else " (NOT isolated)")))
    except WorkspaceError:
        checks.append(("workspace", None, f"not initialised at {root} (run `repoboss init`)"))
    for name, ok, msg in checks:
        mark = {True: "ok  ", False: "FAIL", None: "--  "}[ok]
        color = {True: "green", False: "red", None: "yellow"}[ok]
        typer.secho(f"[{mark}] ", fg=color, nl=False)
        typer.echo(f"{name}: {msg}")
    if not py_ok:
        raise typer.Exit(1)


@app.command()
def init(runner: Annotated[str, typer.Option(help="Default runner: docker or local.")] = "docker") -> None:
    """Create the workspace (database, settings.toml, agents.toml)."""
    if runner not in ("docker", "local"):
        _fail("runner must be 'docker' or 'local'")
    ws, created = Workspace.init(_state["home"], runner=runner)
    typer.echo(f"{'Created' if created else 'Already initialised'}: {ws.root}")
    typer.echo(f"  settings: {ws.settings_path}\n  agents:   {ws.agents_path}")
    if runner == "docker" and not docker_available()[0]:
        typer.secho("Docker is not available here: pass --runner local to try the demo (not isolated).", fg="yellow")


# ---------------------------------------------------------------------------------------------- challenges


@app.command()
def detect(
    repo: Annotated[Path, typer.Option("--repo", exists=True, file_okay=False, resolve_path=True)],
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Write the proposed config to this file.")] = None,
) -> None:
    """Recognise the project type and propose a repoboss.toml (nothing is written into the repo)."""
    from repoboss.config import render_config
    from repoboss.detect import detect as run_detect

    found = run_detect(repo)
    if not found:
        _fail(
            "no known project type found (looked for package.json, Cargo.toml, go.mod, pom.xml, build.gradle, "
            "*.csproj, Gemfile, composer.json, mix.exs, pubspec.yaml, CMakeLists.txt, pyproject.toml, ...)"
        )
        return
    best = found[0]
    text = render_config(best.config, f"Proposed by `repoboss detect` ({best.ecosystem}). Review before use.")
    if len(found) > 1:
        typer.echo(f"Also matched: {', '.join(d.ecosystem for d in found[1:])}")
    for note in best.notes:
        typer.secho(f"note: {note}", fg="yellow")
    if out:
        if out.exists():
            _fail(f"{out} already exists; not overwriting")
        out.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}. Use it with: repoboss challenge create --repo {repo} --config {out}")
    else:
        typer.echo(text)


@app.command()
def languages(
    download: Annotated[bool, typer.Option("--download", help="Download the grammars now (needs network).")] = False,
) -> None:
    """Languages supported for bug injection, and whether their grammars are available offline."""
    from repoboss import polyglot

    langs = sorted(set(polyglot.LANG_BY_EXT.values()))
    typer.echo("python: built in (ast)")
    for lang in langs:
        exts = ", ".join(sorted(e for e, v in polyglot.LANG_BY_EXT.items() if v == lang))
        if download:
            ok, msg = polyglot.parser_available(lang)
        else:
            try:
                import tree_sitter_language_pack as tslp

                ok = lang in tslp.downloaded_languages()
                msg = "cached" if ok else "not downloaded yet (use --download, or it downloads on first use)"
            except ImportError as exc:
                ok, msg = False, str(exc)
        typer.secho(f"{lang}: ", fg="green" if ok else "yellow", nl=False)
        typer.echo(f"{msg}  [{exts}]")


@challenge_app.command("create")
def challenge_create(
    repo: Annotated[
        Path,
        typer.Option("--repo", help="Repository to snapshot.", exists=True, file_okay=False, resolve_path=True),
    ],
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="repoboss.toml to use (default: REPO/repoboss.toml, else auto-detect)."),
    ] = None,
    transform: Annotated[
        list[str] | None,
        typer.Option("--transform", "-t", help=f"Repeatable. One of: {', '.join(TRANSFORMS)}. Default: all."),
    ] = None,
    seed: Annotated[int, typer.Option(help="Seed for candidate order and mutated values.")] = 0,
    max_candidates: Annotated[int, typer.Option(help="Candidates tried per transformation.")] = 8,
    per_transform: Annotated[int, typer.Option(help="Challenges to create per transformation.")] = 1,
    runner: RunnerOpt = None,
) -> None:
    """Snapshot a repository and create verified challenges."""
    from repoboss.config import CONFIG_FILE, load_repo_config, render_config
    from repoboss.detect import detect as run_detect

    ws = _ws()
    run = _runner(ws, runner)
    try:
        if config_file is not None or (repo / CONFIG_FILE).is_file():
            cfg = load_repo_config(repo, config_file)
        else:
            found = run_detect(repo)
            if not found:
                _fail(
                    f"{repo} has no repoboss.toml and its project type was not recognised; "
                    "write a config and pass --config"
                )
                return
            cfg = found[0].config
            saved = ws.root / "configs" / f"{cfg.name}.toml"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(render_config(cfg, f"Auto-detected ({found[0].ecosystem})."), encoding="utf-8")
            typer.secho(
                f"No repoboss.toml: using an auto-detected {found[0].ecosystem} configuration "
                f"(saved to {saved}; edit it and pass --config to change it).",
                fg="yellow",
            )
            for note in found[0].notes:
                typer.secho(f"note: {note}", fg="yellow")
        snapshot, results = create_challenges(
            ws,
            repo,
            run,
            transforms=transform or list(TRANSFORMS),
            seed=seed,
            max_candidates=max_candidates,
            per_transform=per_transform,
            config=cfg,
        )
    except (ConfigError, ChallengeError, ValueError) as exc:
        _fail(str(exc))
        return
    typer.echo(f"snapshot {snapshot.hash[:16]}  ({len(snapshot.files)} files; original repository untouched)")
    for path, reason in snapshot.skipped[:20]:
        typer.echo(f"  skipped {path}: {reason}")
    accepted = 0
    for res in results:
        v = res.verification
        for warning in v.warnings[:5]:
            typer.secho(f"  note: {warning}", fg="yellow")
        if res.challenge_id:
            accepted += 1
            typer.secho(f"[accepted] {res.challenge_id}  {res.transform}: ", fg="green", nl=False)
            typer.echo(
                f"{v.check.candidate.path if v.check else ''}  "
                f"({len(v.check.broken.failing) if v.check and v.check.broken else 0} failing tests)"
            )
        else:
            typer.secho(f"[rejected] {res.transform}: {v.status}", fg="red", nl=False)
            typer.echo(f" - {v.detail}")
            for rej in v.rejected:
                typer.echo(f"    {rej.candidate.path}:{rej.candidate.line} -> {rej.status}")
            if v.original is not None and v.status != "not_applicable" and v.original.status != "passed":
                typer.echo("    original test output (tail):")
                for line in v.original.output_tail.splitlines()[-15:]:
                    typer.echo(f"      {line}")
            if v.prepare_log and v.status not in ("not_applicable", "no_valid_candidate"):
                typer.echo("    preparation log (tail):")
                for line in v.prepare_log.splitlines()[-15:]:
                    typer.echo(f"      {line}")
    if accepted == 0:
        raise typer.Exit(1)


@challenge_app.command("verify")
def challenge_verify(
    challenge_id: str,
    trust: Annotated[bool, typer.Option("--trust", help="Allow executing an imported challenge.")] = False,
    runner: RunnerOpt = None,
) -> None:
    """Re-run original / buggy / restored checks for a challenge."""
    ws = _ws()
    run = _runner(ws, runner)
    try:
        v = verify_challenge(ws, challenge_id, run, trust=trust)
    except ChallengeError as exc:
        _fail(str(exc))
        return
    ok = v.status == ACCEPTED
    typer.secho(f"{challenge_id}: {v.status}", fg="green" if ok else "red")
    typer.echo(f"  {v.detail}")
    for label, rep in (
        ("original", v.original),
        ("broken", v.check.broken if v.check else None),
        ("restored", v.check.restored if v.check else None),
    ):
        if rep is not None:
            typer.echo(
                f"  {label:9} {rep.status:18} passed={rep.passed} failed={rep.failed} "
                f"errors={rep.errors} ({rep.duration_s:.1f}s)"
            )
    if not ok:
        raise typer.Exit(1)


@challenge_app.command("list")
def challenge_list() -> None:
    """List challenges."""
    ws = _ws()
    rows = ws.list_challenges()
    if not rows:
        typer.echo("No challenges yet. Create one with `repoboss challenge create --repo PATH`.")
        return
    _table(
        ["ID", "NAME", "STATUS", "TRUSTED", "RUNNER", "CREATED"],
        [
            [r["id"], r["name"], r["status"], "yes" if r["trusted"] else "NO", r["runner"] or "-", r["created_at"]]
            for r in rows
        ],
    )


@challenge_app.command("show")
def challenge_show(
    challenge_id: str,
    reveal: Annotated[bool, typer.Option("--reveal", help="Also print the injected bug as a diff.")] = False,
) -> None:
    """Show a challenge's configuration, environment and verification."""
    ws = _ws()
    row = ws.get_challenge(challenge_id)
    if row is None:
        _fail(f"challenge {challenge_id} not found")
        return
    cand = Candidate.from_dict(row["candidate"])
    env = row["env"] or {}
    _echo_kv(
        [
            ("id", row["id"]),
            ("name", row["name"]),
            ("status", f"{row['status']} - {row['status_detail']}"),
            ("trusted", "yes" if row["trusted"] else "NO (imported, never executed here)"),
            ("transform", row["transform"]),
            ("seed", row["seed"]),
            ("bug location", f"{cand.path}:{cand.line}" if reveal else f"{cand.path} (use --reveal for details)"),
            ("snapshot", row["snapshot_hash"]),
            ("runner", row["runner"] or "-"),
            ("environment", env.get("image") or env.get("python") or "-"),
            ("image id", env.get("image_id") or "n/a"),
            ("base image", f"{env.get('base_image') or 'n/a'} {env.get('base_digest') or ''}".strip()),
            ("install", row["config"]["install"]),
            ("test", row["config"]["test"]),
            ("hidden", row["config"].get("hidden", "-")),
            ("protected", row["config"]["protected"]),
            ("editable", row["config"]["editable"]),
            ("source repo", row["source_repo"] or "-"),
            ("imported from", row["imported_from"] or "-"),
        ]
    )
    if reveal:
        typer.echo(f"\n{cand.description}")
        snap = ws.snapshot_dir(row["snapshot_hash"]) / "tree" / cand.path
        typer.echo(unified_diff(cand.edit, read_text_exact(snap)))


# ---------------------------------------------------------------------------------------------- runs


@app.command("run")
def run_cmd(
    challenge_id: str,
    agent: Annotated[str, typer.Option("--agent", "-a", help="demo-oracle, demo-noop or a name from agents.toml.")],
    attempts: Annotated[int, typer.Option(help="Independent attempts, each from a clean copy.")] = 1,
    runner: RunnerOpt = None,
    network: Annotated[
        bool | None,
        typer.Option("--network/--no-network", help="Network during the attempt (default: agent setting, off)."),
    ] = None,
    timeout: Annotated[float | None, typer.Option(help="Agent time limit in seconds.")] = None,
    keep_workspace: Annotated[bool, typer.Option(help="Keep working copies for inspection.")] = False,
) -> None:
    """Run an agent on a challenge and evaluate the result with the protected tests."""
    ws = _ws()
    try:
        specs = load_agents(ws.agents_path)
    except AgentConfigError as exc:
        _fail(str(exc))
        return
    if agent not in specs:
        _fail(
            f"unknown agent {agent!r}. Available: {', '.join(sorted(specs))}. "
            f"Custom agents are defined in {ws.agents_path}."
        )
    spec = specs[agent]
    run = _runner(ws, runner)
    if spec.is_demo:
        typer.secho(
            f"note: {spec.name} is a DEMO adapter. It verifies the pipeline and does not measure any model.",
            fg="yellow",
        )
    try:
        results = run_challenge(
            ws,
            challenge_id,
            spec,
            run,
            attempts=attempts,
            network=network,
            timeout_s=timeout,
            keep_workspaces=keep_workspace,
        )
    except RunError as exc:
        _fail(str(exc))
        return
    for res in results:
        rep = res.report
        tests = f"passed={rep.passed} failed={rep.failed} errors={rep.errors}" if rep else "not evaluated"
        color = "green" if res.outcome == "passed" else "red"
        typer.secho(f"{res.run_id}  {res.outcome:18}", fg=color, nl=False)
        typer.echo(f" attempt={res.attempt_status}  {tests}")
        if res.changes and res.changes.violations:
            for v in res.changes.violations:
                typer.echo(f"    violation: {v}")
    passed = sum(1 for r in results if r.outcome == "passed")
    typer.echo(f"{passed}/{len(results)} attempt(s) passed the protected tests.")
    note = sample_note(len(results))
    if note:
        typer.echo(note)
    if passed:
        typer.echo(TESTS_DISCLAIMER)


@runs_app.command("list")
def runs_list(challenge: Annotated[str | None, typer.Option(help="Filter by challenge id.")] = None) -> None:
    """List runs (most recent first)."""
    ws = _ws()
    rows = ws.list_runs(challenge)
    if not rows:
        typer.echo("No runs yet.")
        return
    _table(
        ["RUN", "CHALLENGE", "AGENT", "DEMO", "RUNNER", "OUTCOME", "TESTS P/F/E", "SECONDS"],
        [
            [
                r["id"],
                r["challenge_id"],
                r["agent_name"],
                "yes" if r["agent_is_demo"] else "no",
                r["runner"],
                r["outcome"] or "-",
                f"{r['tests_passed']}/{r['tests_failed']}/{r['tests_errors']}",
                f"{r['total_duration_s']:.1f}" if r["total_duration_s"] is not None else "n/a",
            ]
            for r in rows
        ],
    )


@runs_app.command("show")
def runs_show(
    run_id: str,
    patch: Annotated[bool, typer.Option(help="Print the final patch.")] = False,
    log: Annotated[bool, typer.Option(help="Print the agent log.")] = False,
) -> None:
    """Show one run."""
    ws = _ws()
    r = ws.get_run(run_id)
    if r is None:
        _fail(f"run {run_id} not found")
        return
    for label, values in comparison_rows([r]):
        typer.echo(f"  {label.ljust(24)} {values[0]}")
    if r["violations"]:
        typer.echo("  violations: " + "; ".join(r["violations"]))
    if r["ignored"]:
        typer.echo("  ignored (outside editable paths): " + ", ".join(r["ignored"]))
    if r["notes"]:
        typer.echo("  notes:\n    " + r["notes"].replace("\n", "\n    "))
    if patch:
        typer.echo("\n" + (r["patch"] or "(empty patch)"))
    if log:
        path = ws.run_dir(run_id) / "agent.log"
        typer.echo("\n" + (path.read_text(encoding="utf-8") if path.exists() else "(no log)"))


@app.command()
def compare(
    run_ids: Annotated[list[str] | None, typer.Argument(help="Two or more run ids.")] = None,
    challenge: Annotated[str | None, typer.Option(help="Aggregate all runs of a challenge by agent.")] = None,
) -> None:
    """Compare runs side by side, or aggregate a challenge's runs by agent."""
    ws = _ws()
    if challenge:
        stats = aggregate(ws.list_runs(challenge))
        if not stats:
            typer.echo("No finished runs for this challenge.")
            return
        _table(
            ["AGENT", "VERSION", "RUNNER", "DEMO", "ATTEMPTS", "PASSED", "RATE", "MEDIAN S"],
            [
                [
                    s.agent,
                    s.version or "n/a",
                    s.runner,
                    "yes" if s.is_demo else "no",
                    str(s.attempts),
                    str(s.passed),
                    f"{s.success_rate:.0%}",
                    f"{s.median_duration_s:.1f}" if s.median_duration_s is not None else "n/a",
                ]
                for s in stats
            ],
        )
        for s in stats:
            note = sample_note(s.attempts)
            if note:
                typer.echo(f"{s.agent}: {note}")
        typer.echo("Results apply to this challenge and environment only; they are not a general ranking.")
        return
    if not run_ids or len(run_ids) < 2:
        _fail("give at least two run ids, or --challenge ID")
        return
    runs: list[dict[str, Any]] = []
    for rid in run_ids:
        r = ws.get_run(rid)
        if r is None:
            _fail(f"run {rid} not found")
            return
        runs.append(r)
    challenges = {r["challenge_id"] for r in runs}
    if len(challenges) > 1:
        typer.secho("warning: these runs belong to different challenges.", fg="yellow")
    _table(["", *run_ids], [[label, *values] for label, values in comparison_rows(runs)])


# ---------------------------------------------------------------------------------------------- packages


@app.command("export")
def export_cmd(
    challenge_id: str,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Destination .zip")] = None,
) -> None:
    """Export a challenge as an inspectable zip package."""
    ws = _ws()
    dest = out or Path.cwd() / f"{challenge_id}.zip"
    try:
        res = export_challenge(ws, challenge_id, dest)
    except PackageError as exc:
        _fail(str(exc))
        return
    typer.echo(f"Wrote {res.path}")
    typer.echo(f"Included ({len(res.included)}):")
    for name in res.included:
        typer.echo(f"  {name}")
    typer.secho(
        "The manifest contains the injected edit (the reference fix): keep the package away from the agent under test.",
        fg="yellow",
    )


@app.command("import")
def import_cmd(package: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Validate and import a challenge package. Nothing is executed."""
    ws = _ws()
    try:
        res = import_challenge(ws, package)
    except PackageError as exc:
        _fail(f"package rejected: {exc}")
        return
    typer.echo(f"Imported as {res.challenge_id} ({len(res.files)} files). Status: imported_unverified.")
    typer.echo("Nothing was executed. Review it with:")
    typer.echo(f"  repoboss challenge show {res.challenge_id} --reveal")
    typer.echo("and only then run its commands explicitly:")
    typer.echo(f"  repoboss challenge verify {res.challenge_id} --trust")


@app.command()
def dashboard(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8765,
) -> None:
    """Serve the local dashboard."""
    ws = _ws()
    import uvicorn

    from repoboss.dashboard.app import create_app

    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(
            "warning: the dashboard has no authentication; binding beyond localhost exposes logs and patches.",
            fg="yellow",
        )
    typer.echo(f"RepoBoss dashboard on http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(ws), host=host, port=port, log_level="warning")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def gui(page: Annotated[str | None, typer.Option(hidden=True, help="Page to open first.")] = None) -> None:
    """Open the desktop interface (Tkinter; Windows, macOS and Linux)."""
    try:
        from repoboss.gui import main as gui_main
    except ImportError as exc:  # tkinter missing (e.g. Linux without python3-tk)
        _fail(f"Tkinter is not available: {exc}. On Debian/Ubuntu: sudo apt install python3-tk")
        return
    gui_main(_state["home"], page)


@app.command()
def interactive() -> None:
    """Guided menus in the terminal (no flags to remember)."""
    from repoboss.interactive import main as interactive_main

    interactive_main(_state["home"])


# ---------------------------------------------------------------------------------------------- json


def _emit(data: Any) -> None:
    import json

    typer.echo(json.dumps(data, default=str, indent=1))


def _challenge_public(row: dict[str, Any], reveal: bool = False) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k != "candidate"}
    cand = row["candidate"]
    out["bug_path"] = cand["path"]
    out["language"] = (cand.get("metadata") or {}).get("language", "python" if cand["path"].endswith(".py") else None)
    if reveal:
        out["bug"] = {k: cand[k] for k in ("description", "line", "metadata")}
    return out


@json_app.command("status")
def json_status() -> None:
    """Workspace, runner and version."""
    root = _state["home"] or Workspace.default_root()
    d_ok, d_msg = docker_available()
    data: dict[str, Any] = {
        "version": __version__,
        "workspace": str(root),
        "initialised": False,
        "docker": {"available": d_ok, "detail": d_msg},
    }
    try:
        ws = Workspace.open(_state["home"])
        data.update(
            initialised=True,
            workspace=str(ws.root),
            runner=ws.settings().get("runner", "docker"),
            agents_file=str(ws.agents_path),
        )
    except WorkspaceError:
        pass
    _emit(data)


@json_app.command("challenges")
def json_challenges() -> None:
    """All challenges."""
    _emit([_challenge_public(r) for r in _ws().list_challenges()])


@json_app.command("challenge")
def json_challenge(challenge_id: str, reveal: bool = False) -> None:
    """One challenge (with --reveal: the injected bug)."""
    row = _ws().get_challenge(challenge_id)
    if row is None:
        _fail(f"challenge {challenge_id} not found")
        return
    _emit(_challenge_public(row, reveal))


@json_app.command("runs")
def json_runs(challenge: Annotated[str | None, typer.Option()] = None) -> None:
    """Runs, most recent first (patch omitted)."""
    _emit([{k: v for k, v in r.items() if k != "patch"} for r in _ws().list_runs(challenge)])


@json_app.command("run")
def json_run(run_id: str) -> None:
    """One run, with patch and agent log."""
    ws = _ws()
    r = ws.get_run(run_id)
    if r is None:
        _fail(f"run {run_id} not found")
        return
    log = ws.run_dir(run_id) / "agent.log"
    r["agent_log"] = log.read_text(encoding="utf-8", errors="replace")[-200_000:] if log.exists() else None
    _emit(r)


@json_app.command("agents")
def json_agents() -> None:
    """Configured agents (names, kinds, options; never secrets)."""
    ws = _ws()
    try:
        specs = load_agents(ws.agents_path)
    except AgentConfigError as exc:
        _fail(str(exc))
        return
    _emit([s.public() | {"description": s.description} for s in specs.values()])


@json_app.command("stats")
def json_stats(challenge: Annotated[str | None, typer.Option()] = None) -> None:
    """Per-agent aggregates (attempts, passed, rate, median duration)."""
    stats = aggregate(_ws().list_runs(challenge, limit=100_000))
    _emit([{**vars(s), "success_rate": s.success_rate, "note": sample_note(s.attempts)} for s in stats])


if __name__ == "__main__":
    app()
