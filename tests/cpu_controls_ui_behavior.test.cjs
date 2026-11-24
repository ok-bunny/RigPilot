'use strict';
const {readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();
const html = readStatic('index.html');
const bootstrap = html.match(/<script id="minerAdapterBootstrap" type="application\/json">([\s\S]*?)<\/script>/)[1];
const decode = value => String(value ?? '').replaceAll('&quot;', '"').replaceAll('&amp;', '&').replaceAll('&lt;', '<').replaceAll('&gt;', '>');
const attribute = (text, name) => decode(text.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1] || '');
const syntheticSeineAdapter = {miner_type: 'seine', adapter_id: 'org.zpalmtree.seine', display_name: 'Synthetic Seine CPU',
  default_algorithm: 'blocknet-argon2id', coin_defaults: {BNT: 'blocknet-argon2id'}, resource_kinds: ['cpu'],
  algorithms: [{name: 'blocknet-argon2id', resource_kind: 'cpu', supported_resources: ['cpu'], coins: ['BNT']}],
  capabilities: ['cpu', 'native', 'independent_instances'], pool_schemes: ['stratum+tcp'],
  options: [{name: 'cpu_threads', type: 'integer', scope: 'job', minimum: 1, nullable: true, default: null,
    description: '留空自动选择；每个线程约需 2 GiB 内存（合成测试描述）。'},
  {name: 'cpu_profile', type: 'choice', scope: 'job', default: 'balanced', choices: ['balanced', 'throughput', 'efficiency'],
    description: '合成 CPU 配置选项。'}]};
function seinePackage(id, extra = {}) {
  return {id, name: `Synthetic build ${id}`, version: '0.2.15', managed_miner_type: 'seine', managed_os_family: 'linux',
    managed_channel: 'test', source_type: 'uploaded', available: true, sha256: (id.endsWith('a') ? 'a' : 'b').repeat(64), ...extra};
}

function fixture(additionalAdapters = []) {
  const nodes = new Map();
  class Element {
    constructor(id, tag = 'div', attributes = '') {
      this.id = id; this.tag = tag; this.value = attribute(attributes, 'value'); this.initialValue = this.value; this.dataset = {};
      this.initialChecked = this.checked = /\bchecked\b/.test(attributes); this.disabled = /\bdisabled\b/.test(attributes);
      this.max = attribute(attributes, 'max'); this.min = attribute(attributes, 'min');
      this.controls = []; this.options = []; this.listeners = {}; this.textContent = ''; this.firstChild = {nodeType: 3};
      this.hints = id.endsWith('ExtraArgsField') ? [{textContent: ''}, {textContent: ''}] : [];
      const classes = new Set();
      this.classList = {add: item => classes.add(item), remove: item => classes.delete(item), contains: item => classes.has(item),
        toggle: (item, on) => on ? classes.add(item) : classes.delete(item)};
    }
    get children() { return this.controls; }
    get selectedOptions() { return this.options.filter(option => option.value === this.value); }
    set innerHTML(markup) {
      this.markup = markup; this.controls = [];
      this.options = [...markup.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)]
        .map(match => ({value: attribute(match[1], 'value'), selected: /\bselected\b/.test(match[1]), dataset: {coin: attribute(match[1], 'data-coin')}}));
      if (this.tag === 'select') this.value = (this.options.find(option => option.selected) || this.options[0])?.value || '';
      const pattern = /<input\b([^>]*data-adapter-option[^>]*)>|<select\b([^>]*data-adapter-option[^>]*)>([\s\S]*?)<\/select>/g;
      for (const match of markup.matchAll(pattern)) {
        const attrs = match[1] || match[2], control = new Element('', match[2] ? 'select' : 'input', attrs);
        control.dataset = {adapterOption: attribute(attrs, 'data-adapter-option'), optionType: attribute(attrs, 'data-option-type')};
        if (match[2]) control.innerHTML = match[3];
        this.controls.push(control);
      }
    }
    get innerHTML() { return this.markup || ''; }
    insertAdjacentHTML(position, markup) {
      const fragment = new Element('', this.tag); fragment.innerHTML = markup;
      this.options = position === 'afterbegin' ? [...fragment.options, ...this.options] : [...this.options, ...fragment.options];
      this.markup = position === 'afterbegin' ? markup + this.innerHTML : this.innerHTML + markup;
    }
    querySelector(selector) {
      const name = selector.match(/data-adapter-option=["']?([^\]"']+)/)?.[1];
      return name ? this.controls.find(control => control.dataset.adapterOption === name) || null : new Element('hint');
    }
    querySelectorAll(selector) { return selector === '[data-adapter-option]' ? this.controls : selector === '.field-hint' ? this.hints : []; }
    add(option) { this.options.push(option); }
    closest() { return null; }
    setAttribute(name, value) { this[name] = value; }
    reset() {
      for (const item of nodes.values()) { item.value = item.initialValue; item.checked = item.initialChecked; }
    }
    showModal() { this.open = true; }
    replaceChildren() { this.innerHTML = ''; }
    setCustomValidity(message) { this.validityMessage = message; }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    close() { this.open = false; }
  }
  for (const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) nodes.set(match[3], new Element(match[3], match[1], match[2]));
  for (const match of html.matchAll(/<select\b[^>]*\bid="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) nodes.get(match[1]).innerHTML = match[2];
  nodes.get('minerAdapterBootstrap').textContent = bootstrap;
  const node = id => { assert.ok(nodes.has(id), `missing actual HTML field ${id}`); return nodes.get(id); };
  const context = vm.createContext({document: {
    querySelector(selector) {
      if (selector.startsWith('#')) return node(selector.slice(1));
      const name = selector.match(/^\[([\w-]+)\]$/)?.[1];
      const match = name && html.match(new RegExp(`<\\w+\\b[^>]*\\b${name}(?:\\s|[=>])[^>]*>`));
      assert.ok(match, `missing actual HTML selector ${selector}`);
      return new Element(selector, 'button', match[0]);
    }, querySelectorAll: () => [], addEventListener() {},
  }, window: {matchMedia: () => ({matches: false}), addEventListener() {}}, sessionStorage: {getItem: () => ''}, URL, URLSearchParams, Option: function(text, value) { this.textContent = text; this.value = value; this.dataset = {}; }});
  // Define the real app, including its bootstrap parser and helpers. Never fire
  // DOMContentLoaded: no login, polling, API, server or device can be reached.
  vm.runInContext(source, context);
  context.toast = () => {};
  const state = vm.runInContext('state', context);
  state.minerAdapters = additionalAdapters;
  state.managedMiners = [...new Set([...JSON.parse(bootstrap).map(item => item.miner_type), ...additionalAdapters.map(item => item.miner_type)])].map(id => {
    const version = id === 'seine' ? '0.2.15' : '9.9.9';
    return {id, name: id, latest_version: version, versions: [{version, assets: {linux: {filename: 'synthetic.tar.gz'}}}]};
  });
  state.wallets = [{id: 'wallet', coin: 'XMR'}];
  for (const prefix of ['flight', 'flightSecondary']) {
    node(prefix + 'Wallet').options = [{value: 'wallet', dataset: {coin: 'XMR'}}];
    node(prefix + 'Wallet').value = 'wallet'; node(prefix + 'Coin').value = 'XMR';
    node(prefix + 'ExecutionMode').value = 'native'; node(prefix + 'Pool').value = 'stratum+tcp://synthetic.example:3333';
    node(prefix + 'PoolPassword').value = 'x'; node(prefix + 'DeviceIndices').value = '';
  }
  node('flightSecondaryEnabled').checked = false;
  const option = (prefix, name) => node(prefix + 'AdapterOptions').controls.find(control => control.dataset.adapterOption === name);
  function select(prefix, type, algorithm, extra = {}) {
    const typeChanged = node(prefix + 'MinerType').value !== type;
    node(prefix + 'MinerType').value = type; node(prefix + 'Algorithm').value = algorithm;
    context.setManagedExtra(prefix, extra);
    context.syncManagedLeg(prefix, {preserveAlgorithm: true, resetVersion: typeChanged});
  }
  const build = prefix => context.buildManagedLeg(prefix, 'Synthetic CPU test');
  return {context, state, node, select, option, build};
}

test('hashrate roles omit primary/secondary text and retain numbered algorithm identity', () => {
  const h = fixture();
  assert.equal(h.context.deviceHashrateRoleLabel('primary'), '');
  assert.equal(h.context.deviceHashrateRoleLabel('secondary'), '');
  assert.equal(h.context.deviceHashrateRoleLabel('algorithm_2'), '算法 2');
});

for (const prefix of ['flight', 'flightSecondary']) {
  test(`${prefix}: one uploaded test version keeps both platforms and no fixed package ID`, () => {
    const h = fixture();
    const definition = h.state.managedMiners.find(item => item.id === 'xmrig');
    const grouped = {version: '9.0.0', prerelease: true, source_type: 'controller_cache', assets: {
      windows: {package_id: 'test-xmrig-9.0.0-windows-x64', filename: 'custom-win.zip', cached: true, available: true},
      linux: {package_id: 'test-xmrig-9.0.0-linux-x64', filename: 'custom-linux.tar.gz', cached: true, available: true},
    }};
    definition.versions.push(grouped);
    h.select(prefix, 'xmrig', 'randomx');
    assert.equal(h.node(prefix + 'MinerVersion').value, definition.latest_version);
    assert.match(h.context.managedVersionOptionLabel(grouped, 'xmrig'), /9\.0\.0 · 测试版 · Windows \/ Linux/);
    assert.equal(h.node(prefix + 'MinerVersion').options.filter(item => item.value === '9.0.0').length, 1);
    h.node(prefix + 'MinerVersion').value = '9.0.0';
    h.context.syncManagedLeg(prefix, {preserveAlgorithm: true});
    const body = h.build(prefix);
    assert.equal(body.miner_version, '9.0.0');
    assert.equal(body.miner_package_id, null);
    const details = h.node(prefix + 'MinerVersionCompatibility').innerHTML;
    assert.match(details, /同一上传构建版本按目标设备系统分发软件包/);
    assert.match(details, /custom-win\.zip/);
    assert.match(details, /custom-linux\.tar\.gz/);
    assert.doesNotMatch(details, /需明确选择包源|自动选择官方包/);
    delete grouped.assets.linux;
    h.context.renderManagedVersionCompatibility(prefix);
    assert.match(h.node(prefix + 'MinerVersionCompatibility').innerHTML,
      /platform-asset unavailable"><strong>Linux<\/strong><code>此版本无对应安装包/);
    assert.equal(h.build(prefix).miner_version, '9.0.0'); // Keep edit intent; apply validates the actual target.
  });
}


for (const prefix of ['flight', 'flightSecondary']) {
  test(`${prefix}: actual algorithm resource and mixed CPU/GPU choice control CPU fields`, () => {
    const h = fixture(), suffix = prefix === 'flight' ? '' : 'Secondary';
    for (const [type, algorithm, resource, cpu] of [
      ['xmrig', 'randomx', '', true], ['srbminer', 'randomepic', '', true],
      ['srbminer', 'yespower', '', true], ['srbminer', 'yespower', 'cpu', true],
      ['srbminer', 'yespower', 'gpu', false], ['srbminer', 'pearlhash', '', false],
    ]) {
      h.select(prefix, type, algorithm, resource ? {resource_kind: resource, cpu_threads: 12} : {});
      assert.equal(h.context.managedLegUsesCpuRandomX(prefix), cpu);
      assert.equal(h.node(`flight${suffix}CpuRandomxSection`).classList.contains('hidden'), !cpu);
      assert.equal(h.node(prefix + 'CpuThreads').disabled, !cpu);
      assert.equal(h.node(prefix + 'HugePages').disabled, !cpu);
      assert.equal(Boolean(h.option(prefix, 'randomx_1gb_pages')), cpu);
      if (!cpu) assert.equal(h.node(prefix + 'CpuThreads').value, '');
    }
    h.select(prefix, 'srbminer', 'randomx', {resource_kind: 'gpu'});
    assert.throws(() => h.build(prefix), /所选算法不支持此任务资源/);
  });

  test(`${prefix}: thread controls and payload enforce XMRig 256 and SRBMiner schema maximum`, () => {
    const h = fixture();
    for (const [type, maximum] of [['xmrig', 256], ['srbminer', 1024]]) {
      h.select(prefix, type, 'randomx');
      assert.equal(h.node(prefix + 'CpuThreads').max, String(maximum));
      h.node(prefix + 'CpuThreads').value = String(maximum);
      assert.equal(h.build(prefix).extra_config.cpu_threads, maximum);
      for (const invalid of [0, maximum + 1, 1.5]) {
        h.node(prefix + 'CpuThreads').value = String(invalid);
        assert.throws(() => h.build(prefix), /CPU 线程数/);
      }
      h.node(prefix + 'CpuThreads').value = '';
      assert.equal(Object.hasOwn(h.build(prefix).extra_config, 'cpu_threads'), false);
    }
  });

  test(`${prefix}: a synthetic Seine CPU adapter uses its descriptions without unsupported page flags or an invented thread maximum`, () => {
    const h = fixture([syntheticSeineAdapter]);
    h.node(prefix + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
    h.node(prefix + 'Wallet').value = 'seine-wallet';
    h.select(prefix, 'seine', 'blocknet-argon2id', {cpu_threads: 8, huge_pages: true, randomx_1gb_pages: true, cpu_max_threads_hint: 75});
    assert.equal(h.context.managedLegUsesCpuRandomX(prefix), true);
    assert.equal(h.node(prefix + 'CpuRandomxSection').classList.contains('hidden'), false);
    assert.equal(h.node(prefix + 'CpuRandomxHeading').textContent, 'CPU 挖矿参数');
    assert.match(h.node(prefix + 'CpuRandomxHint').textContent, /CPU.*适配器/);
    assert.doesNotMatch(h.node(prefix + 'CpuRandomxHint').textContent, /RandomX|XMRig|SRBMiner/);
    assert.equal(h.node(prefix + 'CpuThreadsHint').textContent, syntheticSeineAdapter.options[0].description);
    assert.equal(h.node(prefix + 'CpuThreads').disabled, false);
    assert.equal(h.node(prefix + 'CpuThreads').max, '');
    assert.equal(h.node(prefix + 'HugePagesField').classList.contains('hidden'), true);
    assert.equal(h.node(prefix + 'HugePages').disabled, true);
    assert.equal(h.node(prefix + 'CpuHintField').classList.contains('hidden'), true);
    assert.equal(h.node(prefix + 'CpuHint').disabled, true);
    assert.equal(h.option(prefix, 'randomx_1gb_pages'), undefined);
    let extra = h.build(prefix).extra_config;
    assert.equal(extra.cpu_threads, 8);
    for (const name of ['huge_pages', 'randomx_1gb_pages', 'cpu_max_threads_hint']) assert.equal(Object.hasOwn(extra, name), false, name);
    for (const threads of ['1', '2048', '65536']) {
      h.node(prefix + 'CpuThreads').value = threads;
      assert.equal(h.build(prefix).extra_config.cpu_threads, Number(threads));
    }
    for (const invalid of ['false', 'true', '0', '-1', '1.5', 'NaN', 'Infinity', '9007199254740992']) {
      h.node(prefix + 'CpuThreads').value = invalid;
      assert.throws(() => h.build(prefix), /CPU 线程数/, invalid);
    }
    h.node(prefix + 'CpuThreads').value = '';
    extra = h.build(prefix).extra_config;
    assert.equal(Object.hasOwn(extra, 'cpu_threads'), false);
    for (const name of ['huge_pages', 'randomx_1gb_pages', 'cpu_max_threads_hint']) assert.equal(Object.hasOwn(extra, name), false, name);
    h.context.setManagedExtra(prefix, {cpu_threads: false});
    assert.equal(h.node(prefix + 'CpuThreads').value, '');
    assert.equal(Object.hasOwn(h.build(prefix).extra_config, 'cpu_threads'), false);
    h.node(prefix + 'Wallet').options = [{value: 'wallet', dataset: {coin: 'XMR'}}];
    h.node(prefix + 'Wallet').value = 'wallet'; h.select(prefix, 'xmrig', 'randomx');
    assert.equal(h.node(prefix + 'HugePagesField').classList.contains('hidden'), false);
    assert.equal(h.node(prefix + 'HugePages').disabled, false);
    assert.equal(h.node(prefix + 'CpuRandomxHeading').textContent, 'CPU / RandomX');
    assert.equal(h.node(prefix + 'CpuThreads').max, '256');
  });

  test(`${prefix}: a synthetic CPU adapter's explicit schema maximum controls both the input and payload`, () => {
    const adapter = {...syntheticSeineAdapter, options: [{...syntheticSeineAdapter.options[0], maximum: 6}]};
    const h = fixture([adapter]);
    h.node(prefix + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
    h.node(prefix + 'Wallet').value = 'seine-wallet';
    h.select(prefix, 'seine', 'blocknet-argon2id');
    assert.equal(h.node(prefix + 'CpuThreads').max, '6');
    h.node(prefix + 'CpuThreads').value = '6';
    assert.equal(h.build(prefix).extra_config.cpu_threads, 6);
    h.node(prefix + 'CpuThreads').value = '7';
    assert.throws(() => h.build(prefix), /CPU 线程数.*1–6/);
  });

  test(`${prefix}: TCP-only catalog schemes drive pool guidance without suggesting SSL or TLS`, () => {
    const h = fixture([syntheticSeineAdapter]);
    h.select(prefix, 'seine', 'blocknet-argon2id');
    assert.match(h.node(prefix + 'Pool').placeholder, /^stratum\+tcp:\/\//);
    assert.match(h.node(prefix + 'PoolHint').innerHTML, /当前矿工仅支持 Stratum TCP/);
    assert.match(h.node(prefix + 'PoolHint').innerHTML, /stratum\+tcp:\/\//);
    assert.doesNotMatch(h.node(prefix + 'PoolHint').innerHTML, /ssl|tls|SSL|TLS/);
    if (prefix === 'flightSecondary') assert.match(h.node(prefix + 'PoolHint').innerHTML, /独立矿工可使用与主矿工不同的矿池/);
    h.select(prefix, 'xmrig', 'randomx');
    assert.match(h.node(prefix + 'Pool').placeholder, /^stratum\+ssl:\/\//);
    assert.match(h.node(prefix + 'PoolHint').innerHTML, prefix === 'flight' ? /自动启用 TLS/ : /独立矿工可使用与主矿工不同的矿池/);
    h.select(prefix, 'srbminer', 'randomx');
    assert.match(h.node(prefix + 'Pool').placeholder, /^stratum\+ssl:\/\//);
    assert.match(h.node(prefix + 'PoolHint').textContent, /保持所选矿工支持的协议与端口/);
    h.select(prefix, 'pearlminer', 'pearlhash');
    assert.equal(h.node(prefix + 'Pool').placeholder, 'stratum+ssl://pool.example.com:443');
    assert.match(h.node(prefix + 'PoolHint').innerHTML, /Pearl Proxy.*stratum\+tcp.*stratum\+ssl/);
  });

  test(`${prefix}: Seine hides and clears stale pool passwords and serializes an empty password without changing the other leg`, () => {
    const h = fixture([syntheticSeineAdapter]), other = prefix === 'flight' ? 'flightSecondary' : 'flight';
    h.node(prefix + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
    h.node(prefix + 'Wallet').value = 'seine-wallet';
    h.node(prefix + 'PoolPassword').value = 'x';
    h.node(other + 'PoolPassword').value = 'other private password';
    h.select(prefix, 'seine', 'blocknet-argon2id');
    assert.equal(h.node(prefix + 'PoolPasswordField').classList.contains('hidden'), true);
    assert.equal(h.node(prefix + 'PoolPassword').value, '');
    assert.equal(h.build(prefix).pool_password, '');
    h.node(prefix + 'PoolPassword').value = 'stale hidden secret';
    assert.equal(h.build(prefix).pool_password, '');
    h.context.syncManagedLeg(prefix, {preserveAlgorithm: true});
    assert.equal(h.node(prefix + 'PoolPassword').value, '');
    assert.equal(h.node(other + 'PoolPassword').value, 'other private password');
    h.node(prefix + 'Wallet').options = [{value: 'wallet', dataset: {coin: 'XMR'}}];
    h.node(prefix + 'Wallet').value = 'wallet'; h.select(prefix, 'xmrig', 'randomx');
    assert.equal(h.node(prefix + 'PoolPasswordField').classList.contains('hidden'), false);
    for (const password of ['', '  explicit password  ']) {
      h.node(prefix + 'PoolPassword').value = password;
      assert.equal(h.build(prefix).pool_password, password);
    }
  });

  test(`${prefix}: generic CPU profile choice saves, refills and clears as a string without changing the other leg`, () => {
    const h = fixture([syntheticSeineAdapter]), other = prefix === 'flight' ? 'flightSecondary' : 'flight';
    for (const role of [prefix, other]) {
      h.node(role + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
      h.node(role + 'Wallet').value = 'seine-wallet';
    }
    h.select(other, 'seine', 'blocknet-argon2id', {cpu_profile: 'efficiency', cpu_threads: 5});
    const otherStore = h.node(other + 'AdapterOptions').dataset.optionValues;
    h.select(prefix, 'seine', 'blocknet-argon2id');
    assert.equal(h.option(prefix, 'cpu_profile').tag, 'select');
    assert.deepEqual(h.option(prefix, 'cpu_profile').options.map(item => item.value), ['', 'balanced', 'throughput', 'efficiency']);
    assert.equal(h.option(prefix, 'cpu_profile').value, 'balanced');
    assert.equal(h.build(prefix).extra_config.cpu_profile, 'balanced');
    for (const value of ['balanced', 'throughput', 'efficiency']) {
      h.option(prefix, 'cpu_profile').value = value;
      const saved = h.build(prefix).extra_config;
      assert.equal(saved.cpu_profile, value);
      assert.equal(typeof saved.cpu_profile, 'string');
      h.select(prefix, 'seine', 'blocknet-argon2id', saved);
      assert.equal(h.option(prefix, 'cpu_profile').value, value);
      assert.equal(h.node(other + 'AdapterOptions').dataset.optionValues, otherStore);
      assert.equal(h.option(other, 'cpu_profile').value, 'efficiency');
    }
    h.option(prefix, 'cpu_profile').value = '';
    const cleared = h.build(prefix).extra_config;
    assert.equal(Object.hasOwn(cleared, 'cpu_profile'), false);
    assert.equal(h.node(other + 'AdapterOptions').dataset.optionValues, otherStore);
    assert.equal(h.build(other).extra_config.cpu_profile, 'efficiency');
    assert.equal(h.build(other).extra_config.cpu_threads, 5);
    h.select(prefix, 'seine', 'blocknet-argon2id', cleared);
    assert.equal(h.option(prefix, 'cpu_profile').value, 'balanced');
  });

  test(`${prefix}: Seine package source only lists matching uploaded test builds and preserves explicit save, refill and clear`, () => {
    const h = fixture([syntheticSeineAdapter]);
    h.state.minerPackages = [seinePackage('build-a', {name: 'CPU build <unsafe>'}), seinePackage('build-b', {managed_os_family: 'windows'}),
      seinePackage('wrong-version', {version: '0.2.16'}), seinePackage('wrong-type', {managed_miner_type: 'srbminer'}),
      seinePackage('unavailable', {available: false}), seinePackage('not-uploaded', {source_type: 'url'}),
      seinePackage('not-test', {managed_channel: 'stable'}), seinePackage('no-sha', {sha256: 'a'.repeat(63)}),
      seinePackage('no-platform', {managed_os_family: null}), seinePackage('wrong-variant', {managed_variant: 'cuda'})];
    h.node(prefix + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
    h.node(prefix + 'Wallet').value = 'seine-wallet'; h.select(prefix, 'seine', 'blocknet-argon2id');
    const select = h.node(prefix + 'NativePackageId');
    assert.equal(select.value, '');
    assert.equal(select.disabled, false);
    assert.equal(h.node(prefix + 'NativePackageField').classList.contains('hidden'), false);
    assert.match(h.node(prefix + 'NativePackageHint').textContent, /0\.2\.15 官方 Linux 包需要更高 GLIBC.*Ubuntu 22\.04（GLIBC 2\.35）.*仅选择已验证/);
    assert.deepEqual(select.options.map(item => item.value), ['', 'build-a', 'build-b']);
    assert.match(select.innerHTML, /上传构建（非官方）.*Linux.*CPU build &lt;unsafe&gt;.*aaaaaaaaaaaa/);
    assert.match(select.innerHTML, /Windows/);
    select.value = 'build-a'; h.context.renderManagedVersionCompatibility(prefix);
    const saved = h.build(prefix);
    assert.equal(saved.miner_package_id, 'build-a');
    const details = h.node(prefix + 'MinerVersionCompatibility').innerHTML;
    assert.match(details, /固定上传构建（非官方）/);
    assert.ok(details.includes('a'.repeat(64)));
    assert.match(details, /CPU build &lt;unsafe&gt;.*Linux|Linux.*CPU build &lt;unsafe&gt;/);
    assert.doesNotMatch(details, /<unsafe>|已验证兼容/);
    h.context.populateSeineFlightPackageSelect(prefix, '');
    h.context.populateSeineFlightPackageSelect(prefix, saved.miner_package_id);
    assert.equal(select.value, 'build-a');
    assert.equal(h.build(prefix).miner_package_id, 'build-a');
    h.state.flightHistoricalReadOnly = true; h.context.populateSeineFlightPackageSelect(prefix);
    assert.equal(select.disabled, true);
    assert.equal(select.value, 'build-a');
    h.state.flightHistoricalReadOnly = false; h.context.populateSeineFlightPackageSelect(prefix);
    select.value = ''; h.context.renderManagedVersionCompatibility(prefix);
    assert.equal(h.build(prefix).miner_package_id, null);
    assert.doesNotMatch(h.node(prefix + 'MinerVersionCompatibility').innerHTML, /固定上传构建|aaaaaaaaaaaa/);
  });

  test(`${prefix}: missing or invalid Seine pins remain explicit and block saving, while miner/version changes clear stale pins`, () => {
    const h = fixture([syntheticSeineAdapter]);
    h.state.minerPackages = [seinePackage('build-a')];
    h.node(prefix + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
    h.node(prefix + 'Wallet').value = 'seine-wallet'; h.select(prefix, 'seine', 'blocknet-argon2id');
    const select = h.node(prefix + 'NativePackageId');
    select.value = 'build-a'; h.state.minerPackages[0].available = false;
    h.context.populateSeineFlightPackageSelect(prefix); h.context.renderManagedVersionCompatibility(prefix);
    assert.equal(select.value, 'build-a');
    assert.equal(select.dataset.packageMissing, 'true');
    assert.match(select.innerHTML, /绑定上传构建不可用/);
    assert.match(h.node(prefix + 'MinerVersionCompatibility').innerHTML, /不会静默退回官方包/);
    assert.throws(() => h.build(prefix), /绑定的上传构建不可用/);
    h.context.populateSeineFlightPackageSelect(prefix, 'missing-id');
    assert.equal(select.value, 'missing-id');
    assert.throws(() => h.build(prefix), /不会自动改用官方包/);
    h.state.minerPackages[0].available = true;
    h.context.populateSeineFlightPackageSelect(prefix, 'build-a');
    h.state.managedMiners.find(item => item.id === 'seine').versions.push({version: '0.2.16', assets: {linux: {filename: 'synthetic-new.tar.gz'}}});
    h.node(prefix + 'MinerVersion').value = '0.2.16'; h.context.syncManagedLeg(prefix, {preserveAlgorithm: true});
    assert.equal(select.value, '');
    assert.doesNotMatch(h.node(prefix + 'NativePackageHint').textContent, /2\.39|2\.35/);
    assert.equal(h.build(prefix).miner_package_id, null);
    h.node(prefix + 'MinerVersion').value = '0.2.15'; h.context.syncManagedLeg(prefix, {preserveAlgorithm: true});
    h.context.populateSeineFlightPackageSelect(prefix, 'build-a');
    h.node(prefix + 'Wallet').options = [{value: 'wallet', dataset: {coin: 'XMR'}}];
    h.node(prefix + 'Wallet').value = 'wallet'; h.select(prefix, 'xmrig', 'randomx');
    assert.equal(select.value, '');
    assert.equal(select.disabled, true);
    assert.equal(h.node(prefix + 'NativePackageField').classList.contains('hidden'), true);
    assert.equal(h.build(prefix).miner_package_id, null);
  });

  test(`${prefix}: Seine uploaded groups save without a pin and win over same-platform official assets`, () => {
    const h = fixture();
    assert.equal(h.context.minerAdapterDefinition('seine').adapter_id, 'org.zpalmtree.seine');
    const id = 'test-seine-0.2.16-linux-x64';
    const testAsset = {os_family: 'linux', filename: 'seine-0.2.16-compatible.tar.gz', archive_type: 'tar.gz',
      package_id: id, source_type: 'controller_cache', variant: null, cached: true, available: true};
    const testVersion = {version: '0.2.16', prerelease: true, source_type: 'controller_cache', assets: {linux: testAsset}};
    const definition = h.state.managedMiners.find(item => item.id === 'seine');
    definition.latest_version = '0.2.15'; definition.versions.push(testVersion);
    h.state.minerPackages = [seinePackage(id, {version: '0.2.16'})];
    h.node(prefix + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
    h.node(prefix + 'Wallet').value = 'seine-wallet'; h.select(prefix, 'seine', 'blocknet-argon2id');
    h.node(prefix + 'MinerVersion').value = '0.2.16'; h.context.syncManagedLeg(prefix, {preserveAlgorithm: true});
    assert.equal(h.node(prefix + 'NativePackageId').value, '');
    assert.equal(h.context.managedVersionIsAvailable('seine', '0.2.16'), true);
    assert.match(h.context.managedVersionOptionLabel(testVersion, 'seine'), /上传构建/);
    assert.match(h.node(prefix + 'NativePackageId').innerHTML, /按目标平台自动选择.*上传构建/);
    assert.match(h.node(prefix + 'NativePackageHint').textContent, /按目标设备系统选择该版本已上传.*不会自动改用官方包/);
    assert.equal(h.node(prefix + 'MinerVersionCompatibility').classList.contains('warning'), false);
    let details = h.node(prefix + 'MinerVersionCompatibility').innerHTML;
    assert.match(details, /同一上传构建版本按目标设备系统分发软件包.*不会自动改用官方包/);
    assert.match(details, /platform-asset available"><strong>Linux<\/strong>.*上传构建（非官方）/);
    assert.match(details, /platform-asset unavailable"><strong>Windows<\/strong>.*缺少此平台上传构建；不会自动改用官方包/);
    assert.equal(h.build(prefix).miner_package_id, null);
    h.node(prefix + 'NativePackageId').value = id; h.context.renderManagedVersionCompatibility(prefix);
    assert.equal(h.build(prefix).miner_package_id, id);
    assert.match(h.node(prefix + 'MinerVersionCompatibility').innerHTML, /固定上传构建（非官方）/);
    h.node(prefix + 'NativePackageId').value = ''; h.context.renderManagedVersionCompatibility(prefix);
    assert.equal(h.build(prefix).miner_package_id, null);
    testVersion.assets.windows = {os_family: 'windows', filename: 'seine-0.2.16-official.zip',
      package_id: 'builtin-seine-0.2.16-windows-x64', source_type: 'controller_cache', cached: true, available: true};
    assert.equal(h.context.managedVersionIsAvailable('seine', '0.2.16'), true);
    h.context.renderManagedVersionCompatibility(prefix); details = h.node(prefix + 'MinerVersionCompatibility').innerHTML;
    assert.match(details, /platform-asset unavailable"><strong>Windows<\/strong>.*缺少此平台上传构建/);
    assert.match(details, /platform-asset available"><strong>Linux<\/strong>.*上传构建（非官方）/);
    testVersion.assets.linux = {...testAsset, package_id: 'builtin-seine-0.2.16-linux-x64'};
    delete testVersion.assets.windows;
    assert.equal(h.context.managedVersionIsAvailable('seine', '0.2.16'), true);
    h.context.renderManagedVersionCompatibility(prefix); details = h.node(prefix + 'MinerVersionCompatibility').innerHTML;
    assert.match(details, /platform-asset available"><strong>Linux<\/strong>/);
    assert.match(details, /platform-asset available"><strong>Linux<\/strong>.*上传构建（非官方）/);
    assert.equal(h.build(prefix).miner_package_id, null);
    testVersion.assets.linux.available = false;
    assert.equal(h.context.managedVersionIsAvailable('seine', '0.2.16'), true);
  });

  test(`${prefix}: CPU scalars, independent page flags and numeric schema choices retain their types`, () => {
    const h = fixture();
    h.select(prefix, 'xmrig', 'randomx', {cpu_threads: 8, cpu_max_threads_hint: 75, huge_pages: false,
      randomx_1gb_pages: true, pause_on_battery: true, keepalive: false});
    const xmrig = h.build(prefix);
    assert.equal(xmrig.algorithm, null);
    assert.deepEqual(JSON.parse(JSON.stringify(xmrig.extra_config)), {cpu_threads: 8, cpu_max_threads_hint: 75,
      huge_pages: false, randomx_1gb_pages: true, pause_on_battery: true, keepalive: false, extra_args: ""});
    h.select(prefix, 'srbminer', 'yespower', {cpu_threads_reduce: 0, cpu_threads_intensity: 4, huge_pages: true,
      randomx_1gb_pages: false, extended_log: false});
    const srb = h.build(prefix).extra_config;
    assert.equal(srb.cpu_threads_reduce, 0); assert.equal(srb.cpu_threads_intensity, 4);
    assert.equal(srb.huge_pages, true); assert.equal(srb.randomx_1gb_pages, false);
    assert.equal(srb.extended_log, false); assert.equal(Object.hasOwn(srb, 'cpu_max_threads_hint'), false);
    h.node(prefix + 'CpuThreads').value = '2';
    assert.throws(() => h.build(prefix), /不能同时设置/);
  });

  test(`${prefix}: GPU choice omits stale CPU options but retains common GPU job options`, () => {
    const h = fixture();
    h.select(prefix, 'srbminer', 'yespower', {resource_kind: 'gpu', cpu_threads: 8, huge_pages: false,
      randomx_1gb_pages: true, cpu_threads_reduce: 0, cpu_threads_intensity: 4, extended_log: true});
    const extra = h.build(prefix).extra_config;
    for (const name of ['cpu_threads', 'cpu_max_threads_hint', 'huge_pages', 'randomx_1gb_pages',
      'pause_on_battery', 'cpu_threads_reduce', 'cpu_threads_intensity']) assert.equal(Object.hasOwn(extra, name), false, name);
    assert.equal(extra.resource_kind, 'gpu'); assert.equal(extra.keepalive, true); assert.equal(extra.extended_log, true);
  });

  test(`${prefix}: stored values and reset cannot be overwritten by the previous rendered controls`, () => {
    const h = fixture(), other = prefix === 'flight' ? 'flightSecondary' : 'flight';
    h.select(other, 'srbminer', 'randomx', {cpu_threads: 9, cpu_threads_reduce: 0});
    const otherStore = h.node(other + 'AdapterOptions').dataset.optionValues;
    h.select(prefix, 'xmrig', 'randomx', {cpu_threads: 4, huge_pages: false, randomx_1gb_pages: true});
    h.option(prefix, 'randomx_1gb_pages').checked = false;
    h.context.setManagedExtra(prefix, {cpu_threads: 6, huge_pages: false, randomx_1gb_pages: true});
    h.context.syncManagedLeg(prefix, {preserveAlgorithm: true});
    assert.equal(h.build(prefix).extra_config.cpu_threads, 6);
    assert.equal(h.option(prefix, 'randomx_1gb_pages').checked, true);
    h.context.setManagedExtra(prefix, {cpu_threads: '12', cpu_max_threads_hint: '75'});
    assert.equal(h.node(prefix + 'CpuThreads').value, '');
    assert.equal(h.node(prefix + 'CpuHint').value, '100');
    h.state.flightMinerConfigPrefix = prefix;
    h.context.resetActiveFlightMinerConfig();
    const reset = h.build(prefix).extra_config;
    assert.equal(Object.hasOwn(reset, 'cpu_threads'), false);
    assert.equal(reset.huge_pages, true); assert.equal(reset.randomx_1gb_pages, false);
    assert.equal(reset.cpu_max_threads_hint, 100); assert.equal(reset.pause_on_battery, false);
    assert.equal(h.node(prefix + 'ExecutionMode').value, 'native'); assert.equal(h.node(prefix + 'DeviceIndices').value, '');
    assert.equal(h.node(prefix + 'PoolPassword').value, 'x');
    h.select(prefix, 'srbminer', 'yespower', {cpu_threads_reduce: 7, cpu_threads_intensity: 4,
      randomx_1gb_pages: true, extended_log: true});
    h.context.resetActiveFlightMinerConfig();
    const srbReset = h.build(prefix).extra_config;
    assert.equal(Object.hasOwn(srbReset, 'cpu_threads_reduce'), false);
    assert.equal(Object.hasOwn(srbReset, 'cpu_threads_intensity'), false);
    assert.equal(srbReset.huge_pages, true); assert.equal(srbReset.randomx_1gb_pages, false);
    assert.equal(srbReset.extended_log, false);
    assert.equal(h.node(prefix + 'Algorithm').value, 'randomx');
    assert.equal(h.node(other + 'AdapterOptions').dataset.optionValues, otherStore);
  });

  test(`${prefix}: schema rendering drops stored raw/MSR keys without treating them as CPU options`, () => {
    const h = fixture();
    h.select(prefix, 'srbminer', 'randomx', {raw_args: '--unsafe', rdmsr: 1, wrmsr: true, cpu_threads_reduce: 0});
    const extra = h.build(prefix).extra_config;
    for (const name of ['raw_args', 'rdmsr', 'wrmsr']) {
      assert.equal(h.option(prefix, name), undefined);
      assert.equal(Object.hasOwn(extra, name), false);
    }
    assert.equal(extra.cpu_threads_reduce, 0);
  });
}

test('Seine main and secondary package pins stay independent and uploaded identity is visible on flight cards', () => {
  const h = fixture([syntheticSeineAdapter]);
  h.state.minerPackages = [seinePackage('build-a'), seinePackage('build-b')];
  for (const prefix of ['flight', 'flightSecondary']) {
    h.node(prefix + 'Wallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
    h.node(prefix + 'Wallet').value = 'seine-wallet'; h.select(prefix, 'seine', 'blocknet-argon2id');
  }
  h.node('flightNativePackageId').value = 'build-a'; h.node('flightSecondaryNativePackageId').value = 'build-b';
  assert.equal(h.build('flight').miner_package_id, 'build-a');
  assert.equal(h.build('flightSecondary').miner_package_id, 'build-b');
  h.state.flightSheets = [{id: 'pin-card', name: 'Pinned build', ...h.build('flight'), secondary: h.build('flightSecondary')}];
  h.context.setTableVisible = () => {}; h.node('flightTableBody').closest = () => h.node('flightTableBody');
  h.context.renderFlightSheets();
  assert.equal([...h.node('flightTableBody').innerHTML.matchAll(/<code[^>]*>[^<]*上传构建<\/code>/g)].length, 2);
  h.node('flightNativePackageId').value = '';
  assert.equal(h.build('flight').miner_package_id, null);
  assert.equal(h.build('flightSecondary').miner_package_id, 'build-b');
});

test('a valid Seine uploaded pin is independent of official asset availability and cannot silently become official', () => {
  const h = fixture([syntheticSeineAdapter]);
  h.state.minerPackages = [seinePackage('build-a')];
  h.node('flightWallet').options = [{value: 'seine-wallet', dataset: {coin: 'BNT'}}];
  h.node('flightWallet').value = 'seine-wallet'; h.select('flight', 'seine', 'blocknet-argon2id');
  h.context.managedVersionIsAvailable = () => false;
  h.node('flightNativePackageId').value = 'build-a';
  const pinned = h.build('flight');
  assert.equal(pinned.miner_package_id, 'build-a');
  assert.equal(h.context.flightSheetVersionIssue(pinned), '');
  h.state.minerPackages = [];
  assert.match(h.context.flightSheetVersionIssue(pinned), /绑定的 Seine 上传构建/);
  assert.throws(() => h.build('flight'), /不会自动改用官方包/);
  h.node('flightNativePackageId').value = '';
  assert.throws(() => h.build('flight'), /当前不可下载或未在控制端缓存/);
});

test('valid Seine uploaded pins still require a supported adapter on either leg', () => {
  const h = fixture([syntheticSeineAdapter]);
  h.state.minerPackages = [seinePackage('build-a')];
  const lookup = h.context.minerAdapterDefinition;
  h.context.minerAdapterDefinition = type => type === 'seine' ? null : lookup(type);
  const pinned = {miner_type: 'seine', miner_version: '0.2.15', miner_package_id: 'build-a'};
  assert.match(h.context.flightSheetVersionIssue(pinned), /已移除或不受支持/);
  assert.match(h.context.flightSheetVersionIssue({miner_type: 'xmrig', miner_version: '9.9.9', secondary: pinned}), /第二矿工已移除或不受支持/);
});




// Reuse the CPU controls fixture for actual create/edit flow; only seed each
// role's synthetic catalog and wallet/package identities and capture API calls.
function dualCpuEditorFixture() {
  const h = fixture();
  h.state.managedMiners.forEach(definition => {
    const versions = definition.id === 'seine' ? ['0.2.15'] : definition.id === 'srbminer' ? ['3.7.2', '3.7.1'] : ['6.26.0', '6.25.0'];
    definition.latest_version = versions[0];
    definition.versions = versions.map(version => ({version, assets: {linux: {filename: 'synthetic.tar.gz'}}}));
  });
  h.state.wallets = [
    {id: 'primary-xmr', coin: 'XMR', name: 'Primary XMR'}, {id: 'secondary-xmr', coin: 'XMR', name: 'Secondary XMR'},
    {id: 'primary-bnt', coin: 'BNT', name: 'Primary BNT'}, {id: 'secondary-bnt', coin: 'BNT', name: 'Secondary BNT'},
  ];
  h.state.user = {role: 'admin'};
  h.state.minerPackages = [
    seinePackage('seine-primary', {sha256: 'a'.repeat(64)}),
    seinePackage('seine-secondary', {sha256: 'b'.repeat(64)}),
  ];
  const calls = [];
  h.context.refreshAll = async () => {};
  h.context.api = async (endpoint, options) => {
    calls.push({endpoint, method: options.method, body: JSON.parse(JSON.stringify(options.body))});
    return {id: 'synthetic-saved-sheet'};
  };
  return {...h, calls};
}

function configureDualCpuLeg(h, prefix, type, role, {automaticThreads = false} = {}) {
  const coin = type === 'seine' ? 'BNT' : 'XMR';
  h.node(prefix + 'Wallet').value = `${role}-${coin.toLowerCase()}`;
  h.node(prefix + 'Coin').value = coin;
  h.select(prefix, type, type === 'seine' ? 'blocknet-argon2id' : 'randomx', {
    cpu_threads: role === 'primary' ? 3 : 5,
    cpu_profile: role === 'primary' ? 'efficiency' : 'throughput',
    extra_args: `  --${role}-option "${role} value"  `,
  });
  h.node(prefix + 'MinerVersion').value = type === 'seine' ? '0.2.15'
    : type === 'srbminer' ? (role === 'primary' ? '3.7.2' : '3.7.1')
    : (role === 'primary' ? '6.26.0' : '6.25.0');
  h.node(prefix + 'Pool').value = `stratum+tcp://${role}.example.test:${role === 'primary' ? 3333 : 4444}`;
  h.node(prefix + 'PoolPassword').value = role === 'primary' ? 'primary-secret' : '';
  h.node(prefix + 'CpuThreads').value = automaticThreads ? '' : (role === 'primary' ? '3' : '5');
  if (type === 'seine') {
    h.context.populateSeineFlightPackageSelect(prefix, `seine-${role}`);
    h.node(prefix + 'NativePackageId').value = `seine-${role}`;
  }
}

function assertDualCpuLeg(actual, type, role, automaticThreads = false) {
  assert.equal(actual.miner_type, type);
  assert.equal(actual.miner_version, type === 'seine' ? '0.2.15'
    : type === 'srbminer' ? (role === 'primary' ? '3.7.2' : '3.7.1')
    : (role === 'primary' ? '6.26.0' : '6.25.0'));
  assert.equal(actual.wallet_id, `${role}-${type === 'seine' ? 'bnt' : 'xmr'}`);
  assert.equal(actual.pool_url, `stratum+tcp://${role}.example.test:${role === 'primary' ? 3333 : 4444}`);
  assert.equal(actual.pool_password, type === 'seine' ? '' : (role === 'primary' ? 'primary-secret' : ''));
  assert.equal(actual.execution_mode, 'native');
  assert.equal(actual.miner_package_id, type === 'seine' ? `seine-${role}` : null);
  if (type === 'seine') assert.equal(actual.extra_config.cpu_profile, role === 'primary' ? 'efficiency' : 'throughput');
  else assert.equal(Object.hasOwn(actual.extra_config, 'cpu_profile'), false, 'only schema-supported CPU profile is serialized');
  assert.equal(Object.hasOwn(actual.extra_config, 'cpu_threads'), !automaticThreads);
  if (!automaticThreads) assert.equal(actual.extra_config.cpu_threads, role === 'primary' ? 3 : 5);
  assert.equal(actual.extra_config.extra_args, `  --${role}-option "${role} value"  `);
  assert.deepEqual(actual.device_indices, []);
}

for (const [primaryType, secondaryType] of [
  ['xmrig', 'seine'], ['seine', 'xmrig'], ['xmrig', 'xmrig'], ['seine', 'seine'], ['srbminer', 'srbminer'],
]) {
  test(`${primaryType} + ${secondaryType}: independent CPU create and edit preserve each role`, async () => {
    const h = dualCpuEditorFixture();
    h.context.openFlightDialog();
    h.node('flightName').value = 'Two independent CPU miners';
    configureDualCpuLeg(h, 'flight', primaryType, 'primary');
    h.context.setSecondaryEnabled(true);
    h.node('flightSecondaryMode').value = 'independent';
    configureDualCpuLeg(h, 'flightSecondary', secondaryType, 'secondary');
    h.context.syncSecondaryMode({preserveAlgorithm: true, selectedVersion: h.node('flightSecondaryMinerVersion').value});
    assert.equal(h.node('flightSecondaryMinerType').disabled, false);
    assert.equal(h.node('flightSecondaryMinerVersion').disabled, false);
    assert.equal(h.context.managedLegResource('flight'), 'cpu');
    assert.equal(h.context.managedLegResource('flightSecondary'), 'cpu');
    await h.context.saveFlightSheet();
    assert.equal(h.node('flightError').textContent, '');
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].method, 'POST');
    assert.equal(h.calls[0].endpoint, '/api/flight-sheets');
    const first = h.calls[0].body;
    assert.equal(first.secondary_mode, 'independent');
    assert.equal(first.gpu_oc, null);
    assertDualCpuLeg(first, primaryType, 'primary');
    assertDualCpuLeg(first.secondary, secondaryType, 'secondary');
    h.state.flightSheets = [{id: 'dual-cpu-sheet', ...first}];
    h.context.openFlightDialog('dual-cpu-sheet');
    h.node('flightName').value = 'Edited name only';
    await h.context.saveFlightSheet();
    assert.equal(h.node('flightError').textContent, '');
    assert.equal(h.calls.length, 2);
    assert.equal(h.calls[1].method, 'PUT');
    assert.equal(h.calls[1].endpoint, '/api/flight-sheets/dual-cpu-sheet');
    assert.equal(h.calls[1].body.name, 'Edited name only');
    assertDualCpuLeg(h.calls[1].body, primaryType, 'primary');
    assertDualCpuLeg(h.calls[1].body.secondary, secondaryType, 'secondary');
    assert.equal(JSON.stringify(h.state.flightSheets[0]), JSON.stringify({id: 'dual-cpu-sheet', ...first}), 'editor does not mutate stored source');
  });
}

test('independent CPU threads may remain automatic in both roles without a new shared limit', async () => {
  const h = dualCpuEditorFixture(); h.context.openFlightDialog();
  h.node('flightName').value = 'Automatic independent CPU threads';
  configureDualCpuLeg(h, 'flight', 'xmrig', 'primary', {automaticThreads: true});
  h.context.setSecondaryEnabled(true); h.node('flightSecondaryMode').value = 'independent';
  configureDualCpuLeg(h, 'flightSecondary', 'seine', 'secondary', {automaticThreads: true});
  await h.context.saveFlightSheet();
  assert.equal(h.node('flightError').textContent, ''); assert.equal(h.calls.length, 1);
  assertDualCpuLeg(h.calls[0].body, 'xmrig', 'primary', true);
  assertDualCpuLeg(h.calls[0].body.secondary, 'seine', 'secondary', true);
});

test('SRBMiner integrated RandomX + RandomX remains rejected before saving', async () => {
  const h = dualCpuEditorFixture(); h.context.openFlightDialog();
  configureDualCpuLeg(h, 'flight', 'srbminer', 'primary');
  h.context.setSecondaryEnabled(true); h.node('flightSecondaryMode').value = 'integrated';
  configureDualCpuLeg(h, 'flightSecondary', 'srbminer', 'secondary');
  await h.context.saveFlightSheet();
  assert.equal(h.calls.length, 0); assert.match(h.node('flightError').textContent, /两个 RandomX/);
});

test('GPU index syntax stays validated before an independent flight is saved', async () => {
  const h = dualCpuEditorFixture(); h.context.openFlightDialog();
  configureDualCpuLeg(h, 'flight', 'xmrig', 'primary'); h.context.setSecondaryEnabled(true);
  h.node('flightSecondaryMode').value = 'independent';
  h.node('flightSecondaryWallet').value = 'secondary-xmr';
  h.select('flightSecondary', 'srbminer', 'yespower', {resource_kind: 'gpu'});
  h.node('flightSecondaryDeviceIndices').value = '0,-1';
  h.node('flightSecondaryPool').value = 'stratum+tcp://gpu.example.test:5555';
  await h.context.saveFlightSheet();
  assert.equal(h.calls.length, 0); assert.match(h.node('flightError').textContent, /GPU 索引/);
});


// Preserve the current-miner cases formerly located in the withdrawn adapter's
// suite, using this module's existing editor fixture.
function independentGpuEditorFixture() {
  const h = fixture(), calls = [];
  h.state.wallets = [{id: 'quantus-wallet', coin: 'QUANTUS'}, {id: 'pearl-wallet', coin: 'PEARL'}];
  h.context.api = async (endpoint, options) => { calls.push({endpoint, options}); return {id: 'saved'}; };
  h.context.refreshAll = async () => {};
  h.context.openFlightDialog();
  h.node('flightName').value = 'Two independent GPU miners';
  h.node('flightWorker').value = '%WORKER_NAME%';
  h.node('flightWallet').value = 'quantus-wallet'; h.node('flightCoin').value = 'QUANTUS';
  h.select('flight', 'srbminer', 'quantus');
  h.context.setSecondaryEnabled(true); h.node('flightSecondaryMode').value = 'independent';
  h.node('flightSecondaryWallet').value = 'pearl-wallet'; h.node('flightSecondaryCoin').value = 'PEARL';
  h.select('flightSecondary', 'pearlminer', 'pearlhash', {extra_args: '--secondary "literal value"'});
  h.node('flightDeviceIndices').value = '0'; h.node('flightSecondaryDeviceIndices').value = '1';
  for (const prefix of ['flight', 'flightSecondary']) {
    h.node(prefix + 'Pool').value = 'stratum+tcp://synthetic.example:3333';
    h.node(prefix + 'PoolPassword').value = 'x';
  }
  h.context.syncSecondaryMode({preserveAlgorithm: true});
  return {...h, calls};
}

test('existing independent SRBMiner GPU + Pearl GPU miners remain saveable', async () => {
  const h = independentGpuEditorFixture();
  assert.doesNotMatch(h.node('flightSecondaryModeHint').textContent, /CPU 伙伴/);
  await h.context.saveFlightSheet();
  assert.equal(h.calls.length, 1, h.node('flightError').textContent);
  const body = h.calls[0].options.body;
  assert.equal(body.secondary_mode, 'independent');
  assert.equal(body.miner_type, 'srbminer'); assert.equal(body.secondary.miner_type, 'pearlminer');
  assert.deepEqual(Array.from(body.device_indices), [0]);
  assert.deepEqual(Array.from(body.secondary.device_indices), [], 'Pearl does not advertise device_selection');
});

test('disabling the second leg preserves its unsaved current-miner values', async () => {
  const h = independentGpuEditorFixture();
  const snapshot = ['MinerType', 'Algorithm', 'DeviceIndices', 'ExtraArgs'].map(suffix => h.node('flightSecondary' + suffix).value);
  h.node('flightSecondaryEnabled').checked = false; h.context.syncSecondaryMode();
  assert.deepEqual(['MinerType', 'Algorithm', 'DeviceIndices', 'ExtraArgs'].map(suffix => h.node('flightSecondary' + suffix).value), snapshot);
  await h.context.saveFlightSheet();
  assert.equal(h.calls.length, 1, h.node('flightError').textContent);
  assert.equal(h.calls[0].options.body.secondary, null);
});
