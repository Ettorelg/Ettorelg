"""Draft delivery settings are tenant-scoped and cannot enable customer orders."""
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
        self.statements = []
        self.row = None

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params):
        self.statements.append((query, params))
        if "INSERT INTO pizzeria_delivery_config" in query:
            self.row = (False, params[1], Decimal(params[2]), Decimal(params[3]), json.loads(params[4]))
    def fetchone(self): return self.row


class Connection:
    def __init__(self): self.cur = Cursor()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def functions(db):
    nodes = [copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef)
                                and item.name == name))
             for name in ("normalize_delivery_config", "api_pizzeria_delivery_configurazione")]
    for node in nodes: node.decorator_list = []
    scope = {"Decimal": Decimal, "json": json, "request": request, "session": session,
             "jsonify": jsonify, "get_user_shop_id": lambda user_id: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope


def valid_payload():
    return {"tempo_preparazione_minuti": 20, "minuti_per_km": "4,5",
            "raggio_massimo_km": "8", "zone": [
                {"nome": "Vicino", "fino_km": "3", "costo_consegna": "2,50", "ordine_minimo": "10"},
                {"nome": "Lontano", "fino_km": "8", "costo_consegna": "4", "ordine_minimo": "20"}]}


def test_delivery_draft_saves_zones_cost_and_minimum_without_activation():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/delivery/configurazione", method="PUT", json=valid_payload()):
        session["user_id"] = 3
        response = functions(db)["api_pizzeria_delivery_configurazione"]()
    data = response.get_json()
    assert data["attivo"] is False
    assert data["zone"][0]["costo_consegna"] == "2.50"
    assert data["zone"][1]["ordine_minimo"] == "20.00"
    insert = next(params for query, params in db.cur.statements if "INSERT INTO pizzeria_delivery_config" in query)
    assert insert[0] == 7
    assert "attivo" not in next(query for query, _ in db.cur.statements if "INSERT INTO pizzeria_delivery_config" in query)


@pytest.mark.parametrize("change", [
    {"attivo": True},
    {"zone": [{"nome": "Oltre raggio", "fino_km": 9, "costo_consegna": 2, "ordine_minimo": 10}]},
    {"zone": [{"nome": "A", "fino_km": 5, "costo_consegna": 2, "ordine_minimo": 10},
              {"nome": "B", "fino_km": 3, "costo_consegna": 2, "ordine_minimo": 10}]},
    {"minuti_per_km": -1},
])
def test_delivery_draft_rejects_unsafe_configuration(change):
    db = Connection()
    payload = valid_payload() | change
    with FLASK.test_request_context("/api/pizzeria/delivery/configurazione", method="PUT", json=payload):
        session["user_id"] = 3
        _, status = functions(db)["api_pizzeria_delivery_configurazione"]()
    assert status == 400
    assert not db.cur.statements


def test_delivery_draft_requires_owner_account():
    db = Connection()
    with FLASK.test_request_context("/api/pizzeria/delivery/configurazione"):
        _, status = functions(db)["api_pizzeria_delivery_configurazione"]()
    assert status == 403
    assert not db.cur.statements
