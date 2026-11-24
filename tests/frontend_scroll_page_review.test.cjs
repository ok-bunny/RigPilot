'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const {readStatic, functionLoader} = require('./helpers/controller_ui.cjs');
const showPageSource = functionLoader(readStatic())('showPage');

function harness(start, offset, railWidth = 260) {
  const root = {scrollTop: 680, scrollLeft: 0};
  const names = ['dashboard', 'devices', 'wallets', 'pools', 'miners', 'flights', 'workload'];
  const strip = {scrollWidth: names.length * 100, clientWidth: railWidth, scrollLeft: offset,
    getBoundingClientRect: () => ({left: 14, right: 14 + railWidth})};
  function block() {
    const classes = new Set();
    return {classList: {toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      contains: name => classes.has(name)}};
  }
  const pages = names.map(name => ({...block(), id: name+'Page'}));
  const nav = names.map((page, i) => ({...block(), dataset: {page}, parentElement: strip,
    setAttribute(name, value) { this[name] = value; },
    removeAttribute(name) { delete this[name]; },
    getBoundingClientRect() {
      const left = 14 + i*100 - strip.scrollLeft;
      return {left, right: left + 100};
    }}));
  const state = {page: start, user:{role:'admin'}};
  const title = {}, kicker = {};
  const ctx = vm.createContext({state, document:{scrollingElement:root},
    $: selector => selector === '#pageTitle' ? title : kicker,
    $$: selector => selector === '.page' ? pages : nav, workloadRefreshDue: () => false});
  vm.runInContext(showPageSource, ctx);
  return {show: ctx.showPage, root, strip, nav, title, state};
}

test('navigating from a scrolled page opens the new title and reveals only the active nav item', () => {
  for (const [start, target, offset] of [['dashboard','workload',0], ['workload','devices',440]]) {
    const h=harness(start,offset);
    h.show(target);
    assert.equal(h.root.scrollTop,0);
    assert.equal(h.root.scrollLeft,0);
    const current=h.nav.filter(item => item['aria-current']==='page');
    assert.equal(current.length,1);
    assert.equal(current[0].dataset.page,target);
    const item=current[0].getBoundingClientRect(), strip=h.strip.getBoundingClientRect();
    assert.ok(item.left >= strip.left && item.right <= strip.right);
    assert.equal(h.title.textContent, target==='devices' ? '设备管理' : '工作量');
  }
});

test('showing the current page preserves reading position; a wide nav needs no horizontal correction', () => {
  const h=harness('workload',350);
  h.show('workload');
  assert.equal(h.root.scrollTop,680);
  assert.equal(h.strip.scrollLeft,350);
  const wide=harness('workload',0,900);
  wide.show('devices');
  assert.equal(wide.root.scrollTop,0);
  assert.equal(wide.strip.scrollLeft,0);
});
