"""A shop employee may complete only an order belonging to their shop."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, jsonify, request, session


SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self, found=True):
        self.found = found
        self.params = None

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, _query, params): self.params = params
    def fetchone(self): return (123,) if self.found else None


class Connection:
    def __init__(self, found=True): self.cur = Cursor(found)
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def endpoint(db):
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_aggiorna_ordine"))
    node.decorator_list = []
    scope = {
        "request": request, "session": session, "jsonify": jsonify,
        "psycopg2": SimpleNamespace(connect=lambda **kwargs: db),
        "build_db_config": lambda: {}, "get_user_shop_id": lambda user_id: 5,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_aggiorna_ordine"]


def test_employee_can_mark_order_evaso_in_own_shop():
    db = Connection()
    with FLASK.test_request_context("/api/ordini/123", method="PATCH", json={"stato": "evaso"}):
        session.update(employee_id=8, employee_shop_id=7)
        response = endpoint(db)(123)
    assert response.get_json() == {"ok": True, "stato": "evaso"}
    assert db.cur.params == ("evaso", 123, 7, True)


def test_employee_cannot_cancel_or_reopen_order():
    for status in ("annullato", "da_evadere", "in_lavorazione"):
        db = Connection()
        with FLASK.test_request_context("/api/ordini/123", method="PATCH", json={"stato": status}):
            session.update(employee_id=8, employee_shop_id=7)
            _response, code = endpoint(db)(123)
        assert code == 403
        assert db.cur.params is None


def test_employee_cannot_complete_order_outside_own_shop():
    db = Connection(found=False)
    with FLASK.test_request_context("/api/ordini/123", method="PATCH", json={"stato": "evaso"}):
        session.update(employee_id=8, employee_shop_id=7)
        _response, code = endpoint(db)(123)
    assert code == 404
    assert db.cur.params[2] == 7
