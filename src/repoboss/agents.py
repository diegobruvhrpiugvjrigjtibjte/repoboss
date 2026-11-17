"""Agent adapters.

An adapter receives a working copy containing the buggy repository (hidden test files removed) and
a task. It may change files; RepoBoss then evaluates only the allowed changes.

Built-in adapters:

* ``demo-oracle`` — **pipeline demonstration only.** It is handed the known reference fix and
  applies it. Its success says nothing about any model; it proves the pipeline works end to end.
* ``demo-noop`` — **pipeline demonstration only.** Changes nothing; its runs are expected to fail.

Configurable adapters (``agents.toml``):

* ``type = "llm"`` — a real tool-using agent driven by a language model (Ollama or any
  OpenAI-compatible API). See :mod:`repoboss.llm_agent`.
* ``type = "command"`` (default) — runs a user-provided command inside the runner.
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoboss import __version__
from repoboss.config import RepoConfig
from repoboss.runners import EnvInfo, Runner
from repoboss.transforms import Candidate, TransformError, apply_edit
from repoboss.util import walk_files

PLACEHOLDERS = ("{workspace}", "{output_dir}", "{task_file}", "{agent_dir}")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED_ENV_PREFIX = "REPOBOSS_"
LLM_OPTION_TYPES: dict[str, tuple[type, ...]] = {
    "provider": (str,),
    "model": (str,),
    "base_url": (str,),
    "api_key_env": (str,),
    "max_steps": (int,),
    "temperature": (int, float),
    "num_ctx": (int,),
    "think": (bool,),
    "allow_run": (bool,),
    "request_timeout_s": (int, float),
    "price_input_per_mtok": (int, float),
    "price_output_per_mtok": (int, float),
}


class AgentConfigError(Exception):
    pass


@dataclass(frozen=True)
class AgentSpec:
    name: str
    kind: str  # demo-oracle | demo-noop | command | llm
    is_demo: bool
    description: str
    version: str | None = None
    version_source: str | None = None  # "declared" | "builtin" | "detected (...)" | None
    command: tuple[str, ...] = ()
    agent_dir: Path | None = None
    pass_env: tuple[str, ...] = ()
    network: bool = False
    timeout_s: float = 600.0
    options: Mapping[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "is_demo": self.is_demo,
            "version": self.version,
            "version_source": self.version_source,
            "command": list(self.command),
            "agent_dir": str(self.agent_dir) if self.agent_dir else None,
            "pass_env": list(self.pass_env),  # names only, never values
            "network": self.network,
            "timeout_s": self.timeout_s,
            "options": {k: v for k, v in self.options.items()},
        }


@dataclass
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    source: str


@dataclass
class AttemptContext:
    runner: Runner
    env: EnvInfo
    workspace: Path
    outdir: Path
    task: dict[str, Any]
    candidate: Candidate
    timeout_s: float
    network: bool
    config: RepoConfig


@dataclass
class AttemptResult:
    status: str  # completed | agent_error | timeout | launch_error
    exit_code: int | None
    duration_s: float
    log: str
    usage: Usage | None = None
    notes: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    version: str | None = None
    version_source: str | None = None
    steps: int | None = None


class AgentAdapter(ABC):
    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec

    @abstractmethod
    def attempt(self, ctx: AttemptContext) -> AttemptResult: ...


class DemoOracleAgent(AgentAdapter):
    def attempt(self, ctx: AttemptContext) -> AttemptResult:
        try:
            apply_edit(ctx.workspace, ctx.candidate.edit.inverse())
        except TransformError as exc:
            return AttemptResult("agent_error", 1, 0.0, f"DEMO ORACLE could not apply the reference fix: {exc}\n")
        return AttemptResult(
            "completed",
            0,
            0.0,
            "DEMO ORACLE: applied the known reference fix. This verifies the RepoBoss pipeline; "
            "it does not measure any model.\n",
        )


class DemoNoopAgent(AgentAdapter):
    def attempt(self, ctx: AttemptContext) -> AttemptResult:
        return AttemptResult("completed", 0, 0.0, "DEMO NOOP: made no changes (expected to fail evaluation).\n")


def read_usage(outdir: Path) -> tuple[Usage | None, list[str]]:
    """Parse ``usage.json`` written by a command agent. Values are recorded as agent-reported."""
    path = outdir / "usage.json"
    if not path.is_file():
        return None, []
    notes: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8")[:100_000])
    except (ValueError, UnicodeDecodeError):
        return None, ["usage.json is not valid JSON; ignored"]
    if not isinstance(data, dict):
        return None, ["usage.json must contain an object; ignored"]

    def _int(key: str) -> int | None:
        v = data.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            notes.append(f"usage.json: {key} ignored (not a non-negative integer)")
            return None
        return v

    cost = data.get("cost_usd")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0):
        notes.append("usage.json: cost_usd ignored (not a non-negative number)")
        cost = None
    source = data.get("source")
    label = "agent-reported" + (f" ({source[:80]})" if isinstance(source, str) and source else "")
    usage = Usage(_int("input_tokens"), _int("output_tokens"), float(cost) if cost is not None else None, label)
    if usage.input_tokens is None and usage.output_tokens is None and usage.cost_usd is None:
        return None, notes
    return usage, notes


class CommandAgent(AgentAdapter):
    def build_argv(self, ctx: AttemptContext) -> list[str]:
        paths = ctx.runner.paths(ctx.workspace, ctx.outdir, self.spec.agent_dir)
        values = {
            "{workspace}": paths.workspace,
            "{output_dir}": paths.output,
            "{task_file}": f"{paths.output}/task.json",
            "{agent_dir}": paths.agent or "",
        }
        argv: list[str] = []
        for arg in self.spec.command:
            for key, value in values.items():
                arg = arg.replace(key, value)
            argv.append(arg)
        return argv

    def attempt(self, ctx: AttemptContext) -> AttemptResult:
        paths = ctx.runner.paths(ctx.workspace, ctx.outdir, self.spec.agent_dir)
        extra: dict[str, str] = {
            "REPOBOSS_WORKSPACE": paths.workspace,
            "REPOBOSS_OUTPUT_DIR": paths.output,
            "REPOBOSS_TASK_FILE": f"{paths.output}/task.json",
            "REPOBOSS_CHALLENGE_ID": str(ctx.task["challenge_id"]),
            "REPOBOSS_NETWORK": "1" if ctx.network else "0",
        }
        if paths.agent:
            extra["REPOBOSS_AGENT_DIR"] = paths.agent
        for name in self.spec.pass_env:
            if name in os.environ:
                extra[name] = os.environ[name]
        ctx.runner.attach_dependencies(ctx.env, ctx.workspace, ctx.config)
        res = ctx.runner.execute(
            ctx.env,
            self.build_argv(ctx),
            workdir=ctx.workspace,
            outdir=ctx.outdir,
            agent_dir=self.spec.agent_dir,
            network=ctx.network,
            timeout_s=ctx.timeout_s,
            extra_env=extra,
            config=ctx.config,
        )
        usage, notes = read_usage(ctx.outdir)
        if res.launch_error:
            status = "launch_error"
        elif res.timed_out:
            status = "timeout"
        elif res.exit_code != 0:
            status = "agent_error"
        else:
            status = "completed"
        if res.truncated:
            notes.append(f"agent output truncated ({res.output_bytes} bytes produced)")
        return AttemptResult(status, res.exit_code, res.duration_s, res.output, usage, notes, list(res.rejected))


class LLMAgent(AgentAdapter):
    """Tool-using agent; the loop runs in RepoBoss and edits the working copy through checked tools."""

    def attempt(self, ctx: AttemptContext) -> AttemptResult:
        from repoboss import llm_agent

        opts = dict(self.spec.options)
        started = time.monotonic()
        try:
            client = llm_agent.make_client(opts)
        except llm_agent.ModelError as exc:
            return AttemptResult("launch_error", None, 0.0, f"{exc}\n")
        version, version_source = client.detect_version()
        allow_run = bool(opts.get("allow_run", False))
        scratch = ctx.outdir / "run-scratch"

        def run_command(argv: list[str]) -> str:
            ctx.runner.attach_dependencies(ctx.env, ctx.workspace, ctx.config)
            scratch.mkdir(parents=True, exist_ok=True)
            res = ctx.runner.execute(
                ctx.env,
                argv,
                workdir=ctx.workspace,
                outdir=scratch,
                network=False,
                timeout_s=min(180.0, ctx.timeout_s),
                config=ctx.config,
            )
            head = f"exit code: {res.exit_code}{' (timed out)' if res.timed_out else ''}\n"
            return head + res.output[-6000:]

        toolbox = llm_agent.ToolBox(
            workspace=ctx.workspace,
            hidden=ctx.config.hidden,
            protected=ctx.config.protected,
            skip=frozenset(ctx.config.dependency_dirs),
            run_command=run_command if allow_run else None,
        )
        files, _ = walk_files(ctx.workspace, skip=frozenset(ctx.config.dependency_dirs))
        prompt = llm_agent.task_prompt(ctx.task, files, allow_run)
        num_ctx = int(opts.get("num_ctx", 16384))
        loop = llm_agent.run_loop(
            client,
            toolbox,
            prompt,
            max_steps=int(opts.get("max_steps", 30)),
            deadline=started + ctx.timeout_s,
            transcript=ctx.outdir / "transcript.jsonl",
            allow_run=allow_run,
            context_chars=num_ctx * 3,
        )
        duration = time.monotonic() - started
        usage = None
        if loop.prompt_tokens is not None or loop.completion_tokens is not None:
            source = f"{client.provider} api response"
            cost = None
            p_in, p_out = opts.get("price_input_per_mtok"), opts.get("price_output_per_mtok")
            if p_in is not None and p_out is not None:
                cost = round(
                    ((loop.prompt_tokens or 0) * float(p_in) + (loop.completion_tokens or 0) * float(p_out))
                    / 1_000_000,
                    6,
                )
                source += " + cost computed from configured prices"
            usage = Usage(loop.prompt_tokens, loop.completion_tokens, cost, source)
        notes = [f"model steps: {loop.steps}; finished: {'yes' if loop.finished else 'no'}"]
        if loop.model_reported and loop.model_reported != client.model:
            notes.append(f"API reported model: {loop.model_reported}")
        if allow_run and not ctx.runner.isolated:
            notes.append("run tool executed commands on the host (local runner, not isolated)")
        exit_code = 0 if loop.status == "completed" else None
        if version is None and loop.model_reported:
            version, version_source = loop.model_reported, "reported by API"
        return AttemptResult(
            loop.status,
            exit_code,
            duration,
            loop.log + "\n",
            usage,
            notes,
            [],
            version or client.model,
            version_source or "declared (model name)",
            loop.steps,
        )


BUILTIN = {
    "demo-oracle": AgentSpec(
        "demo-oracle",
        "demo-oracle",
        True,
        "DEMO: applies the known fix. Verifies the pipeline; does not measure a model.",
        __version__,
        "builtin",
    ),
    "demo-noop": AgentSpec(
        "demo-noop",
        "demo-noop",
        True,
        "DEMO: changes nothing. Verifies that failures are detected.",
        __version__,
        "builtin",
    ),
}


def _parse_common(name: str, data: Mapping[str, Any]) -> tuple[float, bool, str | None]:
    version = data.get("version")
    if version is not None and not isinstance(version, str):
        raise AgentConfigError(f"agents.{name}.version must be a string")
    timeout = data.get("timeout_s", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise AgentConfigError(f"agents.{name}.timeout_s must be a positive number")
    network = data.get("network", False)
    if not isinstance(network, bool):
        raise AgentConfigError(f"agents.{name}.network must be true or false")
    return float(timeout), network, version


def _parse_llm(name: str, data: Mapping[str, Any]) -> AgentSpec:
    allowed = {"type", "description", "timeout_s", "version", "network", *LLM_OPTION_TYPES}
    unknown = set(data) - allowed
    if unknown:
        raise AgentConfigError(f"agents.{name}: unknown keys {', '.join(sorted(unknown))}")
    options: dict[str, Any] = {}
    for key, types in LLM_OPTION_TYPES.items():
        if key in data:
            value = data[key]
            if isinstance(value, bool) and bool not in types:
                raise AgentConfigError(f"agents.{name}.{key} has the wrong type")
            if not isinstance(value, types):
                raise AgentConfigError(f"agents.{name}.{key} has the wrong type")
            options[key] = value
    if "model" not in options:
        raise AgentConfigError(f'agents.{name}.model is required for type = "llm"')
    options.setdefault("provider", "ollama")
    if options["provider"] not in ("ollama", "openai"):
        raise AgentConfigError(f"agents.{name}.provider must be 'ollama' or 'openai'")
    if "api_key_env" in options and not _ENV_NAME.match(options["api_key_env"]):
        raise AgentConfigError(f"agents.{name}.api_key_env must be an environment variable name")
    timeout, network, version = _parse_common(name, data)
    desc = str(data.get("description", f"{options['provider']} model {options['model']}"))
    return AgentSpec(
        name=name,
        kind="llm",
        is_demo=False,
        description=desc,
        version=version,
        version_source="declared" if version else None,
        network=network,
        timeout_s=timeout,
        options=options,
    )


def _parse_command(name: str, data: Mapping[str, Any], base: Path) -> AgentSpec:
    allowed = {"type", "command", "agent_dir", "version", "pass_env", "network", "timeout_s", "description"}
    unknown = set(data) - allowed
    if unknown:
        raise AgentConfigError(f"agents.{name}: unknown keys {', '.join(sorted(unknown))}")
    command = data.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        raise AgentConfigError(f"agents.{name}.command must be a non-empty list of strings (argv, no shell)")
    for arg in command:
        for token in re.findall(r"\{[^{}]*\}", arg):
            if token not in PLACEHOLDERS:
                raise AgentConfigError(f"agents.{name}.command: unknown placeholder {token}")
    agent_dir = None
    if data.get("agent_dir") is not None:
        if not isinstance(data["agent_dir"], str):
            raise AgentConfigError(f"agents.{name}.agent_dir must be a path string")
        agent_dir = Path(data["agent_dir"])
        if not agent_dir.is_absolute():
            agent_dir = (base / agent_dir).resolve()
        if not agent_dir.is_dir():
            raise AgentConfigError(f"agents.{name}.agent_dir {agent_dir} is not a directory")
    pass_env = data.get("pass_env", [])
    if not isinstance(pass_env, list) or not all(isinstance(v, str) and _ENV_NAME.match(v) for v in pass_env):
        raise AgentConfigError(f"agents.{name}.pass_env must be a list of environment variable names")
    if any(v.upper().startswith(RESERVED_ENV_PREFIX) for v in pass_env):
        raise AgentConfigError(f"agents.{name}.pass_env must not contain REPOBOSS_* names")
    timeout, network, version = _parse_common(name, data)
    return AgentSpec(
        name=name,
        kind="command",
        is_demo=False,
        description=str(data.get("description", "custom command")),
        version=version,
        version_source="declared" if version else None,
        command=tuple(command),
        agent_dir=agent_dir,
        pass_env=tuple(pass_env),
        network=network,
        timeout_s=timeout,
    )


def _parse_agent(name: str, data: Mapping[str, Any], base: Path) -> AgentSpec:
    if not _NAME.match(name) or name in BUILTIN:
        raise AgentConfigError(f"invalid or reserved agent name {name!r}")
    kind = data.get("type", "command")
    if kind == "llm":
        return _parse_llm(name, data)
    if kind == "command":
        return _parse_command(name, data, base)
    raise AgentConfigError(f"agents.{name}.type must be 'command' or 'llm'")


def load_agents(agents_file: Path) -> dict[str, AgentSpec]:
    specs = dict(BUILTIN)
    if agents_file.is_file():
        try:
            data = tomllib.loads(agents_file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise AgentConfigError(f"{agents_file}: {exc}") from exc
        table = data.get("agents", {})
        if not isinstance(table, dict):
            raise AgentConfigError(f"{agents_file}: [agents] must be a table")
        for name, entry in table.items():
            if not isinstance(entry, dict):
                raise AgentConfigError(f"{agents_file}: agents.{name} must be a table")
            specs[name] = _parse_agent(name, entry, agents_file.parent)
    return specs


def make_adapter(spec: AgentSpec) -> AgentAdapter:
    if spec.kind == "demo-oracle":
        return DemoOracleAgent(spec)
    if spec.kind == "demo-noop":
        return DemoNoopAgent(spec)
    if spec.kind == "llm":
        return LLMAgent(spec)
    return CommandAgent(spec)
