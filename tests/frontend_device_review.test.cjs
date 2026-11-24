'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const {deferred, functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const fn = functionLoader(readStatic());
const load = (ctx, names) => vm.runInContext(names.map(fn).join('\n'), ctx);

function drawer() {
  const agent = {id: 'a', name: 'Server name'};
  const state = {user: {role: 'admin'}, authEpoch: 1, selectedAgent: agent, agents: [agent],
    deviceDetailTab: 'overview', selectedAgentTelemetryId: agent.id, selectedAgentTelemetry: ['old A']};
  const content = {dataset: {agentId: agent.id}, contains: () => false, scrollTop: 0};
  const button = {disabled: false}, graph = {getClientRects: () => [1]};
  const field = {id: 'agentDisplayName', value: 'Draft', dataset: {dirty:'true'}};
  const frames = [], draws = [], response = deferred();
  const nodes = {'#drawerContent':content, '#agentDisplayName':field, '#telemetryChart':graph,
    '#deviceDrawer': {classList:{contains: () => true}}};
  const ctx = vm.createContext({state, document: {activeElement:null}, window:{requestAnimationFrame: cb => frames.push(cb)},
    $: key => nodes[key] || button, $$: () => [], toast() {}, api: () => response.promise, renderDevices() {},
    renderDrawer(agent, preserve = true) {
      const saved = preserve ? ctx.captureDrawerState() : null;
      field.value = agent.name; field.dataset = {};
      ctx.switchDeviceDetailTab(state.deviceDetailTab);
      ctx.restoreDrawerState(saved);
    },
    drawTelemetry: points => draws.push(...points)});
  load(ctx, ['captureDrawerState','restoreDrawerState','refreshSelectedAgentDrawer',
             'switchDeviceDetailTab','refreshAgentStatus','saveAgentDetails']);
  return {ctx,state,response,field,frames,draws};
}

test('manual device status refresh keeps a drafted display name', async () => {
  const h = drawer(), pending = h.ctx.refreshAgentStatus();
  h.response.resolve({id:'a', name:'Server name'}); await pending;
  assert.equal(h.field.value,'Draft');
  assert.equal(h.field.dataset.dirty,'true');
});

for (const editAgain of [false,true]) {
  test('saving device name ' + (editAgain ? 'preserves typing during the PATCH' : 'shows the confirmed name'), async () => {
    const h = drawer(), pending = h.ctx.saveAgentDetails({preventDefault() {}});
    if (editAgain) h.field.value = 'Next draft';
    h.response.resolve({id:'a', name:'Draft'}); await pending;
    assert.equal(h.state.selectedAgent.name,'Draft');
    assert.equal(h.field.value,editAgain ? 'Next draft' : 'Draft');
    assert.equal(h.field.dataset.dirty,editAgain ? 'true' : undefined);
  });
}

for (const switchDevice of [false,true]) {
  test('animation frame reads the current device and telemetry after '+(switchDevice ? 'switching devices' : 'a newer sample'), () => {
    const h = drawer();
    h.ctx.refreshSelectedAgentDrawer();
    if (switchDevice) {
      h.state.selectedAgent={id:'b',name:'B'}; h.state.selectedAgentTelemetryId='b';
    }
    h.state.selectedAgentTelemetry = [switchDevice ? 'B' : 'new A'];
    for (const frame of h.frames) frame();
    assert.deepEqual(h.draws,switchDevice ? [] : ['new A']);
  });
}

function gpuDialog() {
  const gpu = {index:0, uuid:'GPU-a', name:'A', capabilities:['power_limit_w'], hasSavedOc:true};
  const agent = {id:'a', name:'A', status:'online', metrics:{gpus:[gpu]}};
  const state = {user:{role:'admin'}, agents:[agent],
    deviceGpuOcTarget:{...gpu,agentId:'a',hasSaved:true}, deviceGpuOcBatchTarget:{agentId:'a',gpus:[gpu]}};
  const nodes=new Map();
  const node = key => {
    if (!nodes.has(key)) nodes.set(key,{textContent:'',disabled:false,open:true,isConnected:true,close(){this.open=false;}});
    return nodes.get(key);
  };
  const response=deferred(), calls=[], results=[], notices=[];
  const ctx=vm.createContext({state, $:node, $$:()=>[], window:{confirm:()=>true},
    gpuOcFields:['power_limit_w'], deviceRuntimePreparationReason:()=>'', agentVersionAtLeast:()=>true,
    gpuMetricIndex:g=>g.index, gpuMetricUuid:g=>g.uuid, gpuMetricIsNvidia:()=>true,
    gpuOcCapabilities:g=>g.capabilities, collectDeviceGpuOcSetting:()=>({index:0,power_limit_w:100}),
    gpuOcEditSummary:()=>'', selectedDeviceGpuOcBatchItems:()=>[gpu], validateCurrentBatchGpuTargets:()=>true,
    collectDeviceGpuOcBatchSetting:()=>({power_limit_w:100}), batchGpuConfirmationLines:()=>'',
    api(path){calls.push(path);return response.promise;}, refreshAll:async()=>{},
    renderBatchOperationResults(){results.push('render');}, toast(text){notices.push(text);}});
  load(ctx,['setDeviceGpuOcPending','batchRangeIntersection','syncDeviceGpuOcBatchSelection',
            'submitDeviceGpuOc','applyDeviceGpuOc','deleteDeviceGpuOc','applyDeviceGpuOcBatch','clearDeviceGpuOcBatch']);
  return {ctx,state,node,response,calls,results,notices};
}

for (const op of ['applyDeviceGpuOc','deleteDeviceGpuOc','applyDeviceGpuOcBatch','clearDeviceGpuOcBatch']) {
  const batch = op.endsWith('Batch');
  for (const outcome of ['success','failure']) {
    test(op+' ignores '+outcome+' from a previous dialog instance', async () => {
      const h=gpuDialog(), key=batch ? 'deviceGpuOcBatchTarget' : 'deviceGpuOcTarget';
      const controls=batch ? ['#applyDeviceGpuOcBatchButton','#clearDeviceGpuOcBatchButton'] :
        ['#applyDeviceGpuOcButton','#deleteDeviceGpuOcButton'];
      const dialog=h.node(batch ? '#deviceGpuOcBatchDialog' : '#deviceGpuOcDialog');
      const error=h.node(batch ? '#deviceGpuOcBatchError' : '#deviceGpuOcError');
      const first=h.ctx[op]();
      if(batch) h.ctx.syncDeviceGpuOcBatchSelection();
      assert.ok(controls.every(k=>h.node(k).disabled));
      await h.ctx[batch ? 'applyDeviceGpuOcBatch' : 'applyDeviceGpuOc']();
      await h.ctx[batch ? 'clearDeviceGpuOcBatch' : 'deleteDeviceGpuOc']();
      assert.equal(h.calls.length,1);
      // Reopening even the same GPU creates a distinct edit. Its own request
      // may already be pending when the first response comes back.
      const replacement={...h.state[key],pending:true}; h.state[key]=replacement;
      error.textContent='New dialog';
      if(outcome==='success') h.response.resolve({status:'reset_pending',reset_command:{},deferred:true});
      else h.response.reject(new Error('Old failure'));
      await first;
      assert.equal(h.state[key],replacement);
      assert.equal(dialog.open,true); assert.equal(error.textContent,'New dialog');
      assert.ok(controls.every(k=>h.node(k).disabled));
      assert.deepEqual(h.results,[]); assert.deepEqual(h.notices,[]);
    });
  }
}
