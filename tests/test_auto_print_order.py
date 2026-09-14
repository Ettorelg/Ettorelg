import ast
import copy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, session


TREE = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8"))
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.params = None

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, _, params): self.params = params
    def fetchall(self): return self.rows


class Connection:
    def __init__(self, rows): self.cur = Cursor(rows)
    def cursor(self): return self.cur
    def close(self): pass


def endpoint(db):
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef)
                              and item.name == "api_ordine_per_stampa"))
    node.decorator_list = []
    scope = {"order_notification_shop_id": lambda: session.get("shop_id"), "jsonify": jsonify,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_ordine_per_stampa"]


def test_print_order_is_scoped_to_current_shop():
    db = Connection([(8, date(2026, 9, 14), "20:00", "Mario", "333", "7", "", 20,
                      "tavolo", "14/09/2026 19:00", "Pizza", 2, 20)])
    with FLASK.test_request_context("/api/ordini/8/stampa"):
        session["shop_id"] = 7
        result = endpoint(db)(8)
    assert db.cur.params == (7, 8)
    assert result.json["ordine"]["prodotti"][0]["nome"] == "Pizza"


def test_print_order_rejects_anonymous_access():
    db = Connection([])
    with FLASK.test_request_context("/api/ordini/8/stampa"):
        _, status = endpoint(db)(8)
    assert status == 403
    assert db.cur.params is None
