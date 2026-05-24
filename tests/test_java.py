"""Tests for the Java walker."""
import unittest
from tests.helpers import build_fixture, names_of_kind, callers_of, callees_of


class JavaWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("java")

    def test_class_is_type(self):
        svc = self.by_name["Service"][0]
        self.assertEqual(svc["kind"], "type")
        self.assertEqual(svc["kind_raw"], "class_declaration")

    def test_interface_is_type(self):
        sym = self.by_name["Greeter"][0]
        self.assertEqual(sym["kind"], "type")
        self.assertEqual(sym["kind_raw"], "interface_declaration")

    def test_enum_is_type_with_constants(self):
        sym = self.by_name["Color"][0]
        self.assertEqual(sym["kind"], "type")
        self.assertEqual(sym["kind_raw"], "enum_declaration")
        self.assertEqual(set(sym["enum_values"]), {"RED", "GREEN", "BLUE"})

    def test_method_has_parent(self):
        for n in ("helper", "process"):
            sym = self.by_name[n][0]
            self.assertEqual(sym["kind"], "function")
            self.assertEqual(sym["parent"], "Service")

    def test_constructor(self):
        sym = self.by_name["Service"]
        # both class (type) and constructor (function) appear
        kinds = {s["kind"] for s in sym}
        self.assertIn("type", kinds)
        ctor = [s for s in sym if s["kind"] == "function"][0]
        self.assertEqual(ctor["kind_raw"], "constructor_declaration")
        self.assertEqual(ctor["parent"], "Service")

    def test_static_final_is_constant(self):
        c = self.by_name["MAX_RETRIES"][0]
        self.assertEqual(c["kind"], "constant")
        mods = c.get("modifiers") or []
        self.assertIn("static", mods)
        self.assertIn("final", mods)

    def test_instance_field_is_variable(self):
        c = self.by_name["counter"][0]
        self.assertEqual(c["kind"], "variable")
        self.assertEqual(c["parent"], "Service")

    def test_call_graph(self):
        # process() calls helper()
        self.assertIn("helper", callees_of(self.idx, "process"))
        self.assertIn("process", callers_of(self.idx, "helper"))


if __name__ == "__main__":
    unittest.main()
