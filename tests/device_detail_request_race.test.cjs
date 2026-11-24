'use strict';
const {deferred, functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();

const functionSource = functionLoader(source);

function fixture(name) {
  const {promise, resolve, reject} = deferred();
  const state = { authEpoch: 1, user: { role: 'admin' }, selectedAgent: { id: 'a', name: 'A', agent_version: '0.8.29' }, agents: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }] };
  const button = { disabled: false };
  const calls = [];
  const context = vm.createContext({
    state,
    $: (selector) => selector === '#agentDisplayName' ? { value: 'Renamed A' } : button,
    api: () => promise,
    refreshSelectedAgentDrawer: () => calls.push('drawer'),
    renderDrawer: () => calls.push('drawer'), agentVersionAtLeast: () => true, refreshAll: async () => {},
    renderDevices: () => calls.push('devices'),
    toast: () => calls.push('toast'),
  });
  vm.runInContext(functionSource(name), context);
  return { state, button, calls, resolve, reject, run: () => context[name]({ preventDefault() {}, currentTarget: {dataset: {miningAutostartEnabled: 'true'}} }) };
}

for (const name of ['refreshAgentStatus', 'saveAgentDetails', 'configureMiningAutostart']) {
  for (const transition of ['select another device', 'close drawer', 'change login']) {
    test(`${name}: ignores response after ${transition}`, async () => {
      const f = fixture(name);
      const pending = f.run();
      if (transition === 'select another device') f.state.selectedAgent = f.state.agents[1];
      else if (transition === 'close drawer') f.state.selectedAgent = null;
      else { f.state.authEpoch += 1; f.state.agents = []; f.state.selectedAgent = null; }
      const expected = f.state.selectedAgent;
      f.resolve({ id: 'a', name: 'Server response A' });
      await pending;
      assert.equal(f.state.selectedAgent, expected);
      assert.deepEqual(f.calls, []);
      if (transition === 'change login') assert.deepEqual(f.state.agents, []);
    });
  }
  test(`${name}: applies a response to the current device`, async () => {
    const f = fixture(name);
    const pending = f.run();
    const response = { id: 'a', name: 'Server response A' };
    f.resolve(response);
    await pending;
    assert.equal(f.state.selectedAgent, response);
    assert.equal(f.state.agents[0], response);
    assert.ok(f.calls.includes('drawer'));
  });
  test(`${name}: ignores an obsolete error after changing login`, async () => {
    const f = fixture(name);
    const pending = f.run();
    f.state.authEpoch += 1;
    f.reject(new Error('Obsolete request failed'));
    await pending;
    assert.deepEqual(f.calls, []);
  });
}

test('opening a device ignores earlier telemetry after reopening, newer refresh, close, or login change', async () => {
  for (const transition of ['reopen', 'refresh', 'close', 'login', 'current']) {
    for (const failure of [false, true]) {
      const requests = [], draws = [], errors = [];
      const state = {authEpoch: 1, agents: [{id: 'a', name: 'A', metrics: {}}, {id: 'b', name: 'B', metrics: {}}],
        deviceTelemetryRequestId: 0, deviceLogRequestId: 0, deviceHashrateRequestId: 0, minerLogPollToken: 0, nvidiaDriverPollToken: 0};
      const node = {open: false, classList: {add() {}, remove() {}}, setAttribute() {}};
      const context = vm.createContext({state, $: () => node, gpuMetricUuid: () => '', renderDrawer() {},
        api() { const request = deferred(); requests.push(request); return request.promise; },
        drawTelemetry: data => draws.push(data), toast: error => errors.push(error)});
      vm.runInContext(functionSource('openAgent'), context);
      const old = context.openAgent('a');
      if (transition === 'reopen') {
        const other = context.openAgent('b'), current = context.openAgent('a');
        requests[1].resolve([]); requests[2].resolve([{marker: 'current'}]);
        await Promise.all([other, current]);
      } else if (transition === 'refresh') {
        state.deviceTelemetryRequestId++; state.selectedAgentTelemetry = [{marker: 'refresh'}];
      } else if (transition === 'close') state.selectedAgent = null;
      else if (transition === 'login') state.authEpoch++;
      const before = state.selectedAgentTelemetry;
      if (failure) requests[0].reject(new Error('old error')); else requests[0].resolve([{marker: 'old'}]);
      await old;
      if (transition !== 'current') {
        assert.equal(state.selectedAgentTelemetry, before, transition);
        assert.deepEqual(errors, []);
        assert.equal(draws.length, transition === 'reopen' ? 1 : 0);
      } else {
        assert.equal(failure ? errors[0] : draws[0][0].marker, failure ? 'old error' : 'old');
      }
    }
  }
});

test('a pending dashboard refresh cannot overwrite telemetry from a later drawer opening', async () => {
  const agent = {id: 'a', name: 'A', metrics: {}};
  const state = {authEpoch: 1, token: 'synthetic', user: {role: 'admin'}, agents: [agent], selectedAgent: agent,
    refreshId: 0, refreshing: false, deviceTelemetryRequestId: 0, deviceLogRequestId: 0,
    deviceHashrateRequestId: 0, minerLogPollToken: 0, nvidiaDriverPollToken: 0};
  const requests = [], node = {open: false, classList: {add() {}, remove() {}}, setAttribute() {}, removeAttribute() {}};
  const context = vm.createContext({state, AbortController, console, $: () => node,
    WORKLOAD_SETTLEMENT_NOTE_PATH: '/settlement', gpuMetricUuid: () => '',
    renderDrawer() {}, renderAll() {}, refreshSelectedAgentDrawer() {}, drawTelemetry() {}, workloadRefreshDue: () => false,
    api(path) {
      if (path.includes('/telemetry?')) { const request = deferred(); requests.push(request); return request.promise; }
      return Promise.resolve(path === '/api/agents' ? [agent] : path === '/api/summary' ? {} : []);
    }});
  vm.runInContext(['refreshAll', 'openAgent'].map(functionSource).join('\n'), context);
  const refreshing = context.refreshAll(true);
  const opening = context.openAgent('a');
  requests[1].resolve([{marker: 'new drawer'}]); await opening;
  requests[0].resolve([{marker: 'old refresh'}]); assert.equal(await refreshing, true);
  assert.equal(state.selectedAgentTelemetry[0].marker, 'new drawer');
});
