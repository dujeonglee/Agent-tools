"""Tests for the Python walker."""
import unittest
from tests.helpers import build_fixture, names_of_kind, callers_of, callees_of


class PythonWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("python")

    # ----- symbol kinds -----

    def test_function_extracted(self):
        self.assertIn("helper", names_of_kind(self.idx, "function"))

    def test_class_is_type(self):
        types = names_of_kind(self.idx, "type")
        self.assertIn("Service", types)
        self.assertIn("DerivedService", types)

    def test_methods_have_parent(self):
        init = self.by_name["__init__"][0]
        self.assertEqual(init["kind"], "function")
        self.assertEqual(init["parent"], "Service")

    def test_upper_snake_is_constant(self):
        self.assertIn("MAX_RETRIES", names_of_kind(self.idx, "constant"))

    def test_lowercase_module_var_is_variable(self):
        self.assertIn("default_timeout", names_of_kind(self.idx, "variable"))

    def test_class_attr_is_variable_with_parent(self):
        attr = self.by_name["instance_count"][0]
        self.assertEqual(attr["kind"], "variable")
        self.assertEqual(attr["parent"], "Service")

    # ----- modifiers -----

    def test_async_modifier(self):
        async_methods = [s for s in self.by_name["process"]
                         if "async" in (s.get("modifiers") or [])]
        self.assertEqual(len(async_methods), 1)
        self.assertEqual(async_methods[0]["parent"], "Service")

    def test_decorator_modifiers(self):
        label = self.by_name["label"][0]
        self.assertIn("property", label["modifiers"] or [])
        make = self.by_name["make"][0]
        self.assertIn("staticmethod", make["modifiers"] or [])

    # ----- call graph -----

    def test_helper_called_from_process(self):
        # Two `process` definitions both call helper(), so process appears twice
        # in callers; here we just verify the set.
        self.assertIn("process", callers_of(self.idx, "helper"))

    def test_async_process_callees_include_helper(self):
        # cmd_callees uses idx.find_symbols(kind=function), which returns both
        # process defs — but the callgraph keys both go to the same name.
        callees = callees_of(self.idx, "process")
        self.assertIn("helper", callees)


if __name__ == "__main__":
    unittest.main()
