"""Tests for the C++ walker."""
import unittest
from tests.helpers import build_fixture, names_of_kind, callers_of, callees_of


class CppWalkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.idx, cls.by_name = build_fixture("cpp")

    def test_class_in_namespace(self):
        svc = self.by_name["Service"][0]
        self.assertEqual(svc["kind"], "type")
        self.assertEqual(svc["kind_raw"], "class")
        self.assertEqual(svc["parent"], "demo")

    def test_inline_methods_have_parent(self):
        # `int helper(int x) const { ... }` defined inside class
        h = self.by_name["helper"][0]
        self.assertEqual(h["kind"], "function")
        self.assertEqual(h["parent"], "demo::Service")

    def test_out_of_line_method(self):
        # `int Service::process(int v) { ... }` — qualified definition.
        # Two `process` symbols: the in-class declaration AND the out-of-line def.
        procs = self.by_name["process"]
        self.assertGreaterEqual(len(procs), 1)
        defs = [s for s in procs if s["is_definition"]]
        self.assertTrue(defs)
        self.assertIn(defs[0]["parent"], ("demo::Service", "Service"))

    def test_field(self):
        c = self.by_name["count"][0]
        self.assertEqual(c["kind"], "variable")
        self.assertEqual(c["parent"], "demo::Service")

    def test_free_function(self):
        f = self.by_name["free_function"][0]
        self.assertEqual(f["kind"], "function")
        self.assertIsNone(f.get("parent"))

    def test_template_unwrapped(self):
        # `template <typename T> T identity(T x)` — only the inner function is indexed
        self.assertIn("identity", names_of_kind(self.idx, "function"))

    def test_call_graph(self):
        # process() calls helper(); free_function calls process()
        self.assertIn("helper", callees_of(self.idx, "process"))
        self.assertIn("process", callees_of(self.idx, "free_function"))


if __name__ == "__main__":
    unittest.main()
