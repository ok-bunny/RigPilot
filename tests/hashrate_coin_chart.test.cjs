'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const fs = require('node:fs'), vm = require('node:vm');
const test = require('node:test'), assert = require('node:assert/strict');
const source = readStatic();
const fn = functionLoader(source);
function harness(history) {
  const chart = {innerHTML:'', querySelectorAll: () => []}, meta = {textContent:''};
  const context = vm.createContext({state:{hashrateHistory:history},
    $: selector => selector === '#hashrateHistoryChart' ? chart : meta,
    hashrateChartColors: () => ['#123456'], escapeHtml: value => String(value).replaceAll('<','&lt;'),
    HASHRATE_UNITS:['H/s','kH/s','MH/s','GH/s','TH/s','PH/s','EH/s'], Intl, Date, Math, Number});
  vm.runInContext(['hashrateHistorySeriesLabel','hashrateHistoryPoints','hashrateHistorySegments',
    'hasHashrateAmount','formatHashrateAmount','renderHashrateSeries','bindHashrateHistoryHover','renderHashrateHistory'].map(fn).join('\n'), context);
  return {context,chart,meta};
}
test('coin aggregate renders correct units and a missing-point gap from API-shaped data', () => {
  const evidence = process.env.RIGPILOT_CHART_API_EVIDENCE;
  // The Python API wrapper can supply its real local response. Standalone Node
  // runs use the same shape with deterministic synthetic values.
  const history = evidence ? JSON.parse(fs.readFileSync(evidence,'utf8')) : {
    group_by: 'coin', hours: 24, bucket_minutes: 5,
    from: '2026-09-28T00:00:00Z', to: '2026-09-28T00:15:00Z',
    series: [{coin: 'PEARL', unit: 'H/s', points: [
      {timestamp: '2026-09-28T00:00:00Z', value: 3.53235e15},
      {timestamp: '2026-09-28T00:05:00Z', value: 3.53235e15},
      {timestamp: '2026-09-28T00:10:00Z', value: null},
      {timestamp: '2026-09-28T00:15:00Z', value: 3e14},
    ]}],
  };
  assert.equal(history.group_by, 'coin'); assert.equal(history.series.length, 1);
  assert.equal(history.series[0].points[0].value, 3.53235e15);
  const h = harness(history); h.context.renderHashrateHistory();
  assert.equal((h.chart.innerHTML.match(/class="hashrate-series-card"/g)||[]).length, 1);
  assert.match(h.chart.innerHTML, /<strong>PEARL<\/strong>/);
  assert.doesNotMatch(h.chart.innerHTML, /pearlhash|PEARL · pearl/);
  assert.match(h.chart.innerHTML, /3.53 PH\/s/); assert.match(h.chart.innerHTML, /300 TH\/s/);
  assert.equal((h.chart.innerHTML.match(/<polyline/g)||[]).length, 1);
  assert.equal((h.chart.innerHTML.match(/class="hashrate-history-point"/g)||[]).length, 1);
});
test('unknown-source labels stay explicit and known coin labels omit algorithm aliases', () => {
  const h = harness({series:[]});
  assert.equal(h.context.hashrateHistorySeriesLabel({coin:'PEARL',algorithm:'pearlhash'}),'PEARL');
  assert.match(h.context.hashrateHistorySeriesLabel({coin:'UNKNOWN',algorithm:'pearl',agent_id:'abcdefgh-1234'}),/未知币种 · pearl · abcdefgh/);
});

test('three API coin series keep independent cards and unit-aware axes', () => {
  const from = '2026-09-28T00:00:00Z';
  const to = '2026-09-28T00:05:00Z';
  const history = { group_by: 'coin', hours: 24, bucket_minutes: 5, from, to, series: [
    { coin: 'PEARL', unit: 'H/s', points: [{timestamp: from, value: 2.4e15}, {timestamp: to, value: 2.5e15}] },
    { coin: 'XMR', unit: 'kH/s', points: [{timestamp: from, value: 110}, {timestamp: to, value: 120}] },
    { coin: 'XEL', unit: 'MH/s', points: [{timestamp: from, value: 0}, {timestamp: to, value: 3}] },
  ] };
  const h = harness(history);
  h.context.renderHashrateHistory();
  assert.equal((h.chart.innerHTML.match(/class="hashrate-series-card"/g) || []).length, 3);
  assert.equal((h.chart.innerHTML.match(/<polyline/g) || []).length, 3);
  assert.match(h.chart.innerHTML, /<strong>PEARL<\/strong>/);
  assert.match(h.chart.innerHTML, /<strong>XMR<\/strong>/);
  assert.match(h.chart.innerHTML, /<strong>XEL<\/strong>/);
  assert.match(h.chart.innerHTML, /120 kH\/s/);
  assert.match(h.chart.innerHTML, /0 MH\/s/);
  assert.doesNotMatch(h.chart.innerHTML, /120 H\/s/);
  assert.match(h.meta.textContent, /3 条序列/);
});

test('missing and invalid historical readings are not converted to zero', () => {
  const h = harness({series: []});
  const points = h.context.hashrateHistoryPoints({points: [
    {timestamp: '2026-09-28T00:00:00Z', value: null},
    {timestamp: '2026-09-28T00:01:00Z', value: ''},
    {timestamp: '2026-09-28T00:02:00Z', value: false},
    {timestamp: '2026-09-28T00:03:00Z', value: 0},
  ]});
  assert.equal(points.length, 1);
  assert.equal(points[0].value, 0);
});
