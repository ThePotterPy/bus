self.CACHE_NAME = 'jaha-tracker-v17';
self.APP_SHELL = ['/', '/index.html', '/feedback.js', '/news.js', '/observed-routes.js', '/shared-trips.js', '/manifest.json'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(self.CACHE_NAME).then((cache) => {
      return cache.addAll(self.APP_SHELL);
    })
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keyList) => {
      return Promise.all(keyList.map((key) => {
        if (key !== self.CACHE_NAME) {
          return caches.delete(key);
        }
      }));
    })
  );
  return self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // La API y los mosaicos del mapa siempre van directo a la red. Solo se
  // conserva la pequeña interfaz propia para poder mostrarla sin conexión.
  if (e.request.method !== 'GET' || url.origin !== self.location.origin ||
      url.pathname.startsWith('/api/') || !self.APP_SHELL.includes(url.pathname)) return;

  // Network-first evita dejar a los usuarios atrapados en una interfaz vieja
  // después de un despliegue; la caché es únicamente el respaldo sin conexión.
  e.respondWith(
    fetch(e.request).then((response) => {
      const copy = response.clone();
      caches.open(self.CACHE_NAME).then((cache) => cache.put(e.request, copy));
      return response;
    }).catch(() => caches.match(e.request))
  );
});

self.addEventListener('push', function(event) {
  let data = { title: 'Notificación', body: 'Mensaje' };
  try {
    if (event.data) {
      data = event.data.json();
    }
  } catch (e) {
    console.error('Error parseando push data', e);
  }
  
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      data: { line: data.line },
      icon: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNTYgMjU2Ij48cmVjdCB3aWR0aD0iMjU2IiBoZWlnaHQ9IjI1NiIgZmlsbD0ibm9uZSIvPjxwYXRoIGQ9Ik0xMjgsMjBBMTQ0LjcsMTQ0LjcsMCwwLDAsMzIsMTA0djU2YTMyLDMyLDAsMCwwLDMyLDMyYTE2LDE2LDAsMCwxLDMyLDBhMTYsMTYsMCwwLDEsMzIsMGExNiwxNiwwLDAsMSwzMiwwYTE2LDE2LDAsMCwxLDMyLDBhMzIsMzIsMCwwLDAsMzItMzJWMTA0QTE0NC43LDE0NC43LDAsMCwwLDEyOCwyMFoiIGZpbGw9IiMzYWEwZmYiLz48L3N2Zz4='
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL('/', self.location.origin);
  const line = event.notification.data && event.notification.data.line;
  if (typeof line === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(line)) {
    url.searchParams.set('line', line);
  }
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (windows) => {
    const current = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (current) {
      await current.navigate(url.href);
      return current.focus();
    }
    return self.clients.openWindow(url.href);
  }));
});
