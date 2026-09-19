(function () {
  const settingsNewsDot = document.getElementById('settingsNewsDot');
  const newsOpen = document.getElementById('newsOpen');
  const newsPillNew = document.getElementById('newsPillNew');
  const newsOverlay = document.getElementById('newsOverlay');
  const newsClose = document.getElementById('newsClose');
  const newsContainer = document.getElementById('newsContainer');
  let newsCache = [];
  let latestNewsId = 0;

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
      const res = await fetch('/api/news', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      newsCache = data.items || [];
      if (newsCache.length > 0) {
        latestNewsId = Math.max(...newsCache.map(n => n.id));
        const lastSeen = parseInt(localStorage.getItem('jaha_last_seen_news_id') || '0', 10);
        if (latestNewsId > lastSeen) {
          if (settingsNewsDot) settingsNewsDot.hidden = false;
          if (newsPillNew) newsPillNew.hidden = false;
        }
      }
    } catch {
      // Ignorar fallos de red silenciosamente
    }
  }

  function openNews() {
    renderNewsList(newsCache);
    newsOverlay.hidden = false;

    // Cerrar panel de ajustes si estaba abierto
    const settingsPanel = document.getElementById('settingsPanel');
    const settingsBtn = document.getElementById('settingsBtn');
    if (settingsPanel) settingsPanel.classList.remove('open');
    if (settingsBtn) settingsBtn.classList.remove('active');

    // Marcar como visto
    if (latestNewsId > 0) {
      localStorage.setItem('jaha_last_seen_news_id', String(latestNewsId));
      if (settingsNewsDot) settingsNewsDot.hidden = true;
      if (newsPillNew) newsPillNew.hidden = true;
    }
  }

  function closeNews() {
    newsOverlay.hidden = true;
  }

  if (newsOpen) newsOpen.addEventListener('click', openNews);
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
