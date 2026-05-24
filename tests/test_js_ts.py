"""Tests for the JavaScript and TypeScript walkers."""
import unittest
from tests.helpers import build_fixture, names_of_kind, callers_of, callees_of


class JavaScriptWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("javascript")

    def test_function_declaration(self):
        self.assertIn("helper", names_of_kind(self.idx, "function"))
        self.assertIn("loader", names_of_kind(self.idx, "function"))

    def test_arrow_const_is_function(self):
        # `const arrowFn = (x) => x + 1;` → kind=function
        sym = self.by_name["arrowFn"][0]
        self.assertEqual(sym["kind"], "function")
        self.assertEqual(sym["kind_raw"], "arrow_function")

    def test_class_is_type(self):
        sym = self.by_name["Service"][0]
        self.assertEqual(sym["kind"], "type")

    def test_method_has_parent(self):
        greet = self.by_name["greet"][0]
        self.assertEqual(greet["parent"], "Service")

    def test_static_method_modifier(self):
        make = self.by_name["make"][0]
        self.assertIn("static", make.get("modifiers") or [])

    def test_field_with_parent(self):
        f = self.by_name["instances"][0]
        self.assertEqual(f["kind"], "variable")
        self.assertEqual(f["parent"], "Service")

    def test_async_modifier(self):
        loader = self.by_name["loader"][0]
        self.assertIn("async", loader.get("modifiers") or [])

    def test_call_graph(self):
        # loader calls helper, make, greet
        cs = callees_of(self.idx, "loader")
        self.assertIn("helper", cs)
        self.assertIn("make", cs)
        self.assertIn("greet", cs)


class TypeScriptWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("typescript")

    def test_class_extracted(self):
        sym = self.by_name["Service"][0]
        self.assertEqual(sym["kind"], "type")

    def test_interface_is_type(self):
        sym = self.by_name["Greeter"][0]
        self.assertEqual(sym["kind"], "type")

    def test_type_alias_is_type(self):
        sym = self.by_name["Maybe"][0]
        self.assertEqual(sym["kind"], "type")
        self.assertEqual(sym["kind_raw"], "type_alias_declaration")

    def test_enum_is_type(self):
        sym = self.by_name["Status"][0]
        self.assertEqual(sym["kind"], "type")
        self.assertEqual(sym["kind_raw"], "enum_declaration")

    def test_exported_function(self):
        helper = self.by_name["helper"][0]
        self.assertEqual(helper["kind"], "function")
        # The export wrapper should record `exported` modifier
        self.assertIn("exported", helper.get("modifiers") or [])


if __name__ == "__main__":
    unittest.main()
