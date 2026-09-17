import ast
import json
from pathlib import Path
from types import SimpleNamespace
from flask import Flask, request, jsonify


def invoke(keys, stocks, shop=7):
    tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'api_pizzeria_preparazione_ordine')
    node.decorator_list = []
    class DB:
        def __init__(self): self.statements = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return self
        def close(self): pass
        def execute(self, sql, params): self.statements.append((sql, params))
        def fetchone(self): return (stocks,)
    db = DB()
    scope = dict(request=request, jsonify=jsonify, json=json,
                 order_notification_shop_id=lambda: shop, build_db_config=lambda: {},
                 psycopg2=SimpleNamespace(connect=lambda **kw: db))
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), scope)
    app = Flask(__name__)
    with app.test_request_context(json={'ordine': keys}, method='PUT'):
        response = scope[node.name]()
        if isinstance(response, tuple): return response[0].get_json(), response[1], db
        return response.get_json(), 200, db


def test_reorder_uses_locked_current_quantities_and_shop_scope():
    stocks = [dict(impasto='Classico', nome='Media', quantita=2), dict(impasto='Classico', nome='Grande', quantita=5)]
    body, status, db = invoke([['Classico', 'Grande'], ['Classico', 'Media']], stocks)
    assert status == 200
    assert [s['quantita'] for s in body['panette']] == [5, 2]
    assert 'FOR UPDATE' in db.statements[0][0]
    assert db.statements[0][1] == (7,)
    assert json.loads(db.statements[1][1][0]) == body['panette']


def test_changed_stock_list_and_duplicates_never_write():
    stocks = [dict(impasto='Classico', nome='Media', quantita=2)]
    for keys, expected in [([['Classico', 'Grande']], 409), ([['Classico', 'Media']]*2, 400)]:
        _, status, db = invoke(keys, stocks)
        assert status == expected
        assert not any('UPDATE pizzeria' in sql for sql, _ in db.statements)


def test_anonymous_cannot_reorder():
    _, status, db = invoke([], [], shop=None)
    assert status == 403
    assert not db.statements
