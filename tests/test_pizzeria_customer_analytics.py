import ast
import copy
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, session


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "templates" / "dashboard_user.html").read_text(encoding="utf-8")
STATS = (ROOT / "templates" / "sections" / "statistiche.html").read_text(encoding="utf-8")
CUSTOMERS = (ROOT / "templates" / "sections" / "clienti.html").read_text(encoding="utf-8")
TREE = ast.parse(APP)
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


def test_pizzeria_analytics_are_scoped_and_exclude_cancelled_orders():
    endpoint = APP[APP.index('def api_statistiche_pizzeria'):APP.index('def api_clienti_statistiche')]
    assert "o.id_negozio=%s" in endpoint
    assert 'status == "annullato"' in endpoint
    assert 'status == "evaso"' in endpoint
    for metric in ("prodotti", "formati", "varianti", "rimozioni", "impasti", "tipi", "fasce", "andamento"):
        assert f'"{metric}"' in endpoint


def test_customer_statistics_use_only_saved_shop_customers():
    endpoint = APP[APP.index('def api_clienti_statistiche'):APP.index('def normalize_delivery_config')]
    assert "FROM clienti_ordini_salvati c" in endpoint
    assert "c.id_negozio=%s" in endpoint
    assert "LEFT JOIN favorites" in endpoint
    assert "LIMIT 500" in endpoint


def test_dashboard_exposes_pizzeria_statistics_and_customer_section():
    assert 'data-section="clienti"' in DASHBOARD
    assert 'id="statsPizzaTab"' in STATS
    assert "/api/statistiche/pizzeria" in STATS
    assert "/api/clienti/statistiche" in CUSTOMERS


def test_pizzeria_endpoint_weights_fractional_tastes_and_separates_revenue():
    class Cursor:
        query = ""
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, query, params): self.query = query; assert params[0] == 7
        def fetchone(self): return (True,)
        def fetchall(self):
            return [
                (1, "evaso", "asporto", date(2026, 9, 15), "20:00", Decimal("1"), Decimal("24"),
                 {"tipo": "multigusto", "_stampa": {"tipo": "Pizza multigusto", "formato": "Familiare", "impasto": "Classico", "gusti": [
                     {"nome": "Margherita", "quota": "1/2", "aggiunte": ["Prosciutto"], "senza": []},
                     {"nome": "Rianata", "quota": "1/2", "aggiunte": [], "senza": ["Aglio"]}]}}),
                (2, "da_evadere", "asporto", date(2026, 9, 15), "21:00", Decimal("1"), Decimal("10"),
                 {"tipo": "pizza", "_stampa": {"tipo": "Pizza", "formato": "Singola", "impasto": "Integrale", "gusti": [{"nome": "Margherita", "aggiunte": [], "senza": []}]}}),
            ]
    db = SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None)
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_statistiche_pizzeria"));node.decorator_list=[]
    scope = {"request": request, "session": session, "jsonify": jsonify, "date": date, "datetime": datetime,
             "timedelta": timedelta, "ZoneInfo": ZoneInfo, "Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP,
             "get_user_license_plan": lambda _: "professional", "get_user_shop_id": lambda _: 7,
             "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    with FLASK.test_request_context("/api/statistiche/pizzeria?periodo=giorno&data=2026-09-15"):
        session["user_id"] = 3
        data = scope["api_statistiche_pizzeria"]().get_json()
    assert data["incassi_evasi"] == "24.00" and data["valore_richieste"] == "34.00"
    assert data["prodotti"][0] == {"nome": "Margherita", "quantita": "1.5"}
    assert data["varianti"] == [{"nome": "Prosciutto", "quantita": "0.5"}]


def test_customer_endpoint_returns_saved_customer_metrics():
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, query, params): assert params[:2] == (7, 7)
        def fetchall(self): return [(5, "Mario", "+39123", "mario@example.it", 3, 2, 1, Decimal("45"), Decimal("15"), "15/09/2026 20:00", "Margherita", Decimal("2"), "15/09/2026 20:05")]
    db = SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None)
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_clienti_statistiche"));node.decorator_list=[]
    scope = {"request": request, "session": session, "jsonify": jsonify, "Decimal": Decimal,
             "get_user_shop_id": lambda _: 7, "psycopg2": SimpleNamespace(connect=lambda **_: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    with FLASK.test_request_context("/api/clienti/statistiche"):
        session["user_id"] = 3
        data = scope["api_clienti_statistiche"]().get_json()
    assert data["totale"] == 1 and data["valore_totale"] == "45"
    assert data["clienti"][0]["prodotto_preferito"] == "Margherita"
