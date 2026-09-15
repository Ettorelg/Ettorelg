from decimal import Decimal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import session
from test_order_requests import FLASK, FakeConnection, FakeCursor, payload, order_function, availability_function


class RetryCursor(FakeCursor):
    existing = None
    def execute(self, query, params=None):
        super().execute(query, params)
        if 'SET chiave_richiesta=' in query:
            self.existing = (123, Decimal('4.00'), params[1], 1)
    def fetchone(self):
        if 'SELECT id,totale,impronta_richiesta' in self.query:
            return self.existing
        return super().fetchone()


def test_retry_returns_original_order_without_another_insert():
    db = FakeConnection()
    db.cur = RetryCursor()
    for expected in (201, 200):
        with FLASK.test_request_context('/api/menu/shop/ordini', method='POST', json=payload(1), headers={'Idempotency-Key': 'retry-key-123456789012345'}):
            response, status = order_function(db)('shop')
        assert status == expected
        assert response.get_json()['ordine_id'] == 123
    assert sum('INSERT INTO ordini_menu' in sql for sql, _ in db.cur.statements) == 1
    with FLASK.test_request_context('/api/menu/shop/ordini', method='POST', json=payload(2), headers={'Idempotency-Key': 'retry-key-123456789012345'}):
        _, status = order_function(db)('shop')
    assert status == 409


def test_manual_availability_requires_auth_and_scopes_employee_shop():
    tomorrow = (datetime.now(ZoneInfo('Europe/Rome')).date()+timedelta(days=1)).isoformat()
    db = FakeConnection(active=False, pickup_enabled=True, pickup_start='18:00', pickup_end='19:00', pickup_minutes=30, pickup_capacity=Decimal(2), slot_rows=[('18:00',2,Decimal(2))])
    with FLASK.test_request_context('/api/ordini/disponibilita?data='+tomorrow):
        _, status = availability_function(db)()
        assert status == 401
    with FLASK.test_request_context('/api/ordini/disponibilita?data='+tomorrow):
        session.update(employee_id=4, employee_shop_id=7)
        response = availability_function(db)()
    assert response.get_json()['fasce'] == ['18:30']
    sql, params = next((sql,params) for sql,params in db.cur.statements if 'FROM negozi' in sql)
    assert 'WHERE id=%s' in sql
    assert params == (7,)
