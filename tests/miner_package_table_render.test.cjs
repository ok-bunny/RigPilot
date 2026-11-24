'use strict';
// Regression coverage for the 0.8.48 login outage: renderMinerPackages still
// called minerPackageAnalysisMarkup after that helper was deleted with the
// HiveOS removal, so every login ended in "minerPackageAnalysisMarkup is not
// defined" and the package table (and the page behind it) never rendered.
// This file executes the real renderMinerPackages against rendered row HTML,
// and adds a static smoke that scans all of app.js for the same incident
// class: a bare call to an identifier that is defined nowhere.
const {constantLoader, functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');

const source = readStatic();
const html = readStatic('index.html');
const functionSource = functionLoader(source);
const constantSource = constantLoader(source);

// The removed HiveOS package-analysis helper must stay removed together with
// its call site; reintroducing either half recreates this outage.
test('the deleted hiveos package-analysis helper is not referenced anywhere', () => {
  assert.doesNotMatch(source, /minerPackageAnalysisMarkup/);
  assert.doesNotMatch(source, /hiveosAnalysisBehaviorLabel/);
  assert.doesNotMatch(source, /hiveosComponentLabel/);
});

function classBag() {
  const classes = new Set();
  return {toggle(key, enabled) { if (enabled) classes.add(key); else classes.delete(key); },
    contains: key => classes.has(key)};
}

// Buttons/details are recovered from the HTML the render actually produced,
// so assertions operate on what innerHTML really received.
function queryRendered(htmlText, attribute) {
  const camel = attribute.replace(/-(\w)/g, (_, letter) => letter.toUpperCase());
  const nodes = [];
  const pattern = new RegExp(`data-${attribute}="([^"]*)"`, 'g');
  for (const match of htmlText.matchAll(pattern)) {
    nodes.push({dataset: {[camel]: match[1]}, handlers: {},
      addEventListener(kind, handler) { this.handlers[kind] = handler; }});
  }
  return nodes;
}

function renderHarness({role = 'admin', packages = [], adapters = []} = {}) {
  const deletes = [];
  const state = {user: role ? {role} : null, minerPackages: packages,
    disclosureStates: new Map(), authEpoch: 7, minerAdapters: undefined};
  const wrap = {classList: classBag()};
  const table = {classList: classBag(), closest: selector => selector === '.table-wrap' ? wrap : null};
  const empty = {classList: classBag()};
  let rendered = '';
  const body = {
    get innerHTML() { return rendered; },
    set innerHTML(value) { rendered = String(value); },
    closest: selector => selector === 'table' ? table : null,
    querySelectorAll: selector => selector.includes('data-disclosure-key') ? queryRendered(rendered, 'disclosure-key') : [],
  };
  const clickHandlers = [];
  const context = vm.createContext({
    state,
    $: selector => selector === '#minerPackageTableBody' ? body : selector === '#minerPackageEmpty' ? empty : null,
    $$: (selector, root) => {
      if (root !== body) return [];
      assert.match(selector, /\[data-delete-miner-package\]/);
      const buttons = queryRendered(rendered, 'delete-miner-package');
      for (const button of buttons) {
        const real = button.addEventListener;
        button.addEventListener = (kind, handler) => { clickHandlers.push({id: button.dataset.deleteMinerPackage, handler}); real.call(button, kind, handler); };
      }
      return buttons;
    },
    deleteResource: (kind, id) => deletes.push([kind, id]),
    fallbackMinerAdapters: adapters, legacyMinerAdapterOrder: new Map(),
  });
  const labelsBlock = source.match(/const MINER_PACKAGE_TYPE_LABELS = Object\.freeze\(\{[\s\S]*?\}\);/);
  assert.ok(labelsBlock, 'actual UI constant MINER_PACKAGE_TYPE_LABELS exists');
  const constants = [constantSource('RETIRED_MINER_LABELS'), labelsBlock[0]].join('\n');
  const functions = ['escapeHtml', 'disclosureKey', 'rememberDisclosures', 'restoreDisclosures', 'formatBytes',
    'setTableVisible', 'availableMinerAdapters', 'minerAdapterDefinition', 'minerPackageTypeLabel',
    'renderMinerPackages'].map(functionSource).join('\n');
  vm.runInContext(`${constants}\n${functions}`, context);
  return {state, body, table, wrap, empty, deletes, clickHandlers, rendered: () => rendered,
    render() { context.renderMinerPackages(); }};
}

const FALLBACK_ADAPTERS = [
  {miner_type: 'xmrig', display_name: 'XMRig', platforms: [{os_family: 'windows'}, {os_family: 'linux'}]},
  {miner_type: 'pearlminer', display_name: 'Pearl Miner', platforms: [{os_family: 'windows'}, {os_family: 'linux'}]},
];

const CLASSIFIED_PACKAGE = {
  id: 'pkg-1', name: 'uploaded bundle', version: '6.21.0', archive_type: 'zip',
  source_type: 'uploaded', original_filename: 'xmrig-win.zip', download_url: '', file_size: 2048,
  managed_miner_type: 'xmrig', managed_os_family: 'windows', managed_variant: '', managed_channel: 'test',
  note: '团队验证包', created_by: 'admin',
};

const EXTERNAL_PACKAGE = {
  id: 'pkg-2', name: 'legacy entry', version: '0.1.0', archive_type: 'tar.gz',
  source_type: 'external', original_filename: '', download_url: 'https://example.invalid/legacy.tar.gz',
  file_size: 0, managed_miner_type: '', note: '', created_by: '',
};

test('package table renders note and no-note rows through the real renderMinerPackages', () => {
  const h = renderHarness({packages: [CLASSIFIED_PACKAGE, EXTERNAL_PACKAGE], adapters: FALLBACK_ADAPTERS});
  // Before the fix this exact call threw "minerPackageAnalysisMarkup is not defined".
  assert.doesNotThrow(() => h.render(), 'renderMinerPackages must not reference deleted helpers');
  const markup = h.rendered();
  const rows = markup.split('<tr>').slice(1);
  assert.equal(rows.length, 2, 'one table row per package');
  // Classified row: adapter display name, version, classification, note column keeps only the note.
  assert.match(rows[0], /<strong>XMRig<\/strong>/);
  assert.match(rows[0], /版本 6\.21\.0/);
  assert.match(rows[0], /XMRig · Windows · 测试版/);
  assert.match(rows[0], /<td>团队验证包<\/td>/);
  assert.doesNotMatch(rows[0], /package-analysis/);
  // External row: filename recovered from the URL, unclassified, em dash for the missing note.
  assert.match(rows[1], /legacy\.tar\.gz/);
  assert.match(rows[1], /未归类/);
  assert.match(rows[1], /<td>—<\/td>/);
  assert.match(rows[1], /外部地址（兼容）/);
  assert.doesNotMatch(rows[1], /团队验证包/);
  assert.equal(h.empty.classList.contains('hidden'), true, 'empty state hidden with packages present');
  assert.equal(h.table.classList.contains('hidden'), false);
  assert.equal(h.wrap.classList.contains('hidden'), false);
});

test('note text is escaped and both admin rows keep working delete wiring', () => {
  const hostile = {...CLASSIFIED_PACKAGE, id: 'pkg-x', note: '<img src=x onerror=alert(1)>&"'};
  const h = renderHarness({packages: [hostile], adapters: FALLBACK_ADAPTERS});
  h.render();
  assert.match(h.rendered(), /&lt;img src=x onerror=alert\(1\)&gt;&amp;&quot;/);
  assert.doesNotMatch(h.rendered(), /<img src=x/);
  assert.equal(h.clickHandlers.length, 1);
  h.clickHandlers[0].handler();
  assert.deepEqual(h.deletes, [['minerPackage', 'pkg-x']]);
});

test('non-admin rows render without delete controls', () => {
  const h = renderHarness({role: 'viewer', packages: [CLASSIFIED_PACKAGE], adapters: FALLBACK_ADAPTERS});
  h.render();
  assert.doesNotMatch(h.rendered(), /data-delete-miner-package/);
  assert.deepEqual(h.clickHandlers, []);
  assert.deepEqual(h.deletes, []);
});

test('empty package list shows the empty state and hides the table', () => {
  const h = renderHarness({packages: [], adapters: FALLBACK_ADAPTERS});
  h.render();
  assert.equal(h.rendered(), '');
  assert.equal(h.empty.classList.contains('hidden'), false);
  assert.equal(h.table.classList.contains('hidden'), true);
  assert.equal(h.wrap.classList.contains('hidden'), true);
});

test('retired hiveos history rows render under the retired label without analysis markup', () => {
  const retired = {...EXTERNAL_PACKAGE, id: 'pkg-h', managed_miner_type: 'hiveos', note: '历史包'};
  const h = renderHarness({packages: [retired], adapters: FALLBACK_ADAPTERS});
  h.render();
  assert.match(h.rendered(), /HiveOS · 已退休/);
  assert.match(h.rendered(), /<td>历史包<\/td>/);
  assert.doesNotMatch(h.rendered(), /软件包内容分析|package-analysis/);
});

// --- Static smoke: no bare call in app.js resolves to an undefined identifier ---
// Regex-based, whole file (every template interpolation included, which is where
// the outage lived). Known limits, stated honestly: a dangling name that
// coincides with some unrelated parameter/local name anywhere is masked, and
// only call syntax `name(` is checked (passing an undefined function as a bare
// callback reference is not). Property calls (`obj.method()`) are out of scope
// by design; they resolve against their receiver.
const CONTROL_KEYWORDS = new Set(['if', 'for', 'while', 'switch', 'catch', 'return', 'typeof', 'delete',
  'void', 'do', 'else', 'in', 'of', 'case', 'await', 'yield', 'new', 'function', 'async', 'throws']);
const JS_AND_BROWSER_GLOBALS = new Set(['AbortController', 'AggregateError', 'Array', 'ArrayBuffer', 'atob',
  'BigInt', 'Blob', 'Boolean', 'btoa', 'clearInterval', 'clearTimeout', 'console', 'crypto', 'CustomEvent',
  'DataView', 'Date', 'decodeURIComponent', 'DOMParser', 'encodeURI', 'encodeURIComponent', 'Error',
  'EvalError', 'event', 'fetch', 'File', 'FileReader', 'Float32Array', 'FormData', 'getComputedStyle',
  'DOMException', 'Headers', 'history', 'Intl', 'isFinite', 'isNaN', 'JSON', 'Map', 'matchMedia', 'Math', 'MutationObserver',
  'Number', 'Object', 'Option', 'parseFloat', 'parseInt', 'performance', 'Promise', 'RangeError', 'ReferenceError',
  'RegExp', 'requestAnimationFrame', 'cancelAnimationFrame', 'requestIdleCallback', 'ResizeObserver',
  'Response', 'Request', 'queueMicrotask', 'scrollTo', 'scrollBy', 'setTimeout', 'setInterval', 'Set',
  'String', 'structuredClone', 'Symbol', 'SyntaxError', 'TextDecoder', 'TextEncoder', 'TypeError',
  'Uint8Array', 'URIError', 'URL', 'URLSearchParams', 'WeakMap', 'WeakSet', 'XMLSerializer', 'Intl']);
// app.js runs after theme-init.js and pci-identity-data.js in index.html; any
// non-browser global it reaches for must actually be declared by a provider.
const BROWSER_GLOBAL_MEMBERS = new Set(['AbortController', 'confirm', 'crypto', 'clearTimeout', 'devicePixelRatio',
  'getComputedStyle', 'location', 'requestAnimationFrame', 'setTimeout']);


// A '/' begins a regex literal (not division) when the last emitted
// non-whitespace character cannot end an expression operand, or when the
// last emitted word is a keyword that can be followed by an expression
// (return /x/, typeof /x/, ...). Stated limit: `if (a) /re/` (regex directly
// after a closing paren of a control clause) would read as division; that
// pattern does not occur in app.js today.
const REGEX_PRECEDING_KEYWORDS = new Set(['return', 'typeof', 'instanceof', 'in', 'of', 'case',
  'delete', 'void', 'throw', 'new', 'do', 'else', 'yield', 'await']);
function startsRegex(out) {
  let k = out.length - 1;
  while (k >= 0 && /\s/.test(out[k])) k--;
  const c = out[k];
  if (c === undefined) return true;
  if ('=(,[!&|?{};:+-*%<>~^'.includes(c)) return true;
  if (/[A-Za-z_$]/.test(c) || /[\w$]/.test(c)) {
    let end = k;
    while (k >= 0 && /[\w$]/.test(out[k])) k--;
    const word = out.slice(k + 1, end + 1);
    return REGEX_PRECEDING_KEYWORDS.has(word);
  }
  return false;
}

// Sequential scanner: returns the source with comments, string bodies, regex
// bodies, and template-literal TEXT removed, while keeping template
// \${...} interpolation code (that is where the outage lived). Braces inside
// an interpolation are frame-tracked so `${f({x})}` / `${{a:1}[k]}` keep the
// whole interpolation scannable. Newlines are preserved everywhere so
// reported line numbers stay true.
function stripStringsAndComments(text) {
  let out = '';
  let state = 'code'; // code | line | block | single | double | template | regex | regex-class
  const frames = [];  // 'interp' for ${ frames, 'brace' for plain { }
  let i = 0;
  while (i < text.length) {
    const ch = text[i], next = text[i + 1];
    if (state === 'code') {
      if (ch === '/' && next === '/') { state = 'line'; i += 2; continue; }
      if (ch === '/' && next === '*') { state = 'block'; i += 2; continue; }
      if (ch === '/' && startsRegex(out)) { state = 'regex'; i += 1; continue; }
      if (ch === "'") { state = 'single'; out += "''"; i += 1; continue; }
      if (ch === '"') { state = 'double'; out += '""'; i += 1; continue; }
      if (ch === '`') { state = 'template'; i += 1; continue; }
      if (ch === '{') { frames.push('brace'); out += ch; i += 1; continue; }
      if (ch === '}' && frames.length) {
        if (frames.pop() === 'interp') state = 'template';
        out += ch; i += 1; continue;
      }
      out += ch; i += 1; continue;
    }
    if (state === 'line') { if (ch === '\n') { state = 'code'; out += '\n'; } i += 1; continue; }
    if (state === 'block') {
      if (ch === '\n') out += '\n';
      if (ch === '*' && next === '/') { state = 'code'; i += 2; } else i += 1;
      continue;
    }
    if (state === 'single') {
      if (ch === '\\') { i += 2; continue; }
      if (ch === "'" || ch === '\n') { state = 'code'; }
      i += 1; continue;
    }
    if (state === 'double') {
      if (ch === '\\') { i += 2; continue; }
      if (ch === '"' || ch === '\n') { state = 'code'; }
      i += 1; continue;
    }
    if (state === 'regex') {
      // Skip the body; keep newlines so line numbers stay true. Escapes and
      // character classes are honoured so an inner '/' does not end the literal.
      if (ch === '\\') { i += 2; continue; }
      if (ch === '\n') { out += '\n'; i += 1; continue; }
      if (ch === '[') { state = 'regex-class'; i += 1; continue; }
      if (ch === '/') { state = 'code'; i += 1; continue; }
      i += 1; continue;
    }
    if (state === 'regex-class') {
      if (ch === '\\') { i += 2; continue; }
      if (ch === '\n') { out += '\n'; i += 1; continue; }
      if (ch === ']') { state = 'regex'; }
      i += 1; continue;
    }
    // template text: keep newlines for line numbering, detect interpolation
    if (ch === '\\') { i += 2; continue; }
    if (ch === '`') { state = 'code'; i += 1; continue; }
    if (ch === '$' && next === '{') { state = 'code'; frames.push('interp'); out += ' '; i += 2; continue; }
    if (ch === '\n') out += '\n';
    i += 1; continue;
  }
  return out;
}

function definedNames(text) {
  const names = new Set();
  for (const match of text.matchAll(/\b(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)/g)) names.add(match[1]);
  for (const match of text.matchAll(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) names.add(match[1]);
  for (const match of text.matchAll(/\bclass\s+([A-Za-z_$][\w$]*)/g)) names.add(match[1]);
  // Destructuring bindings (const {a, b} = / const [a] =) also create names.
  for (const match of text.matchAll(/\b(?:const|let|var)\s*[{[][^=]*?[}\]]\s*=/g)) {
    for (const id of match[0].matchAll(/([A-Za-z_$][\w$]*)/g)) names.add(id[1]);
  }
  // Conservative masking set: any parameter name anywhere can legally shadow.
  for (const match of text.matchAll(/\(([^()=;{}]*?)\)\s*=>/g)) {
    for (const raw of match[1].split(',')) {
      const id = raw.trim().match(/^[A-Za-z_$][\w$]*/);
      if (id) names.add(id[0]);
    }
  }
  for (const match of text.matchAll(/(?:^|[^\w$.])([A-Za-z_$][\w$]*)\s*=>/g)) names.add(match[1]);
  for (const match of text.matchAll(/(?:async\s+)?function\s*\*?\s*[A-Za-z_$][\w$]*\s*\(([^)]*)\)/g)) {
    for (const raw of match[1].split(',')) {
      const id = raw.trim().match(/^[A-Za-z_$][\w$]*/);
      if (id) names.add(id[0]);
    }
  }
  // Object method shorthand definitions.
  for (const match of text.matchAll(/[{,;]\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/g)) names.add(match[1]);
  return names;
}

function undefinedBareCalls(rawText, extraGlobals = new Set()) {
  const text = stripStringsAndComments(rawText);
  const defined = definedNames(text);
  const flagged = [];
  for (const match of text.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
    const name = match[1];
    if (CONTROL_KEYWORDS.has(name) || JS_AND_BROWSER_GLOBALS.has(name) || extraGlobals.has(name)) continue;
    if (defined.has(name)) continue;
    const before = text.slice(Math.max(0, match.index - 16), match.index);
    if (/(?:async\s+)?function\s+$/.test(before)) continue;
    const line = text.slice(0, match.index).split('\n').length;
    flagged.push(`${name} (app.js line ${line})`);
  }
  return [...new Set(flagged)];
}

test('app.js makes no bare call to an identifier defined nowhere (login outage class)', () => {
  const flagged = undefinedBareCalls(source);
  assert.deepEqual(flagged, [], `dangling bare calls in app.js: ${flagged.join(', ')}`);
});

test('non-browser globals reached via globalThis/window exist in a provider script', () => {
  const providers = ['theme-init.js', 'pci-identity-data.js'].map(name => ({name, text: readStatic(name)}));
  const used = new Set([...source.matchAll(/(?:globalThis|window)\.([A-Za-z_$][\w$]*)/g)].map(match => match[1]));
  for (const name of used) {
    if (BROWSER_GLOBAL_MEMBERS.has(name)) continue;
    const provider = providers.find(candidate => new RegExp(`\\.${name}\\s*=|\\b${name}\\s*:`).test(candidate.text));
    assert.ok(provider, `app.js reaches for global ${name}; it must be a browser member or be assigned by a provider script`);
  }
});

test('index.html serves app.js with the current cache stamp', () => {
  const stamps = [...html.matchAll(/\?v=([0-9.]+-[0-9a-z-]+)\b/g)].map(match => match[1]);
  assert.ok(stamps.length >= 4, `expected the static assets to share one stamp, got ${stamps.join(', ')}`);
  assert.equal(new Set(stamps).size, 1, `all static assets must share one stamp, got ${stamps.join(', ')}`);
});
