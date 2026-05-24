"""Shared helpers for tsindex unit tests.

Each language test follows this pattern:

    from tests.helpers import build_fixture
    idx, syms_by_name = build_fixture("python")
    self.assertIn("MyClass", syms_by_name)

Fixtures live in tests/fixtures/<lang>/, indexed into a fresh temp DB per
test method. Index is built once per fixture (class-scoped) for speed.
"""
from __future__ import annotations
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Callable

# Make the tsindex module importable when tests are run from the repo root.
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import tsindex  # noqa: E402


FIXTURES = REPO / "tests" / "fixtures"


def build_fixture(lang: str) -> tuple[tsindex.IndexStore, dict[str, list]]:
    """Build an index over tests/fixtures/<lang>/ into a fresh temp DB and
    return (store, symbols-grouped-by-name)."""
    root = FIXTURES / lang
    if not root.is_dir():
        raise FileNotFoundError(f"fixture directory not found: {root}")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        out = Path(tf.name)
    tsindex.build(root, out, defs_path=None, undef_unknown_configs=False,
                  force_full=True, verbose=False)
    store = tsindex.load_index(out)
    by_name: dict[str, list] = defaultdict(list)
    for s in store.all_symbols():
        by_name[s["name"]].append(s)
    return store, by_name


def syms_of_kind(idx: tsindex.IndexStore, kind: str) -> list[dict]:
    return idx.find_symbols(kind=kind)


def names_of_kind(idx: tsindex.IndexStore, kind: str) -> set[str]:
    return {s["name"] for s in syms_of_kind(idx, kind)}


def callers_of(idx, name: str) -> set[str]:
    """Set of caller function names for the given target."""
    _, callers, _ = tsindex.build_callgraph(idx)
    return set(callers.get(name, {}).keys())


def callees_of(idx, name: str) -> set[str]:
    calls, _, _ = tsindex.build_callgraph(idx)
    return set(calls.get(name, {}).keys())
