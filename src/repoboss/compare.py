"""Comparisons between runs. Small samples are reported as such, never ranked."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

MIN_ATTEMPTS_FOR_TREND = 10


@dataclass
class AgentStats:
    agent: str
    version: str | None
    runner: str
    is_demo: bool
    attempts: int
    passed: int
    rejected: int
    median_duration_s: float | None
    outcomes: dict[str, int]

    @property
    def success_rate(self) -> float:
        return self.passed / self.attempts if self.attempts else 0.0


def sample_note(attempts: int) -> str | None:
    if attempts < MIN_ATTEMPTS_FOR_TREND:
        return f"{attempts} attempt(s): too few to draw statistical conclusions; treat percentages as descriptive only."
    return None


def aggregate(runs: Sequence[dict[str, Any]]) -> list[AgentStats]:
    groups: dict[tuple[str, str | None, str], list[dict[str, Any]]] = {}
    for run in runs:
        if run.get("outcome") == "running":
            continue
        groups.setdefault((run["agent_name"], run["agent_version"], run["runner"]), []).append(run)
    stats: list[AgentStats] = []
    for (agent, version, runner), items in sorted(groups.items(), key=lambda kv: kv[0][0]):
        durations = [r["total_duration_s"] for r in items if r.get("total_duration_s") is not None]
        outcomes: dict[str, int] = {}
        for r in items:
            outcomes[r["outcome"] or "unknown"] = outcomes.get(r["outcome"] or "unknown", 0) + 1
        stats.append(
            AgentStats(
                agent=agent,
                version=version,
                runner=runner,
                is_demo=bool(items[0]["agent_is_demo"]),
                attempts=len(items),
                passed=outcomes.get("passed", 0),
                rejected=outcomes.get("rejected", 0),
                median_duration_s=round(statistics.median(durations), 2) if durations else None,
                outcomes=outcomes,
            )
        )
    return stats


COMPARE_FIELDS = (
    ("agent_name", "Agent"),
    ("agent_version", "Version"),
    ("agent_is_demo", "Demo adapter"),
    ("runner", "Runner"),
    ("runner_isolated", "Isolated"),
    ("network", "Network"),
    ("outcome", "Outcome"),
    ("attempt_status", "Attempt"),
    ("attempt_exit_code", "Agent exit code"),
    ("eval_status", "Evaluation"),
    ("tests_passed", "Tests passed"),
    ("tests_failed", "Tests failed"),
    ("tests_errors", "Test errors"),
    ("tests_total", "Tests total"),
    ("files_changed", "Files changed"),
    ("lines_added", "Lines added"),
    ("lines_removed", "Lines removed"),
    ("matches_reference", "Matches reference fix"),
    ("attempt_duration_s", "Attempt duration (s)"),
    ("total_duration_s", "Total duration (s)"),
    ("input_tokens", "Input tokens"),
    ("output_tokens", "Output tokens"),
    ("cost_usd", "Cost (USD)"),
    ("usage_source", "Usage source"),
)


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def comparison_rows(runs: Sequence[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    for key, label in COMPARE_FIELDS:
        values = []
        for r in runs:
            v = r.get(key)
            if key in ("agent_is_demo", "runner_isolated", "network", "matches_reference") and v is not None:
                v = "yes" if v else "no"
            values.append(fmt(v))
        rows.append((label, values))
    return rows
