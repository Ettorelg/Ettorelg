"""Axon G100 HTTP transport. No sale retry, even after a timeout."""
import http.client
import ipaddress
import json
from decimal import Decimal
from urllib.parse import urlencode


def number(value, low, high):
    if isinstance(value, bool) or not str(value).isdigit() or not low <= int(value) <= high:
        raise ValueError(f'Valore Axon consentito: {low}-{high}.')
    return str(int(value))


def text(value, width):
    value = str(value)
    if len(value) > width or any(ord(c) < 32 or ord(c) > 126 or c == '/' for c in value):
        raise ValueError(f'Testo Axon: massimo {width} caratteri ASCII, senza /.')
    return value


def request(config, packet=None):
    ip = ipaddress.IPv4Address(config['ip'])
    if config.get('brand') != 'axon_micrelec' or config.get('model') != 'Dado RT / RT30' or not any(ip in ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('Configurazione Axon non valida.')
    port = int(number(config.get('port', 80), 1, 65535))
    connection = http.client.HTTPConnection(str(ip), port, timeout=15)
    try:
        query = {'cmd': 0} if packet is None else {'cmd': 4, 'js': 1, 'pkt': packet}
        connection.request('GET', '/_io?' + urlencode(query), headers={'Cache-Control': 'no-store'})
        response = connection.getresponse()
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError('Risposta HTTP Axon non valida.')
        return json.loads(raw)
    finally:
        connection.close()


def command(config, packet):
    result = request(config, packet)
    fields = result.get('response', '').split('/')
    if len(fields) < 4 or any(len(x) != 2 for x in fields[:3]):
        raise ValueError('Risposta Axon incompleta.')
    status = [int(x, 16) for x in fields[:3]]
    if status[0]:
        raise ValueError(f'Axon errore {fields[0]} · comando {packet.split("/")[0]}. Verificare lo stato del registratore; nessun reinvio automatico.')
    return fields[3:-1], status


def idle(config, closed=False):
    fields, status = command(config, 'X/')
    if not fields or fields[0] != '0' or status[1] & 0xB7 or status[2] & 0xFC:
        raise ValueError('Axon occupato, documento aperto o errore hardware. Verifica il registratore.')
    if closed and status[2] & 2:
        raise ValueError('Chiudi prima la giornata fiscale sul registratore. Nessuna configurazione inviata.')
    return fields


def read_programming(config, batch=None):
    batch = batch or {}
    start = int(number(batch.get('start', 1), 1, 99))
    count = int(number(batch.get('count', 25), 1, 25))
    info, _ = command(config, 'v/')
    if len(info) < 12:
        raise ValueError('Informazioni modello Axon incomplete.')
    limit = int(number(info[4], 1, 99))
    result = dict(departments=[], vat=[], payments=[], headers=[], logo={},
                  printer=dict(model=config['model'], ip=config['ip'], serial='', firmware=info[0]), department_count=limit)
    for n in range(start, min(start + count, limit + 1)):
        raw, _ = command(config, f':/{n}/')
        if len(raw) < 7:
            raise ValueError('Reparto Axon incompleto.')
        result['departments'].append(dict(number=n, description=raw[0].rstrip(), vat_group=raw[1], raw=raw))
    if not batch.get('details', True):
        return result
    identity, _ = command(config, 'a/')
    result['printer']['serial'] = identity[0]
    raw, _ = command(config, 'e/')
    if len(raw) != 36:
        raise ValueError('Tabella IVA Axon incompleta: scrittura disabilitata.')
    result['vat'] = [dict(group=str(i+1), rate=str(int(Decimal(raw[i])*100)), nature=raw[i+12], ateco=raw[i+24]) for i in range(12)]
    for n in range(1, int(number(info[5], 1, 20)) + 1):
        raw, _ = command(config, f'{{/{n}/')
        if len(raw) != 10:
            raise ValueError('Pagamento Axon incompleto.')
        result['payments'].append(dict(index=n, description=raw[0].rstrip(), type=raw[4], raw=raw))
    raw, _ = command(config, 'O/')
    if len(raw) != 25:
        raise ValueError('Intestazione Axon incompleta.')
    result['headers'] = [dict(line=i+1, text=raw[i*2].rstrip(), font=int(raw[i*2+1]), centered=False) for i in range(12)]
    return result


def write_programming(config, payload):
    if payload.get('confirmation') != 'SCRIVI CONFIGURAZIONE AXON':
        raise ValueError('Conferma Axon mancante.')
    sections, data = payload.get('sections'), payload.get('data', {})
    if not isinstance(sections, list) or not sections or set(sections) - {'departments', 'vat', 'payments', 'headers'}:
        raise ValueError('Sezioni Axon non valide.')
    packets = []
    # Validate the entire selection BEFORE the first write. Optional blank fields
    # preserve non-editable device settings, including differing read/write flags.
    if 'vat' in sections:
        rows = data['vat']
        if len(rows) != 12 or [str(r['group']) for r in rows] != [str(i) for i in range(1, 13)]:
            raise ValueError('Leggere prima tutta la tabella IVA.')
        rates = [format(Decimal(number(r['rate'], 0, 9999))/100, '.2f') for r in rows]
        fields = rates + [number(r['nature'], 0, 6) if Decimal(rates[i]) == 0 else '' for i, r in enumerate(rows)] + [number(r['ateco'], 0, 12) for r in rows]
        packets.append(('IVA', 'b/' + '/'.join(fields) + '/'))
    if 'departments' in sections:
        for row in data['departments']:
            fields = ['N', number(row['number'], 1, 99), text(row['description'], 30), number(row['vat_group'], 1, 12)] + ['']*6
            packets.append(('Reparto '+str(row['number']), '/'.join(fields)+'/'))
    if 'payments' in sections:
        for row in data['payments']:
            fields = ['E', number(row['index'], 1, 20), text(row['description'], 25), '', '1.00', '', number(row['type'], 0, 3), '', '', '']
            packets.append(('Pagamento '+str(row['index']), '/'.join(fields)+'/'))
    if 'headers' in sections:
        rows = data['headers']
        if len(rows) != 12 or [r['line'] for r in rows] != list(range(1, 13)):
            raise ValueError('Intestazione Axon incompleta.')
        last = max((i for i,r in enumerate(rows) if r['text'].strip()), default=-1)
        if last < 0:
            raise ValueError('Intestazione vuota non consentita.')
        fields = ['L']
        for row in rows[:last+1]:
            font = number(row['font'], 0, 4)
            value = text(row['text'], 48)
            if row.get('centered'):
                value = value.strip()
            if font in ('2', '3') and len(value.strip()) > 24:
                raise ValueError('Doppia larghezza: massimo 24 caratteri.')
            fields += [font, value or ' ', '0' if row.get('centered') else '1']
        packets.append(('Intestazione', '/'.join(fields)+'/'))
    idle(config, closed=True)
    written = []
    for label, packet in packets:
        try:
            command(config, packet)
            written.append(label)
        except Exception as exc:
            raise ValueError(f'{label}: {exc}. Comandi già confermati: {len(written)}. Rileggi prima di riprovare.') from exc
    return dict(ok=True, message='Configurazione Axon inviata. Rileggi per verificare.', sections=sections)


def emit(config, sale):
    before = idle(config)
    pay, _ = command(config, '{/'+number(sale['payment_index'], 1, 20)+'/')
    expected_type = '0' if sale['payment'] == 'contanti' else '1'
    if len(pay) != 10 or pay[4] != expected_type or not pay[6].startswith('10') or pay[3] != '0' or pay[9] != '0':
        raise ValueError('Totalizzatore Axon incompatibile: scegli un pagamento attivo contanti/carta, senza POS integrato/offline o sconti automatici.')
    for dept in sale['departments']:
        raw, _ = command(config, f':/{dept}/')
        if len(raw) < 7 or not raw[6].startswith('110') or raw[6][-1:] != '0':
            raise ValueError(f'Reparto {dept}: richiede prezzo libero, senza chiusura automatica/omaggio.')
    for packet in sale['commands']:
        command(config, packet)
    after = idle(config)
    snapshot = request(config)
    last = snapshot['ej']['lastdoc']
    if int(after[1]) != int(before[1])+1 or int(last['docno']) != int(after[1]):
        raise ValueError('Numero documento Axon non confermato. Verifica manuale richiesta; non reinviare.')
    totals, _ = command(config, f'0/4/{int(last["zno"])}/{int(last["docno"])}/')
    if len(totals) != 31:
        raise ValueError('Totali documento Axon incompleti. Non reinviare.')
    total = sum(Decimal(x) for x in totals[2:14])
    if total != Decimal(sale['due']):
        raise ValueError('Totale Axon diverso dal pagamento. Non reinviare.')
    return dict(brand='axon_micrelec', before=int(before[1]), number=int(after[1]),
                zno=int(last['zno']), date=snapshot['rtc']['date'], serial=snapshot['device']['serial'], amount=str(total))
