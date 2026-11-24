from __future__ import annotations

import ctypes as C
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import nvidia_memory_linux as memory


UUID1 = "GPU-00000000-0000-4000-8000-000000000001"
UUID2 = "GPU-00000000-0000-4000-8000-000000000002"
UUID3 = "GPU-00000000-0000-4000-8000-000000000003"
LIBRARY = Path("/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.999.88")


def selected(*uuids):
    return [
        {"uuid": uuid, "pci_bus_id": f"00000000:{index:02x}:00.0", "driver_version": "999.01"}
        for index, uuid in enumerate(uuids, 1)
    ]


class Function:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


class FakeNvml:
    def __init__(self, rows, *, init_status=0, export_status=0, table_size=2416, tamper=None):
        self.rows = rows
        self.init_calls = self.shutdown_calls = self.export_calls = 0
        self.get_calls = []
        self.tamper = tamper

        def init():
            self.init_calls += 1
            return init_status

        def shutdown():
            self.shutdown_calls += 1
            return 0

        def driver(output, size):
            output.value = b"999.88"
            return 0

        def handle(uuid, output):
            row = rows.get(uuid.decode())
            if row is None or row.get("handle_status", 0):
                return 6
            C.cast(output, C.POINTER(C.c_void_p))[0] = row["handle"]
            return 0

        self.by_handle = {row["handle"]: (uuid, row) for uuid, row in rows.items()}

        def uuid(handle, output, size):
            expected, row = self.by_handle[handle.value]
            output.value = row.get("actual_uuid", expected).encode()
            return row.get("uuid_status", 0)

        def pci(handle, output):
            _, row = self.by_handle[handle.value]
            result = C.cast(output, C.POINTER(memory._PciInfo)).contents
            bus = row["bus"]
            result.bus_id = row.get("actual_bus", f"00000000:{bus:02x}:00.0").encode()
            result.domain, result.bus, result.device = 0, row.get("numeric_bus", bus), 0
            return row.get("pci_status", 0)

        @C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32)
        def getter(handle, command, output, size):
            _, row = self.by_handle[handle]
            params = C.cast(output, C.POINTER(memory._Params)).contents
            self.get_calls.append((handle, command, size, params.count, [params.entries[i].index for i in range(2)]))
            params.entries[0].data = row.get("vendor", 1)
            params.entries[1].data = row.get("type", 21)
            if tamper:
                tamper(params)
            return row.get("get_status", 0)

        self.getter = getter  # Keep callback alive while its pointer is in the export table.
        self.table = (C.c_uint64 * 40)()
        self.table[0] = table_size
        self.table[0x100 // 8] = C.cast(getter, C.c_void_p).value

        def export(output, selector):
            self.export_calls += 1
            assert bytes(selector) == memory._SELECTOR
            C.cast(output, C.POINTER(C.c_void_p))[0] = C.addressof(self.table)
            return export_status

        self.nvmlInit_v2 = Function(init)
        self.nvmlShutdown = Function(shutdown)
        self.nvmlSystemGetDriverVersion = Function(driver)
        self.nvmlDeviceGetHandleByUUID = Function(handle)
        self.nvmlDeviceGetUUID = Function(uuid)
        self.nvmlDeviceGetPciInfo_v3 = Function(pci)
        self.nvmlInternalGetExportTable = Function(export)


def install(monkeypatch, library, *, valid_mapping=True):
    monkeypatch.setattr(memory.platform, "system", lambda: "Linux")
    monkeypatch.setattr(memory.platform, "machine", lambda: "x86_64")
    # The native loader is mocked; Windows has no POSIX dlopen constants.
    monkeypatch.setattr(memory.os, "RTLD_LOCAL", 0, raising=False)
    monkeypatch.setattr(memory.os, "RTLD_NOW", 2, raising=False)
    monkeypatch.setattr(memory, "_library_path", lambda: LIBRARY)
    loads = []

    def load(path, **kwargs):
        loads.append((path, kwargs))
        return library

    monkeypatch.setattr(memory.C, "CDLL", load)
    mapping_calls = []

    def mapped(address, size, permission, path=None):
        mapping_calls.append((address, size, permission, path))
        return valid_mapping

    monkeypatch.setattr(memory, "_mapped_range", mapped)
    return loads, mapping_calls


def test_import_does_not_load_native_library(monkeypatch):
    monkeypatch.setattr(C, "CDLL", lambda *args, **kwargs: pytest.fail("import loaded a native library"))
    importlib.reload(memory)


def test_real_ctypes_export_and_get_layout_bind_uuid_and_full_address(monkeypatch):
    library = FakeNvml({
        UUID3: {"handle": 9, "bus": 3, "vendor": 15, "type": 18},
        UUID1: {"handle": 8, "bus": 1},
        UUID2: {"handle": 7, "bus": 2, "vendor": 6},
    })
    loads, mapping_calls = install(monkeypatch, library)

    result = memory.collect_linux_memory(selected(UUID1, UUID2, UUID3))

    assert (C.sizeof(memory._PciInfo), C.sizeof(memory._Entry), C.sizeof(memory._Params), memory._Params.entries.offset) == (68, 8, 16, 8)
    assert result["0000:01:00.0"]["uuid"] == UUID1
    assert result["0000:02:00.0"]["memory_vendor"] == "SK hynix"
    assert result["0000:03:00.0"]["memory_vendor"] == "Micron"
    assert result["0000:03:00.0"]["memory_type"] == "GDDR6X"
    assert result["0000:01:00.0"]["driver_version"] == "999.88"  # No tested-driver allowlist.
    assert all(row["memory_identity_status"] == "complete" for row in result.values())
    assert all(row["memory_identity_source"] == "nvml_private" for row in result.values())
    assert library.get_calls == [(8, 0x20801301, 16, 2, [0x1C, 0x0D]), (7, 0x20801301, 16, 2, [0x1C, 0x0D]), (9, 0x20801301, 16, 2, [0x1C, 0x0D])]
    assert loads[0][0] == str(LIBRARY)
    assert mapping_calls[-1][2:] == ("x", LIBRARY)
    assert library.shutdown_calls == 1


def test_partial_public_failure_and_unknown_enums_do_not_remove_successes(monkeypatch):
    library = FakeNvml({
        UUID1: {"handle": 1, "bus": 1, "uuid_status": 6},
        UUID2: {"handle": 2, "bus": 2, "vendor": 10},  # NVAPI Micron != RM Micron.
        UUID3: {"handle": 3, "bus": 3, "vendor": 0xFFFFFFFF, "type": 999},
    })
    install(monkeypatch, library)
    result = memory.collect_linux_memory(selected(UUID1, UUID2, UUID3))
    assert "0000:01:00.0" not in result
    assert result["0000:02:00.0"]["memory_vendor"] is None
    assert result["0000:02:00.0"]["memory_type"] == "GDDR7"
    assert result["0000:02:00.0"]["memory_identity_status"] == "partial"
    assert result["0000:03:00.0"]["memory_identity_status"] == "unavailable"
    assert result["0000:03:00.0"]["vendor_raw"] == 0xFFFFFFFF
    assert library.shutdown_calls == 1


def test_nonzero_private_status_cannot_promote_written_data_to_identity(monkeypatch):
    library = FakeNvml({UUID1: {"handle": 1, "bus": 1, "get_status": 27}, UUID2: {"handle": 2, "bus": 2}})
    install(monkeypatch, library)
    result = memory.collect_linux_memory(selected(UUID1, UUID2))
    row = result["0000:01:00.0"]
    assert row["raw_status"] == 27
    assert row["vendor_raw"] == 1
    assert row["memory_vendor"] is row["memory_type"] is None
    assert row["memory_identity_status"] == "unavailable"
    assert result["0000:02:00.0"]["memory_identity_status"] == "complete"


@pytest.mark.parametrize("change", [{"actual_uuid": UUID2}, {"actual_bus": "00000000:0a:00.0"}, {"numeric_bus": 5}])
def test_positive_identity_mismatch_rejects_all_before_any_private_call(monkeypatch, change):
    library = FakeNvml({UUID1: {"handle": 1, "bus": 1}, UUID2: {"handle": 2, "bus": 2, **change}})
    # Make UUID contradiction genuinely differ from the second GPU's UUID.
    if "actual_uuid" in change:
        library.rows[UUID2]["actual_uuid"] = UUID1
    install(monkeypatch, library)
    with pytest.raises(RuntimeError, match="identity changed"):
        memory.collect_linux_memory(selected(UUID1, UUID2))
    assert library.export_calls == 0
    assert library.get_calls == []
    assert library.shutdown_calls == 1


def change_pointer(params):
    params.entries = C.cast(C.c_void_p(0xDEAD), C.POINTER(memory._Entry))


@pytest.mark.parametrize("tamper", [change_pointer, lambda p: setattr(p, "count", 3), lambda p: setattr(p.entries[0], "index", 7)])
def test_replaced_pointer_count_or_index_is_never_dereferenced_or_trusted(monkeypatch, tamper):
    library = FakeNvml({UUID1: {"handle": 1, "bus": 1}}, tamper=tamper)
    install(monkeypatch, library)
    row = memory.collect_linux_memory(selected(UUID1))["0000:01:00.0"]
    assert row["output_layout_intact"] is False
    assert row["memory_vendor"] is row["memory_type"] is None


@pytest.mark.parametrize("table_size,valid_mapping,export_status", [(0x100, True, 0), (2**32, True, 0), (2416, False, 0), (2416, True, 7)])
def test_export_pointer_guards_fail_before_native_get(monkeypatch, table_size, valid_mapping, export_status):
    library = FakeNvml({UUID1: {"handle": 1, "bus": 1}}, table_size=table_size, export_status=export_status)
    install(monkeypatch, library, valid_mapping=valid_mapping)
    with pytest.raises(RuntimeError):
        memory.collect_linux_memory(selected(UUID1))
    assert library.get_calls == []
    assert library.shutdown_calls == 1


def test_failed_initialize_does_not_shutdown_unowned_nvml(monkeypatch):
    library = FakeNvml({}, init_status=9)
    install(monkeypatch, library)
    with pytest.raises(RuntimeError, match="status 9"):
        memory.collect_linux_memory(selected(UUID1))
    assert library.shutdown_calls == 0


def test_other_platform_never_loads_native(monkeypatch):
    monkeypatch.setattr(memory.platform, "system", lambda: "Windows")
    monkeypatch.setattr(C, "CDLL", lambda *args, **kwargs: pytest.fail("loaded Linux library on Windows"))
    assert memory.collect_linux_memory(selected(UUID1)) == {}


def test_duplicate_or_incomplete_selection_rejected_before_native(monkeypatch):
    monkeypatch.setattr(C, "CDLL", lambda *args, **kwargs: pytest.fail("loaded library for invalid selection"))
    duplicate = selected(UUID1, UUID1)
    for invalid in ([], duplicate, [{"uuid": UUID1, "pci_bus_id": "01:00.0"}], [{"uuid": "MIG-123", "pci_bus_id": "0000:01:00.0"}]):
        with pytest.raises(ValueError):
            memory.collect_linux_memory(invalid)


def test_library_candidates_reject_user_owned_or_writable_file(monkeypatch):
    candidates = [Path("/system/user-owned"), Path("/system/writable"), Path("/system/driver")]
    monkeypatch.setattr(memory, "_LIBRARY_CANDIDATES", candidates)
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: path)
    entries = {
        candidates[0]: SimpleNamespace(st_mode=0o100644, st_uid=1000),
        candidates[1]: SimpleNamespace(st_mode=0o100666, st_uid=0),
        candidates[2]: SimpleNamespace(st_mode=0o100644, st_uid=0),
    }
    monkeypatch.setattr(Path, "stat", lambda path: entries[path])
    assert memory._library_path() == candidates[2]


def test_mapped_range_requires_readable_slot_and_same_executable_library(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda path: "1000-2000 r--p 0000 00:00 1 /other\n3000-4000 r-xp 0000 00:00 2 " + str(LIBRARY))
    assert memory._mapped_range(0x1100, 0x108, "r")
    assert not memory._mapped_range(0x1FFF, 8, "r")
    assert not memory._mapped_range(0x1100, 1, "x", LIBRARY)
    assert memory._mapped_range(0x3100, 1, "x", LIBRARY)
    assert not memory._mapped_range(0x3100, 1, "x", Path("/wrong/library"))
