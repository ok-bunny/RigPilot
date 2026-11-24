"""Exercise identity snapshots through actual ctypes GetName pointer output."""
from __future__ import annotations

import ctypes

import pytest

from agent.gpu_oc import GpuOcError, NvmlGpuOcBackend


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


DEVICES = (
    ("GPU-stable-A", b"NVIDIA GeForce RTX 5080"),
    ("GPU-stable-B", b"NVIDIA GeForce RTX 5070"),
    ("GPU-stable-C", "NVIDIA GPU 测试".encode("utf-8")),
)


class TypedNameLibrary:
    """Supply NVML reads; GetName really crosses a typed C callback boundary."""

    NAME_CALLBACK = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_char), ctypes.c_uint
    )

    def __init__(self, *, snapshots=None, failure=None, terminate_name=True):
        self.snapshots = snapshots or [DEVICES, DEVICES, DEVICES]
        self.failure = failure
        self.terminate_name = terminate_name
        self.session = 0
        self.calls = []
        self.name_pointer_details = []
        self.nvmlInit_v2 = FakeFunction(self._init)
        self.nvmlShutdown = FakeFunction(self._shutdown)
        self.nvmlErrorString = FakeFunction(lambda _code: b"mock NVML Not Supported")
        self.nvmlDeviceGetCount_v2 = FakeFunction(self._count)
        self.nvmlDeviceGetHandleByIndex_v2 = FakeFunction(self._handle)
        self.nvmlDeviceGetUUID = FakeFunction(self._uuid)
        self.nvmlDeviceGetName = self.NAME_CALLBACK(self._name)
        for name in (
            "nvmlDeviceSetPowerManagementLimit",
            "nvmlDeviceSetMemoryLockedClocks",
            "nvmlDeviceResetMemoryLockedClocks",
            "nvmlDeviceSetClockOffsets",
            "nvmlDeviceSetFanSpeed_v2",
        ):
            setattr(self, name, FakeFunction(self._mutation(name)))

    @property
    def devices(self):
        return self.snapshots[min(self.session - 1, len(self.snapshots) - 1)]

    def _failed(self, operation):
        return self.failure == (operation, self.session)

    def _init(self):
        self.session += 1
        self.calls.append(("init", self.session))
        return 0

    def _shutdown(self):
        self.calls.append(("shutdown", self.session))
        return 0

    def _count(self, output):
        self.calls.append(("count", self.session))
        if self._failed("count"):
            return 3
        output._obj.value = len(self.devices)
        return 0

    def _handle(self, index, output):
        self.calls.append(("handle", self.session, index.value))
        if self._failed("handle") or index.value >= len(self.devices):
            return 3
        output._obj.value = 0x1000 + index.value
        return 0

    def _uuid(self, handle, output, length):
        index = handle.value - 0x1000
        self.calls.append(("uuid", self.session, index))
        if self._failed("uuid") or index >= len(self.devices):
            return 3
        value = self.devices[index][0].encode("ascii")
        if len(value) + 1 > length.value:
            return 7
        output.value = value
        return 0

    def _name(self, handle, output, length):
        # A CFUNCTYPE callback receives native scalar integers and a real
        # POINTER(c_char), rather than the mock's high-level string return.
        index = handle - 0x1000
        self.calls.append(("name", self.session, index, length))
        self.name_pointer_details.append((handle, isinstance(output, ctypes.POINTER(ctypes.c_char)), length))
        if self._failed("name") or index >= len(self.devices):
            return 3
        value = self.devices[index][1]
        raw = value + b"\0" if self.terminate_name else value
        if len(raw) > length:
            return 7
        ctypes.memmove(output, raw, len(raw))
        return 0

    def _mutation(self, name):
        def write(*_args):
            self.calls.append(("write", name))
            return 0
        return write


def assert_readonly_and_closed(library):
    assert not [call for call in library.calls if call[0] == "write"]
    assert [call[1] for call in library.calls if call[0] == "init"] == [
        call[1] for call in library.calls if call[0] == "shutdown"
    ]


def test_identity_snapshot_get_name_uses_official_pointer_binding_and_complete_reads():
    library = TypedNameLibrary()
    backend = NvmlGpuOcBackend(library)

    result = backend.enumerate_device_identities()

    assert result == {
        gpu_uuid: {"index": index, "name": name.decode("utf-8")}
        for index, (gpu_uuid, name) in enumerate(DEVICES)
    }
    assert library.nvmlDeviceGetName.restype is ctypes.c_int
    assert list(library.nvmlDeviceGetName.argtypes) == [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_char), ctypes.c_uint
    ]
    assert library.name_pointer_details == [(0x1000 + i, True, 96) for i in range(3)]
    assert [call for call in library.calls if call[0] == "count"] == [("count", 1), ("count", 3)]
    assert [call[2] for call in library.calls if call[0] == "uuid" and call[1] == 1] == [0, 1, 2]
    assert [call[2] for call in library.calls if call[0] == "uuid" and call[1] == 3] == [0, 1, 2]
    assert [call for call in library.calls if call[0] in {"uuid", "name"} and call[1] == 2] == [
        ("uuid", 2, 0), ("name", 2, 0, 96),
        ("uuid", 2, 1), ("name", 2, 1, 96),
        ("uuid", 2, 2), ("name", 2, 2, 96),
    ]
    assert_readonly_and_closed(library)


@pytest.mark.parametrize("change", ["during-name-swap", "during-name-remove", "final-swap", "final-add", "final-remove"])
def test_identity_snapshot_rejects_gpu_swap_addition_or_removal(change):
    swapped = (DEVICES[1], DEVICES[0], DEVICES[2])
    middle = swapped if change == "during-name-swap" else DEVICES[:1] if change == "during-name-remove" else DEVICES
    final = {
        "final-swap": swapped,
        "final-add": (*DEVICES, ("GPU-stable-D", b"NVIDIA GeForce RTX 5060")),
        "final-remove": DEVICES[:1],
    }.get(change, DEVICES)
    library = TypedNameLibrary(snapshots=[DEVICES, middle, final])

    with pytest.raises(GpuOcError):
        NvmlGpuOcBackend(library).enumerate_device_identities()

    assert_readonly_and_closed(library)


@pytest.mark.parametrize("failure", [("count", 1), ("handle", 1), ("uuid", 1), ("handle", 2), ("uuid", 2), ("name", 2), ("count", 3), ("uuid", 3)])
def test_identity_snapshot_never_returns_partial_data_after_vendor_read_failure(failure):
    library = TypedNameLibrary(failure=failure)

    with pytest.raises(GpuOcError, match="Not Supported"):
        NvmlGpuOcBackend(library).enumerate_device_identities()

    assert_readonly_and_closed(library)


def test_identity_snapshot_missing_name_symbol_is_not_a_trusted_snapshot():
    library = TypedNameLibrary()
    del library.nvmlDeviceGetName

    with pytest.raises(GpuOcError, match="nvmlDeviceGetName"):
        NvmlGpuOcBackend(library).enumerate_device_identities()

    assert_readonly_and_closed(library)


@pytest.mark.parametrize("name", [b"", b"   ", b"NVIDIA\tRTX 5080", b"NVIDIA\x7fRTX 5080", b"NVIDIA RTX 5080\n", b"\xffinvalid-utf8"])
def test_identity_snapshot_rejects_invalid_name_bytes(name):
    invalid = ((DEVICES[0][0], name), *DEVICES[1:])
    library = TypedNameLibrary(snapshots=[DEVICES, invalid, DEVICES])

    with pytest.raises((GpuOcError, UnicodeDecodeError)):
        NvmlGpuOcBackend(library).enumerate_device_identities()

    assert_readonly_and_closed(library)


def test_identity_snapshot_rejects_successful_name_result_without_nul_terminator():
    invalid = ((DEVICES[0][0], b"X" * 96), *DEVICES[1:])
    library = TypedNameLibrary(snapshots=[DEVICES, invalid, DEVICES], terminate_name=False)

    with pytest.raises(GpuOcError):
        NvmlGpuOcBackend(library).enumerate_device_identities()

    assert_readonly_and_closed(library)
