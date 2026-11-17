"""Subprocess execution with a hard timeout, process-tree kill and a capped log."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LOG_LIMIT = 1_000_000  # bytes kept per process (head + tail)


@dataclass
class ExecResult:
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    duration_s: float
    output: str
    output_bytes: int
    truncated: bool
    launch_error: str | None = None
    rejected: list[str] = field(default_factory=list)  # entries refused when copying files back


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    timeout_s: float,
    log_limit: int = DEFAULT_LOG_LIMIT,
    on_timeout: Callable[[], None] | None = None,
) -> ExecResult:
    """Run ``argv`` (never through a shell). stdout and stderr are merged.

    The kept log is the first and last ``log_limit // 2`` bytes; the total size is reported.
    """
    argv = [str(a) for a in argv]
    half = max(1, log_limit // 2)
    head = bytearray()
    tail = bytearray()
    total = 0
    start = time.monotonic()
    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        return ExecResult(argv, None, False, time.monotonic() - start, f"could not start: {exc}", 0, False, str(exc))

    def _reader() -> None:
        nonlocal total
        stream = proc.stdout
        assert isinstance(stream, io.BufferedReader)
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            room = half - len(head)
            if room > 0:
                head.extend(chunk[:room])
                chunk = chunk[room:]
            if chunk:
                tail.extend(chunk)
                if len(tail) > half:
                    del tail[: len(tail) - half]

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        if on_timeout is not None:
            # Best effort (e.g. `docker kill`); the process-tree kill below always runs.
            with contextlib.suppress(Exception):
                on_timeout()
        _kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=30)
    reader.join(timeout=10)
    duration = time.monotonic() - start
    kept = len(head) + len(tail)
    truncated = total > kept
    data = bytes(head)
    if truncated:
        data += f"\n[... {total - kept} bytes omitted by RepoBoss log limit ...]\n".encode()
    data += bytes(tail)
    text = data.decode("utf-8", errors="replace")
    exit_code = None if timed_out else proc.returncode
    return ExecResult(argv, exit_code, timed_out, duration, text, total, truncated)
