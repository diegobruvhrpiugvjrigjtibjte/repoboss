"""Test-result extraction for many test frameworks.

A run only counts as "tests ran" when a report or a framework summary is recognised. A non-zero
exit code without recognisable results is treated as a build/runner error, never as a test failure:
that is what keeps compile errors from being accepted as "detected bugs".
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from repoboss.util import matches_any

MAX_REPORT_BYTES = 10 * 1024 * 1024
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Conventional JUnit XML locations searched when the framework is auto-detected.
CONVENTIONAL_JUNIT = (
    "target/surefire-reports/*.xml",
    "*/target/surefire-reports/*.xml",
    "build/test-results/*.xml",
    "*/build/test-results/*.xml",
    "junit.xml",
    "test-results.xml",
    "reports/junit*.xml",
)


@dataclass
class Counts:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failing: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text).replace("\r\n", "\n")


# ----------------------------------------------------------------------------------------- JUnit XML


def parse_junit_file(path: Path) -> Counts | None:
    if not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
        return None
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - report written by the test run in a scratch tree
    except ET.ParseError:
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    counts = Counts(source=f"junit:{path.name}")
    for case in root.iter("testcase"):
        ident = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        if case.find("failure") is not None:
            counts.failed += 1
            counts.failing.append(ident)
        elif case.find("error") is not None:
            counts.errors += 1
            counts.failing.append(ident)
        elif case.find("skipped") is not None:
            counts.skipped += 1
        else:
            counts.passed += 1
    return counts


def collect_junit(tree: Path, patterns: Sequence[str]) -> Counts | None:
    if not patterns:
        return None
    total: Counts | None = None
    for path in sorted(tree.rglob("*.xml")):
        rel = path.relative_to(tree).as_posix()
        if "node_modules/" in rel or not matches_any(rel, patterns):
            continue
        parsed = parse_junit_file(path)
        if parsed is None:
            continue
        if total is None:
            total = Counts(source="junit")
        total.passed += parsed.passed
        total.failed += parsed.failed
        total.errors += parsed.errors
        total.skipped += parsed.skipped
        total.failing += parsed.failing
    return total


# ----------------------------------------------------------------------------------------- output parsers


def _ints(match: re.Match[str] | None, *groups: str) -> list[int]:
    """Integer values of numbered groups ("1", "2", ...); missing optional groups count as 0."""
    if match is None:
        return [0 for _ in groups]
    return [int(match.group(int(g)) or 0) for g in groups]


def _last(pattern: str, text: str, flags: int = re.M) -> re.Match[str] | None:
    found = list(re.finditer(pattern, text, flags))
    return found[-1] if found else None


def parse_jest(text: str) -> Counts | None:
    m = _last(r"^Tests:\s+(?P<body>.*\d+ total)\s*$", text)
    if not m:
        return None
    body = m.group("body")

    def n(word: str) -> int:
        mm = re.search(rf"(\d+) {word}", body)
        return int(mm.group(1)) if mm else 0

    c = Counts(passed=n("passed"), failed=n("failed"), skipped=n("skipped") + n("todo"), source="jest")
    c.failing = [x.strip() for x in re.findall(r"^\s*● (.+?)\s*$", text, re.M) if "›" in x or " " in x]
    return c


def parse_vitest(text: str) -> Counts | None:
    m = _last(r"^\s*Tests\s+(?P<body>.+?)\((?P<total>\d+)\)\s*$", text)
    if not m:
        return None
    body = m.group("body")

    def n(word: str) -> int:
        mm = re.search(rf"(\d+) {word}", body)
        return int(mm.group(1)) if mm else 0

    c = Counts(passed=n("passed"), failed=n("failed"), skipped=n("skipped") + n("todo"), source="vitest")
    c.failing = [x.strip() for x in re.findall(r"^\s*(?:×|✗|FAIL)\s+(.+?)\s*$", text, re.M)]
    return c


def parse_node_test(text: str) -> Counts | None:
    stats: dict[str, int] = {}
    for m in re.finditer(r"^[#ℹ]\s*(tests|suites|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*$", text, re.M):
        stats[m.group(1)] = int(m.group(2))
    if "pass" not in stats or "fail" not in stats:
        return None
    c = Counts(
        passed=stats["pass"],
        failed=stats["fail"] + stats.get("cancelled", 0),
        skipped=stats.get("skipped", 0) + stats.get("todo", 0),
        source="node:test",
    )
    names = re.findall(r"^\s*not ok \d+ - (.+?)\s*$", text, re.M)
    names += re.findall(r"^\s*✖ (.+?)\s*(?:\([\d.]+m?s\))?\s*$", text, re.M)
    c.failing = sorted({n for n in names if not n.startswith("failing tests")})
    return c


def parse_mocha(text: str) -> Counts | None:
    passing = _last(r"^\s*(\d+) passing", text)
    if not passing:
        return None
    failing = _last(r"^\s*(\d+) failing", text)
    pending = _last(r"^\s*(\d+) pending", text)
    c = Counts(
        passed=int(passing.group(1)),
        failed=int(failing.group(1)) if failing else 0,
        skipped=int(pending.group(1)) if pending else 0,
        source="mocha",
    )
    c.failing = re.findall(r"^\s+\d+\) (.+?):?\s*$", text, re.M)[: c.failed]
    return c


def parse_ava(text: str) -> Counts | None:
    passed = _last(r"^\s*(\d+) tests? passed", text)
    failed = _last(r"^\s*(\d+) tests? failed", text)
    if not passed and not failed:
        return None
    c = Counts(passed=int(passed.group(1)) if passed else 0, failed=int(failed.group(1)) if failed else 0, source="ava")
    c.failing = re.findall(r"^\s*✘ (?:\[fail\]: )?(.+?)\s*$", text, re.M)
    return c


def parse_cargo(text: str) -> Counts | None:
    found = list(re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text))
    if not found:
        return None
    c = Counts(source="cargo")
    for m in found:
        c.passed += int(m.group(1))
        c.failed += int(m.group(2))
        c.skipped += int(m.group(3))
    c.failing = re.findall(r"^test (\S+) \.\.\. FAILED\s*$", text, re.M)
    return c


def parse_go(text: str) -> Counts | None:
    c = Counts(source="go")
    seen_json = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or "Action" not in event:
            continue
        seen_json = True
        test = event.get("Test")
        if not test:
            continue
        action = event["Action"]
        name = f"{event.get('Package', '')}::{test}"
        if action == "pass":
            c.passed += 1
        elif action == "fail":
            c.failed += 1
            c.failing.append(name)
        elif action == "skip":
            c.skipped += 1
    if seen_json:
        return c
    fails = re.findall(r"^\s*--- FAIL: (\S+)", text, re.M)
    passes = re.findall(r"^\s*--- PASS: (\S+)", text, re.M)
    pkg_ok = re.findall(r"^ok\s+\S+", text, re.M)
    if not fails and not passes and not pkg_ok:
        return None
    c.failed, c.passed, c.failing = len(fails), len(passes), fails
    return c


def parse_maven(text: str) -> Counts | None:
    m = _last(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$", text)
    if not m:
        return None
    run, fail, err, skip = (int(x) for x in m.groups())
    c = Counts(passed=run - fail - err - skip, failed=fail, errors=err, skipped=skip, source="maven")
    c.failing = re.findall(r"^\[ERROR\]\s+(\S+[.#:]\S+)\s", text, re.M)
    return c


def parse_gradle(text: str) -> Counts | None:
    m = _last(r"(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?", text)
    if not m:
        return None
    total, fail, skip = _ints(m, "1", "2", "3")
    c = Counts(passed=total - fail - skip, failed=fail, skipped=skip, source="gradle")
    c.failing = re.findall(r"^(\S+) > (\S+.*) FAILED\s*$", text, re.M) and [
        f"{a} > {b}" for a, b in re.findall(r"^(\S+) > (\S+.*) FAILED\s*$", text, re.M)
    ]
    return c


def parse_dotnet(text: str) -> Counts | None:
    found = list(re.finditer(r"(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+),\s+Skipped:\s+(\d+)", text))
    c = Counts(source="dotnet")
    if found:
        for hit in found:
            c.failed += int(hit.group(1))
            c.passed += int(hit.group(2))
            c.skipped += int(hit.group(3))
    else:
        summary = _last(
            r"total:\s*(\d+),?\s+failed:\s*(\d+),?\s+succeeded:\s*(\d+),?\s+skipped:\s*(\d+)", text, re.M | re.I
        )
        if not summary:
            return None
        c.failed, c.passed, c.skipped = int(summary.group(2)), int(summary.group(3)), int(summary.group(4))
    c.failing = re.findall(r"^\s*Failed (\S+) \[", text, re.M)
    return c


def parse_rspec(text: str) -> Counts | None:
    m = _last(r"^(\d+) examples?, (\d+) failures?(?:, (\d+) pending)?", text)
    if not m:
        return None
    total, fail, pend = _ints(m, "1", "2", "3")
    c = Counts(passed=total - fail - pend, failed=fail, skipped=pend, source="rspec")
    c.failing = re.findall(r"^rspec (\S+) # (.+)$", text, re.M) and [
        f"{a} {b}" for a, b in re.findall(r"^rspec (\S+) # (.+)$", text, re.M)
    ]
    return c


def parse_minitest(text: str) -> Counts | None:
    m = _last(r"(\d+) (?:runs|tests), \d+ assertions, (\d+) failures, (\d+) errors, (\d+) skips", text)
    if not m:
        return None
    total, fail, err, skip = _ints(m, "1", "2", "3", "4")
    return Counts(passed=total - fail - err - skip, failed=fail, errors=err, skipped=skip, source="minitest")


def parse_phpunit(text: str) -> Counts | None:
    ok = _last(r"^OK \((\d+) tests?, \d+ assertions?\)", text)
    if ok:
        return Counts(passed=int(ok.group(1)), source="phpunit")
    m = _last(r"^Tests: (\d+), Assertions: \d+(?P<rest>.*)$", text)
    if not m:
        return None
    rest = m.group("rest")

    def n(word: str) -> int:
        mm = re.search(rf"{word}: (\d+)", rest)
        return int(mm.group(1)) if mm else 0

    total = int(m.group(1))
    fail, err, skip = n("Failures"), n("Errors"), n("Skipped") + n("Incomplete")
    return Counts(passed=total - fail - err - skip, failed=fail, errors=err, skipped=skip, source="phpunit")


def parse_ctest(text: str) -> Counts | None:
    # "92% tests passed, 1 tests failed out of 13"; newer CTest prints "100% tests passed out of 13".
    m = _last(r"(\d+)% tests passed(?:, (\d+) tests? failed)? out of (\d+)", text)
    if not m:
        return None
    fail, total = int(m.group(2) or 0), int(m.group(3))
    c = Counts(passed=total - fail, failed=fail, source="ctest")
    c.failing = re.findall(r"^\s*\d+ - (\S+) \((?:Failed|SEGFAULT|Timeout|Exception)\)", text, re.M)
    return c


def parse_dart(text: str) -> Counts | None:
    m = _last(r"\+(\d+)(?: ~(\d+))?(?: -(\d+))?: (?:All tests passed!|Some tests failed\.)", text)
    if not m:
        return None
    passed, skip, fail = _ints(m, "1", "2", "3")
    return Counts(passed=passed, failed=fail, skipped=skip, source="dart")


def parse_mix(text: str) -> Counts | None:
    m = _last(r"^(\d+) (?:tests?|doctests?(?:, \d+ tests?)?), (\d+) failures?(?:, (\d+) (?:skipped|excluded))?", text)
    if not m:
        return None
    total, fail, skip = _ints(m, "1", "2", "3")
    return Counts(passed=total - fail - skip, failed=fail, skipped=skip, source="mix")


def parse_unittest(text: str) -> Counts | None:
    ran = _last(r"^Ran (\d+) tests? in", text)
    if not ran:
        return None
    total = int(ran.group(1))
    status = _last(r"^(OK|FAILED)(?: \((?P<body>[^)]*)\))?\s*$", text)
    if not status:
        return None
    body = status.group("body") or ""

    def n(word: str) -> int:
        mm = re.search(rf"{word}=(\d+)", body)
        return int(mm.group(1)) if mm else 0

    fail, err, skip = n("failures"), n("errors"), n("skipped")
    c = Counts(passed=total - fail - err - skip, failed=fail, errors=err, skipped=skip, source="unittest")
    c.failing = re.findall(r"^(?:FAIL|ERROR): (\S+ \([^)]*\))", text, re.M)
    return c


def parse_pytest_summary(text: str) -> Counts | None:
    m = _last(r"^=*\s*(?P<body>(?:\d+ \w+(?:, )?)+) in [\d.]+s", text)
    if not m:
        m = _last(r"^(?P<body>(?:\d+ (?:passed|failed|error|errors|skipped|xfailed|xpassed)(?:, )?)+) in [\d.]+s", text)
    if not m:
        return None
    body = m.group("body")

    def n(word: str) -> int:
        mm = re.search(rf"(\d+) {word}", body)
        return int(mm.group(1)) if mm else 0

    c = Counts(passed=n("passed"), failed=n("failed"), errors=n("error"), skipped=n("skipped"), source="pytest")
    c.failing = re.findall(r"^FAILED (\S+)", text, re.M)
    return c


PARSERS: dict[str, Callable[[str], Counts | None]] = {
    "pytest": parse_pytest_summary,
    "unittest": parse_unittest,
    "jest": parse_jest,
    "vitest": parse_vitest,
    "node": parse_node_test,
    "mocha": parse_mocha,
    "ava": parse_ava,
    "cargo": parse_cargo,
    "go": parse_go,
    "maven": parse_maven,
    "gradle": parse_gradle,
    "dotnet": parse_dotnet,
    "rspec": parse_rspec,
    "minitest": parse_minitest,
    "phpunit": parse_phpunit,
    "ctest": parse_ctest,
    "dart": parse_dart,
    "mix": parse_mix,
}


def guess_framework(test_argv: Sequence[str]) -> str | None:
    """Best guess from the test command itself (used when test_framework = "auto")."""
    joined = " ".join(test_argv).lower()
    for needle, name in (
        ("pytest", "pytest"),
        ("unittest", "unittest"),
        ("vitest", "vitest"),
        ("jest", "jest"),
        ("mocha", "mocha"),
        ("ava", "ava"),
        ("node --test", "node"),
        ("cargo", "cargo"),
        ("go test", "go"),
        ("mvn", "maven"),
        ("gradle", "gradle"),
        ("dotnet", "dotnet"),
        ("rspec", "rspec"),
        ("phpunit", "phpunit"),
        ("ctest", "ctest"),
        ("dart test", "dart"),
        ("flutter test", "dart"),
        ("mix test", "mix"),
    ):
        if needle in joined:
            return name
    return None


def extract(framework: str, output: str, tree: Path, junit_patterns: Sequence[str]) -> Counts | None:
    """Counts from JUnit reports if configured/found, else from the framework's output summary."""
    text = strip_ansi(output)
    patterns = list(junit_patterns)
    if framework in ("auto", "maven", "gradle", "junit"):
        patterns += CONVENTIONAL_JUNIT
    junit = collect_junit(tree, patterns)
    if junit is not None and junit.total > 0:
        return junit
    if framework in PARSERS:
        return PARSERS[framework](text)
    if framework == "auto":
        for parser in PARSERS.values():
            parsed = parser(text)
            if parsed is not None and parsed.total > 0:
                return parsed
    return None
