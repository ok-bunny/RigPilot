'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();
const fn = functionLoader(source);
function harness(agents, query = '') {
  const state = { agents, overviewOsFilter: 'all', filter: 'offline' };
  const context = vm.createContext({
    state, Date, Number, Math, Set, Map,
    $: () => ({ value: query }),
    deviceTableMiningSnapshot: agent => ({ rows: agent.rows || [] }),
    gpuMetricUuid: gpu => gpu.uuid || '',
    fullGpuPciAddress: gpu => gpu || '',
    gpuMetricIndex: gpu => Number.isInteger(gpu.index) ? gpu.index : null,
  });
  vm.runInContext([
    'overviewFreshOnlineAgent', 'overviewGpuTelemetry', 'overviewFormatPower',
    'overviewHashrateGroups', 'overviewFilteredAgents',
  ].map(fn).join('\n'), context);
  return context;
}
const row = (coin, algorithm, hps, role = 'primary') => ({ coin, algorithm, hashrateHps: hps, sourceRole: role });
const agent = (id, fields = {}) => ({ id, name: `Rig ${id}`, hostname: `RIG-${id}`, os_family: 'linux',
  status: 'online', last_seen: new Date().toISOString(), rows: [], metrics: { gpus: [] }, ...fields });

test('fleet hashrate groups PEARL algorithm aliases by coin, preserves zero and excludes stale/offline data', () => {
  const old = new Date(Date.now() - 10 * 60_000).toISOString();
  const agents = [
    agent('a', { rows: [row('PEARL', 'pearlhash', 1e12), row('PEARL', 'pearlhash', 1e12, 'total'), row('PEARL', 'pearlhash', 1e12)] }),
    agent('b', { rows: [row('PEARL', 'pearl', 2e12), row('XMR', 'randomx', 100, 'secondary')] }),
    agent('c', { rows: [row('PEARL', 'pearlhash', 4e12)] }),
    agent('d', { status: 'offline', rows: [row('PEARL', 'pearlhash', 999e12)] }),
    agent('e', { last_seen: old, rows: [row('PEARL', 'pearlhash', 999e12)] }),
    agent('f', { rows: [row('PEARL', 'pearlhash', null)] }),
    agent('g', { rows: [row('XEL', 'xelis', 0)] }),
    agent('h', { rows: [row('PEARL', '', 1e12)] }),
  ];
  const result = harness(agents).overviewHashrateGroups();
  assert.equal(result.groups.length, 3);
  assert.equal(result.missingOnline, 1);
  const byCoin = Object.fromEntries(result.groups.map(group => [group.coin, group.hps]));
  assert.equal(byCoin.PEARL, 8e12);
  assert.equal(byCoin.XMR, 100);
  assert.equal(byCoin.XEL, 0);
  assert.deepEqual([...result.groups.find(group => group.coin === 'PEARL').algorithms].sort(), ['pearl', 'pearlhash']);
});

test('GPU power counts only fresh online reported readings and does not double-count a UUID', () => {
  const fresh = agent('a', { metrics: { gpus: [
    { uuid: 'GPU-1', index: 0, power_w: 350, temperature_c: 59, fan_speed_percent: 65 },
    { uuid: 'GPU-2', index: 1, power_w: null, temperature_c: 60, fan_speed_percent: null },
    { uuid: 'GPU-1', index: 0, power_w: 999, temperature_c: 90, fan_speed_percent: 90 },
  ] } });
  const offline = agent('b', { status: 'offline', metrics: { gpus: [{ uuid: 'GPU-3', power_w: 5000 }] } });
  const stale = agent('c', { last_seen: new Date(Date.now() - 10 * 60_000).toISOString(), metrics: { gpus: [{ uuid: 'GPU-4', power_w: 1000 }] } });
  const h = harness([fresh, offline, stale]);
  const reading = h.overviewGpuTelemetry(fresh);
  assert.equal(reading.total, 2);
  assert.equal(reading.powerCount, 1);
  assert.equal(reading.powerW, 350);
  assert.equal(Math.max(...reading.temperatures), 60);
  assert.equal(reading.fans.length, 1);
  assert.equal(h.overviewGpuTelemetry(offline).powerCount, 0);
  assert.equal(h.overviewGpuTelemetry(stale).powerCount, 0);
  assert.equal(h.overviewFormatPower(1300), '1.30 kW');
  assert.equal(h.overviewFormatPower(0), '0 W');
});

test('overview OS and search filters use real agents without inheriting the device-page status filter', () => {
  const linux = agent('01', { rows: [row('PEARL', 'pearlhash', 1e12)] });
  const windows = agent('02', { os_family: 'windows', rows: [row('XMR', 'randomx', 100)] });
  const h = harness([linux, windows], 'pearl');
  assert.equal(h.overviewFilteredAgents().length, 1);
  assert.equal(h.overviewFilteredAgents()[0].id, '01');
  h.state.overviewOsFilter = 'windows';
  assert.equal(h.overviewFilteredAgents().length, 0);
  h.state.overviewOsFilter = 'linux';
  assert.equal(h.overviewFilteredAgents().length, 1);
  assert.equal(h.state.filter, 'offline');
});


test('overview temperature blocks follow reported GPUs, preserve zero, and distinguish unknown states', () => {
  const context = vm.createContext({
    Date, Number, Math, String, Array,
    gpuMetricIndex: gpu => Number.isInteger(gpu?.index) ? gpu.index : null,
    gpuDriverUnavailable: gpu => gpu?.driver_available === false && gpu?.error === 'nvidia_driver_unavailable',
  });
  vm.runInContext(['escapeHtml', 'cpuTemperatureC', 'overviewFreshOnlineAgent', 'overviewTemperatureMarkup'].map(fn).join('\n'), context);
  const rig = agent('01', { metrics: { gpus: [
    { index: 0, name: 'RTX <4090>', temperature_c: 0 },
    { index: 1, name: 'RTX 4090', temperature_c: 75 },
    { index: 2, name: 'RTX 4090', temperature_c: null },
    { index: 3, name: 'RTX 4090', temperature_c: 55, driver_available: false },
  ] } });
  const markup = context.overviewTemperatureMarkup(rig);
  assert.equal((markup.match(/overview-temperature-chip/g) || []).length, 4);
  assert.match(markup, /is-normal[^>]+GPU 0 · RTX &lt;4090&gt; · 实际温度 0°C[^>]*>0<\/span>/);
  assert.match(markup, /is-hot[^>]+GPU 1 · RTX 4090 · 实际温度 75°C[^>]*>75<\/span>/);
  assert.match(markup, /is-missing[^>]+GPU 2 · RTX 4090 · GPU 温度未上报/);
  assert.match(markup, /is-error[^>]+GPU 3 · RTX 4090 · 显卡驱动异常/);
  assert.doesNotMatch(markup, /CPU /);
  const cpu = context.overviewTemperatureMarkup(agent('cpu', { metrics: { gpus: [], cpu: { temperature_c: 0 } } }));
  assert.equal((cpu.match(/overview-temperature-chip/g) || []).length, 1);
  assert.match(cpu, /is-normal is-cpu[^>]+实际 CPU 温度 0°C[^>]*>0<\/span>/);
  const unknown = context.overviewTemperatureMarkup(agent('unknown', { metrics: { cpu: { temperature_c: 60 } } }));
  assert.match(unknown, /GPU 清单未上报/);
  assert.doesNotMatch(unknown, /is-cpu|is-normal/);
  const offline = context.overviewTemperatureMarkup(agent('offline', { status: 'offline', metrics: { gpus: [{ index: 0, temperature_c: 60 }] } }));
  assert.match(offline, /is-missing/);
  assert.doesNotMatch(offline, /is-normal|is-hot/);
  const stale = context.overviewTemperatureMarkup(agent('stale', { last_seen: new Date(Date.now() - 10 * 60_000).toISOString(), metrics: { gpus: [{ index: 0, temperature_c: 60 }] } }));
  assert.match(stale, /心跳过期/);
  assert.doesNotMatch(stale, /is-normal|is-hot/);
  const malformed = context.overviewTemperatureMarkup(agent('malformed', { metrics: { gpus: [null, { index: 1, temperature_c: 50 }] } }));
  assert.equal((malformed.match(/overview-temperature-chip/g) || []).length, 2);
});

test('overview retains row navigation and OS filters without inline mining or update actions', () => {
  const rig = agent('01');
  const body = { innerHTML: '', closest: () => ({}) };
  const empty = { classList: { toggle() {} } };
  const filters = ['all', 'linux', 'windows'].map(value => ({
    dataset: { overviewOs: value }, classList: { toggle() {} }, setAttribute() {}, textContent: '',
  }));
  const state = { agents: [rig], overviewOsFilter: 'all' };
  let bound = 0;
  const context = vm.createContext({
    state,
    $: selector => selector === '#overviewDeviceTableBody' ? body : empty,
    $$: selector => selector === '[data-overview-os]' ? filters : [],
    overviewFilteredAgents: () => [rig],
    overviewDeviceRow: () => '<tr data-agent-id="01"></tr>',
    setTableVisible() {}, bindAgentRows(rows) { bound += 1; assert.equal(rows.length, 0); },
  });
  vm.runInContext([fn('bindAgentUpdateDismiss'), fn('renderOverviewDevices')].join('\n'), context);
  context.renderOverviewDevices();
  assert.match(body.innerHTML, /data-agent-id="01"/);
  assert.doesNotMatch(body.innerHTML, /data-mining-action|data-agent-update-release/);
  assert.equal(bound, 1);
  assert.equal(filters[0].textContent, '全部设备 (1)');
  assert.equal(filters[1].textContent, 'Linux (1)');
});

test('split device coin and miner lists retain matching dual-mining order and version-only second line', () => {
  const context = vm.createContext({});
  vm.runInContext(['escapeHtml', 'deviceMiningColumnMarkup'].map(fn).join('\n'), context);
  const snapshot = { rows: [
    { roleLabel: '主挖', coin: 'PEARL', algorithm: 'pearlhash', amount: '840 TH/s',
      minerIdentity: { name: 'SRBMiner', version: '3.6.3', origin: 'runtime', hiveos: true },
      efficiency: { label: '622 GH/s/W', title: '逐卡功耗' } },
    { roleLabel: '副挖', coin: 'XMR', algorithm: 'randomx', amount: '21.57 kH/s',
      minerIdentity: { name: 'XMRig <CPU>', version: '', origin: 'unknown', hiveos: false },
      efficiency: null },
  ], placeholder: '--' };
  const coin = context.deviceMiningColumnMarkup(snapshot, 'coin');
  const miner = context.deviceMiningColumnMarkup(snapshot, 'miner');
  const rates = context.deviceMiningColumnMarkup(snapshot, 'hashrate');
  for (const markup of [coin, miner, rates]) assert.equal((markup.match(/class="device-mining-row /g) || []).length, 2);
  assert.ok(coin.indexOf('PEARL') < coin.indexOf('XMR'));
  assert.ok(miner.indexOf('SRBMiner') < miner.indexOf('XMRig &lt;CPU&gt;'));
  assert.ok(rates.indexOf('840 TH/s') < rates.indexOf('21.57 kH/s'));
  assert.ok(miner.includes('>3.6.3</span>'));
  assert.ok(miner.includes('title="3.6.3；设备上报的运行或统计版本"'));
  assert.ok(miner.includes('HiveOS'));
  assert.doesNotMatch(miner, /包版本：/);
  assert.ok(rates.includes('能效 622 GH/s/W'));
  assert.ok(rates.includes('能效 —'));
  assert.ok(context.deviceMiningColumnMarkup({ rows: [], placeholder: '未配置' }, 'miner').includes('未配置'));
});

test('summary card shows only coin and fresh hashrate rows for one, many, zero and unknown', () => {
  const elements = {
    '#summaryHashrateGroups': { innerHTML: '' },
    '#summaryGpuPower': { textContent: '' },
    '#summaryGpuPowerNote': { textContent: '' },
  };
  let result = { groups: [], missingOnline: 0 };
  const state = { agents: [{}] };
  const context = vm.createContext({
    state, Number, String, Math, Intl,
    HASHRATE_UNITS: ['H/s','kH/s','MH/s','GH/s','TH/s','PH/s','EH/s'],
    $: selector => elements[selector],
    overviewHashrateGroups: () => result,
    overviewFreshOnlineAgent: () => true,
    overviewGpuTelemetry: () => ({ total: 0, powerCount: 0, powerW: 0 }),
    overviewFormatPower: () => '—',
  });
  vm.runInContext(['escapeHtml','hasHashrateAmount','formatHashrateAmount','renderOverviewAggregateSummary'].map(fn).join('\n'), context);
  const makeGroup = (coin, hps) => ({coin, hps, algorithms: new Set(['sample']), agentIds: new Set(['a'])});
  const visible = () => elements['#summaryHashrateGroups'].innerHTML.replace(/<[^>]+>/g, '');
  result = { groups: [makeGroup('PEARL', 2.43e15), makeGroup('XMR', 110170)], missingOnline: 2 };
  context.renderOverviewAggregateSummary();
  assert.ok(visible().includes('PEARL2.43 PH/s'));
  assert.ok(visible().includes('XMR110.17 kH/s'));
  assert.doesNotMatch(visible(), /2 币种|按币种分别展示|无可用统计/);
  assert.ok(elements['#summaryHashrateGroups'].innerHTML.includes('title="PEARL · 2.43 PH/s · 算法 sample · 1 台在线设备 · 2 台在线设备无可用统计"'));
  result = { groups: [makeGroup('XMR', 0)], missingOnline: 0 };
  context.renderOverviewAggregateSummary();
  assert.equal(visible(), 'XMR0 H/s');
  result = { groups: [], missingOnline: 1 };
  context.renderOverviewAggregateSummary();
  assert.equal(visible(), '—');
});

test('five real GPU tiles retain two-decimal hashrate values and units', () => {
  const context = vm.createContext({
    escapeHtml: value => String(value),
    gpuMetricIndex: gpu => gpu.index,
    gpuTileModelLabel: () => '5060 Ti',
    gpuDriverUnavailable: () => false,
    gpuMiningHashrateTileReading: (_agent, gpu) => ({ identity: 'PEARL', amount: gpu.rate }),
  });
  vm.runInContext(['gpuTileHashrateAmountMarkup', 'gpuUtilizationPlaceholderMarkup', 'gpuUtilizationTableMarkup'].map(fn).join('\n'), context);
  const rates = ['96.19 TH/s', '96.20 TH/s', '96.25 TH/s', '96.39 TH/s', '211.58 TH/s'];
  const agent = { metrics: { gpus: rates.map((rate, index) => ({
    index, name: 'NVIDIA GeForce RTX 5060 Ti', temperature_c: 60 + index,
    fan_speed_percent: 70, rate,
  })) } };
  const markup = context.gpuUtilizationTableMarkup(agent);
  assert.equal((markup.match(/class="gpu-utilization-row"/g) || []).length, 5);
  for (const rate of rates) {
    const [amount, unit] = rate.split(' ');
    assert.ok(markup.includes(amount + '<span class="gpu-temperature-hashrate-unit">' + unit + '</span>'));
  }
  for (const count of [1, 4, 8, 12]) {
    const expanded = { metrics: { gpus: Array.from({ length: count }, (_, index) => ({
      index, name: 'NVIDIA GeForce RTX 5060 Ti', temperature_c: 60, rate: '211.58 TH/s',
    })) } };
    assert.equal((context.gpuUtilizationTableMarkup(expanded).match(/class="gpu-utilization-row"/g) || []).length, count);
  }
  assert.match(context.gpuUtilizationTableMarkup({ metrics: { gpus: [] } }), /class="gpu-utilization-placeholder"[^>]*aria-label="设备当前未上报GPU"/);
});
