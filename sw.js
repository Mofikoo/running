// RunCoach Service Worker
// Le cache se renouvelle automatiquement à chaque nouvelle version de index.html
const CACHE_NAME = 'runcoach-v1';

// On récupère la date de dernière modif de index.html pour versionner le cache
async function getCacheVersion() {
  try {
    const res = await fetch('/running/index.html', { method: 'HEAD' });
    const lastModified = res.headers.get('last-modified') || Date.now();
    return 'runcoach-' + btoa(lastModified).slice(0, 12);
  } catch {
    return CACHE_NAME;
  }
}

const STATIC_ASSETS = [
  '/running/',
  '/running/index.html',
  'https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap',
  'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js',
];

// Installation : on calcule le vrai nom de cache depuis le serveur
self.addEventListener('install', event => {
  event.waitUntil(
    getCacheVersion().then(version =>
      caches.open(version).then(cache =>
        cache.addAll(STATIC_ASSETS).catch(err => console.log('Cache partiel OK:', err))
      )
    )
  );
  self.skipWaiting();
});

// Activation : supprime tous les anciens caches sauf le courant
self.addEventListener('activate', event => {
  event.waitUntil(
    getCacheVersion().then(version =>
      caches.keys().then(keys =>
        Promise.all(keys.filter(k => k !== version).map(k => caches.delete(k)))
      )
    )
  );
  self.clients.claim();
});

// Fetch : Network First pour les API, Cache First pour le reste
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  if (
    url.hostname.includes('supabase.co') ||
    url.hostname.includes('openrouter.ai') ||
    url.hostname.includes('workers.dev') ||
    url.hostname.includes('strava.com')
  ) {
    event.respondWith(fetch(event.request));
    return;
  }

  event.respondWith(
    getCacheVersion().then(version =>
      caches.match(event.request).then(cached => {
        if (cached) return cached;
        return fetch(event.request).then(response => {
          if (response.ok && event.request.method === 'GET') {
            const clone = response.clone();
            caches.open(version).then(cache => cache.put(event.request, clone));
          }
          return response;
        }).catch(() => {
          if (event.request.destination === 'document') {
            return caches.match('/running/');
          }
        });
      })
    )
  );
});

self.addEventListener('message', event => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});
