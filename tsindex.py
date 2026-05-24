#!/usr/bin/env python3
"""tsindex.py — tree-sitter based C symbol indexer.

Sub-commands:
  build           Scan a directory and write an index file.
  stats           Top-line stats.
  lookup NAME     Show every def/decl matching NAME (optionally filter by --kind).
  kind KIND       List all symbols of a given kind.
  file PATH       Show every symbol defined in a file.
  refs NAME       Show every reference to NAME (--kind call|type).
  unused-static   List static functions referenced nowhere outside themselves.

Index file: SQLite, default ./tsindex.db
"""
from __future__ import annotations
import argparse
import bisect
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Iterable
from collections import defaultdict, Counter

# Bump when the data model or preprocessing pipeline changes in a way that
# would make an old on-disk index incompatible with new code. The build()
# command falls back to a full rebuild whenever this differs from what's
# stored in tsindex.db.
SCHEMA_VERSION = 4

from tree_sitter import Language, Parser

UNIFDEF_BIN = shutil.which("unifdef")
KV_RE = re.compile(r"KERNEL_VERSION\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")
# IS_ENABLED(X) → defined(X). unifdef can't evaluate function-like macros, but
# it does evaluate `defined()`, so this lets it prune CONFIG_* branches uniformly.
IS_ENABLED_RE = re.compile(r"IS_ENABLED\s*\(\s*([A-Za-z_]\w*)\s*\)")

# for-each loop macros: tree-sitter sees `id(args) {...}` as invalid C, and
# `id(args) stmt` (no semicolon, single-statement form) is also invalid.
# Append `;` after the macro call if not already present — that turns either
# form into valid C statement-sequence syntax. Call refs remain unchanged.
FOREACH_RE = re.compile(
    r"(\b(?:for_each_\w+|list_for_each(?:_\w+)?|hlist_for_each(?:_\w+)?|"
    r"skb_queue_walk(?:_\w+)?|netdev_for_each_\w+|xa_for_each(?:_\w+)?|"
    r"idr_for_each(?:_\w+)?|rcu_list_for_each(?:_\w+)?|"
    r"radix_tree_for_each(?:_\w+)?|llist_for_each(?:_\w+)?|"
    r"nla_for_each(?:_\w+)?|nlmsg_for_each(?:_\w+)?|skb_walk_frags)\s*"
    r"\([^()]*(?:\([^()]*\)[^()]*)*\))(?!\s*[;,)])"
)

# Type-as-argument macros: `container_of(p, struct foo, m)` is not parseable as C
# because `struct foo` isn't an expression. Strip `struct`/`union`/`enum` keywords
# inside these calls; the type name then parses as a plain identifier.
TYPE_ARG_MACROS = ("container_of", "container_of_const", "offsetof", "offsetofend",
                   "FIELD_SIZEOF", "BUILD_BUG_ON_INVALID", "typeof_member",
                   "list_entry", "list_first_entry", "list_first_entry_or_null",
                   "list_last_entry", "list_next_entry", "list_prev_entry",
                   "hlist_entry", "hlist_entry_safe", "kobj_to_dev",
                   "max_t", "min_t", "clamp_t", "va_arg", "__builtin_va_arg")
TYPE_ARG_CALL_RE = re.compile(
    r"\b(" + "|".join(TYPE_ARG_MACROS) + r")\s*"
    r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)"
)
STRUCT_KW_RE = re.compile(r"\b(struct|union|enum)\s+(\w+)")

# Declaration macros — DECLARE_BITMAP(name, size) etc. — used at file/struct
# member scope. Without expansion tree-sitter sees `call_expression;` which is
# invalid as a member declaration. Rewrite to a placeholder declaration.
DECL_MACRO_RE = re.compile(
    r"\b(DECLARE_BITMAP|DECLARE_KFIFO|DECLARE_KFIFO_PTR|DECLARE_HASHTABLE|"
    r"DECLARE_PER_CPU|DECLARE_COMPLETION|DECLARE_RWSEM|DECLARE_WAIT_QUEUE_HEAD|"
    r"DEFINE_RATELIMIT_STATE|DEFINE_MUTEX|DEFINE_SPINLOCK|DEFINE_PER_CPU|"
    r"DEFINE_STATIC_KEY_FALSE|DEFINE_STATIC_KEY_TRUE|DEFINE_IDA|DEFINE_IDR)"
    # `(name, ...balanced parens...)` — use `[^()]*` after the comma so the
    # outer `\)` only matches the macro's own closing paren, not the first
    # `)` of any nested call inside.
    r"\s*\(\s*(\w+)\s*(?:,[^()]*(?:\([^()]*\)[^()]*)*)?\)"
)

# Bare GCC attribute aliases (no __attribute__ wrapper) — `__packed`, `__aligned`,
# `__used`, etc. — confuse tree-sitter when placed between `}` and an instance
# name (`} __packed name;`). Rewrite to `__attribute__((NAME))`, which tree-sitter
# DOES parse correctly in those positions.
BARE_ATTR_RE = re.compile(
    r"\b(__packed|__used|__unused|__must_check|__deprecated|__cold|__hot|"
    r"__pure|__init|__exit|__weak|__noreturn|__force|__user|__kernel|"
    r"__iomem|__rcu|__percpu|__always_inline|__maybe_unused|__ro_after_init|"
    r"__read_mostly|__initdata|__initconst|__refdata|__visible|__always_unused)\b"
)
# Function-form variants: `__aligned(8)`, `__attribute_used__`.
BARE_ATTR_PAREN_RE = re.compile(
    r"\b(__aligned|__section|__alias)\s*\(([^()]*)\)"
)

# Variadic-macro GCC extension: `#define X(args ...)` → standard `#define X(...)`.
# tree-sitter-c doesn't parse the GCC named-rest-args syntax cleanly.
VARIADIC_MACRO_RE = re.compile(
    r"(#\s*define\s+\w+\s*\([^)]*?)\b(\w+)\s*\.\.\.\)",
    re.MULTILINE,
)

# `#ifdef 0` / `#ifndef 0` — developer typo for `#if 0`. unifdef errors on this.
IFDEF_ZERO_RE = re.compile(r"^(\s*)#\s*ifdef\s+0\b", re.MULTILINE)
IFNDEF_ZERO_RE = re.compile(r"^(\s*)#\s*ifndef\s+0\b", re.MULTILINE)

# Consecutive `__attribute__((...))` chains — tree-sitter-c rejects them
# in declaration position but accepts the merged `__attribute__((A, B))`.
CONSECUTIVE_ATTR_RE = re.compile(
    r"__attribute__\s*\(\(([^()]*(?:\([^()]*\)[^()]*)*)\)\)\s*"
    r"__attribute__\s*\(\(([^()]*(?:\([^()]*\)[^()]*)*)\)\)"
)

# tree-sitter-c rejects `name __attribute__((...)) = {...};` — attribute
# between a declarator identifier and `=`. Simplest: just drop the attribute.
ATTR_BEFORE_EQ_RE = re.compile(
    r"__attribute__\s*\(\(([^()]*(?:\([^()]*\)[^()]*)*)\)\)(\s*=)"
)

# tree-sitter-c rejects block comments INSIDE a `#define` value line.
# Strip block comments from #define lines (per line — must run after fold).
DEFINE_LINE_RE = re.compile(r"^(\s*#\s*define\s[^\n]*)$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


# ---------- preprocessing (optional) ----------

def resolve_kernel_version(s: str) -> str:
    """Replace KERNEL_VERSION(a,b,c) macro calls with the integer they expand to.
    unifdef can't evaluate function-like macros, so we do this substitution first."""
    return KV_RE.sub(
        lambda m: str((int(m.group(1)) << 16) + (int(m.group(2)) << 8) + int(m.group(3))),
        s,
    )


def parse_defs_file(path: Path) -> list[str]:
    """Read a #define/#undef style config file. Return unifdef -D/-U flag list."""
    flags: list[str] = []
    if not path.is_file():
        return flags
    for raw in path.read_text().splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"#define\s+(\w+)(?:\s+(.+))?$", line)
        if m:
            name, val = m.group(1), (m.group(2) or "").strip()
            flags.append(f"-D{name}={val}" if val else f"-D{name}")
            continue
        m = re.match(r"#undef\s+(\w+)$", line)
        if m:
            flags.append(f"-U{m.group(1)}")
    return flags


CONFIG_RE = re.compile(r"\bCONFIG_[A-Z][A-Z0-9_]*\b")

def collect_unknown_configs(root: Path, explicit: set[str]) -> list[str]:
    """Scan all .c/.h for CONFIG_* identifiers; return ones not in `explicit`.
    Used to auto-`#undef` config keys the user didn't list in their defs file."""
    seen: set[str] = set()
    for p in root.rglob("*"):
        if p.suffix not in (".c", ".h") or not p.is_file():
            continue
        try:
            for m in CONFIG_RE.finditer(p.read_text(errors="replace")):
                seen.add(m.group(0))
        except OSError:
            pass
    return sorted(seen - explicit)


def rewrite_foreach(text: str) -> str:
    """Insert `;` after a for-each macro call so the body parses cleanly."""
    return FOREACH_RE.sub(r"\1;", text)


def rewrite_decl_macros(text: str) -> str:
    """Replace DECLARE_BITMAP(name, ...) and friends with a placeholder
    declaration so they parse at struct-member or file scope."""
    return DECL_MACRO_RE.sub(r"unsigned long \2[1]", text)


def rewrite_bare_attributes(text: str) -> str:
    """Replace bare GCC attribute aliases with `__attribute__((...))` form.
    tree-sitter-c handles `__attribute__((X))` but not bare `__X` in many
    positions (notably between `}` and a struct instance name)."""
    text = BARE_ATTR_RE.sub(lambda m: f"__attribute__(({m.group(1).strip('_')}))", text)
    text = BARE_ATTR_PAREN_RE.sub(
        lambda m: f"__attribute__(({m.group(1).strip('_')}({m.group(2)})))", text)
    return text


def rewrite_variadic_macros(text: str) -> str:
    """`#define X(a, b, args ...)` → `#define X(a, b, ...)`.
    GCC named-variadic syntax confuses tree-sitter; standard `...` parses fine."""
    return VARIADIC_MACRO_RE.sub(r"\1...)", text)


def rewrite_ifdef_zero(text: str) -> str:
    """`#ifdef 0` → `#if 0` (and same for `#ifndef`). Developer typo.
    `#ifdef` needs an identifier; `0` is invalid and crashes unifdef."""
    text = IFDEF_ZERO_RE.sub(r"\1#if 0", text)
    text = IFNDEF_ZERO_RE.sub(r"\1#if 1", text)  # #ifndef 0 ≡ always true
    return text


def strip_define_comments(text: str) -> str:
    """Strip /* ... */ block comments from inside #define lines.
    tree-sitter-c rejects block comments embedded in a macro value."""
    def repl(m):
        return BLOCK_COMMENT_RE.sub(" ", m.group(1))
    return DEFINE_LINE_RE.sub(repl, text)


# tree-sitter-c rejects trailing whitespace on `#include "..."` and other
# preprocessor lines. Trim trailing whitespace from any line starting with `#`.
PP_TRAILING_WS_RE = re.compile(r"^(\s*#[^\n]*?)[ \t]+$", re.MULTILINE)


def strip_pp_trailing_ws(text: str) -> str:
    return PP_TRAILING_WS_RE.sub(r"\1", text)


def rewrite_consecutive_attrs(text: str) -> str:
    """Merge `__attribute__((A)) __attribute__((B))` → `__attribute__((A, B))`.
    Loop until fixed point so 3+ consecutive attributes collapse too. Then
    drop any attribute that sits between a declarator-identifier and `=`,
    since tree-sitter-c rejects that position."""
    prev = None
    while prev != text:
        prev = text
        text = CONSECUTIVE_ATTR_RE.sub(r"__attribute__((\1, \2))", text)
    text = ATTR_BEFORE_EQ_RE.sub(r"\2", text)
    return text


def fold_pp_continuations(text: str) -> str:
    """Join `\\`-continued preprocessor directives onto their first physical
    line so unifdef can evaluate them. Subsequent continuation lines become
    blank to preserve total line count.

    unifdef refuses to evaluate `#if` / `#elif` expressions split across
    multiple physical lines ("Obfuscated preprocessor control line"), which
    causes it to bail on the entire file."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    pp_re = re.compile(r"^\s*#")
    while i < len(lines):
        line = lines[i]
        if pp_re.match(line) and line.rstrip().endswith("\\"):
            parts = [line.rstrip()[:-1]]  # strip the trailing backslash
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.rstrip().endswith("\\"):
                    parts.append(nxt.rstrip()[:-1])
                    j += 1
                else:
                    parts.append(nxt)
                    break
            n_cont = j - i
            joined = " ".join(p.strip() for p in parts)
            out.append(joined)
            out.extend([""] * n_cont)
            i = j + 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


_TYPE_ARG_HEAD_RE = re.compile(r"\b(" + "|".join(TYPE_ARG_MACROS) + r")\s*\(")


def _balanced_close(text: str, start: int) -> int:
    """Given text[start-1] == '(' (open paren just consumed), return the index
    AFTER the matching ')'. -1 if unbalanced."""
    depth = 1
    i = start
    while i < len(text):
        c = text[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def rewrite_type_arg_macros(text: str) -> str:
    """Make container_of/offsetof/max_t/va_arg etc. parseable.

    Uses manual paren balancing rather than regex inner groups so it handles
    arbitrarily-nested arg expressions like `max_t(unsigned int,
    RPS_MAP_SIZE(cpumask_weight(mask)), L1_CACHE_BYTES)`.

    Specific fixups:
      - strip `struct`/`union`/`enum` keywords inside the args (universal)
      - `offsetof(TYPE, m.n.o)` → `offsetof(TYPE, m)` (tree-sitter rule allows
        only a single field_identifier as the designator)
      - `max_t(TYPE, ...)` → `max_t(T, ...)` (multi-word types like
        `unsigned int` aren't expressions)
      - `va_arg(ap, TYPE)` → `va_arg(ap, T)` (same reason)
    """
    type_first  = {"max_t", "min_t", "clamp_t"}
    type_second = {"va_arg", "__builtin_va_arg"}
    out: list[str] = []
    i = 0
    while True:
        m = _TYPE_ARG_HEAD_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        head = m.group(1)
        end = _balanced_close(text, m.end())
        if end < 0:
            out.append(text[m.start():])
            break
        args = text[m.end():end - 1]
        stripped = STRUCT_KW_RE.sub(r"\2", args)
        if head == "offsetof":
            parts = stripped.split(",", 1)
            if len(parts) == 2:
                type_part, design = parts
                m2 = re.match(r"\s*([A-Za-z_]\w*)", design)
                if m2:
                    design = " " + m2.group(1)
                stripped = type_part + "," + design
        elif head in type_first:
            parts = stripped.split(",", 1)
            if len(parts) >= 2:
                stripped = "T," + parts[1]
        elif head in type_second:
            parts = stripped.split(",", 1)
            if len(parts) >= 2:
                stripped = parts[0] + ", T"
        out.append(f"{head}({stripped})")
        i = end
    return "".join(out)


def preprocess_source(src: bytes, unifdef_flags: list[str]) -> bytes:
    """Resolve KERNEL_VERSION + IS_ENABLED, rewrite kernel-isms, run unifdef -b.
    Line numbers are preserved — `unifdef -b` blanks out removed lines and our
    rewriters only replace tokens in place, so file:line positions in the
    resulting AST still match the *original* file. The original file can be
    read for human-readable slices (e.g. by a `slice` command)."""
    text = src.decode("utf-8", errors="replace")
    text = rewrite_ifdef_zero(text)
    text = rewrite_variadic_macros(text)
    text = fold_pp_continuations(text)
    text = strip_define_comments(text)
    text = strip_pp_trailing_ws(text)
    text = resolve_kernel_version(text)
    text = IS_ENABLED_RE.sub(r"defined(\1)", text)
    text = rewrite_foreach(text)
    text = rewrite_type_arg_macros(text)
    text = rewrite_decl_macros(text)
    text = rewrite_bare_attributes(text)
    text = rewrite_consecutive_attrs(text)
    if not unifdef_flags or UNIFDEF_BIN is None:
        return text.encode("utf-8")
    r = subprocess.run(
        [UNIFDEF_BIN, "-b", *unifdef_flags],
        input=text, capture_output=True, text=True,
    )
    # unifdef returns 0 (unchanged) or 1 (changed) on success, 2 on error.
    if r.returncode == 2:
        return text.encode("utf-8")
    return r.stdout.encode("utf-8")


# ---------- data model ----------

@dataclass
class Symbol:
    name: str
    kind: str             # function | type | variable | constant | macro | field | module
    file: str
    line: int             # 1-based
    col: int              # 0-based
    end_line: int
    is_definition: bool
    language: str         # c | cpp | java | go | rust | python | javascript | typescript
    kind_raw: Optional[str] = None        # original AST node type (debug / precise filter)
    modifiers: Optional[list[str]] = None # ["static","extern","inline","public","async",...]
    parent: Optional[str] = None          # enclosing class/namespace/module
    signature: Optional[str] = None
    return_type: Optional[str] = None
    enum_values: Optional[list[str]] = None
    params: Optional[list[str]] = None


@dataclass
class Ref:
    name: str
    kind: str           # "call" | "type" | "name"
    file: str
    line: int
    col: int
    language: str


# ---------- AST helpers ----------

def find_innermost_function_name(declarator):
    """Walk a declarator chain; return the identifier-like node that names the
    function, or None. Accepts `identifier` (C) or `field_identifier` (C++
    method) as the terminal name."""
    node = declarator
    while node is not None:
        if node.type == "function_declarator":
            inner = node.child_by_field_name("declarator")
            if inner is None:
                return None
            if inner.type in ("identifier", "field_identifier"):
                return inner
            node = inner
            continue
        if node.type in ("pointer_declarator", "reference_declarator",
                         "init_declarator", "parenthesized_declarator"):
            node = node.child_by_field_name("declarator")
            continue
        return None
    return None


def find_identifier_in_declarator(declarator):
    """For non-function declarators, find the variable name identifier."""
    node = declarator
    seen = 0
    while node is not None and seen < 16:
        seen += 1
        if node is None:
            return None
        if node.type == "identifier":
            return node
        if node.type in ("pointer_declarator", "init_declarator", "array_declarator",
                         "parenthesized_declarator"):
            node = node.child_by_field_name("declarator")
            continue
        return None
    return None


def declarator_is_function(declarator):
    node = declarator
    seen = 0
    while node is not None and seen < 16:
        seen += 1
        if node.type == "function_declarator":
            return True
        if node.type in ("pointer_declarator", "init_declarator", "array_declarator",
                         "parenthesized_declarator"):
            node = node.child_by_field_name("declarator")
            continue
        return False
    return False


def extract_storage_inline(node, src):
    storage = None
    is_inline = False
    for c in node.children:
        if c.type == "storage_class_specifier":
            kw = text(c, src)
            if kw in ("static", "extern"):
                storage = kw
        if c.type == "function_specifier" and text(c, src) == "inline":
            is_inline = True
        if c.type == "type_qualifier" and text(c, src) == "inline":
            is_inline = True
        if c.is_named is False and text(c, src) == "inline":
            is_inline = True
    return storage, is_inline


def extract_return_type(fn_node, src):
    decl = fn_node.child_by_field_name("declarator")
    parts = []
    for c in fn_node.children:
        if c == decl:
            break
        if c.type in ("storage_class_specifier", "function_specifier"):
            continue
        if text(c, src) == "inline":
            continue
        parts.append(text(c, src))
    return " ".join(parts).strip() or None


def extract_param_names(declarator, src) -> list[str]:
    """Walk to the innermost function_declarator and collect parameter names.
    Used by call-graph filters to suppress refs that are actually local
    parameters shadowing a same-named file-scope symbol."""
    node = declarator
    seen = 0
    while node is not None and seen < 16:
        seen += 1
        if node.type == "function_declarator":
            pl = node.child_by_field_name("parameters")
            if pl is None:
                return []
            names: list[str] = []
            for c in pl.named_children:
                if c.type == "parameter_declaration":
                    d = c.child_by_field_name("declarator")
                    if d is not None:
                        ident = find_identifier_in_declarator(d)
                        if ident is not None:
                            names.append(text(ident, src))
            return names
        if node.type in ("pointer_declarator", "init_declarator", "parenthesized_declarator"):
            node = node.child_by_field_name("declarator")
            continue
        return []
    return []


def signature_of_function_def(fn_node, src):
    body = fn_node.child_by_field_name("body")
    if body is None:
        return None
    sig = src[fn_node.start_byte:body.start_byte].decode("utf-8", "replace")
    return " ".join(sig.split())


# ---------- extraction ----------

def is_typedef_decl(node, src):
    for c in node.children:
        if c.type == "storage_class_specifier" and text(c, src) == "typedef":
            return True
    return False


def collect_declarators(node):
    """Yield direct declarator children of a declaration node (excluding type specifiers)."""
    for c in node.children:
        if c.type in ("init_declarator", "identifier", "pointer_declarator",
                      "array_declarator", "function_declarator",
                      "parenthesized_declarator"):
            yield c


def c_modifiers(storage: Optional[str], is_inline: bool) -> Optional[list[str]]:
    mods = []
    if storage:
        mods.append(storage)
    if is_inline:
        mods.append("inline")
    return mods or None


def add_function_def(node, src, rel, out):
    decl = node.child_by_field_name("declarator")
    if decl is None:
        return
    name_node = find_innermost_function_name(decl)
    if name_node is None:
        return
    storage, is_inline = extract_storage_inline(node, src)
    out.append(Symbol(
        name=text(name_node, src),
        kind="function",
        file=rel,
        line=node.start_point[0] + 1,
        col=node.start_point[1],
        end_line=node.end_point[0] + 1,
        is_definition=True,
        language="c",
        kind_raw="function",
        modifiers=c_modifiers(storage, is_inline),
        signature=signature_of_function_def(node, src),
        return_type=extract_return_type(node, src),
        params=extract_param_names(decl, src) or None,
    ))


def add_declaration(node, src, rel, out):
    # Skip typedefs (handled via type_definition node by tree-sitter-c)
    if is_typedef_decl(node, src):
        return
    # Only emit top-level (file-scope) declarations.
    # Parents inside compound_statement (function bodies) or struct fields are local.
    p = node.parent
    if p is None or p.type != "translation_unit":
        # Allow preproc_if/preproc_ifdef wrappers (#if static int foo; #endif)
        while p is not None and p.type in ("preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "linkage_specification"):
            p = p.parent
        if p is None or p.type != "translation_unit":
            return
    storage, is_inline = extract_storage_inline(node, src)
    for d in collect_declarators(node):
        target = d
        if d.type == "init_declarator":
            inner = d.child_by_field_name("declarator")
            if inner is not None:
                target = inner
        if declarator_is_function(target):
            name_node = find_innermost_function_name(target)
            if name_node is None:
                continue
            out.append(Symbol(
                name=text(name_node, src), kind="function",
                file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
                end_line=node.end_point[0] + 1, is_definition=False,
                language="c", kind_raw="prototype",
                modifiers=c_modifiers(storage, is_inline),
                signature=" ".join(text(node, src).split()),
                params=extract_param_names(target, src) or None,
            ))
        else:
            name_node = find_identifier_in_declarator(target)
            if name_node is None:
                continue
            out.append(Symbol(
                name=text(name_node, src), kind="variable",
                file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
                end_line=node.end_point[0] + 1,
                is_definition=(storage != "extern"),
                language="c", kind_raw="var",
                modifiers=c_modifiers(storage, False),
                signature=" ".join(text(node, src).split()),
            ))


def add_record(node, src, rel, out):
    """struct_specifier, union_specifier, enum_specifier — DEFINITIONS only (body present)."""
    name_node = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    if name_node is None or body is None:
        return
    raw = node.type.replace("_specifier", "")  # struct | union | enum
    enum_values = None
    if raw == "enum":
        enum_values = []
        for c in body.named_children:
            if c.type == "enumerator":
                n = c.child_by_field_name("name")
                if n is not None:
                    enum_values.append(text(n, src))
    out.append(Symbol(
        name=text(name_node, src), kind="type",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="c", kind_raw=raw,
        enum_values=enum_values,
    ))


def add_typedef(node, src, rel, out):
    """type_definition: collect every declarator name as a typedef."""
    for d in node.children:
        target = None
        if d.type == "type_identifier":
            target = d
        elif d.type in ("pointer_declarator", "array_declarator"):
            target = find_identifier_in_declarator(d) or None
            if target is None:
                # Look for type_identifier inside
                cur = d
                while cur is not None:
                    if cur.type == "type_identifier":
                        target = cur
                        break
                    nxt = cur.child_by_field_name("declarator")
                    if nxt is None:
                        # search children for type_identifier
                        found = None
                        for ch in cur.children:
                            if ch.type == "type_identifier":
                                found = ch
                                break
                        target = found
                        break
                    cur = nxt
        elif d.type == "function_declarator":
            target = find_innermost_function_name(d)
            if target is None:
                # function-pointer typedef has parenthesized_declarator; skip name extraction
                pass
        if target is None:
            continue
        out.append(Symbol(
            name=text(target, src), kind="type",
            file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
            end_line=node.end_point[0] + 1, is_definition=True,
            language="c", kind_raw="typedef",
            signature=" ".join(text(node, src).split()),
        ))


def add_macro(node, src, rel, out, fn_form: bool):
    name = node.child_by_field_name("name")
    if name is None:
        return
    if fn_form:
        params = node.child_by_field_name("parameters")
        sig = f"#define {text(name, src)}" + (text(params, src) if params else "")
    else:
        value = node.child_by_field_name("value")
        sig = f"#define {text(name, src)}" + (f" {text(value, src)}" if value else "")
    out.append(Symbol(
        name=text(name, src),
        # Function-like macros are callable → kind="function".
        # Object-like macros are values → kind="constant".
        # Both keep kind_raw so callers can filter precisely.
        kind="function" if fn_form else "constant",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="c",
        kind_raw="preproc_function_def" if fn_form else "preproc_def",
        signature=" ".join(sig.split()),
    ))


def c_walk_definitions(root, src, rel, syms):
    """One pass: visit all relevant definition-bearing nodes (including nested)."""
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "function_definition":
            add_function_def(node, src, rel, syms)
            # Don't descend; bodies don't contain top-level defs in C.
            continue
        if nt == "declaration":
            add_declaration(node, src, rel, syms)
            # Declarations may host inline struct/enum defs in the type — descend.
            stack.extend(node.children)
            continue
        if nt == "type_definition":
            add_typedef(node, src, rel, syms)
            stack.extend(node.children)
            continue
        if nt in ("struct_specifier", "union_specifier", "enum_specifier"):
            add_record(node, src, rel, syms)
            stack.extend(node.children)
            continue
        if nt == "preproc_def":
            add_macro(node, src, rel, syms, fn_form=False)
            continue
        if nt == "preproc_function_def":
            add_macro(node, src, rel, syms, fn_form=True)
            continue
        stack.extend(node.children)


def c_walk_refs(root, src, rel, refs, defined_names, identifiers_out, language="c"):
    """Collect refs (calls, type uses, name mentions) AND the set of all distinct
    identifier names that appear anywhere in the file. The identifier set is used
    by incremental builds to detect which unchanged files need a re-Pass2 when a
    new defined symbol appears in a changed file.

    Kinds:
      - call: identifier in call_expression.function
      - type: type_identifier (except when naming a record/typedef definition)
      - name: bare identifier whose name is a known defined symbol
    """
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                name = text(fn, src)
                identifiers_out.add(name)
                refs.append(Ref(name=name, kind="call",
                                file=rel, line=fn.start_point[0] + 1, col=fn.start_point[1],
                                language=language))
                # Don't double-record this identifier under "name" — stop descent into fn child.
                for c in node.children:
                    if c is not fn:
                        stack.append(c)
                continue
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            if name in defined_names:
                refs.append(Ref(name=name, kind="name",
                                file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
                                language=language))
        elif nt == "type_identifier":
            parent = node.parent
            is_def_name = False
            if parent is not None:
                if parent.type in ("struct_specifier", "union_specifier", "enum_specifier"):
                    name_field = parent.child_by_field_name("name")
                    body = parent.child_by_field_name("body")
                    # Only treat as a "definition name" when there's a body — otherwise it's a use.
                    if name_field == node and body is not None:
                        is_def_name = True
                elif parent.type == "type_definition":
                    # The declarator side of typedef is the new name → that IS a def, not a ref.
                    # tree-sitter-c: type_definition has type field (original) + declarator field (new name)
                    declarator_field = parent.child_by_field_name("declarator")
                    if declarator_field == node:
                        is_def_name = True
            name = text(node, src)
            identifiers_out.add(name)
            if not is_def_name:
                refs.append(Ref(name=name, kind="type",
                                file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
                                language=language))
        stack.extend(node.children)


# ---------- Python walker ----------

def py_extract_function(node, src: bytes, rel: str, parent: Optional[str],
                        out: list, extra_modifiers: list[str]):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    params_node = node.child_by_field_name("parameters")
    param_names: list[str] = []
    if params_node is not None:
        for c in params_node.named_children:
            n = None
            if c.type == "identifier":
                n = c
            elif c.type in ("typed_parameter", "default_parameter",
                            "typed_default_parameter", "list_splat_pattern",
                            "dictionary_splat_pattern"):
                n = c.child_by_field_name("name")
                if n is None:
                    for ch in c.children:
                        if ch.type == "identifier":
                            n = ch
                            break
            if n is not None:
                param_names.append(text(n, src))
    mods = list(extra_modifiers)
    # `async def` adds a leading `async` token child.
    for ch in node.children:
        if ch.type == "async":
            mods.append("async")
            break
    return_type = None
    rt = node.child_by_field_name("return_type")
    if rt is not None:
        return_type = text(rt, src)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte:body.start_byte].decode("utf-8", "replace")
        sig = " ".join(sig.split())
    else:
        sig = text(node, src).split("\n", 1)[0]
    out.append(Symbol(
        name=name, kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1,
        is_definition=True,
        language="python", kind_raw="function_definition",
        modifiers=mods or None,
        parent=parent,
        signature=sig,
        return_type=return_type,
        params=param_names or None,
    ))


def py_extract_class(node, src: bytes, rel: str, parent: Optional[str],
                     out: list, extra_modifiers: list[str]):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls_name = text(name_node, src)
    out.append(Symbol(
        name=cls_name, kind="type",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1,
        is_definition=True,
        language="python", kind_raw="class_definition",
        modifiers=extra_modifiers or None,
        parent=parent,
    ))
    body = node.child_by_field_name("body")
    if body is None:
        return
    inner_parent = (parent + "." + cls_name) if parent else cls_name
    for stmt in body.children:
        if stmt.type == "function_definition":
            py_extract_function(stmt, src, rel, inner_parent, out, [])
        elif stmt.type == "class_definition":
            py_extract_class(stmt, src, rel, inner_parent, out, [])
        elif stmt.type == "decorated_definition":
            py_extract_decorated(stmt, src, rel, inner_parent, out)
        elif stmt.type == "expression_statement":
            for inner in stmt.children:
                if inner.type == "assignment":
                    py_extract_assignment(inner, src, rel, inner_parent, out, is_class_attr=True)


def py_extract_decorated(node, src: bytes, rel: str, parent: Optional[str], out: list):
    decorators: list[str] = []
    target = None
    for ch in node.children:
        if ch.type == "decorator":
            dt = text(ch, src).lstrip("@").strip().split("\n", 1)[0]
            m = re.match(r"[A-Za-z_][\w.]*", dt)
            decorators.append(m.group(0) if m else dt)
        elif ch.type == "function_definition":
            target = ("function", ch)
        elif ch.type == "class_definition":
            target = ("class", ch)
    if target is None:
        return
    kind, t = target
    if kind == "function":
        py_extract_function(t, src, rel, parent, out, decorators)
    else:
        py_extract_class(t, src, rel, parent, out, decorators)


def py_extract_assignment(node, src: bytes, rel: str, parent: Optional[str],
                          out: list, is_class_attr: bool = False):
    left = node.child_by_field_name("left")
    if left is None:
        return
    targets: list = []
    if left.type == "identifier":
        targets.append(left)
    elif left.type in ("pattern_list", "tuple_pattern"):
        for c in left.children:
            if c.type == "identifier":
                targets.append(c)
    for t in targets:
        name = text(t, src)
        is_const = name.isupper() and len(name) > 1
        sig = " ".join(text(node, src).split())
        out.append(Symbol(
            name=name,
            # Class attributes are also variables — `parent` distinguishes
            # them from module-level globals.
            kind="constant" if (is_const and not is_class_attr) else "variable",
            file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
            end_line=node.end_point[0] + 1,
            is_definition=True,
            language="python", kind_raw="assignment",
            parent=parent,
            signature=sig[:200],
        ))


def py_walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        if node.type == "function_definition":
            py_extract_function(node, src, rel, None, syms, [])
        elif node.type == "class_definition":
            py_extract_class(node, src, rel, None, syms, [])
        elif node.type == "decorated_definition":
            py_extract_decorated(node, src, rel, None, syms)
        elif node.type == "expression_statement":
            for inner in node.children:
                if inner.type == "assignment":
                    py_extract_assignment(inner, src, rel, None, syms)


def py_walk_refs(root, src: bytes, rel: str, refs: list,
                 defined_names: set, identifiers_out: set, language: str = "python"):
    """Collect call sites + name mentions. Same ref kinds as the C walker."""
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call":
            fn = node.child_by_field_name("function")
            if fn is not None:
                target_name = None
                if fn.type == "identifier":
                    target_name = text(fn, src)
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr is not None and attr.type == "identifier":
                        target_name = text(attr, src)
                if target_name is not None:
                    identifiers_out.add(target_name)
                    refs.append(Ref(name=target_name, kind="call",
                                    file=rel,
                                    line=node.start_point[0] + 1,
                                    col=node.start_point[1],
                                    language=language))
        elif nt == "identifier":
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if pt in ("function_definition", "class_definition"):
                    if parent.child_by_field_name("name") == node:
                        skip = True
                elif pt == "parameters":
                    skip = True
                elif pt in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                    if parent.child_by_field_name("name") == node or parent.children[0] == node:
                        skip = True
                elif pt == "assignment":
                    if parent.child_by_field_name("left") == node:
                        skip = True
            name = text(node, src)
            identifiers_out.add(name)
            if not skip and name in defined_names:
                refs.append(Ref(name=name, kind="name",
                                file=rel,
                                line=node.start_point[0] + 1,
                                col=node.start_point[1],
                                language=language))
        stack.extend(node.children)


# ---------- Go walker ----------

def go_extract_param_names(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type != "parameter_declaration":
            continue
        for ch in c.named_children:
            if ch.type == "identifier":
                names.append(text(ch, src))
    return names


def go_extract_function(node, src: bytes, rel: str, out: list, is_method: bool = False):
    if is_method:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for c in node.children:
                if c.type == "field_identifier":
                    name_node = c
                    break
    else:
        name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    # Receiver of methods → parent (the receiver type name)
    parent = None
    if is_method:
        recv = node.child_by_field_name("receiver")
        if recv is not None:
            # parameter_list with one parameter_declaration; find its type_identifier
            for c in recv.named_children:
                if c.type == "parameter_declaration":
                    for ch in c.named_children:
                        if ch.type == "type_identifier":
                            parent = text(ch, src)
                            break
                        if ch.type == "pointer_type":
                            for x in ch.named_children:
                                if x.type == "type_identifier":
                                    parent = text(x, src)
                                    break
                            break
    params = node.child_by_field_name("parameters")
    return_type = None
    rt = node.child_by_field_name("result")
    if rt is not None:
        return_type = text(rt, src)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte:body.start_byte].decode("utf-8", "replace")
        sig = " ".join(sig.split())
    else:
        sig = text(node, src).split("\n", 1)[0]
    # Go exported (uppercase first letter) → "exported" modifier
    mods = ["exported"] if name and name[0].isupper() else []
    out.append(Symbol(
        name=name, kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="go", kind_raw=node.type,
        modifiers=mods or None,
        parent=parent,
        signature=sig,
        return_type=return_type,
        params=go_extract_param_names(params, src) or None,
    ))


def go_extract_type(node, src: bytes, rel: str, out: list):
    """type_declaration may contain multiple type_specs."""
    for spec in node.children:
        if spec.type != "type_spec":
            continue
        nm = spec.child_by_field_name("name")
        if nm is None:
            for c in spec.children:
                if c.type == "type_identifier":
                    nm = c
                    break
        if nm is None:
            continue
        name = text(nm, src)
        kind_raw = "type"
        for c in spec.children:
            if c.type in ("struct_type", "interface_type"):
                kind_raw = c.type.replace("_type", "")
        mods = ["exported"] if name and name[0].isupper() else []
        out.append(Symbol(
            name=name, kind="type",
            file=rel, line=spec.start_point[0] + 1, col=spec.start_point[1],
            end_line=spec.end_point[0] + 1, is_definition=True,
            language="go", kind_raw=kind_raw,
            modifiers=mods or None,
        ))


def go_extract_const_or_var(node, src: bytes, rel: str, out: list, is_const: bool):
    spec_type = "const_spec" if is_const else "var_spec"
    out_kind = "constant" if is_const else "variable"
    for spec in node.children:
        if spec.type != spec_type:
            continue
        for c in spec.named_children:
            if c.type != "identifier":
                continue
            name = text(c, src)
            mods = ["exported"] if name and name[0].isupper() else []
            out.append(Symbol(
                name=name, kind=out_kind,
                file=rel, line=spec.start_point[0] + 1, col=spec.start_point[1],
                end_line=spec.end_point[0] + 1, is_definition=True,
                language="go", kind_raw=spec_type,
                modifiers=mods or None,
                signature=" ".join(text(spec, src).split())[:200],
            ))


def go_walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        if node.type == "function_declaration":
            go_extract_function(node, src, rel, syms, is_method=False)
        elif node.type == "method_declaration":
            go_extract_function(node, src, rel, syms, is_method=True)
        elif node.type == "type_declaration":
            go_extract_type(node, src, rel, syms)
        elif node.type == "const_declaration":
            go_extract_const_or_var(node, src, rel, syms, is_const=True)
        elif node.type == "var_declaration":
            go_extract_const_or_var(node, src, rel, syms, is_const=False)


def go_walk_refs(root, src: bytes, rel: str, refs: list,
                 defined_names: set, identifiers_out: set, language: str = "go"):
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                target = None
                if fn.type == "identifier":
                    target = text(fn, src)
                elif fn.type == "selector_expression":
                    # `pkg.Func()` or `obj.Method()` → take rightmost field
                    field = fn.child_by_field_name("field")
                    if field is not None:
                        target = text(field, src)
                if target is not None:
                    identifiers_out.add(target)
                    refs.append(Ref(name=target, kind="call",
                                    file=rel, line=node.start_point[0] + 1,
                                    col=node.start_point[1], language=language))
        elif nt == "type_identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            is_def = parent is not None and parent.type == "type_spec" \
                     and parent.child_by_field_name("name") == node
            if not is_def:
                refs.append(Ref(name=name, kind="type",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            # Skip identifiers that are themselves definition names
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if pt == "function_declaration" and parent.child_by_field_name("name") == node:
                    skip = True
                elif pt in ("const_spec", "var_spec"):
                    # only skip if it's a name (first identifier child); refs in
                    # initialiser expressions are children further along
                    skip = node in parent.named_children and \
                           parent.named_children[0] == node and \
                           not any(c.type == "expression_list" and node in c.named_children
                                   for c in parent.children)
                elif pt == "parameter_declaration":
                    skip = True
            if not skip and name in defined_names:
                refs.append(Ref(name=name, kind="name",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        stack.extend(node.children)


# ---------- Rust walker ----------

def rs_visibility(node, src: bytes) -> Optional[str]:
    for c in node.children:
        if c.type == "visibility_modifier":
            return text(c, src)
    return None


def rs_params(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type == "self_parameter":
            names.append("self")
        elif c.type == "parameter":
            # `pattern: type` or `mut name: type`
            pat = c.child_by_field_name("pattern")
            if pat is None:
                continue
            if pat.type == "identifier":
                names.append(text(pat, src))
            else:
                # find first identifier
                stack = [pat]
                while stack:
                    n = stack.pop()
                    if n.type == "identifier":
                        names.append(text(n, src))
                        break
                    stack.extend(n.children)
    return names


def rs_extract_function(node, src: bytes, rel: str, parent: Optional[str], out: list):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    vis = rs_visibility(node, src)
    mods: list[str] = []
    if vis:
        mods.append(vis)  # "pub", "pub(crate)", etc.
    for c in node.children:
        if c.type == "function_modifiers":
            # async / unsafe / extern etc.
            for m in c.children:
                if m.is_named or m.type in ("async", "unsafe", "const"):
                    mods.append(text(m, src))
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte:body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src)
    sig = " ".join(sig.split())
    return_type = None
    rt = node.child_by_field_name("return_type")
    if rt is not None:
        return_type = text(rt, src)
    params = node.child_by_field_name("parameters")
    out.append(Symbol(
        name=name, kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1,
        is_definition=(node.type == "function_item"),
        language="rust", kind_raw=node.type,
        modifiers=mods or None,
        parent=parent,
        signature=sig,
        return_type=return_type,
        params=rs_params(params, src) or None,
    ))


def rs_extract_type(node, src: bytes, rel: str, out: list):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for c in node.children:
            if c.type == "type_identifier":
                name_node = c
                break
    if name_node is None:
        return
    vis = rs_visibility(node, src)
    mods = [vis] if vis else None
    raw_map = {"struct_item": "struct", "enum_item": "enum",
               "trait_item": "trait", "type_item": "type_alias", "union_item": "union"}
    enum_values = None
    if node.type == "enum_item":
        body = node.child_by_field_name("body")
        if body is None:
            for c in node.children:
                if c.type == "enum_variant_list":
                    body = c
                    break
        if body is not None:
            enum_values = []
            for v in body.named_children:
                if v.type == "enum_variant":
                    n = v.child_by_field_name("name")
                    if n is None:
                        for ch in v.children:
                            if ch.type == "identifier":
                                n = ch
                                break
                    if n is not None:
                        enum_values.append(text(n, src))
    out.append(Symbol(
        name=text(name_node, src), kind="type",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="rust", kind_raw=raw_map.get(node.type, node.type),
        modifiers=mods,
        enum_values=enum_values,
    ))


def rs_extract_const_or_static(node, src: bytes, rel: str, out: list):
    name_node = None
    for c in node.children:
        if c.type == "identifier":
            name_node = c
            break
    if name_node is None:
        return
    vis = rs_visibility(node, src)
    mods = [vis] if vis else None
    is_const = node.type == "const_item"
    out.append(Symbol(
        name=text(name_node, src),
        kind="constant" if is_const else "variable",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="rust", kind_raw=node.type,
        modifiers=mods,
        signature=" ".join(text(node, src).split())[:200],
    ))


def rs_extract_impl(node, src: bytes, rel: str, out: list):
    """impl blocks: methods inside get parent = the impl's type name."""
    # impl_item children: optional `impl`, optional trait type_identifier (with `for`),
    # required type_identifier (the type), declaration_list
    type_names = [text(c, src) for c in node.children if c.type == "type_identifier"]
    impl_type = type_names[-1] if type_names else None
    decls = None
    for c in node.children:
        if c.type == "declaration_list":
            decls = c
            break
    if decls is None:
        return
    for child in decls.children:
        if child.type in ("function_item", "function_signature_item"):
            rs_extract_function(child, src, rel, impl_type, out)


def rs_extract_macro(node, src: bytes, rel: str, out: list):
    """macro_rules! foo { ... } — treat as a callable (kind=function) since
    macro invocations look like calls (`foo!(args)`)."""
    name_node = None
    for c in node.children:
        if c.type == "identifier":
            name_node = c
            break
    if name_node is None:
        return
    out.append(Symbol(
        name=text(name_node, src), kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="rust", kind_raw="macro_definition",
        modifiers=["macro_rules"],
    ))


def rs_walk_definitions(root, src: bytes, rel: str, syms: list):
    stack = list(root.children)
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "function_item":
            rs_extract_function(node, src, rel, None, syms)
        elif nt == "function_signature_item":
            # bare trait method signature (no body) — top-level form is rare
            rs_extract_function(node, src, rel, None, syms)
        elif nt in ("struct_item", "enum_item", "trait_item", "type_item", "union_item"):
            rs_extract_type(node, src, rel, syms)
        elif nt in ("const_item", "static_item"):
            rs_extract_const_or_static(node, src, rel, syms)
        elif nt == "impl_item":
            rs_extract_impl(node, src, rel, syms)
        elif nt == "macro_definition":
            rs_extract_macro(node, src, rel, syms)
        elif nt == "mod_item":
            # descend into modules
            body = None
            for c in node.children:
                if c.type == "declaration_list":
                    body = c
                    break
            if body is not None:
                stack.extend(body.children)


def rs_walk_refs(root, src: bytes, rel: str, refs: list,
                 defined_names: set, identifiers_out: set, language: str = "rust"):
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None:
                target = None
                if fn.type == "identifier":
                    target = text(fn, src)
                elif fn.type == "field_expression":
                    f = fn.child_by_field_name("field")
                    if f is not None:
                        target = text(f, src)
                elif fn.type == "scoped_identifier":
                    n = fn.child_by_field_name("name")
                    if n is not None:
                        target = text(n, src)
                if target is not None:
                    identifiers_out.add(target)
                    refs.append(Ref(name=target, kind="call",
                                    file=rel, line=node.start_point[0] + 1,
                                    col=node.start_point[1], language=language))
        elif nt == "macro_invocation":
            # `foo!(args)` — capture as call to foo
            mn = node.child_by_field_name("macro")
            if mn is not None:
                if mn.type == "identifier":
                    name = text(mn, src)
                elif mn.type == "scoped_identifier":
                    n = mn.child_by_field_name("name")
                    name = text(n, src) if n else None
                else:
                    name = None
                if name:
                    identifiers_out.add(name)
                    refs.append(Ref(name=name, kind="call",
                                    file=rel, line=node.start_point[0] + 1,
                                    col=node.start_point[1], language=language))
        elif nt == "type_identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            is_def = False
            if parent is not None and parent.type in (
                    "struct_item", "enum_item", "trait_item", "type_item", "union_item"):
                nf = parent.child_by_field_name("name")
                if nf == node:
                    is_def = True
            if not is_def:
                refs.append(Ref(name=name, kind="type",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if pt in ("function_item", "function_signature_item") and \
                        parent.child_by_field_name("name") == node:
                    skip = True
                elif pt in ("const_item", "static_item", "macro_definition"):
                    if parent.children and node == next(
                            (c for c in parent.children if c.type == "identifier"), None):
                        skip = True
            if not skip and name in defined_names:
                refs.append(Ref(name=name, kind="name",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        stack.extend(node.children)


# ---------- Java walker ----------

def java_modifiers(node, src: bytes) -> list[str]:
    out: list[str] = []
    for c in node.children:
        if c.type == "modifiers":
            for m in c.children:
                if m.is_named or m.type in ("public", "private", "protected", "static",
                                            "final", "abstract", "synchronized", "native",
                                            "default", "transient", "volatile", "strictfp"):
                    out.append(text(m, src))
            break
    return out


def java_params(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type in ("formal_parameter", "spread_parameter"):
            n = c.child_by_field_name("name")
            if n is not None:
                names.append(text(n, src))
    return names


def java_extract_method(node, src: bytes, rel: str, parent: Optional[str], out: list,
                        is_constructor: bool = False):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods = java_modifiers(node, src)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte:body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    rt = node.child_by_field_name("type")
    return_type = text(rt, src) if rt is not None else None
    params = node.child_by_field_name("parameters")
    out.append(Symbol(
        name=name, kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1,
        is_definition=(body is not None),
        language="java",
        kind_raw="constructor_declaration" if is_constructor else "method_declaration",
        modifiers=mods or None,
        parent=parent,
        signature=sig,
        return_type=return_type,
        params=java_params(params, src) or None,
    ))


def java_extract_field(node, src: bytes, rel: str, parent: Optional[str], out: list):
    mods = java_modifiers(node, src)
    is_const = "static" in mods and "final" in mods
    type_node = node.child_by_field_name("type")
    type_text = text(type_node, src) if type_node is not None else ""
    for decl in node.children:
        if decl.type != "variable_declarator":
            continue
        n = decl.child_by_field_name("name")
        if n is None:
            continue
        name = text(n, src)
        out.append(Symbol(
            name=name,
            kind="constant" if is_const else "variable",
            file=rel, line=decl.start_point[0] + 1, col=decl.start_point[1],
            end_line=decl.end_point[0] + 1, is_definition=True,
            language="java", kind_raw="field_declaration",
            modifiers=mods or None,
            parent=parent,
            signature=f"{type_text} {text(decl, src)}".strip(),
        ))


def java_extract_class(node, src: bytes, rel: str, parent: Optional[str],
                       out: list, kind_raw: str = "class_declaration"):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls_name = text(name_node, src)
    mods = java_modifiers(node, src)
    out.append(Symbol(
        name=cls_name, kind="type",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="java", kind_raw=kind_raw,
        modifiers=mods or None,
        parent=parent,
    ))
    body = node.child_by_field_name("body")
    if body is None:
        return
    inner = (parent + "." + cls_name) if parent else cls_name
    for stmt in body.children:
        if stmt.type == "method_declaration":
            java_extract_method(stmt, src, rel, inner, out)
        elif stmt.type == "constructor_declaration":
            java_extract_method(stmt, src, rel, inner, out, is_constructor=True)
        elif stmt.type == "field_declaration":
            java_extract_field(stmt, src, rel, inner, out)
        elif stmt.type == "class_declaration":
            java_extract_class(stmt, src, rel, inner, out, "class_declaration")
        elif stmt.type == "interface_declaration":
            java_extract_class(stmt, src, rel, inner, out, "interface_declaration")
        elif stmt.type == "enum_declaration":
            java_extract_enum(stmt, src, rel, inner, out)


def java_extract_enum(node, src: bytes, rel: str, parent: Optional[str], out: list):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods = java_modifiers(node, src)
    body = node.child_by_field_name("body")
    enum_values: list[str] = []
    if body is not None:
        for c in body.children:
            if c.type == "enum_constant":
                n = c.child_by_field_name("name")
                if n is not None:
                    enum_values.append(text(n, src))
    out.append(Symbol(
        name=name, kind="type",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="java", kind_raw="enum_declaration",
        modifiers=mods or None,
        parent=parent,
        enum_values=enum_values or None,
    ))


def java_walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        if node.type == "class_declaration":
            java_extract_class(node, src, rel, None, syms, "class_declaration")
        elif node.type == "interface_declaration":
            java_extract_class(node, src, rel, None, syms, "interface_declaration")
        elif node.type == "enum_declaration":
            java_extract_enum(node, src, rel, None, syms)


def java_walk_refs(root, src: bytes, rel: str, refs: list,
                   defined_names: set, identifiers_out: set, language: str = "java"):
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "method_invocation":
            n = node.child_by_field_name("name")
            if n is not None:
                name = text(n, src)
                identifiers_out.add(name)
                refs.append(Ref(name=name, kind="call",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        elif nt == "object_creation_expression":
            # `new Foo(...)` — capture as call to Foo (the type's constructor)
            t = node.child_by_field_name("type")
            if t is not None and t.type == "type_identifier":
                name = text(t, src)
                identifiers_out.add(name)
                refs.append(Ref(name=name, kind="call",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        elif nt == "type_identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            is_def = False
            if parent is not None and parent.type in (
                    "class_declaration", "interface_declaration", "enum_declaration"):
                nf = parent.child_by_field_name("name")
                if nf == node:
                    is_def = True
            if not is_def:
                refs.append(Ref(name=name, kind="type",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            parent = node.parent
            skip = False
            if parent is not None:
                pt = parent.type
                if pt in ("method_declaration", "constructor_declaration"):
                    if parent.child_by_field_name("name") == node:
                        skip = True
                elif pt == "variable_declarator":
                    if parent.child_by_field_name("name") == node:
                        skip = True
                elif pt == "formal_parameter":
                    if parent.child_by_field_name("name") == node:
                        skip = True
            if not skip and name in defined_names:
                refs.append(Ref(name=name, kind="name",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        stack.extend(node.children)


# ---------- C++ walker ----------

# C++ reuses most of the C extraction logic. The extras are: namespaces (no
# symbol emitted, but children are walked with parent scope), classes
# (kind=type with methods/fields parented), and templates (unwrap to inner).

def cpp_extract_function_def(node, src: bytes, rel: str, parent: Optional[str], out: list):
    decl = node.child_by_field_name("declarator")
    if decl is None:
        return
    name_node = find_innermost_function_name(decl)
    eff_parent = parent
    if name_node is None:
        # qualified_identifier case (`Service::process`): descend to find it
        cur = decl
        while cur is not None and cur.type != "function_declarator":
            cur = cur.child_by_field_name("declarator")
        if cur is not None:
            inner = cur.child_by_field_name("declarator")
            if inner is not None and inner.type == "qualified_identifier":
                scope = inner.child_by_field_name("scope")
                name_field = inner.child_by_field_name("name")
                # tree-sitter-cpp may parse the name as identifier OR type_identifier
                if name_field is not None and name_field.type in ("identifier", "type_identifier",
                                                                  "field_identifier"):
                    name_node = name_field
                    if scope is not None:
                        scope_text = text(scope, src)
                        eff_parent = (eff_parent + "::" + scope_text) if eff_parent else scope_text
    if name_node is None:
        return
    storage, is_inline = extract_storage_inline(node, src)
    out.append(Symbol(
        name=text(name_node, src), kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="cpp", kind_raw="function_definition",
        modifiers=c_modifiers(storage, is_inline),
        parent=eff_parent,
        signature=signature_of_function_def(node, src),
        return_type=extract_return_type(node, src),
        params=extract_param_names(decl, src) or None,
    ))


def cpp_extract_class(node, src: bytes, rel: str, parent: Optional[str], out: list):
    """class_specifier / struct_specifier / union_specifier with a body."""
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    # tree-sitter-cpp puts the body in field_declaration_list (not a "body" field).
    body = None
    for c in node.children:
        if c.type == "field_declaration_list":
            body = c
            break
    cls = text(name_node, src)
    kind_raw = "class" if node.type == "class_specifier" else node.type.replace("_specifier", "")
    out.append(Symbol(
        name=cls, kind="type",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language="cpp", kind_raw=kind_raw,
        parent=parent,
    ))
    if body is None:
        return
    inner_parent = (parent + "::" + cls) if parent else cls
    for stmt in body.children:
        st = stmt.type
        if st == "function_definition":
            cpp_extract_function_def(stmt, src, rel, inner_parent, out)
        elif st == "field_declaration":
            decl = stmt.child_by_field_name("declarator")
            if decl is not None and declarator_is_function(decl):
                # method prototype (no body in field_declaration form)
                nm = find_innermost_function_name(decl)
                if nm is not None:
                    storage, is_inline = extract_storage_inline(stmt, src)
                    out.append(Symbol(
                        name=text(nm, src), kind="function",
                        file=rel, line=stmt.start_point[0]+1, col=stmt.start_point[1],
                        end_line=stmt.end_point[0]+1, is_definition=False,
                        language="cpp", kind_raw="method_declaration",
                        modifiers=c_modifiers(storage, is_inline),
                        parent=inner_parent,
                        signature=" ".join(text(stmt, src).split()),
                        params=extract_param_names(decl, src) or None,
                    ))
            else:
                # data field. tree-sitter-cpp uses `field_identifier` for the name
                # (inside declarator). Find it.
                nm = None
                if decl is not None:
                    if decl.type == "field_identifier":
                        nm = decl
                    else:
                        cur = decl
                        seen = 0
                        while cur is not None and seen < 8:
                            seen += 1
                            if cur.type in ("field_identifier", "identifier"):
                                nm = cur
                                break
                            inner = cur.child_by_field_name("declarator")
                            if inner is None:
                                # search children for field_identifier
                                for ch in cur.children:
                                    if ch.type in ("field_identifier", "identifier"):
                                        nm = ch
                                        break
                                break
                            cur = inner
                if nm is not None:
                    storage, _ = extract_storage_inline(stmt, src)
                    has_const = "const" in text(stmt, src).split("=", 1)[0]
                    is_const = (storage == "static" and has_const)
                    out.append(Symbol(
                        name=text(nm, src),
                        kind="constant" if is_const else "variable",
                        file=rel, line=stmt.start_point[0]+1, col=stmt.start_point[1],
                        end_line=stmt.end_point[0]+1, is_definition=True,
                        language="cpp", kind_raw="field_declaration",
                        modifiers=c_modifiers(storage, False),
                        parent=inner_parent,
                        signature=" ".join(text(stmt, src).split()),
                    ))
        elif st in ("class_specifier", "struct_specifier", "union_specifier"):
            cpp_extract_class(stmt, src, rel, inner_parent, out)
        # Skip access_specifier (public:/private:) etc.


def cpp_walk_one(node, src: bytes, rel: str, parent: Optional[str], out: list):
    """Recursive dispatcher for a single top-level / namespace-scope node."""
    nt = node.type
    if nt == "namespace_definition":
        # No symbol for the namespace itself; children inherit it as parent.
        name_node = node.child_by_field_name("name")
        ns = text(name_node, src) if name_node is not None else None
        inner = (parent + "::" + ns) if (parent and ns) else (ns or parent)
        body = node.child_by_field_name("body")
        if body is not None:
            for c in body.children:
                cpp_walk_one(c, src, rel, inner, out)
    elif nt == "template_declaration":
        # Unwrap: the inner declaration is what gets indexed.
        for c in node.children:
            if c.type in ("function_definition", "class_specifier",
                          "struct_specifier", "declaration"):
                cpp_walk_one(c, src, rel, parent, out)
    elif nt == "function_definition":
        cpp_extract_function_def(node, src, rel, parent, out)
    elif nt in ("class_specifier", "struct_specifier", "union_specifier"):
        # Only emit if it has a body (definition, not just a type reference).
        if node.child_by_field_name("body") is not None:
            cpp_extract_class(node, src, rel, parent, out)
    elif nt == "enum_specifier":
        # Reuse C handler (covers C++ enums fine).
        add_record(node, src, rel, out)
    elif nt == "type_definition":
        add_typedef(node, src, rel, out)
    elif nt == "declaration":
        # File-scope variable/prototype — reuse the C handler.
        add_declaration(node, src, rel, out)
    elif nt == "preproc_def":
        add_macro(node, src, rel, out, fn_form=False)
    elif nt == "preproc_function_def":
        add_macro(node, src, rel, out, fn_form=True)
    elif nt in ("linkage_specification",):  # `extern "C" { ... }`
        for c in node.children:
            cpp_walk_one(c, src, rel, parent, out)


def cpp_walk_definitions(root, src: bytes, rel: str, syms: list):
    for node in root.children:
        cpp_walk_one(node, src, rel, None, syms)


def cpp_walk_refs(root, src: bytes, rel: str, refs: list,
                  defined_names: set, identifiers_out: set, language: str = "cpp"):
    """Like c_walk_refs, but also recognises `obj.method(...)` and `obj->method(...)`
    as calls to `method` (via field_expression)."""
    stack = [root]
    while stack:
        node = stack.pop()
        nt = node.type
        if nt == "call_expression":
            fn = node.child_by_field_name("function")
            target_name = None
            target_node = None
            if fn is not None:
                if fn.type == "identifier":
                    target_name = text(fn, src)
                    target_node = fn
                elif fn.type == "field_expression":
                    # `s.foo` or `s->foo` — take the rightmost member
                    fld = fn.child_by_field_name("field")
                    if fld is not None and fld.type in ("field_identifier", "identifier"):
                        target_name = text(fld, src)
                        target_node = fld
                elif fn.type == "qualified_identifier":
                    n = fn.child_by_field_name("name")
                    if n is not None and n.type in ("identifier", "type_identifier",
                                                    "field_identifier"):
                        target_name = text(n, src)
                        target_node = n
            if target_name is not None:
                identifiers_out.add(target_name)
                refs.append(Ref(name=target_name, kind="call",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
                # Don't double-record the function-position identifier as a name ref.
                for c in node.children:
                    if c is not fn:
                        stack.append(c)
                continue
        elif nt == "identifier":
            name = text(node, src)
            identifiers_out.add(name)
            if name in defined_names:
                refs.append(Ref(name=name, kind="name",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        elif nt == "type_identifier":
            parent = node.parent
            is_def_name = False
            if parent is not None:
                if parent.type in ("struct_specifier", "union_specifier",
                                   "enum_specifier", "class_specifier"):
                    name_field = parent.child_by_field_name("name")
                    body = parent.child_by_field_name("body")
                    if body is None:
                        for ch in parent.children:
                            if ch.type == "field_declaration_list":
                                body = ch; break
                    if name_field == node and body is not None:
                        is_def_name = True
                elif parent.type == "type_definition":
                    if parent.child_by_field_name("declarator") == node:
                        is_def_name = True
            name = text(node, src)
            identifiers_out.add(name)
            if not is_def_name:
                refs.append(Ref(name=name, kind="type",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1], language=language))
        stack.extend(node.children)


# ---------- JS/TS walker (shared) ----------

def js_params(plist, src: bytes) -> list[str]:
    names: list[str] = []
    if plist is None:
        return names
    for c in plist.named_children:
        if c.type == "identifier":
            names.append(text(c, src))
        elif c.type in ("required_parameter", "optional_parameter"):  # TS
            n = c.child_by_field_name("pattern")
            if n is None:
                for ch in c.children:
                    if ch.type == "identifier":
                        n = ch
                        break
            if n is not None:
                names.append(text(n, src))
        elif c.type in ("assignment_pattern",):
            l = c.child_by_field_name("left")
            if l is not None and l.type == "identifier":
                names.append(text(l, src))
        elif c.type == "rest_pattern":
            for ch in c.children:
                if ch.type == "identifier":
                    names.append(text(ch, src))
    return names


def js_extract_function_decl(node, src: bytes, rel: str, parent: Optional[str],
                             out: list, lang: str, extra_mods: list[str] = ()):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods = list(extra_mods)
    for ch in node.children:
        if ch.type == "async":
            mods.append("async")
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte:body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    params = node.child_by_field_name("parameters")
    out.append(Symbol(
        name=name, kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language=lang, kind_raw=node.type,
        modifiers=mods or None,
        parent=parent,
        signature=sig,
        params=js_params(params, src) or None,
    ))


def js_extract_method(node, src: bytes, rel: str, parent: Optional[str], out: list, lang: str):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = text(name_node, src)
    mods: list[str] = []
    # static/async/get/set modifiers come as bare tokens
    for c in node.children:
        if c.type in ("static", "async", "get", "set"):
            mods.append(c.type)
    body = node.child_by_field_name("body")
    if body is not None:
        sig = src[node.start_byte:body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(node, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    params = node.child_by_field_name("parameters")
    out.append(Symbol(
        name=name, kind="function",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language=lang, kind_raw="method_definition",
        modifiers=mods or None,
        parent=parent,
        signature=sig,
        params=js_params(params, src) or None,
    ))


def js_extract_class(node, src: bytes, rel: str, parent: Optional[str], out: list, lang: str):
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    cls = text(name_node, src)
    out.append(Symbol(
        name=cls, kind="type",
        file=rel, line=node.start_point[0] + 1, col=node.start_point[1],
        end_line=node.end_point[0] + 1, is_definition=True,
        language=lang, kind_raw=node.type,
        parent=parent,
    ))
    body = node.child_by_field_name("body")
    if body is None:
        return
    inner = (parent + "." + cls) if parent else cls
    for stmt in body.children:
        if stmt.type == "method_definition":
            js_extract_method(stmt, src, rel, inner, out, lang)
        elif stmt.type == "field_definition":
            n = stmt.child_by_field_name("property") or stmt.child_by_field_name("name")
            if n is None:
                for c in stmt.children:
                    if c.type == "property_identifier":
                        n = c
                        break
            if n is None:
                continue
            mods: list[str] = []
            for c in stmt.children:
                if c.type == "static":
                    mods.append("static")
            out.append(Symbol(
                name=text(n, src), kind="variable",
                file=rel, line=stmt.start_point[0] + 1, col=stmt.start_point[1],
                end_line=stmt.end_point[0] + 1, is_definition=True,
                language=lang, kind_raw="field_definition",
                modifiers=mods or None,
                parent=inner,
            ))


def js_extract_lexical(node, src: bytes, rel: str, out: list, lang: str):
    """`const x = ...`, `let x = ...`, `var x = ...` at module scope."""
    # first child indicates the kind: const / let / var
    decl_kind = "variable"
    is_const_kw = False
    for c in node.children:
        if c.type == "const":
            decl_kind = "constant"  # default for `const`
            is_const_kw = True
            break
        elif c.type in ("let", "var"):
            break
    for c in node.named_children:
        if c.type != "variable_declarator":
            continue
        n = c.child_by_field_name("name")
        v = c.child_by_field_name("value")
        if n is None or n.type != "identifier":
            continue
        name = text(n, src)
        # Arrow / function expression assigned to const → treat as a function
        if v is not None and v.type in ("arrow_function", "function_expression", "function"):
            js_extract_function_expr_into(c, n, v, src, rel, out, lang, is_const_kw)
            continue
        out.append(Symbol(
            name=name,
            kind="constant" if (is_const_kw and name.isupper()) else
                 ("variable" if not is_const_kw else "constant"),
            file=rel, line=c.start_point[0] + 1, col=c.start_point[1],
            end_line=c.end_point[0] + 1, is_definition=True,
            language=lang, kind_raw="lexical_declaration",
            signature=" ".join(text(c, src).split())[:200],
        ))


def js_extract_function_expr_into(declarator, name_node, fn_node, src, rel, out, lang, is_const):
    name = text(name_node, src)
    mods: list[str] = []
    for ch in fn_node.children:
        if ch.type == "async":
            mods.append("async")
    body = fn_node.child_by_field_name("body")
    if body is not None:
        sig = src[declarator.start_byte:body.start_byte].decode("utf-8", "replace")
    else:
        sig = text(declarator, src).split("\n", 1)[0]
    sig = " ".join(sig.split())
    params = fn_node.child_by_field_name("parameters")
    out.append(Symbol(
        name=name, kind="function",
        file=rel, line=declarator.start_point[0] + 1, col=declarator.start_point[1],
        end_line=declarator.end_point[0] + 1, is_definition=True,
        language=lang, kind_raw=fn_node.type,
        modifiers=mods or None,
        signature=sig,
        params=js_params(params, src) or None,
    ))


def js_walk_definitions_for(lang: str):
    def walker(root, src: bytes, rel: str, syms: list):
        for node in root.children:
            t = node.type
            if t == "function_declaration":
                js_extract_function_decl(node, src, rel, None, syms, lang)
            elif t == "class_declaration":
                js_extract_class(node, src, rel, None, syms, lang)
            elif t == "lexical_declaration":
                js_extract_lexical(node, src, rel, syms, lang)
            elif t == "interface_declaration":  # TS
                js_extract_class(node, src, rel, None, syms, lang)
            elif t == "type_alias_declaration":  # TS
                nm = node.child_by_field_name("name")
                if nm is not None:
                    syms.append(Symbol(
                        name=text(nm, src), kind="type",
                        file=rel, line=node.start_point[0]+1, col=node.start_point[1],
                        end_line=node.end_point[0]+1, is_definition=True,
                        language=lang, kind_raw="type_alias_declaration",
                    ))
            elif t == "enum_declaration":  # TS
                nm = node.child_by_field_name("name")
                if nm is not None:
                    syms.append(Symbol(
                        name=text(nm, src), kind="type",
                        file=rel, line=node.start_point[0]+1, col=node.start_point[1],
                        end_line=node.end_point[0]+1, is_definition=True,
                        language=lang, kind_raw="enum_declaration",
                    ))
            elif t == "export_statement":
                # Recurse into exported declarations.
                for c in node.children:
                    if c.type == "function_declaration":
                        js_extract_function_decl(c, src, rel, None, syms, lang, ["exported"])
                    elif c.type == "class_declaration":
                        js_extract_class(c, src, rel, None, syms, lang)
                    elif c.type == "lexical_declaration":
                        js_extract_lexical(c, src, rel, syms, lang)
                    elif c.type == "interface_declaration":
                        js_extract_class(c, src, rel, None, syms, lang)
                    elif c.type == "type_alias_declaration":
                        nm = c.child_by_field_name("name")
                        if nm is not None:
                            syms.append(Symbol(
                                name=text(nm, src), kind="type",
                                file=rel, line=c.start_point[0]+1, col=c.start_point[1],
                                end_line=c.end_point[0]+1, is_definition=True,
                                language=lang, kind_raw="type_alias_declaration",
                                modifiers=["exported"],
                            ))
                    elif c.type == "enum_declaration":
                        nm = c.child_by_field_name("name")
                        if nm is not None:
                            syms.append(Symbol(
                                name=text(nm, src), kind="type",
                                file=rel, line=c.start_point[0]+1, col=c.start_point[1],
                                end_line=c.end_point[0]+1, is_definition=True,
                                language=lang, kind_raw="enum_declaration",
                                modifiers=["exported"],
                            ))
    return walker


def js_walk_refs_for(lang: str):
    def walker(root, src: bytes, rel: str, refs: list,
               defined_names: set, identifiers_out: set, language: str = lang):
        stack = [root]
        while stack:
            node = stack.pop()
            nt = node.type
            if nt == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None:
                    target = None
                    if fn.type == "identifier":
                        target = text(fn, src)
                    elif fn.type == "member_expression":
                        prop = fn.child_by_field_name("property")
                        if prop is not None and prop.type in ("property_identifier", "identifier"):
                            target = text(prop, src)
                    if target is not None:
                        identifiers_out.add(target)
                        refs.append(Ref(name=target, kind="call",
                                        file=rel, line=node.start_point[0] + 1,
                                        col=node.start_point[1], language=language))
            elif nt == "new_expression":
                cn = node.child_by_field_name("constructor")
                if cn is not None and cn.type == "identifier":
                    name = text(cn, src)
                    identifiers_out.add(name)
                    refs.append(Ref(name=name, kind="call",
                                    file=rel, line=node.start_point[0] + 1,
                                    col=node.start_point[1], language=language))
            elif nt == "identifier":
                name = text(node, src)
                identifiers_out.add(name)
                parent = node.parent
                skip = False
                if parent is not None:
                    pt = parent.type
                    if pt in ("function_declaration", "class_declaration",
                              "method_definition") and parent.child_by_field_name("name") == node:
                        skip = True
                    elif pt == "variable_declarator" and parent.child_by_field_name("name") == node:
                        skip = True
                    elif pt == "formal_parameters":
                        skip = True
                if not skip and name in defined_names:
                    refs.append(Ref(name=name, kind="name",
                                    file=rel, line=node.start_point[0] + 1,
                                    col=node.start_point[1], language=language))
            stack.extend(node.children)
    return walker


# ---------- Language registry ----------

@dataclass
class LangSpec:
    name: str
    exts: tuple[str, ...]
    grammar_factory: callable                # lazy: returns tree-sitter Language
    walk_definitions: callable
    walk_refs: callable
    preprocess: Optional[callable] = None    # raw bytes + flags -> cleaned bytes


def _lang_c():
    import tree_sitter_c
    return Language(tree_sitter_c.language())


def _lang_python():
    import tree_sitter_python
    return Language(tree_sitter_python.language())


def _lang_go():
    import tree_sitter_go
    return Language(tree_sitter_go.language())


def _lang_rust():
    import tree_sitter_rust
    return Language(tree_sitter_rust.language())


def _lang_java():
    import tree_sitter_java
    return Language(tree_sitter_java.language())


def _lang_javascript():
    import tree_sitter_javascript
    return Language(tree_sitter_javascript.language())


def _lang_typescript():
    import tree_sitter_typescript
    return Language(tree_sitter_typescript.language_typescript())


def _lang_tsx():
    import tree_sitter_typescript
    return Language(tree_sitter_typescript.language_tsx())


def _lang_cpp():
    import tree_sitter_cpp
    return Language(tree_sitter_cpp.language())


def _noop_preprocess(src: bytes, _flags) -> bytes:
    """Languages without a separate preprocessor pass — feed bytes to parser as-is."""
    return src


LANGUAGES: dict[str, LangSpec] = {
    "c": LangSpec(
        name="c", exts=(".c", ".h"),
        grammar_factory=_lang_c,
        walk_definitions=c_walk_definitions,
        walk_refs=c_walk_refs,
        preprocess=preprocess_source,
    ),
    "python": LangSpec(
        name="python", exts=(".py",),
        grammar_factory=_lang_python,
        walk_definitions=py_walk_definitions,
        walk_refs=py_walk_refs,
        preprocess=_noop_preprocess,
    ),
    "go": LangSpec(
        name="go", exts=(".go",),
        grammar_factory=_lang_go,
        walk_definitions=go_walk_definitions,
        walk_refs=go_walk_refs,
        preprocess=_noop_preprocess,
    ),
    "rust": LangSpec(
        name="rust", exts=(".rs",),
        grammar_factory=_lang_rust,
        walk_definitions=rs_walk_definitions,
        walk_refs=rs_walk_refs,
        preprocess=_noop_preprocess,
    ),
    "java": LangSpec(
        name="java", exts=(".java",),
        grammar_factory=_lang_java,
        walk_definitions=java_walk_definitions,
        walk_refs=java_walk_refs,
        preprocess=_noop_preprocess,
    ),
    "javascript": LangSpec(
        name="javascript", exts=(".js", ".mjs", ".cjs"),
        grammar_factory=_lang_javascript,
        walk_definitions=js_walk_definitions_for("javascript"),
        walk_refs=js_walk_refs_for("javascript"),
        preprocess=_noop_preprocess,
    ),
    "typescript": LangSpec(
        name="typescript", exts=(".ts",),
        grammar_factory=_lang_typescript,
        walk_definitions=js_walk_definitions_for("typescript"),
        walk_refs=js_walk_refs_for("typescript"),
        preprocess=_noop_preprocess,
    ),
    "tsx": LangSpec(
        name="tsx", exts=(".tsx",),
        grammar_factory=_lang_tsx,
        walk_definitions=js_walk_definitions_for("tsx"),
        walk_refs=js_walk_refs_for("tsx"),
        preprocess=_noop_preprocess,
    ),
    "cpp": LangSpec(
        name="cpp", exts=(".cc", ".cpp", ".cxx", ".hpp", ".hxx", ".hh"),
        grammar_factory=_lang_cpp,
        walk_definitions=cpp_walk_definitions,
        walk_refs=cpp_walk_refs,
        preprocess=preprocess_source,  # C preprocessing applies to C++ too
    ),
}


_PARSER_CACHE: dict[str, Parser] = {}

def get_parser(lang: str) -> Parser:
    if lang not in _PARSER_CACHE:
        _PARSER_CACHE[lang] = Parser(LANGUAGES[lang].grammar_factory())
    return _PARSER_CACHE[lang]


def language_of(p: Path) -> Optional[str]:
    for name, spec in LANGUAGES.items():
        if p.suffix in spec.exts:
            return name
    return None


# ---------- build ----------

def iter_c_files(root: Path):
    """Iterate source files matching any registered language extension.
    (Name kept for back-compat; despite the name, it now covers all languages.)"""
    all_exts: set[str] = set()
    for spec in LANGUAGES.values():
        all_exts |= set(spec.exts)
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in all_exts:
            yield p


# Symbol kinds whose name we want to capture as a "name" ref when we see the
# identifier later. Now uses the multi-language vocabulary.
NAME_KINDS = {"function", "type", "variable", "constant"}


def compute_preproc(root: Path, defs_path: Optional[Path],
                    undef_unknown_configs: bool, verbose: bool):
    """Return (unifdef_flags, preproc_info, preproc_fingerprint)."""
    unifdef_flags: list[str] = []
    preproc_info = {"enabled": False, "defs_file": None, "unifdef_bin": None,
                    "n_flags": 0, "auto_undef_count": 0}
    if defs_path is not None and defs_path.is_file():
        unifdef_flags = parse_defs_file(defs_path)
        auto_undef_count = 0
        if undef_unknown_configs:
            explicit = {re.match(r"-[DU](\w+)", f).group(1) for f in unifdef_flags
                        if re.match(r"-[DU](\w+)", f)}
            extras = collect_unknown_configs(root, explicit)
            unifdef_flags.extend(f"-U{k}" for k in extras)
            auto_undef_count = len(extras)
        preproc_info = {
            "enabled": bool(unifdef_flags) and UNIFDEF_BIN is not None,
            "defs_file": str(defs_path),
            "unifdef_bin": UNIFDEF_BIN,
            "n_flags": len(unifdef_flags),
            "auto_undef_count": auto_undef_count,
        }
        if verbose:
            if UNIFDEF_BIN is None:
                print(f"  [warn] unifdef not in PATH — #if branches won't be pruned",
                      file=sys.stderr)
            else:
                msg = (f"  [preproc] {defs_path} → {len(unifdef_flags)} flag(s) "
                       f"via {UNIFDEF_BIN}")
                if auto_undef_count:
                    msg += f"  (incl. {auto_undef_count} auto-undef CONFIG_*)"
                print(msg, file=sys.stderr)

    # Fingerprint: anything that would make old parsed data incompatible.
    h = hashlib.sha256()
    h.update(f"schema={SCHEMA_VERSION}|".encode())
    h.update(("|".join(sorted(unifdef_flags))).encode())
    return unifdef_flags, preproc_info, h.hexdigest()


def build(root: Path, out_path: Path, defs_path: Optional[Path] = None,
          undef_unknown_configs: bool = True, force_full: bool = False,
          verbose: bool = True):
    t0 = time.time()

    # C-specific preprocessing context. Other languages ignore unifdef_flags
    # and have a no-op preprocess.
    unifdef_flags, preproc_info, fp = compute_preproc(
        root, defs_path, undef_unknown_configs, verbose)

    # Try to load existing index for reuse
    old_store: Optional[IndexStore] = None
    invalidation_reason = None
    if not force_full and out_path.is_file():
        try:
            cand = load_index(out_path)
            if cand.meta.get("schema_version") != SCHEMA_VERSION:
                invalidation_reason = f"schema_version {cand.meta.get('schema_version')} → {SCHEMA_VERSION}"
            elif cand.meta.get("preproc_fingerprint") != fp:
                invalidation_reason = "preproc fingerprint changed (defs/auto-undef differs)"
            elif cand.meta.get("root") != str(root.resolve()):
                invalidation_reason = "root path differs"
            else:
                old_store = cand
        except Exception as e:
            invalidation_reason = f"could not read old index: {e}"
    elif force_full:
        invalidation_reason = "--full requested"

    # Group old per-file data
    old_files_meta = {f["path"]: f for f in (old_store.files if old_store else [])}
    old_syms_by_file: dict[str, list] = defaultdict(list)
    old_refs_by_file: dict[str, list] = defaultdict(list)
    if old_store:
        for s in old_store.all_symbols():
            old_syms_by_file[s["file"]].append(s)
        for r in old_store.all_refs():
            old_refs_by_file[r["file"]].append(r)

    # Walk files, classify as reused vs changed. Skip files whose extension
    # doesn't match any registered language.
    file_records = []  # list of (rel, raw_bytes, sha1, reused: bool, lang: str)
    n_bytes = 0
    for p in iter_c_files(root):
        lang = language_of(p)
        if lang is None:
            continue
        rel = str(p.relative_to(root))
        raw = p.read_bytes()
        n_bytes += len(raw)
        h = hashlib.sha1(raw).hexdigest()
        old = old_files_meta.get(rel)
        reused = bool(old
                      and old.get("sha1") == h
                      and old.get("identifiers") is not None
                      and rel in old_syms_by_file)
        file_records.append((rel, raw, h, reused, lang))

    # Per-file data (everything as plain dicts for uniformity)
    syms_by_file: dict[str, list] = {}
    refs_by_file: dict[str, list] = {}
    idents_by_file: dict[str, list] = {}
    meta_by_file: dict[str, dict] = {}
    parsed_cleaned: dict[str, bytes] = {}  # cache cleaned bytes for Pass 2

    # Pre-populate reused files
    for rel, _, _, reused, _ in file_records:
        if reused:
            syms_by_file[rel] = old_syms_by_file[rel]
            refs_by_file[rel] = old_refs_by_file[rel]
            idents_by_file[rel] = old_files_meta[rel].get("identifiers") or []
            meta_by_file[rel] = old_files_meta[rel]

    # Pass 1 (defs) for changed files
    for rel, raw, h, reused, lang in file_records:
        if reused:
            continue
        spec = LANGUAGES[lang]
        cleaned = spec.preprocess(raw, unifdef_flags) if spec.preprocess else raw
        parsed_cleaned[rel] = cleaned
        parser = get_parser(lang)
        tree = parser.parse(cleaned)
        syms: list[Symbol] = []
        spec.walk_definitions(tree.root_node, cleaned, rel, syms)
        syms_by_file[rel] = [asdict(s) for s in syms]
        meta_by_file[rel] = {
            "path": rel,
            "size": len(raw),
            "lines": raw.count(b"\n") + 1,
            "sha1": h,
            "has_error": tree.root_node.has_error,
            "n_symbols": len(syms),
            "language": lang,
        }

    # Compute new and old defined-name sets
    new_defined = {s["name"] for syms in syms_by_file.values() for s in syms
                   if s["kind"] in NAME_KINDS}
    old_defined: set[str] = set()
    if old_store:
        for s in old_store.all_symbols():
            if s["kind"] in NAME_KINDS:
                old_defined.add(s["name"])
    added_names = new_defined - old_defined

    # Find unchanged files whose ref set could be affected by newly-added names
    # (Option B from the design: only re-Pass2 files that actually mention the
    # new identifier somewhere in their source.)
    affected: set[str] = set()
    if added_names and old_store is not None:
        for rel, _, _, reused, _ in file_records:
            if not reused:
                continue
            ids = idents_by_file.get(rel)
            if ids and added_names.intersection(ids):
                affected.add(rel)

    # Pass 2 (refs) for changed + affected files
    pass2_set: set[str] = {rel for rel, _, _, reused, _ in file_records if not reused}
    pass2_set |= affected
    for rel, raw, _, _, lang in file_records:
        if rel not in pass2_set:
            continue
        spec = LANGUAGES[lang]
        cleaned = parsed_cleaned.get(rel)
        if cleaned is None:
            cleaned = spec.preprocess(raw, unifdef_flags) if spec.preprocess else raw
        parser = get_parser(lang)
        tree = parser.parse(cleaned)
        new_refs: list[Ref] = []
        idents: set[str] = set()
        spec.walk_refs(tree.root_node, cleaned, rel, new_refs, new_defined, idents)
        refs_by_file[rel] = [asdict(r) for r in new_refs]
        idents_by_file[rel] = sorted(idents)
        meta_by_file[rel]["identifiers"] = idents_by_file[rel]

    # Assemble final ordered lists (deterministic, sorted by file path)
    files_meta_list = [meta_by_file[r[0]] for r in file_records]
    all_symbols = [s for r in file_records for s in syms_by_file[r[0]]]
    all_refs    = [x for r in file_records for x in refs_by_file[r[0]]]

    n_files = len(file_records)
    n_reused = sum(1 for _, _, _, reused, _ in file_records if reused)
    n_changed = n_files - n_reused
    elapsed = time.time() - t0

    if verbose:
        if old_store is None:
            mode = f"full rebuild ({invalidation_reason})" if invalidation_reason else "full rebuild (no prior index)"
        else:
            mode = f"incremental: reused {n_reused}, changed {n_changed}, affected {len(affected)}"
        print(f"  [{mode}]", file=sys.stderr)

    out = {
        "schema_version": SCHEMA_VERSION,
        "root": str(root.resolve()),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_seconds": round(elapsed, 3),
        "preprocessing": preproc_info,
        "preproc_fingerprint": fp,
        "files": files_meta_list,
        "symbols": all_symbols,
        "refs": all_refs,
    }
    write_sqlite_index(out_path, out)
    print(f"\nIndexed {n_files} files ({n_bytes/1024:.1f} KiB) in {elapsed:.2f}s",
          file=sys.stderr)
    print(f"  {len(all_symbols)} symbols, {len(all_refs)} refs", file=sys.stderr)
    print(f"  wrote {out_path}  ({out_path.stat().st_size/1024:.1f} KiB)", file=sys.stderr)


# ---------- storage backend (SQLite) ----------

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL              -- JSON-encoded
);
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    lines       INTEGER NOT NULL,
    sha1        TEXT NOT NULL,
    has_error   INTEGER NOT NULL,
    n_symbols   INTEGER NOT NULL,
    identifiers TEXT NOT NULL,       -- JSON array
    language    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,     -- function|type|variable|constant|macro|field|module
    file          TEXT NOT NULL,
    line          INTEGER NOT NULL,
    col           INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    is_definition INTEGER NOT NULL,
    language      TEXT NOT NULL,
    kind_raw      TEXT,              -- original AST node type
    modifiers     TEXT,              -- JSON array
    parent        TEXT,              -- enclosing symbol (class/namespace/module)
    signature     TEXT,
    return_type   TEXT,
    enum_values   TEXT,              -- JSON array
    params        TEXT               -- JSON array
);
CREATE INDEX IF NOT EXISTS idx_symbols_name     ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind     ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file     ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_symbols_language ON symbols(language);
CREATE TABLE IF NOT EXISTS refs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    kind     TEXT NOT NULL,          -- call|type|name
    file     TEXT NOT NULL,
    line     INTEGER NOT NULL,
    col      INTEGER NOT NULL,
    language TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refs_name     ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_file     ON refs(file);
CREATE INDEX IF NOT EXISTS idx_refs_kind     ON refs(kind);
CREATE INDEX IF NOT EXISTS idx_refs_language ON refs(language);
"""


def write_sqlite_index(path: Path, top: dict):
    """Replace the SQLite file with a fresh build from `top`."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(SQLITE_SCHEMA)
        cur = conn.cursor()
        for k, v in top.items():
            if k in ("files", "symbols", "refs"):
                continue
            cur.execute("INSERT INTO meta(key, value) VALUES (?, ?)",
                        (k, json.dumps(v)))
        cur.executemany(
            "INSERT INTO files(path, size, lines, sha1, has_error, n_symbols, identifiers, language) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(f["path"], f.get("size", 0), f.get("lines", 0), f.get("sha1", ""),
              int(bool(f.get("has_error"))), f.get("n_symbols", 0),
              json.dumps(f.get("identifiers") or []),
              f.get("language", "c"))
             for f in top["files"]],
        )
        cur.executemany(
            "INSERT INTO symbols(name, kind, file, line, col, end_line, is_definition, "
            "language, kind_raw, modifiers, parent, signature, return_type, enum_values, params) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(s["name"], s["kind"], s["file"], s["line"], s["col"], s["end_line"],
              int(bool(s.get("is_definition"))),
              s.get("language", "c"),
              s.get("kind_raw"),
              json.dumps(s["modifiers"]) if s.get("modifiers") else None,
              s.get("parent"),
              s.get("signature"),
              s.get("return_type"),
              json.dumps(s["enum_values"]) if s.get("enum_values") else None,
              json.dumps(s["params"]) if s.get("params") else None)
             for s in top["symbols"]],
        )
        cur.executemany(
            "INSERT INTO refs(name, kind, file, line, col, language) VALUES (?,?,?,?,?,?)",
            [(r["name"], r["kind"], r["file"], r["line"], r["col"], r.get("language", "c"))
             for r in top["refs"]],
        )
        conn.commit()
    finally:
        conn.close()


class IndexStore:
    """SQLite-backed index reader.

    Supports dict-style access for convenience (`idx['symbols']`, `idx['root']`)
    and indexed-lookup methods (`find_symbols(name=...)`) that use the SQL
    indexes."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._meta_cache: Optional[dict] = None
        self._files_cache: Optional[list] = None
        self._all_syms: Optional[list] = None
        self._all_refs: Optional[list] = None

    # ----- row converters -----

    @staticmethod
    def _file_row(r) -> dict:
        d = dict(r)
        d["has_error"] = bool(d["has_error"])
        d["identifiers"] = json.loads(d["identifiers"] or "[]")
        return d

    @staticmethod
    def _sym_row(r) -> dict:
        d = dict(r)
        d.pop("id", None)
        d["is_definition"] = bool(d["is_definition"])
        d["modifiers"] = json.loads(d["modifiers"]) if d.get("modifiers") else None
        d["enum_values"] = json.loads(d["enum_values"]) if d.get("enum_values") else None
        d["params"] = json.loads(d["params"]) if d.get("params") else None
        return d

    @staticmethod
    def _ref_row(r) -> dict:
        d = dict(r)
        d.pop("id", None)
        return d

    # ----- dict-style back-compat -----

    def __getitem__(self, key):
        if key == "symbols":
            return self.all_symbols()
        if key == "refs":
            return self.all_refs()
        if key == "files":
            return self.files
        return self.meta[key]

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    # ----- accessors -----

    @property
    def meta(self) -> dict:
        if self._meta_cache is None:
            self._meta_cache = {row["key"]: json.loads(row["value"])
                                for row in self._conn.execute("SELECT key, value FROM meta")}
        return self._meta_cache

    @property
    def files(self) -> list:
        if self._files_cache is None:
            self._files_cache = [self._file_row(r)
                                 for r in self._conn.execute("SELECT * FROM files")]
        return self._files_cache

    def all_symbols(self) -> list:
        if self._all_syms is None:
            self._all_syms = [self._sym_row(r)
                              for r in self._conn.execute("SELECT * FROM symbols")]
        return self._all_syms

    def all_refs(self) -> list:
        if self._all_refs is None:
            self._all_refs = [self._ref_row(r)
                              for r in self._conn.execute("SELECT * FROM refs")]
        return self._all_refs

    # ----- path normalization -----

    @property
    def _file_set(self) -> set[str]:
        """Set of root-relative paths actually in the index."""
        if not hasattr(self, "_file_set_cache"):
            self._file_set_cache = {f["path"] for f in self.files}
        return self._file_set_cache

    def normalize_file_path(self, path: str) -> Optional[str]:
        """Resolve a file-path-ish string to the canonical root-relative path
        stored in the index. Returns None if no match.

        Accepts:
          1. Exact root-relative path that's already in the index ("ba.c")
          2. Absolute path, if it lives under `meta.root` ("/abs/.../ba.c")
          3. Basename / suffix match — if exactly one file's path ends with
             `/path`, that file is returned ("ba.c" → "kunit/kunit-mock-ba.c"
             only if that's the unique suffix match)

        Ambiguous (multiple suffix matches) → returns None; caller can iterate
        `idx.files` for the full list."""
        if not path:
            return None
        # 1. Already canonical
        if path in self._file_set:
            return path
        # 2. Absolute → strip root prefix
        root_str = self.meta.get("root")
        if root_str:
            try:
                p = Path(path)
                if p.is_absolute():
                    rel = str(p.resolve().relative_to(Path(root_str).resolve()))
                    if rel in self._file_set:
                        return rel
            except (ValueError, OSError):
                pass
        # 3. Suffix match (basename or partial)
        if "/" not in path:
            # bare basename: must end with /<path>
            matches = [f for f in self._file_set if f.endswith("/" + path)]
        else:
            matches = [f for f in self._file_set if f.endswith("/" + path) or f == path]
        if len(matches) == 1:
            return matches[0]
        return None

    def find_symbols(self, *, name=None, kind=None, file=None):
        if file is not None:
            file = self.normalize_file_path(file) or file
        clauses, params = [], []
        if name is not None: clauses.append("name = ?"); params.append(name)
        if kind is not None: clauses.append("kind = ?"); params.append(kind)
        if file is not None: clauses.append("file = ?"); params.append(file)
        q = "SELECT * FROM symbols"
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        return [self._sym_row(r) for r in self._conn.execute(q, params)]

    def find_refs(self, *, name=None, kind=None, file=None):
        if file is not None:
            file = self.normalize_file_path(file) or file
        clauses, params = [], []
        if name is not None: clauses.append("name = ?"); params.append(name)
        if kind is not None: clauses.append("kind = ?"); params.append(kind)
        if file is not None: clauses.append("file = ?"); params.append(file)
        q = "SELECT * FROM refs"
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        return [self._ref_row(r) for r in self._conn.execute(q, params)]

    def find_refs_in_range(self, file: str, start_line: int, end_line: int):
        """Refs in a contiguous line range of one file (used for callees)."""
        file = self.normalize_file_path(file) or file
        return [self._ref_row(r) for r in self._conn.execute(
            "SELECT * FROM refs WHERE file = ? AND line BETWEEN ? AND ?",
            (file, start_line, end_line))]

    def kind_counts(self) -> Counter:
        return Counter({r["kind"]: r["n"] for r in self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM symbols GROUP BY kind ORDER BY n DESC")})

    def ref_kind_counts(self) -> Counter:
        return Counter({r["kind"]: r["n"] for r in self._conn.execute(
            "SELECT kind, COUNT(*) AS n FROM refs GROUP BY kind ORDER BY n DESC")})

    def top_ref_names(self, kind: str, limit: int = 10):
        return [(r["name"], r["n"]) for r in self._conn.execute(
            "SELECT name, COUNT(*) AS n FROM refs WHERE kind = ? "
            "GROUP BY name ORDER BY n DESC LIMIT ?", (kind, limit))]

    def n_symbols(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    def n_refs(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]

    def n_definitions(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE is_definition = 1").fetchone()[0]


# ---------- query ----------

def load_index(path: Path) -> IndexStore:
    return IndexStore(path)


def cmd_stats(idx):
    print(f"Root:      {idx['root']}")
    print(f"Built:     {idx['built_at']}  ({idx['elapsed_seconds']}s)")
    files = idx.files
    print(f"Files:     {len(files)}")
    err_files = [f for f in files if f['has_error']]
    if err_files:
        print(f"  with parse errors: {len(err_files)}")
        for f in err_files[:5]:
            print(f"    {f['path']}")
    n_symbols = idx.n_symbols()
    n_defs = idx.n_definitions()
    print(f"Symbols:   {n_symbols}")
    for k, v in idx.kind_counts().most_common():
        print(f"  {k:12s} {v}")
    print(f"  (definitions: {n_defs}, declarations: {n_symbols - n_defs})")
    print(f"Refs:      {idx.n_refs()}")
    for k, v in idx.ref_kind_counts().most_common():
        print(f"  {k:12s} {v}")
    print("\nTop 10 most-called identifiers:")
    for name, n in idx.top_ref_names("call", 10):
        print(f"  {n:5d}  {name}")
    print("\nTop 10 most-used types:")
    for name, n in idx.top_ref_names("type", 10):
        print(f"  {n:5d}  {name}")


def cmd_lookup(idx, name: str, kind: Optional[str]):
    hits = idx.find_symbols(name=name, kind=kind)
    if not hits:
        print(f"(no matches for {name!r})")
        return
    for s in hits:
        flag = "def" if s['is_definition'] else "decl"
        mods = " ".join(s.get('modifiers') or [])
        tag = f"{s['kind']}/{flag}"
        if mods:
            tag += " " + mods
        head = f"{s['file']}:{s['line']}  [{tag}] ({s.get('language','?')})"
        print(head)
        if s.get('signature'):
            print(f"    {s['signature']}")
        if s.get('enum_values'):
            ev = s['enum_values']
            preview = ", ".join(ev[:8]) + (" ..." if len(ev) > 8 else "")
            print(f"    values: {preview}")


def cmd_kind(idx, kind: str):
    hits = idx.find_symbols(kind=kind)
    if not hits:
        print(f"(no symbols of kind {kind!r})")
        return
    for s in sorted(hits, key=lambda x: (x['file'], x['line'])):
        mods = " ".join(s.get('modifiers') or [])
        tag = f"[{mods}] " if mods else ""
        print(f"  {s['file']}:{s['line']}  {tag}{s['name']}")


def cmd_file(idx, path: str):
    hits = idx.find_symbols(file=path)
    if not hits:
        # Fallback: try basename match (lookup files whose path ends with /path)
        all_files = [f['path'] for f in idx.files]
        match = [f for f in all_files if f.endswith('/' + path)]
        if match:
            hits = []
            for fp in match:
                hits.extend(idx.find_symbols(file=fp))
    if not hits:
        print(f"(no symbols in {path!r})")
        return
    by_kind = defaultdict(list)
    for s in hits:
        by_kind[s['kind']].append(s)
    order = ["constant", "type", "variable", "function"]
    for k in order:
        if k not in by_kind:
            continue
        items = sorted(by_kind[k], key=lambda x: x['line'])
        print(f"\n[{k}]  {len(items)} item(s)")
        for s in items:
            extra = ""
            mods = s.get('modifiers') or []
            if mods:
                extra += " " + " ".join(mods)
            if not s.get('is_definition'):
                extra += " (decl)"
            print(f"  {s['line']:5d}  {s['name']}{extra}")


def cmd_refs(idx, name: str, kind: Optional[str]):
    hits = idx.find_refs(name=name, kind=kind)
    if not hits:
        print(f"(no references to {name!r})")
        return
    by_file = defaultdict(list)
    for r in hits:
        by_file[r['file']].append(r)
    print(f"{len(hits)} reference(s) to {name!r} across {len(by_file)} file(s):")
    for f in sorted(by_file):
        rs = sorted(by_file[f], key=lambda x: x['line'])
        kinds = Counter(r['kind'] for r in rs)
        kind_str = " ".join(f"{k}×{v}" for k, v in kinds.most_common())
        lines = ",".join(str(r['line']) for r in rs[:20])
        more = "" if len(rs) <= 20 else f",...(+{len(rs)-20})"
        print(f"  {f}  ({kind_str})  lines: {lines}{more}")


def build_fn_ranges(symbols):
    """file -> (sorted_start_lines, records[(start, end, name, params_set)])."""
    by_file = defaultdict(list)
    for s in symbols:
        if s['kind'] == 'function' and s['is_definition']:
            params = set(s.get('params') or [])
            by_file[s['file']].append((s['line'], s['end_line'], s['name'], params))
    out = {}
    for f, recs in by_file.items():
        recs.sort()
        out[f] = ([r[0] for r in recs], recs)
    return out


def containing_fn(file, line, fn_ranges):
    """Return (name, params_set) of the function containing file:line, or None."""
    info = fn_ranges.get(file)
    if not info:
        return None
    starts, recs = info
    i = bisect.bisect_right(starts, line) - 1
    if i < 0:
        return None
    s, e, name, params = recs[i]
    return (name, params) if s <= line <= e else None


FUNCTION_KINDS = {"function"}


def build_callgraph(idx):
    """Build a strict function-to-function call graph.

    Edges only when BOTH endpoints are functions we have in the index
    (definitions or prototypes — both stored as kind='function' with
    is_definition true/false). Calls to macros, externs (kfree etc.)
    and reads of variables are excluded — those live in the broader
    `refs` query, not the call graph.

    Param shadowing is filtered (a ref whose name matches the enclosing
    function's parameter is a local var access, not a real call).

    Returns (calls_of, callers_of, sites_of).
      calls_of[fn]   -> Counter[callee_fn]
      callers_of[fn] -> Counter[caller_fn]
      sites_of[(caller, callee)] -> list of (file, line, ref_kind)"""
    fn_ranges = build_fn_ranges(idx['symbols'])
    # Names defined as functions/prototypes in our index
    fn_names: set[str] = {s['name'] for s in idx['symbols']
                          if s['kind'] in FUNCTION_KINDS}

    calls_of = defaultdict(Counter)
    callers_of = defaultdict(Counter)
    sites_of = defaultdict(list)
    for r in idx['refs']:
        if r['kind'] not in ('call', 'name'):
            continue
        if r['name'] not in fn_names:
            continue
        ctx = containing_fn(r['file'], r['line'], fn_ranges)
        if ctx is None:
            continue
        cf, params = ctx
        if cf == r['name'] or r['name'] in params:
            continue
        calls_of[cf][r['name']] += 1
        callers_of[r['name']][cf] += 1
        sites_of[(cf, r['name'])].append((r['file'], r['line'], r['kind']))
    return calls_of, callers_of, sites_of


def cmd_callers(idx, name):
    """Targeted: only fetch refs that point at `name`, then map each to its
    enclosing function. Much faster than building the full callgraph."""
    # Verify target is a function we know about
    if not idx.find_symbols(name=name, kind="function"):
        print(f"(no function-to-function callers found for {name!r})")
        return
    fn_ranges = build_fn_ranges(idx.find_symbols(kind="function"))
    refs = [r for r in idx.find_refs(name=name) if r['kind'] in ('call', 'name')]
    callers: Counter = Counter()
    sites: dict = defaultdict(list)
    for r in refs:
        ctx = containing_fn(r['file'], r['line'], fn_ranges)
        if ctx is None:
            continue
        cf, params = ctx
        if cf == name or name in params:
            continue
        callers[cf] += 1
        sites[cf].append((r['file'], r['line'], r['kind']))
    if not callers:
        print(f"(no function-to-function callers found for {name!r})")
        return
    total = sum(callers.values())
    print(f"{total} call site(s) from {len(callers)} caller(s) to {name!r}:")
    for caller, n in callers.most_common():
        ss = sites[caller]
        loc = ", ".join(f"{f}:{l}" for f, l, _ in ss[:6])
        more = "" if len(ss) <= 6 else f" ...+{len(ss)-6}"
        print(f"  {n:4d}  {caller}   [{loc}{more}]")


def cmd_callees(idx, name):
    """Targeted: find NAME's function range, then range-query refs only inside
    that body. Filter to refs that point at other functions."""
    fn_syms = idx.find_symbols(name=name, kind="function")
    fn_syms = [s for s in fn_syms if s.get("is_definition")]
    if not fn_syms:
        print(f"(no function definition for {name!r})")
        return
    fn_names: set = {s['name'] for s in idx.find_symbols(kind='function')}
    callees: Counter = Counter()
    for fn in fn_syms:
        params = set(fn.get('params') or [])
        body_refs = idx.find_refs_in_range(fn['file'], fn['line'], fn['end_line'])
        for r in body_refs:
            if r['kind'] not in ('call', 'name'):
                continue
            tname = r['name']
            if tname not in fn_names or tname == name or tname in params:
                continue
            callees[tname] += 1
    if not callees:
        print(f"(no function callees for {name!r} — only calls externs/macros)")
        return
    total = sum(callees.values())
    print(f"{name}() — calls {len(callees)} function(s), {total} call site(s) total:")
    for callee, n in callees.most_common(40):
        print(f"  {n:4d}  {callee}")
    if len(callees) > 40:
        print(f"  ... (+{len(callees)-40} more)")


def cmd_callgraph(idx, root, direction, depth, max_branch):
    calls_of, callers_of, _ = build_callgraph(idx)
    graph = calls_of if direction == "callees" else callers_of
    print(f"{root}   ({direction}, max depth={depth})")
    visited = set()

    def walk(node, d, prefix):
        if d >= depth:
            return
        if node in visited:
            print(f"{prefix}└─ ...(cycle: {node})")
            return
        visited.add(node)
        nexts = graph.get(node, Counter())
        # Limit per-level fan-out for readability
        items = nexts.most_common(max_branch)
        for i, (n, cnt) in enumerate(items):
            last = (i == len(items) - 1)
            branch = "└─ " if last else "├─ "
            print(f"{prefix}{branch}{n}  (×{cnt})")
            new_prefix = prefix + ("   " if last else "│  ")
            walk(n, d + 1, new_prefix)
        if len(nexts) > max_branch:
            print(f"{prefix}   ... (+{len(nexts) - max_branch} more, suppressed)")

    walk(root, 0, "")


def cmd_slice(idx, name: str, with_callees: bool, with_callers: bool,
              with_types: bool, with_macros: bool, depth: int, max_bytes: int):
    """Print a markdown blob with NAME's definition body plus optional context
    (callees, callers, types, macros). Intended as LLM context.

    Source is read from the ORIGINAL files (line numbers preserved by the
    preprocessor), so any kernel-style `#if/#else` and other macros appear
    as the author wrote them."""
    root = Path(idx["root"])

    # Index symbols by name (functions first, then everything else)
    by_name: dict[str, list] = defaultdict(list)
    for s in idx["symbols"]:
        by_name[s["name"]].append(s)
    kind_priority = {"function": 0, "constant": 1, "type": 2, "variable": 3}

    def pick(name: str, prefer_def: bool = True):
        """Pick the best symbol record for `name`. Prefer function definitions,
        then any definition, then any declaration."""
        cands = by_name.get(name) or []
        if not cands:
            return None
        cands = sorted(cands, key=lambda s: (
            0 if (prefer_def and s.get("is_definition")) else 1,
            kind_priority.get(s["kind"], 99),
            s["file"],
            s["line"],
        ))
        return cands[0]

    target = pick(name)
    if target is None:
        print(f"(no symbol {name!r} in index)")
        return

    def read_lines(rel: str, line: int, end_line: int) -> str:
        path = root / rel
        try:
            txt = path.read_text(errors="replace")
        except OSError as e:
            return f"<could not read {rel}: {e}>"
        lines = txt.splitlines()
        return "\n".join(lines[line - 1 : end_line])

    def section(sym, header_prefix: str = "##") -> str:
        body = read_lines(sym["file"], sym["line"], sym["end_line"])
        loc = f"{sym['file']}:{sym['line']}-{sym['end_line']}"
        mods = " ".join(sym.get("modifiers") or [])
        title = f"{sym['name']}  ({sym['kind']}{', ' + mods if mods else ''})"
        lang = sym.get("language", "c")
        return (f"{header_prefix} {title}  — {loc}\n\n"
                f"```{lang}\n{body}\n```")

    out: list[str] = []
    out.append(f"# Slice: {name}\n")
    out.append("## Definition\n\n" + section(target, header_prefix="###"))

    # Callee bodies (transitive up to `depth`)
    if with_callees and target["kind"] == "function":
        calls_of, _, _ = build_callgraph(idx)
        seen: set[str] = {name}
        frontier = list(calls_of.get(name, Counter()).keys())
        for d in range(1, depth + 1):
            next_frontier: list[str] = []
            level_secs: list[str] = []
            for callee in frontier:
                if callee in seen:
                    continue
                seen.add(callee)
                sym = pick(callee)
                if sym is None or sym["kind"] != "function" or not sym.get("is_definition"):
                    continue
                level_secs.append(section(sym, header_prefix="###"))
                next_frontier.extend(calls_of.get(callee, Counter()).keys())
            if level_secs:
                out.append(f"## Callees (depth {d})\n\n" + "\n\n".join(level_secs))
            frontier = next_frontier
            if not frontier:
                break

    # Caller bodies (transitive up to `depth`)
    if with_callers and target["kind"] == "function":
        _, callers_of, _ = build_callgraph(idx)
        seen: set[str] = {name}
        frontier = list(callers_of.get(name, Counter()).keys())
        for d in range(1, depth + 1):
            next_frontier: list[str] = []
            level_secs: list[str] = []
            for caller in frontier:
                if caller in seen:
                    continue
                seen.add(caller)
                sym = pick(caller)
                if sym is None or sym["kind"] != "function" or not sym.get("is_definition"):
                    continue
                level_secs.append(section(sym, header_prefix="###"))
                next_frontier.extend(callers_of.get(caller, Counter()).keys())
            if level_secs:
                out.append(f"## Callers (depth {d})\n\n" + "\n\n".join(level_secs))
            frontier = next_frontier
            if not frontier:
                break

    # Types referenced inside the target's body
    if with_types and target.get("is_definition"):
        type_names = set()
        for r in idx["refs"]:
            if r["kind"] != "type":
                continue
            if r["file"] == target["file"] and target["line"] <= r["line"] <= target["end_line"]:
                type_names.add(r["name"])
        type_secs = []
        for tn in sorted(type_names):
            sym = pick(tn)
            if sym is None or sym["kind"] != "type":
                continue
            type_secs.append(section(sym, header_prefix="###"))
        if type_secs:
            out.append("## Types referenced\n\n" + "\n\n".join(type_secs))

    # Function-like macros invoked by the target
    if with_macros and target.get("is_definition"):
        macro_names = set()
        for r in idx["refs"]:
            if r["kind"] != "call":
                continue
            if r["file"] == target["file"] and target["line"] <= r["line"] <= target["end_line"]:
                macro_names.add(r["name"])
        macro_secs = []
        for mn in sorted(macro_names):
            sym = pick(mn)
            if sym is None:
                continue
            # Show fn-like C macros and object-like constants.
            is_macro = sym.get("kind_raw") in ("preproc_function_def", "preproc_def")
            if not is_macro and sym["kind"] != "constant":
                continue
            macro_secs.append(section(sym, header_prefix="###"))
        if macro_secs:
            out.append("## Macros used\n\n" + "\n\n".join(macro_secs))

    text = "\n\n".join(out)
    if max_bytes and len(text.encode("utf-8")) > max_bytes:
        text = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
        text += f"\n\n_[truncated to {max_bytes} bytes]_\n"
    print(text)


def cmd_unused_static(idx):
    """Static functions whose only call refs (if any) come from inside themselves."""
    # C-only concept; filter to C functions with a `static` modifier.
    static_funcs = [s for s in idx.find_symbols(kind='function')
                    if s.get('language') == 'c'
                    and 'static' in (s.get('modifiers') or [])
                    and s.get('is_definition')]
    if not static_funcs:
        print("(no static functions)")
        return
    # Any mention (call OR name-as-callback) counts as "referenced".
    by_name = defaultdict(list)
    for r in idx['refs']:
        if r['kind'] in ('call', 'name'):
            by_name[r['name']].append((r['file'], r['line']))

    unused = []
    for s in static_funcs:
        refs = by_name.get(s['name'], [])
        # Strip refs that are within s itself (recursive calls)
        non_self = [
            ref for ref in refs
            if not (ref[0] == s['file'] and s['line'] <= ref[1] <= s['end_line'])
        ]
        if not non_self:
            unused.append(s)

    if not unused:
        print("(every static function is referenced)")
        return
    print(f"{len(unused)} static function(s) with no external references:")
    for s in sorted(unused, key=lambda x: (x['file'], x['line'])):
        sig = (s.get('signature') or s['name'])[:90]
        print(f"  {s['file']}:{s['line']}  {sig}")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p_build = sp.add_parser("build")
    p_build.add_argument("--root", default="pcie_scsc")
    p_build.add_argument("--out",  default="tsindex.db")
    p_build.add_argument("--defs", default="tsindex.defs",
                         help="path to #define/#undef config file (default: tsindex.defs if present)")
    p_build.add_argument("--no-preproc", action="store_true",
                         help="skip preprocessing even if a defs file is present")
    p_build.add_argument("--keep-unknown-configs", action="store_true",
                         help="don't auto-#undef CONFIG_* keys missing from defs file")
    p_build.add_argument("--full", action="store_true",
                         help="force full rebuild even if existing index is reusable")
    p_build.add_argument("-q", "--quiet", action="store_true")

    p_stats = sp.add_parser("stats")
    p_lookup = sp.add_parser("lookup"); p_lookup.add_argument("name"); p_lookup.add_argument("--kind")
    p_kind   = sp.add_parser("kind");   p_kind.add_argument("kind")
    p_file   = sp.add_parser("file");   p_file.add_argument("path")
    p_refs   = sp.add_parser("refs");   p_refs.add_argument("name"); p_refs.add_argument("--kind", choices=["call", "type", "name"])
    p_unused = sp.add_parser("unused-static")

    p_callers = sp.add_parser("callers"); p_callers.add_argument("name")
    p_callees = sp.add_parser("callees"); p_callees.add_argument("name")
    p_cg      = sp.add_parser("callgraph")
    p_cg.add_argument("name")
    p_cg.add_argument("--direction", choices=["callees", "callers"], default="callees")
    p_cg.add_argument("--depth", type=int, default=3)
    p_cg.add_argument("--max-branch", type=int, default=12)

    p_slice = sp.add_parser("slice", help="markdown blob for LLM context")
    p_slice.add_argument("name")
    p_slice.add_argument("--callees", action="store_true", help="include callee bodies")
    p_slice.add_argument("--callers", action="store_true", help="include caller bodies")
    p_slice.add_argument("--types",   action="store_true", help="include struct/enum/typedef referenced inside body")
    p_slice.add_argument("--macros",  action="store_true", help="include function-like macros invoked inside body")
    p_slice.add_argument("--all",     action="store_true", help="shorthand for --callees --callers --types --macros")
    p_slice.add_argument("--depth",   type=int, default=1, help="transitive depth for callees/callers (default 1)")
    p_slice.add_argument("--max-bytes", type=int, default=0, help="cap output size in bytes (default: no cap)")

    for s in (p_stats, p_lookup, p_kind, p_file, p_refs, p_unused, p_callers, p_callees, p_cg, p_slice):
        s.add_argument("--index", default="tsindex.db")

    args = ap.parse_args()

    if args.cmd == "build":
        root = Path(args.root)
        if not root.is_dir():
            sys.exit(f"root not found: {root}")
        defs = None if args.no_preproc else Path(args.defs)
        if defs is not None and not defs.is_file():
            defs = None  # silently skip if default path doesn't exist
        build(root, Path(args.out), defs_path=defs,
              undef_unknown_configs=not args.keep_unknown_configs,
              force_full=args.full,
              verbose=not args.quiet)
        return

    idx_path = Path(args.index)
    if not idx_path.is_file():
        sys.exit(f"index not found: {idx_path}  (run `build` first)")
    idx = load_index(idx_path)

    if args.cmd == "stats":         cmd_stats(idx)
    elif args.cmd == "lookup":      cmd_lookup(idx, args.name, args.kind)
    elif args.cmd == "kind":        cmd_kind(idx, args.kind)
    elif args.cmd == "file":        cmd_file(idx, args.path)
    elif args.cmd == "refs":        cmd_refs(idx, args.name, args.kind)
    elif args.cmd == "unused-static": cmd_unused_static(idx)
    elif args.cmd == "callers":     cmd_callers(idx, args.name)
    elif args.cmd == "callees":     cmd_callees(idx, args.name)
    elif args.cmd == "callgraph":   cmd_callgraph(idx, args.name, args.direction, args.depth, args.max_branch)
    elif args.cmd == "slice":
        wc = args.callees or args.all
        wcr = args.callers or args.all
        wt = args.types or args.all
        wm = args.macros or args.all
        cmd_slice(idx, args.name,
                  with_callees=wc, with_callers=wcr,
                  with_types=wt, with_macros=wm,
                  depth=args.depth, max_bytes=args.max_bytes)


if __name__ == "__main__":
    main()
