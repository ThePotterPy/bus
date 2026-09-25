const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');

test('bus popup exposes sharing without claiming the passenger is on board', () => {
  assert.match(html, />\s*Compartir este bus\s*</);
  assert.doesNotMatch(html, />\s*Estoy en este bus\s*</);
  assert.doesNotMatch(html, />\s*Estoy en esta parada\s*</);
});

test('shared bus links isolate the selected unit and expire', () => {
  assert.match(html, /receivedUnits\.filter\(unit => String\(unit\.unit\) === String\(sharedBusView\.unitId\)\)/);
  assert.match(html, /until:\s*String\(Math\.floor\(Date\.now\(\) \/ 1000\) \+ 3 \* 60 \* 60\)/);
  assert.match(html, /Este enlace de seguimiento venció/);
});

test('a shared bus can be dismissed and restores the normal line view', () => {
  assert.match(html, /id="sharedTripLeave">Dejar de seguir</);
  assert.match(html, /window\.stopSharedBusView = function/);
  assert.match(html, /sharedBusView = null;/);
  assert.match(html, /lineSearch\.disabled = false;/);
  assert.match(html, /providerTabs\.hidden = false;/);
  assert.match(html, /history\.replaceState\(null, '', location\.pathname \+ location\.search\)/);
});

test('destination is optional and supports stops or a map point', () => {
  assert.match(html, /id="shareBusChooseStop"/);
  assert.match(html, /id="shareBusChooseMap"/);
  assert.match(html, /if \(draft\.destination\)/);
  assert.match(html, /tiempo estimado:/);
});

test('only explicitly saved stops are added to the search results', () => {
  assert.match(html, /getSavedStops\(\)/);
  assert.match(html, />Mis paradas</);
  assert.match(html, /toggleStopFavorite/);
  assert.match(html, />Compartir parada</);
});
