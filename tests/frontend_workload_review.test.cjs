'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const {readStatic, functionLoader} = require('./helpers/controller_ui.cjs');
const source = readStatic(), fn = functionLoader(source);

test('automatic workload refresh advances a selected preset but preserves a manually entered range', () => {
  const start = Date.UTC(2026, 10, 21, 6, 30), fields = {'#workloadFrom': {}, '#workloadTo': {}, '#workloadAgent': {value:'rig-1'}};
  let current = start, active = true;
  class Clock extends Date { constructor(...args) { super(...(args.length ? args : [current])); } static now() { return current; } }
  const button = {dataset:{workloadPreset:'24'}, classList:{toggle(_key, value){active=value;}}};
  const ctx = vm.createContext({Date:Clock, URLSearchParams, WORKLOAD_MAX_RANGE_MS:336*3600000, state:{page:'workload'},
    $: selector => selector === '[data-workload-preset].active' ? (active ? button : null) : fields[selector],
    $$: () => [button]});
  const names = ['localDateTimeInputValue', 'setWorkloadPreset', 'workloadRangeForRequest'];
  if (source.includes('function applyWorkloadPresetRange(')) names.push('applyWorkloadPresetRange');
  vm.runInContext(names.map(fn).join('\n'), ctx);
  ctx.setWorkloadPreset(24, false);
  const first = ctx.workloadRangeForRequest();
  current += 15*60000;
  const next = ctx.workloadRangeForRequest();
  assert.equal(next.to.valueOf(), current);
  assert.equal(next.from.valueOf(), first.from.valueOf()+15*60000);
  assert.equal(next.to-next.from, 24*3600000);
  assert.equal(fields['#workloadTo'].value, ctx.localDateTimeInputValue(new Clock(current)));
  assert.equal(new URL(next.path,'https://fixture.test').searchParams.get('agent_id'),'rig-1');
  active = false;
  fields['#workloadFrom'].value='2026-11-15T07:30'; fields['#workloadTo'].value='2026-11-15T09:30';
  current += 60*60000;
  const custom = ctx.workloadRangeForRequest();
  assert.equal(custom.to.valueOf(),new Date('2026-11-15T09:30').valueOf());
  assert.equal(custom.from.valueOf(),new Date('2026-11-15T07:30').valueOf());
});

test('failed settlement write leaves the loaded draft editable and allows a successful retry', async () => {
  const draft='2026-11-21T09:20', input={value:draft}, button={}, status={classList:{add(){},remove(){}}};
  const fields={'#workloadSettlementNote':input, '#workloadSettlementNoteSave':button, '#workloadSettlementUseAsStart':{},
    '#workloadSettlementNoteStatus':status, '#workloadSettlementLegacyNote':{classList:{toggle(){}}}};
  let saves=0;
  const state={token:'test',authEpoch:1,user:{role:'operator'},settlementNoteLoaded:true,settlementNoteDirty:true,
    settlementNoteSaving:false,settlementNote:{revision:2}};
  const ctx=vm.createContext({state,document:{activeElement:null},$:s=>fields[s], WORKLOAD_SETTLEMENT_NOTE_PATH:'/api/workload-settlement-note',
    toast(){},formatTime:()=>'',async api(_path,{body}){ assert.equal(body.expected_revision,2); if (++saves===1) throw Error('暂时无法保存');
      return {settlement_at:body.settlement_at,revision:3}; }});
  vm.runInContext(['settlementIsoFromLocal','settlementLocalValue','renderWorkloadSettlementNote','saveWorkloadSettlementNote'].map(fn).join('\n'),ctx);
  assert.equal(await ctx.saveWorkloadSettlementNote(),false);
  assert.match(status.textContent,/暂时无法保存/);
  assert.equal(input.value,draft); assert.equal(input.readOnly,false); assert.equal(button.disabled,false);
  assert.equal(await ctx.saveWorkloadSettlementNote(),true); assert.equal(state.settlementNote.revision,3);
  state.settlementNoteLoaded=false;ctx.renderWorkloadSettlementNote();
  assert.equal(button.disabled,true);assert.equal(await ctx.saveWorkloadSettlementNote(),false);
  assert.equal(saves,2);
});

test('returning from the second miner configuration restores its parent scroll position and button focus', () => {
  const classList=()=>({add(){}, remove(){}});
  const state={flightMinerConfigPrefix:null,flightEditorReturnTop:0};
  const opener={focus(opts){assert.equal(opts.preventScroll,true);this.focused=true;}};
  const form={scrollTop:483,classList:classList(),querySelector(){return opener;}};
  const fields={classList:classList(),disabled:false,querySelector(){return {focus(){}};}};
  const nodes={'#flightForm':form,'#flightSecondaryFields':fields,'#flightDialog':{open:true},'#flightSecondaryAdvanced':{}};
  const context=vm.createContext({state,$:s=>nodes[s],$$:()=>[],requestAnimationFrame:cb=>cb()});
  vm.runInContext(['openFlightMinerConfig','closeFlightMinerConfig'].map(fn).join('\n'),context);
  context.openFlightMinerConfig('flightSecondary');assert.equal(form.scrollTop,0);
  form.scrollTop=250;
  context.closeFlightMinerConfig();
  assert.equal(form.scrollTop,483);
  assert.equal(opener.focused,true);
  context.closeFlightMinerConfig({restoreFocus:false});
  assert.equal(form.scrollTop,0);
});
