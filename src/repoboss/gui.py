"""Cross-platform desktop interface (Tkinter, standard library only).

Lists and details are read straight from the workspace; long actions (create, verify, run) execute
the regular CLI in a subprocess, streamed into the log panel, so every interface behaves the same.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from repoboss import __version__
from repoboss.agents import AgentConfigError, load_agents
from repoboss.compare import aggregate, comparison_rows, fmt, sample_note
from repoboss.transforms import TRANSFORMS
from repoboss.workspace import Workspace, WorkspaceError

BG = "#0f1216"
PANEL = "#161b21"
PANEL2 = "#1c232b"
LINE = "#2a333d"
TEXT = "#e3e8ee"
MUTED = "#94a1b0"
ACCENT = "#7cb7ff"
OK = "#4fd18b"
BAD = "#ff7b72"
WARN = "#f2c14e"
DEMO = "#c39bff"
MONO = ("Consolas", 10) if sys.platform == "win32" else ("Menlo", 11) if sys.platform == "darwin" else ("Monospace", 10)
SANS = ("Segoe UI", 10) if sys.platform == "win32" else ("Helvetica", 11)


def _style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL2, font=SANS, bordercolor=LINE)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Side.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
    style.configure("Title.TLabel", background=BG, foreground=TEXT, font=(SANS[0], 16, "bold"))
    style.configure("Brand.TLabel", background=PANEL, foreground=TEXT, font=(SANS[0], 15, "bold"))
    style.configure("CardValue.TLabel", background=PANEL, foreground=ACCENT, font=(SANS[0], 22, "bold"))
    style.configure("CardLabel.TLabel", background=PANEL, foreground=MUTED)
    style.configure("TButton", background=PANEL2, foreground=TEXT, bordercolor=LINE, padding=(12, 6), relief="flat")
    style.map("TButton", background=[("active", "#26303a")], bordercolor=[("active", ACCENT)])
    style.configure("Accent.TButton", background="#2b5d9a", foreground="#ffffff", padding=(14, 7))
    style.map("Accent.TButton", background=[("active", "#3570b7")])
    style.configure(
        "Nav.TButton", background=PANEL, foreground=MUTED, anchor="w", padding=(16, 9), relief="flat", borderwidth=0
    )
    style.map("Nav.TButton", background=[("active", PANEL2)], foreground=[("active", TEXT)])
    style.configure(
        "NavActive.TButton",
        background=PANEL2,
        foreground=TEXT,
        anchor="w",
        padding=(16, 9),
        relief="flat",
        borderwidth=0,
    )
    style.configure(
        "Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=26, bordercolor=LINE, font=SANS
    )
    style.configure("Treeview.Heading", background=PANEL2, foreground=MUTED, relief="flat", font=(SANS[0], 9, "bold"))
    style.map("Treeview", background=[("selected", "#2b5d9a")], foreground=[("selected", "#ffffff")])
    style.configure("TCheckbutton", background=BG, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=TEXT, arrowcolor=TEXT)
    style.configure("TSpinbox", fieldbackground=PANEL2, foreground=TEXT, arrowcolor=TEXT)
    style.configure("TEntry", fieldbackground=PANEL2, foreground=TEXT, insertcolor=TEXT)
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL2, bordercolor=LINE)
    root.option_add("*TCombobox*Listbox.background", PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)


def _text(parent: tk.Misc, height: int = 10) -> tk.Text:
    widget = tk.Text(
        parent,
        height=height,
        bg="#0b0e11",
        fg="#cfd6de",
        insertbackground=TEXT,
        relief="flat",
        font=MONO,
        wrap="none",
        highlightthickness=1,
        highlightbackground=LINE,
        padx=8,
        pady=6,
    )
    widget.tag_configure("add", foreground="#b9f3cf", background="#14301f")
    widget.tag_configure("del", foreground="#ffc9c4", background="#3a1a18")
    widget.tag_configure("hunk", foreground=ACCENT)
    widget.tag_configure("muted", foreground=MUTED)
    widget.tag_configure("ok", foreground=OK)
    widget.tag_configure("bad", foreground=BAD)
    widget.tag_configure("warn", foreground=WARN)
    widget.tag_configure("demo", foreground=DEMO)
    widget.tag_configure("title", foreground=TEXT, font=(MONO[0], MONO[1] + 1, "bold"))
    return widget


def _tree(parent: tk.Misc, columns: Sequence[tuple[str, str, int]]) -> ttk.Treeview:
    tree = ttk.Treeview(parent, columns=[c[0] for c in columns], show="headings", selectmode="browse")
    for key, label, width in columns:
        tree.heading(key, text=label)
        tree.column(key, width=width, anchor="w")
    tree.tag_configure("passed", foreground=OK)
    tree.tag_configure("failed", foreground=BAD)
    tree.tag_configure("rejected", foreground=WARN)
    tree.tag_configure("demo", foreground=DEMO)
    return tree


def _put(widget: tk.Text, text: str, tag: str | None = None) -> None:
    widget.insert("end", text, tag or ())


def _put_diff(widget: tk.Text, patch: str) -> None:
    for line in patch.splitlines():
        tag = None
        if line.startswith(("+++", "---")):
            tag = "muted"
        elif line.startswith("@@"):
            tag = "hunk"
        elif line.startswith("+"):
            tag = "add"
        elif line.startswith("-"):
            tag = "del"
        _put(widget, line + "\n", tag)


class App:
    def __init__(self, root: tk.Tk, home: Path | None) -> None:
        self.root = root
        self.home = (home or Workspace.default_root()).resolve()
        self.proc: subprocess.Popen[str] | None = None
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.current = "Overview"
        self.preselect = ""
        self._done: Callable[[], None] | None = None
        root.title(f"RepoBoss {__version__}")
        root.geometry("1280x820")
        root.minsize(980, 640)
        _style(root)

        side = ttk.Frame(root, style="Side.TFrame", width=210)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        ttk.Label(side, text="RepoBoss", style="Brand.TLabel").pack(anchor="w", padx=18, pady=(18, 2))
        ttk.Label(side, text=f"v{__version__} · desktop", style="Panel.TLabel", foreground=MUTED).pack(
            anchor="w", padx=18, pady=(0, 16)
        )
        self.nav: dict[str, ttk.Button] = {}
        main = ttk.Frame(root)
        main.pack(side="left", fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x", padx=20, pady=(16, 8))
        self.title = ttk.Label(top, text="", style="Title.TLabel")
        self.title.pack(side="left")
        self.ws_label = ttk.Label(top, text="", style="Muted.TLabel")
        self.ws_label.pack(side="right")

        self.body = ttk.Frame(main)
        self.body.pack(fill="both", expand=True, padx=20)

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", padx=20, pady=(8, 16))
        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        ttk.Label(bar, text="Activity", style="Muted.TLabel").pack(side="left")
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=180)
        self.progress.pack(side="right", padx=10)
        self.log = _text(bottom, height=9)
        self.log.pack(fill="x", pady=(6, 0))

        self.pages: dict[str, Callable[[], None]] = {
            "Overview": self.page_overview,
            "Challenges": self.page_challenges,
            "New challenge": self.page_new,
            "Run agent": self.page_run,
            "Runs": self.page_runs,
            "Compare": self.page_compare,
            "Settings": self.page_settings,
        }
        for name in self.pages:
            btn = ttk.Button(side, text=name, style="Nav.TButton", command=self._nav_command(name))
            btn.pack(fill="x")
            self.nav[name] = btn
        ttk.Label(
            side,
            text="Demo adapters verify the\npipeline, not a model.",
            style="Panel.TLabel",
            foreground=DEMO,
            justify="left",
        ).pack(side="bottom", anchor="w", padx=18, pady=18)
        self.root.after(100, self._pump)
        self.show("Overview" if self.ws() else "Settings")

    # ------------------------------------------------------------------------------------- helpers
    def _nav_command(self, name: str) -> Callable[[], None]:
        return lambda: self.show(name)

    def ws(self) -> Workspace | None:
        try:
            return Workspace.open(self.home)
        except WorkspaceError:
            return None

    def clear(self, title: str) -> ttk.Frame:
        for child in self.body.winfo_children():
            child.destroy()
        self.title.configure(text=title)
        ws = self.ws()
        runner = ws.settings().get("runner", "docker") if ws else "-"
        self.ws_label.configure(text=f"{self.home}  ·  default runner: {runner}" if ws else f"{self.home} (not set up)")
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        return frame

    def show(self, name: str) -> None:
        for key, btn in self.nav.items():
            btn.configure(style="NavActive.TButton" if key == name else "Nav.TButton")
        self.current = name
        self.pages[name]()

    def need_ws(self, frame: ttk.Frame) -> Workspace | None:
        ws = self.ws()
        if ws is None:
            ttk.Label(
                frame, text="No workspace here yet. Open Settings to create or choose one.", style="Muted.TLabel"
            ).pack(anchor="w", pady=20)
        return ws

    def runner_box(self, parent: tk.Misc) -> tk.StringVar:
        ws = self.ws()
        var = tk.StringVar(value=str(ws.settings().get("runner", "docker")) if ws else "docker")
        row = ttk.Frame(parent)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="Runner").pack(side="left")
        ttk.Combobox(row, textvariable=var, values=["docker", "local"], state="readonly", width=10).pack(
            side="left", padx=8
        )
        ttk.Label(row, text="docker = isolated · local = NOT isolated (trusted code only)", style="Muted.TLabel").pack(
            side="left"
        )
        return var

    # ------------------------------------------------------------------------------------- subprocess
    def run_cli(self, args: Sequence[str], done: Callable[[], None] | None = None) -> None:
        if self.proc is not None:
            messagebox.showinfo("RepoBoss", "Another action is still running.")
            return
        argv = [sys.executable, "-m", "repoboss", "--home", str(self.home), *args]
        self.log.delete("1.0", "end")
        _put(self.log, "$ repoboss " + " ".join(args) + "\n", "muted")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", NO_COLOR="1", TERM="dumb", COLUMNS="160")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=flags,
        )
        self.progress.start(12)
        self.stop_btn.configure(state="normal")
        self._done = done

        def reader(proc: subprocess.Popen[str]) -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self.lines.put(line)
            proc.wait()
            self.lines.put(None)

        threading.Thread(target=reader, args=(self.proc,), daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    code = self.proc.returncode if self.proc else None
                    self.proc = None
                    self.progress.stop()
                    self.stop_btn.configure(state="disabled")
                    _put(self.log, f"[finished, exit code {code}]\n", "ok" if code == 0 else "bad")
                    if self._done:
                        self._done()
                    continue
                tag = None
                low = line.lower()
                if "[accepted]" in low or " passed " in low:
                    tag = "ok"
                elif "[rejected]" in low or "error" in low or " failed " in low:
                    tag = "bad"
                elif "warning" in low or "note:" in low:
                    tag = "warn"
                _put(self.log, line, tag)
                self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def stop(self) -> None:
        if self.proc is not None:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True, check=False)
            else:
                self.proc.kill()

    def refresh(self) -> None:
        self.show(self.current)

    # ------------------------------------------------------------------------------------- pages
    def page_overview(self) -> None:
        frame = self.clear("Overview")
        ws = self.need_ws(frame)
        if ws is None:
            return
        challenges = ws.list_challenges()
        runs = ws.list_runs(limit=100_000)
        real = [r for r in runs if not r["agent_is_demo"]]
        cards = ttk.Frame(frame)
        cards.pack(fill="x", pady=(4, 14))
        for label, value in (
            ("Challenges", str(len(challenges))),
            ("Accepted", str(sum(1 for c in challenges if c["status"] == "accepted"))),
            ("Runs (real agents)", str(len(real))),
            ("Passed (real agents)", str(sum(1 for r in real if r["outcome"] == "passed"))),
        ):
            card = ttk.Frame(cards, style="Panel.TFrame", padding=(18, 12))
            card.pack(side="left", expand=True, fill="x", padx=(0, 10))
            ttk.Label(card, text=value, style="CardValue.TLabel").pack(anchor="w")
            ttk.Label(card, text=label, style="CardLabel.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="Per agent (all challenges). Small samples are descriptive, not a ranking.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        tree = _tree(
            frame,
            [
                ("agent", "Agent", 200),
                ("version", "Version", 220),
                ("kind", "Kind", 90),
                ("attempts", "Attempts", 80),
                ("passed", "Passed", 80),
                ("rate", "Rate", 70),
                ("median", "Median s", 90),
            ],
        )
        for s in aggregate(runs):
            tree.insert(
                "",
                "end",
                values=(
                    s.agent,
                    s.version or "n/a",
                    "DEMO" if s.is_demo else "real",
                    s.attempts,
                    s.passed,
                    f"{s.success_rate:.0%}",
                    fmt(s.median_duration_s),
                ),
                tags=("demo",) if s.is_demo else (),
            )
        tree.pack(fill="both", expand=True)

    def page_challenges(self) -> None:
        frame = self.clear("Challenges")
        ws = self.need_ws(frame)
        if ws is None:
            return
        pane = ttk.PanedWindow(frame, orient="vertical")
        pane.pack(fill="both", expand=True)
        top = ttk.Frame(pane)
        tree = _tree(
            top,
            [
                ("id", "ID", 110),
                ("name", "Name", 260),
                ("lang", "Language", 90),
                ("status", "Status", 130),
                ("trusted", "Trusted", 70),
                ("runner", "Runner", 70),
                ("created", "Created", 170),
            ],
        )
        rows = {c["id"]: c for c in ws.list_challenges()}
        for c in rows.values():
            lang = (c["candidate"].get("metadata") or {}).get("language") or (
                "python" if c["candidate"]["path"].endswith(".py") else ""
            )
            tag = "passed" if c["status"] == "accepted" else "rejected"
            tree.insert(
                "",
                "end",
                iid=c["id"],
                values=(
                    c["id"],
                    c["name"],
                    lang,
                    c["status"],
                    "yes" if c["trusted"] else "NO",
                    c["runner"] or "-",
                    c["created_at"],
                ),
                tags=(tag,),
            )
        tree.pack(fill="both", expand=True)
        buttons = ttk.Frame(top)
        buttons.pack(fill="x", pady=8)
        detail = _text(pane, height=12)
        pane.add(top, weight=3)
        pane.add(detail, weight=2)

        def selected() -> dict[str, Any] | None:
            sel = tree.selection()
            return rows.get(sel[0]) if sel else None

        def show_detail(reveal: bool = False) -> None:
            c = selected()
            detail.delete("1.0", "end")
            if c is None:
                return
            cfg = c["config"]
            _put(detail, f"{c['name']}  ({c['id']})\n", "title")
            _put(detail, f"status: {c['status']} - {c['status_detail']}\n")
            _put(detail, f"snapshot: {c['snapshot_hash'][:16]}  seed: {c['seed']}  runner: {c['runner']}\n")
            _put(detail, f"test: {cfg['test']}  framework: {cfg.get('test_framework')}\n")
            v = c.get("verification") or {}
            for label in ("original", "broken", "restored"):
                rep = v.get(label)
                if rep:
                    tag = "ok" if rep["status"] == "passed" else "bad"
                    _put(detail, f"  {label:9} {rep['status']:12} passed={rep['passed']} failed={rep['failed']}\n", tag)
            if reveal:
                cand = c["candidate"]
                _put(detail, f"\nInjected bug: {cand['description']} at {cand['path']}:{cand['line']}\n", "warn")

        tree.bind("<<TreeviewSelect>>", lambda _e: show_detail())
        if rows:
            tree.selection_set(next(iter(rows)))

        def act(kind: str) -> None:
            c = selected()
            if c is None:
                messagebox.showinfo("RepoBoss", "Select a challenge first.")
                return
            if kind == "verify":
                args = ["challenge", "verify", c["id"]]
                if not c["trusted"]:
                    if not messagebox.askyesno(
                        "Imported challenge",
                        "This challenge was imported. Verifying executes its install and test commands. Continue?",
                    ):
                        return
                    args.append("--trust")
                self.run_cli(args, self.refresh)
            elif kind == "reveal":
                show_detail(reveal=True)
            elif kind == "run":
                self.preselect = c["id"]
                self.show("Run agent")
            elif kind == "export":
                path = filedialog.asksaveasfilename(
                    defaultextension=".zip", initialfile=f"{c['id']}.zip", filetypes=[("Challenge package", "*.zip")]
                )
                if path:
                    self.run_cli(["export", c["id"], "--out", path])

        ttk.Button(buttons, text="Run an agent on it", style="Accent.TButton", command=lambda: act("run")).pack(
            side="left"
        )
        ttk.Button(buttons, text="Re-verify", command=lambda: act("verify")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Reveal bug", command=lambda: act("reveal")).pack(side="left")
        ttk.Button(buttons, text="Export…", command=lambda: act("export")).pack(side="left", padx=6)
        ttk.Button(buttons, text="Import…", command=self.import_package).pack(side="left")

    def import_package(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Challenge package", "*.zip")])
        if path:
            self.run_cli(["import", path], self.refresh)

    def page_new(self) -> None:
        frame = self.clear("New challenge")
        if self.need_ws(frame) is None:
            return
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Repository").pack(side="left")
        repo = tk.StringVar()
        ttk.Entry(row, textvariable=repo, width=80).pack(side="left", padx=8, fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=lambda: repo.set(filedialog.askdirectory() or repo.get())).pack(
            side="left"
        )
        ttk.Button(row, text="Detect", command=lambda: self.run_cli(["detect", "--repo", repo.get()])).pack(
            side="left", padx=6
        )
        opts = ttk.Frame(frame)
        opts.pack(fill="x", pady=8)
        ttk.Label(opts, text="Bug types").pack(side="left")
        chosen: dict[str, tk.BooleanVar] = {}
        for name in TRANSFORMS:
            var = tk.BooleanVar(value=True)
            chosen[name] = var
            ttk.Checkbutton(opts, text=name, variable=var).pack(side="left", padx=6)
        nums = ttk.Frame(frame)
        nums.pack(fill="x", pady=4)
        seed, per = tk.StringVar(value="0"), tk.StringVar(value="1")
        ttk.Label(nums, text="Seed").pack(side="left")
        ttk.Spinbox(nums, from_=0, to=99999, textvariable=seed, width=8).pack(side="left", padx=(6, 18))
        ttk.Label(nums, text="Challenges per bug type").pack(side="left")
        ttk.Spinbox(nums, from_=1, to=10, textvariable=per, width=5).pack(side="left", padx=6)
        runner = self.runner_box(frame)
        ttk.Label(
            frame,
            text=(
                "Without a repoboss.toml the project type is detected automatically (npm, Cargo, "
                ".NET, Maven, Gradle, Go, CMake, Python...). Creating a challenge runs the test suite "
                "several times."
            ),
            style="Muted.TLabel",
            wraplength=900,
            justify="left",
        ).pack(anchor="w", pady=8)

        def create() -> None:
            if not repo.get().strip():
                messagebox.showinfo("RepoBoss", "Choose a repository folder.")
                return
            args = [
                "challenge",
                "create",
                "--repo",
                repo.get().strip(),
                "--seed",
                seed.get(),
                "--per-transform",
                per.get(),
                "--runner",
                runner.get(),
            ]
            for name, var in chosen.items():
                if var.get():
                    args += ["--transform", name]
            self.run_cli(args)

        ttk.Button(frame, text="Create challenges", style="Accent.TButton", command=create).pack(anchor="w", pady=6)

    def page_run(self) -> None:
        frame = self.clear("Run agent")
        ws = self.need_ws(frame)
        if ws is None:
            return
        challenges = [c for c in ws.list_challenges() if c["status"] == "accepted" and c["trusted"]]
        try:
            specs = load_agents(ws.agents_path)
        except AgentConfigError as exc:
            ttk.Label(frame, text=f"agents.toml error: {exc}", foreground=BAD).pack(anchor="w")
            return
        labels = [f"{c['id']}  {c['name']}" for c in challenges]
        preselected = next((lbl for lbl in labels if self.preselect and lbl.startswith(self.preselect)), None)
        ch = tk.StringVar(value=preselected or (labels[0] if labels else ""))
        agent_names = list(specs)
        ag = tk.StringVar(value=next((n for n in agent_names if not specs[n].is_demo), agent_names[0]))
        attempts, timeout = tk.StringVar(value="1"), tk.StringVar(value="")
        grid = ttk.Frame(frame)
        grid.pack(anchor="w", pady=6)
        ttk.Label(grid, text="Challenge").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=ch, values=labels, state="readonly", width=60).grid(row=0, column=1, padx=8)
        ttk.Label(grid, text="Agent").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(grid, textvariable=ag, values=agent_names, state="readonly", width=30).grid(
            row=1, column=1, padx=8, sticky="w"
        )
        ttk.Label(grid, text="Attempts").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(grid, from_=1, to=50, textvariable=attempts, width=6).grid(row=2, column=1, padx=8, sticky="w")
        ttk.Label(grid, text="Time limit (s)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(grid, textvariable=timeout, width=8).grid(row=3, column=1, padx=8, sticky="w")
        runner = self.runner_box(frame)
        info = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=900, justify="left")
        info.pack(anchor="w", pady=6)

        def describe(*_a: object) -> None:
            spec = specs.get(ag.get())
            if spec:
                tag = "DEMO adapter: verifies the pipeline, does not measure a model. " if spec.is_demo else ""
                info.configure(text=f"{tag}{spec.description}", foreground=DEMO if spec.is_demo else MUTED)

        ag.trace_add("write", describe)
        describe()

        def start() -> None:
            if not ch.get():
                messagebox.showinfo("RepoBoss", "No accepted challenge yet. Create one first.")
                return
            args = [
                "run",
                ch.get().split()[0],
                "--agent",
                ag.get(),
                "--attempts",
                attempts.get(),
                "--runner",
                runner.get(),
            ]
            if timeout.get().strip():
                args += ["--timeout", timeout.get().strip()]
            self.run_cli(args)

        ttk.Button(frame, text="Run", style="Accent.TButton", command=start).pack(anchor="w", pady=6)
        ttk.Label(
            frame, text=f"Agents are configured in {ws.agents_path} (Settings lets you edit it).", style="Muted.TLabel"
        ).pack(anchor="w")

    def page_runs(self) -> None:
        frame = self.clear("Runs")
        ws = self.need_ws(frame)
        if ws is None:
            return
        pane = ttk.PanedWindow(frame, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        tree = _tree(
            left,
            [
                ("id", "Run", 110),
                ("agent", "Agent", 130),
                ("challenge", "Challenge", 110),
                ("outcome", "Outcome", 90),
                ("tests", "P/F/E", 70),
                ("secs", "Seconds", 70),
            ],
        )
        rows = {r["id"]: r for r in ws.list_runs(limit=500)}
        for r in rows.values():
            tags = [r["outcome"] or ""] + (["demo"] if r["agent_is_demo"] else [])
            tree.insert(
                "",
                "end",
                iid=r["id"],
                values=(
                    r["id"],
                    r["agent_name"] + (" (DEMO)" if r["agent_is_demo"] else ""),
                    r["challenge_id"],
                    r["outcome"],
                    f"{fmt(r['tests_passed'])}/{fmt(r['tests_failed'])}/{fmt(r['tests_errors'])}",
                    fmt(r["total_duration_s"]),
                ),
                tags=tuple(tags),
            )
        tree.pack(fill="both", expand=True)
        detail = _text(pane)
        pane.add(left, weight=2)
        pane.add(detail, weight=3)

        def show(_e: object = None) -> None:
            sel = tree.selection()
            detail.delete("1.0", "end")
            if not sel:
                return
            r = rows[sel[0]]
            _put(detail, f"{r['id']}  {r['outcome']}\n", "ok" if r["outcome"] == "passed" else "bad")
            if r["agent_is_demo"]:
                _put(detail, "DEMO adapter: verifies the pipeline, not a model.\n", "demo")
            if not r["runner_isolated"]:
                _put(detail, "Local runner: not isolated.\n", "warn")
            for label, values in comparison_rows([r]):
                _put(detail, f"{label:24} {values[0]}\n")
            for v in r.get("violations") or []:
                _put(detail, f"violation: {v}\n", "bad")
            if r.get("notes"):
                _put(detail, "\n" + r["notes"] + "\n", "muted")
            _put(detail, "\nFinal patch\n", "title")
            _put_diff(detail, r.get("patch") or "(empty patch)")
            log = ws.run_dir(r["id"]) / "agent.log"
            if log.exists():
                _put(detail, "\nAgent log\n", "title")
                _put(detail, log.read_text(encoding="utf-8", errors="replace")[-30_000:])

        tree.bind("<<TreeviewSelect>>", show)
        if rows:
            tree.selection_set(next(iter(rows)))

    def page_compare(self) -> None:
        frame = self.clear("Compare")
        ws = self.need_ws(frame)
        if ws is None:
            return
        challenges = ws.list_challenges()
        labels = ["All challenges"] + [f"{c['id']}  {c['name']}" for c in challenges]
        var = tk.StringVar(value=labels[0])
        ttk.Combobox(frame, textvariable=var, values=labels, state="readonly", width=60).pack(anchor="w", pady=6)
        tree = _tree(
            frame,
            [
                ("agent", "Agent", 200),
                ("version", "Version", 230),
                ("runner", "Runner", 80),
                ("kind", "Kind", 70),
                ("attempts", "Attempts", 80),
                ("passed", "Passed", 70),
                ("rate", "Rate", 70),
                ("median", "Median s", 80),
                ("outcomes", "Outcomes", 260),
            ],
        )
        tree.pack(fill="both", expand=True)
        note = ttk.Label(frame, text="", style="Muted.TLabel", wraplength=1000, justify="left")
        note.pack(anchor="w", pady=6)

        def load(*_a: object) -> None:
            tree.delete(*tree.get_children())
            cid = None if var.get() == labels[0] else var.get().split()[0]
            stats = aggregate(ws.list_runs(cid, limit=100_000))
            notes = []
            for s in stats:
                outcomes = ", ".join(f"{k}: {n}" for k, n in s.outcomes.items())
                tree.insert(
                    "",
                    "end",
                    values=(
                        s.agent,
                        s.version or "n/a",
                        s.runner,
                        "DEMO" if s.is_demo else "real",
                        s.attempts,
                        s.passed,
                        f"{s.success_rate:.0%}",
                        fmt(s.median_duration_s),
                        outcomes,
                    ),
                    tags=("demo",) if s.is_demo else (),
                )
                n = sample_note(s.attempts)
                if n:
                    notes.append(f"{s.agent}: {n}")
            note.configure(
                text="\n".join(notes) + "\nResults apply to these challenges and this environment only; "
                "they are not a general ranking."
            )

        var.trace_add("write", load)
        load()

    def page_settings(self) -> None:
        frame = self.clear("Settings")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="Workspace").pack(side="left")
        home = tk.StringVar(value=str(self.home))
        ttk.Entry(row, textvariable=home, width=80).pack(side="left", padx=8, fill="x", expand=True)

        def choose() -> None:
            path = filedialog.askdirectory()
            if path:
                home.set(str(Path(path) / ".repoboss") if not Path(path, "repoboss.db").exists() else path)

        def apply() -> None:
            self.home = Path(home.get()).resolve()
            self.show("Settings")

        ttk.Button(row, text="Choose…", command=choose).pack(side="left")
        ttk.Button(row, text="Use", command=apply).pack(side="left", padx=6)
        ws = self.ws()
        if ws is None:
            box = ttk.Frame(frame)
            box.pack(anchor="w", pady=10)
            ttk.Label(box, text="No workspace at this location.").pack(anchor="w")
            runner = tk.StringVar(value="docker")
            ttk.Combobox(box, textvariable=runner, values=["docker", "local"], state="readonly", width=10).pack(
                anchor="w", pady=6
            )
            ttk.Button(
                box,
                text="Create workspace",
                style="Accent.TButton",
                command=lambda: self.run_cli(["init", "--runner", runner.get()], lambda: self.show("Settings")),
            ).pack(anchor="w")
            return
        ttk.Label(frame, text=f"agents.toml — {ws.agents_path}", style="Muted.TLabel").pack(anchor="w", pady=(12, 4))
        editor = _text(frame, height=18)
        editor.configure(wrap="none")
        editor.insert("1.0", ws.agents_path.read_text(encoding="utf-8") if ws.agents_path.exists() else "")
        editor.pack(fill="both", expand=True)

        def save() -> None:
            ws.agents_path.write_text(editor.get("1.0", "end-1c"), encoding="utf-8")
            try:
                names = ", ".join(load_agents(ws.agents_path))
                messagebox.showinfo("RepoBoss", f"Saved. Agents: {names}")
            except AgentConfigError as exc:
                messagebox.showerror("RepoBoss", f"Saved, but the file has an error:\n{exc}")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="Save agents.toml", style="Accent.TButton", command=save).pack(side="left")
        ttk.Button(buttons, text="Doctor", command=lambda: self.run_cli(["doctor"])).pack(side="left", padx=6)
        ttk.Button(buttons, text="Languages", command=lambda: self.run_cli(["languages"])).pack(side="left")


def main(home: Path | None = None, page: str | None = None) -> None:
    root = tk.Tk()
    app = App(root, home)
    if page:
        app.show(page)
    root.mainloop()
