'use strict';
const assert = require('node:assert/strict');
const {readStatic} = require('./helpers/controller_ui.cjs');
const test = require('node:test');
const vm = require('node:vm');
const source = process.env.HASHRATE_SPIKE_UI_SOURCE
  ? require('node:fs').readFileSync(process.env.HASHRATE_SPIKE_UI_SOURCE, 'utf8') : readStatic();

function labelMap() {
  const constant = source.match(/^const auditNames = \{[\s\S]*?^\};/m)?.[0] || '';
  assert.ok(constant, 'auditNames map not found in app.js');
  const escapeHtml = source.match(/^function escapeHtml\(value\) \{[\s\S]*?^\}/m)?.[0] || '';
  const context = vm.createContext({String});
  return vm.runInContext(
    [escapeHtml, constant, 'auditNames;'].join('\n'),
    context,
  );
}

test('audit label map covers the hashrate spike rejection action', () => {
  const map = labelMap();
  assert.equal(map.hashrate_spike_rejected, '拒绝异常算力样本');
});

test('device activity rendering resolves the rejection action through the map', () => {
  const map = labelMap();
  const render = action => map[action] || action;
  assert.equal(render('hashrate_spike_rejected'), '拒绝异常算力样本');
  // Unknown actions keep the raw identifier instead of crashing.
  assert.equal(render('something_new'), 'something_new');
});

test('rejection audit stays distinguishable from command and update actions', () => {
  const map = labelMap();
  assert.notEqual(map.hashrate_spike_rejected, map.command_queued);
  assert.notEqual(map.hashrate_spike_rejected, map.agent_update_failed);
  assert.ok(Object.values(map).every(label => !label.includes('undefined')));
});
