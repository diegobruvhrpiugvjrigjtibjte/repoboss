"""Fault injection for non-Python languages, using tree-sitter syntax trees.

The same three transformations as for Python, with the same guarantees: a site is located on the
syntax tree (never by searching text), preconditions are checked, and the edit is an exact span
with before/after hashes. Grammars come from ``tree-sitter-language-pack``; they are downloaded on
first use and cached (see ``repoboss languages``).
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Sequence
from functools import cache
from pathlib import PurePosixPath
from typing import Any

LANG_BY_EXT: dict[str, str] = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".dart": "dart",
    ".lua": "lua",
    ".ex": "elixir",
    ".exs": "elixir",
    ".zig": "zig",
}
CODE_EXTS = frozenset(LANG_BY_EXT) | {".py", ".pyi", ".m", ".mm", ".sh", ".ps1"}

# Files that embed their own tests cannot hide them from the agent: never inject bugs there.
INLINE_TEST_MARKERS: dict[str, tuple[str, ...]] = {
    "rust": ("#[test]", "#[cfg(test)]", "#[tokio::test]"),
    "zig": ('test "',),
    "go": ("func Test",),
    "elixir": ("ExUnit.Case",),
    "dart": ("package:test/",),
}

OPS = {
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "==": "!=",
    "!=": "==",
    "===": "!==",
    "!==": "===",
    "&&": "||",
    "||": "&&",
    "and": "or",
    "or": "and",
}
BINARY_TYPES = re.compile(r"binary|comparison|equality|relational|conjunction|disjunction|logical")
FUNCTION_TYPES = re.compile(r"function|method|lambda|closure|arrow|constructor|accessor|getter|setter")
DECL_TYPES = re.compile(
    r"declarat|const_item|static_item|const_spec|var_spec|assignment|field|property|preproc_def|"
    r"constant|variable|let_|lexical"
)
NOT_WHOLE_VALUE = re.compile(
    r"call|binary|argument|array|object|list|tuple|unary|parenthes|subscript|index|range|arithmetic|"
    r"operator|conditional|ternary|template|lambda|function"
)
# Nodes that only wrap a declaration's value (Go `const X = 3` puts the literal in an expression_list).
VALUE_WRAPPERS = {"expression_list", "equals_value_clause", "initializer", "variable_initializer"}
TOP_LEVEL = {"program", "source_file", "module", "translation_unit", "compilation_unit", "source"}
NUMBER_TYPES = re.compile(r"number|integer|int_lit|int$|float|decimal|real|literal")
NUM_RE = re.compile(
    r"^(?P<int>[1-9][\d_]*|0)(?P<frac>\.\d[\d_]*)?"
    r"(?P<suffix>(?:[uUiIfF](?:8|16|32|64|128|size))|[uUlLfFdDmM]{1,3})?$"
)
CONST_NAME = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
PATH_VALUE = re.compile(r"^[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}$")


def language_for(rel: str) -> str | None:
    return LANG_BY_EXT.get(PurePosixPath(rel).suffix.lower())


@cache
def get_parser(lang: str) -> Any:
    import tree_sitter_language_pack as tslp

    return tslp.get_parser(lang)


def parser_available(lang: str) -> tuple[bool, str]:
    try:
        get_parser(lang)
    except Exception as exc:  # grammar download failure, unknown language, ...
        return False, f"{type(exc).__name__}: {exc}"
    return True, "ok"


class PolySource:
    def __init__(self, rel: str, lang: str, text: str) -> None:
        self.rel = rel
        self.lang = lang
        self.text = text
        self.raw = text.encode("utf-8")
        self.tree = get_parser(lang).parse(self.raw)

    def char(self, byte_offset: int) -> int:
        return len(self.raw[:byte_offset].decode("utf-8", errors="strict"))

    def span(self, node: Any) -> tuple[int, int]:
        return self.char(node.start_byte), self.char(node.end_byte)

    def node_text(self, node: Any) -> str:
        return str(self.raw[node.start_byte : node.end_byte].decode("utf-8"))

    def walk(self) -> list[Any]:
        out: list[Any] = []
        stack = [self.tree.root_node]
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(reversed(node.children))
        return out

    def has_inline_tests(self) -> bool:
        return any(marker in self.text for marker in INLINE_TEST_MARKERS.get(self.lang, ()))


def _ancestors(node: Any, limit: int = 64) -> list[Any]:
    out: list[Any] = []
    cur = node.parent
    while cur is not None and len(out) < limit:
        out.append(cur)
        cur = cur.parent
    return out


def _clean(node: Any) -> bool:
    return not (node.has_error or node.is_missing)


# ------------------------------------------------------------------------------------------- mutations


def mutate_number(token: str, rng: random.Random) -> str | None:
    m = NUM_RE.match(token)
    if not m:
        return None
    suffix = m.group("suffix") or ""
    int_part = m.group("int").replace("_", "")
    frac = m.group("frac")
    options: list[str]
    if frac:
        value = float(int_part + frac.replace("_", ""))
        if value == 0.0:
            options = ["1.0", "0.5"]
        else:
            options = [repr(round(v, 6)) for v in (value * 2, value / 2, value + 1) if round(v, 6) != value]
        options = [o if "." in o or "e" in o else o + ".0" for o in options]
    else:
        ivalue = int(int_part)
        candidates = [ivalue + 1, ivalue - 1 if ivalue > 1 else ivalue + 2]
        if ivalue not in (0, 1):
            candidates.append(ivalue * 2)
        options = [str(v) for v in candidates if v != ivalue]
    if not options:
        return None
    return rng.choice(options) + suffix


# ------------------------------------------------------------------------------------------- candidates

MakeEdit = Callable[[str, str, int, int, str], Any]
MakeCandidate = Callable[..., Any]


def _decl_name(decl: Any, src: PolySource, before: int) -> str | None:
    named = decl.child_by_field_name("name") or decl.child_by_field_name("left")
    if named is not None and named.start_byte < before:
        return src.node_text(named).strip("$")
    queue = list(decl.children)
    while queue:
        node = queue.pop(0)
        if node.start_byte >= before:
            continue
        if re.search(r"identifier|^constant$|^name$|variable_name", node.type) and node.child_count <= 1:
            return src.node_text(node).strip("$")
        queue.extend(node.children)
    return None


def config_value_candidates(
    src: PolySource,
    seed: int,
    rng_for: Callable[..., random.Random],
    make_edit: MakeEdit,
    make_candidate: MakeCandidate,
) -> list[Any]:
    out: list[Any] = []
    for node in src.walk():
        if node.type == "preproc_def":  # C/C++ #define NAME value
            name_node, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name_node is None or value is None:
                continue
            raw_value = src.node_text(value)
            token = raw_value.strip()
            name = src.node_text(name_node)
            if not CONST_NAME.match(name) or not NUM_RE.match(token):
                continue
            offset = raw_value.index(token)
            start = src.char(value.start_byte) + len(raw_value[:offset])
            line = node.start_point[0] + 1
            new = mutate_number(token, rng_for(seed, "config_value", src.rel, start))
            if new:
                out.append(
                    make_candidate(
                        "config_value",
                        src.rel,
                        line,
                        f"{name} changed from {token} to {new}",
                        make_edit(src.rel, src.text, start, start + len(token), new),
                        {"constant": name, "old": token, "new": new, "language": src.lang},
                    )
                )
            continue
        if not node.is_named or node.child_count != 0 or not NUMBER_TYPES.search(node.type) or not _clean(node):
            continue
        token = src.node_text(node)
        if not NUM_RE.match(token):
            continue
        decl = None
        for depth, anc in enumerate(_ancestors(node, 5)):
            if anc.type not in VALUE_WRAPPERS and NOT_WHOLE_VALUE.search(anc.type):
                break
            if DECL_TYPES.search(anc.type):
                decl = anc
                break
            if depth >= 4:
                break
        if decl is None:
            continue
        found_name = _decl_name(decl, src, node.start_byte)
        if not found_name:
            continue
        name = found_name
        top = decl.parent is not None and decl.parent.type in TOP_LEVEL
        decl_text = src.node_text(decl)
        is_const = "const" in decl.type or (top and decl_text.startswith(("const ", "export const ")))
        if not (CONST_NAME.match(name) and len(name) >= 2) and not is_const:
            continue
        start, end = src.span(node)
        new = mutate_number(token, rng_for(seed, "config_value", src.rel, start))
        if not new:
            continue
        out.append(
            make_candidate(
                "config_value",
                src.rel,
                node.start_point[0] + 1,
                f"{name} changed from {token} to {new}",
                make_edit(src.rel, src.text, start, end, new),
                {"constant": name, "old": token, "new": new, "language": src.lang},
            )
        )
    return out


def logic_candidates(src: PolySource, seed: int, make_edit: MakeEdit, make_candidate: MakeCandidate) -> list[Any]:
    out: list[Any] = []
    for node in src.walk():
        if not node.is_named or not BINARY_TYPES.search(node.type) or not _clean(node):
            continue
        children = node.children
        ops = [c for c in children if not c.is_named and src.node_text(c) in OPS]
        if len(ops) != 1:
            continue
        op = ops[0]
        idx = children.index(op)
        if idx == 0 or idx == len(children) - 1:
            continue
        operands = [c for c in node.named_children if c.type not in ("comment",)]
        if len(operands) < 2:
            continue
        if all(c.child_count == 0 and NUMBER_TYPES.search(c.type) for c in operands):
            continue
        func = next((a for a in _ancestors(node) if FUNCTION_TYPES.search(a.type)), None)
        if func is None:
            continue
        old = src.node_text(op)
        new = OPS[old]
        start, end = src.span(op)
        fname_node = func.child_by_field_name("name")
        fname = src.node_text(fname_node) if fname_node is not None else func.type
        out.append(
            make_candidate(
                "logic_condition",
                src.rel,
                op.start_point[0] + 1,
                f"in {fname}: '{old}' changed to '{new}'",
                make_edit(src.rel, src.text, start, end, new),
                {"function": fname, "old": old, "new": new, "language": src.lang},
            )
        )
    return out


def path_candidates(
    src: PolySource,
    files: Sequence[str],
    seed: int,
    rng_for: Callable[..., random.Random],
    make_edit: MakeEdit,
    make_candidate: MakeCandidate,
    path_variant: Callable[..., str | None],
) -> list[Any]:
    out: list[Any] = []
    file_set = set(files)
    for node in src.walk():
        if not node.is_named or "string" not in node.type or not _clean(node):
            continue
        if node.parent is not None and "string" in node.parent.type:
            continue
        text = src.node_text(node)
        if len(text) < 2 or text[0] not in "\"'`" or text[-1] != text[0]:
            continue
        value = text[1:-1]
        if any(ch in value for ch in "\\${}%#\n") or not PATH_VALUE.match(value) or "//" in value:
            continue
        if PurePosixPath(value).suffix.lower() in CODE_EXTS:
            continue
        clean = value[2:] if value.startswith("./") else value
        if clean.startswith("/") or ".." in clean.split("/"):
            continue
        targets = [f for f in files if f == clean or f.endswith("/" + clean)]
        if not targets:
            continue
        start, end = src.span(node)
        new_value = path_variant(value, clean, targets, file_set, rng_for(seed, "path_reference", src.rel, start))
        if new_value is None:
            continue
        out.append(
            make_candidate(
                "path_reference",
                src.rel,
                node.start_point[0] + 1,
                f"reference {value!r} changed to {new_value!r}",
                make_edit(src.rel, src.text, start + 1, end - 1, new_value),
                {"old": value, "new": new_value, "resolves_to": targets, "language": src.lang},
            )
        )
    return out
