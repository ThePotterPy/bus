(function () {
  const settingsNewsDot = document.getElementById('settingsNewsDot');
  const newsBell = document.getElementById('newsBell');
  const newsBellDot = document.getElementById('newsBellDot');
  const newsOpen = document.getElementById('newsOpen');
  const newsPillNew = document.getElementById('newsPillNew');
  const newsOverlay = document.getElementById('newsOverlay');
  const newsClose = document.getElementById('newsClose');
  const newsContainer = document.getElementById('newsContainer');
  let newsCache = [];
  let latestNewsId = 0;
  const LAST_SEEN_KEY = 'jaha_last_seen_news_id';

  function readLastSeenNewsId() {
    let stored = 0;
    try { stored = parseInt(localStorage.getItem(LAST_SEEN_KEY) || '0', 10) || 0; } catch (_) {}
    try {
      const match = document.cookie.match(/(?:^|;\s*)jaha_last_seen_news_id=(\d+)/);
      if (match) stored = Math.max(stored, parseInt(match[1], 10) || 0);
    } catch (_) {}
    return stored;
  }

  function saveLastSeenNewsId(id) {
    const value = String(id);
    try { localStorage.setItem(LAST_SEEN_KEY, value); } catch (_) {}
    try {
      document.cookie = `${LAST_SEEN_KEY}=${encodeURIComponent(value)}; Max-Age=31536000; Path=/; SameSite=Lax`;
    } catch (_) {}
  }

  function setUnreadIndicators(visible) {
    if (settingsNewsDot) settingsNewsDot.hidden = !visible;
    if (newsPillNew) newsPillNew.hidden = !visible;
    if (newsBellDot) newsBellDot.hidden = !visible;
  }

  function getClientId() {
    try { return localStorage.getItem('jaha_client_id') || ''; } catch (_) { return ''; }
  }

  async function saveReadCursorToServer(id) {
    const clientId = getClientId();
    if (!clientId || !id) return;
    try {
      await fetch('/api/news/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ clientId, newsId: id }),
        keepalive: true,
      });
    } catch (_) {
      // The local cursor remains a fallback if the user is offline.
    }
  }

  function formatTime(timestampSec) {
    try {
      const d = new Date(timestampSec * 1000);
      return d.toLocaleDateString('es-PY', {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return '';
    }
  }

  function renderNewsList(items) {
    newsContainer.replaceChildren();
    if (!items || !items.length) {
      const empty = document.createElement('div');
      empty.className = 'news-empty-state';
      empty.textContent = 'No hay novedades publicadas por el momento.';
      newsContainer.append(empty);
      return;
    }

    items.forEach(item => {
      const card = document.createElement('article');
      card.className = 'news-card';

      const head = document.createElement('div');
      head.className = 'news-card-header';

      const tag = document.createElement('span');
      const tagType = (item.tag || 'novedad').toLowerCase();
      tag.className = `news-tag tag-${tagType}`;
      tag.textContent = tagType;

      const date = document.createElement('span');
      date.className = 'news-date';
      date.textContent = formatTime(item.created_at);

      head.append(tag, date);

      const title = document.createElement('h3');
      title.className = 'news-card-title';
      title.textContent = item.title;

      const content = document.createElement('p');
      content.className = 'news-card-content';
      content.textContent = item.content;

      card.append(head, title, content);
      newsContainer.append(card);
    });
  }

  async function fetchNews() {
    try {
      const clientId = getClientId();
      const query = clientId ? `?clientId=${encodeURIComponent(clientId)}` : '';
      const res = await fetch(`/api/news${query}`, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      newsCache = Array.isArray(data.items) ? data.items : [];
      if (newsCache.length > 0) {
        latestNewsId = Math.max(0, ...newsCache.map(n => Number(n.id) || 0));
        const localReadId = readLastSeenNewsId();
        const serverReadId = Number(data.lastSeenNewsId) || 0;
        setUnreadIndicators(latestNewsId > Math.max(localReadId, serverReadId));
        if (localReadId > serverReadId) saveReadCursorToServer(localReadId);
      } else {
        latestNewsId = 0;
        setUnreadIndicators(false);
      }
    } catch {
      // Ignorar fallos de red silenciosamente
    }
  }

  function openNews() {
    renderNewsList(newsCache);
    if (newsOverlay) newsOverlay.hidden = false;

    // Cerrar panel de ajustes si estaba abierto
    const settingsPanel = document.getElementById('settingsPanel');
    const settingsBtn = document.getElementById('settingsBtn');
    if (settingsPanel) settingsPanel.classList.remove('open');
    if (settingsBtn) settingsBtn.classList.remove('active');

    // Marcar lo que ya se mostró; después actualizamos la lista sin bloquear el panel.
    markVisibleNewsAsRead();
    fetchNews().then(() => {
      if (!newsOverlay || !newsOverlay.hidden) {
        renderNewsList(newsCache);
        markVisibleNewsAsRead();
      }
    });
  }

  function markVisibleNewsAsRead() {
    if (latestNewsId <= 0) return;
    setUnreadIndicators(false);
    saveLastSeenNewsId(latestNewsId);
    saveReadCursorToServer(latestNewsId);
  }

  function closeNews() {
    newsOverlay.hidden = true;
  }

  if (newsOpen) newsOpen.addEventListener('click', openNews);
  if (newsBell) newsBell.addEventListener('click', openNews);
  if (newsClose) newsClose.addEventListener('click', closeNews);
  if (newsOverlay) {
    newsOverlay.addEventListener('click', (e) => {
      if (e.target === newsOverlay) closeNews();
    });
  }

  document.addEventListener('keydown', (e) => {
    if (newsOverlay && !newsOverlay.hidden && e.key === 'Escape') {
      closeNews();
    }
  });

  // Consultar novedades al inicio
  fetchNews();
})();
