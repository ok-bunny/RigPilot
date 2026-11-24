'use strict';
const {constantLoader, functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const source = readStatic();
const html = readStatic('index.html');

const functionSource = functionLoader(source);
const constantSource = constantLoader(source);

const DUAL_PLATFORM_ADAPTERS = [
  {miner_type: 'xmrig', display_name: 'XMRig', platforms: [{os_family: 'windows'}, {os_family: 'linux'}]},
  {miner_type: 'srbminer', display_name: 'SRBMiner-MULTI', platforms: [{os_family: 'windows'}, {os_family: 'linux'}]},
  {miner_type: 'pearlminer', display_name: 'Pearl Miner', platforms: [{os_family: 'windows'}, {os_family: 'linux', asset_variants: ['nvidia', 'amd']}]},
  {miner_type: 'seine', display_name: 'Seine', platforms: [{os_family: 'windows'}, {os_family: 'linux'}]},
];

function dualPlatformHarness({adapters = DUAL_PLATFORM_ADAPTERS, respond = async () => ({})} = {}) {
  const nodes = new Map(), calls = [], notices = [], refreshes = [];
  const node = selector => {
    if (!nodes.has(selector)) {
      const classes = new Set();
      nodes.set(selector, {value: '', textContent: '', disabled: false, required: false, checked: false,
        readOnly: false, open: false, files: [], dataset: {}, innerHTML: '', classList: {
          toggle(key, enabled) { if (enabled) classes.add(key); else classes.delete(key); },
          contains: key => classes.has(key)},
        insertAdjacentHTML() {}, setAttribute() {}, focus() {},
        showModal() { this.open = true; }, close() { this.open = false; }});
    }
    return nodes.get(selector);
  };
  // Mirror the static markup: single file required, dual inputs disabled without required,
  // and the statically hidden sections start with the hidden class.
  node('#minerPackageFile').required = true;
  node('#minerPackageFileWindows').disabled = true;
  node('#minerPackageFileLinux').disabled = true;
  node('#minerPackageDialog').open = true;
  node('input[name="minerPackageSource"]:checked').value = 'upload';
  for (const selector of ['#minerPackageManagedFields', '#minerPackageManagedVariantField', '#minerPackageImportFields',
    '#minerPackageDualPlatformField', '#minerPackageDualFileFields']) node(selector).classList.toggle('hidden', true);
  const checkedRadio = node('input[name="minerPackageSource"]:checked');
  const radio = value => Object.defineProperty({value}, 'checked', {
    get: () => checkedRadio.value === value,
    set: enabled => { if (enabled) checkedRadio.value = value; }});
  const radios = [radio('upload'), radio('import')];
  const state = {minerPackageLimits: null, minerAdapters: undefined};
  const context = vm.createContext({state, $: node, $$: selector => radios, URLSearchParams,
    api: async (path, options) => { calls.push({path, options}); return respond(calls.length, path, options); },
    toast: text => notices.push(text), refreshAll: async () => { refreshes.push(true); },
    fallbackMinerAdapters: adapters, legacyMinerAdapterOrder: new Map(),
  });
  const labelsBlock = source.match(/const MINER_PACKAGE_TYPE_LABELS = Object\.freeze\(\{[\s\S]*?\}\);/);
  assert.ok(labelsBlock, 'actual UI constant MINER_PACKAGE_TYPE_LABELS exists');
  const constants = [constantSource('RETIRED_MINER_LABELS'), constantSource('LARGE_UPLOAD_TIMEOUT_MS'), labelsBlock[0]].join('\n');
  const functions = ['knownPackageBytes', 'formatBytes', 'minerPackageTypeLabel', 'availableMinerAdapters', 'minerAdapterDefinition',
    'minerPackageManagedMetadata', 'syncMinerPackageSource', 'minerPackagePlatformSupported', 'minerPackageDualPlatformAvailable',
    'minerPackageDualPlatformEnabled', 'syncMinerPackageDualPlatform', 'syncMinerPackageClassification', 'renderMinerPackageLimits',
    'validateMinerPackageFilename', 'validateMinerPackageUploadSize', 'createMinerPackage'].map(functionSource).join('\n');
  vm.runInContext(`${constants}\n${functions}`, context);
  return {context, state, node, calls, notices, refreshes, radios,
    selectType(type) { node('#minerPackageManagedType').value = type; context.syncMinerPackageClassification(); },
    fillShared({version = '9.9.9', note = 'dual note', name = 'XMRig'} = {}) {
      node('#minerPackageName').value = name;
      node('#minerPackageVersion').value = version;
      node('#minerPackageNote').value = note;
    },
    setFiles(windowsName, linuxName) {
      node('#minerPackageFileWindows').files = windowsName ? [{name: windowsName, size: 2048}] : [];
      node('#minerPackageFileLinux').files = linuxName ? [{name: linuxName, size: 4096}] : [];
    },
    enableDual() { node('#minerPackageDualPlatform').checked = true; context.syncMinerPackageClassification({syncArchive: false}); },
    submit() { return context.createMinerPackage({preventDefault() {}}); }};
}

test('registration dialog ships a hidden dual-platform toggle and two platform file inputs', () => {
  const dialog = html.match(/<dialog id="minerPackageDialog"[\s\S]*?<\/dialog>/)[0];
  const toggle = dialog.match(/<label id="minerPackageDualPlatformField"[^>]*>/)[0];
  assert.match(toggle, /class="toggle-field hidden"/);
  assert.match(dialog, /<input id="minerPackageDualPlatform" type="checkbox">/);
  const dualFiles = dialog.match(/<div id="minerPackageDualFileFields"[^>]*>/)[0];
  assert.match(dualFiles, /class="inline-grid hidden"/);
  const windows = dialog.match(/<input id="minerPackageFileWindows"[^>]*>/)[0];
  const linux = dialog.match(/<input id="minerPackageFileLinux"[^>]*>/)[0];
  assert.match(windows, /type="file"/); assert.match(windows, /accept="\.zip/); assert.match(windows, /disabled/);
  assert.doesNotMatch(windows, /required/);
  assert.match(linux, /type="file"/); assert.match(linux, /accept="\.tar\.gz/); assert.match(linux, /disabled/);
  assert.doesNotMatch(linux, /required/);
  // Existing single-platform controls keep their ids so the JS can retarget them.
  assert.match(dialog, /<label id="minerPackageManagedOsField">/);
  assert.match(dialog, /<label id="minerPackageArchiveField">/);
  assert.match(dialog, /<label id="minerPackageSingleFileField">/);
});

test('dual-platform entry appears only for categorized dual-platform test packages on local upload', () => {
  const h = dualPlatformHarness();
  assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), true);
  for (const type of ['xmrig', 'srbminer', 'pearlminer', 'seine']) {
    h.selectType(type);
    assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), false, `${type} offers dual entry`);
    h.context.syncMinerPackageDualPlatform();
    assert.equal(h.node('#minerPackageDualPlatform').checked, false, `${type} starts unchecked`);
  }
  h.selectType('hiveos');
  assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), true, 'hiveos is not a dual test package');
  h.selectType('');
  assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), true, 'uncategorized stays single');
  // Switching to HTTPS import hides the dual entry even for dual-platform miners.
  h.selectType('xmrig');
  h.context.syncMinerPackageSource('import');
  assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), true);
  h.context.syncMinerPackageSource('upload');
  assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), false);
});

test('single-platform adapters never expose the dual entry', () => {
  const h = dualPlatformHarness({adapters: [
    {miner_type: 'solo', display_name: 'Solo Miner', platforms: [{os_family: 'windows'}]},
    {miner_type: 'dualcap', display_name: 'Dual Capable', platforms: [{os_family: 'windows'}, {os_family: 'linux'}]},
  ]});
  h.selectType('solo');
  assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), true);
  h.selectType('dualcap');
  assert.equal(h.node('#minerPackageDualPlatformField').classList.contains('hidden'), false);
});

test('enabling dual mode swaps to per-platform files and shares one version, while single mode restores state', () => {
  const h = dualPlatformHarness();
  h.selectType('pearlminer');
  h.node('#minerPackageManagedOsFamily').value = 'windows';
  h.context.syncMinerPackageClassification();
  assert.equal(h.node('#minerPackageManagedVariantField').classList.contains('hidden'), true, 'pearl windows single mode hides variant');
  h.enableDual();
  assert.equal(h.node('#minerPackageManagedOsField').classList.contains('hidden'), true);
  assert.equal(h.node('#minerPackageManagedOsFamily').disabled, true);
  assert.equal(h.node('#minerPackageArchiveField').classList.contains('hidden'), true);
  assert.equal(h.node('#minerPackageArchiveType').disabled, true);
  assert.equal(h.node('#minerPackageSingleFileField').classList.contains('hidden'), true);
  assert.equal(h.node('#minerPackageFile').disabled, true);
  assert.equal(h.node('#minerPackageFile').required, false);
  assert.equal(h.node('#minerPackageDualFileFields').classList.contains('hidden'), false);
  for (const id of ['#minerPackageFileWindows', '#minerPackageFileLinux']) {
    assert.equal(h.node(id).disabled, false);
    assert.equal(h.node(id).required, true);
  }
  assert.equal(h.node('#minerPackageManagedVariantField').classList.contains('hidden'), false, 'pearl linux variant stays selectable in dual mode');
  assert.equal(h.node('#minerPackageManagedVariant').disabled, false);
  // Turning the toggle off restores the single-platform layout.
  h.node('#minerPackageDualPlatform').checked = false;
  h.context.syncMinerPackageClassification({syncArchive: false});
  assert.equal(h.node('#minerPackageManagedOsField').classList.contains('hidden'), false);
  assert.equal(h.node('#minerPackageDualFileFields').classList.contains('hidden'), true);
  assert.equal(h.node('#minerPackageFile').disabled, false);
  assert.equal(h.node('#minerPackageFile').required, true);
  assert.equal(h.node('#minerPackageManagedVariantField').classList.contains('hidden'), true, 'pearl windows single mode hides variant again');
});

test('dual submission posts two uploads sharing one version with per-platform archives', async () => {
  const h = dualPlatformHarness();
  h.selectType('xmrig');
  h.enableDual();
  h.fillShared();
  h.setFiles('xmrig-win.zip', 'xmrig-linux.tar.gz');
  await h.submit();
  assert.equal(h.calls.length, 2);
  const [windows, linux] = h.calls.map(call => Object.fromEntries(new URLSearchParams(call.path.split('?')[1])));
  assert.equal(new URLSearchParams(h.calls[0].path.split('?')[1]).get('managed_os_family'), 'windows');
  assert.equal(windows.archive_type, 'zip');
  assert.equal(windows.filename, 'xmrig-win.zip');
  assert.equal(windows.version, '9.9.9');
  assert.equal(windows.note, 'dual note');
  assert.equal(windows.managed_channel, 'test');
  assert.equal(windows.managed_miner_type, 'xmrig');
  assert.ok(!('managed_variant' in windows), 'xmrig carries no variant');
  assert.equal(linux.managed_os_family, 'linux');
  assert.equal(linux.archive_type, 'tar.gz');
  assert.equal(linux.filename, 'xmrig-linux.tar.gz');
  assert.equal(linux.version, '9.9.9');
  assert.ok(!('managed_variant' in linux), 'xmrig linux upload carries no variant');
  assert.equal(h.calls[0].options.method, 'POST');
  assert.equal(h.calls[0].options.headers['Content-Type'], 'application/octet-stream');
  assert.equal(h.node('#minerPackageDialog').open, false);
  assert.match(h.notices[0], /XMRig 测试版（同版本号 Windows 与 Linux 各一包）/);
  assert.equal(h.refreshes.length, 1);
  assert.equal(h.node('#createMinerPackageButton').disabled, false);
  assert.equal(h.node('#minerPackageError').textContent, '');
});

test('pearl dual submission attaches the chosen variant only to the linux upload', async () => {
  const h = dualPlatformHarness();
  h.selectType('pearlminer');
  h.enableDual();
  h.node('#minerPackageManagedVariant').value = 'amd';
  h.fillShared({name: 'Pearl Miner'});
  h.setFiles('pearl-win.zip', 'pearl-linux.tgz');
  await h.submit();
  assert.equal(h.calls.length, 2);
  const [windows, linux] = h.calls.map(call => Object.fromEntries(new URLSearchParams(call.path.split('?')[1])));
  assert.equal(windows.managed_os_family, 'windows');
  assert.ok(!('managed_variant' in windows), 'windows pearl upload carries no variant');
  assert.equal(linux.managed_os_family, 'linux');
  assert.equal(linux.managed_variant, 'amd');
  assert.equal(linux.filename, 'pearl-linux.tgz');
});

test('partial failure keeps the successful registration and explains each platform', async () => {
  const h = dualPlatformHarness({respond: async index => {
    if (index === 2) throw Object.assign(new Error('XMRig 9.9.9 linux test package is already registered'), {status: 409});
    return {};
  }});
  h.selectType('xmrig'); h.enableDual(); h.fillShared(); h.setFiles('w.zip', 'l.tar.gz');
  await h.submit();
  assert.equal(h.calls.length, 2);
  assert.equal(h.node('#minerPackageDialog').open, true, 'dialog stays open so the failure is visible');
  assert.match(h.node('#minerPackageError').textContent, /Windows 包已登记/);
  assert.match(h.node('#minerPackageError').textContent, /Linux 包未登记：XMRig 9.9.9 linux test package is already registered/);
  assert.match(h.node('#minerPackageError').textContent, /保留有效/);
  assert.equal(h.refreshes.length, 1, 'the registered package is refreshed into the library');
  assert.equal(h.node('#createMinerPackageButton').disabled, false);
  assert.deepEqual(h.notices, []);
});

test('both platforms already registered or both failing report both outcomes honestly', async () => {
  const dual409 = dualPlatformHarness({respond: async () => {
    throw Object.assign(new Error('test package is already registered'), {status: 409});
  }});
  dual409.selectType('xmrig'); dual409.enableDual(); dual409.fillShared(); dual409.setFiles('w.zip', 'l.tar.gz');
  await dual409.submit();
  assert.equal(dual409.calls.length, 2);
  assert.match(dual409.node('#minerPackageError').textContent, /Windows 包未登记：test package is already registered/);
  assert.match(dual409.node('#minerPackageError').textContent, /Linux 包未登记：test package is already registered/);
  assert.doesNotMatch(dual409.node('#minerPackageError').textContent, /Windows 包已登记/);
  assert.equal(dual409.node('#minerPackageDialog').open, true);
  assert.deepEqual(dual409.refreshes, []);
  assert.deepEqual(dual409.notices, []);

  const dualInvalid = dualPlatformHarness({respond: async index => {
    throw Object.assign(new Error(index === 1 ? '请求失败 (413)' : '请求失败 (422)'), {status: index === 1 ? 413 : 422});
  }});
  dualInvalid.selectType('srbminer'); dualInvalid.enableDual(); dualInvalid.fillShared(); dualInvalid.setFiles('w.zip', 'l.tar.gz');
  await dualInvalid.submit();
  assert.equal(dualInvalid.calls.length, 2);
  assert.match(dualInvalid.node('#minerPackageError').textContent, /Windows 包未登记：请求失败 \(413\)/);
  assert.match(dualInvalid.node('#minerPackageError').textContent, /Linux 包未登记：请求失败 \(422\)/);
  assert.equal(dualInvalid.node('#minerPackageDialog').open, true);
});

test('client-side guards reject missing or mismatched dual files without any request', async () => {
  for (const [windowsName, linuxName, message] of [
    ['', 'l.tar.gz', /请选择 Windows \.zip 测试包文件。/],
    ['w.zip', '', /请选择 Linux \.tar\.gz 或 \.tgz 测试包文件。/],
    ['w.tar.gz', 'l.tar.gz', /请选择 \.zip 文件。/],
    ['w.zip', 'l.zip', /请选择 \.tar\.gz 或 \.tgz 文件。/],
  ]) {
    const h = dualPlatformHarness();
    h.selectType('xmrig'); h.enableDual(); h.fillShared(); h.setFiles(windowsName, linuxName);
    await h.submit();
    assert.deepEqual(h.calls, []);
    assert.match(h.node('#minerPackageError').textContent, message);
    assert.equal(h.node('#minerPackageDialog').open, true);
    assert.equal(h.node('#createMinerPackageButton').disabled, false);
  }
});

test('dual submission locks the button and reports per-file progress while uploading', async () => {
  const pending = [];
  const h = dualPlatformHarness({respond: () => new Promise(resolve => pending.push(resolve))});
  h.selectType('xmrig'); h.enableDual(); h.fillShared(); h.setFiles('w.zip', 'l.tar.gz');
  const submission = h.submit();
  assert.equal(h.node('#createMinerPackageButton').disabled, true);
  assert.equal(h.node('#createMinerPackageButton').textContent, '正在上传 1/2（Windows）…');
  pending.shift()({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.node('#createMinerPackageButton').textContent, '正在上传 2/2（Linux）…');
  assert.equal(h.node('#createMinerPackageButton').disabled, true);
  pending.shift()({});
  await submission;
  assert.equal(h.node('#createMinerPackageButton').disabled, false);
  assert.equal(h.node('#createMinerPackageButton').textContent, '');
});

test('single-platform registration keeps its exact previous behavior', async () => {
  const h = dualPlatformHarness();
  h.selectType('xmrig');
  h.node('#minerPackageManagedOsFamily').value = 'windows';
  h.context.syncMinerPackageClassification();
  h.node('#minerPackageName').value = 'XMRig';
  h.node('#minerPackageVersion').value = '8.8.8';
  h.node('#minerPackageNote').value = 'single note';
  h.node('#minerPackageFile').files = [{name: 'single.zip', size: 512}];
  await h.submit();
  assert.equal(h.calls.length, 1);
  const query = Object.fromEntries(new URLSearchParams(h.calls[0].path.split('?')[1]));
  assert.equal(query.managed_os_family, 'windows');
  assert.equal(query.archive_type, 'zip');
  assert.equal(query.filename, 'single.zip');
  assert.equal(query.managed_channel, 'test');
  assert.equal(query.managed_miner_type, 'xmrig');
  assert.equal(h.node('#minerPackageDialog').open, false);
  assert.match(h.notices[0], /已上传并登记，已归类到 XMRig 测试版/);
  assert.equal(h.refreshes.length, 1);
  // Mismatched archive for the chosen platform still fails before any request.
  const rejected = dualPlatformHarness();
  rejected.selectType('xmrig');
  rejected.node('#minerPackageManagedOsFamily').value = 'linux';
  rejected.context.syncMinerPackageClassification();
  rejected.node('#minerPackageName').value = 'XMRig';
  rejected.node('#minerPackageVersion').value = '8.8.8';
  rejected.node('#minerPackageFile').files = [{name: 'single.zip', size: 512}];
  await rejected.submit();
  assert.deepEqual(rejected.calls, []);
  assert.match(rejected.node('#minerPackageError').textContent, /请选择 \.tar\.gz 或 \.tgz 文件。/);
  // The defensive platform/archive guard also fires if the archive value ever disagrees.
  const defensive = dualPlatformHarness();
  defensive.selectType('xmrig');
  defensive.node('#minerPackageManagedOsFamily').value = 'linux';
  defensive.context.syncMinerPackageClassification();
  defensive.node('#minerPackageArchiveType').value = 'zip';
  defensive.node('#minerPackageName').value = 'XMRig';
  defensive.node('#minerPackageVersion').value = '8.8.8';
  defensive.node('#minerPackageFile').files = [{name: 'single.zip', size: 512}];
  await defensive.submit();
  assert.deepEqual(defensive.calls, []);
  assert.match(defensive.node('#minerPackageError').textContent, /Linux 归类测试包必须使用 TAR\.GZ 格式。/);
});
