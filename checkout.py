"""Authoritative payment preparation and durable fiscal-attempt tracking."""
import hmac
import json
import secrets
import uuid
from flask import jsonify, request, session
from fiscal_printers import payment_totals, receipt_xml, parse_fiscal_response


def register_checkout(app, connect, shop_id_for_request, read_order, read_config):
    def context(order_id):
        shop = shop_id_for_request()
        if not shop:
            return None, None, None, (jsonify(error='Accesso richiesto.'), 403)
        result = read_order(order_id)
        if isinstance(result, tuple):
            return None, None, None, result
        configuration = read_config()
        if isinstance(configuration, tuple):
            return None, None, None, configuration
        return shop, result.get_json()['ordine'], configuration.get_json()['config'], None

    @app.get('/api/ordini/<int:order_id>/pagamento')
    def api_pagamento_info(order_id):
        shop, order, config, error = context(order_id)
        if error:
            return error
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id,stato,riepilogo,risposta FROM pagamenti_ordini WHERE id_ordine=%s AND id_negozio=%s AND stato<>'non_emesso'", (order_id, shop))
                row = cur.fetchone()
            return jsonify(ordine=order, config=config, can_resolve=not bool(session.get('employee_id')), pagamento=dict(id=row[0], stato=row[1], riepilogo=row[2], risposta=row[3]) if row else None)
        finally:
            conn.close()

    @app.post('/api/ordini/<int:order_id>/pagamento')
    def api_pagamento_prepara(order_id):
        shop, order, config, error = context(order_id)
        if error:
            return error
        data = request.get_json(silent=True) or {}
        try:
            totals = payment_totals(order, data)
            xml = receipt_xml(order, config, totals['payment'], totals) if config.get('brand') else None
            action = data.get('action', 'preview')
            if action not in ('preview', 'confirm'):
                raise ValueError('Operazione non valida.')
            if action == 'preview':
                return jsonify(totals=totals, xml=xml, mode=config.get('status', 'none'))
            if config.get('brand') and not (config.get('status') == 'live' and config.get('model') == 'FP-81II RT'):
                raise ValueError('Modalità prova: nessun documento viene emesso. Abilita la modalità reale nella dashboard dopo il collaudo.')
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(error=str(exc)), 400
        conn = connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT stato FROM ordini_menu WHERE id=%s AND id_negozio=%s FOR UPDATE', (order_id, shop))
                    row = cur.fetchone()
                    if not row or row[0] not in ('da_evadere', 'in_lavorazione'):
                        return jsonify(error='Ordine già evaso, annullato o non disponibile.'), 409
                    cur.execute("SELECT id FROM pagamenti_ordini WHERE id_ordine=%s AND stato<>'non_emesso'", (order_id,))
                    if cur.fetchone():
                        return jsonify(error='Esiste già un pagamento o un tentativo fiscale. Recupera l’esito: non verrà inviata una seconda emissione.'), 409
                    attempt, secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
                    payload = dict(id=attempt, secret=secret, config=config, xml=xml, expected=totals['due'])
                    state = 'in_attesa' if xml else 'registrato'
                    cur.execute('INSERT INTO pagamenti_ordini (id,id_ordine,id_negozio,stato,riepilogo,payload,segreto) VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)',
                                (attempt, order_id, shop, state, json.dumps(totals), json.dumps(payload), secret))
                    if not xml:
                        cur.execute("UPDATE ordini_menu SET stato='evaso',aggiornato_il=NOW() WHERE id=%s AND id_negozio=%s", (order_id, shop))
            return jsonify(id=attempt, totals=totals, state=state, job=payload if xml else None)
        finally:
            conn.close()

    @app.post('/api/ordini/<int:order_id>/pagamento/recupera')
    def api_pagamento_recupera(order_id):
        shop = shop_id_for_request()
        if not shop:
            return jsonify(error='Accesso richiesto.'), 403
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id,segreto,stato FROM pagamenti_ordini WHERE id_ordine=%s AND id_negozio=%s AND stato<>'non_emesso'", (order_id, shop))
                row = cur.fetchone()
            if not row:
                return jsonify(error='Nessun tentativo da recuperare.'), 404
            return jsonify(id=row[0], secret=row[1], state=row[2])
        finally:
            conn.close()

    @app.post('/api/ordini/<int:order_id>/pagamento/verifica-non-emesso')
    def api_pagamento_verifica(order_id):
        shop = shop_id_for_request()
        if not shop or session.get('employee_id'):
            return jsonify(error='Verifica riservata al titolare.'), 403
        data = request.get_json(silent=True) or {}
        note = str(data.get('note', '')).strip()
        if data.get('confirmation') != 'CONFERMO NON EMESSO' or not 12 <= len(note) <= 500:
            return jsonify(error='Conferma la mancata emissione e descrivi la verifica fatta sul registratore.'), 400
        conn = connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id,stato,aggiornato_il<NOW()-INTERVAL '5 minutes' FROM pagamenti_ordini WHERE id_ordine=%s AND id_negozio=%s AND stato<>'non_emesso' FOR UPDATE", (order_id, shop))
                    row = cur.fetchone()
                    if not row or row[1] in ('emesso', 'registrato'):
                        return jsonify(error='Pagamento concluso: non è possibile sbloccarlo da qui.'), 409
                    if row[1] != 'in_attesa' and not row[2]:
                        return jsonify(error='Attendi almeno cinque minuti e verifica il registratore: l’operazione potrebbe essere ancora in corso.'), 409
                    cur.execute("UPDATE pagamenti_ordini SET stato='non_emesso',risposta=risposta||%s::jsonb,aggiornato_il=NOW() WHERE id=%s", (json.dumps(dict(verifica_manuale=note, verificato_da=session.get('user_id'))), row[0]))
            return jsonify(ok=True)
        finally:
            conn.close()

    @app.post('/api/fiscale/lavoro')
    def api_fiscale_lavoro():
        data = request.get_json(silent=True) or {}
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not 40 <= len(token) <= 100:
            return jsonify(error='Accesso negato.'), 403
        conn = connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize claims for this shop. A pending/uncertain emission
                    # must be verified before any other sale reaches the RT.
                    cur.execute('SELECT id FROM negozi WHERE id=(SELECT id_negozio FROM pagamenti_ordini WHERE id=%s) FOR UPDATE', (str(data.get('id', '')),))
                    cur.execute('SELECT segreto,stato,payload FROM pagamenti_ordini WHERE id=%s FOR UPDATE', (str(data.get('id', '')),))
                    row = cur.fetchone()
                    if not row or not hmac.compare_digest(token, row[0]):
                        return jsonify(error='Accesso negato.'), 403
                    if row[1] != 'in_attesa':
                        return jsonify(error='Tentativo già preso in carico. Nessun reinvio consentito.'), 409
                    cur.execute("SELECT id FROM pagamenti_ordini WHERE id_negozio=(SELECT id_negozio FROM pagamenti_ordini WHERE id=%s) AND stato IN ('inviato','incerto') AND id<>%s LIMIT 1", (data['id'], data['id']))
                    if cur.fetchone():
                        return jsonify(error='Un’altra emissione è in corso o da verificare. Nessun invio effettuato.'), 409
                    cur.execute("UPDATE pagamenti_ordini SET stato='inviato',aggiornato_il=NOW() WHERE id=%s", (data['id'],))
            return jsonify(row[2])
        finally:
            conn.close()

    @app.post('/api/fiscale/esito')
    def api_fiscale_esito():
        # This one endpoint is authenticated by a per-attempt 256-bit capability,
        # not by browser cookies; the local bridge can report after browser closure.
        data = request.get_json(silent=True) or {}
        token = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if len(token) < 40 or len(token) > 100 or not isinstance(data.get('id'), str):
            return jsonify(error='Accesso negato.'), 403
        conn = connect()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT segreto,stato,riepilogo,id_ordine,id_negozio FROM pagamenti_ordini WHERE id=%s FOR UPDATE', (data['id'],))
                    row = cur.fetchone()
                    if not row or not hmac.compare_digest(token, row[0]):
                        return jsonify(error='Accesso negato.'), 403
                    if row[1] == 'emesso':
                        return jsonify(ok=True, state='emesso')
                    if row[1] not in ('inviato', 'incerto'):
                        return jsonify(error='Tentativo non inviato al registratore.'), 409
                    try:
                        info = parse_fiscal_response(data.get('xml'), row[2]['due'])
                        state = 'emesso'
                    except ValueError as exc:
                        info = dict(error=str(exc), dettaglio=str(data.get('error', ''))[:300])
                        state = 'incerto'
                    cur.execute('UPDATE pagamenti_ordini SET stato=%s,risposta=%s::jsonb,aggiornato_il=NOW() WHERE id=%s', (state, json.dumps(info), data['id']))
                    if state == 'emesso':
                        cur.execute("UPDATE ordini_menu SET stato='evaso',aggiornato_il=NOW() WHERE id=%s AND id_negozio=%s", (row[3], row[4]))
            return jsonify(ok=state == 'emesso', state=state, result=info)
        finally:
            conn.close()
