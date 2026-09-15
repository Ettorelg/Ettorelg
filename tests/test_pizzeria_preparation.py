"""Preparation drafts remain shop-scoped and do not consume stock."""
import ast
import copy
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, request, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self):
        self.query = ""
        self.statements = []
        self.saved = None

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params):
        self.query = query
        self.statements.append((query, params))
        if "INSERT INTO pizzeria_preparazione_config" in query:
            self.saved = (json.loads(params[1]), json.loads(params[2]))

    def fetchall(self):
        return [("Singola",), ("Gigante",)]

    def fetchone(self):
        return self.saved


class Connection:
    def __init__(self): self.cur = Cursor()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def functions(db):
    nodes = [copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef)
                                and item.name == name))
             for name in ("normalize_pizzeria_preparation", "api_pizzeria_preparazione")]
    for node in nodes: node.decorator_list = []
    scope = {"Decimal": Decimal, "json": json, "request": request, "session": session,
             "jsonify": jsonify, "get_user_shop_id": lambda _: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope


def payload():
    return {"impasti": [
        {"nome": "Classico", "supplemento": "0.00", "disponibile": True},
        {"nome": "Integrale", "supplemento": "2,50", "disponibile": True}],
        "panette": [{"formato": "Singola", "impasto": "Integrale", "quantita": 30,
                     "illimitate": False}]}


def test_doughs_and_stocks_normalized():
    doughs, stocks = functions(Connection())["normalize_pizzeria_preparation"](payload(), ["Singola"])
    assert doughs[1]["supplemento"] == "2.50"
    assert stocks == [{"formato": "Singola", "impasto": "Integrale", "quantita": 30,
                       "illimitate": False}]


@pytest.mark.parametrize("change", [
    lambda p: p["impasti"].pop(0),
    lambda p: p["panette"][0].update(formato="Non mio"),
    lambda p: p["panette"][0].update(formato="Gigante"),
    lambda p: p["panette"][0].update(quantita=-1),
    lambda p: p["panette"].append(p["panette"][0].copy()),
])
def test_invalid_preparation_rejected(change):
    item = payload()
    change(item)
    with pytest.raises(ValueError):
        functions(Connection())["normalize_pizzeria_preparation"](item, ["Singola", "Gigante"])


def test_save_is_scoped_and_does_not_activate_or_consume():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/preparazione", method="PUT", json=payload()):
        session["user_id"] = 3
        response = functions(db)["api_pizzeria_preparazione"]()
    assert response.get_json()["attivo"] is False
    assert db.cur.saved[1][0]["quantita"] == 30
    assert all(params[0] == 7 for query, params in db.cur.statements)
    assert all("UPDATE negozi" not in query and "UPDATE ordini" not in query
               for query, _ in db.cur.statements)


def test_employee_cannot_save():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/preparazione", method="PUT", json=payload()):
        session["user_id"] = 3
        session["employee_id"] = 5
        _, status = functions(db)["api_pizzeria_preparazione"]()
    assert status == 403
    assert db.cur.statements == []
