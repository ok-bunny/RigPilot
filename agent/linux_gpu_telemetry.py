"""Read-only Linux GPU snapshots.

No miner, setting command, driver installer, NVML setter or sysfs write is used.
Pass the Agent's already collected NVIDIA rows to reuse one host sample. Missing
fields remain None; a successful empty scan differs from an unavailable scan.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

MAX_GPUS = 64
MAX_SYSFS_ENTRIES = 512
MAX_TEXT_BYTES = 4096
MAX_COMMAND_BYTES = 256 * 1024
_PCI = re.compile(r"^(?:(?P<domain>[0-9a-fA-F]{1,8}):)?(?P<bus>[0-9a-fA-F]{1,2}):(?P<device>[0-9a-fA-F]{1,2})\.(?P<function>[0-7])$")
_NVIDIA_FIELDS = ("index", "uuid", "pci.bus_id", "name", "temperature.gpu", "fan.speed", "power.draw", "utilization.gpu", "clocks.current.graphics", "clocks.current.memory")
_TELEMETRY = {"temperature_c": (-30, 150), "junction_temperature_c": (-30, 150),
              "memory_temperature_c": (-30, 150), "fan_speed_percent": (0, 100),
              "fan_pwm_percent": (0, 100), "fan_rpm": (0, 100_000),
              "power_w": (0, 20_000), "utilization_percent": (0, 100),
              "graphics_clock_mhz": (0, 100_000), "memory_clock_mhz": (0, 100_000),
              "memory_total_mib": (0, 10_000_000), "memory_used_mib": (0, 10_000_000)}
def canonical_pci_id(value: Any) -> str | None:
    """Normalize BDF to the existing Agent's eight-digit PCI domain format."""
    match = _PCI.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    domain, bus, device, function = (int(match.group(k) or "0", 16) for k in ("domain", "bus", "device", "function"))
    if device > 31:
        return None
    return f"{domain:08x}:{bus:02x}:{device:02x}.{function:x}"


def _number(value: Any, low: float = 0, high: float = float("inf")) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def _text(value: Any, maximum: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def _index(value: Any) -> int | None:
    number = _number(value, 0, 65535)
    return int(number) if number is not None and number.is_integer() else None


def _read_text(path: Path) -> str:
    with path.open("r", encoding="ascii") as stream:
        value = stream.read(MAX_TEXT_BYTES + 1)
    if len(value) > MAX_TEXT_BYTES:
        raise ValueError("oversized sysfs field")
    return value


def _read(path: Path, reader: Callable[[Path], str]) -> tuple[str | None, str | None]:
    try:
        value = reader(path)
        if not isinstance(value, str) or len(value) > MAX_TEXT_BYTES:
            return None, "invalid_value"
        return value.strip(), None
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        return None, "permission_denied"
    except (OSError, UnicodeError):
        return None, "read_error"
    except ValueError:
        return None, "invalid_value"


def _entries(path: Path) -> tuple[list[Path], str | None]:
    try:
        with os.scandir(path) as stream:
            entries = []
            for entry in stream:
                if len(entries) >= MAX_SYSFS_ENTRIES:
                    return [], "entry_limit"
                entries.append(Path(entry.path))
        return sorted(entries, key=lambda p: p.name), None
    except FileNotFoundError:
        return [], "missing"
    except PermissionError:
        return [], "permission_denied"
    except OSError:
        return [], "read_error"


def _blank(vendor: str, pci: str | None, uuid: str | None, native_index: int | None) -> dict[str, Any]:
    return {"vendor": vendor, "pci_bus_id": pci, "uuid": uuid,
            "gpu_key": f"{vendor}:{uuid or pci}" if uuid or pci else None,
            "native_index": native_index, "native_index_kind": "nvml" if vendor == "nvidia" else "drm",
            "control_index": native_index if vendor == "nvidia" else None,
            "index": native_index if vendor == "nvidia" else None,
            "name": None, "driver_available": None, "device_type": None,
            "identity_status": "verified" if pci or uuid else "unavailable",
            "field_status": {}, "field_sources": {},
            **{field: None for field in _TELEMETRY}}


def _nvidia_rows(rows: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_GPUS:
        return [], {"status": "unavailable", "reason": "invalid_nvidia_sample"}
    result = []
    invalid = False
    for raw in rows:
        if not isinstance(raw, Mapping) or raw.get("vendor", "nvidia") != "nvidia":
            invalid = True
            continue
        uuid = _text(raw.get("uuid"), 128)
        if uuid is not None and (not uuid.startswith("GPU-") or any(c.isspace() for c in uuid)):
            uuid = None
        pci = canonical_pci_id(raw.get("pci_bus_id"))
        if uuid is None and pci is None:
            invalid = True
            continue
        row = _blank("nvidia", pci, uuid, _index(raw.get("index")))
        row["name"] = _text(raw.get("name"))
        row["driver_available"] = raw.get("driver_available") if type(raw.get("driver_available")) is bool else bool(uuid)
        row["device_type"] = _text(raw.get("device_type"), 24)
        for field, bounds in _TELEMETRY.items():
            row[field] = _number(raw.get(field), *bounds)
            row["field_status"][field] = "ok" if row[field] is not None else ("driver_unavailable" if row["driver_available"] is False else "unavailable")
            if row[field] is not None:
                row["field_sources"][field] = "nvidia_smi"
        for field in ("board_vendor", "vbios_version", "memory_type", "memory_vendor"):
            row[field] = _text(raw.get(field))
        result.append(row)
    partial = invalid or any(row["driver_available"] is False for row in result)
    return result, {"status": "partial" if partial else "ok", "reason": "invalid_or_unavailable_devices" if partial else None}


def _sample_nvidia(executable: str, runner: Callable[..., Any], timeout: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # One invocation only. No retry groups that can multiply the stats deadline.
    try:
        completed = runner([executable, "--query-gpu=" + ",".join(_NVIDIA_FIELDS), "--format=csv,noheader,nounits"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, shell=False,
                           timeout=timeout, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"})
    except FileNotFoundError:
        return [], {"status": "unavailable", "reason": "command_missing"}
    except subprocess.TimeoutExpired:
        return [], {"status": "unavailable", "reason": "command_timeout"}
    except OSError:
        return [], {"status": "unavailable", "reason": "command_failed"}
    if completed.returncode != 0:
        return [], {"status": "unavailable", "reason": "driver_query_failed"}
    output = completed.stdout
    if not isinstance(output, str) or len(output.encode("utf-8")) > MAX_COMMAND_BYTES:
        return [], {"status": "unavailable", "reason": "invalid_nvidia_sample"}
    rows = []
    try:
        for values in csv.reader(io.StringIO(output)):
            if not values:
                continue
            if len(values) != len(_NVIDIA_FIELDS) or len(rows) >= MAX_GPUS:
                raise ValueError
            raw = dict(zip(_NVIDIA_FIELDS, values))
            rows.append({"vendor": "nvidia", "driver_available": True,
                "index": raw["index"], "uuid": raw["uuid"].strip(), "pci_bus_id": raw["pci.bus_id"], "name": raw["name"],
                **{field: raw[source] for field, source in (("temperature_c", "temperature.gpu"), ("fan_speed_percent", "fan.speed"),
                   ("power_w", "power.draw"), ("utilization_percent", "utilization.gpu"),
                   ("graphics_clock_mhz", "clocks.current.graphics"), ("memory_clock_mhz", "clocks.current.memory"))}})
    except (csv.Error, ValueError):
        return [], {"status": "unavailable", "reason": "invalid_nvidia_sample"}
    return _nvidia_rows(rows)


def _amd_device(path: Path, pci: str, reader: Callable[[Path], str]) -> dict[str, Any]:
    drm, drm_error = _entries(path / "drm")
    indices = [int(p.name[4:]) for p in drm if re.fullmatch(r"card[0-9]+", p.name)]
    row = _blank("amd", pci, None, indices[0] if len(indices) == 1 else None)
    try:
        driver = (path / "driver").resolve(strict=True).name
    except (OSError, RuntimeError):
        driver = None
    row["driver_available"] = driver == "amdgpu"
    row["driver_name"] = driver
    row["name"] = _read(path / "product_name", reader)[0] or None
    row["name"] = _text(row["name"])
    row["pci_device_id"] = _read(path / "device", reader)[0]
    row["field_status"]["native_index"] = "ok" if row["native_index"] is not None else drm_error or "ambiguous_or_missing_drm_card"
    hwmons, hwmon_error = _entries(path / "hwmon")
    matches = [p for p in hwmons if re.fullmatch(r"hwmon[0-9]+", p.name) and _read(p / "name", reader)[0] == "amdgpu"]
    hwmon = matches[0] if len(matches) == 1 else None
    failure = "driver_unavailable" if not row["driver_available"] else hwmon_error or "hwmon_unavailable_or_ambiguous"
    for field in _TELEMETRY:
        row["field_status"][field] = failure
    if not row["driver_available"]:
        return row

    def measure(field: str, base: Path | None, filename: str, divisor: float = 1) -> None:
        if base is None:
            return
        text, error = _read(base / filename, reader)
        number = _number(text, 0 if divisor != 1000 or "temperature" not in field else -30_000)
        value = number / divisor if number is not None else None
        value = _number(value, *_TELEMETRY[field])
        if field == "fan_rpm" and value == 65535:
            value = None
        row[field] = value
        row["field_status"][field] = "ok" if value is not None else error or "invalid_value"
        if value is not None:
            row["field_sources"][field] = "amdgpu_sysfs:" + filename

    for field, filename, divisor in (("temperature_c", "temp1_input", 1000), ("junction_temperature_c", "temp2_input", 1000),
         ("memory_temperature_c", "temp3_input", 1000), ("fan_rpm", "fan1_input", 1),
         ("power_w", "power1_average", 1e6), ("graphics_clock_mhz", "freq1_input", 1e6), ("memory_clock_mhz", "freq2_input", 1e6)):
        measure(field, hwmon, filename, divisor)
    if row["power_w"] is None:
        original_error = row["field_status"]["power_w"]
        measure("power_w", hwmon, "power1_input", 1e6)
        if row["power_w"] is None and original_error not in {"missing", "hwmon_unavailable_or_ambiguous"}:
            row["field_status"]["power_w"] = original_error
    # The amdgpu ABI may report SoC power for an APU; don't claim GPU-only scope.
    row["power_scope"] = "device_or_soc" if row["power_w"] is not None else None
    measure("utilization_percent", path, "gpu_busy_percent")
    measure("memory_total_mib", path, "mem_info_vram_total", 1024**2)
    measure("memory_used_mib", path, "mem_info_vram_used", 1024**2)
    if hwmon is not None:
        pwm_raw, pwm_error = _read(hwmon / "pwm1", reader)
        lo_raw, lo_error = _read(hwmon / "pwm1_min", reader)
        hi_raw, hi_error = _read(hwmon / "pwm1_max", reader)
        # Documented amdgpu PWM ABI bounds, not invented sensor measurements.
        lo = 0.0 if lo_error == "missing" else _number(lo_raw, 0, 65535)
        hi = 255.0 if hi_error == "missing" else _number(hi_raw, 0, 65535)
        pwm = _number(pwm_raw, 0, 65535)
        if pwm is not None and lo is not None and hi is not None and lo <= pwm <= hi and hi > lo:
            row["fan_pwm_percent"] = (pwm - lo) * 100 / (hi - lo)
            row["field_status"]["fan_pwm_percent"] = "ok"
            row["field_sources"]["fan_pwm_percent"] = "amdgpu_pwm"
        else:
            row["field_status"]["fan_pwm_percent"] = pwm_error or lo_error or hi_error or "invalid_value"
    # PWM percentage is exposed only under its real semantics. The Hive view
    # uses it for `fan`, as the official gpu-stats does; it is not fan RPM.
    row["fan_speed_percent"] = None
    row["field_status"]["fan_speed_percent"] = "not_reported_by_driver"
    return row


def collect_gpu_snapshot(*, nvidia_metrics: Sequence[Mapping[str, Any]] | None = None,
                         pci_root: Path = Path("/sys/bus/pci/devices"),
                         nvidia_smi: str = "nvidia-smi", runner: Callable[..., Any] = subprocess.run,
                         timeout: float = 2.0, captured_at: float | None = None,
                         read_text: Callable[[Path], str] = _read_text,
                         cpu_temperature_c: float | None = None) -> dict[str, Any]:
    """Collect one bounded read-only snapshot; supplied NVIDIA rows avoid commands.

    `native_index` is vendor-specific (NVML or DRM); `display_index`/`hive_index`
    only describe this snapshot's order. AMD has no NVIDIA control index/UUID.
    Source status `ok` plus devices=[] is a proven empty scan, not an error.
    """
    if _number(timeout, 0.05, 5) is None:
        raise ValueError("GPU query timeout must be between 0.05 and 5 seconds")
    timestamp = time.time() if captured_at is None else _number(captured_at)
    if timestamp is None:
        raise ValueError("invalid GPU snapshot time")
    devices, nvidia = _nvidia_rows(nvidia_metrics) if nvidia_metrics is not None else _sample_nvidia(nvidia_smi, runner, timeout)
    pci_entries, scan_error = _entries(Path(pci_root))
    pci_failures = []
    amd = []
    nvidia_pci = []
    for path in pci_entries:
        pci = canonical_pci_id(path.name)
        if pci is None:
            continue
        vendor, vendor_error = _read(path / "vendor", read_text)
        cls, cls_error = _read(path / "class", read_text)
        if vendor_error or cls_error:
            pci_failures.append(vendor_error or cls_error)
            continue
        try:
            vendor_id, class_id = int(vendor or "", 16), int(cls or "", 16)
            if not 0 <= vendor_id <= 0xffff or not 0 <= class_id <= 0xffffff:
                raise ValueError
        except ValueError:
            pci_failures.append("invalid_pci_attribute")
            continue
        if class_id >> 16 != 3:
            continue
        if vendor_id == 0x1002:
            amd.append(_amd_device(path, pci, read_text))
        elif vendor_id == 0x10DE:
            nvidia_pci.append(pci)
    known_pci = {row["pci_bus_id"] for row in devices}
    for pci in nvidia_pci:
        if pci not in known_pci:
            placeholder = _blank("nvidia", pci, None, None)
            placeholder["driver_available"] = False
            placeholder["field_status"] = {field: "driver_unavailable" for field in _TELEMETRY}
            devices.append(placeholder)
            nvidia = {"status": "partial", "reason": "nvidia_hardware_without_telemetry"}
    if not nvidia_pci and not scan_error and not pci_failures and nvidia["status"] == "unavailable":
        nvidia = {"status": "not_present", "reason": "no_nvidia_display_devices"}
    devices.extend(amd)
    devices.sort(key=lambda row: (row["pci_bus_id"] or "~", row["vendor"], row["gpu_key"] or "~"))
    overflow = len(devices) > MAX_GPUS
    devices = devices[:MAX_GPUS]
    keys = Counter(row["gpu_key"] for row in devices if row["gpu_key"])
    addresses = Counter(row["pci_bus_id"] for row in devices if row["pci_bus_id"])
    native_counts = Counter(row["native_index"] for row in devices if row["vendor"] == "nvidia" and row["native_index"] is not None)
    for index, row in enumerate(devices):
        if row["vendor"] == "nvidia" and row["native_index"] is not None and native_counts[row["native_index"]] > 1:
            row["control_index"] = row["index"] = None
            row["field_status"]["native_index"] = "ambiguous"
        row["display_index"] = row["hive_index"] = index
        observed = [row[field] is not None for field in ("temperature_c", "power_w", "fan_pwm_percent" if row["vendor"] == "amd" else "fan_speed_percent")]
        row["telemetry_status"] = "ok" if all(observed) else "partial" if any(observed) else "unavailable"
        if keys[row["gpu_key"]] > 1 or (row["pci_bus_id"] and addresses[row["pci_bus_id"]] > 1):
            row["identity_status"] = "ambiguous"
    pci_status = {"status": "unavailable" if scan_error else "partial" if pci_failures or overflow else "ok",
                  "reason": scan_error or (pci_failures[0] if pci_failures else "device_limit" if overflow else None)}
    amd_status = {"status": "unavailable" if scan_error else "partial" if pci_failures or any(not row["driver_available"] for row in amd) else "ok",
                  "reason": scan_error or (pci_failures[0] if pci_failures else "driver_unavailable" if any(not row["driver_available"] for row in amd) else None)}
    snapshot = {"version": 1, "captured_at": timestamp, "scan_complete": pci_status["status"] == "ok",
                "sources": {"pci": pci_status, "nvidia": nvidia, "amd": amd_status}, "devices": devices,
                "cpu_temperature_c": _number(cpu_temperature_c, -30, 150)}
    snapshot["snapshot_id"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return snapshot


def _snapshot_devices(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    devices = snapshot.get("devices")
    if snapshot.get("version") != 1 or not isinstance(devices, list) or len(devices) > MAX_GPUS or any(not isinstance(row, Mapping) for row in devices):
        raise ValueError("invalid GPU snapshot")
    return devices



MAX_COMPACT_SNAPSHOT_BYTES = 60 * 1024


def compact_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Keep one original snapshot binding and only helper/mapping fields.

    The private GPU context has its own 60 KiB bound, leaving room for a small
    request envelope under 64 KiB. It must not share a size budget with the
    independently bounded miner stats payload. Strings used only for display
    are shortened; physical UUID/PCI identity is never truncated or invented.
    This function performs no hardware queries and preserves device order.
    """
    devices = _snapshot_devices(snapshot)
    identifier = snapshot.get("snapshot_id")
    captured = _number(snapshot.get("captured_at"))
    if (not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{64}", identifier) is None
            or captured is None or type(snapshot.get("scan_complete")) is not bool):
        raise ValueError("invalid GPU snapshot binding")
    def display(value: Any) -> str | None:
        text = _text(value)
        return text.encode("utf-8")[:48].decode("utf-8", errors="ignore") if text else None
    rows = []
    for raw in devices:
        vendor = raw.get("vendor")
        uuid = _text(raw.get("uuid"), 128)
        pci = canonical_pci_id(raw.get("pci_bus_id"))
        if (vendor not in {"amd", "nvidia"} or not (uuid or pci)
                or (uuid is not None and (not uuid.isascii() or any(c.isspace() for c in uuid)))
                or raw.get("gpu_key") != f"{vendor}:{uuid or pci}"):
            raise ValueError("invalid GPU snapshot device identity")
        row = {"gpu_key": raw["gpu_key"], "vendor": vendor, "uuid": uuid, "pci_bus_id": pci,
               "identity_status": raw.get("identity_status") if raw.get("identity_status") in {"verified", "ambiguous", "unavailable"} else "unavailable",
               "native_index_kind": "nvml" if vendor == "nvidia" else "drm"}
        for field in ("native_index", "control_index", "index", "display_index", "hive_index"):
            row[field] = _index(raw.get(field))
        if vendor == "amd":
            row["control_index"] = row["index"] = None
        for field in ("temperature_c", "junction_temperature_c", "memory_temperature_c", "fan_speed_percent",
                      "fan_pwm_percent", "power_w", "utilization_percent"):
            row[field] = _number(raw.get(field), *_TELEMETRY[field])
        for field in ("name", "board_vendor", "vbios_version", "memory_vendor"):
            row[field] = display(raw.get(field))
        rows.append(row)
    result = {"version": 1, "snapshot_id": identifier, "captured_at": captured,
              "scan_complete": snapshot["scan_complete"], "devices": rows,
              "cpu_temperature_c": _number(snapshot.get("cpu_temperature_c"), -30, 150)}
    encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_COMPACT_SNAPSHOT_BYTES:
        raise ValueError("GPU hardware context exceeds its private channel limit")
    return result
