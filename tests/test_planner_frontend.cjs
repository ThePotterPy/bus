const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const code = html.split('// --- Lógica del Planificador de Viajes ---')[1].split('</script>')[0];

function element() {
  return {
    value: '', textContent: '', innerHTML: '', style: {}, children: [], handlers: {},
    classList: { toggle() {}, remove() {} },
    addEventListener(event, cb) { this.handlers[event] = cb; },
    append(...items) { this.children.push(...items); },
    appendChild(item) { this.children.push(item); },
    replaceChildren() { this.children = []; },
  };
}
function fixture() {
  const pending = [];
  const context = {
    AbortController, setTimeout, clearTimeout, console,
    currentLine: null, activePlanOption: null,
    userLocationMarker: null, lastUserAccuracy: null,
    map: { on() {}, removeLayer() {}, setView() {}, fitBounds() {} },
    L: { divIcon: v => v, marker: () => ({ addTo() { return this; }, setLatLng() {} }) },
    plannedTripLayer: { clearLayers() {} }, etaRouteLayer: { clearLayers() {} },
    applyBranchVisibility() {}, setStatus() {}, selectLine() {},
    document: { getElementById: element, addEventListener() {}, createElement: element },
    fetch: () => new Promise(resolve => pending.push(resolve)),
  };
  for (const key of ['originInput', 'destInput', 'originList', 'destList', 'originField', 'destField',
    'originAccuracy', 'planResult', 'planHint', 'planPanel', 'planBtn', 'clearPlanBtn', 'swapPlanBtn', 'calculatePlanBtn']) {
    context[key] = element();
  }
  vm.createContext(context);
  vm.runInContext(code, context);
  return { context, pending, run: js => vm.runInContext(js, context) };
}

test('clearing a pending trip prevents old results from reappearing', async () => {
  const f = fixture();
  f.run("setPlanPoint('origin', -25, -57, 'A'); setPlanPoint('dest', -25, -56.99, 'B');");
  const request = f.context.calculatePlanBtn.handlers.click();
  f.run('clearPlan()');
  f.pending[0]({ ok: true, json: async () => ({success: true, data: [{ name: 'obsolete' }]}) });
  await request;
  assert.equal(f.context.planResult.children.length, 0);
  assert.equal(f.context.calculatePlanBtn.disabled, true);
  assert.equal(f.run('planLoading'), false);
});

test('editing an address invalidates its selected coordinates', () => {
  const f = fixture();
  f.run("setPlanPoint('origin', -25, -57, 'A'); setPlanPoint('dest', -25, -56.99, 'B');");
  f.context.originInput.value = 'X';
  f.context.originInput.handlers.input();
  assert.equal(f.run('planOrigin'), null);
  assert.equal(f.context.calculatePlanBtn.disabled, true);
  assert.equal(f.run('planTarget'), 'origin');
});

test('address search only runs after explicit submission, not while typing', async () => {
  const f = fixture();
  f.context.originInput.value = 'Terminal';
  f.context.originInput.handlers.input();
  assert.equal(f.pending.length, 0);
  f.context.originInput.handlers.keydown({key: 'Enter', preventDefault() {}});
  assert.equal(f.pending.length, 1);
  f.pending[0]({ok: true, json: async () => ({success: true, data: []})});
  await new Promise(resolve => setImmediate(resolve));
});

test('clearing a pending address lookup discards its late suggestions', async () => {
  const f = fixture();
  const request = f.run("fetchGeocode('Terminal', originList, 'origin')");
  f.run('clearPlan()');
  f.pending[0]({ ok: true, json: async () => [{ lat: '-25', lon: '-57', display_name: 'Old result' }] });
  await request;
  assert.equal(f.context.originList.children.length, 0);
  assert.equal(f.context.originList.style.display, 'none');
});

test('choosing both points protects them from accidental map edits', () => {
  const f = fixture();
  f.run("setPlanPoint('origin', -25, -57, 'A'); setPlanPoint('dest', -25, -56.99, 'B');");
  assert.equal(f.run('planTarget'), null);
  f.context.swapPlanBtn.handlers.click();
  assert.equal(f.run('planOrigin.lon'), -56.99);
  assert.equal(f.context.originInput.value, 'B');
});

test('matching a planned route requires service and direction, even with shared IDs', () => {
  const context = { activePlanOption: {serviceId: 1, routeId: 7, direction: 'Ida'} };
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf('function matchesPlannedPolyline('), html.indexOf('function bestPolylineForUnit(')), context);
  assert.equal(context.matchesPlannedPolyline({serviceId: 2, routeId: 7, dirLabel: 'Ida'}), false);
  assert.equal(context.matchesPlannedPolyline({serviceId: 1, routeId: 7, dirLabel: 'Vuelta'}), false);
  assert.equal(context.matchesPlannedPolyline({serviceId: 1, routeId: 7, dirLabel: 'Ida'}), true);
});
