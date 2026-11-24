'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert=require('node:assert/strict'),vm=require('node:vm'),test=require('node:test');
const source=readStatic();
const fn = functionLoader(source);
function harness(){
 const nodes=new Map(),messages=[],confirms=[];
 const $=selector=>{if(!nodes.has(selector))nodes.set(selector,{value:'',textContent:''});return nodes.get(selector)};
 const state={pools:[{id:'pearl-tcp',name:'Pearl TCP',coin:'PEARL',protocol:'stratum+tcp',pool_url:'stratum+tcp://new.example:4444',pool_password:'new-pass'},
   {id:'xmr-ssl',name:'XMR SSL',coin:'XMR',protocol:'stratum+ssl',pool_url:'stratum+ssl://xmr.example:443',pool_password:'xmr-pass'}],
   wallets:[{id:'pearl-wallet',coin:'PEARL'},{id:'xmr-wallet',coin:'XMR'}]};
 const context=vm.createContext({$,state,window:{confirm:question=>{confirms.push(question);return true}},toast:message=>messages.push(message)});
 vm.runInContext(['selectedSavedPool','applySavedPoolToFlight'].map(fn).join('\n'),context);
 $('#flightWallet').value='pearl-wallet';$('#flightSecondaryWallet').value='xmr-wallet';$('#flightMinerType').value='srbminer';$('#flightSecondaryMinerType').value='xmrig';
 $('#flightPool').value='stratum+ssl://old.example:443';$('#flightPoolPassword').value='old-pass';
 $('#flightSecondaryPool').value='stratum+tcp://old-xmr.example:3333';$('#flightSecondaryPoolPassword').value='old-xmr-pass';
 return {context,state,$,messages,confirms};
}
test('saved pool copies complete URL and exact password to only the selected flight leg',()=>{
 const h=harness();h.$('#flightSavedPool').value='pearl-tcp';h.context.applySavedPoolToFlight('flight');
 assert.equal(h.$('#flightPool').value,'stratum+tcp://new.example:4444');assert.equal(h.$('#flightPoolPassword').value,'new-pass');
 assert.equal(h.$('#flightSecondaryPool').value,'stratum+tcp://old-xmr.example:3333');assert.equal(h.$('#flightSecondaryPoolPassword').value,'old-xmr-pass');
 h.$('#flightSecondarySavedPool').value='xmr-ssl';h.context.applySavedPoolToFlight('flightSecondary');
 assert.equal(h.$('#flightSecondaryPool').value,'stratum+ssl://xmr.example:443');assert.equal(h.$('#flightSecondaryPoolPassword').value,'xmr-pass');
 assert.equal(h.$('#flightPool').value,'stratum+tcp://new.example:4444');
});
test('coin mismatch refuses selection without changing wallet or raw flight fields',()=>{
 const h=harness();h.$('#flightSavedPool').value='xmr-ssl';h.context.applySavedPoolToFlight('flight');
 assert.equal(h.$('#flightSavedPool').value,'');assert.equal(h.$('#flightPool').value,'stratum+ssl://old.example:443');
 assert.equal(h.$('#flightWallet').value,'pearl-wallet');assert.match(h.$('#flightError').textContent,/钱包/);
});
