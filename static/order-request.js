/* Shared order delivery and live pickup availability. */
window.AlphaOrders = (() => {
  const pending = new Map();
  async function send(url, options) {
    const signature = url + '\n' + options.body;
    const storageKey = 'alpha-order-pending';
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch (_) {}
    let key = pending.get(signature) || (saved?.signature === signature ? saved.key : null);
    if (!key) key = crypto.randomUUID();
    pending.set(signature, key);
    try { sessionStorage.setItem(storageKey, JSON.stringify({signature, key})); } catch (_) {}
    const response = await fetch(url, {...options, headers: {...options.headers, 'Idempotency-Key': key}});
    // Keep the key if the response body is interrupted, so retrying is safe.
    const body = await response.clone().json();
    if (response.ok && body.ok) {
      pending.delete(signature);
      try { if (JSON.parse(sessionStorage.getItem(storageKey) || 'null')?.key === key) sessionStorage.removeItem(storageKey); } catch (_) {}
    }
    return response;
  }
  function availability({form, date, slot, field, message, quantities}) {
    let version = 0, ready = false, timer;
    const submit = form.querySelector('[type="submit"]');
    async function refresh() {
      const current = ++version;
      ready = false; submit.disabled = true;
      if (!date.value) return;
      message.textContent = 'Controllo disponibilità…';
      try {
        const articles = quantities().reduce((sum, value) => sum + (Number(value) || 0), 0);
        const query = new URLSearchParams({data: date.value, articoli: articles.toFixed(3)});
        const response = await fetch('/api/ordini/disponibilita?' + query, {cache:'no-store'});
        const data = await response.json();
        if (current !== version) return;
        if (!response.ok) throw new Error(data.error || 'Disponibilità non verificabile.');
        const selected = slot.value;
        field.hidden = !data.fasce_ritiro_attive; slot.required = !!data.fasce_ritiro_attive;
        slot.replaceChildren(new Option('Scegli una fascia', ''));
        for (const start of data.fasce || []) {
          const minute = Number(start.slice(0,2))*60 + Number(start.slice(3)) + data.minuti_fascia_ritiro;
          const end = String(Math.floor(minute/60)).padStart(2,'0') + ':' + String(minute%60).padStart(2,'0');
          slot.add(new Option(start + '–' + end, start));
        }
        if ((data.fasce || []).includes(selected)) slot.value = selected;
        ready = !!data.disponibile;
        message.textContent = ready ? '' : 'Nessuna disponibilità per questa data e quantità. Scegli un’altra data.';
        submit.disabled = !ready;
      } catch (error) { if (current === version) message.textContent = error.message; }
    }
    function schedule() { ready = false; submit.disabled = true; ++version; clearTimeout(timer); timer = setTimeout(refresh, 200); }
    form.addEventListener('input', event => { if (event.target === date || event.target.type === 'number') schedule(); });
    form.addEventListener('submit', event => { if (!ready) { event.preventDefault(); event.stopImmediatePropagation(); refresh(); } }, true);
    return {refresh, schedule};
  }
  return {send, availability};
})();
