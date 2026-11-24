'use strict';
const {deferred, functionLoader, nextTurn, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();

const functionSource = functionLoader(source);

function passwordChangeHarness(respond = async () => ({changed: true, reauthenticate: true})) {
  const nodes = new Map(), calls = [], notices = [], views = [], session = new Map([['rigpilot_token', 'synthetic-session']]);
  const node = selector => {
    if (!nodes.has(selector)) {
      const classes = new Set();
      nodes.set(selector, {value: '', textContent: '', disabled: false, open: false,
        classList: {toggle(key, enabled) { if (enabled) classes.add(key); else classes.delete(key); }, contains: key => classes.has(key)},
        setAttribute(key, value) { this[key] = value; }, focus() {}, showModal() { this.open = true; }, close() { this.open = false; }});
    }
    return nodes.get(selector);
  };
  const fields = ['#passwordChangeCurrent', '#passwordChangeNew', '#passwordChangeConfirm'];
  const controls = [...fields, '#submitPasswordChangeButton', '#passwordChangeClose'].map(node);
  const state = {user: {username: 'synthetic-admin', role: 'admin'}, token: 'synthetic-session', authEpoch: 1,
    refreshId: 0, passwordChangeRequest: null, workloadOwnersRequestId: 0, agentUpdateRequestSequence: 0,
    minerLogPollToken: 0, deviceLogRequestId: 0, deviceHashrateRequestId: 0, nvidiaDriverPollToken: 0,
    miningResidueRepairToken: 0, minerCacheCleanupToken: 0, hiveosQuickImportEpoch: 0};
  const context = vm.createContext({state, $: node,
    $$: selector => selector === 'input, button' ? controls : selector === 'dialog[open]' && node('#passwordChangeDialog').open ? [node('#passwordChangeDialog')] : [],
    sessionStorage: {removeItem: key => session.delete(key)}, stopLiveMinerLogs() {}, stopNvidiaDriverLogs() {},
    setWorkloadPreset() {}, closeDrawer() {}, setMobileSidebarOpen() {}, renderAll() {}, loginView: show => views.push(show),
    toast: text => notices.push(text),
    api: async (path, options) => { calls.push({path, options}); return respond(path, options); },
  });
  vm.runInContext(['logout', 'setPasswordChangeBusy', 'clearPasswordChangeForm', 'openPasswordChange', 'changeOwnPassword'].map(functionSource).join('\n'), context);
  const fill = (current = 'old synthetic password', next = 'new synthetic password', confirm = next) => {
    fields.forEach((selector, i) => { node(selector).value = [current, next, confirm][i]; });
  };
  return {context, state, node, fields, controls, calls, notices, views, session, fill,
    open() { context.openPasswordChange(); }, submit() { return context.changeOwnPassword({preventDefault() {}}); }};
}

test('password change entry belongs to settings and starts with native empty secret fields', () => {
  const html = readStatic('index.html');
  const settings = html.slice(html.indexOf('<section id="accessPage"'), html.indexOf('<div id="backdrop"'));
  assert.match(settings, /id="changePasswordButton"[^>]*admin-only[^>]*>修改我的密码/);
  const dialog = html.match(/<dialog id="passwordChangeDialog"[\s\S]*?<\/dialog>/)[0];
  assert.match(dialog, /aria-labelledby="passwordChangeTitle"/);
  for (const id of ['Current', 'New', 'Confirm']) {
    const field = dialog.match(new RegExp(`<input id="passwordChange${id}"[^>]*>`))[0];
    assert.match(field, /type="password"/); assert.doesNotMatch(field, /\bvalue=/);
    assert.match(field, new RegExp(`autocomplete="section-rigpilot-password-change ${id === 'Current' ? 'current' : 'new'}-password"`));
  }
});

test('password change requires the current signed-in admin and rejects invalid input before posting', async () => {
  for (const role of ['viewer', 'operator']) {
    const h = passwordChangeHarness(); h.state.user.role = role; h.open(); h.fill(); await h.submit();
    assert.equal(h.node('#passwordChangeDialog').open, false); assert.equal(h.calls.length, 0);
  }
  for (const [current, next, confirm] of [
    ['', 'new synthetic password'], ['a'.repeat(257), 'new synthetic password'],
    ['old synthetic password', 'short'], ['old synthetic password', 'a'.repeat(257)],
    ['old synthetic password', 'new synthetic password', 'different synthetic password'],
  ]) {
    const h = passwordChangeHarness(); h.open(); h.fill(current, next, confirm); await h.submit();
    assert.equal(h.calls.length, 0); assert.ok(h.node('#passwordChangeError').textContent);
    assert.equal(h.node('#passwordChangeCurrent').value, current); assert.equal(h.node('#passwordChangeNew').value, next);
  }
});

test('password submission preserves whitespace and Unicode characters, then clears the actual session for login', async () => {
  for (const next of ['  new synthetic password  ', '😀'.repeat(12), 'a'.repeat(256)]) {
    const h = passwordChangeHarness(); h.open(); const current = '  old synthetic password  '; h.fill(current, next); await h.submit();
    assert.equal(h.calls.length, 1); assert.equal(h.calls[0].path, '/api/auth/password');
    assert.equal(h.calls[0].options.method, 'POST');
    assert.deepEqual(Object.keys(h.calls[0].options.body).sort(), ['confirm_password', 'current_password', 'new_password']);
    assert.equal(h.calls[0].options.body.current_password, current);
    assert.equal(h.calls[0].options.body.new_password, next); assert.equal(h.calls[0].options.body.confirm_password, next);
    assert.equal(h.state.token, ''); assert.equal(h.state.user, null); assert.equal(h.session.size, 0);
    assert.equal(h.node('#passwordChangeDialog').open, false); assert.equal(h.node('#username').value, 'synthetic-admin');
    assert.match(h.node('#loginError').textContent, /新密码重新登录/);
    for (const field of h.fields) assert.equal(h.node(field).value, '');
    assert.deepEqual(h.notices, []); assert.deepEqual(h.views, [true]);
  }
});

test('password API failures keep fields editable and use fixed public errors without echoing response details', async () => {
  for (const status of [400, 403, 422, 429, undefined, '__proto__']) {
    const h = passwordChangeHarness(async () => { throw Object.assign(new Error('raw synthetic password private detail'), {status}); });
    h.open(); h.fill(); await h.submit();
    assert.equal(h.node('#passwordChangeDialog').open, true); assert.equal(h.state.token, 'synthetic-session');
    assert.equal(h.node('#passwordChangeCurrent').value, 'old synthetic password');
    assert.equal(h.node('#passwordChangeNew').value, 'new synthetic password');
    assert.equal(h.state.passwordChangeRequest, null); assert.ok(h.node('#passwordChangeError').textContent);
    assert.doesNotMatch(h.node('#passwordChangeError').textContent, /raw synthetic|private detail|__proto__/);
    assert.ok(h.controls.every(control => !control.disabled)); assert.deepEqual(h.notices, []);
  }
});

test('one password request locks duplicate submission and incomplete success never claims a changed password', async () => {
  const pending = deferred(), h = passwordChangeHarness(() => pending.promise); h.open(); h.fill();
  const request = h.submit(); await h.submit(); assert.equal(h.calls.length, 1);
  assert.ok(h.controls.every(control => control.disabled));
  pending.resolve({changed: true}); await request;
  assert.equal(h.state.token, 'synthetic-session'); assert.equal(h.node('#passwordChangeDialog').open, true);
  assert.match(h.node('#passwordChangeError').textContent, /无法确认修改结果/); assert.ok(h.controls.every(control => !control.disabled));
});

test('password cancel, logout and late responses cannot retain secrets or mutate a replacement account', async () => {
  for (const transition of ['close', 'logout', 'login']) for (const failed of [false, true]) {
    const pending = deferred(), h = passwordChangeHarness(() => pending.promise); h.open(); h.fill(); const request = h.submit();
    if (transition === 'close') { h.node('#passwordChangeDialog').close(); h.context.clearPasswordChangeForm(); }
    else h.context.logout(false);
    for (const field of h.fields) assert.equal(h.node(field).value, '');
    if (transition === 'login') {
      h.state.user = {username: 'replacement-admin', role: 'admin'}; h.state.token = 'replacement-token'; h.state.authEpoch++;
      h.open(); h.fill('replacement old password', 'replacement new password');
    }
    if (failed) pending.reject(Object.assign(new Error('obsolete private detail'), {status: 400}));
    else pending.resolve({changed: true, reauthenticate: true});
    await request;
    if (transition === 'login') {
      assert.equal(h.state.token, 'replacement-token'); assert.equal(h.state.user.username, 'replacement-admin');
      assert.equal(h.node('#passwordChangeCurrent').value, 'replacement old password');
    } else for (const field of h.fields) assert.equal(h.node(field).value, '');
    assert.equal(h.node('#passwordChangeError').textContent, ''); assert.deepEqual(h.notices, []);
  }
});

test('a background 401 during password commit clears the old session and gives a neutral login notice before the late 200', async () => {
  for (const newLogin of [false, true]) {
    const response = deferred(), h = passwordChangeHarness();
    Object.assign(h.context, {API_TIMEOUT_MS: 20000, AbortController, DOMException, Blob, ArrayBuffer, FormData,
      window: {setTimeout, clearTimeout},
      fetch: async path => path === '/api/auth/password' ? response.promise
        : {ok: false, status: 401, headers: {get: () => 'application/json'}, json: async () => ({detail: 'Invalid or expired session'})},
    });
    vm.runInContext(functionSource('api'), h.context); h.open(); h.fill(); const changing = h.submit();
    await assert.rejects(h.context.api('/api/summary'), error => error.status === 401);
    assert.equal(h.state.token, ''); assert.equal(h.session.size, 0);
    assert.equal(h.node('#passwordChangeDialog').open, false); for (const field of h.fields) assert.equal(h.node(field).value, '');
    assert.equal(h.node('#loginError').textContent, '登录已失效；若刚修改密码，请使用新密码登录。');
    if (newLogin) {
      h.state.authEpoch++; h.state.token = 'replacement-session'; h.state.user = {username: 'replacement-admin', role: 'admin'};
      h.node('#loginError').textContent = 'replacement login notice';
    }
    response.resolve({ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => ({changed: true, reauthenticate: true})});
    await changing;
    assert.equal(h.state.token, newLogin ? 'replacement-session' : '');
    assert.equal(h.node('#loginError').textContent, newLogin ? 'replacement login notice' : '登录已失效；若刚修改密码，请使用新密码登录。');
    if (newLogin) assert.equal(h.state.user.username, 'replacement-admin');
  }
});

const setupRecord = (status = 'pending', step = 'password', password_changed = false, revision = 1) => ({status, step, password_changed, revision, updated_at: null});
function setupHarness(respond = async () => setupRecord()) {
  const h = passwordChangeHarness(respond), pages = [];
  Object.assign(h.state, {setupState: null, setupLoading: false, setupSaving: false, setupRequestId: 0, setupError: ''});
  const panels = ['password', 'device', 'configuration'].map(step => {
    const panel = h.node('#setupPanel-' + step); panel.dataset = {setupPanel: step}; return panel;
  });
  const steps = ['password', 'device', 'configuration'].map(step => {
    const button = h.node('#setupStep-' + step); button.dataset = {setupStep: step}; return button;
  });
  const setupControls = [...steps, ...['#setupPreviousButton', '#setupNextButton', '#setupPasswordButton', '#setupEnrollmentButton'].map(h.node)];
  const previousSelect = h.context.$$;
  h.context.$$ = (selector, parent) => selector === '[data-setup-panel]' ? panels
    : selector === '[data-setup-step]' ? steps
      : selector === 'button' && parent === h.node('#setupDialog') ? setupControls
        : selector === 'dialog[open]' ? [h.node('#setupDialog'), h.node('#passwordChangeDialog')].filter(dialog => dialog.open)
          : previousSelect(selector, parent);
  Object.assign(h.context, {window: {location: {origin: 'https://controller.example.test'}}, showPage: page => pages.push(page)});
  vm.runInContext(['validSetupState', 'renderSetupWizard', 'loadSetupState', 'updateSetupState', 'openSetupWizard', 'openSetupPage'].map(functionSource).join('\n'), h.context);
  return {...h, panels, steps, setupControls, pages};
}

test('setup auto-opens only pending admin progress and resumes a changed password at device step', async () => {
  for (const record of [setupRecord(), setupRecord('pending', 'device', true, 2), setupRecord('deferred', 'device', false, 4), setupRecord('complete', 'done', false, 0)]) {
    const h = setupHarness(async () => record); assert.equal(await h.context.loadSetupState({autoOpen: true}), true);
    assert.equal(h.node('#setupDialog').open, record.status === 'pending'); assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].path, '/api/setup'); assert.equal(h.calls[0].options, undefined);
    assert.equal(h.node('#setupControllerAddress').textContent, 'https://controller.example.test');
    if (record.step === 'device') assert.equal(h.panels[1].classList.contains('hidden'), false);
    assert.equal(h.node('#setupPasswordProgress').textContent, record.password_changed ? '已修改' : '未修改');
  }
  for (const role of ['viewer', 'operator']) {
    const h = setupHarness(); h.state.user.role = role;
    assert.equal(await h.context.loadSetupState({autoOpen: true}), false); assert.equal(h.calls.length, 0);
    assert.equal(h.node('#setupDialog').open, false);
  }
});

test('setup reopen and later save server revisions while preserving a skipped password fact', async () => {
  const answers = [setupRecord('deferred', 'device', false, 4), setupRecord('pending', 'device', false, 5), setupRecord('deferred', 'device', false, 6)];
  const h = setupHarness(async () => answers.shift()); await h.context.openSetupWizard();
  assert.equal(h.node('#setupDialog').open, true); assert.equal(h.state.setupState.step, 'device');
  assert.equal(h.calls[1].options.body.action, 'reopen'); assert.equal(h.calls[1].options.body.expected_revision, 4);
  await h.context.updateSetupState('later'); assert.equal(h.node('#setupDialog').open, false);
  assert.equal(h.state.setupState.status, 'deferred'); assert.equal(h.state.setupState.password_changed, false);
  assert.equal(h.calls[2].options.body.expected_revision, 5);
  for (const call of h.calls.filter(call => call.options)) assert.deepEqual(Object.keys(call.options.body).sort(), ['action', 'expected_revision']);
  const reloaded = setupHarness(async () => h.state.setupState); await reloaded.context.loadSetupState({autoOpen: true});
  assert.equal(reloaded.node('#setupDialog').open, false); assert.equal(reloaded.state.setupState.step, 'device');
});

test('setup skip, navigation and completion never claim a password change or create mining resources', async () => {
  const answers = [setupRecord('pending', 'device', false, 2), setupRecord('pending', 'configuration', false, 3), setupRecord('complete', 'done', false, 4)];
  const h = setupHarness(async () => answers.shift()); h.state.setupState = setupRecord(); h.node('#setupDialog').showModal();
  await h.context.updateSetupState('skip'); assert.equal(h.state.setupState.password_changed, false);
  assert.equal(h.node('#setupPasswordProgress').textContent, '未修改');
  await h.context.updateSetupState('step', 'configuration');
  assert.deepEqual(Object.keys(h.calls[1].options.body).sort(), ['action', 'expected_revision', 'step']);
  for (const page of ['wallets', 'pools', 'flights']) h.context.openSetupPage(page);
  h.context.openSetupPage('agentReleases'); assert.deepEqual(h.pages, ['wallets', 'pools', 'flights']);
  assert.equal(h.calls.length, 2); await h.context.updateSetupState('complete');
  assert.equal(h.state.setupState.status, 'complete'); assert.equal(h.state.setupState.password_changed, false);
  assert.ok(h.calls.every(call => call.path === '/api/setup')); assert.equal(h.node('#setupDialog').open, false);
});

test('setup CAS conflict refreshes progress once without an implicit overwrite or exposing raw failure text', async () => {
  const h = setupHarness(async (path, options) => {
    if (options) throw Object.assign(new Error('private synthetic server detail'), {status: 409});
    return setupRecord('pending', 'configuration', false, 8);
  });
  h.state.setupState = setupRecord(); h.node('#setupDialog').showModal();
  assert.equal(await h.context.updateSetupState('later'), false); assert.equal(h.calls.length, 2);
  assert.equal(h.calls[0].options.method, 'PATCH'); assert.equal(h.calls[1].options, undefined);
  assert.equal(h.state.setupState.revision, 8); assert.equal(h.state.setupSaving, false); assert.equal(h.state.setupLoading, false);
  assert.match(h.node('#setupError').textContent, /其他会话更新/); assert.doesNotMatch(h.node('#setupError').textContent, /private synthetic/);
  assert.equal(h.node('#setupDialog').open, true);
});

test('setup in-flight saves lock reentry and password cancellation does not change persisted facts', async () => {
  const pending = deferred(), h = setupHarness(() => pending.promise); h.state.setupState = setupRecord(); h.node('#setupDialog').showModal();
  const saving = h.context.updateSetupState('skip'); await h.context.updateSetupState('later'); h.context.openSetupPage('wallets');
  assert.equal(h.calls.length, 1); assert.ok(h.setupControls.every(control => control.disabled)); assert.deepEqual(h.pages, []);
  pending.resolve(setupRecord('pending', 'device', false, 2)); await saving; assert.equal(h.state.setupSaving, false);
  const noSave = setupHarness(); noSave.state.setupState = setupRecord(); noSave.node('#setupDialog').showModal();
  noSave.open(); noSave.fill(); noSave.node('#passwordChangeDialog').close(); noSave.context.clearPasswordChangeForm();
  assert.equal(noSave.state.setupState.password_changed, false); assert.equal(noSave.state.setupState.revision, 1);
  assert.equal(noSave.node('#setupDialog').open, true); assert.equal(noSave.calls.length, 0);
});

test('late setup reads and saves cannot reopen or replace another login progress', async () => {
  for (const method of ['load', 'save']) for (const failed of [false, true]) {
    const pending = deferred(), h = setupHarness(() => pending.promise); h.state.setupState = setupRecord();
    const request = method === 'load' ? h.context.loadSetupState({autoOpen: true}) : h.context.updateSetupState('skip');
    h.context.logout(false); h.state.user = {username: 'replacement-admin', role: 'admin'};
    h.state.token = 'replacement-token'; h.state.authEpoch++; h.state.setupState = setupRecord('deferred', 'configuration', false, 9);
    if (failed) pending.reject(new Error('obsolete private detail')); else pending.resolve(setupRecord('pending', 'device', true, 2));
    await request;
    assert.equal(h.state.token, 'replacement-token'); assert.equal(h.state.setupState.revision, 9);
    assert.equal(h.state.setupState.status, 'deferred'); assert.equal(h.node('#setupDialog').open, false);
    assert.equal(h.state.setupError, '');
  }
});

function pollingHarness() {
  let nextId = 0;
  const timers = new Map();
  const context = vm.createContext({window: {
    setTimeout(callback, milliseconds) { const id = ++nextId; timers.set(id, {callback, milliseconds}); return id; },
    clearTimeout(id) { timers.delete(id); },
  }});
  vm.runInContext(functionSource('logPollingDelay'), context);
  return {context, timers, fire() {
    const [id, timer] = timers.entries().next().value;
    timers.delete(id);
    timer.callback();
  }};
}

test('log polling delay resolves at its timer and detaches its abort listener', async () => {
  const h = pollingHarness(), controller = new AbortController();
  let added = 0, removed = 0;
  const signal = {
    get aborted() { return controller.signal.aborted; },
    addEventListener(...args) { added++; controller.signal.addEventListener(...args); },
    removeEventListener(...args) { removed++; controller.signal.removeEventListener(...args); },
  };
  const pending = h.context.logPollingDelay(750, signal);
  assert.equal(h.timers.values().next().value.milliseconds, 750);
  h.fire();
  await pending;
  assert.equal(added, 1);
  assert.equal(removed, 1);
  assert.equal(h.timers.size, 0);
  controller.abort();
  assert.equal(h.timers.size, 0);
});

test('log polling delay cancels pending timers and rejects already stopped signals without scheduling', async () => {
  for (const alreadyAborted of [false, true]) {
    const h = pollingHarness(), controller = new AbortController();
    if (alreadyAborted) controller.abort();
    const pending = h.context.logPollingDelay(2000, controller.signal);
    assert.equal(h.timers.size, alreadyAborted ? 0 : 1);
    const rejection = assert.rejects(pending, error => error.name === 'AbortError');
    controller.abort();
    await rejection;
    assert.equal(h.timers.size, 0);
  }
});

test('both log loops stop during their shared delay without another request or render', async () => {
  {
    const h = pollingHarness();
    const session = {agentId: 'A', active: true, token: 1, controller: new AbortController(), commandId: null};
    const state = {selectedAgent: {id: 'A'}, user: {role: 'viewer'}, token: 'synthetic', agents: [],
      liveMinerLog: session, liveMinerLogToken: 1};
    let reads = 0, renders = 0;
    Object.assign(h.context, {state, $: () => ({open: true}),
      api: async () => { reads++; return {id: 'A'}; },
      requestLiveMinerLogSnapshot: async () => { reads++; return {logs: []}; },
      appendLiveMinerLogSnapshot: () => 0,
      renderLiveMinerLogs: () => { renders++; },
    });
    const names = ['liveMinerLogSessionActive', 'runLiveMinerLogLoop', 'stopLiveMinerLogs'];
    vm.runInContext(names.map(functionSource).join('\n'), h.context);
    const pending = h.context[names[1]](session);
    await nextTurn();
    assert.equal(h.timers.size, 1);
    assert.equal(reads, 1);
    assert.equal(renders, 1);
    h.context[names[2]]();
    await pending;
    assert.equal(h.timers.size, 0);
    assert.equal(session.active, false);
    assert.equal(reads, 1);
    assert.equal(renders, 1);
  }
});

test('mining table reuses one identity while preserving labels, version origin, zero and missing values', () => {
  let projections = 0;
  const identity = {name: 'SRBMiner', version: '3.7.1', origin: 'runtime', hiveos: true};
  const context = vm.createContext({
    deviceMiningMinerIdentity() { projections++; return identity; },
    deviceHashrateRoleLabel: () => '', deviceHashrateEfficiency: () => null,
    formatHashrateAmount: value => `${value} H/s`, hashrateToHps: value => typeof value === 'number' ? value : null,
  });
  vm.runInContext(['hasHashrateAmount', 'deviceMiningTableEntry'].map(functionSource).join('\n'), context);
  const row = context.deviceMiningTableEntry({primary: {}}, 'primary', 0, 'H/s', {coin: 'pearl', algorithm: 'pearlhash'});
  assert.equal(projections, 1);
  assert.equal(row.minerIdentity, identity);
  assert.equal(row.miner, 'SRBMiner 3.7.1');
  assert.equal(row.coin, 'PEARL');
  assert.equal(row.amount, '0 H/s');
  identity.name = 'SRBMiner 3.7.1';
  const missing = context.deviceMiningTableEntry({}, 'primary', null, 'H/s');
  assert.equal(missing.miner, identity.name);
  assert.equal(missing.amount, '--');
  assert.equal(missing.minerIdentity.origin, 'runtime');
});

test('device activity keeps queued cancellation and offline admin abandonment, with updates excluded', () => {
  const state = {user: {role: 'admin'}, deviceLog: {agentId: 'A', commands: [
    {id: 'queued', action: 'reboot', status: 'queued'},
    {id: 'running', action: 'reboot', status: 'running'},
    {id: 'update', action: 'agent_update.v1', status: 'queued'},
    {id: 'oc-running', action: 'apply_gpu_profile', status: 'running'},
  ], audit: [{action: 'command_queued', actor: '<actor>'}]}};
  const context = vm.createContext({state, actionNames: {reboot: '重启'}, auditNames: {command_queued: '任务入队'},
    RUNTIME_CANCELLABLE_ACTIONS: new Set(['apply_gpu_profile']),
    formatTime: () => 'time', commandStatus: value => value, commandErrorMessage: () => ''});
  vm.runInContext(['escapeHtml', 'deviceActivityMarkup'].map(functionSource).join('\n'), context);
  const markup = context.deviceActivityMarkup({id: 'A', status: 'offline'});
  assert.match(markup, /data-device-command-operation="cancel" data-command-id="queued"/);
  assert.match(markup, /data-device-command-operation="abandon" data-command-id="running"/);
  assert.doesNotMatch(markup, /data-command-id="update"/);
  assert.match(markup, /任务入队/);
  assert.match(markup, /&lt;actor&gt;/);
  // Runtime cancellable actions offer cancel even for an offline device and
  // never degrade into the offline abandonment path.
  assert.match(markup, /data-device-command-operation="cancel" data-command-id="oc-running" data-command-name="[^"]*" data-command-state="running"/);
  state.user.role = 'operator';
  assert.doesNotMatch(context.deviceActivityMarkup({id: 'A', status: 'offline'}), /operation="abandon"/);
  state.user.role = 'viewer';
  assert.doesNotMatch(context.deviceActivityMarkup({id: 'A', status: 'offline'}), /data-device-command-operation/);
});

function pendingPollHarness(name) {
  const {promise: response, resolve} = deferred();
  const state = {authEpoch: 1, selectedAgent: {id: 'A'},
    miningResidueRepairToken: 1, miningResidueRepair: {agentId: 'A'}};
  let applied = 0;
  const context = vm.createContext({state, api: () => response,
    commandErrorMessage: command => command.error,
  });
  vm.runInContext(functionSource(name), context);
  const run = () => context[name]('command', 1, 1000);
  return {state, resolve, run, applied: () => applied,
    answer: {status: 'succeeded', result: {scan_fingerprint: 'synthetic'}}};
}

for (const name of ['waitForMiningResidueCommand']) {
  for (const change of ['close', 'agent', 'login']) {
    test(`${name} rejects a response after ${change} before applying or returning it`, async () => {
      const h = pendingPollHarness(name), pending = h.run();
      const rejection = assert.rejects(pending, /取消/);
      if (change === 'login') h.state.authEpoch++;
      else if (change === 'agent') {
        h.state.miningResidueRepair = {agentId: 'B'};
      }
      else { h.state.miningResidueRepairToken++; }
      h.resolve(h.answer);
      await rejection;
      assert.equal(h.applied(), 0);
    });
  }
  test(`${name} accepts the current terminal response`, async () => {
    const h = pendingPollHarness(name), pending = h.run();
    h.resolve(h.answer);
    assert.equal(await pending, h.answer);
    assert.equal(h.applied(), 0);
  });
}

test('logout advances polling tokens so a later login cannot reuse an old request identity', () => {
  const state = {authEpoch: 1, refreshId: 0,
    miningResidueRepairToken: 7, miningResidueRepair: {agentId: 'A'},
    deviceLogRequestId: 0, deviceHashrateRequestId: 0, minerCacheCleanupToken: 0,
    agentUpdateRequestSequence: 0};
  const node = {value: '', textContent: ''};
  const context = vm.createContext({state, $: () => node, $$: () => [],
    sessionStorage: {removeItem() {}}, stopLiveMinerLogs() {}, stopNvidiaDriverLogs() {},
    setWorkloadPreset() {}, closeDrawer() {}, setMobileSidebarOpen() {}, renderAll() {}, loginView() {},
  });
  vm.runInContext(functionSource('logout'), context);
  context.logout(false);
  assert.ok(state.miningResidueRepairToken > 7);
  assert.equal(state.miningResidueRepair, null);
  assert.notEqual(++state.miningResidueRepairToken, 7);
});

test('live miner log dedups per mining role so dual legs never collide', () => {
  const session = {previousBySource: new Map(), lines: [], dropped: 0, lastResult: null};
  const context = vm.createContext({session});
  vm.runInContext(
    functionSource('liveMinerLogOverlap') + '\n' + functionSource('appendLiveMinerLogSnapshot'),
    context,
  );
  context.appendLiveMinerLogSnapshot(session, {captured_at: '2026-11-16T00:00:00Z', logs: [
    {role: 'primary', source: 'miner', lines: ['a1', 'a2']},
    {role: 'secondary', source: 'miner', lines: ['b1']},
  ]});
  // Same source name on both legs: the old single-source key would drop or
  // duplicate lines here. The (role, source) key must keep them separate.
  context.appendLiveMinerLogSnapshot(session, {captured_at: '2026-11-16T00:00:01Z', logs: [
    {role: 'primary', source: 'miner', lines: ['a2', 'a3']},
    {role: 'secondary', source: 'miner', lines: ['b1', 'b2']},
  ]});
  assert.deepEqual(
    session.lines.map(entry => `${entry.role}:${entry.line}`),
    ['primary:a1', 'primary:a2', 'secondary:b1', 'primary:a3', 'secondary:b2'],
  );
  assert.equal(session.previousBySource.size, 2);
  assert.deepEqual(session.previousBySource.get('primary:miner'), ['a2', 'a3']);
  assert.deepEqual(session.previousBySource.get('secondary:miner'), ['b1', 'b2']);
});

test('live miner log line markup keeps escaping, role badges and numeric highlights', () => {
  const context = vm.createContext({
    escapeHtml: undefined,
    minerLogLineKind: () => ({name: 'hashrate', label: 'RATE'}),
  });
  vm.runInContext(
    'function escapeHtml(value) { return String(value ?? "")\n'
      + '  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")\n'
      + '  .replaceAll(\'"\', "&quot;").replaceAll("\'", "&#039;"); }\n'
      + functionSource('liveMinerLogRoleLabel') + '\n'
      + functionSource('liveMinerLogHighlight') + '\n'
      + functionSource('liveMinerLogLineMarkup'),
    context,
  );
  const entry = {role: 'primary', source: 'miner', line: "Accepted 123.4 <kh/s> it's 90%", capturedAt: '2026-11-16T00:00:00Z'};
  const markup = context.liveMinerLogLineMarkup(entry, {showRole: true});
  assert.ok(markup.includes('miner-live-role is-primary'));
  assert.ok(markup.includes('&lt;kh/s&gt;'));
  assert.ok(!markup.includes('<kh/s>'));
  // Apostrophe entity digits must not be rewrapped inside the highlight span.
  assert.ok(markup.includes('&#039;'));
  assert.ok(!markup.includes('&#<span class="miner-live-num">039</span>;'));
  assert.ok(markup.includes('<span class="miner-live-num">123.4</span>'));
  assert.ok(markup.includes('90<span class="miner-live-num">%</span>') || markup.includes('<span class="miner-live-num">90%</span>'));
  const plain = context.liveMinerLogLineMarkup(entry, {});
  assert.ok(!plain.includes('miner-live-role'));
});

function settlementHarness() {
  const {promise: response, resolve, reject} = deferred();
  const input = {value: 'A draft'};
  const state = {authEpoch: 1, token: 'A-token', user: {username: 'A', role: 'admin'},
    settlementNote: {revision: 1}, settlementNoteLoaded: true, settlementNoteDirty: true, settlementNoteSaving: false};
  const calls = [], toasts = [];
  let getResponse = Promise.resolve({settlement_at: 'latest', revision: 3});
  const context = vm.createContext({state, $: () => input,
    WORKLOAD_SETTLEMENT_NOTE_PATH: '/api/workload-settlement-note',
    settlementIsoFromLocal: value => value, settlementLocalValue: value => value,
    renderWorkloadSettlementNote() {}, toast: text => toasts.push(text),
    api(path, options) { calls.push({path, options}); return options?.method === 'PUT' ? response : getResponse; },
  });
  vm.runInContext(functionSource('saveWorkloadSettlementNote'), context);
  return {context, state, input, calls, toasts, resolve, reject,
    getResponse(value) { getResponse = value; },
    switchLogin() { state.authEpoch++; state.token = 'B-token'; state.user = {username: 'B', role: 'operator'};
      state.settlementNote = {revision: 7}; state.settlementNoteDirty = true; state.settlementNoteSaving = true;
      state.settlementNoteError = 'B error'; state.settlementNoteNotice = 'B notice'; input.value = 'B draft'; },
  };
}

test('late settlement PUT success, failure or conflict cannot mutate another login or start a stale GET', async () => {
  for (const outcome of ['success', 'failure', 'conflict']) {
    const h = settlementHarness();
    const pending = h.context.saveWorkloadSettlementNote();
    h.switchLogin();
    if (outcome === 'success') h.resolve({settlement_at: 'A saved', revision: 2});
    else h.reject(Object.assign(new Error(outcome), {status: outcome === 'conflict' ? 409 : 503}));
    assert.equal(await pending, false);
    assert.equal(h.state.settlementNote.revision, 7);
    assert.equal(h.input.value, 'B draft');
    assert.equal(h.state.settlementNoteSaving, true);
    assert.equal(h.state.settlementNoteError, 'B error');
    assert.equal(h.state.settlementNoteNotice, 'B notice');
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.toasts, []);
  }
});

test('late settlement conflict reload cannot replace a new login draft', async () => {
  const h = settlementHarness();
  const {promise, resolve: release} = deferred();
  h.getResponse(promise);
  const pending = h.context.saveWorkloadSettlementNote();
  h.reject(Object.assign(new Error('conflict'), {status: 409}));
  await nextTurn();
  assert.equal(h.calls.length, 2);
  h.switchLogin();
  release({settlement_at: 'A latest', revision: 3});
  assert.equal(await pending, false);
  assert.equal(h.state.settlementNote.revision, 7);
  assert.equal(h.input.value, 'B draft');
  assert.equal(h.state.settlementNoteSaving, true);
  assert.equal(h.state.settlementNoteNotice, 'B notice');
});

test('actual Agent upload uses the bounded transfer timeout without deploying a device', async () => {
  const nodes = new Map(); const node = key => { if (!nodes.has(key)) nodes.set(key, {value: '', files: [], textContent: '', disabled: false, close() {}}); return nodes.get(key); };
  node('#agentReleaseVersion').value = '0.8.28'; node('#agentReleasePlatform').value = 'windows';
  node('#agentReleaseFile').files = [{name: 'synthetic.zip', size: 1}]; const calls = [];
  const context = vm.createContext({$: node, state: {user: {role: 'admin'}}, URLSearchParams, LARGE_UPLOAD_TIMEOUT_MS: 1234,
    api: async (path, options) => calls.push({path, options}), toast() {}, refreshAll: async () => {},
  });
  vm.runInContext(functionSource('uploadAgentRelease'), context); await context.uploadAgentRelease();
  assert.equal(calls.length, 1); assert.match(calls[0].path, /^\/api\/agent-releases\/upload\?/);
  assert.equal(calls[0].options.timeoutMs, 1234); assert.equal(calls[0].options.method, 'POST');
  assert.doesNotMatch(calls[0].path, /deploy/); assert.equal(node('#uploadAgentReleaseButton').disabled, false);
});



test('late enrollment secrets cannot repopulate a closed dialog or another login', async () => {
  for (const transition of ['close', 'login', 'current']) for (const failed of [false, true]) {
    const pending = deferred(), nodes = new Map(), errors = [];
    const node = id => { if (!nodes.has(id)) nodes.set(id, {value: '', textContent: '', disabled: false, classList: {add() {}, remove() {}}}); return nodes.get(id); };
    const state = {authEpoch: 1, enrollmentRequestId: 0};
    const context = vm.createContext({state, $: node, api: () => pending.promise, formatTime: String, toast: text => errors.push(text)});
    vm.runInContext(['generateEnrollmentToken', 'clearEnrollmentSecrets'].map(functionSource).join('\n'), context);
    const request = context.generateEnrollmentToken();
    if (transition === 'close') context.clearEnrollmentSecrets();
    else if (transition === 'login') state.authEpoch++;
    if (failed) pending.reject(new Error('obsolete')); else pending.resolve({token: 'secret-token', expires_at: 'time'});
    await request;
    assert.equal(node('#enrollmentToken').textContent, transition === 'current' && !failed ? 'secret-token' : '');
    assert.equal(errors.length, transition === 'current' && failed ? 1 : 0);
  }
});

test('automatic refresh pauses in hidden tabs and resumes when the logged-in view becomes visible', async () => {
  const listeners = new Map(), refreshes = [];
  const document = {hidden: true, addEventListener(name, callback) { listeners.set(name, callback); }};
  let interval, appHidden = false;
  const context = vm.createContext({document, state: {token: 'synthetic'},
    $: () => ({classList: {contains: () => appHidden}}), $$: () => [], bindEvents() {},
    setInterval(callback, milliseconds) { assert.equal(milliseconds, 10000); interval = callback; },
    api: async () => ({role: 'admin'}), enterApp: async () => {}, logout() {},
    refreshAll: silent => refreshes.push(silent),
  });
  vm.runInContext(functionSource('boot'), context);
  await context.boot();
  interval(); listeners.get('visibilitychange')();
  assert.deepEqual(refreshes, []);
  document.hidden = false;
  listeners.get('visibilitychange')();
  assert.deepEqual(refreshes, [true]);
  interval();
  assert.deepEqual(refreshes, [true, true]);
  appHidden = true;
  interval(); listeners.get('visibilitychange')();
  assert.deepEqual(refreshes, [true, true]);
});

test('workload device options reuse unchanged lists and keep or clear the selected device when the list changes', () => {
  let writes = 0, html = '';
  const select = {value: 'B', dataset: {},
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; writes++; this.value = ''; },
  };
  const state = {agents: [{id: 'B', name: 'Beta'}, {id: 'A', name: 'Alpha'}]};
  const context = vm.createContext({state, $: () => select, renderWorkloadCoinControls() {}, renderWorkloadOwnerControls() {}});
  vm.runInContext(['escapeHtml', 'renderWorkloadFilterOptions'].map(functionSource).join('\n'), context);
  context.renderWorkloadFilterOptions();
  assert.equal(writes, 1); assert.equal(select.value, 'B');
  assert.ok(html.indexOf('value="A"') < html.indexOf('value="B"'));
  state.agents.reverse();
  context.renderWorkloadFilterOptions();
  assert.equal(writes, 1); assert.equal(select.value, 'B');
  state.agents.find(agent => agent.id === 'B').name = '<Aardvark>';
  context.renderWorkloadFilterOptions();
  assert.equal(writes, 2); assert.equal(select.value, 'B');
  assert.match(html, /&lt;Aardvark&gt;/);
  assert.ok(html.indexOf('value="B"') < html.indexOf('value="A"'));
  state.agents.push({id: 'C', hostname: 'Charlie'});
  context.renderWorkloadFilterOptions();
  assert.equal(writes, 3); assert.equal(select.value, 'B'); assert.match(html, /Charlie/);
  state.agents = state.agents.filter(agent => agent.id !== 'B');
  context.renderWorkloadFilterOptions();
  assert.equal(writes, 4); assert.equal(select.value, '');
  context.renderWorkloadFilterOptions();
  assert.equal(writes, 4);
  state.agents = [];
  context.renderWorkloadFilterOptions();
  assert.equal(writes, 5); assert.equal(select.value, '');
  assert.equal(html, '<option value="">全部设备</option>');
});
