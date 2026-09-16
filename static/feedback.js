(function () {
  const overlay = document.getElementById('feedbackOverlay');
  const form = document.getElementById('feedbackForm');
  const status = document.getElementById('feedbackStatus');
  const submit = document.getElementById('feedbackSubmit');
  const line = document.getElementById('feedbackLine');
  const copyCode = document.getElementById('feedbackCopyCode');
  let lastDeleteCode = '';
  let returnFocus = null;

  fetch('/api/feedback/config').then(response => response.json()).then(config => {
    if (!config.enabled) document.getElementById('feedbackSettingsRow').hidden = true;
  }).catch(() => {});

  function openFeedback() {
    returnFocus = document.activeElement;
    line.value = typeof currentLine !== 'undefined' && currentLine && String(currentLine) !== '__all__'
      ? String(currentLine) : '';
    status.textContent = '';
    status.className = '';
    copyCode.hidden = !lastDeleteCode;
    overlay.hidden = false;
    document.getElementById('settingsPanel').classList.remove('open');
    document.getElementById('settingsBtn').classList.remove('active');
    document.getElementById('feedbackMessage').focus();
  }

  function closeFeedback() {
    overlay.hidden = true;
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
  }

  function diagnostics() {
    const ua = navigator.userAgent || '';
    const platform = navigator.userAgentData?.platform || navigator.platform || '';
    const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox'
      : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Otro';
    const device = /iPad|Tablet/i.test(ua) ? 'Tablet' : /Mobi|Android|iPhone/i.test(ua) ? 'Teléfono' : 'Computadora';
    const system = /Android/i.test(platform + ua) ? 'Android' : /iPhone|iPad|iOS/i.test(platform + ua) ? 'iOS'
      : /Windows/i.test(platform + ua) ? 'Windows' : /Mac/i.test(platform + ua) ? 'macOS'
      : /Linux/i.test(platform + ua) ? 'Linux' : 'Otro';
    return {
      device,
      platform: system,
      browser,
      viewport: `${Math.round(window.innerWidth / 100) * 100} px aprox.`,
      app: 'web-feedback-1',
    };
  }

  document.getElementById('feedbackOpen').addEventListener('click', openFeedback);
  document.getElementById('feedbackClose').addEventListener('click', closeFeedback);
  overlay.addEventListener('click', event => {
    if (event.target === overlay) closeFeedback();
  });
  document.addEventListener('keydown', event => {
    if (overlay.hidden) return;
    if (event.key === 'Escape') closeFeedback();
    if (event.key !== 'Tab') return;
    const controls = [...overlay.querySelectorAll('button, input:not([tabindex="-1"]), select, textarea')]
      .filter(element => !element.disabled);
    const first = controls[0], last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const consent = document.getElementById('feedbackDiagnostics').checked;
    const payload = {
      category: document.getElementById('feedbackCategory').value,
      lineId: line.value.trim(),
      name: document.getElementById('feedbackName').value.trim(),
      message: document.getElementById('feedbackMessage').value.trim(),
      website: document.getElementById('feedbackWebsite').value,
      diagnosticsConsent: consent,
    };
    if (consent) payload.diagnostics = diagnostics();
    submit.disabled = true;
    status.className = '';
    status.textContent = 'Enviando…';
    try {
      const response = await fetch('/api/feedback', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'No pudimos enviar el comentario.');
      form.reset();
      lastDeleteCode = result.code;
      copyCode.hidden = false;
      status.className = 'success';
      status.textContent = `¡Gracias! Lo recibimos. Guardá este código privado para poder eliminar el comentario más adelante: ${result.code}`;
    } catch (error) {
      status.className = 'error';
      status.textContent = error.message || 'No pudimos enviar el comentario. Intentá de nuevo.';
    } finally {
      submit.disabled = false;
    }
  });

  copyCode.addEventListener('click', async () => {
    if (!lastDeleteCode) return;
    try {
      await navigator.clipboard.writeText(lastDeleteCode);
      copyCode.textContent = 'Código copiado';
      setTimeout(() => { copyCode.textContent = 'Copiar código de eliminación'; }, 2500);
    } catch {
      status.textContent = `Copiá este código manualmente: ${lastDeleteCode}`;
    }
  });

  document.getElementById('feedbackDeleteForm').addEventListener('submit', async event => {
    event.preventDefault();
    const input = document.getElementById('feedbackDeleteCode');
    const deletionStatus = document.getElementById('feedbackDeleteStatus');
    if (!window.confirm('¿Eliminar definitivamente este comentario?')) return;
    deletionStatus.textContent = 'Eliminando…';
    deletionStatus.className = '';
    try {
      const response = await fetch('/api/feedback/delete', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: input.value.trim() }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'No se pudo eliminar.');
      if (input.value.trim() === lastDeleteCode) {
        lastDeleteCode = '';
        copyCode.hidden = true;
      }
      input.value = '';
      deletionStatus.className = 'success';
      deletionStatus.textContent = 'Comentario y datos técnicos eliminados.';
    } catch (error) {
      deletionStatus.className = 'error';
      deletionStatus.textContent = error.message || 'No se pudo eliminar.';
    }
  });
})();
