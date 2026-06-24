// Minimal service worker for installability + basic offline fallback
// El valor 0.1.0-24e00aa-dirty-20260612183516708 será reemplazado en prebuild por scripts/build-sw.mjs
const CACHE_NAME = 'nexora-app-cache-0.1.0-24e00aa-dirty-20260612183516708';
const URLS_TO_CACHE = ['/', '/index.html'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(URLS_TO_CACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  event.respondWith(
    fetch(request).catch(() => caches.match(request))
  );
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_) {
    data = {};
  }

  const title = data.title || 'NEXORA';
  const options = {
    body: data.body || '',
    icon: data.icon || '/icons/logo-app-192.png',
    badge: data.badge || '/icons/logo-app-192.png',
    tag: data.tag || 'nexora-notificacion',
    data: {
      href: data.href || '/',
      notificationId: data.notificationId || null,
      notificationKey: data.notificationKey || '',
      entityType: data.entityType || '',
      entityId: data.entityId || '',
      payload: data.payload || {},
    },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const href = event.notification && event.notification.data ? event.notification.data.href : '/';
  const targetUrl = new URL(href || '/', self.location.origin).href;

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (clientList) => {
      for (const client of clientList) {
        if ('focus' in client && new URL(client.url).origin === self.location.origin) {
          try {
            if ('navigate' in client) {
              const navigatedClient = await client.navigate(targetUrl);
              return (navigatedClient || client).focus();
            }
            return client.focus();
          } catch (_) {
            break;
          }
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
      return undefined;
    })
  );
});
