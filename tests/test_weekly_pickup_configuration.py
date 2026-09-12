"""The weekly pickup settings must not write empty strings to TIME columns."""
import ast
import copy
import json
import re
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class FakeCursor:
    def __init__(self):
        self.statements = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params): self.statements.append((query, params))
    def fetchone(self):
        return (True, False, 0, True, None, None, 15, Decimal("0"), "ordini", [[{"dalle": "12:00", "alle": "14:00"}]] + [[] for _ in range(6)])


class FakeConnection:
    def __init__(self): self.cur = FakeCursor()
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def config_endpoint(db):
    scope = {"request": request, "session": session, "jsonify": jsonify,
             "Decimal": Decimal, "re": re, "json": json,
             "psycopg2": SimpleNamespace(connect=lambda **kwargs: db),
             "build_db_config": lambda: {}, "get_user_shop_id": lambda _: 7}
    nodes = [copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == name))
             for name in ("validate_pickup_schedule", "api_ordini_configurazione")]
    for node in nodes: node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_ordini_configurazione"]


def test_weekly_windows_save_null_legacy_time_columns():
    db = FakeConnection()
    schedule = [[{"dalle": "12:00", "alle": "14:00"}]] + [[] for _ in range(6)]
    payload = {"fasce_ritiro_attive": True, "fasce_settimanali": schedule,
               "minuti_fascia_ritiro": 15, "limite_fascia_ritiro": 0,
               "criterio_limite_fascia": "ordini"}
    with FLASK.test_request_context("/api/ordini/configurazione", method="PUT", json=payload):
        session["user_id"] = 3
        response = config_endpoint(db)()
    assert response.get_json()["fasce_settimanali"] == schedule
    params = next(params for query, params in db.cur.statements if "fasce_ritiro_settimanali=COALESCE" in query)
    assert params[1:3] == (None, None)
    assert json.loads(params[6]) == schedule
