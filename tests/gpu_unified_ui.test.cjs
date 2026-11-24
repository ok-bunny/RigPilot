'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();
const fn = functionLoader(source);
const pciIdentitySource = readStatic('pci-identity-data.js');

function fixture() {
  const gpu = {index: 0, uuid: 'GPU-synthetic-0', vendor: 'nvidia', name: 'NVIDIA Synthetic Card',
    board_vendor: 'Gigabyte', memory_vendor: 'Samsung', memory_type: 'GDDR7',
    memory_used_mib: 571, memory_total_mib: 16030, memory_percent: 4,
    utilization_percent: 100, temperature_c: 68, fan_speed_percent: 67, power_w: 250,
    graphics_clock_mhz: 2767, memory_clock_mhz: 810, vbios_version: '98.synthetic',
    power_min_limit_w: 100, power_default_limit_w: 360, power_max_limit_w: 400, power_limit_w: 250,
    pci_bus_id: '0000:01:00.0', driver_version: '615.synthetic',
    oc_capabilities: ['power_limit_w', 'core_clock_offset_mhz', 'memory_clock_lock_mhz']};
  const status = {state: 'running', miner_type: 'srbminer', gpu_oc_profiles: [], hashrate_details: [
    {coin: 'PEARL', algorithm: 'pearlhash', miner_type: 'srbminer', gpu_hashrates: [{index: 0, hashrate: 223, hashrate_unit: 'TH/s'}]},
    {coin: 'SECOND', algorithm: 'synthetic', miner_type: 'srbminer', gpu_hashrates: [{index: 0, hashrate: 0, hashrate_unit: 'MH/s'}]},
  ]};
  const agent = {id: 'synthetic-rig', name: 'Synthetic rig', status: 'online', agent_version: '0.8.37',
    last_seen: new Date().toISOString(), metrics: {gpus: [gpu]}, inventory: {mining_status: status}, desired_gpu_oc: []};
  const state = {user: {role: 'operator'}, selectedAgent: agent};
  const context = vm.createContext({state, Date, Math, Number, Object, Array, String, Set, Map, Boolean,
    gpuModelMemoryTypes: {},
    gpuOcFields: ['power_limit_w', 'core_clock_offset_mhz', 'core_clock_lock_mhz', 'memory_clock_offset_mhz', 'memory_clock_lock_mhz', 'fan_percent'],
    escapeHtml: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    miningStatusForAgent: item => item.inventory.mining_status,
    miningDisplayState: item => item.state,
    miningStatusUsesHiveos: item => item?.miner_type === 'hiveos',
    deviceRuntimePreparationReason: item => item.preparationReason || '',
    formatHashrateAmount: (value, unit) => `${value} ${unit}`,
    hiveosVerifiedGpuReadings: () => [],
  });
  vm.runInContext(pciIdentitySource, context);
  vm.runInContext([
    'metric', 'gpuNumberValue', 'gpuTextValue', 'gpuMetricIndex', 'gpuMetricUuid', 'gpuReportedVendor',
    'gpuMetricIsNvidia', 'gpuOcCapabilities', 'gpuDriverUnavailable', 'gpuDisplayIdentity', 'fullGpuPciAddress',
    'gpuNormalizedModel', 'gpuMemoryIdentityParts', 'gpuMemoryIdentityProvenance', 'gpuBoardLabel',
    'gpuMemoryLabel', 'gpuPowerLimitsLabel', 'gpuOcParameterSummary', 'gpuOcProfiles',
    'gpuDesiredOcEntry', 'gpuStandaloneOcSetting', 'gpuOcSettingMatches', 'compactGpuModel', 'gpuActiveOcMarkup',
    'gpuMiningHashrateSnapshot', 'gpuMiningHashrateMarkup', 'hasHashrateAmount', 'readingTimestamp', 'miningHashrateDetailIsFresh',
    'agentVersionAtLeast', 'overviewFreshOnlineAgent', 'gpuOcButtonMarkup', 'deviceGpuDriversMarkup', 'deviceDetailGpuRow',
  ].map(fn).join('\n'), context);
  return {context, state, agent, gpu, status};
}

test('one GPU row retains nine cells, every algorithm, complete hardware and exact per-card controls', () => {
  const {context, agent, gpu, status} = fixture();
  const markup = context.deviceDetailGpuRow(agent, gpu);
  assert.match(markup, /class="device-gpu-row"/);
  assert.equal((markup.match(/<tr\b/g) || []).length, 1);
  assert.equal((markup.match(/<td\b/g) || []).length, 9);
  assert.deepEqual([...markup.matchAll(/<td class="device-gpu-cell-([^"]+)"/g)].map(item => item[1]),
    ['model', 'hashrate', 'thermals', 'utilization', 'clocks', 'power', 'identity', 'oc', 'status']);
  assert.doesNotMatch(markup, /device-gpu-detail-row|device-gpu-hardware-band|colspan|data-select-gpu-uuid|tabindex|aria-selected/);
  assert.match(markup, /class="device-gpu-model-heading"/);
  assert.match(markup, /class="device-gpu-model-identity">GPU 0/);
  for (const value of ['Gigabyte', 'Samsung GDDR7', '223 TH/s', 'PEARL · pearlhash', '0 MH/s', 'SECOND · synthetic',
    '100%', '571 / 16030 MiB · 4%', '2767 MHz', '810 MHz', '98.synthetic', '100 / 360 / 400 W', '当前 PL 250 W',
    'UUID GPU-synthetic-0', 'PCI 0000:01:00.0', 'data-device-gpu-oc="0"']) assert.ok(markup.includes(value), value);
  assert.doesNotMatch(markup, /<details|完整硬件与 OC/);
  assert.doesNotMatch(markup, /615.synthetic/); // Shared driver is rendered once in the header.
  assert.doesNotMatch(markup, /class="device-gpu-vendor"/); // NVIDIA already appears in the model.
  for (const value of ['UUID GPU-synthetic-0', 'PCI 0000:01:00.0']) {
    assert.ok(markup.includes(`class="device-gpu-identifier" title="${value}" aria-label="${value}">${value}</span>`), value);
  }
  const second = {...gpu, index: 1, uuid: 'GPU-synthetic-1', pci_bus_id: '0000:02:00.0', temperature_c: 58, power_w: 150};
  agent.metrics.gpus.push(second);
  status.hashrate_details[0].gpu_hashrates.push({index: 1, hashrate: 99, hashrate_unit: 'TH/s'});
  const secondMarkup = context.deviceDetailGpuRow(agent, second);
  for (const value of ['GPU 1', 'UUID GPU-synthetic-1', 'PCI 0000:02:00.0', '58°C', '150 W', '99 TH/s', 'data-device-gpu-oc="1"']) assert.ok(secondMarkup.includes(value), value);
  assert.doesNotMatch(secondMarkup, /GPU-synthetic-0|0000:01:00.0|68°C|223 TH\/s|data-device-gpu-oc="0"/);
});

test('equal OC values render once while retaining all three source meanings without sampled target observations', () => {
  const {context, agent, gpu, status} = fixture();
  const setting = {power_limit_w: 250, core_clock_offset_mhz: 0, memory_clock_lock_mhz: 810};
  agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, name: gpu.name, setting: {index: 0, ...setting}}];
  status.gpu_oc_profiles = [
    {source: 'srbminer_cli', state: 'declared', settings: [{nvml_index: 0, ...setting}]},
    {source: 'nvml', state: 'applied_record', identity_complete: true, settings: [{uuid: gpu.uuid, index: 7, ...setting}]},
  ];
  const markup = context.gpuActiveOcMarkup(agent, gpu);
  assert.equal((markup.match(/class="device-gpu-oc-record gpu-active-oc"/g) || []).length, 1);
  assert.equal((markup.match(/功耗上限 250 W/g) || []).length, 1);
  assert.equal((markup.match(/class="device-gpu-oc-parameter"/g) || []).length, 3);
  for (const label of ['核心偏移 +0 MHz', '控制端已保存', 'SRBMiner 参数声明', 'NVML 最近成功下发',
    '参数一致']) assert.ok(markup.includes(label), label);
  assert.doesNotMatch(markup, /采样观测|显存目标|核心目标|不等同于当前生效参数/);
});




test('OC editor defaults prefer desired, then matching legacy NVML successful records', () => {
  const {context, agent, gpu, status} = fixture();
  const old = {source: 'nvml', state: 'applied_record', settings: [{uuid: gpu.uuid, index: 0, power_limit_w: 210}]};
  status.gpu_oc_profiles = [old];
  agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, setting: {power_limit_w: 250}}];
  assert.equal(context.gpuStandaloneOcSetting(agent, gpu).power_limit_w, 250);
  agent.desired_gpu_oc = [];
  assert.equal(context.gpuStandaloneOcSetting(agent, gpu).power_limit_w, 210);
  old.state = 'unconfirmed_record';
  assert.equal(context.gpuStandaloneOcSetting(agent, gpu), null);
});

test('localized record labels share the exact legacy formatting and preserve its default English consumers', () => {
  const {context} = fixture();
  const setting = {power_limit_w: 250, core_clock_offset_mhz: 0, core_clock_lock_mhz: 1800,
    memory_clock_offset_mhz: -100, memory_clock_lock_mhz: 810, fan_percent: 0};
  assert.equal(context.gpuOcParameterSummary(setting),
    'Power Limit 250 W · Core Offset +0 MHz · Lock Core 1800 MHz · Memory Offset -100 MHz · Lock Memory 810 MHz · Fan 0%');
  assert.equal(context.gpuOcParameterSummary(setting, true),
    '功耗上限 250 W · 核心偏移 +0 MHz · 核心锁频 1800 MHz · 显存偏移 -100 MHz · 显存锁频 810 MHz · 风扇 0%');
});

test('only redundant NVIDIA labels disappear; AMD, unknown vendors and incomplete names keep meaningful labels', () => {
  const {context, agent, gpu} = fixture();
  assert.doesNotMatch(context.deviceDetailGpuRow(agent, gpu), /class="device-gpu-vendor"/);
  gpu.name = 'Synthetic Card';
  assert.match(context.deviceDetailGpuRow(agent, gpu), /class="device-gpu-vendor">NVIDIA/);
  gpu.vendor = 'amd'; gpu.name = 'AMD Synthetic Card';
  assert.match(context.deviceDetailGpuRow(agent, gpu), /class="device-gpu-vendor">AMD · 只读监控/);
  gpu.vendor = ''; gpu.name = 'Unconfirmed Card';
  assert.match(context.deviceDetailGpuRow(agent, gpu), /class="device-gpu-vendor">厂商未确认/);
});

test('different or missing OC values never merge by rounded or partial display', () => {
  const {context, agent, gpu, status} = fixture();
  agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, name: gpu.name, setting: {index: 0, power_limit_w: 250}}];
  status.gpu_oc_profiles = [
    {source: 'srbminer_cli', state: 'declared', settings: [{index: 0, power_limit_w: 250, core_clock_offset_mhz: 0}]},
    // Persisted real profiles reject null. This synthetic malformed input only
    // confirms defensive grouping cannot equate null with a missing field.
    {source: 'nvml', state: 'applied_record', identity_complete: true, settings: [{uuid: gpu.uuid, power_limit_w: 250, core_clock_offset_mhz: null}]},
    {source: 'nvml', state: 'applied_record', identity_complete: true, settings: [{uuid: gpu.uuid, power_limit_w: 250.1}]},
  ];
  const markup = context.gpuActiveOcMarkup(agent, gpu);
  assert.equal((markup.match(/class="device-gpu-oc-record gpu-active-oc"/g) || []).length, 4);
  assert.doesNotMatch(markup, /参数一致/);
  assert.match(markup, /功耗上限 250.1 W/);
});

test('OC matching rejects another UUID and inactive or unconfirmed SRB/NVML states', () => {
  const {context, agent, gpu, status} = fixture();
  status.gpu_oc_profiles = [
    {source: 'nvml', state: 'applied_record', identity_complete: true, settings: [{uuid: 'GPU-other', index: 0, power_limit_w: 200}]},
    {source: 'nvml', state: 'failed', settings: [{index: 0, power_limit_w: 210}]},
    {source: 'srbminer_cli', state: 'pending', settings: [{index: 0, power_limit_w: 220}]},
  ];
  assert.match(context.gpuActiveOcMarkup(agent, gpu), /暂无已保存/);
  status.gpu_oc_profiles.push({source: 'srbminer_cli', state: 'declared', settings: [{index: 0, power_limit_w: 230}]});
  status.state = 'stopped';
  assert.doesNotMatch(context.gpuActiveOcMarkup(agent, gpu), /功耗上限|class="device-gpu-oc-source"/);
  assert.equal(context.gpuStandaloneOcSetting(agent, gpu), null);
});

for (const [recoveryStatus, label] of [
  ['rolled_back', '上次尝试：参数已回滚'], ['defaults_restored', '上次尝试：参数恢复默认'],
  ['partial', '上次尝试：参数部分恢复'], ['failed', '上次尝试：参数恢复未完成'],
]) {
  test(`OC replay hold displays saved values and the actual ${recoveryStatus} receipt without hiding the raw error`, () => {
    const {context, agent, gpu} = fixture();
    const error = 'query GPU 0 failed: <script>"GPU requires reset"</script>';
    agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, intent_id: 'current-intent',
      setting: {index: 0, power_limit_w: 250}, replay_block: {
        intent_id: 'current-intent', reason: 'hardware_reset_required', error,
        recovery_status: recoveryStatus, hardware_reset_required: true,
        state_reconciled: false, confirmed_reset: [
          {gpu_uuid: gpu.uuid, index: 0, reset: ['power_limit_w', 'power_limit_w', 'unsupported_field']},
          {gpu_uuid: 'GPU-another', index: 1, reset: ['fan_percent', 'core_clock_lock_mhz']},
        ],
      }}];
    const markup = context.gpuActiveOcMarkup(agent, gpu);
    for (const value of ['功耗上限 250 W', '控制端已保存 · 自动重试已暂停', label,
      '上次尝试报告需要设备重置', 'Agent 记录同步未确认', '上次确认 1 项参数已复位',
      '&lt;script&gt;&quot;GPU requires reset&quot;&lt;/script&gt;']) assert.ok(markup.includes(value), value);
    assert.doesNotMatch(markup, /<script>|上次确认 [23] 项|当前硬件已恢复|设备健康/);
    assert.equal((markup.match(/query GPU 0 failed:/g) || []).length, 2); // title and visible error.
  });
}

test('legacy OC replay hold keeps original error and never upgrades it to a recovery claim', () => {
  const {context, agent, gpu} = fixture();
  agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, intent_id: 'legacy-intent',
    setting: {index: 0, memory_clock_lock_mhz: 7001}, replay_block: {
      intent_id: 'legacy-intent', reason: 'legacy_reset_required',
      error: 'set GPU 0 memory_clock_lock_mhz failed: GPU requires reset',
      recovery_status: 'rolled_back', recovered: true,
    }}];
  const markup = context.gpuActiveOcMarkup(agent, gpu);
  assert.match(markup, /自动重试已暂停/);
  assert.match(markup, /旧 Agent 未上报恢复结果/);
  assert.match(markup, /set GPU 0 memory_clock_lock_mhz failed: GPU requires reset/);
  assert.doesNotMatch(markup, /上次尝试：参数已回滚|上次尝试：参数恢复默认|已恢复正常/);
});

test('NVML15 device fault pauses retry without claiming device reset or verified hardware identity', () => {
  const {context, agent, gpu} = fixture();
  agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, intent_id: 'device-fault-intent',
    setting: {index: 0, core_clock_offset_mhz: 500}, replay_block: {
      intent_id: 'device-fault-intent', reason: 'hardware_fault', error: 'NVML15: GPU is lost',
      recovery_status: 'failed', recovered: false, nvml_codes: [15],
      hardware_reset_required: false, hardware_identity_confirmed: false,
    }}];
  const markup = context.gpuActiveOcMarkup(agent, gpu);
  for (const label of ['控制端已保存 · 自动重试已暂停', '上次尝试：参数恢复未完成',
    '故障回执未确认硬件身份', 'NVML15: GPU is lost']) assert.ok(markup.includes(label), label);
  assert.doesNotMatch(markup, /需要设备重置|已确认硬件身份|已恢复正常/);
});

test('OC replay hold stays with its UUID and current intent; ordinary saved records remain unchanged', () => {
  const {context, agent, gpu} = fixture();
  const block = {intent_id: 'current-intent', reason: 'recovery_failed', error: 'original-card failure', recovery_status: 'failed'};
  agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, intent_id: 'current-intent',
    setting: {index: 0, power_limit_w: 250}, replay_block: block},
  {gpu_uuid: 'GPU-second', intent_id: 'second-intent', setting: {index: 1, power_limit_w: 150}}];
  const other = {...gpu, uuid: 'GPU-second', index: 1};
  const second = context.gpuActiveOcMarkup(agent, other);
  assert.match(second, /功耗上限 150 W/); assert.match(second, /控制端已保存/);
  assert.doesNotMatch(second, /自动重试已暂停|original-card failure/);
  agent.desired_gpu_oc[0].intent_id = 'new-intent';
  const newer = context.gpuActiveOcMarkup(agent, gpu);
  assert.match(newer, /功耗上限 250 W/); assert.match(newer, /控制端已保存/);
  assert.doesNotMatch(newer, /自动重试已暂停|original-card failure/);
  delete agent.desired_gpu_oc[0].intent_id;
  assert.doesNotMatch(context.gpuActiveOcMarkup(agent, gpu), /自动重试已暂停/);
});

test('missing numeric telemetry never becomes a zero, and valid zero remains visible', () => {
  const {context, agent, gpu} = fixture();
  for (const field of ['memory_total_mib', 'memory_used_mib', 'memory_percent', 'graphics_clock_mhz',
    'memory_clock_mhz', 'power_min_limit_w', 'power_default_limit_w', 'power_max_limit_w', 'power_limit_w']) gpu[field] = null;
  assert.equal(context.gpuMemoryLabel(gpu), '—');
  assert.equal(context.gpuPowerLimitsLabel(gpu), '未上报');
  for (const raw of [undefined, null, '', ' ', true, false, NaN, Infinity]) assert.equal(context.gpuNumberValue(raw), null);
  const missingMarkup = context.deviceDetailGpuRow(agent, gpu);
  assert.doesNotMatch(missingMarkup, /当前 PL 0|0 MHz|0 \/ 0 MiB/);
  Object.assign(gpu, {memory_total_mib: 0, memory_used_mib: 0, memory_percent: 0,
    graphics_clock_mhz: 0, memory_clock_mhz: 0, power_min_limit_w: 0, power_default_limit_w: 0,
    power_max_limit_w: 0, power_limit_w: 0, temperature_c: 0, fan_speed_percent: 0, power_w: 0});
  const zeroMarkup = context.deviceDetailGpuRow(agent, gpu);
  for (const value of ['0 / 0 MiB · 0%', '0 MHz', '0 / 0 / 0 W', '当前 PL 0 W', '0°C', '0%']) assert.ok(zeroMarkup.includes(value), value);
});

test('memory percentage derives from real used/total only when the reported percentage is missing', () => {
  const {context} = fixture();
  assert.equal(context.gpuMemoryLabel({memory_used_mib: 0, memory_total_mib: 100}), '0 / 100 MiB · 0%');
  assert.equal(context.gpuMemoryLabel({memory_total_mib: 100}), '— / 100 MiB');
  assert.equal(context.gpuMemoryLabel({memory_used_mib: 50, memory_total_mib: 100}), '50 / 100 MiB · 50%');
  assert.equal(context.gpuPowerLimitsLabel({power_default_limit_w: 200}), '— / 200 / — W');
});

test('common driver is shown once and differing or partially missing drivers retain GPU provenance', () => {
  const {context, gpu} = fixture();
  const second = {...gpu, index: 1, uuid: 'GPU-synthetic-1'};
  assert.equal((context.deviceGpuDriversMarkup([gpu, second]).match(/615.synthetic/g) || []).length, 1);
  second.driver_version = '616.synthetic';
  const different = context.deviceGpuDriversMarkup([gpu, second]);
  assert.match(different, /615.synthetic · GPU 0/); assert.match(different, /616.synthetic · GPU 1/);
  delete second.driver_version;
  assert.match(context.deviceGpuDriversMarkup([gpu, second]), /615.synthetic · GPU 0/);
});

test('AMD, missing identity, offline and maintenance controls remain read-only or disabled correctly', () => {
  const {context, agent, gpu} = fixture();
  gpu.vendor = 'amd'; gpu.index = null; gpu.display_index = 9; gpu.name = 'AMD Synthetic Card'; delete gpu.uuid;
  let markup = context.deviceDetailGpuRow(agent, gpu);
  assert.match(markup, /GPU 9/); assert.match(markup, /AMD · 只读监控/); assert.doesNotMatch(markup, /data-device-gpu-oc=/);
  assert.match(markup, /class="device-gpu-model-identity">GPU 9/);
  gpu.index = 0; // A legacy hint must not replace the vendor-specific display identity.
  assert.match(context.deviceDetailGpuRow(agent, gpu), /GPU 9/);
  gpu.display_index = null;
  assert.match(context.deviceDetailGpuRow(agent, gpu), /PCI 0000:01:00.0/);
  Object.assign(gpu, {index: 0, vendor: 'nvidia', uuid: 'GPU-synthetic-0'});
  agent.status = 'offline';
  assert.match(context.deviceDetailGpuRow(agent, gpu), /data-device-gpu-oc="0" disabled title="设备离线/);
  agent.status = 'online'; agent.preparationReason = '正在准备 HiveOS 运行环境';
  assert.match(context.deviceDetailGpuRow(agent, gpu), /disabled title="正在准备 HiveOS/);
  assert.doesNotMatch(context.deviceDetailGpuRow(agent, gpu, false), /data-device-gpu-oc=/);
});

test('driver errors retain diagnostics and identity and never offer OC or invented zero metrics', () => {
  const {context, agent, gpu} = fixture();
  Object.assign(gpu, {driver_available: false, error: 'nvidia_driver_unavailable', temperature_c: 0, power_w: 0});
  const markup = context.deviceDetailGpuRow(agent, gpu);
  assert.match(markup, /驱动异常/); assert.match(markup, /Above 4G Decoding/); assert.match(markup, /PCI 0000:01:00.0/);
  assert.doesNotMatch(markup, /data-device-gpu-oc=|0°C|>0 W<|223 TH\/s/);
});

test('explicit unavailable driver overrides stale OC capabilities and current-PL cache without hiding saved records', () => {
  const {context, agent, gpu} = fixture();
  Object.assign(gpu, {driver_available: false, error: 'nvidia_driver_unavailable', power_limit_w: 250});
  agent.desired_gpu_oc = [{gpu_uuid: gpu.uuid, name: gpu.name, setting: {index: 0, power_limit_w: 250}}];
  assert.ok(gpu.oc_capabilities.length > 0);
  const row = context.deviceDetailGpuRow(agent, gpu);
  assert.doesNotMatch(row, /当前 PL|data-device-gpu-oc=/);
  assert.match(row, /控制端已保存/); assert.match(row, /功耗上限 250 W/);
});

test('unmatched or unavailable per-GPU hashrate retains its explicit reason in the merged table', () => {
  const {context, agent, gpu, status} = fixture();
  status.hashrate_details = []; status.gpu_hashrate_status = 'identity_unmatched';
  assert.match(context.deviceDetailGpuRow(agent, gpu), /已拒绝显示可能错卡的算力/);
  status.gpu_hashrate_status = 'not_applicable'; status.miner_type = 'xmrig';
  assert.match(context.deviceDetailGpuRow(agent, gpu), /XMRig 当前仅上报 CPU 总算力/);
});

test('untrusted long GPU names, board names and OC/driver identifiers remain escaped', () => {
  const {context, agent, gpu} = fixture();
  gpu.name = '<script>synthetic()</script>' + 'long '.repeat(30);
  gpu.board_vendor = '<img onerror="synthetic">';
  gpu.driver_version = '<script>driver</script>';
  gpu.memory_identity_source = '<script>memory-source</script>';
  gpu.memory_identity_status = 'complete';
  const markup = context.deviceDetailGpuRow(agent, gpu) + context.deviceGpuDriversMarkup([gpu]);
  assert.doesNotMatch(markup, /<script>|<img /);
  assert.match(markup, /&lt;script&gt;/); assert.match(markup, /&lt;img onerror=&quot;/);
});

test('driver-reported Linux and Windows memory values override model hints without physical-inspection claims', () => {
  const {context, agent, gpu} = fixture();
  Object.assign(gpu, {name: 'NVIDIA GeForce RTX 5060 Ti', vbios_version: '98.synthetic', pci_subsystem_id: 'fffffefe'});
  for (const [source, vendor, type, label] of [
    ['nvml_private', 'Micron', 'GDDR6', 'NVML 驱动查询'],
    ['nvapi', 'SK hynix', 'GDDR7', 'NVAPI 驱动上报'],
  ]) {
    Object.assign(gpu, {memory_vendor: vendor, memory_type: type, memory_identity_source: source,
      memory_identity_status: 'complete', memory_identity_unavailable_reason: null});
    assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), [vendor, type]);
    const markup = context.deviceDetailGpuRow(agent, gpu);
    assert.ok(markup.includes(`${vendor} ${type}`));
    assert.ok(markup.includes(`显存来源：${label} · 采集状态：完整`));
    assert.doesNotMatch(markup, /物理拆检|已拆检/);
  }
});

test('explicit failed or unknown memory collection never fills a maker or type from model hints', () => {
  const {context, gpu} = fixture();
  Object.assign(gpu, {name: 'NVIDIA GeForce RTX 5060 Ti', vbios_version: '98.synthetic',
    pci_subsystem_id: 'fffffefe', memory_vendor: null, memory_type: null});
  context.gpuModelMemoryTypes['rtx 5060 ti'] = 'MODEL_TYPE';
  for (const source of ['nvml_private', 'nvapi']) {
    for (const reason of ['driver_memory_identity_unknown', 'native_probe_timeout', 'native_probe_crashed',
      'native_probe_unavailable', 'native_probe_identity_conflict']) {
      Object.assign(gpu, {memory_identity_source: source, memory_identity_status: 'unavailable',
        memory_identity_unavailable_reason: reason});
      assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), ['显存颗粒未上报']);
    }
  }
  delete gpu.memory_identity_source;
  delete gpu.memory_identity_status;
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), ['显存颗粒未上报']); // Explicit failure reason remains.
  delete gpu.memory_identity_unavailable_reason;
  delete gpu.vbios_version;
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), ['MODEL_TYPE（颗粒厂商未上报）']);
});

test('partial and legacy explicitly reported memory retain their values and source meaning', () => {
  const {context, gpu} = fixture();
  Object.assign(gpu, {memory_vendor: null, memory_type: 'GDDR7', memory_identity_source: 'nvml_private',
    memory_identity_status: 'partial', memory_identity_unavailable_reason: 'driver_reported_partial_identity'});
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), ['GDDR7（颗粒厂商未上报）']);
  Object.assign(gpu, {memory_vendor: 'Samsung', memory_type: null, memory_identity_source: 'nvapi'});
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), ['Samsung']);
  Object.assign(gpu, {memory_vendor: 'Samsung', memory_type: 'GDDR7', memory_identity_source: 'verified_vbios', memory_identity_status: 'complete'});
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), ['Samsung', 'GDDR7']);
  assert.match(context.gpuMemoryIdentityProvenance(gpu), /既有 VBIOS 对应记录/);
  Object.assign(gpu, {memory_vendor: null, memory_identity_source: 'product_spec', memory_identity_status: 'partial'});
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(gpu)), ['GDDR7（颗粒厂商未上报）']);
  assert.match(context.gpuMemoryIdentityProvenance(gpu), /型号规格 · 采集状态：部分/);
});
