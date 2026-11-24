"use strict";
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const vm=require("node:vm"),test=require("node:test"),assert=require("node:assert/strict");
const source=readStatic();
const html=readStatic('index.html');
const adapters=JSON.parse(html.match(/<script id="minerAdapterBootstrap" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const srb=adapters.find(a=>a.miner_type==="srbminer");
const fn = functionLoader(source);
function context(){const ctx=vm.createContext({minerAdapterDefinition:()=>srb,minerAdapterAlgorithms:()=>srb.algorithms});vm.runInContext(["minerAdapterAlgorithmSpec","minerAdapterAlgorithmResource","srbDualPairSupported","sameGpuIndices","validateSrbDualFlight"].map(fn).join("\n"),ctx);return ctx}
const leg=(algorithm,options={})=>({miner_type:"srbminer",miner_version:"3.7.1",algorithm,device_indices:[],extra_config:options});
for(const [name,primary,secondary] of [["primary fixed",{cpu_threads:2},{}],["secondary fixed",{},{cpu_threads:8}]])test(`periodic CPU mixed automatic/fixed: ${name} gives the current launcher explanation`,()=>{const ctx=context();assert.throws(()=>ctx.validateSrbDualFlight(leg("randomepic",primary),leg("randomx",secondary)),e=>e.message===srb.periodic_cpu_threads_rule.hint&&/当前启动器/.test(e.message)&&/全部留空/.test(e.message)&&/分别明确/.test(e.message))});
for(const [name,primary,secondary] of [["automatic",{},{}],["explicit null",{cpu_threads:null},{}],["both fixed",{cpu_threads:2},{cpu_threads:8}],["reduce and intensity",{cpu_threads_reduce:2},{cpu_threads_reduce:8,cpu_threads_intensity:4}]])test(`periodic CPU valid ${name} leaves per-leg values intact`,()=>{const ctx=context(),p=leg("randomepic",primary),s=leg("randomx",secondary),before=JSON.stringify([p,s]);assert.doesNotThrow(()=>ctx.validateSrbDualFlight(p,s));assert.equal(JSON.stringify([p,s]),before)});
test("CPU plus GPU single fixed CPU thread count is unaffected",()=>{const ctx=context();assert.doesNotThrow(()=>ctx.validateSrbDualFlight(leg("randomepic",{cpu_threads:2}),leg("pearlhash",{resource_kind:"gpu"})))});
test("GPU plus CPU secondary fixed thread count is unaffected",()=>{const ctx=context();assert.doesNotThrow(()=>ctx.validateSrbDualFlight(leg("progpow_epic",{resource_kind:"gpu"}),leg("randomx",{cpu_threads:8})))});
