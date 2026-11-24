'use strict';
const assert = require('node:assert/strict');
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const test = require('node:test');
const vm = require('node:vm');
const source = process.env.AGENT_UPDATE_STALL_SOURCE
  ? require('node:fs').readFileSync(process.env.AGENT_UPDATE_STALL_SOURCE, 'utf8') : readStatic();
const fn = functionLoader(source);

const rig = (id = 'A') => ({
  id, name: `Rig ${id}`, agent_version: '0.8.43', status: 'online',
  os_family: 'windows', inventory: {architecture: 'AMD64'}, capabilities: ['agent_update.v1'],
});
const job = (commandId = 'cmd-A', agentId = 'A', status = 'activating', extra = {}) => ({
  command_id: commandId, agent_id: agentId, status, command_status: status,
  target_version: '0.8.44', source_version: '0.8.43', agent_name: `Rig ${agentId}`,
  created_at: new Date(Date.now() - 60_000).toISOString(),
  updated_at: new Date(Date.now() - 30_000).toISOString(),
  completed_at: null, stalled_at: null, target_heartbeat_at: null,
  file_name: 'synthetic-agent.zip', size: 1, created_by: 'admin', error: '',
  activate_remote_workspace: 0, workspace_activation_status: 'not_requested',
  downloaded_at: new Date().toISOString(), ...extra,
});

function fixture({jobs, agents, role = 'admin', eligible = true} = {}) {
  const state = {
    user: {username: 'admin', role}, authEpoch: 1,
    agents: agents || [rig()], agentUpdates: jobs || [job()], agentUpdateRequests: {},
    agentUpdateDismissals: {}, agentReleases: [], commands: [],
  };
  const effects = {dialogs: []};
  const body = {
    innerHTML: '',
    closest: () => ({}),
  };
  const context = vm.createContext({
    state, Date, Number, JSON,
    localStorage: {getItem: () => null, setItem() {}},
    agentBootstrapLabel: () => '', agentGpuDriverLabel: () => '',
    latestCompatibleAgentRelease: agent => (eligible
      ? {id: 'rel-1', version: '0.8.44', deployable: true, update_protocol: 'agent_update.v1',
        platform: 'windows', architecture: 'x86_64'}
      : null),
    agentUpdateEligibility: () => ({eligible, reason: eligible ? '可以更新' : '设备不在线'}),
    openAgentUpdateDialog(releaseId, agentId) { effects.dialogs.push({releaseId, agentId}); },
    cancelAgentUpdate() { throw new Error('cancel not exercised here'); },
    formatTime: value => String(value || ''), formatBytes: value => `${value} B`,
    setTableVisible() {},
    $: selector => selector === '#agentUpdateTableBody'
      ? body
      : {classList: {toggle() {}}},
    $$: () => [],
    document: {},
  });
  const constants = ['agentUpdateStatusLabels',
    'auditNames', 'actionNames']
    .map(name => source.match(new RegExp(`^const ${name} = (?:[^\\n]+;|\\{[\\s\\S]*?^\\};)`, 'm'))?.[0] || '')
    .filter(Boolean);
  const names = ['escapeHtml', 'agentUpdateProgress', 'agentUpdateStatusMarkup',
    'agentUpdateIsTerminal', 'agentUpdateDismissalSignature', 'dismissedAgentUpdates',
    'agentUpdateIsDismissed', 'agentUpdateRetryTarget', 'renderAgentUpdates'];
  vm.runInContext(
    [...constants, ...names.map(fn), 'globalThis.labelMaps = {auditNames, actionNames};'].join('\n'),
    context,
  );
  return {
    context, state, effects, body,
    markup: (agent = state.agents[0]) => context.agentUpdateStatusMarkup(agent),
    history: () => {
      context.renderAgentUpdates();
      return body.innerHTML;
    },
  };
}

test('activating badge distinguishes a stalled replacement from a normal wait', () => {
  const stalled = fixture({jobs: [job('cmd-A', 'A', 'activating', {
    stalled_at: new Date().toISOString(),
    target_heartbeat_count: 2,
    target_heartbeat_at: new Date(Date.now() - 6 * 60_000).toISOString(),
  })]});
  const markup = stalled.markup();
  assert.match(markup, /升级疑似卡滞（新版已回滚或未运行）/);
  assert.match(markup, /agent-update-state failed/);
  assert.doesNotMatch(markup, /升级并回连中/);

  const waiting = fixture({jobs: [job('cmd-A', 'A', 'activating', {
    target_heartbeat_count: 2,
    target_heartbeat_at: new Date().toISOString(),
  })]});
  const normal = waiting.markup();
  assert.match(normal, /升级并回连中/);
  assert.match(normal, /agent-update-state progress/);
  assert.doesNotMatch(normal, /卡滞/);
});

test('failed badge surfaces the reported failure reason inline', () => {
  const failed = fixture({jobs: [job('cmd-A', 'A', 'failed', {
    completed_at: new Date().toISOString(),
    error: 'insufficient disk space: need 512MiB',
  })]});
  const markup = failed.markup();
  assert.match(markup, /更新失败/);
  assert.match(markup, /<small> · insufficient disk space: need 512MiB<\/small>/);
  const truncated = fixture({jobs: [job('cmd-A', 'A', 'failed', {
    completed_at: new Date().toISOString(),
    error: 'x'.repeat(120),
  })]});
  const short = truncated.markup();
  assert.match(short, /<small>[^<]*…<\/small>/);
  const visible = /<small>([^<]*)<\/small>/.exec(short)[1];
  assert.ok(visible.length <= 61, 'inline reason stays a short fragment');
  assert.ok(!visible.includes('x'.repeat(61)), 'inline reason is truncated');
  // The tooltip still carries the complete reported reason.
  assert.ok(short.includes('x'.repeat(120)));
});

test('update history marks stalled activations and offers informed retry', () => {
  const stalled = fixture({jobs: [job('cmd-A', 'A', 'activating', {
    stalled_at: new Date().toISOString(), target_heartbeat_count: 2,
  })]});
  const stalledHtml = stalled.history();
  assert.match(stalledHtml, /正在替换并回连 · 疑似卡滞/);
  assert.match(stalledHtml, /已回滚或未运行/);
  assert.doesNotMatch(stalledHtml, /data-agent-update-release/);

  const failed = fixture({jobs: [job('cmd-A', 'A', 'failed', {
    completed_at: new Date().toISOString(),
    error: 'insufficient disk space: need 512MiB',
  })]});
  const failedHtml = failed.history();
  assert.match(failedHtml, /insufficient disk space: need 512MiB/);
  assert.match(failedHtml, /data-agent-update-release="rel-1"/);
  assert.match(failedHtml, /data-agent-update-agent="A"/);
  assert.match(failedHtml, /重试更新/);
});

test('retry button is withheld when the device is not eligible right now', () => {
  const offline = fixture({eligible: false, jobs: [job('cmd-A', 'A', 'failed', {
    completed_at: new Date().toISOString(),
  })]});
  const offlineHtml = offline.history();
  assert.doesNotMatch(offlineHtml, /data-agent-update-release/);
  assert.doesNotMatch(offlineHtml, /重试更新/);

  const viewer = fixture({role: 'viewer', jobs: [job('cmd-A', 'A', 'failed', {
    completed_at: new Date().toISOString(),
  })]});
  assert.doesNotMatch(viewer.history(), /重试更新/);

  const succeeded = fixture({jobs: [job('cmd-A', 'A', 'succeeded', {
    completed_at: new Date().toISOString(),
  })]});
  assert.doesNotMatch(succeeded.history(), /重试更新/);
});

test('audit and command labels cover the new controller actions', () => {
  const labels = fixture();
  assert.equal(labels.context.labelMaps.auditNames.agent_update_replacement_stalled, '被控端更新疑似卡滞');
  assert.equal(labels.context.labelMaps.auditNames.agent_update_forensics_queued, '排队更新失败取证');
  assert.equal(labels.context.labelMaps.actionNames['collect_update_forensics.v1'], '更新失败取证');
});
