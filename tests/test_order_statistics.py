"""Statistiche ordini: visibilità e intervalli senza database esterno."""
import ast
import copy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, session


SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class Cursor:
    def __init__(self, active=True):
        self.active = active
        self.query = ""
        self.params = None
        self.statements = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params=None):
        self.query, self.params = query, params
        self.statements.append((query, params))

    def fetchone(self):
        if "FROM negozi" in self.query: return (self.active, False)
        if "COUNT(*) FILTER" in self.query:
            return (4, 1, 1, 1, 1, 1, 2, Decimal("24.50"))
        raise AssertionError(self.query)

    def fetchall(self):
        if "FROM righe_ordini_menu" in self.query: return [("Pasta (kg)", Decimal("2.5"), 2)]
        if "GROUP BY o.ora_richiesta" in self.query: return [("12:20", 2)]
        if "REGEXP_REPLACE" in self.query: return [("Mario Rossi", 2, Decimal("15.00"))]
        if "DATE_TRUNC" in self.query or "GROUP BY COALESCE" in self.query: return [("2026-09-01", 3)]
        raise AssertionError(self.query)


class Connection:
    def __init__(self, active=True): self.cur = Cursor(active)
    def cursor(self): return self.cur
    def close(self): pass


def statistics_function(db):
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_statistiche_ordini"))
    node.decorator_list = []
    scope = {
        "request": request, "session": session, "jsonify": jsonify,
        "date": date, "datetime": datetime, "timedelta": timedelta, "ZoneInfo": ZoneInfo,
        "psycopg2": SimpleNamespace(connect=lambda **kwargs: db), "build_db_config": lambda: {},
        "get_user_shop_id": lambda user_id: 7, "get_user_license_plan": lambda user_id: "professional",
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_statistiche_ordini"]


def test_order_statistics_are_hidden_when_modules_are_disabled():
    db = Connection(active=False)
    with FLASK.test_request_context("/api/statistiche/ordini?periodo=mese&data=2026-09-12"):
        session["user_id"] = 11
        response, status = statistics_function(db)()
    assert status == 403
    assert len(db.cur.statements) == 1


def test_order_statistics_include_products_slots_customers_and_month_range():
    db = Connection()
    with FLASK.test_request_context("/api/statistiche/ordini?periodo=mese&data=2026-09-12"):
        session["user_id"] = 11
        response = statistics_function(db)()
    result = response.get_json()
    assert result["da"] == "2026-09-01" and result["a"] == "2026-09-30"
    assert result["prodotti"][0]["quantita"] == "2.5"
    assert result["fasce"][0]["fascia"] == "12:20"
    assert result["clienti"][0]["ordini"] == 2
    assert result["valore_richieste"] == "24.50"
    assert all(params[0] == 7 for _, params in db.cur.statements)


def test_order_statistics_year_uses_monthly_trend():
    db = Connection()
    with FLASK.test_request_context("/api/statistiche/ordini?periodo=anno&data=2026-09-12"):
        session["user_id"] = 11
        response = statistics_function(db)()
    assert response.get_json()["da"] == "2026-01-01"
    assert response.get_json()["a"] == "2026-12-31"
    assert any("DATE_TRUNC('month'" in sql for sql, _ in db.cur.statements)


def test_order_statistics_tab_is_conditional_in_page():
    html = (Path(__file__).resolve().parents[1] / "templates" / "sections" / "statistiche.html").read_text(encoding="utf-8")
    assert 'id="statsTabs"' in html and 'id="statsOrdersTab"' in html
    assert "if(!data.moduli_ordini_attivi)return" in html
