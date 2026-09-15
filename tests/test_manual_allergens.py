"""Regressioni per gli allergeni aggiunti dal titolare."""
import ast
from pathlib import Path
from types import SimpleNamespace


SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
KEYWORDS = ast.literal_eval(next(
    node.value for node in TREE.body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "ALLERGEN_KEYWORDS" for target in node.targets)
))


def load_function(name, scope):
    node = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope[name]


def test_manual_allergens_are_combined_without_duplicates():
    combine = load_function("combined_allergens", {"ALLERGEN_KEYWORDS": KEYWORDS})
    assert combine(["Latte", "Uova"], ["Crostacei", "Latte"]) == ["Crostacei", "Uova", "Latte"]
    assert combine([], ["Molluschi"]) == ["Molluschi"]


def test_manual_allergens_accept_only_known_unique_categories():
    values = ["Latte", "Molluschi"]
    scope = {"ALLERGEN_KEYWORDS": KEYWORDS, "request": SimpleNamespace(form=SimpleNamespace(getlist=lambda _: values))}
    parse = load_function("manual_allergens_from_form", scope)
    assert parse() == values
    values[:] = ["Latte", "Latte"]
    assert parse() is None
    values[:] = ["Non previsto"]
    assert parse() is None
