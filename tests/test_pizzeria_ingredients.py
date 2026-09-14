"""Pizza ingredient removals are owner-configured and never discount the price."""
import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, request, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self, owned=True): self.owned = owned; self.query = ""; self.statements = []; self.saved = []
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params):
        self.query = query; self.statements.append((query, params))
        if "INSERT INTO pizzeria_ingredienti" in query: self.saved = json.loads(params[2])
    def fetchone(self): return (1,) if self.owned else None
    def fetchall(self): return [(10, "Margherita", "Pomodoro, mozzarella", self.saved)]


class Connection:
    def __init__(self, owned=True): self.cur = Cursor(owned)
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def functions(db):
    nodes = [copy.deepcopy(next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                                and node.name == name))
             for name in ("normalize_pizzeria_ingredients", "api_pizzeria_ingredienti")]
    for node in nodes: node.decorator_list = []
    scope = {"json": json, "request": request, "session": session, "jsonify": jsonify,
             "get_user_shop_id": lambda _: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope


def test_names_are_trimmed_and_unique():
    normalize = functions(Connection())["normalize_pizzeria_ingredients"]
    assert normalize({"id_prodotto": 10, "ingredienti": [" Mozzarella ", "Pomodoro"]}) == (10, ["Mozzarella", "Pomodoro"])
    with pytest.raises(ValueError):
        normalize({"id_prodotto": 10, "ingredienti": ["Mozzarella", "mozzarella"]})


def test_owner_can_save_only_own_pizza():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/ingredienti", method="PUT", json={
        "id_prodotto": 10, "ingredienti": ["Mozzarella"]}):
        session["user_id"] = 3
        response = functions(db)["api_pizzeria_ingredienti"]()
    assert response.get_json()["pizze"][0]["ingredienti_rimovibili"] == ["Mozzarella"]
    assert any("EXISTS (SELECT 1 FROM pizzeria_formati" in query and params == (10, 7)
               for query, params in db.cur.statements)
    assert all("UPDATE prodotti" not in query for query, _ in db.cur.statements)


def test_other_shop_pizza_rejected():
    db = Connection(owned=False)
    with FLASK.test_request_context("/api/pizzeria/ingredienti", method="PUT", json={
        "id_prodotto": 10, "ingredienti": ["Mozzarella"]}):
        session["user_id"] = 3
        _, status = functions(db)["api_pizzeria_ingredienti"]()
    assert status == 404
    assert not any("INSERT INTO pizzeria_ingredienti" in query for query, _ in db.cur.statements)


def test_employee_cannot_edit():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/ingredienti", method="PUT", json={
        "id_prodotto": 10, "ingredienti": ["Mozzarella"]}):
        session["user_id"] = 3; session["employee_id"] = 4
        _, status = functions(db)["api_pizzeria_ingredienti"]()
    assert status == 403
