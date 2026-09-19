"""Calzones and sandwiches inherit the selected pizza format price unless overridden."""
import ast
import copy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, request, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self): self.query = ""; self.statements = []; self.saved = []
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params):
        self.query = query
        self.statements.append((query, params))
        if "DELETE FROM pizzeria_derivati" in query: self.saved.clear()
        if "INSERT INTO pizzeria_derivati" in query: self.saved.append(params)
    def fetchall(self):
        if "FROM pizzeria_formati" in self.query:
            return [(10, "Margherita", "Singola", Decimal("8.00"), True),
                    (10, "Margherita", "Doppia", Decimal("15.00"), True),
                    (10, "Margherita", "Familiare", Decimal("22.00"), True)]
        if "FROM pizzeria_derivati" in self.query:
            return [(item[1], item[2], item[3], Decimal(item[4]) if item[4] is not None else None, item[5])
                    for item in self.saved]
        return []


class Connection:
    def __init__(self): self.cur = Cursor()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def functions(db):
    nodes = [copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef)
                                and item.name == name))
             for name in ("normalize_pizzeria_derivatives", "api_pizzeria_derivati")]
    for node in nodes: node.decorator_list = []
    scope = {"Decimal": Decimal, "request": request, "session": session, "jsonify": jsonify,
             "get_user_shop_id": lambda _: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope


def item(**changes):
    result = {"id_pizza": 10, "tipo": "calzone", "formato": "Singola", "prezzo_override": None, "disponibile": True}
    result.update(changes)
    return result


def test_default_and_override_are_normalized():
    normalize = functions(Connection())["normalize_pizzeria_derivatives"]
    formats = {10: {"singola": "Singola", "doppia": "Doppia", "familiare": "Familiare"}}
    assert normalize({"derivati": [item()]}, formats)[0]["prezzo_override"] is None
    assert normalize({"derivati": [item(prezzo_override="9,50")]}, formats)[0]["prezzo_override"] == "9.50"
    assert normalize({"derivati": [item()]}, formats)[0]["panette_per_unita"] == 1
    assert normalize({"derivati": [item(formato="Doppia")]}, formats)[0]["panette_per_unita"] is None
    assert len(normalize({"derivati": [item(), item(formato="Doppia"), item(formato="Familiare")]}, formats)) == 3


@pytest.mark.parametrize("items", [
    [item(id_pizza=999)],
    [item(tipo="pizza")],
    [item(prezzo_override="9.999")],
    [item(prezzo_override="NaN")],
    [item(), item()],
    [item(formato="Non disponibile")],
])
def test_invalid_or_cross_shop_derivatives_rejected(items):
    with pytest.raises(ValueError):
        functions(Connection())["normalize_pizzeria_derivatives"]({"derivati": items}, {10: {"singola": "Singola", "doppia": "Doppia"}})


def test_save_is_owner_scoped_and_does_not_activate():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/derivati", method="PUT", json={"derivati": [item(), item(formato="Doppia"), item(formato="Familiare", tipo="panino")]}):
        session["user_id"] = 3
        response = functions(db)["api_pizzeria_derivati"]()
    result = response.get_json()
    assert result["attivo"] is False
    assert result["derivati"][0]["prezzo_effettivo"] == "8.00"
    assert result["derivati"][0]["formato"] == "Singola"
    assert result["derivati"][0]["panette_per_unita"] == 1
    assert {row["formato"]: row["prezzo_effettivo"] for row in result["derivati"]} == {
        "Singola": "8.00", "Doppia": "15.00", "Familiare": "22.00"}
    assert result["derivati"][1]["panette_per_unita"] is None
    assert all(params[0] == 7 for _, params in db.cur.statements)
    assert all("UPDATE negozi" not in query and "UPDATE ordini" not in query
               for query, _ in db.cur.statements)


def test_employee_cannot_edit_derivatives():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/derivati", method="PUT", json={"derivati": [item()]}):
        session["user_id"] = 3; session["employee_id"] = 4
        _, status = functions(db)["api_pizzeria_derivati"]()
    assert status == 403
    assert db.cur.statements == []
