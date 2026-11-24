'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const test = require('node:test');
const vm = require('node:vm');
const source = process.env.AGENT_UPDATE_DISMISS_SOURCE
  ? fs.readFileSync(process.env.AGENT_UPDATE_DISMISS_SOURCE, 'utf8') : readStatic();
const fn = functionLoader(source);

const rig = (id = 'A') => ({id, name: `Rig ${id}`, agent_version: '0.8.25', status: 'online'});
const job = (commandId = 'cmd-A', agentId = 'A', status = 'succeeded', extra = {}) => ({
  command_id: commandId, agent_id: agentId, status, command_status: 'succeeded',
  target_version: '0.8.25', source_version: '0.8.24', agent_name: `Rig ${agentId}`,
  created_at: new Date(Date.now() - 1000).toISOString(), updated_at: new Date().toISOString(),
  completed_at: status === 'succeeded' ? new Date().toISOString() : null,
  file_name: 'synthetic-agent.tar.gz', size: 1, created_by: 'admin', error: '', ...extra,
});

function fixture({username = 'admin', storage = new Map(), readFails = false, writeFails = false} = {}) {
  const state = {user: {username, role: 'admin'}, authEpoch: 1,
    agents: [rig(), rig('B')], agentUpdates: [job()], agentUpdateRequests: {},
    agentUpdateDismissals: {}, configurationRefreshDismissals: {}, selectedAgent: null, agentReleases: [], commands: []};
  const effects = {api: 0, devices: 0, drawers: [], opened: [], writes: [], notices: []};
  const history = {innerHTML: '', closest: () => ({})};
  const clearButton = {disabled: true, textContent: '', setAttribute(name, value) { this[name] = value; }};
  const context = vm.createContext({state, Date, Number,
    localStorage: {
      getItem(key) { if (readFails) throw new Error('storage read blocked'); return storage.get(key) || null; },
      setItem(key, value) {
        effects.writes.push({key, value});
        if (writeFails) throw new Error('storage write blocked');
        storage.set(key, value);
      },
    },
    api() { effects.api++; throw new Error('badge dismissal must never request an API'); },
    renderDevices() { effects.devices++; }, renderDrawer(agent) { effects.drawers.push(agent.id); },
    openAgent(id) { effects.opened.push(id); }, toast(message) { effects.notices.push(message); },
    agentBootstrapLabel: () => '', agentGpuDriverLabel: () => '', latestCompatibleAgentRelease: () => null,
    agentUpdateRetryTarget: () => null,
    formatTime: value => String(value || ''), formatBytes: value => `${value} B`, setTableVisible() {},
    $: selector => selector === '#agentUpdateTableBody' ? history
      : selector === '#clearConfigurationRefreshBadgesButton' ? clearButton : {classList: {toggle() {}}},
    $$: (_selector, root) => root?.buttons || [],
    document: {},
  });
  // Only define actual selected functions; no bootstrap or API runs. The
  // shared loader includes agentUpdateDismissKey after agentUpdateProgress.
  const constants = ['configurationRefreshDismissKey', 'agentUpdateStatusLabels']
    .map(name => source.match(new RegExp(`^const ${name} = (?:[^\\n]+;|\\{[\\s\\S]*?^\\};)`, 'm'))?.[0] || '');
  const names = ['escapeHtml', 'agentUpdateProgress', 'agentUpdateStatusMarkup', 'agentVersionCellMarkup',
    'agentUpdateIsTerminal', 'agentUpdateDismissalSignature', 'dismissedAgentUpdates',
    'persistAgentUpdateDismissals', 'agentUpdateIsDismissed', 'dismissAgentUpdate',
    'bindAgentUpdateDismiss', 'bindAgentRows', 'renderAgentUpdates', 'dismissibleAgentUpdates',
    'configurationRefreshSignature', 'dismissedConfigurationRefreshes', 'persistConfigurationRefreshDismissals',
    'configurationRefreshIsDismissed', 'dismissibleConfigurationRefreshAgents',
    'syncConfigurationRefreshClearButton', 'dismissAllConfigurationRefreshes'];
  vm.runInContext([...constants, ...names.map(fn)].join('\n'), context);
  return {context, state, effects, storage, history, clearButton,
    markup: (agent = state.agents[0]) => context.agentUpdateStatusMarkup(agent),
    progress: (agent = state.agents[0]) => context.agentUpdateProgress(agent),
    dismiss: (agentId = 'A', commandId = 'cmd-A') => context.dismissAgentUpdate(agentId, commandId)};
}

test('completed Agent025 renders an accessible close button and escapes diagnostic content', () => {
  const h = fixture();
  h.state.agentUpdates[0].error = '<script>synthetic</script>';
  const markup = h.markup();
  assert.match(markup, /更新成功/);
  assert.match(markup, /0\.8\.25/);
  assert.match(markup, /<button\b[^>]*type="button"[^>]*data-dismiss-agent-update="A"/);
  assert.match(markup, /data-dismiss-agent-update-command="cmd-A"/);
  assert.match(markup, /<button\b[^>]*aria-label="[^"]+"[^>]*>×<\/button>/);
  assert.doesNotMatch(markup, /<script>/);
  assert.match(markup, /&lt;script&gt;/);
});

test('existing progress keeps active jobs, terminal age limits and pending auth isolation', () => {
  const h = fixture(), old = new Date(Date.now() - 25 * 60 * 60_000).toISOString();
  h.state.agentUpdates = [job('cmd-old', 'A', 'succeeded', {completed_at: old, updated_at: old, created_at: old})];
  assert.equal(h.progress(), null); assert.equal(h.markup(), '');
  h.state.agentUpdates = [job('cmd-active', 'A', 'activating', {completed_at: null, updated_at: old, created_at: old})];
  assert.equal(h.progress().commandId, 'cmd-active');
  assert.notEqual(h.markup(), '');
  assert.doesNotMatch(h.markup(), /data-dismiss-agent-update=/);
  h.state.agentUpdateRequests.A = {status: 'requesting', authEpoch: 0, startedAt: Date.now(), targetVersion: '0.8.26'};
  assert.equal(h.progress().commandId, 'cmd-active');
  assert.equal(h.state.agentUpdateRequests.A, undefined);
  h.state.user.role = 'viewer';
  assert.equal(h.progress(), null); assert.equal(h.markup(), '');
});

test('only real terminal job statuses get close controls; command succeeded is insufficient', () => {
  const h = fixture();
  for (const status of ['succeeded', 'failed', 'cancelled', 'expired']) {
    h.state.agentUpdates = [job('cmd-A', 'A', status)];
    assert.equal(h.context.agentUpdateIsTerminal(h.progress()), true, status);
    assert.match(h.markup(), /data-dismiss-agent-update=/, status);
  }
  for (const status of ['queued', 'dispatched', 'activating', 'unknown']) {
    h.state.agentUpdates = [job('cmd-A', 'A', status, {command_status: 'succeeded'})];
    assert.equal(h.context.agentUpdateIsTerminal(h.progress()), false, status);
    assert.doesNotMatch(h.markup(), /data-dismiss-agent-update=/, status);
    assert.notEqual(h.markup(), '', status);
  }
  h.state.agentUpdates = [job('', 'A')];
  assert.equal(h.context.agentUpdateIsTerminal(h.progress()), false);
  assert.doesNotMatch(h.markup(), /data-dismiss-agent-update=/);
  for (const status of ['requesting', 'queued', 'request_failed']) {
    h.state.agentUpdates = [];
    h.state.agentUpdateRequests.A = {status, commandId: null, authEpoch: 1, startedAt: Date.now(), targetVersion: '0.8.25'};
    assert.doesNotMatch(h.markup(), /data-dismiss-agent-update=/, status);
    h.dismiss('A', 'cmd-A');
    assert.equal(h.effects.writes.length, 0, status);
  }
  h.state.agentUpdateRequests.A = {status: 'requesting', commandId: null, authEpoch: 1,
    startedAt: Date.now() - 61_000, targetVersion: '0.8.25'};
  assert.equal(h.progress().status, 'verifying');
  assert.doesNotMatch(h.markup(), /data-dismiss-agent-update=/);
});

test('dismiss saves identity metadata, hides the badge and retains real history without API effects', () => {
  const h = fixture(), jobs = h.state.agentUpdates;
  const original = JSON.stringify(jobs);
  h.state.agentUpdates[0].error = 'error-must-not-be-stored';
  const withError = JSON.stringify(jobs);
  h.state.selectedAgent = h.state.agents[0];
  h.dismiss();
  assert.equal(h.markup(), '');
  assert.equal(h.state.agentUpdates, jobs);
  assert.equal(JSON.stringify(jobs), withError);
  assert.notEqual(withError, original);
  assert.equal(h.effects.api, 0);
  assert.equal(h.effects.devices, 1);
  assert.deepEqual(h.effects.drawers, ['A']);
  assert.equal(h.effects.writes.length, 1);
  assert.doesNotMatch(h.effects.writes[0].value, /error-must-not-be-stored|synthetic-agent\.tar\.gz/);
  h.context.renderAgentUpdates();
  // The history renderer still receives the original complete jobs; its close
  // preference is deliberately unrelated to command cancellation/cleanup.
  assert.equal(h.state.agentUpdates.length, 1);
  assert.match(h.history.innerHTML, /Rig A/);
  assert.match(h.history.innerHTML, /error-must-not-be-stored/);
  assert.match(h.history.innerHTML, /synthetic-agent\.tar\.gz/);
});

test('same terminal command stays dismissed after timestamp refresh and VM reload for the same user', () => {
  const storage = new Map(), h = fixture({storage});
  h.dismiss();
  h.state.agentUpdates = [job('cmd-A', 'A', 'succeeded', {updated_at: new Date(Date.now() + 1000).toISOString()})];
  assert.equal(h.markup(), '');
  const reloaded = fixture({storage});
  assert.equal(reloaded.markup(), '');
  assert.equal(reloaded.effects.api, 0);
});

test('new commands, changed terminal outcome and other devices/users remain visible', () => {
  const storage = new Map(), h = fixture({storage});
  h.dismiss();
  h.state.agentUpdates = [job('cmd-B', 'A')];
  assert.notEqual(h.markup(), '');
  h.state.agentUpdates = [job('cmd-A', 'A', 'failed')];
  assert.notEqual(h.markup(), '');
  h.state.agentUpdates = [job('cmd-A', 'B')];
  assert.notEqual(h.markup(h.state.agents[1]), '');
  const otherUser = fixture({storage, username: 'second-admin'});
  assert.notEqual(otherUser.markup(), '');
  assert.equal(fixture({storage}).markup(), '');
});

test('dismissed metadata never hides a reactivated job or changes active version protection', () => {
  const h = fixture(); h.dismiss();
  for (const status of ['queued', 'dispatched', 'activating']) {
    h.state.agentUpdates = [job('cmd-A', 'A', status)];
    assert.notEqual(h.markup(), '');
    assert.doesNotMatch(h.markup(), /data-dismiss-agent-update=/);
    assert.match(h.context.agentVersionCellMarkup(h.state.agents[0]), /目标 Agent 0\.8\.25/);
    const writes = h.effects.writes.length;
    h.dismiss();
    assert.equal(h.effects.writes.length, writes);
  }
});

test('stale close identities, missing devices/commands and nonadmins cannot dismiss current work', () => {
  const h = fixture();
  h.state.agentUpdates = [job('cmd-B', 'A')];
  h.dismiss('A', 'cmd-A'); h.dismiss('unknown', 'cmd-B'); h.dismiss('A', '');
  assert.equal(h.effects.writes.length, 0);
  assert.notEqual(h.markup(), '');
  for (const role of ['viewer', 'operator']) {
    h.state.user.role = role; h.dismiss('A', 'cmd-B');
    assert.equal(h.effects.writes.length, 0);
    assert.equal(h.markup(), '');
  }
  assert.equal(h.effects.api, 0);
});

test('corrupt stored shapes and blocked storage preserve safe rendering and memory fallback', () => {
  for (const broken of ['{bad-json', 'null', '[]', '"text"', '42']) {
    const storage = new Map(), seed = fixture({storage}); seed.dismiss();
    for (const key of storage.keys()) storage.set(key, broken);
    const h = fixture({storage});
    assert.notEqual(h.markup(), '', broken);
    h.dismiss(); assert.equal(h.markup(), '', broken);
  }
  for (const flags of [{readFails: true}, {writeFails: true}, {readFails: true, writeFails: true}]) {
    const h = fixture(flags);
    assert.notEqual(h.markup(), '');
    h.dismiss(); assert.equal(h.markup(), '');
    assert.equal(h.effects.api, 0);
  }
});

test('real close binding prevents row click propagation and preserves captured command identity', () => {
  const h = fixture(), listeners = {};
  const button = {dataset: {dismissAgentUpdate: 'A', dismissAgentUpdateCommand: 'cmd-A'},
    addEventListener(name, callback) { listeners[name] = callback; }};
  h.context.bindAgentUpdateDismiss({buttons: [button]});
  assert.equal(typeof listeners.click, 'function');
  let prevented = 0, stopped = 0;
  listeners.click({preventDefault() { prevented++; }, stopPropagation() { stopped++; }});
  assert.equal(prevented, 1); assert.equal(stopped, 1);
  assert.equal(h.markup(), ''); assert.equal(h.effects.api, 0);
  const stale = fixture();
  stale.context.bindAgentUpdateDismiss({buttons: [button]});
  stale.state.agentUpdates = [job('cmd-B')];
  listeners.click({preventDefault() {}, stopPropagation() {}});
  assert.equal(stale.effects.writes.length, 0);
});

test('row Enter/Space navigation leaves nested interactive controls native default actions intact', () => {
  const h = fixture(), listeners = {}, attributes = {};
  const row = {dataset: {agentId: 'A'}, querySelector: () => ({textContent: 'Rig A'}),
    setAttribute(name, value) { attributes[name] = value; },
    addEventListener(name, callback) { listeners[name] = callback; }};
  h.context.bindAgentRows([row]);
  assert.equal(row.tabIndex, 0); assert.match(attributes['aria-label'], /Rig A/);
  for (const key of ['Enter', ' ']) {
    let prevented = 0; const openedBefore = h.effects.opened.length;
    listeners.keydown({key, target: {closest: () => ({tagName: 'BUTTON'})}, preventDefault() { prevented++; }});
    assert.equal(prevented, 0, `button ${key} must keep its native click default`);
    assert.equal(h.effects.opened.length, openedBefore);
    listeners.keydown({key, target: {closest: () => null}, preventDefault() { prevented++; }});
    assert.equal(prevented, 1);
  }
  assert.deepEqual(h.effects.opened, ['A', 'A']);
});

test('bulk clear combines terminal Agent and successful configuration badges as local metadata only', () => {
  const h = fixture();
  h.state.agents.push(rig('C'), rig('D'));
  h.state.agents[0].configuration_refresh = {status: 'succeeded', command_id: 'config-A', desired_fingerprint: 'fingerprint-A'};
  h.state.agents[1].configuration_refresh = {status: 'pending', command_id: 'config-B'};
  h.state.agents[2].configuration_refresh = {status: 'failed', command_id: 'config-C'};
  h.state.agentUpdates = [job('cmd-A', 'A'), job('cmd-B', 'B', 'activating'),
    job('cmd-C', 'C', 'failed'), job('cmd-D', 'D', 'cancelled')];
  const before = JSON.stringify({agents: h.state.agents, jobs: h.state.agentUpdates});
  h.state.selectedAgent = h.state.agents[0];
  h.context.syncConfigurationRefreshClearButton();
  assert.equal(h.clearButton.disabled, false);
  assert.match(h.clearButton.textContent, /\(4\)/);
  assert.match(h.clearButton['aria-label'], /4/);
  h.context.dismissAllConfigurationRefreshes();
  assert.equal(h.markup(), '');
  assert.notEqual(h.markup(h.state.agents[1]), '');
  assert.equal(h.markup(h.state.agents[2]), '');
  assert.equal(h.markup(h.state.agents[3]), '');
  assert.equal(h.context.configurationRefreshIsDismissed(h.state.agents[0]), true);
  assert.equal(h.context.configurationRefreshIsDismissed(h.state.agents[1]), false);
  assert.equal(h.context.configurationRefreshIsDismissed(h.state.agents[2]), false);
  assert.equal(JSON.stringify({agents: h.state.agents, jobs: h.state.agentUpdates}), before);
  assert.equal(h.effects.api, 0); assert.equal(h.effects.devices, 1);
  assert.deepEqual(h.effects.drawers, ['A']);
  assert.equal(h.effects.notices.length, 1); assert.match(h.effects.notices[0], /4/);
  h.context.syncConfigurationRefreshClearButton();
  assert.equal(h.clearButton.disabled, true);
  const writes = h.effects.writes.length;
  h.context.dismissAllConfigurationRefreshes();
  assert.equal(h.effects.writes.length, writes);
  assert.equal(h.effects.devices, 1);
});
