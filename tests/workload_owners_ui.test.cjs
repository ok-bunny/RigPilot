// Exercises actual app helpers with an in-memory API transport; no server or network is started.
'use strict';
const {constantLoader, deferred, functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();
const ownerKey = (user) => `rigpilot_workload_owners_v1:${user}`;
const preferenceKey = (user) => `rigpilot_workload_preferences_v1:${user}`;
const plain = (value) => JSON.parse(JSON.stringify(value));
const owner = (id = 'one', name = 'Alice', agents = ['a']) => ({id, name, agent_ids: agents});
const saved = (revision, groups = []) => ({initialized: true, revision, groups, updated_at: 'synthetic-time'});
const missing = () => ({initialized: false, revision: 0, groups: [], updated_at: null});
const legacy = (groups) => JSON.stringify({revision: 'old-browser-revision', groups});
function error(message, status) { return Object.assign(new Error(message), {status}); }
const appFunction = functionLoader(source);
const appConstant = constantLoader(source);
async function fixture({storage = new Map(), username = 'alice', privateStorage = false,
  server = {records: new Map(), calls: []}, handler = null, autoLoad = true} = {}) {
  const nodes = new Map(), session = new Map([['rigpilot_token', `synthetic-${username}`]]);
  const document = {activeElement: null};
  const state = {
    token: username ? `synthetic-${username}` : '', user: username ? {username} : null,
    agents: [], authEpoch: 0, refreshId: 0,
    workloadHasRequested: true, workloadLoading: false, workloadError: '',
    workload: {available_coins: ['PEARL'], fleet_totals_by_coin: {PEARL: 10e9}, series: []},
    workloadPreferences: {mode: 'multiple', coins: null, weights: {}},
    workloadPreferencesUser: '', workloadPreferencesStorageError: false, workloadRevenueAmount: '',
    workloadOwners: missing(), workloadOwnersUser: '', workloadOwnersLoaded: false,
    workloadOwnersBusy: '', workloadOwnersRequestId: 0, workloadOwnersLegacy: null,
    workloadOwnerDraft: null, workloadOwnerError: '',
  };
  function node(dataset = {}) {
    let html = '';
    return {
      value: '', disabled: false, hidden: false, textContent: '', dataset,
      checked: false, validity: {badInput: false}, children: [], listeners: new Map(),
      focus() { document.activeElement = this; },
      addEventListener(type, callback) { this.listeners.set(type, callback); },
      get innerHTML() { return html; },
      set innerHTML(value) {
        html = value;
        this.children = [...String(value).matchAll(/data-workload-owner-agent="([^"]+)"/g)]
          .map((match) => node({workloadOwnerAgent: match[1]}));
      },
    };
  }
  const $ = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, node());
    return nodes.get(selector);
  };
  const $$ = (selector, parent) => selector === '[data-workload-owner-agent]' ? (parent?.children || [])
    : selector === 'input' && parent === $('#workloadOwnerForm')
      ? [$('#workloadOwnerName'), ...$('#workloadOwnerAgents').children] : [];
  const localStorage = {
    getItem(key) { if (privateStorage) throw new Error('storage unavailable'); return storage.get(key) ?? null; },
    setItem(key, value) { if (privateStorage) throw new Error('storage unavailable'); storage.set(key, String(value)); },
  };
  let respond = handler;
  async function api(apiPath, options = {}) {
    assert.equal(apiPath, '/api/workload-owner-groups');
    const call = {path: apiPath, method: options.method || 'GET', body: options.body ? plain(options.body) : null,
      token: state.token, username: state.user?.username};
    server.calls.push(call);
    if (respond) {
      const response = await respond(call);
      if (response !== undefined) return plain(response);
    }
    const current = server.records.get(call.username) || missing();
    if (call.method === 'GET') return plain(current);
    assert.equal(call.method, 'PUT');
    if (call.body.expected_revision !== current.revision) throw error('CAS conflict', 409);
    const result = saved(current.revision + 1, call.body.groups);
    server.records.set(call.username, plain(result));
    return plain(result);
  }
  // Stubs model DOM/disclosure and unrelated logout effects, not owner guards or calculations.
  const noop = () => {};
  const context = vm.createContext({state, $, $$, localStorage, api,
    sessionStorage: {removeItem: (key) => session.delete(key)}, document, crypto: webcrypto,
    disclosureKey: (...parts) => parts.join(':'), rememberDisclosures: noop, restoreDisclosures: noop,
    stopLiveMinerLogs: noop, stopNvidiaDriverLogs: noop, setWorkloadPreset: noop,
    closeDrawer: noop, setMobileSidebarOpen: noop, renderAll: noop, loginView: noop,
  });
  const names = [
    'escapeHtml', 'logout', 'workloadCanonicalCoin', 'workloadWeightNumber',
    'loadWorkloadPreferences', 'persistWorkloadPreferences', 'supportedWorkloadCoins', 'selectedWorkloadCoins',
    'workloadValueNumber', 'formatWorkAmount', 'formatWorkloadPercentage',
    'workloadOwnerAgentId', 'workloadOwnerRecord', 'workloadOwnersKey', 'readWorkloadOwners',
    'workloadServerOwnerRecord', 'workloadOwnersContext', 'workloadOwnersContextCurrent',
    'loadWorkloadOwners', 'commitWorkloadOwners', 'importLegacyWorkloadOwners', 'workloadOwnerAgentCatalog',
    'beginWorkloadOwnerEdit', 'cancelWorkloadOwnerEdit', 'reloadSavedWorkloadOwners', 'saveWorkloadOwner', 'removeWorkloadOwner',
    'renderWorkloadOwnerControls', 'workloadIndividualDevices',
    'workloadOwnerAssignments', 'workloadCombinedModel', 'formatWorkloadWeightedAmount',
    'syncWorkloadRevenueInput', 'rememberWorkloadRevenueTotal', 'workloadRevenueTotal',
    'workloadRevenueForPercentage', 'formatWorkloadRevenue', 'renderWorkloadSummary',
  ];
  vm.runInContext([appConstant('WORKLOAD_FOLD_HASHES'), appConstant('WORKLOAD_COLORS'), ...names.map(appFunction)].join('\n'), context);
  context.loadWorkloadPreferences();
  state.workloadPreferences.weights.PEARL ??= '1';
  const result = {state, $, $$, context, storage, session, server, document,
    respond(callback) { respond = callback; }};
  if (autoLoad) assert.equal(await context.loadWorkloadOwners(), true);
  return result;
}
const device = (id, value, name = 'Same') => ({agent_id: id, agent_name: name, hostname: `host-${id}`, value});
const series = (coin, individual, total, legacyDevices = individual) => ({coin, unit: 'H', basis: 'agent_hashrate_integral',
  estimated: true, series_total: total, total_number: total, devices: legacyDevices, individual_devices: individual});
async function groups(f, records) {
  assert.equal(await f.context.commitWorkloadOwners(records, f.state.workloadOwners.revision), true);
}
function render(f, amount) {
  f.context.renderWorkloadSummary();
  if (amount !== undefined) {
    f.$('#workloadRevenueTotal').value = String(amount);
    f.context.rememberWorkloadRevenueTotal();
  }
  return f.$('#workloadSummary').innerHTML;
}
function revenues(html) {
  return [...html.matchAll(/<small>收益<\/small><strong>(.*?)<\/strong>/g)].map((match) => match[1]);
}

test('same-name Agent IDs stay independent when unassigned or assigned to different owners', async () => {
  const f = await fixture();
  f.state.workload.series = [series('PEARL', [device('a', 2e9), device('b', 5e9)], 10e9)];
  let model = f.context.workloadCombinedModel();
  assert.equal(model.rows.length, 2);
  assert.deepEqual(Array.from(model.rows, (row) => [row.id, row.name, row.value]),
    [['agent:b', 'Same', 5], ['agent:a', 'Same', 2]]);
  await groups(f, [{id: 'owner-a', name: 'Alice', agent_ids: ['a']}, {id: 'owner-b', name: 'Bob', agent_ids: ['b']}]);
  model = f.context.workloadCombinedModel();
  assert.deepEqual(Array.from(model.rows, (row) => [row.owner_id, row.value]), [['owner-b', 5], ['owner-a', 2]]);
  assert.equal(model.rows[0].members[0].id, 'b');
  assert.equal(model.rows[1].members[0].id, 'a');
  assert.deepEqual(revenues(render(f, 100)), ['50.00', '20.00']);
});

test('per-device coin work folds once before owner aggregation, preserving raw data and physical ID counts', async () => {
  const f = await fixture();
  f.state.workloadPreferences.coins = ['PEARL', 'PEARL', 'QUANTUS', 'XMR'];
  f.state.workloadPreferences.weights = {PEARL: '2', QUANTUS: '0.5', XMR: '1'};
  f.state.workload = {fleet_totals_by_coin: {PEARL: 10e9, QUANTUS: 20e9, XMR: 30e9}, series: [
    series('PEARL', [device('a', 2e9), device('b', 3e9), device('a', 999e9)], 10e9,
      [{...device('a', 5e9), agent_ids: ['a', 'b'], device_count: 2}]),
    series('QUANTUS', [device('a', 4e9)], 20e9), series('XMR', [device('b', 5e9)], 30e9),
  ]};
  await groups(f, [{id: 'family', name: 'Family', agent_ids: ['a', 'b']}]);
  const before = JSON.stringify(f.state.workload);
  const model = f.context.workloadCombinedModel(), row = model.rows[0];
  assert.equal(model.ready, true);
  assert.equal(model.total, 60);
  assert.equal(row.value, 17);
  assert.deepEqual(plain(row.raw), {PEARL: 5e9, QUANTUS: 4e9, XMR: 5e9});
  assert.deepEqual(Array.from(row.agent_ids).sort(), ['a', 'b']);
  assert.deepEqual(Array.from(row.members, (member) => [member.id, member.value]), [['a', 6], ['b', 11]]);
  const html = render(f, 120);
  assert.deepEqual(revenues(html), ['34.00']);
  assert.match(html, /归属分组 · 2 台矿工/);
  assert.match(html, /PEARL 原始 5 GH/);
  assert.equal(JSON.stringify(f.state.workload), before);
});

test('single-Agent response contributes only that Agent while revenue retains all selected fleet totals', async () => {
  const f = await fixture();
  f.state.workloadPreferences.weights = {PEARL: '2', QUANTUS: '0.5', XMR: '1'};
  f.state.workload = {fleet_totals_by_coin: {PEARL: 10e9, QUANTUS: 20e9, XMR: 30e9}, series: [
    series('PEARL', [device('a', 2e9)], 10e9), series('QUANTUS', [device('a', 4e9)], 20e9),
  ]};
  await groups(f, [{id: 'family', name: 'Family', agent_ids: ['a', 'b']}]);
  f.$('#workloadAgent').value = 'a';
  const model = f.context.workloadCombinedModel();
  assert.equal(model.total, 60);
  assert.equal(model.rows[0].value, 6);
  assert.deepEqual(Array.from(model.rows[0].agent_ids), ['a']);
  const html = render(f, 120);
  assert.deepEqual(revenues(html), ['12.00']);
  assert.match(html, /class="workload-percentage">10%<\/span>/);
});

test('empty actual detail never falls back, and a legacy merged multi-ID value is never divided', async () => {
  const f = await fixture();
  const item = series('PEARL', [], 10e9, [device('a', 10e9)]);
  f.state.workload.series = [item];
  assert.equal(f.context.workloadCombinedModel().rows.length, 0);
  assert.deepEqual(revenues(render(f)), []);
  delete item.individual_devices;
  item.devices = [{...device('a', 10e9), agent_ids: ['a', 'b'], device_count: 2}];
  let model = f.context.workloadCombinedModel();
  assert.equal(model.detailUnavailable, true);
  assert.equal(model.ready, false);
  assert.equal(model.rows.length, 0);
  const unavailableHtml = render(f);
  assert.match(unavailableHtml, /缺少逐矿工明细/);
  assert.doesNotMatch(unavailableHtml, /所选范围暂无工作量/);
  assert.equal(f.$('#workloadRevenueTotal').disabled, true);
  item.devices = [{...device('a', 2e9), agent_ids: ['a'], device_count: 1}];
  model = f.context.workloadCombinedModel();
  assert.equal(model.ready, true);
  assert.equal(model.rows[0].value, 2);
});

test('async saves preserve group identity, reassign each ID once and persist empty removal', async () => {
  const f = await fixture();
  f.state.workload.series = [series('PEARL', [device('a', 2e9), device('b', 5e9)], 10e9)];
  await groups(f, [owner('one', 'One', ['a']), owner('two', 'Two', ['b'])]);
  f.context.beginWorkloadOwnerEdit('one');
  f.state.workloadOwnerDraft.name = 'Renamed';
  f.state.workloadOwnerDraft.agent_ids = ['a', 'b', 'b'];
  await f.context.saveWorkloadOwner();
  assert.equal(f.state.workloadOwnerDraft, null);
  assert.deepEqual(plain(f.state.workloadOwners.groups), [owner('one', 'Renamed', ['a', 'b'])]);
  assert.equal(f.context.workloadCombinedModel().rows[0].value, 7);
  await f.context.removeWorkloadOwner('one');
  assert.deepEqual(plain(f.state.workloadOwners.groups), []);
  assert.equal(f.server.records.get('alice').initialized, true);
  assert.equal(f.server.records.get('alice').revision, 3);
  assert.deepEqual(Array.from(f.context.workloadCombinedModel().rows, (row) => row.id), ['agent:b', 'agent:a']);
});

test('initial load blocks editing and failure never converts legacy data into a server write', async () => {
  const pending = deferred();
  const f = await fixture({autoLoad: false, storage: new Map([[ownerKey('alice'), legacy([owner()])]]),
    handler: () => pending.promise});
  const loading = f.context.loadWorkloadOwners();
  assert.equal(f.state.workloadOwnersBusy, 'loading');
  assert.equal(f.$('#workloadOwnerAdd').disabled, true);
  f.context.beginWorkloadOwnerEdit();
  assert.equal(f.state.workloadOwnerDraft, null);
  assert.equal(await f.context.commitWorkloadOwners([owner()], 0), false);
  assert.equal(await f.context.loadWorkloadOwners(), false);
  await f.context.removeWorkloadOwner('one');
  assert.equal(f.server.calls.length, 1);
  pending.reject(error('offline', 503));
  assert.equal(await loading, false);
  assert.equal(f.state.workloadOwnersLoaded, false);
  assert.equal(f.state.workloadOwnersBusy, '');
  assert.match(f.state.workloadOwnerError, /offline/);
  assert.equal(f.server.calls.filter((call) => call.method === 'PUT').length, 0);
  f.respond(() => saved(1));
  assert.equal(await f.context.loadWorkloadOwners(), true);
  f.context.beginWorkloadOwnerEdit();
  assert.ok(f.state.workloadOwnerDraft);
});

test('server groups survive another browser and logout isolates accounts without touching local weights', async () => {
  const prefs = JSON.stringify({mode: 'multiple', coins: ['PEARL'], weights: {PEARL: '2'}});
  const storage = new Map([[preferenceKey('alice'), prefs]]);
  const alice = await fixture({storage});
  await groups(alice, [owner()]);
  assert.equal(storage.get(preferenceKey('alice')), prefs);
  assert.equal(storage.has(ownerKey('alice')), false);
  alice.state.workloadPreferences.weights.PEARL = '3';
  alice.context.persistWorkloadPreferences();
  const freshBrowser = await fixture({server: alice.server});
  assert.deepEqual(plain(freshBrowser.state.workloadOwners.groups), [owner()]);
  freshBrowser.context.logout(false);
  assert.equal(freshBrowser.state.user, null);
  assert.equal(freshBrowser.state.token, '');
  assert.equal(freshBrowser.state.workloadOwnersLoaded, false);
  assert.equal(freshBrowser.state.workloadOwnersUser, '');
  assert.equal(freshBrowser.state.workloadOwnersLegacy, null);
  assert.equal(freshBrowser.session.size, 0);
  assert.deepEqual(plain(freshBrowser.state.workloadOwners.groups), []);
  freshBrowser.state.user = {username: 'bob'};
  freshBrowser.state.token = 'synthetic-bob';
  assert.equal(await freshBrowser.context.loadWorkloadOwners(), true);
  assert.deepEqual(plain(freshBrowser.state.workloadOwners.groups), []);
  await groups(freshBrowser, [owner('two', 'Bob', ['b'])]);
  assert.deepEqual(alice.server.records.get('alice').groups, [owner()]);
  assert.deepEqual(alice.server.records.get('bob').groups, [owner('two', 'Bob', ['b'])]);
});

test('only an explicit missing server record auto-migrates valid legacy groups with CAS 0', async () => {
  for (const records of [[owner()], []]) {
    const raw = legacy(records), storage = new Map([[ownerKey('alice'), raw]]);
    const f = await fixture({storage});
    assert.deepEqual(f.server.calls.map((call) => [call.method, call.body]), [
      ['GET', null], ['PUT', {expected_revision: 0, groups: records}],
    ]);
    assert.deepEqual(plain(f.state.workloadOwners), saved(1, records));
    assert.equal(f.state.workloadOwnersLegacy, null);
    assert.equal(storage.get(ownerKey('alice')), raw);
  }
});

test('initialized empty server groups never resurrect stale local groups, including after removal', async () => {
  const raw = legacy([owner()]), storage = new Map([[ownerKey('alice'), raw]]);
  const server = {records: new Map([['alice', saved(1)]]), calls: []};
  const f = await fixture({storage, server});
  assert.deepEqual(plain(f.state.workloadOwners.groups), []);
  assert.deepEqual(server.calls.map((call) => call.method), ['GET']);
  assert.deepEqual(plain(f.state.workloadOwnersLegacy), [owner()]);
  assert.equal(f.$('#workloadOwnerImport').hidden, false);
  assert.equal(await f.context.importLegacyWorkloadOwners(), true);
  assert.deepEqual(server.calls.at(-1).body, {expected_revision: 1, groups: [owner()]});
  await f.context.removeWorkloadOwner('one');
  const anotherBrowser = await fixture({storage, server});
  assert.deepEqual(plain(anotherBrowser.state.workloadOwners.groups), []);
  assert.equal(server.calls.at(-1).method, 'GET');
  assert.equal(storage.get(ownerKey('alice')), raw);
});

test('concurrent first migration accepts the CAS winner and never retries an overwrite', async () => {
  let firstGet = true;
  const winner = saved(1, [owner('winner', 'Other browser', ['b'])]);
  const server = {records: new Map(), calls: []};
  const f = await fixture({server, storage: new Map([[ownerKey('alice'), legacy([owner()])]]),
    handler(call) {
      if (call.method === 'GET' && firstGet) { firstGet = false; return missing(); }
      if (call.method === 'PUT') { server.records.set('alice', winner); throw error('CAS conflict', 409); }
    }});
  assert.deepEqual(server.calls.map((call) => call.method), ['GET', 'PUT', 'GET']);
  assert.deepEqual(plain(f.state.workloadOwners.groups), winner.groups);
  assert.deepEqual(plain(f.state.workloadOwnersLegacy), [owner()]);
});

test('unavailable or invalid legacy storage stays read-only and does not prevent server editing', async () => {
  const invalid = ['{broken', JSON.stringify({revision: 2, groups: []}),
    JSON.stringify({revision: 'r', groups: {}}), legacy([owner('one', ' Name ', ['a'])]),
    legacy([owner('one', 'Name', ['a', 'a'])]), legacy([owner('one', 'Bad\nName', ['a'])]),
    JSON.stringify({revision: 'r', groups: [], padding: 'x'.repeat(1024 * 1024)})];
  for (const raw of invalid) {
    const storage = new Map([[ownerKey('alice'), raw]]);
    const f = await fixture({storage});
    assert.equal(f.state.workloadOwnersLegacy, null);
    assert.deepEqual(f.server.calls.map((call) => call.method), ['GET']);
    await groups(f, [owner()]);
    assert.equal(storage.get(ownerKey('alice')), raw);
  }
  const privateBrowser = await fixture({privateStorage: true});
  await groups(privateBrowser, [owner()]);
  assert.equal(privateBrowser.storage.size, 0);
  assert.equal(privateBrowser.server.records.get('alice').groups[0].name, 'Alice');
});

test('CAS conflicts preserve draft and refresh preserves base revision until successful explicit reload', async () => {
  const f = await fixture();
  await groups(f, [owner()]);
  f.context.beginWorkloadOwnerEdit('one');
  const draft = f.state.workloadOwnerDraft;
  draft.name = 'Unsaved';
  f.$('#workloadOwnerName').value = 'Unsaved';
  f.$('#workloadOwnerName').focus();
  f.server.records.set('alice', saved(2, [owner('one', 'Fresh', ['a'])]));
  await f.context.saveWorkloadOwner();
  assert.equal(f.state.workloadOwnerDraft, draft);
  assert.equal(draft.baseRevision, 1);
  assert.match(f.state.workloadOwnerError, /其他页面修改/);
  assert.equal(f.server.records.get('alice').groups[0].name, 'Fresh');
  assert.equal(await f.context.loadWorkloadOwners({migrate: false}), true);
  assert.equal(f.state.workloadOwners.groups[0].name, 'Fresh');
  assert.equal(f.state.workloadOwnerDraft, draft);
  assert.equal(draft.baseRevision, 1);
  assert.equal(f.document.activeElement, f.$('#workloadOwnerName'));
  assert.equal(f.$('#workloadOwnerName').value, 'Unsaved');
  const calls = f.server.calls.length;
  await f.context.saveWorkloadOwner();
  assert.equal(f.server.calls.length, calls);
  f.respond(() => { throw error('reload failed', 503); });
  assert.equal(await f.context.reloadSavedWorkloadOwners(), false);
  assert.equal(f.state.workloadOwnerDraft, draft);
  f.respond(null);
  assert.equal(await f.context.reloadSavedWorkloadOwners(), true);
  assert.equal(f.state.workloadOwnerDraft, null);
  assert.equal(f.state.workloadOwnerError, '');
});

test('failed saves preserve the draft and saved groups without local fallback or implicit retry', async () => {
  const f = await fixture();
  await groups(f, [owner()]);
  f.context.beginWorkloadOwnerEdit('one');
  const draft = f.state.workloadOwnerDraft;
  draft.name = 'Unsaved';
  f.respond(() => { throw error('save offline', 503); });
  await f.context.saveWorkloadOwner();
  assert.equal(f.state.workloadOwnerDraft, draft);
  assert.equal(f.state.workloadOwners.groups[0].name, 'Alice');
  assert.equal(f.server.records.get('alice').groups[0].name, 'Alice');
  assert.match(f.state.workloadOwnerError, /save offline/);
  assert.equal(f.state.workloadOwnersBusy, '');
  assert.equal(f.storage.has(ownerKey('alice')), false);
  assert.equal(f.server.calls.length, 3);
});

test('one pending save blocks additional operations and newly rebuilt checkbox inputs stay disabled', async () => {
  const f = await fixture();
  f.state.agents = [{id: 'a', name: 'Same'}];
  f.context.beginWorkloadOwnerEdit();
  const draft = f.state.workloadOwnerDraft;
  draft.name = 'Alice'; draft.agent_ids = ['a'];
  const pending = deferred();
  f.respond(() => pending.promise);
  const saving = f.context.saveWorkloadOwner();
  assert.equal(f.state.workloadOwnersBusy, 'saving');
  f.state.agents.push({id: 'b', name: 'Same'});
  f.context.renderWorkloadOwnerControls();
  assert.equal(f.$('#workloadOwnerAdd').disabled, true);
  assert.ok(f.$('#workloadOwnerAgents').children.every((input) => input.disabled));
  f.context.beginWorkloadOwnerEdit();
  f.context.cancelWorkloadOwnerEdit();
  await f.context.saveWorkloadOwner();
  await f.context.removeWorkloadOwner('one');
  assert.equal(await f.context.loadWorkloadOwners(), false);
  assert.equal(f.state.workloadOwnerDraft, draft);
  assert.equal(f.server.calls.length, 2);
  pending.resolve(saved(1, [owner()]));
  await saving;
  assert.equal(f.state.workloadOwnerDraft, null);
  assert.equal(f.state.workloadOwnersBusy, '');
});

test('logout discards a delayed load and prevents migration into the next account or clearing its busy state', async () => {
  const old = deferred(), next = deferred();
  const f = await fixture({autoLoad: false, storage: new Map([[ownerKey('alice'), legacy([owner()])]]),
    handler: () => old.promise});
  const oldLoad = f.context.loadWorkloadOwners();
  f.context.logout(false);
  f.state.user = {username: 'bob'}; f.state.token = 'synthetic-bob';
  f.respond(() => next.promise);
  const bobLoad = f.context.loadWorkloadOwners();
  old.resolve(missing());
  assert.equal(await oldLoad, false);
  assert.equal(f.state.workloadOwnersUser, 'bob');
  assert.equal(f.state.workloadOwnersBusy, 'loading');
  assert.equal(f.state.workloadOwnersLoaded, false);
  assert.equal(f.state.workloadOwnerError, '');
  assert.equal(f.server.calls.filter((call) => call.method === 'PUT').length, 0);
  next.resolve(saved(1, [owner('bob', 'Bob', ['b'])]));
  assert.equal(await bobLoad, true);
  assert.deepEqual(plain(f.state.workloadOwners.groups), [owner('bob', 'Bob', ['b'])]);
});

test('delayed save success and failure cannot mutate the next login or its draft', async () => {
  for (const failure of [false, true]) {
    const f = await fixture();
    await groups(f, [owner()]);
    f.context.beginWorkloadOwnerEdit('one');
    f.state.workloadOwnerDraft.name = 'Alice pending';
    const pending = deferred(); f.respond(() => pending.promise);
    const saving = f.context.saveWorkloadOwner();
    f.context.logout(false);
    f.state.user = {username: 'bob'}; f.state.token = 'synthetic-bob';
    f.respond(() => saved(1, [owner('bob', 'Bob', ['b'])]));
    assert.equal(await f.context.loadWorkloadOwners(), true);
    f.context.beginWorkloadOwnerEdit('bob');
    const bobDraft = f.state.workloadOwnerDraft;
    bobDraft.name = 'Bob unsaved';
    if (failure) pending.reject(error('old account failure', 409));
    else pending.resolve(saved(2, [owner('one', 'Alice pending', ['a'])]));
    await saving;
    assert.equal(f.state.workloadOwnerDraft, bobDraft);
    assert.equal(f.state.workloadOwners.groups[0].name, 'Bob');
    assert.equal(f.state.workloadOwnerError, '');
    assert.equal(f.state.workloadOwnersBusy, '');
    assert.equal(f.server.calls.at(-1).method, 'GET');
  }
});

test('each captured auth or request field rejects stale reads before any automatic migration', async () => {
  for (const mutate of [
    (state) => { state.authEpoch += 1; },
    (state) => { state.token = 'new-token'; },
    (state) => { state.user = {username: 'bob'}; },
    (state) => { state.workloadOwnersRequestId += 1; },
  ]) {
    const pending = deferred();
    const f = await fixture({autoLoad: false, storage: new Map([[ownerKey('alice'), legacy([owner()])]]),
      handler: () => pending.promise});
    const loading = f.context.loadWorkloadOwners();
    mutate(f.state); pending.resolve(missing());
    assert.equal(await loading, false);
    assert.equal(f.state.workloadOwnersLoaded, false);
    assert.equal(f.server.calls.length, 1);
    assert.deepEqual(plain(f.state.workloadOwners.groups), []);
  }
});

test('malformed server records never become a missing response or trigger a legacy import', async () => {
  const invalid = [null, {...missing(), revision: '0'}, {...missing(), revision: false},
    {...missing(), revision: -1}, {...missing(), revision: 0.5}, saved(0),
    {...missing(), groups: [owner()]}, saved(1, [owner('bad id')]),
    saved(1, [owner('one', ' Alice ')]), saved(1, [owner('one', 'Alice', ['a', 'a'])])];
  for (const response of invalid) {
    const f = await fixture({autoLoad: false, handler: () => response,
      storage: new Map([[ownerKey('alice'), legacy([owner()])]])});
    assert.equal(await f.context.loadWorkloadOwners(), false);
    assert.equal(f.state.workloadOwnersLoaded, false);
    assert.equal(f.server.calls.length, 1);
    assert.match(f.state.workloadOwnerError, /记录格式异常/);
  }
});

test('names remain escaped and invalid draft names or duplicate names never dispatch a save', async () => {
  const f = await fixture();
  await groups(f, [owner('one', 'Owner <&>')]);
  f.context.renderWorkloadOwnerControls();
  assert.match(f.$('#workloadOwnerGroups').innerHTML, /Owner &lt;&amp;&gt;/);
  assert.doesNotMatch(f.$('#workloadOwnerGroups').innerHTML, /Owner <&>/);
  for (const name of ['Bad\nName', 'A'.repeat(81), ' ', 'Owner <&>']) {
    f.context.beginWorkloadOwnerEdit();
    f.state.workloadOwnerDraft.name = name; f.state.workloadOwnerDraft.agent_ids = ['b'];
    const before = f.server.calls.length;
    await f.context.saveWorkloadOwner();
    assert.equal(f.server.calls.length, before);
    assert.ok(f.state.workloadOwnerDraft);
    assert.ok(f.state.workloadOwnerError);
    f.context.cancelWorkloadOwnerEdit();
  }
});

test('natural catalog rendering keeps draft selection and focused checkbox identity', async () => {
  const f = await fixture();
  f.state.agents = [{id: 'a', name: 'Same'}, {id: 'b', name: 'Same'}];
  f.context.beginWorkloadOwnerEdit();
  const draft = f.state.workloadOwnerDraft;
  draft.name = 'Typing'; draft.agent_ids = ['a'];
  f.context.renderWorkloadOwnerControls();
  const first = f.$('#workloadOwnerAgents').children.find((input) => input.dataset.workloadOwnerAgent === 'a');
  first.focus();
  f.state.agents.push({id: 'c', name: 'Same'});
  f.context.renderWorkloadOwnerControls();
  assert.equal(f.state.workloadOwnerDraft, draft);
  assert.deepEqual(plain(draft.agent_ids), ['a']);
  assert.equal(f.document.activeElement.dataset.workloadOwnerAgent, 'a');
  assert.equal(f.document.activeElement.checked, true);
  const b = f.$('#workloadOwnerAgents').children.find((input) => input.dataset.workloadOwnerAgent === 'b');
  b.checked = true; b.listeners.get('change')();
  assert.deepEqual(plain(draft.agent_ids), ['a', 'b']);
});
