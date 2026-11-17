"""Per-repository configuration (``repoboss.toml``)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from repoboss.util import check_relpath

CONFIG_FILE = "repoboss.toml"
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

FRAMEWORKS = (
    "auto",
    "pytest",
    "unittest",
    "jest",
    "vitest",
    "node",
    "mocha",
    "ava",
    "cargo",
    "go",
    "maven",
    "gradle",
    "junit",
    "dotnet",
    "rspec",
    "minitest",
    "phpunit",
    "ctest",
    "dart",
    "mix",
    "generic",
)


def _anywhere(*names: str) -> tuple[str, ...]:
    out: list[str] = []
    for name in names:
        out += [name, f"*/{name}"]
    return tuple(out)


# Test code: removed from the agent's working copy and restored for evaluation.
DEFAULT_HIDDEN = (
    *_anywhere("tests/*", "test/*", "spec/*", "__tests__/*", "conftest.py"),
    "tests",
    "test",
    "spec",
    "*.test.*",
    "*.spec.*",
    "*_test.go",
    "*_test.py",
    "test_*.py",
    "*/test_*.py",
    "*_spec.rb",
    "*Test.java",
    "*Tests.java",
    "*Test.kt",
    "*Tests.kt",
    "*Test.php",
    "*.Tests/*",
    "*.Test/*",
    "*Tests.cs",
    "*_test.exs",
    "*_test.dart",
)

# Build and test configuration: visible to the agent but read-only.
DEFAULT_PROTECTED = (
    CONFIG_FILE,
    *_anywhere(
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "pytest.ini",
        "noxfile.py",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "jest.config.*",
        "vitest.config.*",
        "vite.config.*",
        ".mocharc*",
        "babel.config.*",
        "tsconfig*.json",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "pom.xml",
        "build.gradle*",
        "settings.gradle*",
        "gradle.properties",
        "*.csproj",
        "*.fsproj",
        "*.sln",
        "Directory.Build.*",
        "Gemfile",
        "Gemfile.lock",
        "*.gemspec",
        "Rakefile",
        "composer.json",
        "composer.lock",
        "phpunit.xml*",
        "CMakeLists.txt",
        "*.cmake",
        "Makefile",
        "mix.exs",
        "mix.lock",
        "pubspec.yaml",
        "pubspec.lock",
    ),
    ".github/*",
)


class ConfigError(Exception):
    """Raised when a repoboss.toml is missing or invalid."""


@dataclass(frozen=True)
class RepoConfig:
    name: str
    install: tuple[tuple[str, ...], ...]
    test: tuple[str, ...]
    build: tuple[tuple[str, ...], ...] = ()
    test_framework: str = "auto"
    hidden: tuple[str, ...] = DEFAULT_HIDDEN
    protected: tuple[str, ...] = DEFAULT_PROTECTED
    editable: tuple[str, ...] = ("**",)
    source: tuple[str, ...] = ("**",)
    junit: tuple[str, ...] = ()
    dependency_dirs: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 300.0
    prepare_timeout_s: float = 1800.0
    docker_image: str = "python:3.12-slim"

    @property
    def locked(self) -> tuple[str, ...]:
        """Paths the agent must not change: hidden (absent from its copy) plus protected (read-only)."""
        return (*self.hidden, *self.protected)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["install"] = [list(cmd) for cmd in self.install]
        data["build"] = [list(cmd) for cmd in self.build]
        for key in ("test", "hidden", "protected", "editable", "source", "junit", "dependency_dirs"):
            data[key] = list(data[key])
        data["env"] = dict(self.env)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_name: str = "repo") -> RepoConfig:
        return _parse(data, default_name)


def _str_list(value: Any, field_name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"'{field_name}' must be a list of non-empty strings")
    if not allow_empty and not value:
        raise ConfigError(f"'{field_name}' must not be empty")
    return tuple(value)


def _patterns(value: Any, field_name: str) -> tuple[str, ...]:
    items = _str_list(value, field_name)
    for item in items:
        if "\\" in item or item.startswith("/") or ".." in item.split("/"):
            raise ConfigError(f"'{field_name}' entry {item!r} must be a relative POSIX glob without '..'")
    return items


KNOWN_KEYS = {
    "name",
    "install",
    "build",
    "test",
    "test_framework",
    "hidden",
    "protected",
    "editable",
    "source",
    "junit",
    "dependency_dirs",
    "env",
    "timeout_s",
    "prepare_timeout_s",
    "docker_image",
}


def _parse(data: dict[str, Any], default_name: str) -> RepoConfig:
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise ConfigError(f"unknown keys in [repoboss]: {', '.join(sorted(unknown))}")

    name = data.get("name", default_name)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        raise ConfigError("'name' must be 1-64 characters of letters, digits, '.', '_' or '-'")

    raw_install = data.get("install", [])
    if not isinstance(raw_install, list):
        raise ConfigError("'install' must be a list of commands (each a list of strings)")
    install = tuple(_str_list(cmd, "install[]", allow_empty=False) for cmd in raw_install)
    raw_build = data.get("build", [])
    if not isinstance(raw_build, list):
        raise ConfigError("'build' must be a list of commands (each a list of strings)")
    build = tuple(_str_list(cmd, "build[]", allow_empty=False) for cmd in raw_build)

    if "test" not in data:
        raise ConfigError('\'test\' is required, e.g. test = ["python", "-m", "pytest", "-q"]')
    test = _str_list(data["test"], "test", allow_empty=False)

    framework = data.get("test_framework", "auto")
    if framework not in FRAMEWORKS:
        raise ConfigError(f"'test_framework' must be one of: {', '.join(FRAMEWORKS)}")

    hidden = _patterns(data.get("hidden", list(DEFAULT_HIDDEN)), "hidden")
    protected = _patterns(data.get("protected", list(DEFAULT_PROTECTED)), "protected")
    if CONFIG_FILE not in protected:
        protected = (*protected, CONFIG_FILE)
    editable = _patterns(data.get("editable", ["**"]), "editable")
    source = _patterns(data.get("source", ["**"]), "source")
    junit = _patterns(data.get("junit", []), "junit")
    deps = _patterns(data.get("dependency_dirs", []), "dependency_dirs")
    for dep in deps:
        if any(ch in dep for ch in "*?["):
            raise ConfigError("'dependency_dirs' entries must be plain relative directory paths")
        check_relpath(dep)

    env = data.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and ENV_NAME_RE.match(k) and isinstance(v, str) for k, v in env.items()
    ):
        raise ConfigError("'env' must be a table of NAME = \"value\" strings")

    def _positive(key: str, default: float) -> float:
        value = data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise ConfigError(f"'{key}' must be a positive number of seconds")
        return float(value)

    image = data.get("docker_image", "python:3.12-slim")
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise ConfigError("'docker_image' must be a plain image reference such as python:3.12-slim")

    return RepoConfig(
        name=name,
        install=install,
        test=test,
        build=build,
        test_framework=framework,
        hidden=hidden,
        protected=protected,
        editable=editable,
        source=source,
        junit=junit,
        dependency_dirs=deps,
        env=dict(env),
        timeout_s=_positive("timeout_s", 300.0),
        prepare_timeout_s=_positive("prepare_timeout_s", 1800.0),
        docker_image=image,
    )


def parse_config_text(text: str, *, origin: str, default_name: str) -> RepoConfig:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{origin}: {exc}") from exc
    table = data.get("repoboss")
    if not isinstance(table, dict):
        raise ConfigError(f"{origin}: missing [repoboss] table")
    return _parse(table, default_name)


def default_name_for(repo: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", repo.resolve().name)[:64] or "repo"


def load_repo_config(repo: Path, config_path: Path | None = None) -> RepoConfig:
    """Load ``config_path`` if given, else ``repo/repoboss.toml``."""
    path = config_path or repo / CONFIG_FILE
    if not path.is_file():
        raise ConfigError(
            f"{path} not found. Run `repoboss detect --repo {repo}` to generate a configuration, "
            "or add a repoboss.toml (see docs/challenge-format.md)."
        )
    return parse_config_text(path.read_text(encoding="utf-8"), origin=str(path), default_name=default_name_for(repo))


def render_config(config: RepoConfig, comment: str = "") -> str:
    """TOML text for a configuration (used by `repoboss detect`)."""

    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def arr(items: tuple[str, ...] | list[str]) -> str:
        return "[" + ", ".join(q(i) for i in items) + "]"

    lines = []
    if comment:
        lines += [f"# {line}" if line else "#" for line in comment.splitlines()]
    lines += [
        "[repoboss]",
        f"name = {q(config.name)}",
        "install = [" + ", ".join(arr(c) for c in config.install) + "]",
        "build = [" + ", ".join(arr(c) for c in config.build) + "]",
        f"test = {arr(config.test)}",
        f"test_framework = {q(config.test_framework)}",
    ]
    if config.junit:
        lines.append(f"junit = {arr(config.junit)}")
    if config.dependency_dirs:
        lines.append(f"dependency_dirs = {arr(config.dependency_dirs)}")
    if config.env:
        lines.append("env = { " + ", ".join(f"{k} = {q(v)}" for k, v in config.env.items()) + " }")
    if config.hidden != DEFAULT_HIDDEN:
        lines.append(f"hidden = {arr(config.hidden)}")
    if config.protected != DEFAULT_PROTECTED:
        lines.append(f"protected = {arr(config.protected)}")
    if config.editable != ("**",):
        lines.append(f"editable = {arr(config.editable)}")
    if config.source != ("**",):
        lines.append(f"source = {arr(config.source)}")
    lines += [
        f"timeout_s = {config.timeout_s:g}",
        f"docker_image = {q(config.docker_image)}",
    ]
    return "\n".join(lines) + "\n"
