"""Pizzeria format drafts are per product and cannot alter live order prices."""
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
    def __init__(self, owned=True):
        self.owned = owned
        self.query = ""
        self.statements = []
        self.formats = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params):
        self.query = query
        self.statements.append((query, params))
        if "INSERT INTO pizzeria_formati" in query:
            self.formats.append(params)
        if "DELETE FROM pizzeria_formati" in query:
            self.formats.clear()
    def fetchone(self):
        if "SELECT unita_prezzo" in self.query:
            return ("pezzo",) if self.owned else None
        if "SELECT modulo_pizzeria_attivo" in self.query:
            return (False,)
        return None
    def fetchall(self):
        if "FROM prodotti p LEFT JOIN pizzeria_formati" in self.query:
            if self.formats:
                return [(10, "Margherita", Decimal("8.00"), "pezzo", item[2], Decimal(item[3]), item[4])
                        for item in self.formats]
            return [(10, "Margherita", Decimal("8.00"), "pezzo", None, None, None)]
        return []


class Connection:
    def __init__(self, owned=True): self.cur = Cursor(owned)
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def functions(db):
    nodes = [copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef)
                                and item.name == name))
             for name in ("normalize_pizzeria_formats", "api_pizzeria_formati")]
    for node in nodes: node.decorator_list = []
    scope = {"Decimal": Decimal, "request": request, "session": session, "jsonify": jsonify,
             "get_user_shop_id": lambda user_id: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope


def category_normalizer():
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef)
                              and item.name == "normalize_category_pizzeria_config"))
    scope = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["normalize_category_pizzeria_config"]


def test_formats_are_optional_per_product_and_prices_are_normalized():
    product_id, formats = functions(Connection())["normalize_pizzeria_formats"]({
        "id_prodotto": 10, "formati": [{"nome": "Singola", "prezzo": "8,50"}]})
    assert product_id == 10
    assert formats == [{"nome": "Singola", "prezzo": "8.50", "disponibile": True, "impasti": ["Classico"]}]


@pytest.mark.parametrize("formats", [
    [{"nome": "Singola", "prezzo": -1}],
    [{"nome": "Singola", "prezzo": "8.001"}],
    [{"nome": "Singola", "prezzo": "8"}, {"nome": "singola", "prezzo": "9"}],
])
def test_invalid_formats_are_rejected(formats):
    with pytest.raises(ValueError):
        functions(Connection())["normalize_pizzeria_formats"]({"id_prodotto": 10, "formati": formats})


def test_empty_formats_disable_pizzeria_configuration_for_product():
    product_id, formats = functions(Connection())["normalize_pizzeria_formats"]({"id_prodotto": 10, "formati": []})
    assert product_id == 10
    assert formats == []


def test_category_owns_ordered_formats_and_product_type():
    assert category_normalizer()({"tipo_pizzeria": "calzone", "formati": ["Normale", "Doppio"], "combina_gusti": True}) == (
        "calzone", ["Normale", "Doppio"], True, [], [])


def test_category_can_limit_tastes_to_categories_and_products():
    assert category_normalizer()({
        "tipo_pizzeria": "pizza", "formati": ["Singola"], "combina_gusti": True,
        "categorie_gusti": [8, 8, 12], "prodotti_gusti": [40, 41, 40],
    }) == ("pizza", ["Singola"], True, [8, 12], [40, 41])


@pytest.mark.parametrize("payload", [
    {"tipo_pizzeria": "calzone", "formati": []},
    {"tipo_pizzeria": "standard", "formati": ["Singola"]},
    {"tipo_pizzeria": "pizza", "formati": ["Singola", "singola"]},
])
def test_invalid_category_format_configuration_is_rejected(payload):
    with pytest.raises(ValueError):
        category_normalizer()(payload)


def test_format_save_is_scoped_to_owner_shop_and_does_not_activate_module():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/formati", method="PUT", json={
        "id_prodotto": 10, "formati": [{"nome": "Singola", "prezzo": "8"}]}):
        session["user_id"] = 3
        response = functions(db)["api_pizzeria_formati"]()
    assert response.get_json()["attivo"] is False
    assert response.get_json()["ordinazione_varianti_attiva"] is False
    assert response.get_json()["prodotti"][0]["formati"][0]["prezzo"] == "8.00"
    assert any("SELECT unita_prezzo" in query and params == (10, 7) for query, params in db.cur.statements)
    assert all("UPDATE negozi" not in query and "UPDATE prodotti" not in query for query, _ in db.cur.statements)


def test_other_shops_product_cannot_be_configured():
    db = Connection(owned=False)
    with FLASK.test_request_context("/api/pizzeria/formati", method="PUT", json={
        "id_prodotto": 10, "formati": [{"nome": "Singola", "prezzo": "8"}]}):
        session["user_id"] = 3
        _, status = functions(db)["api_pizzeria_formati"]()
    assert status == 404
    assert not any("DELETE FROM pizzeria_formati" in query for query, _ in db.cur.statements)
