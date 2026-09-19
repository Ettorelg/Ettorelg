"""Removable pizza ingredients follow the current product Ingredients field."""
import ast
import copy
import re
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self): self.statements = []
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params): self.statements.append((query, params))
    def fetchall(self): return [(10, "Margherita", "farina 00, pomodoro, mozzarella, semola, basilico")]


class Connection:
    def __init__(self): self.cur = Cursor()
    def cursor(self): return self.cur
    def close(self): pass


def functions(db):
    nodes = [copy.deepcopy(next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                                and node.name == name))
             for name in ("derive_pizzeria_removable_ingredients", "api_pizzeria_ingredienti")]
    for node in nodes: node.decorator_list = []
    scope = {"re": re, "session": session, "jsonify": jsonify,
             "get_user_shop_id": lambda _: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope


def test_ingredients_derive_from_product_and_exclude_flour():
    derive = functions(Connection())["derive_pizzeria_removable_ingredients"]
    assert derive("Farina di grano duro, Pomodoro; mozzarella.\nBasilico, pomodoro, Semola rimacinata") == [
        "Pomodoro", "mozzarella", "Basilico"]
    assert derive(None) == []
    assert len(derive(",".join(f"Ingrediente {i}" for i in range(50)))) == 40


def test_owner_reads_current_ingredients_without_writes():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/ingredienti"):
        session["user_id"] = 3
        response = functions(db)["api_pizzeria_ingredienti"]()
    assert response.get_json()["pizze"][0]["ingredienti_rimovibili"] == [
        "pomodoro", "mozzarella", "basilico"]
    assert len(db.cur.statements) == 1
    assert db.cur.statements[0][1] == (7,)
    assert "prodotti" in db.cur.statements[0][0]
    assert "pizzeria_ingredienti" not in db.cur.statements[0][0]


def test_employee_cannot_read_owner_draft():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/ingredienti"):
        session["user_id"] = 3; session["employee_id"] = 4
        _, status = functions(db)["api_pizzeria_ingredienti"]()
    assert status == 403
    assert db.cur.statements == []
