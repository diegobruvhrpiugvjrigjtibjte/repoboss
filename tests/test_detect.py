from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoboss.config import parse_config_text, render_config
from repoboss.detect import detect


def make(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "files, ecosystem, test_cmd, framework",
    [
        (
            {
                "package.json": json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "^29"}}),
                "package-lock.json": "{}",
            },
            "node",
            ("npm", "test"),
            "jest",
        ),
        (
            {"package.json": json.dumps({"scripts": {"test": "vitest"}}), "pnpm-lock.yaml": ""},
            "node",
            ("pnpm", "test", "--", "--run"),
            "vitest",
        ),
        ({"Cargo.toml": "[package]\nname='x'\n"}, "rust", ("cargo", "test"), "cargo"),
        ({"go.mod": "module x\n"}, "go", ("go", "test", "-json", "./..."), "go"),
        ({"pom.xml": "<project/>"}, "java-maven", ("mvn", "-B", "test"), "maven"),
        ({"build.gradle": "", "gradlew": ""}, "java-gradle", ("./gradlew", "test", "--no-daemon"), "gradle"),
        ({"App.csproj": "<Project/>"}, "dotnet", ("dotnet", "test", "--nologo"), "dotnet"),
        ({"Gemfile": "", "spec/a_spec.rb": ""}, "ruby", ("bundle", "exec", "rspec"), "rspec"),
        ({"composer.json": "{}"}, "php", ("vendor/bin/phpunit",), "phpunit"),
        (
            {"pyproject.toml": "[project]\nname='x'\n[project.optional-dependencies]\ntest=['pytest']\n"},
            "python",
            ("python", "-m", "pytest", "-q"),
            "pytest",
        ),
    ],
)
def test_detection(
    tmp_path: Path, files: dict[str, str], ecosystem: str, test_cmd: tuple[str, ...], framework: str
) -> None:
    found = detect(make(tmp_path, files))
    assert found and found[0].ecosystem == ecosystem
    cfg = found[0].config
    assert cfg.test == test_cmd and cfg.test_framework == framework
    # The proposal renders to TOML that parses back to the same configuration.
    assert parse_config_text(render_config(cfg), origin="t", default_name="x") == cfg


def test_python_editable_install_with_extras(tmp_path: Path) -> None:
    found = detect(
        make(tmp_path, {"pyproject.toml": "[project]\nname='x'\n[project.optional-dependencies]\ntest=['pytest']\n"})
    )
    assert ("python", "-m", "pip", "install", "-e", ".[test]") in found[0].config.install


def test_node_dependencies_are_linked_not_copied(tmp_path: Path) -> None:
    found = detect(
        make(tmp_path, {"package.json": json.dumps({"devDependencies": {"jest": "1"}, "scripts": {"test": "jest"}})})
    )
    assert found[0].config.dependency_dirs == ("node_modules",)


def test_cmake_has_build_steps(tmp_path: Path) -> None:
    found = detect(make(tmp_path, {"CMakeLists.txt": "project(x)\n"}))
    cfg = found[0].config
    assert cfg.build[0][:5] == ("cmake", "-S", ".", "-B", "build")
    assert cfg.test[0] == "ctest"


def test_unknown_project(tmp_path: Path) -> None:
    assert detect(make(tmp_path, {"README.md": "hi"})) == []
