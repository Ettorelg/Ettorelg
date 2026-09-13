(() => {
  let button = document.querySelector('[data-order-notifications]');
  if (!button && document.querySelector('header')) {
    button = document.createElement('button');
    button.className = 'order-notification-button';
    button.type = 'button';
    button.dataset.orderNotifications = '';
    document.querySelector('header').insertBefore(button, document.querySelector('header').lastElementChild);
  }
  if (!button) return;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || (typeof csrf !== 'undefined' ? csrf : '');
  const banner = document.createElement('div');
  banner.className = 'order-arrival-banner';
  banner.setAttribute('role', 'status');
  banner.setAttribute('aria-live', 'assertive');
  banner.hidden = true;
  document.body.append(banner);
  let bannerTimer;
  let lastId = null;
  let polling = false;
  let audioContext;
  let soundEnabled = false;
  try { soundEnabled = localStorage.getItem('alpha-menu-order-sound') === '1'; } catch (_) {}

  function showBanner(message) {
    banner.textContent = message;
    banner.hidden = false;
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(() => { banner.hidden = true; }, 12000);
  }
  function playSound() {
    if (!soundEnabled) return;
    try {
      audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
      audioContext.resume();
      for (const [delay, frequency] of [[0, 880], [0.22, 1175]]) {
        const oscillator = audioContext.createOscillator();
        const gain = audioContext.createGain();
        oscillator.type = 'sine';
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, audioContext.currentTime + delay);
        gain.gain.exponentialRampToValueAtTime(0.22, audioContext.currentTime + delay + 0.025);
        gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + delay + 0.17);
        oscillator.connect(gain).connect(audioContext.destination);
        oscillator.start(audioContext.currentTime + delay);
        oscillator.stop(audioContext.currentTime + delay + 0.18);
      }
    } catch (_) { /* Il browser può sospendere l'audio finché l'utente non interagisce. */ }
  }
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const query = lastId === null ? '' : '?dopo=' + encodeURIComponent(lastId);
      const response = await fetch('/api/ordini/notifiche' + query, {cache: 'no-store'});
      if (!response.ok) return;
      const result = await response.json();
      if (lastId !== null && result.nuovi?.length) {
        const count = result.nuovi.length;
        showBanner(count === 1 ? '🔔 Nuovo ordine ricevuto! Apri gli ordini da evadere.' : `🔔 ${count} nuovi ordini ricevuti!`);
        playSound();
      }
      lastId = result.ultimo_id;
    } catch (_) { /* Riprovare al prossimo aggiornamento. */ }
    finally { polling = false; }
  }
  poll();
  setInterval(poll, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  const pushSupported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  function setButton(active) {
    button.textContent = active ? '🔔 Notifiche attive · disattiva' : '🔔 Attiva notifiche ordini';
    button.setAttribute('aria-pressed', String(active));
  }
  setButton(false);
  async function registration() {
    await navigator.serviceWorker.register('/service-worker.js');
    return navigator.serviceWorker.ready;
  }
  async function saveSubscription(subscription) {
    const response = await fetch('/api/ordini/notifiche/sottoscrizione', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify(subscription.toJSON())
    });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || 'Attivazione non riuscita.');
  }
  if (pushSupported && Notification.permission === 'granted') {
    registration().then(async reg => {
      const subscription = await reg.pushManager.getSubscription();
      if (subscription) { await saveSubscription(subscription); setButton(true); }
    }).catch(() => {});
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      if (button.getAttribute('aria-pressed') === 'true') {
        if (pushSupported) {
          const reg = await registration();
          const subscription = await reg.pushManager.getSubscription();
          if (subscription) {
            const response = await fetch('/api/ordini/notifiche/sottoscrizione', {
              method: 'DELETE', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
              body: JSON.stringify({endpoint: subscription.endpoint})
            });
            if (!response.ok) throw new Error('Disattivazione non riuscita. Riprova.');
            await subscription.unsubscribe();
          }
        }
        soundEnabled = false;
        try { localStorage.removeItem('alpha-menu-order-sound'); } catch (_) {}
        setButton(false);
        showBanner('Notifiche ordini disattivate su questo dispositivo.');
        return;
      }
      soundEnabled = true;
      try { localStorage.setItem('alpha-menu-order-sound', '1'); } catch (_) {}
      playSound();
      if (!pushSupported) {
        setButton(true);
        showBanner('Avvisi visivi e sonori attivi nel banco. Le notifiche a pagina chiusa non sono supportate su questo browser.');
        return;
      }
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        setButton(true);
        showBanner('Avvisi nel banco attivi. Per ricevere push a pagina chiusa, consenti le notifiche nelle impostazioni del dispositivo.');
        return;
      }
      const keyResponse = await fetch('/api/ordini/notifiche/chiave');
      if (!keyResponse.ok) throw new Error('Chiave notifiche non disponibile.');
      const {chiave_pubblica} = await keyResponse.json();
      const bytes = Uint8Array.from(atob(chiave_pubblica.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - chiave_pubblica.length % 4) % 4)), c => c.charCodeAt(0));
      const reg = await registration();
      const subscription = await reg.pushManager.getSubscription() || await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: bytes});
      await saveSubscription(subscription);
      setButton(true);
      showBanner('Notifiche attive su questo dispositivo, anche quando il banco è chiuso.');
    } catch (error) {
      showBanner(error.message || 'Impossibile attivare le notifiche.');
    } finally { button.disabled = false; }
  });
})();
