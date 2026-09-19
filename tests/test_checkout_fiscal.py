import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
from xml.etree.ElementTree import fromstring
import pytest
from flask import Flask, jsonify
from checkout import register_checkout
from fiscal_printers import payment_totals, receipt_xml, parse_fiscal_response, validate_config

CONFIG = dict(brand='epson', model='FP-81II RT', ip='192.168.1.70', departments={'3': 2})
ORDER = dict(id=123, totale='12.50', prodotti=[dict(nome='PIZZA', quantita='1', totale='12.50', id_categoria=3)])
RESPONSE = '<response success="true"><addInfo><fiscalReceiptNumber>15</fiscalReceiptNumber><fiscalReceiptDate>17/09/2026</fiscalReceiptDate><fiscalReceiptAmount>11,25</fiscalReceiptAmount></addInfo></response>'


def test_cash_percent_discount_rounding_and_change():
    totals = payment_totals(ORDER, dict(payment='contanti', discount='10', discount_type='percent', tendered='20'))
    assert totals['due'] == '11.25' and totals['change'] == '8.75'
    xml = fromstring(receipt_xml(ORDER, CONFIG, 'contanti', totals))
    assert xml.find('printRecSubtotalAdjustment').get('amount') == '1,25'
    assert xml.find('printRecTotal').get('payment') == '20,00'


@pytest.mark.parametrize('changes', [dict(discount='100', discount_type='percent'),dict(discount='13'),dict(discount='-1'),dict(discount='NaN'),dict(tendered='1'),dict(payment='unknown'),dict(discount='0.001')])
def test_bad_payment_rejected(changes):
    with pytest.raises(ValueError):
        payment_totals(ORDER, dict(payment='contanti') | changes)


def test_card_exact_amount_and_explicit_live_enable():
    totals = payment_totals(ORDER, dict(payment='carta', tendered='100'))
    assert totals['tendered'] == '12.50' and totals['change'] == '0.00'
    assert validate_config(CONFIG)['status'] == 'preview'
    assert validate_config(CONFIG | dict(live=True))['status'] == 'preview'
    assert validate_config(CONFIG | dict(live=True, verified=True))['status'] == 'live'


def test_response_requires_document_and_matching_amount():
    assert parse_fiscal_response(RESPONSE, '11.25')['fiscalReceiptNumber'] == '15'
    for xml in ['<response success="true"/>','<response success="false" code="PAPER"/>', RESPONSE.replace('11,25','0,00'), '<!DOCTYPE x><response success="true"/>']:
        with pytest.raises(ValueError):
            parse_fiscal_response(xml, '11.25')


def test_preview_does_not_touch_database_and_live_is_blocked_by_default():
    app = Flask(__name__)
    connect = Mock(side_effect=AssertionError('Preview must not mutate DB'))
    register_checkout(app, connect, lambda: 7, lambda _: jsonify(ordine=ORDER), lambda: jsonify(config=validate_config(CONFIG)))
    client = app.test_client()
    assert client.post('/api/ordini/123/pagamento', json=dict(payment='carta',action='preview')).status_code == 200
    assert client.post('/api/ordini/123/pagamento', json=dict(payment='carta',action='confirm')).status_code == 400
    assert not connect.called


def test_bridge_ledger_prevents_reissue_after_callback_failure(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('epson_test', Path(__file__).parents[1] / 'tools/epson_bridge.py')
    bridge = importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
    monkeypatch.setattr(bridge, 'LEDGER', tmp_path / 'jobs.db')
    send = Mock(return_value=RESPONSE);monkeypatch.setattr(bridge, 'send', send)
    job = {'id':'829d4363-3bd6-4963-bd87-1d16ef696d9c', 'secret':'x'*43}
    attempts=[]
    def cloud(path, data, secret):
        attempts.append(path)
        if path=='lavoro': return dict(config=CONFIG, xml='<printerFiscalReceipt/>')
        if attempts.count('esito')==1: raise TimeoutError('Cloud offline')
        return dict(ok=True)
    monkeypatch.setattr(bridge, 'cloud', cloud)
    with pytest.raises(TimeoutError): bridge.dispatch('/fiscal/emit', job)
    assert bridge.dispatch('/fiscal/recover', job)['ok']
    assert send.call_count == 1 and attempts.count('lavoro') == 1


def test_bridge_timeout_is_not_retried(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('epson_timeout', Path(__file__).parents[1] / 'tools/epson_bridge.py')
    bridge = importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
    monkeypatch.setattr(bridge, 'LEDGER', tmp_path / 'jobs.db')
    send = Mock(side_effect=TimeoutError('Printer timeout'));monkeypatch.setattr(bridge, 'send', send)
    monkeypatch.setattr(bridge,'cloud',lambda path,data,secret: dict(config=CONFIG,xml='<printerFiscalReceipt/>') if path=='lavoro' else dict(ok=False,state='incerto'))
    job={'id':'829d4363-3bd6-4963-bd87-1d16ef696d9c','secret':'x'*43}
    assert not bridge.dispatch('/fiscal/emit', job)['ok']
    assert not bridge.dispatch('/fiscal/emit', job)['ok']
    assert send.call_count == 1


class PaymentDB:
    def __init__(self): self.payment=None;self.order_state='da_evadere';self.result=None
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def cursor(self): return self
    def close(self): pass
    def fetchone(self): return self.result
    def execute(self,sql,args):
        self.result=None
        if sql.startswith('SELECT stato FROM ordini_menu'): self.result=(self.order_state,)
        elif sql.startswith('SELECT id FROM pagamenti_ordini'):
            if self.payment and "stato IN" not in sql:self.result=(self.payment['id'],)
        elif sql.startswith('INSERT INTO pagamenti_ordini'):
            self.payment=dict(id=args[0],order=args[1],shop=args[2],state=args[3],totals=json.loads(args[4]),payload=json.loads(args[5]),secret=args[6])
        elif sql.startswith('SELECT segreto,stato,payload'):
            if self.payment:self.result=(self.payment['secret'],self.payment['state'],self.payment['payload'])
        elif sql.startswith('SELECT payload FROM pagamenti_ordini'):
            if self.payment:self.result=(self.payment['payload'],)
        elif sql.startswith('SELECT segreto,stato,riepilogo'):
            if self.payment:self.result=(self.payment['secret'],self.payment['state'],self.payment['totals'],self.payment['order'],self.payment['shop'])
        elif sql.startswith('SELECT id,stato,riepilogo,payload'):
            if self.payment:self.result=(self.payment['id'],self.payment['state'],self.payment['totals'],self.payment['payload'])
        elif "SET stato='inviato'" in sql:self.payment['state']='inviato'
        elif "SET stato='emesso'" in sql:self.payment['state']='emesso'
        elif sql.startswith('UPDATE pagamenti_ordini SET stato='):self.payment['state']=args[0]
        elif "UPDATE ordini_menu SET stato='evaso'" in sql:self.order_state='evaso'


def test_server_attempt_can_only_be_claimed_once_and_only_success_closes_order():
    app=Flask(__name__);db=PaymentDB()
    register_checkout(app,lambda:db,lambda:7,lambda _:jsonify(ordine=ORDER),lambda:jsonify(config=validate_config(CONFIG|dict(live=True,verified=True))))
    client=app.test_client()
    body=dict(payment='contanti',discount='10',discount_type='percent',tendered='20',action='confirm')
    prepared=client.post('/api/ordini/123/pagamento',json=body)
    assert prepared.status_code==200
    assert client.post('/api/ordini/123/pagamento',json=body).status_code==409
    job=prepared.json['job']; headers={'Authorization':'Bearer '+job['secret']}
    assert client.post('/api/fiscale/lavoro',json={'id':job['id']},headers={'Authorization':'Bearer '+'z'*43}).status_code==403
    assert client.post('/api/fiscale/lavoro',json={'id':job['id']},headers=headers).status_code==200
    assert client.post('/api/fiscale/lavoro',json={'id':job['id']},headers=headers).status_code==409
    failure=client.post('/api/fiscale/esito',json={'id':job['id'],'error':'Timeout'},headers=headers)
    assert failure.json['state']=='incerto' and db.order_state=='da_evadere'
    success=client.post('/api/fiscale/esito',json={'id':job['id'],'xml':RESPONSE},headers=headers)
    assert success.json['state']=='emesso' and db.order_state=='evaso'
    assert client.post('/api/fiscale/esito',json={'id':job['id'],'error':'Late error'},headers=headers).json['state']=='emesso'


def test_axon_server_requires_matching_provider_and_amount():
    app=Flask(__name__);db=PaymentDB()
    config=CONFIG|dict(brand='axon_micrelec',model='Dado RT / RT30',live=True,verified=True,card_index=4)
    register_checkout(app,lambda:db,lambda:7,lambda _:jsonify(ordine=ORDER),lambda:jsonify(config=validate_config(config)))
    client=app.test_client()
    prepared=client.post('/api/ordini/123/pagamento',json=dict(payment='carta',action='confirm'))
    assert prepared.status_code==200
    job=prepared.json['job'];headers={'Authorization':'Bearer '+job['secret']}
    assert job['xml']['brand']=='axon_micrelec'
    assert client.post('/api/fiscale/lavoro',json={'id':job['id']},headers=headers).status_code==200
    result=client.post('/api/fiscale/esito',json={'id':job['id'],'xml':RESPONSE},headers=headers)
    assert result.json['state']=='incerto'
    proof=dict(brand='axon_micrelec',before=0,number=1,zno=2,date='18-09-2026',serial='ABC',amount='12.50')
    result=client.post('/api/fiscale/esito',json={'id':job['id'],'axon':proof},headers=headers)
    assert result.json['state']=='emesso' and db.order_state=='evaso'


def test_owner_can_reconcile_confirmed_dado_document_without_reissue():
    app=Flask(__name__);app.secret_key='test';db=PaymentDB()
    config=CONFIG|dict(brand='axon_micrelec',model='Dado RT / RT30',live=True,verified=True,card_index=4)
    register_checkout(app,lambda:db,lambda:7,lambda _:jsonify(ordine=ORDER),lambda:jsonify(config=validate_config(config)))
    client=app.test_client();client.post('/api/ordini/123/pagamento',json=dict(payment='contanti',action='confirm'))
    db.payment['state']='incerto'
    with client.session_transaction() as session: session['user_id']=9
    result=client.post('/api/ordini/123/pagamento/conferma-emesso',json=dict(
        confirmation='CONFERMO EMESSO',zno=1,document=1,note='Verificato scontrino cartaceo DADO'))
    assert result.status_code==200 and db.payment['state']=='emesso' and db.order_state=='evaso'


class CounterDB(PaymentDB):
    def __init__(self):
        super().__init__()
        self.reserved=False;self.sale_state='aperta';self.statements=[]
    def execute(self,sql,args):
        self.statements.append(sql)
        if sql.startswith('SELECT carrello FROM vendite_banco'):
            self.result=(ORDER,) if args[1]==7 else None
        elif sql.startswith('SELECT stato,scorte_impegnate'):
            self.result=(self.sale_state,self.reserved)
        elif sql.startswith('SELECT scorte_impegnate'):
            self.result=(self.reserved,)
        elif sql.startswith('SELECT id,stato,riepilogo,risposta'):
            self.result=(self.payment['id'],self.payment['state'],self.payment['totals'],{}) if self.payment else None
        elif sql.startswith('UPDATE vendite_banco SET scorte_impegnate'):
            self.reserved=True
        elif sql.startswith('UPDATE vendite_banco SET stato='):
            self.sale_state='conclusa'
        else:
            super().execute(sql,args)
            if sql.startswith('INSERT INTO pagamenti_ordini'):
                self.payment['order']=None


def counter_client(db,stock,shop=7,config=None):
    app=Flask(__name__);app.secret_key='test'
    register_checkout(app,lambda:db,lambda:shop,Mock(side_effect=AssertionError('Must not read orders')),
                      lambda:jsonify(config=validate_config(config or CONFIG|dict(live=True,verified=True))),stock)
    return app.test_client()


def test_direct_sale_reserves_once_and_never_creates_or_completes_order():
    db=CounterDB();stock=Mock();client=counter_client(db,stock)
    url='/api/banco/vendite/test-sale/pagamento'
    assert client.get(url).json['editable']
    assert client.post(url,json=dict(payment='contanti',action='preview')).status_code==200
    stock.assert_not_called()
    body=dict(payment='contanti',action='confirm',discount='10',discount_type='percent')
    prepared=client.post(url,json=body)
    assert prepared.status_code==200 and stock.call_count==1
    assert not client.get(url).json['editable']
    assert client.post(url,json=body).status_code==409 and stock.call_count==1
    job=prepared.json['job'];headers={'Authorization':'Bearer '+job['secret']}
    assert client.post('/api/fiscale/lavoro',json={'id':job['id']},headers=headers).status_code==200
    result=client.post('/api/fiscale/esito',json={'id':job['id'],'xml':RESPONSE},headers=headers)
    assert result.json['state']=='emesso' and db.sale_state=='conclusa'
    assert not any('ordini_menu' in q for q in db.statements)


def test_counter_is_shop_scoped_and_stock_error_does_not_create_attempt():
    db=CounterDB();stock=Mock(side_effect=ValueError('Panette insufficienti'))
    client=counter_client(db,stock)
    result=client.post('/api/banco/vendite/test-sale/pagamento',json=dict(payment='contanti',action='confirm'))
    assert result.status_code==409 and db.payment is None and not db.reserved
    assert counter_client(db,stock,shop=8).get('/api/banco/vendite/test-sale/pagamento').status_code==404


def test_counter_retry_after_manual_verification_does_not_reserve_twice():
    db=CounterDB();db.reserved=True;stock=Mock();client=counter_client(db,stock)
    assert client.post('/api/banco/vendite/test-sale/pagamento',json=dict(payment='contanti',action='confirm')).status_code==200
    stock.assert_not_called()
