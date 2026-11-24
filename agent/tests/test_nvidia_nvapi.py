from __future__ import annotations

import ctypes
import pytest

from agent.nvidia_nvapi import collect_windows_memory, windows_nvidia_memory_by_bus


INITIALIZE = 0x0150E828
UNLOAD = 0xD22BDD7E
ENUM_PHYSICAL_GPUS = 0xE5AC921F
GPU_GET_BUS_ID = 0x1BE0B8E5
GPU_GET_BUS_SLOT_ID = 0x2A0A350F
GPU_GET_RAM_MAKER = 0x42AEA16A
GPU_GET_RAM_TYPE = 0x57F7CAAC

StatusFunction = ctypes.CFUNCTYPE(ctypes.c_int)
EnumFunction = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_uint),
)
GpuUintFunction = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint),
)
GpuEnumFunction = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int),
)


class FakeQuery:
    def __init__(self, callbacks):
        self.callbacks = callbacks
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, identifier):
        value = getattr(identifier, "value", identifier)
        self.calls.append(value)
        callback = self.callbacks.get(value)
        if callback is None:
            return None
        return ctypes.cast(callback, ctypes.c_void_p).value


class FakeNvapi:
    def __init__(
        self,
        gpus=(),
        *,
        initialize_status=0,
        enumerate_status=0,
        reported_count=None,
        missing=(),
    ):
        self.gpus = {gpu[0]: gpu for gpu in gpus}
        self.order = [gpu[0] for gpu in gpus]
        self.initialize_calls = 0
        self.unload_calls = 0
        self.memory_calls = []

        @StatusFunction
        def initialize():
            self.initialize_calls += 1
            return initialize_status

        @StatusFunction
        def unload():
            self.unload_calls += 1
            return 0

        @EnumFunction
        def enumerate_gpus(handles, count):
            count[0] = len(self.order) if reported_count is None else reported_count
            if enumerate_status == 0:
                for index, handle in enumerate(self.order[:64]):
                    handles[index] = handle
            return enumerate_status

        @GpuUintFunction
        def get_bus_id(handle, output):
            gpu = self.gpus.get(handle)
            if gpu is None or gpu[1] is None:
                return -1
            output[0] = gpu[1]
            return 0

        @GpuUintFunction
        def get_bus_slot_id(handle, output):
            gpu = self.gpus.get(handle)
            if gpu is None or (len(gpu) > 4 and gpu[4] is None):
                return -1
            output[0] = gpu[4] if len(gpu) > 4 else 0
            return 0

        @GpuEnumFunction
        def get_ram_maker(handle, output):
            self.memory_calls.append(("maker", handle))
            gpu = self.gpus.get(handle)
            if gpu is None or gpu[2] is None:
                return -1
            output[0] = gpu[2]
            return 0

        @GpuEnumFunction
        def get_ram_type(handle, output):
            self.memory_calls.append(("type", handle))
            gpu = self.gpus.get(handle)
            if gpu is None or gpu[3] is None:
                return -1
            output[0] = gpu[3]
            return 0

        callbacks = {
            INITIALIZE: initialize,
            UNLOAD: unload,
            ENUM_PHYSICAL_GPUS: enumerate_gpus,
            GPU_GET_BUS_ID: get_bus_id,
            GPU_GET_BUS_SLOT_ID: get_bus_slot_id,
            GPU_GET_RAM_MAKER: get_ram_maker,
            GPU_GET_RAM_TYPE: get_ram_type,
        }
        for identifier in missing:
            callbacks.pop(identifier, None)
        # Retain every CFUNCTYPE object for the lifetime of returned pointers.
        self.callbacks = callbacks
        self.nvapi_QueryInterface = FakeQuery(callbacks)


def loader_for(library, calls):
    def load(name, **kwargs):
        calls.append((name, kwargs))
        return library

    return load


def selected(*bdfs):
    return [
        {"uuid": f"GPU-test-{index}", "pci_bus_id": bdf,
         "driver_version": "999.01", "vbios_version": "FE.ED.00.00.05"}
        for index, bdf in enumerate(bdfs)
    ]


def read(library, *bdfs):
    return collect_windows_memory(
        selected(*bdfs), system_name="Windows", library_loader=loader_for(library, [])
    )


def labels(row):
    return {key: row[key] for key in ("memory_vendor", "memory_type") if key in row}


def test_reads_samsung_gddr7_and_loads_only_from_system32():
    library = FakeNvapi([(0x1111, 1, 1, 16)])
    loads = []

    result = collect_windows_memory(
        selected("0000:01:00.0"),
        system_name="Windows",
        library_loader=loader_for(library, loads),
    )

    assert result["00000000:01:00.0"] == {
        "uuid": "GPU-test-0", "pci_bus_id": "00000000:01:00.0",
        "memory_vendor": "Samsung", "memory_type": "GDDR7",
        "memory_vendor_source": "nvapi", "memory_type_source": "nvapi",
        "memory_vendor_status": 0, "memory_type_status": 0,
        "memory_vendor_raw": 1, "memory_type_raw": 16, "memory_probe_status": "ok",
    }
    assert loads == [("nvapi64.dll", {"winmode": 0x00000800})]
    assert library.initialize_calls == 1
    assert library.unload_calls == 1
    assert set(library.nvapi_QueryInterface.calls) == {
        INITIALIZE, UNLOAD, ENUM_PHYSICAL_GPUS, GPU_GET_BUS_ID,
        GPU_GET_BUS_SLOT_ID, GPU_GET_RAM_MAKER, GPU_GET_RAM_TYPE,
    }


def test_matches_multiple_gpus_by_bus_instead_of_enumeration_index():
    library = FakeNvapi(
        [
            (0x2222, 5, 10, 14),
            (0x1111, 1, 6, 16),
        ]
    )

    result = read(library, "00000000:01:00.0", "00000000:05:00.0")

    assert labels(result["00000000:01:00.0"]) == {
        "memory_vendor": "SK hynix",
        "memory_type": "GDDR7",
    }
    assert labels(result["00000000:05:00.0"]) == {
        "memory_vendor": "Micron",
        "memory_type": "GDDR6",
    }


def test_unsupported_field_or_unknown_enum_is_omitted_without_raw_numbers():
    library = FakeNvapi(
        [
            (0x1111, 1, None, 16),
            (0x2222, 2, 999, 999),
        ]
    )

    result = read(library, "00000000:01:00.0", "00000000:02:00.0")

    assert labels(result["00000000:01:00.0"]) == {"memory_type": "GDDR7"}
    assert result["00000000:01:00.0"]["memory_vendor_status"] == -1
    assert result["00000000:01:00.0"]["memory_probe_status"] == "partial"
    unknown = result["00000000:02:00.0"]
    assert labels(unknown) == {}
    assert unknown["memory_vendor_raw"] == 999
    assert unknown["memory_type_raw"] == 999
    assert unknown["memory_probe_status"] == "unknown"
    assert library.unload_calls == 1


@pytest.mark.parametrize("first_fields", [(1, 16), (None, None), (999, 999)])
def test_duplicate_bus_is_dropped_as_ambiguous(first_fields):
    library = FakeNvapi(
        [
            (0x1111, 1, *first_fields),
            (0x2222, 1, 10, 15),
        ]
    )

    result = read(library, "00000000:01:00.0")

    assert result == {}
    assert library.unload_calls == 1
    assert library.memory_calls == []


def test_driver_count_over_hard_limit_fails_closed_and_unloads():
    library = FakeNvapi([], reported_count=65)

    result = read(library, "00000000:01:00.0")

    assert result == {}
    assert library.unload_calls == 1


def test_initialize_failure_does_not_call_unload():
    library = FakeNvapi([], initialize_status=-1)

    result = read(library, "00000000:01:00.0")

    assert result == {}
    assert library.initialize_calls == 1
    assert library.unload_calls == 0


def test_missing_required_interface_and_loader_error_degrade_to_empty():
    library = FakeNvapi([], missing=(GPU_GET_BUS_ID,))
    assert (
        collect_windows_memory(
            selected("00000000:01:00.0"),
            system_name="Windows",
            library_loader=loader_for(library, []),
        )
        == {}
    )
    assert library.initialize_calls == 0

    def failed_loader(_name, **_kwargs):
        raise OSError("driver unavailable")

    assert (
        collect_windows_memory(
            selected("00000000:01:00.0"),
            system_name="Windows", library_loader=failed_loader
        )
        == {}
    )


def test_non_windows_never_attempts_to_load_nvapi():
    def forbidden_loader(_name, **_kwargs):
        raise AssertionError("loader must not be called")

    assert (
        collect_windows_memory(
            selected("00000000:01:00.0"),
            system_name="Linux", library_loader=forbidden_loader
        )
        == {}
    )


def test_full_selected_domain_and_nonzero_device_are_preserved():
    library = FakeNvapi([(0x1111, 1, 10, 14, 0x1A)])
    result = read(library, "0002:01:1a.0")
    assert list(result) == ["00000002:01:1a.0"]
    assert result["00000002:01:1a.0"]["uuid"] == "GPU-test-0"
    assert result["00000002:01:1a.0"]["memory_vendor"] == "Micron"


def test_distinct_slots_on_one_bus_are_bound_without_enumeration_order():
    library = FakeNvapi([(0x2222, 1, 10, 14, 5), (0x1111, 1, 6, 16, 3)])
    result = read(library, "0000:01:03.0", "0000:01:05.0")
    assert result["00000000:01:03.0"]["memory_vendor"] == "SK hynix"
    assert result["00000000:01:05.0"]["memory_vendor"] == "Micron"


@pytest.mark.parametrize("bdfs", [
    ("0000:01:00.0", "0001:01:00.0"),
    ("0000:01:00.0", "0000:01:00.1"),
    ("0000:01:00.0", "00000000:01:00.0"),
    ("0000:01:00.1",),
])
def test_missing_domain_or_function_information_is_never_guessed(bdfs):
    library = FakeNvapi([(0x1111, 1, 1, 16)])
    assert read(library, *bdfs) == {}
    assert library.memory_calls == []


def test_duplicate_selected_uuid_drops_both_identities():
    library = FakeNvapi([(0x1111, 1, 1, 16), (0x2222, 2, 6, 16)])
    rows = selected("0000:01:00.0", "0000:02:00.0")
    rows[1]["uuid"] = rows[0]["uuid"]
    assert collect_windows_memory(rows, system_name="Windows", library_loader=loader_for(library, [])) == {}
    assert library.initialize_calls == 0


def test_missing_bus_getter_cannot_match():
    library = FakeNvapi([(0x1111, 1, 1, 16)], missing=(GPU_GET_BUS_ID,))
    assert read(library, "0000:01:00.0") == {}
    assert library.initialize_calls == 0


@pytest.mark.parametrize("slot", [32, 0x300, 0xFFFFFFFF])
def test_successfully_reported_invalid_slot_does_not_enable_bus_fallback(slot):
    library = FakeNvapi([(0x1111, 1, 1, 16, slot)])
    assert read(library, "0000:01:00.0") == {}
    assert library.unload_calls == 1


@pytest.mark.parametrize("missing_slot", [True, False])
def test_optional_slot_uses_unique_bus_and_preserves_reported_full_identity(missing_slot):
    library = FakeNvapi(
        [(0x1111, 1, 1, 16, None)],
        missing=(GPU_GET_BUS_SLOT_ID,) if missing_slot else (),
    )
    row = read(library, "0002:01:1a.3")["00000002:01:1a.3"]
    assert row["uuid"] == "GPU-test-0"
    assert row["pci_bus_id"] == "00000002:01:1a.3"
    assert labels(row) == {"memory_vendor": "Samsung", "memory_type": "GDDR7"}
    assert library.unload_calls == 1


@pytest.mark.parametrize("bdfs", [
    ("0000:01:00.0", "0001:01:00.0"),
    ("0000:01:00.0", "0000:01:01.0"),
    ("0000:01:00.0", "0000:01:00.1"),
])
def test_optional_slot_fallback_rejects_any_selected_bus_collision(bdfs):
    library = FakeNvapi([(0x1111, 1, 1, 16)], missing=(GPU_GET_BUS_SLOT_ID,))
    assert read(library, *bdfs) == {}
    assert library.memory_calls == []


def test_optional_slot_fallback_rejects_duplicate_native_bus():
    library = FakeNvapi(
        [(0x1111, 1, 1, 16), (0x2222, 1, 6, 16)], missing=(GPU_GET_BUS_SLOT_ID,),
    )
    assert read(library, "0000:01:00.0") == {}
    assert library.memory_calls == []


@pytest.mark.parametrize("bad_native", [(0x2222, None, 6, 16), (0, 2, 6, 16)])
def test_any_failed_native_bus_read_or_null_handle_disables_all_bus_fallback(bad_native):
    library = FakeNvapi(
        [(0x1111, 1, 1, 16), bad_native], missing=(GPU_GET_BUS_SLOT_ID,),
    )
    assert read(library, "0000:01:00.0", "0000:02:00.0") == {}
    assert library.memory_calls == []
    assert library.unload_calls == 1


def test_malformed_selected_row_disables_bus_fallback():
    library = FakeNvapi([(0x1111, 1, 1, 16)], missing=(GPU_GET_BUS_SLOT_ID,))
    rows = selected("0000:01:00.0") + [{"uuid": "GPU-bad", "pci_bus_id": "bad"}]
    assert collect_windows_memory(rows, system_name="Windows", library_loader=loader_for(library, [])) == {}
    assert library.memory_calls == []


def test_successful_slot_mismatch_is_not_replaced_with_bus_fallback():
    library = FakeNvapi([(0x1111, 1, 1, 16, 3)])
    assert read(library, "0000:01:00.0") == {}
    assert library.memory_calls == []


def test_missing_maker_interface_preserves_successful_type():
    library = FakeNvapi([(0x1111, 1, 1, 16)], missing=(GPU_GET_RAM_MAKER,))
    row = read(library, "0000:01:00.0")["00000000:01:00.0"]
    assert labels(row) == {"memory_type": "GDDR7"}
    assert row["memory_vendor_status"] is None
    assert row["memory_probe_status"] == "partial"


def test_all_per_gpu_query_errors_are_explicit_without_output_labels():
    library = FakeNvapi([(0x1111, 1, None, None)])
    row = read(library, "0000:01:00.0")["00000000:01:00.0"]
    assert labels(row) == {}
    assert row["memory_probe_status"] == "query_failed"
    assert row["memory_vendor_status"] == row["memory_type_status"] == -1


def test_legacy_entry_point_requires_full_identity_and_cannot_invent_bdf():
    library = FakeNvapi([(0x1111, 1, 1, 16)])
    assert windows_nvidia_memory_by_bus(system_name="Windows", library_loader=loader_for(library, [])) == {}
    assert library.initialize_calls == 0
