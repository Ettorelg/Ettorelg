"""Fiscal configuration and offline Epson ePOS receipt preparation.

Reference: Epson ePOS Fiscal Print Solution Development Guide Rev T, §5.4, §11.1.
No network transmission or fiscal completion is performed by this module.
"""
import ipaddress
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from xml.etree.ElementTree import Element, SubElement, tostring, fromstring

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
                cash_index=integer(data.get('cash_index', 1), 0, 5),
                card_index=integer(data.get('card_index', 1), 1, 10),
                status=('live' if data.get('live') is True and data.get('verified') is True
                        and data['model'] == 'FP-81II RT' else 'preview') if brand == 'epson' else 'unsupported',
                live=data.get('live') is True and data.get('verified') is True and brand == 'epson' and data['model'] == 'FP-81II RT',
                verified=data.get('verified') is True)


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


def payment_totals(order, data):
    if not isinstance(data, dict) or data.get('payment') not in ('contanti', 'carta'):
        raise ValueError('Scegli contanti o carta.')
    total = amount(order['totale'])
    discount = amount(data.get('discount', '0'))
    if discount != discount.quantize(Decimal('.01')):
        raise ValueError('Usa al massimo due decimali.')
    kind = data.get('discount_type', 'euro')
    if kind not in ('euro', 'percent') or (kind == 'percent' and discount >= 100):
        raise ValueError('Sconto non valido.')
    reduction = (total * discount / 100 if kind == 'percent' else discount).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    due = total - reduction
    if due <= 0 or due != due.quantize(Decimal('.01')):
        raise ValueError('Lo sconto deve essere inferiore al totale.')
    tendered = amount(data.get('tendered') or due) if data['payment'] == 'contanti' else due
    if tendered < due or tendered > Decimal('999999.99') or tendered != tendered.quantize(Decimal('.01')):
        raise ValueError('Importo ricevuto insufficiente o non valido.')
    return dict(payment=data['payment'], gross=str(total), discount=str(reduction),
                due=str(due), tendered=str(tendered), change=str(tendered-due))


def receipt_xml(order, config, payment, settlement=None):
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
    if settlement:
        reduction = amount(settlement['discount'])
        if reduction:
            SubElement(root, 'printRecSubtotalAdjustment', operator=operator,
                       adjustmentType='1', description='SCONTO', amount=money(reduction), justification='1')
        total = amount(settlement['tendered']) if payment == 'contanti' else amount(settlement['due'])
    SubElement(root, 'printRecTotal', operator=operator, description=payment.upper(),
               payment=money(total), paymentType='0' if payment == 'contanti' else '2',
               index=str(config['cash_index'] if payment == 'contanti' else config['card_index']), justification='1')
    SubElement(root, 'endFiscalReceipt', operator=operator)
    return tostring(root, encoding='unicode')


def parse_fiscal_response(xml, expected=None):
    if not isinstance(xml, str) or len(xml) > 65536 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('Risposta Epson non valida.')
    try:
        root = fromstring(xml)
    except Exception as exc:
        raise ValueError('Risposta Epson illeggibile: verificare il documento sul registratore.') from exc
    response = next((el for el in root.iter() if el.tag.split('}')[-1] == 'response'), None)
    if response is None or response.get('success') != 'true':
        raise ValueError('Epson: ' + (response.get('code', 'errore') if response is not None else 'risposta mancante') + '. Verificare il registratore prima di riprovare.')
    info = {el.tag.split('}')[-1]: (el.text or '').strip() for el in response.iter()}
    if expected is not None:
        if not info.get('fiscalReceiptNumber', '').isdigit() or not info.get('fiscalReceiptDate'):
            raise ValueError('Epson non ha confermato il numero e la data del documento.')
        if amount(info.get('fiscalReceiptAmount', '').replace(',', '.')) != amount(expected):
            raise ValueError('Totale fiscale diverso dal pagamento. Verificare il registratore.')
    return {key: value for key, value in info.items() if key not in ('response', 'addInfo', 'elementList')}
