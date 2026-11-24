'use strict';
const {functionLoader, readStatic} = require('./helpers/controller_ui.cjs');
const assert = require('node:assert/strict');
const test = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const source = readStatic();
const asset = readStatic('pci-identity-data.js');
const datasetBytes = fs.readFileSync(path.join(__dirname, '../vendor/pciids/gpu-identities.json'));
const dataset = JSON.parse(datasetBytes);
const fn = functionLoader(source);

function fixture() {
  const context = vm.createContext({});
  // Both the generated runtime asset and the real UI consumer run unmodified.
  // Expected names come independently from the checked-in PCI data JSON.
  vm.runInContext(asset, context);
  vm.runInContext(['gpuTextValue', 'gpuBoardLabel'].map(fn).join('\n'), context);
  return context;
}

const expectedBoard = id => dataset.brand_aliases[id] || dataset.vendors[id];
const unknownBoard = id => `未知品牌 (ID ${id.toUpperCase()})`;

test('generated asset is bound to the actual registry JSON and the UI resolves every registered vendor', () => {
  const context = fixture();
  assert.equal(context.RigPilotPciIdentity.registrySha256, crypto.createHash('sha256').update(datasetBytes).digest('hex'));
  const ids = Object.keys(dataset.vendors);
  assert.deepEqual(Object.keys(context.RigPilotPciIdentity.vendors), ids);
  for (const id of ids) {
    assert.equal(context.gpuBoardLabel({board_vendor_id: id}), expectedBoard(id), `ID-only legacy Agent ${id}`);
    assert.equal(context.gpuBoardLabel({board_vendor_id: id, board_vendor: 'unreported'}), expectedBoard(id), `old placeholder ${id}`);
    assert.equal(context.gpuBoardLabel({board_vendor_id: id, board_vendor: 'Wrong old reported brand'}), expectedBoard(id), `registry priority ${id}`);
  }
});

test('all registered IDs normalize uppercase 0X hex and integer representations without a manual brand table', () => {
  const context = fixture();
  for (const id of Object.keys(dataset.vendors)) {
    for (const raw of [id.toUpperCase(), `0X${id.toUpperCase()}`, Number.parseInt(id, 16)]) {
      assert.equal(context.gpuBoardLabel({board_vendor_id: raw}), expectedBoard(id), `${id}: ${raw}`);
    }
  }
  // This partner was absent from the controller's previous twelve-name table.
  assert.equal(context.gpuBoardLabel({board_vendor_id: '10b0', board_vendor: 'Unknown (10b0)'}), expectedBoard('10b0'));
  assert.equal(context.RigPilotPciIdentity.lookupVendor('10b0'), dataset.vendors['10b0']);
});

test('unregistered IDs retain their real identity while genuine reported brands remain usable', () => {
  const context = fixture();
  assert.equal(Object.hasOwn(dataset.vendors, 'fefe'), false);
  for (const raw of ['fefe', 'FEFE', '0XFEFE', 0xfefe]) {
    assert.equal(context.gpuBoardLabel({board_vendor_id: raw}), unknownBoard('fefe'));
    assert.equal(context.gpuBoardLabel({board_vendor_id: raw, board_vendor: '  Genuine   Board Partner  '}), 'Genuine Board Partner');
  }
  assert.equal(context.gpuBoardLabel({board_vendor: 'Genuine Board Partner'}), 'Genuine Board Partner');
});

test('placeholder, missing and unsafe reported text cannot masquerade as an unknown-ID brand', () => {
  const context = fixture();
  for (const reported of [undefined, null, true, 1234, {}, '', '  ', 'UNKNOWN', 'Unknown vendor', 'Unknown brand',
    'unknown (fefe)', 'unreported', 'NOT REPORTED', 'N/A', 'Not available', '[NOT SUPPORTED]', '[NO PERMISSION]',
    'none', 'null', '未知', '未知品牌', '未知厂商', '未上报', '未提供', '不支持', '暂无',
    'Real\x00Name', 'Real\nName', 'Real\tName', 'Real\x7fName', 'A'.repeat(257)]) {
    assert.equal(context.gpuBoardLabel({board_vendor_id: 'fefe', board_vendor: reported}), unknownBoard('fefe'), String(reported));
  }
});

test('compound subsystem IDs are never truncated into a registered board vendor', () => {
  const context = fixture();
  for (const raw of ['205f10b0', '0X205F10B0']) {
    assert.equal(context.RigPilotPciIdentity.normalizeId(raw), null);
    const label = context.gpuBoardLabel({board_vendor_id: raw});
    assert.notEqual(label, expectedBoard('10b0'));
    assert.ok(label.includes(raw.toUpperCase()), 'keep the entire unresolved identity for diagnostics');
  }
});

test('booleans, absent IDs and non-ID values never become vendor 0001, and zero stays unregistered', () => {
  const context = fixture();
  for (const raw of [undefined, null, true, false, {}, ['10b0'], '', ' ', NaN, Infinity, -1, 65536, 1.5]) {
    assert.equal(context.RigPilotPciIdentity.normalizeId(raw), null);
    assert.equal(context.gpuBoardLabel({board_vendor_id: raw}), '品牌未上报', String(raw));
  }
  assert.equal(context.gpuBoardLabel({board_vendor_id: 0}), unknownBoard('0000'));
  assert.equal(context.gpuBoardLabel({board_vendor_id: '0000'}), unknownBoard('0000'));
  assert.equal(context.gpuBoardLabel({board_vendor_id: 1}), expectedBoard('0001'));
});

test('board lookup never reads or fabricates a memory maker and driver-reported memory is independent', () => {
  const context = fixture();
  const gpu = {board_vendor_id: '10b0'};
  for (const field of ['memory_vendor', 'memory_type', 'pci_subsystem_id', 'vbios_version']) {
    Object.defineProperty(gpu, field, {get() {throw new Error(`board lookup read ${field}`);}});
  }
  assert.equal(context.gpuBoardLabel(gpu), expectedBoard('10b0'));
  const statement = source.match(/^const gpuModelMemoryTypes = \{[\s\S]*?^\};/m)?.[0];
  assert.ok(statement, 'public memory-type specifications loaded from the UI');
  vm.runInContext(statement, context);
  vm.runInContext(['gpuNormalizedModel', 'gpuMemoryIdentityParts'].map(fn).join('\n'), context);
  const unobserved = {name: 'Synthetic PCI-only Board', board_vendor_id: '10b0'};
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(unobserved)), ['显存颗粒未上报']);
  assert.equal(Object.hasOwn(unobserved, 'memory_vendor'), false);
  const modelOnly = {name: 'NVIDIA GeForce RTX 5060 Ti', vbios_version: 'synthetic', pci_subsystem_id: 'fffffefe', board_vendor_id: '10b0'};
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(modelOnly)), ['GDDR7（颗粒厂商未上报）']);
  const reported = {...modelOnly, memory_vendor: 'Fixture DRAM', memory_type: 'GDDR6'};
  assert.deepEqual(Array.from(context.gpuMemoryIdentityParts(reported)), ['Fixture DRAM', 'GDDR6']);
  assert.equal(context.gpuBoardLabel(reported), expectedBoard('10b0'));
});
