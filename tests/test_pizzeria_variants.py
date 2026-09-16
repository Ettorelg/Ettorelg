"""Pizzeria mixed-cut and addition settings remain owner-scoped drafts."""
import ast
import copy
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, request, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self): self.query = ""; self.statements = []; self.saved = None
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params):
        self.query = query
        self.statements.append((query, params))
        if "INSERT INTO pizzeria_varianti_config" in query:
            self.saved = (json.loads(params[1]), json.loads(params[2]))
    def fetchall(self):
        if "FROM categorie" in self.query: return [(2, "Pizze")]
        if "FROM prodotti" in self.query: return [(10, "Margherita", 2)]
        if "FROM pizzeria_formati" in self.query: return [("Singola",), ("Gigante",)]
        return []
    def fetchone(self): return self.saved


class Connection:
    def __init__(self): self.cur = Cursor()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def functions(db):
    nodes = [copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef)
                                and item.name == name))
             for name in ("normalize_pizzeria_variants", "calculate_pizzeria_multigusto_price", "api_pizzeria_varianti")]
    for node in nodes: node.decorator_list = []
    scope = {"Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP, "json": json, "request": request, "session": session,
             "jsonify": jsonify, "get_user_shop_id": lambda _: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope


def payload():
    return {"frazioni": [{"formato": "Gigante", "tagli": [2, 3, 4]}],
            "aggiunte": [{"nome": "Mozzarella extra", "id_categoria": 2, "id_prodotto": None,
                           "prezzi": {"Singola": "1,50", "Gigante": "3.00"}, "disponibile": True}]}


def test_valid_rules_have_normalized_prices():
    fractions, additions = functions(Connection())["normalize_pizzeria_variants"](
        payload(), {2}, {10: 2}, {"singola": "Singola", "gigante": "Gigante"})
    assert fractions[0]["tagli"] == [2, 3, 4]
    assert fractions[0]["formato"] == "Gigante"
    assert additions[0]["prezzi"] == {"Singola": "1.50", "Gigante": "3.00"}


def test_one_addition_can_target_multiple_categories():
    item = payload()
    item["aggiunte"][0].update(id_categoria=None, id_categorie=[2, 3])
    _, additions = functions(Connection())["normalize_pizzeria_variants"](
        item, {2, 3}, {10: 2}, {"singola": "Singola", "gigante": "Gigante"})
    assert additions[0]["id_categorie"] == [2, 3]
    assert additions[0]["id_categoria"] is None


def test_multigusto_price_weights_flavours_and_fractional_toppings():
    calculate = functions(Connection())["calculate_pizzeria_multigusto_price"]
    assert calculate("Gigante", 3, [
        {"formato": "Gigante", "quota": 2, "prezzo_gusto": "9.00", "aggiunte": []},
        {"formato": "Gigante", "quota": 1, "prezzo_gusto": "15.00", "aggiunte": ["3.00"]},
    ]) == Decimal("12.00")  # 2/3 of €9 + 1/3 of (€15 + €3).


def test_multigusto_price_rejects_different_format_or_incomplete_fraction():
    calculate = functions(Connection())["calculate_pizzeria_multigusto_price"]
    with pytest.raises(ValueError):
        calculate("Gigante", 3, [
            {"formato": "Gigante", "quota": 2, "prezzo_gusto": "9.00", "aggiunte": []},
            {"formato": "Singola", "quota": 1, "prezzo_gusto": "8.00", "aggiunte": []},
        ])
    with pytest.raises(ValueError):
        calculate("Gigante", 3, [
            {"formato": "Gigante", "quota": 1, "prezzo_gusto": "9.00", "aggiunte": []},
            {"formato": "Gigante", "quota": 1, "prezzo_gusto": "8.00", "aggiunte": []},
        ])


@pytest.mark.parametrize("change", [
    lambda p: p["frazioni"][0].update(formato="Altro"),
    lambda p: p["frazioni"].append({"formato": "Gigante", "tagli": [2]}),
    lambda p: p["frazioni"][0].update(tagli=[2, 2]),
    lambda p: p["frazioni"][0].update(tagli=[5]),
    lambda p: p["aggiunte"][0].update(id_prodotto=10),
    lambda p: p["aggiunte"][0].update(id_categoria=None, id_prodotto=999),
    lambda p: p["aggiunte"][0].update(prezzi={"Media": "2"}),
    lambda p: p["aggiunte"][0].update(prezzi={"Singola": "1.001"}),
])
def test_invalid_or_cross_shop_rules_rejected(change):
    item = payload(); change(item)
    with pytest.raises(ValueError):
        functions(Connection())["normalize_pizzeria_variants"](
            item, {2}, {10: 2}, {"singola": "Singola", "gigante": "Gigante"})


def test_save_does_not_activate_public_module():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/varianti", method="PUT", json=payload()):
        session["user_id"] = 3
        response = functions(db)["api_pizzeria_varianti"]()
    assert response.get_json()["attivo"] is False
    assert db.cur.saved[1][0]["prezzi"]["Gigante"] == "3.00"
    assert all(params[0] == 7 for _, params in db.cur.statements)
    assert all("UPDATE ordini" not in query and "UPDATE negozi" not in query
               for query, _ in db.cur.statements)
    product_query = next(query for query, _ in db.cur.statements if "FROM prodotti" in query)
    assert "JOIN pizzeria_formati" in product_query


def test_employee_cannot_edit_variants():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/varianti", method="PUT", json=payload()):
        session["user_id"] = 3; session["employee_id"] = 4
        _, status = functions(db)["api_pizzeria_varianti"]()
    assert status == 403
    assert db.cur.statements == []
