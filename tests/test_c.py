"""Tests for the C walker."""
import unittest
from tests.helpers import build_fixture, names_of_kind, callers_of, callees_of


class CWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("c")

    # ----- 4-kind vocab on C constructs -----

    def test_function_extracted(self):
        self.assertIn("helper", names_of_kind(self.idx, "function"))
        self.assertIn("compute", names_of_kind(self.idx, "function"))

    def test_object_macro_is_constant(self):
        self.assertIn("MAX_BUF", names_of_kind(self.idx, "constant"))
        macro = self.by_name["MAX_BUF"][0]
        self.assertEqual(macro["kind_raw"], "preproc_def")

    def test_function_macro_is_function(self):
        self.assertIn("DBG", names_of_kind(self.idx, "function"))
        macro = self.by_name["DBG"][0]
        self.assertEqual(macro["kind_raw"], "preproc_function_def")

    def test_struct_is_type(self):
        self.assertIn("point", names_of_kind(self.idx, "type"))

    def test_typedef_is_type(self):
        u32 = self.by_name.get("u32")
        self.assertIsNotNone(u32)
        self.assertEqual(u32[0]["kind"], "type")
        self.assertEqual(u32[0]["kind_raw"], "typedef")

    def test_enum_is_type(self):
        color = self.by_name.get("color")
        self.assertIsNotNone(color)
        self.assertEqual(color[0]["kind"], "type")
        self.assertEqual(color[0]["kind_raw"], "enum")
        self.assertIn("RED", color[0].get("enum_values") or [])

    def test_global_var_is_variable(self):
        self.assertIn("origin", names_of_kind(self.idx, "variable"))

    def test_static_modifier(self):
        helper = self.by_name["helper"][0]
        self.assertIn("static", helper.get("modifiers") or [])

    # ----- callgraph includes fn-like macros -----

    def test_macro_appears_in_call_graph(self):
        # compute() invokes DBG(). Since DBG is now kind=function, it should
        # appear in compute's callees.
        self.assertIn("DBG", callees_of(self.idx, "compute"))

    def test_function_to_function_edge(self):
        # compute() calls helper() twice.
        self.assertIn("helper", callees_of(self.idx, "compute"))
        self.assertIn("compute", callers_of(self.idx, "helper"))


if __name__ == "__main__":
    unittest.main()
