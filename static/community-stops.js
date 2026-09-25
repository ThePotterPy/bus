/**
 * Módulo para Reportes Comunitarios de Paradas en JAHA.
 * Permite a los usuarios reportar paradas en cualquier punto del país sacando una foto,
 * valida duplicados antes del envío y recibe notificaciones de aprobación o rechazo (Push e In-App).
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'jaha_my_stop_reports';

  // Inyectar HTML del modal si no existe
  function ensureModalHtml() {
    if (document.getElementById('stopReportOverlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'stopReportOverlay';
    overlay.hidden = true;
    overlay.innerHTML = `
      <div class="feedback-dialog stop-report-dialog" role="dialog" aria-modal="true" aria-labelledby="stopReportTitle" style="max-width:480px; width:92%; max-height:90vh; overflow-y:auto;">
        <div class="feedback-head" style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--panel-border, #353c4b); padding-bottom:10px; margin-bottom:14px;">
          <h2 id="stopReportTitle" style="margin:0; font-size:18px; color:var(--text, #fff); display:flex; align-items:center; gap:8px;">
            <span>📍 Reportar parada</span>
          </h2>
          <button type="button" class="feedback-close" id="stopReportClose" aria-label="Cerrar" style="background:transparent; border:none; color:var(--muted, #a6afc1); font-size:24px; cursor:pointer;">×</button>
        </div>

        <p style="font-size:13px; color:var(--muted, #a6afc1); margin:0 0 14px; line-height:1.4;">
          Ayudá a mejorar el mapa reportando paradas de colectivo en cualquier ciudad del país. Solo necesitás estar en el lugar y sacar una foto.
        </p>

        <form id="stopReportForm">
          <!-- Paso 1: GPS -->
          <div style="background:rgba(255,255,255,0.04); border:1px solid var(--panel-border, #353c4b); border-radius:10px; padding:12px; margin-bottom:14px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <span style="font-size:13px; font-weight:700; color:var(--text, #fff);">1. Ubicación actual</span>
              <span id="stopGpsBadge" style="font-size:11px; padding:2px 8px; border-radius:999px; background:#44371c; color:#ffd17c;">Sin ubicación</span>
            </div>
            <button type="button" class="btn-primary" id="stopGetGpsBtn" style="width:100%; min-height:40px; margin-top:4px;">
              🛰️ Obtener mi ubicación actual
            </button>
            <div id="stopGpsText" style="font-size:12px; color:var(--muted, #a6afc1); margin-top:6px;"></div>
            <div id="stopNearbyWarning" style="display:none; font-size:12px; color:#ffd17c; background:rgba(255,209,124,0.12); border:1px solid rgba(255,209,124,0.3); border-radius:8px; padding:8px; margin-top:8px;"></div>
          </div>

          <!-- Paso 2: Foto -->
          <div style="background:rgba(255,255,255,0.04); border:1px solid var(--panel-border, #353c4b); border-radius:10px; padding:12px; margin-bottom:14px;">
            <div style="font-size:13px; font-weight:700; color:var(--text, #fff); margin-bottom:6px;">
              2. Foto de la parada o refugio
            </div>
            <p style="font-size:12px; color:var(--muted, #a6afc1); margin:0 0 8px;">
              Sacá una foto clara donde se vea el cartel, refugio o la vereda de la parada.
            </p>
            <input type="file" id="stopPhotoInput" accept="image/*" capture="environment" style="display:none;">
            <button type="button" id="stopPhotoBtn" style="width:100%; min-height:42px; border:1px dashed #00d3ec; background:rgba(0,211,236,0.08); color:#00d3ec; border-radius:8px; font-weight:600; cursor:pointer;">
              📸 Sacar o elegir foto
            </button>
            <div id="stopPhotoPreviewContainer" style="display:none; margin-top:10px; text-align:center;">
              <img id="stopPhotoPreview" src="" alt="Vista previa" style="max-height:160px; max-width:100%; border-radius:8px; border:1px solid #4c586d; object-fit:cover;">
              <div style="margin-top:6px;">
                <button type="button" id="stopPhotoRemoveBtn" style="font-size:12px; background:transparent; border:none; color:#ff9fb2; text-decoration:underline; cursor:pointer;">Cambiar foto</button>
              </div>
            </div>
          </div>

          <!-- Paso 3: Detalles opcionales -->
          <div style="margin-bottom:14px;">
            <label for="stopDescInput" style="display:block; font-size:13px; font-weight:600; color:var(--text, #fff); margin-bottom:4px;">
              Descripción o referencia (opcional)
            </label>
            <input type="text" id="stopDescInput" maxlength="300" placeholder="Ej: Hay un refugio metálico frente a la farmacia" style="width:100%; padding:10px; border-radius:8px; background:var(--panel, #111722); color:#fff; border:1px solid var(--panel-border, #4c586d); font-size:13px;">
          </div>

          <div style="margin-bottom:14px;">
            <label for="stopNameInput" style="display:block; font-size:13px; font-weight:600; color:var(--text, #fff); margin-bottom:4px;">
              Tu nombre o alias (opcional)
            </label>
            <input type="text" id="stopNameInput" maxlength="60" placeholder="Podés dejarlo vacío" style="width:100%; padding:10px; border-radius:8px; background:var(--panel, #111722); color:#fff; border:1px solid var(--panel-border, #4c586d); font-size:13px;">
          </div>

          <!-- Paso 4: Notificaciones -->
          <div style="margin-bottom:16px;">
            <label style="display:flex; align-items:flex-start; gap:8px; font-size:13px; color:var(--text, #fff); cursor:pointer;">
              <input type="checkbox" id="stopNotifyCheck" checked style="margin-top:2px; width:auto;">
              <span>Recibir aviso push además del aviso dentro de JAHA (si ya activaste notificaciones en este dispositivo).</span>
            </label>
          </div>

          <div id="stopSubmitStatus" role="status" style="font-size:13px; margin-bottom:10px; min-height:18px;"></div>

          <button type="submit" class="btn-primary" id="stopSubmitBtn" style="width:100%; min-height:44px; font-size:15px; font-weight:700;" disabled>
            Enviar reporte de parada
          </button>
        </form>
      </div>
    `;
    document.body.appendChild(overlay);

    // Contenedor de Toasts para notificaciones in-app
    if (!document.getElementById('stopToastContainer')) {
      const toastContainer = document.createElement('div');
      toastContainer.id = 'stopToastContainer';
      toastContainer.style.cssText = 'position:fixed; bottom:20px; left:50%; transform:translateX(-50%); z-index:99999; display:flex; flex-direction:column; gap:10px; max-width:90%; width:380px; pointer-events:none;';
      document.body.appendChild(toastContainer);
    }
  }

  // Toast flotante animado para avisos al usuario
  function showToast(title, message, isSuccess = true) {
    const container = document.getElementById('stopToastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.style.cssText = `
      background: ${isSuccess ? '#122e23' : '#3d1b24'};
      border: 1px solid ${isSuccess ? '#21d19f' : '#ff7a90'};
      color: #fff;
      padding: 12px 16px;
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6);
      pointer-events: auto;
      font-size: 13px;
      line-height: 1.4;
      animation: stopToastIn 0.3s ease-out;
    `;
    const head = document.createElement('div');
    head.style.cssText = 'display:flex; justify-content:space-between; align-items:flex-start; gap:8px;';
    const heading = document.createElement('strong');
    heading.style.cssText = `color:${isSuccess ? '#7bf0ae' : '#ffb4c2'}; font-size:14px;`;
    heading.textContent = title;
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Cerrar notificación');
    closeBtn.style.cssText = 'background:transparent; border:none; color:#a6afc1; cursor:pointer; font-size:18px; line-height:1; padding:0;';
    closeBtn.textContent = '×';
    head.append(heading, closeBtn);
    const body = document.createElement('div');
    body.style.cssText = 'margin-top:4px; white-space:pre-wrap;';
    body.textContent = message;
    toast.append(head, body);

    closeBtn.addEventListener('click', () => toast.remove());

    container.appendChild(toast);
    setTimeout(() => {
      if (toast.isConnected) {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.4s';
        setTimeout(() => toast.remove(), 400);
      }
    }, 8000);
  }

  // Historial local de reportes en localStorage
  function getMyReports() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed)
        ? parsed.filter(r => r && Number.isSafeInteger(Number(r.id)) && Number(r.id) > 0)
          .map(r => ({ ...r, id: Number(r.id) }))
        : [];
    } catch {
      return [];
    }
  }

  function getClientId() {
    try {
      return (typeof clientId !== 'undefined' && clientId) || localStorage.getItem('jaha_client_id') || '';
    } catch {
      return '';
    }
  }

  function saveMyReport(reportData) {
    try {
      const list = getMyReports();
      list.unshift(reportData);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(0, 30)));
    } catch (e) {
      console.warn('Error guardando reporte en localStorage', e);
    }
  }

  // Chequeo de estado de reportes para notificar al usuario (In-App)
  async function checkUserReportsStatus() {
    const reports = getMyReports();
    if (!reports.length) return;

    // Solo consultar los que aún no tengan estado terminal notificado
    const pendingReports = reports.filter(r => r.status === 'pendiente' || !r.notified);
    if (!pendingReports.length) return;

    const ids = pendingReports.map(r => r.id).join(',');
    const cId = getClientId();
    if (!cId) return;

    try {
      const res = await fetch(`/api/stop-reports/my-status?clientId=${encodeURIComponent(cId)}&ids=${encodeURIComponent(ids)}`);
      if (!res.ok) return;
      const data = await res.json();
      if (!data.success || !Array.isArray(data.reports)) return;

      let changed = false;
      const allReports = getMyReports();

      data.reports.forEach(serverRep => {
        const localRep = allReports.find(r => r.id === serverRep.id);
        if (!localRep) return;

        // Si cambió a aprobada o rechazada y no fue notificado
        if (serverRep.status !== 'pendiente' && !localRep.notified) {
          localRep.status = serverRep.status;
          localRep.notified = true;
          changed = true;

          if (serverRep.status === 'aprobada') {
            showToast(
              '🎉 ¡Parada aprobada y agregada al mapa!',
              `Tu carga de parada en ${serverRep.stop_name || serverRep.street_name || 'tu zona'} fue aprobada por los moderadores. ¡Gracias por colaborar!`,
              true
            );
          } else if (serverRep.status === 'rechazada') {
            const reason = serverRep.rejection_reason || 'No cumple con los requisitos de paradas.';
            showToast(
              '❌ Tu carga de parada fue rechazada',
              `Motivo: "${reason}"`,
              false
            );
          }
        }
      });

      if (changed) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(allReports));
      }
    } catch (err) {
      console.warn('Error consultando estado de reportes', err);
    }
  }

  // Estado del modal de reporte
  let currentCoords = null;
  let selectedFile = null;

  function initModalLogic() {
    const overlay = document.getElementById('stopReportOverlay');
    const closeBtn = document.getElementById('stopReportClose');
    const form = document.getElementById('stopReportForm');
    const getGpsBtn = document.getElementById('stopGetGpsBtn');
    const gpsText = document.getElementById('stopGpsText');
    const gpsBadge = document.getElementById('stopGpsBadge');
    const nearbyWarning = document.getElementById('stopNearbyWarning');
    const photoBtn = document.getElementById('stopPhotoBtn');
    const photoInput = document.getElementById('stopPhotoInput');
    const previewContainer = document.getElementById('stopPhotoPreviewContainer');
    const previewImg = document.getElementById('stopPhotoPreview');
    const photoRemoveBtn = document.getElementById('stopPhotoRemoveBtn');
    const submitBtn = document.getElementById('stopSubmitBtn');
    const submitStatus = document.getElementById('stopSubmitStatus');

    let locationValid = false;

    function updateSubmitState() {
      submitBtn.disabled = !(currentCoords && selectedFile && locationValid);
    }

    function openModal() {
      currentCoords = null;
      selectedFile = null;
      locationValid = false;
      gpsText.textContent = '';
      gpsBadge.textContent = 'Sin ubicación';
      gpsBadge.style.background = '#44371c';
      gpsBadge.style.color = '#ffd17c';
      nearbyWarning.style.display = 'none';
      previewContainer.style.display = 'none';
      previewImg.removeAttribute('src');
      photoInput.value = '';
      photoBtn.style.display = 'block';
      submitStatus.textContent = '';
      submitBtn.disabled = true;
      overlay.hidden = false;
      overlay.style.display = 'flex';

      // Cerrar panel de ajustes si está abierto
      const settingsPanel = document.getElementById('settingsPanel');
      if (settingsPanel) settingsPanel.classList.remove('open');
      const settingsBtn = document.getElementById('settingsBtn');
      if (settingsBtn) settingsBtn.classList.remove('active');

      // Intentar auto-obtener GPS al abrir
      getGps();
    }

    function closeModal() {
      overlay.hidden = true;
      overlay.style.display = 'none';
    }

    closeBtn.addEventListener('click', closeModal);
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeModal();
    });
    document.addEventListener('keydown', (e) => {
      if (!overlay.hidden && e.key === 'Escape') closeModal();
    });

    // Obtener GPS y verificar cercanía a líneas de colectivo
    async function getGps() {
      getGpsBtn.disabled = true;
      getGpsBtn.textContent = 'Buscando GPS…';
      gpsBadge.textContent = 'Buscando…';
      locationValid = false;

      try {
        const pos = await new Promise((resolve, reject) => {
          if (!navigator.geolocation) return reject(new Error('GPS no disponible'));
          navigator.geolocation.getCurrentPosition(resolve, reject, {
            enableHighAccuracy: true,
            timeout: 15000,
            maximumAge: 0
          });
        });

        const lat = pos.coords.latitude;
        const lon = pos.coords.longitude;
        const acc = Math.round(pos.coords.accuracy || 0);

        currentCoords = { lat, lon, accuracy: acc };
        gpsText.textContent = `Lat: ${lat.toFixed(5)}, Lon: ${lon.toFixed(5)} (±${acc}m)`;

        // Validar cercanía con recorridos de colectivos en backend
        await checkLocationOnServer(lat, lon, acc);
      } catch (err) {
        currentCoords = null;
        locationValid = false;
        gpsText.textContent = 'No pudimos acceder a tu GPS. Por favor revisá los permisos de ubicación de tu dispositivo.';
        gpsBadge.textContent = 'Error GPS';
        gpsBadge.style.background = '#482333';
        gpsBadge.style.color = '#ff9fb2';
        nearbyWarning.style.display = 'none';
      } finally {
        getGpsBtn.disabled = false;
        getGpsBtn.textContent = '🛰️ Actualizar mi ubicación';
        updateSubmitState();
      }
    }

    getGpsBtn.addEventListener('click', getGps);

    // Verificación de ubicación contra recorridos de buses y duplicados
    async function checkLocationOnServer(lat, lon, acc) {
      nearbyWarning.style.display = 'block';
      nearbyWarning.style.color = '#ffd17c';
      nearbyWarning.style.background = 'rgba(255,209,124,0.1)';
      nearbyWarning.style.border = '1px solid rgba(255,209,124,0.3)';
      nearbyWarning.textContent = 'Verificando cercanía con recorridos de colectivos…';

      try {
        const res = await fetch(`/api/stop-reports/check-location?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}&accuracy=${encodeURIComponent(acc)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        if (!data.success || !data.valid) {
          locationValid = false;
          nearbyWarning.style.display = 'block';
          nearbyWarning.style.color = '#ff9fb2';
          nearbyWarning.style.background = 'rgba(255,159,178,0.12)';
          nearbyWarning.style.border = '1px solid rgba(255,159,178,0.3)';
          nearbyWarning.textContent = `❌ ${data.error || 'Ubicación no permitida'}`;
          gpsBadge.textContent = data.reason === 'no_bus_routes' ? 'Sin buses cerca' : 'No válida';
          gpsBadge.style.background = '#482333';
          gpsBadge.style.color = '#ff9fb2';
          return;
        }

        // Ubicación válida con líneas cercanas
        locationValid = true;
        const lineNames = (data.lines || []).map(l => l.line_name).slice(0, 4).join(', ');
        const dist = Math.round(data.distance_to_route_m || 0);
        nearbyWarning.style.display = 'block';
        nearbyWarning.style.color = '#7bf0ae';
        nearbyWarning.style.background = 'rgba(123,240,174,0.12)';
        nearbyWarning.style.border = '1px solid rgba(123,240,174,0.3)';
        nearbyWarning.textContent = `🚌 Calle con transporte público (a ${dist}m de ruta). Líneas registradas: ${lineNames || 'Detectadas'}.`;
        gpsBadge.textContent = 'En recorrido';
        gpsBadge.style.background = '#173b2a';
        gpsBadge.style.color = '#7bf0ae';
      } catch (err) {
        console.warn('Error verificando ubicación:', err);
        locationValid = false;
        nearbyWarning.style.display = 'block';
        nearbyWarning.style.color = '#ffd17c';
        nearbyWarning.style.background = 'rgba(255,209,124,0.12)';
        nearbyWarning.style.border = '1px solid rgba(255,209,124,0.3)';
        nearbyWarning.textContent = 'No pudimos verificar esta ubicación. Revisá tu conexión y volvé a actualizarla.';
        gpsBadge.textContent = 'Sin verificar';
        gpsBadge.style.background = '#44371c';
        gpsBadge.style.color = '#ffd17c';
      }
    }

    function haversineM(lat1, lon1, lat2, lon2) {
      const R = 6371000;
      const dLat = (lat2 - lat1) * Math.PI / 180;
      const dLon = (lon2 - lon1) * Math.PI / 180;
      const a = Math.sin(dLat/2) * Math.sin(dLat/2) +
                Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                Math.sin(dLon/2) * Math.sin(dLon/2);
      return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
    }

    // Cámara y Foto
    photoBtn.addEventListener('click', () => photoInput.click());
    photoInput.addEventListener('change', () => {
      const file = photoInput.files && photoInput.files[0];
      if (!file) return;

      selectedFile = file;
      const reader = new FileReader();
      reader.onload = (e) => {
        previewImg.src = e.target.result;
        previewContainer.style.display = 'block';
        photoBtn.style.display = 'none';
        updateSubmitState();
      };
      reader.readAsDataURL(file);
    });

    photoRemoveBtn.addEventListener('click', () => {
      selectedFile = null;
      photoInput.value = '';
      previewContainer.style.display = 'none';
      photoBtn.style.display = 'block';
      updateSubmitState();
    });

    // Envío del Formulario
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!currentCoords || !selectedFile || !locationValid) return;
      const cId = getClientId();
      if (!cId) {
        submitStatus.textContent = '❌ No pudimos identificar este dispositivo para dar seguimiento al reporte. Activá el almacenamiento del sitio y volvé a intentar.';
        submitStatus.style.color = '#ff9fb2';
        return;
      }

      submitBtn.disabled = true;
      submitBtn.textContent = 'Enviando foto y reporte…';
      submitStatus.textContent = '';
      submitStatus.className = '';

      const formData = new FormData();
      formData.append('photo', selectedFile);
      formData.append('lat', String(currentCoords.lat));
      formData.append('lon', String(currentCoords.lon));
      formData.append('accuracy', String(currentCoords.accuracy));
      formData.append('clientId', cId);
      formData.append('description', document.getElementById('stopDescInput').value.trim());
      formData.append('name', document.getElementById('stopNameInput').value.trim());

      // Capturar pushSubscription si el usuario aceptó
      const notifyCheck = document.getElementById('stopNotifyCheck');
      if (notifyCheck && notifyCheck.checked && 'serviceWorker' in navigator && 'PushManager' in window) {
        try {
          const reg = await navigator.serviceWorker.ready;
          const sub = await reg.pushManager.getSubscription();
          if (sub) {
            formData.append('pushSubscription', JSON.stringify(sub));
          }
        } catch {}
      }

      try {
        const res = await fetch('/api/stop-report', {
          method: 'POST',
          body: formData
        });
        const data = await res.json();

        if (!res.ok || !data.success) {
          throw new Error(data.error || 'Ocurrió un error al enviar el reporte');
        }

        // Guardar en historial local para seguimiento in-app
        saveMyReport({
          id: data.id,
          createdAt: Math.floor(Date.now() / 1000),
          status: 'pendiente',
          notified: false
        });

        submitStatus.textContent = '✅ ¡Gracias! Tu parada fue enviada y será revisada por un moderador. Te avisaremos dentro de JAHA; el aviso push depende de que ya tengas activadas las notificaciones.';
        submitStatus.style.color = '#7bf0ae';
        submitBtn.textContent = 'Reporte enviado';

        setTimeout(() => {
          closeModal();
          showToast(
            '📍 Reporte recibido',
            'Te notificaremos cuando el moderador revise y confirme tu parada.',
            true
          );
        }, 1500);
      } catch (err) {
        submitStatus.textContent = `❌ ${err.message}`;
        submitStatus.style.color = '#ff9fb2';
        submitBtn.disabled = false;
        submitBtn.textContent = 'Enviar reporte de parada';
      }
    });

    // Exponer función de apertura global
    window.openStopReportModal = openModal;
  }

  let initialized = false;
  function setup() {
    if (initialized) return;
    ensureModalHtml();
    initModalLogic();

    const bindButton = (id) => {
      const btn = document.getElementById(id);
      if (btn && !btn._stopReportBound) {
        btn._stopReportBound = true;
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          if (typeof window.openStopReportModal === 'function') {
            window.openStopReportModal();
          }
        });
      }
    };

    bindButton('stopReportOpen');
    bindButton('openReportFromLayers');

    // Chequear periódicamente el estado de reportes anteriores para notificaciones in-app
    checkUserReportsStatus();
    setInterval(checkUserReportsStatus, 60000); // Cada 60 segundos
    initialized = true;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setup);
  } else {
    setup();
  }
})();
