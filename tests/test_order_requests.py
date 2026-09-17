"""Regole del nuovo ordine senza richiedere un database esterno."""
import ast
import copy
import hashlib
import json
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
        "json": json, "hashlib": hashlib,
    }
    helper = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "pickup_windows_for_day"))
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "app.py", "exec"), scope)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_crea_ordine_menu"]


def availability_function(db):
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "api_disponibilita_ordini"))
    node.decorator_list = []
    scope = {
        "request": request, "session": session, "get_user_shop_id": lambda _: 7, "jsonify": jsonify, "date": date, "datetime": datetime,
        "timedelta": timedelta, "ZoneInfo": ZoneInfo, "Decimal": Decimal,
        "psycopg2": SimpleNamespace(connect=lambda **kwargs: db),
        "build_db_config": lambda: {},
        "json": json,
    }
    helper = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "pickup_windows_for_day"))
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "app.py", "exec"), scope)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    return scope["api_disponibilita_ordini"]


class FakeCursor:
    def __init__(self, limit=0, active=True, table_active=False, pickup_enabled=False, pickup_start=None, pickup_end=None, product_unit="pezzo", pickup_minutes=15, pickup_capacity=Decimal("0"), pickup_criterion="ordini", used_slot_orders=0, used_slot_articles=Decimal("0"), slot_rows=None, weekly_schedule=None):
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
        self.weekly_schedule = weekly_schedule
        self.query = ""
        self.statements = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, query, params=None):
        self.query = query
        self.statements.append((query, params))

    def fetchone(self):
        if "FROM negozi" in self.query and "ordini_tavolo_attivi" not in self.query:
            return (7, self.active, self.limit, self.pickup_enabled, self.pickup_start, self.pickup_end, self.pickup_minutes, self.pickup_capacity, self.pickup_criterion, self.weekly_schedule)
        if "FROM negozi" in self.query: return (7, self.active, self.table_active, self.limit, self.pickup_enabled, self.pickup_start, self.pickup_end, self.pickup_minutes, self.pickup_capacity, self.pickup_criterion, self.weekly_schedule)
        if "COUNT(DISTINCT o.id)" in self.query: return (self.used_slot_orders, self.used_slot_articles)
        if "data_richiesta=%s" in self.query: return (self.limit,)
        if "telefono_cliente=%s" in self.query: return (0,)
        if "origine='tavolo'" in self.query: return (0,)
        if "RETURNING ultimo_numero" in self.query: return (1,)
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
    assert order[-2] == "mario@example.it"
    assert order[-1] == 1


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
    assert '<input name="salva_cliente" type="checkbox" checked>' in html
    assert "salva_cliente:orderMode==='asporto'&&fields.has('salva_cliente')" in html
    assert 'name="email" type="email"' in html


def test_public_menu_keeps_order_form_closed_until_products_are_selected():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert 'id="orderTakeawayStart" type="button">🛍️ Prenota un ordine' in html
    assert '<dialog class="order-panel" id="orderPanel"' in html
    assert '<h2 id="orderModeTitle">Ordine <span' in html
    assert 'id="orderCartPreviewLines"' in html
    assert 'id="orderCartPreviewTotal"' in html
    assert 'id="orderCartOpen" type="button">Prenota un ordine' in html
    assert "if(orderCart.size&&!orderPanel.open){showOrderStep('review');orderPanel.showModal()" in html
    assert 'id="orderForm" hidden' in html
    assert 'id="orderContinue"' in html
    assert "if(orderMode==='view')return" in html
    assert "orderAddButtons.forEach(button=>button.hidden=mode==='view')" in html
    assert "openMenu({% if shop.ordini_attivi %}'asporto'{% elif shop.ordini_tavolo_attivi %}'tavolo'{% else %}'view'{% endif %})" in html
    assert "if(location.hash==='#orderPanel' && orderForm){openMenu('asporto');restoreOrderDraft();}" in html
    assert "#orderFeedback.is-error" in html
    assert "orderFeedback.scrollIntoView({behavior:'smooth',block:'center'})" in html
    assert "button.classList.toggle('selected',Boolean(selected))" in html
    assert "orderPanel.hidden=mode==='view'" not in html


def test_public_order_confirmation_is_a_dialog_and_home_is_always_available():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert 'id="menuHome"' in html
    assert "hideMenu.click();" in html
    assert '<dialog class="order-success" id="orderSuccess"' in html
    assert "orderPanel.close();" in html
    assert "orderSuccess.showModal();" in html
    assert "orderFeedback.scrollIntoView({behavior:'smooth',block:'center'})" in html
    assert "ricevuto dal locale. Nessun pagamento effettuato online." in html
    assert "potrà contattarti per confermarla" not in html
    assert html.index('id="orderPickupField"') < html.index('id="orderGoogleInfo"') < html.index('name="nome"')


def test_product_detail_price_is_below_content_and_right_aligned_on_mobile():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert ".product-dialog .product,.product-dialog .product.with-image{display:flex;flex-direction:column;align-items:stretch}" in html
    assert ".product-dialog .product .price{align-self:flex-end;order:2" in html
    assert '{{ ui.book }}' not in html


def test_public_menu_view_mode_is_read_only_and_takeaway_mode_allows_ordering():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert "showMenu.addEventListener('click', () => openMenu({% if shop.ordini_attivi %}'asporto'{% elif shop.ordini_tavolo_attivi %}'tavolo'{% else %}'view'{% endif %}))" in html
    assert "orderTakeawayStart?.addEventListener('click', () => openMenu('asporto'))" in html
    assert "orderAddButtons.forEach(button=>button.hidden=mode==='view')" in html
    assert "if(orderMode==='view')return" in html
    assert "if(orderMode==='view')setOrderMode('asporto')" not in html


def test_public_order_google_action_and_estimated_total_are_prominent():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert 'class="google-signin" href="{{ url_for(\'auth_google_order\', slug=shop.slug, lang=language) }}"' in html
    assert '<span>Accedi con Google</span>' in html
    assert 'id="orderGoogleInfo" hidden' in html
    assert 'id="orderTotal"' in html
    assert '#orderTotal{display:block' in html
    assert 'font-size:clamp(1.15rem,3vw,1.42rem)' in html


def test_public_product_details_hint_sits_beside_add_button():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert '<div class="product-actions"><span class="product-open-hint">Tocca per i dettagli</span></div>' in html
    assert "controls.append(removeButton,button)" in html
    assert "card.querySelector('.product-actions').appendChild(controls)" in html
    assert "removeButton.textContent = '−'" in html
    assert "if(current.pizzeria || current.quantita<=1) orderCart.delete(key)" in html
    assert "button.closest('[data-order-id]').dataset.orderId" in html
    assert '.product-actions{grid-column:1/-1;display:flex' in html


def test_public_pizzeria_formats_are_visible_subpages_with_exact_product_prices():
    html = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert 'class="pizzeria-format-nav"' in html
    assert 'data-format-target="{{ format_name|e }}"' in html
    assert 'data-pizzeria-page-format="{{ product.pizzeria_format or \'\' }}"' in html
    assert "function activateFormat(section, formatName)" in html
    assert "card.classList.toggle('format-hidden', Boolean(productFormat) && productFormat !== formatName)" in html
    assert "combine.dataset.pizzeriaFormat = formatName" in html


def test_category_formats_drive_product_configuration_and_support_direct_calzones():
    root = Path(__file__).resolve().parents[1] / "templates"
    categories = (root / "sections" / "categorie.html").read_text(encoding="utf-8")
    products = (root / "sections" / "prodotti.html").read_text(encoding="utf-8")
    configurator = (root / "pizzeria_test.html").read_text(encoding="utf-8")
    assert 'id="tipo_pizzeria"' in categories
    assert '<option value="standard">Standard</option>' in categories
    assert '<option value="personalizzabile">Personalizzabile</option>' in categories
    assert "Pizze personalizzabili" not in categories
    assert "Calzoni personalizzabili" not in categories
    assert "Panini personalizzabili" not in categories
    assert "tipo_pizzeria:savedCategoryType()" in categories
    assert 'id="varianti_abilitate"' in categories
    assert 'varianti_abilitate:f("varianti_abilitate").checked' in categories
    assert 'id="varianti_abilitate_override"' in products
    assert 'fd.append("varianti_abilitate_override"' in products
    assert 'id="prodotto_pizzeria"' not in products
    assert "function categoryIsCustom()" in products
    assert 'id="categoryFormats"' in categories
    assert "formati:categoryFormats()" in categories
    assert "function renderCategoryFormats(existing=[])" in products
    assert "data-pizza-format-enabled" in products
    assert 'value="calzone_prodotto"' in configurator


def test_category_can_enable_combine_tastes_for_its_own_products():
    root = Path(__file__).resolve().parents[1]
    categories = (root / "templates" / "sections" / "categorie.html").read_text(encoding="utf-8")
    app_source = (root / "app.py").read_text(encoding="utf-8")
    assert 'id="combina_gusti"' in categories
    assert 'combina_gusti:f("combina_gusti").checked' in categories
    assert 'categorie_gusti:' in categories
    assert 'prodotti_gusti:' in categories
    assert 'section["pizzeria_combine_enabled"]' in app_source
    assert 'combine_enabled and category["pizzeria_mixed_formats"]' in app_source
    configurator = (root / "templates" / "pizzeria_test.html").read_text(encoding="utf-8")
    assert "(p.tipo_pizzeria||'pizza')===categoryKind" in configurator
    assert "Calzone gusto pizza" not in configurator
    assert "Panino gusto pizza" not in configurator
    assert "Calzoni e panini con gusto pizza" not in (root / "templates" / "sections" / "pizzeria.html").read_text(encoding="utf-8")
    assert '"pizzeria_choose_taste": True' not in app_source


def test_formats_page_configures_taste_sources_and_groups_stock_by_dough():
    root = Path(__file__).resolve().parents[1]
    formats = (root / "templates" / "sections" / "formati.html").read_text(encoding="utf-8")
    public_menu = (root / "templates" / "public_menu.html").read_text(encoding="utf-8")
    configurator = (root / "templates" / "pizzeria_test.html").read_text(encoding="utf-8")
    assert 'class="taste-category"' in formats
    assert 'class="taste-product"' in formats
    assert 'stock-format-checks' in formats
    assert 'group.dataset.dough' in formats
    assert 'Nome panetta' in formats
    assert "category.tipo_pizzeria==='pizza'?'Pizza'" in formats
    assert 'data-cut="${number}"' in formats
    assert "fetch('/api/pizzeria/varianti'" in formats
    assert "${number} gusti" in formats and "[2,3,4]" in formats
    assert 'data-category-id="{{ category.id }}"' in public_menu
    assert "requestedCategory=params.get('category')" in configurator
    assert 'cfg.selezioni_gusti||{}' in configurator
    assert '(mixed||derived)?compatibleTastes' in configurator
    assert "limited?(categoryIds.includes(p.id_categoria)||productIds.includes(p.id))" in configurator
    assert "payload.id_categoria_configurazione=Number(requestedCategory)" in configurator
    assert "cfg.equivalenze_formati||{}" in configurator
    assert "const productFormat=" in configurator
    assert "kind.value==='multigusto'?'pizza'" in configurator


def test_formats_section_is_shared_with_pizzeria_and_replaces_quick_configuration():
    root = Path(__file__).resolve().parents[1]
    dashboard = (root / "templates" / "dashboard_user.html").read_text(encoding="utf-8")
    formats = (root / "templates" / "sections" / "formati.html").read_text(encoding="utf-8")
    pizzeria = (root / "templates" / "sections" / "pizzeria.html").read_text(encoding="utf-8")
    assert 'data-section="formati"' in dashboard
    assert 'Panette per impasto' in formats
    assert "fetch('/api/categorie_full'" in formats
    assert 'id="openFormatsSection"' in pizzeria
    assert 'Configurazione rapida prodotti' not in pizzeria


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
    data["ora_richiesta"] = "12:67"
    db = FakeConnection()
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        response, status = order_function(db)("esempio")
    assert status == 400
    assert "orario valido" in response.get_json()["error"]
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


def test_custom_seventeen_minute_slots_skip_incomplete_tail():
    tomorrow = (datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)).isoformat()
    db = FakeConnection(pickup_enabled=True, pickup_start="12:03", pickup_end="13:00", pickup_minutes=17)
    with FLASK.test_request_context("/api/menu/esempio/ordini/disponibilita?data=" + tomorrow):
        response = availability_function(db)("esempio")
    assert response.get_json()["fasce"] == ["12:03", "12:20", "12:37"]
    data = payload(1)
    data.update(data_richiesta=tomorrow, ora_richiesta="12:20")
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        _response, status = order_function(db)("esempio")
    assert status == 201
    data["ora_richiesta"] = "12:54"
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        _response, status = order_function(db)("esempio")
    assert status == 400


def test_two_pickup_windows_apply_only_to_the_selected_weekday():
    tomorrow = datetime.now(ZoneInfo("Europe/Rome")).date() + timedelta(days=1)
    schedule = [[] for _ in range(7)]
    schedule[tomorrow.weekday()] = [{"dalle": "11:00", "alle": "12:00"}, {"dalle": "18:30", "alle": "19:30"}]
    db = FakeConnection(pickup_enabled=True, pickup_minutes=30, weekly_schedule=schedule)
    with FLASK.test_request_context("/api/menu/esempio/ordini/disponibilita?data=" + tomorrow.isoformat()):
        response = availability_function(db)("esempio")
    assert response.get_json()["fasce"] == ["11:00", "11:30", "18:30", "19:00"]
    data = payload(1)
    data.update(data_richiesta=tomorrow.isoformat(), ora_richiesta="18:30")
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        _response, status = order_function(db)("esempio")
    assert status == 201
    data["ora_richiesta"] = "14:00"
    with FLASK.test_request_context("/api/menu/esempio/ordini", method="POST", json=data):
        _response, status = order_function(db)("esempio")
    assert status == 400
    following = tomorrow + timedelta(days=1)
    with FLASK.test_request_context("/api/menu/esempio/ordini/disponibilita?data=" + following.isoformat()):
        response = availability_function(db)("esempio")
    assert response.get_json()["fasce"] == []
    assert response.get_json()["disponibile"] is False


def test_weekly_pickup_schedule_rejects_overlap_and_more_than_two_windows():
    node = copy.deepcopy(next(item for item in TREE.body if isinstance(item, ast.FunctionDef) and item.name == "validate_pickup_schedule"))
    scope = {"re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
    validate = scope["validate_pickup_schedule"]
    schedule = [[] for _ in range(7)]
    schedule[0] = [{"dalle": "18:00", "alle": "19:00"}, {"dalle": "12:00", "alle": "13:00"}]
    assert validate(schedule, 30)[0] == [{"dalle": "12:00", "alle": "13:00"}, {"dalle": "18:00", "alle": "19:00"}]
    schedule[0][1]["dalle"] = "18:30"
    schedule[0][1]["alle"] = "19:30"
    assert validate(schedule, 30) is None
    schedule[0] = [{"dalle": "12:00", "alle": "13:00"}] * 3
    assert validate(schedule, 30) is None


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
                return [(19, 4, day, "12:15", "Mario Rossi", "+39123456", "", "", "da_evadere", Decimal("6.00"), "cliente", "12/09/2026 09:00", "Articolo", Decimal("1.5"), Decimal("6.00"), 3, 2, "Primi", 1, 4, {"_stampa": {"tipo": "Pizza", "formato": "Doppia", "impasto": "Classico", "gusti": [{"nome": "MARGHERITA", "quota": "", "senza": ["pomodoro"], "aggiunte": ["prosciutto"]}]}})]

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
    assert result["ordini"][0]["numero"] == 4
    assert result["ordini"][0]["prodotti"][0]["quantita"] == "1.5"
    assert result["ordini"][0]["prodotti"][0]["configurazione"]["_stampa"]["gusti"][0]["nome"] == "MARGHERITA"


def test_fulfillment_page_always_shows_product_totals_and_completed_orders():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert 'id="articleCount"' in html
    assert 'id="totalCount"' in html
    assert 'id="pendingCount"' in html
    assert 'id="doneCount"' in html
    assert "ready?' working':done?' done'" in html
    assert 'id="orderHistory"' in html
    assert "historyPanel.showModal()" in html
    assert "load({silent:true})},5000" in html


def test_fulfillment_big_product_number_switches_between_total_pending_and_done():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert "quantityMode='total'" in html
    assert "[['total','Totale'],['pending','Da preparare'],['done','Evasi']]" in html
    assert "amounts[quantityMode]/1000" in html
    assert "number.dataset[mode]" in html
    assert "button.addEventListener('click',()=>selectQuantityMode(mode))" in html


def test_fulfillment_page_keeps_separate_product_and_order_views():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert 'id="summaryView" type="button" aria-pressed="true"' in html
    assert 'id="ordersView" type="button" aria-pressed="false"' in html
    assert 'id="addOrderView" type="button" aria-pressed="false"' in html
    assert 'id="defaultPage"' in html
    assert "alpha-menu-fulfillment-default-page" in html
    assert "let lastOrders=[],displayMode='totali'" in html
    assert "prep.hidden=displayMode!=='totali'" in html
    assert "grid.hidden=displayMode!=='ordini'" in html
    assert "['Evasi',amounts.done,'evaded']" in html
    assert "['Da preparare',amounts.pending,'remaining']" in html
    assert "['Totale',amounts.total,'all']" in html


def test_fulfillment_orders_are_compact_and_expandable():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert "node(grid,'details',undefined,'order-card'" in html
    assert "node(card,'summary',undefined,'order-summary')" in html
    assert "'order-summary-count'" in html
    assert "'order-summary-status'" in html
    assert ".order-card[open]>.order-summary::after" in html
    assert "grid-template-areas:'title title title toggle' 'meta count status toggle'" in html
    cards_css = (Path(__file__).resolve().parents[1] / "static" / "fulfillment-cards.css").read_text(encoding="utf-8")
    assert "minmax(min(100%, 365px), 1fr)" in cards_css
    assert "const body=node(card,'div',undefined,'order-card-body')" in html
    assert "expandedOrderIds.has(String(order.id))" in html


def test_fulfillment_orders_show_product_names_then_variants_like_public_review():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert "function pizzeriaPresentation(product)" in html
    assert "mixed?' · combina gusti':''" in html
    assert "/^\\s*(?:Pizza|Calzone|Panino)\\s*·/i.test(taste.nome)?taste.nome:baseType+' · '+taste.nome" in html
    assert "...(taste.senza||[]).map(name=>'− '+String(name).replace" in html
    assert "...(taste.aggiunte||[]).map(name=>'+ '+String(name).replace" in html
    assert "clean=name=>String(name).replace" in html
    assert ".manual-cart-row-head b{flex:0 0 auto;white-space:nowrap}" in html
    assert "appendProductGroups(line,presentation,'order-product-groups'" in html
    assert "appendProductGroups(row,{meta:item.meta||'',groups:item.groups||[]},'manual-cart-groups'" in html
    assert ".manual-cart-groups{display:grid" in html


def test_fulfillment_product_section_follows_date_and_navigation_is_lateral():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert html.index('id="date"') < html.index('id="groups"') < html.index('class="metrics"')
    assert 'body{padding-left:270px;transition:padding-left .18s}' in html
    assert 'class="side-view-nav"' in html
    assert 'id="sidebarToggle"' in html
    assert 'class="side-tools"' in html
    assert '<details class="side-tools"><summary>Strumenti e collegamenti</summary>' in html
    assert 'alpha-menu-fulfillment-sidebar-collapsed' in html
    assert "classList.toggle('sidebar-collapsed',collapsed)" in html


def test_order_operations_are_on_fulfillment_page_not_settings():
    root = Path(__file__).resolve().parents[1] / "templates"
    settings = (root / "sections" / "ordini.html").read_text(encoding="utf-8")
    fulfillment = (root / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    for old_control in ('id="manualOrderForm"', 'id="ordersPeriod"', 'id="ordersSummary"', 'id="ordersList"'):
        assert old_control not in settings
    assert 'id="addOrderView"' in fulfillment
    assert 'id="manualOrderForm"' in fulfillment
    assert 'id="historyPeriod"' in fulfillment


def test_order_forms_pair_reference_and_notes_and_manual_products_are_searchable():
    root = Path(__file__).resolve().parents[1] / "templates"
    fulfillment = (root / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    public = (root / "public_menu.html").read_text(encoding="utf-8")
    assert 'class="manual-extra-fields"' in fulfillment
    assert 'class="order-details" id="orderDetails"' in public
    assert 'id="manualProductSearch" type="search"' in fulfillment
    assert 'id="manualCategories"' in fulfillment
    assert 'id="manualProducts"' in fulfillment
    assert 'id="manualCart"' in fulfillment
    assert "manualProducts.filter(product=>" in fulfillment
    assert "if(product.descrizione)node(card,'small',product.descrizione)" not in fulfillment
    assert "query?normalizeSearch(product.nome+' '+product.descrizione+' '+product.categoria_nome).includes(query)" in fulfillment
    assert "classList.toggle('active',!query&&id===selectedManualCategory)" in fulfillment
    assert "if(query)node(card,'small',product.categoria_nome||'Senza categoria','manual-product-category')" in fulfillment
    assert "openPizzeria({kind" in fulfillment
    assert "pizzeria:item.pizzeria" in fulfillment
    assert "datalist" not in fulfillment
    assert '<option value="anno">Anno</option>' in fulfillment


def test_owner_manual_orders_accept_server_validated_pizzeria_configuration():
    endpoint = SOURCE[SOURCE.index('def api_crea_ordine_menu'):SOURCE.index('def order_push_keys')]
    assert 'if manual or not isinstance(config, dict)' not in endpoint
    assert 'if not isinstance(config, dict)' in endpoint
    assert 'quote_pizzeria_draft({**config, "quantita": 1}, *settings)' in endpoint


def test_add_order_is_a_dedicated_page_with_a_saved_default_view():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert '<section class="manual-order" id="manualOrderPanel"' in html
    assert 'function openManualPage()' in html
    assert "selectAppPage(defaultPage.value)" in html
    assert "localStorage.setItem(pagePreferenceKey,defaultPage.value)" in html


def test_manual_order_compacts_customer_and_uses_direct_slots_and_private_configurator():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert 'id="manualCustomerSummary" hidden' in html
    assert "manualCustomer.classList.add('compact')" in html
    assert 'id="manualSlotButtons"' in html
    assert "onUpdate:renderManualSlotButtons" in html
    assert '<span>Orario</span>' in html
    assert "node(manualSlotButtons,'button',option.value)" in html
    assert '.manual-slot-buttons{gap:4px;flex-wrap:wrap;overflow:visible}' in html
    assert '.manual-slot-buttons button{min-width:64px;min-height:31px' in html
    assert '.manual-categories{flex-wrap:wrap;overflow:visible;padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid #456584}' in html
    assert ".manual-categories button.active::before{content:'✓ '}" in html
    assert "for(const format of Object.values(pizza.formati)" not in html
    assert "url_for('manual_order_product_configurator')" in html
    configurator = (Path(__file__).resolve().parents[1] / "templates" / "pizzeria_test.html").read_text(encoding="utf-8")
    assert ".embedded .taste .ingredients-summary{display:none}" in configurator
    assert "'public' if public_slug and not order_embed else ''" in configurator
    assert ".embedded{background:#0d1627;color:#f2f6ff}" in configurator
    assert "card.onclick=event=>" in html


def test_staff_product_configurator_uses_direct_format_buttons():
    configurator = (Path(__file__).resolve().parents[1] / "templates" / "pizzeria_test.html").read_text(encoding="utf-8")
    assert 'id="format" hidden aria-hidden="true"' in configurator
    assert 'id="formatButtons" class="format-buttons"' in configurator
    assert '.format-choice{grid-column:1/-1}' in configurator
    assert ".format-buttons button[aria-pressed=true]::before,.option-buttons button[aria-pressed=true]::before{content:'✓ '" in configurator
    assert 'background:#f2b83f;color:#172033' in configurator
    assert '.embedded .format-buttons button[aria-pressed=true],.embedded .option-buttons button[aria-pressed=true]{background:#f2b83f;color:#172033}' in configurator
    assert "const renderFormatButtons=(formats,requested)=>" in configurator
    assert "Number(price).toLocaleString('it-IT'" in configurator
    assert "format.value=name;render()" in configurator
    assert 'id="cut" hidden aria-hidden="true"' in configurator
    assert 'id="cutButtons" class="option-buttons"' in configurator
    assert 'id="dough" hidden aria-hidden="true"' in configurator
    assert 'id="doughButtons" class="option-buttons"' in configurator
    assert "renderSelectButtons(cut,cutButtons,render)" in configurator
    assert "renderSelectButtons(dough,doughButtons,scheduleQuote)" in configurator
    assert "publicSlug&&requestedProduct&&requested?" in configurator
    assert "publicSlug&&requestedProduct&&!mixed&&requested?" not in configurator


def test_product_customization_compacts_and_orders_ingredient_actions():
    configurator = (Path(__file__).resolve().parents[1] / "templates" / "pizzeria_test.html").read_text(encoding="utf-8")
    assert "removalLabel.textContent='Togli'" in configurator
    assert "additionLabel.textContent='Aggiungi'" in configurator
    assert configurator.index("removalLabel.textContent='Togli'") < configurator.index("additionLabel.textContent='Aggiungi'")
    assert "document.createTextNode('− '+name)" in configurator
    assert "label:'+ '+a.nome+' · € '" in configurator
    assert "Ingredienti da aggiungere (facoltativi)" not in configurator
    assert "Ingredienti da togliere (facoltativi)" not in configurator
    assert ".taste-removals label:has(input:checked)" in configurator


def test_pickup_choices_show_single_times_instead_of_ranges():
    shared = (Path(__file__).resolve().parents[1] / "static" / "order-request.js").read_text(encoding="utf-8")
    public = (Path(__file__).resolve().parents[1] / "templates" / "public_menu.html").read_text(encoding="utf-8")
    assert "new Option(start, start)" in shared
    assert "start + '–' + end" not in shared
    assert "new Option(slot,slot)" in public
    assert "slot+'–'+end" not in public


def test_manual_order_prominently_shows_current_dough_stock():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert html.index('id="manualDoughStock"') < html.index('<h3>Scegli gli articoli</h3>')
    assert 'aria-label="Disponibilità panette"' in html
    assert '🍕 Panette disponibili' not in html
    assert 'Disponibilità attuale' not in html
    assert "node(copy,'small','Per: '" not in html
    assert "fetch('/api/pizzeria/preparazione',{cache:'no-store'})" in html
    assert "remaining<=5?'stock-low':'stock-ok'" in html
    assert "remaining===0?'esaurite'" in html
    assert "Promise.all([load(),refreshManualDoughStock()])" in html


def test_manual_dough_stocks_stay_on_one_row_and_support_persistent_long_press_reordering():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert ".manual-dough-stock-items{display:flex;flex-wrap:nowrap" in html
    assert "function enableDoughStockLongPress(card)" in html
    assert "timer=setTimeout(()=>{active=true" in html
    assert "touch-action:none" in html
    assert "card.setPointerCapture?.(pointerId)" in html
    assert "card.parentElement.scrollLeft-=event.clientX-lastX" in html
    assert "siblings.find(item=>" in html
    assert "if(target)items.insertBefore(card,target);else items.append(card)" in html
    assert "document.elementFromPoint" not in html
    assert "fetch('/api/pizzeria/preparazione',{method:'PUT'" in html
    assert "note.hidden=false;note.textContent='Salvataggio del nuovo ordine…'" in html
    assert "Tieni premuto e trascina per spostare" in html


def test_manual_dough_stock_updates_from_selected_cart_items_before_submission():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert "document.getElementById('manualCartTotal').textContent" in html
    assert "renderManualDoughStock();" in html
    assert "const reserved=manualCart.reduce" in html
    assert "quantity-reserved" in html
    assert "reserved?'residue':'disponibili'" in html
    assert "Quantità residue previste dopo gli articoli selezionati" in html
    assert "note.hidden=!note.textContent" in html
    assert "I formati non elencati sono illimitati" not in html


def test_manual_order_emphasizes_insufficient_dough_stock_errors():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    assert ".manual-feedback.is-error" in html
    assert ".manual-feedback.is-stock-error::before{content:'⚠ SCORTE INSUFFICIENTI'" in html
    assert "isError&&/^Panette insufficienti/i.test(message||'')" in html
    assert "manualFeedback.scrollIntoView({behavior:'smooth',block:'center'})" in html
    assert "catch(error){setManualFeedback(error.message,'error')}" in html


def test_manual_dough_stock_spans_above_catalog_and_cart_in_a_compact_row():
    html = (Path(__file__).resolve().parents[1] / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
    stock = html.index('id="manualDoughStock"')
    workspace = html.index('<div class="manual-order-workspace">')
    catalog = html.index('<section class="manual-catalog">')
    assert stock < workspace < catalog
    assert ".manual-dough-stock-card{display:flex;align-items:center;justify-content:space-between;flex:0 0 180px" in html
    assert ".manual-dough-stock{margin:0;padding:7px 9px" in html
