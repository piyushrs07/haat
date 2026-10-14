/* Haat service worker - version {{ version }}
 *
 * What it does:
 *   - makes the site installable and shows /offline/ when the network is down
 *   - caches static files (CSS/JS/icons, content-hashed so safe to keep) and product photos
 * What it deliberately does NOT do:
 *   - cache HTML pages, forms, /admin/ or any POST - those are per-user / contain account data
 *     and must always come fresh from the server.
 */
const VERSION = '{{ version }}';
const STATIC_CACHE = 'haat-static-' + VERSION;
const IMAGE_CACHE = 'haat-images-v1';
const OFFLINE_URL = '/offline/';
const MAX_IMAGES = 80;

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then(function (cache) { return cache.add(new Request(OFFLINE_URL, { cache: 'reload' })); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys
        .filter(function (k) { return k.indexOf('haat-static-') === 0 && k !== STATIC_CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

function trim(cacheName, max) {
  return caches.open(cacheName).then(function (cache) {
    return cache.keys().then(function (keys) {
      if (keys.length <= max) { return; }
      return cache.delete(keys[0]).then(function () { return trim(cacheName, max); });
    });
  });
}

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') { return; }
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) { return; }
  if (url.pathname.indexOf('/admin') === 0 || url.pathname === '/service-worker.js') { return; }

  // Page navigations: always network; offline page only if the network fails.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(function () { return caches.match(OFFLINE_URL); })
    );
    return;
  }

  // Static assets: cache-first (file names change when content changes).
  if (url.pathname.indexOf('/static/') === 0) {
    event.respondWith(
      caches.match(req).then(function (hit) {
        if (hit) { return hit; }
        return fetch(req).then(function (res) {
          if (res.ok) {
            var copy = res.clone();
            caches.open(STATIC_CACHE).then(function (c) { c.put(req, copy); });
          }
          return res;
        });
      })
    );
    return;
  }

  // Product photos: show the cached copy instantly, refresh it in the background.
  if (url.pathname.indexOf('/media/') === 0) {
    event.respondWith(
      caches.open(IMAGE_CACHE).then(function (cache) {
        return cache.match(req).then(function (hit) {
          var network = fetch(req).then(function (res) {
            if (res.ok) { cache.put(req, res.clone()).then(function () { trim(IMAGE_CACHE, MAX_IMAGES); }); }
            return res;
          }).catch(function () { return hit; });
          return hit || network;
        });
      })
    );
  }
});
