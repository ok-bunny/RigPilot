'use strict';
const {constantLoader, functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();

const appFunction = functionLoader(source);
const appConstant = constantLoader(source);
function fixture({storage = new Map(), username = 'alice', privateStorage = false} = {}) {
  const nodes = new Map();
  const state = {
    user: username ? {username} : null, agents: [],
    workloadHasRequested: true, workloadLoading: false, workloadError: '',
    workload: {supported_coins: ['PEARL', 'QUANTUS', 'XMR'], series: []},
    workloadPreferences: {mode: 'multiple', coins: null, weights: {}},
    workloadPreferencesUser: '', workloadPreferencesStorageError: false,
    workloadRevenueAmount: '',
  };
  const $ = (selector) => {
    if (!nodes.has(selector)) nodes.set(selector, {
      value: '', disabled: false, innerHTML: '', textContent: '', dataset: {}, validity: {badInput: false},
    });
    return nodes.get(selector);
  };
  const localStorage = {
    getItem(key) {
      if (privateStorage) throw new Error('storage unavailable');
      return storage.get(key) ?? null;
    },
    setItem(key, value) {
      if (privateStorage) throw new Error('storage unavailable');
      storage.set(key, String(value));
    },
  };
  const context = vm.createContext({state, $, $$: () => [], localStorage, document: {activeElement: null}, rememberDisclosures() {}, restoreDisclosures() {}, disclosureKey: (...parts) => JSON.stringify(parts)});
  const helpers = [
    'escapeHtml', 'workloadCanonicalCoin', 'workloadWeightNumber', 'loadWorkloadPreferences',
    'persistWorkloadPreferences', 'supportedWorkloadCoins', 'selectedWorkloadCoins',
    'chooseWorkloadCoin', 'setWorkloadCoinMode', 'renderWorkloadCoinControls',
    'workloadValueNumber', 'formatWorkAmount',
    'formatWorkloadPercentage',
    'workloadOwnerAgentId', 'workloadIndividualDevices', 'workloadOwnerAssignments', 'workloadCombinedModel', 'formatWorkloadWeightedAmount', 'syncWorkloadRevenueInput',
    'rememberWorkloadRevenueTotal', 'workloadRevenueTotal', 'workloadRevenueForPercentage',
    'formatWorkloadRevenue', 'renderWorkloadSummary',
  ];
  vm.runInContext([appConstant('WORKLOAD_FOLD_HASHES'), appConstant('WORKLOAD_COLORS'),
    ...helpers.map(appFunction)].join('\n'), context);
  context.loadWorkloadPreferences();
  return {state, $, context, storage};
}
function series(coin, devices, total) {
  return {coin, unit: 'H', basis: 'agent_hashrate_integral', estimated: true,
    total, total_number: total, series_total: total, devices};
}
function filteredFixture() {
  const f = fixture();
  f.state.workloadPreferences.coins = ['PEARL', 'XMR'];
  f.state.workloadPreferences.weights = {PEARL: '3', XMR: '2'};
  f.state.workload = {available_coins: ['PEARL', 'XMR'], fleet_totals_by_coin: {PEARL: 10e9, XMR: 20e9},
    series: [series('PEARL', [{agent_id: 'a', agent_name: 'A', value: 2e9}], 10e9)]};
  f.$('#workloadAgent').value = 'a';
  return f;
}
function render(f) {
  f.context.renderWorkloadSummary();
  return f.$('#workloadSummary').innerHTML;
}
function setRevenue(f, value, badInput = false) {
  const input = f.$('#workloadRevenueTotal');
  input.value = value;
  input.validity.badInput = badInput;
  f.context.rememberWorkloadRevenueTotal();
  return f.$('#workloadSummary').innerHTML;
}
function revenues(html) {
  return [...html.matchAll(/<small>收益<\/small><strong>(.*?)<\/strong>/g)].map((match) => match[1]);
}

test('raw coin buttons support single/multiple selection and explicit XMR exclusion', () => {
  const f = fixture();
  f.state.workload.supported_coins.push('RVN', '+ASSET', 'PRL', 'bad coin', '<coin>', 'A'.repeat(25));
  assert.deepEqual(Array.from(f.context.supportedWorkloadCoins()), ['+ASSET', 'PEARL', 'QUANTUS', 'RVN', 'XMR']);
  for (const coin of ['.COIN', '_COIN', '-COIN', '+COIN']) {
    assert.equal(f.context.workloadCanonicalCoin(coin.toLowerCase()), coin);
  }
  assert.equal(f.context.workloadCanonicalCoin(' prl '), 'PEARL');
  assert.equal(f.context.workloadCanonicalCoin('qtc'), 'QTC');
  f.context.renderWorkloadCoinControls();
  const html = f.$('#workloadCoinOptions').innerHTML;
  assert.match(html, /data-workload-coin="XMR"/);
  assert.match(html, /data-workload-coin="RVN"/);
  assert.match(html, /data-workload-coin="\+ASSET"/);
  assert.doesNotMatch(html, /data-workload-coin="PRL"/);
  assert.doesNotMatch(html, /bad coin|&lt;coin&gt;/);
  f.context.chooseWorkloadCoin('XMR');
  assert.deepEqual(Array.from(f.context.selectedWorkloadCoins()), ['+ASSET', 'PEARL', 'QUANTUS', 'RVN']);
  f.context.setWorkloadCoinMode('single');
  f.context.chooseWorkloadCoin('XMR');
  assert.deepEqual(Array.from(f.context.selectedWorkloadCoins()), ['XMR']);
  f.context.setWorkloadCoinMode('multiple');
  f.context.chooseWorkloadCoin('PEARL');
  assert.deepEqual(Array.from(f.context.selectedWorkloadCoins()), ['XMR', 'PEARL']);
  f.context.chooseWorkloadCoin('<coin>');
  assert.deepEqual(Array.from(f.context.selectedWorkloadCoins()), ['XMR', 'PEARL']);
});

test('weights require explicit finite decimal values and zero never becomes a default', () => {
  const f = fixture();
  for (const value of [null, undefined, true, {}, '', ' ', '-1', 'NaN', 'Infinity', '0x10', '1e309']) {
    assert.equal(f.context.workloadWeightNumber(value), null, `weight ${String(value)}`);
  }
  for (const [value, expected] of [['.25', 0.25], ['1.5e2', 150], ['0', 0], [2, 2]]) {
    assert.equal(f.context.workloadWeightNumber(value), expected);
  }
  f.state.workloadPreferences.coins = ['PEARL'];
  f.state.workload.series = [series('PEARL', [{agent_id: 'a', value: 1e9}], 1e9)];
  let model = f.context.workloadCombinedModel();
  assert.equal(model.ready, false);
  assert.equal(model.total, null);
  assert.deepEqual(Array.from(model.missing), ['PEARL']);
  assert.match(render(f), /待折算/);
  assert.equal(f.$('#workloadRevenueTotal').disabled, true);
  f.state.workloadPreferences.weights.PEARL = '0';
  model = f.context.workloadCombinedModel();
  assert.equal(model.ready, true);
  assert.equal(model.total, 0);
  assert.equal(model.rows[0].value, 0);
  assert.deepEqual(revenues(render(f)), ['—']);
  assert.equal(f.$('#workloadRevenueTotal').disabled, true);
  f.state.workloadPreferences.weights.PEARL = '0.25';
  assert.equal(f.context.workloadCombinedModel().total, 0.25);
});

test('Agent filtering preserves every selected coin fleet denominator and duplicate inputs do not add twice', () => {
  const f = filteredFixture();
  f.state.workloadPreferences.coins = ['PEARL', 'PEARL', 'XMR', 'XMR'];
  f.state.workload.series.push(series('PEARL', [{agent_id: 'a', agent_name: 'A', value: 100e9}], 100e9));
  const rawBefore = JSON.stringify(f.state.workload);
  const model = f.context.workloadCombinedModel();
  assert.deepEqual(Array.from(model.coins), ['PEARL', 'XMR']);
  assert.equal(model.ready, true);
  assert.equal(model.total, 70);
  assert.equal(model.rows.length, 1);
  assert.equal(model.rows[0].value, 6);
  assert.equal(model.rows[0].raw.PEARL, 2e9);
  render(f);
  const html = setRevenue(f, '70');
  assert.deepEqual(revenues(html), ['6.00']);
  assert.match(html, /class="workload-percentage">8\.57%<\/span>/);
  assert.match(html, /PEARL 原始 2 GH · 权重 3 WU\/GH/);
  assert.equal(JSON.stringify(f.state.workload), rawBefore, 'projection does not alter raw counters or history');
  f.context.chooseWorkloadCoin('XMR');
  assert.equal(f.context.workloadCombinedModel().total, 30);
  assert.deepEqual(revenues(f.$('#workloadSummary').innerHTML), ['14.00']);
});

test('one graph groups explicitly assigned IDs across coins and sorts weighted amounts', () => {
  const f = fixture();
  f.state.workloadOwners = {revision: 'test', groups: [{id: 'owner-same', name: 'Same', agent_ids: ['a', 'b']}]};
  f.state.workloadPreferences.weights = {PEARL: '2', QUANTUS: '0.5', XMR: '1'};
  f.state.workload.fleet_totals_by_coin = {PEARL: 10e9, QUANTUS: 20e9, XMR: 30e9};
  f.state.workload.series = [
    series('PEARL', [{agent_id: 'a', agent_name: 'Same', value: 1e9},
      {agent_id: 'b', agent_name: 'Same', value: 2e9}, {agent_id: 'c', agent_name: 'High', value: 3e9}], 10e9),
    series('QUANTUS', [{agent_id: 'a', agent_name: 'Same', value: 4e9, accepted_shares: 999},
      {agent_id: 'c', agent_name: 'High', value: 1e9}], 20e9),
    series('XMR', [{agent_id: 'd', agent_name: 'Other', value: 5e9}], 30e9),
  ];
  const model = f.context.workloadCombinedModel();
  assert.equal(model.total, 60);
  assert.deepEqual(Array.from(model.rows, (row) => [row.name, row.value]), [['Same', 8], ['High', 6.5], ['Other', 5]]);
  assert.deepEqual(Array.from(model.rows[0].agent_ids).sort(), ['a', 'b']);
  assert.equal(model.rows[0].raw.PEARL, 3e9);
  assert.equal(model.rows[0].raw.QUANTUS, 4e9);
  render(f);
  const html = setRevenue(f, '120');
  assert.equal((html.match(/<section /g) || []).length, 1);
  assert.deepEqual(revenues(html), ['16.00', '13.00', '10.00']);
  assert.match(html, /归属分组 · 2 台矿工/);
  assert.match(html, /QUANTUS 原始 4 GH · 权重 0\.5 WU\/GH/);
  assert.match(html, /用户权重折算 · 原始 H 为算力积分/);
  assert.doesNotMatch(html, /999|is-confirmed/);
});

test('one revenue amount follows the unified share, with blank/invalid unknown and explicit zero valid', () => {
  const f = filteredFixture();
  render(f);
  for (const value of ['', ' ', '-1', 'invalid', 'NaN', 'Infinity']) {
    assert.deepEqual(revenues(setRevenue(f, value)), ['—'], `amount ${JSON.stringify(value)}`);
  }
  assert.deepEqual(revenues(setRevenue(f, '12', true)), ['—']);
  assert.deepEqual(revenues(setRevenue(f, '0')), ['0.00']);
  assert.equal(f.context.workloadRevenueTotal(), 0);
  assert.deepEqual(revenues(setRevenue(f, '70')), ['6.00']);
  assert.equal(f.context.workloadRevenueForPercentage(null, 25), null);
  assert.equal(f.context.workloadRevenueForPercentage(undefined, 25), null);
});

test('selection and weights survive reload, persist empty selection and stay isolated by user', () => {
  const storage = new Map();
  const alice = fixture({storage});
  alice.state.workloadPreferences.coins = ['XMR'];
  alice.state.workloadPreferences.weights = {XMR: '2.5'};
  alice.context.persistWorkloadPreferences();
  const reloaded = fixture({storage});
  assert.deepEqual(Array.from(reloaded.context.selectedWorkloadCoins()), ['XMR']);
  assert.equal(reloaded.state.workloadPreferences.weights.XMR, '2.5');
  reloaded.context.chooseWorkloadCoin('XMR');
  assert.deepEqual(Array.from(fixture({storage}).context.selectedWorkloadCoins()), []);
  reloaded.state.user = {username: 'bob'};
  reloaded.context.loadWorkloadPreferences();
  assert.deepEqual(Object.keys(reloaded.state.workloadPreferences.weights), []);
  reloaded.state.workloadPreferences.coins = ['PEARL'];
  reloaded.state.workloadPreferences.weights = {PEARL: '9'};
  reloaded.context.persistWorkloadPreferences();
  reloaded.state.user = {username: 'alice'};
  reloaded.context.loadWorkloadPreferences();
  assert.deepEqual(Array.from(reloaded.context.selectedWorkloadCoins()), []);
  assert.deepEqual(Object.entries(reloaded.state.workloadPreferences.weights), [['XMR', '2.5']]);
  assert.equal(fixture({storage, username: 'bob'}).state.workloadPreferences.weights.PEARL, '9');
  const anonymous = fixture({username: null});
  anonymous.context.persistWorkloadPreferences();
  assert.equal(anonymous.storage.size, 0);
});

test('damaged/private storage creates no weights and failed saving retains session preferences', () => {
  const damaged = fixture({storage: new Map([['rigpilot_workload_preferences_v1:alice', '{broken']])});
  assert.deepEqual(Object.keys(damaged.state.workloadPreferences.weights), []);
  const stored = {mode: 'single', coins: ['PEARL', 'PEARL', 'XMR', '<coin>'],
    weights: {PEARL: 0, XMR: '-1', QUANTUS: '0x10', RVN: '0.25', '<coin>': '1'}};
  const restored = fixture({storage: new Map([['rigpilot_workload_preferences_v1:alice', JSON.stringify(stored)]])});
  assert.deepEqual(Array.from(restored.context.selectedWorkloadCoins()), ['PEARL']);
  assert.deepEqual(Object.entries(restored.state.workloadPreferences.weights), [['PEARL', '0'], ['RVN', '0.25']]);
  const privateSession = fixture({privateStorage: true});
  assert.deepEqual(Object.keys(privateSession.state.workloadPreferences.weights), []);
  privateSession.state.workloadPreferences.coins = ['PEARL'];
  privateSession.state.workloadPreferences.weights.PEARL = '2';
  privateSession.context.persistWorkloadPreferences();
  assert.equal(privateSession.state.workloadPreferencesStorageError, true);
  privateSession.state.workload.series = [series('PEARL', [{agent_id: 'a', value: 1e9}], 1e9)];
  assert.equal(privateSession.context.workloadCombinedModel().total, 2);
  render(privateSession);
  assert.match(privateSession.$('#workloadWeightsStatus').textContent, /当前会话仍保留/);
});

test('finite excessive products or sums cannot overflow into percentages or revenue', () => {
  const f = fixture();
  f.state.workloadPreferences.coins = ['PEARL'];
  f.state.workloadPreferences.weights.PEARL = '1e308';
  f.state.workload.series = [series('PEARL', [{agent_id: 'a', value: 10e9}], 10e9)];
  let model = f.context.workloadCombinedModel();
  assert.equal(model.overflow, true);
  assert.equal(model.ready, false);
  assert.equal(model.total, null);
  assert.deepEqual(revenues(render(f)), ['—']);
  assert.equal(f.$('#workloadRevenueTotal').disabled, true);
  f.state.workloadPreferences.coins = ['PEARL', 'QUANTUS'];
  f.state.workloadPreferences.weights.QUANTUS = '1e308';
  f.state.workload.series = [series('PEARL', [{agent_id: 'a', value: 1e9}], 1e9),
    series('QUANTUS', [{agent_id: 'b', value: 1e9}], 1e9)];
  model = f.context.workloadCombinedModel();
  assert.equal(model.overflow, true);
  assert.equal(model.total, null);
});

test('loading/errors/unrequested state disables revenue without clearing weights or amount', () => {
  for (const mode of ['loading', 'error', 'unrequested']) {
    const f = filteredFixture();
    render(f);
    setRevenue(f, '70');
    const preferencesBefore = JSON.stringify(f.state.workloadPreferences);
    if (mode === 'loading') f.state.workloadLoading = true;
    if (mode === 'error') f.state.workloadError = '<failed>';
    if (mode === 'unrequested') f.state.workloadHasRequested = false;
    const html = render(f);
    assert.equal(f.$('#workloadRevenueTotal').disabled, true, mode);
    assert.deepEqual(revenues(html), [], mode);
    assert.equal(JSON.stringify(f.state.workloadPreferences), preferencesBefore, mode);
    assert.equal(f.state.workloadRevenueAmount, '70', mode);
    if (mode === 'error') assert.match(html, /&lt;failed&gt;/);
    f.state.workloadLoading = false;
    f.state.workloadError = '';
    f.state.workloadHasRequested = true;
    assert.deepEqual(revenues(render(f)), ['6.00'], mode);
  }
});
