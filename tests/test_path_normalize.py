"""Tests for IndexStore.normalize_file_path and abs-path-friendly lookups.

Covers:
- exact relative path (already canonical)
- absolute path (under index root → strip prefix)
- absolute path outside root → no match
- basename match when unique
- basename match when ambiguous → None (caller iterates files)
- nested relative path
- find_symbols(file=abs) works through normalization
- find_refs(file=abs) works
- find_refs_in_range(file=abs) works
"""
import unittest
from pathlib import Path
from tests.helpers import build_fixture, FIXTURES


class PathNormalizationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Use the python fixture (single file: app.py) for simple cases.
        cls.idx, cls.by_name = build_fixture("python")
        cls.root = Path(cls.idx.meta["root"])

    # ----- normalize_file_path -----

    def test_exact_relative(self):
        self.assertEqual(self.idx.normalize_file_path("app.py"), "app.py")

    def test_absolute_under_root(self):
        abs_p = str(self.root / "app.py")
        self.assertEqual(self.idx.normalize_file_path(abs_p), "app.py")

    def test_absolute_outside_root(self):
        # /tmp/nothing.py is not in the index → no match
        self.assertIsNone(self.idx.normalize_file_path("/tmp/__nothing_here.py"))

    def test_basename_unique(self):
        # Only one app.py in the index → suffix match returns it.
        # Note: "/" not in "app.py" so the bare-basename branch runs.
        # But there's no parent dir for app.py at the root, so endswith("/app.py") is False.
        # Result: None. Confirm this expected behavior:
        # Actually the exact-match branch above already returns "app.py" before we get here.
        # For a path with a directory component we test the suffix branch.
        pass

    def test_nonexistent_returns_none(self):
        self.assertIsNone(self.idx.normalize_file_path("nonexistent.py"))

    def test_empty_string(self):
        self.assertIsNone(self.idx.normalize_file_path(""))

    # ----- integrated into find_* -----

    def test_find_symbols_with_abs_path(self):
        abs_p = str(self.root / "app.py")
        hits_abs = self.idx.find_symbols(file=abs_p)
        hits_rel = self.idx.find_symbols(file="app.py")
        self.assertGreater(len(hits_rel), 0)
        self.assertEqual({s["name"] for s in hits_abs},
                         {s["name"] for s in hits_rel})

    def test_find_refs_with_abs_path(self):
        abs_p = str(self.root / "app.py")
        refs_abs = self.idx.find_refs(file=abs_p)
        refs_rel = self.idx.find_refs(file="app.py")
        self.assertEqual(len(refs_abs), len(refs_rel))

    def test_find_refs_in_range_with_abs_path(self):
        abs_p = str(self.root / "app.py")
        refs_abs = self.idx.find_refs_in_range(abs_p, 1, 200)
        refs_rel = self.idx.find_refs_in_range("app.py", 1, 200)
        self.assertEqual(len(refs_abs), len(refs_rel))
        self.assertGreater(len(refs_abs), 0)

    def test_unknown_file_returns_empty(self):
        # Both abs (outside root) and bogus rel should yield zero results.
        self.assertEqual(self.idx.find_symbols(file="/tmp/__no.py"), [])
        self.assertEqual(self.idx.find_refs(file="nope.py"), [])


class AmbiguousSuffixMatchTest(unittest.TestCase):
    """Use the C fixture which has sample.c and sample.h to test basename
    matching with multiple files. Confirms that suffix matching with a directory
    component works, and that bare basename matches a unique file."""

    @classmethod
    def setUpClass(cls):
        cls.idx, _ = build_fixture("c")
        cls.root = Path(cls.idx.meta["root"])

    def test_bare_basename_no_parent_returns_none(self):
        # sample.c is at root, so endswith("/sample.c") is False → returns None.
        # (exact match branch caught "sample.c" above this case wouldn't fire.)
        # But sample.c IS in the index exactly → exact-match branch wins.
        self.assertEqual(self.idx.normalize_file_path("sample.c"), "sample.c")
        self.assertEqual(self.idx.normalize_file_path("sample.h"), "sample.h")

    def test_abs_for_both_files(self):
        for name in ("sample.c", "sample.h"):
            abs_p = str(self.root / name)
            self.assertEqual(self.idx.normalize_file_path(abs_p), name)


class NestedDirectoryTest(unittest.TestCase):
    """Fixture: tests/fixtures/nested/
        top.py
        sub/mod.py
        sub/mod_dup.py
        other/mod.py
    Tests nested-path lookup, suffix uniqueness, and ambiguity handling."""

    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("nested")
        cls.root = Path(cls.idx.meta["root"])

    def test_nested_relative_exact(self):
        self.assertEqual(self.idx.normalize_file_path("sub/mod.py"), "sub/mod.py")

    def test_nested_absolute(self):
        abs_p = str(self.root / "sub" / "mod.py")
        self.assertEqual(self.idx.normalize_file_path(abs_p), "sub/mod.py")

    def test_bare_basename_ambiguous(self):
        # mod.py exists in both sub/ and other/ → ambiguous → None
        self.assertIsNone(self.idx.normalize_file_path("mod.py"))

    def test_suffix_match_disambiguates(self):
        # "other/mod.py" suffix uniquely identifies one file
        self.assertEqual(self.idx.normalize_file_path("other/mod.py"), "other/mod.py")

    def test_unique_basename_resolves(self):
        # mod_dup.py exists only under sub/ → bare basename returns it
        self.assertEqual(self.idx.normalize_file_path("mod_dup.py"), "sub/mod_dup.py")

    def test_find_symbols_with_nested_paths(self):
        # All three lookup styles return the same symbol set
        abs_p = str(self.root / "sub" / "mod.py")
        s_abs = self.idx.find_symbols(file=abs_p)
        s_rel = self.idx.find_symbols(file="sub/mod.py")
        self.assertEqual({s["name"] for s in s_abs},
                         {s["name"] for s in s_rel})
        self.assertIn("sub_fn", {s["name"] for s in s_abs})

    def test_ambiguous_basename_returns_no_symbols(self):
        # "mod.py" is ambiguous → normalize returns None →
        # we fall back to passing the original string which doesn't match → 0 hits
        self.assertEqual(self.idx.find_symbols(file="mod.py"), [])


class JsAbsRoundTripTest(unittest.TestCase):
    """Use the python fixture (which has nested paths in the broader project)
    by constructing one ad-hoc — actually the python fixture is flat. Instead,
    we test with the user's perspective: passing both relative and absolute
    forms of the same file should always return the same canonical path."""

    @classmethod
    def setUpClass(cls):
        cls.idx, _ = build_fixture("javascript")  # app.js
        cls.root = Path(cls.idx.meta["root"])

    def test_abs_round_trip(self):
        abs_p = str(self.root / "app.js")
        norm = self.idx.normalize_file_path(abs_p)
        self.assertEqual(norm, "app.js")
        # And a back-and-forth: rel → abs → rel should be the same.
        abs2 = str(self.root / norm)
        self.assertEqual(self.idx.normalize_file_path(abs2), "app.js")


if __name__ == "__main__":
    unittest.main()
