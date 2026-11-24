"""NVIDIA GPU tuning through the vendor-supported NVML library.

The backend never invokes a shell or a third-party overclocking utility.  It
preflights every requested setting against device-reported limits before it
changes any GPU.  Driver/platform support still varies, so unsupported calls
fail explicitly.
"""

from __future__ import annotations

import ctypes
import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable


if TYPE_CHECKING:
    from .config import AgentConfig


def gpu_oc_state_path(config: AgentConfig) -> Path:
    """Return the shared path for Agent-owned GPU OC writes and telemetry."""
    base = config.managed_miner_runtime_dir
    if base is None:
        base = (
            config.flight_sheet_file.parent
            if config.flight_sheet_file is not None
            else config.state_file.parent
        )
    return base / "rigpilot-gpu-oc-state.json"


class _NvmlClockOffsetV1(ctypes.Structure):
    """ABI for ``nvmlClockOffset_v1_t`` introduced with driver 555."""

    _fields_ = [
        ("version", ctypes.c_uint),
        ("type", ctypes.c_uint),
        ("pstate", ctypes.c_uint),
        ("clockOffsetMHz", ctypes.c_int),
        ("minClockOffsetMHz", ctypes.c_int),
        ("maxClockOffsetMHz", ctypes.c_int),
    ]


# NVML_STRUCT_VERSION(ClockOffset, 1): version in the high byte, ABI size in
# the low 24 bits.  Keep this derived from the ctypes structure so an ABI edit
# cannot silently leave a stale constant behind.
_NVML_CLOCK_OFFSET_V1 = (1 << 24) | ctypes.sizeof(_NvmlClockOffsetV1)
_NVML_PSTATE_0 = 0
_NVML_DEVICE_UUID_BUFFER_SIZE = 96
_GPU_FAN_SAFE_MIN_PERCENT = 30
_GPU_FAN_SAFE_MAX_PERCENT = 100
_NVML_ERROR_NOT_SUPPORTED = 3
_NVML_ERROR_GPU_IS_LOST = 15
_NVML_ERROR_RESET_REQUIRED = 16
_NVML_HARDWARE_FAULT_CODES = {_NVML_ERROR_GPU_IS_LOST, _NVML_ERROR_RESET_REQUIRED}

_CLOCK_OFFSET_SPECS: dict[str, tuple[int, str, str, str]] = {
    "core_clock_offset_mhz": (
        0,  # NVML_CLOCK_GRAPHICS
        "nvmlDeviceGetGpcClkMinMaxVfOffset",
        "nvmlDeviceSetGpcClkVfOffset",
        "core clock offset",
    ),
    "memory_clock_offset_mhz": (
        2,  # NVML_CLOCK_MEM
        "nvmlDeviceGetMemClkMinMaxVfOffset",
        "nvmlDeviceSetMemClkVfOffset",
        "memory clock offset",
    ),
}
_GPU_OC_FIELDS = (
    "power_limit_w",
    "core_clock_offset_mhz",
    "memory_clock_offset_mhz",
    "core_clock_lock_mhz",
    "memory_clock_lock_mhz",
    "fan_percent",
)


class GpuOcError(RuntimeError):
    """The driver rejected or does not support a requested GPU setting."""

    def __init__(
        self,
        message: str,
        *,
        nvml_codes: tuple[int, ...] = (),
        recovery: dict[str, Any] | None = None,
        write_attempted: bool = False,
        failure_target: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.nvml_codes = tuple(nvml_codes)
        self.reset_required = _NVML_ERROR_RESET_REQUIRED in self.nvml_codes
        self.hardware_fault = bool(_NVML_HARDWARE_FAULT_CODES.intersection(self.nvml_codes))
        self.recovery = recovery
        self.write_attempted = write_attempted
        self.failure_target = failure_target


class NvmlGpuOcBackend:
    """Small dependency-free wrapper around the official NVIDIA NVML API."""

    # Command dispatchers may use this flag to add an ephemeral UUID binding to
    # settings passed to transition().  The binding is never persisted, and is
    # checked on the exact NVML handle used for the subsequent writes.
    supports_uuid_bound_transitions = True

    def __init__(self, library: Any | None = None) -> None:
        self.library = library or self._load_library()
        self._error_string = self._optional_function(
            "nvmlErrorString", [ctypes.c_int], ctypes.c_char_p
        )
        self._init = self._first_function(
            ("nvmlInit_v2", "nvmlInit"), [], ctypes.c_int
        )
        self._shutdown = self._first_function(
            ("nvmlShutdown",), [], ctypes.c_int
        )
        self._get_handle = self._first_function(
            ("nvmlDeviceGetHandleByIndex_v2", "nvmlDeviceGetHandleByIndex"),
            [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
            ctypes.c_int,
        )

    @staticmethod
    def _load_library() -> Any:
        errors: list[str] = []
        if platform.system() == "Windows":
            candidates = ["nvml.dll"]
            program_files = os.environ.get("ProgramW6432") or os.environ.get(
                "ProgramFiles"
            )
            if program_files:
                candidates.insert(
                    0, str(Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvml.dll")
                )
            loader: Callable[[str], Any] = ctypes.WinDLL
        else:
            candidates = ["libnvidia-ml.so.1", "libnvidia-ml.so"]
            loader = ctypes.CDLL
        for candidate in candidates:
            try:
                return loader(candidate)
            except OSError as exc:
                errors.append(str(exc))
        raise GpuOcError(
            "NVIDIA NVML library is unavailable; install a supported NVIDIA driver"
            + (f" ({errors[-1]})" if errors else "")
        )

    def _optional_function(
        self, name: str, argtypes: list[Any], restype: Any
    ) -> Any | None:
        function = getattr(self.library, name, None)
        if function is None:
            return None
        function.argtypes = argtypes
        function.restype = restype
        return function

    def _first_function(
        self, names: tuple[str, ...], argtypes: list[Any], restype: Any
    ) -> Any:
        for name in names:
            function = self._optional_function(name, argtypes, restype)
            if function is not None:
                return function
        raise GpuOcError(f"NVIDIA driver does not expose {names[0]}")

    def _required_function(
        self, name: str, argtypes: list[Any], restype: Any = ctypes.c_int
    ) -> Any:
        function = self._optional_function(name, argtypes, restype)
        if function is None:
            raise GpuOcError(f"NVIDIA driver does not support {name}")
        return function

    @staticmethod
    def _validate_expected_uuid(index: int, expected_uuid: str) -> None:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 31:
            raise GpuOcError("GPU index is invalid")
        if (
            not isinstance(expected_uuid, str)
            or not 4 <= len(expected_uuid) <= 128
            or any(ord(character) < 32 or ord(character) == 127 for character in expected_uuid)
        ):
            raise GpuOcError("expected GPU UUID is invalid")

    def _verify_handle_uuid(
        self,
        handle: ctypes.c_void_p,
        index: int,
        expected_uuid: str,
    ) -> str:
        """Verify identity on an already-open handle in the active NVML session."""

        self._validate_expected_uuid(index, expected_uuid)
        get_uuid = self._required_function(
            "nvmlDeviceGetUUID",
            [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_char),
                ctypes.c_uint,
            ],
        )
        buffer = ctypes.create_string_buffer(_NVML_DEVICE_UUID_BUFFER_SIZE)
        self._check(
            get_uuid(
                handle,
                buffer,
                ctypes.c_uint(len(buffer)),
            ),
            f"query GPU {index} UUID",
        )
        try:
            actual_uuid = buffer.value.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise GpuOcError(f"GPU {index} reported an invalid UUID") from exc
        if actual_uuid != expected_uuid:
            raise GpuOcError(
                f"GPU {index} identity changed; expected {expected_uuid}, "
                f"reported {actual_uuid or 'empty UUID'}"
            )
        return actual_uuid

    def verify_device_uuid(self, index: int, expected_uuid: str) -> str:
        """Fail closed unless the current NVML index is the selected GPU."""

        self._validate_expected_uuid(index, expected_uuid)
        self._check(self._init(), "initialize NVML")
        try:
            handle = self._device(index)
            return self._verify_handle_uuid(handle, index, expected_uuid)
        finally:
            try:
                self._shutdown()
            except Exception:
                pass

    def enumerate_device_indices(self) -> dict[str, int]:
        """Return one complete, mutation-free UUID-to-index snapshot.

        Callers that tolerate a physically absent card must still prove the
        identity of every card that is currently present. Returning the full
        snapshot lets those callers defer an absent UUID without ever touching
        whichever different card may now occupy its persisted index hint.
        """

        self._check(self._init(), "initialize NVML")
        try:
            get_count = self._first_function(
                ("nvmlDeviceGetCount_v2", "nvmlDeviceGetCount"),
                [ctypes.POINTER(ctypes.c_uint)],
                ctypes.c_int,
            )
            get_uuid = self._required_function(
                "nvmlDeviceGetUUID",
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char), ctypes.c_uint],
            )
            count = ctypes.c_uint()
            self._check(get_count(ctypes.byref(count)), "query NVIDIA GPU count")
            if int(count.value) > 32:
                raise GpuOcError("NVIDIA GPU count exceeds the supported limit")
            resolved: dict[str, int] = {}
            for index in range(int(count.value)):
                handle = self._device(index)
                buffer = ctypes.create_string_buffer(_NVML_DEVICE_UUID_BUFFER_SIZE)
                self._check(
                    get_uuid(handle, buffer, ctypes.c_uint(len(buffer))),
                    f"query GPU {index} UUID",
                )
                try:
                    gpu_uuid = buffer.value.decode("ascii", errors="strict")
                except UnicodeDecodeError as exc:
                    raise GpuOcError(f"GPU {index} reported an invalid UUID") from exc
                if (
                    not 4 <= len(gpu_uuid) <= 128
                    or any(
                        character.isspace()
                        or ord(character) < 32
                        or ord(character) == 127
                        for character in gpu_uuid
                    )
                ):
                    raise GpuOcError(f"GPU {index} reported an invalid UUID")
                if gpu_uuid in resolved:
                    raise GpuOcError(f"GPU UUID {gpu_uuid} is ambiguous")
                resolved[gpu_uuid] = index
            return resolved
        finally:
            try:
                self._shutdown()
            except Exception:
                pass

    def enumerate_device_identities(self) -> dict[str, dict[str, Any]]:
        """Read a complete UUID/index/name snapshot for model-rule coverage."""
        indices = self.enumerate_device_indices()
        self._check(self._init(), "initialize NVML")
        try:
            get_name = self._required_function(
                "nvmlDeviceGetName",
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char), ctypes.c_uint],
            )
            result: dict[str, dict[str, Any]] = {}
            for gpu_uuid, index in indices.items():
                handle = self._device(index)
                self._verify_handle_uuid(handle, index, gpu_uuid)
                buffer = ctypes.create_string_buffer(96)
                self._check(
                    get_name(handle, buffer, ctypes.c_uint(len(buffer))),
                    f"query GPU {index} name",
                )
                if b"\0" not in buffer.raw:
                    raise GpuOcError(f"GPU {index} reported an unterminated name")
                try:
                    name = buffer.value.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    raise GpuOcError(f"GPU {index} reported an invalid name") from exc
                if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
                    raise GpuOcError(f"GPU {index} reported an invalid name")
                result[gpu_uuid] = {"index": index, "name": name.strip()}
        finally:
            try:
                self._shutdown()
            except Exception:
                pass
        if self.enumerate_device_indices() != indices:
            raise GpuOcError("GPU identity changed during the name snapshot")
        return result

    def resolve_device_indices(self, expected_uuids: list[str]) -> dict[str, int]:
        """Resolve a complete UUID set before any caller performs NVML writes."""

        if (
            not isinstance(expected_uuids, list)
            or not 1 <= len(expected_uuids) <= 32
            or any(
                not isinstance(gpu_uuid, str)
                or not 4 <= len(gpu_uuid) <= 128
                or any(
                    character.isspace()
                    or ord(character) < 32
                    or ord(character) == 127
                    for character in gpu_uuid
                )
                for gpu_uuid in expected_uuids
            )
            or len(set(expected_uuids)) != len(expected_uuids)
        ):
            raise GpuOcError("expected GPU UUID set is invalid")
        expected = set(expected_uuids)
        enumerated = self.enumerate_device_indices()
        resolved = {
            gpu_uuid: enumerated[gpu_uuid]
            for gpu_uuid in expected_uuids
            if gpu_uuid in enumerated
        }
        missing = sorted(expected - set(resolved))
        if missing:
            raise GpuOcError("GPU UUID is no longer reported: " + ", ".join(missing))
        return resolved

    def _message(self, code: int) -> str:
        if self._error_string is not None:
            raw = self._error_string(code)
            if raw:
                return raw.decode("utf-8", errors="replace")
        return f"NVML error {code}"

    def _check(
        self, code: int, operation: str, *, write_attempted: bool = False,
        failure_target: dict[str, Any] | None = None,
    ) -> None:
        if code != 0:
            raise GpuOcError(
                f"{operation} failed: {self._message(code)}",
                nvml_codes=(int(code),),
                write_attempted=write_attempted,
                failure_target=failure_target,
            )

    def _device(self, index: int) -> ctypes.c_void_p:
        handle = ctypes.c_void_p()
        self._check(
            self._get_handle(ctypes.c_uint(index), ctypes.byref(handle)),
            f"open GPU {index}",
        )
        return handle

    @staticmethod
    def _clock_offset_info(clock_type: int, value: int = 0) -> _NvmlClockOffsetV1:
        info = _NvmlClockOffsetV1()
        info.version = _NVML_CLOCK_OFFSET_V1
        info.type = int(clock_type)
        # NVIDIA documents the replacement API as per-PState.  P0 is the
        # maximum-performance state and is the direct replacement used for a
        # miner's single Core/Memory Offset value.
        info.pstate = _NVML_PSTATE_0
        info.clockOffsetMHz = int(value)
        return info

    def _clock_offset_plan(
        self,
        handle: ctypes.c_void_p,
        field: str,
        index: int,
        *,
        required: bool,
    ) -> dict[str, Any] | None:
        """Resolve a read-probed NVML offset API for one clock domain.

        Driver 555 added ``nvmlDeviceGet/SetClockOffsets`` and NVIDIA now
        marks the older GPC/MEM VF-offset calls for deprecation.  Newer
        GeForce generations can therefore reject the legacy query even when
        the replacement works.  Prefer the replacement API, but retain the
        legacy read-only probe for older drivers and boards.
        """

        clock_type, legacy_query_name, legacy_setter_name, label = (
            _CLOCK_OFFSET_SPECS[field]
        )
        failures: list[str] = []
        failure_codes: list[int] = []

        query_v1 = self._optional_function(
            "nvmlDeviceGetClockOffsets",
            [ctypes.c_void_p, ctypes.POINTER(_NvmlClockOffsetV1)],
            ctypes.c_int,
        )
        setter_v1 = self._optional_function(
            "nvmlDeviceSetClockOffsets",
            [ctypes.c_void_p, ctypes.POINTER(_NvmlClockOffsetV1)],
            ctypes.c_int,
        )
        if query_v1 is not None and setter_v1 is not None:
            info = self._clock_offset_info(clock_type)
            try:
                code = int(query_v1(handle, ctypes.byref(info)))
            except Exception as exc:
                if getattr(exc, "hardware_fault", False):
                    if required:
                        raise
                    return None
                failures.append(f"replacement query raised {exc}")
            else:
                if code in _NVML_HARDWARE_FAULT_CODES:
                    # Device faults are not an unsupported API.  A later
                    # legacy success cannot make this card safe to tune.
                    if required:
                        raise GpuOcError(
                            f"query GPU {index} {label} limits failed: "
                            f"replacement query: {self._message(code)}",
                            nvml_codes=(code,),
                            failure_target={"index": index, "field": field, "operation": "query"},
                        )
                    return None
                if (
                    code == 0
                    and info.minClockOffsetMHz <= 0 <= info.maxClockOffsetMHz
                ):
                    return {
                        "api": "clock_offsets_v1",
                        "setter": setter_v1,
                        "clock_type": clock_type,
                        "minimum": int(info.minClockOffsetMHz),
                        "maximum": int(info.maxClockOffsetMHz),
                    }
                if code == 0:
                    failures.append("replacement query returned a range without reset value 0")
                else:
                    failure_codes.append(code)
                    failures.append(f"replacement query: {self._message(code)}")
        else:
            failures.append("replacement query/setter unavailable")

        legacy_query = self._optional_function(
            legacy_query_name,
            [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
            ],
            ctypes.c_int,
        )
        legacy_setter = self._optional_function(
            legacy_setter_name,
            [ctypes.c_void_p, ctypes.c_int],
            ctypes.c_int,
        )
        if legacy_query is not None and legacy_setter is not None:
            minimum, maximum = ctypes.c_int(), ctypes.c_int()
            try:
                code = int(
                    legacy_query(
                        handle,
                        ctypes.byref(minimum),
                        ctypes.byref(maximum),
                    )
                )
            except Exception as exc:
                failures.append(f"legacy query raised {exc}")
            else:
                if code == 0 and minimum.value <= 0 <= maximum.value:
                    return {
                        "api": "legacy_vf_offset",
                        "setter": legacy_setter,
                        "minimum": int(minimum.value),
                        "maximum": int(maximum.value),
                    }
                if code == 0:
                    failures.append("legacy query returned a range without reset value 0")
                else:
                    failure_codes.append(code)
                    failures.append(f"legacy query: {self._message(code)}")
        else:
            failures.append("legacy query/setter unavailable")

        if required:
            raise GpuOcError(
                f"query GPU {index} {label} limits failed: " + "; ".join(failures),
                nvml_codes=tuple(failure_codes),
                failure_target={"index": index, "field": field, "operation": "query"},
            )
        return None

    def _clock_offset_reset_plan(self, field: str) -> list[dict[str, Any]]:
        """Resolve setters for the fixed zero default, without a range query.

        Resetting an owned offset never writes an arbitrary requested value.
        In particular, a driver that cannot currently answer a range query
        must still receive the one explicit attempt to restore zero.  Apply
        and capability discovery retain their device-reported range checks.
        """

        clock_type, _query_name, legacy_setter_name, _label = (
            _CLOCK_OFFSET_SPECS[field]
        )
        plans: list[dict[str, Any]] = []
        setter = self._optional_function(
            "nvmlDeviceSetClockOffsets",
            [ctypes.c_void_p, ctypes.POINTER(_NvmlClockOffsetV1)],
            ctypes.c_int,
        )
        if setter is not None:
            plans.append(
                {"api": "clock_offsets_v1", "setter": setter, "clock_type": clock_type}
            )
        legacy_setter = self._optional_function(
            legacy_setter_name,
            [ctypes.c_void_p, ctypes.c_int],
            ctypes.c_int,
        )
        if legacy_setter is not None:
            plans.append({"api": "legacy_vf_offset", "setter": legacy_setter})
        if not plans:
            raise GpuOcError(f"NVIDIA driver does not support resetting {field}")
        return plans

    def _reset_clock_offset(
        self,
        handle: ctypes.c_void_p,
        plans: list[dict[str, Any]],
        operation: str,
    ) -> None:
        for position, plan in enumerate(plans):
            try:
                self._set_clock_offset(handle, plan, 0, operation)
                return
            except GpuOcError as exc:
                # A modern symbol can exist on a driver/board which supports
                # only the legacy setter.  Other vendor failures, including
                # RESET_REQUIRED, are final and must not cause repeated writes.
                if (
                    exc.nvml_codes != (_NVML_ERROR_NOT_SUPPORTED,)
                    or position + 1 == len(plans)
                ):
                    raise

    def _set_clock_offset(
        self,
        handle: ctypes.c_void_p,
        offset_plan: dict[str, Any],
        value: int,
        operation: str,
    ) -> None:
        if offset_plan["api"] == "clock_offsets_v1":
            info = self._clock_offset_info(offset_plan["clock_type"], value)
            code = offset_plan["setter"](handle, ctypes.byref(info))
        else:
            code = offset_plan["setter"](handle, ctypes.c_int(value))
        self._check(code, operation, write_attempted=True)

    def _locked_clock_fields(
        self, handle: ctypes.c_void_p
    ) -> list[str]:
        """Identify clock-lock candidates using device-specific read queries.

        NVIDIA documents GPU locking for Volta+ and memory locking for
        Ampere+, but GeForce/RTX exposes only a subset of NVML on some driver
        combinations.  Architecture alone is therefore insufficient.  A
        control is advertised only when the architecture floor, both command
        symbols, and two device-specific read queries for that clock domain
        all agree on Linux and Windows.  These read queries do not prove write
        permission or setter acceptance: the actual command retains the
        driver's error.
        """

        system_name = platform.system()
        if system_name not in {"Linux", "Windows"}:
            return []
        get_architecture = self._optional_function(
            "nvmlDeviceGetArchitecture",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        get_max_clock = self._optional_function(
            "nvmlDeviceGetMaxClockInfo",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        get_current_clock = self._optional_function(
            "nvmlDeviceGetClockInfo",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        if (
            get_architecture is None
            or get_max_clock is None
            or get_current_clock is None
        ):
            return []
        architecture = ctypes.c_uint()
        try:
            if get_architecture(handle, ctypes.byref(architecture)) != 0:
                return []
        except Exception:
            return []
        if architecture.value == 0xFFFFFFFF:  # NVML_DEVICE_ARCH_UNKNOWN
            return []

        specs = (
            (
                "core_clock_lock_mhz",
                5,  # NVML_DEVICE_ARCH_VOLTA
                0,  # NVML_CLOCK_GRAPHICS
                "nvmlDeviceSetGpuLockedClocks",
                "nvmlDeviceResetGpuLockedClocks",
            ),
            (
                "memory_clock_lock_mhz",
                7,  # NVML_DEVICE_ARCH_AMPERE
                2,  # NVML_CLOCK_MEM
                "nvmlDeviceSetMemoryLockedClocks",
                "nvmlDeviceResetMemoryLockedClocks",
            ),
        )
        fields: list[str] = []
        for (
            field,
            minimum_architecture,
            clock_type,
            setter_name,
            reset_name,
        ) in specs:
            if architecture.value < minimum_architecture:
                continue
            setter = self._optional_function(
                setter_name,
                [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint],
                ctypes.c_int,
            )
            reset = self._optional_function(
                reset_name,
                [ctypes.c_void_p],
                ctypes.c_int,
            )
            if setter is None or reset is None:
                continue
            maximum, current = ctypes.c_uint(), ctypes.c_uint()
            try:
                domain_supported = (
                    get_max_clock(
                        handle,
                        ctypes.c_uint(clock_type),
                        ctypes.byref(maximum),
                    )
                    == 0
                    and maximum.value > 0
                    and get_current_clock(
                        handle,
                        ctypes.c_uint(clock_type),
                        ctypes.byref(current),
                    )
                    == 0
                    and current.value > 0
                )
            except Exception:
                domain_supported = False
            if domain_supported:
                fields.append(field)
        return fields

    def _fan_control_plan(
        self, handle: ctypes.c_void_p, *, index: int | None = None, required: bool = False
    ) -> dict[str, Any] | None:
        """Read-only proof and hardware limits for per-fan NVML control.

        NVML can export fan setters even when a particular board has no fan or
        when its driver does not expose the per-fan interface.  Do not advertise
        the control from symbols alone: require a valid fan count and a
        successful per-fan speed query for every reported fan, plus both the
        manual setter and the matching return-to-default operation.  The probe
        itself never changes fan policy or speed.
        """

        get_num_fans = self._optional_function(
            "nvmlDeviceGetNumFans",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        get_fan_speed = self._optional_function(
            "nvmlDeviceGetFanSpeed_v2",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        get_fan_limits = self._optional_function(
            "nvmlDeviceGetMinMaxFanSpeed",
            [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint),
            ],
            ctypes.c_int,
        )
        set_fan_speed = self._optional_function(
            "nvmlDeviceSetFanSpeed_v2",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint],
            ctypes.c_int,
        )
        reset_fan_speed = self._optional_function(
            "nvmlDeviceSetDefaultFanSpeed_v2",
            [ctypes.c_void_p, ctypes.c_uint],
            ctypes.c_int,
        )
        if any(
            function is None
            for function in (
                get_num_fans,
                get_fan_speed,
                get_fan_limits,
                set_fan_speed,
                reset_fan_speed,
            )
        ):
            return None

        fan_count = ctypes.c_uint()
        minimum, maximum = ctypes.c_uint(), ctypes.c_uint()

        def query_succeeded(code: int, operation: str) -> bool:
            if code != 0 and required:
                self._check(
                    code, f"query GPU {index} {operation}",
                    failure_target={"index": index, "field": "fan_percent", "operation": "query"},
                )
            return code == 0

        try:
            if not query_succeeded(get_num_fans(handle, ctypes.byref(fan_count)), "fans"):
                return None
            # Keep a defensive bound before iterating driver-provided data.
            if not 1 <= fan_count.value <= 32:
                return None
            if not query_succeeded(
                get_fan_limits(handle, ctypes.byref(minimum), ctypes.byref(maximum)),
                "fan limits",
            ):
                return None
            if not 0 <= minimum.value <= maximum.value <= 100:
                return None
            for fan_index in range(fan_count.value):
                speed = ctypes.c_uint()
                if not query_succeeded(
                    get_fan_speed(
                        handle,
                        ctypes.c_uint(fan_index),
                        ctypes.byref(speed),
                    ),
                    f"fan {fan_index} speed",
                ):
                    return None
        except Exception:
            if required:
                raise
            return None
        return {
            "fan_count": int(fan_count.value),
            "minimum": int(minimum.value),
            "maximum": int(maximum.value),
            "set_fan": set_fan_speed,
            "reset_fan": reset_fan_speed,
        }

    def supported_fan_limits(self) -> dict[int, tuple[int, int]]:
        """Return ranges from the most recent ``supported_fields`` probe."""

        return dict(getattr(self, "_last_supported_fan_limits", {}))

    def supported_fields(self, indices: list[int]) -> dict[int, list[str]]:
        """Return controls that can be safely preflighted for each GPU.

        NVML exports many entry points even when a particular board or driver
        rejects them with ``NVML_ERROR_NOT_SUPPORTED``.  The controller must
        not infer support from symbol presence alone, so every advertised
        control has device-specific, read-only queries that succeed before it
        is shown.  Linux and Windows locked clocks additionally
        require the official architecture floor and both setter/reset symbols.
        Actual setter acceptance and permission remain driver decisions and
        their errors are reported by the command; discovery never writes.
        Fan control requires successful read-only per-fan queries and both the
        manual/default setter symbols.  This supports Windows and Linux without
        mutating fan policy during inventory collection.  SRBMiner's own OC
        path remains separate.
        """

        self._last_supported_fan_limits: dict[int, tuple[int, int]] = {}
        self._check(self._init(), "initialize NVML")
        try:
            result: dict[int, list[str]] = {}
            get_power_constraints = self._optional_function(
                "nvmlDeviceGetPowerManagementLimitConstraints",
                [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint),
                    ctypes.POINTER(ctypes.c_uint),
                ],
                ctypes.c_int,
            )
            get_default_power = self._optional_function(
                "nvmlDeviceGetPowerManagementDefaultLimit",
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)],
                ctypes.c_int,
            )
            set_power = self._optional_function(
                "nvmlDeviceSetPowerManagementLimit",
                [ctypes.c_void_p, ctypes.c_uint],
                ctypes.c_int,
            )
            for raw_index in indices:
                index = int(raw_index)
                fields: list[str] = []
                result[index] = fields
                try:
                    handle = self._device(index)
                except (GpuOcError, OSError, TypeError, ValueError):
                    continue

                if (
                    get_power_constraints is not None
                    and get_default_power is not None
                    and set_power is not None
                ):
                    minimum, maximum, default = (
                        ctypes.c_uint(),
                        ctypes.c_uint(),
                        ctypes.c_uint(),
                    )
                    try:
                        power_supported = (
                            get_power_constraints(
                                handle, ctypes.byref(minimum), ctypes.byref(maximum)
                            )
                            == 0
                            and get_default_power(handle, ctypes.byref(default)) == 0
                            and minimum.value <= default.value <= maximum.value
                            and maximum.value > 0
                        )
                    except Exception:
                        power_supported = False
                    if power_supported:
                        fields.append("power_limit_w")

                for field in _CLOCK_OFFSET_SPECS:
                    if self._clock_offset_plan(
                        handle, field, index, required=False
                    ) is not None:
                        fields.append(field)
                fields.extend(self._locked_clock_fields(handle))
                fan_plan = self._fan_control_plan(handle)
                if (
                    fan_plan is not None
                    and max(_GPU_FAN_SAFE_MIN_PERCENT, fan_plan["minimum"])
                    <= min(_GPU_FAN_SAFE_MAX_PERCENT, fan_plan["maximum"])
                ):
                    fields.append("fan_percent")
                    self._last_supported_fan_limits[index] = (
                        fan_plan["minimum"],
                        fan_plan["maximum"],
                    )

            return result
        finally:
            try:
                self._shutdown()
            except Exception:
                pass

    def _preflight(self, setting: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._preflight_plan(setting)
        except Exception as exc:
            raise self._target_error(exc, setting, "query") from exc

    def _preflight_plan(self, setting: dict[str, Any]) -> dict[str, Any]:
        index = int(setting["index"])
        handle = self._device(index)
        plan: dict[str, Any] = {"index": index, "handle": handle, "setting": setting}
        expected_uuid = setting.get("_expected_gpu_uuid")
        if expected_uuid is not None:
            self._verify_handle_uuid(handle, index, expected_uuid)

        if "power_limit_w" in setting:
            get_constraints = self._required_function(
                "nvmlDeviceGetPowerManagementLimitConstraints",
                [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint),
                    ctypes.POINTER(ctypes.c_uint),
                ],
            )
            minimum, maximum = ctypes.c_uint(), ctypes.c_uint()
            self._check(
                get_constraints(handle, ctypes.byref(minimum), ctypes.byref(maximum)),
                f"query GPU {index} power limits",
                failure_target={"index": index, "field": "power_limit_w", "operation": "query"},
            )
            requested = int(round(float(setting["power_limit_w"]) * 1000))
            if not minimum.value <= requested <= maximum.value:
                raise GpuOcError(
                    f"GPU {index} power limit is outside hardware range "
                    f"{minimum.value / 1000:g}-{maximum.value / 1000:g} W",
                    failure_target={"index": index, "field": "power_limit_w", "operation": "query"},
                )
            plan["power_mw"] = requested
            plan["set_power"] = self._required_function(
                "nvmlDeviceSetPowerManagementLimit",
                [ctypes.c_void_p, ctypes.c_uint],
            )

        for field, (_clock_type, _query_name, _setter_name, label) in (
            _CLOCK_OFFSET_SPECS.items()
        ):
            if field not in setting:
                continue
            offset_plan = self._clock_offset_plan(
                handle, field, index, required=True
            )
            assert offset_plan is not None
            requested = int(setting[field])
            if not offset_plan["minimum"] <= requested <= offset_plan["maximum"]:
                raise GpuOcError(
                    f"GPU {index} {label} is outside hardware range "
                    f"{offset_plan['minimum']}-{offset_plan['maximum']} MHz",
                    failure_target={"index": index, "field": field, "operation": "query"},
                )
            plan[field] = requested
            plan[f"{field}_plan"] = offset_plan

        lock_specs = (
            (
                "core_clock_lock_mhz",
                "nvmlDeviceSetGpuLockedClocks",
                "nvmlDeviceResetGpuLockedClocks",
                0,  # NVML_CLOCK_GRAPHICS
                "core clock lock",
            ),
            (
                "memory_clock_lock_mhz",
                "nvmlDeviceSetMemoryLockedClocks",
                "nvmlDeviceResetMemoryLockedClocks",
                2,  # NVML_CLOCK_MEM
                "memory clock lock",
            ),
        )
        get_max_clock = self._optional_function(
            "nvmlDeviceGetMaxClockInfo",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)],
            ctypes.c_int,
        )
        for field, setter_name, reset_name, clock_type, label in lock_specs:
            if field in setting:
                requested = int(setting[field])
                if get_max_clock is not None:
                    maximum = ctypes.c_uint()
                    self._check(
                        get_max_clock(
                            handle,
                            ctypes.c_uint(clock_type),
                            ctypes.byref(maximum),
                        ),
                        f"query GPU {index} maximum {label}",
                        failure_target={"index": index, "field": field, "operation": "query"},
                    )
                    if requested > maximum.value:
                        raise GpuOcError(
                            f"GPU {index} {label} exceeds hardware maximum "
                            f"{maximum.value} MHz",
                            failure_target={"index": index, "field": field, "operation": "query"},
                        )
                plan[field] = requested
                plan[f"set_{field}"] = self._required_function(
                    setter_name,
                    [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint],
                )

        if "fan_percent" in setting:
            fan_plan = self._fan_control_plan(handle, index=index, required=True)
            if fan_plan is None:
                raise GpuOcError(
                    f"GPU {index} does not expose safe NVML manual fan control",
                    failure_target={"index": index, "field": "fan_percent", "operation": "query"},
                )
            requested = int(setting["fan_percent"])
            if not fan_plan["minimum"] <= requested <= fan_plan["maximum"]:
                raise GpuOcError(
                    f"GPU {index} fan speed is outside hardware range "
                    f"{fan_plan['minimum']}-{fan_plan['maximum']}%",
                    failure_target={"index": index, "field": "fan_percent", "operation": "query"},
                )
            plan["fan_count"] = fan_plan["fan_count"]
            plan["set_fan"] = fan_plan["set_fan"]
            plan["reset_fan"] = fan_plan["reset_fan"]
        return plan

    @staticmethod
    def _field_setting(setting: dict[str, Any], field: str) -> dict[str, Any]:
        result = {"index": int(setting["index"]), field: setting[field]}
        if "_expected_gpu_uuid" in setting:
            result["_expected_gpu_uuid"] = setting["_expected_gpu_uuid"]
        return result

    @staticmethod
    def _target_error(
        exc: Exception, setting: dict[str, Any], operation: str
    ) -> GpuOcError:
        target = dict(getattr(exc, "failure_target", None) or {})
        target.setdefault("index", int(setting["index"]))
        target.setdefault("operation", operation)
        if "_expected_gpu_uuid" in setting:
            target["_expected_gpu_uuid"] = setting["_expected_gpu_uuid"]
        fields = [field for field in _GPU_OC_FIELDS if field in setting]
        if len(fields) == 1 and operation != "query":
            target.setdefault("field", fields[0])
        return GpuOcError(
            str(exc), nvml_codes=getattr(exc, "nvml_codes", ()),
            recovery=getattr(exc, "recovery", None),
            write_attempted=getattr(exc, "write_attempted", False),
            failure_target=target,
        )

    @staticmethod
    def _recovery_error(
        original: Exception,
        confirmed: list[dict[str, Any]],
        errors: list[Exception],
        status: str,
        *,
        write_attempted: bool = True,
    ) -> GpuOcError:
        """Keep driver classification and only proven final defaults."""

        rows: dict[tuple[int, str | None], dict[str, Any]] = {}
        for setting in confirmed:
            index = int(setting["index"])
            gpu_uuid = setting.get("_expected_gpu_uuid")
            key = (index, gpu_uuid)
            row = rows.setdefault(key, {"index": index, "reset": []})
            if gpu_uuid is not None:
                row["_expected_gpu_uuid"] = gpu_uuid
            for field in _GPU_OC_FIELDS:
                if field in setting and field not in row["reset"]:
                    row["reset"].append(field)
        for row in rows.values():
            row["reset"].sort(key=_GPU_OC_FIELDS.index)
        codes = tuple(dict.fromkeys(
            code
            for error in (original, *errors)
            for code in getattr(error, "nvml_codes", ())
        ))
        inherited = getattr(original, "recovery", None) or {}
        all_messages = [*inherited.get("errors", []), *[str(error) for error in errors]]
        messages = [message[:256] for message in all_messages[:8]]
        if len(all_messages) > 8:
            messages[-1] = f"{len(all_messages) - 7} additional recovery failures"
        if messages:
            status = "partial" if rows else "failed"
        if _NVML_HARDWARE_FAULT_CODES.intersection(codes):
            status = "partial" if rows else "failed"
        message = str(original)
        if errors:
            message += "; rollback also failed: " + "; ".join(str(error)[:256] for error in errors[:8])
        return GpuOcError(
            message,
            nvml_codes=codes,
            recovery={
                "status": status,
                "confirmed_reset": list(rows.values()),
                "hardware_reset_required": _NVML_ERROR_RESET_REQUIRED in codes,
                "hardware_fault": bool(_NVML_HARDWARE_FAULT_CODES.intersection(codes)),
                "errors": messages,
            },
            write_attempted=write_attempted,
            failure_target=getattr(original, "failure_target", None),
        )

    def apply(self, settings: list[dict[str, Any]]) -> dict[str, Any]:
        self._check(self._init(), "initialize NVML")
        journal: list[dict[str, Any]] = []
        confirmed: list[dict[str, Any]] = []
        rollback_errors: list[Exception] = []
        fan_attempted = False
        active_setting: dict[str, Any] | None = None
        try:
            # Validate capabilities and hardware limits for every GPU first.
            plans = [self._preflight(setting) for setting in settings]
            operations: list[dict[str, Any]] = []
            for plan in plans:
                index = plan["index"]
                handle = plan["handle"]
                setting = plan["setting"]
                applied: list[str] = []
                if "power_mw" in plan:
                    active_setting = self._field_setting(setting, "power_limit_w")
                    journal.append(active_setting)
                    self._check(
                        plan["set_power"](handle, ctypes.c_uint(plan["power_mw"])),
                        f"set GPU {index} power limit",
                        write_attempted=True,
                    )
                    applied.append("power_limit_w")
                for field in (
                    "core_clock_offset_mhz",
                    "memory_clock_offset_mhz",
                ):
                    if field in plan:
                        active_setting = self._field_setting(setting, field)
                        journal.append(active_setting)
                        self._set_clock_offset(
                            handle,
                            plan[f"{field}_plan"],
                            plan[field],
                            f"set GPU {index} {field}",
                        )
                        applied.append(field)
                for field in (
                    "core_clock_lock_mhz",
                    "memory_clock_lock_mhz",
                ):
                    if field in plan:
                        active_setting = self._field_setting(setting, field)
                        journal.append(active_setting)
                        requested = ctypes.c_uint(plan[field])
                        self._check(
                            plan[f"set_{field}"](handle, requested, requested),
                            f"set GPU {index} {field}",
                            write_attempted=True,
                        )
                        applied.append(field)
                if "fan_count" in plan:
                    active_setting = self._field_setting(setting, "fan_percent")
                    attempted_fans: list[int] = []
                    try:
                        for fan in range(plan["fan_count"]):
                            fan_attempted = True
                            attempted_fans.append(fan)
                            self._check(
                                plan["set_fan"](
                                    handle,
                                    ctypes.c_uint(fan),
                                    ctypes.c_uint(int(setting["fan_percent"])),
                                ),
                                f"set GPU {index} fan {fan}",
                                write_attempted=True,
                            )
                    except Exception:
                        reset_fans: list[int] = []
                        for fan in reversed(attempted_fans):
                            try:
                                self._check(
                                    plan["reset_fan"](
                                        handle,
                                        ctypes.c_uint(fan),
                                    ),
                                    f"reset GPU {index} fan {fan}",
                                    write_attempted=True,
                                )
                                reset_fans.append(fan)
                            except Exception as rollback_exc:
                                rollback_errors.append(rollback_exc)
                        if len(reset_fans) == plan["fan_count"]:
                            confirmed.append(self._field_setting(setting, "fan_percent"))
                        raise
                    journal.append(self._field_setting(setting, "fan_percent"))
                    applied.append("fan_percent")
                operations.append({"index": index, "applied": applied})
            return {"vendor": "nvidia", "operations": operations}
        except Exception as exc:
            # A setter failure does not prove that the driver left the field
            # untouched.  Compensate each attempted field once, including the
            # failed setter, without touching a later unattempted field.
            if not journal and not fan_attempted:
                raise
            if active_setting is not None:
                exc = self._target_error(exc, active_setting, "apply")
            for setting in reversed(journal):
                try:
                    self.reset([setting])
                    confirmed.append(setting)
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
            status = (
                "partial" if confirmed else "failed"
            ) if rollback_errors else "defaults_restored"
            raise self._recovery_error(exc, confirmed, rollback_errors, status) from exc
        finally:
            try:
                self._shutdown()
            except Exception:
                pass

    def reset(self, settings: list[dict[str, Any]]) -> dict[str, Any]:
        """Return only settings previously owned by RigPilot to driver defaults."""

        if not settings:
            return {"vendor": "nvidia", "operations": []}
        self._check(self._init(), "initialize NVML")
        write_attempted = False
        active_setting: dict[str, Any] | None = None
        try:
            plans: list[dict[str, Any]] = []
            for setting in settings:
                active_setting = setting
                index = int(setting["index"])
                handle = self._device(index)
                expected_uuid = setting.get("_expected_gpu_uuid")
                if expected_uuid is not None:
                    self._verify_handle_uuid(handle, index, expected_uuid)
                plan: dict[str, Any] = {
                    "index": index,
                    "handle": handle,
                    "setting": setting,
                }
                if "power_limit_w" in setting:
                    plan["get_default_power"] = self._required_function(
                        "nvmlDeviceGetPowerManagementDefaultLimit",
                        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)],
                    )
                    plan["set_power"] = self._required_function(
                        "nvmlDeviceSetPowerManagementLimit",
                        [ctypes.c_void_p, ctypes.c_uint],
                    )
                    default_power = ctypes.c_uint()
                    self._check(
                        plan["get_default_power"](
                            handle, ctypes.byref(default_power)
                        ),
                        f"query GPU {index} default power limit",
                    )
                    plan["default_power"] = default_power.value
                if "core_clock_offset_mhz" in setting:
                    plan["reset_core_offset"] = self._clock_offset_reset_plan(
                        "core_clock_offset_mhz"
                    )
                if "memory_clock_offset_mhz" in setting:
                    plan["reset_memory_offset"] = self._clock_offset_reset_plan(
                        "memory_clock_offset_mhz"
                    )
                if "core_clock_lock_mhz" in setting:
                    plan["reset_core_lock"] = self._required_function(
                        "nvmlDeviceResetGpuLockedClocks", [ctypes.c_void_p]
                    )
                if "memory_clock_lock_mhz" in setting:
                    plan["reset_memory_lock"] = self._required_function(
                        "nvmlDeviceResetMemoryLockedClocks", [ctypes.c_void_p]
                    )
                if "fan_percent" in setting:
                    get_num_fans = self._required_function(
                        "nvmlDeviceGetNumFans",
                        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)],
                    )
                    fan_count = ctypes.c_uint()
                    self._check(
                        get_num_fans(handle, ctypes.byref(fan_count)),
                        f"query GPU {index} fans",
                    )
                    if not 1 <= fan_count.value <= 32:
                        raise GpuOcError(
                            f"GPU {index} reported an invalid controllable fan count"
                        )
                    plan["fan_count"] = int(fan_count.value)
                    plan["reset_fan"] = self._required_function(
                        "nvmlDeviceSetDefaultFanSpeed_v2",
                        [ctypes.c_void_p, ctypes.c_uint],
                    )
                    plan["set_fan"] = self._required_function(
                        "nvmlDeviceSetFanSpeed_v2",
                        [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint],
                    )
                plans.append(plan)

            operations: list[dict[str, Any]] = []
            for plan in plans:
                index = plan["index"]
                handle = plan["handle"]
                setting = plan["setting"]
                reset_fields: list[str] = []
                if "default_power" in plan:
                    active_setting = self._field_setting(setting, "power_limit_w")
                    write_attempted = True
                    self._check(
                        plan["set_power"](
                            handle, ctypes.c_uint(plan["default_power"])
                        ),
                        f"reset GPU {index} power limit",
                        write_attempted=True,
                    )
                    reset_fields.append("power_limit_w")
                if "reset_core_offset" in plan:
                    active_setting = self._field_setting(setting, "core_clock_offset_mhz")
                    write_attempted = True
                    self._reset_clock_offset(
                        handle,
                        plan["reset_core_offset"],
                        f"reset GPU {index} core clock offset",
                    )
                    reset_fields.append("core_clock_offset_mhz")
                if "reset_memory_offset" in plan:
                    active_setting = self._field_setting(setting, "memory_clock_offset_mhz")
                    write_attempted = True
                    self._reset_clock_offset(
                        handle,
                        plan["reset_memory_offset"],
                        f"reset GPU {index} memory clock offset",
                    )
                    reset_fields.append("memory_clock_offset_mhz")
                if "reset_core_lock" in plan:
                    active_setting = self._field_setting(setting, "core_clock_lock_mhz")
                    write_attempted = True
                    self._check(
                        plan["reset_core_lock"](handle),
                        f"reset GPU {index} core clock lock",
                        write_attempted=True,
                    )
                    reset_fields.append("core_clock_lock_mhz")
                if "reset_memory_lock" in plan:
                    active_setting = self._field_setting(setting, "memory_clock_lock_mhz")
                    write_attempted = True
                    self._check(
                        plan["reset_memory_lock"](handle),
                        f"reset GPU {index} memory clock lock",
                        write_attempted=True,
                    )
                    reset_fields.append("memory_clock_lock_mhz")
                if "reset_fan" in plan:
                    active_setting = self._field_setting(setting, "fan_percent")
                    reset_fans: list[int] = []
                    try:
                        for fan in range(plan["fan_count"]):
                            write_attempted = True
                            self._check(
                                plan["reset_fan"](handle, ctypes.c_uint(fan)),
                                f"reset GPU {index} fan {fan}",
                                write_attempted=True,
                            )
                            reset_fans.append(fan)
                    except Exception as exc:
                        rollback_errors: list[Exception] = []
                        # Device faults prohibit replaying the prior manual
                        # policy, including faults first seen during replay.
                        restore_fans = [] if getattr(exc, "hardware_fault", False) else reset_fans
                        for fan in reversed(restore_fans):
                            try:
                                self._check(
                                    plan["set_fan"](
                                        handle,
                                        ctypes.c_uint(fan),
                                        ctypes.c_uint(
                                            int(plan["setting"]["fan_percent"])
                                        ),
                                    ),
                                    f"restore GPU {index} fan {fan}",
                                    write_attempted=True,
                                )
                            except Exception as rollback_exc:
                                rollback_errors.append(rollback_exc)
                                if getattr(rollback_exc, "hardware_fault", False):
                                    break
                        if rollback_errors:
                            raise self._recovery_error(
                                exc, [], rollback_errors, "failed"
                            ) from exc
                        raise
                    reset_fields.append("fan_percent")
                operations.append({"index": index, "reset": reset_fields})
            return {"vendor": "nvidia", "operations": operations}
        except Exception as exc:
            if active_setting is not None:
                exc = self._target_error(
                    exc, active_setting, "reset" if write_attempted else "query"
                )
            if isinstance(exc, GpuOcError):
                exc.write_attempted = exc.write_attempted or write_attempted
                raise exc
            raise GpuOcError(str(exc), write_attempted=write_attempted) from exc
        finally:
            try:
                self._shutdown()
            except Exception:
                pass

    def transition(
        self,
        previous: list[dict[str, Any]],
        current: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Replace RigPilot-owned fields without touching an unmodified field.

        Each one-field write carries the ephemeral UUID binding through the
        exact NVML session that opens the handle and performs the mutation.
        Successful writes are journalled individually, so a later failure never
        resets all fields from ``current`` (which could erase unrelated OC).
        """

        def field_settings(
            settings: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for setting in settings:
                for field in _GPU_OC_FIELDS:
                    if field in setting:
                        result.append(self._field_setting(setting, field))
            return result

        reset_operations: dict[int, list[str]] = {}
        apply_operations: dict[int, list[str]] = {}
        # (inverse operation, exact one-field setting)
        journal: list[tuple[str, dict[str, Any]]] = []
        confirmed: dict[tuple[int, str | None, str], dict[str, Any]] = {}

        def remember(setting: dict[str, Any], *, remove: bool = False) -> None:
            for field in _GPU_OC_FIELDS:
                if field not in setting:
                    continue
                key = (int(setting["index"]), setting.get("_expected_gpu_uuid"), field)
                if remove:
                    confirmed.pop(key, None)
                else:
                    confirmed[key] = self._field_setting(setting, field)

        def remember_recovery(exc: Exception) -> None:
            recovery = getattr(exc, "recovery", None)
            if not isinstance(recovery, dict):
                return
            for row in recovery.get("confirmed_reset", []):
                setting = {"index": row["index"]}
                if "_expected_gpu_uuid" in row:
                    setting["_expected_gpu_uuid"] = row["_expected_gpu_uuid"]
                for field in row["reset"]:
                    if field in _GPU_OC_FIELDS:
                        setting[field] = 0
                remember(setting)

        phase = "preflight"
        failed_setting: dict[str, Any] | None = None
        try:
            if current:
                # Reject invalid requests and an already-faulted card before
                # resetting any previous field.  Each actual apply still
                # rechecks its bound handle and current hardware limits.
                self._check(self._init(), "initialize NVML")
                try:
                    for setting in current:
                        failed_setting = setting
                        self._preflight(setting)
                finally:
                    try:
                        self._shutdown()
                    except Exception:
                        pass
            phase = "reset"
            for setting in field_settings(previous):
                failed_setting = setting
                result = self.reset([setting])
                index = int(setting["index"])
                reset_operations.setdefault(index, []).extend(
                    result["operations"][0]["reset"]
                )
                journal.append(("apply", setting))
                remember(setting)
            phase = "apply"
            for setting in field_settings(current):
                failed_setting = setting
                try:
                    result = self.apply([setting])
                except Exception as exc:
                    if getattr(exc, "recovery", None) is not None:
                        if exc.recovery["errors"]:
                            remember(setting, remove=True)
                        remember_recovery(exc)
                    raise
                remember(setting, remove=True)
                index = int(setting["index"])
                apply_operations.setdefault(index, []).extend(
                    result["operations"][0]["applied"]
                )
                journal.append(("reset", setting))
        except Exception as exc:
            if failed_setting is not None and getattr(exc, "failure_target", None) is None:
                exc = self._target_error(exc, failed_setting, "query" if phase == "preflight" else phase)
            rollback_errors: list[Exception] = []
            blocked_indices: set[int] = set()
            if getattr(exc, "hardware_fault", False) and failed_setting is not None:
                blocked_indices.add(int(failed_setting["index"]))
            write_attempted = bool(journal) or getattr(exc, "write_attempted", False)
            for operation, setting in reversed(journal):
                try:
                    if operation == "apply":
                        index = int(setting["index"])
                        same_failed_value = (
                            phase == "apply"
                            and failed_setting is not None
                            and index == int(failed_setting["index"])
                            and any(
                                field in setting and field in failed_setting
                                and setting[field] == failed_setting[field]
                                for field in _GPU_OC_FIELDS
                            )
                        )
                        if index in blocked_indices or same_failed_value:
                            continue
                        write_attempted = True
                        try:
                            self.apply([setting])
                        except Exception as restore_exc:
                            if getattr(restore_exc, "recovery", None) is not None:
                                if restore_exc.recovery["errors"]:
                                    remember(setting, remove=True)
                                remember_recovery(restore_exc)
                            raise
                        remember(setting, remove=True)
                    else:
                        write_attempted = True
                        self.reset([setting])
                        remember(setting)
                except Exception as rollback_exc:
                    rollback_errors.append(rollback_exc)
                    if getattr(rollback_exc, "hardware_fault", False):
                        blocked_indices.add(int(setting["index"]))
            status = (
                "partial" if confirmed else "failed"
            ) if rollback_errors else (
                "defaults_restored" if confirmed else "rolled_back" if journal else "failed"
            )
            raise self._recovery_error(
                exc, list(confirmed.values()), rollback_errors, status,
                write_attempted=write_attempted,
            ) from exc
        return {
            "vendor": "nvidia",
            "write_attempted": bool(journal),
            "reset": [
                {"index": index, "reset": reset_operations[index]}
                for index in reset_operations
            ],
            "operations": [
                {"index": index, "applied": apply_operations[index]}
                for index in apply_operations
            ],
        }
