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
