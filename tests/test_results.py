"""Framework output parsers. Samples follow each tool's real output format."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoboss.results import PARSERS, extract, guess_framework, parse_junit_file, strip_ansi

SAMPLES: dict[str, tuple[str, tuple[int, int, int]]] = {
    "jest": (
        "  ● pricing › bulk discount\n\n    expect(received).toBe(expected)\n\n"
        "Test Suites: 1 failed, 1 total\nTests:       2 failed, 1 skipped, 10 passed, 13 total\n",
        (10, 2, 1),
    ),
    "vitest": (
        " × pricing > bulk discount 3ms\n\n Test Files  1 failed (1)\n      Tests  2 failed | 11 passed (13)\n",
        (11, 2, 0),
    ),
    "node": (
        "✖ bulk discount (1.2ms)\nℹ tests 13\nℹ suites 0\nℹ pass 11\nℹ fail 2\nℹ cancelled 0\nℹ skipped 0\nℹ todo 0\n",
        (11, 2, 0),
    ),
    "mocha": ("  11 passing (20ms)\n  2 failing\n\n  1) pricing\n       bulk:\n", (11, 2, 0)),
    "ava": ("  ✘ bulk discount\n\n  2 tests failed\n  11 tests passed\n", (11, 2, 0)),
    "cargo": (
        "test bulk ... FAILED\ntest result: FAILED. 11 passed; 2 failed; 1 ignored; 0 measured\n"
        "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured\n",
        (14, 2, 1),
    ),
    "go": (
        '{"Action":"pass","Package":"p","Test":"TestA"}\n{"Action":"fail","Package":"p","Test":"TestB"}\n'
        '{"Action":"skip","Package":"p","Test":"TestC"}\n{"Action":"fail","Package":"p"}\n',
        (1, 1, 1),
    ),
    "maven": ("[ERROR] Tests run: 13, Failures: 2, Errors: 1, Skipped: 1\n", (9, 2, 1)),
    "gradle": ("PricingTest > bulk() FAILED\n13 tests completed, 2 failed, 1 skipped\n", (10, 2, 1)),
    "dotnet": (
        "  Failed PricingTests.Bulk [3 ms]\n"
        "Failed!  - Failed:     2, Passed:    11, Skipped:     0, Total:    13, Duration: 40 ms\n",
        (11, 2, 0),
    ),
    "rspec": ("13 examples, 2 failures, 1 pending\n\nrspec ./spec/p_spec.rb:4 # bulk\n", (10, 2, 1)),
    "minitest": ("13 runs, 20 assertions, 2 failures, 0 errors, 1 skips\n", (10, 2, 1)),
    "phpunit": ("FAILURES!\nTests: 13, Assertions: 20, Failures: 2, Skipped: 1.\n", (10, 2, 1)),
    "ctest": ("      7 - bulk (Failed)\n85% tests passed, 2 tests failed out of 13\n", (11, 2, 0)),
    "dart": ("00:01 +11 -2: Some tests failed.\n", (11, 2, 0)),
    "mix": ("13 tests, 2 failures, 1 excluded\n", (10, 2, 1)),
    "unittest": ("FAIL: test_bulk (test_p.T)\nRan 13 tests in 0.01s\n\nFAILED (failures=2, skipped=1)\n", (10, 2, 1)),
    "pytest": (
        "FAILED tests/test_p.py::test_bulk - assert 1 == 2\n======= 2 failed, 11 passed in 0.12s =======\n",
        (11, 2, 0),
    ),
}


@pytest.mark.parametrize("framework", sorted(SAMPLES))
def test_framework_parsers(framework: str) -> None:
    text, (passed, failed, skipped) = SAMPLES[framework]
    counts = PARSERS[framework](text)
    assert counts is not None, framework
    assert (counts.passed, counts.failed, counts.skipped) == (passed, failed, skipped)


def test_parsers_do_not_claim_unrelated_output() -> None:
    compile_error = "error[E0425]: cannot find value `tax_amount` in this scope\nerror: could not compile `shopcalc`\n"
    for name, parser in PARSERS.items():
        assert parser(compile_error) is None, name


def test_node_tap_format_and_failing_names() -> None:
    tap = "not ok 3 - bulk discount\n# tests 13\n# pass 12\n# fail 1\n"
    counts = PARSERS["node"](tap)
    assert counts is not None and counts.failed == 1 and counts.failing == ["bulk discount"]


def test_dotnet_new_summary_format() -> None:
    counts = PARSERS["dotnet"]("Test summary: total: 13, failed: 2, succeeded: 11, skipped: 0, duration: 1s\n")
    assert counts is not None and (counts.passed, counts.failed) == (11, 2)


def test_ansi_is_stripped() -> None:
    assert strip_ansi("\x1b[31mTests:\x1b[0m 1 failed") == "Tests: 1 failed"


def test_junit_reports(tmp_path: Path) -> None:
    report = tmp_path / "target" / "surefire-reports" / "TEST-Pricing.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        '<testsuite name="Pricing"><testcase classname="Pricing" name="ok"/>'
        '<testcase classname="Pricing" name="bad"><failure message="x"/></testcase>'
        '<testcase classname="Pricing" name="boom"><error/></testcase>'
        '<testcase classname="Pricing" name="skip"><skipped/></testcase></testsuite>',
        encoding="utf-8",
    )
    counts = parse_junit_file(report)
    assert counts is not None and (counts.passed, counts.failed, counts.errors, counts.skipped) == (1, 1, 1, 1)
    assert counts.failing == ["Pricing::bad", "Pricing::boom"]
    # Conventional locations are found automatically for maven/gradle/auto.
    found = extract("maven", "", tmp_path, [])
    assert found is not None and found.total == 4


def test_guess_framework() -> None:
    assert guess_framework(["python", "-m", "pytest"]) == "pytest"
    assert guess_framework(["npx", "vitest", "run"]) == "vitest"
    assert guess_framework(["node", "--test"]) == "node"
    assert guess_framework(["cargo", "test"]) == "cargo"
    assert guess_framework(["make", "check"]) is None


def test_auto_detects_from_output(tmp_path: Path) -> None:
    counts = extract("auto", SAMPLES["cargo"][0], tmp_path, [])
    assert counts is not None and counts.source == "cargo"
