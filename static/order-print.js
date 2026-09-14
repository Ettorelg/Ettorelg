window.AlphaOrderPrint = (() => {
  function chooseMode(orderId, hasSummary) {
    return new Promise(resolve => {
      const dialog = document.createElement('dialog');
      dialog.setAttribute('aria-label', 'Scegli dove stampare l’ordine #' + orderId);
      dialog.style.cssText = 'max-width:min(420px,calc(100vw - 32px));padding:22px;border:1px solid #526987;border-radius:16px;background:#18263b;color:#fff;box-shadow:0 25px 70px #0009;font:16px system-ui';
      const title = document.createElement('h2');
      title.textContent = 'Stampa ordine #' + orderId;
      title.style.margin = '0 0 12px';
      dialog.append(title);
      const hint = document.createElement('p');
      hint.textContent = 'Scegli le stampanti a cui inviare questo ordine.';
      dialog.append(hint);
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:9px;flex-wrap:wrap';
      for (const [mode, label, disabled] of [
        ['summary', 'Solo riepilogo', !hasSummary],
        ['all', 'Tutte le stampanti', false],
        [null, 'Annulla', false]
      ]) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = label;
        button.disabled = disabled;
        button.title = disabled ? 'Configura prima la stampante di riepilogo nelle impostazioni Ordini.' : '';
        button.style.cssText = 'padding:10px 12px;border:1px solid #7797bd;border-radius:9px;background:#25486d;color:#fff;font:inherit;cursor:pointer';
        button.onclick = () => dialog.close(mode || 'cancel');
        actions.append(button);
      }
      dialog.append(actions);
      dialog.addEventListener('close', () => { const mode = dialog.returnValue; dialog.remove(); resolve(mode === 'summary' || mode === 'all' ? mode : null); }, {once: true});
      document.body.append(dialog);
      dialog.showModal();
    });
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
      const mode = automatic || quiet ? 'all' : await chooseMode(order.id, Boolean(routes.stampante_riepilogo_ip));
      if (!mode) return false;
      const healthResponse = await fetch('http://127.0.0.1:17891/health', {signal: AbortSignal.timeout(5000)});
      const health = await healthResponse.json();
      if (!healthResponse.ok || health.version !== 7) throw new Error('Aggiorna il programma di stampa sul PC e riavvialo per usare il nuovo formato degli scontrini.');
      const response = await fetch('http://127.0.0.1:17891/print', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({printer_ip: routes.stampante_ip, summary_ip: routes.stampante_riepilogo_ip, printers: routes.categorie, order: currentOrder, automatic, mode}),
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
