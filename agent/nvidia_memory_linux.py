"""Optional Linux NVIDIA memory identity, called only by an isolated worker.

The public NVML functions bind physical GPUs by UUID and full PCI address. An
undocumented NVML export then issues the framebuffer GET_INFO command used by
HiveOS nvtool. This small read-only path was independently validated on NVIDIA
595.71.05, 610.43.02 and 615.71.09; those versions are evidence, not an allowlist.
No native library is loaded at import time. The caller must bound this entire
function in a separate process because a private native ABI can fail or hang.
"""

from __future__ import annotations

import ctypes as C
import os
from pathlib import Path
import platform
import re
import stat
import sys
from typing import Any


_SELECTOR = bytes.fromhex("c4fe3e6cc98f6c4ea327ee696e12f7c4")
_GET_INFO = 0x20801301
_VENDOR_INDEX = 0x1C
_TYPE_INDEX = 0x0D
_UNKNOWN = 0xFFFFFFFF
_LIBRARY_CANDIDATES = (
    Path("/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1"),
    Path("/usr/lib64/libnvidia-ml.so.1"),
    Path("/usr/lib/libnvidia-ml.so.1"),
)

# These are RM framebuffer enums, which differ from the Windows NVAPI enums.
_VENDORS = {
    1: "Samsung", 2: "Qimonda", 3: "Elpida", 4: "Etron", 5: "Nanya",
    6: "SK hynix", 7: "Mosel", 8: "Winbond", 9: "ESMT", 15: "Micron",
}
_TYPES = {
    3: "SDDR2", 4: "GDDR2", 5: "GDDR3", 6: "GDDR4", 7: "SDDR3",
    8: "GDDR5", 12: "SDDR4", 14: "HBM", 15: "HBM2", 16: "GDDR5X",
    17: "GDDR6", 18: "GDDR6X", 20: "HBM3", 21: "GDDR7",
}


class _PciInfo(C.Structure):
    _fields_ = [
        ("legacy", C.c_char * 16), ("domain", C.c_uint32),
        ("bus", C.c_uint32), ("device", C.c_uint32),
        ("device_id", C.c_uint32), ("subsystem_id", C.c_uint32),
        ("bus_id", C.c_char * 32),
    ]


class _Entry(C.Structure):
    _fields_ = [("index", C.c_uint32), ("data", C.c_uint32)]


class _Params(C.Structure):
    _fields_ = [("count", C.c_uint32), ("entries", C.POINTER(_Entry))]


def _bdf(value: Any) -> str:
    match = re.fullmatch(
        r"([0-9a-fA-F]{4}|[0-9a-fA-F]{8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])",
        value if isinstance(value, str) else "",
    )
    if not match or int(match[1], 16) > 0xFFFF or int(match[3], 16) > 0x1F:
        raise ValueError("invalid complete PCI address")
    return f"{int(match[1], 16):04x}:{int(match[2], 16):02x}:{int(match[3], 16):02x}.{match[4]}"


def _selected_gpus(selected: list[dict[str, str]]) -> list[tuple[str, str]]:
    if not isinstance(selected, list) or not 1 <= len(selected) <= 64:
        raise ValueError("explicit selected GPU identities required")
    result: list[tuple[str, str]] = []
    uuids: set[str] = set()
    buses: set[str] = set()
    for row in selected:
        uuid = row.get("uuid") if isinstance(row, dict) else None
        if not isinstance(uuid, str) or not re.fullmatch(
            r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid
        ):
            raise ValueError("physical GPU UUID required")
        bus = _bdf(row.get("pci_bus_id"))
        if uuid.lower() in uuids or bus in buses:
            raise ValueError("duplicate selected GPU UUID or PCI address")
        uuids.add(uuid.lower())
        buses.add(bus)
        result.append((uuid, bus))
    return result


def _library_path() -> Path:
    for candidate in _LIBRARY_CANDIDATES:
        try:
            path = candidate.resolve(strict=True)
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022:
            return path
    raise RuntimeError("trusted installed system NVML library unavailable")


def _mapped_range(address: int, size: int, permission: str, library: Path | None = None) -> bool:
    if not isinstance(address, int) or address <= 0 or size <= 0:
        return False
    for line in Path("/proc/self/maps").read_text().splitlines():
        parts = line.split(None, 5)
        lo, hi = (int(value, 16) for value in parts[0].split("-"))
        if lo <= address and address + size <= hi and permission in parts[1]:
            return library is None or (len(parts) == 6 and parts[5] == str(library))
    return False


def _bind(lib: Any, name: str, args: list[Any]) -> Any:
    function = getattr(lib, name)
    function.argtypes, function.restype = args, C.c_int
    return function


def _checked(function: Any, name: str, *args: Any) -> None:
    status = int(function(*args))
    if status != 0:
        raise RuntimeError(f"{name} failed with public NVML status {status}")


def _private_getter(lib: Any, path: Path) -> Any:
    export = _bind(lib, "nvmlInternalGetExportTable", [C.POINTER(C.c_void_p), C.c_void_p])
    selector = (C.c_ubyte * 16).from_buffer_copy(_SELECTOR)
    table = C.c_void_p()
    status = int(export(C.byref(table), selector))
    # Private statuses are intentionally not interpreted as public NVML/RM enums.
    if status != 0 or not table.value or not _mapped_range(table.value, 8, "r"):
        raise RuntimeError(f"private NVML export unavailable (raw status {status})")
    size = C.c_uint64.from_address(table.value).value
    if not 0x108 <= size <= 1024 * 1024 or not _mapped_range(table.value, 0x108, "r"):
        raise RuntimeError("private NVML export has no complete getter slot")
    address = C.c_void_p.from_address(table.value + 0x100).value
    if not address or not _mapped_range(address, 1, "x", path):
        raise RuntimeError("private NVML getter outside installed executable library mapping")
    return C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32)(address)


def collect_linux_memory(selected: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Query selected physical GPUs without trusting their enumeration order.

    Missing public per-device interfaces omit only the affected device. A
    positive UUID/BDF contradiction rejects the selection before any private
    query. Failures of the library/ABI are raised to the bounded worker caller.
    Expected driver/VBIOS versions are cache references, not driver allowlists.
    """
    selection = _selected_gpus(selected)
    if (
        platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}
        or C.sizeof(C.c_void_p) != 8 or sys.byteorder != "little"
    ):
        return {}
    if (C.sizeof(_PciInfo), C.sizeof(_Entry), C.sizeof(_Params), _Params.entries.offset) != (68, 8, 16, 8):
        raise RuntimeError("unexpected native NVML ctypes layout")
    path = _library_path()
    lib = C.CDLL(str(path), mode=os.RTLD_LOCAL | os.RTLD_NOW)
    init = _bind(lib, "nvmlInit_v2", [])
    shutdown = _bind(lib, "nvmlShutdown", [])
    get_driver = _bind(lib, "nvmlSystemGetDriverVersion", [C.c_void_p, C.c_uint32])
    get_handle = _bind(lib, "nvmlDeviceGetHandleByUUID", [C.c_char_p, C.POINTER(C.c_void_p)])
    get_uuid = _bind(lib, "nvmlDeviceGetUUID", [C.c_void_p, C.c_void_p, C.c_uint32])
    get_pci = _bind(lib, "nvmlDeviceGetPciInfo_v3", [C.c_void_p, C.POINTER(_PciInfo)])
    initialized = False
    try:
        _checked(init, "nvmlInit_v2")
        initialized = True
        version = C.create_string_buffer(128)
        _checked(get_driver, "nvmlSystemGetDriverVersion", version, len(version))
        driver = version.value.decode("ascii", errors="strict")
        if not driver:
            raise RuntimeError("empty actual NVIDIA driver version")
        handles: list[tuple[Any, str, str]] = []
        for expected_uuid, expected_bus in selection:
            handle, uuid, pci = C.c_void_p(), C.create_string_buffer(128), _PciInfo()
            if int(get_handle(expected_uuid.encode("ascii"), C.byref(handle))) != 0 or not handle.value:
                continue
            if int(get_uuid(handle, uuid, len(uuid))) != 0 or int(get_pci(handle, C.byref(pci))) != 0:
                continue
            actual_uuid = uuid.value.decode("ascii", errors="strict")
            actual_bus = _bdf(bytes(pci.bus_id).decode("ascii", errors="strict"))
            numeric_bus = f"{pci.domain:04x}:{pci.bus:02x}:{pci.device:02x}"
            if (
                actual_uuid.lower() != expected_uuid.lower() or actual_bus != expected_bus
                or actual_bus.rsplit(".", 1)[0] != numeric_bus
            ):
                raise RuntimeError("selected GPU UUID/PCI identity changed")
            handles.append((handle, actual_uuid, actual_bus))
        if not handles:
            return {}
        getter = _private_getter(lib, path)
        result: dict[str, dict[str, Any]] = {}
        for handle, uuid, bus in handles:
            entries = (_Entry * 2)(_Entry(_VENDOR_INDEX, _UNKNOWN), _Entry(_TYPE_INDEX, _UNKNOWN))
            params = _Params(2, entries)
            pointer = C.cast(params.entries, C.c_void_p).value
            status = int(getter(handle, _GET_INFO, C.byref(params), C.sizeof(params)))
            intact = (
                params.count == 2 and C.cast(params.entries, C.c_void_p).value == pointer
                and entries[0].index == _VENDOR_INDEX and entries[1].index == _TYPE_INDEX
            )
            vendor_raw, type_raw = int(entries[0].data), int(entries[1].data)
            vendor = _VENDORS.get(vendor_raw) if status == 0 and intact else None
            memory_type = _TYPES.get(type_raw) if status == 0 and intact else None
            completeness = "complete" if vendor and memory_type else "partial" if vendor or memory_type else "unavailable"
            result[bus] = {
                "uuid": uuid, "pci_bus_id": bus, "driver_version": driver,
                "memory_vendor": vendor, "memory_type": memory_type,
                "memory_identity_source": "nvml_private", "memory_identity_status": completeness,
                "raw_status": status, "vendor_raw": vendor_raw, "type_raw": type_raw,
                "output_layout_intact": intact,
            }
        return result
    finally:
        if initialized:
            try:
                shutdown()
            except (ArithmeticError, OSError, TypeError, ValueError):
                pass
