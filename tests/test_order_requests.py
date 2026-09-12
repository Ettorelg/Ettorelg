"""Regole del nuovo ordine senza richiedere un database esterno."""
import ast
import copy
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, session, redirect


SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


def order_function(db):
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_crea_ordine_menu"))
    node.decorator_list = []
    scope = {
        "request": request, "session": session, "jsonify": jsonify,
        "date": date, "datetime": datetime, "timedelta": timedelta, "ZoneInfo": ZoneInfo,
        "Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP, "re": re,
        "psycopg2": SimpleNamespace(connect=lambda **kwargs: db),
        "build_db_config": lambda: {},
        "get_user_shop_id": lambda user_id: 7,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_crea_ordine_menu"]


class FakeCursor:
    def __init__(self, limit=0, active=True, table_active=False):
        self.limit = limit
        self.active = active
        self.table_active = table_active
        self.query = ""
        self.statements = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params=None):
        self.query = query
        self.statements.append((query, params))

    def fetchone(self):
        if "FROM negozi" in self.query: return (7, self.active, self.table_active, self.limit)
        if "data_richiesta=%s" in self.query: return (self.limit,)
        if "telefono_cliente=%s" in self.query: return (0,)
        if "origine='tavolo'" in self.query: return (0,)
        if "RETURNING id" in self.query: return (123,)
        raise AssertionError(self.query)

    def fetchall(self):
        if "FROM prodotti p" in self.query: return [(3, "Articolo", Decimal("4.00"))]
        raise AssertionError(self.query)


class FakeConnection:
    def __init__(self, limit=0, active=True, table_active=False): self.cur = FakeCursor(limit, active, table_active)
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def cursor(self): return self.cur
    def close(self): pass


def payload(quantity):
    return {
        "nome": "Mario Rossi", "telefono": "+39 333 1234567",
        "data_richiesta": datetime.now(ZoneInfo("Europe/Rome")).date().isoformat(),
        "prodotti": [{"id": 3, "quantita": quantity}],
    }


def test_decimal_quantity_is_saved_with_exact_total():
    db = FakeConnection()
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=payload("1,5")):
        response, status = order_function(db)("esempio")
    assert status == 201
    assert response.get_json()["totale"] == "6.00"
    line = next(params for sql, params in db.cur.statements if "INSERT INTO righe_ordini_menu" in sql)
    assert line[3] == Decimal("1.5")


def test_full_day_is_rejected_before_product_lookup():
    db = FakeConnection(limit=2)
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=payload(1)):
        response, status = order_function(db)("esempio")
    assert status == 409
    assert "completa" in response.get_json()["error"]
    assert not any("FROM prodotti p" in sql for sql, _ in db.cur.statements)


def test_owner_can_enter_order_when_online_orders_are_disabled():
    db = FakeConnection(active=False)
    with FLASK.test_request_context("/api/ordini/manuale", method="POST", json=payload("1.5")):
        session["user_id"] = 11
        response, status = order_function(db)()
    assert status == 201
    order = next(params for sql, params in db.cur.statements if "INSERT INTO ordini_menu" in sql)
    assert order[8] == "titolare"


def test_order_time_must_be_on_fifteen_minute_boundary():
    data = payload(1)
    data["ora_richiesta"] = "12:07"
    db = FakeConnection()
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400
    assert "15 minuti" in response.get_json()["error"]
    assert not db.cur.statements


def test_valid_order_time_is_saved():
    data = payload(1)
    data["ora_richiesta"] = "12:15"
    db = FakeConnection()
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 201
    order = next(params for sql, params in db.cur.statements if "INSERT INTO ordini_menu" in sql)
    assert order[7] == "12:15"


def test_table_order_requires_only_table_reference_and_does_not_use_takeaway_capacity():
    db = FakeConnection(limit=2, active=False, table_active=True)
    data = {"modalita": "tavolo", "riferimento": "4", "prodotti": [{"id": 3, "quantita": 1}]}
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 201
    order = next(params for sql, params in db.cur.statements if "INSERT INTO ordini_menu" in sql)
    assert order[1] == "Tavolo 4"
    assert order[2] == ""
    assert order[8] == "tavolo"
    assert not any("data_richiesta=%s" in sql for sql, _ in db.cur.statements)


def test_table_order_is_rejected_when_table_mode_is_disabled():
    db = FakeConnection(active=True, table_active=False)
    data = {"modalita": "tavolo", "riferimento": "4", "prodotti": [{"id": 3, "quantita": 1}]}
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 403
    assert "non è disponibile" in response.get_json()["error"]
    assert not any("INSERT INTO ordini_menu" in sql for sql, _ in db.cur.statements)


def test_google_customer_login_does_not_create_owner_account():
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "auth_google_callback"))
    node.decorator_list = []
    profile = {"sub": "google-customer-1", "email": "cliente@example.it", "email_verified": True, "name": "Cliente Test"}
    scope = {
        "google_enabled": lambda: True,
        "google": SimpleNamespace(authorize_access_token=lambda: {"userinfo": profile}),
        "session": session, "redirect": redirect,
        "url_for": lambda endpoint, **kwargs: "/menu/locale" if endpoint == "public_menu" else "/login",
        "psycopg2": SimpleNamespace(connect=lambda **kwargs: (_ for _ in ()).throw(AssertionError("owner database accessed"))),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    with FLASK.test_request_context("/auth/google/callback"):
        session["customer_order_slug"] = "locale"
        response = scope["auth_google_callback"]()
        assert response.location == "/menu/locale#orderPanel"
        assert session["customer_google"]["email"] == "cliente@example.it"


def test_fulfillment_api_returns_only_shop_scoped_open_orders():
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_ordini_evasione"))
    node.decorator_list = []
    day = date.today()

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params):
            assert "o.id_negozio=%s" in sql
            assert "o.stato IN ('da_evadere','in_lavorazione')" in sql
            assert params[0] == 7
        def fetchall(self):
            return [(19, day, "12:15", "Mario Rossi", "+39123456", "", "", "da_evadere", Decimal("6.00"), "cliente", "12/09/2026 09:00", "Articolo", Decimal("1.5"), Decimal("6.00"))]

    db = SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None)
    scope = {
        "request": request, "session": session, "jsonify": jsonify,
        "date": date, "timedelta": timedelta,
        "get_user_shop_id": lambda user_id: 7,
        "psycopg2": SimpleNamespace(connect=lambda **kwargs: db),
        "build_db_config": lambda: {},
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    with FLASK.test_request_context("/api/ordini/evasione?periodo=giorno&data=" + day.isoformat()):
        session["user_id"] = 11
        result = scope["api_ordini_evasione"]().get_json()
    assert result["ordini"][0]["ora_richiesta"] == "12:15"
    assert result["ordini"][0]["prodotti"][0]["quantita"] == "1.5"


def test_fulfillment_page_defaults_to_product_totals_and_can_switch_to_orders():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert 'id="summaryView" type="button" aria-pressed="true"' in html
    assert 'id="ordersView" type="button" aria-pressed="false"' in html
    assert "let displayMode='totali'" in html
    assert "selectDisplayMode('ordini')" in html
