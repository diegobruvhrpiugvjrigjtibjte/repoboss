# RepoBoss: notes for agents and contributors

RepoBoss is an open source tool (MIT). It creates verifiable code-repair challenges from a user's
repository, in many languages, and evaluates real AI coding agents on them. Write code and docs in
English; the maintainer (Diego) talks in Italian.

## Map (`src/repoboss/`)

| Module | Role |
|---|---|
| `cli.py` | Typer app. Commands: doctor, init, detect, languages, challenge {create, verify, list, show}, run, runs {list, show}, compare, export, import, dashboard, version |
| `config.py` | `repoboss.toml`: install / build / test argv lists, `test_framework`, `hidden` (tests, removed from the agent copy) vs `protected` (read-only config), editable, source, junit, dependency_dirs, env (`{cache}`), timeouts, docker_image; `render_config` |
| `detect.py` | Project-type detection: node (npm/pnpm/yarn, jest/vitest/mocha/ava/node:test), python, rust, go, dotnet, maven, gradle, ruby, php, cmake, elixir, dart |
| `snapshot.py` | Read-only, content-addressed copy of the repo (git ls-files with GIT_OPTIONAL_LOCKS=0, or a walk); skips noise, links, secrets |
| `transforms.py` | `Edit` (exact span plus sha256 before/after, exact inverse). Python transforms via `ast`; dispatches other languages to `polyglot.py`; `path_variant` |
| `polyglot.py` | tree-sitter (`tree-sitter-language-pack`) transforms for about 17 languages. Inline-test files skipped; generics, interpolated strings and code imports excluded |
| `results.py` | JUnit XML plus output parsers for pytest/unittest/jest/vitest/node:test/mocha/ava/cargo/go/maven/gradle/dotnet/rspec/minitest/phpunit/ctest/dart/mix |
| `testing.py` | Runs build steps and tests in a tree. Statuses: passed / failed / build_error / no_results / collection_error / dependency_missing / timeout / setup_error / no_tests / error |
| `challenge.py` | Acceptance: original passes, bug → `failed`, inverse (in a fresh copy) is byte-identical and passes. `create_challenges(per_transform=…)`, `verify_challenge` |
| `runners.py` | `LocalRunner`: env allowlist, argv[0] resolution (npm.cmd, ./gradlew.bat), dependency dirs linked with junctions/symlinks, `{cache}` dir. `DockerRunner`: image per env key, every non-dependency file deleted after install, `docker create` + tar copy-in/out (no bind mounts), `extract_tar_safely` |
| `procs.py` | Subprocess with timeout, process-tree kill, head+tail capped log |
| `agents.py` | Demo adapters, `CommandAgent` (type = "command"), `LLMAgent` (type = "llm"); agents.toml parsing |
| `llm_agent.py` | Tool-using agent loop: Ollama `/api/chat` and OpenAI-compatible `/chat/completions`. Tools: list_files, read_file, search, replace_in_file, write_file, run (opt-in), finish. Path-guarded ToolBox, transcript, measured tokens |
| `evaluation.py` | Agent baseline (bug, no hidden files); changes classified as violations / ignored (outside editable, new build-output dirs) / allowed; clean evaluation tree |
| `run.py` | One attempt end to end; `run_outcome` maps evaluation status to passed / failed / rejected / timeout / infra_error |
| `compare.py`, `packaging.py`, `workspace.py`, `dashboard/` | Comparison, export/import, SQLite and paths, read-only UI |

`examples/` holds the same small "shopcalc" project in Python, JavaScript (node:test), Rust (cargo,
tests in tests/), C# (.NET 8 + xUnit) and C (CMake + CTest), each with 13 real tests. It also has
`agents/skeleton_agent.py`.

## Commands

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
.venv/Scripts/ruff.exe check src tests && .venv/Scripts/ruff.exe format --check src tests
.venv/Scripts/mypy.exe            # strict, src only
.venv/Scripts/python.exe -m pytest -q -rs     # e2e language tests skip when a toolchain is missing
```

## Decisions

- **Commands are argv lists everywhere.** Never a shell, no interpolation of untrusted text.
- **Only recognised test failures prove a detected bug.** Compile errors, unknown output, pytest
  collection errors, timeouts and missing dependencies are rejected.
- **Hidden vs protected.** Tests are hidden from the agent; build/test configuration is visible but
  read-only. Both reject the attempt if changed, matched case-insensitively.
- **The install context is the whole snapshot**, so test projects can restore their own packages.
  The Docker image deletes every non-dependency file after install.
- **Docker uses no bind mounts**: `docker create`, then `docker cp` tar in and out. Links coming
  back are reported as violations.
- **Local env on Windows** forces English tool output (`DOTNET_CLI_UI_LANGUAGE=en`, `VSLANG=1033`)
  and disables MSBuild/Roslyn build servers, which otherwise lock temp files. Scratch cleanup is
  best effort (`rmtree_quiet`).
- **Metrics:**
  - tokens come from API responses (LLM agent) or are agent-reported (`usage.json`);
  - cost only when prices are configured;
  - unknown values stay NULL.
- **No native Claude Code or Anthropic integration.** No CLI or API key was available to verify it.
- **Interfaces share one engine.** `gui.py` (Tkinter), the dashboard, `interactive.py` and the CLI.
  The WPF app (`windows/`) and the SwiftUI app (`macos/`) call `repoboss json …` for data and the
  normal CLI for actions, with argv lists only.

## Interfaces

- **Windows (WPF, .NET 8):**
  - build: `dotnet publish windows/RepoBoss.Windows -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -o dist/windows`;
  - screenshot mode: `RepoBossApp.exe --home DIR --command PATH --page Runs --screenshot out.png`.
  - **The exe must never be named `repoboss.exe`.** Windows resolves the `repoboss` command to the app
    folder first, so the app launched itself recursively (a fork bomb, fixed in 0.2.1). Guards now in
    place: own folder excluded, CLI-style args make the app exit, single-instance mutex.
- **macOS (SwiftUI):** `macos/build-app.sh`. Never compiled; it needs a Mac.
- **Desktop (Python):** `repoboss gui [--page NAME]`.
- **Terminal:** `repoboss interactive`.
- **Images:** `assets/src/*.html`, rendered with headless Chrome into `assets/*.png`.

## Verified state (2026-11-17, Windows 11, Python 3.12.10)

- ruff, ruff format and mypy strict: clean.
- Full pytest suite: see README "Status" for the latest counts.
  - Skipped: Docker (no daemon here) and symlink creation (Windows without Developer Mode).
- Real toolchains, three accepted challenges each: Python, Node 24, Rust 1.96, .NET 8 SDK
  (xUnit), gcc 16 + CMake 4.3 + Ninja.
- Real AI agent: qwen3:14b through Ollama, on all 15 example challenges with 2 attempts each.
  Results are in the README.
- OpenAI-compatible provider: verified against Ollama's `/v1` endpoint.

## Not verified / next steps

1. **Docker runner end to end.** No Docker on the development PC. The CI workflow with a Docker job
   is in `ci/github-actions.yml`; move it to `.github/workflows/` to enable it.
2. **Real third-party repositories.** Running their install executes third-party code on the host;
   do it only with the Docker runner.
3. **Not run locally:** Go, Java/Maven/Gradle, Ruby, PHP, Elixir and Dart toolchains were not
   installed. Their parsers and transforms are unit-tested only.
4. **The macOS app** has never been compiled.
