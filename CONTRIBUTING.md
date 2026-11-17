# Contributing

Thanks for helping. Keep changes small and verifiable.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Before opening a pull request

```bash
ruff check src tests
ruff format --check src tests
mypy
pytest
```

- If you have Docker, also run `pytest -m docker`.
- Say in the PR which checks you ran and on which OS.

## Ground rules

- **Never invent data.**
  - Metrics that are not measured stay `None` / `NULL`.
  - Don't add compatibility claims or integrations you have not verified against the real tool.
  - Demo adapters must stay clearly labelled.
- **No shell strings.** Commands are argv lists end to end, and configuration values are not
  interpolated into commands.
- **Transformations** need:
  - explicit preconditions;
  - an exact span edit with hash checks;
  - an inverse that restores bytes exactly;
  - tests for both "applies" and "not applicable".
- **Security-relevant changes** need a test that fails without the change: isolation flags, change
  transfer, import validation.
- **Dependencies:** keep the runtime list short (`typer`, `fastapi`, `jinja2`, `uvicorn`,
  `tree-sitter-language-pack`).
- **New language or framework:**
  - add a parser in `results.py` with a sample of the tool's real output (`tests/test_results.py`);
  - add a snippet in `tests/test_polyglot.py`;
  - if you can run the toolchain, add an example under `examples/` covered by
    `tests/test_multilang_e2e.py`.
- **LLM providers:** add one only after verifying its protocol against the real service. Test it with
  the scripted HTTP server in `tests/test_llm_agent.py`.

## Layout

See `CLAUDE.md` for the architecture map and design decisions.
