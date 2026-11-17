"""The LLM agent: tool safety, the loop, and the real HTTP protocol against a scripted local server."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from repoboss.agents import AgentSpec
from repoboss.challenge import create_challenges
from repoboss.llm_agent import OllamaClient, OpenAIClient, ToolBox, ToolCall
from repoboss.run import run_challenge
from repoboss.runners import LocalRunner
from repoboss.snapshot import load_snapshot
from repoboss.transforms import Candidate
from repoboss.workspace import Workspace


@pytest.fixture
def box(tmp_path: Path) -> ToolBox:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("LIMIT = 3\n\ndef f(x):\n    return x < LIMIT\n", encoding="utf-8")
    (ws / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    return ToolBox(ws, hidden=("tests/*",), protected=("pyproject.toml",), skip=frozenset())


def call(box: ToolBox, name: str, **args: Any) -> str:
    return box.dispatch(ToolCall(name, args))


def test_tools_read_search_and_replace(box: ToolBox) -> None:
    assert "src/a.py" in call(box, "list_files")
    assert "    4|     return x < LIMIT" in call(box, "read_file", path="src/a.py")
    assert call(box, "search", pattern=r"x <").startswith("src/a.py:4:")
    assert "occurs 0 times" in call(box, "replace_in_file", path="src/a.py", old="nope", new="x")
    assert call(box, "replace_in_file", path="src/a.py", old="x < LIMIT", new="x <= LIMIT").startswith("replaced")
    assert "x <= LIMIT" in (box.workspace / "src" / "a.py").read_text()
    assert box.changed == {"src/a.py"}


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "src/../../x"])
def test_tools_refuse_paths_outside_the_workspace(box: ToolBox, path: str) -> None:
    out = call(box, "write_file", path=path, content="x")
    assert out.startswith("error:")
    assert not (box.workspace.parent / "outside.txt").exists()


def test_tools_refuse_tests_and_read_only_files(box: ToolBox) -> None:
    assert "test file" in call(box, "write_file", path="tests/test_a.py", content="def test(): pass")
    assert "read-only" in call(box, "write_file", path="pyproject.toml", content="")
    assert not (box.workspace / "tests").exists()


def test_tools_refuse_symlinks(box: ToolBox, tmp_path: Path) -> None:
    try:
        os.symlink(tmp_path, box.workspace / "escape", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this system")
    assert "links are not allowed" in call(box, "read_file", path="escape/ws/src/a.py")


def test_run_tool_disabled_by_default(box: ToolBox) -> None:
    assert "disabled" in call(box, "run", command=["python", "-c", "print(1)"])


# ------------------------------------------------------------------------------- scripted model server


class Script:
    """Replies served in order; records every request body."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []


def serve(script: Script) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            body = json.dumps({"models": [{"name": "fake:1b", "digest": "0123456789abcdef"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            script.requests.append((self.path, payload, dict(self.headers)))
            reply = script.replies.pop(0) if script.replies else {"message": {"content": "done"}}
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def ollama_reply(tool: str | None, args: dict[str, Any] | None = None, content: str = "") -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool:
        msg["tool_calls"] = [{"function": {"name": tool, "arguments": args or {}}}]
    return {"model": "fake:1b", "message": msg, "prompt_eval_count": 100, "eval_count": 10}


def test_ollama_protocol_and_version_detection() -> None:
    script = Script([ollama_reply("list_files")])
    for base in serve(script):
        client = OllamaClient({"model": "fake:1b", "base_url": base})
        assert client.detect_version() == ("fake:1b@0123456789ab", "detected (ollama model digest)")
        reply = client.chat([{"role": "user", "content": "hi"}], [])
        path, payload, _ = script.requests[0]
        assert path == "/api/chat" and payload["stream"] is False and payload["think"] is False
        assert payload["options"]["num_ctx"] == 16384
        assert reply.tool_calls[0].name == "list_files" and reply.prompt_tokens == 100


def test_openai_protocol_sends_key_and_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RB_FAKE_KEY", "sk-test")
    script = Script(
        [
            {
                "model": "gpt-fake",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5},
            }
        ]
    )
    for base in serve(script):
        client = OpenAIClient({"model": "gpt-fake", "base_url": base + "/v1", "api_key_env": "RB_FAKE_KEY"})
        reply = client.chat([], [{"type": "function", "function": {"name": "read_file", "parameters": {}}}])
        path, payload, headers = script.requests[0]
        assert path == "/v1/chat/completions"
        assert headers.get("Authorization") == "Bearer sk-test"
        assert payload["tools"][0]["type"] == "function"
        assert reply.tool_calls[0].arguments == {"path": "a"} and reply.tool_calls[0].call_id == "call_1"
        assert client.tool_message(reply.tool_calls[0], "x")["tool_call_id"] == "call_1"
        assert (reply.prompt_tokens, reply.completion_tokens) == (50, 5)


def test_full_llm_run_against_scripted_model(demo_repo: Path, ws: Workspace, runner: LocalRunner) -> None:
    """End to end: the 'model' reads the file and applies the fix through tools; RepoBoss evaluates it."""
    _, results = create_challenges(ws, demo_repo, runner, transforms=["logic_condition"], seed=0)
    cid = results[0].challenge_id
    assert cid is not None
    row = ws.get_challenge(cid)
    assert row is not None
    cand = Candidate.from_dict(row["candidate"])
    snap = load_snapshot(ws.root / "snapshots", row["snapshot_hash"])
    assert snap.root is not None
    # Script the conversation from the real bug: replace the buggy line with the original one.
    original_text = (snap.root / cand.path).read_text(encoding="utf-8")
    bug_text = original_text[: cand.edit.start] + cand.edit.new + original_text[cand.edit.end :]
    original_line = original_text.splitlines()[cand.line - 1]
    bug_line = bug_text.splitlines()[cand.line - 1]
    assert original_line != bug_line
    script = Script(
        [
            ollama_reply("read_file", {"path": cand.path}),
            ollama_reply("replace_in_file", {"path": cand.path, "old": bug_line, "new": original_line}),
            ollama_reply("finish", {"summary": "restored the boundary"}),
        ]
    )
    for base in serve(script):
        spec = AgentSpec(
            name="fake",
            kind="llm",
            is_demo=False,
            description="scripted",
            options={"provider": "ollama", "model": "fake:1b", "base_url": base},
        )
        run = ws.get_run(run_challenge(ws, cid, spec, runner)[0].run_id)
    assert run is not None
    assert run["outcome"] == "passed", run
    assert run["input_tokens"] == 300 and run["output_tokens"] == 30
    assert run["usage_source"] == "ollama api response" and run["cost_usd"] is None
    assert run["agent_version"] == "fake:1b@0123456789ab"
    assert "finished: yes" in run["notes"]
    transcript = ws.run_dir(run["id"]) / "agent-out" / "transcript.jsonl"
    assert len(transcript.read_text(encoding="utf-8").splitlines()) == 6


def test_model_errors_are_recorded(demo_repo: Path, ws: Workspace, runner: LocalRunner) -> None:
    _, results = create_challenges(ws, demo_repo, runner, transforms=["config_value"], seed=0)
    cid = results[0].challenge_id
    assert cid is not None
    spec = AgentSpec(
        name="down",
        kind="llm",
        is_demo=False,
        description="unreachable",
        options={"provider": "ollama", "model": "x", "base_url": "http://127.0.0.1:9", "request_timeout_s": 5},
    )
    run = ws.get_run(run_challenge(ws, cid, spec, runner)[0].run_id)
    assert run is not None
    assert run["attempt_status"] == "agent_error"
    assert run["input_tokens"] is None  # nothing measured, nothing invented
    assert run["outcome"] == "failed"
