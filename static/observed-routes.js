/* Shared observations: browsers only read evidence; the server owns counts. */
(() => {
  const history = document.getElementById('observedHistory');
  const notice = document.getElementById('observedNotice');
  const renderer = L.canvas({ padding: 0.2 });
  let signature = '', nextRefresh = 0, controller = null;
  let busy = false;
  // Granate resalta mejor que el celeste sobre el fondo claro del mapa.
  // El celeste queda reservado para muestras GPS todavía sin ajustar.
  const color = n => n >= 4 ? '#a855f7' : n === 3 ? '#2684ff' : n === 2 ? '#FFA500' : '#8B1E3F';

  function branchFor(route) {
    if (!route) {
      let branch = branchByKey('__unidentified__');
      if (!branch) {
        branch = { key: '__unidentified__', label: 'Sin ramal identificado', polylines: [], layers: [] };
        lineBranches.push(branch);
      }
      return branch.key;
    }
    // Keep observed branches selectable even if the provider has no geometry.
    const exact = branchByKey(normalizeBranchText(route));
    return exact ? exact.key : ensureLineBranch(route).key;
  }

  function paint(data, layer, individual) {
    layer.clearLayers();
    const groups = new Map();
    const getGroup = route => {
      if (!groups.has(route)) {
        const group = L.layerGroup().addTo(layer);
        groups.set(route, group);
        if (individual) observedDeviationItems.push({ layer: group, branchKey: branchFor(route) });
      }
      return groups.get(route);
    };
    const bundles = new Map();
    for (const item of data.alternatives || []) {
      const key = JSON.stringify([item.route, item.buses, item.passes, item.archived, item.kind]);
      if (!bundles.has(key)) bundles.set(key, { item, points: [] });
      bundles.get(key).points.push(item.points);
    }
    for (const { item, points } of bundles.values()) {
      const title = item.kind === 'observed' ? 'Recorrido observado (sin trazado oficial)' : 'Trayecto alternativo observado';
      L.polyline(points, { renderer, color: item.archived ? '#9ca3af' : color(item.buses),
        weight: 4, opacity: item.archived ? 0.5 : 0.9, dashArray: '7 6' })
        .bindPopup(`<b>${title}</b><br>${escapeHtml(item.route || 'Sin ramal identificado')}<br>` +
          `${item.buses} buses distintos · ${item.passes} pasadas en 14 días` +
          (item.archived ? '<br>Histórica: sin actividad en los últimos 14 días' : ''))
        .addTo(getGroup(item.route));
    }
    // GPS points are evidence, not proof of the connecting road. Never draw
    // straight connectors across buildings when matching is pending or fails.
    const seen = new Set();
    function points(items, active) {
      for (const item of items || []) {
        for (const p of item.points || []) {
          const key = `${item.route}|${p[0]}|${p[1]}`;
          if (seen.has(key)) continue;
          seen.add(key);
          L.circleMarker([p[0], p[1]], { renderer, radius: active ? 3 : 2,
            color: '#00e5ff', weight: 1, opacity: 0.65, fillOpacity: 0.45 })
            .bindPopup(`${active ? 'Estela GPS en vivo' : 'Trayecto GPS pendiente de ajuste a calles'}<br>` +
              `Unidad ${escapeHtml(item.unit)} · ${escapeHtml(item.route || 'Sin ramal identificado')}`)
            .addTo(getGroup(item.route || ''));
        }
      }
    }
    points(data.tails, true);
    points(data.pending, false);
    return groups.size;
  }

  function currentSignature() {
    return JSON.stringify([String(currentLine), allLinesGeneration, routeLoadGeneration, history.checked]);
  }

  async function refresh() {
    if (!currentLine || document.hidden) return;
    const key = currentSignature();
    if (key !== signature) {
      controller?.abort();
      signature = key;
      nextRefresh = 0;
      busy = false;
    }
    if (busy || Date.now() < nextRefresh) return;
    busy = true;
    const request = new AbortController();
    controller = request;
    const timeout = setTimeout(() => request.abort(), 25000);
    try {
      if (allLinesMode) {
        let visible = false, pending = false;
        const ids = [...allLineStates.keys()];
        for (let i = 0; i < ids.length; i += 25) {
          const res = await fetch('/api/observed-routes/batch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: request.signal,
            body: JSON.stringify({ lines: ids.slice(i, i+25), history: history.checked }),
          });
          const data = await res.json();
          if (!res.ok || !data.success) throw new Error('No se pudieron cargar las observaciones');
          if (key !== currentSignature()) return;
          for (const [id, snapshot] of Object.entries(data.lines)) {
            const state = allLineStates.get(id);
            if (!state) continue;
            visible = !!paint(snapshot, state.observed, false) || visible;
            pending = !!snapshot.pending?.length || pending;
          }
        }
        observedRouteLegend.classList.add('visible');
        notice.textContent = pending ? 'Los puntos celestes esperan un ajuste confiable a las calles.' :
          visible ? '' : 'El servidor está reuniendo evidencia de los recorridos.';
      } else {
        const res = await fetch('/api/observed-routes?line=' + encodeURIComponent(currentLine) +
          (history.checked ? '&history=1' : ''), { signal: request.signal });
        const data = await res.json();
        if (!res.ok || !data.success) throw new Error('No se pudieron cargar las observaciones');
        if (key !== currentSignature()) return;
        observedDeviationItems.length = 0;
        paint(data, observedDeviationLayer, true);
        renderBranchSelector();
        applyBranchVisibility();
        observedRouteLegend.classList.add('visible');
        notice.textContent = !data.matching_enabled ? 'Ajuste a calles desactivado; se conservan los puntos GPS.' :
          data.pending?.length ? 'Los puntos celestes esperan un ajuste confiable a las calles.' :
          data.truncated ? 'Hay más historia disponible que la mostrada en esta vista.' :
          !data.alternatives.length && !data.tails.length ? 'El servidor está reuniendo evidencia de este recorrido.' : '';
      }
      nextRefresh = Date.now() + (allLinesMode ? 60000 : 15000);
    } catch (err) {
      if (key === currentSignature() && err.name !== 'AbortError') {
        notice.textContent = 'No se pudo actualizar el historial. Reintentando…';
        observedRouteLegend.classList.add('visible');
        nextRefresh = Date.now()+10000;
      }
    } finally {
      clearTimeout(timeout);
      if (controller === request) busy = false;
    }
  }
  history.addEventListener('change', () => { nextRefresh = 0; refresh(); });
  setInterval(refresh, 1000);
  document.addEventListener('visibilitychange', refresh);
})();
