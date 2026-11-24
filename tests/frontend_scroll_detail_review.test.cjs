'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const fn = functionLoader(readStatic());

test('drawer refresh restores both scroll owners and follows the same GPU when records reorder', () => {
  let focused;
  const action = (uuid,index) => ({dataset:{gpuUuid:uuid,deviceGpuOc:String(index)},
    focus(options){assert.equal(options.preventScroll,true); focused=this;}});
  const oldButton=action('GPU-a',0), otherButton=action('GPU-b',0), sameButton=action('GPU-a',1);
  const content={dataset:{agentId:'rig-a'},scrollTop:236,contains:()=>true};
  let table={scrollLeft:145}, buttons=[oldButton];
  const ctx=vm.createContext({document:{activeElement:oldButton},
    $: selector=>selector==='#drawerContent'?content:selector.includes('table-wrap')?table:null,
    $$: ()=>buttons});
  vm.runInContext(['captureDrawerState','restoreDrawerState'].map(fn).join('\n'),ctx);
  const snapshot=ctx.captureDrawerState();
  content.scrollTop=0; table={scrollLeft:0}; buttons=[otherButton,sameButton];
  ctx.restoreDrawerState(snapshot);
  assert.equal(content.scrollTop,236); assert.equal(table.scrollLeft,145); assert.equal(focused,sameButton);
  content.dataset.agentId='rig-b'; content.scrollTop=table.scrollLeft=0; focused=null;
  ctx.restoreDrawerState(snapshot);
  assert.equal(content.scrollTop,0); assert.equal(table.scrollLeft,0); assert.equal(focused,null);
});

function logFixture() {
  const session={activeTab:'all',previousBySource:new Map(),lines:[],dropped:0};
  let rows=[], y=0, writes=0, tabWrites=0, tabHtml='', tabButtons=[];
  const output={
    clientHeight:80,
    get scrollHeight(){return rows.length*20;},
    get scrollTop(){return y;}, set scrollTop(v){y=Math.max(0,Math.min(v,this.scrollHeight-this.clientHeight));},
    set innerHTML(html){ writes++; rows=[...html.matchAll(/data-log-sequence="(\d+)"/g)].map(m=>Number(m[1])); this.scrollTop=y; },
    querySelector(selector){const id=Number(selector.match(/"(\d+)"/)[1]), i=rows.indexOf(id);
      return i<0?null:{getBoundingClientRect:()=>({top:i*20-y})};},
  };
  const tabs={ownerDocument:{activeElement:null},contains:e=>tabButtons.includes(e),querySelectorAll:()=>tabButtons,
    get innerHTML(){return tabHtml;},set innerHTML(value){
      tabWrites++;tabHtml=value; tabs.ownerDocument.activeElement=null;
      tabButtons=[...value.matchAll(/data-tab="([^"]+)"/g)].map(m=>({
        dataset:{tab:m[1]},focus(opts){assert.equal(opts.preventScroll,true); tabs.ownerDocument.activeElement=this;},
      }));
    }};
  const ctx=vm.createContext({state:{liveMinerLog:session},escapeHtml:s=>String(s),
    $: k=>k==='#liveMinerLogOutput'?output:k==='#liveMinerLogTabs'?tabs:null,
    minerLogLineKind:()=>({name:'info',label:'I'}),liveMinerLogHighlight:s=>s,
    minerLogErrorMarkup:()=>''});
  vm.runInContext(['liveMinerLogOverlap','appendLiveMinerLogSnapshot','liveMinerLogRoleLabel',
    'liveMinerLogLineMarkup','renderLiveMinerLogs'].map(fn).join('\n'),ctx);
  let n=0;
  function add(count,role='primary'){ctx.appendLiveMinerLogSnapshot(session,{logs:[{role,source:'log',lines:Array.from({length:count},()=>String(++n))}]});}
  return {session,add,draw:()=>ctx.renderLiveMinerLogs(),output,tabs,
    atTop:()=>rows[Math.floor(y/20)],writes:()=>writes,tabWrites:()=>tabWrites};
}

test('live log preserves the retained row and offset while oldest entries expire, and follows the tail at bottom',()=>{
  const h=logFixture();
  for(let i=0;i<4;i++) h.add(500);
  h.draw();assert.equal(h.output.scrollTop,h.output.scrollHeight-h.output.clientHeight);
  h.output.scrollTop=4013;
  const first=h.atTop(), offset=h.output.scrollTop%20;
  h.add(50);h.draw();
  assert.equal(h.session.dropped,50);assert.equal(h.atTop(),first);assert.equal(h.output.scrollTop%20,offset);
  h.output.scrollTop=h.output.scrollHeight; h.add(50);h.draw();
  assert.equal(h.output.scrollTop,h.output.scrollHeight-h.output.clientHeight);
});

test('log polling status does not replace unchanged history; switching legs keeps keyboard focus and displays the selected tail',()=>{
  const h=logFixture();h.add(20);h.add(20,'secondary');h.draw();
  const writes=h.writes(), tabWrites=h.tabWrites();
  const secondary=h.tabs.querySelectorAll().find(b=>b.dataset.tab==='secondary');
  secondary.focus({preventScroll:true});h.output.scrollTop=35;
  h.session.status='waiting';h.draw();
  assert.equal(h.writes(),writes);assert.equal(h.tabWrites(),tabWrites);assert.equal(h.output.scrollTop,35);
  h.session.activeTab='secondary';h.draw();
  assert.equal(h.tabs.ownerDocument.activeElement.dataset.tab,'secondary');
  assert.equal(h.output.scrollHeight,400);assert.equal(h.output.scrollTop,320);
});
