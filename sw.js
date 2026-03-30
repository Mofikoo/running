// RunCoach Service Worker
const CACHE_NAME = 'runcoach-v34';
const STATIC_ASSETS = [
  '/running/',
  '/running/index.html',
  'https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap',
  'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js',
];

// Installation : mise en cache des ressources statiques
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(err => {
        console.log('Cache partiel OK:', err);
      });
    })
  );
  self.skipWaiting();
});

// Activation : nettoyage des anciens caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch : stratégie Network First pour Supabase/API, Cache First pour statique
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // API calls (Supabase, OpenRouter, Strava) → toujours réseau
  if (
    url.hostname.includes('supabase.co') ||
    url.hostname.includes('openrouter.ai') ||
    url.hostname.includes('workers.dev') ||
    url.hostname.includes('strava.com')
  ) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Ressources statiques → Cache First avec fallback réseau
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        // Met en cache les nouvelles ressources statiques
        if (response.ok && event.request.method === 'GET') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => {
        // Offline fallback : retourne la page principale
        if (event.request.destination === 'document') {
          return caches.match('/running/');
        }
      });
    })
  );
});

// Message pour forcer la mise à jour
self.addEventListener('message', event => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});
