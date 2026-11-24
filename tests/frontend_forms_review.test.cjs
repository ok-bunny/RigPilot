'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const {deferred, readStatic} = require('./helpers/controller_ui.cjs');
const source = readStatic();

function harness() {
  const nodes = new Map(), pending = [];
  function node(selector) {
    if (!nodes.has(selector)) {
      const classes = new Set();
      nodes.set(selector, {
        value: '', textContent: '', checked: false, disabled: false, required: false, files: [], dataset: {},
        classList: {
          add: (name) => classes.add(name), remove: (name) => classes.delete(name),
          contains: (name) => classes.has(name),
          toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
        },
        querySelectorAll: () => [], reset() {}, showModal() { this.open = true; }, close() { this.open = false; },
      });
    }
    return nodes.get(selector);
  }
  const radios = ['upload', 'import'].map(value => ({value, checked: value === 'upload'}));
  const document = {
    addEventListener() {},
    querySelector: selector => selector === 'input[name="minerPackageSource"]:checked'
      ? radios.find(radio => radio.checked) : node(selector),
    querySelectorAll: selector => selector === 'input[name="minerPackageSource"]' ? radios : [],
  };
  const ctx = vm.createContext({document, window: {matchMedia: () => ({matches: false}), addEventListener() {}},
    sessionStorage: {getItem: () => ''}, URL, URLSearchParams});
  vm.runInContext(source, ctx);  // DOMContentLoaded stays unfired.
  const state = vm.runInContext('state', ctx);
  state.user = {role: 'admin'};
  ctx.api = (path, options) => {
    const wait = deferred(); pending.push({path, options, ...wait}); return wait.promise;
  };
  ctx.refreshAll = async () => {};
  ctx.toast = () => {};
  ctx.minerAdapterDefinition = type => type ? {
    display_name: type, platforms: [{os_family:'windows'}, {os_family:'linux'}],
  } : null;
  return {ctx, state, node, pending};
}

test('HTTPS import clears every file requirement and restores the single-platform classification', () => {
  const {ctx, node} = harness();
  node('#minerPackageManagedType').value = 'pearlminer';
  node('#minerPackageManagedOsFamily').value = 'linux';
  node('#minerPackageDualPlatform').checked = true;
  ctx.syncMinerPackageClassification();
  assert.equal(node('#minerPackageManagedOsFamily').disabled, true);
  assert.equal(node('#minerPackageFileLinux').required, true);
  ctx.syncMinerPackageSource('import');
  for (const key of ['minerPackageFile', 'minerPackageFileWindows', 'minerPackageFileLinux']) {
    assert.equal(node('#'+key).disabled, true, key);
    assert.equal(node('#'+key).required, false, key);
  }
  assert.equal(node('#minerPackageUrl').required, true);
  assert.equal(node('#minerPackageUrl').disabled, false);
  assert.equal(node('#minerPackageManagedOsFamily').disabled, false);
  assert.equal(node('#minerPackageArchiveType').disabled, false);
  assert.equal(node('#minerPackageArchiveType').value, 'tar.gz');
  ctx.syncMinerPackageSource('upload');
  assert.equal(node('#minerPackageFile').required, true);
  assert.equal(node('#minerPackageFile').disabled, false);
  assert.equal(node('#minerPackageUrl').required, false);
  assert.equal(node('#minerPackageUrl').disabled, true);
});

for (const sessionChanged of [false, true]) {
  test('dual upload binds the submitted Linux variant and session (session changes: '+sessionChanged+')', async () => {
    const {ctx, state, node, pending} = harness();
    node('#minerPackageManagedType').value = 'pearlminer';
    node('#minerPackageManagedOsFamily').value = 'windows';
    node('#minerPackageManagedVariant').value = 'amd';
    node('#minerPackageDualPlatform').checked = true;
    node('#minerPackageName').value = 'pearlminer';
    node('#minerPackageVersion').value = '1.2.3';
    node('#minerPackageFileWindows').files = [{name:'w.zip',size:2}];
    node('#minerPackageFileLinux').files = [{name:'l.tar.gz',size:2}];
    const uploading = ctx.createMinerPackage();
    assert.equal(pending.length, 1);
    node('#minerPackageManagedVariant').value = 'nvidia';
    if (sessionChanged) state.authEpoch++;
    pending[0].resolve({});
    await Promise.resolve();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(pending.length, sessionChanged ? 1 : 2);
    if (!sessionChanged) {
      const query = new URL(pending[1].path, 'http://localhost').searchParams;
      assert.equal(query.get('managed_variant'), 'amd');
      assert.equal(query.get('managed_os_family'), 'linux');
      pending[1].resolve({});
    }
    await uploading;
  });
}

const editorKinds = [
  ['wallet', 'openWalletDialog', 'saveWallet', 'editingWalletId', 'createWalletButton'],
  ['pool', 'openPoolDialog', 'savePool', 'editingPoolId', 'savePoolButton'],
  ['flight', 'openFlightDialog', 'saveFlightSheet', 'editingFlightSheetId', 'createFlightButton'],
];
for (const [kind, open, save, idKey, buttonId] of editorKinds) {
  for (const staleFailure of [false,true]) {
    test(kind+' editor preserves a reopened draft when the earlier save '+(staleFailure?'fails':'succeeds'), async () => {
      const {ctx, state, node, pending} = harness();
      state.wallets = [{id:'w',name:'wallet',coin:'XMR'}];
      state.flightSheets = ['a','b'].map(id => ({id,name:id,miner_type:'xmrig',wallet_id:'w'}));
      // Stub independent miner-option rendering; exercise the real editor open/save path.
      for (const key of ['closeFlightMinerConfig','setFlightFormHistoricalReadOnly','populateManagedMinerTypeSelect',
        'setManagedExtra','setSecondaryEnabled','syncManagedLeg','setFlightAlgorithmValue','populateSeineFlightPackageSelect',
        'populateSavedPoolSelectors']) ctx[key] = () => {};
      ctx.walletOptions = () => '';
      ctx.flightSheetMigrationReason = () => '';
      ctx.buildManagedLeg = () => ({miner_type:'xmrig'});
      ctx.flightGpuOcIsSupported = () => false;
      const show = id => ctx[open](kind === 'flight' ? id : {id,name:id,coin:'XMR',address:'wallet-address'});
      const dialog = node('#'+kind+'Dialog'), button=node('#'+buttonId);
      show('a');
      const first = ctx[save]();
      assert.equal(pending.length,1);
      dialog.close();
      show('b');
      assert.equal(button.disabled,false);
      const second = ctx[save]();
      assert.equal(pending.length,2);
      assert.match(pending[1].path,/\/b$/);
      if (staleFailure) pending[0].reject(new Error('old form error'));
      else pending[0].resolve({});
      await first;
      assert.equal(dialog.open, true);
      assert.equal(state[idKey], 'b');
      assert.equal(node('#'+kind+'Error').textContent, '');
      assert.equal(button.disabled, true);
      pending[1].resolve({});
      await second;
      assert.equal(dialog.open, false);
      assert.equal(button.disabled, false);
    });
  }
}

for (const staleFailure of [false, true]) {
  test('flight batch stays locked across refresh and shows no verdict under a different selection (old failure: '+staleFailure+')', async () => {
    const {ctx, state, node, pending} = harness();
    ctx.window.confirm = () => true;
    const worker={id:'worker',name:'Test worker'};
    state.agents=[worker];
    state.flightSheets=['one','two'].map(id=>({id,name:id}));
    ctx.selectedFlightBatchApplyItems=()=>[worker];
    ctx.flightTargetEligibility=()=>({eligible:true});
    ctx.flightSheetVersionIssue=()=>null;
    ctx.filteredAgents=()=>[];
    let shown=0;
    ctx.renderBatchOperationResults=()=>{shown++;};
    ctx.openFlightBatchApplyDialog();
    node('#flightBatchApplySheet').value='one';
    ctx.renderFlightBatchApplyAgents();
    const first=ctx.applyFlightSheetBatch();
    assert.equal(pending.length,1);
    assert.match(pending[0].path,/\/one\/apply-batch$/);
    ctx.syncFlightBatchApplySelection(); // also runs on ordinary telemetry refresh
    assert.equal(node('#applyFlightSheetBatchButton').disabled,true);
    node('#flightBatchApplySheet').value='two';
    ctx.renderFlightBatchApplyAgents();
    const duplicate=ctx.applyFlightSheetBatch();
    assert.equal(pending.length,1);
    await duplicate;
    if (staleFailure) pending[0].reject(new Error('old selection failed'));
    else pending[0].resolve({accepted_count:1});
    await first;
    assert.equal(shown,0);
    assert.equal(node('#flightBatchApplyError').textContent,'');
    assert.equal(node('#applyFlightSheetBatchButton').disabled,false);
    const second=ctx.applyFlightSheetBatch();
    assert.equal(pending.length,2);
    assert.match(pending[1].path,/\/two\/apply-batch$/);
    pending[1].resolve({accepted_count:1});
    await second;
    assert.equal(shown,1);
  });
}
