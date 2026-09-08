import ast
import re
from pathlib import Path
import unittest


class TableNumberRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "normalize_table_number_ranges")
        scope = {"re": re}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), scope)
        cls.normalize = staticmethod(scope["normalize_table_number_ranges"])

    def test_separate_ranges(self):
        self.assertEqual(self.normalize("1-30, 90-120"), ("1-30, 90-120", 61))

    def test_overlaps_are_rejected(self):
        self.assertIsNone(self.normalize("1-30, 30-40"))

    def test_malformed_range_is_rejected(self):
        self.assertIsNone(self.normalize("30-1"))

    def test_compound_quote_discount(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "qr_quote_price")
        scope = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), scope)
        quote = scope["qr_quote_price"](30, True, True)
        self.assertEqual(quote["base_cents"], 500)
        self.assertEqual(quote["unit_cents"], 365)
        self.assertEqual(quote["total_cents"], 10950)


if __name__ == "__main__":
    unittest.main()
