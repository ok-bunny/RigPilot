'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const vm=require('node:vm'),test=require('node:test'),assert=require('node:assert/strict');
const source=readStatic();
const fn = functionLoader(source);
const fields=['power_limit_w','core_clock_offset_mhz','core_clock_lock_mhz','memory_clock_offset_mhz','memory_clock_lock_mhz','fan_percent'];
function context(){const nodes=new Map(),state={workloadPreferences:{mode:'multiple',coins:['PEARL'],weights:{PEARL:'2'}},workloadRevenueAmount:''};const node=(key)=>{if(!nodes.has(key))nodes.set(key,{id:key,value:'',checked:false,disabled:false,innerHTML:'',validity:{}});return nodes.get(key);};
 const $=(key,parent)=>node((parent?.id||'')+'|'+key);const ctx=vm.createContext({state,$,document:{activeElement:null},rememberDisclosures(){},restoreDisclosures(){},disclosureKey:(...parts)=>JSON.stringify(parts),WORKLOAD_FOLD_HASHES:1e9,WORKLOAD_COLORS:['#9bc75b'],gpuOcFields:fields,escapeHtml:s=>String(s??'').replaceAll('<','&lt;').replaceAll('>','&gt;'),selectedDeviceGpuOcBatchItems:()=>[{capabilities:fields}],Number,Math,Map,Set,Intl});
 const names=['gpuOcNumberIsValid','collectDeviceGpuOcSetting','collectDeviceGpuOcBatchSetting','gpuOcEditSummary','gpuOcParameterSummary','workloadCanonicalCoin','workloadWeightNumber','supportedWorkloadCoins','selectedWorkloadCoins','workloadValueNumber','formatWorkAmount','formatWorkloadPercentage','workloadOwnerAgentId','workloadIndividualDevices','workloadOwnerAssignments','workloadCombinedModel','formatWorkloadWeightedAmount','syncWorkloadRevenueInput','rememberWorkloadRevenueTotal','workloadRevenueTotal','workloadRevenueForPercentage','formatWorkloadRevenue','renderWorkloadSummary'];vm.runInContext(names.map(fn).join('\n'),ctx);return{ctx,state,$};}

test('single card clearing an existing value emits null, untouched blanks are omitted, zero stays zero',()=>{
 const f=context();f.state.deviceGpuOcTarget={index:2,capabilities:fields,initialSetting:{core_clock_offset_mhz:100,fan_percent:65}};const form=f.$('#deviceGpuOcForm');
 f.$('[data-device-gpu-field=core_clock_offset_mhz]',form).value='0';
 const result=JSON.parse(JSON.stringify(f.ctx.collectDeviceGpuOcSetting()));assert.deepEqual(result,{index:2,core_clock_offset_mhz:0,fan_percent:null});
 assert.match(f.ctx.gpuOcEditSummary(result),/Core Offset \+0/);assert.match(f.ctx.gpuOcEditSummary(result),/Fan 恢复默认/);
 f.$('[data-device-gpu-field=core_clock_offset_mhz]',form).value='';assert.deepEqual(JSON.parse(JSON.stringify(f.ctx.collectDeviceGpuOcSetting())),{index:2,core_clock_offset_mhz:null,fan_percent:null});
});

test('batch initial blanks do nothing and only an explicit reset choice clears a field',()=>{
 const f=context();assert.throws(()=>f.ctx.collectDeviceGpuOcBatchSetting(),/勾选/);const form=f.$('#deviceGpuOcBatchForm');
 f.$('[data-device-gpu-batch-reset="fan_percent"]',form).checked=true;
 assert.deepEqual(JSON.parse(JSON.stringify(f.ctx.collectDeviceGpuOcBatchSetting())),{fan_percent:null});
 f.$('[data-device-gpu-batch-field=core_clock_offset_mhz]',form).value='0';
 assert.deepEqual(JSON.parse(JSON.stringify(f.ctx.collectDeviceGpuOcBatchSetting())),{core_clock_offset_mhz:0,fan_percent:null});
});

test('filtered individual workload retains fleet coin denominator and revenue share',()=>{
 const f=context();f.state.workloadHasRequested=true;f.state.workload={group_by:'coin',series:[{coin:'PEARL',algorithm:null,algorithms:['pearlhash','pearlhash-v2'],unit:'H',estimated:true,series_total:125e9,total:125e9,devices:[{agent_id:'a',agent_name:'A',value:55e9,percentage:44}]}]};f.ctx.renderWorkloadSummary();f.$('#workloadRevenueTotal').value='100';f.ctx.rememberWorkloadRevenueTotal();
 const html=f.$('#workloadSummary').innerHTML;assert.match(html,/44%/);assert.match(html,/<small>收益<\/small><strong>44\.00<\/strong>/);assert.doesNotMatch(html,/100\.00|pearlhash/);assert.equal((html.match(/<section class="workload-algorithm-card workload-combined-card">/g)||[]).length,1);
});
