'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const vm=require('node:vm'),assert=require('node:assert/strict'),test=require('node:test');
const source=readStatic();
const fn = functionLoader(source);
function disclosureHarness(){const state={disclosureStates:new Map(),authEpoch:1};const ctx=vm.createContext({state,JSON,String});vm.runInContext(['disclosureKey','rememberDisclosures','restoreDisclosures'].map(fn).join('\n'),ctx);return{state,ctx}}
function node(key,open=false){const listeners={};return{dataset:{disclosureKey:key},open,isConnected:true,listeners,addEventListener(name,cb){listeners[name]=cb}}}
const root=(...nodes)=>({querySelectorAll:()=>nodes});
test('latest close is captured synchronously across two refresh replacements; detached toggles are ignored',()=>{
 const{state,ctx}=disclosureHarness(),key=ctx.disclosureKey('device','A','advanced','GPU-1');const first=node(key,true);ctx.rememberDisclosures(root(first));const second=node(key);ctx.restoreDisclosures(root(second));assert.equal(second.open,true);
 second.open=false;ctx.rememberDisclosures(root(second));second.isConnected=false;const third=node(key);ctx.restoreDisclosures(root(third));assert.equal(third.open,false);
 second.open=true;second.listeners.toggle();assert.equal(state.disclosureStates.get(key),false);
 const fourth=node(key,true);ctx.rememberDisclosures(root(third));ctx.restoreDisclosures(root(fourth));assert.equal(fourth.open,false);
});
test('device, GPU, package, release and context keys isolate matching names and prevent delimiter collisions',()=>{
 const{ctx}=disclosureHarness();const keys=[ctx.disclosureKey('device','A','advanced','GPU-1'),ctx.disclosureKey('device','B','advanced','GPU-1'),ctx.disclosureKey('device','A','advanced','GPU-2'),ctx.disclosureKey('device','A','full'),ctx.disclosureKey('package','p1','analysis'),ctx.disclosureKey('package','p2','analysis'),ctx.disclosureKey('release','r1','same-digest'),ctx.disclosureKey('release','r2','same-digest'),ctx.disclosureKey('a:b','c'),ctx.disclosureKey('a','b:c')];assert.equal(new Set(keys).size,keys.length);
 const opened=node(keys[0],true);ctx.rememberDisclosures(root(opened));const same=node(keys[0]),other=node(keys[1]);ctx.restoreDisclosures(root(other,same));assert.equal(same.open,true);assert.equal(other.open,false);
});
test('new login cannot revive a previous session disclosure',()=>{const{ctx,state}=disclosureHarness();const old=ctx.disclosureKey('device','A','full');ctx.rememberDisclosures(root(node(old,true)));state.authEpoch++;const fresh=node(ctx.disclosureKey('device','A','full'));ctx.restoreDisclosures(root(fresh));assert.equal(fresh.open,false)});
test('sample order, explicit null gap, invalid readings and real zero stay distinct',()=>{
 const ctx=vm.createContext({Date,Number,Object,Math});vm.runInContext(['hashrateHistoryPoints','hashrateHistorySegments'].map(fn).join('\n'),ctx);
 const base=Date.parse('2026-09-30T00:00:00Z');const points=ctx.hashrateHistoryPoints({points:[{timestamp:new Date(base+120000).toISOString(),value:0},{timestamp:new Date(base).toISOString(),value:10},{timestamp:new Date(base+60000).toISOString(),value:null,hashrate:99},{timestamp:new Date(base+180000).toISOString(),value:' '},{timestamp:new Date(base+240000).toISOString(),value:false},{timestamp:new Date(base+300000).toISOString(),value:-1}]});
 assert.deepEqual(Array.from(points,p=>p.value),[10,0]);assert.equal(points[1].gapBefore,true);assert.equal(ctx.hashrateHistorySegments(points,525000).length,2);
});

test('nested parent/child disclosures keep independent intent when the parent is closed',()=>{
 const{ctx}=disclosureHarness(),parentKey=ctx.disclosureKey('device','A','parent'),childKey=ctx.disclosureKey('device','A','child');ctx.rememberDisclosures(root(node(parentKey,false),node(childKey,true)));const parent=node(parentKey,true),child=node(childKey,false);ctx.restoreDisclosures(root(parent,child));assert.equal(parent.open,false);assert.equal(child.open,true);parent.open=true;ctx.rememberDisclosures(root(parent,child));const nextParent=node(parentKey),nextChild=node(childKey);ctx.restoreDisclosures(root(nextParent,nextChild));assert.equal(nextParent.open,true);assert.equal(nextChild.open,true);
});
test('device loading, errors, empty data and wrong-device series are explicit without drawing fleet data',()=>{
 const chart={innerHTML:''},agent={id:'A',name:'Rig A'};let drawCount=0;
 const state={selectedAgent:agent,deviceHashrateHistory:null};const ctx=vm.createContext({state,$:()=>chart,Array,escapeHtml:String,hashrateHistoryPoints:series=>series?.points||[],renderHashrateSeries(){drawCount++}});vm.runInContext(fn('renderDeviceHashrateHistory'),ctx);
 ctx.renderDeviceHashrateHistory();assert.match(chart.innerHTML,/正在读取此设备/);
 state.deviceHashrateHistory={agentId:'A',error:'加载错误',loading:false};ctx.renderDeviceHashrateHistory();assert.match(chart.innerHTML,/加载错误/);
 state.deviceHashrateHistory={agentId:'A',loading:false,data:{agent_id:'B',series:[{coin:'XMR',points:[{value:120}]}]}};ctx.renderDeviceHashrateHistory();assert.match(chart.innerHTML,/暂无可核验算力/);
 state.deviceHashrateHistory={agentId:'A',loading:false,data:{agent_id:'A',series:[]}};ctx.renderDeviceHashrateHistory();assert.match(chart.innerHTML,/暂无可核验算力/);
 state.deviceHashrateHistory={agentId:'A',loading:false,data:{agent_id:'A',series:[{agent_id:'B',coin:'XMR',points:[{value:120}]}]}};ctx.renderDeviceHashrateHistory();assert.match(chart.innerHTML,/设备身份不匹配/);assert.equal(drawCount,0);
});
