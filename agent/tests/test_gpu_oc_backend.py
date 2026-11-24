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


class FakeNvml:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.nvmlInit_v2 = FakeFunction(lambda: 0)
        self.nvmlShutdown = FakeFunction(lambda: 0)
        self.nvmlErrorString = FakeFunction(lambda _code: b"fake error")
        self.nvmlDeviceGetHandleByIndex_v2 = FakeFunction(self._handle)
        self.nvmlDeviceGetPowerManagementLimitConstraints = FakeFunction(
            self._power_limits
        )
        self.nvmlDeviceGetPowerManagementDefaultLimit = FakeFunction(
            self._default_power
        )
        self.nvmlDeviceSetPowerManagementLimit = FakeFunction(self._set_power)
        self.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(self._offset_limits)
        self.nvmlDeviceSetGpcClkVfOffset = FakeFunction(self._set_core_offset)
        self.nvmlDeviceGetMemClkMinMaxVfOffset = FakeFunction(self._offset_limits)
        self.nvmlDeviceSetMemClkVfOffset = FakeFunction(self._set_memory_offset)
        self.nvmlDeviceGetMaxClockInfo = FakeFunction(self._max_clock)
        self.nvmlDeviceSetGpuLockedClocks = FakeFunction(self._set_core_lock)
        self.nvmlDeviceSetMemoryLockedClocks = FakeFunction(self._set_memory_lock)
        self.nvmlDeviceResetGpuLockedClocks = FakeFunction(self._reset_core_lock)
        self.nvmlDeviceResetMemoryLockedClocks = FakeFunction(
            self._reset_memory_lock
        )
        self.nvmlDeviceGetNumFans = FakeFunction(self._fan_count)
        self.nvmlDeviceSetFanSpeed_v2 = FakeFunction(self._set_fan)
        self.nvmlDeviceSetDefaultFanSpeed_v2 = FakeFunction(self._reset_fan)

    @staticmethod
    def _handle(index, output):
        output._obj.value = 1000 + index.value
        return 0

    @staticmethod
    def _power_limits(_handle, minimum, maximum):
        minimum._obj.value = 50_000
        maximum._obj.value = 300_000
        return 0

    def _set_power(self, _handle, value):
        self.calls.append(("power", value.value))
        return 0

    @staticmethod
    def _default_power(_handle, value):
        value._obj.value = 150_000
        return 0

    @staticmethod
    def _offset_limits(_handle, minimum, maximum):
        minimum._obj.value = -5000
        maximum._obj.value = 5000
        return 0

    def _set_core_offset(self, _handle, value):
        self.calls.append(("core_offset", value.value))
        return 0

    def _set_memory_offset(self, _handle, value):
        self.calls.append(("memory_offset", value.value))
        return 0

    @staticmethod
    def _max_clock(_handle, clock_type, maximum):
        maximum._obj.value = 3000 if clock_type.value == 0 else 12000
        return 0

    def _set_core_lock(self, _handle, minimum, maximum):
        self.calls.append(("core_lock", minimum.value, maximum.value))
        return 0

    def _set_memory_lock(self, _handle, minimum, maximum):
        self.calls.append(("memory_lock", minimum.value, maximum.value))
        return 0

    def _reset_core_lock(self, _handle):
        self.calls.append(("reset_core_lock",))
        return 0

    def _reset_memory_lock(self, _handle):
        self.calls.append(("reset_memory_lock",))
        return 0

    @staticmethod
    def _fan_count(_handle, count):
        count._obj.value = 1
        return 0

    @staticmethod
    def _fan_limits(_handle, minimum, maximum):
        minimum._obj.value = 30
        maximum._obj.value = 100
        return 0

    def _set_fan(self, _handle, fan, value):
        self.calls.append(("fan", fan.value, value.value))
        return 0

    def _reset_fan(self, _handle, fan):
        self.calls.append(("reset_fan", fan.value))
        return 0


class FakeNvmlWithClockOffsets(FakeNvml):
    def __init__(self) -> None:
        super().__init__()
        self.offset_queries: list[tuple[int, int, int]] = []
        self.nvmlDeviceGetClockOffsets = FakeFunction(self._get_clock_offsets)
        self.nvmlDeviceSetClockOffsets = FakeFunction(self._set_clock_offsets)

    def _get_clock_offsets(self, _handle, info):
        value = info._obj
        self.offset_queries.append((value.version, value.type, value.pstate))
        value.clockOffsetMHz = 0
        value.minClockOffsetMHz = -1000 if value.type == 0 else -5000
        value.maxClockOffsetMHz = 1000 if value.type == 0 else 5000
        return 0

    def _set_clock_offsets(self, _handle, info):
        value = info._obj
        self.calls.append(
            (
                "clock_offset_v1",
                value.version,
                value.type,
                value.pstate,
                value.clockOffsetMHz,
            )
        )
        return 0


class FakeNvmlWithUuid(FakeNvml):
    def __init__(self) -> None:
        super().__init__()
        self.uuid_queries: list[int] = []
        self.nvmlDeviceGetUUID = FakeFunction(self._uuid)
        self.nvmlDeviceGetCount_v2 = FakeFunction(self._count)

    @staticmethod
    def _count(output):
        output._obj.value = 3
        return 0

    def _uuid(self, handle, output, length):
        index = int(handle.value) - 1000
        self.uuid_queries.append(index)
        value = f"GPU-stable-{index}".encode("ascii")
        assert len(value) + 1 <= length.value
        output.value = value
        return 0


class FakeNvmlWithFanReadback(FakeNvml):
    def __init__(self) -> None:
        super().__init__()
        self.fan_queries: list[int] = []
        self.nvmlDeviceGetFanSpeed_v2 = FakeFunction(self._fan_speed)
        self.nvmlDeviceGetMinMaxFanSpeed = FakeFunction(self._fan_limits)

    def _fan_speed(self, _handle, fan, speed):
        self.fan_queries.append(fan.value)
        speed._obj.value = 47
        return 0


def test_nvml_backend_verifies_index_uuid_without_mutating_gpu():
    library = FakeNvmlWithUuid()
    backend = NvmlGpuOcBackend(library)

    assert backend.verify_device_uuid(1, "GPU-stable-1") == "GPU-stable-1"
    assert library.uuid_queries == [1]
    assert library.calls == []

    with pytest.raises(GpuOcError, match="identity changed"):
        backend.verify_device_uuid(1, "GPU-another-device")
    assert library.uuid_queries == [1, 1]
    assert library.calls == []


def test_nvml_backend_resolves_complete_uuid_set_without_mutating_gpu():
    library = FakeNvmlWithUuid()
    backend = NvmlGpuOcBackend(library)

    assert backend.resolve_device_indices(["GPU-stable-2", "GPU-stable-0"]) == {
        "GPU-stable-0": 0,
        "GPU-stable-2": 2,
    }
    assert library.uuid_queries == [0, 1, 2]
    assert library.calls == []


def test_nvml_backend_enumerates_all_uuid_indices_without_mutating_gpu():
    library = FakeNvmlWithUuid()
    backend = NvmlGpuOcBackend(library)

    assert backend.enumerate_device_indices() == {
        "GPU-stable-0": 0,
        "GPU-stable-1": 1,
        "GPU-stable-2": 2,
    }
    assert library.uuid_queries == [0, 1, 2]
    assert library.calls == []


def test_nvml_backend_strict_resolution_still_rejects_a_missing_uuid():
    library = FakeNvmlWithUuid()
    backend = NvmlGpuOcBackend(library)

    with pytest.raises(GpuOcError, match="no longer reported: GPU-removed"):
        backend.resolve_device_indices(["GPU-stable-1", "GPU-removed"])
    assert library.uuid_queries == [0, 1, 2]
    assert library.calls == []


def test_nvml_backend_applies_offsets_and_absolute_locks_on_separate_gpus():
    library = FakeNvmlWithFanReadback()
    result = NvmlGpuOcBackend(library).apply(
        [
            {
                "index": 0,
                "core_clock_offset_mhz": 100,
                "memory_clock_offset_mhz": 800,
                "power_limit_w": 125,
                "fan_percent": 65,
            },
            {
                "index": 1,
                "core_clock_lock_mhz": 1450,
                "memory_clock_lock_mhz": 8100,
            },
        ]
    )

    assert library.calls == [
        ("power", 125_000),
        ("core_offset", 100),
        ("memory_offset", 800),
        ("fan", 0, 65),
        ("core_lock", 1450, 1450),
        ("memory_lock", 8100, 8100),
    ]
    assert result["operations"][0]["applied"] == [
        "power_limit_w",
        "core_clock_offset_mhz",
        "memory_clock_offset_mhz",
        "fan_percent",
    ]
    assert result["operations"][1]["applied"] == [
        "core_clock_lock_mhz",
        "memory_clock_lock_mhz",
    ]


def test_nvml_backend_applies_offset_then_lock_for_the_same_clock():
    library = FakeNvml()
    result = NvmlGpuOcBackend(library).apply(
        [
            {
                "index": 0,
                "core_clock_offset_mhz": 100,
                "core_clock_lock_mhz": 1450,
                "memory_clock_offset_mhz": 700,
                "memory_clock_lock_mhz": 8100,
            }
        ]
    )
    assert library.calls == [
        ("core_offset", 100),
        ("memory_offset", 700),
        ("core_lock", 1450, 1450),
        ("memory_lock", 8100, 8100),
    ]
    assert result["operations"][0]["applied"] == [
        "core_clock_offset_mhz",
        "memory_clock_offset_mhz",
        "core_clock_lock_mhz",
        "memory_clock_lock_mhz",
    ]


def test_nvml_backend_rejects_lock_above_hardware_max_before_changes():
    library = FakeNvml()
    with pytest.raises(GpuOcError, match="hardware maximum 3000 MHz"):
        NvmlGpuOcBackend(library).apply(
            [{"index": 0, "core_clock_lock_mhz": 3200}]
        )
    assert library.calls == []


def test_offset_only_profile_changes_only_requested_nvml_controls():
    library = FakeNvml()
    NvmlGpuOcBackend(library).apply(
        [
            {
                "index": 0,
                "core_clock_offset_mhz": 75,
                "memory_clock_offset_mhz": 500,
            }
        ]
    )
    assert library.calls == [
        ("core_offset", 75),
        ("memory_offset", 500),
    ]


def test_reset_returns_only_previously_owned_fields_to_defaults():
    library = FakeNvml()
    result = NvmlGpuOcBackend(library).reset(
        [
            {
                "index": 0,
                "power_limit_w": 125,
                "core_clock_offset_mhz": 75,
                "memory_clock_lock_mhz": 8100,
                "fan_percent": 65,
            }
        ]
    )
    assert library.calls == [
        ("power", 150_000),
        ("core_offset", 0),
        ("reset_memory_lock",),
        ("reset_fan", 0),
    ]
    assert result["operations"][0]["reset"] == [
        "power_limit_w",
        "core_clock_offset_mhz",
        "memory_clock_lock_mhz",
        "fan_percent",
    ]


def test_transition_reapplies_equal_settings_after_a_possible_driver_reset():
    library = FakeNvml()
    settings = [{"index": 0, "core_clock_offset_mhz": 75}]
    result = NvmlGpuOcBackend(library).transition(settings, settings)
    assert library.calls == [
        ("core_offset", 0),
        ("core_offset", 75),
    ]
    assert result["operations"][0]["applied"] == ["core_clock_offset_mhz"]


def test_transition_compensates_attempted_fields_and_does_not_touch_later_fields():
    library = FakeNvml()

    def reject_core_offset(_handle, value):
        library.calls.append(("core_offset_failed", value.value))
        return 3

    library.nvmlDeviceSetGpcClkVfOffset = FakeFunction(reject_core_offset)
    with pytest.raises(GpuOcError, match="core_clock_offset_mhz failed") as failure:
        NvmlGpuOcBackend(library).transition(
            [],
            [
                {
                    "index": 0,
                    "power_limit_w": 125,
                    "core_clock_offset_mhz": 100,
                    "memory_clock_lock_mhz": 8100,
                }
            ],
        )

    assert library.calls == [
        ("power", 125_000),
        ("core_offset_failed", 100),
        ("core_offset_failed", 0),
        ("power", 150_000),
    ]
    assert failure.value.write_attempted is True
    assert failure.value.recovery["status"] == "partial"
    assert failure.value.recovery["confirmed_reset"] == [
        {"index": 0, "reset": ["power_limit_w"]}
    ]


def test_transition_verifies_uuid_in_the_same_session_as_the_write():
    library = FakeNvmlWithUuid()
    backend = NvmlGpuOcBackend(library)

    with pytest.raises(GpuOcError, match="identity changed"):
        backend.transition(
            [],
            [
                {
                    "index": 0,
                    "_expected_gpu_uuid": "GPU-stable-another",
                    "power_limit_w": 125,
                }
            ],
        )
    assert library.uuid_queries == [0]
    assert library.calls == []

    backend.transition(
        [],
        [
            {
                "index": 0,
                "_expected_gpu_uuid": "GPU-stable-0",
                "power_limit_w": 125,
            }
        ],
    )
    # Complete read-only preflight, then revalidation in the write session.
    assert library.uuid_queries == [0, 0, 0]
    assert library.calls == [("power", 125_000)]


def test_supported_fields_uses_read_only_runtime_queries_and_omits_not_supported():
    library = FakeNvml()
    library.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(
        lambda _handle, _minimum, _maximum: 3  # NVML_ERROR_NOT_SUPPORTED
    )

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert fields[0] == [
        "power_limit_w",
        "memory_clock_offset_mhz",
    ]
    assert library.calls == []


def test_supported_fields_never_guesses_setter_only_lock_or_fan_support():
    library = FakeNvml()

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert "memory_clock_lock_mhz" not in fields[0]
    assert "fan_percent" not in fields[0]
    assert "core_clock_lock_mhz" not in fields[0]
    assert "power_limit_w" in fields[0]


def test_supported_fields_advertises_fan_after_read_only_per_fan_probe():
    library = FakeNvmlWithFanReadback()
    backend = NvmlGpuOcBackend(library)

    fields = backend.supported_fields([0])

    assert "fan_percent" in fields[0]
    assert backend.supported_fan_limits() == {0: (30, 100)}
    assert library.fan_queries == [0]
    # Capability collection must never set a speed or switch fan policy.
    assert library.calls == []


def test_supported_fields_omits_fan_when_readback_fails_for_the_device():
    library = FakeNvmlWithFanReadback()
    library.nvmlDeviceGetFanSpeed_v2 = FakeFunction(
        lambda _handle, _fan, _speed: 3  # NVML_ERROR_NOT_SUPPORTED
    )

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert "fan_percent" not in fields[0]
    assert library.calls == []


def test_supported_fields_accepts_driver_reported_fan_speed_above_100_percent():
    library = FakeNvmlWithFanReadback()
    library.nvmlDeviceGetFanSpeed_v2 = FakeFunction(
        lambda _handle, _fan, speed: setattr(speed._obj, "value", 115) or 0
    )

    assert "fan_percent" in NvmlGpuOcBackend(library).supported_fields([0])[0]


def test_fan_apply_rejects_value_outside_device_hardware_range_before_writes():
    library = FakeNvmlWithFanReadback()
    library.nvmlDeviceGetMinMaxFanSpeed = FakeFunction(
        lambda _handle, minimum, maximum: (
            setattr(minimum._obj, "value", 45),
            setattr(maximum._obj, "value", 90),
            0,
        )[-1]
    )

    with pytest.raises(GpuOcError, match="hardware range 45-90%"):
        NvmlGpuOcBackend(library).apply([{"index": 0, "fan_percent": 40}])
    assert library.calls == []


def test_supported_fields_omits_fan_when_hardware_range_has_no_safe_intersection():
    library = FakeNvmlWithFanReadback()
    library.nvmlDeviceGetMinMaxFanSpeed = FakeFunction(
        lambda _handle, minimum, maximum: (
            setattr(minimum._obj, "value", 0),
            setattr(maximum._obj, "value", 20),
            0,
        )[-1]
    )

    backend = NvmlGpuOcBackend(library)
    assert "fan_percent" not in backend.supported_fields([0])[0]
    assert backend.supported_fan_limits() == {}


def test_supported_fields_omits_fan_when_driver_reports_malformed_limits():
    library = FakeNvmlWithFanReadback()
    library.nvmlDeviceGetMinMaxFanSpeed = FakeFunction(
        lambda _handle, minimum, maximum: (
            setattr(minimum._obj, "value", 30),
            setattr(maximum._obj, "value", 110),
            0,
        )[-1]
    )

    backend = NvmlGpuOcBackend(library)
    assert "fan_percent" not in backend.supported_fields([0])[0]
    assert backend.supported_fan_limits() == {}


@pytest.mark.parametrize("invalid", [None, {}, [], True, 17])
def test_uuid_resolution_rejects_non_string_members_before_driver_queries(invalid, monkeypatch):
    library = FakeNvml()
    backend = NvmlGpuOcBackend(library)
    monkeypatch.setattr(backend, "enumerate_device_indices",
                        lambda: pytest.fail("invalid UUIDs must be rejected before querying NVML"))
    with pytest.raises(GpuOcError, match="expected GPU UUID set is invalid"):
        backend.resolve_device_indices(["GPU-valid", invalid])
    assert library.calls == []


def test_fan_reset_rejects_unbounded_driver_fan_count_before_writes():
    library = FakeNvml()
    library.nvmlDeviceGetNumFans = FakeFunction(
        lambda _handle, count: setattr(count._obj, "value", 33) or 0
    )

    with pytest.raises(GpuOcError, match="invalid controllable fan count"):
        NvmlGpuOcBackend(library).reset([{"index": 0, "fan_percent": 65}])
    assert library.calls == []


def test_partial_multi_fan_apply_returns_successful_fans_to_default():
    library = FakeNvmlWithFanReadback()
    library.nvmlDeviceGetNumFans = FakeFunction(
        lambda _handle, count: setattr(count._obj, "value", 2) or 0
    )

    def fail_second_fan(_handle, fan, value):
        library.calls.append(("fan", fan.value, value.value))
        return 3 if fan.value == 1 else 0

    library.nvmlDeviceSetFanSpeed_v2 = FakeFunction(fail_second_fan)
    with pytest.raises(GpuOcError, match="set GPU 0 fan 1 failed"):
        NvmlGpuOcBackend(library).apply(
            [{"index": 0, "fan_percent": 65}]
        )
    assert library.calls == [
        ("fan", 0, 65),
        ("fan", 1, 65),
        ("reset_fan", 1),
        ("reset_fan", 0),
    ]


def test_partial_multi_fan_reset_restores_only_successfully_reset_fans():
    library = FakeNvml()
    library.nvmlDeviceGetNumFans = FakeFunction(
        lambda _handle, count: setattr(count._obj, "value", 2) or 0
    )

    def fail_second_reset(_handle, fan):
        library.calls.append(("reset_fan", fan.value))
        return 3 if fan.value == 1 else 0

    library.nvmlDeviceSetDefaultFanSpeed_v2 = FakeFunction(fail_second_reset)
    with pytest.raises(GpuOcError, match="reset GPU 0 fan 1 failed"):
        NvmlGpuOcBackend(library).reset(
            [{"index": 0, "fan_percent": 65}]
        )
    assert library.calls == [
        ("reset_fan", 0),
        ("reset_fan", 1),
        ("fan", 0, 65),
    ]


def test_replacement_clock_offset_api_is_preferred_and_uses_versioned_p0_struct():
    library = FakeNvmlWithClockOffsets()
    # Prove that a modern driver does not fall through to the deprecated API.
    library.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(
        lambda *_args: pytest.fail("legacy core offset query should not run")
    )
    library.nvmlDeviceGetMemClkMinMaxVfOffset = FakeFunction(
        lambda *_args: pytest.fail("legacy memory offset query should not run")
    )

    backend = NvmlGpuOcBackend(library)
    assert backend.supported_fields([0]) == {
        0: [
            "power_limit_w",
            "core_clock_offset_mhz",
            "memory_clock_offset_mhz",
        ]
    }
    backend.apply(
        [
            {
                "index": 0,
                "core_clock_offset_mhz": 125,
                "memory_clock_offset_mhz": 900,
            }
        ]
    )
    backend.reset(
        [
            {
                "index": 0,
                "core_clock_offset_mhz": 125,
                "memory_clock_offset_mhz": 900,
            }
        ]
    )

    assert set(library.offset_queries) == {
        (0x1000018, 0, 0),
        (0x1000018, 2, 0),
    }
    assert library.calls == [
        ("clock_offset_v1", 0x1000018, 0, 0, 125),
        ("clock_offset_v1", 0x1000018, 2, 0, 900),
        ("clock_offset_v1", 0x1000018, 0, 0, 0),
        ("clock_offset_v1", 0x1000018, 2, 0, 0),
    ]


def test_replacement_clock_offset_not_supported_falls_back_to_legacy_query():
    library = FakeNvmlWithClockOffsets()
    library.nvmlDeviceGetClockOffsets = FakeFunction(
        lambda _handle, _info: 3  # NVML_ERROR_NOT_SUPPORTED
    )

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert fields[0] == [
        "power_limit_w",
        "core_clock_offset_mhz",
        "memory_clock_offset_mhz",
    ]
    assert library.calls == []


def test_replacement_clock_offset_range_is_used_for_preflight():
    library = FakeNvmlWithClockOffsets()

    with pytest.raises(GpuOcError, match=r"outside hardware range -1000-1000 MHz"):
        NvmlGpuOcBackend(library).apply(
            [{"index": 0, "core_clock_offset_mhz": 1001}]
        )

    assert library.calls == []


def test_linux_read_only_probe_advertises_documented_locked_clock_controls(
    monkeypatch,
):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Linux")
    library = FakeNvml()

    def architecture(_handle, value):
        value._obj.value = 7  # NVML_DEVICE_ARCH_AMPERE
        return 0

    def current_clock(_handle, clock_type, value):
        value._obj.value = 1800 if clock_type.value == 0 else 8000
        return 0

    library.nvmlDeviceGetArchitecture = FakeFunction(architecture)
    library.nvmlDeviceGetClockInfo = FakeFunction(current_clock)

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert fields[0] == [
        "power_limit_w",
        "core_clock_offset_mhz",
        "memory_clock_offset_mhz",
        "core_clock_lock_mhz",
        "memory_clock_lock_mhz",
    ]
    # Capability discovery must never set or reset a user clock.
    assert library.calls == []


def test_linux_locked_clock_probe_respects_architecture_floor_and_read_failures(
    monkeypatch,
):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Linux")
    library = FakeNvml()

    def architecture(_handle, value):
        value._obj.value = 6  # NVML_DEVICE_ARCH_TURING
        return 0

    def current_clock(_handle, clock_type, value):
        if clock_type.value == 2:
            return 3  # NVML_ERROR_NOT_SUPPORTED
        value._obj.value = 1800
        return 0

    library.nvmlDeviceGetArchitecture = FakeFunction(architecture)
    library.nvmlDeviceGetClockInfo = FakeFunction(current_clock)

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert "core_clock_lock_mhz" in fields[0]
    assert "memory_clock_lock_mhz" not in fields[0]
    assert library.calls == []


class FakeNvmlWithLockedClockReadback(FakeNvml):
    def __init__(self) -> None:
        super().__init__()
        self.lock_queries: list[tuple] = []
        self.nvmlDeviceGetArchitecture = FakeFunction(self._architecture)
        self.nvmlDeviceGetMaxClockInfo = FakeFunction(self._queried_max_clock)
        self.nvmlDeviceGetClockInfo = FakeFunction(self._current_clock)

    def _architecture(self, handle, value):
        self.lock_queries.append(("architecture", handle.value))
        value._obj.value = 7  # NVML_DEVICE_ARCH_AMPERE
        return 0

    def _queried_max_clock(self, handle, clock_type, value):
        self.lock_queries.append(("maximum", handle.value, clock_type.value))
        return self._max_clock(handle, clock_type, value)

    def _current_clock(self, handle, clock_type, value):
        self.lock_queries.append(("current", handle.value, clock_type.value))
        value._obj.value = 1800 if clock_type.value == 0 else 8000
        return 0


def test_windows_read_only_probe_advertises_core_and_memory_lock(monkeypatch):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Windows")
    library = FakeNvmlWithLockedClockReadback()

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert "memory_clock_lock_mhz" in fields[0]
    assert "core_clock_lock_mhz" in fields[0]
    assert library.lock_queries == [
        ("architecture", 1000),
        ("maximum", 1000, 0),
        ("current", 1000, 0),
        ("maximum", 1000, 2),
        ("current", 1000, 2),
    ]
    assert library.calls == []


@pytest.mark.parametrize(
    "field, symbol",
    [
        ("core_clock_lock_mhz", "nvmlDeviceSetGpuLockedClocks"),
        ("core_clock_lock_mhz", "nvmlDeviceResetGpuLockedClocks"),
        ("memory_clock_lock_mhz", "nvmlDeviceSetMemoryLockedClocks"),
        ("memory_clock_lock_mhz", "nvmlDeviceResetMemoryLockedClocks"),
    ],
)
def test_windows_locked_clock_requires_both_command_symbols(monkeypatch, field, symbol):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Windows")
    library = FakeNvmlWithLockedClockReadback()
    delattr(library, symbol)

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    other = "memory_clock_lock_mhz" if field == "core_clock_lock_mhz" else "core_clock_lock_mhz"
    assert field not in fields[0]
    assert other in fields[0]
    assert library.calls == []


@pytest.mark.parametrize(
    "architecture, core_supported, memory_supported",
    [(4, False, False), (5, True, False), (6, True, False), (7, True, True), (0xFFFFFFFF, False, False)],
    ids=["pascal", "volta", "turing", "ampere", "unknown"],
)
def test_windows_locked_clock_respects_documented_architecture_floors(
    monkeypatch, architecture, core_supported, memory_supported
):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Windows")
    library = FakeNvmlWithLockedClockReadback()
    library.nvmlDeviceGetArchitecture = FakeFunction(
        lambda _handle, value: setattr(value._obj, "value", architecture) or 0
    )

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert ("core_clock_lock_mhz" in fields[0]) == core_supported
    assert ("memory_clock_lock_mhz" in fields[0]) == memory_supported
    assert library.calls == []


@pytest.mark.parametrize(
    "symbol, failure",
    [
        ("nvmlDeviceGetArchitecture", "not_supported"),
        ("nvmlDeviceGetArchitecture", "exception"),
        ("nvmlDeviceGetMaxClockInfo", "not_supported"),
        ("nvmlDeviceGetMaxClockInfo", "exception"),
        ("nvmlDeviceGetMaxClockInfo", "zero"),
        ("nvmlDeviceGetClockInfo", "not_supported"),
        ("nvmlDeviceGetClockInfo", "exception"),
        ("nvmlDeviceGetClockInfo", "zero"),
    ],
)
def test_windows_locked_clock_read_probe_failure_never_advertises_or_writes(
    monkeypatch, symbol, failure
):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Windows")
    library = FakeNvmlWithLockedClockReadback()

    def fail_read(*arguments):
        if failure == "exception":
            raise RuntimeError("readback failed")
        if failure == "zero":
            arguments[-1]._obj.value = 0
            return 0
        return 3  # NVML_ERROR_NOT_SUPPORTED

    setattr(library, symbol, FakeFunction(fail_read))
    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert "memory_clock_lock_mhz" not in fields[0]
    assert "core_clock_lock_mhz" not in fields[0]
    assert "power_limit_w" in fields[0]
    assert library.calls == []


@pytest.mark.parametrize("rejected_domain", [0, 2], ids=["core", "memory"])
def test_windows_inventory_reports_locked_clocks_per_card_and_domain_without_writes(
    monkeypatch, rejected_domain
):
    from agent.inventory import annotate_nvidia_oc_capabilities

    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Windows")
    library = FakeNvmlWithLockedClockReadback()
    read_maximum = library.nvmlDeviceGetMaxClockInfo

    def maximum(handle, clock_type, value):
        if handle.value == 1001 and clock_type.value == rejected_domain:
            return 3  # One card's driver rejects this clock domain.
        return read_maximum(handle, clock_type, value)

    library.nvmlDeviceGetMaxClockInfo = FakeFunction(maximum)
    gpus = [
        {"index": 0, "vendor": "nvidia"},
        {"index": 1, "vendor": "nvidia"},
        {"index": 2, "vendor": "amd"},
    ]
    annotate_nvidia_oc_capabilities(
        gpus, backend_factory=lambda: NvmlGpuOcBackend(library)
    )

    assert "memory_clock_lock_mhz" in gpus[0]["oc_capabilities"]
    assert "core_clock_lock_mhz" in gpus[0]["oc_capabilities"]
    assert ("core_clock_lock_mhz" in gpus[1]["oc_capabilities"]) == (rejected_domain != 0)
    assert ("memory_clock_lock_mhz" in gpus[1]["oc_capabilities"]) == (rejected_domain != 2)
    assert "oc_capabilities" not in gpus[2]
    assert library.calls == []


@pytest.mark.parametrize("operation", ["apply", "reset"])
@pytest.mark.parametrize(
    "field, value, setter, resetter",
    [
        ("core_clock_lock_mhz", 1800, "nvmlDeviceSetGpuLockedClocks", "nvmlDeviceResetGpuLockedClocks"),
        ("memory_clock_lock_mhz", 8000, "nvmlDeviceSetMemoryLockedClocks", "nvmlDeviceResetMemoryLockedClocks"),
    ],
    ids=["core", "memory"],
)
@pytest.mark.parametrize(
    "code, message",
    [(3, "Not Supported"), (4, "Insufficient Permissions")],
    ids=["not_supported", "no_permission"],
)
def test_windows_locked_clock_vendor_write_errors_remain_explicit(
    monkeypatch, operation, field, value, setter, resetter, code, message
):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Windows")
    library = FakeNvmlWithLockedClockReadback()
    library.nvmlErrorString = FakeFunction(lambda _code: message.encode("ascii"))

    def reject(*_arguments):
        library.calls.append((operation, code))
        return code

    symbol = setter if operation == "apply" else resetter
    setattr(library, symbol, FakeFunction(reject))
    backend = NvmlGpuOcBackend(library)
    # A successful read-only probe does not guarantee the driver will permit a write.
    assert field in backend.supported_fields([0])[0]
    assert library.calls == []

    with pytest.raises(GpuOcError, match=message):
        getattr(backend, operation)([{"index": 0, field: value}])

    expected = [(operation, code)]
    if operation == "apply":
        expected.append(("reset_core_lock" if field == "core_clock_lock_mhz" else "reset_memory_lock",))
    assert library.calls == expected


def test_linux_unknown_architecture_never_satisfies_locked_clock_floor(monkeypatch):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: "Linux")
    library = FakeNvml()
    library.nvmlDeviceGetArchitecture = FakeFunction(
        lambda _handle, value: setattr(value._obj, "value", 0xFFFFFFFF) or 0
    )
    library.nvmlDeviceGetClockInfo = FakeFunction(
        lambda _handle, _clock_type, value: setattr(value._obj, "value", 2000) or 0
    )

    fields = NvmlGpuOcBackend(library).supported_fields([0])

    assert "core_clock_lock_mhz" not in fields[0]
    assert "memory_clock_lock_mhz" not in fields[0]
    assert library.calls == []


class FaultingCoreNvml(FakeNvmlWithUuid):
    """Model a failed setter which may already have changed the target card."""

    def __init__(self, *, failure_code=3, reset_code=0, fault_queries=False):
        super().__init__()
        self.failure_code = failure_code
        self.reset_code = reset_code
        self.fault_queries = fault_queries
        self.rejected_value = 420
        self.hardware = {
            index: {"core_clock_offset_mhz": 300, "power_limit_w": 125}
            for index in range(3)
        }
        self.faulted: set[int] = set()
        self.nvmlErrorString = FakeFunction(
            lambda code: b"GPU requires reset" if code == 16 else b"setter rejected"
        )
        self.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(self._core_limits)

    def _core_limits(self, handle, minimum, maximum):
        if self.fault_queries and handle.value - 1000 in self.faulted:
            return 16
        return self._offset_limits(handle, minimum, maximum)

    def _set_core_offset(self, handle, value):
        index = handle.value - 1000
        self.calls.append(("core", index, value.value))
        if value.value == 0 and self.reset_code:
            return self.reset_code
        self.hardware[index]["core_clock_offset_mhz"] = value.value
        if value.value == self.rejected_value:
            self.faulted.add(index)
            return self.failure_code
        return 0

    def _set_power(self, handle, value):
        index = handle.value - 1000
        self.calls.append(("power", index, value.value))
        self.hardware[index]["power_limit_w"] = value.value / 1000
        return 0


def bound_setting(index, **fields):
    return {"index": index, "_expected_gpu_uuid": f"GPU-stable-{index}", **fields}


@pytest.mark.parametrize("field, clock_type", [
    ("core_clock_offset_mhz", 0), ("memory_clock_offset_mhz", 2),
])
def test_offset_reset_uses_fixed_zero_without_faulted_range_queries(field, clock_type):
    library = FakeNvmlWithClockOffsets()
    library.nvmlDeviceGetClockOffsets = FakeFunction(
        lambda *_args: pytest.fail("a zero reset must not depend on the range query")
    )
    library.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(
        lambda *_args: pytest.fail("a zero reset must not query legacy limits")
    )
    library.nvmlDeviceGetMemClkMinMaxVfOffset = library.nvmlDeviceGetGpcClkMinMaxVfOffset

    result = NvmlGpuOcBackend(library).reset([{"index": 1, field: 420}])

    assert result["operations"] == [{"index": 1, "reset": [field]}]
    assert library.calls == [("clock_offset_v1", 0x1000018, clock_type, 0, 0)]


def test_reset_required_modern_query_stops_before_the_legacy_query_and_writes():
    library = FakeNvmlWithClockOffsets()
    library.nvmlErrorString = FakeFunction(lambda _code: b"GPU requires reset")
    library.nvmlDeviceGetClockOffsets = FakeFunction(lambda *_args: 16)
    library.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(
        lambda *_args: pytest.fail("a device fault must not fall back to another API")
    )

    with pytest.raises(GpuOcError, match="replacement query: GPU requires reset") as failure:
        NvmlGpuOcBackend(library).apply([{"index": 1, "core_clock_offset_mhz": 420}])

    assert failure.value.nvml_codes == (16,)
    assert failure.value.reset_required is True
    assert failure.value.write_attempted is False
    assert failure.value.failure_target == {
        "index": 1, "field": "core_clock_offset_mhz", "operation": "query"
    }
    assert library.calls == []


@pytest.mark.parametrize("code", [4, 16])
def test_zero_offset_reset_preserves_vendor_error_without_legacy_retry(code):
    library = FakeNvmlWithClockOffsets()

    def reject_reset(_handle, info):
        library.calls.append(("preferred_zero", info._obj.clockOffsetMHz))
        return code

    library.nvmlDeviceSetClockOffsets = FakeFunction(reject_reset)
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).reset([{"index": 0, "core_clock_offset_mhz": 420}])

    assert library.calls == [("preferred_zero", 0)]
    assert failure.value.nvml_codes == (code,)
    assert failure.value.reset_required is (code == 16)
    assert failure.value.write_attempted is True


def test_zero_offset_reset_can_use_legacy_only_when_preferred_is_not_supported():
    library = FakeNvmlWithClockOffsets()

    def unsupported(_handle, info):
        library.calls.append(("preferred_zero", info._obj.clockOffsetMHz))
        return 3

    library.nvmlDeviceSetClockOffsets = FakeFunction(unsupported)
    NvmlGpuOcBackend(library).reset([{"index": 0, "core_clock_offset_mhz": 420}])
    assert library.calls == [("preferred_zero", 0), ("core_offset", 0)]


def test_failed_apply_restores_attempted_fields_across_cards_and_skips_later_card():
    library = FaultingCoreNvml(fault_queries=True)
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).apply([
            bound_setting(0, power_limit_w=175),
            bound_setting(1, core_clock_offset_mhz=420),
            bound_setting(2, power_limit_w=200),
        ])

    assert library.calls == [
        ("power", 0, 175_000), ("core", 1, 420),
        ("core", 1, 0), ("power", 0, 150_000),
    ]
    assert library.hardware[0]["power_limit_w"] == 150
    assert library.hardware[1]["core_clock_offset_mhz"] == 0
    assert library.hardware[2]["power_limit_w"] == 125
    assert failure.value.recovery == {
        "status": "defaults_restored",
        "confirmed_reset": [
            {"index": 1, "_expected_gpu_uuid": "GPU-stable-1", "reset": ["core_clock_offset_mhz"]},
            {"index": 0, "_expected_gpu_uuid": "GPU-stable-0", "reset": ["power_limit_w"]},
        ],
        "hardware_reset_required": False, "hardware_fault": False, "errors": [],
    }
    assert failure.value.failure_target == {
        "index": 1, "_expected_gpu_uuid": "GPU-stable-1",
        "field": "core_clock_offset_mhz", "operation": "apply",
    }


def test_failed_setter_and_failed_zero_reset_are_attempted_once_and_remain_failed():
    library = FaultingCoreNvml(failure_code=16, reset_code=16)
    with pytest.raises(GpuOcError, match="rollback also failed") as failure:
        NvmlGpuOcBackend(library).apply([bound_setting(1, core_clock_offset_mhz=420)])

    assert library.calls == [("core", 1, 420), ("core", 1, 0)]
    assert failure.value.nvml_codes == (16,)
    assert failure.value.recovery["status"] == "failed"
    assert failure.value.recovery["confirmed_reset"] == []
    assert failure.value.recovery["hardware_reset_required"] is True
    assert len(failure.value.recovery["errors"]) == 1


def test_transition_keeps_normal_previous_restore_but_never_replays_same_failed_value():
    library = FaultingCoreNvml()
    backend = NvmlGpuOcBackend(library)
    with pytest.raises(GpuOcError) as failure:
        backend.transition(
            [bound_setting(1, core_clock_offset_mhz=300)],
            [bound_setting(1, core_clock_offset_mhz=420)],
        )
    assert library.calls == [("core", 1, 0), ("core", 1, 420), ("core", 1, 0), ("core", 1, 300)]
    assert library.hardware[1]["core_clock_offset_mhz"] == 300
    assert failure.value.recovery["status"] == "rolled_back"
    assert failure.value.recovery["confirmed_reset"] == []

    library.calls.clear()
    with pytest.raises(GpuOcError) as same_failure:
        backend.transition(
            [bound_setting(1, core_clock_offset_mhz=420)],
            [bound_setting(1, core_clock_offset_mhz=420)],
        )
    assert library.calls == [("core", 1, 0), ("core", 1, 420), ("core", 1, 0)]
    assert library.hardware[1]["core_clock_offset_mhz"] == 0
    assert same_failure.value.recovery["status"] == "defaults_restored"
    assert same_failure.value.recovery["confirmed_reset"] == [
        {"index": 1, "_expected_gpu_uuid": "GPU-stable-1", "reset": ["core_clock_offset_mhz"]}
    ]


def test_transition_reset_required_leaves_proven_defaults_and_does_not_replay_prior_oc():
    library = FaultingCoreNvml(failure_code=16)
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).transition(
            [bound_setting(1, power_limit_w=125, core_clock_offset_mhz=400)],
            [bound_setting(1, power_limit_w=175, core_clock_offset_mhz=420)],
        )

    assert library.calls == [
        ("power", 1, 150_000), ("core", 1, 0), ("power", 1, 175_000),
        ("core", 1, 420), ("core", 1, 0), ("power", 1, 150_000),
    ]
    assert library.hardware[1] == {"core_clock_offset_mhz": 0, "power_limit_w": 150}
    assert failure.value.recovery == {
        "status": "partial",
        "confirmed_reset": [{
            "index": 1, "_expected_gpu_uuid": "GPU-stable-1",
            "reset": ["power_limit_w", "core_clock_offset_mhz"],
        }],
        "hardware_reset_required": True, "hardware_fault": True, "errors": [],
    }
    assert failure.value.write_attempted is True


@pytest.mark.parametrize("reset_required", [False, True])
def test_transition_preflight_failure_never_resets_previous_fields(reset_required):
    library = FaultingCoreNvml(fault_queries=True)
    if reset_required:
        library.faulted.add(1)
    requested = 420 if reset_required else 5001
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).transition(
            [bound_setting(1, core_clock_offset_mhz=300)],
            [bound_setting(1, core_clock_offset_mhz=requested)],
        )

    assert library.calls == []
    assert library.hardware[1]["core_clock_offset_mhz"] == 300
    assert failure.value.write_attempted is False
    assert failure.value.reset_required is reset_required
    assert failure.value.recovery["confirmed_reset"] == []
    assert failure.value.failure_target == {
        "index": 1, "_expected_gpu_uuid": "GPU-stable-1",
        "field": "core_clock_offset_mhz", "operation": "query",
    }


def test_failed_setter_compensation_rechecks_uuid_and_never_writes_replacement_card():
    library = FaultingCoreNvml()
    original_uuid = library.nvmlDeviceGetUUID

    def changed_uuid(handle, output, length):
        if handle.value - 1000 in library.faulted:
            output.value = b"GPU-replacement"
            return 0
        return original_uuid(handle, output, length)

    library.nvmlDeviceGetUUID = FakeFunction(changed_uuid)
    with pytest.raises(GpuOcError, match="identity changed") as failure:
        NvmlGpuOcBackend(library).apply([bound_setting(1, core_clock_offset_mhz=420)])

    assert library.calls == [("core", 1, 420)]
    assert failure.value.recovery["confirmed_reset"] == []
    assert failure.value.recovery["status"] == "failed"


def test_reset_required_partial_fan_reset_never_replays_previous_manual_policy():
    library = FakeNvml()
    library.nvmlDeviceGetNumFans = FakeFunction(
        lambda _handle, count: setattr(count._obj, "value", 2) or 0
    )

    def failed_reset(_handle, fan):
        library.calls.append(("reset_fan", fan.value))
        return 16 if fan.value == 1 else 0

    library.nvmlDeviceSetDefaultFanSpeed_v2 = FakeFunction(failed_reset)
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).reset([{"index": 0, "fan_percent": 65}])

    assert library.calls == [("reset_fan", 0), ("reset_fan", 1)]
    assert failure.value.reset_required is True
    assert failure.value.write_attempted is True


def test_transition_success_reports_actual_write_attempts():
    backend = NvmlGpuOcBackend(FakeNvml())
    assert backend.transition([], [])["write_attempted"] is False
    assert backend.transition([], [{"index": 0, "core_clock_offset_mhz": 75}])["write_attempted"] is True


@pytest.mark.parametrize("query", [
    "nvmlDeviceGetNumFans", "nvmlDeviceGetMinMaxFanSpeed", "nvmlDeviceGetFanSpeed_v2",
])
@pytest.mark.parametrize("code", [4, 16])
def test_requested_fan_query_retains_numeric_fault_but_discovery_stays_read_only(query, code):
    library = FakeNvmlWithFanReadback()
    setattr(library, query, FakeFunction(lambda *_args: code))
    backend = NvmlGpuOcBackend(library)
    assert "fan_percent" not in backend.supported_fields([0])[0]
    with pytest.raises(GpuOcError) as failure:
        backend.apply([{"index": 0, "fan_percent": 65}])
    assert library.calls == []
    assert failure.value.nvml_codes == (code,)
    assert failure.value.reset_required is (code == 16)
    assert failure.value.write_attempted is False
    assert failure.value.failure_target == {
        "index": 0, "field": "fan_percent", "operation": "query",
    }


def test_transition_retains_proof_of_default_unattempted_fans_after_partial_apply():
    library = FakeNvmlWithFanReadback()
    speeds = [65, 65, 65]
    library.nvmlDeviceGetNumFans = FakeFunction(
        lambda _handle, count: setattr(count._obj, "value", 3) or 0
    )

    def set_speed(_handle, fan, value):
        library.calls.append(("fan", fan.value, value.value))
        speeds[fan.value] = value.value
        return 3 if fan.value == 1 else 0

    def reset_speed(_handle, fan):
        library.calls.append(("reset_fan", fan.value))
        speeds[fan.value] = None
        return 0

    library.nvmlDeviceSetFanSpeed_v2 = FakeFunction(set_speed)
    library.nvmlDeviceSetDefaultFanSpeed_v2 = FakeFunction(reset_speed)
    setting = {"index": 0, "fan_percent": 65}
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).transition([setting], [setting])

    assert library.calls == [
        ("reset_fan", 0), ("reset_fan", 1), ("reset_fan", 2),
        ("fan", 0, 65), ("fan", 1, 65), ("reset_fan", 1), ("reset_fan", 0),
    ]
    assert speeds == [None, None, None]
    assert failure.value.recovery["confirmed_reset"] == [{"index": 0, "reset": ["fan_percent"]}]
    assert failure.value.recovery["status"] == "defaults_restored"


@pytest.mark.parametrize("code", [15, 16])
def test_modern_hardware_fault_is_not_hidden_by_a_successful_legacy_query(code):
    library = FakeNvmlWithClockOffsets()
    query_calls = []

    def modern_query(_handle, _info):
        query_calls.append("modern")
        return code

    def legacy_query(handle, minimum, maximum):
        query_calls.append("legacy")
        return library._offset_limits(handle, minimum, maximum)

    library.nvmlDeviceGetClockOffsets = FakeFunction(modern_query)
    library.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(legacy_query)
    backend = NvmlGpuOcBackend(library)
    with pytest.raises(GpuOcError) as failure:
        backend.transition(
            [{"index": 0, "core_clock_offset_mhz": 300}],
            [{"index": 0, "core_clock_offset_mhz": 420}],
        )
    assert query_calls == ["modern"]
    assert library.calls == []
    assert failure.value.nvml_codes == (code,)
    assert failure.value.hardware_fault is True
    assert failure.value.reset_required is (code == 16)
    assert failure.value.write_attempted is False
    assert failure.value.recovery["hardware_fault"] is True
    assert failure.value.recovery["hardware_reset_required"] is (code == 16)

    query_calls.clear()
    fields = backend.supported_fields([0])[0]
    assert "core_clock_offset_mhz" not in fields
    assert "memory_clock_offset_mhz" not in fields
    assert query_calls == ["modern", "modern"]
    assert library.calls == []


@pytest.mark.parametrize("fault_code", [15, 16])
def test_fan_restore_stops_old_policy_replay_immediately_after_a_hardware_fault(fault_code):
    library = FakeNvml()
    speeds = [65, 65, 65]
    library.nvmlDeviceGetNumFans = FakeFunction(
        lambda _handle, count: setattr(count._obj, "value", 3) or 0
    )

    def reset_fan(_handle, fan):
        code = 3 if fan.value == 2 else 0
        library.calls.append(("default", fan.value, code))
        if code == 0:
            speeds[fan.value] = None
        return code

    def restore_fan(_handle, fan, value):
        library.calls.append(("restore", fan.value, value.value, fault_code))
        speeds[fan.value] = value.value  # Failed writes have unknown effects.
        return fault_code

    library.nvmlDeviceSetDefaultFanSpeed_v2 = FakeFunction(reset_fan)
    library.nvmlDeviceSetFanSpeed_v2 = FakeFunction(restore_fan)
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).transition([{"index": 0, "fan_percent": 65}], [])
    assert library.calls == [
        ("default", 0, 0), ("default", 1, 0), ("default", 2, 3),
        ("restore", 1, 65, fault_code),
    ]
    assert speeds == [None, 65, 65]
    assert failure.value.nvml_codes == (3, fault_code)
    assert failure.value.hardware_fault is True
    assert failure.value.reset_required is (fault_code == 16)
    assert failure.value.recovery["confirmed_reset"] == []  # Whole fan policy is not default.


def test_lost_gpu_never_replays_old_core_value_after_the_new_setter_fails():
    library = FaultingCoreNvml(failure_code=15)
    with pytest.raises(GpuOcError) as failure:
        NvmlGpuOcBackend(library).transition(
            [bound_setting(1, core_clock_offset_mhz=300)],
            [bound_setting(1, core_clock_offset_mhz=420)],
        )
    assert library.calls == [("core", 1, 0), ("core", 1, 420), ("core", 1, 0)]
    assert library.hardware[1]["core_clock_offset_mhz"] == 0
    assert failure.value.reset_required is False
    assert failure.value.hardware_fault is True
    assert failure.value.recovery["hardware_fault"] is True
    assert failure.value.recovery["hardware_reset_required"] is False
    assert failure.value.recovery["status"] == "partial"
