"""Tests for the Rust walker."""
import unittest
from tests.helpers import build_fixture, names_of_kind, callers_of, callees_of


class RustWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("rust")

    def test_function_extracted(self):
        funcs = names_of_kind(self.idx, "function")
        self.assertIn("helper", funcs)
        self.assertIn("private_caller", funcs)

    def test_struct_is_type(self):
        sym = self.by_name["Point"][0]
        self.assertEqual(sym["kind"], "type")
        self.assertEqual(sym["kind_raw"], "struct")

    def test_enum_is_type_with_variants(self):
        sym = self.by_name["Color"][0]
        self.assertEqual(sym["kind"], "type")
        self.assertEqual(sym["kind_raw"], "enum")
        self.assertEqual(set(sym["enum_values"]), {"Red", "Green", "Blue"})

    def test_trait_is_type(self):
        sym = self.by_name["Greet"][0]
        self.assertEqual(sym["kind"], "type")
        self.assertEqual(sym["kind_raw"], "trait")

    def test_const_is_constant(self):
        self.assertIn("MAX_RETRIES", names_of_kind(self.idx, "constant"))
        c = self.by_name["MAX_RETRIES"][0]
        self.assertIn("pub", c.get("modifiers") or [])

    def test_static_is_variable(self):
        self.assertIn("COUNTER", names_of_kind(self.idx, "variable"))

    def test_impl_methods_have_parent(self):
        new_method = self.by_name["new"][0]
        self.assertEqual(new_method["parent"], "Point")
        sum_method = self.by_name["sum"][0]
        self.assertEqual(sum_method["parent"], "Point")

    def test_macro_rules_is_function(self):
        sym = self.by_name["shout"][0]
        self.assertEqual(sym["kind"], "function")
        self.assertEqual(sym["kind_raw"], "macro_definition")

    def test_call_graph_includes_macro_calls(self):
        # private_caller invokes helper; sum invokes helper too.
        callers = callers_of(self.idx, "helper")
        self.assertIn("private_caller", callers)
        self.assertIn("sum", callers)


if __name__ == "__main__":
    unittest.main()
