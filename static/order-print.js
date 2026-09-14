window.AlphaOrderPrint = (() => {
  const quantity = value => Number(value || 0).toLocaleString('it-IT', {maximumFractionDigits: 3});
  function add(parent, tag, value, className) {
    const element = parent.ownerDocument.createElement(tag);
    if (value !== undefined && value !== null) element.textContent = String(value);
    if (className) element.className = className;
    parent.appendChild(element);
    return element;
  }
  async function direct(order, {quiet = false, automatic = false} = {}) {
    try {
      const [routesResponse, orderResponse] = await Promise.all([
        fetch('/api/ordini/stampanti', {cache: 'no-store'}),
        fetch('/api/ordini/' + encodeURIComponent(order.id) + '/stampa', {cache: 'no-store'})
      ]);
      if (!routesResponse.ok || !orderResponse.ok) throw new Error('Impossibile leggere le stampanti o l’ordine.');
      const routes = await routesResponse.json();
      const currentOrder = (await orderResponse.json()).ordine;
      if (!routes.stampante_ip && !routes.stampante_riepilogo_ip && !(routes.categorie || []).length) throw new Error('Imposta una stampante generale, di riepilogo o per categoria.');
      const healthResponse = await fetch('http://127.0.0.1:17891/health', {signal: AbortSignal.timeout(5000)});
      const health = await healthResponse.json();
      if (!healthResponse.ok || health.version !== 4) throw new Error('Aggiorna il programma di stampa sul PC e riavvialo per usare il nuovo formato degli ordini.');
      const response = await fetch('http://127.0.0.1:17891/print', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({printer_ip: routes.stampante_ip, summary_ip: routes.stampante_riepilogo_ip, printers: routes.categorie, order: currentOrder, automatic}),
        signal: AbortSignal.timeout(30000)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'Stampa diretta non riuscita.');
      if (!quiet) window.alert(result.message || ('Ordine #' + order.id + ' inviato alla stampante.'));
      return result.printed || 0;
    } catch (error) {
      const message = (error instanceof TypeError || error.name === 'TimeoutError'
        ? 'Programma di stampa non raggiungibile sul PC. Avvia escpos_bridge.py e riprova.'
        : error.message);
      if (quiet) throw new Error(message);
      window.alert(message + '\nPuoi usare “Stampa dal browser” come alternativa.');
      return false;
    }
  }
  function print(order) {
    const paper = window.open('', '_blank', 'width=420,height=720');
    if (!paper) {
      window.alert('Consenti le finestre popup per stampare l’ordine.');
      return false;
    }
    const doc = paper.document;
    doc.title = 'Ordine ' + order.id + ' · Alpha Menu';
    const style = doc.createElement('style');
    style.textContent = '@page{size:80mm auto;margin:3mm}*{box-sizing:border-box}body{width:74mm;margin:0 auto;color:#000;background:#fff;font:12px/1.35 Arial,sans-serif}h1{font-size:19px;margin:0 0 4mm;text-align:center}.center{text-align:center}.line{border-top:1px dashed #000;margin:3mm 0}.item{display:flex;justify-content:space-between;gap:2mm;margin:2mm 0}.item span:first-child{flex:1;overflow-wrap:anywhere}.total{font-weight:bold;font-size:16px}.note{white-space:pre-wrap;overflow-wrap:anywhere}.small{font-size:10px}.print-actions{padding:15px;text-align:center}@media print{.print-actions{display:none}body{width:auto}}';
    doc.head.appendChild(style);
    const body = doc.body;
    add(body, 'h1', 'ORDINE #' + order.id);
    add(body, 'div', order.origine === 'tavolo' ? 'AL TAVOLO' : 'DA ASPORTO', 'center');
    add(body, 'div', 'Data: ' + (order.data_richiesta || '') + (order.ora_richiesta ? ' · Ore ' + order.ora_richiesta : ''));
    add(body, 'div', 'Ricevuto: ' + (order.creato_il || ''));
    add(body, 'div', undefined, 'line');
    if (order.nome) add(body, 'div', 'Cliente: ' + order.nome);
    if (order.riferimento) add(body, 'div', 'Riferimento/tavolo: ' + order.riferimento);
    if (order.telefono) add(body, 'div', 'Telefono: ' + order.telefono);
    add(body, 'div', undefined, 'line');
    for (const product of order.prodotti || []) {
      const row = add(body, 'div', undefined, 'item');
      add(row, 'span', quantity(product.quantita) + ' × ' + product.nome);
    }
    add(body, 'div', undefined, 'line');
    if (order.note) add(body, 'div', 'NOTE: ' + order.note, 'note');
    add(body, 'p', 'Promemoria ordine · non è uno scontrino fiscale', 'small center');
    const actions = add(body, 'div', undefined, 'print-actions');
    const button = add(actions, 'button', 'Stampa');
    button.type = 'button';
    button.onclick = () => paper.print();
    paper.focus();
    setTimeout(() => { if (!paper.closed) paper.print(); }, 250);
    return true;
  }
  return {print, direct};
})();
