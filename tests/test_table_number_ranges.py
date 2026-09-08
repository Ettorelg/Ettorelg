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


if __name__ == "__main__":
    unittest.main()
