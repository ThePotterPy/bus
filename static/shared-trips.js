(function () {
  'use strict';

  const OWNER_KEY = 'jaha_shared_trip_owner_v1';
  const ALIASES_KEY = 'jaha_stop_aliases_v1';
  const SAVED_STOPS_KEY = 'jaha_saved_stops_v2';
  const HEARTBEAT_INTERVAL = 30000;
  let owner = loadJson(OWNER_KEY, null);
  const directPublicLink = /(?:^#|&)(?:busLine|parada)=/.test(location.hash);
  let publicToken = tokenFromHash() || (!directPublicLink && owner && owner.publicToken) || '';
  let currentTrip = null;
  let tripLayer = null;
  let pollTimer = null;
  let heartbeatWatch = null;
  let lastHeartbeat = 0;
  let centeredOnce = false;

  function loadJson(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      return value ? JSON.parse(value) : fallback;
    } catch (_) {
      return fallback;
    }
  }

  function saveJson(key, value) {
    try {
      if (value == null) localStorage.removeItem(key);
      else localStorage.setItem(key, JSON.stringify(value));
    } catch (_) {}
  }

  function aliases() {
    const value = loadJson(ALIASES_KEY, {});
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function savedStops() {
    const value = loadJson(SAVED_STOPS_KEY, {});
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }

  function stopById(stopId) {
    return typeof municipalStopsData !== 'undefined'
      ? municipalStopsData.find(item => String(item.id) === String(stopId)) : null;
  }

  function updateSavedStop(stop, changes) {
    if (!stop) return;
    const saved = savedStops();
    const id = String(stop.id);
    const previous = saved[id] || {};
    const next = {
      id, name: stop.name, type: stop.type, lat: Number(stop.lat), lon: Number(stop.lon),
      favorite: Boolean(previous.favorite), alias: String(previous.alias || ''), ...changes,
    };
    if (!next.favorite && !next.alias) delete saved[id];
    else saved[id] = next;
    saveJson(SAVED_STOPS_KEY, saved);
    if (typeof refreshMunicipalStopPopup === 'function') refreshMunicipalStopPopup(id);
    if (typeof renderLineList === 'function') renderLineList();
  }

  window.getSavedStops = function () {
    return Object.values(savedStops()).filter(stop => stop && (stop.favorite || stop.alias));
  };

  window.getSavedStop = function (stopId) {
    return savedStops()[String(stopId)] || null;
  };

  window.isFavoriteStop = function (stopId) {
    return Boolean(savedStops()[String(stopId)]?.favorite);
  };

  window.toggleStopFavorite = function (stopId) {
    const stop = stopById(stopId) || savedStops()[String(stopId)];
    if (!stop) return;
    updateSavedStop(stop, { favorite: !window.isFavoriteStop(stopId) });
  };

  window.stopDisplayName = function (stop) {
    const saved = savedStops()[String(stop.id)];
    const legacyAlias = aliases()[String(stop.id)] || '';
    if (!saved && legacyAlias) updateSavedStop(stop, { alias: legacyAlias });
    return saved?.alias || legacyAlias || stop.name;
  };

  window.editStopAlias = function (stopId) {
    const stop = typeof municipalStopsData !== 'undefined'
      ? municipalStopsData.find(item => String(item.id) === String(stopId)) || savedStops()[String(stopId)]
      : savedStops()[String(stopId)];
    if (!stop) return;
    const current = savedStops()[String(stopId)];
    const value = prompt('Apodo privado para esta parada:', current?.alias || aliases()[String(stopId)] || '');
    if (value === null) return;
    const clean = value.trim().slice(0, 60);
    updateSavedStop(stop, { alias: clean });
  };

  function tokenFromHash() {
    const match = location.hash.match(/(?:^#|&)viaje=([^&]+)/);
    if (!match) return '';
    try { return decodeURIComponent(match[1]); } catch (_) { return ''; }
  }

  function clientIdentity() {
    try {
      let value = localStorage.getItem('jaha_client_id');
      if (!value) {
        value = 'c-' + (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2));
        localStorage.setItem('jaha_client_id', value);
      }
      return value;
    } catch (_) {
      return 'c-' + Math.random().toString(36).slice(2);
    }
  }

  async function post(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok || !data.success) {
      const error = new Error(data.error || 'No se pudo completar la operación');
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function browserPosition() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error('Tu navegador no permite usar la ubicación'));
      navigator.geolocation.getCurrentPosition(resolve, reject, {
        enableHighAccuracy: true, maximumAge: 10000, timeout: 18000,
      });
    });
  }

  function positionPayload(position) {
    return {
      lat: position.coords.latitude,
      lon: position.coords.longitude,
      accuracy: Number.isFinite(position.coords.accuracy) ? position.coords.accuracy : 9999,
    };
  }

  function rememberCreated(data) {
    owner = { publicToken: data.publicToken, ownerToken: data.ownerToken };
    publicToken = data.publicToken;
    saveJson(OWNER_KEY, owner);
    history.replaceState(null, '', '#viaje=' + encodeURIComponent(publicToken));
    centeredOnce = false;
    startPolling();
  }

  async function createShare(payload) {
    const data = await post('/api/shared-trip/start', { clientId: clientIdentity(), ...payload });
    rememberCreated(data);
    await refreshTrip();
    await shareCurrentLink();
  }

  window.startSharedBusTrip = async function (unitId) {
    if (typeof currentLine === 'undefined' || !currentLine) {
      alert('Primero elegí una línea.');
      return;
    }
    try {
      const position = await browserPosition();
      const location = positionPayload(position);
      if (owner && publicToken && currentTrip && currentTrip.status !== 'ended') {
        const data = await post('/api/shared-trip/update', {
          token: publicToken, ownerToken: owner.ownerToken, action: 'board',
          lineId: String(currentLine), unitId: String(unitId),
          destinationStopId: currentTrip.destination
            ? currentTrip.destination.id
            : (currentTrip.status === 'planning' && currentTrip.stop ? currentTrip.stop.id : null),
          ...location,
        });
        currentTrip = data.trip;
        renderTrip();
        startHeartbeat();
        await shareCurrentLink();
      } else {
        await createShare({
          kind: 'bus', intent: 'trip', lineId: String(currentLine),
          unitId: String(unitId), ...location,
        });
        startHeartbeat();
      }
    } catch (error) {
      alert(error.message || 'No pudimos iniciar el viaje compartido.');
    }
  };

  window.shareStop = async function (stopId, intent) {
    try {
      const payload = { kind: 'stop', intent: intent === 'waiting' ? 'waiting' : 'going', stopId: String(stopId) };
      if (payload.intent === 'waiting') Object.assign(payload, positionPayload(await browserPosition()));
      await createShare(payload);
    } catch (error) {
      alert(error.message || 'No pudimos compartir esta parada.');
    }
  };

  window.setSharedDestination = async function (stopId) {
    try {
      if (owner && publicToken && currentTrip && currentTrip.status !== 'ended') {
        const data = await post('/api/shared-trip/update', {
          token: publicToken, ownerToken: owner.ownerToken,
          action: 'destination', stopId: String(stopId),
        });
        currentTrip = data.trip;
        renderTrip();
      } else {
        await createShare({ kind: 'stop', intent: 'going', stopId: String(stopId) });
      }
    } catch (error) {
      alert(error.message || 'No pudimos guardar la parada de descenso.');
    }
  };

  async function refreshTrip() {
    if (!publicToken) return;
    try {
      const data = await post('/api/shared-trip/view', { token: publicToken });
      currentTrip = data.trip;
      renderTrip();
      if (isOwner() && currentTrip.status === 'on_bus') startHeartbeat();
      if (currentTrip.status === 'ended') {
        stopHeartbeat();
        stopPolling();
        dismissLegacyTrip();
      }
    } catch (error) {
      showUnavailable(error.message);
      if (error.status === 404) stopPolling();
    }
  }

  function isOwner() {
    return Boolean(owner && owner.publicToken === publicToken && owner.ownerToken);
  }

  function stopName(stop) {
    if (!stop) return '';
    return isOwner() ? (aliases()[String(stop.id)] || stop.name) : stop.name;
  }

  function remainingText() {
    if (!currentTrip || currentTrip.status === 'ended') return '';
    const minutes = Math.max(0, Math.ceil((currentTrip.expiresAt * 1000 - Date.now()) / 60000));
    return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
  }

  function statusCopy() {
    if (!currentTrip) return ['Viaje compartido', 'Cargando…'];
    if (currentTrip.status === 'ended') return ['Viaje finalizado', endReason(currentTrip.endReason)];
    if (currentTrip.status === 'waiting') return ['Esperando en una parada', stopName(currentTrip.stop)];
    if (currentTrip.status === 'planning') return ['Destino compartido', stopName(currentTrip.stop)];
    const title = `Línea ${currentTrip.lineId} · Unidad ${currentTrip.unitId}`;
    if (currentTrip.destination && currentTrip.destinationDistanceMeters != null && currentTrip.destinationDistanceMeters <= 300) {
      return [title, 'Llegando al destino · posible descenso'];
    }
    if (currentTrip.verification === 'checking') return [title, 'Verificando posible descenso del bus'];
    if (currentTrip.verification === 'unavailable') return [title, 'Siguiendo el bus · pasajero sin verificar'];
    return [title, 'A bordo confirmado'];
  }

  function endReason(reason) {
    return ({
      expired: 'Se alcanzó el tiempo máximo', signal_lost: 'No se pudo volver a verificar al pasajero',
      left_bus: 'El pasajero se alejó del bus', owner_ended: 'Finalizado por quien compartió',
      replaced: 'Se inició otro viaje',
    })[reason] || 'Este enlace ya no está activo';
  }

  function renderTrip() {
    const bar = document.getElementById('sharedTripBar');
    if (!bar || !currentTrip) return;
    bar.hidden = false;
    const copy = statusCopy();
    document.getElementById('sharedTripTitle').textContent = copy[0];
    document.getElementById('sharedTripSubtitle').textContent = copy[1];
    const destination = currentTrip.destination || (currentTrip.intent === 'going' ? currentTrip.stop : null);
    const destinationCopy = destination ? `Destino: ${stopName(destination)}` : 'Sin parada de descenso marcada';
    const distance = currentTrip.destinationDistanceMeters != null
      ? ` · bus a ${formatDistance(currentTrip.destinationDistanceMeters)}` : '';
    const remaining = remainingText();
    document.getElementById('sharedTripDetail').textContent =
      `${destinationCopy}${distance}${remaining ? ` · finaliza en ${remaining}` : ''}`;
    document.getElementById('sharedTripEnd').hidden = !isOwner() || currentTrip.status === 'ended';
    document.getElementById('sharedTripEnd').textContent = currentTrip.status === 'on_bus' ? 'Me bajé / finalizar' : 'Finalizar';
    document.getElementById('sharedTripExtend').hidden = !isOwner() || currentTrip.status === 'ended' || currentTrip.extended;
    document.getElementById('sharedTripLeave').hidden = isOwner();
    document.getElementById('sharedTripShare').hidden = currentTrip.status === 'ended';
    drawPublicTrip();
  }

  function formatDistance(meters) {
    return meters < 1000 ? `${Math.round(meters)} m` : `${(meters / 1000).toFixed(1)} km`;
  }

  function drawPublicTrip() {
    if (!tripLayer || !currentTrip) return;
    tripLayer.clearLayers();
    const points = [];
    if (currentTrip.bus && Number.isFinite(Number(currentTrip.bus.lat)) && Number.isFinite(Number(currentTrip.bus.lon))) {
      const ll = [Number(currentTrip.bus.lat), Number(currentTrip.bus.lon)];
      const icon = L.divIcon({
        className: '', html: '<div class="bus-icon-wrap moving">🚌</div>',
        iconSize: [32, 32], iconAnchor: [16, 16],
      });
      L.marker(ll, { icon }).bindPopup(`Unidad ${escapeText(currentTrip.unitId)}`).addTo(tripLayer);
      points.push(ll);
    }
    const destination = currentTrip.destination || (currentTrip.intent === 'going' ? currentTrip.stop : null);
    if (destination) {
      const ll = [Number(destination.lat), Number(destination.lon)];
      L.circleMarker(ll, { radius: 9, color: '#fff', weight: 2, fillColor: '#ff3366', fillOpacity: 1 })
        .bindPopup(`Parada: ${escapeText(stopName(destination))}`).addTo(tripLayer);
      points.push(ll);
    } else if (currentTrip.stop) {
      const ll = [Number(currentTrip.stop.lat), Number(currentTrip.stop.lon)];
      L.circleMarker(ll, { radius: 9, color: '#fff', weight: 2, fillColor: '#0055ff', fillOpacity: 1 })
        .bindPopup(`Parada: ${escapeText(stopName(currentTrip.stop))}`).addTo(tripLayer);
      points.push(ll);
    }
    if (!centeredOnce && points.length) {
      centerPoints(points);
      centeredOnce = true;
    }
  }

  function escapeText(value) {
    return String(value || '').replace(/[&<>"']/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' })[char]);
  }

  function centerPoints(points) {
    if (!points.length) return;
    if (points.length === 1) map.setView(points[0], Math.max(map.getZoom(), 15));
    else map.fitBounds(L.latLngBounds(points), { padding: [50, 80], maxZoom: 16 });
  }

  function showUnavailable(message) {
    const bar = document.getElementById('sharedTripBar');
    if (!bar) return;
    currentTrip = null;
    if (tripLayer) tripLayer.clearLayers();
    stopHeartbeat();
    bar.hidden = false;
    document.getElementById('sharedTripTitle').textContent = 'Viaje no disponible';
    document.getElementById('sharedTripSubtitle').textContent = message;
    document.getElementById('sharedTripDetail').textContent = 'El enlace pudo vencer o haber sido finalizado.';
    document.getElementById('sharedTripEnd').hidden = true;
    document.getElementById('sharedTripLeave').hidden = !publicToken || isOwner();
    document.getElementById('sharedTripShare').hidden = true;
    document.getElementById('sharedTripExtend').hidden = true;
  }

  function shareUrl() {
    const url = new URL(location.origin + location.pathname);
    url.hash = 'viaje=' + encodeURIComponent(publicToken);
    return url.toString();
  }

  async function shareCurrentLink() {
    if (!publicToken) return;
    const data = { title: 'Seguimiento de viaje', text: 'Podés seguir mi bus o la parada compartida desde este enlace.', url: shareUrl() };
    if (navigator.share) {
      try { await navigator.share(data); return; } catch (error) { if (error.name === 'AbortError') return; }
    }
    try {
      await navigator.clipboard.writeText(data.url);
      alert('Enlace copiado.');
    } catch (_) {
      prompt('Copiá este enlace:', data.url);
    }
  }

  function startHeartbeat() {
    if (!isOwner() || heartbeatWatch != null || !navigator.geolocation) return;
    heartbeatWatch = navigator.geolocation.watchPosition(async position => {
      if (Date.now() - lastHeartbeat < HEARTBEAT_INTERVAL) return;
      lastHeartbeat = Date.now();
      try {
        const data = await post('/api/shared-trip/update', {
          token: publicToken, ownerToken: owner.ownerToken, action: 'heartbeat',
          ...positionPayload(position),
        });
        currentTrip = data.trip;
        renderTrip();
      } catch (_) {}
    }, () => {}, { enableHighAccuracy: true, maximumAge: 15000, timeout: 20000 });
  }

  function stopHeartbeat() {
    if (heartbeatWatch != null && navigator.geolocation) navigator.geolocation.clearWatch(heartbeatWatch);
    heartbeatWatch = null;
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  function dismissLegacyTrip() {
    currentTrip = null;
    publicToken = '';
    centeredOnce = false;
    stopHeartbeat();
    stopPolling();
    if (tripLayer) tripLayer.clearLayers();
    const bar = document.getElementById('sharedTripBar');
    if (bar) bar.hidden = true;
  }

  function startPolling() {
    if (!publicToken) return;
    stopPolling();
    refreshTrip();
    pollTimer = setInterval(refreshTrip, 10000);
  }

  function bindUi() {
    tripLayer = L.layerGroup().addTo(map);
    const bar = document.getElementById('sharedTripBar');
    const toggle = document.getElementById('sharedTripToggle');
    const toggleBar = () => {
      const collapsed = bar.classList.toggle('collapsed');
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.querySelector('.shared-trip-chevron').textContent = collapsed ? '▲' : '▼';
    };
    toggle.addEventListener('click', toggleBar);
    document.getElementById('sharedTripCenter').addEventListener('click', () => {
      if (window.sharedBusView && typeof window.centerSharedBusView === 'function') {
        window.centerSharedBusView();
        return;
      }
      centeredOnce = false;
      drawPublicTrip();
    });
    document.getElementById('sharedTripShare').addEventListener('click', () => {
      if (window.sharedBusView && typeof window.shareCurrentBusLink === 'function') window.shareCurrentBusLink();
      else shareCurrentLink();
    });
    document.getElementById('sharedTripLeave').addEventListener('click', () => {
      if (window.sharedBusView && typeof window.stopSharedBusView === 'function') {
        window.stopSharedBusView();
        return;
      }
      if (isOwner()) return;
      history.replaceState(null, '', location.pathname + location.search);
      dismissLegacyTrip();
    });
    document.getElementById('sharedTripExtend').addEventListener('click', async () => {
      if (!isOwner()) return;
      try {
        const data = await post('/api/shared-trip/update', {
          token: publicToken, ownerToken: owner.ownerToken, action: 'extend',
        });
        currentTrip = data.trip;
        renderTrip();
      } catch (error) { alert(error.message); }
    });
    document.getElementById('sharedTripEnd').addEventListener('click', async () => {
      if (!isOwner() || !confirm('¿Finalizar este viaje compartido?')) return;
      try {
        await post('/api/shared-trip/update', {
          token: publicToken, ownerToken: owner.ownerToken, action: 'end',
        });
        saveJson(OWNER_KEY, null);
        owner = null;
        dismissLegacyTrip();
      } catch (error) { alert(error.message); }
    });
    if (publicToken) startPolling();
  }

  window.addEventListener('hashchange', () => {
    const next = tokenFromHash();
    if (!next) {
      publicToken = '';
      currentTrip = null;
      centeredOnce = false;
      stopHeartbeat();
      stopPolling();
      if (tripLayer) tripLayer.clearLayers();
      const bar = document.getElementById('sharedTripBar');
      if (bar) bar.hidden = true;
    } else if (next !== publicToken) {
      publicToken = next;
      currentTrip = null;
      centeredOnce = false;
      stopHeartbeat();
      startPolling();
    }
  });
  window.addEventListener('DOMContentLoaded', bindUi);
})();
