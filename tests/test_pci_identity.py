"""Offline PCI source coverage, identity boundaries and Python/browser parity."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import types

import pytest

from server import pci_identity


ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "vendor" / "pciids"
GENERATOR_PATH = ROOT / "work" / "update_pci_identity_data.py"
_spec = importlib.util.spec_from_file_location("update_pci_identity_data", GENERATOR_PATH)
assert _spec is not None and _spec.loader is not None
generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(generator)


def test_all_pinned_vendors_and_selected_names_retained_exactly():
    raw = (DATA_ROOT / "pci.ids").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "25b626586086220f2804d7b11275a5652dac94eceacce216448ab5e5220aace1"
    pre_classes = raw.decode("utf-8").split("\nC ", 1)[0]
    # Independent source scans establish completeness, rather than asking the
    # generator to verify its own output.
    vendors = dict(re.findall(r"^([0-9a-f]{4})  (.+)$", pre_classes, re.MULTILINE))
    nvidia_block = re.search(
        r"^10de  [^\n]+\n(.*?)(?=^[0-9a-f]{4}  |\Z)",
        pre_classes, re.MULTILINE | re.DOTALL,
    )
    assert nvidia_block is not None
    names = dict(re.findall(
        r"^\t([0-9a-f]{4})  (.*GeForce RTX (?:40|50)\d{2}[^\n]*)$",
        nvidia_block[1], re.MULTILINE,
    ))
    assert pci_identity.VENDORS == vendors
    assert {key: value["name"] for key, value in pci_identity.NVIDIA_RTX_DEVICES.items()} == names
    assert len(vendors) == 2519 and len(names) == 49
    assert sum(len(record["subsystems"]) for record in pci_identity.NVIDIA_RTX_DEVICES.values()) == 0
    assert pci_identity.get_nvidia_rtx_device("2d04")["name"] == "GB206 [GeForce RTX 5060 Ti]"
    assert pci_identity.get_nvidia_rtx_device("27a0")["name"] == "AD104M [GeForce RTX 4080 Max-Q / Mobile]"
    assert pci_identity.get_nvidia_rtx_device("2702")["name"] == "AD103 [GeForce RTX 4080 SUPER]"


@pytest.mark.parametrize(("value", "expected"), [
    ("10B0", "10b0"), ("0x10B0", "10b0"), ("0X10b0", "10b0"),
    ("0000", "0000"), ("ffff", "ffff"), (0, "0000"), (0xffff, "ffff"),
    (0x10b0, "10b0"), ("fefe", "fefe"),
    (None, None), (True, None), (False, None), (1.0, None),
    (-1, None), (0x10000, None), ("", None), ("1b0", None),
    ("010b0", None), ("0x010b0", None), ("10b0:1234", None),
    ("fffffefe", None), ("0xfffffefe", None), (" 10b0", None),
    ("10b0\n", None), ("10b0\r", None), ("10b0 ", None), ("10g0", None),
    ({}, None), ([], None),
])
def test_one_id_normalization_rejects_truncation_and_non_ids(value, expected):
    assert pci_identity.normalize_pci_id(value) == expected


def test_registry_names_display_aliases_and_unknown_fallback_are_distinct():
    assert pci_identity.lookup_vendor("10b0") == "CardExpert Technology"
    assert pci_identity.board_vendor_label("10b0", "unreported") == "Gainward / CardExpert"
    assert pci_identity.lookup_vendor("1458") == "Gigabyte Technology Co., Ltd"
    assert pci_identity.board_vendor_label("1458", "Different reported label") == "Gigabyte"
    assert pci_identity.board_vendor_label("8086", "Different reported label") == pci_identity.lookup_vendor("8086")
    assert pci_identity.lookup_vendor("fefe") is None
    assert pci_identity.board_vendor_label("fefe", "  Genuine   Board Partner  ") == "Genuine Board Partner"
    assert pci_identity.board_vendor_label(None, "Genuine Board Partner") == "Genuine Board Partner"
    assert len(pci_identity.BRAND_ALIASES) == 13
    assert set(pci_identity.BRAND_ALIASES).issubset(pci_identity.VENDORS)


@pytest.mark.parametrize("reported", [
    None, True, 1234, {}, "", "  ", "UNKNOWN", "Unknown vendor", "Unknown brand",
    "unknown (fefe)", "unreported", "NOT REPORTED", "N/A", "NA", "Not available",
    "Not supported", "[NOT SUPPORTED]", "[NO PERMISSION]", "none", "null",
    "未知", "未知品牌", "未知厂商", "未上报", "未提供", "不支持", "暂无",
    "Real\x00Name", "Real\nName", "Real\tName", "Real\x7fName", "A" * 257,
])
def test_placeholder_or_unsafe_reported_text_does_not_become_a_brand(reported):
    assert pci_identity.board_vendor_label("fefe", reported) is None


def test_device_record_is_copy_and_does_not_claim_physical_memory():
    device = pci_identity.get_nvidia_rtx_device("2d04")
    assert device is not None and set(device) == {"name", "subsystems"}
    device["name"] = "Modified"
    device["subsystems"]["10b0:1234"] = "Modified"
    assert pci_identity.get_nvidia_rtx_device("2d04") == {
        "name": "GB206 [GeForce RTX 5060 Ti]", "subsystems": {},
    }
    assert pci_identity.get_nvidia_rtx_device("26b1") is None
    assert pci_identity.get_nvidia_rtx_device("ffff") is None
    assert pci_identity.subsystem_device_label("2d04", "10b0", "1234") is None
    assert pci_identity.subsystem_device_label("2d04", "bad", "1234") is None
    assert "do not identify DRAM" in pci_identity.SELECTION["memory_vendor"]


def test_future_registered_subsystems_remain_exact_and_never_become_memory_evidence():
    fixture = (
        "10de  NVIDIA Corporation\n"
        "\t2684  AD102 [GeForce RTX 4090]\n"
        "\t\t10b0 1234  Registered board description\n"
        "\t27a0  AD104M [GeForce RTX 4080 Max-Q / Mobile]\n"
        "\t2d04  GB206 [GeForce RTX 5060 Ti]\n"
        "\t2702  AD103 [GeForce RTX 4080 SUPER]\n"
        "\t26b1  AD102GL [RTX 6000 Ada Generation]\n"
        "\t1c81  GP107 [GeForce GTX 1050]\n"
        "\t1341  GM108 [GeForce 840M]\n"
        "1458  Gigabyte Technology Co., Ltd\n"
        "C 01  Storage controller\n"
        "\t00  Non-VGA unclassified device\n"
    )
    vendors, devices = generator.parse_pci_ids(fixture)
    assert set(vendors) == {"10de", "1458"}
    assert set(devices) == {"2684", "27a0", "2d04", "2702"}
    assert devices["2684"]["subsystems"] == {"10b0:1234": "Registered board description"}
    registry = copy.deepcopy(pci_identity.REGISTRY)
    registry["nvidia_rtx_devices"] = devices
    module = types.ModuleType("fixture_pci_identity")
    exec(generator.render_python(registry, "fixture", pci_identity.LICENSE_NOTICE), module.__dict__)
    assert module.subsystem_device_label("2684", "0X10B0", "1234") == "Registered board description"
    assert module.subsystem_device_label("2684", "1458", "1234") is None
    assert set(module.get_nvidia_rtx_device("2684")) == {"name", "subsystems"}


@pytest.mark.parametrize("bad", [
    "\t2684  orphan", "10de  NVIDIA\n10de  duplicate",
    "10de  NVIDIA\n\t2684  A\n\t2684  B",
    "10de  NVIDIA\n\t2684  A\n\t\t10b0 1234  A\n\t\t10b0 1234  B",
    "10de  NVIDIA\n\tbroken", "10de  NVIDIA\n\t\t10b0 1234  orphan",
])
def test_corrupt_source_structure_is_rejected(bad):
    with pytest.raises(ValueError):
        generator.parse_pci_ids(bad)


def test_source_pin_and_generated_bytes_are_reproducible_and_tamper_evident():
    manifest = json.loads((DATA_ROOT / "SOURCE.json").read_text(encoding="utf-8"))
    raw = (DATA_ROOT / "pci.ids").read_bytes()
    with pytest.raises(ValueError, match="differs"):
        generator.build_registry(raw + b"\n", manifest)
    changed = copy.deepcopy(manifest)
    changed["commit"] = "master"
    with pytest.raises(ValueError, match="full lowercase"):
        generator.build_registry(raw, changed)
    changed = copy.deepcopy(manifest)
    changed["files"][0]["url"] = "https://example.invalid/pci.ids"
    with pytest.raises(ValueError, match="URL"):
        generator.build_registry(raw, changed)
    outputs = generator.generated_outputs(ROOT)
    for path, generated in outputs.items():
        assert path.read_bytes() == generated
    assert hashlib.sha256((DATA_ROOT / "gpu-identities.json").read_bytes()).hexdigest() == pci_identity.REGISTRY_SHA256
    assert pci_identity.SOURCE["commit"] == "3452638d16a34aa3d6e427d21e4cd0bae0fb884e"
    assert pci_identity.SOURCE["license"] == "BSD-3-Clause"
    for needle in ("Martin Mares and Albert Pool", "Redistributions in binary form", "AS IS"):
        assert needle in pci_identity.LICENSE_NOTICE
        assert needle in (ROOT / "server/static/pci-identity-data.js").read_text(encoding="utf-8")


def test_python_and_browser_apis_share_registry_and_identity_results():
    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable for the browser registry contract")
    normalization = ["10B0", "0x10B0", "0X10b0", "0000", "ffff", 0, 65535,
                     None, True, False, -1, 65536, "1b0", "010b0", "0x010b0",
                     "10b0:1234", "fffffefe", " 10b0", "10b0\n", "10b0\r", {}, []]
    labels = [[vendor, "unreported"] for vendor in pci_identity.VENDORS]
    labels.extend([["fefe", value] for value in [
        None, "Genuine Board", "UNKNOWN", "N/A", "[NOT SUPPORTED]", "[NO PERMISSION]",
        "未知", "未上报", "  Genuine   Board  ", "Real\x00Name", "Real\nName",
        "\u0085Genuine\u0085Board\u0085", "🧩" * 256, "🧩" * 257,
    ]])
    payload = {"normalization": normalization, "labels": labels}
    bridge = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context, { timeout: 5000 });
const api = context.RigPilotPciIdentity;
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const record = api.getNvidiaRtxDevice("2d04");
record.name = "Modified";
record.subsystems["10b0:1234"] = "Modified";
process.stdout.write(JSON.stringify({
  source: api.source, registrySha256: api.registrySha256,
  vendors: api.vendors, brandAliases: api.brandAliases, devices: api.nvidiaRtxDevices,
  normalization: input.normalization.map(api.normalizeId),
  specialNumbers: [NaN, Infinity, -Infinity, 1.5].map(api.normalizeId),
  vendorLookups: Object.keys(api.vendors).map(api.lookupVendor),
  labels: input.labels.map(([id, text]) => api.boardVendorLabel(id, text)),
  unchangedRecord: api.getNvidiaRtxDevice("2d04"),
  unregisteredSubsystem: api.subsystemDeviceLabel("2d04", "10b0", "1234"),
  frozen: Object.isFrozen(api) && Object.isFrozen(api.vendors) && Object.isFrozen(api.nvidiaRtxDevices)
}));
'''
    response = subprocess.run(
        [node, "-e", bridge, str(ROOT / "server/static/pci-identity-data.js")],
        input=json.dumps(payload, ensure_ascii=False), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=15,
    )
    browser = json.loads(response.stdout)
    assert browser["source"] == pci_identity.SOURCE
    assert browser["registrySha256"] == pci_identity.REGISTRY_SHA256
    assert browser["vendors"] == pci_identity.VENDORS
    assert browser["brandAliases"] == pci_identity.BRAND_ALIASES
    assert browser["devices"] == pci_identity.NVIDIA_RTX_DEVICES
    assert browser["normalization"] == [pci_identity.normalize_pci_id(value) for value in normalization]
    assert browser["specialNumbers"] == [None] * 4
    assert browser["vendorLookups"] == [pci_identity.lookup_vendor(vendor) for vendor in browser["vendors"]]
    assert browser["labels"] == [pci_identity.board_vendor_label(vendor, text) for vendor, text in labels]
    assert browser["unchangedRecord"] == pci_identity.get_nvidia_rtx_device("2d04")
    assert browser["unregisteredSubsystem"] is None
    assert browser["frozen"]
