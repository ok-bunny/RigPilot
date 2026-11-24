#!/usr/bin/env python3
"""Regenerate RigPilot's offline PCI identity data from its pinned local input.

No downloads occur here. See vendor/pciids/README.txt for manual snapshot
updates. JSON, Python and JavaScript are generated from one parsed registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


REPOSITORY = "https://github.com/pciutils/pciids"
VENDOR = re.compile(r"([0-9a-fA-F]{4})  (.+)")
DEVICE = re.compile(r"\t([0-9a-fA-F]{4})  (.+)")
SUBSYSTEM = re.compile(r"\t\t([0-9a-fA-F]{4}) ([0-9a-fA-F]{4})  (.+)")
GEFORCE_RTX_40_50 = re.compile(r"\bGeForce RTX (?:40|50)\d{2}\b")

# Display aliases preserve existing RigPilot labels. Canonical vendor names
# remain unchanged in the registry and lookup_vendor(). These are board
# partner labels; neither the aliases nor PCI IDs identify DRAM makers.
BRAND_ALIASES = {
    "10de": "NVIDIA",
    "1043": "ASUS",
    "1458": "Gigabyte",
    "1462": "MSI",
    "1569": "Palit",
    "1771": "Inno3D",
    "1b4c": "GALAX",
    "1dd5": "Manli",
    "19da": "ZOTAC",
    "196e": "PNY",
    "3842": "EVGA",
    "7377": "Colorful",
    "10b0": "Gainward / CardExpert",
}

PLACEHOLDERS = (
    "unknown", "unknown vendor", "unknown brand", "unreported", "not reported",
    "n/a", "na", "not available", "not supported", "no permission", "none",
    "null", "未知", "未知品牌", "未知厂商", "未上报", "未提供", "不支持", "暂无",
)


def parse_pci_ids(text: str) -> tuple[dict[str, str], dict[str, dict]]:
    """Return all PCI vendor names and registered GeForce RTX 40/50 subtrees."""
    vendors: dict[str, str] = {}
    nvidia_devices: dict[str, dict] = {}
    vendor_id: str | None = None
    device_id: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        if line.startswith("C "):
            break
        match = VENDOR.fullmatch(line)
        if match:
            vendor_id = match[1].lower()
            device_id = None
            if vendor_id in vendors:
                raise ValueError(f"Duplicate vendor at line {number}: {vendor_id}")
            vendors[vendor_id] = match[2]
            continue
        match = DEVICE.fullmatch(line)
        if match and vendor_id is not None:
            device_id = match[1].lower()
            if vendor_id == "10de":
                if device_id in nvidia_devices:
                    raise ValueError(f"Duplicate NVIDIA device at line {number}: {device_id}")
                nvidia_devices[device_id] = {"name": match[2], "subsystems": {}}
            continue
        match = SUBSYSTEM.fullmatch(line)
        if match and vendor_id is not None and device_id is not None:
            if vendor_id == "10de":
                key = f"{match[1].lower()}:{match[2].lower()}"
                subsystems = nvidia_devices[device_id]["subsystems"]
                if key in subsystems:
                    raise ValueError(f"Duplicate NVIDIA subsystem at line {number}: {key}")
                subsystems[key] = match[3]
            continue
        raise ValueError(f"Unsupported vendor/device syntax at line {number}")
    devices = {
        device: record for device, record in nvidia_devices.items()
        if GEFORCE_RTX_40_50.search(record["name"])
    }
    return vendors, devices


def build_registry(raw: bytes, source_manifest: dict) -> dict:
    source_file = next(
        entry for entry in source_manifest["files"] if entry["path"] == "pci.ids"
    )
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != source_file["sha256"] or len(raw) != source_file["size_bytes"]:
        raise ValueError("pci.ids differs from its fixed source manifest")
    commit = source_manifest["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Source commit must be a full lowercase Git object ID")
    if source_manifest["repository"] != REPOSITORY:
        raise ValueError("Unsupported PCI data source")
    expected_url = f"https://raw.githubusercontent.com/pciutils/pciids/{commit}/pci.ids"
    if source_file["url"] != expected_url:
        raise ValueError("pci.ids URL does not match the fixed source commit")
    text = raw.decode("utf-8")
    version = re.search(r"^#\s+Version:\s+(\S+)\s*$", text, re.MULTILINE)
    if not version:
        raise ValueError("Missing pci.ids version header")
    if "3-clause BSD License" not in text[:2048]:
        raise ValueError("Missing upstream BSD permission notice")
    vendors, devices = parse_pci_ids(text)
    if "10de" not in vendors or not devices:
        raise ValueError("Input contains no NVIDIA GeForce RTX 40/50 data")
    if not set(BRAND_ALIASES).issubset(vendors):
        raise ValueError("Display alias refers to an unregistered PCI vendor")
    return {
        "schema_version": 1,
        "source": {
            "repository": REPOSITORY,
            "commit": commit,
            "url": expected_url,
            "pci_ids_version": version[1],
            "sha256": actual_sha,
            "license": "BSD-3-Clause",
            "copyright": "Martin Mares and Albert Pool",
        },
        "selection": {
            "vendors": "all top-level vendor IDs before the device-class section",
            "devices": "10de entries named GeForce RTX 40xx/50xx, including their variants",
            "subsystems": "all registered subsystems of selected device IDs",
            "memory_vendor": "not provided: PCI board/vendor IDs do not identify DRAM",
        },
        "vendors": vendors,
        "brand_aliases": BRAND_ALIASES,
        "nvidia_rtx_devices": devices,
    }


def registry_bytes(registry: dict) -> bytes:
    return (json.dumps(registry, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def render_python(registry: dict, digest: str, license_notice: str) -> bytes:
    prefix = "\n".join("# " + line for line in license_notice.rstrip().splitlines())
    serialized = json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content = f'''# Generated by work/update_pci_identity_data.py; do not edit.
{prefix}
"""Offline PCI board identities. These records never identify DRAM makers."""

from __future__ import annotations

import json
import re

REGISTRY_SHA256 = {digest!r}
LICENSE_NOTICE = {license_notice!r}
REGISTRY = json.loads({serialized!r})
SOURCE = REGISTRY["source"]
SELECTION = REGISTRY["selection"]
VENDORS = REGISTRY["vendors"]
BRAND_ALIASES = REGISTRY["brand_aliases"]
NVIDIA_RTX_DEVICES = REGISTRY["nvidia_rtx_devices"]
_PLACEHOLDERS = frozenset({PLACEHOLDERS!r})


def normalize_pci_id(value: object) -> str | None:
    """Normalize one 16-bit ID, without truncating compound or oversized IDs."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{{value:04x}}" if 0 <= value <= 0xffff else None
    if isinstance(value, str) and re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]{{4}}", value):
        return value[-4:].lower()
    return None


def lookup_vendor(vendor_id: object) -> str | None:
    return VENDORS.get(normalize_pci_id(vendor_id))


def _reported_board_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    label = " ".join(value.split())
    placeholder = label.lower().removeprefix("[").removesuffix("]")
    if (not label or len(label) > 256 or placeholder in _PLACEHOLDERS
            or placeholder.startswith("unknown (")):
        return None
    return label


def board_vendor_label(vendor_id: object, reported: object = None) -> str | None:
    """Prefer the registered board vendor; use genuine reported text if unknown."""
    normalized = normalize_pci_id(vendor_id)
    registered = VENDORS.get(normalized)
    if registered is not None:
        return BRAND_ALIASES.get(normalized, registered)
    return _reported_board_label(reported)


def get_nvidia_rtx_device(device_id: object) -> dict | None:
    record = NVIDIA_RTX_DEVICES.get(normalize_pci_id(device_id))
    if record is None:
        return None
    return {{"name": record["name"], "subsystems": dict(record["subsystems"])}}


def subsystem_device_label(device_id: object, subsystem_vendor_id: object,
                           subsystem_device_id: object) -> str | None:
    record = NVIDIA_RTX_DEVICES.get(normalize_pci_id(device_id))
    vendor = normalize_pci_id(subsystem_vendor_id)
    device = normalize_pci_id(subsystem_device_id)
    if record is None or vendor is None or device is None:
        return None
    return record["subsystems"].get(f"{{vendor}}:{{device}}")
'''
    return content.encode("utf-8")


def render_javascript(registry: dict, digest: str, license_notice: str) -> bytes:
    prefix = "\n".join("// " + line for line in license_notice.rstrip().splitlines())
    serialized = json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content = f'''// Generated by work/update_pci_identity_data.py; do not edit.
{prefix}
// PCI board identities are descriptions, not observations of DRAM makers.
(function (global) {{
  "use strict";
  const registry = {serialized};
  const placeholders = new Set({json.dumps(PLACEHOLDERS, ensure_ascii=False)});
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  function normalizeId(value) {{
    if (typeof value === "number") {{
      return Number.isInteger(value) && value >= 0 && value <= 0xffff
        ? value.toString(16).padStart(4, "0") : null;
    }}
    return typeof value === "string" && (value.length === 4 || value.length === 6)
      && /^(?:0[xX])?[0-9a-fA-F]{{4}}$/.test(value)
      ? value.slice(-4).toLowerCase() : null;
  }}
  function lookupVendor(vendorId) {{
    const id = normalizeId(vendorId);
    return id !== null && has(registry.vendors, id) ? registry.vendors[id] : null;
  }}
  function reportedBoardLabel(value) {{
    if (typeof value !== "string") return null;
    if (/[\\x00-\\x1f\\x7f]/u.test(value)) return null;
    const label = value.replace(/[\\s\\u0085]+/gu, " ").trim();
    const placeholder = label.toLowerCase().replace(/^\\[|\\]$/g, "");
    return !label || Array.from(label).length > 256 || placeholders.has(placeholder)
      || placeholder.startsWith("unknown (")
      ? null : label;
  }}
  function boardVendorLabel(vendorId, reported = null) {{
    const id = normalizeId(vendorId);
    const registered = lookupVendor(id);
    if (registered !== null) {{
      return has(registry.brand_aliases, id) ? registry.brand_aliases[id] : registered;
    }}
    return reportedBoardLabel(reported);
  }}
  function getNvidiaRtxDevice(deviceId) {{
    const id = normalizeId(deviceId);
    if (id === null || !has(registry.nvidia_rtx_devices, id)) return null;
    const record = registry.nvidia_rtx_devices[id];
    return {{ name: record.name, subsystems: {{ ...record.subsystems }} }};
  }}
  function subsystemDeviceLabel(deviceId, subsystemVendorId, subsystemDeviceId) {{
    const record = getNvidiaRtxDevice(deviceId);
    const vendor = normalizeId(subsystemVendorId);
    const device = normalizeId(subsystemDeviceId);
    if (record === null || vendor === null || device === null) return null;
    const key = `${{vendor}}:${{device}}`;
    return has(record.subsystems, key) ? record.subsystems[key] : null;
  }}
  for (const record of Object.values(registry.nvidia_rtx_devices)) {{
    Object.freeze(record.subsystems);
    Object.freeze(record);
  }}
  for (const value of Object.values(registry)) {{
    if (typeof value === "object" && value !== null) Object.freeze(value);
  }}
  Object.freeze(registry);
  global.RigPilotPciIdentity = Object.freeze({{
    registrySha256: {json.dumps(digest)},
    licenseNotice: {json.dumps(license_notice, ensure_ascii=False)},
    source: registry.source,
    selection: registry.selection,
    vendors: registry.vendors,
    brandAliases: registry.brand_aliases,
    nvidiaRtxDevices: registry.nvidia_rtx_devices,
    normalizeId, lookupVendor, boardVendorLabel, getNvidiaRtxDevice, subsystemDeviceLabel
  }});
}})(globalThis);
'''
    return content.encode("utf-8")


def generated_outputs(root: Path) -> dict[Path, bytes]:
    data_root = root / "vendor" / "pciids"
    registry = build_registry(
        (data_root / "pci.ids").read_bytes(),
        json.loads((data_root / "SOURCE.json").read_text(encoding="utf-8")),
    )
    license_notice = (data_root / "LICENSE").read_text(encoding="utf-8")
    canonical = registry_bytes(registry)
    digest = hashlib.sha256(canonical).hexdigest()
    return {
        data_root / "gpu-identities.json": canonical,
        root / "server" / "pci_identity.py": render_python(registry, digest, license_notice),
        root / "server" / "static" / "pci-identity-data.js": render_javascript(registry, digest, license_notice),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true", help="verify generated bytes without writing")
    args = parser.parse_args()
    outputs = generated_outputs(args.root)
    failures = []
    for path, content in outputs.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != content:
                failures.append(str(path.relative_to(args.root)))
        else:
            path.write_bytes(content)
    if failures:
        raise SystemExit("Generated PCI identity files differ: " + ", ".join(failures))
    print(json.dumps({
        "status": "PASS", "mode": "check" if args.check else "generate",
        "files": [{"path": str(path.relative_to(args.root)), "size_bytes": len(content),
                   "sha256": hashlib.sha256(content).hexdigest()} for path, content in outputs.items()],
    }))


if __name__ == "__main__":
    main()
