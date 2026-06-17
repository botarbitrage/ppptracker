'use strict';

const CACHE_NAME    = 'pppokerha-v5';
const OFFLINE_URL   = '/offline';

// Truly static assets — cache-first (content never changes between deploys)
const PRECACHE = [
  '/static/manifest.json',
  '/static/icons/icon.svg',
  '/offline',
];

// App code — stale-while-revalidate: serve cached version instantly, refresh in background.
// Users get the latest version on the next page load after a deploy.
const STALE_WHILE_REVALIDATE = [
  '/static/app.js',
  '/static/style.css',
];

// ── Install: pre-cache static shell assets ────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: purge old caches ────────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: strategy depends on request type ───────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET and cross-origin requests
  if (request.method !== 'GET' || url.origin !== location.origin) return;

  // API calls: network-only, never cache
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request).catch(() =>
        new Response(JSON.stringify({ error: 'You are offline. Please reconnect and try again.' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } })
      )
    );
    return;
  }

  // app.js / style.css: stale-while-revalidate
  // Serve the cached copy immediately for speed, fetch network in background to keep cache fresh.
  // After a deploy, users get the new version on the very next page load.
  if (STALE_WHILE_REVALIDATE.includes(url.pathname)) {
    event.respondWith(
      caches.open(CACHE_NAME).then(cache =>
        cache.match(request).then(cached => {
          const networkFetch = fetch(request).then(response => {
            if (response.ok) cache.put(request, response.clone());
            return response;
          }).catch(() => null);
          // Return cache immediately if available; otherwise wait for network
          return cached || networkFetch;
        })
      )
    );
    return;
  }

  // HTML shell (/): network-first so fresh HTML is fetched on every navigation.
  // Falls back to cache when offline.
  if (url.pathname === '/') {
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response.ok) {
            caches.open(CACHE_NAME).then(cache => cache.put(request, response.clone()));
          }
          return response;
        })
        .catch(() =>
          caches.match(request).then(cached => cached || caches.match(OFFLINE_URL))
        )
    );
    return;
  }

  // Everything else: cache-first with network fallback
  event.respondWith(
    caches.match(request).then(cached => {
      if (cached) return cached;
      return fetch(request)
        .then(response => {
          if (response.ok && url.pathname.startsWith('/static/')) {
            caches.open(CACHE_NAME).then(cache => cache.put(request, response.clone()));
          }
          return response;
        })
        .catch(() => caches.match(OFFLINE_URL));
    })
  );
});
