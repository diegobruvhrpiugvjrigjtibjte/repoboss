"""Project-type detection: proposes a repoboss.toml for common ecosystems.

The result is a starting point printed for review (``repoboss detect``); nothing is written into
the user's repository.
"""

from __future__ import annotations

import json
import shutil
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from repoboss.config import RepoConfig, default_name_for


@dataclass
class Detection:
    ecosystem: str
    config: RepoConfig
    notes: list[str] = field(default_factory=list)


def _node(repo: Path, name: str) -> Detection | None:
    pkg_path = repo / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except ValueError:
        pkg = {}
    notes: list[str] = []
    if (repo / "pnpm-lock.yaml").is_file():
        install = [["pnpm", "install", "--frozen-lockfile"]]
        runner = ["pnpm", "test"]
    elif (repo / "yarn.lock").is_file():
        install = [["yarn", "install", "--frozen-lockfile"]]
        runner = ["yarn", "test"]
    elif (repo / "package-lock.json").is_file() or (repo / "npm-shrinkwrap.json").is_file():
        install = [["npm", "ci", "--no-audit", "--no-fund"]]
        runner = ["npm", "test"]
    else:
        install = [["npm", "install", "--no-audit", "--no-fund"]]
        runner = ["npm", "test"]
        notes.append("no lockfile: dependency versions may drift between environments")
    scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
    test_script = str(scripts.get("test", "")) if isinstance(scripts, dict) else ""
    deps: dict[str, object] = {}
    for key in ("dependencies", "devDependencies"):
        part = pkg.get(key, {}) if isinstance(pkg, dict) else {}
        if isinstance(part, dict):
            deps.update(part)
    framework = "auto"
    for needle, fw in (
        ("vitest", "vitest"),
        ("jest", "jest"),
        ("mocha", "mocha"),
        ("ava", "ava"),
        ("node --test", "node"),
        ("node:test", "node"),
    ):
        if needle in test_script or needle in deps:
            framework = fw
            break
    if not test_script:
        notes.append('package.json has no "test" script: set `test` by hand')
    if framework == "vitest":
        runner = [*runner, "--", "--run"]
    if not deps and not (repo / "package-lock.json").is_file():
        install = []
    return Detection(
        "node",
        RepoConfig(
            name=name,
            install=tuple(tuple(c) for c in install),
            test=tuple(runner),
            test_framework=framework,
            dependency_dirs=("node_modules",) if install else (),
            docker_image="node:22-slim",
        ),
        notes,
    )


def _python(repo: Path, name: str) -> Detection | None:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini")
    if not any((repo / m).is_file() for m in markers):
        return None
    install: list[list[str]] = []
    notes: list[str] = []
    if (repo / "requirements.txt").is_file():
        install.append(["python", "-m", "pip", "install", "-r", "requirements.txt"])
    for req in ("requirements-dev.txt", "requirements_test.txt", "requirements-test.txt", "test-requirements.txt"):
        if (repo / req).is_file():
            install.append(["python", "-m", "pip", "install", "-r", req])
    extras = ""
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        opt = data.get("project", {}).get("optional-dependencies", {})
        for key in ("test", "tests", "testing", "dev"):
            if key in opt:
                extras = f"[{key}]"
                break
        if "project" in data or ("tool" in data and "poetry" in data.get("tool", {})):
            install.append(["python", "-m", "pip", "install", "-e", f".{extras}"])
    elif (repo / "setup.py").is_file():
        install.append(["python", "-m", "pip", "install", "-e", "."])
    install.append(["python", "-m", "pip", "install", "pytest"])
    notes.append("editable install (-e) keeps tests importing the working copy, not a frozen copy")
    return Detection(
        "python",
        RepoConfig(
            name=name,
            install=tuple(tuple(c) for c in install),
            test=("python", "-m", "pytest", "-q"),
            test_framework="pytest",
            docker_image="python:3.12-slim",
        ),
        notes,
    )


def _rust(repo: Path, name: str) -> Detection | None:
    if not (repo / "Cargo.toml").is_file():
        return None
    return Detection(
        "rust",
        RepoConfig(
            name=name,
            install=(("cargo", "fetch"),),
            test=("cargo", "test"),
            test_framework="cargo",
            env={"CARGO_TARGET_DIR": "{cache}/target", "CARGO_TERM_COLOR": "never"},
            docker_image="rust:1-slim",
        ),
        ["unit tests inside src files (#[cfg(test)]) stay visible: those files are never used for bugs"],
    )


def _go(repo: Path, name: str) -> Detection | None:
    if not (repo / "go.mod").is_file():
        return None
    return Detection(
        "go",
        RepoConfig(
            name=name,
            install=(("go", "mod", "download"),),
            test=("go", "test", "-json", "./..."),
            test_framework="go",
            docker_image="golang:1.23",
        ),
    )


def _dotnet(repo: Path, name: str) -> Detection | None:
    if not any(repo.glob("*.sln")) and not any(repo.glob("*.csproj")) and not any(repo.glob("*/*.csproj")):
        return None
    return Detection(
        "dotnet",
        RepoConfig(
            name=name,
            install=(("dotnet", "restore"),),
            test=("dotnet", "test", "--nologo"),
            test_framework="dotnet",
            docker_image="mcr.microsoft.com/dotnet/sdk:8.0",
        ),
    )


def _maven(repo: Path, name: str) -> Detection | None:
    if not (repo / "pom.xml").is_file():
        return None
    return Detection(
        "java-maven",
        RepoConfig(
            name=name,
            install=(("mvn", "-B", "-q", "dependency:go-offline"),),
            test=("mvn", "-B", "test"),
            test_framework="maven",
            junit=("target/surefire-reports/*.xml", "*/target/surefire-reports/*.xml"),
            docker_image="maven:3-eclipse-temurin-21",
        ),
    )


def _gradle(repo: Path, name: str) -> Detection | None:
    if not any((repo / f).is_file() for f in ("build.gradle", "build.gradle.kts")):
        return None
    wrapper = (repo / "gradlew").is_file()
    cmd = "./gradlew" if wrapper else "gradle"
    return Detection(
        "java-gradle",
        RepoConfig(
            name=name,
            install=((cmd, "testClasses", "--no-daemon"),),
            test=(cmd, "test", "--no-daemon"),
            test_framework="gradle",
            junit=("build/test-results/*.xml", "*/build/test-results/*.xml"),
            docker_image="gradle:8-jdk21",
        ),
        [] if wrapper else ["no gradle wrapper: a system Gradle is required"],
    )


def _ruby(repo: Path, name: str) -> Detection | None:
    if not (repo / "Gemfile").is_file():
        return None
    rspec = (repo / "spec").is_dir() or (repo / ".rspec").is_file()
    test = ("bundle", "exec", "rspec") if rspec else ("bundle", "exec", "rake", "test")
    return Detection(
        "ruby",
        RepoConfig(
            name=name,
            install=(("bundle", "install"),),
            test=test,
            test_framework="rspec" if rspec else "minitest",
            docker_image="ruby:3.3",
        ),
    )


def _php(repo: Path, name: str) -> Detection | None:
    if not (repo / "composer.json").is_file():
        return None
    return Detection(
        "php",
        RepoConfig(
            name=name,
            install=(("composer", "install", "--no-interaction", "--no-progress"),),
            test=("vendor/bin/phpunit",),
            test_framework="phpunit",
            dependency_dirs=("vendor",),
            docker_image="composer:2",
        ),
    )


def _cmake(repo: Path, name: str) -> Detection | None:
    if not (repo / "CMakeLists.txt").is_file():
        return None
    configure = ["cmake", "-S", ".", "-B", "build"]
    notes: list[str] = []
    if sys.platform == "win32":
        if shutil.which("ninja"):
            configure += ["-G", "Ninja"]
            notes.append("Windows: using the Ninja generator (Docker images need ninja too, or drop -G)")
        elif shutil.which("mingw32-make"):
            configure += ["-G", "MinGW Makefiles"]
            notes.append("Windows: using the MinGW Makefiles generator (not available in Linux images)")
    return Detection(
        "cmake",
        RepoConfig(
            name=name,
            install=(),
            build=(tuple(configure), ("cmake", "--build", "build")),
            test=("ctest", "--test-dir", "build", "--output-on-failure"),
            test_framework="ctest",
            docker_image="gcc:14",
        ),
        [*notes, "the gcc image has no cmake: use an image that provides it for Docker runs"],
    )


def _elixir(repo: Path, name: str) -> Detection | None:
    if not (repo / "mix.exs").is_file():
        return None
    return Detection(
        "elixir",
        RepoConfig(
            name=name,
            install=(("mix", "deps.get"),),
            test=("mix", "test"),
            test_framework="mix",
            docker_image="elixir:1.17",
        ),
    )


def _dart(repo: Path, name: str) -> Detection | None:
    if not (repo / "pubspec.yaml").is_file():
        return None
    return Detection(
        "dart",
        RepoConfig(
            name=name,
            install=(("dart", "pub", "get"),),
            test=("dart", "test"),
            test_framework="dart",
            docker_image="dart:stable",
        ),
    )


DETECTORS = (_node, _rust, _go, _dotnet, _maven, _gradle, _ruby, _php, _elixir, _dart, _cmake, _python)


def detect(repo: Path) -> list[Detection]:
    """All matching ecosystems, most specific first."""
    name = default_name_for(repo)
    found = [d for d in (fn(repo, name) for fn in DETECTORS) if d is not None]
    for det in found:
        missing = sorted(
            {
                cmd[0]
                for cmd in (*det.config.install, *det.config.build, det.config.test)
                if cmd[0] != "python"
                and not cmd[0].startswith("./")
                and not cmd[0].startswith("vendor/")
                and shutil.which(cmd[0]) is None
            }
        )
        if missing:
            det.notes.append(f"not found on this machine: {', '.join(missing)} (needed for the local runner)")
    return found


def with_name(det: Detection, name: str) -> Detection:
    return Detection(det.ecosystem, replace(det.config, name=name), det.notes)
