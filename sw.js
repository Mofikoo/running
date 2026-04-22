// RunCoach Service Worker — auto-versioning sans fetch imbriqué
// La version est déterminée une seule fois à l'install/activate

let ACTIVE_CACHE = 'runcoach-v1';

// Calcule la version depuis le Last-Modified de index.html
// Appelé UNIQUEMENT pendant install/activate, jamais pendant fetch
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
  'https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@300;400;500&family=DM+Sans:wght@300;400;500;600&display=swap',
  'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js',
];

self.addEventListener('install', event => {
  event.waitUntil(
    resolveVersion().then(version => {
      ACTIVE_CACHE = version;
      return caches.open(version).then(cache =>
        cache.addAll(STATIC_ASSETS).catch(err => console.log('Cache partiel OK:', err))
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
  const url = new URL(event.request.url);

  // API calls → toujours réseau direct, jamais de cache
  if (
    url.hostname.includes('supabase.co') ||
    url.hostname.includes('openrouter.ai') ||
    url.hostname.includes('workers.dev') ||
    url.hostname.includes('strava.com') ||
    url.hostname.includes('googleapis.com') && url.pathname.includes('fonts')
  ) {
    return; // laisser passer sans respondWith = comportement réseau normal
  }

  // Ressources statiques → Cache First, fallback réseau
  event.respondWith(
    caches.match(event.request).then(cached => {
      if (cached) return cached;
      return fetch(event.request).then(response => {
        if (response.ok && event.request.method === 'GET') {
          const clone = response.clone();
          caches.open(ACTIVE_CACHE).then(cache => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => {
        if (event.request.destination === 'document') {
          return caches.match('/running/index.html');
        }
      });
    })
  );
});

self.addEventListener('message', event => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});