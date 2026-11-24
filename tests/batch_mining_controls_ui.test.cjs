'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();
const html = readStatic('index.html');
const fn = functionLoader(source);

test('batch mining control toolbar keeps explicit operator-only entries beside batch flight apply', () => {
  const toolbar = html.match(/<div class="device-toolbar-actions">[\s\S]*?<\/div>/)[0];
  assert.match(toolbar, /id="openFlightBatchApplyButton"/);
  assert.match(toolbar, /id="openMiningStopBatchButton"[^>]*type="button"[^>]*>批量停止挖矿</);
  assert.match(toolbar, /id="openMiningRestartBatchButton"[^>]*type="button"[^>]*>批量重启挖矿</);
  assert.match(toolbar, /id="openMiningStopBatchButton"[^>]*class="[^"]*operator-only/);
  assert.match(toolbar, /id="openMiningRestartBatchButton"[^>]*class="[^"]*operator-only/);
});

test('batch mining control dialog reuses the explicit batch UI contract', () => {
  assert.match(html, /id="miningControlBatchDialog"/);
  assert.match(html, /id="miningControlBatchForm"/);
  assert.match(html, /id="miningControlBatchTitle">批量停止挖矿</);
  assert.match(html, /id="miningControlBatchAgents"/);
  assert.match(html, /id="miningControlBatchSelectedCount">已选择 0 台</);
  assert.match(html, /id="miningControlBatchSubmitButton"/);
  assert.ok(html.match(/id="miningControlBatchWarning"[\s\S]{0,400}?>/), 'dialog carries the no-default-selection warning');
  const dialog = html.match(/<dialog id="miningControlBatchDialog"[\s\S]*?<\/dialog>/)[0];
  assert.match(dialog, /不会默认选择任何设备/);
  assert.match(dialog, /batch-choice-list/);
  assert.match(dialog, /batch-result-list/);
  assert.match(dialog, /与单机操作完全相同/);
});

test('batch mining control submits explicit selection through the shared result renderer', () => {
  assert.match(source, /function miningControlBatchEligibility\(agent, action\)/);
  assert.match(source, /function excludedMiningControlBatchAgents\(action\)/);
  assert.match(source, /function miningControlBatchConfirmMessage\(action, selected, excluded\)/);
  assert.match(source, /function openMiningControlBatchDialog\(action\)/);
  assert.match(source, /function renderMiningControlBatchAgents\(\)/);
  assert.match(source, /function selectedMiningControlBatchItems\(\)/);
  assert.match(source, /function syncMiningControlBatchSelection\(\)/);
  assert.match(source, /async function submitMiningControlBatch\(\)/);
  assert.match(source, /data-mining-control-batch="\$\{escapeHtml\(agent\.id\)\}"/);
  assert.match(source, /api\("\/api\/agents\/commands\/batch"/);
  assert.match(source, /body: \{ action, agent_ids: selected\.map\(\(agent\) => agent\.id\) \}/);
  assert.match(source, /renderBatchOperationResults\(\s*\$\("#miningControlBatchResults"\)/);
  assert.match(source, /\$\("#openMiningStopBatchButton"\)\.addEventListener\("click", \(\) => openMiningControlBatchDialog\("stop_mining"\)\)/);
  assert.match(source, /\$\("#openMiningRestartBatchButton"\)\.addEventListener\("click", \(\) => openMiningControlBatchDialog\("restart_mining"\)\)/);

  // The single-device entries stay authoritative and untouched.
  assert.match(source, /button\("stop_mining", "停止挖矿", stopUnavailable, "danger-text"\)/);
  assert.match(source, /button\("restart_mining", "重启挖矿", restartUnavailable\)/);
  assert.match(source, /api\(`\/api\/agents\/\$\{encodeURIComponent\(agentId\)\}\/commands`/);
});

function eligibilityHarness(overrides = {}) {
  const state = {commands: overrides.commands || []};
  const context = vm.createContext({
    state,
    agentSupportsMiningControl: () => overrides.supports !== false,
    miningStatusForAgent: () => overrides.status || {configured: true, state: 'running'},
    miningDisplayState: () => overrides.miningState || 'running',
    miningControlPresentation: () => overrides.presentation || {stopActive: true, stopReason: ''},
    deviceRuntimePreparationReason: () => overrides.preparationReason || '',
    miningStatusUsesHiveos: () => Boolean(overrides.hiveos),
    hiveosTargetEligibility: () => overrides.hiveosEligibility || {eligible: true, reason: ''},
  });
  vm.runInContext(fn('miningControlBatchEligibility'), context);
  return context;
}

test('batch mining eligibility reuses the single-device gate order and wording', () => {
  const base = {id: 'A', status: 'online', agent_version: '0.8.48', name: 'Rig A'};
  const offline = eligibilityHarness().miningControlBatchEligibility({...base, status: 'offline'}, 'stop_mining');
  assert.equal(offline.eligible, false);
  assert.equal(offline.reason, '设备离线，无法下发矿工控制任务');

  const oldAgent = eligibilityHarness({supports: false}).miningControlBatchEligibility(base, 'restart_mining');
  assert.equal(oldAgent.eligible, false);
  assert.equal(oldAgent.reason, '需要 RigPilot Agent 0.5.2 或更高版本');

  const unconfigured = eligibilityHarness({status: {configured: false}}).miningControlBatchEligibility(base, 'stop_mining');
  assert.equal(unconfigured.eligible, false);
  assert.equal(unconfigured.reason, '设备尚未应用飞行表');

  const pending = eligibilityHarness({commands: [
    {agent_id: 'A', action: 'stop_mining', status: 'queued'},
  ]}).miningControlBatchEligibility(base, 'stop_mining');
  assert.equal(pending.eligible, false);
  assert.equal(pending.reason, '已有停止或重启挖矿任务正在执行');

  const otherAgentPending = eligibilityHarness({commands: [
    {agent_id: 'B', action: 'restart_mining', status: 'running'},
  ]}).miningControlBatchEligibility(base, 'stop_mining');
  assert.equal(otherAgentPending.eligible, true);

  const stoppedNowhere = eligibilityHarness({presentation: {stopActive: false, stopReason: '挖矿已经停止'}})
    .miningControlBatchEligibility(base, 'stop_mining');
  assert.equal(stoppedNowhere.eligible, false);
  assert.equal(stoppedNowhere.reason, '挖矿已经停止');

  const hiveosBlocked = eligibilityHarness({hiveos: true})
    .miningControlBatchEligibility(base, 'restart_mining');
  assert.equal(hiveosBlocked.eligible, false);
  assert.equal(hiveosBlocked.reason, 'HiveOS 已停止支持，不能重启该矿工');

  const stopEligible = eligibilityHarness().miningControlBatchEligibility(base, 'stop_mining');
  assert.equal(stopEligible.eligible, true);
  assert.equal(stopEligible.reason, '');
  const restartEligible = eligibilityHarness().miningControlBatchEligibility(base, 'restart_mining');
  assert.equal(restartEligible.eligible, true);
  assert.equal(restartEligible.reason, '');
});

test('confirm message lists executed count, excluded devices with reasons and unified copy', () => {
  const context = vm.createContext({});
  vm.runInContext(fn('miningControlBatchConfirmMessage'), context);
  const selected = [
    {id: 'a1', name: 'Rig One'},
    {id: 'a2', name: 'Rig Two'},
  ];
  const excluded = [
    {agent: {id: 'b1', name: 'Rig Off'}, reason: '设备离线，无法下发矿工控制任务'},
  ];
  const stopMessage = context.miningControlBatchConfirmMessage('stop_mining', selected, excluded);
  assert.match(stopMessage, /确认对以下 2 台设备执行批量停止挖矿？/);
  assert.match(stopMessage, /Rig One（a1）/);
  assert.match(stopMessage, /NVML OC 恢复为硬件默认值/);
  assert.match(stopMessage, /本次不会执行的设备（1 台）：/);
  assert.match(stopMessage, /Rig Off（b1）：设备离线，无法下发矿工控制任务/);
  assert.match(stopMessage, /未勾选设备不受影响/);

  const restartMessage = context.miningControlBatchConfirmMessage('restart_mining', selected, []);
  assert.match(restartMessage, /确认对以下 2 台设备执行批量重启挖矿？/);
  assert.match(restartMessage, /挖矿会短暂中断。/);
  assert.match(restartMessage, /本次不会执行的设备（0 台）：\n无/);

  const manyExcluded = Array.from({length: 22}, (_, index) => (
    {agent: {id: `x${index}`, name: `Rig ${index}`}, reason: '设备尚未应用飞行表'}
  ));
  const truncated = context.miningControlBatchConfirmMessage('stop_mining', selected.slice(0, 1), manyExcluded);
  assert.match(truncated, /本次不会执行的设备（22 台）：/);
  assert.match(truncated, /…以及其他 2 台设备/);
  assert.ok(!truncated.includes('Rig 21（x21）'), 'the truncated tail stays out of the message');
});

function submitHarness({role = 'admin', confirmOk = true, apiResponse = null, apiError = null} = {}) {
  const agents = [
    {id: 'a1', name: 'Rig One', status: 'online', agent_version: '0.8.48'},
    {id: 'a2', name: 'Rig Two', status: 'online', agent_version: '0.8.48'},
  ];
  const excludedAgent = {id: 'b1', name: 'Rig Off', status: 'offline', agent_version: '0.8.48'};
  const state = {
    user: {role},
    miningControlBatchAction: 'stop_mining',
    commands: [],
    agents: [...agents, excludedAgent],
  };
  const effects = {confirmMessages: [], api: null, rendered: null, toasts: [], refreshed: 0};
  const errorElement = {textContent: ''};
  const resultsElement = {innerHTML: '', classList: {add() {}, remove() {}}};
  const button = {disabled: false};
  const countElement = {textContent: ''};
  const hintElement = {textContent: ''};
  const checkedInputs = agents.map((agent) => ({checked: true, dataset: {miningControlBatch: agent.id}}));
  const eligibilityDeps = {
    agentSupportsMiningControl: () => true,
    miningStatusForAgent: () => ({configured: true, state: 'running'}),
    miningDisplayState: () => 'running',
    miningControlPresentation: () => ({stopActive: true, stopReason: ''}),
    deviceRuntimePreparationReason: () => '',
    miningStatusUsesHiveos: () => false,
    hiveosTargetEligibility: () => ({eligible: true, reason: ''}),
  };
  const context = vm.createContext({
    state,
    window: {confirm(message) { effects.confirmMessages.push(message); return confirmOk; }},
    $: (selector) => selector === '#miningControlBatchError' ? errorElement
      : selector === '#miningControlBatchResults' ? resultsElement
      : selector === '#miningControlBatchSubmitButton' ? button
      : selector === '#miningControlBatchSelectedCount' ? countElement
      : selector === '#miningControlBatchEligibilityHint' ? hintElement
      : selector === '#miningControlBatchAgents' ? {} : null,
    $$: () => checkedInputs,
    filteredAgents: () => [...agents, excludedAgent],
    api: async (url, options) => {
      effects.api = {url, options};
      if (apiError) throw apiError;
      return apiResponse || {accepted_count: 2, rejected_count: 0, accepted: [], rejected: []};
    },
    renderBatchOperationResults: (target, selected, response, idKeys) => {
      effects.rendered = {target, selected, response, idKeys};
    },
    toast: (message) => effects.toasts.push(message),
    refreshAll: async () => { effects.refreshed += 1; },
    ...eligibilityDeps,
  });
  vm.runInContext([
    fn('miningControlBatchEligibility'),
    fn('excludedMiningControlBatchAgents'),
    fn('miningControlBatchConfirmMessage'),
    fn('selectedMiningControlBatchItems'),
    fn('syncMiningControlBatchSelection'),
    fn('submitMiningControlBatch'),
  ].join('\n'), context);
  return {context, state, effects, errorElement, resultsElement, button};
}

test('submit posts the explicit selection to the batch endpoint and renders per-device results', async () => {
  const harness = submitHarness({apiResponse: {accepted_count: 2, rejected_count: 0, accepted: [{agent_id: 'a1'}], rejected: []}});
  await harness.context.submitMiningControlBatch();
  assert.ok(harness.effects.api, 'the batch endpoint was called');
  assert.equal(harness.effects.api.url, '/api/agents/commands/batch');
  assert.equal(harness.effects.api.options.method, 'POST');
  assert.equal(harness.effects.api.options.body.action, 'stop_mining');
  assert.deepEqual(Array.from(harness.effects.api.options.body.agent_ids), ['a1', 'a2']);
  assert.deepEqual(Array.from(harness.effects.rendered.selected.map((item) => item.id)), ['a1', 'a2']);
  assert.deepEqual(Array.from(harness.effects.rendered.idKeys), ['agent_id', 'id']);
  assert.match(harness.effects.toasts[0], /批量停止挖矿已逐台处理：2 台进入队列，0 台被拒绝/);
  assert.equal(harness.effects.refreshed, 1);
  const message = harness.effects.confirmMessages[0];
  assert.match(message, /确认对以下 2 台设备执行批量停止挖矿？/);
  assert.match(message, /本次不会执行的设备（1 台）：/);
  assert.match(message, /Rig Off（b1）：设备离线，无法下发矿工控制任务/);
});

test('submit is blocked by confirmation, viewer role and API errors without side effects', async () => {
  const declined = submitHarness({confirmOk: false});
  await declined.context.submitMiningControlBatch();
  assert.equal(declined.effects.api, null);
  assert.equal(declined.effects.toasts.length, 0);

  const viewer = submitHarness({role: 'viewer'});
  await viewer.context.submitMiningControlBatch();
  assert.equal(viewer.effects.api, null);
  assert.equal(viewer.effects.confirmMessages.length, 0);

  const failed = submitHarness({apiError: new Error('控制端暂不可用')});
  await failed.context.submitMiningControlBatch();
  assert.equal(failed.errorElement.textContent, '控制端暂不可用');
  assert.equal(failed.effects.toasts.length, 0);
});
