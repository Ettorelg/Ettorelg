from unittest.mock import Mock
import pytest
from tools import axon_bridge as axon, epson_bridge as bridge
from fiscal_printers import receipt_xml, payment_totals, validate_config, parse_axon_response

CONFIG = dict(brand='axon_micrelec', model='Dado RT / RT30', ip='192.168.2.30', departments={'3': 1}, cash_index=1, card_index=4)
ORDER = dict(totale='12.50', prodotti=[dict(nome='PIZZA / CAFFÈ', quantita=2, totale='12.50', id_categoria=3)])


def test_payload_and_discount():
    totals = payment_totals(ORDER, dict(payment='contanti', discount='10', discount_type='percent', tendered='20'))
    sale = receipt_xml(ORDER, CONFIG, 'contanti', totals)
    assert sale['commands'] == ['3/S/PIZZA - CAFFE//2/6.25/1/////', '4/1.25/SCONTO//0/1/1/', '5/1/20.00////PC///']
    assert sale['due'] == '11.25'
    assert validate_config(CONFIG)['status'] == 'preview'
    assert validate_config(CONFIG | dict(live=True, verified=True))['status'] == 'live'


@pytest.mark.parametrize('row', [dict(number=1, description='BAD/COMMAND', vat_group=1), dict(number=1, description='OK', vat_group=13)])
def test_invalid_write_never_sends(monkeypatch, row):
    sender = Mock();monkeypatch.setattr(axon, 'command', sender)
    with pytest.raises(ValueError):
        axon.write_programming(CONFIG, dict(confirmation='SCRIVI CONFIGURAZIONE AXON', sections=['departments'], data=dict(departments=[row])))
    sender.assert_not_called()


def test_department_optional_flags_preserved_and_closed_day_required(monkeypatch):
    sender = Mock(return_value=(['0','0'], [0,0,0]));monkeypatch.setattr(axon,'command',sender)
    payload=dict(confirmation='SCRIVI CONFIGURAZIONE AXON', sections=['departments'], data=dict(departments=[dict(number=1,description='PIZZE',vat_group=3)]))
    axon.write_programming(CONFIG,payload)
    assert sender.call_args.args[1] == 'N/1/PIZZE/3///////'
    sender.reset_mock();sender.return_value=(['0','0'],[0,0,2])
    with pytest.raises(ValueError, match='giornata'):axon.write_programming(CONFIG,payload)
    assert sender.call_count == 1


def test_emit_checks_document_and_total(monkeypatch):
    monkeypatch.setattr(axon,'idle',Mock(side_effect=[['0','0'],['0','1']]))
    def command(config, packet):
        if packet.startswith('{/'):return ['CONTANTI','CONT','','0','0','0','10101101','0','5','0'],[0,0,0]
        if packet.startswith(':/'):return ['REP','1','1','0','0','0','110000000'],[0,0,0]
        if packet.startswith('0/4/'):return ['','', '12.50']+['0']*28,[0,0,2]
        return [],[0,0,2]
    monkeypatch.setattr(axon,'command',command)
    monkeypatch.setattr(axon,'request',lambda _:dict(ej=dict(lastdoc=dict(zno=2,docno=1)),rtc=dict(date='18-09-2026'),device=dict(serial='ABC')))
    result=axon.emit(CONFIG,receipt_xml(ORDER,CONFIG,'contanti'))
    assert parse_axon_response(result,'12.50')['number']==1
    with pytest.raises(ValueError):parse_axon_response(result,'12.51')


def test_wrong_payment_stops_before_sale(monkeypatch):
    monkeypatch.setattr(axon,'idle',Mock(return_value=['0','0']))
    sender=Mock(return_value=(['CREDITI','CRED','','0','2','0','10000000','0','4','0'],[0,0,0]))
    monkeypatch.setattr(axon,'command',sender)
    with pytest.raises(ValueError,match='Totalizzatore'):axon.emit(CONFIG,receipt_xml(ORDER,CONFIG,'contanti'))
    assert sender.call_count==1


@pytest.mark.parametrize('fail', [False, True])
def test_axon_ledger_never_repeats_sale(monkeypatch,tmp_path,fail):
    monkeypatch.setattr(bridge,'LEDGER',tmp_path/'jobs.db')
    emit=Mock(side_effect=TimeoutError() if fail else None,return_value=dict(number=1))
    monkeypatch.setattr(axon,'emit',emit)
    monkeypatch.setattr(bridge,'cloud',lambda path,data,secret:dict(config=CONFIG,xml={}) if path=='lavoro' else dict(ok=not fail))
    job=dict(id='829d4363-3bd6-4963-bd87-1d16ef696d9c',secret='x'*43)
    bridge.dispatch('/fiscal/emit',job)
    bridge.dispatch('/fiscal/recover',job)
    bridge.dispatch('/fiscal/emit',job)
    assert emit.call_count==1
