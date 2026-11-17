"""FastAPI app serving a read-only view of the workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from repoboss import __version__
from repoboss.compare import aggregate, comparison_rows, fmt, sample_note
from repoboss.run import TESTS_DISCLAIMER
from repoboss.transforms import Candidate, unified_diff
from repoboss.util import read_text_exact
from repoboss.workspace import Workspace

HERE = Path(__file__).parent
MAX_LOG_CHARS = 200_000


def diff_lines(patch: str | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in (patch or "").splitlines():
        if line.startswith(("+++", "---")):
            cls = "meta"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append((cls, line))
    return out


def _read_log(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_LOG_CHARS:
        return text[:MAX_LOG_CHARS] + f"\n[... {len(text) - MAX_LOG_CHARS} characters not shown ...]"
    return text


def create_app(ws: Workspace) -> FastAPI:
    app = FastAPI(title="RepoBoss", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(version=__version__, fmt=fmt, workspace=str(ws.root))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        return page(request, "error.html", status_code=exc.status_code, code=exc.status_code, message=exc.detail)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        challenges = ws.list_challenges()
        runs = ws.list_runs(limit=50)
        counts: dict[str, int] = {}
        for r in ws.list_runs(limit=100_000):
            counts[r["challenge_id"]] = counts.get(r["challenge_id"], 0) + 1
        return page(request, "index.html", challenges=challenges, runs=runs, run_counts=counts)

    @app.get("/challenges/{challenge_id}", response_class=HTMLResponse)
    def challenge(request: Request, challenge_id: str) -> HTMLResponse:
        row = ws.get_challenge(challenge_id)
        if row is None:
            raise StarletteHTTPException(404, f"Challenge {challenge_id} does not exist in this workspace.")
        cand = Candidate.from_dict(row["candidate"])
        snap_file = ws.snapshot_dir(row["snapshot_hash"]) / "tree" / cand.path
        bug_diff = unified_diff(cand.edit, read_text_exact(snap_file)) if snap_file.is_file() else ""
        runs = ws.list_runs(challenge_id)
        stats = aggregate(runs)
        return page(
            request,
            "challenge.html",
            c=row,
            cand=cand,
            bug_diff=diff_lines(bug_diff),
            runs=runs,
            stats=stats,
            notes={s.agent: sample_note(s.attempts) for s in stats},
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run(request: Request, run_id: str) -> HTMLResponse:
        r = ws.get_run(run_id)
        if r is None:
            raise StarletteHTTPException(404, f"Run {run_id} does not exist in this workspace.")
        rdir = ws.run_dir(run_id)
        return page(
            request,
            "run.html",
            r=r,
            rows=comparison_rows([r]),
            patch=diff_lines(r["patch"]),
            agent_log=_read_log(rdir / "agent.log"),
            eval_log=_read_log(rdir / "eval-out" / "eval.log"),
            prepare_log=_read_log(rdir / "prepare.log"),
            disclaimer=TESTS_DISCLAIMER,
        )

    @app.get("/compare", response_class=HTMLResponse)
    def compare(request: Request, run: list[str] = Query(default=[])) -> HTMLResponse:  # noqa: B008
        found = [ws.get_run(rid) for rid in run]
        runs = [r for r in found if r is not None]
        missing = [rid for rid, r in zip(run, found, strict=True) if r is None]
        rows = comparison_rows(runs) if runs else []
        mixed = len({r["challenge_id"] for r in runs}) > 1
        return page(
            request,
            "compare.html",
            runs=runs,
            rows=rows,
            missing=missing,
            mixed=mixed,
            patches=[(r["id"], diff_lines(r["patch"])) for r in runs],
        )

    return app
