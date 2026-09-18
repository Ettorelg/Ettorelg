"""Epson FP-81II RT transport. Never retry an emission after claiming it."""
import http.client
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import threading
import urllib.request
import uuid
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

BASE = 'https://menu.alphasystemsrl.it'
LOCK = threading.Lock()
LEDGER = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'AlphaMenu' / 'fiscal-jobs.sqlite3'


def _response(xml, command):
    if not isinstance(xml, str) or len(xml) > 65536 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('Risposta Epson non valida.')
    root = fromstring(xml)
    response = next((node for node in root.iter() if node.tag.split('}')[-1] == 'response'), None)
    if response is None or response.get('success') != 'true':
        if response is None:
            raise ValueError('Epson: risposta non riconosciuta')
        code = response.get('code', 'risposta non riconosciuta')
        status = response.get('status', '')
        explanations = {
            '17': 'operazione non consentita nello stato attuale; per IVA e intestazione la giornata fiscale deve essere chiusa',
            '21': 'uno dei dati inviati non è ammesso dal registratore',
        }
        detail = explanations.get(status)
        suffix = (f' · codice {status}' if status else '') + (f' · {detail}' if detail else '')
        raise ValueError('Epson: ' + code + suffix)
    info = {node.tag.split('}')[-1]: (node.text or '') for node in response.iter()}
    if info.get('responseCommand') != command:
        raise ValueError('Risposta Epson non corrispondente al comando richiesto.')
    return info.get('responseData', '')


def direct(config, command, data):
    if not command.isdigit() or len(command) != 4 or not isinstance(data, str) or len(data) > 128:
        raise ValueError('Comando Epson non valido.')
    xml = send(config, f'<printerCommand><directIO command="{command}" data="{data}" /></printerCommand>')
    return _response(xml, command)


def _number(value, low, high, width):
    if isinstance(value, bool) or not str(value).isdigit() or not low <= int(value) <= high:
        raise ValueError(f'Valore Epson consentito: {low}-{high}.')
    return str(int(value)).zfill(width)


def _text(value, width):
    value = str(value or '').strip()
    if len(value) > width or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError(f'Testo Epson non valido (massimo {width} caratteri senza accenti).')
    return value.ljust(width)


def _header_text(value, centered=False):
    value = str(value or '').strip()
    if len(value) > 40 or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError('Testo Epson non valido (massimo 40 caratteri senza accenti).')
    return value.center(40) if centered else value.ljust(40)


def read_programming(config, batch=None):
    batch = batch if isinstance(batch, dict) else {}
    start = int(batch.get('start', 1))
    count = int(batch.get('count', 25))
    details = batch.get('details', True) is True
    if not 1 <= start <= 99 or not 1 <= count <= 25:
        raise ValueError('Blocco di lettura Epson non valido.')
    departments = []
    used_vat = set()
    for number in range(start, min(100, start + count)):
        raw = direct(config, '4202', f'{number:02d}')
        if len(raw) < 67:
            raise ValueError('Risposta reparto Epson incompleta.')
        row = dict(number=number, description=raw[2:22].rstrip(), price1=raw[22:31], price2=raw[31:40],
                   price3=raw[40:49], single=raw[49], vat_group=raw[50:52], price_limit=raw[52:61],
                   print_group=raw[61:63], product_group=raw[63:65], unit=raw[65:67].rstrip())
        # Empty factory departments are omitted but remain addressable when creating a new row.
        if row['description'].strip() or any(raw[22:49].strip('0 ')):
            departments.append(row)
            if row['vat_group'] not in ('00',) and int(row['vat_group']) not in range(10, 20):
                used_vat.add(row['vat_group'])
    vat = []
    for group in (sorted(used_vat | {f'{n:02d}' for n in range(1, 10)}) if details else []):
        try:
            raw = direct(config, '4205', group)
            vat.append(dict(group=group, rate=raw[2:6]))
        except ValueError:
            if group in used_vat:
                raise
    headers = []
    for line in (range(1, 17) if details else []):
        raw = direct(config, '3216', f'{line:02d}')
        text = raw[2:42].rstrip()
        font = 1
        if line <= 9:
            font_raw = direct(config, '4216', str(line))
            if len(font_raw) < 2 or font_raw[0] != str(line) or font_raw[1] not in '1234':
                raise ValueError(f'Risposta formato intestazione riga {line} non valida.')
            font = int(font_raw[1])
        headers.append(dict(line=line, text=text.strip(), centered=bool(text[:1].isspace()), font=font))
    payments = []
    for index in (range(1, 6) if details else []):
        raw = direct(config, '4253', f'{index:02d}')
        payments.append(dict(index=index, description=raw[2:22].rstrip()))
    logo = {}
    for key, parameter in ([('header', 9), ('footer', 10), ('alignment', 22)] if details else []):
        raw = direct(config, '4215', f'{parameter:02d}')
        logo[key] = int(raw[2:5])
    serial = direct(config, '3217', _number(config.get('operator', 1), 1, 12, 2)) if details else ''
    return dict(departments=departments, vat=vat, headers=headers, payments=payments, logo=logo,
                printer=dict(serial=serial.strip(), model=config.get('model'), ip=config.get('ip')))


def write_programming(config, payload):
    if not isinstance(payload, dict) or payload.get('confirmation') != 'SCRIVI CONFIGURAZIONE EPSON':
        raise ValueError('Conferma scrittura Epson non valida.')
    sections = payload.get('sections')
    data = payload.get('data')
    if not isinstance(sections, list) or not isinstance(data, dict) or not sections or len(sections) > 5:
        raise ValueError('Selezionare almeno una sezione valida.')
    allowed = {'departments', 'vat', 'headers', 'payments', 'logo'}
    if set(sections) - allowed:
        raise ValueError('Sezione Epson non valida.')
    written = []
    with LOCK:
        if 'vat' in sections:
            for row in data.get('vat', []):
                command = _number(row.get('group'), 1, 59, 2) + _number(row.get('rate'), 1, 9999, 4)
                try:
                    direct(config, '4005', command)
                except ValueError as exc:
                    raise ValueError(f'Aliquota IVA gruppo {row.get("group")}: {exc}') from exc
            written.append('IVA')
        if 'departments' in sections:
            for row in data.get('departments', []):
                command = (_number(row.get('number'), 1, 99, 2) + _text(row.get('description'), 20) +
                           _number(row.get('price1', 0), 0, 999999999, 9) + _number(row.get('price2', 0), 0, 999999999, 9) +
                           _number(row.get('price3', 0), 0, 999999999, 9) + _number(row.get('single', 0), 0, 1, 1) +
                           _number(row.get('vat_group'), 0, 59, 2) + _number(row.get('price_limit', 0), 0, 999999999, 9) +
                           _number(row.get('print_group', 0), 0, 10, 2) + _number(row.get('product_group', 0), 0, 10, 2) +
                           _text(row.get('unit'), 2))
                try:
                    direct(config, '4002', command)
                except ValueError as exc:
                    raise ValueError(f'Reparto {row.get("number")}: {exc}') from exc
            written.append('reparti')
        if 'payments' in sections:
            for row in data.get('payments', []):
                try:
                    direct(config, '4053', _number(row.get('index'), 1, 5, 2) + _text(row.get('description'), 20))
                except ValueError as exc:
                    raise ValueError(f'Pagamento contanti {row.get("index")}: {exc}') from exc
            written.append('pagamenti')
        if 'headers' in sections:
            for row in data.get('headers', []):
                try:
                    direct(config, '3016', _number(row.get('line'), 1, 16, 2) +
                           _header_text(row.get('text'), row.get('centered') is True))
                except ValueError as exc:
                    raise ValueError(f'Intestazione riga {row.get("line")}: {exc}. Esegui prima la chiusura giornaliera.') from exc
            try:
                direct(config, '3016', '99' + (' ' * 40))
            except ValueError as exc:
                raise ValueError(f'Conferma intestazione: {exc}. Esegui prima la chiusura giornaliera.') from exc
            for row in data.get('headers', []):
                line = int(row.get('line', 0))
                if line <= 9:
                    try:
                        direct(config, '4016', _number(line, 1, 9, 1) + _number(row.get('font', 1), 1, 4, 1))
                    except ValueError as exc:
                        raise ValueError(f'Formato intestazione riga {line}: {exc}') from exc
            written.append('intestazione')
        if 'logo' in sections:
            logo = data.get('logo', {})
            for parameter, key, maximum in [(9, 'header', 9), (10, 'footer', 9), (22, 'alignment', 2)]:
                try:
                    direct(config, '4015', f'{parameter:02d}' + _number(logo.get(key, 0), 0, maximum, 3))
                except ValueError as exc:
                    raise ValueError(f'Logo parametro {parameter}: {exc}') from exc
            written.append('logo')
    return dict(ok=True, message='Configurazione inviata: ' + ', '.join(written) + '.', sections=written)


def cloud(path, data, secret):
    req = urllib.request.Request(BASE + '/api/fiscale/' + path, data=json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def send(config, xml):
    ip = ipaddress.IPv4Address(config['ip'])
    if not any(ip in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Indirizzo registratore non valido.')
    if config.get('brand') != 'epson' or config.get('model') != 'FP-81II RT':
        raise ValueError('Modello non supportato per il collaudo.')
    port = int(config.get('port', 80))
    if not 1 <= port <= 65535:
        raise ValueError('Porta non valida.')
    root = Element('{http://schemas.xmlsoap.org/soap/envelope/}Envelope')
    SubElement(root, '{http://schemas.xmlsoap.org/soap/envelope/}Body').append(fromstring(xml))
    connection = http.client.HTTPConnection(str(ip), port, timeout=35)
    try:
        connection.request('POST', '/cgi-bin/fpmate.cgi?devid=local_printer&timeout=10000',
                           body=tostring(root, encoding='utf-8', xml_declaration=True),
                           headers={'Content-Type': 'text/xml; charset=utf-8', 'If-Modified-Since': 'Thu, 01 Jan 1970 00:00:00 GMT'})
        response = connection.getresponse()
        content = response.read(65537)
        if response.status != 200 or len(content) > 65536:
            raise ValueError('Risposta HTTP Epson non valida. Verificare il registratore.')
        return content.decode('utf-8-sig')
    finally:
        connection.close()


def dispatch(path, data):
    if path in ('/fiscal/config/read', '/fiscal/config/write'):
        config = data.get('config', {})
        return (read_programming(config, data.get('batch')) if path.endswith('/read')
                else write_programming(config, data.get('programming', {})))
    if path == '/fiscal/probe':
        config = data.get('config', {})
        operator = int(config.get('operator', 1))
        if not 1 <= operator <= 12:
            raise ValueError('Operatore non valido.')
        xml = send(config, f'<printerCommand><queryPrinterStatus operator="{operator}" statusType="0" /></printerCommand>')
        if '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
            raise ValueError('Risposta non valida.')
        root = fromstring(xml)
        response = next((el for el in root.iter() if el.tag.split('}')[-1] == 'response'), None)
        if response is None or response.get('success') != 'true':
            raise ValueError('Epson non pronto: ' + (response.get('code', '') if response is not None else 'risposta non riconosciuta'))
        return dict(ok=True, message='Epson raggiungibile. Nessun documento emesso.', xml=xml)
    if path not in ('/fiscal/emit', '/fiscal/recover'):
        raise ValueError('Operazione fiscale non valida.')
    job_id = str(uuid.UUID(data.get('id', '')))
    secret = data.get('secret', '')
    if not isinstance(secret, str) or not 40 <= len(secret) <= 100:
        raise ValueError('Autorizzazione non valida.')
    with LOCK:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(LEDGER) as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, result TEXT)')
            row = db.execute('SELECT result FROM jobs WHERE id=?', (job_id,)).fetchone()
            if row:
                if not row[0]:
                    raise ValueError('Emissione interrotta: verificare il registratore. Reinvio bloccato.')
                result = json.loads(row[0])
            elif path == '/fiscal/recover':
                raise ValueError('Nessun esito su questo PC. Usa il PC di emissione e verifica il registratore; non reinviare l’ordine.')
            else:
                # Server acquisition is atomic across PCs. Then persist locally
                # before any printer I/O. A lost claim response never triggers I/O.
                job = cloud('lavoro', {'id': job_id}, secret)
                db.execute('INSERT INTO jobs(id) VALUES (?)', (job_id,))
                db.commit()
                result = {'id': job_id}
                try:
                    result['xml'] = send(job['config'], job['xml'])
                except Exception as exc:
                    result['error'] = str(exc)
                db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(result), job_id))
                db.commit()
            # Only reporting the saved response is retryable, never sending the sale.
            return cloud('esito', result, secret)
