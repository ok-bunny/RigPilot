'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const test = require('node:test');
const vm = require('node:vm');
const source = process.env.RUNTIME_CANCEL_SOURCE
  ? fs.readFileSync(process.env.RUNTIME_CANCEL_SOURCE, 'utf8') : readStatic();
const fn = functionLoader(source);

function fixture({role = 'admin', agentStatus = 'online', deviceTab = 'logs', drawerOpen = true} = {}) {
  const state = {
    user: {username: 'admin', role},
    deviceLog: null,
    deviceDetailTab: deviceTab,
    selectedAgent: {id: 'A', name: 'Rig A', status: agentStatus, agent_version: '0.8.56'},
    token: 'token',
  };
  const effects = {api: [], notices: [], confirmations: [], deviceReloads: 0, refreshes: 0};
  const windowShim = {confirm(question) { effects.confirmations.push(question); return true; }};
  const context = vm.createContext({
    state,
    window: windowShim,
    Date,
    console,
    escapeHtml: undefined,
    api: async (path, options) => {
      effects.api.push({path, options});
      return {status: 'cancelled'};
    },
    toast: message => effects.notices.push(message),
    loadDeviceActivity: async () => {
      effects.deviceReloads++;
    },
    refreshAll: async () => {
      effects.refreshes++;
      return true;
    },
    formatTime: value => String(value || ''),
    commandErrorMessage: () => '',
    commandStatus: status => ({queued: '等待设备', dispatched: '已送达', running: '执行中',
      succeeded: '成功', failed: '失败', cancelled: '已取消', expired: '已过期'}[status] || status),
    $: () => ({classList: {contains: () => drawerOpen}}),
  });
  const constants = ['actionNames', 'RUNTIME_CANCELLABLE_ACTIONS', 'auditNames']
    .map(name => source.match(new RegExp(`^const ${name} = (?:[^\\n]+;|\\{[\\s\\S]*?^\\};)`, 'm'))?.[0] || '');
  const names = ['escapeHtml', 'deviceActivityMarkup', 'controlRemoteCommand'];
  vm.runInContext([...constants, ...names.map(name => fn(name))].join('\n'), context);
  return {context, state, effects};
}

function command(status, action = 'apply_gpu_profile') {
  return {
    id: `cmd-${status}`, agent_id: 'A', action, status,
    payload: {}, created_by: 'admin',
    created_at: new Date(Date.now() - 5000).toISOString(),
    finished_at: ['succeeded', 'failed', 'cancelled', 'expired'].includes(status)
      ? new Date().toISOString() : null,
    result: {}, error: null,
  };
}

function attach(h, commands, {error = '', loading = false} = {}) {
  h.state.deviceLog = {agentId: 'A', commands, audit: [], error, loading, fetchedAt: Date.now()};
  return h.context.deviceActivityMarkup(h.state.selectedAgent);
}

test('running GPU OC renders a cancel button carrying the in-flight state', () => {
  for (const role of ['admin', 'operator']) {
    const h = fixture({role});
    const markup = attach(h, [command('running')]);
    assert.match(markup, new RegExp(`data-device-command-operation="cancel"[^>]*data-command-id="cmd-running"[^>]*data-command-state="running"`), role);
    assert.match(markup, /应用 GPU OC 配置/);
    assert.match(markup, /执行中/);
  }
});

test('queued GPU OC keeps the legacy cancel affordance with its queued state', () => {
  const h = fixture();
  const markup = attach(h, [command('queued')]);
  assert.match(markup, /data-device-command-operation="cancel"[^>]*data-command-state="queued"/);
});

test('other in-flight actions and viewers get no cancel button', () => {
  const h = fixture();
  for (const status of ['dispatched', 'running']) {
    const markup = attach(h, [command(status, 'collect_diagnostics')]);
    assert.doesNotMatch(markup, /data-device-command-operation/, `${status} operator`);
    assert.doesNotMatch(markup, /结束遗留/, 'online device never offers abandon');
  }
  const viewer = fixture({role: 'viewer'});
  assert.doesNotMatch(attach(viewer, [command('running')]), /data-device-command-operation/);
  assert.doesNotMatch(attach(viewer, [command('queued')]), /data-device-command-operation/);
});

test('offline admin abandons non-cancellable rows but still cancels running GPU OC', () => {
  const h = fixture({agentStatus: 'offline'});
  const markup = attach(h, [command('running', 'collect_diagnostics')]);
  assert.match(markup, /data-device-command-operation="abandon"/);
  const ocMarkup = attach(fixture({agentStatus: 'offline'}), [command('running')]);
  assert.match(ocMarkup, /data-device-command-operation="cancel"/);
  assert.doesNotMatch(ocMarkup, /data-device-command-operation="abandon"/);
});

test('cancelled history rows stay readable without controls', () => {
  const h = fixture();
  const markup = attach(h, [command('cancelled')]);
  assert.match(markup, /command-status cancelled[^>]*>已取消</);
  assert.doesNotMatch(markup, /data-device-command-operation/);
});

test('runtime cancel confirmation states the server-side truth honestly', async () => {
  const h = fixture();
  attach(h, [command('running')]);
  await h.context.controlRemoteCommand(
    'cmd-running', 'cancel', '应用 GPU OC 配置',
    {disabled: false, dataset: {commandState: 'running'}},
  );
  assert.equal(h.effects.confirmations.length, 1);
  assert.match(h.effects.confirmations[0], /确认取消执行中的“应用 GPU OC 配置”任务/);
  assert.match(h.effects.confirmations[0], /取消将停止服务端任务并释放操作通道/);
  assert.match(h.effects.confirmations[0], /若设备端已卡死（如 GPU 驱动无响应），可能仍需重启设备恢复/);
  assert.equal(h.effects.api.length, 1);
  assert.equal(h.effects.api[0].path, '/api/commands/cmd-running/cancel');
  assert.equal(h.effects.api[0].options.method, 'POST');
  assert.match(h.effects.notices[0], /执行中任务已取消（服务端）/);
  assert.equal(h.effects.deviceReloads, 1);
  assert.equal(h.effects.refreshes, 1);
});

test('runtime cancel aborts quietly when the operator declines', async () => {
  const h = fixture();
  h.context.window.confirm = () => false;
  await h.context.controlRemoteCommand(
    'cmd-running', 'cancel', '应用 GPU OC 配置',
    {disabled: false, dataset: {commandState: 'running'}},
  );
  assert.equal(h.effects.api.length, 0);
  assert.equal(h.effects.notices.length, 0);
});

test('queued cancel keeps its original copy and still refreshes the device log', async () => {
  const h = fixture();
  await h.context.controlRemoteCommand(
    'cmd-queued', 'cancel', '应用 GPU OC 配置',
    {disabled: false, dataset: {commandState: 'queued'}},
  );
  assert.match(h.effects.confirmations[0], /尚未被设备领取/);
  assert.doesNotMatch(h.effects.confirmations[0], /释放操作通道/);
  assert.equal(h.effects.api[0].path, '/api/commands/cmd-queued/cancel');
  assert.match(h.effects.notices[0], /等待任务已取消/);
  assert.equal(h.effects.deviceReloads, 1);
});

test('abandon keeps its offline-recovery copy', async () => {
  const h = fixture();
  await h.context.controlRemoteCommand(
    'cmd-running', 'abandon', '采集诊断信息',
    {disabled: false, dataset: {commandState: 'running'}},
  );
  assert.match(h.effects.confirmations[0], /离线遗留记录/);
  assert.equal(h.effects.api[0].path, '/api/commands/cmd-running/abandon');
  assert.equal(h.effects.deviceReloads, 1);
});
