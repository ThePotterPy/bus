const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const news = fs.readFileSync(path.join(__dirname, '../static/news.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');

test('viewed news is persisted with a durable fallback and hidden immediately', () => {
  assert.match(news, /localStorage\.setItem\(LAST_SEEN_KEY, value\)/);
  assert.match(news, /document\.cookie = `\$\{LAST_SEEN_KEY\}/);
  assert.match(news, /Max-Age=31536000; Path=\/; SameSite=Lax/);
  assert.match(news, /setUnreadIndicators\(latestNewsId > Math\.max\(localReadId, serverReadId\)\)/);
  assert.match(news, /setUnreadIndicators\(false\);\s*saveLastSeenNewsId\(latestNewsId\);/);
  assert.match(html, /id="newsBellDot" hidden/);
});
