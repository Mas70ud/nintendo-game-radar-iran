/* ============================================================
   Service Worker for Nintendo Notifier PWA
   - Caches the app shell (HTML/CSS/JS/icons) for offline use
   - Never caches API responses (always fresh)
   ============================================================ */

const CACHE_NAME = "nintendo-notifier-v12";

const APP_SHELL = [
  "/",
  "/static/css/style.css?v=28",
  "/static/js/app.js?v=28",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/nintendo-favicon.svg?v=1",
  "/static/fonts/IRANSansXFaNum-Regular.ttf",
  "/static/fonts/IRANSansXFaNum-Bold.ttf",
];

// Install: pre-cache the shell
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return Promise.all(
        APP_SHELL.map((url) =>
          cache.add(url).catch(() => {
            /* ignore individual failures (e.g. login redirect) */
          })
        )
      );
    })
  );
  self.skipWaiting();
});

// Activate: clean up old caches
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// Fetch strategy:
//   - /api/*        -> network only
//   - navigation    -> network first, fall back to cached "/"
//   - everything else -> cache first, then network
self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only handle same-origin
  if (url.origin !== self.location.origin) return;

  // Never cache API or SSE
  if (url.pathname.startsWith("/api/")) {
    return; // let the browser do its thing
  }

  // Navigation requests: network first, fallback to cache
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match("/"))
    );
    return;
  }

  // Static assets: cache first
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((res) => {
        // Cache successful GET responses for static assets
        if (req.method === "GET" && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
        }
        return res;
      }).catch(() => cached);
    })
  );
});
