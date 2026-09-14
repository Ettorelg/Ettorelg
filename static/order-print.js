window.AlphaOrderPrint = (() => {
  const amount = value => Number(value || 0).toLocaleString('it-IT', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const quantity = value => Number(value || 0).toLocaleString('it-IT', {maximumFractionDigits: 3});
  function add(parent, tag, value, className) {
    const element = parent.ownerDocument.createElement(tag);
    if (value !== undefined && value !== null) element.textContent = String(value);
    if (className) element.className = className;
    parent.appendChild(element);
    return element;
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
      if (product.totale !== undefined) add(row, 'span', '€ ' + amount(product.totale));
    }
    add(body, 'div', undefined, 'line');
    if (order.note) add(body, 'div', 'NOTE: ' + order.note, 'note');
    if (order.totale !== undefined) add(body, 'div', 'Totale richiesto: € ' + amount(order.totale), 'total');
    add(body, 'p', 'Promemoria ordine · non è uno scontrino fiscale', 'small center');
    const actions = add(body, 'div', undefined, 'print-actions');
    const button = add(actions, 'button', 'Stampa');
    button.type = 'button';
    button.onclick = () => paper.print();
    paper.focus();
    setTimeout(() => { if (!paper.closed) paper.print(); }, 250);
    return true;
  }
  return {print};
})();
