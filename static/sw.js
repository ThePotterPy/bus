self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open('jaha-tracker-v8').then((cache) => {
      return cache.addAll([
        '/',
        '/index.html',
        '/observed-routes.js',
        '/manifest.json'
      ]);
    })
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keyList) => {
      return Promise.all(keyList.map((key) => {
        if (key !== 'jaha-tracker-v8') {
          return caches.delete(key);
        }
      }));
    })
  );
  return self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  // Solo cacheamos GET, y obviamos llamadas a la API
  if (e.request.method !== 'GET' || e.request.url.includes('/api/')) {
    return;
  }
  
  e.respondWith(
    caches.match(e.request).then((response) => {
      return response || fetch(e.request).then((res) => {
          return caches.open('jaha-tracker-v8').then((cache) => {
              cache.put(e.request, res.clone());
              return res;
          });
      });
    })
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
      icon: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNTYgMjU2Ij48cmVjdCB3aWR0aD0iMjU2IiBoZWlnaHQ9IjI1NiIgZmlsbD0ibm9uZSIvPjxwYXRoIGQ9Ik0xMjgsMjBBMTQ0LjcsMTQ0LjcsMCwwLDAsMzIsMTA0djU2YTMyLDMyLDAsMCwwLDMyLDMyYTE2LDE2LDAsMCwxLDMyLDBhMTYsMTYsMCwwLDEsMzIsMGExNiwxNiwwLDAsMSwzMiwwYTE2LDE2LDAsMCwxLDMyLDBhMzIsMzIsMCwwLDAsMzItMzJWMTA0QTE0NC43LDE0NC43LDAsMCwwLDEyOCwyMFoiIGZpbGw9IiMzYWEwZmYiLz48L3N2Zz4='
    })
  );
});
