"use strict";
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=readStatic();
const fn = functionLoader(source);
test('native GPU readings and display formatter distinguish missing from real zero',()=>{
 const context=vm.createContext({Number,Intl,HASHRATE_UNITS:['H/s','kH/s','MH/s','GH/s','TH/s','PH/s','EH/s'],gpuMetricIndex:g=>g.index,gpuReportedVendor:()=> 'nvidia',fullGpuPciAddress:()=>'',miningStatusForAgent:a=>a.inventory.mining_status,miningDisplayState:()=> 'running'});
 vm.runInContext(['hasHashrateAmount','formatHashrateAmount','readingTimestamp','gpuMiningHashrateSnapshot','miningHashrateDetailIsFresh'].map(fn).join('\n'),context);
 for(const rate of [null,false,'',' ',[],{},-1,Infinity]){const a={status:'online',inventory:{mining_status:{state:'running',miner_type:'srbminer',hashrate_unit:'H/s',hashrate_details:[{role:'primary',algorithm:'pearlhash',gpu_hashrates:[{index:0,hashrate:rate}]}]}}};assert.equal(context.gpuMiningHashrateSnapshot(a,{index:0}).readings.length,0);assert.equal(context.formatHashrateAmount(rate),'—')}
 for(const rate of [0,'0',42]){const a={status:'online',inventory:{mining_status:{state:'running',miner_type:'srbminer',hashrate_details:[{role:'primary',algorithm:'pearlhash',gpu_hashrates:[{index:0,hashrate:rate}]}]}}};assert.equal(context.gpuMiningHashrateSnapshot(a,{index:0}).readings.length,1);assert.notEqual(context.formatHashrateAmount(rate),'—')}
});
test('untouched rename follows new server value; actual user draft survives redraw',()=>{
 let field={id:'agentDisplayName',type:'text',value:'server-old',dataset:{}};const content={dataset:{agentId:'A'},scrollTop:5,contains:()=>false};
 const ctx=vm.createContext({document:{activeElement:null},$:(s)=>s==='#drawerContent'?content:s==='#agentDisplayName'?field:null,$$:()=>[]});vm.runInContext(fn('captureDrawerState')+'\n'+fn('restoreDrawerState'),ctx);
 const clean=ctx.captureDrawerState();field={...field,value:'server-new',dataset:{}};ctx.restoreDrawerState(clean);assert.equal(field.value,'server-new');
 field.value='user-draft';field.dataset.dirty='true';const dirty=ctx.captureDrawerState();field={...field,value:'server-next',dataset:{}};ctx.restoreDrawerState(dirty);assert.equal(field.value,'user-draft');assert.equal(field.dataset.dirty,'true');
});
test('GPU index lists cannot silently turn empty fields or numeric notation into GPU 0',()=>{
 const input={value:''};const ctx=vm.createContext({$:()=>input});vm.runInContext(fn('parseDeviceIndices'),ctx);
 for(const invalid of ['1,',',1','1,,2','0x1','1e0','1.0','-0','32','1,1']){
  input.value=invalid;assert.throws(()=>ctx.parseDeviceIndices('#indices','GPU'),/GPU/);
 }
 for(const [raw,expected] of [['',[]],['0',[0]],[' 1, 0,31 ',[1,0,31]]]){
  input.value=raw;assert.deepEqual(Array.from(ctx.parseDeviceIndices('#indices','GPU')),expected);
 }
});
