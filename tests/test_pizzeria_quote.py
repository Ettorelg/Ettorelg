"""A pizzeria draft quote trusts server-side settings, not browser prices."""
import ast
import copy
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, request, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
NODES = [copy.deepcopy(next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                            and node.name == name))
         for name in ("calculate_pizzeria_multigusto_price", "quote_pizzeria_draft", "derive_pizzeria_removable_ingredients", "load_pizzeria_order_settings")]
scope = {"Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP, "re": re}
exec(compile(ast.Module(body=NODES, type_ignores=[]), "app.py", "exec"), scope)
quote = scope["quote_pizzeria_draft"]
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self): self.query = ""; self.statements = []
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params): self.query = query; self.statements.append((query, params))
    def fetchall(self):
        if "FROM pizzeria_formati" in self.query: return [(10, 2, "Margherita", "Singola", Decimal("8.00"), True, "farina, pomodoro, mozzarella", "pizza", True)]
        if "FROM pizzeria_derivati" in self.query: return []
        return []
    def fetchone(self):
        if "FROM pizzeria_varianti_config" in self.query: return None
        if "FROM pizzeria_preparazione_config" in self.query: return None
        return None


class Connection:
    def __init__(self): self.cur = Cursor()
    def cursor(self): return self.cur
    def close(self): pass


def endpoint(db):
    node = copy.deepcopy(next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                              and node.name == "api_pizzeria_preventivo"))
    node.decorator_list = []
    local_scope = {**scope, "request": request, "session": session, "jsonify": jsonify,
                   "get_user_shop_id": lambda _: 7,
                   "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), local_scope)
    return local_scope["api_pizzeria_preventivo"]


def settings():
    pizzas = {
        10: {"id": 10, "id_categoria": 2, "tipo_pizzeria": "pizza", "formati": {"singola": {"prezzo": "8.00", "disponibile": True, "impasti": ["Classico", "Integrale"]}, "gigante": {"prezzo": "20.00", "disponibile": True, "impasti": ["Classico"]}}},
        11: {"id": 11, "id_categoria": 2, "tipo_pizzeria": "pizza", "formati": {"gigante": {"prezzo": "26.00", "disponibile": True, "impasti": ["Classico"]}}},
        12: {"id": 12, "id_categoria": 3, "tipo_pizzeria": "calzone", "formati": {"singola": {"prezzo": "9.00", "disponibile": True, "impasti": ["Classico"]}, "gigante": {"prezzo": "22.00", "disponibile": True, "impasti": ["Classico"]}}},
        14: {"id": 14, "id_categoria": 4, "tipo_pizzeria": "panino", "formati": {"singola": {"prezzo": "9.50", "disponibile": True, "impasti": ["Classico"]}}},
    }
    fractions = {"gigante": [2, 3]}
    additions = [{"id_categoria": 2, "id_prodotto": None, "prezzi": {"Singola": "1.50", "Gigante": "3.00"}, "disponibile": True}]
    derivatives = {(10, "calzone", "singola"): {"prezzo_override": None, "disponibile": True},
                   (10, "panino", "singola"): {"prezzo_override": "9.50", "disponibile": True},
                   (10, "calzone", "gigante"): {"prezzo_override": None, "disponibile": True}}
    doughs = {"classico": {"supplemento": "0.00", "disponibile": True},
              "integrale": {"supplemento": "2.00", "disponibile": True}}
    return pizzas, fractions, additions, derivatives, doughs


def test_single_pizza_uses_saved_prices_and_dough():
    result = quote({"tipo": "pizza", "id_pizza": 10, "formato": "Singola", "aggiunte": [0],
                    "impasto": "Integrale", "quantita": 2, "prezzo": "0.01"}, *settings())
    assert result == {"prezzo_unitario": "11.50", "quantita": 2, "totale": "23.00", "solo_anteprima": True}


def test_product_variant_override_is_enforced_server_side():
    local = settings()
    local[0][10]["varianti_abilitate"] = False
    with pytest.raises(ValueError, match="varianti non sono abilitate"):
        quote({"tipo": "pizza", "id_pizza": 10, "formato": "Singola", "aggiunte": [0]}, *local)


def test_removing_ingredient_never_reduces_price():
    settings_tuple = settings()
    base = quote({"tipo": "pizza", "id_pizza": 10, "formato": "Singola"}, *settings_tuple, {10: ["Mozzarella", "Basilico"]})
    without = quote({"tipo": "pizza", "id_pizza": 10, "formato": "Singola", "senza": [0]},
                    *settings_tuple, {10: ["Mozzarella", "Basilico"]})
    assert without["totale"] == base["totale"] == "8.00"
    assert without["ingredienti_tolti_per_gusto"] == [["Mozzarella"]]


def test_removal_applies_only_to_selected_multigusto_flavour():
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 2, "gusti": [
        {"id_pizza": 10, "quota": 1, "senza": [0]}, {"id_pizza": 11, "quota": 1, "senza": []}]},
        *settings(), {10: ["Mozzarella"], 11: ["Funghi"]})
    assert result["totale"] == "23.00"
    assert result["ingredienti_tolti_per_gusto"] == [["Mozzarella"], []]


def test_multigusto_pizza_averages_equal_tastes_and_fractional_topping():
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 2, "gusti": [
        {"id_pizza": 10, "quota": 1, "aggiunte": [0]}, {"id_pizza": 11, "quota": 1, "aggiunte": []}]}, *settings())
    assert result["prezzo_unitario"] == "24.50"  # (20+3+26)/2


def test_multigusto_can_have_two_thirds_one_taste_and_one_third_another():
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 3, "gusti": [
        {"id_pizza": 10, "quota": 2}, {"id_pizza": 12, "quota": 1}]}, *settings())
    assert result["prezzo_unitario"] == "20.67"


def test_addition_on_one_third_is_charged_only_one_third():
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 3, "gusti": [
        {"id_pizza": 10, "quota": 2}, {"id_pizza": 11, "quota": 1, "aggiunte": [0]}]}, *settings())
    assert result["prezzo_unitario"] == "23.00"  # 2/3 of €20 + 1/3 of (€26 + €3).


def test_multigusto_can_have_three_quarters_and_one_quarter():
    local = settings()
    local[1]["gigante"].append(4)
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 4, "gusti": [
        {"id_pizza": 10, "quota": 3}, {"id_pizza": 12, "quota": 1}]}, *local)
    assert result["prezzo_unitario"] == "20.50"


def test_multigusto_can_have_half_and_two_quarters():
    local = settings()
    local[1]["gigante"].append(4)
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 4, "gusti": [
        {"id_pizza": 10, "quota": 2}, {"id_pizza": 11, "quota": 1},
        {"id_pizza": 12, "quota": 1}]}, *local)
    assert result["prezzo_unitario"] == "22.00"


def test_multigusto_tastes_use_format_rule_even_across_categories():
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 2, "gusti": [
        {"id_pizza": 10, "quota": 1}, {"id_pizza": 12, "quota": 1}]}, *settings())
    assert result["prezzo_unitario"] == "21.00"


def test_calzone_and_panino_use_their_own_products_and_prices():
    assert quote({"tipo": "calzone_prodotto", "id_pizza": 12}, *settings())["totale"] == "9.00"
    assert quote({"tipo": "panino_prodotto", "id_pizza": 14}, *settings())["totale"] == "9.50"
    with pytest.raises(ValueError, match="non appartiene"):
        quote({"tipo": "calzone", "id_pizza": 10}, *settings())


def test_calzone_and_panino_can_mix_tastes_but_remain_one_whole_item():
    local = settings()
    local[0][13] = {"id": 13, "id_categoria": 3, "tipo_pizzeria": "calzone",
                    "formati": {"gigante": {"prezzo": "18.00", "disponibile": True, "impasti": ["Classico"]}}}
    result = quote({"tipo": "calzone_multigusto", "formato": "Gigante", "taglio": 2,
                    "gusti": [{"id_pizza": 12, "quota": 1}, {"id_pizza": 13, "quota": 1}],
                    "quantita": 1}, *local)
    assert result["quantita"] == 1
    assert result["totale"] == "20.00"


def test_calzone_cannot_mix_a_pizza_with_a_calzone_product():
    local = settings()
    local[0][13] = {"id": 13, "id_categoria": 4, "tipo_pizzeria": "calzone",
                    "formati": {"gigante": {"prezzo": "18.00", "disponibile": True, "impasti": ["Classico"]}}}
    with pytest.raises(ValueError, match="non appartiene"):
        quote({"tipo": "calzone_multigusto", "formato": "Gigante", "taglio": 2,
               "gusti": [{"id_pizza": 10, "quota": 1}, {"id_pizza": 13, "quota": 1}]}, *local)


def test_multitaste_shares_must_always_make_one_whole_item():
    with pytest.raises(ValueError):
        quote({"tipo": "panino_multigusto", "formato": "Gigante", "taglio": 2,
               "gusti": [{"id_pizza": 10, "quota": 1}]}, *settings())


@pytest.mark.parametrize("payload", [
    {"tipo": "multigusto", "formato": "Singola", "taglio": 2, "gusti": [{"id_pizza": 10, "quota": 1}, {"id_pizza": 11, "quota": 1}]},
    {"tipo": "multigusto", "formato": "Gigante", "taglio": 3, "gusti": [{"id_pizza": 10, "quota": 1}, {"id_pizza": 11, "quota": 1}]},
    {"tipo": "pizza", "id_pizza": 10, "formato": "Singola", "aggiunte": [0, 0]},
    {"tipo": "pizza", "id_pizza": 10, "formato": "Gigante", "impasto": "Integrale"},
    {"tipo": "pizza", "id_pizza": 99, "formato": "Singola"},
    {"tipo": "calzone", "id_pizza": 11},
    {"tipo": "pizza", "id_pizza": 10, "formato": "Singola", "quantita": 101},
])
def test_invalid_or_unavailable_choices_rejected(payload):
    with pytest.raises(ValueError):
        quote(payload, *settings())


def test_unknown_ingredient_cannot_be_removed():
    with pytest.raises(ValueError):
        quote({"tipo": "pizza", "id_pizza": 10, "formato": "Singola", "senza": [1]},
              *settings(), {10: ["Mozzarella"]})


def test_same_taste_can_fill_multiple_parts_with_distinct_customizations():
    result = quote({"tipo": "multigusto", "formato": "Gigante", "taglio": 2,
                    "gusti": [{"id_pizza": 10, "quota": 1, "senza": [0]},
                              {"id_pizza": 10, "quota": 1, "aggiunte": [0]}]},
                   *settings(), {10: ["Mozzarella"]})
    assert result["ingredienti_tolti_per_gusto"] == [["Mozzarella"], []]


def test_owner_quote_is_read_only_and_scoped_to_shop():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/preventivo", method="POST", json={
        "tipo": "pizza", "id_pizza": 10, "formato": "Singola", "prezzo": "0.01"}):
        session["user_id"] = 3
        response = endpoint(db)()
    assert response.get_json()["totale"] == "8.00"
    assert response.get_json()["solo_anteprima"] is True
    assert all(params == (7,) for _, params in db.cur.statements)
    assert all(query.lstrip().upper().startswith("SELECT") for query, _ in db.cur.statements)


def test_owner_quote_removes_ingredient_from_live_product_description():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/preventivo", method="POST", json={
        "tipo": "pizza", "id_pizza": 10, "formato": "Singola", "senza": [1]}):
        session["user_id"] = 3
        response = endpoint(db)()
    assert response.get_json()["totale"] == "8.00"
    assert response.get_json()["ingredienti_tolti_per_gusto"] == [["mozzarella"]]


def test_employee_cannot_quote_owner_draft():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/preventivo", method="POST", json={
        "tipo": "pizza", "id_pizza": 10, "formato": "Singola"}):
        session["user_id"] = 3; session["employee_id"] = 4
        _, status = endpoint(db)()
    assert status == 403
    assert db.cur.statements == []
