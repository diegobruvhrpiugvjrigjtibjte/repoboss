from __future__ import annotations

from pathlib import Path

import pytest

from repoboss.transforms import (
    TRANSFORMS,
    TransformError,
    apply_edit,
    find_candidates,
)
from repoboss.util import sha256_file, walk_files

SRC = ("src/*.py",)
PROT = ("tests/**",)


def candidates(root: Path, transform: str, seed: int = 0):
    files, _ = walk_files(root)
    return find_candidates(root, files, transform=transform, seed=seed, source=SRC, protected=PROT)


@pytest.mark.parametrize("name", list(TRANSFORMS))
def test_each_transform_applies_and_inverts_exactly(demo_repo: Path, name: str) -> None:
    found = candidates(demo_repo, name)
    assert found, f"{name} found no site in the demo repository"
    cand = found[0]
    target = demo_repo / cand.path
    before_bytes = target.read_bytes()
    before_hash = sha256_file(target)
    apply_edit(demo_repo, cand.edit)
    after = target.read_bytes()
    assert after != before_bytes
    # Only the recorded span changed.
    assert after.decode()[: cand.edit.start] == before_bytes.decode()[: cand.edit.start]
    apply_edit(demo_repo, cand.edit.inverse())
    assert sha256_file(target) == before_hash


def test_metadata_and_descriptions(demo_repo: Path) -> None:
    cfg = candidates(demo_repo, "config_value")
    assert {c.metadata["constant"] for c in cfg} >= {"TAX_RATE", "FREE_SHIPPING_THRESHOLD"}
    path = candidates(demo_repo, "path_reference")
    assert [c.metadata["old"] for c in path] == ["catalog.json"]
    assert path[0].metadata["resolves_to"] == ["src/shopcalc/data/catalog.json"]
    logic = candidates(demo_repo, "logic_condition")
    assert {c.metadata["function"] for c in logic} >= {"line_total", "shipping"}


def test_apply_refuses_when_file_changed(demo_repo: Path) -> None:
    cand = candidates(demo_repo, "config_value")[0]
    target = demo_repo / cand.path
    target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    with pytest.raises(TransformError):
        apply_edit(demo_repo, cand.edit)


def test_seed_is_deterministic(demo_repo: Path) -> None:
    a = [c.to_dict() for c in candidates(demo_repo, "config_value", seed=7)]
    b = [c.to_dict() for c in candidates(demo_repo, "config_value", seed=7)]
    assert a == b


def test_not_applicable_sites_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "src").mkdir(parents=True)
    (root / "src" / "m.py").write_text(
        'lower_case = 3\nNAME = "text"\nPATH = "missing.json"\nESCAPED = "data\\x2fx.json"\n'
        "if 1 < 2:\n    pass\n"
        "def f(a, b):\n    return (a  # comment <\n        < b)\n",
        encoding="utf-8",
    )
    (root / "src" / "x.json").write_text("{}", encoding="utf-8")
    assert candidates(root, "config_value") == []
    assert candidates(root, "path_reference") == []
    assert candidates(root, "logic_condition") == []


def test_protected_and_non_source_files_are_never_targets(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.py").write_text("LIMIT = 3\ndef f(a):\n    return a < 3\n", encoding="utf-8")
    (root / "tools.py").write_text("LIMIT = 3\n", encoding="utf-8")
    for name in TRANSFORMS:
        assert candidates(root, name) == []


def test_crlf_and_unicode_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "src").mkdir(parents=True)
    path = root / "src" / "m.py"
    path.write_bytes("# prezzo € unitario\r\nLIMIT = 10\r\ndef f(x):\r\n    return x >= LIMIT\r\n".encode())
    original = path.read_bytes()
    for name in ("config_value", "logic_condition"):
        cand = candidates(root, name)[0]
        apply_edit(root, cand.edit)
        assert b"\r\n" in path.read_bytes() and b"\n\n" not in path.read_bytes()
        apply_edit(root, cand.edit.inverse())
        assert path.read_bytes() == original


def test_unknown_transform(demo_repo: Path) -> None:
    with pytest.raises(TransformError):
        candidates(demo_repo, "delete_everything")
