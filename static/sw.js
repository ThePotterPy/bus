// Service worker minimo: solo recibe notificaciones push y las muestra.
// No hace cache de la app (el mapa necesita datos en vivo, no tiene sentido
// funcionar offline), asi que no interceptamos fetch.

self.addEventListener('push', (event) => {
  let payload = { title: '🚌 Bus cerca', body: 'Un bus que estabas siguiendo esta cerca de tu ubicacion.' };
  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch (err) {
      payload.body = event.data.text();
    }
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: undefined,
      tag: 'jaha-proximidad',
      renotify: true,
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow('/');
    })
  );
});
