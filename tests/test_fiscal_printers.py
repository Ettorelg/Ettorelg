import pytest
from xml.etree.ElementTree import fromstring
from fiscal_printers import validate_config, receipt_xml


def config():
    return dict(brand='epson', model='FP-81II RT', ip='192.168.1.70', departments={'3': 2})


def order():
    return dict(totale='12.50', prodotti=[dict(nome='PIZZA & <EXTRA>', quantita='2', totale='12.50', id_categoria=3)])


def test_xml_escapes_names_preserves_totals_and_payment():
    root = fromstring(receipt_xml(order(), config(), 'carta'))
    assert root.find('printRecItem').attrib['description'] == 'PIZZA & <EXTRA>'
    assert root.find('printRecItem').attrib['unitPrice'] == '6,25'
    assert root.find('printRecItem').attrib['department'] == '2'
    assert root.find('printRecTotal').attrib['payment'] == '12,50'
    assert root.find('printRecTotal').attrib['paymentType'] == '2'
    assert root.find('printRecTotal').attrib['index'] == '1'


def test_missing_vat_mapping_and_total_mismatch_are_rejected():
    settings = config(); settings['departments'] = {}
    with pytest.raises(ValueError): receipt_xml(order(), settings, 'contanti')
    sale = order(); sale['totale'] = '15'
    with pytest.raises(ValueError): receipt_xml(sale, config(), 'contanti')


@pytest.mark.parametrize('ip', ['127.0.0.1', '8.8.8.8', '169.254.169.254', 'http://192.168.1.1'])
def test_invalid_network_targets(ip):
    settings = config(); settings['ip'] = ip
    with pytest.raises(ValueError): validate_config(settings)


def test_other_brands_can_be_saved_but_cannot_generate_epson_commands():
    settings = config(); settings.update(brand='axon_micrelec', model='Dado RT / RT30')
    assert validate_config(settings)['status'] == 'unsupported'
    with pytest.raises(ValueError): receipt_xml(order(), settings, 'contanti')


def test_invalid_numbers_and_unrepresentable_rounding_are_rejected():
    for total in ['NaN', 'Infinity', '-1']:
        sale = order(); sale['prodotti'][0]['totale'] = total
        with pytest.raises(ValueError): receipt_xml(sale, config(), 'contanti')
    sale = order(); sale['prodotti'][0]['quantita'] = '3'
    with pytest.raises(ValueError): receipt_xml(sale, config(), 'contanti')
