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


def availability_function(db):
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_disponibilita_ordini"))
    node.decorator_list = []
    scope = {
        "request": request, "jsonify": jsonify, "date": date, "datetime": datetime,
        "timedelta": timedelta, "ZoneInfo": ZoneInfo, "Decimal": Decimal,
        "psycopg2": SimpleNamespace(connect=lambda **kwargs: db),
        "build_db_config": lambda: {},
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_disponibilita_ordini"]


class FakeCursor:
    def __init__(self, limit=0, active=True, table_active=False, pickup_enabled=False, pickup_start=None, pickup_end=None, product_unit="pezzo", pickup_minutes=15, pickup_capacity=Decimal("0"), pickup_criterion="ordini", used_slot_orders=0, used_slot_articles=Decimal("0"), slot_rows=None):
        self.limit = limit
        self.active = active
        self.table_active = table_active
        self.pickup_enabled = pickup_enabled
        self.pickup_start = pickup_start
        self.pickup_end = pickup_end
        self.product_unit = product_unit
        self.pickup_minutes = pickup_minutes
        self.pickup_capacity = pickup_capacity
        self.pickup_criterion = pickup_criterion
        self.used_slot_orders = used_slot_orders
        self.used_slot_articles = used_slot_articles
        self.slot_rows = slot_rows or []
        self.query = ""
        self.statements = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params=None):
        self.query = query
        self.statements.append((query, params))

    def fetchone(self):
        if "FROM negozi" in self.query and "ordini_tavolo_attivi" not in self.query:
            return (7, self.active, self.limit, self.pickup_enabled, self.pickup_start, self.pickup_end, self.pickup_minutes, self.pickup_capacity, self.pickup_criterion)
        if "FROM negozi" in self.query: return (7, self.active, self.table_active, self.limit, self.pickup_enabled, self.pickup_start, self.pickup_end, self.pickup_minutes, self.pickup_capacity, self.pickup_criterion)
        if "COUNT(DISTINCT o.id)" in self.query: return (self.used_slot_orders, self.used_slot_articles)
        if "data_richiesta=%s" in self.query: return (self.limit,)
        if "telefono_cliente=%s" in self.query: return (0,)
        if "origine='tavolo'" in self.query: return (0,)
        if "RETURNING id" in self.query: return (123,)
        raise AssertionError(self.query)

    def fetchall(self):
        if "GROUP BY o.ora_richiesta" in self.query: return self.slot_rows
        if "FROM prodotti p" in self.query: return [(3, "Articolo", Decimal("4.00"), self.product_unit)]
        raise AssertionError(self.query)


class FakeConnection:
    def __init__(self, **kwargs): self.cur = FakeCursor(**kwargs)
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
    assert not any("INSERT INTO clienti_ordini_salvati" in sql for sql, _ in db.cur.statements)


def test_online_takeaway_customer_is_saved_only_with_explicit_choice():
    db = FakeConnection()
    data = payload(1)
    data["salva_cliente"] = True
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 201
    saved = next(params for sql, params in db.cur.statements if "INSERT INTO clienti_ordini_salvati" in sql)
    assert saved == (7, "Mario Rossi", "+39 333 1234567", "393331234567", "")


def test_online_customer_email_is_saved_for_search_when_opted_in():
    db = FakeConnection()
    data = payload(1)
    data.update({"email": " Mario@Example.it ", "salva_cliente": True})
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 201
    saved = next(params for sql, params in db.cur.statements if "INSERT INTO clienti_ordini_salvati" in sql)
    assert saved[-1] == "mario@example.it"
    order = next(params for sql, params in db.cur.statements if "INSERT INTO ordini_menu" in sql)
    assert order[-1] == "mario@example.it"


def test_google_email_is_saved_only_after_takeaway_customer_opts_in():
    db = FakeConnection()
    data = payload(1)
    data["salva_cliente"] = True
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        session["customer_google"] = {"sub": "google-1", "email": "google@example.it"}
        response, status = order_function(db)("esempio")
    assert status == 201
    saved = next(params for sql, params in db.cur.statements if "INSERT INTO clienti_ordini_salvati" in sql)
    assert saved[-1] == "google@example.it"


def test_saved_customer_search_matches_email_and_returns_it():
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_ordini_clienti"))
    node.decorator_list = []

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params):
            assert "email ILIKE %s" in sql
            assert params[-1] == "%mario@example.it%"
        def fetchall(self): return [(2, "Mario", "3331234567", "mario@example.it")]

    db = SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None)
    scope = {"request": request, "session": session, "jsonify": jsonify,
             "get_user_shop_id": lambda user_id: 7,
             "psycopg2": SimpleNamespace(connect=lambda **kwargs: db), "build_db_config": lambda: {}}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    with FLASK.test_request_context("/api/ordini/clienti?q=mario%40example.it"):
        session["user_id"] = 11
        result = scope["api_ordini_clienti"]().get_json()
    assert result["clienti"][0]["email"] == "mario@example.it"


def test_online_customer_choice_must_be_boolean():
    db = FakeConnection()
    data = payload(1)
    data["salva_cliente"] = "true"
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400
    assert not db.cur.statements


def test_public_takeaway_form_offers_optional_address_book_choice():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert 'id="orderSaveCustomerField" data-takeaway-field hidden' in html
    assert '<input name="salva_cliente" type="checkbox">' in html
    assert "salva_cliente:orderMode==='asporto'&&fields.has('salva_cliente')" in html
    assert 'name="email" type="email"' in html


def test_kilogram_product_uses_weight_and_labels_order_line():
    db = FakeConnection(product_unit="kg")
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=payload("1,5")):
        response, status = order_function(db)("esempio")
    assert status == 201
    assert response.get_json()["totale"] == "6.00"
    line = next(params for sql, params in db.cur.statements if "INSERT INTO righe_ordini_menu" in sql)
    assert line[2] == "Articolo (kg)"
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
    assert not any("INSERT INTO clienti_ordini_salvati" in sql for sql, _ in db.cur.statements)


def test_owner_can_explicitly_save_customer_for_future_orders():
    db = FakeConnection(active=False)
    data = payload(1)
    data["salva_cliente"] = True
    with FLASK.test_request_context("/api/ordini/manuale", method="POST", json=data):
        session["user_id"] = 11
        response, status = order_function(db)()
    assert status == 201
    saved = next(params for sql, params in db.cur.statements if "INSERT INTO clienti_ordini_salvati" in sql)
    assert saved == (7, "Mario Rossi", "+39 333 1234567", "393331234567", "")


def test_manual_customer_save_requires_explicit_boolean():
    db = FakeConnection()
    data = payload(1)
    data["salva_cliente"] = "true"
    with FLASK.test_request_context("/api/ordini/manuale", method="POST", json=data):
        session["user_id"] = 11
        response, status = order_function(db)()
    assert status == 400
    assert not db.cur.statements


def test_order_time_must_be_on_five_minute_boundary():
    data = payload(1)
    data["ora_richiesta"] = "12:07"
    db = FakeConnection()
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400
    assert "5 minuti" in response.get_json()["error"]
    assert not db.cur.statements


def test_valid_order_time_is_saved():
    data = payload(1)
    data["data_richiesta"] = (datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)).isoformat()
    data["ora_richiesta"] = "12:15"
    db = FakeConnection(pickup_enabled=True, pickup_start="12:00", pickup_end="13:00")
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 201
    order = next(params for sql, params in db.cur.statements if "INSERT INTO ordini_menu" in sql)
    assert order[7] == "12:15"


def test_twenty_minute_slots_accept_only_configured_start_times():
    data = payload(1)
    data["data_richiesta"] = (datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)).isoformat()
    data["ora_richiesta"] = "12:20"
    db = FakeConnection(pickup_enabled=True, pickup_start="12:00", pickup_end="13:00", pickup_minutes=20)
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 201
    data["ora_richiesta"] = "12:15"
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400


def test_slot_order_limit_rejects_full_slot():
    data = payload(1)
    data["data_richiesta"] = (datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)).isoformat()
    data["ora_richiesta"] = "12:00"
    db = FakeConnection(pickup_enabled=True, pickup_start="12:00", pickup_end="13:00", pickup_capacity=Decimal("2"), used_slot_orders=2)
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 409
    assert "capienza" in response.get_json()["error"]


def test_slot_article_limit_counts_decimal_quantities():
    data = payload("1,5")
    data["data_richiesta"] = (datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)).isoformat()
    data["ora_richiesta"] = "12:00"
    db = FakeConnection(pickup_enabled=True, pickup_start="12:00", pickup_end="13:00", pickup_capacity=Decimal("3"), pickup_criterion="articoli", used_slot_articles=Decimal("2"))
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 409


def test_availability_returns_only_slots_with_capacity_for_cart():
    tomorrow = (datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)).isoformat()
    db = FakeConnection(pickup_enabled=True, pickup_start="12:00", pickup_end="13:00", pickup_minutes=20,
                        pickup_capacity=Decimal("3"), pickup_criterion="articoli",
                        slot_rows=[("12:00", 1, Decimal("2")), ("12:20", 1, Decimal("1"))])
    with FLASK.test_request_context("/api/menu/esempio/ordini/disponibilita?data=" + tomorrow + "&articoli=1.5"):
        response = availability_function(db)("esempio")
    assert response.get_json()["fasce"] == ["12:20", "12:40"]
    assert response.get_json()["minuti_fascia_ritiro"] == 20


def test_date_only_shop_rejects_time():
    data = payload(1)
    data["ora_richiesta"] = "12:15"
    db = FakeConnection()
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400
    assert "solo il giorno" in response.get_json()["error"]


def test_time_slot_shop_requires_time_and_rejects_end_boundary():
    db = FakeConnection(pickup_enabled=True, pickup_start="12:00", pickup_end="13:00")
    data = payload(1)
    data["data_richiesta"] = (datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)).isoformat()
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400
    data["ora_richiesta"] = "13:00"
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400


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
    assert not any("INSERT INTO clienti_ordini_salvati" in sql for sql, _ in db.cur.statements)


def test_table_order_cannot_save_customer_to_address_book():
    db = FakeConnection(table_active=True)
    data = {"modalita": "tavolo", "riferimento": "4", "salva_cliente": True, "prodotti": [{"id": 3, "quantita": 1}]}
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400
    assert not db.cur.statements


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


def test_fulfillment_api_returns_shop_scoped_open_and_completed_orders():
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_ordini_evasione"))
    node.decorator_list = []
    day = date.today()

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params):
            assert "o.id_negozio=%s" in sql
            assert "o.stato IN ('da_evadere','in_lavorazione','evaso')" in sql
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


def test_fulfillment_page_always_shows_product_totals_and_completed_orders():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert 'id="articleCount"' in html
    assert 'id="totalCount"' in html
    assert 'id="pendingCount"' in html
    assert 'id="doneCount"' in html
    assert "order.stato==='evaso'?' done'" in html
    assert 'id="orderHistory"' in html
    assert "historyPanel.showModal()" in html
    assert "load({silent:true})},5000" in html


def test_fulfillment_page_keeps_separate_product_and_order_views():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert 'id="summaryView" type="button" aria-pressed="true"' in html
    assert 'id="ordersView" type="button" aria-pressed="false"' in html
    assert "let lastOrders=[],displayMode='totali'" in html
    assert "prep.hidden=displayMode!=='totali'" in html
    assert "grid.hidden=displayMode!=='ordini'" in html
    assert "['Evasi',amounts.done,'evaded']" in html
    assert "['Da preparare',amounts.pending,'remaining']" in html
    assert "['Totale',amounts.total,'all']" in html


def test_fulfillment_product_section_follows_date_and_add_order_floats():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert html.index('id="date"') < html.index('id="groups"') < html.index('class="metrics"')
    assert 'position:fixed;right:max(20px,env(safe-area-inset-right));bottom:max(20px,env(safe-area-inset-bottom))' in html
    assert 'id="addOrderButton" class="add-order-button"' in html


def test_order_operations_are_on_fulfillment_page_not_settings():
    root = Path(__file__).resolve().parents[1] / "templates"
    settings = (root / "sections" / "ordini.html").read_text(encoding="utf-8")
    fulfillment = (root / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    for old_control in ('id="manualOrderForm"', 'id="ordersPeriod"', 'id="ordersSummary"', 'id="ordersList"'):
        assert old_control not in settings
    assert 'id="addOrderButton"' in fulfillment
    assert 'id="manualOrderForm"' in fulfillment
    assert 'id="historyPeriod"' in fulfillment


def test_order_forms_pair_reference_and_notes_and_manual_products_are_searchable():
    root = Path(__file__).resolve().parents[1] / "templates"
    fulfillment = (root / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    public = (root / "public_menu.html").read_text(encoding="utf-8")
    assert 'class="manual-extra-fields"' in fulfillment
    assert 'class="order-details" id="orderDetails"' in public
    assert "search.type='search'" in fulfillment
    assert "node(productPicker,'div',undefined,'product-results')" in fulfillment
    assert "manualProducts.filter(product=>" in fulfillment
    assert "row.dataset.productId=String(product.id)" in fulfillment
    assert "id:Number(row.dataset.productId)" in fulfillment
    assert "datalist" not in fulfillment
    assert "Scegli ogni articolo dai suggerimenti" in fulfillment
    assert '<option value="anno">Anno</option>' in fulfillment


def test_add_order_opens_a_dialog_without_inline_manual_panel():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert '<dialog class="manual-order" id="manualOrderPanel"' in html
    assert 'manualPanel.showModal()' in html
    assert 'manualPanel.close()' in html
    assert 'Nuovo ordine manuale' not in html
