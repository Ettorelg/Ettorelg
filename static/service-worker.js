const CACHE = 'alpha-menu-static-v1';
const APP_ASSETS = [
  '/manifest.webmanifest',
  '/static/app-icon-192.png',
  '/static/app-icon-512.png',
  '/static/alpha-menu-logo.png'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(APP_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || !APP_ASSETS.includes(url.pathname)) return;
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)));
});

self.addEventListener('push', event => {
  let payload = {};
  try { payload = event.data ? event.data.json() : {}; } catch (_) {}
  const url = payload.url === '/dipendenti/ordini' ? '/dipendenti/ordini' : '/ordini/evasione';
  event.waitUntil(self.registration.showNotification('Alpha Menu · Nuovo ordine', {
    body: payload.body || 'Hai un nuovo ordine da evadere.',
    icon: '/static/app-icon-192.png',
    badge: '/static/app-icon-192.png',
    tag: `alpha-menu-order-${payload.id || Date.now()}`,
    data: {url}
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/ordini/evasione';
  event.waitUntil((async () => {
    const pages = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    const existing = pages.find(page => new URL(page.url).pathname === url);
    if (existing) return existing.focus();
    return self.clients.openWindow(url);
  })());
});
