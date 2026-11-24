'use strict';
const {readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();
const html = readStatic('index.html');
const attr = (text, name) => text.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1] || '';

function fixture() {
  const calls = [], nodes = new Map();
  class Element {
    constructor(attributes = '') {
      this.value = attr(attributes, 'value'); this.min = attr(attributes, 'min'); this.max = attr(attributes, 'max');
      this.step = attr(attributes, 'step');
      this.dataset = {}; this.checked = false; this.disabled = false; this.isConnected = true; this.validity = {};
      this.fields = []; this.rows = []; this.textContent = ''; this.listeners = {};
      const classes = new Set();
      this.classList = {contains: name => classes.has(name), add: name => classes.add(name),
        remove: name => classes.delete(name), toggle: (name, value) => value ? classes.add(name) : classes.delete(name)};
    }
    checkValidity() {
      if (this.value === '') return !this.validity.badInput;
      const value = Number(this.value);
      const step = this.step && this.step !== 'any' ? Number(this.step) : null;
      const steps = step ? (value - Number(this.min || 0)) / step : 0;
      return !this.validity.badInput && Number.isFinite(value)
        && (this.min === '' || value >= Number(this.min)) && (this.max === '' || value <= Number(this.max))
        && (!step || Math.abs(steps - Math.round(steps)) < 1e-7);
    }
    reportValidity() { return this.fields.every(field => field.input.disabled || field.input.checkValidity()); }
    querySelector(selector) {
      if (selector === 'input') return this.input;
      if (selector === 'small') return this.small || null;
      const match = selector.match(/^\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]$/);
      if (match) {
        const key = match[1], value = match[2];
        return this.fields.find(field => field.key === key && (value === undefined || field.name === value))?.input || null;
      }
      return null;
    }
    querySelectorAll(selector) {
      if (selector === '.flight-gpu-oc-row') return this.rows;
      if (selector === '[data-device-gpu-oc-batch-saved]') return this.fields.filter(field => field.key === 'data-device-gpu-oc-batch-saved').map(field => field.input);
      if (selector === 'input, select, textarea, button' && this.id === 'flightGpuOcSection') return [node('addFlightGpuOcRowButton'), ...node('flightGpuOcRows').rows.flatMap(row => row.fields.filter(field => field.key === 'data-flight-gpu-field').map(field => field.input))];
      if (selector.includes('batch-control')) return this.fields.filter(field => field.key === 'data-device-gpu-batch-field').map(field => field.label);
      if (selector.includes('data-device-gpu-control')) return this.fields.map(field => field.label);
      if (selector.includes('data-device-gpu-field')) return this.fields.map(field => field.input);
      return [];
    }
    set innerHTML(markup) {
      this.markup = markup; this.fields = [];
      for (const match of markup.matchAll(/<input\b([^>]*data-flight-gpu-field="([^"]+)"[^>]*)>/g)) {
        this.fields.push({key: 'data-flight-gpu-field', name: match[2], input: new Element(match[1])});
      }
      for (const key of ['data-remove-flight-gpu-row', 'data-flight-gpu-index-hint']) {
        if (markup.includes(key)) this.fields.push({key, input: new Element()});
      }
      for (const match of markup.matchAll(/<small\b[^>]*data-flight-gpu-hint="([^"]+)"[^>]*>([\s\S]*?)<\/small>/g)) {
        const hint = new Element(); hint.textContent = match[2]; this.fields.push({key: 'data-flight-gpu-hint', name: match[1], input: hint});
      }
      for (const match of markup.matchAll(/<label\b[^>]*data-flight-gpu-control="([^"]+)"[^>]*>/g)) {
        this.fields.push({key: 'data-flight-gpu-control', name: match[1], input: new Element(match[0])});
      }
      for (const match of markup.matchAll(/<span\b[^>]*data-device-gpu-oc-batch-saved="([^"]+)"[^>]*>([\s\S]*?)<\/span>/g)) {
        const span = new Element(); span.dataset.deviceGpuOcBatchSaved = match[1]; span.textContent = match[2];
        this.fields.push({key: 'data-device-gpu-oc-batch-saved', name: match[1], input: span});
      }
    }
    get innerHTML() { return this.markup || ''; }
    append(row) { row.parent = this; this.rows.push(row); }
    remove() { if (this.parent) this.parent.rows = this.parent.rows.filter(row => row !== this); }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    reset() { this.fields.forEach(field => { field.input.value = ''; field.input.checked = false; }); }
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) { const element = new Element(); element.id = match[1]; nodes.set(match[1], element); }
  const node = id => { assert.ok(nodes.has(id), id); return nodes.get(id); };
  function addControls(formId, pattern, key, datasetKey) {
    const form = node(formId);
    for (const match of html.matchAll(pattern)) {
      const input = new Element(match[2]), label = new Element(); label.input = input; label.dataset[datasetKey] = match[1];
      label.small = new Element();
      input.dataset.deviceGpuField = match[1];
      form.fields.push({key, name: match[1], input, label});
      if (key.includes('batch')) {
        form.fields.push({key: 'data-device-gpu-batch-reset', name: match[1], input: new Element()});
        form.fields.push({key: 'data-device-gpu-batch-reset-label', name: match[1], input: new Element()});
      }
    }
  }
  addControls('deviceGpuOcForm', /<label data-device-gpu-control="([^"]+)"[^>]*>[\s\S]*?<input\b([^>]*)>/g,
    'data-device-gpu-field', 'deviceGpuControl');
  addControls('deviceGpuOcBatchForm', /<div data-device-gpu-batch-control="([^"]+)"[^>]*>[\s\S]*?<input\b([^>]*)>/g,
    'data-device-gpu-batch-field', 'deviceGpuBatchControl');
  node('minerAdapterBootstrap').textContent = html.match(/<script id="minerAdapterBootstrap" type="application\/json">([\s\S]*?)<\/script>/)[1];
  const context = vm.createContext({document: {
    querySelector: selector => node(selector.slice(1)), querySelectorAll: () => [], addEventListener() {},
    createElement: () => new Element(),
  }, window: {matchMedia: () => ({matches: false}), confirm: () => true}, sessionStorage: {getItem: () => ''}, URL});
  vm.runInContext(source, context);
  context.toast = () => {}; context.refreshAll = async () => {};
  context.api = async (endpoint, options) => { calls.push({endpoint, options}); return {deferred: true}; };
  const state = vm.runInContext('state', context), gpu = {index: 0, uuid: 'GPU-synthetic-A', vendor: 'nvidia',
    name: 'Synthetic card', oc_capabilities: ['power_limit_w', 'core_clock_offset_mhz'], power_min_limit_w: 15, power_max_limit_w: 1500};
  const agent = {id: 'synthetic-A', name: 'Synthetic rig', status: 'online', agent_version: '0.8.25', metrics: {gpus: [gpu]}, inventory: {}};
  state.user = {role: 'operator'}; state.agents = [agent]; state.selectedAgent = agent;
  node('flightMinerType').value = 'srbminer';
  node('flightAlgorithm').value = 'pearlhash'; node('flightExecutionMode').value = 'native';
  node('flightSecondaryEnabled').checked = false;
  const target = {agentId: agent.id, index: gpu.index, uuid: gpu.uuid, name: gpu.name, capabilities: gpu.oc_capabilities, initialSetting: {}};
  state.deviceGpuOcTarget = target;
  let selected = context.batchDeviceGpuCandidates(agent);
  state.deviceGpuOcBatchTarget = {agentId: agent.id, gpus: selected};
  context.selectedDeviceGpuOcBatchItems = () => selected;
  const single = field => node('deviceGpuOcForm').querySelector(`[data-device-gpu-field=${field}]`);
  const batch = field => node('deviceGpuOcBatchForm').querySelector(`[data-device-gpu-batch-field=${field}]`);
  const reset = field => node('deviceGpuOcBatchForm').querySelector(`[data-device-gpu-batch-reset=${field}]`);
  function flight(values = {model_match: 'Synthetic card'}) {
    context.addFlightGpuOcRow(values);
    return node('flightGpuOcRows').rows.at(-1);
  }
  return {context, state, node, agent, gpu, target, single, batch, reset, flight, calls,
    select(gpus) { agent.metrics.gpus = gpus; selected = context.batchDeviceGpuCandidates(agent); }};
}

test('single-card controls use real power bounds and clear stale unknown upper limits', () => {
  const h = fixture(), input = h.single('power_limit_w');
  h.context.fillDeviceGpuOcControls(h.node('deviceGpuOcForm'), h.target, h.gpu, {power_limit_w: 1200});
  assert.equal(input.min, '15'); assert.equal(input.max, '1500');
  for (const value of ['15', '1200']) { input.value = value; assert.equal(input.checkValidity(), true); }
  input.value = '14'; assert.equal(input.checkValidity(), false);
  h.context.fillDeviceGpuOcControls(h.node('deviceGpuOcForm'), h.target,
    {...h.gpu, power_min_limit_w: null, power_max_limit_w: null}, null);
  assert.equal(input.min, '0'); assert.equal(input.max, '');
  assert.equal(h.single('core_clock_offset_mhz').min, '-2000');
  assert.equal(h.single('fan_percent').min, '30'); assert.equal(h.single('fan_percent').max, '100');
});

test('malformed hardware bounds stay unknown while nonempty numeric telemetry text remains usable', () => {
  const h = fixture(), input = h.single('power_limit_w');
  for (const raw of [null, undefined, '', ' ', true, false, {}, [15], 'garbage', NaN, Infinity, -1]) {
    h.context.fillDeviceGpuOcControls(h.node('deviceGpuOcForm'), h.target,
      {...h.gpu, power_min_limit_w: raw, power_max_limit_w: raw}, null);
    assert.equal(input.min, '0'); assert.equal(input.max, '');
  }
  h.context.fillDeviceGpuOcControls(h.node('deviceGpuOcForm'), h.target,
    {...h.gpu, power_min_limit_w: '15', power_max_limit_w: '1500'}, null);
  assert.equal(input.min, '15'); assert.equal(input.max, '1500');
});

test('batch power intersection retains real boundaries and no unknown upper is converted to zero or Infinity', () => {
  const h = fixture();
  h.select([h.gpu, {...h.gpu, index: 1, uuid: 'GPU-synthetic-B', power_min_limit_w: 10, power_max_limit_w: 1800}]);
  h.context.syncDeviceGpuOcBatchSelection();
  assert.equal(h.batch('power_limit_w').min, '15'); assert.equal(h.batch('power_limit_w').max, '1500');
  h.select([h.gpu, {...h.gpu, index: 1, uuid: 'GPU-synthetic-B', power_min_limit_w: null, power_max_limit_w: null}]);
  h.context.syncDeviceGpuOcBatchSelection(); assert.equal(h.batch('power_limit_w').max, '1500');
  h.select([{...h.gpu, power_min_limit_w: null, power_max_limit_w: null}]);
  h.context.syncDeviceGpuOcBatchSelection();
  assert.equal(h.batch('power_limit_w').min, '0'); assert.equal(h.batch('power_limit_w').max, '');
  h.select([{...h.gpu, power_max_limit_w: 400},
    {...h.gpu, index: 1, uuid: 'GPU-synthetic-B', power_min_limit_w: 500, power_max_limit_w: 1200}]);
  h.context.syncDeviceGpuOcBatchSelection(); h.batch('power_limit_w').value = '600';
  assert.equal(h.batch('power_limit_w').min, '500'); assert.equal(h.batch('power_limit_w').max, '400');
  assert.equal(h.node('deviceGpuOcBatchForm').reportValidity(), false);
  h.reset('power_limit_w').checked = true; h.context.syncDeviceGpuOcBatchSelection();
  assert.equal(h.node('deviceGpuOcBatchForm').reportValidity(), true);
  assert.equal(h.context.collectDeviceGpuOcBatchSetting().power_limit_w, null);
});

test('fractional reported minima do not impose a fixed power step', () => {
  const h = fixture(); h.gpu.power_min_limit_w = 37.5;
  h.context.fillDeviceGpuOcControls(h.node('deviceGpuOcForm'), h.target, h.gpu, null);
  h.select([h.gpu]); h.context.syncDeviceGpuOcBatchSelection();
  const flight = h.flight().querySelector('[data-flight-gpu-field=power_limit_w]');
  for (const input of [h.single('power_limit_w'), h.batch('power_limit_w'), flight]) {
    assert.equal(input.step, 'any');
    for (const value of ['90', '90.25']) { input.value = value; assert.equal(input.checkValidity(), true); }
  }
});

for (const mode of ['single', 'batch', 'flight']) {
  test(`${mode}: numeric form text 15/1200 is preserved as numeric power`, () => {
    const h = fixture(), row = mode === 'flight' ? h.flight() : null;
    for (const value of ['15', '1200']) {
      if (row) row.querySelector('[data-flight-gpu-field=power_limit_w]').value = value;
      else (mode === 'single' ? h.single : h.batch)('power_limit_w').value = value;
      const result = mode === 'single' ? h.context.collectDeviceGpuOcSetting() : mode === 'batch'
        ? h.context.collectDeviceGpuOcBatchSetting() : h.context.collectFlightGpuOc().settings[0];
      assert.equal(result.power_limit_w, Number(value));
    }
  });
  test(`${mode}: power rejects zero, negative, non-finite and nonnumeric form text while offset zero remains valid`, () => {
    const h = fixture(), row = mode === 'flight' ? h.flight() : null;
    const input = field => row ? row.querySelector(`[data-flight-gpu-field=${field}]`) : (mode === 'single' ? h.single : h.batch)(field);
    const collect = () => mode === 'single' ? h.context.collectDeviceGpuOcSetting() : mode === 'batch'
      ? h.context.collectDeviceGpuOcBatchSetting() : h.context.collectFlightGpuOc().settings[0];
    for (const invalid of ['0', '-1', 'NaN', 'Infinity', 'garbage']) {
      input('power_limit_w').value = invalid; assert.throws(collect);
    }
    input('power_limit_w').value = ''; input('core_clock_offset_mhz').value = '0';
    input('power_limit_w').validity.badInput = true;
    assert.throws(collect);
    input('power_limit_w').validity.badInput = false;
    assert.equal(collect().core_clock_offset_mhz, 0);
  });
}

test('single and batch explicit clears remain null while untouched blank fields stay omitted', () => {
  const h = fixture(); h.target.initialSetting = {power_limit_w: 1200};
  assert.equal(h.context.collectDeviceGpuOcSetting().power_limit_w, null);
  h.reset('power_limit_w').checked = true; h.batch('power_limit_w').value = 'garbage';
  h.batch('power_limit_w').validity.badInput = true;
  assert.equal(h.context.collectDeviceGpuOcBatchSetting().power_limit_w, null);
  h.target.initialSetting = {}; h.single('core_clock_offset_mhz').value = '0';
  assert.equal(Object.hasOwn(h.context.collectDeviceGpuOcSetting(), 'power_limit_w'), false);
});



test('SRB flight OC retains five fields and its uniform-column rule', () => {
  const h = fixture();
  const row = h.flight({model_match: '5080', power_limit_w: 250});
  assert.equal(row.querySelector('[data-flight-gpu-field=fan_percent]'), null);
  assert.equal(Object.hasOwn(h.context.collectFlightGpuOc().settings[0], 'fan_percent'), false);
  h.flight({model_match: '5070', core_clock_offset_mhz: 0});
  assert.throws(() => h.context.collectFlightGpuOc(), /相同的控制列/);
});


test('valid power does not bypass current UUID, permission or explicit target checks', async () => {
  const h = fixture(), form = h.node('deviceGpuOcForm'), error = new (class {textContent = '';})();
  assert.throws(() => h.context.collectDeviceGpuOcSetting(null));
  h.select([]); assert.throws(() => h.context.collectDeviceGpuOcBatchSetting());
  assert.equal(h.calls.length, 0); h.select([h.gpu]);
  h.single('power_limit_w').value = '1200';
  h.agent.metrics.gpus[0] = {...h.gpu, uuid: 'GPU-changed'};
  await h.context.submitDeviceGpuOc(h.target, form, error, h.node('applyDeviceGpuOcButton'));
  assert.equal(h.calls.length, 0); assert.match(error.textContent, /UUID/);
  h.agent.metrics.gpus[0] = h.gpu; h.state.user.role = 'viewer';
  await h.context.submitDeviceGpuOc(h.target, form, error, h.node('applyDeviceGpuOcButton'));
  assert.equal(h.calls.length, 0);
  h.state.user.role = 'operator';
  await h.context.submitDeviceGpuOc(h.target, form, error, h.node('applyDeviceGpuOcButton'));
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.body.gpu_uuid, h.gpu.uuid);
  assert.equal(h.calls[0].options.body.setting.power_limit_w, 1200);
});

test('an unavailable driver blocks stale-capability dialogs and fresh modal submission while older missing fields stay compatible', async () => {
  const h = fixture(), form = h.node('deviceGpuOcForm'), error = {textContent: ''}, messages = [];
  h.context.toast = text => messages.push(text);
  h.single('power_limit_w').value = '1200';
  // Keep the old target/UUID/capabilities while latest telemetry explicitly
  // says the driver is unavailable, as can happen after opening the dialog.
  h.agent.metrics.gpus[0] = {...h.gpu, driver_available: false};
  h.context.openDeviceGpuOc(h.gpu.index);
  assert.match(messages.at(-1), /显卡驱动异常/);
  assert.equal(h.node('deviceGpuOcDialog').open, undefined);
  await h.context.submitDeviceGpuOc(h.target, form, error, h.node('applyDeviceGpuOcButton'));
  assert.equal(h.calls.length, 0); assert.match(error.textContent, /显卡驱动异常/);
  h.agent.metrics.gpus[0] = {...h.gpu}; // Undefined remains the existing older-Agent compatibility path.
  await h.context.submitDeviceGpuOc(h.target, form, error, h.node('applyDeviceGpuOcButton'));
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.body.gpu_uuid, h.gpu.uuid);
  assert.equal(h.calls[0].options.body.setting.power_limit_w, 1200);
});

test('batch OC rechecks known driver availability against fresh candidates without rejecting older absent telemetry', async () => {
  const h = fixture();
  assert.equal(h.context.batchDeviceGpuCandidates(h.agent).length, 1);
  h.agent.metrics.gpus[0] = {...h.gpu, driver_available: false};
  assert.equal(h.context.batchDeviceGpuCandidates(h.agent).length, 0);
  h.batch('power_limit_w').value = '1200';
  // The dialog still holds its original valid candidate after telemetry changed.
  await h.context.applyDeviceGpuOcBatch({preventDefault() {}, currentTarget: h.node('deviceGpuOcBatchForm')});
  assert.equal(h.calls.length, 0);
  assert.match(h.node('deviceGpuOcBatchError').textContent, /驱动可用状态已变化/);
  h.agent.metrics.gpus[0] = {...h.gpu};
  assert.equal(h.context.batchDeviceGpuCandidates(h.agent).length, 1);
});

test('saved OC deletion checks fresh driver availability before queuing hardware reset, without blocking older missing fields', async () => {
  const h = fixture();
  h.target.hasSaved = true;
  h.agent.metrics.gpus[0] = {...h.gpu, driver_available: false};
  await h.context.deleteDeviceGpuOc();
  assert.equal(h.calls.length, 0);
  assert.match(h.node('deviceGpuOcError').textContent, /显卡驱动异常.*恢复默认/);
  h.agent.metrics.gpus[0] = {...h.gpu};
  await h.context.deleteDeviceGpuOc();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.method, 'DELETE');
  assert.match(h.calls[0].endpoint, /gpu_uuid=GPU-synthetic-A/);
});








test('legacy invalid delete receipts remain errors without closing the dialog', async () => {
  for (const response of [{status: 'saved', reset_command: null}, {status: 'queued', reset_command: {id: 'wrong-route'}}]) {
    const h = fixture(); h.target.hasSaved = true; h.node('deviceGpuOcDialog').open = true;
    h.context.api = async () => response;
    await h.context.deleteDeviceGpuOc();
    assert.equal(h.node('deviceGpuOcDialog').open, true); assert.equal(h.state.deviceGpuOcTarget, h.target);
    assert.match(h.node('deviceGpuOcError').textContent, /未确认 GPU 恢复默认任务/);
  }
  const valid = fixture(), messages = [];
  valid.target.hasSaved = true; valid.context.toast = text => messages.push(text);
  valid.context.api = async () => ({status: 'reset_pending', reset_command: {id: 'legacy-reset'}});
  await valid.context.deleteDeviceGpuOc();
  assert.match(messages[0], /恢复默认任务已排队，成功后才删除保存记录/);
});


test('forget_stale remains a double-confirmed controller-only deletion', async () => {
  const h = fixture(), messages = [], confirms = [];
  const uuid = 'GPU-permanently-missing';
  h.agent.desired_gpu_oc = [{gpu_uuid: uuid, name: 'Missing GPU', setting: {index: 2, power_limit_w: 250}}];
  h.context.toast = text => messages.push(text); h.context.window.confirm = text => {confirms.push(text); return true;};
  h.context.api = async (endpoint, options) => {h.calls.push({endpoint, options}); return {status: 'deleted', gpu_uuid: uuid, gpu_index: 2, desired_gpu_oc: [], reset_command: null};};
  await h.context.forgetMissingDeviceGpuOc(uuid);
  assert.equal(confirms.length, 2); assert.equal(h.calls.length, 1);
  assert.match(h.calls[0].endpoint, /forget_stale=true/);
  assert.match(messages[0], /未向设备下发硬件任务/); assert.doesNotMatch(messages[0], /重应用/);
  h.context.api = async () => ({status: 'queued', action: 'apply_flight_sheet', gpu_uuid: uuid});
  await h.context.forgetMissingDeviceGpuOc(uuid);
  assert.match(messages[1], /未确认缺席 OC 记录已在不触碰硬件/);
  assert.doesNotMatch(messages[1], /设备覆盖已清除|重应用已排队/);
});


for (const [dialogId, stateKey] of [
  ['deviceGpuOcDialog', 'deviceGpuOcTarget'],
  ['deviceGpuOcBatchDialog', 'deviceGpuOcBatchTarget'],
]) {
  test(`${dialogId}: a queued close after immediate reopening retains the new target, while a real closed dialog clears it`, () => {
    // Register the production close listener without firing DOMContentLoaded
    // or unrelated app handlers. close() queues a browser event; reopening the
    // same element can precede delivery of that older event.
    const statement = source.split('\n').find(line => line.includes(`$("#${dialogId}").addEventListener("close",`));
    assert.ok(statement, 'actual modal close listener exists');
    const oldTarget = {agentId: 'rig-old', uuid: 'GPU-old'};
    const newTarget = {agentId: 'rig-new', uuid: 'GPU-new'};
    const state = {[stateKey]: oldTarget};
    let onClose;
    const dialog = {open: false, addEventListener(name, callback) {assert.equal(name, 'close'); onClose = callback;}};
    const context = vm.createContext({state, $: selector => {assert.equal(selector, `#${dialogId}`); return dialog;}});
    vm.runInContext(statement, context);
    const queuedOldClose = () => onClose({currentTarget: dialog});
    dialog.open = true; state[stateKey] = newTarget;
    queuedOldClose();
    assert.equal(state[stateKey], newTarget, 'late close must not erase the reopened dialog target');
    dialog.open = false; onClose({currentTarget: dialog});
    assert.equal(state[stateKey], null, 'ordinary final close clears its target');
  });
}


// Reuse the device OC DOM fixture: Windows uses the same capability-driven
// form and wire values; support is reported by the Agent, never inferred here.
function windowsLockFixture(field = 'memory_clock_lock_mhz') {
  const h = fixture();
  h.agent.os_family = 'windows'; h.agent.platform = 'Windows';
  h.gpu.oc_capabilities = ['power_limit_w', field];
  h.target.capabilities = h.gpu.oc_capabilities;
  h.context.fillDeviceGpuOcControls(h.node('deviceGpuOcForm'), h.target, h.gpu, null);
  h.select([h.gpu]); h.context.syncDeviceGpuOcBatchSelection();
  return h;
}

for (const [field, mhz, labelText] of [
  ['memory_clock_lock_mhz', 7001, 'Lock Memory'],
  ['core_clock_lock_mhz', 2490, 'Lock Core'],
]) {
test(`Windows single-card ${field} is exposed only by its capability and sends unscaled MHz`, async () => {
  const h = windowsLockFixture(field), messages = [], questions = [];
  h.context.toast = text => messages.push(text);
  h.context.window.confirm = text => { questions.push(text); return true; };
  const label = h.node('deviceGpuOcForm').fields.find(item => item.name === field).label;
  assert.equal(label.classList.contains('hidden'), false);
  assert.equal(h.single(field).disabled, false);
  assert.equal(h.single(field === 'memory_clock_lock_mhz' ? 'core_clock_lock_mhz' : 'memory_clock_lock_mhz').disabled, true);
  assert.equal(h.single(field).value, '', 'support does not invent a default clock');
  h.single(field).value = String(mhz);
  await h.context.applyDeviceGpuOc();
  assert.equal(h.calls[0].endpoint, `/api/agents/${h.agent.id}/gpu-oc/apply`);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].options.body)),
    {vendor: 'nvidia', gpu_uuid: h.gpu.uuid, setting: {index: 0, [field]: mhz}});
  assert.ok(questions[0].includes(`${labelText} ${mhz} MHz`));
  assert.match(messages[0], /参数已保存.*开始或重启挖矿后自动应用/);
  assert.doesNotMatch(messages[0], /已生效|设置成功/);
});

test(`Windows missing ${field} capability hides and excludes it; an opened dialog detects capability loss`, async () => {
  const h = windowsLockFixture(field), input = h.single(field);
  h.target.capabilities = ['power_limit_w'];
  h.context.fillDeviceGpuOcControls(h.node('deviceGpuOcForm'), h.target, h.gpu, {[field]: mhz});
  assert.equal(input.disabled, true); assert.equal(input.value, '');
  const label = h.node('deviceGpuOcForm').fields.find(item => item.name === field).label;
  assert.equal(label.classList.contains('hidden'), true);
  h.single('power_limit_w').value = '250';
  assert.equal(Object.hasOwn(h.context.collectDeviceGpuOcSetting(), field), false);
  h.target.capabilities = ['power_limit_w', field]; input.value = String(mhz);
  h.gpu.oc_capabilities = ['power_limit_w'];
  await h.context.applyDeviceGpuOc();
  assert.equal(h.calls.length, 0);
  assert.match(h.node('deviceGpuOcError').textContent, /OC 能力已变化/);
});

test(`Windows batch ${field} requires the selected capability intersection and sends unchanged MHz to explicit UUIDs`, async () => {
  const h = windowsLockFixture(field);
  const second = {...h.gpu, index: 1, uuid: 'GPU-synthetic-B'};
  h.select([h.gpu, second]); h.context.syncDeviceGpuOcBatchSelection();
  assert.equal(h.batch(field).disabled, false);
  h.batch(field).value = String(mhz);
  await h.context.applyDeviceGpuOcBatch();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].options.body)),
    {vendor: 'nvidia', gpu_uuids: [h.gpu.uuid, second.uuid], setting: {[field]: mhz}});
  h.select([h.gpu, {...second, oc_capabilities: ['power_limit_w']}]);
  h.context.syncDeviceGpuOcBatchSelection();
  assert.equal(h.batch(field).disabled, true);
  assert.equal(h.batch(field).value, '');
  assert.equal(h.reset(field).disabled, true);
  h.batch('power_limit_w').value = '250';
  assert.equal(Object.hasOwn(h.context.collectDeviceGpuOcBatchSetting(), field), false);
});
}

test('Windows core-lock single and batch submit handlers retain the shared 100..5000 integer input range', () => {
  for (const mode of ['single', 'batch']) {
    const h = windowsLockFixture('core_clock_lock_mhz');
    const formId = mode === 'single' ? 'deviceGpuOcForm' : 'deviceGpuOcBatchForm';
    const submit = mode === 'single' ? 'applyDeviceGpuOc' : 'applyDeviceGpuOcBatch';
    const form = h.node(formId), input = h[mode]('core_clock_lock_mhz');
    assert.equal(input.min, '100'); assert.equal(input.max, '5000'); assert.equal(input.step, '1');
    let submits = 0;
    h.context[submit] = () => { submits++; };
    const listener = source.split('\n').find(line => line.includes(`$("#${formId}").addEventListener("submit",`));
    assert.ok(listener, 'actual form submission checks native validity');
    vm.runInContext(listener, h.context);
    for (const value of ['99', '5001', '2490.5']) {
      input.value = value;
      form.listeners.submit({preventDefault() {}, currentTarget: form});
      assert.equal(submits, 0);
    }
    for (const value of ['100', '5000']) {
      input.value = value;
      form.listeners.submit({preventDefault() {}, currentTarget: form});
    }
    assert.equal(submits, 2);
    assert.equal(h.calls.length, 0);
  }
});

test('Windows memory-lock field clears send null without erasing untouched parameters', async () => {
  const h = windowsLockFixture();
  h.target.initialSetting = {memory_clock_lock_mhz: 7001};
  await h.context.applyDeviceGpuOc();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].options.body.setting)),
    {index: 0, memory_clock_lock_mhz: null});
  h.reset('memory_clock_lock_mhz').checked = true;
  h.context.syncDeviceGpuOcBatchSelection();
  assert.equal(h.batch('memory_clock_lock_mhz').disabled, true);
  await h.context.applyDeviceGpuOcBatch();
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[1].options.body.setting)),
    {memory_clock_lock_mhz: null});
});

for (const [field, mhz] of [['memory_clock_lock_mhz', 7001], ['core_clock_lock_mhz', 2490]]) {
for (const mode of ['single', 'batch']) {
  test(`Windows ${mode} ${field} request failure retains input and displays the returned reason`, async () => {
    const h = windowsLockFixture(field), messages = [];
    h.context.toast = text => messages.push(text);
    h.context.api = async () => { throw new Error(`${field}: driver rejected locked clocks`); };
    const input = (mode === 'single' ? h.single : h.batch)(field); input.value = String(mhz);
    h.node(mode === 'single' ? 'deviceGpuOcDialog' : 'deviceGpuOcBatchDialog').open = true;
    await (mode === 'single' ? h.context.applyDeviceGpuOc() : h.context.applyDeviceGpuOcBatch());
    assert.equal(input.value, String(mhz)); assert.deepEqual(messages, []);
    assert.ok(h.node(mode === 'single' ? 'deviceGpuOcError' : 'deviceGpuOcBatchError').textContent.includes(`${field}: driver rejected`));
    assert.equal(h.node(mode === 'single' ? 'deviceGpuOcDialog' : 'deviceGpuOcBatchDialog').open, true);
  });
}
}

test('Windows saved memory-lock whole-card clears retain pending versus failed reset semantics', async () => {
  const h = windowsLockFixture(), messages = [];
  h.agent.desired_gpu_oc = [{gpu_uuid: h.gpu.uuid, setting: {index: 0, memory_clock_lock_mhz: 7001}}];
  h.target.hasSaved = true; h.context.toast = text => messages.push(text);
  h.context.api = async (endpoint, options) => {
    h.calls.push({endpoint, options}); return {status: 'reset_pending', reset_command: {id: 'reset-memory'}};
  };
  await h.context.deleteDeviceGpuOc();
  assert.equal(h.calls[0].options.method, 'DELETE');
  assert.match(h.calls[0].endpoint, /gpu-oc\/0\?gpu_uuid=GPU-synthetic-A/);
  assert.match(messages[0], /恢复默认任务已排队，成功后才删除保存记录/);
  assert.equal(h.agent.desired_gpu_oc[0].setting.memory_clock_lock_mhz, 7001);
  h.select([h.gpu]); h.context.syncDeviceGpuOcBatchSelection();
  h.context.api = async (endpoint, options) => {
    h.calls.push({endpoint, options});
    return {results: [{gpu_uuid: h.gpu.uuid, accepted: false, status: 'failed', reason: 'locked memory reset rejected'}]};
  };
  await h.context.clearDeviceGpuOcBatch();
  assert.match(h.calls[1].endpoint, /gpu-oc\/batch-clear$/);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[1].options.body)), {vendor: 'nvidia', gpu_uuids: [h.gpu.uuid]});
  assert.match(h.node('deviceGpuOcBatchResults').innerHTML, /batch-result rejected/);
  assert.match(h.node('deviceGpuOcBatchResults').innerHTML, /locked memory reset rejected/);
  assert.equal(h.agent.desired_gpu_oc[0].setting.memory_clock_lock_mhz, 7001);
});
