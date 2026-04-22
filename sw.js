// RunCoach Service Worker v4
let ACTIVE_CACHE = 'runcoach-v4';

async function resolveVersion() {
  try {
    const res = await fetch('/running/index.html', { method: 'HEAD', cache: 'no-store' });
    const lm = res.headers.get('last-modified');
    if (lm) return 'runcoach-' + btoa(lm).replace(/[^a-zA-Z0-9]/g,'').slice(0, 10);
  } catch {}
  return ACTIVE_CACHE;
}

const STATIC_ASSETS = [
  '/running/',
  '/running/index.html',
];

self.addEventListener('install', event => {
  event.waitUntil(
    resolveVersion().then(version => {
      ACTIVE_CACHE = version;
      return caches.open(version).then(cache =>
        cache.addAll(STATIC_ASSETS).catch(() => {})
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    resolveVersion().then(version => {
      ACTIVE_CACHE = version;
      return caches.keys().then(keys =>
        Promise.all(keys.filter(k => k !== version).map(k => caches.delete(k)))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // Laisser passer TOUT ce qui n'est pas GET (POST, OPTIONS...) sans toucher
  if (req.method !== 'GET') return;

  // Laisser passer toutes les requêtes cross-origin (API, CDN, fonts)
  if (url.origin !== self.location.origin) return;

  // Seulement les ressources de notre propre domaine en GET
  event.respondWith(
    caches.match(req).then(cached => {
      if (cached) return cached;
      return fetch(req).then(response => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(ACTIVE_CACHE).then(cache => cache.put(req, clone));
        }
        return response;
      }).catch(() => {
        if (req.destination === 'document') {
          return caches.match('/running/index.html');
        }
      });
    })
  );
});

self.addEventListener('message', event => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});