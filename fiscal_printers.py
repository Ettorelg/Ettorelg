"""Fiscal configuration and offline Epson ePOS receipt preparation.

Reference: Epson ePOS Fiscal Print Solution Development Guide Rev T, §5.4, §11.1.
No network transmission or fiscal completion is performed by this module.
"""
import ipaddress
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree.ElementTree import Element, SubElement, tostring

MODELS = {
    'epson': ['FP-81II RT', 'FP-90III RT', 'Altro Epson ePOS Fiscal'],
    'axon_micrelec': ['Dado RT / RT30', 'Altro Axon-Micrelec'],
    'altro': ['Altro modello'],
}


def integer(value, low, high):
    if isinstance(value, bool) or not str(value).isdigit() or not low <= int(value) <= high:
        raise ValueError(f'Inserire un numero intero da {low} a {high}.')
    return int(value)


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError('Configurazione non valida.')
    brand = data.get('brand', '')
    if not brand:
        return {}
    if brand not in MODELS or data.get('model') not in MODELS[brand]:
        raise ValueError('Marca o modello non valido.')
    try:
        ip = ipaddress.IPv4Address(data.get('ip', ''))
    except (ValueError, TypeError):
        raise ValueError('Inserire un indirizzo IPv4 della rete locale.')
    networks = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
    if not any(ip in ipaddress.ip_network(net) for net in networks):
        raise ValueError('Il registratore deve essere nella rete locale.')
    departments = data.get('departments', {})
    if not isinstance(departments, dict) or len(departments) > 500:
        raise ValueError('Reparti non validi.')
    return dict(brand=brand, model=data['model'], ip=str(ip),
                port=integer(data.get('port', 80), 1, 65535),
                operator=integer(data.get('operator', 1), 1, 12),
                departments={str(integer(k, 1, 2147483647)): integer(v, 1, 99)
                             for k, v in departments.items()},
                status='preview' if brand == 'epson' else 'unsupported')


def amount(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Importo non valido.')
    if not number.is_finite() or number < 0:
        raise ValueError('Importo non valido.')
    return number


def money(value):
    return format(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f').replace('.', ',')


def receipt_xml(order, config, payment):
    config = validate_config(config)
    if config.get('brand') != 'epson':
        raise ValueError('Tracciato disponibile soltanto per Epson ePOS Fiscal.')
    if payment not in ('contanti', 'carta'):
        raise ValueError('Scegliere contanti o carta.')
    rows = order.get('prodotti', [])
    if not rows:
        raise ValueError('Ordine senza articoli.')
    root = Element('printerFiscalReceipt')
    operator = str(config['operator'])
    SubElement(root, 'beginFiscalReceipt', operator=operator)
    total = Decimal(0)
    for row in rows:
        department = config['departments'].get(str(row.get('id_categoria')))
        if department is None:
            raise ValueError('Configurare il reparto fiscale per: ' + str(row.get('categoria') or row.get('nome')))
        qty, line = amount(row['quantita']), amount(row['totale'])
        if qty <= 0 or qty != qty.quantize(Decimal('.001')) or line != line.quantize(Decimal('.01')):
            raise ValueError('Quantità o totale riga non rappresentabile.')
        unit = (line / qty).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if (unit * qty).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) != line:
            raise ValueError('Arrotondamento da verificare prima dell’emissione: ' + str(row['nome']))
        name = str(row['nome']).strip()
        if not name or any(ord(c) < 32 for c in name):
            raise ValueError('Descrizione articolo non valida.')
        SubElement(root, 'printRecItem', operator=operator, description=name[:38],
                   quantity=format(qty, 'f').replace('.', ','), unitPrice=money(unit),
                   department=str(department), justification='1')
        total += line
    if total != amount(order['totale']) or total <= 0:
        raise ValueError('Il totale ordine non coincide con le righe.')
    SubElement(root, 'printRecTotal', operator=operator, description=payment.upper(),
               payment=money(total), paymentType='0' if payment == 'contanti' else '2',
               index='1', justification='1')
    SubElement(root, 'endFiscalReceipt', operator=operator)
    return tostring(root, encoding='unicode')
