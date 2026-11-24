"""Portable host inventory and health metric collection."""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import logging
import math
import os
import platform
import re
import socket
import stat
import subprocess
import sys
import time
from dataclasses import replace
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

import statistics
from collections import deque

import psutil
import requests

from server.pci_identity import board_vendor_label
from server.miner_adapters import MINER_ADAPTERS
from server.miner_adapters.validation import parse_pool_endpoint
from server.miner_adapters.retired import retired_miner_type
from .seine_runtime import parse_seine_runtime_state
from .seine_telemetry import SeineTelemetryObserver

from .external_process import external_process_environment
from .miner_packages import _retired_managed_package_type
from .linux_gpu_telemetry import canonical_pci_id, collect_gpu_snapshot, compact_snapshot
from .managed_miners import (
    ManagedMinerError,
    _managed_role_runtime,
    _wsl_identity_from_record,
    load_pending_secondary_marker,
)
from .process_identity import (
    managed_process_argv_matches_record,
    securely_rebind_managed_process,
)
from .wsl_mining import (
    WSL_GPU_MINING_CAPABILITY,
    WslMiningError,
    query_wsl_guest,
    wsl_cached_executable_matches,
    wsl_executable_path,
)

try:
    import winreg
except ImportError:  # pragma: no cover - winreg is available only on Windows.
    winreg = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from .config import AgentConfig

LOGGER = logging.getLogger(__name__)


_MINING_STATES = {
    "running",
    "stopped",
    "stale",
    "error",
    "starting",
    "stopping",
    "failed",
    "unknown",
    "not_configured",
}

_MANAGED_PROCESS_V1_FIELDS = frozenset(
    {
        "version",
        "pid",
        "executable",
        "create_time",
        "miner_type",
        "flight_sheet_id",
        "started_at",
        "survives_agent_restart",
    }
)
_LEGACY_MANAGED_PROCESS_FIELDS = _MANAGED_PROCESS_V1_FIELDS - {
    "survives_agent_restart"
}
_MANAGED_PROCESS_V2_FIELDS = _MANAGED_PROCESS_V1_FIELDS | frozenset(
    {"argv_sha256", "api_port"}
)
_MANAGED_PROCESS_V3_FIELDS = _MANAGED_PROCESS_V2_FIELDS | frozenset(
    {"execution_mode", "target_executable"}
)
_MANAGED_PROCESS_V4_FIELDS = _MANAGED_PROCESS_V3_FIELDS | frozenset(
    {"wsl_token", "wsl_guest_executable", "wsl_identity"}
)
_MANAGED_PROCESS_V5_FIELDS = _MANAGED_PROCESS_V4_FIELDS | frozenset(
    {"wsl_cache_key"}
)
_MANAGED_PROCESS_V7_FIELDS = _MANAGED_PROCESS_V2_FIELDS | frozenset(
    {"seine_data_dir"}
)
_MANAGED_MINER_TYPES = MINER_ADAPTERS.miner_types
_MANAGED_MINER_EXECUTABLES = {
    (system_name, adapter.legacy_miner_type): adapter.executable_candidates(
        system_name
    )
    for adapter in MINER_ADAPTERS.list()
    for system_name in ("Windows", "Linux")
}
_PROCESS_CREATE_TIME_TOLERANCE_SECONDS = 0.5
_HOST_REBOOT_PROCESS_ERROR = "host rebooted; managed miner was not restarted"

# Private hand-off from the asynchronous HostDataSampler worker to the HTTP
# heartbeat thread.  The client removes this field before sending inventory to
# the controller.  Keeping capability probes in the same disposable telemetry
# worker is important on USB/HDD Linux installations: update-layout, systemd,
# SSH and other readiness checks touch the local filesystem, and a sick block
# device must not be able to park the authenticated heartbeat thread in D I/O.
_HEARTBEAT_CAPABILITIES_KEY = "_heartbeat_capabilities"
_HEARTBEAT_MAINTENANCE_KEY = "_heartbeat_maintenance"
_MAINTENANCE_UNKNOWN = "unknown"


def _linux_heartbeat_maintenance_hints(
    *,
    supervision: dict[str, Any],
) -> dict[str, bool | str]:
    """Probe fixed maintenance markers only on the telemetry worker.

    These are hints, never authorization: the main-thread transaction still
    revalidates every path when a hint is true.  A false/absent hint merely
    keeps ordinary idle heartbeats away from the installation disk.
    """

    from .agent_updates import LINUX_DATA_ROOT, LINUX_PROGRAM_ROOT

    # ``False`` means the complete trusted boundary was inspected and no work
    # exists.  An I/O/parse failure is deliberately different: reporting it as
    # false can strand a replacement Agent or suppress a rollback receipt just
    # because a degraded boot disk could not be read on this pass.  Runtime
    # treats ``unknown`` as retryable worker state, never as authority to scan
    # the installation disk on an otherwise idle heartbeat thread.
    pending_update: bool | str = False
    rollback_receipt: bool | str = False
    try:
        pending_path = LINUX_PROGRAM_ROOT / "pending-update.json"
        pending_info = pending_path.lstat()
        if (
            stat.S_ISREG(pending_info.st_mode)
            and pending_info.st_size <= 32_768
        ):
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            activation_id = (
                pending.get("activation_id") if isinstance(pending, dict) else None
            )
            if (
                isinstance(activation_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", activation_id) is not None
            ):
                reported = (
                    LINUX_DATA_ROOT
                    / "agent-updates"
                    / "reported"
                    / f"{activation_id}.json"
                )
                pending_update = not reported.is_file()
            else:
                pending_update = _MAINTENANCE_UNKNOWN
        else:
            pending_update = _MAINTENANCE_UNKNOWN
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, json.JSONDecodeError):
        pending_update = _MAINTENANCE_UNKNOWN
    staged = LINUX_DATA_ROOT / "agent-updates" / "staged"
    try:
        with os.scandir(staged) as entries:
            # Successful activation directories intentionally retain their
            # small terminal receipt.  A fixed "first N" cutoff would
            # eventually hide a newer rollback forever because scandir order
            # is not chronological.  This scan keeps memory constant and each
            # candidate is still confined to a 32-hex root-owned directory and
            # a <=32 KiB receipt; it runs only on HostDataSampler's disposable
            # worker, never on the authenticated heartbeat thread.
            for entry in entries:
                if not entry.name or re.fullmatch(r"[0-9a-f]{32}", entry.name) is None:
                    continue
                try:
                    job = Path(entry.path)
                    receipt_path = job / "activation-result.json"
                    reported_path = job / "rollback-reported.json"
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    receipt_info = receipt_path.lstat()
                    if (
                        not stat.S_ISREG(receipt_info.st_mode)
                        or receipt_info.st_size > 32_768
                    ):
                        rollback_receipt = _MAINTENANCE_UNKNOWN
                        continue
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if (
                        isinstance(receipt, dict)
                        and receipt.get("status") == "rolled_back"
                    ):
                        try:
                            reported_info = reported_path.lstat()
                        except FileNotFoundError:
                            rollback_receipt = True
                            break
                        if (
                            not stat.S_ISREG(reported_info.st_mode)
                            or reported_info.st_size > 32_768
                        ):
                            rollback_receipt = _MAINTENANCE_UNKNOWN
                            continue
                        reported = json.loads(
                            reported_path.read_text(encoding="utf-8")
                        )
                        expected_report = {
                            "schema_version": 1,
                            "command_id": receipt.get("command_id"),
                            "reported": True,
                        }
                        if reported != expected_report:
                            rollback_receipt = _MAINTENANCE_UNKNOWN
                        continue
                    if not isinstance(receipt, dict) or receipt.get("status") not in {
                        "succeeded",
                        "rolled_back",
                    }:
                        rollback_receipt = _MAINTENANCE_UNKNOWN
                except FileNotFoundError:
                    continue
                except (OSError, UnicodeError, json.JSONDecodeError):
                    # A malformed/unreadable candidate means the scan cannot
                    # prove that no rollback is waiting.  Keep scanning so a
                    # later valid receipt can still upgrade this to ``True``.
                    rollback_receipt = _MAINTENANCE_UNKNOWN
                    continue
    except FileNotFoundError:
        pass
    except OSError:
        rollback_receipt = _MAINTENANCE_UNKNOWN
    return {
        "pending_agent_update": pending_update,
        "rollback_receipt": rollback_receipt,
        "supervision_repair": bool(
            supervision.get("configured") is True
            and supervision.get("ready") is not True
        ),
    }

_MANAGED_TELEMETRY_API_PATHS = {
    "builtin.xmrig.http.v1": "/2/summary",
    "builtin.srbminer.http.v1": "/",
    "builtin.seine.http.v1": "/v1/runtime/state",
}
_SEINE_TELEMETRY_OBSERVER = SeineTelemetryObserver()
_MANAGED_LOG_TELEMETRY_IDS = frozenset({"builtin.pearlminer.log.v1"})
_DECLARED_MANAGED_TELEMETRY_IDS = frozenset(
    adapter.telemetry_id for adapter in MINER_ADAPTERS.list()
)
if _DECLARED_MANAGED_TELEMETRY_IDS != (
    frozenset(_MANAGED_TELEMETRY_API_PATHS) | _MANAGED_LOG_TELEMETRY_IDS
):
    raise RuntimeError(
        "managed miner adapter telemetry and trusted Agent collectors disagree"
    )

# Miner aggregates can freeze at an implausibly high rate during a GPU stall.
# Apply the same gate to all telemetry sources before publishing their samples.
_HASHRATE_GATE_HISTORY = 12
_HASHRATE_GATE_MAX_FACTOR = 8.0
# Bursty low-rate series (Seine blocknet windows at single-digit H/s) jump
# 15x within one legitimate stats window because Poisson variance dominates
# there.  An absolute slack keeps those series flowing while staying twelve
# orders of magnitude below every observed PH/s spike.
_HASHRATE_GATE_ABS_SLACK = 1000.0
_HASHRATE_GATE_RAMP_CONFIRM = 36
_HASHRATE_GATE_STALE_SECONDS = 600.0
_HASHRATE_GATE_MAX_SERIES = 64
_HASHRATE_GATE_GPU_MIN_FACTOR = 0.1
_HASHRATE_GATE_GPU_MAX_FACTOR = 4.0


class _HashrateAmplitudeGate:
    """Reject aggregate rate spikes one mining session cannot produce.

    A reading is accepted when it stays within ``MAX_FACTOR`` of the rolling
    median of recently accepted nonzero samples for its series (session,
    coin, algorithm) and, when the same payload carries per-GPU rows, inside
    a loose band around their sum.  Measured zeros and low readings always
    pass and never seed the baseline: a collapsing rig is real signal, an
    inflated aggregate is a miner artifact.  A sustained elevation that
    outlives every observed frozen plateau re-baselines the series so a
    genuine change is eventually accepted instead of rejected forever.
    """

    def __init__(self) -> None:
        self._series: dict[
            tuple[Any, Any, Any], dict[str, Any]
        ] = {}

    def reset(self) -> None:
        self._series.clear()

    def _series_state(self, key: tuple[Any, Any, Any], now: float) -> dict[str, Any]:
        state = self._series.get(key)
        if state is not None and now - state["last_seen"] <= _HASHRATE_GATE_STALE_SECONDS:
            return state
        if len(self._series) >= _HASHRATE_GATE_MAX_SERIES:
            oldest = min(self._series, key=lambda item: self._series[item]["last_seen"])
            self._series.pop(oldest)
        state = {"accepted": deque(maxlen=_HASHRATE_GATE_HISTORY), "last_seen": now, "elevated_run": 0}
        self._series[key] = state
        return state

    def observe(
        self,
        key: tuple[Any, Any, Any],
        rate: float,
        *,
        gpu_total: float | None,
        now: float,
    ) -> str | None:
        """Return ``None`` when accepted, otherwise a fixed reason code."""
        if not math.isfinite(rate) or rate < 0:
            return "invalid_rate"
        if rate == 0:
            state = self._series.get(key)
            if state is not None:
                state["last_seen"] = now
            return None
        if gpu_total is not None and gpu_total > 0 and not (
            _HASHRATE_GATE_GPU_MIN_FACTOR * gpu_total
            <= rate
            <= _HASHRATE_GATE_GPU_MAX_FACTOR * gpu_total
        ):
            return "gpu_total_mismatch"
        state = self._series_state(key, now)
        state["last_seen"] = now
        accepted = state["accepted"]
        # The upper-bound check also covers the seeding window (<3 samples,
        # median of what exists): a parser reading a stale frozen log during
        # session startup must not poison a fresh baseline with a fake rate,
        # and the same 36-sample sustained-elevation rebuild stays available
        # for a legitimately ramping series.  Series with per-GPU rows are
        # already bounded above by the gpu_total band regardless of seeding.
        if accepted:
            baseline = statistics.median(tuple(accepted))
            if rate > baseline * _HASHRATE_GATE_MAX_FACTOR + _HASHRATE_GATE_ABS_SLACK:
                state["elevated_run"] += 1
                if state["elevated_run"] < _HASHRATE_GATE_RAMP_CONFIRM:
                    return "above_baseline"
                # Sustained elevation: every observed frozen miner plateau
                # ended far sooner.  Accept it and rebuild the baseline.
                accepted.clear()
        state["elevated_run"] = 0
        accepted.append(rate)
        return None


_HASHRATE_AMPLITUDE_GATE = _HashrateAmplitudeGate()
_LOCAL_MINER_API_TIMEOUT = (0.25, 0.75)
_MAX_MINER_API_RESPONSE_BYTES = 256 * 1024
_MAX_PEARL_LOG_BYTES = 512 * 1024
_PEARL_LOG_FRESH_SECONDS = 150
_PEARL_RATE = re.compile(
    r'proof_per_sec="(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kMGTPE]?H|[kMGTPE]?)/s"',
    re.IGNORECASE,
)
_PEARL_INLINE_RATE = re.compile(
    r'(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kMGTPE]?H|[kMGTPE]?)/s',
    re.IGNORECASE,
)
_PEARL_DEVICE_PROGRESS = re.compile(
    r"\bevent=large\.progress\.device\b.*?\bdevice=(?P<index>\d{1,2})\b",
    re.IGNORECASE,
)
_PEARL_TOTAL_PROGRESS = re.compile(r"\bevent=large\.progress\b", re.IGNORECASE)
_PEARL_DEVICE_LIST = re.compile(r'\bdevices="\[(?P<devices>[^"\r\n]*)\]"', re.IGNORECASE)
_PEARL_TUI_TOTAL = re.compile(
    r"\|\s*TOTAL\s*\|[^|\r\n]{0,32}\|[^|\r\n]{0,32}\|\s*"
    r"(?P<proof_s>\d{1,16})\s*/\s*(?P<proof_r>\d{1,16})\s*/\s*"
    r"(?P<proof_d>\d{1,16})\s*\|\s*"
    r"(?P<accepted>\d{1,16})\s*/\s*(?P<rejected>\d{1,16})\s*"
    r"\(\s*(?P<stale>\d{1,16})\s*\)\s*\|",
    re.IGNORECASE,
)
_PEARL_TUI_HEADER = re.compile(
    r"\bProof\s+S\s*/\s*R\s*/\s*D\b.*?\bPool\s+A\s*/\s*R\s*\(\s*St\s*\)",
    re.IGNORECASE,
)
_ANSI_CONTROL_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_SAFE_TELEMETRY_COUNTER = (1 << 53) - 1

_GPU_OC_NUMERIC_FIELDS = frozenset(
    {
        "power_limit_w",
        "core_clock_offset_mhz",
        "core_clock_lock_mhz",
        "memory_clock_offset_mhz",
        "memory_clock_lock_mhz",
        "fan_percent",
    }
)


def ip_addresses() -> list[str]:
    found: set[str] = set()
    for addresses in psutil.net_if_addrs().values():
        for address in addresses:
            if address.family in (socket.AF_INET, socket.AF_INET6):
                value = address.address.split("%", 1)[0]
                if value and not value.startswith("127.") and value != "::1":
                    found.add(value)
    return sorted(found)


_LAN_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16")
)
_LAN_IPV6_NETWORK = ipaddress.ip_network("fc00::/7")


def network_ip_addresses(addresses: list[str] | None = None) -> dict[str, Any]:
    """Classify interface addresses without contacting an external IP service.

    The controller observes the connection source and treats that as the
    authoritative public address.  A directly assigned global interface
    address is still reported as a fallback for controllers behind a local
    reverse proxy, while loopback, multicast and link-local addresses are
    deliberately omitted from the user-facing network inventory.
    """

    local: set[str] = set()
    public: set[str] = set()
    for raw in addresses if addresses is not None else ip_addresses():
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except (AttributeError, ValueError):
            continue
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            continue
        if isinstance(address, ipaddress.IPv4Address):
            if any(address in network for network in _LAN_IPV4_NETWORKS):
                local.add(str(address))
            elif address.is_global:
                public.add(str(address))
        elif address in _LAN_IPV6_NETWORK:
            local.add(address.compressed)
        elif address.is_global:
            public.add(address.compressed)
    return {
        "schema_version": 1,
        "local_ip_addresses": sorted(local),
        "public_ip_addresses": sorted(public),
    }


def _disk_metrics() -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for partition in psutil.disk_partitions(all=False):
        if partition.mountpoint in seen:
            continue
        seen.add(partition.mountpoint)
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except (OSError, PermissionError):
            continue
        disks.append(
            {
                "device": partition.device,
                "mountpoint": partition.mountpoint,
                "filesystem": partition.fstype,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": usage.percent,
            }
        )
    return disks


def _float_or_none(value: str) -> float | None:
    value = value.strip()
    if not value or value.upper() in {
        "N/A",
        "NOT SUPPORTED",
        "[NOT SUPPORTED]",
        "[NO PERMISSION]",
    }:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _text_or_none(value: str | None, *, maximum: int = 256) -> str | None:
    """Normalize optional ``nvidia-smi`` text without retaining error labels."""

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().split())
    if (
        not normalized
        or len(normalized) > maximum
        or normalized.upper()
        in {
            "N/A",
            "NOT SUPPORTED",
            "[NOT SUPPORTED]",
            "[NO PERMISSION]",
        }
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        return None
    return normalized


# Board partner identity uses the complete pinned PCI vendor registry shared
# with the Controller. Unknown IDs remain available in the original fields;
# board identity must never be used to infer a DRAM manufacturer.


def _pci_subsystem_vendor_id(value: str | None) -> str | None:
    """Return the four-digit vendor component of an NVIDIA subsystem ID."""

    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    if not normalized or len(normalized) > 8:
        return None
    try:
        normalized = f"{int(normalized, 16):08x}"
    except ValueError:
        return None
    return normalized[-4:]


_NVIDIA_BASE_FIELDS = (
    "index",
    "name",
    "uuid",
    "pci.bus_id",
    "utilization.gpu",
    "memory.total",
    "memory.used",
    "temperature.gpu",
    "power.draw",
)

_NVIDIA_DETAIL_FIELDS = (
    "pci.sub_device_id",
    "serial",
    "vbios_version",
    "driver_version",
    "power.limit",
    "power.default_limit",
    "power.min_limit",
    "power.max_limit",
)

_NVIDIA_IDENTITY_FIELDS = (
    "pci.sub_device_id",
    "vbios_version",
    "driver_version",
)

_NVIDIA_POWER_FIELDS = (
    "power.limit",
    "power.default_limit",
    "power.min_limit",
    "power.max_limit",
)

# Fan telemetry is optional even on otherwise supported NVIDIA cards.  Keep it
# out of both the base inventory and combined static-detail query: older
# drivers can reject ``fan.speed`` for a passive card and would otherwise make
# the Agent discard every GPU row in that request.
_NVIDIA_FAN_FIELDS = ("fan.speed",)

# Current clocks are dynamic telemetry and are queried independently from both
# the static detail group and fan speed.  One unsupported optional field must
# never make the Agent discard otherwise valid GPU inventory.
_NVIDIA_CLOCK_FIELDS = (
    "clocks.current.graphics",
    "clocks.current.memory",
)


def _nvidia_bus_key(value: str | None) -> str | None:
    """Normalize NVIDIA PCI domain:bus:device.function for safe row merging."""

    if value is None:
        return None
    normalized = value.strip().lower()
    parts = normalized.split(":")
    if len(parts) == 2:
        parts.insert(0, "0000")
    if len(parts) != 3 or "." not in parts[2]:
        return None
    device, function = parts[2].split(".", 1)
    try:
        domain = int(parts[0], 16)
        bus = int(parts[1], 16)
        device_number = int(device, 16)
        function_number = int(function, 16)
    except ValueError:
        return None
    if (
        not 0 <= domain <= 0xFFFFFFFF
        or not 0 <= bus <= 0xFF
        or not 0 <= device_number <= 0x1F
        or not 0 <= function_number <= 7
    ):
        return None
    return f"{domain:08x}:{bus:02x}:{device_number:02x}.{function_number:x}"


def _nvidia_detail_rows(
    executable: str,
    fields: tuple[str, ...],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, dict[str, str]] | None:
    """Return detail values keyed by PCI bus without trusting row order."""

    query_fields = ("pci.bus_id",) + fields
    rows = _query_nvidia_csv(executable, query_fields, runner)
    if rows is None:
        return None
    details: dict[str, dict[str, str]] = {}
    for row in rows:
        bus_key = _nvidia_bus_key(row[0])
        if bus_key is None or bus_key in details:
            return None
        details[bus_key] = dict(zip(fields, row[1:], strict=True))
    return details


def _merge_nvidia_details(
    target: dict[str, dict[str, str]],
    source: dict[str, dict[str, str]] | None,
) -> None:
    if source is None:
        return
    for bus_key, values in source.items():
        target.setdefault(bus_key, {}).update(values)


def _fallback_nvidia_static_details(
    executable: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, dict[str, str]]:
    """Collect independent detail groups after a combined query is rejected.

    A driver can reject one unsupported field and fail the complete CSV query.
    Identity, serial and power are therefore isolated.  If the compact identity
    group is also rejected, its three fields are retried one by one so board
    vendor information is not lost just because VBIOS is unavailable.
    """

    details: dict[str, dict[str, str]] = {}
    identity = _nvidia_detail_rows(executable, _NVIDIA_IDENTITY_FIELDS, runner)
    if identity is None:
        for field in _NVIDIA_IDENTITY_FIELDS:
            _merge_nvidia_details(
                details, _nvidia_detail_rows(executable, (field,), runner)
            )
    else:
        _merge_nvidia_details(details, identity)
    _merge_nvidia_details(
        details, _nvidia_detail_rows(executable, ("serial",), runner)
    )
    _merge_nvidia_details(
        details, _nvidia_detail_rows(executable, _NVIDIA_POWER_FIELDS, runner)
    )
    return details


def _query_nvidia_csv(
    executable: str,
    fields: tuple[str, ...],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> list[list[str]] | None:
    try:
        completed = runner(
            [
                executable,
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=external_process_environment("Linux"),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    rows = list(csv.reader(io.StringIO(completed.stdout)))
    if not rows or any(len(row) != len(fields) for row in rows):
        return None
    return rows


def _read_linux_pci_hex(path: Path, *, digits: int) -> str | None:
    """Read one bounded hexadecimal sysfs PCI attribute."""

    try:
        raw = path.read_text(encoding="ascii").strip().lower()
    except (OSError, UnicodeError):
        return None
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != digits or re.fullmatch(r"[0-9a-f]+", raw) is None:
        return None
    return raw


def linux_nvidia_display_devices(
    devices_root: Path = Path("/sys/bus/pci/devices"),
) -> list[dict[str, Any]]:
    """Return bounded PCI identities when the NVIDIA driver is unavailable.

    This probe deliberately reads only Linux sysfs.  It is not a replacement
    for NVML and never invents an NVML index, UUID, temperature, utilization,
    clock or power value.  Its sole purpose is to distinguish a host with no
    NVIDIA display device from a host whose NVIDIA hardware exists but whose
    driver failed to bind.
    """

    try:
        candidates = sorted(devices_root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    devices: list[dict[str, Any]] = []
    for candidate in candidates[:512]:
        vendor_id = _read_linux_pci_hex(candidate / "vendor", digits=4)
        class_id = _read_linux_pci_hex(candidate / "class", digits=6)
        if vendor_id != "10de" or class_id is None:
            continue
        # PCI class 0x03 is display; NVIDIA audio/USB functions must not be
        # mistaken for GPUs just because they share the NVIDIA vendor ID.
        if int(class_id, 16) >> 16 != 0x03:
            continue
        bus_key = _nvidia_bus_key(candidate.name)
        if bus_key is None:
            continue
        device_id = _read_linux_pci_hex(candidate / "device", digits=4)
        subsystem_vendor = _read_linux_pci_hex(
            candidate / "subsystem_vendor", digits=4
        )
        subsystem_device = _read_linux_pci_hex(
            candidate / "subsystem_device", digits=4
        )
        subsystem_id = (
            f"{subsystem_device}{subsystem_vendor}"
            if subsystem_device is not None and subsystem_vendor is not None
            else None
        )
        devices.append(
            {
                "index": None,
                "name": "NVIDIA GPU（驱动不可用）",
                "uuid": None,
                "vendor": "nvidia",
                "device_type": "discrete",
                "pci_bus_id": bus_key,
                "pci_device_id": device_id,
                "pci_subsystem_id": subsystem_id,
                "board_vendor_id": subsystem_vendor,
                "board_vendor": board_vendor_label(subsystem_vendor),
                "driver_available": False,
                "health": "error",
                "error": "nvidia_driver_unavailable",
                "utilization_percent": None,
                "memory_total_mib": None,
                "memory_used_mib": None,
                "memory_percent": None,
                "temperature_c": None,
                "power_w": None,
                "fan_speed_percent": None,
                "graphics_clock_mhz": None,
                "memory_clock_mhz": None,
                "serial_number": None,
                "vbios_version": None,
                "driver_version": None,
                "power_limit_w": None,
                "power_default_limit_w": None,
                "power_min_limit_w": None,
                "power_max_limit_w": None,
                "memory_vendor": None,
                "memory_type": None,
                "memory_identity_source": None,
                "memory_identity_status": "unavailable",
                "memory_identity_unavailable_reason": "nvidia_driver_unavailable",
            }
        )
        if len(devices) >= 64:
            break
    return devices


def nvidia_gpu_metrics(
    executable: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    memory_probe: Callable[[], dict[str, dict[str, str]]] | None = None,
    system_name: str | None = None,
    linux_pci_probe: Callable[[], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Query NVIDIA GPUs without a shell and tolerate older driver fields.

    Some older ``nvidia-smi`` versions reject the whole request when one of the
    detail fields is unknown.  In that case the Agent retries independent
    detail groups and merges them by PCI bus, rather than losing fields that the
    driver does support.  On Windows a read-only NVAPI probe can enrich memory
    maker/type. Linux uses the driver framebuffer GET through a private NVML
    export. Both native libraries are isolated in a bounded cached child;
    unknown/failure stays explicit instead of being replaced by old material.
    """

    resolved_system = system_name or platform.system()
    fields = _NVIDIA_BASE_FIELDS + _NVIDIA_DETAIL_FIELDS
    combined_rows = _query_nvidia_csv(executable, fields, runner)
    details_by_bus: dict[str, dict[str, str]] = {}
    if combined_rows is not None:
        rows = [row[: len(_NVIDIA_BASE_FIELDS)] for row in combined_rows]
        for row in combined_rows:
            bus_key = _nvidia_bus_key(row[3])
            if bus_key is not None:
                details_by_bus[bus_key] = dict(
                    zip(
                        _NVIDIA_DETAIL_FIELDS,
                        row[len(_NVIDIA_BASE_FIELDS) :],
                        strict=True,
                    )
                )
    else:
        rows = _query_nvidia_csv(executable, _NVIDIA_BASE_FIELDS, runner)
        if rows is not None:
            details_by_bus = _fallback_nvidia_static_details(executable, runner)
    if rows is None and resolved_system == "Linux":
        try:
            fallback = (
                linux_pci_probe()
                if linux_pci_probe is not None
                else linux_nvidia_display_devices()
            )
        except Exception:
            fallback = []
        return [
            dict(device)
            for device in fallback[:64]
            if isinstance(device, dict)
            and device.get("vendor") == "nvidia"
            and device.get("driver_available") is False
        ]
    if rows is None:
        return []

    # Always probe fan speed independently.  Unsupported fields, a timeout, or
    # malformed optional output simply return ``None`` from the detail helper;
    # the already collected base inventory remains authoritative and intact.
    _merge_nvidia_details(
        details_by_bus,
        _nvidia_detail_rows(executable, _NVIDIA_FAN_FIELDS, runner),
    )
    _merge_nvidia_details(
        details_by_bus,
        _nvidia_detail_rows(executable, _NVIDIA_CLOCK_FIELDS, runner),
    )

    memory_by_bus: dict[str, dict[str, Any]] = {}
    native_source = "nvapi" if resolved_system == "Windows" else "nvml_private"
    selection = []
    for row in rows:
        detail = details_by_bus.get(_nvidia_bus_key(row[3]) or "", {})
        selection.append({"uuid": row[2].strip(), "pci_bus_id": row[3].strip(),
                          "driver_version": _text_or_none(detail.get("driver_version", "")),
                          "vbios_version": _text_or_none(detail.get("vbios_version", ""))})
    if resolved_system in {"Linux", "Windows"}:
        try:
            if memory_probe is not None:
                # Existing no-argument injection seam remains for pure tests;
                # production always passes the complete SMI identity selection.
                candidate = memory_probe()
            else:
                from .nvidia_memory import collect_nvidia_memory
                candidate = collect_nvidia_memory(selection, resolved_system)
            if isinstance(candidate, dict):
                memory_by_bus = {key: value for raw, value in candidate.items()
                                 if (key := _nvidia_bus_key(raw)) is not None}
        except Exception:  # pragma: no cover - third-party driver boundary.
            memory_by_bus = {}

    gpus: list[dict[str, Any]] = []
    for row in rows:
        bus_key = _nvidia_bus_key(row[3])
        detail = details_by_bus.get(bus_key or "", {})
        memory_candidate = memory_by_bus.get(bus_key or "", {})
        memory_detail = memory_candidate if isinstance(memory_candidate, dict) else {}
        pci_subsystem_id = _text_or_none(detail.get("pci.sub_device_id", ""))
        board_vendor_id = _pci_subsystem_vendor_id(pci_subsystem_id)
        vbios_version = _text_or_none(detail.get("vbios_version", ""))
        memory_vendor = _text_or_none(
            memory_detail.get("memory_vendor", ""), maximum=32
        )
        memory_type = _text_or_none(
            memory_detail.get("memory_type", ""), maximum=32
        )
        memory_identity_source: str | None = (
            native_source if resolved_system in {"Linux", "Windows"} else None
        )

        if memory_vendor is not None and memory_type is not None:
            memory_identity_status = "complete"
            memory_identity_unavailable_reason = None
        elif memory_vendor is not None or memory_type is not None:
            memory_identity_status = "partial"
            memory_identity_unavailable_reason = "driver_reported_partial_identity"
        else:
            memory_identity_status = "unavailable"
            memory_identity_unavailable_reason = memory_detail.get(
                "memory_identity_unavailable_reason", "native_probe_unavailable"
            )
        gpus.append(
            {
                "index": int(row[0].strip()) if row[0].strip().isdigit() else row[0].strip(),
                "name": row[1].strip(),
                "uuid": row[2].strip(),
                # These two fields make the controller's numeric NVIDIA/NVML
                # index traceable to another miner's mixed-vendor index.  In
                # particular, SRBMiner numbers OpenCL iGPUs before CUDA cards
                # on some hosts, so the two numeric indices are not identities.
                "vendor": "nvidia",
                "device_type": "discrete",
                "pci_bus_id": row[3].strip(),
                "utilization_percent": _float_or_none(row[4]),
                "memory_total_mib": _float_or_none(row[5]),
                "memory_used_mib": _float_or_none(row[6]),
                "temperature_c": _float_or_none(row[7]),
                "power_w": _float_or_none(row[8]),
                "fan_speed_percent": _float_or_none(detail.get("fan.speed", "")),
                "graphics_clock_mhz": _float_or_none(
                    detail.get("clocks.current.graphics", "")
                ),
                "memory_clock_mhz": _float_or_none(
                    detail.get("clocks.current.memory", "")
                ),
                "pci_subsystem_id": pci_subsystem_id,
                "board_vendor_id": board_vendor_id,
                "board_vendor": board_vendor_label(board_vendor_id),
                "serial_number": _text_or_none(detail.get("serial", "")),
                "vbios_version": vbios_version,
                "driver_version": _text_or_none(detail.get("driver_version", "")),
                "power_limit_w": _float_or_none(detail.get("power.limit", "")),
                "power_default_limit_w": _float_or_none(
                    detail.get("power.default_limit", "")
                ),
                "power_min_limit_w": _float_or_none(
                    detail.get("power.min_limit", "")
                ),
                "power_max_limit_w": _float_or_none(
                    detail.get("power.max_limit", "")
                ),
                "memory_vendor": memory_vendor,
                "memory_type": memory_type,
                "memory_identity_source": memory_identity_source,
                "memory_identity_status": memory_identity_status,
                "memory_identity_unavailable_reason": (
                    memory_identity_unavailable_reason
                ),
            }
        )
    return gpus


def annotate_nvidia_oc_capabilities(
    gpus: list[dict[str, Any]],
    *,
    backend_factory: Callable[[], Any] | None = None,
) -> None:
    """Attach per-card NVML controls proven by read-only runtime probes.

    An empty, present ``oc_capabilities`` list means the probe completed but no
    safe controls were confirmed.  Older Agents omit the field entirely, which
    lets the controller use a deliberately conservative compatibility fallback.
    """

    indices = [
        int(gpu["index"])
        for gpu in gpus
        if str(gpu.get("vendor", "")).lower() == "nvidia"
        and isinstance(gpu.get("index"), int)
        and not isinstance(gpu.get("index"), bool)
    ]
    if not indices:
        return
    capabilities: dict[int, list[str]] = {}
    fan_limits: dict[int, tuple[int, int]] = {}
    try:
        if backend_factory is None:
            from .gpu_oc import NvmlGpuOcBackend

            backend_factory = NvmlGpuOcBackend
        backend = backend_factory()
        candidate = backend.supported_fields(indices)
        if isinstance(candidate, dict):
            capabilities = candidate
        limit_reader = getattr(backend, "supported_fan_limits", None)
        candidate_limits = limit_reader() if callable(limit_reader) else {}
        if isinstance(candidate_limits, dict):
            fan_limits = candidate_limits
    except Exception:
        capabilities = {}
    allowed = {
        "power_limit_w",
        "core_clock_offset_mhz",
        "core_clock_lock_mhz",
        "memory_clock_offset_mhz",
        "memory_clock_lock_mhz",
        "fan_percent",
    }
    for gpu in gpus:
        index = gpu.get("index")
        if (
            str(gpu.get("vendor", "")).lower() != "nvidia"
            or isinstance(index, bool)
            or not isinstance(index, int)
        ):
            continue
        values = capabilities.get(index, [])
        gpu["oc_capabilities"] = [
            value for value in values if isinstance(value, str) and value in allowed
        ]
        fan_range = fan_limits.get(index)
        if (
            "fan_percent" in gpu["oc_capabilities"]
            and isinstance(fan_range, tuple)
            and len(fan_range) == 2
            and all(isinstance(value, int) and not isinstance(value, bool) for value in fan_range)
            and 0 <= fan_range[0] <= fan_range[1] <= 100
        ):
            gpu["fan_min_percent"], gpu["fan_max_percent"] = fan_range


def _bounded_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read a small regular local JSON file without following links."""

    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} does not exist") from exc
    except OSError as exc:
        raise ValueError(f"{label} cannot be inspected") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        bool(getattr(info, "st_file_attributes", 0) & reparse_flag)
        or not stat.S_ISREG(info.st_mode)
    ):
        raise ValueError(f"{label} must be a regular local file")
    if info.st_size > 64 * 1024:
        raise ValueError(f"{label} is too large")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{label} root must be an object")
    return raw


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _gpu_oc_settings_summary(
    raw: Any, *, allow_model_match: bool
) -> list[dict[str, Any]] | None:
    """Return bounded display-only OC settings or reject the whole profile.

    Flight-sheet rules may target an SRBMiner model substring while standalone
    NVML settings always target a numeric NVIDIA index.  Keeping this parser
    independent from command validation makes heartbeat collection read-only
    and prevents malformed local state from becoming trusted UI metadata.
    """

    if not isinstance(raw, list) or not raw or len(raw) > 32:
        return None
    normalized: list[dict[str, Any]] = []
    for setting in raw:
        if not isinstance(setting, dict):
            return None
        allowed = {"index"} | set(_GPU_OC_NUMERIC_FIELDS)
        if allow_model_match:
            allowed.add("model_match")
        if not set(setting) <= allowed:
            return None
        index = setting.get("index")
        model_match = setting.get("model_match")
        has_index = (
            not isinstance(index, bool)
            and isinstance(index, int)
            and 0 <= index <= 31
        )
        has_model = (
            allow_model_match
            and isinstance(model_match, str)
            and bool(model_match.strip())
            and len(model_match.strip()) <= 96
            and not any(ord(character) < 32 for character in model_match)
        )
        if has_index == has_model:
            return None
        item: dict[str, Any] = (
            {"index": index}
            if has_index
            else {"model_match": model_match.strip()}
        )
        for field in _GPU_OC_NUMERIC_FIELDS:
            if field not in setting:
                continue
            value = setting[field]
            if _finite_number(value) is None:
                return None
            item[field] = value
        if len(item) == 1:
            return None
        normalized.append(item)
    return normalized


def _standalone_gpu_oc_state_path(config: "AgentConfig") -> Path:
    from .gpu_oc import gpu_oc_state_path

    return gpu_oc_state_path(config)


def _standalone_gpu_oc_profile(config: "AgentConfig") -> dict[str, Any] | None:
    """Read the Agent-owned NVML profile without changing mining health."""

    try:
        state = _bounded_json_object(
            _standalone_gpu_oc_state_path(config), "GPU OC state file"
        )
    except ValueError:
        return None
    version = state.get("version")
    expected_fields = (
        {"version", "settings"}
        if version == 1
        else {"version", "devices"}
        if version == 2
        else set()
    )
    if set(state) != expected_fields:
        return None
    raw_settings = state.get("settings")
    device_uuids: list[str] = []
    if version == 2:
        devices = state.get("devices")
        if not isinstance(devices, list):
            return None
        raw_settings = []
        for device in devices:
            if (
                not isinstance(device, dict)
                or set(device) != {"gpu_uuid", "index_hint", "setting"}
                or not isinstance(device.get("setting"), dict)
            ):
                return None
            gpu_uuid = device.get("gpu_uuid")
            if (
                not isinstance(gpu_uuid, str)
                or not 4 <= len(gpu_uuid) <= 128
                or any(
                    character.isspace()
                    or ord(character) < 32
                    or ord(character) == 127
                    for character in gpu_uuid
                )
                or gpu_uuid in device_uuids
            ):
                return None
            device_uuids.append(gpu_uuid)
            raw_settings.append(
                {"index": device.get("index_hint"), **device["setting"]}
            )
    settings = _gpu_oc_settings_summary(
        raw_settings, allow_model_match=False
    )
    if not settings:
        return None
    if version == 2:
        if len(settings) != len(device_uuids):
            return None
        for setting, gpu_uuid in zip(settings, device_uuids):
            setting["uuid"] = gpu_uuid
    return {
        "source": "nvml",
        "state": "applied_record",
        "state_version": version,
        "identity_complete": version == 2,
        "settings": settings,
    }


def _managed_process_truth(
    config: "AgentConfig",
    process_factory: Callable[[int], Any],
    system_name: str | None = None,
    boot_time_factory: Callable[[], float] = psutil.boot_time,
    process_iter_factory: Callable[[], Any] = psutil.process_iter,
) -> tuple[str, bool, str | None, dict[str, Any] | None]:
    """Return the state of the exact managed process recorded by RigPilot."""

    runtime = config.managed_miner_runtime_dir
    if runtime is None:
        return "error", False, "managed miner runtime is not configured", None
    try:
        record = _bounded_json_object(
            runtime / "managed-miner-process.json", "managed miner process record"
        )
    except ValueError as exc:
        if str(exc).endswith(" does not exist"):
            return "absent", False, None, None
        return "error", False, "managed miner process record is invalid", None

    if retired_miner_type(record.get("miner_type")) is not None:
        # Retain the saved identity for an explicit retired status, without
        # inspecting, adopting or requesting telemetry from its process.
        return "error", False, "managed miner adapter is retired", record

    if record.get("miner_type") == "hiveos" or record.get("version") == 6:
        # HiveOS sessions were removed.  A legacy supervisor record cannot be
        # verified or adopted; report it as an explicit error for manual care.
        return (
            "error",
            False,
            "legacy HiveOS managed process record is no longer supported",
            record,
        )

    fields = frozenset(record)
    version = record.get("version")
    restart_safe = record.get("survives_agent_restart", False)
    executable = record.get("executable")
    target_executable = record.get("target_executable")
    created = record.get("create_time")
    wsl_record = version in {3, 4, 5}
    if (
        not (
            (version == 2 and fields == _MANAGED_PROCESS_V2_FIELDS)
            or (version == 3 and fields == _MANAGED_PROCESS_V3_FIELDS)
            or (version == 4 and fields == _MANAGED_PROCESS_V4_FIELDS)
            or (version == 5 and fields == _MANAGED_PROCESS_V5_FIELDS)
            or (version == 7 and fields == _MANAGED_PROCESS_V7_FIELDS)
            or (
                version == 1
                and fields
                in {_MANAGED_PROCESS_V1_FIELDS, _LEGACY_MANAGED_PROCESS_FIELDS}
            )
        )
        or isinstance(record.get("pid"), bool)
        or not isinstance(record.get("pid"), int)
        or record["pid"] <= 0
        or record.get("miner_type") not in _MANAGED_MINER_TYPES
        or not isinstance(executable, str)
        or not executable.strip()
        or len(executable) > 4096
        or not Path(executable).is_absolute()
        or _finite_number(created) is None
        or not isinstance(record.get("flight_sheet_id"), str)
        or not isinstance(record.get("started_at"), str)
        or not isinstance(restart_safe, bool)
        or (
            wsl_record
            and (
                record.get("execution_mode") != "wsl"
                or not isinstance(target_executable, str)
                or not target_executable.strip()
                or len(target_executable) > 4096
                or not Path(target_executable).is_absolute()
            )
        )
    ):
        return "error", False, "managed miner process record is invalid", None
    if version in {2, 3, 4, 5, 7} and (
        not isinstance(record.get("argv_sha256"), str)
        or len(record["argv_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in record["argv_sha256"]
        )
        or isinstance(record.get("api_port"), bool)
        or not isinstance(record.get("api_port"), int)
        or not 1024 <= record["api_port"] <= 65535
    ):
        return "error", False, "managed miner process record is invalid", None
    if version == 7 and (
        record.get("miner_type") != "seine"
        or not isinstance(record.get("seine_data_dir"), str)
        or not record["seine_data_dir"].strip()
        or not Path(record["seine_data_dir"]).is_absolute()
        or len(record["seine_data_dir"]) > 4096
        or "\x00" in record["seine_data_dir"]
    ):
        return "error", False, "managed Seine process record is invalid", record
    if version == 5 and (
        not isinstance(record.get("wsl_cache_key"), str)
        or re.fullmatch(r"[0-9a-f]{64}", record["wsl_cache_key"]) is None
        or not isinstance(record.get("wsl_guest_executable"), str)
        or not wsl_cached_executable_matches(
            record["wsl_guest_executable"], record["wsl_cache_key"]
        )
    ):
        return "error", False, "managed WSL cache identity is invalid", record

    try:
        current_boot_time = float(boot_time_factory())
    except (OSError, TypeError, ValueError):
        current_boot_time = math.nan
    if math.isfinite(current_boot_time) and float(created) < current_boot_time:
        # A PID from a previous Windows boot must never be inspected or used
        # as permission to restart mining.  Ordinary Agent startup is
        # intentionally observational here; only a bound update transaction
        # may explicitly resume a miner stopped by that transaction.
        return "rebooted", True, _HOST_REBOOT_PROCESS_ERROR, record

    if config.miner_packages_dir is None:
        return "error", False, "managed miner package directory is not configured", record
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    host_system = system_name or platform.system()
    try:
        packages_root = config.miner_packages_dir.resolve(strict=True)
    except OSError:
        return "error", False, "managed miner package directory cannot be inspected", record

    supervised_guest_alive: bool | None = None
    if wsl_record:
        if host_system != "Windows":
            return "error", False, "managed WSL miner record is valid only on Windows", record
        target_names = _MANAGED_MINER_EXECUTABLES.get(
            ("Linux", str(record["miner_type"]))
        )
        if target_names is None or Path(str(target_executable)).name not in target_names:
            return "error", False, "managed WSL miner target name is invalid", record
        try:
            trusted_wrapper = wsl_executable_path()
            recorded_path = Path(executable).resolve(strict=True)
        except (OSError, WslMiningError):
            return "error", False, "trusted Windows WSL wrapper is unavailable", record
        if os.path.normcase(os.path.abspath(executable)) != os.path.normcase(
            os.path.abspath(trusted_wrapper)
        ):
            return "error", False, "managed WSL wrapper path is invalid", record
        if version in {4, 5}:
            try:
                supervised_guest_alive = query_wsl_guest(
                    recorded_path,
                    str(record["wsl_token"]),
                    str(record["wsl_guest_executable"]),
                    _wsl_identity_from_record(record),
                )
            except (KeyError, ManagedMinerError, WslMiningError):
                return "error", False, "managed WSL guest identity is invalid", record
            if supervised_guest_alive:
                return "running", True, None, record
        raw_target = Path(str(target_executable))
        try:
            target_info = raw_target.lstat()
            target_path = raw_target.resolve(strict=True)
            target_path.relative_to(packages_root)
        except FileNotFoundError:
            if version != 5:
                return (
                    "error",
                    False,
                    "managed WSL miner target cannot be inspected",
                    record,
                )
            try:
                raw_target.resolve(strict=False).relative_to(packages_root)
            except (OSError, ValueError):
                return (
                    "error",
                    False,
                    "managed WSL miner target is outside the package directory",
                    record,
                )
        except (OSError, ValueError):
            return (
                "error",
                False,
                "managed WSL miner target is outside the package directory",
                record,
            )
        else:
            if (
                bool(
                    getattr(target_info, "st_file_attributes", 0) & reparse_flag
                )
                or not stat.S_ISREG(target_info.st_mode)
            ):
                return (
                    "error",
                    False,
                    "managed WSL miner target is not a regular local file",
                    record,
                )
    else:
        expected_names = _MANAGED_MINER_EXECUTABLES.get(
            (host_system, str(record["miner_type"]))
        )
        if expected_names is None or Path(executable).name not in expected_names:
            return "error", False, "managed miner executable name is invalid", record
        try:
            recorded_info = Path(executable).lstat()
        except OSError:
            return "error", False, "managed miner executable cannot be inspected", record
        if (
            bool(getattr(recorded_info, "st_file_attributes", 0) & reparse_flag)
            or not stat.S_ISREG(recorded_info.st_mode)
        ):
            return "error", False, "managed miner executable is not a regular local file", record
        try:
            recorded_path = Path(executable).resolve(strict=True)
            recorded_path.relative_to(packages_root)
        except (OSError, ValueError):
            return "error", False, "managed miner executable is outside the package directory", record

    def try_rebind() -> tuple[dict[str, Any], Any] | None:
        # WSL's host wrapper is a one-to-one launch identity.  Guessing another
        # wsl.exe process could bind this record to a different distro command,
        # so v3 records deliberately never use the native watchdog scan.
        if wsl_record:
            return None
        assert config.managed_miner_runtime_dir is not None
        return securely_rebind_managed_process(
            state_path=(
                config.managed_miner_runtime_dir / "managed-miner-process.json"
            ),
            record=record,
            recorded_path=recorded_path,
            process_iter_factory=process_iter_factory,
            create_time_tolerance=_PROCESS_CREATE_TIME_TOLERANCE_SECONDS,
        )

    def rebound_or_stale(message: str) -> tuple[str, bool, str | None, dict[str, Any] | None]:
        rebound = try_rebind()
        if rebound is not None:
            rebound_record, _process = rebound
            return "running", True, None, rebound_record
        return "stale", False, message, record

    try:
        process = process_factory(int(record["pid"]))
    except psutil.NoSuchProcess:
        return rebound_or_stale("managed miner process no longer exists")
    except (psutil.Error, OSError, ValueError):
        return "error", False, "managed miner process cannot be inspected", record

    try:
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return rebound_or_stale("managed miner process no longer exists")
        actual_executable = process.exe()
        actual_created = float(process.create_time())
    except psutil.NoSuchProcess:
        return rebound_or_stale("managed miner process no longer exists")
    except (psutil.Error, OSError, TypeError, ValueError):
        return "error", False, "managed miner process identity cannot be read", record
    if (
        not isinstance(actual_executable, str)
        or not actual_executable
        or not math.isfinite(actual_created)
    ):
        return "error", False, "managed miner process identity cannot be read", record
    if os.path.normcase(os.path.realpath(actual_executable)) != os.path.normcase(
        os.path.realpath(recorded_path)
    ):
        return rebound_or_stale("managed miner PID belongs to another executable")
    if abs(actual_created - float(created)) > _PROCESS_CREATE_TIME_TOLERANCE_SECONDS:
        return rebound_or_stale("managed miner PID creation time does not match")
    try:
        argv_matches = managed_process_argv_matches_record(
            process,
            record=record,
            recorded_path=recorded_path,
        )
    except psutil.NoSuchProcess:
        return rebound_or_stale("managed miner process no longer exists")
    except (psutil.Error, OSError, TypeError, ValueError, AttributeError):
        return (
            "error",
            False,
            "managed miner process command line cannot be inspected",
            record,
        )
    if not argv_matches:
        return rebound_or_stale("managed miner process command line does not match")
    if version in {4, 5} and not supervised_guest_alive:
        return "stale", False, "managed WSL guest process no longer exists", record
    return "running", True, None, record


def _managed_miner_session_id(record: dict[str, Any] | None) -> str | None:
    """Return a non-secret stable ID for exactly one verified miner session."""

    if not isinstance(record, dict):
        return None
    identity = {
        "flight_sheet_id": record.get("flight_sheet_id"),
        "started_at": record.get("started_at"),
        "pid": record.get("pid"),
        "create_time": record.get("create_time"),
        "argv_sha256": record.get("argv_sha256"),
    }
    if (
        not isinstance(identity["flight_sheet_id"], str)
        or not isinstance(identity["started_at"], str)
        or isinstance(identity["pid"], bool)
        or not isinstance(identity["pid"], int)
        or isinstance(identity["create_time"], bool)
        or not isinstance(identity["create_time"], (int, float))
    ):
        return None
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "managed-" + hashlib.sha256(encoded).hexdigest()[:32]


def _safe_status_text(value: Any, maximum: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(item) < 32 for item in value):
        return None
    return value


def _safe_pool_url(value: Any) -> str | None:
    """Project a shared supported pool endpoint without secrets or paths."""

    text = _safe_status_text(value, 512)
    if text is None:
        return None
    try:
        endpoint = parse_pool_endpoint(
            value,
            {scheme for adapter in MINER_ADAPTERS.list() for scheme in adapter.pool_schemes},
        )
    except ValueError:
        return None
    host = endpoint.host
    if ":" in host:
        host = f"[{host}]"
    return f"{endpoint.scheme}://{host}:{endpoint.port}"


def _retired_leg_type(value: Any) -> str | None:
    """Recognize saved retired identifiers without creating an adapter."""
    if not isinstance(value, dict):
        return None
    package = value.get("miner_package")
    identities = [value.get("miner_type"), value.get("adapter_id"), value.get("miner_profile")]
    if isinstance(package, dict):
        managed_type = _retired_managed_package_type(package.get("id"))
        if managed_type is not None:
            identities.append(managed_type)
    return next(
        (miner_type for identity in identities
         if (miner_type := retired_miner_type(identity)) is not None),
        None,
    )


def _mining_leg_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    profile = _safe_status_text(value.get("miner_profile"), 80)
    if profile is None:
        return None
    package_value = value.get("miner_package")
    package: dict[str, str] | None = None
    if isinstance(package_value, dict):
        candidate = {
            key: _safe_status_text(package_value.get(key), maximum)
            for key, maximum in (("id", 80), ("name", 120), ("version", 80))
        }
        if all(candidate.values()):
            package = {key: str(item) for key, item in candidate.items()}
    summary: dict[str, Any] = {
        "miner_profile": profile,
        "miner_package": package,
    }
    retired_type = _retired_leg_type(value)
    if retired_type is not None:
        summary.update(miner_type=retired_type, retired=True)
    coin = _safe_status_text(value.get("coin"), 24)
    algorithm = _safe_status_text(value.get("algorithm"), 64)
    pool_url = _safe_pool_url(value.get("pool_url"))
    if coin is not None:
        summary["coin"] = coin.upper()
    if algorithm is not None:
        summary["algorithm"] = algorithm.lower()
    if pool_url is not None:
        summary["pool_url"] = pool_url
    return summary


def _service_state(
    service: str | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    system_name: str,
) -> str:
    if not service:
        return "not_configured"
    if system_name == "Windows":
        argv = ["sc.exe", "query", service]
    elif system_name == "Linux":
        argv = ["systemctl", "is-active", "--", service]
    else:
        return "unknown"
    try:
        completed = runner(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=external_process_environment(system_name),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return "unknown"
    output = (completed.stdout or "").strip().lower()
    if system_name == "Windows":
        for marker, state_name in (
            ("running", "running"),
            ("start_pending", "starting"),
            ("continue_pending", "starting"),
            ("stop_pending", "stopping"),
            ("pause_pending", "stopping"),
            ("stopped", "stopped"),
            ("paused", "stopped"),
        ):
            if marker in output:
                return state_name
        return "unknown"
    linux_states = {
        "active": "running",
        "inactive": "stopped",
        "failed": "stopped",
        "activating": "starting",
        "deactivating": "stopping",
    }
    return linux_states.get(output, "unknown")


def _non_negative_number(value: Any) -> float | int | None:
    value = _finite_number(value)
    return value if value is not None and value >= 0 else None


def _miner_stats(value: dict[str, Any]) -> dict[str, Any]:
    hashrate_value = value.get("hashrate")
    hashrate: float | int | dict[str, float | int] | None = _non_negative_number(
        hashrate_value
    )
    if isinstance(hashrate_value, dict):
        normalized: dict[str, float | int] = {}
        if set(hashrate_value) <= {"total", "primary", "secondary"}:
            for key, nested in hashrate_value.items():
                numeric = _non_negative_number(nested)
                if numeric is None:
                    normalized = {}
                    break
                normalized[key] = numeric
        hashrate = normalized or None

    shares_value = value.get("shares")
    shares: dict[str, int] | None = None
    if isinstance(shares_value, dict) and set(shares_value) <= {
        "accepted",
        "rejected",
        "stale",
    }:
        candidate: dict[str, int] = {}
        for key, nested in shares_value.items():
            if isinstance(nested, bool) or not isinstance(nested, int) or nested < 0:
                candidate = {}
                break
            candidate[key] = nested
        shares = candidate or None

    return {
        "hashrate": hashrate,
        "hashrate_unit": _safe_status_text(value.get("hashrate_unit"), 16),
        "shares": shares,
        "stats_updated_at": _safe_status_text(value.get("updated_at"), 64),
    }


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _first_hashrate(value: Any) -> float | int | None:
    """Return the first official interval value that is a finite number."""

    if isinstance(value, (list, tuple)):
        for item in value[:3]:
            numeric = _non_negative_number(item)
            if numeric is not None:
                return numeric
        return None
    return _non_negative_number(value)


def _parse_xmrig_summary(value: Any) -> dict[str, Any] | None:
    """Extract only XMRig's documented /2/summary telemetry fields."""

    if not isinstance(value, dict):
        return None
    hashrate_value: float | int | None = None
    hashrate = value.get("hashrate")
    if isinstance(hashrate, dict):
        hashrate_value = _first_hashrate(hashrate.get("total"))

    accepted: int | None = None
    rejected: int | None = None
    results = value.get("results")
    if isinstance(results, dict):
        accepted = _non_negative_integer(results.get("shares_good"))
        shares_total = _non_negative_integer(results.get("shares_total"))
        if (
            accepted is not None
            and shares_total is not None
            and shares_total >= accepted
        ):
            rejected = shares_total - accepted

    if hashrate_value is None and (accepted is None or rejected is None):
        return None
    return {
        "hashrate": hashrate_value,
        "hashrate_unit": "H/s" if hashrate_value is not None else None,
        "shares": (
            {"accepted": accepted, "rejected": rejected}
            if accepted is not None and rejected is not None
            else None
        ),
    }


def _srb_gpu_hashrates(
    value: Any,
    gpu_index_map: dict[int, int] | None = None,
) -> list[dict[str, float | int]]:
    """Return SRBMiner's real per-GPU rates without deriving a split.

    Current SRBMiner API responses place device rates below
    ``algorithms[].hashrate.gpu`` using names such as ``gpu0``.  ``total`` is
    deliberately ignored: assigning an algorithm total to a card (or dividing
    it by the number of cards) would fabricate device telemetry.
    """

    if not isinstance(value, dict):
        return []
    hashrate = value.get("hashrate")
    if not isinstance(hashrate, dict):
        return []
    gpu_rates = hashrate.get("gpu")
    if not isinstance(gpu_rates, dict):
        return []

    readings: list[dict[str, float | int]] = []
    seen: set[int] = set()
    for key, raw_rate in list(gpu_rates.items())[:65]:
        if not isinstance(key, str):
            continue
        match = re.fullmatch(r"gpu[_-]?(\d{1,2})", key.strip(), flags=re.IGNORECASE)
        if match is None:
            continue
        miner_index = int(match.group(1))
        if miner_index >= 64:
            continue
        # SRBMiner IDs include integrated OpenCL devices, while NVIDIA/NVML
        # indices do not.  ``None`` keeps pre-mapping status files compatible;
        # an explicit map (including an empty one) must match safely or the
        # reading is omitted rather than attached to the wrong physical card.
        if gpu_index_map is None:
            index = miner_index
        else:
            index = gpu_index_map.get(miner_index, -1)
        if index < 0 or index >= 64 or index in seen:
            continue
        rate = _non_negative_number(raw_rate)
        if rate is None:
            continue
        readings.append({"index": index, "hashrate": rate})
        seen.add(index)
    return sorted(readings, key=lambda item: int(item["index"]))


def _srb_algorithm_stats(
    value: Any,
    gpu_index_map: dict[int, int] | None = None,
) -> tuple[
    float | int | None,
    int | None,
    int | None,
    list[dict[str, float | int]],
]:
    if not isinstance(value, dict):
        return None, None, None, []
    current: float | int | None = None
    hashrate = value.get("hashrate")
    if isinstance(hashrate, dict):
        current = _non_negative_number(hashrate.get("now"))
        if current in (None, 0):
            one_minute = _non_negative_number(hashrate.get("1min"))
            if one_minute is None:
                one_minute = _non_negative_number(hashrate.get("1m"))
            if one_minute is not None:
                current = one_minute
    shares = value.get("shares")
    accepted = rejected = None
    if isinstance(shares, dict):
        accepted = _non_negative_integer(shares.get("accepted"))
        rejected = _non_negative_integer(shares.get("rejected"))
    return current, accepted, rejected, _srb_gpu_hashrates(value, gpu_index_map)


def _algorithm_key(value: Any) -> str | None:
    text = _safe_status_text(value, 64)
    if text is None:
        return None
    normalized = "".join(character for character in text.lower() if character.isalnum())
    return normalized or None


def _hashrate_detail(
    role: str,
    leg: dict[str, Any] | None,
    *,
    algorithm: str | None,
    hashrate: float | int | None,
    accepted: int | None,
    rejected: int | None,
    gpu_hashrates: list[dict[str, float | int]] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "role": role,
        "hashrate": hashrate,
        "hashrate_unit": "H/s" if hashrate is not None else None,
        "shares": (
            {"accepted": accepted, "rejected": rejected}
            if accepted is not None and rejected is not None
            else None
        ),
    }
    if isinstance(leg, dict):
        for key in ("coin", "pool_url"):
            candidate = leg.get(key)
            if isinstance(candidate, str):
                detail[key] = candidate
        configured_algorithm = leg.get("algorithm")
        if isinstance(configured_algorithm, str):
            detail["algorithm"] = configured_algorithm
    if "algorithm" not in detail and algorithm is not None:
        detail["algorithm"] = algorithm
    if gpu_hashrates:
        detail["gpu_hashrates"] = [
            {
                "index": reading["index"],
                "hashrate": reading["hashrate"],
                "hashrate_unit": "H/s",
            }
            for reading in gpu_hashrates[:64]
        ]
    return detail


def _parse_srbminer_summary(
    value: Any,
    mining_legs: tuple[dict[str, Any] | None, dict[str, Any] | None] = (None, None),
    gpu_index_map: dict[int, int] | None = None,
) -> dict[str, Any] | None:
    """Extract the current and legacy fields used by SRBMiner's own scripts."""

    if not isinstance(value, dict):
        return None
    readings: list[dict[str, Any]] = []
    accepted_values: list[int] = []
    rejected_values: list[int] = []
    algorithms = value.get("algorithms")
    if isinstance(algorithms, list):
        # SRBMiner currently supports up to four simultaneous algorithms.  The
        # Agent only launches one or two, but this bound keeps parsing cheap if
        # a future official build returns more entries.
        for algorithm_value in algorithms[:4]:
            current, accepted, rejected, gpu_hashrates = _srb_algorithm_stats(
                algorithm_value, gpu_index_map
            )
            if (
                current is None
                and accepted is None
                and rejected is None
                and not gpu_hashrates
            ):
                continue
            name = (
                _safe_status_text(algorithm_value.get("name"), 64)
                if isinstance(algorithm_value, dict)
                else None
            )
            if name is None and isinstance(algorithm_value, dict):
                name = _safe_status_text(algorithm_value.get("algorithm"), 64)
            readings.append(
                {
                    "name": name.lower() if name is not None else None,
                    "key": _algorithm_key(name),
                    "hashrate": current,
                    "accepted": accepted,
                    "rejected": rejected,
                    "gpu_hashrates": gpu_hashrates,
                }
            )
            if accepted is not None and rejected is not None:
                accepted_values.append(accepted)
                rejected_values.append(rejected)
    else:
        # SRBMiner's official HiveOS h-stats.sh retains these pre-0.4.8 names.
        legacy_hashrate = _non_negative_number(value.get("hashrate_total_now"))
        shares = value.get("shares")
        accepted = rejected = None
        if isinstance(shares, dict):
            accepted = _non_negative_integer(shares.get("accepted"))
            rejected = _non_negative_integer(shares.get("rejected"))
            if accepted is not None and rejected is not None:
                accepted_values.append(accepted)
                rejected_values.append(rejected)
        if legacy_hashrate is not None or accepted is not None or rejected is not None:
            legacy_algorithm = _safe_status_text(value.get("algorithm"), 64)
            readings.append(
                {
                    "name": (
                        legacy_algorithm.lower() if legacy_algorithm is not None else None
                    ),
                    "key": _algorithm_key(legacy_algorithm),
                    "hashrate": legacy_hashrate,
                    "accepted": accepted,
                    "rejected": rejected,
                    "gpu_hashrates": [],
                }
            )

    details: list[dict[str, Any]] = []
    unused_readings = set(range(len(readings)))
    configured_legs = [
        (role, leg)
        for role, leg in zip(("primary", "secondary"), mining_legs)
        if isinstance(leg, dict)
    ]
    matched: dict[str, int] = {}
    canonical_legs: dict[str, dict[str, Any]] = {}
    adapter = MINER_ADAPTERS.require("srbminer")
    for role, leg in configured_legs:
        try:
            canonical_algorithm = adapter.normalize_algorithm(
                leg.get("algorithm"), coin=leg.get("coin")
            )
        except ValueError:
            # Keep an explicit historical name comparable to its actual API
            # name; unknown metadata never authorizes process actions here.
            canonical_algorithm = leg.get("algorithm")
        canonical_legs[role] = {**leg, "algorithm": canonical_algorithm}
        expected_key = _algorithm_key(canonical_algorithm)
        if expected_key is None:
            continue
        match = next(
            (
                index
                for index in sorted(unused_readings)
                if readings[index]["key"] == expected_key
            ),
            None,
        )
        if match is not None:
            matched[role] = match
            unused_readings.remove(match)

    # A name match is authoritative.  Positional fallback is used only for the
    # still-unmatched legs so legacy/unnamed SRBMiner responses remain useful.
    for role, _leg in configured_legs:
        if role in matched:
            continue
        match = next(
            (index for index in sorted(unused_readings) if readings[index]["name"] is None),
            None,
        )
        if match is not None:
            matched[role] = match
            unused_readings.remove(match)

    for role, leg in configured_legs:
        index = matched.get(role)
        if index is None:
            continue
        reading = readings[index]
        details.append(
            _hashrate_detail(
                role,
                canonical_legs[role],
                algorithm=reading["name"],
                hashrate=reading["hashrate"],
                accepted=reading["accepted"],
                rejected=reading["rejected"],
                gpu_hashrates=reading["gpu_hashrates"],
            )
        )

    if not configured_legs:
        for index, reading in enumerate(readings):
            role = ("primary", "secondary")[index] if index < 2 else f"algorithm_{index + 1}"
            details.append(
                _hashrate_detail(
                    role,
                    None,
                    algorithm=reading["name"],
                    hashrate=reading["hashrate"],
                    accepted=reading["accepted"],
                    rejected=reading["rejected"],
                    gpu_hashrates=reading["gpu_hashrates"],
                )
            )
        unused_readings.clear()

    # Keep any extra official algorithm entry visible without assigning it to
    # the wrong configured coin or pool.
    for index in sorted(unused_readings):
        reading = readings[index]
        details.append(
            _hashrate_detail(
                f"algorithm_{index + 1}",
                None,
                algorithm=reading["name"],
                hashrate=reading["hashrate"],
                accepted=reading["accepted"],
                rejected=reading["rejected"],
                gpu_hashrates=reading["gpu_hashrates"],
            )
        )

    hashrate_value: float | int | dict[str, float | int] | None = None
    role_hashrates = {
        str(detail["role"]): detail["hashrate"]
        for detail in details
        if detail["role"] in {"primary", "secondary"}
        and detail["hashrate"] is not None
    }
    is_dual = any(role == "secondary" for role, _leg in configured_legs) or len(
        role_hashrates
    ) > 1
    if is_dual and role_hashrates:
        # Hashrates from different algorithms are not mathematically additive.
        hashrate_value = role_hashrates
    elif len(role_hashrates) == 1:
        hashrate_value = next(iter(role_hashrates.values()))
    elif len(readings) == 1 and not configured_legs:
        hashrate_value = readings[0]["hashrate"]
    shares_value: dict[str, int] | None = None
    if accepted_values and len(accepted_values) == len(rejected_values):
        shares_value = {
            "accepted": sum(accepted_values),
            "rejected": sum(rejected_values),
        }
    if hashrate_value is None and shares_value is None and not details:
        return None
    return {
        "hashrate": hashrate_value,
        "hashrate_unit": "H/s" if hashrate_value is not None else None,
        "hashrate_details": details or None,
        "shares": shares_value,
    }


def _loopback_http_get(url: str, **kwargs: Any) -> requests.Response:
    """Perform one request without consulting environment proxy settings."""

    session = requests.Session()
    session.trust_env = False
    try:
        return session.get(url, **kwargs)
    finally:
        session.close()


def _bounded_response_json(response: Any) -> Any:
    if getattr(response, "status_code", None) != 200:
        raise ValueError("unexpected local API response status")
    headers = getattr(response, "headers", {})
    if isinstance(headers, Mapping):
        content_length = headers.get("Content-Length") or headers.get(
            "content-length"
        )
        if isinstance(content_length, str) and content_length.isdigit():
            if int(content_length) > _MAX_MINER_API_RESPONSE_BYTES:
                raise ValueError("local API response is too large")
    reader = getattr(response, "iter_content", None)
    if callable(reader):
        payload = bytearray()
        for chunk in reader(chunk_size=16384):
            if len(payload) + len(chunk) > _MAX_MINER_API_RESPONSE_BYTES:
                raise ValueError("local API response is too large")
            payload.extend(chunk)
        return json.loads(payload.decode("utf-8"))
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        if len(content) > _MAX_MINER_API_RESPONSE_BYTES:
            raise ValueError("local API response is too large")
        return json.loads(bytes(content).decode("utf-8"))

    # This branch makes pure mock responses convenient while preserving the
    # same bound before any mock payload reaches the parser.
    loader = getattr(response, "json", None)
    if not callable(loader):
        raise ValueError("local API response has no JSON body")
    value = loader()
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_MINER_API_RESPONSE_BYTES:
        raise ValueError("local API response is too large")
    return value


def _srbminer_gpu_index_map(
    api_payload: Any,
    persisted_mapping: Any,
    gpu_metrics: list[dict[str, Any]] | None,
) -> dict[int, int] | None:
    """Translate SRBMiner mixed-device IDs to current NVIDIA/NVML indices.

    ``None`` means no NVML inventory was supplied (legacy/test callers), so
    the parser retains its old direct-index behaviour.  A dictionary means
    identity-aware mapping is active; missing or conflicting identities are
    deliberately omitted instead of guessing from list order.
    """

    if gpu_metrics is None:
        return None

    nvml_by_pci: dict[str, int] = {}
    ambiguous_pci: set[str] = set()
    nvml_by_bus: dict[int, list[int]] = {}
    for gpu in gpu_metrics[:64]:
        if not isinstance(gpu, dict):
            continue
        index = gpu.get("index")
        if isinstance(index, str) and index.isdigit():
            index = int(index)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 64:
            continue
        pci_key = _nvidia_bus_key(
            gpu.get("pci_bus_id") if isinstance(gpu.get("pci_bus_id"), str) else None
        )
        if pci_key is None:
            continue
        # Keep every raw bus candidate: dropping ambiguous full PCI rows must
        # not make a less-specific bus appear unique across physical domains.
        bus_number = int(pci_key.split(":", 2)[1], 16)
        nvml_by_bus.setdefault(bus_number, []).append(index)
        if pci_key in ambiguous_pci:
            continue
        if pci_key in nvml_by_pci:
            # A PCI address should identify exactly one physical function.  If
            # malformed telemetry repeats it, remove the earlier candidate as
            # well instead of silently attaching a miner rate to the first row.
            nvml_by_pci.pop(pci_key, None)
            ambiguous_pci.add(pci_key)
            continue
        nvml_by_pci[pci_key] = index

    mapped: dict[int, int | None] = {}

    def bind(miner_id: Any, nvml_index: int | None) -> None:
        if (
            isinstance(miner_id, bool)
            or not isinstance(miner_id, int)
            or not 0 <= miner_id < 64
            or nvml_index is None
        ):
            return
        previous = mapped.get(miner_id)
        if miner_id in mapped and previous != nvml_index:
            # Persisted discovery and the live API must never disagree about a
            # card.  Mark the identity unusable for this heartbeat.
            mapped[miner_id] = None
        elif miner_id not in mapped:
            mapped[miner_id] = nvml_index

    if isinstance(persisted_mapping, list) and len(persisted_mapping) <= 64:
        for item in persisted_mapping:
            if not isinstance(item, dict):
                continue
            pci_value = item.get("pci_bus_id")
            pci_key = _nvidia_bus_key(pci_value if isinstance(pci_value, str) else None)
            bind(item.get("miner_id"), nvml_by_pci.get(pci_key or ""))

    devices = api_payload.get("gpu_devices") if isinstance(api_payload, dict) else None
    if isinstance(devices, list) and len(devices) <= 64:
        for device in devices:
            if not isinstance(device, dict):
                continue
            miner_id = device.get("id")
            if isinstance(miner_id, str) and miner_id.isdigit():
                miner_id = int(miner_id)
            pci_key = None
            for field in ("topology_id", "pci_bus_id"):
                candidate = device.get(field)
                if isinstance(candidate, str):
                    pci_key = _nvidia_bus_key(candidate)
                    if pci_key is not None:
                        break
            nvml_index = nvml_by_pci.get(pci_key or "")
            if nvml_index is None and pci_key is None:
                raw_bus = device.get("bus_id")
                if isinstance(raw_bus, str):
                    try:
                        raw_bus = int(raw_bus, 0)
                    except ValueError:
                        raw_bus = None
                if isinstance(raw_bus, int) and not isinstance(raw_bus, bool):
                    candidates = nvml_by_bus.get(raw_bus, [])
                    if len(candidates) == 1:
                        nvml_index = candidates[0]
            bind(miner_id, nvml_index)

    return {
        miner_id: nvml_index
        for miner_id, nvml_index in mapped.items()
        if nvml_index is not None
    }


def _pearl_rate_hps(line: str) -> float | None:
    match = _PEARL_RATE.search(line) or _PEARL_INLINE_RATE.search(line)
    if match is None:
        return None
    try:
        value = float(match.group("value"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    unit = match.group("unit").upper().removesuffix("H")
    multiplier = {
        "": 1.0,
        "K": 1e3,
        "M": 1e6,
        "G": 1e9,
        "T": 1e12,
        "P": 1e15,
        "E": 1e18,
    }.get(unit)
    if multiplier is None:
        return None
    result = value * multiplier
    return result if math.isfinite(result) else None


def _bounded_pearl_log(path: Path, *, now: float | None = None) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ValueError("Pearl Miner log is unavailable") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        bool(getattr(before, "st_file_attributes", 0) & reparse_flag)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise ValueError("Pearl Miner log is unsafe")
    current_time = time.time() if now is None else now
    if current_time - float(before.st_mtime) > _PEARL_LOG_FRESH_SECONDS:
        raise ValueError("Pearl Miner log has no recent progress")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise ValueError("Pearl Miner log changed while opening")
            stream.seek(max(0, opened.st_size - _MAX_PEARL_LOG_BYTES))
            payload = stream.read(_MAX_PEARL_LOG_BYTES + 1)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("Pearl Miner log is unavailable") from exc
    if len(payload) > _MAX_PEARL_LOG_BYTES:
        payload = payload[-_MAX_PEARL_LOG_BYTES:]
    return payload.decode("utf-8", errors="replace")


def _latest_pearl_tui_counters(
    lines: list[str],
) -> tuple[dict[str, int], dict[str, int]] | None:
    """Return counters from only the newest complete Pearl TUI snapshot."""

    for raw_line in reversed(lines):
        line = _ANSI_CONTROL_SEQUENCE.sub("", raw_line)
        match = _PEARL_TUI_TOTAL.search(line)
        if match is not None:
            values = {key: int(match.group(key)) for key in match.groupdict()}
            if any(value > _MAX_SAFE_TELEMETRY_COUNTER for value in values.values()):
                return None
            return (
                {
                    "accepted": values["accepted"],
                    "rejected": values["rejected"],
                    "stale": values["stale"],
                },
                {
                    "s": values["proof_s"],
                    "r": values["proof_r"],
                    "d": values["proof_d"],
                },
            )
        # A newer table header without its TOTAL row means that snapshot is
        # still being written.  Do not reuse counters from an older table.
        if _PEARL_TUI_HEADER.search(line):
            return None
    return None


def _parse_pearlminer_log(
    text: str,
    mining_legs: tuple[dict[str, Any] | None, dict[str, Any] | None] = (None, None),
) -> dict[str, Any] | None:
    """Parse official progress events and the latest complete TUI counters."""

    if not isinstance(text, str) or len(text.encode("utf-8", errors="replace")) > _MAX_PEARL_LOG_BYTES:
        return None
    lines = text.splitlines()[-4096:]
    counters = _latest_pearl_tui_counters(lines)
    shares, proofs = counters if counters is not None else (None, None)
    total: float | None = None
    gpu_rates: dict[int, float] = {}
    aggregate_gpu_rates: dict[int, float] = {}
    for raw_line in reversed(lines):
        line = _ANSI_CONTROL_SEQUENCE.sub("", raw_line)
        device_match = _PEARL_DEVICE_PROGRESS.search(line)
        if device_match is not None:
            index = int(device_match.group("index"))
            if index <= 31 and index not in gpu_rates:
                rate = _pearl_rate_hps(line)
                if rate is not None:
                    gpu_rates[index] = rate
            continue
        if total is None and _PEARL_TOTAL_PROGRESS.search(line):
            total = _pearl_rate_hps(line)
            device_list = _PEARL_DEVICE_LIST.search(line)
            if device_list is not None:
                for index, item in enumerate(device_list.group("devices").split(",")):
                    if index > 31:
                        break
                    rate = _pearl_rate_hps(item)
                    if rate is not None:
                        aggregate_gpu_rates[index] = rate
    if total is None and not gpu_rates and shares is None:
        return None
    for index, rate in aggregate_gpu_rates.items():
        gpu_rates.setdefault(index, rate)
    if total is None and gpu_rates:
        total = _finite_number(sum(gpu_rates.values()))
    gpu_hashrates = [
        {"index": index, "hashrate": rate}
        for index, rate in sorted(gpu_rates.items())
    ]
    detail = _hashrate_detail(
        "primary",
        mining_legs[0],
        algorithm="pearlhash",
        hashrate=total,
        accepted=(shares or {}).get("accepted"),
        rejected=(shares or {}).get("rejected"),
        gpu_hashrates=gpu_hashrates,
    )
    if shares is not None:
        detail["shares"] = dict(shares)
    if proofs is not None:
        detail["proofs"] = dict(proofs)
    return {
        "hashrate": total,
        "hashrate_unit": "H/s" if total is not None else None,
        "hashrate_details": [detail],
        "shares": shares,
        "proofs": proofs,
        "gpu_hashrate_status": "reported" if gpu_hashrates else "not_reported",
    }


def _seine_api_headers(record: Mapping[str, Any]) -> dict[str, str]:
    """Read the per-run Seine bearer token without persisting it elsewhere."""

    # Seine 0.2.15 had an unauthenticated loopback API. Its older process
    # records intentionally omit this field and keep the legacy request shape.
    data_dir = record.get("seine_data_dir")
    if data_dir is None:
        return {}
    if not isinstance(data_dir, str) or not data_dir:
        raise ValueError("invalid Seine API cookie path")
    cookie = Path(data_dir) / "control-api.cookie"
    info = cookie.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, "st_file_attributes", 0) & reparse)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size != 64
    ):
        raise ValueError("invalid Seine API cookie")
    token = cookie.read_text(encoding="ascii")
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("invalid Seine API token")
    return {"Authorization": f"Bearer {token}"}


def _managed_miner_api_stats(
    status: dict[str, Any],
    requester: Callable[..., Any],
    mining_legs: tuple[dict[str, Any] | None, dict[str, Any] | None] = (None, None),
    gpu_metrics: list[dict[str, Any]] | None = None,
    runtime_dir: Path | None = None,
    process_record: dict[str, Any] | None = None,
    gpu_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Query only fixed, verified Agent-managed telemetry backends."""

    miner_type = status.get("miner_type")
    if retired_miner_type(miner_type) is not None:
        return None, "managed miner adapter is retired"
    api_port = status.get("api_port")
    adapter = MINER_ADAPTERS.get(miner_type)
    if adapter is None:
        return None, "managed miner has invalid local API metadata"
    telemetry_id = adapter.telemetry_id
    record = process_record or {}
    seine_identity = (record.get("pid"), record.get("create_time"), record.get("argv_sha256"))
    if telemetry_id == "builtin.pearlminer.log.v1":
        if status.get("version") != 1 or runtime_dir is None:
            return None, "managed Pearl Miner has invalid local status metadata"
        try:
            parsed = _parse_pearlminer_log(
                _bounded_pearl_log(runtime_dir / "pearlminer.log"),
                mining_legs,
            )
        except ValueError:
            parsed = None
        if parsed is None:
            return None, "local Pearl Miner log has no recent progress"
        return parsed, None
    if (
        status.get("version") != 1
        or telemetry_id not in _MANAGED_TELEMETRY_API_PATHS
        or status.get("api_host") != "127.0.0.1"
        or isinstance(api_port, bool)
        or not isinstance(api_port, int)
        or not 1024 <= api_port <= 65535
    ):
        if telemetry_id == "builtin.seine.http.v1":
            _SEINE_TELEMETRY_OBSERVER.observe(None, seine_identity, now=time.monotonic())
        return None, "managed miner has invalid local API metadata"

    path = _MANAGED_TELEMETRY_API_PATHS[telemetry_id]
    url = f"http://127.0.0.1:{api_port}{path}"
    response: Any | None = None
    try:
        headers = {"Accept": "application/json"}
        if telemetry_id == "builtin.seine.http.v1":
            headers.update(_seine_api_headers(record))
        response = requester(
            url,
            headers=headers,
            timeout=_LOCAL_MINER_API_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        payload = _bounded_response_json(response)
        if telemetry_id == "builtin.xmrig.http.v1":
            parsed = _parse_xmrig_summary(payload)
            if parsed is not None:
                parsed["gpu_hashrate_status"] = "not_applicable"
                parsed["hashrate_details"] = [
                    _hashrate_detail(
                        "primary",
                        mining_legs[0],
                        algorithm=None,
                        hashrate=parsed.get("hashrate"),
                        accepted=(parsed.get("shares") or {}).get("accepted"),
                        rejected=(parsed.get("shares") or {}).get("rejected"),
                    )
                ]
        elif telemetry_id == "builtin.srbminer.http.v1":
            gpu_index_map = _srbminer_gpu_index_map(
                payload, status.get("gpu_mapping"), gpu_metrics
            )
            parsed = _parse_srbminer_summary(
                payload, mining_legs, gpu_index_map
            )
            if parsed is not None:
                has_gpu_rates = any(
                    isinstance(detail, dict)
                    and isinstance(detail.get("gpu_hashrates"), list)
                    and bool(detail["gpu_hashrates"])
                    for detail in (parsed.get("hashrate_details") or [])
                )
                raw_has_gpu_rates = any(
                    isinstance(algorithm, dict)
                    and isinstance(algorithm.get("hashrate"), dict)
                    and isinstance(algorithm["hashrate"].get("gpu"), dict)
                    and any(
                        isinstance(key, str)
                        and re.fullmatch(r"gpu[_-]?\d{1,2}", key, flags=re.IGNORECASE)
                        for key in algorithm["hashrate"]["gpu"]
                    )
                    for algorithm in (
                        payload.get("algorithms", [])
                        if isinstance(payload, dict)
                        and isinstance(payload.get("algorithms"), list)
                        else []
                    )
                )
                if has_gpu_rates:
                    parsed["gpu_hashrate_status"] = "reported"
                elif gpu_index_map is not None and raw_has_gpu_rates:
                    parsed["gpu_hashrate_status"] = "identity_unmatched"
                else:
                    parsed["gpu_hashrate_status"] = "not_reported"
        elif telemetry_id == "builtin.seine.http.v1":
            decoded = parse_seine_runtime_state(payload)
            # An invalid response or stopped run breaks observation history.
            # A retained STATS row cannot revive the preceding positive window.
            window = _SEINE_TELEMETRY_OBSERVER.observe(
                decoded, seine_identity, now=time.monotonic(), wall_time=time.time(),
            )
            parsed = None
            if decoded is not None and decoded.get("running") is True:
                rate = window.get("hashrate_hs") if window is not None else None
                sampled = window.get("sampled_at_unix") if window is not None else None
                sampled_at = (datetime.fromtimestamp(sampled, timezone.utc).isoformat()
                              if isinstance(sampled, (int, float)) and math.isfinite(sampled)
                              else None)
                detail = _hashrate_detail(
                    "primary", mining_legs[0], algorithm="blocknet-argon2id",
                    hashrate=rate, accepted=None, rejected=None,
                )
                detail["stats_updated_at"] = sampled_at
                detail["session_id"] = hashlib.sha256(json.dumps([
                    record.get("pid"), record.get("create_time"),
                    record.get("argv_sha256"), decoded.get("run_id"),
                ], separators=(",", ":")).encode()).hexdigest()
                if window is not None:
                    detail.update(
                        workload_source="seine_stats_window",
                        workload_sample_id=hashlib.sha256(json.dumps([
                            decoded.get("run_id"), window.get("fingerprint"),
                        ], separators=(",", ":")).encode()).hexdigest(),
                        workload_delta_work=window.get("delta_hashes", 0),
                    )
                parsed = {"hashrate": rate, "hashrate_unit": "H/s", "shares": None,
                          "gpu_hashrate_status": "not_applicable",
                          "stats_updated_at": sampled_at,
                          "hashrate_details": [detail]}
        else:  # pragma: no cover - the API path allowlist rejects this above.
            parsed = None
        if parsed is None:
            return None, f"local {miner_type} API response is invalid"
        return parsed, None
    except Exception:
        if telemetry_id == "builtin.seine.http.v1":
            _SEINE_TELEMETRY_OBSERVER.observe(None, seine_identity, now=time.monotonic())
        # API response bodies and exception strings can contain pool or wallet
        # data.  Heartbeats therefore expose only this fixed, bounded error.
        return None, f"local {miner_type} API is unavailable"
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _detail_gpu_total(detail: dict[str, Any]) -> float | None:
    """Sum the payload's own per-GPU rows, the payload's unit only."""
    rows = detail.get("gpu_hashrates")
    if not isinstance(rows, list):
        return None
    total = 0.0
    counted = 0
    for row in rows[:64]:
        if not isinstance(row, dict) or row.get("hashrate_unit") not in (None, "H/s"):
            continue
        rate = _non_negative_number(row.get("hashrate"))
        if rate is None:
            continue
        total += float(rate)
        counted += 1
    return total if counted else None


def _apply_hashrate_amplitude_gate(
    api_stats: dict[str, Any],
    session_id: Any,
    *,
    now: float | None = None,
) -> None:
    """Drop implausible miner aggregates before they enter mining_status.

    Every managed telemetry producer's parsed result passes through here.
    A rejected detail keeps its shares, proofs and per-GPU rows for display
    but contributes no hashrate sample: a missing reading beats a fake one.
    The controller sees ``hashrate: null`` and records no sample for that
    heartbeat instead of a multi-PH/s spike, and the Agent's own workload
    integration -- which reads the same details -- stays unpolluted.
    """
    details = api_stats.get("hashrate_details")
    if not isinstance(details, list) or not details:
        return
    moment = time.time() if now is None else now
    rejected_roles: set[str] = set()
    reasons: set[str] = set()
    for detail in details:
        if not isinstance(detail, dict):
            continue
        rate = _non_negative_number(detail.get("hashrate"))
        if rate is None:
            continue
        key = (session_id, detail.get("coin"), detail.get("algorithm"))
        reason = _HASHRATE_AMPLITUDE_GATE.observe(
            key, float(rate), gpu_total=_detail_gpu_total(detail), now=moment,
        )
        if reason is None:
            continue
        rejected_roles.add(str(detail.get("role")))
        reasons.add(reason)
        detail["hashrate"] = None
        detail["hashrate_unit"] = None
    if not rejected_roles:
        return
    hashrate = api_stats.get("hashrate")
    if isinstance(hashrate, dict):
        survived = {
            role: (None if role in rejected_roles else value)
            for role, value in hashrate.items()
        }
        api_stats["hashrate"] = survived if any(
            value is not None for value in survived.values()
        ) else None
    else:
        detail_roles = {
            str(detail.get("role"))
            for detail in details
            if isinstance(detail, dict)
        }
        if "primary" in rejected_roles or not (detail_roles - rejected_roles):
            api_stats["hashrate"] = None
    if api_stats.get("hashrate") is None:
        api_stats["hashrate_unit"] = None
    LOGGER.warning(
        "dropped implausible miner hashrate aggregate",
        extra={
            "event": "hashrate_amplitude_rejected",
            "roles": sorted(rejected_roles)[:8],
            "reasons": sorted(reasons)[:4],
            "miner_session": bool(session_id),
        },
    )


def _retired_mining_status(result: dict[str, Any], miner_type: str) -> dict[str, Any]:
    """Keep historical configuration visible and remove execution telemetry."""
    for field in (
        "hashrate", "hashrate_unit", "hashrate_details", "shares", "proofs",
        "stats_updated_at", "stats_sampled_at", "session_id", "gpu_hashrate_status",
    ):
        if field in result:
            result[field] = None
    result.update(
        retired=True, miner_type=miner_type, state="error", process_verified=False,
        process_error="managed miner adapter is retired",
        error="managed miner adapter is retired",
    )
    return result


def collect_mining_status(
    config: "AgentConfig",
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    system_name: str | None = None,
    requester: Callable[..., Any] = _loopback_http_get,
    process_factory: Callable[[int], Any] = psutil.Process,
    boot_time_factory: Callable[[], float] = psutil.boot_time,
    process_iter_factory: Callable[[], Any] = psutil.process_iter,
    gpu_metrics: list[dict[str, Any]] | None = None,
    gpu_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect a bounded, non-secret summary for heartbeat inventory."""

    checked_at = datetime.now(timezone.utc).isoformat()
    service_state = _service_state(
        config.miner_service, runner, system_name or platform.system()
    )
    result: dict[str, Any] = {
        "configured": False,
        "state": service_state,
        "service_name": config.miner_service,
        "service_state": service_state,
        "flight_sheet_id": None,
        "flight_sheet_name": None,
        "worker_name": None,
        "applied_at": None,
        "primary": None,
        "secondary": None,
        "hashrate": None,
        "hashrate_unit": None,
        "hashrate_details": None,
        "shares": None,
        "proofs": None,
        "stats_updated_at": None,
        "miner_type": None,
        "gpu_hashrate_status": None,
        "checked_at": checked_at,
        "error": None,
        "process_verified": None,
        "process_error": None,
        "session_id": None,
        # Declarative SRBMiner settings and the last successfully persisted
        # standalone NVML profile are kept separate.  An empty list means the
        # Agent has no RigPilot-owned OC settings to display; it does not guess
        # from a driver's current clocks or power limit.
        "gpu_oc_profiles": [],
    }
    errors: list[str] = []
    independent_mode = False
    retired_flight_type: str | None = None
    managed_state: str | None = None
    managed_stats: dict[str, Any] | None = None
    status_file_present = False
    if config.flight_sheet_file is not None:
        try:
            sheet = _bounded_json_object(config.flight_sheet_file, "flight sheet")
        except ValueError as exc:
            if not str(exc).endswith(" does not exist"):
                errors.append(str(exc))
        else:
            independent_mode = (
                sheet.get("version") == 4
                and sheet.get("secondary_mode") == "independent"
                and isinstance(sheet.get("secondary"), dict)
            )
            retired_flight_type = next(
                (miner_type for role in ("primary", "secondary")
                 if (miner_type := _retired_leg_type(sheet.get(role))) is not None),
                None,
            )
            primary = _mining_leg_summary(sheet.get("primary"))
            if primary is None:
                errors.append("flight sheet has no valid primary miner")
            else:
                result.update(
                    {
                        "configured": True,
                        "flight_sheet_id": _safe_status_text(
                            sheet.get("flight_sheet_id"), 64
                        ),
                        "flight_sheet_name": _safe_status_text(sheet.get("name"), 80),
                        "worker_name": _safe_status_text(sheet.get("worker_name"), 128),
                        "applied_at": _safe_status_text(sheet.get("applied_at"), 64),
                        "primary": primary,
                        "secondary": _mining_leg_summary(sheet.get("secondary")),
                    }
                )
                raw_gpu_oc = sheet.get("gpu_oc")
                primary = sheet.get("primary")
                if (
                    isinstance(primary, dict)
                    and primary.get("miner_type") == "srbminer"
                    and isinstance(raw_gpu_oc, dict)
                    and raw_gpu_oc.get("vendor") == "nvidia"
                    and set(raw_gpu_oc) == {"vendor", "settings"}
                ):
                    flight_settings = _gpu_oc_settings_summary(
                        raw_gpu_oc.get("settings"), allow_model_match=True
                    )
                    if flight_settings is not None:
                        result["gpu_oc_profiles"].append(
                            {
                                "source": "srbminer_cli",
                                "state": "declared",
                                "settings": flight_settings,
                            }
                        )

    standalone_gpu_oc = _standalone_gpu_oc_profile(config)
    if standalone_gpu_oc is not None:
        result["gpu_oc_profiles"].append(standalone_gpu_oc)

    if retired_flight_type is not None:
        return _retired_mining_status(result, retired_flight_type)

    if independent_mode and config.managed_mining_enabled:
        runtime = config.managed_miner_runtime_dir
        if runtime is None:
            result["state"] = "error"
            result["process_verified"] = False
            result["process_error"] = "managed miner runtime is not configured"
            result["error"] = result["process_error"]
            result["running_roles"] = []
            result["role_errors"] = {
                "primary": result["process_error"],
                "secondary": result["process_error"],
            }
            result["degraded"] = False
            return result
        role_statuses: dict[str, dict[str, Any]] = {}
        role_errors: dict[str, str] = {}
        for role in ("primary", "secondary"):
            try:
                role_runtime = _managed_role_runtime(
                    runtime.resolve(strict=True),
                    role,
                    create=False,
                )
            except (ManagedMinerError, OSError) as exc:
                role_errors[role] = str(exc)
                continue
            role_config = replace(
                config,
                managed_miner_runtime_dir=role_runtime,
                miner_status_file=role_runtime / "miner-status.json",
                flight_sheet_file=role_runtime / "active-flight-sheet.json",
            )
            role_statuses[role] = collect_mining_status(
                role_config,
                runner=runner,
                system_name=system_name,
                requester=requester,
                process_factory=process_factory,
                boot_time_factory=boot_time_factory,
                process_iter_factory=process_iter_factory,
                gpu_metrics=gpu_metrics,
                gpu_snapshot=gpu_snapshot,
            )

        details: list[dict[str, Any]] = []
        hashrates: dict[str, float | int] = {}
        for role, role_status in role_statuses.items():
            role_details = role_status.get("hashrate_details")
            if isinstance(role_details, list):
                for raw_detail in role_details:
                    if isinstance(raw_detail, dict):
                        detail = dict(raw_detail)
                        # Each independent role owns its sampling clock and
                        # process. A fresh GPU poll must not date an old CPU
                        # observation as new work.
                        detail.setdefault("stats_updated_at", role_status.get("stats_updated_at"))
                        detail.setdefault("session_id", role_status.get("session_id"))
                        if detail.get("role") == "primary":
                            detail["role"] = role
                        elif str(detail.get("role", "")).startswith("algorithm_"):
                            # A named API mismatch remains unbound even when
                            # its process is assigned an independent role.
                            detail["role"] = f"algorithm_{len(details) + 1}"
                        details.append(detail)
            rate = _non_negative_number(role_status.get("hashrate"))
            if rate is not None:
                hashrates[role] = rate

        states = [str(item.get("state")) for item in role_statuses.values()]
        expected_flight_sheet_id = result.get("flight_sheet_id")
        # Delayed secondary-leg start: while the deferred launch is pending,
        # the missing second role is a transitional window, not a fault.  Drop
        # it from this observation so the aggregate reports "starting".
        secondary_start_pending = False
        try:
            pending_marker = load_pending_secondary_marker(
                runtime.resolve(strict=True)
            )
        except (ManagedMinerError, OSError):
            pending_marker = None
        primary_status = role_statuses.get("primary")
        if (
            pending_marker is not None
            and isinstance(expected_flight_sheet_id, str)
            and pending_marker["flight_sheet_id"] == expected_flight_sheet_id
            and isinstance(primary_status, dict)
            and primary_status.get("state") == "running"
            and primary_status.get("process_verified") is True
            and primary_status.get("flight_sheet_id") == expected_flight_sheet_id
            and role_statuses.get("secondary", {}).get("state") != "running"
        ):
            secondary_start_pending = True
            role_statuses.pop("secondary", None)
            role_errors.pop("secondary", None)
            states = [str(item.get("state")) for item in role_statuses.values()]
        identity_error_roles: set[str] = set()
        for role, item in role_statuses.items():
            if item.get("flight_sheet_id") != expected_flight_sheet_id:
                role_errors[role] = (
                    "managed miner flight sheet does not match active flight sheet"
                )
                identity_error_roles.add(role)
                continue
            process_verified = item.get("process_verified")
            if process_verified is False:
                identity_error_roles.add(role)
            message = (
                item.get("process_error")
                if process_verified is False
                else item.get("error")
            )
            if isinstance(message, str) and message:
                role_errors[role] = message
        verified = len(role_statuses) == 2 and not identity_error_roles and all(
            item.get("process_verified") is True
            for item in role_statuses.values()
        )
        running_roles = [
            role
            for role, item in role_statuses.items()
            if item.get("state") == "running"
            and item.get("process_verified") is True
            and role not in identity_error_roles
        ]
        degraded = len(running_roles) == 1 or bool(role_errors)
        if states == ["running", "running"] and verified:
            aggregate_state = "running"
        elif (
            len(states) == 2
            and not role_errors
            and all(state in {"stopped", "not_configured"} for state in states)
        ):
            aggregate_state = "stopped"
        elif running_roles:
            aggregate_state = "stale"
        elif role_errors or any(state == "error" for state in states):
            aggregate_state = "error"
        else:
            aggregate_state = "stale"
        if secondary_start_pending and aggregate_state != "running":
            # One verified primary plus a durably pending delayed secondary
            # is a startup transition, not a degraded or faulted session.
            # The pending window popped the not-yet-started secondary role,
            # so no residual role error may describe it as missing.
            aggregate_state = "starting"
            role_errors.clear()
        role_error_messages = [
            f"{role}: {message}" for role, message in role_errors.items()
        ]
        session_parts = [
            str(role_statuses.get(role, {}).get("session_id") or "")
            for role in ("primary", "secondary")
        ]
        role_gpu_statuses = {
            str(item.get("gpu_hashrate_status"))
            for item in role_statuses.values()
            if isinstance(item.get("gpu_hashrate_status"), str)
        }
        aggregate_gpu_status = (
            "identity_unmatched"
            if "identity_unmatched" in role_gpu_statuses
            else "api_unavailable"
            if "api_unavailable" in role_gpu_statuses
            else "reported"
            if "reported" in role_gpu_statuses
            else "not_applicable"
            if role_gpu_statuses == {"not_applicable"}
            else "not_reported"
        )
        result.update(
            {
                "state": aggregate_state,
                "miner_type": (
                    result.get("primary", {}).get("miner_type")
                    if isinstance(result.get("primary"), dict)
                    else None
                ),
                "hashrate": hashrates or None,
                "hashrate_unit": "H/s" if hashrates else None,
                "hashrate_details": details or None,
                # Independent legs may use different algorithms and share
                # difficulties.  Their counters remain on the role-tagged
                # hashrate details and are never added into a false total.
                "shares": None,
                "proofs": None,
                "stats_updated_at": (
                    checked_at if details or hashrates else None
                ),
                "process_verified": (
                    None if secondary_start_pending else verified
                ),
                "process_error": (
                    "; ".join(role_error_messages) if role_error_messages else None
                ),
                "running_roles": running_roles,
                "role_errors": role_errors,
                "degraded": degraded,
                "session_id": (
                    hashlib.sha256(
                        "|".join(session_parts).encode("utf-8")
                    ).hexdigest()[:32]
                    if aggregate_state == "running" and all(session_parts)
                    else None
                ),
                "gpu_hashrate_status": aggregate_gpu_status,
                "error": "; ".join(errors + role_error_messages)
                if errors or role_error_messages
                else None,
            }
        )
        return result

    miner_status_file = config.miner_status_file
    if (
        miner_status_file is None
        and config.managed_mining_enabled
        and config.managed_miner_runtime_dir is not None
    ):
        miner_status_file = config.managed_miner_runtime_dir / "miner-status.json"
    if miner_status_file is not None:
        try:
            stats = _bounded_json_object(miner_status_file, "miner status file")
        except ValueError as exc:
            if not str(exc).endswith(" does not exist"):
                errors.append(str(exc))
        else:
            retired_type = retired_miner_type(stats.get("miner_type"))
            if retired_type is not None:
                return _retired_mining_status(result, retired_type)
            status_file_present = True
            managed_stats = stats
            result.update(_miner_stats(stats))
            status_miner_type = _safe_status_text(stats.get("miner_type"), 16)
            if status_miner_type in _MANAGED_MINER_TYPES:
                result["miner_type"] = status_miner_type
            candidate_state = _safe_status_text(stats.get("state"), 16)
            if candidate_state in _MINING_STATES:
                managed_state = candidate_state
            elif config.managed_mining_enabled:
                errors.append("managed miner status has an invalid state")

    if config.managed_mining_enabled:
        process_state, process_verified, process_error, process_record = (
            _managed_process_truth(
                config,
                process_factory,
                system_name,
                boot_time_factory,
                process_iter_factory,
            )
        )
        retired_type = retired_miner_type(
            process_record.get("miner_type") if isinstance(process_record, dict) else None
        )
        if retired_type is not None:
            return _retired_mining_status(result, retired_type)
        result["process_verified"] = process_verified
        result["process_error"] = process_error
        if process_state == "running":
            if (
                process_record is not None
                and result.get("configured") is True
                and process_record.get("flight_sheet_id")
                != result.get("flight_sheet_id")
            ):
                mismatch = (
                    "managed miner process does not match the active flight sheet"
                )
                result["state"] = "stale"
                result["process_verified"] = False
                result["process_error"] = mismatch
                errors.append(mismatch)
            else:
                result["state"] = "running"
                result["session_id"] = _managed_miner_session_id(process_record)
                status_miner_type = (
                    _safe_status_text(managed_stats.get("miner_type"), 16)
                    if managed_stats is not None
                    else None
                )
                if (
                    process_record is not None
                    and status_miner_type is not None
                    and status_miner_type != process_record.get("miner_type")
                ):
                    errors.append("managed miner status does not match the live process")
                elif managed_stats is not None:
                    api_stats, api_error = _managed_miner_api_stats(
                        managed_stats,
                        requester,
                        (result.get("primary"), result.get("secondary")),
                        gpu_metrics,
                        config.managed_miner_runtime_dir,
                        process_record,
                        gpu_snapshot,
                    )
                    if api_stats is not None:
                        # One choke point for every managed telemetry
                        # producer: reject miner-emitted rate spikes before
                        # they reach heartbeat samples or workload counters.
                        _apply_hashrate_amplitude_gate(
                            api_stats, result.get("session_id")
                        )
                        result.update(api_stats)
                        if "stats_updated_at" not in api_stats:
                            result["stats_updated_at"] = checked_at
                    elif api_error is not None:
                        errors.append(api_error)
                        api_adapter = MINER_ADAPTERS.get(status_miner_type)
                        if (
                            api_adapter is not None
                            and api_adapter.telemetry_id in _MANAGED_TELEMETRY_API_PATHS
                        ):
                            # The status producer does not cache fresh API rates.
                            # Keep process truth, not old numeric samples, on failure.
                            result.update(
                                hashrate=None, hashrate_unit=None,
                                hashrate_details=[], shares=None, stats_updated_at=None,
                            )
                            if "gpu" in api_adapter.resource_kinds:
                                result["gpu_hashrate_status"] = "api_unavailable"
        elif process_state in {"stale", "error"}:
            result["state"] = process_state
            if process_state == "error" and process_error is not None:
                errors.append(process_error)
        elif process_state == "rebooted" or (process_state == "stopped" and process_verified):
            result["state"] = "stopped"
        elif managed_state in {"running", "starting", "stopping"}:
            result["state"] = "stale"
            result["process_verified"] = False
            result["process_error"] = "managed miner has no live process record"
        elif managed_state == "failed" or (
            status_file_present and managed_state is None
        ):
            result["state"] = "error"
            result["process_verified"] = False
            result["process_error"] = "managed miner status is invalid"
            if not errors:
                errors.append("managed miner status is invalid")
        else:
            result["state"] = (
                "stopped" if result["configured"] or managed_state == "stopped" else "not_configured"
            )
            result["process_verified"] = True
            result["process_error"] = None

        if result["state"] != "running":
            result["session_id"] = None
            for field in (
                "hashrate",
                "hashrate_unit",
                "hashrate_details",
                "shares",
                "proofs",
                "stats_updated_at",
                "stats_sampled_at",
                "gpu_hashrate_status",
            ):
                if field in result:
                    result[field] = None
    elif not config.miner_service:
        result["state"] = "unknown" if result["configured"] else "not_configured"
    if result["state"] not in _MINING_STATES:
        result["state"] = "unknown"
    result["error"] = "; ".join(errors) if errors else None
    return result


def _safe_cpu_model(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > 256
        or any(ord(character) < 32 for character in normalized)
    ):
        return None
    return normalized


def _windows_registry_cpu_model() -> str | None:
    """Return Windows' human-readable processor brand string when available."""

    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, "ProcessorNameString")
    except (OSError, ValueError):
        return None
    return _safe_cpu_model(value)


def cpu_model(
    *,
    system_name: str | None = None,
    cpuinfo_path: Path = Path("/proc/cpuinfo"),
) -> str | None:
    """Return a bounded human-readable processor model when the OS exposes one."""

    system = system_name or platform.system()
    if system == "Windows":
        registry_model = _windows_registry_cpu_model()
        if registry_model is not None:
            return registry_model
    if system == "Linux":
        try:
            with cpuinfo_path.open("r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(1024 * 1024 + 1)
            if len(text) <= 1024 * 1024:
                values: dict[str, str] = {}
                for line in text.splitlines():
                    key, separator, value = line.partition(":")
                    if separator:
                        values.setdefault(key.strip().casefold(), value.strip())
                for key in ("model name", "hardware", "cpu model"):
                    candidate = _safe_cpu_model(values.get(key))
                    if candidate is not None:
                        return candidate
        except OSError:
            pass
    for candidate in (
        platform.processor(),
        os.environ.get("PROCESSOR_IDENTIFIER") if system == "Windows" else None,
    ):
        model = _safe_cpu_model(candidate)
        if model is not None:
            return model
    return None


_CPU_TEMPERATURE_MIN_C = -20.0
_CPU_TEMPERATURE_MAX_C = 150.0
_WINDOWS_TEMPERATURE_OUTPUT_LIMIT = 64 * 1024
_WINDOWS_CPU_TEMPERATURE_CACHE_TTL_SECONDS = 30.0
_WINDOWS_CPU_TEMPERATURE_FAILURE_CACHE_TTL_SECONDS = 300.0
_windows_cpu_temperature_cache: tuple[float, dict[str, Any]] | None = None
_WINDOWS_CPU_TEMPERATURE_HELPER = Path(
    "tools", "cpu-temperature", "RigPilot.CpuTemperature.exe"
)
_WINDOWS_CPU_SENSOR_PROVIDERS = (
    ("LibreHardwareMonitor", r"root\LibreHardwareMonitor"),
    ("OpenHardwareMonitor", r"root\OpenHardwareMonitor"),
)


def _valid_cpu_temperature(value: Any) -> float | None:
    """Normalize a plausible Celsius reading without accepting booleans/NaN."""

    value = _finite_number(value)
    if value is None:
        return None
    temperature = float(value)
    if (
        not math.isfinite(temperature)
        or temperature < _CPU_TEMPERATURE_MIN_C
        or temperature > _CPU_TEMPERATURE_MAX_C
    ):
        return None
    return round(temperature, 1)


def _temperature_source_component(value: Any, fallback: str) -> str:
    """Return a short display-safe sensor identifier, never arbitrary output."""

    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.split())[:80]
    normalized = re.sub(r"[^A-Za-z0-9_.()+/#: -]", "_", normalized)
    return normalized or fallback


def _cpu_temperature_label_score(label: str, *, allow_core: bool) -> int:
    """Rank explicit whole-CPU sensors above individual core readings."""

    normalized = " ".join(label.casefold().replace("_", " ").split())
    if re.search(r"(?:cpu )?package(?: id)?(?: \d+)?$", normalized):
        return 120
    if "tctl/tdie" in normalized or ("tctl" in normalized and "tdie" in normalized):
        return 115
    # Tdie is the measured die temperature.  Prefer it over AMD's control
    # temperature (Tctl), which can include a model-specific offset.
    if re.search(r"\btdie\b", normalized):
        return 110
    if re.search(r"\btctl\b", normalized):
        return 105
    if normalized in {"cpu", "cpu temp", "cpu temperature", "core max"}:
        return 95
    if allow_core and re.fullmatch(r"(?:cpu )?core(?: #?\d+)?", normalized):
        return 70
    return 0


def _linux_cpu_temperature(
    sensors_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Choose an explicit Linux CPU sensor from psutil's hwmon inventory."""

    try:
        sensors = sensors_factory()
    except Exception:  # psutil/hwmon is an optional platform boundary.
        return {}
    if not isinstance(sensors, dict):
        return {}

    candidates: list[tuple[int, float, str]] = []
    for raw_chip, entries in list(sensors.items())[:128]:
        if not isinstance(raw_chip, str) or not isinstance(entries, (list, tuple)):
            continue
        chip = raw_chip.casefold()
        # These are Linux hwmon drivers/devices that explicitly identify a CPU.
        # In particular, do not treat acpitz or a generic thermal zone as CPU.
        known_cpu_chip = any(
            marker in chip
            for marker in ("coretemp", "k10temp", "zenpower", "cpu_thermal")
        )
        if not known_cpu_chip:
            continue
        for entry in list(entries)[:128]:
            label_value = getattr(entry, "label", "")
            label = label_value if isinstance(label_value, str) else ""
            score = _cpu_temperature_label_score(
                label,
                allow_core="coretemp" in chip,
            )
            if score == 0 and "cpu_thermal" in chip and not label.strip():
                score = 90
            if score == 0:
                continue
            temperature = _valid_cpu_temperature(getattr(entry, "current", None))
            if temperature is None:
                continue
            safe_chip = _temperature_source_component(raw_chip, "cpu")
            safe_label = _temperature_source_component(label, "temperature")
            candidates.append(
                (score, temperature, f"psutil:{safe_chip}:{safe_label}")
            )

    if not candidates:
        return {}
    _score, temperature, source = max(
        candidates, key=lambda candidate: (candidate[0], candidate[1])
    )
    return {"temperature_c": temperature, "temperature_source": source}


def _windows_cpu_temperature_query(namespace: str) -> str:
    """Build a PowerShell command only for a compile-time allowlisted namespace."""

    if namespace not in {item[1] for item in _WINDOWS_CPU_SENSOR_PROVIDERS}:
        raise ValueError("unsupported hardware monitor namespace")
    # Limit the provider-side result count as well as validating the captured
    # byte/character count below.  Only read-only Sensor instances are queried.
    return (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
        f"@(Get-CimInstance -Namespace '{namespace}' -ClassName Sensor "
        "-Filter \"SensorType='Temperature'\" -ErrorAction Stop | "
        "Select-Object -First 64 Identifier,Name,Value) | "
        "ConvertTo-Csv -NoTypeInformation"
    )


def _windows_cpu_temperature(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    helper_path: Path | None = None,
) -> dict[str, Any]:
    """Read an explicitly identified CPU sensor from optional monitor WMI.

    LibreHardwareMonitor/OpenHardwareMonitor expose CPU identifiers such as
    ``/amdcpu/0/temperature/0`` and ``/intelcpu/0/temperature/0``.  Generic
    ACPI thermal zones are deliberately not queried because Windows cannot
    prove that they represent the CPU package.
    """

    helper = helper_path or (
        Path(sys.executable).parent / _WINDOWS_CPU_TEMPERATURE_HELPER
    )
    try:
        helper_info = helper.lstat()
        helper_is_local_file = (
            stat.S_ISREG(helper_info.st_mode)
            and not bool(
                getattr(helper_info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            and 0 < helper_info.st_size <= 128 * 1024 * 1024
        )
    except OSError:
        helper_is_local_file = False
    if helper_is_local_file:
        try:
            completed = runner(
                [str(helper), "--json"],
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
            )
        except Exception:  # bundled helper is optional/best-effort telemetry.
            completed = None
        if completed is not None:
            output = completed.stdout
            if (
                completed.returncode == 0
                and isinstance(output, str)
                and 0 < len(output) <= _WINDOWS_TEMPERATURE_OUTPUT_LIMIT
            ):
                try:
                    value = json.loads(output)
                except (json.JSONDecodeError, UnicodeError):
                    value = None
                if isinstance(value, dict) and set(value) <= {
                    "temperature_c",
                    "sensor",
                    "sensor_name",
                    "source",
                }:
                    temperature = _valid_cpu_temperature(value.get("temperature_c"))
                    # LHM/OHM use 0 °C as an unavailable sentinel when their
                    # low-level Windows sensor could not be opened.  Do not
                    # turn that into a believable dashboard reading.
                    if temperature is not None and temperature > 0:
                        sensor = value.get("sensor_name", value.get("sensor"))
                        safe_sensor = _temperature_source_component(
                            sensor, "CPU Package"
                        )
                        return {
                            "temperature_c": temperature,
                            "temperature_source": (
                                f"windows_helper:{safe_sensor}"
                            ),
                        }

    for provider, namespace in _WINDOWS_CPU_SENSOR_PROVIDERS:
        argv = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _windows_cpu_temperature_query(namespace),
        ]
        try:
            completed = runner(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
            )
        except Exception:  # optional PowerShell/CIM/provider boundary.
            continue
        output = completed.stdout
        if (
            completed.returncode != 0
            or not isinstance(output, str)
            or not output
            or len(output) > _WINDOWS_TEMPERATURE_OUTPUT_LIMIT
        ):
            continue
        try:
            rows = list(csv.DictReader(io.StringIO(output)))
        except (csv.Error, UnicodeError):
            continue
        candidates: list[tuple[int, float, str]] = []
        for row in rows[:64]:
            if not isinstance(row, dict):
                continue
            identifier = row.get("Identifier")
            if not isinstance(identifier, str) or len(identifier) > 256:
                continue
            normalized_identifier = identifier.strip().casefold()
            if re.fullmatch(
                r"/(?:amd|intel)cpu/\d+/temperature/\d+",
                normalized_identifier,
            ) is None:
                continue
            name = row.get("Name")
            label = name if isinstance(name, str) and len(name) <= 256 else ""
            score = _cpu_temperature_label_score(label, allow_core=True)
            if score == 0:
                # The identifier already proves this is a CPU temperature;
                # retain an unknown CPU sub-sensor only below named package/core
                # sensors, rather than substituting a generic ACPI zone.
                score = 50
            try:
                raw_value = float(str(row.get("Value", "")).strip())
            except (TypeError, ValueError):
                continue
            temperature = _valid_cpu_temperature(raw_value)
            if temperature is None or temperature <= 0:
                continue
            safe_name = _temperature_source_component(label, "temperature")
            candidates.append(
                (score, temperature, f"windows_wmi:{provider}:{safe_name}")
            )
        if candidates:
            _score, temperature, source = max(
                candidates, key=lambda candidate: (candidate[0], candidate[1])
            )
            return {"temperature_c": temperature, "temperature_source": source}
    return {}


def cpu_temperature_metrics(
    *,
    system_name: str | None = None,
    sensors_factory: Callable[[], Any] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    windows_helper_path: Path | None = None,
) -> dict[str, Any]:
    """Return best-effort, positively identified CPU temperature telemetry."""

    global _windows_cpu_temperature_cache
    system = system_name or platform.system()
    if system == "Linux":
        if sensors_factory is None:
            sensors_factory = getattr(psutil, "sensors_temperatures", None)
        if not callable(sensors_factory):
            return {}
        return _linux_cpu_temperature(sensors_factory)
    if system == "Windows":
        use_cache = (
            system_name is None
            and sensors_factory is None
            and runner is subprocess.run
            and windows_helper_path is None
        )
        now = time.monotonic()
        if use_cache and _windows_cpu_temperature_cache is not None:
            cached_at, cached_value = _windows_cpu_temperature_cache
            cache_ttl = (
                _WINDOWS_CPU_TEMPERATURE_CACHE_TTL_SECONDS
                if cached_value
                else _WINDOWS_CPU_TEMPERATURE_FAILURE_CACHE_TTL_SECONDS
            )
            if now - cached_at < cache_ttl:
                return dict(cached_value)
        result = _windows_cpu_temperature(runner, windows_helper_path)
        if use_cache:
            _windows_cpu_temperature_cache = (now, dict(result))
        return result
    return {}


def _remote_listener_available(
    port: int,
    *,
    connections_factory: Callable[..., Any] = psutil.net_connections,
) -> bool:
    """Verify that a TCP listener is reachable beyond loopback without connecting."""

    try:
        connections = connections_factory(kind="tcp")
    except (OSError, psutil.Error):
        return False
    for connection in connections:
        if getattr(connection, "status", None) != psutil.CONN_LISTEN:
            continue
        local = getattr(connection, "laddr", None)
        local_port = getattr(local, "port", None)
        local_ip = getattr(local, "ip", None)
        if local_port is None and isinstance(local, (tuple, list)) and len(local) >= 2:
            local_ip, local_port = local[0], local[1]
        if local_port != port or not isinstance(local_ip, str):
            continue
        if local_ip in {"0.0.0.0", "::"}:
            return True
        try:
            if not ipaddress.ip_address(local_ip.split("%", 1)[0]).is_loopback:
                return True
        except ValueError:
            continue
    return False


def remote_access_metadata(
    config: "AgentConfig",
    *,
    system_name: str | None = None,
    connections_factory: Callable[..., Any] = psutil.net_connections,
) -> dict[str, Any] | None:
    """Describe explicitly allowed Linux SSH services without credentials.

    This only advertises an already-listening service.  It never enables sshd,
    opens a firewall, carries a password/key, or creates a remote shell tunnel.
    """

    if (system_name or platform.system()) != "Linux" or not (
        config.advertise_ssh or config.advertise_sftp
    ):
        return None
    available = _remote_listener_available(
        config.ssh_port, connections_factory=connections_factory
    )
    metadata: dict[str, Any] = {"schema_version": 1}
    for protocol, enabled in (
        ("ssh", config.advertise_ssh),
        ("sftp", config.advertise_sftp),
    ):
        item: dict[str, Any] = {
            "available": bool(enabled and available),
            "port": config.ssh_port,
        }
        if config.ssh_username is not None:
            item["username"] = config.ssh_username
        metadata[protocol] = item
    return metadata


def collect_host_data(
    collect_gpu: bool = True,
    nvidia_smi_path: str = "nvidia-smi",
    mining_config: "AgentConfig | None" = None,
) -> dict[str, Any]:
    """Return the heartbeat fields expected by the RigPilot server."""

    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    cpu_frequency = psutil.cpu_freq()
    processor_model = cpu_model()
    processor_temperature = cpu_temperature_metrics()
    host_ip_addresses = ip_addresses()
    network = network_ip_addresses(host_ip_addresses)
    cpu_metrics: dict[str, Any] = {
        "model": processor_model,
        "percent": psutil.cpu_percent(interval=0.1),
        "logical_count": psutil.cpu_count(logical=True),
        "physical_count": psutil.cpu_count(logical=False),
        "frequency_mhz": cpu_frequency.current if cpu_frequency else None,
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
    }
    cpu_metrics.update(processor_temperature)
    metrics: dict[str, Any] = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "boot_time": datetime.fromtimestamp(psutil.boot_time(), timezone.utc).isoformat(),
        "uptime_seconds": max(0, int(datetime.now().timestamp() - psutil.boot_time())),
        "cpu": cpu_metrics,
        "memory": {
            "total_bytes": memory.total,
            "available_bytes": memory.available,
            "used_bytes": memory.used,
            "percent": memory.percent,
            "swap_total_bytes": swap.total,
            "swap_used_bytes": swap.used,
            "swap_percent": swap.percent,
        },
        "disks": _disk_metrics(),
    }
    gpu_snapshot = None
    if collect_gpu:
        metrics["gpus"] = nvidia_gpu_metrics(nvidia_smi_path)
        annotate_nvidia_oc_capabilities(metrics["gpus"])
        if platform.system() == "Linux":
            gpu_snapshot = collect_gpu_snapshot(
                nvidia_metrics=metrics["gpus"],
                captured_at=datetime.fromisoformat(metrics["collected_at"]).timestamp(),
                cpu_temperature_c=processor_temperature.get("temperature_c"),
            )
            # Preserve all existing NVIDIA model/driver/OC fields and indices.
            # The common snapshot adds identity facts; AMD has no NVML index.
            for gpu in metrics["gpus"]:
                pci = canonical_pci_id(gpu.get("pci_bus_id"))
                matches = [row for row in gpu_snapshot["devices"] if row["vendor"] == "nvidia"
                           and ((pci is not None and row.get("pci_bus_id") == pci)
                                or (pci is None and gpu.get("uuid") and row.get("uuid") == gpu["uuid"]))]
                if len(matches) == 1:
                    for field in ("gpu_key", "vendor", "pci_bus_id", "native_index", "native_index_kind",
                                  "display_index", "hive_index", "control_index", "identity_status"):
                        gpu[field] = matches[0][field]
            metrics["gpus"].extend(dict(row) for row in gpu_snapshot["devices"] if row["vendor"] == "amd")

    inventory: dict[str, Any] = {
        "architecture": platform.machine(),
        "processor": processor_model,
        "ip_addresses": host_ip_addresses,
        "network": network,
        "python_version": sys.version.split()[0],
        "network_interfaces": sorted(psutil.net_if_addrs()),
    }
    if mining_config is not None:
        if collect_gpu:
            inventory["mining_status"] = collect_mining_status(
                mining_config,
                gpu_metrics=metrics.get("gpus"),
                gpu_snapshot=gpu_snapshot,
            )
        else:
            inventory["mining_status"] = collect_mining_status(mining_config)
        remote_access = remote_access_metadata(mining_config)
        if remote_access is not None:
            inventory["remote_access"] = remote_access
        from .agent_updates import AGENT_UPDATE_ACTION, agent_update_readiness
        from .managed_miners import (
            MINING_EXTRA_ARGS_CAPABILITY,
            MINING_CONFIG_FINGERPRINT_CAPABILITY,
            MINING_RESULT_BINDING_CAPABILITY,
            MINING_RESIDUE_REPAIR_CAPABILITY,
            MINING_RESIDUE_SELECTIVE_CAPABILITY,
            MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY,
            INDEPENDENT_SRB_GPU_OC_CAPABILITY,
            STOP_MINING_RESETS_GPU_OC_CAPABILITY,
        )
        from .miner_cache_cleanup import CAPABILITY as MINER_CACHE_CLEANUP_CAPABILITY

        # These checks deliberately run on HostDataSampler's single,
        # coalescing daemon worker.  Some of them validate fixed files with
        # lstat/resolve/read and therefore cannot be made reliably cancellable
        # when a physical disk is failing.  Heartbeats consume only the last
        # JSON-safe result below and never repeat those I/O operations.
        update_readiness = agent_update_readiness(mining_config)
        inventory["agent_update_readiness"] = update_readiness
        heartbeat_capabilities: list[str] = []
        if update_readiness.get("available") is True:
            heartbeat_capabilities.append(AGENT_UPDATE_ACTION)
        if mining_config.managed_mining_enabled:
            heartbeat_capabilities.extend(
                (
                    "managed_mining.dual_process.v1",
                    MINING_EXTRA_ARGS_CAPABILITY,
                    MINING_CONFIG_FINGERPRINT_CAPABILITY,
                    MINING_RESULT_BINDING_CAPABILITY,
                    MINING_RESIDUE_REPAIR_CAPABILITY,
                    MINING_RESIDUE_SELECTIVE_CAPABILITY,
                    MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY,
                    INDEPENDENT_SRB_GPU_OC_CAPABILITY,
                    STOP_MINING_RESETS_GPU_OC_CAPABILITY,
                    MINER_CACHE_CLEANUP_CAPABILITY,
                )
            )
            # This capability describes a Windows-only execution backend, not
            # merely support for parsing its wire shape.  Linux Agents must not
            # advertise it even though the controller and command validator
            # still reject a mismatched target defensively.
            if platform.system() == "Windows":
                heartbeat_capabilities.append(WSL_GPU_MINING_CAPABILITY)

        if platform.system() == "Linux":
            from .linux_ssh import LINUX_SSH_MANAGE_CAPABILITY, linux_ssh_status

            linux_ssh = linux_ssh_status()
            inventory["linux_ssh"] = linux_ssh
            if linux_ssh.get("manageable") is True:
                heartbeat_capabilities.append(LINUX_SSH_MANAGE_CAPABILITY)
            # This is the Agent heartbeat supervisor, not the legacy optional
            # miner_service field in mining_status.  The fixed, secret-free
            # diagnostics let the controller distinguish old Agents from a
            # missing or degraded systemd watchdog policy.
            from .linux_supervision import supervision_diagnostics

            supervision = supervision_diagnostics(mining_config)
            inventory["agent_supervision"] = supervision
            inventory[_HEARTBEAT_MAINTENANCE_KEY] = (
                _linux_heartbeat_maintenance_hints(
                    supervision=supervision,
                )
            )

        if isinstance(remote_access, dict):
            for protocol in ("ssh", "sftp"):
                metadata = remote_access.get(protocol)
                if isinstance(metadata, dict) and metadata.get("available") is True:
                    heartbeat_capabilities.append(f"remote_access.{protocol}")
        # Preserve the existing deterministic capability ordering while
        # defending against accidental duplication between readiness sources.
        inventory[_HEARTBEAT_CAPABILITIES_KEY] = list(
            dict.fromkeys(heartbeat_capabilities)
        )

    return {
        "hostname": socket.gethostname(),
        "os_family": platform.system(),
        "platform": platform.platform(),
        "ip_addresses": host_ip_addresses,
        "local_ip_addresses": network["local_ip_addresses"],
        "public_ip_addresses": network["public_ip_addresses"],
        "metrics": metrics,
        "inventory": inventory,
    }
