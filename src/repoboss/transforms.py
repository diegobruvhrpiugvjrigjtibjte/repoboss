"""Controlled fault injection.

Each transformation locates a precise source span through the Python AST, checks explicit
preconditions, and produces an :class:`Edit` (patch) whose inverse restores the original bytes.
Nothing here performs global text substitution: an edit touches exactly one span and is refused
when the file content or the span does not match what was recorded.
"""

from __future__ import annotations

import ast
import difflib
import random
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from repoboss.polyglot import CODE_EXTS
from repoboss.util import matches_any, read_text_exact, sha256_bytes, write_text_exact


class TransformError(Exception):
    """A transformation cannot be applied (precondition failed)."""


@dataclass(frozen=True)
class Edit:
    path: str
    start: int  # character offsets in the decoded text
    end: int
    old: str
    new: str
    sha256_before: str
    sha256_after: str

    def inverse(self) -> Edit:
        return Edit(
            path=self.path,
            start=self.start,
            end=self.start + len(self.new),
            old=self.new,
            new=self.old,
            sha256_before=self.sha256_after,
            sha256_after=self.sha256_before,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Edit:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Candidate:
    transform: str
    path: str
    line: int
    description: str
    edit: Edit
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transform": self.transform,
            "path": self.path,
            "line": self.line,
            "description": self.description,
            "edit": self.edit.to_dict(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Candidate:
        return cls(
            transform=data["transform"],
            path=data["path"],
            line=int(data["line"]),
            description=data["description"],
            edit=Edit.from_dict(data["edit"]),
            metadata=dict(data.get("metadata", {})),
        )


# --------------------------------------------------------------------------- edit application


def apply_edit(root: Path, edit: Edit) -> None:
    """Apply one edit, refusing if the file or the span differs from what was recorded."""
    target = root / edit.path
    if not target.is_file():
        raise TransformError(f"{edit.path}: file not found")
    raw = target.read_bytes()
    if sha256_bytes(raw) != edit.sha256_before:
        raise TransformError(f"{edit.path}: content differs from the recorded precondition")
    text = read_text_exact(target)
    if text[edit.start : edit.end] != edit.old:
        raise TransformError(f"{edit.path}: span {edit.start}:{edit.end} does not contain {edit.old!r}")
    write_text_exact(target, text[: edit.start] + edit.new + text[edit.end :])
    if sha256_bytes(target.read_bytes()) != edit.sha256_after:
        write_text_exact(target, text)
        raise TransformError(f"{edit.path}: result hash mismatch, edit rolled back")


def unified_diff(edit: Edit, original_text: str) -> str:
    """Unified diff of an edit, computed from the text it applies to."""
    after = original_text[: edit.start] + edit.new + original_text[edit.end :]
    return "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{edit.path}",
            tofile=f"b/{edit.path}",
        )
    )


def _make_edit(path: str, text: str, start: int, end: int, new: str) -> Edit:
    before = text.encode("utf-8")
    after = (text[:start] + new + text[end:]).encode("utf-8")
    return Edit(path, start, end, text[start:end], new, sha256_bytes(before), sha256_bytes(after))


# --------------------------------------------------------------------------- source helpers


_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")


class Source:
    """A parsed Python file with AST-position to character-offset mapping."""

    def __init__(self, rel: str, text: str) -> None:
        self.rel = rel
        self.text = text
        self.tree = ast.parse(text, filename=rel)
        self.lines = _LINE_RE.findall(text)
        self.starts: list[int] = []
        pos = 0
        for line in self.lines:
            self.starts.append(pos)
            pos += len(line)

    def offset(self, lineno: int, col_byte: int) -> int:
        line = self.lines[lineno - 1]
        prefix = line.encode("utf-8")[:col_byte].decode("utf-8")
        return self.starts[lineno - 1] + len(prefix)

    def span(self, node: ast.AST) -> tuple[int, int]:
        lineno = getattr(node, "lineno", None)
        end_lineno = getattr(node, "end_lineno", None)
        col = getattr(node, "col_offset", None)
        end_col = getattr(node, "end_col_offset", None)
        if lineno is None or end_lineno is None or col is None or end_col is None:
            raise TransformError("node has no position")
        return self.offset(lineno, col), self.offset(end_lineno, end_col)


def _load_sources(root: Path, files: Sequence[str], source: Sequence[str], protected: Sequence[str]) -> list[Source]:
    out: list[Source] = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        if not matches_any(rel, source) or matches_any(rel, protected, casefold=True):
            continue
        try:
            out.append(Source(rel, read_text_exact(root / rel)))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
    return out


def _rng(seed: int, *parts: object) -> random.Random:
    return random.Random("|".join([str(seed), *map(str, parts)]))


# --------------------------------------------------------------------------- transformations


class Transform(ABC):
    name: str
    summary: str

    @abstractmethod
    def candidates(self, src: Source, files: Sequence[str], seed: int) -> list[Candidate]:
        """All applicable sites in ``src`` (preconditions already checked)."""


_CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


class ConfigValueTransform(Transform):
    """Changes a module-level UPPER_CASE numeric or boolean setting."""

    name = "config_value"
    summary = "change a module-level configuration constant (int, float or bool)"

    @staticmethod
    def _mutations(value: int | float | bool) -> list[int | float | bool]:
        if isinstance(value, bool):
            return [not value]
        if isinstance(value, int):
            options: list[int | float | bool] = [value + 1, value - 1 if value > 1 else value + 2]
            if value not in (0, 1):
                options.append(value * 2)
            return [v for v in options if v != value]
        if value == 0.0:
            return [1.0, 0.5]
        options = [round(value * 2, 6), round(value / 2, 6), round(value + 1, 6)]
        return [v for v in options if v != value]

    def candidates(self, src: Source, files: Sequence[str], seed: int) -> list[Candidate]:
        out: list[Candidate] = []
        for node in src.tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                name, value = node.target.id, node.value
            else:
                continue
            if not _CONST_NAME.match(name) or not isinstance(value, ast.Constant):
                continue
            literal = value.value
            if not isinstance(literal, int | float) or isinstance(literal, complex):
                continue
            start, end = src.span(value)
            token = src.text[start:end]
            try:
                if ast.literal_eval(token) != literal:
                    continue
            except (ValueError, SyntaxError):
                continue
            new_value = _rng(seed, self.name, src.rel, start).choice(self._mutations(literal))
            new_token = repr(new_value)
            out.append(
                Candidate(
                    transform=self.name,
                    path=src.rel,
                    line=node.lineno,
                    description=f"{name} changed from {token} to {new_token}",
                    edit=_make_edit(src.rel, src.text, start, end, new_token),
                    metadata={"constant": name, "old": token, "new": new_token},
                )
            )
        return out


_PATH_VALUE = re.compile(r"^[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}$")


def path_variant(value: str, clean: str, targets: Sequence[str], file_set: set[str], rng: random.Random) -> str | None:
    """A plausible wrong file name for ``value`` that does not exist in the snapshot."""
    path = PurePosixPath(value)
    stem, suffix = path.stem, path.suffix
    variants = [f"{stem}s{suffix}"]
    if len(stem) > 3:
        variants.append(f"{stem[:-1]}{suffix}")
    if "_" in stem:
        variants.append(f"{stem.replace('_', '-')}{suffix}")
    rng.shuffle(variants)
    for variant in variants:
        new_value = str(path.with_name(variant)) if str(path.parent) != "." else variant
        new_value = new_value.replace("\\", "/")
        broken = [t[: len(t) - len(clean)] + new_value.removeprefix("./") for t in targets]
        if not any(b in file_set for b in broken):
            return new_value
    return None


class PathReferenceTransform(Transform):
    """Breaks a string literal that refers to an existing data file of the repository."""

    name = "path_reference"
    summary = "break a string literal that names an existing non-Python file"

    def candidates(self, src: Source, files: Sequence[str], seed: int) -> list[Candidate]:
        skip: set[int] = set()
        for node in ast.walk(src.tree):
            if isinstance(node, ast.JoinedStr):
                skip.update(id(v) for v in ast.walk(node))
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                skip.add(id(node.value))  # docstrings and bare strings
        file_set = set(files)
        out: list[Candidate] = []
        for node in ast.walk(src.tree):
            if id(node) in skip or not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if not _PATH_VALUE.match(value) or value.endswith(".py") or "//" in value:
                continue
            clean = value[2:] if value.startswith("./") else value
            if clean.startswith("/") or ".." in clean.split("/"):
                continue
            targets = [f for f in files if f == clean or f.endswith("/" + clean)]
            if not targets:
                continue
            start, end = src.span(node)
            token = src.text[start:end]
            if len(token) < 2 or token[0] not in "'\"" or token[-1] != token[0] or token[1:-1] != value:
                continue  # prefixed, escaped or implicitly concatenated literal: not a safe site
            if PurePosixPath(value).suffix.lower() in CODE_EXTS:
                continue  # imports/includes of code files are not data references
            chosen = path_variant(value, clean, targets, file_set, _rng(seed, self.name, src.rel, start))
            if chosen is None:
                continue
            new_token = token[0] + chosen + token[0]
            out.append(
                Candidate(
                    transform=self.name,
                    path=src.rel,
                    line=node.lineno,
                    description=f"reference {value!r} changed to {chosen!r}",
                    edit=_make_edit(src.rel, src.text, start, end, new_token),
                    metadata={"old": value, "new": chosen, "resolves_to": targets},
                )
            )
        return out


_CMP_SWAP: dict[type[ast.cmpop], tuple[str, str]] = {
    ast.Lt: ("<", "<="),
    ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="),
    ast.GtE: (">=", ">"),
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
}
_FILLER = re.compile(r"^[\s()\\]*$")


class LogicConditionTransform(Transform):
    """Flips one comparison boundary (``<`` / ``<=``, ``==`` / ``!=``) or ``and`` / ``or``."""

    name = "logic_condition"
    summary = "flip one comparison operator or boolean connective inside a function"

    def _operator_site(self, src: Source, left: ast.AST, right: ast.AST, op: str) -> tuple[int, int] | None:
        _, seg_start = src.span(left)
        seg_end, _ = src.span(right)
        seg = src.text[seg_start:seg_end]
        if seg.count(op) != 1 or "#" in seg:
            return None
        idx = seg.index(op)
        if not _FILLER.match(seg[:idx] + seg[idx + len(op) :]):
            return None
        if op.isalpha() and not (seg[idx - 1 : idx].isspace() or seg[idx - 1 : idx] == ")"):
            return None
        return seg_start + idx, seg_start + idx + len(op)

    def candidates(self, src: Source, files: Sequence[str], seed: int) -> list[Candidate]:
        out: list[Candidate] = []
        seen: set[int] = set()
        for func in ast.walk(src.tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if id(node) in seen:
                    continue
                seen.add(id(node))
                site: tuple[int, int] | None = None
                old = new = ""
                if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP_SWAP:
                    if isinstance(node.left, ast.Constant) and isinstance(node.comparators[0], ast.Constant):
                        continue
                    old, new = _CMP_SWAP[type(node.ops[0])]
                    site = self._operator_site(src, node.left, node.comparators[0], old)
                elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
                    old, new = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
                    site = self._operator_site(src, node.values[0], node.values[1], old)
                if site is None:
                    continue
                start, end = site
                line_no = src.text.count("\n", 0, start) + 1
                out.append(
                    Candidate(
                        transform=self.name,
                        path=src.rel,
                        line=line_no,
                        description=f"in {func.name}(): '{old}' changed to '{new}'",
                        edit=_make_edit(src.rel, src.text, start, end, new),
                        metadata={"function": func.name, "old": old, "new": new},
                    )
                )
        return out


TRANSFORMS: dict[str, Transform] = {
    t.name: t for t in (ConfigValueTransform(), PathReferenceTransform(), LogicConditionTransform())
}


def _candidate(
    transform: str, path: str, line: int, description: str, edit: Edit, metadata: dict[str, Any]
) -> Candidate:
    return Candidate(transform, path, line, description, edit, metadata)


def _poly_candidates(
    root: Path,
    files: Sequence[str],
    transform: str,
    seed: int,
    source: Sequence[str],
    locked: Sequence[str],
    warnings: list[str] | None,
) -> list[Candidate]:
    from repoboss import polyglot

    out: list[Candidate] = []
    failed_langs: set[str] = set()
    for rel in files:
        lang = polyglot.language_for(rel)
        if lang is None or lang in failed_langs:
            continue
        if not matches_any(rel, source) or matches_any(rel, locked, casefold=True):
            continue
        try:
            text = read_text_exact(root / rel)
        except (UnicodeDecodeError, OSError):
            continue
        try:
            src = polyglot.PolySource(rel, lang, text)
        except Exception as exc:  # grammar unavailable (offline first use) or parser failure
            failed_langs.add(lang)
            if warnings is not None:
                warnings.append(f"{lang}: parser unavailable ({type(exc).__name__}: {exc})")
            continue
        if src.has_inline_tests():
            if warnings is not None:
                warnings.append(f"{rel}: skipped, it contains inline tests the agent could edit")
            continue
        if transform == "config_value":
            out += polyglot.config_value_candidates(src, seed, _rng, _make_edit, _candidate)
        elif transform == "logic_condition":
            out += polyglot.logic_candidates(src, seed, _make_edit, _candidate)
        elif transform == "path_reference":
            out += polyglot.path_candidates(src, files, seed, _rng, _make_edit, _candidate, path_variant)
    return out


def find_candidates(
    root: Path,
    files: Sequence[str],
    *,
    transform: str,
    seed: int,
    source: Sequence[str],
    protected: Sequence[str],
    warnings: list[str] | None = None,
) -> list[Candidate]:
    """Applicable sites for one transformation, in a seed-determined order.

    ``protected`` is every path the agent may not change (hidden + protected); bugs are never placed
    there. Python files use the ``ast`` module, other languages tree-sitter.
    """
    if transform not in TRANSFORMS:
        raise TransformError(f"unknown transformation {transform!r}; known: {', '.join(TRANSFORMS)}")
    impl = TRANSFORMS[transform]
    found: list[Candidate] = []
    for src in _load_sources(root, files, source, protected):
        found.extend(impl.candidates(src, files, seed))
    found.extend(_poly_candidates(root, files, transform, seed, source, protected, warnings))
    found.sort(key=lambda c: (c.path, c.edit.start))
    random.Random(f"{seed}|order|{transform}").shuffle(found)
    return found
