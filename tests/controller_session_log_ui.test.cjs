'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const {deferred, functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const fn = functionLoader(readStatic());

test('live log badges and tabs group an unrecognized device role with ordinary miner output', () => {
  const session = {previousBySource: new Map(), lines: [], dropped: 0, activeTab: 'all'};
  const tabs = {}, output = {};
  const ctx = vm.createContext({
    state: {liveMinerLog: session},
    $: selector => ({'#liveMinerLogTabs': tabs, '#liveMinerLogOutput': output}[selector]),
    minerLogLineKind: () => ({name: 'info', label: 'INFO'}),
  });
  vm.runInContext(['escapeHtml', 'liveMinerLogOverlap', 'appendLiveMinerLogSnapshot',
    'liveMinerLogRoleLabel', 'liveMinerLogHighlight', 'liveMinerLogLineMarkup',
    'minerLogErrorMarkup', 'renderLiveMinerLogs'].map(fn).join('\n'), ctx);
  ctx.appendLiveMinerLogSnapshot(session, {logs: [
    {role: 'primary', source: 'p', lines: ['normal']},
    {role: 'new role "v2"', source: 'q', lines: ['extra stream']},
  ]});
  ctx.renderLiveMinerLogs();
  assert.deepEqual([...tabs.innerHTML.matchAll(/data-tab="([^"]+)"/g)].map(m => m[1]), ['all', 'primary', 'legacy']);
  assert.match(output.innerHTML, /miner-live-role is-legacy/);
  assert.match(output.innerHTML, /extra stream/);
  assert.doesNotMatch(tabs.innerHTML + output.innerHTML, /new role/);
  session.activeTab = 'legacy';
  ctx.renderLiveMinerLogs();
  assert.match(output.innerHTML, /extra stream/);
  assert.doesNotMatch(output.innerHTML, /normal/);
});

for (const changeSession of [false, true]) {
  for (const responseType of ['archive', 'unauthorized', 'network failure']) {
    test(`release download handles ${responseType} in the ${changeSession ? 'replaced' : 'original'} session`, async () => {
      const response = deferred(), body = deferred(), requested = deferred();
      const state = {authEpoch: 1, token: 'old', agentReleases: [{id:'pkg', version:'0.8.60', platform:'linux'}]};
      const events = [], button = {isConnected: true};
      const ctx = vm.createContext({
        state, fetch() {return response.promise;},
        URL: {createObjectURL() {return 'blob:test';}, revokeObjectURL() {}},
        document: {createElement() {return {click() {events.push('download');}, remove() {}};}, body: {append() {}}},
        setTimeout() {}, toast() {events.push('notice');}, logout() {events.push('logout');},
      });
      vm.runInContext(fn('downloadAgentRelease'), ctx);
      const flight = ctx.downloadAgentRelease('pkg', button);
      if (responseType !== 'network failure') {
        const read = () => {requested.resolve(); return body.promise;};
        response.resolve({ok: responseType === 'archive', status: responseType === 'archive' ? 200 : 401,
          blob: read, json: read});
        await requested.promise;
      }
      if (changeSession) {state.authEpoch++; state.token = 'new';}
      if (responseType === 'network failure') response.reject(new Error('Offline'));
      else body.resolve(responseType === 'archive' ? {} : {detail:'Expired'});
      await flight;
      const expected = responseType === 'archive' ? ['download','notice'] :
        responseType === 'unauthorized' ? ['logout','notice'] : ['notice'];
      assert.deepEqual(events, changeSession ? [] : expected);
      assert.equal(button.disabled, false);
    });
  }
}

for (const restoreSucceeds of [true, false]) {
  test(`interactive login waits for saved session restoration (${restoreSucceeds ? 'valid' : 'expired'})`, async () => {
    const me = deferred(), state = {token: 'saved', authEpoch: 0, user: null};
    const button = {disabled: false}, nodes = new Map(), calls = [];
    const ctx = vm.createContext({
      state, document: {addEventListener() {}}, bindEvents() {}, setInterval() {},
      $: selector => {
        if (selector === '#loginForm button[type=submit]') return button;
        if (!nodes.has(selector)) nodes.set(selector, {value: '', textContent: ''});
        return nodes.get(selector);
      },
      $$: () => [], loadWorkloadPreferences() {}, loadWorkloadOwners() {}, roleLabel: role => role,
      showPage() {}, refreshAll: async () => true, loginView() {}, logout() {state.token = '';},
      api(path) {calls.push(path); return me.promise;},
    });
    vm.runInContext(['login','enterApp','boot'].map(fn).join('\n'),ctx);
    const boot = ctx.boot();
    assert.equal(button.disabled, true);
    await ctx.login({preventDefault() {}});
    assert.deepEqual(calls, ['/api/auth/me']);
    if (restoreSucceeds) me.resolve({username: 'restored', role: 'viewer'}); else me.reject(new Error('expired'));
    await boot;
    assert.equal(button.disabled, false);
    assert.equal(state.user?.username, restoreSucceeds ? 'restored' : undefined);
    assert.equal(state.token, restoreSucceeds ? 'saved' : '');
  });
}
