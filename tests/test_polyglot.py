"""Bug injection in non-Python languages (tree-sitter). Grammars download on first use."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoboss.transforms import apply_edit, find_candidates
from repoboss.util import sha256_file

ts = pytest.importorskip("tree_sitter_language_pack")


def _grammar(lang: str) -> None:
    try:
        ts.get_parser(lang)
    except Exception as exc:  # offline without a cached grammar
        pytest.skip(f"{lang} grammar unavailable: {exc}")


def cands(root: Path, transform: str) -> list:
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    return find_candidates(root, files, transform=transform, seed=0, source=("**",), protected=("tests/*",))


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    return root


CASES = {
    "javascript": (
        "src/a.js",
        "export const MAX_ITEMS = 10;\nexport function ok(n) { return n < MAX_ITEMS && n >= 0; }\n",
    ),
    "typescript": (
        "src/a.ts",
        "export const LIMIT: number = 5;\nexport function f(xs: Array<number>): boolean {\n"
        "  return xs.length <= LIMIT;\n}\n",
    ),
    "java": (
        "src/A.java",
        "class A {\n  static final int MAX_SIZE = 8;\n"
        "  boolean f(java.util.List<String> l) { return l.size() > MAX_SIZE; }\n}\n",
    ),
    "go": ("a.go", "package a\n\nconst Limit = 3\n\nfunc F(n int) bool {\n\treturn n >= Limit\n}\n"),
    "c": ("src/a.c", "#define MAX_LEN 16\nint f(int n) { return n == MAX_LEN; }\n"),
    "csharp": (
        "src/A.cs",
        "class A {\n  public const int MAX_SIZE = 8;\n  bool F(int n) { return n != MAX_SIZE; }\n}\n",
    ),
    "ruby": ("lib/a.rb", "MAX_SIZE = 4\n\ndef ok?(n)\n  n > 0 && n <= MAX_SIZE\nend\n"),
    "php": ("src/a.php", "<?php\nconst MAX_SIZE = 4;\nfunction ok($n) { return $n < MAX_SIZE; }\n"),
    "kotlin": ("src/A.kt", "const val MAX_SIZE = 4\nfun ok(n: Int): Boolean = n < MAX_SIZE\n"),
}


@pytest.mark.parametrize("lang", sorted(CASES))
def test_config_and_logic_in_language(tmp_path: Path, lang: str) -> None:
    _grammar(lang)
    rel, code = CASES[lang]
    root = write(tmp_path, {rel: code})
    for transform in ("config_value", "logic_condition"):
        found = cands(root, transform)
        assert found, f"{lang}: no {transform} candidate"
        cand = found[0]
        assert cand.metadata["language"] == lang
        before = sha256_file(root / rel)
        apply_edit(root, cand.edit)
        assert sha256_file(root / rel) != before
        apply_edit(root, cand.edit.inverse())
        assert sha256_file(root / rel) == before


def test_generics_are_never_mutated(tmp_path: Path) -> None:
    _grammar("typescript")
    root = write(
        tmp_path, {"src/a.ts": "export function f(m: Map<string, Array<number>>): number { return m.size; }\n"}
    )
    assert cands(root, "logic_condition") == []


def test_rust_files_with_inline_tests_are_skipped(tmp_path: Path) -> None:
    _grammar("rust")
    root = write(
        tmp_path,
        {
            "src/lib.rs": "pub const MAX: u32 = 3;\npub fn f(a: u32) -> bool { a < MAX }\n"
            "#[cfg(test)]\nmod tests { #[test] fn t() { assert!(super::f(1)); } }\n",
            "src/other.rs": "pub const LIMIT: u32 = 7;\npub fn g(a: u32) -> bool { a >= LIMIT }\n",
        },
    )
    paths = {c.path for c in cands(root, "logic_condition")} | {c.path for c in cands(root, "config_value")}
    assert paths == {"src/other.rs"}


def test_path_reference_skips_interpolation_and_code_imports(tmp_path: Path) -> None:
    _grammar("javascript")
    root = write(
        tmp_path,
        {
            "src/a.js": 'import x from "./b.js";\nconst A = `${dir}/data.json`;\nconst B = "data.json";\n',
            "src/b.js": "export default 1;\n",
            "src/data.json": "{}",
        },
    )
    found = cands(root, "path_reference")
    assert [c.metadata["old"] for c in found] == ["data.json"]
    assert found[0].edit.old == "data.json"  # quotes untouched


def test_number_suffixes_are_preserved(tmp_path: Path) -> None:
    _grammar("rust")
    root = write(tmp_path, {"src/a.rs": "pub const TIMEOUT_MS: u64 = 1_500u64;\n"})
    found = cands(root, "config_value")
    assert found and found[0].edit.new.endswith("u64")


def test_protected_paths_are_never_targets(tmp_path: Path) -> None:
    _grammar("javascript")
    root = write(tmp_path, {"tests/a.js": "const MAX = 1;\nfunction f(a) { return a < MAX; }\n"})
    assert cands(root, "config_value") == [] and cands(root, "logic_condition") == []
