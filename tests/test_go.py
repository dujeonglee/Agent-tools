"""Tests for the Go walker."""
import unittest
from tests.helpers import build_fixture, names_of_kind, callers_of, callees_of


class GoWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("go")

    def test_function_extracted(self):
        funcs = names_of_kind(self.idx, "function")
        self.assertIn("Helper", funcs)
        self.assertIn("unexported", funcs)

    def test_method_has_receiver_as_parent(self):
        # Two methods on *Point
        for name in ("String", "Sum"):
            sym = self.by_name[name][0]
            self.assertEqual(sym["kind"], "function")
            self.assertEqual(sym["parent"], "Point")

    def test_struct_is_type(self):
        point = self.by_name["Point"][0]
        self.assertEqual(point["kind"], "type")
        self.assertEqual(point["kind_raw"], "struct")

    def test_interface_is_type(self):
        stringer = self.by_name["Stringer"][0]
        self.assertEqual(stringer["kind"], "type")
        self.assertEqual(stringer["kind_raw"], "interface")

    def test_const_is_constant(self):
        self.assertIn("MaxRetries", names_of_kind(self.idx, "constant"))

    def test_var_is_variable(self):
        self.assertIn("counter", names_of_kind(self.idx, "variable"))

    def test_exported_modifier(self):
        helper = self.by_name["Helper"][0]
        self.assertIn("exported", helper.get("modifiers") or [])
        un = self.by_name["unexported"][0]
        self.assertNotIn("exported", un.get("modifiers") or [])

    def test_call_graph(self):
        self.assertIn("Helper", callees_of(self.idx, "unexported"))
        self.assertIn("unexported", callers_of(self.idx, "Helper"))
        self.assertIn("Sum", callers_of(self.idx, "Helper"))


if __name__ == "__main__":
    unittest.main()
