"""A real tool-using coding agent driven by a language model.

The loop runs inside RepoBoss: the model can only act through the tools below, every file path is
checked against the working copy, hidden/protected files cannot be written, and the optional
``run`` tool executes commands through the configured runner (sandboxed with Docker, network off).

Providers:

* ``ollama`` — Ollama's native ``/api/chat`` with tool calling (e.g. ``qwen3:14b``).
* ``openai`` — any OpenAI-compatible ``/chat/completions`` endpoint (OpenAI, OpenRouter, Groq,
  LM Studio, vLLM, Ollama's ``/v1``...). The API key is read from the environment variable named in
  ``api_key_env``.

Token counts come from the API responses. Cost is only computed when prices are configured.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoboss.util import is_link_like, matches_any, walk_files

MAX_READ_LINES = 400
MAX_TOOL_OUTPUT = 12_000
MAX_WRITE_BYTES = 256 * 1024

SYSTEM_PROMPT = """You are an expert software engineer working in a repository that contains a bug.
Some tests fail because of the bug, but the test files are hidden from you. Your job: find the bug
in the source code and fix it with the smallest correct change.

Work method:
1. Look at the file list and read the code related to the failing tests' names.
2. Use search to find relevant identifiers.
3. Fix the bug with replace_in_file (preferred) or write_file. Do not rewrite unrelated code.
4. Call finish with a one-sentence summary when you are done.

Rules: only use the tools. Paths are relative to the repository root. Build and test configuration
files are read-only. Do not create or modify test files."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


@dataclass
class ModelReply:
    content: str
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]
    prompt_tokens: int | None
    completion_tokens: int | None
    model: str | None


class ModelError(Exception):
    pass


def _post(url: str, payload: dict[str, Any], headers: Mapping[str, str], timeout: float) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise ModelError(f"only http(s) endpoints are supported: {url}")
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)  # noqa: S310 - scheme checked
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ModelError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelError(f"cannot reach {url}: {exc}") from exc
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise ModelError(f"invalid JSON from {url}") from exc
    if not isinstance(parsed, dict):
        raise ModelError(f"unexpected response from {url}")
    return parsed


def _get(url: str, timeout: float) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read())
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


class Client:
    provider: str

    def __init__(self, options: Mapping[str, Any]) -> None:
        self.options = options
        self.model = str(options["model"])
        self.timeout = float(options.get("request_timeout_s", 600))
        self.temperature = float(options.get("temperature", 0.2))

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        raise NotImplementedError

    def assistant_message(self, reply: ModelReply) -> dict[str, Any]:
        return reply.raw_message

    def tool_message(self, call: ToolCall, content: str) -> dict[str, Any]:
        raise NotImplementedError

    def detect_version(self) -> tuple[str | None, str | None]:
        return None, None


class OllamaClient(Client):
    provider = "ollama"

    def __init__(self, options: Mapping[str, Any]) -> None:
        super().__init__(options)
        self.base = str(options.get("base_url", "http://127.0.0.1:11434")).rstrip("/")
        self.num_ctx = int(options.get("num_ctx", 16384))
        self.think = bool(options.get("think", False))

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "think": self.think,
            "keep_alive": "15m",
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
        }
        data = _post(f"{self.base}/api/chat", payload, {}, self.timeout)
        msg = data.get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            calls.append(ToolCall(str(fn.get("name", "")), args if isinstance(args, dict) else {}, f"call_{i}"))
        raw = {"role": "assistant", "content": msg.get("content", "") or ""}
        if msg.get("tool_calls"):
            raw["tool_calls"] = msg["tool_calls"]
        return ModelReply(
            raw["content"], calls, raw, data.get("prompt_eval_count"), data.get("eval_count"), data.get("model")
        )

    def tool_message(self, call: ToolCall, content: str) -> dict[str, Any]:
        return {"role": "tool", "content": content, "tool_name": call.name}

    def detect_version(self) -> tuple[str | None, str | None]:
        tags = _get(f"{self.base}/api/tags", 10)
        for model in (tags or {}).get("models", []):
            if model.get("name") == self.model or model.get("model") == self.model:
                digest = str(model.get("digest", ""))[:12]
                return f"{self.model}@{digest}", "detected (ollama model digest)"
        return None, None


class OpenAIClient(Client):
    provider = "openai"

    def __init__(self, options: Mapping[str, Any]) -> None:
        super().__init__(options)
        self.base = str(options.get("base_url", "https://api.openai.com/v1")).rstrip("/")
        key_env = options.get("api_key_env")
        self.key = os.environ.get(str(key_env), "") if key_env else ""
        if key_env and not self.key:
            raise ModelError(f"environment variable {key_env} is not set")

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": [{"type": "function", "function": t["function"]} for t in tools],
            "temperature": self.temperature,
        }
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        data = _post(f"{self.base}/chat/completions", payload, headers, self.timeout)
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"no choices in response: {str(data)[:300]}")
        msg = choices[0].get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(str(fn.get("name", "")), args if isinstance(args, dict) else {}, tc.get("id")))
        raw: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            raw["tool_calls"] = msg["tool_calls"]
        usage = data.get("usage") or {}
        return ModelReply(
            raw["content"], calls, raw, usage.get("prompt_tokens"), usage.get("completion_tokens"), data.get("model")
        )

    def tool_message(self, call: ToolCall, content: str) -> dict[str, Any]:
        return {"role": "tool", "tool_call_id": call.call_id or "", "content": content}


def make_client(options: Mapping[str, Any]) -> Client:
    provider = options.get("provider", "ollama")
    if provider == "ollama":
        return OllamaClient(options)
    if provider == "openai":
        return OpenAIClient(options)
    raise ModelError(f"unknown provider {provider!r} (use 'ollama' or 'openai')")


# ------------------------------------------------------------------------------------------------ tools


def tool_schemas(allow_run: bool) -> list[dict[str, Any]]:
    def fn(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {"type": "object", "properties": props, "required": required},
            },
        }

    tools = [
        fn(
            "list_files",
            "List repository files (optionally under a directory).",
            {"path": {"type": "string", "description": "Directory relative to the repo root; empty for all."}},
            [],
        ),
        fn(
            "read_file",
            "Read a text file with line numbers.",
            {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
            ["path"],
        ),
        fn(
            "search",
            "Search a regular expression in repository files. Returns file:line: text.",
            {"pattern": {"type": "string"}, "path": {"type": "string"}},
            ["pattern"],
        ),
        fn(
            "replace_in_file",
            "Replace an exact text that occurs exactly once in a file.",
            {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
            ["path", "old", "new"],
        ),
        fn(
            "write_file",
            "Create or overwrite a file with the given content.",
            {"path": {"type": "string"}, "content": {"type": "string"}},
            ["path", "content"],
        ),
        fn("finish", "Call when the fix is complete.", {"summary": {"type": "string"}}, ["summary"]),
    ]
    if allow_run:
        tools.insert(
            5,
            fn(
                "run",
                "Run a command (argv list, no shell) in the repository, without network.",
                {"command": {"type": "array", "items": {"type": "string"}}},
                ["command"],
            ),
        )
    return tools


@dataclass
class ToolBox:
    workspace: Path
    hidden: tuple[str, ...]
    protected: tuple[str, ...]
    skip: frozenset[str]
    run_command: Any = None  # callable(argv) -> str, when the run tool is allowed
    changed: set[str] = field(default_factory=set)

    def _path(self, raw: Any, *, must_exist: bool) -> tuple[Path, str]:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("path is required")
        rel = raw.strip().replace("\\", "/").removeprefix("./").strip("/")
        if rel in ("", ".") and not must_exist:
            raise ValueError("path is required")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts) or re.match(r"^[A-Za-z]:", rel) or raw.startswith("/"):
            raise ValueError("path must be relative to the repository and stay inside it")
        target = self.workspace.joinpath(*parts) if parts else self.workspace
        cur = self.workspace
        for p in parts:
            cur = cur / p
            if is_link_like(cur):
                raise ValueError("links are not allowed")
        if not target.resolve().is_relative_to(self.workspace.resolve()):
            raise ValueError("path escapes the repository")
        if must_exist and not target.exists():
            raise ValueError(f"{'/'.join(parts)} does not exist")
        return target, "/".join(parts)

    def list_files(self, args: Mapping[str, Any]) -> str:
        sub = args.get("path") or ""
        base, rel = self._path(sub, must_exist=True) if sub else (self.workspace, "")
        files, _ = walk_files(base, skip=self.skip if not rel else frozenset())
        listed = [f"{rel}/{f}" if rel else f for f in files]
        out = listed[:400]
        more = f"\n... {len(listed) - 400} more" if len(listed) > 400 else ""
        return "\n".join(out) + more if out else "(no files)"

    def read_file(self, args: Mapping[str, Any]) -> str:
        target, rel = self._path(args.get("path"), must_exist=True)
        if target.is_dir():
            return f"{rel} is a directory; use list_files"
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return f"{rel} is not a UTF-8 text file"
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or start + MAX_READ_LINES - 1)
        end = min(len(lines), end, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))
        tail = f"\n[{len(lines) - end} more lines]" if end < len(lines) else ""
        return f"{rel} ({len(lines)} lines)\n{body}{tail}"

    def search(self, args: Mapping[str, Any]) -> str:
        pattern = str(args.get("pattern") or "")
        if not pattern or len(pattern) > 200:
            return "pattern must be 1-200 characters"
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"invalid regular expression: {exc}"
        sub = args.get("path") or ""
        base, rel = self._path(sub, must_exist=True) if sub else (self.workspace, "")
        files, _ = walk_files(base, skip=self.skip if not rel else frozenset())
        hits: list[str] = []
        for f in files:
            path = base / f
            if path.stat().st_size > 1_000_000:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line[:500]):
                    hits.append(f"{rel + '/' if rel else ''}{f}:{n}: {line.strip()[:200]}")
                    if len(hits) >= 80:
                        return "\n".join(hits) + "\n[more matches omitted]"
        return "\n".join(hits) or "no matches"

    def _check_writable(self, rel: str) -> None:
        if matches_any(rel, self.hidden, casefold=True):
            raise ValueError(f"{rel} is a test file: not allowed")
        if matches_any(rel, self.protected, casefold=True):
            raise ValueError(f"{rel} is read-only build/test configuration: not allowed")

    def replace_in_file(self, args: Mapping[str, Any]) -> str:
        target, rel = self._path(args.get("path"), must_exist=True)
        self._check_writable(rel)
        old, new = args.get("old"), args.get("new")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return "old and new must be strings, old non-empty"
        with target.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = text.count(old)
        if count != 1:
            return f"'old' occurs {count} times in {rel}; it must occur exactly once (add surrounding lines)"
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text.replace(old, new, 1))
        self.changed.add(rel)
        return f"replaced 1 occurrence in {rel}"

    def write_file(self, args: Mapping[str, Any]) -> str:
        target, rel = self._path(args.get("path"), must_exist=False)
        self._check_writable(rel)
        content = args.get("content")
        if not isinstance(content, str):
            return "content must be a string"
        if len(content.encode()) > MAX_WRITE_BYTES:
            return "content too large"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        self.changed.add(rel)
        return f"wrote {rel} ({len(content)} characters)"

    def run(self, args: Mapping[str, Any]) -> str:
        if self.run_command is None:
            return "running commands is disabled for this agent"
        command = args.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            return "command must be a non-empty list of strings"
        return str(self.run_command(command))

    def dispatch(self, call: ToolCall) -> str:
        handler = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search": self.search,
            "replace_in_file": self.replace_in_file,
            "write_file": self.write_file,
            "run": self.run,
        }.get(call.name)
        if handler is None:
            return f"unknown tool {call.name!r}"
        try:
            result = handler(call.arguments)
        except (ValueError, OSError) as exc:
            return f"error: {exc}"
        if len(result) > MAX_TOOL_OUTPUT:
            result = result[:MAX_TOOL_OUTPUT] + "\n[output truncated]"
        return result


# ------------------------------------------------------------------------------------------------ loop


@dataclass
class LoopResult:
    status: str  # completed | agent_error | timeout
    finished: bool
    steps: int
    prompt_tokens: int | None
    completion_tokens: int | None
    model_reported: str | None
    log: str
    error: str | None = None


def _compact(messages: list[dict[str, Any]], budget_chars: int) -> None:
    """Drop old tool outputs when the conversation grows beyond the model's context budget."""
    total = sum(len(str(m.get("content", ""))) for m in messages)
    for msg in messages[2:-6]:
        if total <= budget_chars:
            return
        if msg.get("role") == "tool" and len(str(msg.get("content", ""))) > 200:
            total -= len(msg["content"]) - 40
            msg["content"] = "[earlier tool output omitted to save context]"


def run_loop(
    client: Client,
    toolbox: ToolBox,
    task_text: str,
    *,
    max_steps: int,
    deadline: float,
    transcript: Path,
    allow_run: bool,
    context_chars: int,
) -> LoopResult:
    tools = tool_schemas(allow_run)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task_text},
    ]
    log: list[str] = []
    p_tokens: int | None = None
    c_tokens: int | None = None
    reported: str | None = None
    finished = False
    nudges = 0
    step = 0
    with transcript.open("w", encoding="utf-8") as tr:
        for step in range(1, max_steps + 1):
            if time.monotonic() > deadline:
                log.append(f"[step {step}] time limit reached")
                return LoopResult("timeout", False, step - 1, p_tokens, c_tokens, reported, "\n".join(log))
            try:
                reply = client.chat(messages, tools)
            except ModelError as exc:
                log.append(f"[step {step}] model error: {exc}")
                return LoopResult(
                    "agent_error", False, step - 1, p_tokens, c_tokens, reported, "\n".join(log), str(exc)
                )
            reported = reply.model or reported
            if reply.prompt_tokens is not None:
                p_tokens = (p_tokens or 0) + int(reply.prompt_tokens)
            if reply.completion_tokens is not None:
                c_tokens = (c_tokens or 0) + int(reply.completion_tokens)
            messages.append(client.assistant_message(reply))
            tr.write(
                json.dumps(
                    {
                        "step": step,
                        "assistant": reply.content[:4000],
                        "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in reply.tool_calls],
                        "prompt_tokens": reply.prompt_tokens,
                        "completion_tokens": reply.completion_tokens,
                    }
                )
                + "\n"
            )
            if reply.content.strip():
                log.append(f"[step {step}] {reply.content.strip()[:600]}")
            if not reply.tool_calls:
                nudges += 1
                if nudges >= 3:
                    log.append(f"[step {step}] model stopped calling tools")
                    break
                messages.append(
                    {"role": "user", "content": "Use the tools to inspect and fix the code. Call finish when done."}
                )
                continue
            nudges = 0
            for call in reply.tool_calls:
                if call.name == "finish":
                    finished = True
                    result = "ok"
                    log.append(f"[step {step}] finish: {str(call.arguments.get('summary', ''))[:300]}")
                else:
                    result = toolbox.dispatch(call)
                    short = json.dumps(call.arguments)[:160]
                    first = result.splitlines()[0][:120] if result else ""
                    log.append(f"[step {step}] {call.name}({short}) -> {first}")
                messages.append(client.tool_message(call, result))
                tr.write(json.dumps({"step": step, "tool": call.name, "result": result[:4000]}) + "\n")
            if finished:
                break
            _compact(messages, context_chars)
    if not finished:
        log.append(f"stopped after {step} step(s) without calling finish")
    return LoopResult("completed", finished, step, p_tokens, c_tokens, reported, "\n".join(log))


def task_prompt(task: Mapping[str, Any], file_list: list[str], allow_run: bool) -> str:
    files = "\n".join(file_list[:300]) + (f"\n... {len(file_list) - 300} more" if len(file_list) > 300 else "")
    failing = "\n".join(f"- {t}" for t in task.get("failing_tests", [])[:40]) or "- (not available)"
    run_note = (
        "You may run commands with the run tool (no network); the hidden tests are not available."
        if allow_run
        else "You cannot run commands or tests: reason from the code."
    )
    return (
        f"{task.get('instructions', '')}\n\n"
        f"Failing tests (names only; the test files are hidden):\n{failing}\n\n"
        f"Test command used by the evaluator: {' '.join(task.get('test_command', []))}\n"
        f"{run_note}\n\n"
        f"Repository files:\n{files}\n"
    )
