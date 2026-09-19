(() => {
  const header = document.querySelector('header');
  if (!header || !window.AlphaOrderPrint) return;
  const control = document.createElement('button');
  control.type = 'button';
  control.className = 'order-notification-button';
  control.textContent = '🖨 Attiva stampa automatica';
  control.style.margin = '0 8px';
  control.setAttribute('aria-pressed', 'false');
  const indicator = document.createElement('span');
  indicator.setAttribute('role', 'status');
  indicator.style.cssText = 'font-size:.82rem;color:#c9dcff;max-width:260px';
  const target = header.querySelector('.toplinks') || header;
  target.prepend(indicator);
  target.prepend(control);

  let storageKey;
  let busy = false;
  const read = () => {
    try { return JSON.parse(localStorage.getItem(storageKey) || '{}'); }
    catch (_) { return {}; }
  };
  const save = state => localStorage.setItem(storageKey, JSON.stringify(state));
  const refresh = () => {
    const enabled = read().enabled === true;
    control.textContent = enabled ? '🖨 Stampa automatica attiva · disattiva' : '🖨 Attiva stampa automatica';
    control.setAttribute('aria-pressed', String(enabled));
  };
  const notifications = async cursor => {
    const url = '/api/ordini/notifiche' + (cursor === undefined ? '' : '?dopo=' + encodeURIComponent(cursor));
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok) throw new Error('Impossibile controllare i nuovi ordini.');
    return response.json();
  };

  async function pollLocked() {
    if (!read().enabled) return;
    let state = read();
    if (!Number.isSafeInteger(state.cursor) || state.cursor < 0) {
      const baseline = await notifications();
      save({enabled: true, cursor: Number(baseline.ultimo_id)});
      return;
    }
    const update = await notifications(state.cursor);
    for (const item of update.nuovi || []) {
      state = read();
      if (!state.enabled) return;
      if (item.id <= state.cursor) continue;
      const response = await fetch('/api/ordini/' + encodeURIComponent(item.id) + '/stampa', {cache: 'no-store'});
      if (!response.ok) throw new Error('Impossibile leggere l’ordine #' + item.id + '.');
      const {ordine} = await response.json();
      const printed = await AlphaOrderPrint.direct(ordine, {quiet: true, automatic: true});
      save({enabled: true, cursor: item.id});
      const visibleNumber = ordine.numero || item.id;
      indicator.textContent = printed ? 'Stampato automaticamente ordine #' + visibleNumber : 'Ordine #' + visibleNumber + ': nessuna stampante assegnata alle sue categorie.';
    }
  }

  async function poll() {
    if (busy || !read().enabled) return;
    busy = true;
    try {
      if (!navigator.locks?.request) {
        indicator.textContent = 'Stampa automatica disponibile con Chrome o Edge aggiornato.';
        return;
      }
      await navigator.locks.request('alpha-menu-auto-print-' + storageKey, {ifAvailable: true}, async lock => {
        if (lock) await pollLocked();
      });
    } catch (error) {
      indicator.textContent = 'Stampa automatica sospesa: ' + (error.message || 'errore') + ' Riprovo tra 5 secondi.';
    } finally { busy = false; }
  }

  control.addEventListener('click', async () => {
    control.disabled = true;
    try {
      if (read().enabled) {
        save({enabled: false, cursor: read().cursor});
        indicator.textContent = 'Stampa automatica disattivata su questo PC.';
      } else {
        const response = await fetch('/api/ordini/stampanti', {cache: 'no-store'});
        const config = await response.json();
        if (!response.ok || (!config.stampante_ip && !config.stampante_riepilogo_ip && !(config.categorie || []).length && !(config.stampanti || []).length)) throw new Error('Configura prima una stampante generale, di riepilogo o per categoria.');
        const baseline = await notifications();
        save({enabled: true, cursor: Number(baseline.ultimo_id)});
        indicator.textContent = 'Attiva: stamperò i nuovi ordini da adesso. Lascia aperta questa pagina e il programma sul PC.';
      }
      refresh();
    } catch (error) { indicator.textContent = error.message || 'Attivazione non riuscita.'; }
    finally { control.disabled = false; }
  });

  fetch('/api/ordini/configurazione', {cache: 'no-store'}).then(response => response.json()).then(config => {
    storageKey = 'alpha-menu-auto-print-' + config.shop_id;
    refresh();
    if (read().enabled) indicator.textContent = 'Stampa automatica attiva su questo PC.';
    poll();
    setInterval(poll, 5000);
  }).catch(() => {
    control.disabled = true;
    indicator.textContent = 'Stampa automatica non disponibile.';
  });
})();
