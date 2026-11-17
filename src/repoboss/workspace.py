"""The local RepoBoss workspace: SQLite database, snapshots, environments and run artifacts."""

from __future__ import annotations

import json
import os
import sqlite3
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from repoboss.util import now_iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS challenges (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_repo TEXT,
    snapshot_hash TEXT NOT NULL,
    transform TEXT NOT NULL,
    seed INTEGER NOT NULL,
    description TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    status_detail TEXT,
    verification_json TEXT,
    env_json TEXT,
    runner TEXT,
    trusted INTEGER NOT NULL DEFAULT 1,
    imported_from TEXT,
    verified_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    challenge_id TEXT NOT NULL REFERENCES challenges(id),
    batch_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    agent_name TEXT NOT NULL,
    agent_version TEXT,
    agent_version_source TEXT,
    agent_is_demo INTEGER NOT NULL,
    runner TEXT NOT NULL,
    runner_isolated INTEGER NOT NULL,
    network INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    env_json TEXT,
    outcome TEXT,
    prepare_status TEXT,
    attempt_status TEXT,
    attempt_exit_code INTEGER,
    attempt_duration_s REAL,
    eval_status TEXT,
    eval_exit_code INTEGER,
    eval_duration_s REAL,
    tests_passed INTEGER,
    tests_failed INTEGER,
    tests_errors INTEGER,
    tests_skipped INTEGER,
    tests_total INTEGER,
    failing_tests_json TEXT,
    total_duration_s REAL,
    patch TEXT,
    files_changed INTEGER,
    lines_added INTEGER,
    lines_removed INTEGER,
    matches_reference INTEGER,
    violations_json TEXT,
    ignored_json TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    usage_source TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_challenge ON runs(challenge_id, created_at);
"""

JSON_COLUMNS = {
    "candidate_json",
    "config_json",
    "verification_json",
    "env_json",
    "failing_tests_json",
    "violations_json",
    "ignored_json",
}

DEFAULT_SETTINGS = """\
# RepoBoss workspace settings.
# runner: "docker" (isolated, recommended) or "local" (NOT isolated: only for trusted code and demos).
runner = "{runner}"

[limits]
cpus = 2.0
memory = "4g"
pids = 512
log_limit_bytes = 1000000
"""

DEFAULT_AGENTS = """\
# Agents for `repoboss run CHALLENGE_ID --agent NAME`. Built in: demo-oracle, demo-noop (pipeline demos).
# See docs/agents.md for every option.
#
# --- A real AI agent through a local model in Ollama (no account, runs on your GPU) -----------------
# [agents.qwen]
# type = "llm"
# provider = "ollama"
# model = "qwen3:14b"
# base_url = "http://127.0.0.1:11434"
# max_steps = 25
# num_ctx = 16384
# timeout_s = 900
#
# --- Any OpenAI-compatible API (OpenAI, OpenRouter, Groq, LM Studio, vLLM...) ------------------------
# [agents.gpt]
# type = "llm"
# provider = "openai"
# base_url = "https://api.openai.com/v1"
# model = "gpt-4.1-mini"
# api_key_env = "OPENAI_API_KEY"          # read from your environment, never stored
# price_input_per_mtok = 0.40             # optional: enables a computed cost
# price_output_per_mtok = 1.60
#
# --- Your own command (any tool or script) ----------------------------------------------------------
# Commands are argv lists (no shell). Placeholders: {workspace} {output_dir} {task_file} {agent_dir}
# [agents.custom]
# command = ["python", "{agent_dir}/my_agent.py"]
# agent_dir = "C:/path/to/my-agent"
# version = "0.1.0"
# pass_env = []                           # host variables explicitly forwarded (none by default)
# network = false                         # network during the attempt (Docker runner only)
# timeout_s = 600
"""


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    # paths -----------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.root / "repoboss.db"

    @property
    def settings_path(self) -> Path:
        return self.root / "settings.toml"

    @property
    def agents_path(self) -> Path:
        return self.root / "agents.toml"

    def snapshot_dir(self, snapshot_hash: str) -> Path:
        return self.root / "snapshots" / snapshot_hash

    def challenge_dir(self, challenge_id: str) -> Path:
        return self.root / "challenges" / challenge_id

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    @property
    def envs_dir(self) -> Path:
        return self.root / "envs"

    # lifecycle -------------------------------------------------------------
    @staticmethod
    def default_root() -> Path:
        env = os.environ.get("REPOBOSS_HOME")
        return Path(env) if env else Path.cwd() / ".repoboss"

    @classmethod
    def open(cls, root: Path | None = None) -> Workspace:
        ws = cls(root or cls.default_root())
        if not ws.db_path.is_file():
            raise WorkspaceError(f"No RepoBoss workspace at {ws.root}. Run `repoboss init` first.")
        return ws

    @classmethod
    def init(cls, root: Path | None = None, *, runner: str = "docker") -> tuple[Workspace, bool]:
        ws = cls(root or cls.default_root())
        created = not ws.db_path.exists()
        for sub in ("snapshots", "challenges", "runs", "envs"):
            (ws.root / sub).mkdir(parents=True, exist_ok=True)
        if not ws.settings_path.exists():
            if runner not in ("docker", "local"):
                raise WorkspaceError("runner must be 'docker' or 'local'")
            ws.settings_path.write_text(DEFAULT_SETTINGS.replace("{runner}", runner), encoding="utf-8")
        if not ws.agents_path.exists():
            ws.agents_path.write_text(DEFAULT_AGENTS, encoding="utf-8")
        gitignore = ws.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        with ws.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?), ('created_at', ?)",
                (str(SCHEMA_VERSION), now_iso()),
            )
        return ws, created

    def settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {"runner": "docker", "limits": {}}
        return tomllib.loads(self.settings_path.read_text(encoding="utf-8"))

    # database --------------------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in list(data):
            if key in JSON_COLUMNS:
                raw = data.pop(key)
                data[key[: -len("_json")]] = json.loads(raw) if raw is not None else None
        return data

    @staticmethod
    def _encode(values: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in values.items():
            if f"{key}_json" in JSON_COLUMNS:
                out[f"{key}_json"] = None if value is None else json.dumps(value, sort_keys=True)
            elif isinstance(value, bool):
                out[key] = int(value)
            else:
                out[key] = value
        return out

    def _insert(self, table: str, values: dict[str, Any]) -> None:
        enc = self._encode(values)
        cols = ", ".join(enc)
        marks = ", ".join("?" for _ in enc)
        with self.connect() as conn:
            conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(enc.values()))  # noqa: S608

    def _update(self, table: str, row_id: str, values: dict[str, Any]) -> None:
        enc = self._encode(values)
        sets = ", ".join(f"{k} = ?" for k in enc)
        with self.connect() as conn:
            conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*enc.values(), row_id))  # noqa: S608

    def insert_challenge(self, values: dict[str, Any]) -> None:
        self._insert("challenges", values)

    def update_challenge(self, challenge_id: str, values: dict[str, Any]) -> None:
        self._update("challenges", challenge_id, values)

    def get_challenge(self, challenge_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM challenges WHERE id = ?", (challenge_id,)).fetchone()
        return self._decode(row)

    def list_challenges(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM challenges ORDER BY created_at DESC, id").fetchall()
        return [d for d in map(self._decode, rows) if d is not None]

    def insert_run(self, values: dict[str, Any]) -> None:
        self._insert("runs", values)

    def update_run(self, run_id: str, values: dict[str, Any]) -> None:
        self._update("runs", run_id, values)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._decode(row)

    def list_runs(self, challenge_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if challenge_id:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE challenge_id = ? ORDER BY created_at DESC, attempt DESC LIMIT ?",
                    (challenge_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC, attempt DESC LIMIT ?", (limit,)
                ).fetchall()
        return [d for d in map(self._decode, rows) if d is not None]
