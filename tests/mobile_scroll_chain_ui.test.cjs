'use strict';
// Style contract for the mobile (<=900px) touch scroll chain fix (0.8.45).
//
// Mobile pages must scroll as one document: the pinned-panel inner wrappers
// only ever scroll tables sideways, so the vertical overscroll chain has to
// stay open (overscroll-behavior-y: auto) for a finger anywhere on a table to
// scroll the page. Horizontal containment stays so sideways table swipes do
// not drag the page. Desktop pinned panels (min-width: 901px) must keep their
// own vertical contain behavior and fixed-height layout untouched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const styles = fs.readFileSync(
  path.join(__dirname, '..', 'server', 'static', 'styles.css'),
  'utf8',
);

function mediaBlocks(query) {
  const needle = `@media (${query})`;
  const blocks = [];
  let at = styles.indexOf(needle);
  while (at !== -1) {
    const open = styles.indexOf('{', at);
    assert.ok(open > at, `media query ${query} has an opening brace`);
    let depth = 0;
    let end = -1;
    for (let i = open; i < styles.length; i += 1) {
      if (styles[i] === '{') depth += 1;
      else if (styles[i] === '}') {
        depth -= 1;
        if (depth === 0) {
          end = i;
          break;
        }
      }
    }
    assert.ok(end > open, `media query ${query} block is balanced`);
    blocks.push({ start: at, end, body: styles.slice(open + 1, end) });
    at = styles.indexOf(needle, end);
  }
  return blocks;
}

function flat(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/\s+/g, ' ');
}

test('mobile block opens the vertical overscroll chain on pinned-panel wrappers', () => {
  const mobile = mediaBlocks('max-width: 900px');
  assert.ok(mobile.length > 0, 'a max-width: 900px media block exists');
  const joined = mobile.map((b) => flat(b.body)).join('\n');
  assert.match(
    joined,
    /\.pinned-scroll-panel > \.table-wrap, \.pinned-scroll-panel > \.empty-state\s*\{ overscroll-behavior-x: contain; overscroll-behavior-y: auto; \}/,
    'the pinned-panel inner scrollers keep inline containment but re-open the block/vertical chain',
  );
});

test('mobile block unclips the pinned panels themselves', () => {
  const mobile = mediaBlocks('max-width: 900px');
  const joined = mobile.map((b) => flat(b.body)).join('\n');
  assert.match(
    joined,
    /\.pinned-scroll-panel\s*\{ overflow: visible; \}/,
    'pinned panels stop clipping on narrow screens so no engine can make them vertical scrollers',
  );
});

test('desktop pinned-panel contain behavior stays untouched', () => {
  const base = styles.slice(
    styles.indexOf('.pinned-scroll-panel > .table-wrap {'),
    styles.indexOf('.pinned-scroll-panel > .empty-state'),
  );
  assert.ok(base.length > 0, 'base .pinned-scroll-panel > .table-wrap rule exists');
  assert.match(flat(base), /overflow: auto; overscroll-behavior: contain;/);
  const desktop = mediaBlocks('min-width: 901px');
  assert.ok(desktop.length > 0, 'a min-width: 901px media block exists');
  const joined = desktop.map((b) => flat(b.body)).join('\n');
  assert.match(joined, /\.pinned-page\.active \{/, 'desktop fixed-height pinned pages stay');
  assert.match(joined, /calc\(100dvh - 182px\)/, 'desktop pinned page height formula stays');
});

test('mobile override is declared after the base pinned rules it overrides', () => {
  const baseAt = styles.search(/\.pinned-scroll-panel\s*\{ display: flex/);
  const mobileAt = styles.indexOf('@media (max-width: 900px)', baseAt);
  assert.ok(baseAt > 0, 'base pinned-panel rule exists');
  assert.ok(mobileAt > baseAt, 'the narrow-viewport block that overrides it comes later in the file');
});
