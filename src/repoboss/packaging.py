"""Export and import of challenge packages (zip with a versioned manifest).

A package contains the snapshot files and a manifest with hashes, configuration, the injected edit
(which reveals the reference fix: do not hand a package to the agent under test) and the last
verification summary. Import never executes anything: the challenge is stored as untrusted until
``repoboss challenge verify ID --trust`` is run explicitly.
"""

from __future__ import annotations

import json
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoboss import __version__
from repoboss.challenge import IMPORTED_UNVERIFIED
from repoboss.config import ConfigError, RepoConfig
from repoboss.snapshot import load_snapshot
from repoboss.transforms import Candidate
from repoboss.util import (
    check_relpath,
    is_secret_like,
    matches_any,
    new_id,
    now_iso,
    rmtree,
    sha256_bytes,
    tree_hash,
)
from repoboss.workspace import Workspace

FORMAT = "repoboss-challenge"
FORMAT_VERSION = 1
MAX_ENTRIES = 20_000
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024


class PackageError(Exception):
    pass


@dataclass
class ExportResult:
    path: Path
    included: list[str]
    excluded: list[tuple[str, str]] = field(default_factory=list)


def export_challenge(ws: Workspace, challenge_id: str, out: Path, *, include_secrets: bool = False) -> ExportResult:
    row = ws.get_challenge(challenge_id)
    if row is None:
        raise PackageError(f"challenge {challenge_id} not found")
    snapshot = load_snapshot(ws.root / "snapshots", row["snapshot_hash"])
    assert snapshot.root is not None
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    entries: list[dict[str, Any]] = []
    for rel in snapshot.files:
        if is_secret_like(rel) and not include_secrets:
            excluded.append((rel, "looks like a credential file"))
            continue
        data = (snapshot.root / rel).read_bytes()
        entries.append({"path": f"snapshot/{rel}", "sha256": sha256_bytes(data), "size": len(data)})
        included.append(rel)
    if excluded:
        raise PackageError(
            "the snapshot contains credential-like files; refusing to export a package whose snapshot hash "
            "would no longer match: " + ", ".join(p for p, _ in excluded)
        )
    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_by": f"repoboss {__version__}",
        "exported_at": now_iso(),
        "challenge": {
            "id": row["id"],
            "name": row["name"],
            "transform": row["transform"],
            "seed": row["seed"],
            "description": row["description"],
            "snapshot_hash": row["snapshot_hash"],
            "candidate": row["candidate"],
            "config": row["config"],
            "status_at_export": row["status"],
            "verification": row["verification"],
            "env": row["env"],
        },
        "files": entries,
        "notice": "Contains the injected edit, i.e. the reference fix. Do not give this package to the agent.",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        for rel in included:
            zf.write(snapshot.root / rel, f"snapshot/{rel}")
    return ExportResult(out, ["manifest.json", *(f"snapshot/{r}" for r in included)], excluded)


def _check_entry(info: zipfile.ZipInfo) -> str:
    name = info.filename
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise PackageError(f"symbolic link in package: {name}")
    if info.flag_bits & 0x1:
        raise PackageError(f"encrypted entry: {name}")
    if info.is_dir():
        return ""
    try:
        check_relpath(name)
    except ValueError as exc:
        raise PackageError(f"unsafe path in package: {exc}") from exc
    if info.file_size > MAX_FILE_BYTES:
        raise PackageError(f"entry too large: {name}")
    return name


@dataclass
class ImportResult:
    challenge_id: str
    files: list[str]
    manifest: dict[str, Any]


def import_challenge(ws: Workspace, package: Path) -> ImportResult:
    """Validate and store a package. Never executes code from it."""
    try:
        zf = zipfile.ZipFile(package)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError(f"not a readable zip file: {exc}") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackageError("too many entries")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise PackageError("package too large once extracted")
        names = [n for n in (_check_entry(i) for i in infos) if n]
        if len(names) != len(set(names)) or len({n.lower() for n in names}) != len(names):
            raise PackageError("duplicate entries (possibly differing only by case)")
        if "manifest.json" not in names:
            raise PackageError("manifest.json missing")
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PackageError(f"manifest.json is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise PackageError("not a RepoBoss challenge package")
        if manifest.get("format_version") != FORMAT_VERSION:
            raise PackageError(f"unsupported format_version {manifest.get('format_version')!r}")
        ch = manifest.get("challenge")
        files = manifest.get("files")
        if not isinstance(ch, dict) or not isinstance(files, list):
            raise PackageError("manifest is missing 'challenge' or 'files'")

        # Validate configuration and candidate before touching the disk.
        try:
            config = RepoConfig.from_dict(ch["config"])
            candidate = Candidate.from_dict(ch["candidate"])
            check_relpath(candidate.path)
            if matches_any(candidate.path, config.locked, casefold=True):
                raise ValueError("the bug location is a hidden or protected file")
            seed = int(ch["seed"])
            snapshot_hash = str(ch["snapshot_hash"])
        except (KeyError, TypeError, ValueError, ConfigError) as exc:
            raise PackageError(f"invalid challenge metadata: {exc}") from exc
        if not (len(snapshot_hash) == 64 and all(c in "0123456789abcdef" for c in snapshot_hash)):
            raise PackageError("invalid snapshot hash")

        listed: dict[str, str] = {}
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise PackageError("invalid file entry in manifest")
            path = entry["path"]
            if not path.startswith("snapshot/"):
                raise PackageError(f"unexpected file outside snapshot/: {path}")
            listed[path] = str(entry.get("sha256", ""))
        extra = set(names) - set(listed) - {"manifest.json"}
        if extra:
            raise PackageError(f"files not declared in the manifest: {', '.join(sorted(extra)[:5])}")
        missing = set(listed) - set(names)
        if missing:
            raise PackageError(f"declared files missing: {', '.join(sorted(missing)[:5])}")

        staging = ws.root / "snapshots" / f".import-{snapshot_hash[:12]}"
        rmtree(staging)
        tree = staging / "tree"
        rels: list[str] = []
        try:
            for path, digest in sorted(listed.items()):
                data = zf.read(path)
                if sha256_bytes(data) != digest:
                    raise PackageError(f"hash mismatch for {path}")
                rel = path[len("snapshot/") :]
                check_relpath(rel)
                target = tree / rel
                if not target.resolve().is_relative_to(tree.resolve()):
                    raise PackageError(f"path escapes the package root: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                rels.append(rel)
            if tree_hash(tree, rels) != snapshot_hash:
                raise PackageError("snapshot hash does not match the files")
            if candidate.path not in rels:
                raise PackageError("the bug location is not part of the snapshot")
        except PackageError:
            rmtree(staging)
            raise

    final = ws.snapshot_dir(snapshot_hash)
    if final.exists():
        rmtree(staging)
    else:
        (staging / "files.json").write_text(json.dumps(sorted(rels)), encoding="utf-8")
        staging.rename(final)

    cid = str(ch.get("id", ""))
    if not re.fullmatch(r"ch-[0-9a-f]{8}", cid) or ws.get_challenge(cid) is not None:
        cid = new_id("ch")
    ws.insert_challenge(
        {
            "id": cid,
            "name": str(ch.get("name", "imported"))[:120],
            "created_at": now_iso(),
            "source_repo": None,
            "snapshot_hash": snapshot_hash,
            "transform": candidate.transform,
            "seed": seed,
            "description": candidate.description,
            "candidate": candidate.to_dict(),
            "config": config.to_dict(),
            "status": IMPORTED_UNVERIFIED,
            "status_detail": "imported; not executed on this machine yet",
            "verification": None,
            "env": None,
            "runner": None,
            "trusted": False,
            "imported_from": str(package.resolve()),
        }
    )
    return ImportResult(cid, sorted(rels), manifest)
