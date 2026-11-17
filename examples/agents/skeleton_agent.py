"""Skeleton of a RepoBoss custom agent (standard library only).

It demonstrates the protocol and makes no attempt to fix anything: replace `solve()` with calls to
your own tool or model. RepoBoss provides:

  REPOBOSS_WORKSPACE   working copy to edit (tests and other protected files are not present)
  REPOBOSS_TASK_FILE   JSON task: instructions, failing test ids, test command, editable globs
  REPOBOSS_OUTPUT_DIR  where you may write usage.json ({"input_tokens", "output_tokens", "cost_usd", "source"})

Exit code 0 means "attempt finished"; the result is decided only by RepoBoss's evaluation.
"""

import json
import os
from pathlib import Path


def solve(workspace: Path, task: dict) -> None:
    """Placeholder: inspect the workspace and print what an agent would start from."""
    files = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*.py"))
    print(f"{len(files)} Python files visible; failing tests: {task.get('failing_tests')}")


def main() -> int:
    workspace = Path(os.environ["REPOBOSS_WORKSPACE"])
    task = json.loads(Path(os.environ["REPOBOSS_TASK_FILE"]).read_text(encoding="utf-8"))
    print(task["instructions"])
    solve(workspace, task)
    # Only write usage.json when the numbers come from a real, measurable source (e.g. an API response).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
