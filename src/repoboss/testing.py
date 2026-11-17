"""Running a repository's build and test commands and classifying the outcome."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from repoboss.config import RepoConfig
from repoboss.procs import ExecResult
from repoboss.results import Counts, extract, guess_framework, parse_junit_file
from repoboss.runners import EnvInfo, Runner

# Test statuses
PASSED = "passed"
FAILED = "failed"  # tests ran and at least one failed: the only state that proves a detected bug
COLLECTION_ERROR = "collection_error"
BUILD_ERROR = "build_error"
NO_RESULTS = "no_results"
DEPENDENCY_MISSING = "dependency_missing"
TIMEOUT = "timeout"
SETUP_ERROR = "setup_error"
NO_TESTS = "no_tests"
ERROR = "error"

_MISSING = (
    re.compile(r"No module named '?([A-Za-z_][A-Za-z0-9_.]*)'?"),  # Python
    re.compile(r"Cannot find (?:module|package) '([^']+)'"),  # Node (CJS and ESM)
    re.compile(r"no required module provides package (\S+)"),  # Go
    re.compile(r"can't find crate for `([^`]+)`"),  # Rust
)


@dataclass
class TestReport:
    __test__ = False  # not a pytest test class

    status: str
    exit_code: int | None
    timed_out: bool
    duration_s: float
    passed: int | None = None
    failed: int | None = None
    errors: int | None = None
    skipped: int | None = None
    total: int | None = None
    failing: list[str] = field(default_factory=list)
    framework: str | None = None
    results_source: str | None = None
    missing_module: str | None = None
    output_tail: str = ""
    log: str = ""

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("log")
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TestReport:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


def repo_modules(files: Sequence[str]) -> set[str]:
    """Top-level importable names provided by the repository itself (Python layout)."""
    names: set[str] = set()
    for rel in files:
        parts = PurePosixPath(rel).parts
        if not rel.endswith(".py"):
            continue
        if parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        names.add(parts[0][:-3] if len(parts) == 1 else parts[0])
    return names


def find_missing_dependency(output: str, known_files: Sequence[str]) -> str | None:
    own = repo_modules(known_files)
    for pattern in _MISSING:
        for match in pattern.finditer(output):
            name = match.group(1)
            if name.startswith((".", "/")) or re.match(r"^[A-Za-z]:[\\/]", name):
                continue  # a relative/absolute path: the repository's own file, not a dependency
            if name.split(".")[0].split("/")[0] in own:
                continue
            return name
    return None


def effective_framework(config: RepoConfig) -> str:
    if config.test_framework != "auto":
        return config.test_framework
    return guess_framework(config.test) or "auto"


def _report_from(res: ExecResult, status: str) -> TestReport:
    return TestReport(
        status=status,
        exit_code=res.exit_code,
        timed_out=res.timed_out,
        duration_s=round(res.duration_s, 3),
        output_tail=res.output[-4000:],
        log=res.output,
    )


def run_tests(
    runner: Runner,
    env: EnvInfo,
    config: RepoConfig,
    tree: Path,
    outdir: Path,
    *,
    label: str,
    known_files: Sequence[str],
) -> TestReport:
    outdir.mkdir(parents=True, exist_ok=True)
    runner.attach_dependencies(env, tree, config)
    framework = effective_framework(config)
    log_parts: list[str] = []

    for cmd in config.build:
        res = runner.execute(
            env, list(cmd), workdir=tree, outdir=outdir, network=False, timeout_s=config.timeout_s, config=config
        )
        log_parts.append(f"$ {' '.join(cmd)}\n{res.output}\n")
        if res.exit_code != 0:
            (outdir / f"{label}.log").write_text("".join(log_parts), encoding="utf-8")
            if res.timed_out:
                status = TIMEOUT
            elif res.launch_error:
                status = DEPENDENCY_MISSING
            else:
                status = BUILD_ERROR
            report = _report_from(res, status)
            report.framework = framework
            report.log = "".join(log_parts)
            if status == DEPENDENCY_MISSING:
                report.missing_module = cmd[0]
            return report

    paths = runner.paths(tree, outdir, None)
    argv = list(config.test)
    junit_host = outdir / f"{label}-junit.xml"
    junit_host.unlink(missing_ok=True)
    if framework == "pytest":
        argv += ["-p", "no:cacheprovider", f"--junitxml={paths.output}/{label}-junit.xml"]
    res = runner.execute(
        env, argv, workdir=tree, outdir=outdir, network=False, timeout_s=config.timeout_s, config=config
    )
    log_parts.append(res.output)
    full_log = "".join(log_parts)
    (outdir / f"{label}.log").write_text(full_log, encoding="utf-8")
    report = _report_from(res, ERROR)
    report.log = full_log
    report.framework = framework

    counts: Counts | None
    if framework == "pytest":
        counts = parse_junit_file(junit_host)
        if counts is not None:
            counts.source = "pytest junit"
    else:
        counts = extract(framework, res.output, tree, config.junit)
    if counts is not None:
        report.passed, report.failed = counts.passed, counts.failed
        report.errors, report.skipped, report.total = counts.errors, counts.skipped, counts.total
        report.failing = counts.failing[:200]
        report.results_source = counts.source

    report.status = classify(res, report, framework, known_files)
    if report.status == DEPENDENCY_MISSING:
        report.missing_module = find_missing_dependency(res.output, known_files) or (
            argv[0] if res.launch_error else None
        )
    return report


def classify(res: ExecResult, report: TestReport, framework: str, known_files: Sequence[str]) -> str:
    if res.timed_out:
        return TIMEOUT
    if res.launch_error is not None or res.exit_code is None:
        return DEPENDENCY_MISSING if res.launch_error else SETUP_ERROR
    if res.exit_code != 0 and find_missing_dependency(res.output, known_files):
        return DEPENDENCY_MISSING
    if framework == "generic":
        return PASSED if res.exit_code == 0 else FAILED
    if framework == "pytest":
        if res.exit_code == 2:
            return COLLECTION_ERROR
        if res.exit_code == 5:
            return NO_TESTS
    if report.total is None:
        # No recognisable results: a zero exit still means success, anything else is not a test failure.
        return PASSED if res.exit_code == 0 else NO_RESULTS
    if report.total == 0:
        return NO_TESTS if res.exit_code in (0, 5) else NO_RESULTS
    if (report.failed or 0) + (report.errors or 0) > 0:
        return FAILED
    return PASSED if res.exit_code == 0 else ERROR
