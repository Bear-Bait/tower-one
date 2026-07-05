/* Tower Fallback Control — service worker.
 *
 * Purpose: let the installed app icon open the dashboard shell instantly,
 * even on a flaky WireGuard tunnel. It does NOT make the tower controllable
 * offline — all live state still comes from the server.
 *
 * Strategy:
 *   - /api/* and non-GET requests are NEVER intercepted (must hit the
 *     network live — status, health, control commands).
 *   - Everything else (shell HTML, icons, fonts) is network-first with a
 *     cache fallback, so a fresh deploy is always picked up when online and
 *     the last-known shell still opens when the box is unreachable.
 *
 * Bump CACHE on any change to force old caches out on activate.
 */
const CACHE = 'tower-shell-v1';

self.addEventListener('install', (e) => {
  // Take over as soon as installed; no precache list (shell is cached on first load).
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // Live data and state changes must always hit the network — never cached.
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) {
    return; // default browser handling
  }

  // Network-first, fall back to cache when offline.
  e.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res && res.ok) {
        const cache = await caches.open(CACHE);
        cache.put(req, res.clone());
      }
      return res;
    } catch (err) {
      const cached = await caches.match(req);
      if (cached) return cached;
      if (req.mode === 'navigate') {
        const root = await caches.match('/');
        if (root) return root;
      }
      throw err;
    }
  })());
});
