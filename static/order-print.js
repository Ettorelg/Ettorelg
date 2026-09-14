window.AlphaOrderPrint = (() => {
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
      if (!healthResponse.ok || health.version !== 5) throw new Error('Aggiorna il programma di stampa sul PC e riavvialo per usare il nuovo formato degli ordini.');
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
      window.alert(message);
      return false;
    }
  }
  return {direct};
})();
