// Minimal service worker for installability + basic offline fallback
// El valor 0.1.0-1ce3a76-dirty-20260609193348825 será reemplazado en prebuild por scripts/build-sw.mjs
const CACHE_NAME = 'sr-app-cache-0.1.0-1ce3a76-dirty-20260609193348825';
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
