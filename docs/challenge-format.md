# Challenge format

## `repoboss.toml`

`repoboss detect --repo PATH` proposes this file for common project types. Without a config,
`challenge create` auto-detects one and saves it under `.repoboss/configs/`.

Commands are **argv lists**: they are never passed through a shell. A leading `"python"` is
replaced by the environment's interpreter. On Windows, the local runner resolves `npm`, `mvn`,
`./gradlew` and similar to their `.cmd`/`.bat`/`.exe` files.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | folder name | Used in challenge names (`name:transformation`) |
| `install` | list of argv lists | `[]` | Dependency installation, once per environment, network allowed |
| `build` | list of argv lists | `[]` | Run in every fresh tree before the tests (e.g. CMake configure + build). A failure is a **build error**, never a test failure |
| `test` | argv list | required | Test command, run with the network disabled (Docker) |
| `test_framework` | string | `"auto"` | `auto`, `pytest`, `unittest`, `jest`, `vitest`, `node`, `mocha`, `ava`, `cargo`, `go`, `maven`, `gradle`, `junit`, `dotnet`, `rspec`, `minitest`, `phpunit`, `ctest`, `dart`, `mix`, `generic` |
| `junit` | globs | `[]` | JUnit XML reports written by the test command (searched in the tree) |
| `hidden` | globs | common test locations for all languages | Removed from the agent's copy, restored for evaluation |
| `protected` | globs | build/test configuration files | Visible to the agent but read-only. `repoboss.toml` is always added |
| `editable` | globs | `["**"]` | Only changes matching these are transferred; others are reported as ignored |
| `source` | globs | `["**"]` | Where bugs may be injected (hidden and protected files are always excluded) |
| `dependency_dirs` | paths | `[]` | Directories produced by `install` that tests need (`node_modules`, `vendor`) |
| `env` | table | `{}` | Extra environment variables; `{cache}` expands to a per-environment cache directory |
| `timeout_s` | number | `300` | Limit for each build/test command |
| `prepare_timeout_s` | number | `1800` | Limit for each install command |
| `docker_image` | string | `python:3.12-slim` | Base image for the Docker runner |

Changing a hidden or a protected file rejects the attempt. Matching is case-insensitive. Globs use
`fnmatch` semantics on POSIX relative paths: `*` also matches `/`, and `dir/**` matches everything
under `dir`.

### Examples

```toml
# JavaScript (node:test, no dependencies)
[repoboss]
install = []
test = ["node", "--test"]
test_framework = "node"
editable = ["src/**"]

# JavaScript with npm dependencies (jest)
[repoboss]
install = [["npm", "ci"]]
test = ["npm", "test"]
test_framework = "jest"
dependency_dirs = ["node_modules"]

# Rust: share compiled dependencies between trees
[repoboss]
test = ["cargo", "test"]
test_framework = "cargo"
env = { CARGO_TARGET_DIR = "{cache}/target" }

# C with CMake + CTest
[repoboss]
build = [["cmake", "-S", ".", "-B", "build", "-G", "Ninja"], ["cmake", "--build", "build"]]
test = ["ctest", "--test-dir", "build", "--output-on-failure"]
test_framework = "ctest"

# .NET
[repoboss]
install = [["dotnet", "restore", "App.Tests/App.Tests.csproj"]]
test = ["dotnet", "test", "App.Tests/App.Tests.csproj"]
test_framework = "dotnet"

# Java (Maven) with JUnit reports
[repoboss]
install = [["mvn", "-B", "-q", "dependency:go-offline"]]
test = ["mvn", "-B", "test"]
test_framework = "maven"
junit = ["target/surefire-reports/*.xml"]
```

The repository's `examples/` folder has complete, tested projects for Python, JavaScript, Rust, C#
and C.

**Python: installing your own project.** Prefer an editable install (`pip install -e .`) or pytest's
`pythonpath`. A non-editable `pip install .` copies the original source into `site-packages`, where
tests would import it instead of the agent's fix.

## Supported languages

**Bug injection:**
- Python uses the standard `ast` module.
- Other languages use tree-sitter grammars (package `tree-sitter-language-pack`):
  - **Languages:** JavaScript, TypeScript/TSX, Java, Kotlin, Scala, Rust, Go, C, C++, C#, Ruby,
    PHP, Swift, Dart, Lua, Elixir and Zig.
  - **Grammars:** downloaded on first use and cached. `repoboss languages --download` fetches them
    ahead of time.

**Verified so far:**

| Level | Languages |
|---|---|
| End to end with real toolchains (`tests/test_multilang_e2e.py`) | Python, JavaScript, Rust, C#, C |
| Bug injection unit tests | TypeScript, Java, Go, Ruby, PHP, Kotlin |

**Test result recognition:**
- Any language whose framework writes JUnit XML.
- Built-in parsers for the frameworks listed under `test_framework`.

**Files with inline tests are never used for bugs**, because the agent could edit those tests. This
covers Rust `#[test]` / `#[cfg(test)]`, Go `func Test`, Elixir `ExUnit.Case` and Zig `test "…"`.

## Snapshot

- **File list:**
  - normally from `git ls-files --cached --others --exclude-standard`, with `GIT_OPTIONAL_LOCKS=0` so
    the index is not rewritten;
  - otherwise from a directory walk.
- **Skipped**, each printed with the reason:
  - noise: `.git`, virtual environments, caches, `node_modules`;
  - symlinks;
  - files over 5 MB;
  - credential-like files: `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.netrc`, `.pypirc`, …
- **Copy and hash:** files are copied byte for byte into `.repoboss/snapshots/<sha256>/tree`. The
  hash is the SHA-256 over sorted `path\0sha256(content)` lines.

## Transformations

Each transformation:
- locates its site on the syntax tree;
- checks explicit preconditions;
- records an **edit**: file, character span, old and new text, file SHA-256 before and after.

Applying an edit is refused when the file hash or the span differ. The inverse swaps old and new.
No transformation does search-and-replace on text.

| Name | Site | Mutation |
|---|---|---|
| `config_value` | A numeric or boolean constant declared as `UPPER_CASE` (any language), a `const` at top level (JS/TS/Go…) or a C `#define NAME number` | int ±1 or ×2; float ×2, ÷2 or +1; bool negated; type suffixes kept (`10u32`, `0.5f`, `4.9m`) |
| `path_reference` | A plain string literal naming an existing non-code file of the repository | A plausible wrong name that does not exist (`catalogs.json`, `catalo.json`) |
| `logic_condition` | A comparison or boolean operator inside a function | `<`↔`<=`, `>`↔`>=`, `==`↔`!=`, `===`↔`!==`, `&&`↔`\|\|`, `and`↔`or` |

**Excluded sites:**
- strings with escapes or interpolation (`${}`, `#{}`, `$x`), and prefixed strings (raw, verbatim);
- imports of code files;
- generic type arguments (`List<String>`);
- constant-vs-constant comparisons;
- syntax error regions.

Candidates are ordered by `random.Random(seed)`, and mutated values come from seeded generators.
The same snapshot and seed give the same challenge.

## Acceptance

Each step runs in a fresh copy of the snapshot. The `build` steps run first, then the test command.

| Step | Required status |
|---|---|
| original | `passed` |
| with the bug | `failed`: test results were recognised and at least one test failed |
| inverse applied | byte-identical to the snapshot (in a fresh copy), then `passed` |

### Statuses of a test run

| Status | Meaning |
|---|---|
| `passed` | Exit 0 |
| `failed` | Recognised results with at least one failure or error |
| `build_error` | A `build` step failed |
| `no_results` | Non-zero exit without recognisable test results, typically a compile error |
| `collection_error` | pytest could not collect the tests |
| `dependency_missing` | A package or tool is not available (`No module named`, `Cannot find module`, command not found, pip/npm/NuGet download errors…) |
| `timeout` | The run exceeded its time limit |
| `setup_error` | Environment preparation or launch failed |
| `no_tests` | No tests ran |

### Rejection reasons for a candidate bug

| Reason | Meaning |
|---|---|
| `bug_not_detected` | The tests still pass with the bug |
| `bug_breaks_build` | The bug causes a compile error or unrecognisable output |
| `bug_breaks_collection` | The bug makes pytest fail before running tests |
| `restore_fails` | The inverse edit does not restore the original or its passing tests |
| `not_applicable` | The bug site is hidden or protected |
| `timeout` | The buggy version timed out |

A compile error points straight at the bug, so it does not count as a valid challenge.

## Agent workspace and evaluation

1. **Baseline.** The snapshot plus the bug, minus hidden files. Protected files are present and
   read-only.
2. **The attempt.** The agent works on a copy of the baseline. Installed dependency directories are
   linked in (local runner) or come from the image (Docker).
3. **Collecting changes.** RepoBoss walks the copy, never follows links, and skips dependency
   directories and caches. Each change falls into one of three groups:
   - **violation**: rejects the attempt and nothing is evaluated;
     - a hidden or protected path changed;
     - any symlink, junction or special file appears;
     - a changed file is over 2 MB;
   - **ignored**: reported only;
     - the path is outside `editable`;
     - or it is a new file in a new build-output directory (`bin/`, `obj/`, `target/`, `build/`,
       `dist/`…);
   - **allowed**: everything else.
4. **Evaluation tree.** Pristine snapshot (hidden tests included), plus the bug, plus the allowed
   changes. The build steps and the tests then run with the network disabled (Docker).

**Outcome of a run:**

| Outcome | When |
|---|---|
| `passed` | The evaluation passed |
| `failed` | Tests failed, or the code no longer builds or runs |
| `rejected` | A violation was found |
| `timeout` | The evaluation timed out |
| `infra_error` | A problem with the environment |

The evaluation status keeps the detail.

`matches_reference` records whether the bug file equals the original. It is descriptive only: a
different correct fix is still a pass.

## Package format (`repoboss export`)

```
challenge.zip
├── manifest.json        {"format": "repoboss-challenge", "format_version": 1, "challenge": {...},
│                         "files": [{"path", "sha256", "size"}], ...}
└── snapshot/<every snapshot file>
```

The manifest contains the edit, so **the package reveals the fix: never give it to the agent being
evaluated.**

**Import rejects:**
- absolute paths, `..`, backslashes and drive letters;
- symlink entries and encrypted entries;
- duplicate or case-colliding names;
- undeclared or missing files;
- hash mismatches, and a recomputed snapshot hash that differs;
- an unsupported `format_version`;
- invalid configuration, such as shell strings;
- a bug placed in a hidden or protected file;
- oversized archives.

Files are written one at a time. Imported challenges stay untrusted: `run` refuses them, and
`challenge verify ID` needs `--trust`.
