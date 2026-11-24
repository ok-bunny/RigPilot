"""Read-only NVIDIA memory identity collection through Windows NVAPI.

Public NVML queries and ``nvidia-smi`` do not expose the DRAM manufacturer or
memory generation used by consumer NVIDIA boards. The Windows driver exposes
those values through undocumented NVAPI query-interface entries.  This module
keeps that optional, best-effort enrichment isolated from normal inventory:
missing DLLs, missing interfaces, unsupported GPUs, and driver errors all
degrade to an empty result.

The DLL is loaded by basename with ``LOAD_LIBRARY_SEARCH_SYSTEM32``.  Never
fall back to the current directory, ``PATH``, or a configurable DLL location.
"""

from __future__ import annotations

import ctypes
import platform
import re
from typing import Any, Callable


# Initialize, unload, enumeration and bus lookup are documented NVAPI entries.
# RAM maker/type are undocumented driver query-interface entries and must stay
# optional: an NVIDIA driver may remove or change them without notice.
_NVAPI_INITIALIZE = 0x0150E828
_NVAPI_UNLOAD = 0xD22BDD7E
_NVAPI_ENUM_PHYSICAL_GPUS = 0xE5AC921F
_NVAPI_GPU_GET_BUS_ID = 0x1BE0B8E5
_NVAPI_GPU_GET_BUS_SLOT_ID = 0x2A0A350F
_NVAPI_GPU_GET_RAM_MAKER = 0x42AEA16A
_NVAPI_GPU_GET_RAM_TYPE = 0x57F7CAAC

_NVAPI_OK = 0
_NVAPI_MAX_PHYSICAL_GPUS = 64
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800

_StatusFunction = ctypes.CFUNCTYPE(ctypes.c_int)
_EnumPhysicalGpusFunction = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_uint),
)
_GpuUintFunction = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint),
)
_GpuEnumFunction = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int),
)


# NVAPI enums are append-only.  Do not expose an unknown numeric value as a
# label: a future driver may add an enum before this Agent is updated.
_RAM_MAKERS = {
    1: "Samsung",
    2: "Qimonda",
    3: "Elpida",
    4: "Etron",
    5: "Nanya",
    6: "SK hynix",
    7: "Mosel",
    8: "Winbond",
    9: "ESMT",
    10: "Micron",
}

_RAM_TYPES = {
    1: "SDRAM",
    2: "DDR1",
    3: "DDR2",
    4: "GDDR2",
    5: "GDDR3",
    6: "GDDR4",
    7: "DDR3",
    8: "GDDR5",
    9: "LPDDR2",
    10: "GDDR5X",
    # 11-13 and 15 are deliberately omitted: the hidden enum is not part of
    # NVIDIA's supported API, and we do not turn an unverified raw value into
    # a potentially incorrect user-facing label.
    14: "GDDR6",
    # Empirically verified on a GeForce RTX 5080 with the current driver.
    16: "GDDR7",
}


def _interface(query: Any, identifier: int, prototype: Any) -> Any | None:
    """Resolve one bounded NVAPI function pointer, or return ``None``."""

    try:
        address = query(identifier)
        if isinstance(address, ctypes.c_void_p):
            address = address.value
        if not isinstance(address, int) or address <= 0:
            return None
        return prototype(address)
    except (ArithmeticError, OSError, TypeError, ValueError):
        return None


_BDF_RE = re.compile(r"([0-9a-f]{4}|[0-9a-f]{8}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])", re.I)


def _selected_identities(selected: list[dict[str, Any]]) -> tuple[
    dict[tuple[int, int], dict[str, str]], dict[int, dict[str, str]]
]:
    """Keep only unambiguous full SMI identities that NVAPI can corroborate.

    NVAPI's public bus/slot getters do not return the PCI domain or function.
    Slot matching requires unique bus+device and function zero. The optional
    bus fallback requires uniqueness across the complete SMI bus inventory.
    Both paths preserve the reported full domain/device/function.
    """

    groups: dict[tuple[int, int], list[dict[str, str]]] = {}
    buses: dict[int, list[dict[str, str]]] = {}
    uuid_counts: dict[str, int] = {}
    bdf_counts: dict[str, int] = {}
    complete = True
    for row in selected:
        if not isinstance(row, dict):
            complete = False
            continue
        uuid = row.get("uuid")
        bdf = row.get("pci_bus_id")
        if not isinstance(uuid, str) or not uuid.strip() or not isinstance(bdf, str):
            complete = False
            continue
        match = _BDF_RE.fullmatch(bdf.strip())
        if match is None:
            complete = False
            continue
        domain, bus, device, function = (int(part, 16) for part in match.groups())
        if device > 0x1F:
            complete = False
            continue
        canonical = f"{domain:08x}:{bus:02x}:{device:02x}.{function:x}"
        identity = {"uuid": uuid.strip(), "pci_bus_id": canonical}
        groups.setdefault((bus, device), []).append(identity)
        buses.setdefault(bus, []).append(identity)
        uuid_counts[identity["uuid"]] = uuid_counts.get(identity["uuid"], 0) + 1
        bdf_counts[canonical] = bdf_counts.get(canonical, 0) + 1
    slots = {
        slot: rows[0]
        for slot, rows in groups.items()
        if len(rows) == 1
        and rows[0]["pci_bus_id"].endswith(".0")
        and uuid_counts[rows[0]["uuid"]] == 1
        and bdf_counts[rows[0]["pci_bus_id"]] == 1
    }
    # If any selected row is malformed, uniqueness of the complete SMI bus
    # inventory is unknown. That must never enable the optional bus fallback.
    unique_buses = {
        bus: rows[0]
        for bus, rows in buses.items()
        if complete
        and len(rows) == 1
        and uuid_counts[rows[0]["uuid"]] == 1
        and bdf_counts[rows[0]["pci_bus_id"]] == 1
    }
    return slots, unique_buses


def collect_windows_memory(
    selected: list[dict[str, Any]],
    *,
    system_name: str | None = None,
    library_loader: Callable[..., Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read NVAPI memory fields in the isolated memory worker only.

    ``selected`` is the complete current nvidia-smi GPU identity selection,
    including UUID and full PCI address. Unknown values retain raw diagnostics
    without manufacturing labels. Missing/ambiguous identity never merges.
    This function must not run in the Agent heartbeat process: its caller owns
    the process timeout and crash isolation. Injection arguments are test seams.
    """

    if (system_name or platform.system()) != "Windows":
        return {}
    if not isinstance(selected, list) or not 0 < len(selected) <= _NVAPI_MAX_PHYSICAL_GPUS:
        return {}
    selected_slots, selected_buses = _selected_identities(selected)
    if not selected_slots and not selected_buses:
        return {}

    loader = library_loader or getattr(ctypes, "WinDLL", None)
    if loader is None:
        return {}
    try:
        library = loader(
            "nvapi64.dll",
            winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32,
        )
        query = getattr(library, "nvapi_QueryInterface", None)
        if query is None:
            return {}
        query.argtypes = [ctypes.c_uint]
        query.restype = ctypes.c_void_p

        initialize = _interface(query, _NVAPI_INITIALIZE, _StatusFunction)
        unload = _interface(query, _NVAPI_UNLOAD, _StatusFunction)
        enumerate_gpus = _interface(
            query, _NVAPI_ENUM_PHYSICAL_GPUS, _EnumPhysicalGpusFunction
        )
        get_bus_id = _interface(query, _NVAPI_GPU_GET_BUS_ID, _GpuUintFunction)
        get_bus_slot_id = _interface(query, _NVAPI_GPU_GET_BUS_SLOT_ID, _GpuUintFunction)
        get_ram_maker = _interface(
            query, _NVAPI_GPU_GET_RAM_MAKER, _GpuEnumFunction
        )
        get_ram_type = _interface(
            query, _NVAPI_GPU_GET_RAM_TYPE, _GpuEnumFunction
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return {}

    if any(function is None for function in (initialize, unload, enumerate_gpus, get_bus_id)):
        return {}
    if get_ram_maker is None and get_ram_type is None:
        return {}

    initialized = False
    try:
        if initialize() != _NVAPI_OK:
            return {}
        initialized = True

        handles = (ctypes.c_void_p * _NVAPI_MAX_PHYSICAL_GPUS)()
        count = ctypes.c_uint()
        if enumerate_gpus(handles, ctypes.byref(count)) != _NVAPI_OK:
            return {}
        if count.value > _NVAPI_MAX_PHYSICAL_GPUS:
            return {}

        result: dict[str, dict[str, Any]] = {}
        handles_by_slot: dict[tuple[int, int], list[ctypes.c_void_p]] = {}
        native_buses: dict[int, int] = {}
        native_identities: list[tuple[ctypes.c_void_p, int, int | None]] = []
        complete_native_buses = True
        for index in range(count.value):
            raw_handle = handles[index]
            if not raw_handle:
                complete_native_buses = False
                continue
            handle = ctypes.c_void_p(raw_handle)
            bus = ctypes.c_uint()
            try:
                if get_bus_id(handle, ctypes.byref(bus)) != _NVAPI_OK:
                    complete_native_buses = False
                    continue
            except (ArithmeticError, OSError, TypeError, ValueError):
                complete_native_buses = False
                continue
            if bus.value > 0xFF:
                complete_native_buses = False
                continue
            native_buses[bus.value] = native_buses.get(bus.value, 0) + 1
            slot_value = None
            if get_bus_slot_id is not None:
                slot = ctypes.c_uint(0xFFFFFFFF)
                try:
                    if get_bus_slot_id(handle, ctypes.byref(slot)) == _NVAPI_OK:
                        slot_value = slot.value
                except (ArithmeticError, OSError, TypeError, ValueError):
                    pass
            native_identities.append((handle, bus.value, slot_value))
            if slot_value is not None:
                handles_by_slot.setdefault((bus.value, slot_value), []).append(handle)

        for handle, bus, slot in native_identities:
            if slot is None:
                # Older drivers may omit the public slot getter. A unique bus
                # on BOTH complete inventories is sufficient without inventing
                # the SMI domain/device/function. One failed bus read disables
                # this fallback globally because native uniqueness is unknown.
                identity = (
                    selected_buses.get(bus)
                    if complete_native_buses and native_buses[bus] == 1 else None
                )
            else:
                identity = selected_slots.get((bus, slot)) if slot <= 0x1F else None
                if len(handles_by_slot[(bus, slot)]) != 1:
                    identity = None
            if identity is None:
                continue
            details: dict[str, Any] = dict(identity)
            details["memory_vendor_status"] = None
            details["memory_type_status"] = None
            if get_ram_maker is not None:
                maker = ctypes.c_int(-1)
                try:
                    status = int(get_ram_maker(handle, ctypes.byref(maker)))
                    details["memory_vendor_status"] = status
                    if status == _NVAPI_OK:
                        details["memory_vendor_raw"] = maker.value
                        label = _RAM_MAKERS.get(maker.value)
                        if label is not None:
                            details["memory_vendor"] = label
                            details["memory_vendor_source"] = "nvapi"
                except (ArithmeticError, OSError, TypeError, ValueError):
                    pass
            if get_ram_type is not None:
                ram_type = ctypes.c_int(-1)
                try:
                    status = int(get_ram_type(handle, ctypes.byref(ram_type)))
                    details["memory_type_status"] = status
                    if status == _NVAPI_OK:
                        details["memory_type_raw"] = ram_type.value
                        label = _RAM_TYPES.get(ram_type.value)
                        if label is not None:
                            details["memory_type"] = label
                            details["memory_type_source"] = "nvapi"
                except (ArithmeticError, OSError, TypeError, ValueError):
                    pass
            known_count = sum(key in details for key in ("memory_vendor", "memory_type"))
            details["memory_probe_status"] = (
                "ok" if known_count == 2 else "partial" if known_count else
                "unknown" if _NVAPI_OK in (details["memory_vendor_status"], details["memory_type_status"])
                else "query_failed"
            )
            result[identity["pci_bus_id"]] = details
        return result
    except (ArithmeticError, OSError, TypeError, ValueError):
        return {}
    finally:
        if initialized:
            try:
                unload()
            except (ArithmeticError, OSError, TypeError, ValueError):
                pass


def windows_nvidia_memory_by_bus(
    *,
    selected: list[dict[str, Any]] | None = None,
    system_name: str | None = None,
    library_loader: Callable[..., Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compatibility entry point; full SMI identities are now mandatory."""

    return collect_windows_memory(
        selected or [], system_name=system_name, library_loader=library_loader
    )
