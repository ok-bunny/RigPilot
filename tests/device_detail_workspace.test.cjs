'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert=require('node:assert/strict'),vm=require('node:vm'),test=require('node:test');
const source=readStatic();
const fn = functionLoader(source);
const now=()=>new Date().toISOString();
test('all-zero device history labels a real zero while retaining a safe nonzero plotting scale',()=>{
 const chart={innerHTML:'',querySelectorAll:()=>[],insertAdjacentHTML(_where,markup){this.innerHTML=markup+this.innerHTML}},agent={id:'A',name:'Rig A'};
 const state={selectedAgent:agent,deviceHashrateHistory:{agentId:'A',data:{agent_id:'A',from:new Date(Date.now()-3600000).toISOString(),to:now(),bucket_minutes:5,series:[{coin:'PEARL',unit:'H/s',points:[{timestamp:new Date(Date.now()-1800000).toISOString(),value:0},{timestamp:now(),value:0}]}]},loading:false,error:''}};
 const context=vm.createContext({state,$:selector=>selector==='#deviceHashrateChart'?chart:null,Date,Math,Number,Array,
  hashrateHistoryPoints:series=>series.points.map(point=>({timestamp:Date.parse(point.timestamp),value:point.value})),hashrateHistorySegments:points=>[points],hashrateChartColors:()=>['#ffb020'],hashrateHistorySeriesLabel:series=>series.coin,formatHashrateAmount:value=>`${value} H/s`,escapeHtml:value=>String(value)});
 vm.runInContext(['renderHashrateSeries','bindHashrateHistoryHover','renderDeviceHashrateHistory'].map(fn).join('\n'),context);context.renderDeviceHashrateHistory();
 assert.match(chart.innerHTML,/最高 0 H\/s/);assert.doesNotMatch(chart.innerHTML,/最高 1 H\/s/);assert.doesNotMatch(chart.innerHTML,/NaN|Infinity/);
});
test('a device history response without exact agent identity is refused',async()=>{
 const state={selectedAgent:{id:'A'},deviceDetailTab:'gpus',deviceHashrateHistory:null,deviceHashrateRequestId:0,authEpoch:0};
 const context=vm.createContext({state,Date,encodeURIComponent,renderDeviceHashrateHistory(){},api:async()=>({series:[]})});
 vm.runInContext(fn('loadDeviceHashrateHistory'),context);await context.loadDeviceHashrateHistory('A');
 assert.match(state.deviceHashrateHistory.error,/未确认此设备身份/);assert.equal(state.deviceHashrateHistory.data,null);
});
test('same display name never substitutes for exact device audit identity',()=>{
 const context=vm.createContext({});vm.runInContext(fn('deviceAuditMatchesAgent'),context);
 const commands=[{id:'cmd-A',agent_id:'A'}];
 assert.equal(context.deviceAuditMatchesAgent({target_type:'agent',target_id:'A'},'A',commands),true);
 assert.equal(context.deviceAuditMatchesAgent({target_type:'agent',target_id:'B',related_agent_id:'A'},'A',commands),false);
 assert.equal(context.deviceAuditMatchesAgent({target_type:'command',target_id:'cmd-A'},'A',commands),true);
 assert.equal(context.deviceAuditMatchesAgent({target_type:'user',target_id:'shared',related_agent_id:'A'},'A',commands),false);
});
test('late first-device log response cannot replace second-device logs',async()=>{
 const resolvers={};const state={selectedAgent:{id:'A',name:'Same'},deviceLog:null,deviceLogRequestId:0,authEpoch:0};
 const context=vm.createContext({state,Date,encodeURIComponent,renderDeviceActivity(){},deviceAuditMatchesAgent:(row,id)=>row.target_id===id,
  api:url=>new Promise(resolve=>{resolvers[url]=resolve})});
 vm.runInContext(fn('loadDeviceActivity'),context);
 const first=context.loadDeviceActivity('A');
 state.selectedAgent={id:'B',name:'Same'};state.deviceLogRequestId+=1;state.deviceLog=null;
 const second=context.loadDeviceActivity('B');
 resolvers['/api/commands?agent_id=B&limit=100&include_all_active=true']([{id:'B-command',agent_id:'B'}]);
 resolvers['/api/audit?agent_id=B&limit=100']([{target_type:'agent',target_id:'B'}]);
 await second;
 resolvers['/api/commands?agent_id=A&limit=100&include_all_active=true']([{id:'A-command',agent_id:'A'}]);
 resolvers['/api/audit?agent_id=A&limit=100']([{target_type:'agent',target_id:'A'}]);
 await first;
 assert.equal(state.deviceLog.agentId,'B');assert.deepEqual(state.deviceLog.commands.map(item=>item.id),['B-command']);
 assert.deepEqual(state.deviceLog.audit.map(item=>item.target_id),['B']);
});
test('modal OC clear remains null, zero remains numeric, and untouched blanks stay omitted',()=>{
 const inputs={fan_percent:{value:'',validity:{badInput:false}},core_clock_offset_mhz:{value:'0',validity:{badInput:false}},memory_clock_offset_mhz:{value:'',validity:{badInput:false}}};
 const target={agentId:'A',uuid:'GPU-1',index:0,capabilities:Object.keys(inputs),initialSetting:{fan_percent:65,core_clock_offset_mhz:100}};
 const context=vm.createContext({state:{deviceGpuOcTarget:target},gpuOcFields:Object.keys(inputs),Number,Object,
  $:selector=>inputs[selector.match(/data-device-gpu-field=([^\]]+)/)?.[1]]});
 vm.runInContext(fn('gpuOcNumberIsValid')+'\n'+fn('collectDeviceGpuOcSetting'),context);
 const setting=context.collectDeviceGpuOcSetting(target,{});
 assert.equal(setting.fan_percent,null);assert.equal(setting.core_clock_offset_mhz,0);
 assert.equal(Object.hasOwn(setting,'memory_clock_offset_mhz'),false);
});
test('workload refresh is lazy, visible-only and throttled after a request',()=>{
 const state={page:'dashboard',token:'auth',workloadLoading:false,workloadHasRequested:false,workloadLastRequestAt:0};
 const context=vm.createContext({state,Date});vm.runInContext(fn('workloadRefreshDue'),context);
 assert.equal(context.workloadRefreshDue(),false);state.page='workload';assert.equal(context.workloadRefreshDue(),true);
 state.workloadHasRequested=true;state.workloadLastRequestAt=Date.now();assert.equal(context.workloadRefreshDue(),false);
 state.workloadLastRequestAt-=61000;assert.equal(context.workloadRefreshDue(),true);
 state.workloadLoading=true;assert.equal(context.workloadRefreshDue(),false);
});


test('per-card modal apply keeps its own target and form outside drawer refreshes', async()=>{
 const form={id:'deviceGpuOcForm'},state={deviceGpuOcTarget:{agentId:'A',uuid:'GPU-1'}};
 let submitted=null;
 const context=vm.createContext({state,$:selector=>selector==='#deviceGpuOcForm'?form:{id:selector},
  submitDeviceGpuOc:async(target,actualForm,error,button)=>{submitted={target,actualForm,error,button}}});
 vm.runInContext(fn('applyDeviceGpuOc'),context);
 await context.applyDeviceGpuOc();
 assert.equal(submitted.actualForm,form);assert.equal(submitted.target,state.deviceGpuOcTarget);
 assert.equal(submitted.error.id,'#deviceGpuOcError');assert.equal(submitted.button.id,'#applyDeviceGpuOcButton');
});

test('fresh cached device history redraws after the drawer DOM is replaced without refetching',async()=>{
 let draws=0,requests=0;
 const state={selectedAgent:{id:'A'},deviceDetailTab:'gpus',deviceHashrateHistory:{agentId:'A',loading:false,data:{agent_id:'A',series:[]},fetchedAt:Date.now()},deviceHashrateRequestId:0,authEpoch:0};
 const context=vm.createContext({state,Date,encodeURIComponent,renderDeviceHashrateHistory(){draws++},api:async()=>{requests++;return {}}});
 vm.runInContext(fn('loadDeviceHashrateHistory'),context);
 await context.loadDeviceHashrateHistory('A');
 assert.equal(draws,1);assert.equal(requests,0);
});
