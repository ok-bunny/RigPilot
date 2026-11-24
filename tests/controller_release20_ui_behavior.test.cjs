"use strict";
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const vm=require("node:vm"),test=require("node:test"),assert=require("node:assert/strict");
const source=readStatic();
const html=readStatic('index.html');
const adapters=JSON.parse(html.match(/<script id="minerAdapterBootstrap" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const fn = functionLoader(source);
function harness(extraAdapters=[]){
 const registry=[...extraAdapters,...adapters.filter(item=>!extraAdapters.some(extra=>extra.miner_type===item.miner_type))];
 const item=(type,algorithm,resource="")=>({type:{value:type},algorithm,extra:{resource_kind:resource},executionMode:{value:"native",disabled:false},gpuBackend:{value:"amd"}});
 const legs={flight:item("srbminer","yespower","gpu"),flightSecondary:item("xmrig","randomx")};
 const settings={secondary:true,mode:"independent",hidden:false};
 const draft={value:"180",disabled:false},section={classList:{toggle:(_,hidden)=>settings.hidden=hidden}},field={classList:{remove:()=>{}}},hint={textContent:""};
 const ctx=vm.createContext({
   $:selector=>selector==="#flightMinerType"?legs.flight.type:selector==="#flightExecutionMode"?legs.flight.executionMode:selector==="#flightSecondaryEnabled"?{checked:settings.secondary}:selector==="#flightGpuOcSection"?section:selector.endsWith("ExecutionModeField")?field:selector.endsWith("ExecutionModeHint")?hint:null,
   $$:()=>[draft],updateFlightGpuOcIndexHints:()=>{},
   managedLegElements:prefix=>legs[prefix],effectiveFlightAlgorithm:prefix=>legs[prefix].algorithm,
   adapterSchemaOptionValue:(prefix,key,fallback)=>legs[prefix].extra[key]||fallback,
   minerAdapterDefinition:type=>registry.find(a=>a.miner_type===type),minerAdapterAlgorithms:type=>registry.find(a=>a.miner_type===type)?.algorithms || [],
   minerAdapterHasCapability:(type,cap)=>registry.find(a=>a.miner_type===type)?.capabilities.includes(cap) || false,
   usesIntegratedSrbDual:()=>settings.secondary&&settings.mode==="integrated",
   flightSecondaryMode:()=>settings.mode
 });
 vm.runInContext(["minerAdapterAlgorithmSpec","minerAdapterAlgorithmResource","managedLegResource","managedLegSupportsWsl","syncManagedExecutionMode","flightGpuOcIsSupported","syncFlightGpuOcVisibility","srbDualPairSupported","sameGpuIndices","validateSrbDualFlight","validateIntegratedDualFlight"].map(fn).join("\n"),ctx);
 return {ctx,legs,settings,draft,hint};
}
test("GPU OC eligibility follows actual resource ownership and execution mode",()=>{
 const h=harness();assert.equal(h.ctx.flightGpuOcIsSupported(),true);
 h.legs.flight.extra.resource_kind="cpu";assert.equal(h.ctx.flightGpuOcIsSupported(),false);
 h.legs.flight.extra.resource_kind="gpu";h.legs.flightSecondary.type.value="srbminer";h.legs.flightSecondary.algorithm="pearlhash";h.legs.flightSecondary.extra.resource_kind="gpu";assert.equal(h.ctx.flightGpuOcIsSupported(),false);
 h.settings.mode="integrated";assert.equal(h.ctx.flightGpuOcIsSupported(),true);
 h.legs.flight.executionMode.value="wsl";assert.equal(h.ctx.flightGpuOcIsSupported(),false);
 h.legs.flight.executionMode.value="native";h.legs.flight.type.value="lolminer";h.legs.flight.algorithm="ETCHASH";assert.equal(h.ctx.flightGpuOcIsSupported(),false);
});
test("unsupported GPU OC hides controls and retains drafts through re-enable",()=>{
 const h=harness();h.legs.flight.extra.resource_kind="cpu";h.ctx.syncFlightGpuOcVisibility();assert.equal(h.settings.hidden,true);assert.equal(h.draft.disabled,true);assert.equal(h.draft.value,"180");
 h.legs.flight.extra.resource_kind="gpu";h.ctx.syncFlightGpuOcVisibility();assert.equal(h.settings.hidden,false);assert.equal(h.draft.disabled,false);assert.equal(h.draft.value,"180");
});
test("WSL eligibility and synchronization follow capabilities and actual selected resource",()=>{
 const h=harness();h.legs.flight.extra.resource_kind="cpu";h.legs.flight.executionMode.value="wsl";assert.equal(h.ctx.managedLegSupportsWsl("flight"),false);h.ctx.syncManagedExecutionMode("flight");assert.equal(h.legs.flight.executionMode.value,"native");assert.equal(h.legs.flight.executionMode.disabled,true);
 h.legs.flight.extra.resource_kind="gpu";h.legs.flight.executionMode.value="wsl";assert.equal(h.ctx.managedLegSupportsWsl("flight"),true);h.ctx.syncManagedExecutionMode("flight");assert.equal(h.legs.flight.executionMode.value,"wsl");assert.equal(h.legs.flight.executionMode.disabled,false);
 h.settings.mode="integrated";h.legs.flightSecondary.type.value="srbminer";assert.equal(h.ctx.managedLegSupportsWsl("flight"),false);
 h.settings.secondary=false;h.legs.flight.type.value="xmrig";h.legs.flight.algorithm="randomx";h.legs.flight.extra.resource_kind="";assert.equal(h.ctx.managedLegSupportsWsl("flight"),false);
 h.legs.flight.type.value="forgeminer";h.legs.flight.algorithm="qhash";assert.equal(h.ctx.managedLegSupportsWsl("flight"),false);
 h.legs.flight.type.value="pearlminer";h.legs.flight.algorithm="pearlhash";h.legs.flight.executionMode.value="wsl";h.ctx.syncManagedExecutionMode("flight");assert.equal(h.legs.flight.gpuBackend.value,"nvidia");
});
test("ordered GPU pairs preserve order while periodic and CPU plus GPU remain supported",()=>{
 const h=harness();assert.equal(h.ctx.srbDualPairSupported("fishhash","qhash","gpu","gpu"),true);assert.equal(h.ctx.srbDualPairSupported("qhash","fishhash","gpu","gpu"),false);
 assert.equal(h.ctx.srbDualPairSupported("randomepic","randomx","cpu","cpu"),true);assert.equal(h.ctx.srbDualPairSupported("yespower","xelishashv3","cpu","gpu"),true);assert.equal(h.ctx.srbDualPairSupported("randomx","randomx","cpu","cpu"),false);
});
test("the generic integrated validator retains SRB-specific restrictions",()=>{
 const h=harness(),leg=algorithm=>({miner_type:"srbminer",miner_version:"3.7.2",algorithm,device_indices:[],extra_config:{}});
 assert.throws(()=>h.ctx.validateIntegratedDualFlight(leg("randomx"),leg("randomx")),/两个 RandomX/);
 assert.throws(()=>h.ctx.validateIntegratedDualFlight({...leg("fishhash"),device_indices:[0]}, {...leg("qhash"),device_indices:[1]}),/相同顺序的 GPU 索引/);
});
