"""Locally opted-in management for built-in managed miners.

Remote flight-sheet data never supplies an executable path, API port, log path,
or an arbitrary command line.  The Agent locates one exact executable inside a
verified package, builds a small miner-specific configuration, and records the
PID together with both executable identity and process creation time before it
will ever terminate that process again.
"""

from __future__ import annotations

import copy
import ctypes
from contextlib import contextmanager
from ctypes import wintypes
import errno
import hashlib
import json
import logging
import math
import os
import platform
import re
import select
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlparse

import psutil

from server.miner_adapters import MINER_ADAPTERS, MinerAdapter, OptionSpec, is_retired_miner
from server.miner_adapters.srb_catalog import (
    SRB_DUAL_ALGORITHM_PAIRS,
    srb_dual_pair_supported,
    validate_periodic_cpu_threads,
)
from server.miner_adapters.validation import parse_extra_args, parse_pool_endpoint, validate_option_value

from .config import AgentConfig
from .fsutil import is_link_or_reparse as _link_or_reparse
from .fsutil import normalized_path as _normalized_path
from .state import _fsync_directory
from .external_process import external_process_environment
from .miner_runtime_handlers import (
    MinerRuntimePreparation,
    MinerRuntimePrepareContext,
    TrustedMinerRuntimeHandlerRegistry,
)
from .native_miner_runtime import (
    LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY,
    NATIVE_SRB_RUNTIME_DIRECTORY,
    NativeMinerRuntimeError,
    discard_native_srb_bundle,
    mark_native_srb_probe,
    prepare_native_srb_bundle,
)
from .process_identity import (
    ManagedProcessCandidate,
    inspect_managed_process_replacement,
    managed_argv_fingerprint,
    managed_process_candidate_is_current,
    managed_process_argv_matches_record,
    remove_managed_process_record_if_unchanged,
    scan_managed_process_replacement,
)
from .windows_active_user import WindowsActiveUserError
from .windows_active_user import popen as active_user_popen
from .wsl_mining import (
    WslLaunchIdentity,
    WslMiningError,
    ensure_wsl_package_read_access,
    new_wsl_supervisor_token,
    prepare_wsl_gpu_runtime,
    query_wsl_guest,
    stop_wsl_guest,
    wait_wsl_guest_ready,
    wsl_cached_executable_matches,
    wsl_executable_path,
)

LOGGER = logging.getLogger(__name__)

# Prepared native bundles are not persisted until launch. Managers sharing a
# cache (including independent role managers) must protect each other's plans.
@dataclass
class _NativeSrbPlan:
    bundle: Path
    runtime: Path
    role: str
    phase: str = "prepared"
    probe_exposed: bool = False
    probe_closed: bool = True


_NATIVE_SRB_PREPARED: dict[str, dict[object, _NativeSrbPlan]] = {}
_NATIVE_SRB_PREPARED_LOCK = threading.RLock()


class ManagedMinerError(RuntimeError):
    """A managed miner request or local runtime state is unsafe."""


class ManagedMinerTransitionError(ManagedMinerError):
    """A failed switch that records exactly which mutation phases occurred."""

    def __init__(
        self,
        message: str,
        *,
        state_changed: bool = False,
        old_stopped: bool = False,
        target_started: bool = False,
        target_cleanup_unconfirmed: bool = False,
        target_oc_may_have_applied: bool = False,
        rollback_errors: Sequence[str] = (),
    ) -> None:
        self.message = str(message)
        self.state_changed = bool(
            state_changed or old_stopped or target_started
        )
        self.old_stopped = bool(old_stopped)
        self.target_started = bool(target_started)
        self.target_cleanup_unconfirmed = bool(target_cleanup_unconfirmed)
        self.target_oc_may_have_applied = bool(
            target_oc_may_have_applied
        )
        self.rollback_errors = tuple(str(item) for item in rollback_errors)
        suffix = (
            "; rollback incomplete: " + "; ".join(self.rollback_errors)
            if self.rollback_errors
            else ""
        )
        super().__init__(f"{self.message}{suffix}")


MANAGED_MINER_TYPES = MINER_ADAPTERS.miner_types
# Recognition is only for existing process ownership, stopping and cache safety.
# It never grants a retired miner an executable adapter or a launch handler.
# The removed HiveOS session flavor stays recognized in recorded process state
# only so a stale on-disk record can be reported, never launched again.
_RECORDED_MINER_TYPES = MANAGED_MINER_TYPES | frozenset(
    {"lolminer", "forgeminer", "hiveos"}
)
MINING_RESIDUE_SCAN_ACTION = "scan_mining_residue.v1"
MINING_RESIDUE_CLEANUP_ACTION = "cleanup_mining_residue.v1"
MINING_RESIDUE_REPAIR_CAPABILITY = "managed_mining.residue_repair.v1"
MINING_RESIDUE_SELECTIVE_CAPABILITY = (
    "managed_mining.residue_repair.selective.v1"
)
MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY = (
    "managed_mining.residue_repair.explicit_candidate.v1"
)
STOP_MINING_RESETS_GPU_OC_CAPABILITY = "managed_mining.stop_resets_gpu_oc.v1"
INDEPENDENT_SRB_GPU_OC_CAPABILITY = (
    "managed_mining.independent_srb_gpu_oc.v1"
)
MINING_RESULT_BINDING_CAPABILITY = "managed_mining.result_binding.v1"
MINING_EXTRA_ARGS_CAPABILITY = "managed_mining.extra_args.v1"
MINING_CONFIG_FINGERPRINT_CAPABILITY = (
    "managed_mining.config_fingerprint.v1"
)
FLIGHT_RUNTIME_FINGERPRINT_FIELD = "runtime_fingerprint"


def managed_package_id_matches_miner(package_id: object, miner_type: str) -> bool:
    """Return whether an ID belongs to one controller-managed miner namespace."""

    adapter = MINER_ADAPTERS.get(miner_type)
    return adapter.matches_package_id(package_id) if adapter is not None else False


# Compatibility export for callers describing default GPU algorithms. Runtime
# ownership below always uses the task's selected metadata resource instead.
SRB_GPU_ALGORITHMS = frozenset(
    spec.name for spec in MINER_ADAPTERS.require("srbminer").algorithm_specs
    if spec.resource_kind == "gpu"
)
_SRB_GPU_VENDORS = frozenset({"amd", "intel", "nvidia"})


def leg_effective_resource(leg: Mapping[str, Any]) -> str | None:
    """Return this task's selected resource, not all algorithm capabilities."""

    miner_type = leg.get("miner_type")
    if miner_type is None:
        return None
    adapter = MINER_ADAPTERS.get(miner_type)
    if adapter is None:
        raise ManagedMinerError("unsupported managed miner type")
    extra = validate_managed_extra_config(str(miner_type), leg.get("extra_config", {}))
    try:
        return adapter.algorithm_resource(
            leg.get("algorithm"), coin=leg.get("coin"),
            resource_kind=extra.get("resource_kind"),
        )
    except ValueError as exc:
        raise ManagedMinerError(str(exc)) from exc


def validate_managed_algorithm_context(leg: Mapping[str, Any]) -> str:
    """Validate declared algorithm/version/transport without exposing secrets."""

    miner_type = leg.get("miner_type")
    adapter = MINER_ADAPTERS.get(miner_type)
    if adapter is None:
        raise ManagedMinerError("unsupported managed miner type")
    package = leg.get("miner_package")
    version = package.get("version") if isinstance(package, Mapping) else None
    if not isinstance(version, str) or not version:
        raise ManagedMinerError("managed miner package version is unavailable")
    extra = validate_managed_extra_config(str(miner_type), leg.get("extra_config", {}))
    credential = leg.get("pool_password", "")
    if not isinstance(credential, str):
        raise ManagedMinerError("managed miner pool credential is invalid")
    try:
        algorithm = adapter.validate_execution_context(
            leg.get("algorithm"), version, extra, coin=leg.get("coin"),
        )
        adapter.validate_pool_transport(
            str(leg.get("pool_url") or ""), algorithm, version, extra,
            pool_password=credential,
        )
        return algorithm
    except ValueError as exc:
        raise ManagedMinerError(str(exc)) from exc


def _document_srb_gpu_roles(document: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the roles whose SRBMiner leg owns discrete GPUs."""

    roles: list[str] = []
    for role in ("primary", "secondary"):
        leg = document.get(role)
        if not isinstance(leg, Mapping):
            continue
        if (
            leg.get("miner_type") == "srbminer"
            and leg.get("execution_mode", "native") == "native"
            and leg_effective_resource(leg) == "gpu"
        ):
            roles.append(role)
    return tuple(roles)


def _document_uses_srb_gpu_oc(
    document: Mapping[str, Any],
    *,
    local_oc_enabled: bool = False,
    system_name: str | None = None,
) -> bool:
    """Return whether an SRBMiner GPU flight needs an idempotent reset.

    A populated flight ``gpu_oc`` records SRBMiner OC ownership. On Linux and
    Windows, allowing device NVML tuning does not give a no-OC miner ownership:
    its delayed startup reset could erase a later device profile. Explicit CLI
    OC still requires the original reset. Other platforms retain their legacy
    local OC reset policy.
    The role scan also supports an independent flight with SRBMiner secondary.
    """

    raw_oc = document.get("gpu_oc")
    has_recorded_oc = bool(
        isinstance(raw_oc, Mapping)
        and raw_oc.get("vendor") == "nvidia"
        and isinstance(raw_oc.get("settings"), list)
        and raw_oc["settings"]
    )
    policy_reset = (
        local_oc_enabled and (system_name or platform.system()) not in {"Linux", "Windows"}
    )
    if not (has_recorded_oc or policy_reset):
        return False
    return bool(_document_srb_gpu_roles(document))


def _srb_gpu_oc_reset_satisfied(result: Any) -> bool:
    """Accept an acknowledged reset or an explicit, well-formed no-op."""
    if not isinstance(result, Mapping):
        return False
    cleared = result.get("cleared")
    if not isinstance(cleared, bool):
        return False
    if "required" in result and not isinstance(result["required"], bool):
        return False
    return cleared or result.get("required") is False


_CUDA_VERSION = re.compile(r"\bCUDA\s+(?:UMD\s+)?Version\s*:\s*(?P<major>\d+)(?:\.\d+)?\b", re.I)

_PROCESS_STATE_V1_FIELDS = {
    "version",
    "pid",
    "executable",
    "create_time",
    "miner_type",
    "flight_sheet_id",
    "started_at",
    "survives_agent_restart",
}
_LEGACY_PROCESS_STATE_FIELDS = _PROCESS_STATE_V1_FIELDS - {"survives_agent_restart"}
_PROCESS_STATE_V2_FIELDS = _PROCESS_STATE_V1_FIELDS | {"argv_sha256", "api_port"}
_PROCESS_STATE_V3_FIELDS = _PROCESS_STATE_V2_FIELDS | {
    "execution_mode",
    "target_executable",
}
_PROCESS_STATE_V4_FIELDS = _PROCESS_STATE_V3_FIELDS | {
    "wsl_token",
    "wsl_guest_executable",
    "wsl_identity",
}
_PROCESS_STATE_V5_FIELDS = _PROCESS_STATE_V4_FIELDS | {"wsl_cache_key"}
_PROCESS_STATE_V7_FIELDS = _PROCESS_STATE_V2_FIELDS | {"seine_data_dir"}
_MAX_PACKAGE_ENTRIES = 4096
_MAX_ACTIVE_FLIGHT_SHEET_BYTES = 256 * 1024
_CREATE_TIME_TOLERANCE_SECONDS = 0.5
_ORPHAN_CONFIRMATION_SECONDS = 1.0
_ORPHAN_CONFIRMATION_POLL_SECONDS = 0.2
_ORPHAN_API_PROBE_TIMEOUT_SECONDS = 0.15
_MAX_DEVICE_LIST_BYTES = 256 * 1024
_SRB_DEVICE_DISCOVERY_TIMEOUT_SECONDS = 15
_SRB_OC_RESET_READY_TIMEOUT_SECONDS = 15.0
_SRB_OC_RESET_READY_POLL_SECONDS = 0.2
_SRB_OC_RESET_LOG_STABLE_SECONDS = 8.0
_SRB_OC_RESET_LOG_NAMES = ("managed-miner-launcher.log", "srbminer.log")
_MINER_READY_TIMEOUT_SECONDS = 30.0
_MINER_READY_POLL_SECONDS = 0.25
_MINER_LOG_STABLE_SECONDS = 3.0
_PTY_READ_CHUNK_BYTES = 8192

# Delayed secondary-leg start for independent dual mining (2026-11-01):
# the primary leg launches first and the secondary leg follows after this
# many seconds.  The value is an Agent-local product constant; tests shrink
# it (0 keeps the historical synchronous launch) through the module global.
SECONDARY_LEG_START_DELAY_SECONDS = 30.0
# A durable pending marker older than its deadline plus this grace is no
# longer trusted as a transitional "starting" window by status readers.
_SECONDARY_START_MARKER_GRACE_SECONDS = 600.0
_SECONDARY_START_CANCEL_JOIN_SECONDS = 2.0
_SECONDARY_START_MARKER_NAME = "secondary-start-pending.json"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SRB_DEVICE_LINE = re.compile(
    # Greedy prefix is intentional: PCI addresses themselves contain colons,
    # while SRBMiner's device/name delimiter is the final colon on the line.
    r"^\s*GPU(?P<index>\d+)\s+(?P<prefix>.*)\s+:\s+(?P<description>.+?)\s*$",
    re.IGNORECASE,
)
_PCI_ADDRESS = re.compile(
    r"(?<![0-9a-f])(?:(?P<domain>[0-9a-f]{4,8}):)?"
    r"(?P<bus>[0-9a-f]{2}):(?P<device>[0-9a-f]{2})\.(?P<function>[0-7])"
    r"(?![0-9a-f])",
    re.IGNORECASE,
)


def _wsl_identity_from_record(record: Mapping[str, Any]) -> WslLaunchIdentity:
    value = record.get("wsl_identity")
    if not isinstance(value, dict) or set(value) != {
        "windows_session_id",
        "windows_user_sid",
        "distribution",
        "linux_uid",
    }:
        raise ManagedMinerError("managed WSL launch identity is invalid")
    session_id = value.get("windows_session_id")
    user_sid = value.get("windows_user_sid")
    distribution = value.get("distribution")
    linux_uid = value.get("linux_uid")
    if (
        isinstance(session_id, bool)
        or not isinstance(session_id, int)
        or session_id < 0
        or session_id > 0xFFFFFFFE
        or not isinstance(user_sid, str)
        or re.fullmatch(r"S-[0-9-]{3,184}", user_sid, re.I) is None
        or not isinstance(distribution, str)
        or re.fullmatch(r"[A-Za-z0-9._+ -]{1,128}", distribution) is None
        or isinstance(linux_uid, bool)
        or not isinstance(linux_uid, int)
        or linux_uid < 0
        or linux_uid > 2**31 - 1
    ):
        raise ManagedMinerError("managed WSL launch identity is invalid")
    return WslLaunchIdentity(session_id, user_sid, distribution, linux_uid)


def _external_miner_environment(system_name: str) -> dict[str, str]:
    """Return an environment safe for a non-bundled miner executable.

    PyInstaller's Linux bootloader prepends its private extraction directory to
    ``LD_LIBRARY_PATH`` so the frozen Agent can find its own shared libraries.
    Passing that value to XMRig or SRBMiner can make the miner load the Agent's
    bundled libraries instead of the system/miner versions.  Restore the value
    saved by the bootloader before both device discovery and the real launch.

    An ordinary source installation is left untouched.  A frozen build without
    ``LD_LIBRARY_PATH_ORIG`` represents a process that originally had no loader
    override, so its bootloader-added value is removed.
    """

    return external_process_environment(system_name)


def _expected_executable_names(system_name: str, miner_type: str) -> tuple[str, ...]:
    adapter = MINER_ADAPTERS.get(miner_type)
    return adapter.executable_candidates(system_name) if adapter is not None else ()


def _recorded_executable_names(system_name: str, miner_type: str) -> tuple[str, ...]:
    """Recognize only former exact entry names when proving an old process."""
    retired_names = {
        ("Windows", "lolminer"): ("lolMiner.exe",),
        ("Linux", "lolminer"): ("lolMiner",),
        ("Windows", "forgeminer"): ("forge.exe",),
        ("Linux", "forgeminer"): ("forge",),
    }
    return retired_names.get((system_name, miner_type), ()) or _expected_executable_names(system_name, miner_type)


def _reported_cuda_major(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int | None:
    """Read only nvidia-smi's bounded banner, matching upstream pick-miner.sh."""

    try:
        completed = runner(
            ["nvidia-smi"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=external_process_environment("Linux"),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    output = "\n".join(
        value
        for value in (completed.stdout, completed.stderr)
        if isinstance(value, str)
    )
    if len(output.encode("utf-8", errors="replace")) > 256 * 1024:
        return None
    match = _CUDA_VERSION.search(output)
    return int(match.group("major")) if match else None


def _pearl_environment(executable: Path, system_name: str) -> dict[str, str]:
    environment = _external_miner_environment(system_name)
    environment["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    if system_name == "Linux" and executable.name == "miner":
        library_directory = executable.parent / "lib"
        try:
            info = library_directory.lstat()
        except OSError as exc:
            raise ManagedMinerError("Pearl Miner AMD package has no lib directory") from exc
        if _link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ManagedMinerError("Pearl Miner AMD lib directory is unsafe")
        existing = environment.get("LD_LIBRARY_PATH")
        environment["LD_LIBRARY_PATH"] = (
            f"{library_directory}{os.pathsep}{existing}"
            if existing
            else str(library_directory)
        )
    return environment



def _normalized_pci_address(value: str) -> str | None:
    match = _PCI_ADDRESS.search(value)
    if match is None:
        return None
    domain = int(match.group("domain") or "0", 16)
    bus = int(match.group("bus"), 16)
    device = int(match.group("device"), 16)
    function = int(match.group("function"), 16)
    return f"{domain:04x}:{bus:02x}:{device:02x}.{function:x}"


def _normalized_device_name(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _compact_device_name(value: str) -> str:
    """Return the comparison key used by model-based OC rules."""

    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _srb_device_vendor(name: str, prefix: str) -> str | None:
    normalized = _normalized_device_name(f"{name} {prefix}")
    if "nvidia" in normalized or " cuda " in f" {normalized} ":
        return "nvidia"
    if "intel" in normalized:
        return "intel"
    if "amd" in normalized or "radeon" in normalized or "firepro" in normalized:
        return "amd"
    return None


def _is_discrete_srb_device(vendor: str | None, name: str) -> bool:
    """Conservatively classify SRBMiner devices without enabling an unknown iGPU.

    NVIDIA's supported compute devices are discrete.  AMD and Intel expose both
    integrated and discrete OpenCL devices, so those vendors are accepted only
    when the model string identifies a discrete product family.  Unknown models
    fail closed instead of silently mining on shared system memory.
    """

    if vendor == "nvidia":
        return True
    normalized = _normalized_device_name(name)
    if vendor == "amd":
        discrete_patterns = (
            r"\bradeon rx\s*\d",
            r"\bradeon pro\b",
            r"\bradeon vii\b",
            r"\bradeon hd\s*\d",
            r"\bradeon r[579]\b",
            r"\bfirepro\b",
            r"\bfiregl\b",
            r"\binstinct\b",
            r"\bbc\s*250\b",
            r"\bvega (?:frontier|56|64)\b",
        )
        return any(re.search(pattern, normalized) for pattern in discrete_patterns)
    if vendor == "intel":
        discrete_patterns = (
            r"\barc (?:tm )?(?:pro )?[ab]\d{2,4}\b",
            r"\biris xe max\b",
            r"\bdata center gpu (?:max|flex)\b",
        )
        return any(re.search(pattern, normalized) for pattern in discrete_patterns)
    return False


def _srb_duplicate_igpu_identity(
    prefix: str,
    description: str,
    name: str,
    vendor: str | None,
    discrete: bool,
) -> tuple[str, str, str] | None:
    """Identify only an explicitly disabled iGPU for duplicate-ID tolerance.

    SRBMiner 3.8.9 can report two OpenCL entries for the same disabled AMD
    iGPU with one GPU ID. Unknown identity and known discrete products remain
    conflicts, even if their output claims to be an iGPU. Keep this stricter
    check separate from the existing display/telemetry PCI parser.
    """
    if vendor not in {"amd", "intel"} or discrete or not name.isascii():
        return None
    markers = re.findall(r"\[\s*igpu\s*:\s*([^\[\]]*)\]", description, re.IGNORECASE)
    if len(markers) != 1 or " ".join(markers[0].casefold().split()) != "disabled by default":
        return None
    # Require one complete bracketed BDF. A substring match could otherwise
    # turn a malformed domain or multiple addresses into a false identity.
    if re.fullmatch(r"(?:\[[^\[\]]+\]\s*)+", prefix) is None:
        return None
    addresses = [part for part in re.findall(r"\[([^\[\]]+)\]", prefix) if ":" in part]
    if len(addresses) != 1:
        return None
    pci = _PCI_ADDRESS.fullmatch(addresses[0])
    if pci is None:
        return None
    raw_domain = pci.group("domain")
    if raw_domain is not None and len(raw_domain) not in {4, 8}:
        return None
    domain = int(raw_domain or "0", 16)
    device = int(pci.group("device"), 16)
    if domain > 0xFFFF or device > 0x1F:
        return None
    model = _normalized_device_name(name)
    if not model:
        return None
    address = f"{domain:04x}:{int(pci.group('bus'), 16):02x}:{device:02x}.{pci.group('function')}"
    return vendor, model, address.lower()


def parse_srbminer_devices(output: str) -> list[dict[str, Any]]:
    """Parse the bounded, human-readable output of SRBMiner ``--list-devices``."""

    if (
        not isinstance(output, str)
        or len(output.encode("utf-8", errors="replace")) > _MAX_DEVICE_LIST_BYTES
    ):
        raise ManagedMinerError("SRBMiner device list is invalid")
    devices: list[dict[str, Any]] = []
    seen: dict[int, tuple[str, str, str] | None] = {}
    for raw_line in output.splitlines():
        line = _ANSI_ESCAPE.sub("", raw_line)
        match = _SRB_DEVICE_LINE.match(line)
        if match is None:
            continue
        index = int(match.group("index"))
        if index > 31:
            raise ManagedMinerError("SRBMiner device list has invalid GPU indices")
        prefix = match.group("prefix").strip()
        description = match.group("description").strip()
        # SRBMiner appends architecture, capability, and memory fields in
        # brackets.  The part before the first bracket is the stable model name.
        name = re.split(r"\s+\[", description, maxsplit=1)[0].strip()
        if not name or len(name) > 160:
            raise ManagedMinerError("SRBMiner device list has an invalid GPU name")
        vendor = _srb_device_vendor(name, prefix)
        discrete = _is_discrete_srb_device(vendor, name)
        igpu_identity = _srb_duplicate_igpu_identity(prefix, description, name, vendor, discrete)
        if index in seen:
            if igpu_identity is not None and seen[index] == igpu_identity:
                continue
            raise ManagedMinerError("SRBMiner device list has invalid GPU indices")
        devices.append(
            {
                "index": index,
                "name": name,
                "vendor": vendor,
                "pci_bus_id": _normalized_pci_address(prefix),
                "discrete": discrete,
            }
        )
        seen[index] = igpu_identity
    if not devices:
        raise ManagedMinerError("SRBMiner did not report any GPU devices")
    return sorted(devices, key=lambda item: int(item["index"]))


def _reap_pty_process(process: subprocess.Popen[bytes]) -> bool:
    """Confirm exit of this exact direct child; do not claim descendant closure."""

    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            code = process.wait(timeout=1)
            return isinstance(code, int) and not isinstance(code, bool)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                code = process.wait(timeout=1)
                return isinstance(code, int) and not isinstance(code, bool)
            except (OSError, subprocess.TimeoutExpired):
                # A local, direct child should be reaped after SIGKILL.  Keep
                # this final wait bounded so a broken OS/process abstraction
                # cannot wedge the Agent command loop indefinitely.
                return False
        except OSError:
            return False
    else:
        # ``poll`` normally performs the waitpid, while ``wait`` also handles
        # mocked/subclassed process implementations consistently.
        try:
            code = process.wait(timeout=0)
            return isinstance(code, int) and not isinstance(code, bool)
        except (OSError, subprocess.TimeoutExpired):
            return False


def _run_linux_pty_command(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: int,
    max_output_bytes: int,
) -> subprocess.CompletedProcess[str]:
    """Run one fixed argv in a bounded Linux PTY and capture combined output.

    SRBMiner 3.x emits ``--list-devices`` only when stdout is a terminal on
    Linux.  This helper provides that terminal without invoking a shell.  It
    enforces both a wall-clock deadline and a byte limit, closes every PTY file
    descriptor, and always reaps the direct child on every exit path.
    """

    master_fd = -1
    slave_fd = -1
    process: subprocess.Popen[bytes] | None = None
    chunks: list[bytes] = []
    output_size = 0
    deadline = time.monotonic() + timeout
    completed = None
    failure = None
    try:
        master_fd, slave_fd = os.openpty()
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            shell=False,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=dict(env),
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        while True:
            running = process.poll() is None
            remaining = deadline - time.monotonic()
            if running and remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout, output=b"".join(chunks))
            wait_seconds = min(0.1, max(remaining, 0.0)) if running else 0.0
            readable, _, _ = select.select([master_fd], [], [], wait_seconds)
            if readable:
                try:
                    chunk = os.read(master_fd, _PTY_READ_CHUNK_BYTES)
                except OSError as exc:
                    # Linux PTY masters commonly report EIO after the slave is
                    # closed; it is the PTY equivalent of EOF.
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output_size += len(chunk)
                if output_size > max_output_bytes:
                    raise ManagedMinerError("SRBMiner device list is invalid")
                chunks.append(chunk)
                continue
            if not running:
                break

        remaining = deadline - time.monotonic()
        if process.poll() is None:
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout, output=b"".join(chunks))
            returncode = process.wait(timeout=remaining)
        else:
            returncode = int(process.returncode or 0)
        output = b"".join(chunks).decode("utf-8", errors="replace")
        completed = subprocess.CompletedProcess(argv, returncode, output, "")
        return completed
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if slave_fd >= 0:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        if master_fd >= 0:
            try:
                os.close(master_fd)
            except OSError:
                pass
        try:
            closed = _reap_pty_process(process) if process is not None else True
        except (OSError, subprocess.TimeoutExpired):
            closed = False
        if completed is not None:
            completed.owned_direct_child_closed = closed
        if failure is not None:
            failure.owned_direct_child_closed = closed


def discover_srbminer_devices(
    executable: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    system_name: str | None = None,
) -> list[dict[str, Any]]:
    """Ask the verified local SRBMiner binary for its actual mixed-vendor IDs."""

    target_system = system_name or platform.system()
    argv = [str(executable), "--list-devices"]
    environment = _external_miner_environment(target_system)
    try:
        # The production Linux path uses a PTY because SRBMiner 3.x suppresses
        # its device rows when stdout is a pipe.  Keep the existing injected
        # runner hook for deterministic validation/tests; remote payloads can
        # never supply this callable.
        if target_system == "Linux" and runner is subprocess.run:
            completed = _run_linux_pty_command(
                argv,
                cwd=str(executable.parent),
                env=environment,
                timeout=_SRB_DEVICE_DISCOVERY_TIMEOUT_SECONDS,
                max_output_bytes=_MAX_DEVICE_LIST_BYTES,
            )
        else:
            completed = runner(
                argv,
                cwd=str(executable.parent),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=_SRB_DEVICE_DISCOVERY_TIMEOUT_SECONDS,
                env=environment,
            )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedMinerError("SRBMiner GPU discovery failed") from exc
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    stderr = completed.stderr if isinstance(completed.stderr, str) else ""
    output = f"{stdout}\n{stderr}"
    if completed.returncode != 0:
        raise ManagedMinerError("SRBMiner GPU discovery failed")
    return parse_srbminer_devices(output)






def _gpu_leg_keys(document: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        role for role in ("primary", "secondary")
        if isinstance(document.get(role), Mapping)
        and leg_effective_resource(document[role]) == "gpu"
    )


def _srb_gpu_vendor_capability(
    document: Mapping[str, Any], gpu_keys: tuple[str, ...]
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Intersect the shared vendor facts for the actual GPU task legs."""

    supported = set(_SRB_GPU_VENDORS)
    algorithms: list[str] = []
    adapter = MINER_ADAPTERS.require("srbminer")
    for key in gpu_keys:
        leg = document.get(key)
        if not isinstance(leg, Mapping):
            continue
        spec = adapter.algorithm_spec(leg.get("algorithm"), coin=leg.get("coin"))
        if spec.name not in algorithms:
            algorithms.append(spec.name)
        # Historical entries retain their existing discrete-vendor behavior;
        # current algorithms use the explicitly declared upstream vendor set.
        vendors = spec.gpu_vendors or (_SRB_GPU_VENDORS if spec.catalogue_status == "legacy" else ())
        supported.intersection_update(vendors)
    return frozenset(supported), tuple(algorithms)


def _resolve_srbminer_gpu_selection(
    document: Mapping[str, Any],
    *,
    executable: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    system_name: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate SRBMiner GPU IDs and expand an automatic discrete selection.

    Explicit flight-sheet indices keep SRBMiner's documented ``--list-devices``
    ID semantics for compatibility with existing sheets.  An empty selection
    expands to every eligible discrete card so SRBMiner is never allowed to
    fall back to integrated OpenCL devices.
    """

    for role in ("primary", "secondary"):
        leg = document.get(role)
        if isinstance(leg, Mapping):
            validate_managed_algorithm_context(leg)
    gpu_keys = _gpu_leg_keys(document)
    resolved = copy.deepcopy(dict(document))
    if not gpu_keys:
        return resolved, []
    devices = discover_srbminer_devices(
        executable,
        runner=runner,
        system_name=system_name,
    )
    eligible = [device for device in devices if device["discrete"]]
    if not eligible:
        raise ManagedMinerError(
            "SRBMiner found no supported discrete GPU; integrated GPUs are excluded"
        )
    available = ", ".join(
        f"GPU{device['index']}={str(device['name'])[:64]}"
        for device in eligible[:8]
    )
    eligible_by_id = {int(device["index"]): device for device in eligible}
    supported_vendors, gpu_algorithms = _srb_gpu_vendor_capability(resolved, gpu_keys)
    algorithm_label = "+".join(gpu_algorithms) or "selected algorithm"

    def validate_selected_hardware(device: Mapping[str, Any]) -> None:
        for key in gpu_keys:
            leg = resolved[key]
            package = leg.get("miner_package") or {}
            try:
                MINER_ADAPTERS.require("srbminer").validate_hardware_version(
                    _algorithm(leg, "srbminer"), str(package.get("version") or ""),
                    vendor=str(device.get("vendor") or ""),
                    name=str(device.get("name") or "").replace("_", " ").replace("-", " "),
                )
            except ValueError as exc:
                raise ManagedMinerError(str(exc)) from exc

    first_leg = resolved[gpu_keys[0]]
    requested = list(first_leg.get("device_indices") or [])
    if any(
        isinstance(index, bool)
        or not isinstance(index, int)
        or index not in eligible_by_id
        for index in requested
    ):
        raise ManagedMinerError(
            "SRBMiner GPU ID is not an eligible discrete device; "
            f"available: {available}"
        )
    if requested:
        unsupported = [
            eligible_by_id[index]
            for index in requested
            if eligible_by_id[index]["vendor"] not in supported_vendors
        ]
        if unsupported:
            selected = ", ".join(
                f"GPU{device['index']}={str(device['name'])[:64]} "
                f"({device['vendor'] or 'unknown'})"
                for device in unsupported[:4]
            )
            raise ManagedMinerError(
                f"SRBMiner {algorithm_label} does not support the selected GPU vendor: "
                f"{selected}; supported vendors: {', '.join(sorted(supported_vendors))}"
            )
        for index in requested:
            validate_selected_hardware(eligible_by_id[index])
        selected_ids = requested
    else:
        selected_ids = []
        for device in eligible:
            if device["vendor"] not in supported_vendors:
                continue
            try:
                validate_selected_hardware(device)
            except ManagedMinerError:
                continue  # Only documented incompatible backend floors.
            selected_ids.append(int(device["index"]))
        if not selected_ids:
            raise ManagedMinerError(
                f"SRBMiner found no compatible discrete GPU for {algorithm_label}; "
                f"supported vendors: {', '.join(sorted(supported_vendors))}"
            )
    mapped_indices = list(selected_ids)
    selected_devices = [eligible_by_id[index] for index in selected_ids]
    mapping = [
        {
            "logical_index": position,
            "miner_id": int(device["index"]),
            "vendor": device["vendor"],
            "name": device["name"],
            # SRBMiner numbers every OpenCL/CUDA device, while the dashboard's
            # NVIDIA/NVML list has its own index space.  Keep the stable PCI
            # identity so heartbeat collection can translate gpuN statistics
            # without guessing from either numeric index.
            "pci_bus_id": device["pci_bus_id"],
        }
        for position, device in enumerate(selected_devices)
    ]
    for key in gpu_keys:
        leg = resolved[key]
        leg_requested = list(leg.get("device_indices") or [])
        if leg_requested and leg_requested != requested:
            raise ManagedMinerError(
                "SRBMiner GPU legs must select the same discrete GPU order"
            )
        leg["device_indices"] = list(mapped_indices)

    raw_oc = resolved.get("gpu_oc")
    if raw_oc is not None:
        settings = raw_oc.get("settings") if isinstance(raw_oc, dict) else None
        if not isinstance(settings, list):
            raise ManagedMinerError("managed SRBMiner GPU OC is invalid")
        locator_modes: set[str] = set()
        for setting in settings:
            if not isinstance(setting, dict):
                raise ManagedMinerError("managed SRBMiner GPU OC is invalid")
            locators = {key for key in ("index", "model_match") if key in setting}
            if len(locators) != 1:
                raise ManagedMinerError("managed SRBMiner GPU OC target is invalid")
            locator_modes.update(locators)
        if len(locator_modes) != 1:
            raise ManagedMinerError(
                "managed SRBMiner GPU OC cannot mix indices and model matchers"
            )

        if "index" in locator_modes:
            mapped_settings: list[dict[str, Any]] = []
            for setting in settings:
                gpu_id = setting.get("index")
                if (
                    isinstance(gpu_id, bool)
                    or not isinstance(gpu_id, int)
                    or gpu_id not in eligible_by_id
                ):
                    raise ManagedMinerError("SRBMiner GPU OC ID is not an eligible discrete GPU")
                device = eligible_by_id[gpu_id]
                if device["vendor"] != "nvidia":
                    raise ManagedMinerError(
                        "SRBMiner flight GPU OC supports only NVIDIA; "
                        f"GPU{gpu_id} is {str(device['name'])[:96]}"
                    )
                mapped = dict(setting)
                mapped["index"] = gpu_id
                mapped_settings.append(mapped)
            raw_oc["settings"] = mapped_settings
        else:
            mapped_by_miner_id: dict[int, dict[str, Any]] = {}
            seen_matchers: set[str] = set()
            for position, setting in enumerate(settings):
                raw_matcher = setting.get("model_match")
                if not isinstance(raw_matcher, str):
                    raise ManagedMinerError(
                        f"SRBMiner GPU OC model rule {position} is invalid"
                    )
                matcher = _compact_device_name(raw_matcher)
                if len(matcher) < 2 or matcher in seen_matchers:
                    raise ManagedMinerError(
                        f"SRBMiner GPU OC model rule {position} is invalid or duplicated"
                    )
                seen_matchers.add(matcher)
                matches = [
                    device
                    for device in selected_devices
                    if matcher in _compact_device_name(str(device["name"]))
                ]
                # Extra model rows are intentionally ignored on a rig that does
                # not contain that model.  This lets one flight sheet carry
                # 5080, 5070, and 5060 Ti rules and be reused across rigs.
                if not matches:
                    continue
                distinct_models = {
                    _compact_device_name(str(device["name"])) for device in matches
                }
                if len(distinct_models) != 1:
                    names = ", ".join(
                        sorted({str(device["name"])[:64] for device in matches})
                    )
                    raise ManagedMinerError(
                        f"SRBMiner GPU model matcher {raw_matcher!r} is ambiguous: {names}"
                    )
                for device in matches:
                    if device["vendor"] != "nvidia":
                        raise ManagedMinerError(
                            "SRBMiner flight GPU OC supports only NVIDIA; "
                            f"model rule {raw_matcher!r} matched {str(device['name'])[:96]}"
                        )
                    miner_id = int(device["index"])
                    if miner_id in mapped_by_miner_id:
                        raise ManagedMinerError(
                            f"SRBMiner GPU model rules overlap on {str(device['name'])[:96]}"
                        )
                    mapped = {
                        key: value
                        for key, value in setting.items()
                        if key != "model_match"
                    }
                    mapped["index"] = miner_id
                    mapped_by_miner_id[miner_id] = mapped

            raw_oc["settings"] = [
                mapped_by_miner_id[miner_id]
                for miner_id in mapped_indices
                if miner_id in mapped_by_miner_id
            ]
    return resolved, mapping


def resolve_srbminer_gpu_indices(
    document: Mapping[str, Any],
    *,
    executable: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    system_name: str | None = None,
) -> dict[str, Any]:
    """Public document-only wrapper used by validation-focused callers/tests."""

    resolved, _mapping = _resolve_srbminer_gpu_selection(
        document,
        executable=executable,
        runner=runner,
        system_name=system_name,
    )
    return resolved


def has_elevated_miner_privileges(system_name: str) -> bool:
    """Return whether SRBMiner OC can use the OS vendor-driver controls."""

    if system_name == "Windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    if system_name == "Linux":
        try:
            return os.geteuid() == 0
        except AttributeError:
            return False
    return False

def validate_srb_dual_configuration(
    primary: Mapping[str, Any], secondary: Mapping[str, Any]
) -> tuple[str, str]:
    """Use shared ordered pair policy and preserve actual GPU-index proof."""

    primary_algorithm = _algorithm(primary, "srbminer")
    secondary_algorithm = _algorithm(secondary, "srbminer")
    primary_resource = leg_effective_resource(primary)
    secondary_resource = leg_effective_resource(secondary)
    if not srb_dual_pair_supported(
        primary_algorithm, secondary_algorithm, primary_resource, secondary_resource
    ):
        if primary_algorithm == secondary_algorithm == "randomx":
            raise ManagedMinerError("SRBMiner dual mining cannot use RandomX for both algorithms")
        raise ManagedMinerError("SRBMiner secondary algorithm is not an official supported pair")
    if primary_resource == secondary_resource == "cpu":
        primary_options = validate_managed_extra_config(
            "srbminer", primary.get("extra_config", {}), "primary.extra_config"
        )
        secondary_options = validate_managed_extra_config(
            "srbminer", secondary.get("extra_config", {}), "secondary.extra_config"
        )
        try:
            validate_periodic_cpu_threads(primary_options, secondary_options)
        except ValueError as exc:
            raise ManagedMinerError(str(exc)) from exc
    if primary_resource == secondary_resource == "gpu":
        if list(primary.get("device_indices") or []) != list(secondary.get("device_indices") or []):
            raise ManagedMinerError(
                "SRBMiner GPU dual mining must use the same ordered GPU indices for both algorithms"
            )
    return primary_algorithm, secondary_algorithm


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ManagedMinerError(f"{label} must be a boolean")
    return value


_TRUSTED_OPTION_CHOICE_ALIASES: dict[
    tuple[str, str], Mapping[str, str | int]
] = {
    # Older Controller payloads used the UI labels as values.  Keep accepting
    # them without expanding the declarative adapter ABI or trusting remote
    # aliases.
    ("builtin.pearlminer.v1", "cuda_variant"): {
        "cuda12": "12",
        "cuda13": "13",
    },
}


def _choice_error(
    adapter: MinerAdapter,
    spec: OptionSpec,
    label: str,
) -> ManagedMinerError:
    choices: list[str] = []
    aliases = _TRUSTED_OPTION_CHOICE_ALIASES.get(
        (adapter.runtime_id, spec.name), {}
    )
    for choice in spec.choices:
        display = str(choice)
        legacy = next(
            (
                alias
                for alias, target in aliases.items()
                if target == choice and alias.startswith("cuda")
            ),
            None,
        )
        choices.append(legacy or display)
    if len(choices) == 1:
        rendered = choices[0]
    elif len(choices) == 2:
        rendered = " or ".join(choices)
    else:
        rendered = ", ".join(choices[:-1]) + f", or {choices[-1]}"
    return ManagedMinerError(f"{label} must be {rendered}")


def _validate_adapter_option(
    adapter: MinerAdapter,
    spec: OptionSpec,
    value: Any,
    label: str,
) -> str | int | bool | None:
    """Use shared scalar rules while retaining Agent aliases/error messages."""

    candidate = value
    if spec.value_type == "choice" and value is not None:
        aliases = _TRUSTED_OPTION_CHOICE_ALIASES.get(
            (adapter.runtime_id, spec.name), {}
        )
        if isinstance(value, bool):
            raise _choice_error(adapter, spec, label)
        if isinstance(value, str) or (aliases and isinstance(value, int)):
            # ``cuda_variant`` historically accepted integer 12/13 because
            # the legacy validator normalized via ``str(value)``.
            raw_candidate = str(value).strip().lower()
            declared = {
                str(choice).strip().lower(): choice
                for choice in spec.choices
                if isinstance(choice, str)
            }
            wire_candidate = aliases.get(raw_candidate, raw_candidate)
            reverse_declared = any(
                alias in declared and target == raw_candidate
                for alias, target in aliases.items()
            )
            if (
                raw_candidate not in declared
                and str(wire_candidate).lower() not in declared
                and not reverse_declared
            ):
                raise _choice_error(adapter, spec, label)
            candidate: str | int = wire_candidate
        else:
            if aliases:
                raise _choice_error(adapter, spec, label)
            candidate = value
    validation_candidate = candidate
    if spec.value_type == "choice" and value is not None:
        # Validate the declared spelling, then retain the historical runtime
        # alias (for example cuda12 -> 12) without widening shared choices.
        validation_candidate = next(
            (alias for alias, target in aliases.items()
             if target == candidate and alias in spec.choices),
            candidate,
        )
    try:
        validated = validate_option_value(spec, validation_candidate)
        return candidate if spec.value_type == "choice" and value is not None else validated
    except ValueError as exc:
        if spec.value_type == "choice":
            raise _choice_error(adapter, spec, label) from exc
        if spec.value_type == "boolean":
            _bool(value, label)
        if spec.value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ManagedMinerError(f"{label} must be an integer") from exc
            if spec.minimum is not None and value < spec.minimum:
                if spec.maximum is not None:
                    raise ManagedMinerError(f"{label} must be from {spec.minimum} to {spec.maximum}") from exc
                raise ManagedMinerError(f"{label} must be at least {spec.minimum}") from exc
            if spec.maximum is not None and value > spec.maximum:
                if spec.minimum is not None:
                    raise ManagedMinerError(f"{label} must be from {spec.minimum} to {spec.maximum}") from exc
                raise ManagedMinerError(f"{label} must be at most {spec.maximum}") from exc
        raise ManagedMinerError(str(exc).replace(spec.name, label, 1)) from exc


def validate_managed_extra_config(
    miner_type: str, value: Any, label: str = "extra_config"
) -> dict[str, Any]:
    """Validate typed options and literal extra arguments for an active miner."""

    adapter = MINER_ADAPTERS.get(miner_type)
    if adapter is None:
        if is_retired_miner(miner_type):
            raise ManagedMinerError(f"{miner_type} is retired; select an active miner")
        raise ManagedMinerError("unsupported managed miner type")
    if not isinstance(value, dict):
        raise ManagedMinerError(f"{label} must be an object")
    specs = {spec.name: spec for spec in adapter.options}
    unknown = set(value) - adapter.option_keys
    if unknown:
        raise ManagedMinerError(
            f"{label} contains unsupported {miner_type} keys: "
            + ", ".join(sorted(unknown))
        )
    # The immutable registry validates every declared default at construction.
    # Requests need to validate only their supplied values and legacy aliases.
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        normalized[key] = _validate_adapter_option(
            adapter,
            specs[key],
            item,
            f"{label}.{key}",
        )
    try:
        parse_extra_args(normalized.get("extra_args", ""), miner_type=miner_type)
    except ValueError as exc:
        raise ManagedMinerError(f"{label}.{exc}") from exc
    return normalized




def _plain_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ManagedMinerError(f"{label} cannot be inspected") from exc
    if _link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise ManagedMinerError(f"{label} must be a real local directory")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _managed_role_runtime(
    runtime: Path,
    role: str,
    *,
    create: bool,
) -> Path:
    """Return one fixed role directory without following a link/reparse point."""

    if role not in {"primary", "secondary"}:
        raise ManagedMinerError("managed miner role is invalid")
    role_runtime = runtime / role
    if create:
        try:
            role_runtime.mkdir(parents=False, exist_ok=True)
        except OSError as exc:
            raise ManagedMinerError(
                f"managed miner {role} runtime directory is unavailable"
            ) from exc
    try:
        info = role_runtime.lstat()
    except FileNotFoundError:
        if not create:
            return role_runtime
        raise ManagedMinerError(
            f"managed miner {role} runtime directory is unavailable"
        )
    except OSError as exc:
        raise ManagedMinerError(
            f"managed miner {role} runtime directory cannot be inspected"
        ) from exc
    if _link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise ManagedMinerError(
            f"managed miner {role} runtime directory must be a real local directory"
        )
    try:
        resolved_root = runtime.resolve(strict=True)
        resolved_role = role_runtime.resolve(strict=True)
    except OSError as exc:
        raise ManagedMinerError(
            f"managed miner {role} runtime directory cannot be resolved"
        ) from exc
    if not _inside(resolved_role, resolved_root):
        raise ManagedMinerError(
            f"managed miner {role} runtime directory is outside managed runtime"
        )
    return resolved_role


def find_miner_executable(
    package_root: Path,
    miner_type: str,
    *,
    system_name: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    gpu_backend: str = "auto",
    cuda_variant: str = "auto",
) -> Path:
    """Return the unique exact official entry name without following links."""

    if is_retired_miner(miner_type):
        raise ManagedMinerError(f"{miner_type} is retired; select an active miner")
    system = system_name or platform.system()
    if gpu_backend not in {"auto", "nvidia", "amd"}:
        raise ManagedMinerError("Pearl Miner GPU backend is invalid")
    if cuda_variant not in {"auto", "12", "13"}:
        raise ManagedMinerError("Pearl Miner CUDA variant is invalid")
    expected_names = _expected_executable_names(system, miner_type)
    if not expected_names:
        raise ManagedMinerError(
            f"{miner_type} managed mining is not supported on {system or 'this OS'}"
        )
    try:
        root = package_root.resolve(strict=True)
    except OSError as exc:
        raise ManagedMinerError("installed miner package does not exist") from exc
    _plain_directory(root, "installed miner package")

    matches: list[Path] = []
    entries_seen = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: item.name)
        except OSError as exc:
            raise ManagedMinerError("installed miner package cannot be scanned") from exc
        for entry in entries:
            entries_seen += 1
            if entries_seen > _MAX_PACKAGE_ENTRIES:
                raise ManagedMinerError("installed miner package has too many entries")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ManagedMinerError(
                    "installed miner package entry cannot be inspected"
                ) from exc
            if _link_or_reparse(info):
                raise ManagedMinerError("installed miner package contains a link")
            path = Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode) and entry.name in expected_names:
                try:
                    resolved = path.resolve(strict=True)
                except OSError as exc:
                    raise ManagedMinerError("miner executable cannot be resolved") from exc
                if not _inside(resolved, root):
                    raise ManagedMinerError("miner executable escapes its package")
                matches.append(resolved)
            elif not stat.S_ISREG(info.st_mode):
                raise ManagedMinerError("installed miner package contains a special file")
    if not matches:
        if miner_type != "pearlminer" and len(expected_names) == 1:
            raise ManagedMinerError(
                f"installed {miner_type} package has no exact {expected_names[0]} entry"
            )
        raise ManagedMinerError(
            f"installed {miner_type} package has no exact official executable entry"
        )
    by_name: dict[str, list[Path]] = {}
    for match in matches:
        by_name.setdefault(match.name, []).append(match)
    if any(len(items) != 1 for items in by_name.values()):
        if miner_type != "pearlminer" and len(expected_names) == 1:
            raise ManagedMinerError(
                f"installed {miner_type} package has multiple {expected_names[0]} entries"
            )
        raise ManagedMinerError(
            f"installed {miner_type} package has duplicate executable entries"
        )
    if miner_type == "pearlminer" and system == "Linux":
        has_amd = "miner" in by_name
        cuda_names = {name for name in by_name if name.startswith("miner-cuda")}
        if has_amd and cuda_names:
            raise ManagedMinerError("installed Pearl Miner package mixes AMD and NVIDIA builds")
        if has_amd:
            if gpu_backend == "nvidia" or cuda_variant != "auto":
                raise ManagedMinerError(
                    "Pearl Miner flight backend does not match the AMD package"
                )
            executable = by_name["miner"][0]
        elif cuda_names:
            if gpu_backend == "amd":
                raise ManagedMinerError(
                    "Pearl Miner flight backend does not match the NVIDIA package"
                )
            cuda_major = _reported_cuda_major(runner)
            requested_cuda = (
                cuda_variant
                if cuda_variant != "auto"
                else ("13" if cuda_major is not None and cuda_major >= 13 else "12")
            )
            preferred = f"miner-cuda{requested_cuda}"
            if preferred not in by_name:
                raise ManagedMinerError(
                    f"Pearl Miner package has no requested CUDA {requested_cuda} build"
                )
            executable = by_name[preferred][0]
        else:  # pragma: no cover - guarded by the non-empty match check
            raise ManagedMinerError("installed Pearl Miner package has no Linux build")
    elif len(matches) != 1:
        raise ManagedMinerError(
            f"installed {miner_type} package has multiple executable entries"
        )
    else:
        if miner_type == "pearlminer" and system == "Windows" and (
            gpu_backend == "amd" or cuda_variant != "auto"
        ):
            raise ManagedMinerError(
                "Pearl Miner Windows package does not support that backend override"
            )
        executable = matches[0]
    if system != "Windows":
        try:
            mode = executable.lstat().st_mode
            executable.chmod(mode | stat.S_IXUSR)
        except OSError as exc:
            raise ManagedMinerError("miner executable cannot be made executable") from exc
    return executable


def _algorithm(leg: Mapping[str, Any], miner_type: str) -> str:
    if is_retired_miner(miner_type):
        raise ManagedMinerError(f"{miner_type} is retired; select an active miner")
    raw = leg.get("algorithm")
    algorithm = raw.lower() if isinstance(raw, str) else ""
    coin = str(leg.get("coin", "")).upper()
    if miner_type == "pearlminer":
        if coin != "PEARL" or algorithm not in {"", "pearlhash"}:
            raise ManagedMinerError("Pearl Miner requires PEARL / pearlhash")
        return "pearlhash"
    if miner_type == "xmrig":
        if coin != "XMR":
            raise ManagedMinerError("the managed XMRig template currently supports XMR only")
        if algorithm not in {"", "randomx", "rx", "rx/0"}:
            raise ManagedMinerError("XMR managed mining requires RandomX")
        return "rx/0"
    if miner_type != "srbminer":
        raise ManagedMinerError("unsupported managed miner type")
    try:
        return MINER_ADAPTERS.require("srbminer").normalize_algorithm(algorithm, coin=coin)
    except ValueError as exc:
        raise ManagedMinerError(str(exc)) from exc


def build_pearlminer_argv(
    executable: Path,
    document: Mapping[str, Any],
) -> list[str]:
    """Build Pearl Miner's documented CLI without a shell or remote text."""

    if document.get("secondary") is not None:
        raise ManagedMinerError("managed Pearl Miner does not support a secondary leg")
    primary = document.get("primary")
    if not isinstance(primary, Mapping):
        raise ManagedMinerError("managed flight sheet has no primary leg")
    validate_managed_extra_config(
        "pearlminer",
        primary.get("extra_config", {}),
        "primary.extra_config",
    )
    _algorithm(primary, "pearlminer")
    device_indices = primary.get("device_indices") or []
    if device_indices:
        raise ManagedMinerError("Pearl Miner does not document a GPU index selector")
    endpoint, _tls = _pool(primary)
    worker = str(primary.get("worker_name") or document.get("worker_name") or "")
    wallet = str(primary.get("wallet_address") or "")
    if not worker or not wallet:
        raise ManagedMinerError("Pearl Miner requires wallet and worker")
    pool_url = str(primary.get("pool_url") or "")
    pool_scheme = urlparse(pool_url).scheme.lower()
    if pool_scheme not in {"pearl", "stratum+ssl", "stratum+tcp"}:
        raise ManagedMinerError(
            "Pearl Miner pool URL must use pearl://host:port or "
            "stratum+ssl://host:port or stratum+tcp://host:port"
        )
    proxy_endpoint = pool_url if pool_scheme.startswith("stratum+") else endpoint
    argv = [
        str(executable),
        "--proxy",
        proxy_endpoint,
    ]
    if pool_scheme in {"stratum+ssl", "stratum+tcp"}:
        argv.extend(["--stratum-dialect", "kryptex_prl"])
    argv.extend(["--address", wallet])
    argv.extend(["--worker", worker])
    argv.append("-gpu")
    argv.extend(parse_extra_args(
        primary.get("extra_config", {}).get("extra_args", ""),
        miner_type="pearlminer",
    ))
    return argv






def _pool(leg: Mapping[str, Any]) -> tuple[str, bool]:
    try:
        parsed = urlparse(str(leg["pool_url"]))
        host = parsed.hostname
        port = parsed.port
    except (KeyError, ValueError) as exc:
        raise ManagedMinerError("managed miner pool URL is malformed") from exc
    if not host or port is None:
        raise ManagedMinerError("managed miner pool URL must contain host and port")
    host_text = f"[{host}]" if ":" in host else host
    scheme_tls = parsed.scheme.lower() in {"stratum+ssl", "stratum+tls"}
    extra = leg.get("extra_config") or {}
    explicit_tls = extra.get("tls")
    if scheme_tls and explicit_tls is False:
        raise ManagedMinerError("TLS cannot be disabled for a TLS pool URL")
    tls = scheme_tls or explicit_tls is True
    return f"{host_text}:{port}", tls


def build_xmrig_config(
    document: Mapping[str, Any], *, api_port: int, log_path: Path
) -> dict[str, Any]:
    """Build the complete XMRig JSON document from structured fields only."""

    if document.get("secondary") is not None:
        raise ManagedMinerError("managed XMRig does not support a secondary leg")
    leg = document["primary"]
    extra = validate_managed_extra_config(
        "xmrig", leg.get("extra_config", {}), "primary.extra_config"
    )
    endpoint, tls = _pool(leg)
    cpu: dict[str, Any] = {
        "enabled": True,
        "huge-pages": extra.get("huge_pages", True),
    }
    if "cpu_max_threads_hint" in extra:
        cpu["max-threads-hint"] = extra["cpu_max_threads_hint"]
    return {
        "autosave": False,
        "background": False,
        "colors": False,
        "title": False,
        "randomx": {"1gb-pages": extra.get("randomx_1gb_pages", False)},
        "cpu": cpu,
        "opencl": False,
        "cuda": False,
        "log-file": str(log_path),
        "print-time": 60,
        "health-print-time": 60,
        "pause-on-battery": extra.get("pause_on_battery", False),
        "pools": [
            {
                "algo": _algorithm(leg, "xmrig"),
                "coin": "monero",
                "url": endpoint,
                "user": leg["wallet_address"],
                "pass": leg.get("pool_password") or "x",
                "rig-id": document["worker_name"],
                "keepalive": extra.get("keepalive", False),
                "tls": tls,
                "enabled": True,
            }
        ],
        "http": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": api_port,
            "access-token": None,
            "restricted": True,
        },
    }


def build_xmrig_argv(
    executable: Path, config_path: Path, leg: Mapping[str, Any]
) -> list[str]:
    extra = validate_managed_extra_config(
        "xmrig", leg.get("extra_config", {}), "primary.extra_config"
    )
    argv = [str(executable), "--config", str(config_path), "--no-color"]
    threads = extra.get("cpu_threads")
    if threads is not None:
        argv.extend(["--threads", str(threads)])
    argv.extend(parse_extra_args(extra.get("extra_args", ""), miner_type="xmrig"))
    return argv


def _srb_connection_args(leg: Mapping[str, Any]) -> list[str]:
    validate_managed_algorithm_context(leg)
    adapter = MINER_ADAPTERS.require("srbminer")
    extra = validate_managed_extra_config("srbminer", leg.get("extra_config", {}))
    parsed = parse_pool_endpoint(leg["pool_url"], adapter.pool_schemes)
    if parsed.scheme == "quic":
        host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
        try:
            wallet = adapter.quic_wallet_value(
                str(leg.get("wallet_address") or ""), extra,
                pool_password=leg.get("pool_password"),
            )
        except ValueError as exc:
            raise ManagedMinerError(str(exc)) from exc
        # QUIC public-wallet and node-token identities are exact: no worker
        # suffix/flag, encoding conversion, or inferred secret credential.
        return ["--pool", f"{host}:{parsed.port}", "--wallet", wallet,
                "--tls-cert-sha256", str(extra["tls_cert_sha256"])]
    endpoint, tls = _pool(leg)
    password = leg.get("pool_password") or "x"
    if ";" in password or "!" in password:
        raise ManagedMinerError("managed SRBMiner pool password must not contain ';' or '!'")
    return ["--pool", endpoint, "--wallet", str(leg["wallet_address"]),
            "--worker", str(leg.get("worker_name") or ""), "--password", str(password),
            "--tls", "true" if tls else "false",
            "--keepalive", "true" if extra.get("keepalive", False) else "false"]


def _append_srb_cpu_options(
    argv: list[str], leg: Mapping[str, Any],
    *, secondary: Mapping[str, Any] | None = None,
) -> None:
    extra = validate_managed_extra_config(
        "srbminer", leg.get("extra_config", {}), "primary.extra_config"
    )
    second = (
        validate_managed_extra_config(
            "srbminer", secondary.get("extra_config", {}), "secondary.extra_config"
        ) if secondary is not None else None
    )
    # Upstream documents these per-algorithm defaults. Automatic thread-count
    # slots are not inferred from either default or the other algorithm.
    numeric_defaults = {"cpu_threads_reduce": 0, "cpu_threads_intensity": 1}
    for key, flag in (
        ("cpu_threads", "--cpu-threads"),
        ("cpu_threads_reduce", "--cpu-threads-reduce"),
        ("cpu_threads_intensity", "--cpu-threads-intensity"),
    ):
        value = extra.get(key)
        if second is not None:
            secondary_value = second.get(key)
            if value is not None and secondary_value is not None:
                argv.extend([flag, f"{value};{secondary_value}"])
                continue
            if key in numeric_defaults and (value is not None or secondary_value is not None):
                default = numeric_defaults[key]
                argv.extend([flag, f"{default if value is None else value};{default if secondary_value is None else secondary_value}"])
                continue
        if value is not None:
            argv.extend([flag, str(value)])
    if extra.get("huge_pages") is False:
        argv.append("--disable-huge-pages")
    if extra.get("randomx_1gb_pages", False):
        argv.append("--randomx-use-1gb-pages")
    if extra.get("extended_log", False):
        argv.append("--extended-log")


_SRB_GPU_OC_FLAGS = (
    ("core_clock_lock_mhz", "--gpu-cclock"),
    ("memory_clock_lock_mhz", "--gpu-mclock"),
    ("core_clock_offset_mhz", "--gpu-coffset"),
    ("memory_clock_offset_mhz", "--gpu-moffset"),
    ("power_limit_w", "--gpu-plimit"),
)


def _srb_oc_value(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManagedMinerError(f"{label} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ManagedMinerError(f"{label} must be finite")
    return str(int(numeric)) if numeric.is_integer() else format(numeric, ".10g")


def _append_srb_gpu_oc(
    argv: list[str],
    document: Mapping[str, Any],
    gpu_indices: list[int],
    algorithm_slots: tuple[int, ...],
) -> list[int]:
    """Append documented SRBMiner OC flags aligned with ``--gpu-id`` order."""

    raw_oc = document.get("gpu_oc")
    if raw_oc is None:
        return gpu_indices
    if (
        not isinstance(raw_oc, dict)
        or raw_oc.get("vendor") != "nvidia"
        or set(raw_oc) != {"vendor", "settings"}
        or not isinstance(raw_oc.get("settings"), list)
    ):
        raise ManagedMinerError("managed SRBMiner GPU OC is invalid")
    if not algorithm_slots:
        raise ManagedMinerError("managed SRBMiner GPU OC has no GPU algorithm")

    by_index: dict[int, Mapping[str, Any]] = {}
    allowed_fields = {"index"} | {field for field, _ in _SRB_GPU_OC_FLAGS}
    for position, setting in enumerate(raw_oc["settings"]):
        if not isinstance(setting, dict) or not set(setting) <= allowed_fields:
            raise ManagedMinerError(
                f"managed SRBMiner GPU OC setting {position} is invalid"
            )
        index = setting.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 31:
            raise ManagedMinerError(
                f"managed SRBMiner GPU OC setting {position} has an invalid index"
            )
        if index in by_index or len(setting) == 1:
            raise ManagedMinerError("managed SRBMiner GPU OC settings are invalid")
        by_index[index] = setting

    if not gpu_indices:
        raise ManagedMinerError(
            "managed SRBMiner GPU OC requires explicit --gpu-id values"
        )
    if not set(by_index) <= set(gpu_indices):
        raise ManagedMinerError(
            "managed SRBMiner GPU OC contains a GPU outside --gpu-id"
        )
    if not by_index:
        # Model rules may all be for models absent from this rig.  The caller
        # still emits --gpu-reset-oc, leaving every selected card at defaults.
        return gpu_indices
    control_sets = {frozenset(setting) - {"index"} for setting in by_index.values()}
    if len(control_sets) != 1:
        raise ManagedMinerError(
            "managed SRBMiner GPU OC rows must use the same control fields"
        )
    controls = next(iter(control_sets))
    for slot in algorithm_slots:
        for field, flag in _SRB_GPU_OC_FLAGS:
            if field not in controls:
                continue
            values = [
                (
                    _srb_oc_value(
                        by_index[index][field],
                        f"GPU {index} {field}",
                    )
                    if index in by_index
                    else "0"
                )
                for index in gpu_indices
            ]
            argv.extend([f"{flag}{slot}", ",".join(values)])
    return gpu_indices


def build_srbminer_argv(
    document: Mapping[str, Any], *, executable: Path, api_port: int, log_path: Path
) -> list[str]:
    """Build one SRBMiner argv; no shell text or remote path is accepted."""

    primary = document["primary"]
    validate_managed_extra_config(
        "srbminer", primary.get("extra_config", {}), "primary.extra_config"
    )
    primary_algorithm = validate_managed_algorithm_context(primary)
    primary_resource = leg_effective_resource(primary)
    argv = [str(executable)]
    secondary = document.get("secondary")
    cpu_leg: Mapping[str, Any] | None = None
    gpu_leg: Mapping[str, Any] | None = None
    gpu_algorithm_slots: tuple[int, ...] = ()
    if secondary is None:
        argv.extend(["--algorithm", primary_algorithm])
        primary_with_worker = dict(primary, worker_name=document["worker_name"])
        argv.extend(_srb_connection_args(primary_with_worker))
        if primary_resource == "cpu":
            cpu_leg = primary
            argv.append("--disable-gpu")
        elif primary_resource == "gpu":
            gpu_leg = primary
            gpu_algorithm_slots = (0,)
            argv.append("--disable-cpu")
    else:
        validate_managed_extra_config(
            "srbminer", secondary.get("extra_config", {}), "secondary.extra_config"
        )
        validate_managed_algorithm_context(secondary)
        primary_algorithm, secondary_algorithm = validate_srb_dual_configuration(
            primary, secondary
        )
        secondary_resource = leg_effective_resource(secondary)
        if {primary_resource, secondary_resource} == {"cpu", "gpu"}:
            if primary_resource == "cpu":
                cpu_leg, gpu_leg = primary, secondary
                cpu_algorithm, gpu_algorithm = primary_algorithm, secondary_algorithm
            else:
                cpu_leg, gpu_leg = secondary, primary
                cpu_algorithm, gpu_algorithm = secondary_algorithm, primary_algorithm
            for flag, algorithm, leg in (
                ("--algorithm-cpu", cpu_algorithm, cpu_leg),
                ("--algorithm-gpu", gpu_algorithm, gpu_leg),
            ):
                argv.extend([flag, algorithm])
                argv.extend(
                    _srb_connection_args(
                        dict(leg, worker_name=document["worker_name"])
                    )
                )
            # SRBMiner reports the GPU side of a CPU+GPU launch as A0 and the
            # CPU side as A1, regardless of which flight-sheet leg was
            # entered first.  Its per-algorithm OC suffix must therefore be 0.
            gpu_algorithm_slots = (0,)
        else:
            for algorithm, leg in (
                (primary_algorithm, primary),
                (secondary_algorithm, secondary),
            ):
                argv.extend(["--algorithm", algorithm])
                argv.extend(
                    _srb_connection_args(
                        dict(leg, worker_name=document["worker_name"])
                    )
                )
            if primary_resource == secondary_resource == "gpu":
                gpu_leg = primary
                gpu_algorithm_slots = (0, 1)
                argv.append("--disable-cpu")
            else:
                cpu_leg = primary
                argv.append("--disable-gpu")
    if cpu_leg is not None:
        _append_srb_cpu_options(
            argv, cpu_leg,
            secondary=(secondary if primary_resource == "cpu" and secondary is not None
                       and secondary_resource == "cpu" else None),
        )
    elif primary.get("extra_config", {}).get("extended_log", False):
        argv.append("--extended-log")
    gpu_indices = list(gpu_leg.get("device_indices") or []) if gpu_leg else []
    reset_srb_gpu_oc = document.get("_reset_srb_gpu_oc", False)
    if not isinstance(reset_srb_gpu_oc, bool):
        raise ManagedMinerError("managed SRBMiner reset flag is invalid")
    if document.get("gpu_oc") is not None or reset_srb_gpu_oc:
        argv.append("--gpu-reset-oc")
    gpu_indices = _append_srb_gpu_oc(
        argv, document, gpu_indices, gpu_algorithm_slots
    )
    if gpu_indices:
        argv.extend(["--gpu-id", ",".join(str(item) for item in gpu_indices)])
    argv.extend(
        [
            "--api-enable",
            "--api-port",
            str(api_port),
            "--api-rig-name",
            str(document["worker_name"]),
            "--log-file",
            str(log_path),
            "--log-file-mode",
            "1",
        ]
    )
    # Integrated SRB has one process. Preserve the user's primary/secondary
    # text order even when generated CPU/GPU connections are reordered.
    # Software flags keep upstream process/per-algorithm semantics; no
    # invented leg suffix or secondary argument is silently discarded.
    for role in ("primary", "secondary"):
        leg = document.get(role)
        if isinstance(leg, Mapping):
            argv.extend(parse_extra_args(
                leg.get("extra_config", {}).get("extra_args", ""),
                miner_type="srbminer",
            ))
    return argv


def _write_private_json(
    path: Path, value: Mapping[str, Any], *, durable: bool = True
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(encoded)
            output.flush()
            if durable:
                os.fsync(output.fileno())
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise ManagedMinerError(f"cannot write {path.name}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _snapshot_private_json(path: Path, label: str) -> dict[str, Any] | None:
    """Read a bounded fixed Agent-owned JSON file for transactional rollback."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ManagedMinerError(f"{label} cannot be inspected") from exc
    if (
        _link_or_reparse(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > _MAX_ACTIVE_FLIGHT_SHEET_BYTES
    ):
        raise ManagedMinerError(f"{label} is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManagedMinerError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ManagedMinerError(f"{label} is invalid")
    return value


def _restore_private_json(path: Path, value: Mapping[str, Any] | None) -> None:
    if value is not None:
        _write_private_json(path, value)
        return
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ManagedMinerError(f"cannot inspect {path.name} during rollback") from exc
    if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ManagedMinerError(f"cannot safely roll back {path.name}")
    try:
        path.unlink()
    except OSError as exc:
        raise ManagedMinerError(f"cannot roll back {path.name}") from exc


def _allocate_loopback_port() -> int:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])
    except OSError as exc:
        raise ManagedMinerError("cannot allocate a local miner API port") from exc




_XMRIG_CONTENT_CONFIG_RE = re.compile(r"xmrig-config-[0-9a-f]{64}\.json")


def _write_xmrig_session_config(
    runtime: Path,
    value: Mapping[str, Any],
) -> Path:
    """Write a content-bound XMRig configuration whose path identifies it."""

    canonical = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    path = runtime / (
        f"xmrig-config-{hashlib.sha256(canonical).hexdigest()}.json"
    )
    _write_private_json(path, value)
    return path


def _prune_xmrig_session_configs(
    runtime: Path,
    *,
    keep: Path | None = None,
) -> None:
    """Delete only obsolete fixed-name Agent-owned XMRig config files."""

    keep_normalized = _normalized_path(keep) if keep is not None else None
    try:
        children = list(runtime.iterdir())
    except OSError as exc:
        raise ManagedMinerError(
            "managed XMRig configuration directory cannot be inspected"
        ) from exc
    for path in children:
        if (
            path.name != "xmrig-config.json"
            and _XMRIG_CONTENT_CONFIG_RE.fullmatch(path.name) is None
        ):
            continue
        if keep_normalized is not None and _normalized_path(path) == keep_normalized:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ManagedMinerError(
                "managed XMRig configuration cannot be inspected"
            ) from exc
        if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise ManagedMinerError("managed XMRig configuration is unsafe")
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ManagedMinerError(
                "obsolete managed XMRig configuration cannot be removed"
            ) from exc


def _prepare_xmrig_runtime(
    context: MinerRuntimePrepareContext,
) -> MinerRuntimePreparation:
    xmrig_config = build_xmrig_config(
        context.document,
        api_port=context.api_port,
        log_path=context.miner_log,
    )
    # The immutable content-addressed path participates in the argv
    # fingerprint.  A later flight can no longer rewrite a fixed config
    # beneath the previous live XMRig process.
    config_path = _write_xmrig_session_config(context.runtime, xmrig_config)
    return MinerRuntimePreparation(
        argv=build_xmrig_argv(
            context.executable,
            config_path,
            context.primary,
        ),
        gpu_mapping=[],
        config_path=config_path,
    )


def _prepare_srbminer_runtime(
    context: MinerRuntimePrepareContext,
) -> MinerRuntimePreparation:
    for role in ("primary", "secondary"):
        leg = context.document.get(role)
        if isinstance(leg, Mapping):
            validate_managed_algorithm_context(leg)
    # A declared SRBMiner OC profile is NVIDIA-only and always needs
    # elevation. Reject it before executing package discovery.
    if context.document.get("gpu_oc") is not None and not context.privilege_checker(
        context.system_name
    ):
        raise ManagedMinerError(
            "SRBMiner GPU OC requires the Agent to run as administrator/root"
        )
    srb_document = dict(context.document)
    if context.execution_mode == "wsl":
        # SRBMiner CLI OC is intentionally a Windows-host concern in the first
        # WSL release.  Never pass the internal idempotent reset flag into the
        # guest miner.
        srb_document.pop("_reset_srb_gpu_oc", None)
    runtime_document, gpu_mapping = _resolve_srbminer_gpu_selection(
        srb_document,
        executable=context.executable,
        runner=(
            context.wsl_runtime.run_linux
            if context.wsl_runtime is not None
            else context.device_runner
        ),
        system_name=(
            "Linux" if context.wsl_runtime is not None else context.system_name
        ),
    )
    if context.execution_mode == "wsl":
        non_nvidia = [
            item for item in gpu_mapping if item.get("vendor") != "nvidia"
        ]
        if non_nvidia:
            selected = ", ".join(
                f"GPU{item.get('miner_id')}={str(item.get('name') or 'unknown')[:64]}"
                for item in non_nvidia[:4]
            )
            raise ManagedMinerError(
                "WSL SRBMiner currently supports NVIDIA GPUs only; "
                f"the selected device list includes {selected}"
            )
    reset_requested = runtime_document.get("_reset_srb_gpu_oc") is True
    reset_targets_nvidia = any(
        item.get("vendor") == "nvidia" for item in gpu_mapping
    )
    if reset_requested and not reset_targets_nvidia:
        # SRBMiner documents --gpu-reset-oc as NVIDIA-only. A controller may
        # conservatively request an idempotent reset before device discovery;
        # do not pass that unsupported flag to an AMD/Intel-only flight.
        runtime_document.pop("_reset_srb_gpu_oc", None)
    elif reset_requested and not context.privilege_checker(context.system_name):
        raise ManagedMinerError(
            "SRBMiner GPU OC requires the Agent to run as administrator/root"
        )
    argv = build_srbminer_argv(
        runtime_document,
        executable=context.executable,
        api_port=context.api_port,
        log_path=context.miner_log,
    )
    if context.execution_mode == "wsl":
        # The guest user receives read/execute permission only for verified
        # packages. SYSTEM opens the protected log and passes its handle to
        # WSL, so the guest never needs write access to Agent state/runtime.
        for flag in ("--log-file", "--log-file-mode"):
            try:
                position = argv.index(flag)
            except ValueError:
                continue
            del argv[position : position + 2]
    return MinerRuntimePreparation(
        argv=argv,
        gpu_mapping=gpu_mapping,
    )


def _prepare_pearlminer_runtime(
    context: MinerRuntimePrepareContext,
) -> MinerRuntimePreparation:
    return MinerRuntimePreparation(
        argv=build_pearlminer_argv(context.executable, context.document),
        gpu_mapping=[],
    )


def _prepare_seine_runtime(
    context: MinerRuntimePrepareContext,
) -> MinerRuntimePreparation:
    from .seine_runtime import build_seine_cpu_arguments

    if context.execution_mode != "native" or context.document.get("secondary") is not None:
        raise ManagedMinerError("Seine requires one native CPU/pool leg")
    primary = context.primary
    validate_managed_algorithm_context(primary)
    extra = validate_managed_extra_config("seine", primary.get("extra_config", {}))
    if primary.get("pool_password") not in (None, ""):
        raise ManagedMinerError("Seine pool protocol does not support a pool password")
    # A changed or cleared option must not reload a previous cached setting.
    # The private per-document directory is stable for idempotent launch plans.
    signature = hashlib.sha256(json.dumps(
        {"leg": dict(primary), "worker_name": context.document.get("worker_name")},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    package = primary.get("miner_package")
    version = package.get("version") if isinstance(package, dict) else None
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", version) if isinstance(version, str) else None
    # Legacy synthetic/older package versions preserve the pre-0.2.23 argv
    # shape; authenticated current packages carry a three-part version.
    version_key = tuple(int(part) for part in match.groups()) if match is not None else None
    try:
        arguments = build_seine_cpu_arguments(
            address=str(primary.get("wallet_address") or ""),
            pool_url=str(primary.get("pool_url") or ""),
            worker=str(primary.get("worker_name") or context.document.get("worker_name") or ""),
            data_dir=str(context.runtime / "seine-data" / signature),
            api_port=context.api_port, threads=extra.get("cpu_threads"),
            cpu_profile=extra.get("cpu_profile"),
            disable_update_check=version_key is not None and version_key >= (0, 2, 23),
            disable_telemetry=version_key is not None and version_key >= (0, 2, 26),
        )
        arguments.extend(parse_extra_args(extra.get("extra_args", ""), miner_type="seine"))
    except ValueError as exc:
        raise ManagedMinerError(str(exc)) from exc
    return MinerRuntimePreparation(
        argv=[str(context.executable), *arguments],
        gpu_mapping=[],
        config_path=(
            Path(arguments[arguments.index("--data-dir") + 1]) / "control-api.cookie"
            if version_key is not None and version_key >= (0, 2, 23) else None
        ),
    )


# This is deliberately a source-defined, immutable registry.  Adapter data
# can select a runtime ID from it, but cannot add handlers, import code, or
# cause a package callback or shell command to execute.
TRUSTED_MINER_RUNTIME_HANDLERS = TrustedMinerRuntimeHandlerRegistry(
    {
        "builtin.xmrig.v1": _prepare_xmrig_runtime,
        "builtin.srbminer.v1": _prepare_srbminer_runtime,
        "builtin.pearlminer.v1": _prepare_pearlminer_runtime,
        "builtin.seine.v1": _prepare_seine_runtime,
    }
)
_DECLARED_MINER_RUNTIME_IDS = frozenset(
    adapter.runtime_id for adapter in MINER_ADAPTERS.list()
)
if TRUSTED_MINER_RUNTIME_HANDLERS.runtime_ids != _DECLARED_MINER_RUNTIME_IDS:
    raise RuntimeError(
        "managed miner adapter runtimes and trusted Agent handlers disagree"
    )


def secondary_leg_start_delay_seconds() -> float:
    """Return the configured secondary-leg start delay (>= 0)."""

    try:
        value = float(SECONDARY_LEG_START_DELAY_SECONDS)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0.0 else 0.0


def _pending_secondary_marker_path(runtime: Path) -> Path:
    return runtime / _SECONDARY_START_MARKER_NAME


def _write_pending_secondary_marker(
    runtime: Path, *, flight_sheet_id: str, delay_seconds: float
) -> None:
    now = time.time()
    _write_private_json(
        _pending_secondary_marker_path(runtime),
        {
            "version": 1,
            "flight_sheet_id": flight_sheet_id,
            "delay_seconds": float(delay_seconds),
            "scheduled_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "deadline_epoch": now + float(delay_seconds),
        },
    )


def load_pending_secondary_marker(runtime: Path) -> dict[str, Any] | None:
    """Return a valid, unexpired deferred secondary-start marker, else None."""

    path = _pending_secondary_marker_path(runtime)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if (
        _link_or_reparse(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > _MAX_ACTIVE_FLIGHT_SHEET_BYTES
    ):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    flight_sheet_id = value.get("flight_sheet_id")
    delay = value.get("delay_seconds")
    deadline = value.get("deadline_epoch")
    if (
        value.get("version") != 1
        or not isinstance(flight_sheet_id, str)
        or not 1 <= len(flight_sheet_id) <= 64
        or isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not math.isfinite(float(delay))
        or float(delay) <= 0.0
        or isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
    ):
        return None
    if time.time() > float(deadline) + _SECONDARY_START_MARKER_GRACE_SECONDS:
        return None
    return {
        "flight_sheet_id": flight_sheet_id,
        "delay_seconds": float(delay),
        "deadline_epoch": float(deadline),
    }


def _remove_pending_secondary_marker(
    runtime: Path, *, expected_flight_sheet_id: str | None = None
) -> None:
    """Unlink the durable pending-start marker, optionally identity-checked.

    Today every writer and remover runs on the main thread, so the check is
    defense in depth; if a future change ever moves a cancel or recovery
    path off the main thread, re-reading the marker's ``flight_sheet_id``
    keeps a stale unlink from destroying a newer handle's restart recovery.
    An unreadable or old-format marker is removed as before.
    """

    path = _pending_secondary_marker_path(runtime)
    if expected_flight_sheet_id is not None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if (
            isinstance(record, dict)
            and isinstance(record.get("flight_sheet_id"), str)
            and record["flight_sheet_id"] != expected_flight_sheet_id
        ):
            LOGGER.info(
                "kept a pending secondary start marker owned by a newer handle",
                extra={
                    "event": "secondary_start_marker_kept",
                    "runtime": str(runtime),
                },
            )
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        LOGGER.warning(
            "could not remove the pending secondary start marker",
            extra={
                "event": "secondary_start_marker_remove_failed",
                "runtime": str(runtime),
            },
            exc_info=True,
        )


class _DeferredSecondaryStartHandle:
    __slots__ = ("runtime", "flight_sheet_id", "delay_seconds", "cancel_event", "thread")

    def __init__(
        self, runtime: Path, flight_sheet_id: str, delay_seconds: float
    ) -> None:
        self.runtime = runtime
        self.flight_sheet_id = flight_sheet_id
        self.delay_seconds = delay_seconds
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None


_secondary_start_state_lock = threading.Lock()
_deferred_secondary_start_handle: _DeferredSecondaryStartHandle | None = None


def _schedule_deferred_secondary_start(
    handle: _DeferredSecondaryStartHandle,
    starter: Callable[[_DeferredSecondaryStartHandle], None],
    *,
    packages_dir: Path | None,
) -> None:
    """Arm one deferred secondary start, replacing any previous pending one.

    The durable marker is committed before the waiter thread starts so a
    process crash between the two can never lose the pending launch: startup
    recovery replays it from disk.
    """

    global _deferred_secondary_start_handle
    _write_pending_secondary_marker(
        handle.runtime,
        flight_sheet_id=handle.flight_sheet_id,
        delay_seconds=handle.delay_seconds,
    )
    previous: _DeferredSecondaryStartHandle | None = None

    def runner() -> None:
        if handle.cancel_event.wait(handle.delay_seconds):
            LOGGER.info(
                "deferred secondary start was cancelled before launch",
                extra={
                    "event": "secondary_start_cancelled",
                    "flight_sheet_id": handle.flight_sheet_id,
                },
            )
            return
        # Serialize against flight switches and package-cache cleanup exactly
        # like ManagedMinerManager.apply does, then re-check the cancel flag
        # under that lock so a racing cancel can still win.
        if packages_dir is not None:
            from .miner_cache_cleanup import (
                MinerCacheCleanupError,
                cache_operation_lock,
            )

            try:
                with cache_operation_lock(packages_dir, wait_seconds=30.0):
                    if handle.cancel_event.is_set():
                        return
                    starter(handle)
            except MinerCacheCleanupError:
                LOGGER.error(
                    "miner package cache busy; deferred secondary start left pending",
                    extra={
                        "event": "secondary_start_cache_busy",
                        "flight_sheet_id": handle.flight_sheet_id,
                    },
                )
                return
        else:
            if handle.cancel_event.is_set():
                return
            starter(handle)

    try:
        handle.thread = threading.Thread(
            target=runner,
            name="rigpilot-secondary-start",
            daemon=True,
        )
    except RuntimeError as exc:
        _remove_pending_secondary_marker(
            handle.runtime, expected_flight_sheet_id=handle.flight_sheet_id
        )
        raise ManagedMinerError(
            "deferred secondary start thread cannot be created"
        ) from exc
    with _secondary_start_state_lock:
        previous = _deferred_secondary_start_handle
        _deferred_secondary_start_handle = handle
    if previous is not None and previous is not handle:
        previous.cancel_event.set()
        if previous.thread is not None:
            previous.thread.join(timeout=_SECONDARY_START_CANCEL_JOIN_SECONDS)
    try:
        handle.thread.start()
    except RuntimeError as exc:
        with _secondary_start_state_lock:
            if _deferred_secondary_start_handle is handle:
                _deferred_secondary_start_handle = None
        _remove_pending_secondary_marker(
            handle.runtime, expected_flight_sheet_id=handle.flight_sheet_id
        )
        raise ManagedMinerError(
            "deferred secondary start thread cannot be started"
        ) from exc


def cancel_deferred_secondary_start(reason: str) -> bool:
    """Immediately cancel a pending deferred secondary start.

    Returns True when a pending start existed and was interrupted.  A waiter
    still inside its launch window is asked to stop and given a short, bounded
    join so stop/switch/shutdown command chains are never blocked.
    """

    global _deferred_secondary_start_handle
    with _secondary_start_state_lock:
        handle = _deferred_secondary_start_handle
        _deferred_secondary_start_handle = None
    if handle is None:
        return False
    handle.cancel_event.set()
    if handle.thread is not None:
        handle.thread.join(timeout=_SECONDARY_START_CANCEL_JOIN_SECONDS)
    _remove_pending_secondary_marker(
        handle.runtime, expected_flight_sheet_id=handle.flight_sheet_id
    )
    LOGGER.info(
        "deferred secondary start cancelled",
        extra={
            "event": "secondary_start_cancelled",
            "flight_sheet_id": handle.flight_sheet_id,
            "reason": str(reason)[:128],
        },
    )
    return True


def wait_for_deferred_secondary_start(timeout: float) -> bool:
    """Wait for the armed deferred start thread to finish (test/verification)."""

    with _secondary_start_state_lock:
        handle = _deferred_secondary_start_handle
    if handle is None or handle.thread is None:
        return True
    handle.thread.join(timeout=timeout)
    return not handle.thread.is_alive()


def _finish_deferred_secondary_start(
    handle: _DeferredSecondaryStartHandle,
) -> None:
    """Clear the durable marker when this handle is still the active one."""

    global _deferred_secondary_start_handle
    with _secondary_start_state_lock:
        if _deferred_secondary_start_handle is not handle:
            return
        _deferred_secondary_start_handle = None
    _remove_pending_secondary_marker(
        handle.runtime, expected_flight_sheet_id=handle.flight_sheet_id
    )


class ManagedMinerManager:
    """Start and replace the managed miner processes owned by this Agent."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        process_factory: Callable[[int], Any] = psutil.Process,
        process_iter_factory: Callable[[], Any] = psutil.process_iter,
        boot_time_factory: Callable[[], float] = psutil.boot_time,
        port_allocator: Callable[[], int] = _allocate_loopback_port,
        system_name: str | None = None,
        device_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        cuda_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        privilege_checker: Callable[[str], bool] = has_elevated_miner_privileges,
        enforce_startup_readiness: bool | None = None,
    ) -> None:
        self.config = config
        self._native_srb_base_config = config
        self._native_srb_role = "single"
        self.popen_factory = popen_factory
        self.process_factory = process_factory
        self.process_iter_factory = process_iter_factory
        self.boot_time_factory = boot_time_factory
        self.port_allocator = port_allocator
        self.system_name = system_name or platform.system()
        self.device_runner = device_runner
        self.cuda_runner = cuda_runner
        self.privilege_checker = privilege_checker
        self.enforce_startup_readiness = (
            popen_factory is subprocess.Popen
            and process_factory is psutil.Process
            if enforce_startup_readiness is None
            else bool(enforce_startup_readiness)
        )

    def _runtime(self) -> Path:
        if not self.config.managed_mining_enabled:
            raise ManagedMinerError("managed mining is disabled by local configuration")
        if self.config.managed_miner_runtime_dir is None:
            raise ManagedMinerError("managed miner runtime directory is not configured")
        runtime = self.config.managed_miner_runtime_dir
        try:
            runtime.mkdir(parents=True, exist_ok=True)
            # Inspect the configured path before resolving it.  Resolving first
            # would hide a final-component symlink/junction and allow a role
            # runtime to escape the Agent-owned directory.
            _plain_directory(runtime, "managed miner runtime directory")
            runtime = runtime.resolve(strict=True)
            _plain_directory(runtime, "managed miner runtime directory")
            runtime.chmod(0o700)
        except ManagedMinerError:
            raise
        except OSError as exc:
            raise ManagedMinerError("managed miner runtime directory is unavailable") from exc
        self._reject_legacy_hiveos_journal(runtime)
        return runtime

    @staticmethod
    def _reject_legacy_hiveos_journal(runtime: Path) -> None:
        """Never launch a new workload over a legacy unverified start journal.

        HiveOS session support was removed, so a leftover pending-start journal
        can no longer be verified or aborted programmatically.  Refuse managed
        mining until an administrator removes the fixed journal file after
        confirming no rigpilot hiveos systemd unit is still running.
        """

        pending = _snapshot_private_json(
            runtime / "pending-hiveos-start.json", "pending HiveOS start"
        )
        if pending is not None:
            raise ManagedMinerError(
                "a legacy pending HiveOS start journal is present; verify no "
                "rigpilot hiveos systemd unit is running, then remove "
                "pending-hiveos-start.json manually"
            )

    def _status_path(self, runtime: Path) -> Path:
        return self.config.miner_status_file or (runtime / "miner-status.json")

    @staticmethod
    def _independent(document: Mapping[str, Any]) -> bool:
        return (
            document.get("secondary_mode") == "independent"
            and isinstance(document.get("secondary"), Mapping)
        )

    def _role_manager(self, runtime: Path, role: str) -> "ManagedMinerManager":
        role_runtime = _managed_role_runtime(runtime, role, create=False)
        role_config = replace(
            self.config,
            managed_miner_runtime_dir=role_runtime,
            miner_status_file=role_runtime / "miner-status.json",
            flight_sheet_file=role_runtime / "active-flight-sheet.json",
        )
        manager = ManagedMinerManager(
            role_config,
            popen_factory=self.popen_factory,
            process_factory=self.process_factory,
            process_iter_factory=self.process_iter_factory,
            boot_time_factory=self.boot_time_factory,
            port_allocator=self.port_allocator,
            system_name=self.system_name,
            device_runner=self.device_runner,
            cuda_runner=self.cuda_runner,
            privilege_checker=self.privilege_checker,
            enforce_startup_readiness=self.enforce_startup_readiness,
        )
        manager._native_srb_base_config = self._native_srb_base_config
        manager._native_srb_role = role
        return manager

    def _native_srb_registry_key(self) -> str:
        root = self._native_srb_base_config.miner_packages_dir
        if root is None:
            raise ManagedMinerError("miner package directory is not configured")
        return _normalized_path(root)

    def _release_native_srb_plan(self, prepared: Mapping[str, Any] | None) -> None:
        lease = prepared.get("_native_srb_lease") if prepared is not None else None
        if lease is None:
            return
        plan = None
        with _NATIVE_SRB_PREPARED_LOCK:
            key = self._native_srb_registry_key()
            leases = _NATIVE_SRB_PREPARED.get(key)
            if leases is not None:
                plan = leases.pop(lease, None)
                if not leases:
                    _NATIVE_SRB_PREPARED.pop(key, None)
        # A successful launch has transferred responsibility to its process
        # record. An attempted launch/probe with no positive exit proof stays.
        if plan is not None and plan.phase in {"prepared", "closed"} and plan.probe_closed:
            self._discard_native_srb_run(plan.bundle, plan.runtime, plan.role,
                                         confirmed_stop=plan.phase == "closed")

    def _native_srb_plan(self, prepared: Mapping[str, Any]) -> _NativeSrbPlan | None:
        token = prepared.get("_native_srb_lease")
        if token is None:
            return None
        with _NATIVE_SRB_PREPARED_LOCK:
            return _NATIVE_SRB_PREPARED.get(self._native_srb_registry_key(), {}).get(token)

    def _discard_native_srb_run(self, bundle: Path, runtime: Path, role: str,
                                *, confirmed_stop: bool = False, prior_boot_closed: bool = False) -> None:
        from .miner_cache_cleanup import MinerCacheCleanupError
        try:
            with _NATIVE_SRB_PREPARED_LOCK:
                if any(plan.bundle == bundle and (plan.phase != "closed" or not plan.probe_closed)
                       for plan in _NATIVE_SRB_PREPARED.get(self._native_srb_registry_key(), {}).values()):
                    return
            base = self._native_srb_base_config.managed_miner_runtime_dir
            for directory in (base, base / "primary", base / "secondary"):
                record = self._read_process_state(directory / "managed-miner-process.json")
                if record is not None:
                    for field in ("executable", "target_executable"):
                        value = record.get(field)
                        if isinstance(value, str) and Path(value).is_relative_to(bundle):
                            return
            discard_native_srb_bundle(bundle, self._native_srb_base_config, runtime,
                                      role=role, confirmed_stop=confirmed_stop,
                                      prior_boot_closed=prior_boot_closed)
        except (NativeMinerRuntimeError, MinerCacheCleanupError, ManagedMinerError,
                OSError, ValueError, TypeError):
            LOGGER.warning("native SRBMiner completed run retained because cleanup was not verified",
                           extra={"event": "native_srb_run_retained", "role": role}, exc_info=True)

    def _discard_stopped_native_srb_record(self, record: Mapping[str, Any], runtime: Path,
                                          *, prior_boot_closed: bool = False) -> None:
        value = record.get("executable")
        if record.get("miner_type") != "srbminer" or not isinstance(value, str):
            return
        if self._native_srb_base_config.miner_packages_dir is None:
            return
        for name in (NATIVE_SRB_RUNTIME_DIRECTORY, LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY):
            namespace = self._native_srb_base_config.miner_packages_dir / name
            try:
                relative = Path(value).relative_to(namespace)
            except ValueError:
                continue
            break
        else:
            return
        if (len(relative.parts) < 3 or relative.parts[0] != self._native_srb_role
                or re.fullmatch(r"generation-[0-9a-f]{32}", relative.parts[1]) is None
                or relative.parts[2] != "bundle"):
            return
        bundle = namespace / relative.parts[0] / relative.parts[1] / "bundle"
        with _NATIVE_SRB_PREPARED_LOCK:
            for plan in _NATIVE_SRB_PREPARED.get(self._native_srb_registry_key(), {}).values():
                if plan.bundle == bundle and plan.phase in {"launch_attempted", "committed"}:
                    plan.phase = "closed"
        self._discard_native_srb_run(bundle, runtime, self._native_srb_role, confirmed_stop=True,
                                    prior_boot_closed=prior_boot_closed)

    @contextmanager
    def _native_srb_transaction(self, document: Mapping[str, Any]) -> Iterator[None]:
        primary = document.get("primary")
        if (not isinstance(primary, Mapping) or primary.get("miner_type") != "srbminer"
                or primary.get("execution_mode", "native") != "native"):
            yield
            return
        from .miner_cache_cleanup import cache_operation_lock, MinerCacheCleanupError
        self._native_srb_registry_key()
        try:
            # Direct apply and transient reset also keep preparation-to-record
            # atomic across processes; in-memory leases protect peer managers.
            with cache_operation_lock(self._native_srb_base_config.miner_packages_dir, wait_seconds=30.0):
                yield
        except MinerCacheCleanupError as exc:
            raise ManagedMinerError("miner package cache is busy or unsafe") from exc

    @staticmethod
    def _role_document(
        document: Mapping[str, Any], role: str
    ) -> dict[str, Any]:
        leg = document.get(role)
        if role not in {"primary", "secondary"} or not isinstance(leg, Mapping):
            raise ManagedMinerError("independent managed miner leg is invalid")
        role_document = {
            "version": 3,
            "flight_sheet_id": document.get("flight_sheet_id"),
            "name": document.get("name"),
            "worker_name": document.get("worker_name"),
            "primary": dict(leg),
            "secondary": None,
            "gpu_oc": (
                copy.deepcopy(document.get("gpu_oc"))
                if role == "primary"
                and leg.get("miner_type") == "srbminer"
                and leg_effective_resource(leg) == "gpu"
                and isinstance(document.get("gpu_oc"), Mapping)
                else None
            ),
        }
        runtime_fingerprint = document.get(
            FLIGHT_RUNTIME_FINGERPRINT_FIELD
        )
        if isinstance(runtime_fingerprint, str):
            role_document[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = (
                runtime_fingerprint
            )
        if (
            document.get("_reset_srb_gpu_oc") is True
            and role in _document_srb_gpu_roles(document)
        ):
            role_document["_reset_srb_gpu_oc"] = True
        return role_document

    def _read_active_flight_sheet(self) -> dict[str, Any]:
        path = self.config.flight_sheet_file
        if path is None:
            raise ManagedMinerError("active flight sheet is not configured")
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise ManagedMinerError("active flight sheet does not exist") from exc
        except OSError as exc:
            raise ManagedMinerError("active flight sheet cannot be inspected") from exc
        if (
            _link_or_reparse(info)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > _MAX_ACTIVE_FLIGHT_SHEET_BYTES
        ):
            raise ManagedMinerError("active flight sheet is unsafe")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManagedMinerError("active flight sheet is invalid") from exc
        required = {
            "version",
            "flight_sheet_id",
            "name",
            "worker_name",
            "primary",
            "secondary",
            "applied_at",
        }
        if (
            not isinstance(document, dict)
            or not required <= set(document)
            or set(document)
            - (
                required
                | {
                    "gpu_oc",
                    "secondary_mode",
                    FLIGHT_RUNTIME_FINGERPRINT_FIELD,
                }
            )
            or document.get("version") not in {3, 4}
            or (
                document.get("version") == 3
                and document.get("secondary_mode") is not None
            )
            or (
                document.get("version") == 4
                and document.get("secondary_mode") != "independent"
            )
            or not isinstance(document.get("flight_sheet_id"), str)
            or not document["flight_sheet_id"].strip()
            or len(document["flight_sheet_id"]) > 64
            or not isinstance(document.get("name"), str)
            or not document["name"].strip()
            or len(document["name"]) > 80
            or not isinstance(document.get("worker_name"), str)
            or not document["worker_name"].strip()
            or len(document["worker_name"]) > 128
            or not isinstance(document.get("applied_at"), str)
            or (
                document.get(FLIGHT_RUNTIME_FINGERPRINT_FIELD) is not None
                and (
                    not isinstance(
                        document.get(FLIGHT_RUNTIME_FINGERPRINT_FIELD), str
                    )
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(
                            document.get(
                                FLIGHT_RUNTIME_FINGERPRINT_FIELD
                            )
                        ),
                    )
                    is None
                )
            )
            or not isinstance(document.get("primary"), dict)
            or (
                document.get("secondary") is not None
                and not isinstance(document.get("secondary"), dict)
            )
        ):
            raise ManagedMinerError("active flight sheet has invalid fields")
        return document

    def update_resume_identity(self) -> dict[str, str | None]:
        """Return the configured identity an Agent update may resume.

        This deliberately comes from the already validated active flight
        sheet, not from a stale process record.  It is used only after a
        residue scan has independently proved which live process may be
        stopped.  Keeping those two proofs separate lets an update recover a
        watchdog/PID mismatch without adopting that process as a managed
        miner or inheriting its possibly changed command line.
        """

        document = self._read_active_flight_sheet()
        primary = document.get("primary")
        miner_type = (
            primary.get("miner_type") if isinstance(primary, Mapping) else None
        )
        flight_sheet_id = document.get("flight_sheet_id")
        runtime_fingerprint = document.get(
            FLIGHT_RUNTIME_FINGERPRINT_FIELD
        )
        if (
            miner_type not in MANAGED_MINER_TYPES
            or not isinstance(flight_sheet_id, str)
            or not flight_sheet_id.strip()
            or len(flight_sheet_id) > 64
        ):
            raise ManagedMinerError(
                "active flight sheet has no valid update resume identity"
            )
        return {
            "miner_type": str(miner_type),
            "flight_sheet_id": flight_sheet_id,
            "runtime_fingerprint": runtime_fingerprint,
        }

    def _write_status(
        self,
        runtime: Path,
        document: Mapping[str, Any],
        state: str,
        *,
        miner_type: str,
        api_port: int | None = None,
        pid: int | None = None,
        started_at: str | None = None,
        error: str | None = None,
        gpu_mapping: list[dict[str, Any]] | None = None,
    ) -> None:
        status = {
            "version": 1,
            "state": state,
            "miner_type": miner_type,
            "flight_sheet_id": document.get("flight_sheet_id"),
            "pid": pid,
            "api_host": "127.0.0.1" if api_port else None,
            "api_port": api_port,
            "started_at": started_at,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hashrate": None,
            "shares": None,
            "error": error[:512] if error else None,
        }
        if gpu_mapping:
            # This is Agent-derived local hardware metadata.  It contains no
            # controller input, command text, wallet or pool credentials.
            status["gpu_mapping"] = gpu_mapping
        # Status is an observation; process ownership and recovery records
        # use the durable default of the same writer.
        _write_private_json(self._status_path(runtime), status, durable=False)

    def _package_root(self, leg: Mapping[str, Any], miner_type: str) -> Path:
        if is_retired_miner(miner_type):
            raise ManagedMinerError(f"{miner_type} is retired; select an active miner")
        package = leg.get("miner_package")
        if not isinstance(package, dict):
            raise ManagedMinerError("managed miner requires an installed package")
        package_id = package.get("id")
        if not managed_package_id_matches_miner(package_id, miner_type):
            raise ManagedMinerError(
                f"managed {miner_type} package id must identify a controller-managed "
                "official or test package"
            )
        installed = package.get("installed_path")
        if not isinstance(installed, str) or not installed:
            raise ManagedMinerError("managed miner package was not installed")
        try:
            root = Path(installed).resolve(strict=True)
        except OSError as exc:
            raise ManagedMinerError("installed managed miner package is missing") from exc
        if self.config.miner_packages_dir is None:
            raise ManagedMinerError("miner package directory is not configured")
        try:
            packages_root = self.config.miner_packages_dir.resolve(strict=True)
        except OSError as exc:
            raise ManagedMinerError("miner package directory is unavailable") from exc
        if not _inside(root, packages_root):
            raise ManagedMinerError("managed miner package path is outside miner_packages_dir")
        return root

    def _read_process_state(self, path: Path) -> dict[str, Any] | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ManagedMinerError("managed miner process state cannot be inspected") from exc
        if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 32768:
            raise ManagedMinerError("managed miner process state is unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ManagedMinerError("managed miner process state is invalid") from exc
        if not isinstance(value, dict):
            raise ManagedMinerError("managed miner process state has invalid fields")
        version = value.get("version")
        if value.get("miner_type") == "hiveos" or version == 6:
            # HiveOS sessions were removed.  A legacy supervisor record can no
            # longer be verified or stopped safely, so it must be resolved by
            # an administrator instead of being launched or replayed.
            raise ManagedMinerError(
                "legacy HiveOS managed process state is no longer supported; "
                "verify no rigpilot hiveos systemd unit is running, then "
                "remove the record manually"
            )
        fields = frozenset(value)
        if not (
            (version == 2 and fields == frozenset(_PROCESS_STATE_V2_FIELDS))
            or (version == 3 and fields == frozenset(_PROCESS_STATE_V3_FIELDS))
            or (version == 4 and fields == frozenset(_PROCESS_STATE_V4_FIELDS))
            or (version == 5 and fields == frozenset(_PROCESS_STATE_V5_FIELDS))
            or (version == 7 and fields == frozenset(_PROCESS_STATE_V7_FIELDS))
            or (
                version == 1
                and fields
                in {
                    frozenset(_PROCESS_STATE_V1_FIELDS),
                    frozenset(_LEGACY_PROCESS_STATE_FIELDS),
                }
            )
        ):
            raise ManagedMinerError("managed miner process state has invalid fields")
        created = value.get("create_time")
        try:
            finite_created = (
                not isinstance(created, bool) and isinstance(created, (int, float))
                and math.isfinite(float(created))
            )
        except OverflowError:
            finite_created = False
        if (
            isinstance(value.get("pid"), bool)
            or not isinstance(value.get("pid"), int)
            or value["pid"] <= 0
            or value.get("miner_type") not in _RECORDED_MINER_TYPES
            or not isinstance(value.get("executable"), str)
            or not isinstance(value.get("flight_sheet_id"), str)
            or not isinstance(value.get("started_at"), str)
            or not finite_created
        ):
            raise ManagedMinerError("managed miner process state has invalid values")
        restart_safe = value.get("survives_agent_restart", False)
        if not isinstance(restart_safe, bool):
            raise ManagedMinerError("managed miner process state has invalid values")
        if version in {2, 3, 4, 5, 7} and (
            not isinstance(value.get("argv_sha256"), str)
            or len(value["argv_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in value["argv_sha256"])
            or isinstance(value.get("api_port"), bool)
            or not isinstance(value.get("api_port"), int)
            or not 1024 <= value["api_port"] <= 65535
        ):
            raise ManagedMinerError("managed miner process state has invalid values")
        if version == 7 and (
            value.get("miner_type") != "seine"
            or not isinstance(value.get("seine_data_dir"), str)
            or not Path(value["seine_data_dir"]).is_absolute()
            or len(value["seine_data_dir"]) > 4096
            or "\x00" in value["seine_data_dir"]
        ):
            raise ManagedMinerError("managed Seine process state has invalid values")
        if version in {3, 4, 5} and (
            value.get("execution_mode") != "wsl"
            or not isinstance(value.get("target_executable"), str)
            or not value["target_executable"]
            or len(value["target_executable"]) > 4096
            or not Path(value["target_executable"]).is_absolute()
        ):
            raise ManagedMinerError("managed miner process state has invalid values")
        if version in {4, 5} and (
            not isinstance(value.get("wsl_token"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["wsl_token"]) is None
            or not isinstance(value.get("wsl_guest_executable"), str)
            or not value["wsl_guest_executable"].startswith("/")
            or len(value["wsl_guest_executable"]) > 4096
            or "\r" in value["wsl_guest_executable"]
            or "\n" in value["wsl_guest_executable"]
            or "/../" in f"{value['wsl_guest_executable']}/"
        ):
            raise ManagedMinerError("managed miner process state has invalid values")
        if version in {4, 5}:
            _wsl_identity_from_record(value)
        if version == 5 and (
            not isinstance(value.get("wsl_cache_key"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["wsl_cache_key"]) is None
            or not wsl_cached_executable_matches(
                str(value["wsl_guest_executable"]),
                str(value["wsl_cache_key"]),
            )
            or Path(str(value["wsl_guest_executable"])).name
            not in _recorded_executable_names("Linux", str(value["miner_type"]))
        ):
            raise ManagedMinerError("managed WSL cache identity is invalid")
        # 0.5.6 records did not carry this proof.  Treat them conservatively;
        # an Agent update is not allowed to guess that a Task Scheduler job
        # will preserve the miner after the Agent exits.
        value["survives_agent_restart"] = restart_safe
        return value

    @staticmethod
    def _outside_windows_job(process_handle: Any) -> bool:
        """Prove the child is outside every Windows Job Object.

        CREATE_BREAKAWAY_FROM_JOB succeeding is not proof by itself because
        nested/silent-breakaway policies vary.  IsProcessInJob(NULL) is the
        authoritative post-launch check.  An unavailable handle/API fails
        closed and the process record remains unsafe for Agent restart.
        """

        raw_handle = getattr(process_handle, "_handle", None)
        if raw_handle is None or platform.system() != "Windows":
            return False
        in_job = wintypes.BOOL()
        try:
            succeeded = ctypes.windll.kernel32.IsProcessInJob(
                wintypes.HANDLE(int(raw_handle)),
                None,
                ctypes.byref(in_job),
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        return bool(succeeded) and not bool(in_job.value)

    @staticmethod
    def _unlink_state(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ManagedMinerError("managed miner process state cannot be removed") from exc

    @staticmethod
    def _loopback_api_is_listening(port: int) -> bool:
        """Conservatively detect a miner API that outlived its recorded PID."""

        try:
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=_ORPHAN_API_PROBE_TIMEOUT_SECONDS,
            ):
                return True
        except OSError:
            return False

    def _verified_previous(
        self,
        runtime: Path,
        *,
        clear_definitely_absent: bool = False,
    ) -> tuple[dict[str, Any], Any] | None:
        """Return a proven owner of the recorded previous managed miner.

        Both update handoff and ordinary stop/restart operations must make the
        same PID/path/creation-time decision.  Keeping that proof in one place
        prevents an Agent update from treating a stale PID as a miner that it
        is allowed to stop or later restart.
        """

        state_path = runtime / "managed-miner-process.json"
        record = self._read_process_state(state_path)
        if record is None:
            return None
        if self.config.miner_packages_dir is None:
            raise ManagedMinerError("miner package directory is not configured")
        try:
            packages_root = self.config.miner_packages_dir.resolve(strict=True)
        except OSError as exc:
            raise ManagedMinerError(
                "miner package directory is unavailable"
            ) from exc
        wsl_record = record.get("version") in {3, 4, 5}
        supervised_wsl_record = record.get("version") in {4, 5}
        if wsl_record:
            try:
                recorded_path = Path(record["executable"]).resolve(strict=True)
                expected_wsl = wsl_executable_path()
            except (OSError, WslMiningError) as exc:
                raise ManagedMinerError(
                    "previous WSL managed miner identity cannot be resolved"
                ) from exc
            if _normalized_path(recorded_path) != _normalized_path(expected_wsl):
                raise ManagedMinerError(
                    "previous WSL managed miner launcher path is invalid"
                )
            expected_names = _recorded_executable_names(
                "Linux", str(record["miner_type"])
            )
            raw_target = Path(record["target_executable"])
            try:
                target_path = raw_target.resolve(strict=True)
                target_exists = True
            except FileNotFoundError:
                if record.get("version") != 5:
                    raise ManagedMinerError(
                        "previous WSL miner target cannot be resolved"
                    )
                target_path = raw_target.resolve(strict=False)
                target_exists = False
            except OSError as exc:
                raise ManagedMinerError(
                    "previous WSL miner target cannot be resolved"
                ) from exc
            if (
                target_path.name not in expected_names
                or not _inside(target_path, packages_root)
            ):
                raise ManagedMinerError(
                    "previous WSL miner target is outside miner_packages_dir "
                    "or has an invalid name"
                )
            if target_exists:
                try:
                    target_info = target_path.lstat()
                except OSError as exc:
                    raise ManagedMinerError(
                        "previous WSL miner target cannot be inspected"
                    ) from exc
                if _link_or_reparse(target_info) or not stat.S_ISREG(
                    target_info.st_mode
                ):
                    raise ManagedMinerError("previous WSL miner target is unsafe")
        else:
            expected_names = _recorded_executable_names(
                self.system_name,
                str(record["miner_type"]),
            )
            if Path(record["executable"]).name not in expected_names:
                raise ManagedMinerError(
                    "previous managed miner executable name is invalid"
                )
            recorded_path = Path(record["executable"]).resolve()
            if not _inside(recorded_path, packages_root):
                raise ManagedMinerError(
                    "previous managed miner path is outside miner_packages_dir"
                )
        try:
            recorded_info = recorded_path.lstat()
        except OSError as exc:
            raise ManagedMinerError(
                "previous managed miner executable cannot be inspected"
            ) from exc
        if _link_or_reparse(recorded_info) or not stat.S_ISREG(recorded_info.st_mode):
            raise ManagedMinerError("previous managed miner executable is unsafe")

        def supervised_guest_running() -> bool:
            if not supervised_wsl_record:
                return False
            try:
                return query_wsl_guest(
                    recorded_path,
                    str(record["wsl_token"]),
                    str(record["wsl_guest_executable"]),
                    _wsl_identity_from_record(record),
                )
            except WslMiningError as exc:
                raise ManagedMinerError(str(exc)) from exc

        def scan_replacement():
            return scan_managed_process_replacement(
                state_path=state_path,
                record=record,
                recorded_path=recorded_path,
                process_iter_factory=self.process_iter_factory,
                create_time_tolerance=_CREATE_TIME_TOLERANCE_SECONDS,
            )

        # A record created before the current host boot cannot identify a
        # surviving process.  Do not inspect either its PID (which may have
        # been reused) or same-name processes newly started on this boot.
        # Remove only the unchanged Agent-owned record; any current miner must
        # have its own current-boot record before RigPilot may control it.
        try:
            current_boot_time = float(self.boot_time_factory())
        except (OSError, TypeError, ValueError):
            current_boot_time = math.nan
        if (
            math.isfinite(current_boot_time)
            and current_boot_time > 0
            and float(record["create_time"]) < current_boot_time
        ):
            if remove_managed_process_record_if_unchanged(state_path, record):
                self._discard_stopped_native_srb_record(record, runtime, prior_boot_closed=True)
                return None
            raise ManagedMinerError(
                "managed miner process state changed during preboot cleanup"
            )

        def require_rebound(
            error_message: str | None = None,
            *,
            allow_absence_reconcile: bool = True,
        ) -> tuple[dict[str, Any], Any] | None:
            if wsl_record:
                if not supervised_wsl_record:
                    # Legacy v3 records have no guest token/PID proof.  A
                    # vanished wsl.exe wrapper does not prove that Pearl/SRB
                    # exited inside the distro (Pearl also has no dependable
                    # loopback API).  Never consume this record as an absence
                    # proof or a second instance could be launched.
                    raise ManagedMinerError(
                        "legacy WSL miner launcher exited without a token-bound "
                        "guest identity; refusing to clear its record or start "
                        "a duplicate miner"
                    )
                if supervised_wsl_record and supervised_guest_running():
                    # The one-to-one host wrapper may already have disappeared,
                    # but the token record still proves the exact guest child.
                    # Return a None host handle so stop() uses guest control
                    # rather than guessing another wsl.exe process.
                    return record, None
                if clear_definitely_absent and allow_absence_reconcile:
                    raw_api_port = record.get("api_port")
                    api_port = (
                        raw_api_port
                        if isinstance(raw_api_port, int)
                        and not isinstance(raw_api_port, bool)
                        and 1024 <= raw_api_port <= 65535
                        else None
                    )
                    if api_port is not None and self._loopback_api_is_listening(
                        api_port
                    ):
                        raise ManagedMinerError(
                            "previous WSL miner API is still listening; refusing "
                            "to start a duplicate miner"
                        )
                    time.sleep(_ORPHAN_CONFIRMATION_SECONDS)
                    if supervised_wsl_record and supervised_guest_running():
                        return record, None
                    if api_port is not None and self._loopback_api_is_listening(
                        api_port
                    ):
                        raise ManagedMinerError(
                            "previous WSL miner API became reachable during "
                            "orphan confirmation"
                        )
                    if remove_managed_process_record_if_unchanged(
                        state_path, record
                    ):
                        return None
                    raise ManagedMinerError(
                        "managed WSL miner process state changed during cleanup"
                    )
                raise ManagedMinerError(
                    error_message
                    or (
                        "previous WSL miner launcher exited; refusing to guess "
                        "or terminate another WSL process"
                    )
                )
            scan = scan_replacement()
            if scan.status == "rebound" and scan.rebound is not None:
                return scan.rebound
            if (
                clear_definitely_absent
                and allow_absence_reconcile
                and scan.status == "definitely_absent"
            ):
                # A watchdog replacement can appear after the original PID has
                # already vanished.  Observe the complete process table and the
                # fixed loopback API port for a full second before consuming an
                # absence proof.  The final CAS delete also proves that no
                # concurrent Agent thread rebound or replaced the record.
                raw_api_port = record.get("api_port")
                api_port = (
                    raw_api_port
                    if isinstance(raw_api_port, int)
                    and not isinstance(raw_api_port, bool)
                    and 1024 <= raw_api_port <= 65535
                    else None
                )
                deadline = time.monotonic() + _ORPHAN_CONFIRMATION_SECONDS
                confirmation = scan
                while True:
                    if (
                        api_port is not None
                        and self._loopback_api_is_listening(api_port)
                    ):
                        raise ManagedMinerError(
                            "previous managed miner API is still listening; "
                            "refusing to start a duplicate miner"
                        )
                    if (
                        confirmation.status == "rebound"
                        and confirmation.rebound is not None
                    ):
                        return confirmation.rebound
                    if confirmation.status != "definitely_absent":
                        raise ManagedMinerError(
                            "previous managed miner identity became ambiguous "
                            "during orphan confirmation; refusing to start a "
                            "duplicate miner"
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if remove_managed_process_record_if_unchanged(
                            state_path,
                            record,
                        ):
                            self._discard_stopped_native_srb_record(record, runtime)
                            return None
                        raise ManagedMinerError(
                            "managed miner process state changed during orphan cleanup"
                        )
                    time.sleep(
                        min(_ORPHAN_CONFIRMATION_POLL_SECONDS, remaining)
                    )
                    confirmation = scan_replacement()
            raise ManagedMinerError(
                error_message
                or (
                    "previous managed miner exited or restarted itself, but no unique "
                    "exact replacement could be verified; refusing to start a duplicate "
                    "miner (inspect/stop the miner manually or reboot the host)"
                )
            )

        try:
            process = self.process_factory(record["pid"])
        except psutil.NoSuchProcess:
            return require_rebound()
        except psutil.Error as exc:
            raise ManagedMinerError("previous managed miner cannot be inspected") from exc
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return require_rebound()
            actual_executable = process.exe()
            actual_create_time = float(process.create_time())
        except psutil.NoSuchProcess:
            return require_rebound()
        except (psutil.Error, OSError, ValueError) as exc:
            raise ManagedMinerError("previous managed miner identity cannot be read") from exc

        if _normalized_path(actual_executable) != _normalized_path(recorded_path):
            return require_rebound(
                "previous managed miner PID now belongs to another path; refusing "
                "to start a duplicate miner",
                allow_absence_reconcile=False,
            )
        if (
            not math.isfinite(actual_create_time)
            or abs(actual_create_time - float(record["create_time"]))
            > _CREATE_TIME_TOLERANCE_SECONDS
        ):
            return require_rebound(
                "previous managed miner PID creation time no longer matches; "
                "refusing to start a duplicate miner"
            )
        try:
            argv_matches = managed_process_argv_matches_record(
                process, record=record, recorded_path=recorded_path,
            )
        except psutil.NoSuchProcess:
            return require_rebound()
        except (psutil.Error, OSError, TypeError, ValueError, AttributeError) as exc:
            raise ManagedMinerError("previous managed miner argv cannot be inspected") from exc
        if not argv_matches:
            return require_rebound(
                "previous managed miner invocation no longer matches; refusing "
                "to stop an unrelated invocation",
                allow_absence_reconcile=False,
            )
        if supervised_wsl_record and not supervised_guest_running():
            if clear_definitely_absent:
                # The exact wrapper remains and can be reaped by stop(), but it
                # no longer represents a running guest miner.
                return record, process
            raise ManagedMinerError(
                "previous WSL miner guest process is no longer running"
            )
        return record, process

    @staticmethod
    def _partial_invalid_residue_record(path: Path, error: str) -> dict[str, Any] | None:
        """Extract fixed identity fields from a parseable invalid record."""
        if "unsafe" in error or "cannot be inspected" in error:
            return None
        try:
            info = path.lstat()
            if (
                _link_or_reparse(info)
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > 32768
            ):
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        version = value.get("version")
        pid = value.get("pid")
        miner_type = value.get("miner_type")
        executable = value.get("executable")
        flight_sheet_id = value.get("flight_sheet_id")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or miner_type not in _RECORDED_MINER_TYPES
            or not isinstance(executable, str)
            or not executable
            or len(executable) > 4096
            or "\x00" in executable
            or not Path(executable).is_absolute()
            or not isinstance(flight_sheet_id, str)
            or not flight_sheet_id.strip()
            or len(flight_sheet_id) > 64
            or not isinstance(value.get("started_at"), str)
        ):
            return None
        return dict(value)

    def _invalid_residue_process_absence_proven(
        self, record: Mapping[str, Any]
    ) -> bool:
        """Require a complete same-name process-table absence proof."""
        miner_type = str(record["miner_type"])
        expected_names = _recorded_executable_names(self.system_name, miner_type)
        if not expected_names or self.config.miner_packages_dir is None:
            return False
        try:
            packages_root = self.config.miner_packages_dir.resolve(strict=True)
            recorded_path = Path(str(record["executable"])).resolve(strict=False)
            if (
                not recorded_path.is_absolute()
                or recorded_path.name not in expected_names
                or not _inside(recorded_path, packages_root)
            ):
                return False
            processes = list(self.process_iter_factory())
        except (OSError, TypeError, ValueError, psutil.Error):
            return False
        normalized_recorded = _normalized_path(recorded_path)
        for process in processes:
            try:
                executable = process.exe()
            except psutil.NoSuchProcess:
                continue
            except (psutil.Error, OSError):
                return False
            if not isinstance(executable, str) or not executable:
                return False
            candidate = Path(executable)
            if (
                candidate.name in expected_names
                or _normalized_path(candidate) == normalized_recorded
            ):
                return False
        return True

    @staticmethod
    def _residue_target_fingerprint(
        role: str,
        finding: Mapping[str, Any],
        record: Mapping[str, Any] | None,
    ) -> str:
        material = {
            "role": role,
            "finding": dict(finding),
            # The record contains no argv, wallet, password or pool secret.
            # Its v2 argv_sha256 binds the complete invocation without
            # disclosing it.
            "record": dict(record) if record is not None else None,
        }
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _residue_candidate_fingerprint(
        role: str,
        record: Mapping[str, Any],
        recorded_path: Path,
        candidate: ManagedProcessCandidate,
    ) -> str:
        """Bind a manual choice to one secret-free process snapshot."""

        material = {
            "role": role,
            "record": dict(record),
            "candidate": {
                "pid": candidate.pid,
                "create_time": candidate.create_time,
                "executable": _normalized_path(recorded_path),
                "argv_sha256": candidate.argv_sha256,
                "members": [
                    {
                        "pid": member.pid,
                        "create_time": member.create_time,
                        "argv_sha256": member.argv_sha256,
                        "ppid": member.ppid,
                        "sid": member.sid,
                        "pgid": member.pgid,
                    }
                    for member in (
                        candidate.members
                        if candidate.members
                        else (candidate,)
                    )
                ],
            },
        }
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def _probe_residue_target(
        self,
        role: str,
        runtime: Path,
    ) -> dict[str, Any]:
        """Classify one fixed process record without killing or deleting.

        A v2 watchdog replacement is recognized with exact executable and
        complete argv-fingerprint proof, but this scan deliberately does not
        rebind the process record.  That keeps the scan fingerprint stable
        until an explicitly authorized cleanup rechecks it.
        """

        state_path = runtime / "managed-miner-process.json"
        finding: dict[str, Any] = {
            "role": role,
            "status": "no_record",
            "miner_type": None,
            "flight_sheet_id": None,
            "record_pid": None,
            "verified_pid": None,
            "process_verified": False,
            "clearable": False,
            "reason": "no managed miner process record",
            "manual_candidates": [],
        }
        try:
            record = self._read_process_state(state_path)
        except ManagedMinerError as exc:
            finding.update(
                status="ambiguous",
                reason=str(exc),
            )
            record = None
            partial = self._partial_invalid_residue_record(state_path, str(exc))
            if partial is not None:
                finding.update(
                    miner_type=str(partial["miner_type"]),
                    flight_sheet_id=str(partial["flight_sheet_id"]),
                    record_pid=int(partial["pid"]),
                )
                if self._invalid_residue_process_absence_proven(partial):
                    finding.update(
                        status="invalid_record",
                        clearable=True,
                        reason=(
                            "invalid managed miner record has no live trusted-path "
                            "process candidate"
                        ),
                    )
                else:
                    finding.update(
                        reason=(
                            "invalid managed miner record is ambiguous or a "
                            "trusted-path process candidate is live"
                        ),
                    )
                record = partial
                return {
                    "role": role,
                    "runtime": runtime,
                    "state_path": state_path,
                    "record": record,
                    "finding": finding,
                    "target_fingerprint": self._residue_target_fingerprint(
                        role, finding, record
                    ),
                }
        if record is None:
            return {
                "role": role,
                "runtime": runtime,
                "state_path": state_path,
                "record": None,
                "finding": finding,
                "target_fingerprint": self._residue_target_fingerprint(
                    role, finding, None
                ),
            }

        finding.update(
            miner_type=str(record["miner_type"]),
            flight_sheet_id=str(record["flight_sheet_id"]),
            record_pid=int(record["pid"]),
        )
        wsl_record = record.get("version") in {3, 4, 5}
        try:
            if self.config.miner_packages_dir is None:
                raise ManagedMinerError(
                    "miner package directory is not configured"
                )
            packages_root = self.config.miner_packages_dir.resolve(strict=True)
            if wsl_record:
                recorded_path = Path(record["executable"]).resolve(strict=True)
                if _normalized_path(recorded_path) != _normalized_path(
                    wsl_executable_path()
                ):
                    raise ManagedMinerError(
                        "managed WSL launcher path is invalid"
                    )
                expected_names = _recorded_executable_names(
                    "Linux", str(record["miner_type"])
                )
                raw_target = Path(record["target_executable"])
                try:
                    target_path = raw_target.resolve(strict=True)
                    target_exists = True
                except FileNotFoundError:
                    if record.get("version") != 5:
                        raise
                    target_path = raw_target.resolve(strict=False)
                    target_exists = False
                if (
                    target_path.name not in expected_names
                    or not _inside(target_path, packages_root)
                ):
                    raise ManagedMinerError(
                        "managed WSL target is outside miner_packages_dir or "
                        "has an invalid name"
                    )
                if target_exists:
                    target_info = target_path.lstat()
                    if _link_or_reparse(target_info) or not stat.S_ISREG(
                        target_info.st_mode
                    ):
                        raise ManagedMinerError("managed WSL target is unsafe")
            else:
                expected_names = _recorded_executable_names(
                    self.system_name,
                    str(record["miner_type"]),
                )
                if Path(record["executable"]).name not in expected_names:
                    raise ManagedMinerError(
                        "managed miner executable name is invalid"
                    )
                recorded_path = Path(record["executable"]).resolve()
                if not _inside(recorded_path, packages_root):
                    raise ManagedMinerError(
                        "managed miner executable is outside miner_packages_dir"
                    )
            recorded_info = recorded_path.lstat()
            if _link_or_reparse(recorded_info) or not stat.S_ISREG(
                recorded_info.st_mode
            ):
                raise ManagedMinerError("managed miner executable is unsafe")
        except (ManagedMinerError, WslMiningError, OSError) as exc:
            finding.update(status="ambiguous", reason=str(exc))
            return {
                "role": role,
                "runtime": runtime,
                "state_path": state_path,
                "record": record,
                "finding": finding,
                "target_fingerprint": self._residue_target_fingerprint(
                    role, finding, record
                ),
            }

        manual_candidate: ManagedProcessCandidate | None = None

        def replacement_finding() -> None:
            nonlocal manual_candidate
            if wsl_record:
                if record.get("version") == 3:
                    finding.update(
                        status="ambiguous",
                        reason="legacy WSL launcher absence cannot prove guest miner absence",
                    )
                    return
                if record.get("version") in {4, 5}:
                    try:
                        guest_running = query_wsl_guest(
                            recorded_path,
                            str(record["wsl_token"]),
                            str(record["wsl_guest_executable"]),
                            _wsl_identity_from_record(record),
                        )
                    except (ManagedMinerError, WslMiningError) as exc:
                        finding.update(status="ambiguous", reason=str(exc))
                        return
                    if guest_running:
                        finding.update(
                            status="verified_process",
                            verified_pid=int(record["pid"]),
                            process_verified=True,
                            clearable=True,
                            reason=(
                                "token-bound WSL guest miner is verified; its "
                                "host wrapper is absent"
                            ),
                        )
                        return
                finding.update(
                    status="orphan_record",
                    clearable=True,
                    reason=(
                        "managed WSL launcher is absent; no replacement WSL "
                        "process was guessed"
                    ),
                )
                return
            scan = inspect_managed_process_replacement(
                state_path=state_path,
                record=record,
                recorded_path=recorded_path,
                process_iter_factory=self.process_iter_factory,
                create_time_tolerance=_CREATE_TIME_TOLERANCE_SECONDS,
            )
            if scan.status == "exact_replacement" and scan.rebound is not None:
                replacement, _process = scan.rebound
                finding.update(
                    status="exact_replacement",
                    verified_pid=int(replacement["pid"]),
                    process_verified=True,
                    clearable=True,
                    reason=scan.reason,
                )
            elif scan.status == "definitely_absent":
                finding.update(
                    status="orphan_record",
                    clearable=True,
                    reason=scan.reason,
                )
            else:
                finding.update(
                    status="ambiguous",
                    reason=scan.reason or "managed miner identity is ambiguous",
                )
                if scan.explicit_candidate is not None:
                    manual_candidate = scan.explicit_candidate
                    candidate_fingerprint = self._residue_candidate_fingerprint(
                        role,
                        record,
                        recorded_path,
                        scan.explicit_candidate,
                    )
                    finding["manual_candidates"] = [
                        {
                            "pid": scan.explicit_candidate.pid,
                            "candidate_fingerprint": candidate_fingerprint,
                        }
                    ]

        try:
            current_boot_time = float(self.boot_time_factory())
        except (OSError, TypeError, ValueError):
            current_boot_time = math.nan
        if (
            math.isfinite(current_boot_time)
            and float(record["create_time"]) < current_boot_time
        ):
            # Never inspect either the old PID or new-boot same-name processes.
            # Cleanup may only CAS-delete this unchanged old record.
            finding.update(
                status="preboot_record",
                clearable=True,
                reason="managed miner record predates the current host boot",
            )
        else:
            try:
                process = self.process_factory(int(record["pid"]))
            except psutil.NoSuchProcess:
                replacement_finding()
            except psutil.Error as exc:
                finding.update(
                    status="ambiguous",
                    reason=f"managed miner process cannot be inspected: {exc}",
                )
            else:
                try:
                    live = (
                        process.is_running()
                        and process.status() != psutil.STATUS_ZOMBIE
                    )
                    executable = process.exe() if live else None
                    created = float(process.create_time()) if live else math.nan
                    argv_matches = (
                        managed_process_argv_matches_record(
                            process,
                            record=record,
                            recorded_path=recorded_path,
                        )
                        if live
                        else False
                    )
                except (
                    psutil.NoSuchProcess,
                    psutil.Error,
                    OSError,
                    TypeError,
                    ValueError,
                    AttributeError,
                ):
                    finding.update(
                        status="ambiguous",
                        reason="managed miner process identity cannot be read",
                    )
                else:
                    if (
                        live
                        and _normalized_path(str(executable))
                        == _normalized_path(recorded_path)
                        and math.isfinite(created)
                        and abs(created - float(record["create_time"]))
                        <= _CREATE_TIME_TOLERANCE_SECONDS
                        and argv_matches
                    ):
                        finding.update(
                            status="verified_process",
                            verified_pid=int(record["pid"]),
                            process_verified=True,
                            clearable=True,
                            reason="recorded PID identity is verified",
                        )
                    else:
                        replacement_finding()

        return {
            "role": role,
            "runtime": runtime,
            "state_path": state_path,
            "record": record,
            "recorded_path": recorded_path,
            "manual_candidate": manual_candidate,
            "finding": finding,
            "target_fingerprint": self._residue_target_fingerprint(
                role, finding, record
            ),
        }

    def _scan_residue_internal(self) -> dict[str, Any]:
        runtime = self._runtime()
        probes: list[dict[str, Any]] = [
            self._probe_residue_target("legacy", runtime)
        ]
        for role in ("primary", "secondary"):
            try:
                role_runtime = _managed_role_runtime(
                    runtime,
                    role,
                    create=False,
                )
            except ManagedMinerError as exc:
                finding = {
                    "role": role,
                    "status": "ambiguous",
                    "miner_type": None,
                    "flight_sheet_id": None,
                    "record_pid": None,
                    "verified_pid": None,
                    "process_verified": False,
                    "clearable": False,
                    "reason": str(exc),
                    "manual_candidates": [],
                }
                probes.append(
                    {
                        "role": role,
                        "runtime": runtime / role,
                        "state_path": runtime
                        / role
                        / "managed-miner-process.json",
                        "record": None,
                        "finding": finding,
                        "target_fingerprint": self._residue_target_fingerprint(
                            role, finding, None
                        ),
                    }
                )
                continue
            probes.append(self._probe_residue_target(role, role_runtime))

        # One live PID must never be offered as a manual candidate for more
        # than one fixed runtime slot (or while another slot already verified
        # that PID).  Independent slot records can be stale in overlapping
        # ways; assigning the process to the first role would be arbitrary.
        # Remove every conflicting offer and bind the revised finding into a
        # new target fingerprint so the controller cannot replay the earlier
        # per-role snapshot.
        pid_owners: dict[int, list[dict[str, Any]]] = {}
        for probe in probes:
            finding = probe["finding"]
            verified_pid = finding.get("verified_pid")
            if isinstance(verified_pid, int) and not isinstance(
                verified_pid, bool
            ):
                pid_owners.setdefault(verified_pid, []).append(probe)
            manual_candidates = finding.get("manual_candidates")
            if isinstance(manual_candidates, list):
                for candidate in manual_candidates:
                    if isinstance(candidate, Mapping):
                        pid = candidate.get("pid")
                        if isinstance(pid, int) and not isinstance(pid, bool):
                            pid_owners.setdefault(pid, []).append(probe)
        conflicting_manual_probes: dict[int, dict[str, Any]] = {}
        for owners in pid_owners.values():
            if len({id(owner) for owner in owners}) <= 1:
                continue
            for owner in owners:
                if owner["finding"].get("manual_candidates"):
                    conflicting_manual_probes[id(owner)] = owner
        for probe in conflicting_manual_probes.values():
            probe["finding"]["manual_candidates"] = []
            probe["finding"]["reason"] = (
                "same live process matches multiple managed runtime slots"
            )
            probe["manual_candidate"] = None
            probe["target_fingerprint"] = self._residue_target_fingerprint(
                str(probe["role"]),
                probe["finding"],
                probe["record"],
            )
        clearable_count = sum(
            1 for probe in probes if probe["finding"]["clearable"]
        )
        ambiguous_count = sum(
            1
            for probe in probes
            if probe["finding"]["status"] == "ambiguous"
        )
        fingerprint_material = [
            {
                "role": probe["role"],
                "target_fingerprint": probe["target_fingerprint"],
            }
            for probe in probes
        ]
        scan_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_material,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).hexdigest()
        return {
            "schema_version": 3,
            "scan_fingerprint": scan_fingerprint,
            # A clean, no-record scan is still a valid cleanup transaction:
            # the command handler may need to reset persisted NVML/SRBMiner OC
            # even though there is no process record left to remove.
            # Version 2 supports choosing individual clearable targets.  An
            # ambiguous, unselected role must not block another proven role,
            # and an OC-only transaction remains available with no records.
            "cleanup_available": True,
            "clearable_count": clearable_count,
            "ambiguous_count": ambiguous_count,
            "findings": [
                {
                    **dict(probe["finding"]),
                    "target_fingerprint": probe["target_fingerprint"],
                }
                for probe in probes
            ],
            "_probes": probes,
        }

    def scan_residue(self) -> dict[str, Any]:
        """Return a secret-free, non-destructive managed-miner residue scan."""

        report = self._scan_residue_internal()
        report.pop("_probes", None)
        return report

    @staticmethod
    def _stop_explicit_residue_candidate(
        candidate: ManagedProcessCandidate,
        recorded_path: Path,
    ) -> bool:
        """Stop exactly one reverified operator-selected process snapshot."""

        if not managed_process_candidate_is_current(
            candidate,
            recorded_path=recorded_path,
            create_time_tolerance=_CREATE_TIME_TOLERANCE_SECONDS,
        ):
            raise ManagedMinerError(
                "explicit cleanup candidate changed before termination"
            )
        members = list(candidate.members) if candidate.members else [candidate]
        # Stop descendants before their session/group leader so the parent
        # cannot leave an already-known worker behind while it exits.
        members.sort(
            key=lambda member: (
                member.pid == candidate.pid,
                -member.pid,
            )
        )
        terminated: list[Any] = []
        for member in members:
            try:
                member.process.terminate()
                terminated.append(member.process)
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as exc:
                error_type = (
                    ManagedMinerTransitionError if terminated else ManagedMinerError
                )
                if error_type is ManagedMinerTransitionError:
                    raise error_type(
                        "explicit cleanup process group termination was incomplete",
                        state_changed=True,
                        old_stopped=True,
                    ) from exc
                raise error_type(
                    "explicit cleanup process group could not be stopped"
                ) from exc
        for process in terminated:
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
                except psutil.Error as exc:
                    raise ManagedMinerTransitionError(
                        "explicit cleanup process group termination was incomplete",
                        state_changed=True,
                        old_stopped=True,
                    ) from exc
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                raise ManagedMinerTransitionError(
                    "explicit cleanup process group changed while stopping",
                    state_changed=True,
                    old_stopped=True,
                ) from exc
        return bool(terminated)

    def cleanup_residue(
        self,
        scan_fingerprint: str,
        targets: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Clean only residue bound to an unchanged, prior scan.

        An ambiguous same-name process is touched only when the operator chose
        the unique candidate from this exact scan.  That candidate is rebound
        to PID/path/create-time and its observed argv fingerprint immediately
        before termination.  Automatic cleanup retains the stricter recorded
        argv proof.  Every record removal remains a compare-and-swap operation.
        """

        if (
            not isinstance(scan_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", scan_fingerprint) is None
        ):
            raise ManagedMinerError("residue scan fingerprint is invalid")
        report = self._scan_residue_internal()
        probes = report.pop("_probes")
        if report["scan_fingerprint"] != scan_fingerprint:
            raise ManagedMinerError(
                "managed miner residue changed after scan; scan again"
            )
        results: list[dict[str, Any]] = []
        partial_mutation = False
        # ``None`` is the 0.6.50 compatibility contract: clean every
        # clearable role, but keep the old fail-closed global ambiguity
        # behavior.  New callers always send an explicit list, including an
        # empty list for an OC-only transaction.
        legacy_select_all = targets is None
        if legacy_select_all and report["ambiguous_count"]:
            return {
                **report,
                "blocked": True,
                "changed": False,
                "processes_stopped": 0,
                "records_removed": 0,
                "results": results,
                "selected_targets": [],
            }

        probes_by_role = {str(probe["role"]): probe for probe in probes}
        if legacy_select_all:
            selected_probes = [
                probe for probe in probes if probe["finding"]["clearable"]
            ]
        else:
            if not isinstance(targets, Sequence) or isinstance(
                targets, (str, bytes, bytearray)
            ):
                raise ManagedMinerError(
                    "residue cleanup targets must be a list"
                )
            selected_probes = []
            seen_roles: set[str] = set()
            for target in targets:
                if not isinstance(target, Mapping) or set(target) not in (
                    {"role", "target_fingerprint"},
                    {
                        "role",
                        "target_fingerprint",
                        "candidate_fingerprint",
                    },
                ):
                    raise ManagedMinerError(
                        "residue cleanup target is invalid"
                    )
                role = target.get("role")
                fingerprint = target.get("target_fingerprint")
                candidate_fingerprint = target.get("candidate_fingerprint")
                if (
                    role not in {"legacy", "primary", "secondary"}
                    or role in seen_roles
                    or not isinstance(fingerprint, str)
                    or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
                    or (
                        candidate_fingerprint is not None
                        and (
                            not isinstance(candidate_fingerprint, str)
                            or re.fullmatch(
                                r"[0-9a-f]{64}", candidate_fingerprint
                            )
                            is None
                        )
                    )
                ):
                    raise ManagedMinerError(
                        "residue cleanup target is invalid"
                    )
                probe = probes_by_role.get(str(role))
                if probe is None or probe["target_fingerprint"] != fingerprint:
                    raise ManagedMinerError(
                        f"residue cleanup target {role} is not selectable; "
                        "scan again"
                    )
                if candidate_fingerprint is None:
                    selectable = bool(
                        probe["finding"]["status"] != "ambiguous"
                        and probe["finding"]["clearable"]
                    )
                else:
                    manual_candidates = probe["finding"].get(
                        "manual_candidates"
                    )
                    selectable = bool(
                        probe["finding"]["status"] == "ambiguous"
                        and probe["finding"]["clearable"] is False
                        and probe.get("manual_candidate") is not None
                        and isinstance(manual_candidates, list)
                        and manual_candidates
                        == [
                            {
                                "pid": probe["manual_candidate"].pid,
                                "candidate_fingerprint": candidate_fingerprint,
                            }
                        ]
                    )
                if not selectable:
                    raise ManagedMinerError(
                        f"residue cleanup target {role} is not selectable; "
                        "scan again"
                    )
                seen_roles.add(str(role))
                probe["selected_candidate_fingerprint"] = candidate_fingerprint
                selected_probes.append(probe)
        selected_targets = [
            {
                "role": str(probe["role"]),
                "target_fingerprint": str(probe["target_fingerprint"]),
                **(
                    {
                        "candidate_fingerprint": str(
                            probe["selected_candidate_fingerprint"]
                        )
                    }
                    if probe.get("selected_candidate_fingerprint") is not None
                    else {}
                ),
            }
            for probe in selected_probes
        ]

        # Complete a non-mutating preflight for every selected role before
        # stopping any process.  Without this pass, a change in the second
        # selected role could be discovered only after the first role had
        # already been stopped, creating an avoidable partial cleanup.
        blocked = False
        preflight: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for original_probe in selected_probes:
            current_probe = self._probe_residue_target(
                str(original_probe["role"]),
                original_probe["runtime"],
            )
            if (
                current_probe["target_fingerprint"]
                != original_probe["target_fingerprint"]
            ):
                blocked = True
                results.append(
                    {
                        "role": original_probe["role"],
                        "status": "changed_after_scan",
                        "process_stopped": False,
                        "record_removed": False,
                        "reason": (
                            "managed miner residue changed immediately before cleanup"
                        ),
                    }
                )
                break
            selected_candidate_fingerprint = original_probe.get(
                "selected_candidate_fingerprint"
            )
            if selected_candidate_fingerprint is not None:
                current_candidates = current_probe["finding"].get(
                    "manual_candidates"
                )
                current_candidate = current_probe.get("manual_candidate")
                if (
                    current_candidate is None
                    or current_candidates
                    != [
                        {
                            "pid": current_candidate.pid,
                            "candidate_fingerprint": (
                                selected_candidate_fingerprint
                            ),
                        }
                    ]
                ):
                    blocked = True
                    results.append(
                        {
                            "role": original_probe["role"],
                            "status": "changed_after_scan",
                            "process_stopped": False,
                            "record_removed": False,
                            "reason": (
                                "explicit cleanup candidate changed "
                                "immediately before cleanup"
                            ),
                        }
                    )
                    break
            record = current_probe["record"]
            if record is None:
                blocked = True
                results.append(
                    {
                        "role": original_probe["role"],
                        "status": "changed_after_scan",
                        "process_stopped": False,
                        "record_removed": False,
                        "reason": "managed miner process record disappeared",
                    }
                )
                break
            preflight.append((original_probe, current_probe))

        if not blocked:
            for original_probe, current_probe in preflight:
                finding = current_probe["finding"]
                record = current_probe["record"]
                if record is None:
                    # Kept as a defensive invariant even though the preflight
                    # above already rejects this condition.
                    raise ManagedMinerError(
                        "managed miner process record disappeared after preflight"
                    )
                process_stopped = False
                record_removed = False
                try:
                    selected_candidate_fingerprint = original_probe.get(
                        "selected_candidate_fingerprint"
                    )
                    if selected_candidate_fingerprint is not None:
                        candidate = current_probe.get("manual_candidate")
                        recorded_path = current_probe.get("recorded_path")
                        if not isinstance(candidate, ManagedProcessCandidate) or not isinstance(
                            recorded_path, Path
                        ):
                            raise ManagedMinerError(
                                "explicit cleanup candidate is unavailable"
                            )
                        process_stopped = self._stop_explicit_residue_candidate(
                            candidate,
                            recorded_path,
                        )
                        # A watchdog or another actor may replace the selected
                        # process immediately after it exits.  Do not remove
                        # the stale record or permit a restart until another
                        # complete process-table scan proves that no same-name
                        # process remains.
                        confirmation = self._probe_residue_target(
                            str(original_probe["role"]),
                            original_probe["runtime"],
                        )
                        if confirmation["finding"]["status"] != "orphan_record":
                            raise ManagedMinerTransitionError(
                                "explicit cleanup candidate was replaced or another "
                                "same-name miner is still running",
                                state_changed=True,
                                old_stopped=True,
                            )
                        if not remove_managed_process_record_if_unchanged(
                            original_probe["state_path"],
                            record,
                        ):
                            raise ManagedMinerTransitionError(
                                "managed miner process state changed after explicit cleanup",
                                state_changed=True,
                                old_stopped=True,
                            )
                        record_removed = True
                        result_status = "explicit_candidate_stopped"
                    elif finding["status"] in {"preboot_record", "orphan_record", "invalid_record"}:
                        # Repeat the complete absence proof just before the CAS.
                        confirmation = self._probe_residue_target(
                            str(original_probe["role"]),
                            original_probe["runtime"],
                        )
                        if (
                            confirmation["target_fingerprint"]
                            != original_probe["target_fingerprint"]
                        ):
                            raise ManagedMinerError(
                                "managed miner residue changed during orphan cleanup"
                                if finding["status"] != "invalid_record"
                                else "managed miner residue changed during invalid record cleanup"
                            )
                        if not remove_managed_process_record_if_unchanged(
                            original_probe["state_path"],
                            record,
                        ):
                            raise ManagedMinerError(
                                "managed miner process state changed during orphan cleanup"
                                if finding["status"] != "invalid_record"
                                else "managed miner process state changed during invalid record cleanup"
                            )
                        record_removed = True
                        result_status = (
                            "orphan_record_removed"
                            if finding["status"] != "invalid_record"
                            else "invalid_record_removed"
                        )
                    else:
                        process_stopped = self._stop_previous(
                            original_probe["runtime"],
                            clear_definitely_absent=True,
                            expected_initial_record=record,
                        )
                        record_removed = not original_probe["state_path"].exists()
                        if not record_removed:
                            raise ManagedMinerError(
                                "managed miner process record remains after cleanup"
                            )
                        result_status = "verified_process_stopped"

                    target_manager = (
                        self
                        if original_probe["role"] == "legacy"
                        else self._role_manager(
                            self._runtime(),
                            str(original_probe["role"]),
                        )
                    )
                    target_manager._write_status(
                        original_probe["runtime"],
                        {"flight_sheet_id": record["flight_sheet_id"]},
                        "stopped",
                        miner_type=str(record["miner_type"]),
                    )
                    results.append(
                        {
                            "role": original_probe["role"],
                            "status": result_status,
                            "process_stopped": process_stopped,
                            "record_removed": record_removed,
                            "reason": None,
                        }
                    )
                except ManagedMinerError as exc:
                    blocked = True
                    if isinstance(exc, ManagedMinerTransitionError):
                        partial_mutation = partial_mutation or exc.state_changed
                    results.append(
                        {
                            "role": original_probe["role"],
                            "status": "cleanup_failed",
                            "process_stopped": process_stopped,
                            "record_removed": record_removed,
                            "reason": str(exc),
                        }
                    )
                    break

        processes_stopped = sum(
            1 for result in results if result["process_stopped"]
        )
        records_removed = sum(
            1 for result in results if result["record_removed"]
        )
        return {
            **report,
            "blocked": blocked,
            "changed": bool(partial_mutation or processes_stopped or records_removed),
            "processes_stopped": processes_stopped,
            "records_removed": records_removed,
            "results": results,
            "selected_targets": selected_targets,
        }

    def running(self) -> dict[str, Any] | None:
        """Describe the live managed miner without exposing process control.

        ``None`` means that no verified RigPilot-owned miner is currently
        running.  This intentionally performs the same fail-closed identity
        checks used by :meth:`stop`.
        """

        runtime = self._runtime()
        try:
            document = self._read_active_flight_sheet()
        except ManagedMinerError:
            document = None
        independent = bool(
            isinstance(document, Mapping) and self._independent(document)
        )
        if not independent:
            for role in ("primary", "secondary"):
                role_runtime = _managed_role_runtime(runtime, role, create=False)
                if (role_runtime / "managed-miner-process.json").exists():
                    independent = True
                    break
        if independent:
            roles: dict[str, dict[str, Any]] = {}
            for role in ("primary", "secondary"):
                current = self._role_manager(runtime, role).running()
                if current is not None:
                    roles[role] = current
            if not roles:
                return None
            if len(roles) != 2:
                # While the delayed secondary start is still pending, the
                # single verified primary is a valid transitional session
                # view, not an inconsistent one.
                pending = load_pending_secondary_marker(runtime)
                primary_view = roles.get("primary")
                if (
                    pending is not None
                    and primary_view is not None
                    and pending["flight_sheet_id"]
                    == str(primary_view.get("flight_sheet_id"))
                ):
                    view = {
                        "managed": True,
                        "miner_type": str(primary_view.get("miner_type")),
                        "flight_sheet_id": str(
                            primary_view.get("flight_sheet_id")
                        ),
                        "pid": int(primary_view["pid"]),
                        "create_time": float(primary_view["create_time"]),
                        "roles": roles,
                        "complete": False,
                        "secondary_start_pending": True,
                    }
                    return view
                raise ManagedMinerError(
                    "independent managed miner session is incomplete"
                )
            flight_sheet_ids = {
                str(item["flight_sheet_id"]) for item in roles.values()
            }
            if len(flight_sheet_ids) != 1:
                raise ManagedMinerError(
                    "independent managed miner roles disagree on flight sheet"
                )
            primary_type = (
                document.get("primary", {}).get("miner_type")
                if isinstance(document, Mapping)
                and isinstance(document.get("primary"), Mapping)
                else roles.get("primary", next(iter(roles.values()))).get(
                    "miner_type"
                )
            )
            return {
                "managed": True,
                "miner_type": str(primary_type),
                "flight_sheet_id": next(iter(flight_sheet_ids)),
                "roles": roles,
                "complete": True,
            }
        verified = self._verified_previous(runtime)
        if verified is None:
            return None
        record, _process = verified
        return {
            "managed": True,
            "miner_type": str(record["miner_type"]),
            "flight_sheet_id": str(record["flight_sheet_id"]),
            "pid": int(record["pid"]),
            "create_time": float(record["create_time"]),
        }

    def transition_has_live_processes(self) -> bool:
        """Verify all fixed slots without requiring a complete dual session."""

        runtime = self._runtime()
        targets: list[tuple[ManagedMinerManager, Path]] = [(self, runtime)]
        for role in ("primary", "secondary"):
            manager = self._role_manager(runtime, role)
            targets.append((manager, manager._runtime()))
        live = False
        for manager, slot_runtime in targets:
            if (
                manager._verified_previous(
                    slot_runtime,
                    clear_definitely_absent=True,
                )
                is not None
            ):
                live = True
        return live

    def _stop_previous(
        self,
        runtime: Path,
        *,
        clear_definitely_absent: bool = False,
        expected_initial_record: Mapping[str, Any] | None = None,
    ) -> bool:
        state_path = runtime / "managed-miner-process.json"

        def invocation_identity(value: Mapping[str, Any]) -> tuple[Any, ...]:
            return tuple(value.get(field) for field in (
                "version",
                "executable",
                "target_executable",
                "execution_mode",
                "wsl_token",
                "wsl_guest_executable",
                "wsl_identity",
                "wsl_cache_key",
                "miner_type",
                "flight_sheet_id",
                "argv_sha256",
            ))

        stopped = False
        expected_invocation: tuple[Any, ...] | None = None
        if expected_initial_record is not None:
            current = self._read_process_state(state_path)
            if current != dict(expected_initial_record):
                raise ManagedMinerError(
                    "managed miner process state changed after residue scan"
                )
            expected_invocation = invocation_identity(current)
        # SRBMiner can replace its watchdog PID while termination is in
        # progress.  Re-verify and stop a bounded chain of exact replacements;
        # never unlink a record that another Agent thread has just rebound.
        for _attempt in range(4):
            verified = self._verified_previous(
                runtime,
                clear_definitely_absent=clear_definitely_absent,
            )
            if verified is None:
                return stopped
            record, process = verified
            if expected_invocation is not None and invocation_identity(record) != expected_invocation:
                raise ManagedMinerError(
                    "managed miner invocation changed after residue scan"
                )
            if record.get("version") in {4, 5}:
                guest_stopped = False
                try:
                    guest_stopped = stop_wsl_guest(
                        Path(record["executable"]),
                        str(record["wsl_token"]),
                        str(record["wsl_guest_executable"]),
                        _wsl_identity_from_record(record),
                    )
                    stopped = stopped or guest_stopped
                except WslMiningError as exc:
                    raise ManagedMinerTransitionError(
                        str(exc),
                        state_changed=stopped,
                        old_stopped=stopped,
                    ) from exc
                if process is not None:
                    try:
                        process.terminate()
                        stopped = True
                        try:
                            process.wait(timeout=10)
                        except psutil.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    except psutil.NoSuchProcess:
                        pass
                    except psutil.Error as exc:
                        raise ManagedMinerTransitionError(
                            "WSL guest stopped but its exact host wrapper could "
                            "not be reaped",
                            state_changed=True,
                            old_stopped=True,
                        ) from exc
                if remove_managed_process_record_if_unchanged(state_path, record):
                    return stopped
                raise ManagedMinerTransitionError(
                    "managed WSL miner state changed after guest stop",
                    state_changed=stopped,
                    old_stopped=stopped,
                )
            if process is None:
                raise ManagedMinerError("native managed miner process handle is unavailable")
            try:
                process.terminate()
                stopped = True
                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                if stopped:
                    raise ManagedMinerTransitionError(
                        "previous managed miner changed state while it was "
                        "being stopped",
                        state_changed=True,
                        old_stopped=True,
                    ) from exc
                raise ManagedMinerError(
                    "previous managed miner could not be stopped"
                ) from exc
            if (record.get("miner_type") == "srbminer"
                    and re.search(r"[\\/]generation-[0-9a-f]{32}[\\/]bundle[\\/]",
                                  str(record.get("executable", "")))):
                # Re-enter the existing complete replacement/absence and API
                # reconciliation before CAS disposal. A stopped watchdog PID
                # does not prove this run has no replacement.
                continue
            if remove_managed_process_record_if_unchanged(state_path, record):
                self._discard_stopped_native_srb_record(record, runtime)
                return stopped
            # A concurrent inventory pass may have rebound the record to the
            # watchdog replacement.  Loop and prove that new identity before
            # touching it.  If another actor merely removed the record,
            # _verified_previous returns None on the next iteration.
        raise ManagedMinerTransitionError(
            "managed miner kept replacing itself while being stopped",
            state_changed=stopped,
            old_stopped=stopped,
        )

    def stop(self) -> dict[str, Any]:
        """Stop only the currently recorded RigPilot-owned miner process."""

        # A stop must interrupt a pending delayed secondary start immediately.
        cancel_deferred_secondary_start("stop")
        runtime = self._runtime()
        try:
            active_document = self._read_active_flight_sheet()
        except ManagedMinerError:
            active_document = None
        has_independent_state = False
        for role in ("primary", "secondary"):
            role_runtime = _managed_role_runtime(runtime, role, create=False)
            if (role_runtime / "managed-miner-process.json").exists():
                has_independent_state = True
                break
        if (
            isinstance(active_document, Mapping)
            and self._independent(active_document)
        ) or has_independent_state:
            role_results: dict[str, dict[str, Any]] = {}
            stop_errors: list[str] = []
            known_flight_sheet_id = (
                str(active_document.get("flight_sheet_id"))
                if isinstance(active_document, Mapping)
                else ""
            )
            try:
                stopped_any = self._stop_previous(
                    runtime, clear_definitely_absent=True
                )
            except ManagedMinerError as exc:
                stopped_any = False
                stop_errors.append(f"legacy: {exc}")
            for role in ("primary", "secondary"):
                leg = (
                    active_document.get(role)
                    if isinstance(active_document, Mapping)
                    else None
                )
                miner_type = (
                    str(leg.get("miner_type"))
                    if isinstance(leg, Mapping)
                    and leg.get("miner_type") in _RECORDED_MINER_TYPES
                    else None
                )
                role_stopped = False
                try:
                    manager = self._role_manager(runtime, role)
                    role_runtime = manager._runtime()
                    record = manager._read_process_state(
                        role_runtime / "managed-miner-process.json"
                    )
                    if record is not None:
                        miner_type = str(record["miner_type"])
                        if not known_flight_sheet_id:
                            known_flight_sheet_id = str(record["flight_sheet_id"])
                    role_stopped = manager._stop_previous(
                        role_runtime, clear_definitely_absent=True
                    )
                    stopped_any = stopped_any or role_stopped
                    if miner_type is not None:
                        manager._write_status(
                            role_runtime,
                            {
                                "flight_sheet_id": (
                                    active_document.get("flight_sheet_id")
                                    if isinstance(active_document, Mapping)
                                    else (
                                        record.get("flight_sheet_id")
                                        if record is not None
                                        else None
                                    )
                                )
                            },
                            "stopped",
                            miner_type=miner_type,
                        )
                except ManagedMinerError as exc:
                    stop_errors.append(f"{role}: {exc}")
                role_results[role] = {
                    "miner_type": miner_type,
                    "stopped": role_stopped,
                }
            if stop_errors:
                raise ManagedMinerError(
                    "independent managed miner stop was incomplete: "
                    + "; ".join(stop_errors)
                )
            primary_leg = (
                active_document.get("primary")
                if isinstance(active_document, Mapping)
                else None
            )
            primary_type = (
                primary_leg.get("miner_type")
                if isinstance(primary_leg, Mapping)
                else role_results["primary"].get("miner_type")
            )
            return {
                "managed": True,
                "miner_type": primary_type,
                "flight_sheet_id": known_flight_sheet_id,
                "stopped": stopped_any,
                "roles": role_results,
                "status_path": str(self._status_path(runtime)),
            }
        state_path = runtime / "managed-miner-process.json"
        record = self._read_process_state(state_path)
        if record is None:
            try:
                document = self._read_active_flight_sheet()
            except ManagedMinerError as exc:
                if str(exc) not in {
                    "active flight sheet is not configured",
                    "active flight sheet does not exist",
                }:
                    raise
                return {
                    "managed": True,
                    "miner_type": None,
                    "flight_sheet_id": None,
                    "stopped": False,
                    "status_path": str(self._status_path(runtime)),
                }
            primary = document.get("primary")
            miner_type = primary.get("miner_type") if isinstance(primary, dict) else None
            if miner_type not in _RECORDED_MINER_TYPES:
                raise ManagedMinerError(
                    "active flight sheet has no supported managed miner"
                )
            flight_sheet_id = str(document["flight_sheet_id"])
        else:
            miner_type = str(record["miner_type"])
            flight_sheet_id = str(record["flight_sheet_id"])
        stopped = self._stop_previous(
            runtime,
            clear_definitely_absent=True,
        )
        self._write_status(
            runtime,
            {"flight_sheet_id": flight_sheet_id},
            "stopped",
            miner_type=miner_type,
        )
        return {
            "managed": True,
            "miner_type": miner_type,
            "flight_sheet_id": flight_sheet_id,
            "stopped": stopped,
            "status_path": str(self._status_path(runtime)),
        }

    def remove_flight_sheet(self) -> dict[str, Any]:
        """Stop the owned miner and clear only fixed Agent-owned state files.

        Every deletion target is derived from local configuration. All targets
        are validated before stopping the miner, so a link/reparse-point attack
        cannot turn a rejected removal into an unexpected mining interruption.
        """

        cancel_deferred_secondary_start("remove_flight_sheet")

        runtime = self._runtime()
        active_path = self.config.flight_sheet_file
        paths: list[tuple[str, Path]] = []
        role_runtimes: dict[str, Path] = {}
        if active_path is not None:
            paths.extend(
                [
                    ("active flight sheet", active_path),
                    (
                        "pending flight sheet",
                        active_path.with_name(f".{active_path.name}.pending"),
                    ),
                ]
            )
        paths.append(("miner status", self._status_path(runtime)))
        for role in ("primary", "secondary"):
            role_runtime = _managed_role_runtime(runtime, role, create=False)
            role_runtimes[role] = role_runtime
            paths.extend(
                [
                    (
                        f"{role} active flight sheet",
                        role_runtime / "active-flight-sheet.json",
                    ),
                    (
                        f"{role} pending flight sheet",
                        role_runtime / ".active-flight-sheet.json.pending",
                    ),
                    (
                        f"{role} miner status",
                        role_runtime / "miner-status.json",
                    ),
                ]
            )
        paths.append(
            ("independent miner manifest", runtime / "independent-miners.json")
        )

        def xmrig_config_paths(root: Path) -> list[Path]:
            try:
                entries = list(root.iterdir())
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise ManagedMinerError(
                    "managed XMRig configuration directory cannot be inspected"
                ) from exc
            if len(entries) > 8192:
                raise ManagedMinerError(
                    "managed XMRig configuration directory is too large"
                )
            return sorted(
                (
                    path
                    for path in entries
                    if path.name == "xmrig-config.json"
                    or _XMRIG_CONTENT_CONFIG_RE.fullmatch(path.name) is not None
                ),
                key=lambda path: path.name,
            )

        xmrig_roots = [("legacy", runtime), *role_runtimes.items()]
        initial_xmrig_configs: set[Path] = set()
        for role, root in xmrig_roots:
            for config_path in xmrig_config_paths(root):
                initial_xmrig_configs.add(config_path)
                paths.append(
                    (
                        f"{role} XMRig configuration {config_path.name}",
                        config_path,
                    )
                )

        normalized = [os.path.normcase(str(path.absolute())) for _label, path in paths]
        if len(set(normalized)) != len(normalized):
            raise ManagedMinerError("managed miner removal paths overlap")

        def inspect(path: Path, label: str) -> os.stat_result | None:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ManagedMinerError(f"{label} cannot be inspected") from exc
            if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                raise ManagedMinerError(f"{label} is not a safe regular file")
            return info

        before = {path: inspect(path, label) for label, path in paths}

        # A normal stop deliberately parses the active sheet when no process
        # record remains.  A remove command can arrive immediately after its
        # first exact stop, however, and the remaining safe regular JSON may
        # itself be corrupt.  In that state there is no process authorization
        # left to exercise: skip the redundant second stop and proceed with
        # the already prevalidated fixed-file removal.
        active_unreadable = False
        if active_path is not None and before.get(active_path) is not None:
            try:
                self._read_active_flight_sheet()
            except ManagedMinerError:
                active_unreadable = True

        process_record_paths = [
            runtime / "managed-miner-process.json",
            *(
                role_runtime / "managed-miner-process.json"
                for role_runtime in role_runtimes.values()
            ),
        ]
        def entry_present(path: Path) -> bool:
            try:
                path.lstat()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise ManagedMinerError(
                    "managed miner process record cannot be inspected"
                ) from exc
            return True

        process_record_present = any(
            entry_present(path) for path in process_record_paths
        )
        if active_unreadable and not process_record_present:
            stopped = {
                "flight_sheet_id": None,
                "stopped": False,
            }
        else:
            stopped = self.stop()

        # stop() may atomically create/replace miner-status.json. Validate all
        # final objects together before deleting any one of them.
        final_xmrig_configs = {
            config_path
            for _role, root in xmrig_roots
            for config_path in xmrig_config_paths(root)
        }
        if final_xmrig_configs - initial_xmrig_configs:
            raise ManagedMinerError(
                "managed XMRig configuration appeared during removal"
            )
        after: dict[Path, os.stat_result] = {}
        for label, path in paths:
            info = inspect(path, label)
            if info is None:
                continue
            original = before[path]
            status_file = label.endswith("miner status")
            if not status_file and original is None:
                raise ManagedMinerError(f"{label} appeared during removal")
            if original is not None and not status_file and (
                info.st_dev != original.st_dev or info.st_ino != original.st_ino
            ):
                raise ManagedMinerError(f"{label} changed during removal")
            after[path] = info

        # Remove transient/status files first and the active configuration
        # last. If a final unlink fails, the Agent still reports a configured
        # but stopped sheet that an operator can safely retry removing.
        removal_order = sorted(
            paths, key=lambda item: item[0] == "active flight sheet"
        )
        for label, path in removal_order:
            expected = after.get(path)
            if expected is None:
                continue
            current = inspect(path, label)
            if current is None or (
                current.st_dev != expected.st_dev or current.st_ino != expected.st_ino
            ):
                raise ManagedMinerError(f"{label} changed during removal")
            try:
                path.unlink()
            except OSError as exc:
                raise ManagedMinerError(f"{label} cannot be removed") from exc

        return {
            "managed": True,
            "removed": active_path in after if active_path is not None else False,
            "flight_sheet_id": stopped.get("flight_sheet_id"),
            "miner_stopped": bool(stopped.get("stopped")),
            "configuration_cleared": True,
        }

    def restart(self) -> dict[str, Any]:
        """Restart from the bounded Agent-owned active flight sheet."""

        source = self._read_active_flight_sheet()
        runtime_document = copy.deepcopy(source)
        if _document_uses_srb_gpu_oc(
            source,
            local_oc_enabled=self.config.allow_gpu_oc,
            system_name=self.system_name,
        ):
            # The private runtime flag is deliberately not written back to the
            # saved flight.  Every start/restart is nevertheless idempotent:
            # restore defaults first, then apply any declared SRBMiner OC.
            runtime_document["_reset_srb_gpu_oc"] = True
        result = dict(self.apply(runtime_document))
        result["restarted"] = True
        return result

    @staticmethod
    def _snapshot_srb_oc_reset_logs(
        runtime: Path,
    ) -> dict[str, tuple[int, int, int] | None]:
        """Capture fixed Agent-owned log identities before starting reset.

        The snapshot is deliberately metadata-only.  Miner output can contain
        pool or wallet data and is never used as an authorization signal.
        """

        markers: dict[str, tuple[int, int, int] | None] = {}
        for name in _SRB_OC_RESET_LOG_NAMES:
            path = runtime / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                markers[name] = None
                continue
            except OSError as exc:
                raise ManagedMinerError(
                    "temporary SRBMiner GPU OC reset log cannot be inspected"
                ) from exc
            if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                raise ManagedMinerError(
                    "temporary SRBMiner GPU OC reset log is unsafe"
                )
            markers[name] = (int(info.st_dev), int(info.st_ino), int(info.st_size))
        return markers

    @staticmethod
    def _srb_oc_reset_log_progressed(
        runtime: Path,
        markers: Mapping[str, tuple[int, int, int] | None],
    ) -> bool:
        """Prove that the newly launched reset invocation produced output."""

        progressed = False
        for name in _SRB_OC_RESET_LOG_NAMES:
            path = runtime / name
            before = markers.get(name)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ManagedMinerError(
                    "temporary SRBMiner GPU OC reset log cannot be inspected"
                ) from exc
            if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                raise ManagedMinerError(
                    "temporary SRBMiner GPU OC reset log is unsafe"
                )
            current_identity = (int(info.st_dev), int(info.st_ino))
            if before is None:
                progressed = progressed or info.st_size > 0
                continue
            if current_identity != before[:2]:
                # SRBMiner may rotate its own fixed log during startup.  The
                # replacement is still required to be a safe regular file
                # inside the already-validated Agent runtime, and process
                # identity is checked separately on every readiness poll.
                progressed = progressed or info.st_size > 0
                continue
            progressed = progressed or info.st_size > before[2]
        return progressed

    def _wait_for_srb_oc_reset_ready(
        self,
        runtime: Path,
        api_port: int,
        log_markers: Mapping[str, tuple[int, int, int] | None],
        expected_argv_sha256: str,
    ) -> str:
        """Wait until the transient SRBMiner reset process has initialized.

        SRBMiner applies ``--gpu-reset-oc`` during startup before exposing its
        loopback API.  The API is the preferred proof, but it is only a
        monitoring endpoint and can remain unavailable on an otherwise healthy
        Windows launch.  The official CLI defines the flag as resetting
        defaults "on miner start", so the bounded fallback is tied to the
        exact executable, create-time and reset argv fingerprint rather than
        pool connectivity: prefer fresh Agent-owned log output, but also accept
        that exact invocation after it remains alive for the full stability
        window.  A process that exits, restarts or changes identity still fails
        closed.
        """

        deadline = time.monotonic() + _SRB_OC_RESET_READY_TIMEOUT_SECONDS
        last_error = "local API did not become ready"
        fallback_started: float | None = None
        exact_process_stable_since: float | None = None
        while time.monotonic() < deadline:
            verified = self._verified_previous(
                runtime,
                clear_definitely_absent=True,
            )
            if verified is None:
                raise ManagedMinerError(
                    "temporary SRBMiner GPU OC reset process exited before readiness"
                )
            record, _process = verified
            if (
                record.get("miner_type") != "srbminer"
                or record.get("api_port") != api_port
                or record.get("argv_sha256") != expected_argv_sha256
            ):
                raise ManagedMinerError(
                    "temporary SRBMiner GPU OC reset process identity changed"
                )
            try:
                with socket.create_connection(
                    ("127.0.0.1", api_port),
                    timeout=min(0.5, max(0.05, deadline - time.monotonic())),
                ):
                    return "loopback_api"
            except OSError as exc:
                last_error = str(exc) or type(exc).__name__
            now = time.monotonic()
            if exact_process_stable_since is None:
                exact_process_stable_since = now
            if self._srb_oc_reset_log_progressed(runtime, log_markers):
                if fallback_started is None:
                    fallback_started = now
                elif now - fallback_started >= _SRB_OC_RESET_LOG_STABLE_SECONDS:
                    return "verified_reset_invocation_stable"
            else:
                fallback_started = None
            if (
                exact_process_stable_since is not None
                and now - exact_process_stable_since
                >= _SRB_OC_RESET_LOG_STABLE_SECONDS
            ):
                return "verified_exact_reset_invocation_stable"
            time.sleep(
                min(
                    _SRB_OC_RESET_READY_POLL_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            )
        raise ManagedMinerError(
            "temporary SRBMiner GPU OC reset process did not become ready: "
            f"{last_error[:300]}"
        )

    def _clear_srb_gpu_oc_document(
        self,
        document: Mapping[str, Any],
        *,
        restart_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Clear one runtime's SRBMiner OC without changing its saved flight.

        SRBMiner owns these settings itself; they are deliberately not mixed
        with RigPilot's separately persisted NVML profile.  To restore the
        vendor defaults reliably, launch the already-verified active SRBMiner
        package once with ``--gpu-reset-oc`` and no OC values, confirm that the
        exact recorded PID initialized through its loopback API or bounded log
        fallback, then stop it.  Transactional flight switches may restart the
        original saved flight on failure; explicit stop/remove operations
        disable that rollback so their final state always remains stopped.
        """

        with self._native_srb_transaction(document):
            reset_document = copy.deepcopy(document)
            reset_document["gpu_oc"] = None
            reset_document["_reset_srb_gpu_oc"] = True
            # Validate the complete package, device selection and reset argv before
            # touching the currently running miner.
            prepared = self._prepare_single(reset_document)
            try:
                return self._clear_srb_gpu_oc_prepared(
                    document, reset_document, prepared, restart_on_failure=restart_on_failure,
                )
            finally:
                self._release_native_srb_plan(prepared)

    def _clear_srb_gpu_oc_prepared(
        self,
        document: Mapping[str, Any],
        reset_document: Mapping[str, Any],
        prepared: Mapping[str, Any],
        *,
        restart_on_failure: bool,
    ) -> dict[str, Any]:
        runtime = prepared.get("runtime")
        api_port = prepared.get("api_port")
        gpu_mapping = prepared.get("gpu_mapping")
        if (
            not isinstance(runtime, Path)
            or isinstance(api_port, bool)
            or not isinstance(api_port, int)
            or not isinstance(gpu_mapping, list)
        ):
            raise ManagedMinerError("temporary SRBMiner GPU OC reset plan is invalid")
        if not any(
            isinstance(item, Mapping) and item.get("vendor") == "nvidia"
            for item in gpu_mapping
        ):
            # The official reset flag only affects NVIDIA. Treat an
            # AMD/Intel-only flight as an already-clean no-op instead of
            # launching an unsupported transient reset process.
            return {
                "required": False,
                "cleared": True,
                "method": "not_applicable_no_nvidia",
                "previous_was_running": self.running() is not None,
                "stopped_previous": False,
            }

        previous_was_running = False
        stopped_previous = False
        transient_started = False
        try:
            stopped_previous = self._stop_previous(
                runtime,
                clear_definitely_absent=True,
            )
            previous_was_running = stopped_previous
            log_markers = self._snapshot_srb_oc_reset_logs(runtime)
            reset_result = self._apply_single(
                reset_document,
                prepared=prepared,
                stop_previous=False,
                # ``--gpu-reset-oc`` is a one-shot startup side effect.  Its
                # confirmation below is deliberately stricter and bound to
                # the exact reset argv.  Running the ordinary miner readiness
                # loop first made this transition wait twice (and, on some
                # Windows launches, miss the short API-ready window entirely).
                require_startup_readiness=False,
            )
            transient_started = True
            expected_argv_sha256 = managed_argv_fingerprint(
                list(prepared["argv"]),
                executable=prepared["executable"],
            )
            confirmation = self._wait_for_srb_oc_reset_ready(
                runtime,
                api_port,
                log_markers,
                expected_argv_sha256,
            )
            transient_stopped = self._stop_previous(
                runtime,
                clear_definitely_absent=True,
            )
            if not transient_stopped:
                raise ManagedMinerError(
                    "temporary SRBMiner GPU OC reset process could not be confirmed stopped"
                )
            self._write_status(
                runtime,
                document,
                "stopped",
                miner_type="srbminer",
                api_port=api_port,
            )
            return {
                "required": True,
                "cleared": True,
                "method": "srbminer_cli_reset",
                "previous_was_running": previous_was_running,
                "stopped_previous": stopped_previous,
                "temporary_pid": reset_result.get("pid"),
                "confirmation": confirmation,
            }
        except ManagedMinerError as exc:
            cleanup_errors: list[str] = []
            if transient_started:
                try:
                    self._stop_previous(
                        runtime,
                        clear_definitely_absent=True,
                    )
                except ManagedMinerError as cleanup_exc:
                    cleanup_errors.append(f"temporary reset cleanup: {cleanup_exc}")
            if restart_on_failure and previous_was_running and not cleanup_errors:
                try:
                    # The active sheet was never replaced.  Re-applying it also
                    # restores its original SRBMiner OC in one bounded argv.
                    self._apply_single(document, stop_previous=False)
                except ManagedMinerError as rollback_exc:
                    cleanup_errors.append(f"previous flight restart: {rollback_exc}")
            if cleanup_errors:
                raise ManagedMinerError(
                    f"{exc}; SRBMiner GPU OC reset rollback was incomplete: "
                    + "; ".join(cleanup_errors)
                ) from exc
            raise

    def clear_srb_gpu_oc_for_document(
        self,
        document: Mapping[str, Any],
        *,
        restart_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Clear SRBMiner OC described by one already-validated flight."""

        srb_roles = _document_srb_gpu_roles(document)
        reset_requested = document.get("_reset_srb_gpu_oc") is True
        if not srb_roles or not (
            reset_requested
            or _document_uses_srb_gpu_oc(
                document,
                local_oc_enabled=self.config.allow_gpu_oc,
                system_name=self.system_name,
            )
        ):
            return {
                "required": False,
                "cleared": False,
                "previous_was_running": self.running() is not None,
            }
        if not self._independent(document):
            return self._clear_srb_gpu_oc_document(
                document,
                restart_on_failure=restart_on_failure,
            )

        runtime = self._runtime()
        role_results: dict[str, dict[str, Any]] = {}
        cleared_roles: list[
            tuple[str, ManagedMinerManager, dict[str, Any], dict[str, Any]]
        ] = []
        try:
            for role in srb_roles:
                manager = self._role_manager(runtime, role)
                role_document = self._role_document(document, role)
                result = manager._clear_srb_gpu_oc_document(
                    role_document,
                    restart_on_failure=restart_on_failure,
                )
                role_results[role] = result
                cleared_roles.append((role, manager, role_document, result))
        except ManagedMinerError as exc:
            rollback_errors: list[str] = []
            # A later role failed after an earlier one reached defaults. Restore
            # only roles that had been running before this transaction.
            for role, manager, role_document, result in reversed(cleared_roles):
                if not restart_on_failure:
                    continue
                if not result.get("previous_was_running"):
                    continue
                try:
                    manager._apply_single(role_document, stop_previous=False)
                except ManagedMinerError as rollback_exc:
                    rollback_errors.append(f"{role}: {rollback_exc}")
            if rollback_errors:
                raise ManagedMinerError(
                    f"{exc}; independent SRBMiner OC rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise
        if not role_results:
            raise ManagedMinerError(
                "independent flight reports SRBMiner GPU OC without an SRBMiner GPU role"
            )
        return {
            "required": True,
            "cleared": all(result.get("cleared") for result in role_results.values()),
            "method": "srbminer_cli_reset",
            "previous_was_running": any(
                result.get("previous_was_running")
                for result in role_results.values()
            ),
            "independent": True,
            "roles": role_results,
        }

    def clear_active_srb_gpu_oc(
        self,
        *,
        restart_on_failure: bool = True,
    ) -> dict[str, Any]:
        """Clear active SRBMiner OC for a single or independent dual flight."""

        return self.clear_srb_gpu_oc_for_document(
            self._read_active_flight_sheet(),
            restart_on_failure=restart_on_failure,
        )

    @staticmethod
    def _startup_log_marker(path: Path) -> tuple[int, int, int] | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ManagedMinerError(
                "managed miner startup log cannot be inspected"
            ) from exc
        if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise ManagedMinerError("managed miner startup log is unsafe")
        return (int(info.st_dev), int(info.st_ino), int(info.st_size))

    @classmethod
    def _startup_log_progressed(
        cls,
        path: Path,
        marker: tuple[int, int, int] | None,
    ) -> bool:
        current = cls._startup_log_marker(path)
        if current is None:
            return False
        if marker is None or current[:2] != marker[:2]:
            return current[2] > 0
        return current[2] > marker[2]

    def _wait_for_managed_miner_ready(
        self,
        runtime: Path,
        record: Mapping[str, Any],
        *,
        log_path: Path,
        log_marker: tuple[int, int, int] | None,
    ) -> str:
        """Require the exact new invocation to become observably healthy."""

        miner_type = str(record["miner_type"])
        api_port = int(record["api_port"])
        deadline = time.monotonic() + _MINER_READY_TIMEOUT_SECONDS
        log_stable_since: float | None = None
        while time.monotonic() < deadline:
            verified = self._verified_previous(
                runtime,
                clear_definitely_absent=True,
            )
            if verified is None:
                raise ManagedMinerError(
                    "managed miner exited before startup readiness"
                )
            current, _process = verified
            if (
                current.get("flight_sheet_id")
                != record.get("flight_sheet_id")
                or current.get("argv_sha256") != record.get("argv_sha256")
                or current.get("api_port") != api_port
            ):
                raise ManagedMinerError(
                    "managed miner identity changed during startup readiness"
                )
            if (
                miner_type in {"xmrig", "srbminer"}
                and self._loopback_api_is_listening(api_port)
            ):
                return "loopback_api"
            now = time.monotonic()
            if self._startup_log_progressed(log_path, log_marker):
                if log_stable_since is None:
                    log_stable_since = now
                elif now - log_stable_since >= _MINER_LOG_STABLE_SECONDS:
                    return "verified_process_and_fresh_log"
            else:
                log_stable_since = None
            time.sleep(
                min(
                    _MINER_READY_POLL_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            )
        raise ManagedMinerError(
            "managed miner did not become ready before timeout"
        )

    def _launch(
        self,
        argv: list[str],
        executable: Path,
        runtime: Path,
        miner_type: str = "xmrig",
        *,
        execution_mode: str = "native",
        wsl_token: str | None = None,
        wsl_guest_executable: str | None = None,
        wsl_identity: WslLaunchIdentity | None = None,
    ) -> tuple[int, float, bool]:
        if is_retired_miner(miner_type):
            raise ManagedMinerError(f"{miner_type} is retired; select an active miner")
        if miner_type not in MANAGED_MINER_TYPES:
            raise ManagedMinerError("unsupported managed miner type")
        launcher_log = runtime / (
            f"{miner_type}.log"
            if miner_type == "pearlminer"
            else "srbminer.log"
            if execution_mode == "wsl" and miner_type == "srbminer"
            else "managed-miner-launcher.log"
        )
        options: dict[str, Any] = {
            "cwd": str(executable.parent),
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if miner_type == "seine":
            options["cwd"] = str(runtime)
        windows_base_flags = 0
        if self.system_name == "Windows":
            windows_base_flags = int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ) | int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            options["creationflags"] = windows_base_flags
        else:
            options["start_new_session"] = True
        options["env"] = (
            _pearl_environment(executable, self.system_name)
            if miner_type == "pearlminer"
            else _external_miner_environment(self.system_name)
        )
        process_handle: Any | None = None
        create_attempted = False
        survives_agent_restart = self.system_name != "Windows"
        try:
            if miner_type == "pearlminer":
                try:
                    log_info = launcher_log.lstat()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise ManagedMinerError("managed miner log cannot be inspected") from exc
                else:
                    if _link_or_reparse(log_info) or not stat.S_ISREG(log_info.st_mode):
                        raise ManagedMinerError("managed miner log is unsafe")
            # Log-based Pearl telemetry must never expose an old session.
            log_mode = (
                "wb"
                if miner_type == "pearlminer"
                else "ab"
            )
            with launcher_log.open(log_mode, buffering=0) as output:
                if execution_mode == "wsl":
                    if self.system_name != "Windows":
                        raise ManagedMinerError(
                            "WSL managed miner launch is Windows-only"
                        )
                    if (
                        not isinstance(wsl_token, str)
                        or not isinstance(wsl_guest_executable, str)
                        or not isinstance(wsl_identity, WslLaunchIdentity)
                    ):
                        raise ManagedMinerError(
                            "WSL managed miner supervisor identity is missing"
                        )
                    try:
                        create_attempted = True
                        process_handle = active_user_popen(
                            argv,
                            stdout=output,
                        )
                    except WindowsActiveUserError as exc:
                        raise ManagedMinerError(str(exc)) from exc
                    survives_agent_restart = self._outside_windows_job(
                        process_handle
                    )
                elif self.system_name == "Windows":
                    breakaway_options = dict(options)
                    breakaway_options["creationflags"] = (
                        windows_base_flags
                        | int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
                    )
                    try:
                        create_attempted = True
                        process_handle = self.popen_factory(
                            argv, stdout=output, **breakaway_options
                        )
                        survives_agent_restart = self._outside_windows_job(
                            process_handle
                        )
                    except OSError as exc:
                        # ERROR_ACCESS_DENIED is returned when the current
                        # scheduler/service job does not permit breakaway.
                        # Mining may still start, but a later Agent update must
                        # refuse to interrupt this process.
                        if not (
                            isinstance(exc, PermissionError)
                            or getattr(exc, "winerror", None) == 5
                        ):
                            raise
                        process_handle = self.popen_factory(
                            argv, stdout=output, **options
                        )
                        survives_agent_restart = False
                else:
                    create_attempted = True
                    process_handle = self.popen_factory(argv, stdout=output, **options)
            pid = int(process_handle.pid)
            observed = self.process_factory(pid)
            if not observed.is_running():
                raise ManagedMinerError("managed miner exited during startup")
            if _normalized_path(observed.exe()) != _normalized_path(executable):
                raise ManagedMinerError("started managed miner executable path does not match")
            create_time = float(observed.create_time())
            if not math.isfinite(create_time):
                raise ManagedMinerError("started managed miner creation time is invalid")
            if execution_mode == "wsl":
                try:
                    wait_wsl_guest_ready(
                        executable,
                        str(wsl_token),
                        str(wsl_guest_executable),
                        wsl_identity,
                    )
                except WslMiningError as exc:
                    raise ManagedMinerError(str(exc)) from exc
            return pid, create_time, survives_agent_restart
        except (ManagedMinerError, OSError, ValueError, psutil.Error) as exc:
            rollback_errors: list[str] = []
            if (
                execution_mode == "wsl"
                and isinstance(wsl_token, str)
                and isinstance(wsl_guest_executable, str)
                and isinstance(wsl_identity, WslLaunchIdentity)
            ):
                try:
                    stop_wsl_guest(
                        executable,
                        wsl_token,
                        wsl_guest_executable,
                        wsl_identity,
                    )
                except WslMiningError as cleanup_exc:
                    rollback_errors.append(str(cleanup_exc))
            if process_handle is not None:
                closed = self._terminate_launch_handle(process_handle)
                if not closed:
                    rollback_errors.append(
                        "owned launch process exit could not be verified"
                    )
                # The child existed even when the later identity probe failed.
                # Only its Popen handle and token-bound guest prove cleanup.
                failure = ManagedMinerTransitionError(
                    str(exc) if isinstance(exc, ManagedMinerError)
                    else "managed miner could not be started",
                    state_changed=True,
                    target_started=True,
                    target_cleanup_unconfirmed=bool(rollback_errors),
                    rollback_errors=rollback_errors,
                )
                failure.owned_launch_closed = closed and not rollback_errors
                raise failure from exc
            if rollback_errors:
                raise ManagedMinerTransitionError(
                    str(exc) if isinstance(exc, ManagedMinerError)
                    else "managed miner could not be started",
                    rollback_errors=rollback_errors,
                ) from exc
            if isinstance(exc, ManagedMinerError):
                exc.owned_launch_closed = not create_attempted
                raise
            failure = ManagedMinerError("managed miner could not be started")
            failure.owned_launch_closed = (not create_attempted or (
                isinstance(exc, OSError) and self.popen_factory is subprocess.Popen
                and execution_mode == "native"))
            raise failure from exc

    @staticmethod
    def _terminate_launch_handle(process_handle: Any) -> bool:
        """Confirm bounded cleanup through the exact newly created handle."""

        try:
            process_handle.terminate()
        except (OSError, AttributeError):
            # A failed signal can still mean the child already exited. Wait on
            # the owned handle rather than looking up or killing a numeric PID.
            pass
        waiter = getattr(process_handle, "wait", None)
        if not callable(waiter):
            return False
        try:
            exit_code = waiter(timeout=10)
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                return True
        except (OSError, AttributeError, subprocess.TimeoutExpired, TimeoutError):
            pass
        killer = getattr(process_handle, "kill", None)
        if callable(killer):
            try:
                killer()
            except (OSError, AttributeError):
                pass
        try:
            exit_code = waiter(timeout=5)
            return isinstance(exit_code, int) and not isinstance(exit_code, bool)
        except (OSError, AttributeError, subprocess.TimeoutExpired, TimeoutError):
            return False

    def _prepare_single(
        self,
        document: Mapping[str, Any],
        *,
        disallowed_api_ports: set[int] | None = None,
    ) -> dict[str, Any]:
        primary = document.get("primary")
        native_srb = (
            isinstance(primary, Mapping)
            and primary.get("miner_type") == "srbminer"
            and primary.get("execution_mode", "native") == "native"
        )
        if not native_srb:
            return self._prepare_single_plan(document, disallowed_api_ports=disallowed_api_ports)
        from .miner_cache_cleanup import cache_operation_lock, MinerCacheCleanupError
        self._native_srb_registry_key()
        lease = object()
        try:
            # The copy helper uses the same reentrant operation lock. Keep it
            # through registration/discovery so no other manager can recycle
            # the newly copied, not-yet-persisted plan.
            with cache_operation_lock(self._native_srb_base_config.miner_packages_dir, wait_seconds=30.0):
                try:
                    return self._prepare_single_plan(
                        document, disallowed_api_ports=disallowed_api_ports,
                        native_srb_lease=lease,
                    )
                except BaseException:
                    self._release_native_srb_plan({"_native_srb_lease": lease})
                    raise
        except (NativeMinerRuntimeError, MinerCacheCleanupError) as exc:
            raise ManagedMinerError(str(exc)) from exc

    def _prepare_single_plan(
        self,
        document: Mapping[str, Any],
        *,
        disallowed_api_ports: set[int] | None = None,
        native_srb_lease: object | None = None,
    ) -> dict[str, Any]:
        initial_primary = document.get("primary")
        if isinstance(initial_primary, Mapping) and is_retired_miner(initial_primary.get("miner_type")):
            raise ManagedMinerError(f"{initial_primary['miner_type']} is retired; select an active miner")
        runtime = self._runtime()
        primary = document.get("primary")
        if not isinstance(primary, dict):
            raise ManagedMinerError("managed flight sheet has no primary leg")
        miner_type = primary.get("miner_type")
        if miner_type not in MANAGED_MINER_TYPES:
            raise ManagedMinerError("managed flight sheet has no supported miner type")
        adapter = MINER_ADAPTERS.require(miner_type)
        runtime_id = adapter.runtime_id
        runtime_handler = TRUSTED_MINER_RUNTIME_HANDLERS.get(runtime_id)
        if runtime_handler is None:
            raise ManagedMinerError("managed miner adapter runtime is not trusted")
        execution_mode = str(primary.get("execution_mode", "native"))
        if execution_mode not in {"native", "wsl"}:
            raise ManagedMinerError("managed miner execution mode is invalid")
        package_root = self._package_root(primary, miner_type)
        package_metadata = primary.get("miner_package")
        if not isinstance(package_metadata, Mapping):
            raise ManagedMinerError("managed miner package metadata is invalid")
        if miner_type == "srbminer":
            validate_managed_algorithm_context(primary)
        pearl_extra = (
            validate_managed_extra_config(
                miner_type,
                primary.get("extra_config", {}),
                "primary.extra_config",
            )
            if miner_type in {"pearlminer"}
            else {}
        )
        executable_system = self.system_name
        if execution_mode == "wsl":
            if self.system_name != "Windows":
                raise ManagedMinerError(
                    "WSL GPU mining is available only on Windows devices"
                )
            if package_metadata.get("managed_os_family") != "linux":
                raise ManagedMinerError(
                    "WSL GPU mining requires a controller-managed Linux package"
                )
            if document.get("secondary") is not None:
                raise ManagedMinerError(
                    "integrated managed dual mining does not support WSL"
                )
            if leg_effective_resource(primary) != "gpu":
                raise ManagedMinerError(
                    "WSL is available only to Windows GPU miners"
                )
            if document.get("gpu_oc") is not None:
                raise ManagedMinerError(
                    "WSL SRBMiner flight OC is unsupported; use Windows host "
                    "NVML OC controls"
                )
            if miner_type == "pearlminer" and pearl_extra.get("gpu_backend") == "amd":
                raise ManagedMinerError(
                    "the first WSL release supports NVIDIA GPU miners only"
                )
            executable_system = "Linux"
        source_package_root = package_root
        if native_srb_lease is not None:
            # Each plan has its own never-before-existing run. Old copies are
            # neither inspected nor overwritten as a prerequisite for starting.
            package_root = prepare_native_srb_bundle(
                package_root, package_metadata, self._native_srb_base_config, runtime,
                role=self._native_srb_role,
            )
            with _NATIVE_SRB_PREPARED_LOCK:
                _NATIVE_SRB_PREPARED.setdefault(self._native_srb_registry_key(), {})[
                    native_srb_lease
                ] = _NativeSrbPlan(package_root, runtime, self._native_srb_role)
        executable = find_miner_executable(
            package_root,
            miner_type,
            system_name=executable_system,
            runner=self.cuda_runner,
            gpu_backend=(
                "nvidia"
                if execution_mode == "wsl" and miner_type == "pearlminer"
                else str(pearl_extra.get("gpu_backend", "auto"))
            ),
            cuda_variant=str(pearl_extra.get("cuda_variant", "auto")),
        )
        wsl_runtime = None
        if execution_mode == "wsl":
            try:
                ensure_wsl_package_read_access(
                    package_root,
                    packages_root=self.config.miner_packages_dir,
                )
                wsl_runtime = prepare_wsl_gpu_runtime(
                    executable,
                    package_root=package_root,
                    system_name=self.system_name,
                )
            except WslMiningError as exc:
                raise ManagedMinerError(str(exc)) from exc
        secondary = document.get("secondary")
        if secondary is not None:
            if not isinstance(secondary, dict) or secondary.get("miner_type") != miner_type:
                raise ManagedMinerError("managed primary and secondary must use one miner type")
            secondary_root = self._package_root(secondary, miner_type)
            if secondary_root != source_package_root:
                raise ManagedMinerError("managed dual mining must use one package version")
        excluded_ports = disallowed_api_ports or set()
        api_port: int | None = None
        for _attempt in range(8):
            candidate = self.port_allocator()
            if (
                isinstance(candidate, bool)
                or not isinstance(candidate, int)
                or not 1024 <= candidate <= 65535
            ):
                raise ManagedMinerError("allocated miner API port is invalid")
            if candidate not in excluded_ports:
                api_port = candidate
                break
        if api_port is None:
            raise ManagedMinerError(
                "could not allocate a distinct managed miner API port"
            )
        miner_log = runtime / f"{miner_type}.log"
        device_runner = self.device_runner
        native_plan = self._native_srb_plan({"_native_srb_lease": native_srb_lease})
        if native_plan is not None:
            def device_runner(*args, **kwargs):
                native_plan.probe_exposed = True
                native_plan.probe_closed = False
                mark_native_srb_probe(native_plan.bundle, self._native_srb_base_config,
                                      native_plan.runtime, role=native_plan.role, closed=False)
                try:
                    if self.device_runner is subprocess.run:
                        if self.system_name == "Linux":
                            result = _run_linux_pty_command(
                                args[0], cwd=kwargs["cwd"], env=kwargs["env"],
                                timeout=kwargs["timeout"], max_output_bytes=_MAX_DEVICE_LIST_BYTES,
                            )
                        else:
                            from .native_srb_probe import run_native_srb_probe
                            result = run_native_srb_probe(*args, system_name=self.system_name, **kwargs)
                    else:
                        result = self.device_runner(*args, **kwargs)
                except BaseException as exc:
                    native_plan.probe_closed = getattr(exc, "owned_direct_child_closed", False) is True
                    if native_plan.probe_closed:
                        mark_native_srb_probe(native_plan.bundle, self._native_srb_base_config,
                                              native_plan.runtime, role=native_plan.role, closed=True)
                    raise
                native_plan.probe_closed = getattr(result, "owned_direct_child_closed", False) is True
                if native_plan.probe_closed:
                    mark_native_srb_probe(native_plan.bundle, self._native_srb_base_config,
                                          native_plan.runtime, role=native_plan.role, closed=True)
                return result
        runtime_plan = runtime_handler(
            MinerRuntimePrepareContext(
                document=document,
                primary=primary,
                runtime=runtime,
                executable=executable,
                execution_mode=execution_mode,
                api_port=api_port,
                miner_log=miner_log,
                wsl_runtime=wsl_runtime,
                system_name=self.system_name,
                device_runner=device_runner,
                privilege_checker=self.privilege_checker,
            )
        )
        argv = runtime_plan.argv
        gpu_mapping = runtime_plan.gpu_mapping
        config_path = runtime_plan.config_path
        process_executable = executable
        wsl_token: str | None = None
        wsl_guest_executable: str | None = None
        wsl_identity: WslLaunchIdentity | None = None
        wsl_cache_key: str | None = None
        if wsl_runtime is not None:
            try:
                wsl_token = new_wsl_supervisor_token()
                argv = wsl_runtime.supervised_command(
                    argv,
                    token=wsl_token,
                )
            except WslMiningError as exc:
                raise ManagedMinerError(str(exc)) from exc
            process_executable = wsl_runtime.wsl_executable
            wsl_guest_executable = wsl_runtime.linux_executable
            wsl_identity = wsl_runtime.identity
            wsl_cache_key = wsl_runtime.cache_key
        return {
            "runtime": runtime,
            "document": document,
            "miner_type": miner_type,
            "api_port": api_port,
            "gpu_mapping": gpu_mapping,
            "argv": argv,
            "executable": executable,
            "process_executable": process_executable,
            "execution_mode": execution_mode,
            "wsl_token": wsl_token,
            "wsl_guest_executable": wsl_guest_executable,
            "wsl_identity": wsl_identity,
            "wsl_cache_key": wsl_cache_key,
            "config_path": config_path,
            **({"_native_srb_lease": native_srb_lease} if native_srb_lease is not None else {}),
        }

    def _stop_newly_launched(
        self,
        *,
        pid: int,
        executable: Path,
        create_time: float,
    ) -> None:
        """Terminate a just-launched exact process before its record exists."""

        try:
            process = self.process_factory(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return
            actual_executable = process.exe()
            actual_create_time = float(process.create_time())
        except psutil.NoSuchProcess:
            return
        except (psutil.Error, OSError, TypeError, ValueError) as exc:
            raise ManagedMinerError(
                "new managed miner identity cannot be read during rollback"
            ) from exc
        if (
            _normalized_path(actual_executable) != _normalized_path(executable)
            or abs(actual_create_time - create_time)
            > _CREATE_TIME_TOLERANCE_SECONDS
        ):
            raise ManagedMinerError(
                "new managed miner identity changed during rollback"
            )
        try:
            process.terminate()
            try:
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except psutil.NoSuchProcess:
            return
        except psutil.Error as exc:
            raise ManagedMinerError(
                "new managed miner could not be stopped during rollback"
            ) from exc

    def _apply_single(
        self,
        document: Mapping[str, Any],
        *,
        prepared: Mapping[str, Any] | None = None,
        stop_previous: bool = True,
        require_startup_readiness: bool | None = None,
    ) -> dict[str, Any]:
        with self._native_srb_transaction(document):
            owns_plan = not prepared
            launch = dict(prepared or self._prepare_single(document))
            try:
                return self._apply_single_plan(
                    document, prepared=launch, stop_previous=stop_previous,
                    require_startup_readiness=require_startup_readiness,
                )
            finally:
                # A supplied plan belongs to the surrounding transition/reset,
                # which keeps its lease through preflight and rollback.
                if owns_plan:
                    self._release_native_srb_plan(launch)

    def _apply_single_plan(
        self,
        document: Mapping[str, Any],
        *,
        prepared: Mapping[str, Any],
        stop_previous: bool,
        require_startup_readiness: bool | None,
    ) -> dict[str, Any]:
        launch = dict(prepared)
        if is_retired_miner(launch.get("miner_type")):
            raise ManagedMinerError(f"{launch['miner_type']} is retired; select an active miner")
        runtime = launch["runtime"]
        miner_type = str(launch["miner_type"])
        api_port = int(launch["api_port"])
        gpu_mapping = list(launch["gpu_mapping"])
        argv = list(launch["argv"])
        executable = launch["executable"]
        process_executable = launch.get("process_executable", executable)
        execution_mode = str(launch.get("execution_mode", "native"))
        wsl_token = launch.get("wsl_token")
        wsl_guest_executable = launch.get("wsl_guest_executable")
        wsl_identity = launch.get("wsl_identity")
        wsl_cache_key = launch.get("wsl_cache_key")
        config_path = launch.get("config_path")
        native_plan = self._native_srb_plan(launch)
        if (
            not isinstance(runtime, Path)
            or not isinstance(executable, Path)
            or not isinstance(process_executable, Path)
            or miner_type not in MANAGED_MINER_TYPES
            or execution_mode not in {"native", "wsl"}
            or (
                execution_mode == "wsl"
                and (
                    not isinstance(wsl_token, str)
                    or not isinstance(wsl_guest_executable, str)
                    or not isinstance(wsl_identity, WslLaunchIdentity)
                    or not isinstance(wsl_cache_key, str)
                    or re.fullmatch(r"[0-9a-f]{64}", wsl_cache_key) is None
                    or not wsl_cached_executable_matches(
                        wsl_guest_executable, wsl_cache_key
                    )
                )
            )
            or (config_path is not None and not isinstance(config_path, Path))
            or (
                miner_type == "seine"
                and (
                    execution_mode != "native"
                    or (
                        config_path is not None
                        and (
                            not isinstance(config_path, Path)
                            or config_path.name != "control-api.cookie"
                            or config_path.parent.parent != runtime / "seine-data"
                        )
                    )
                )
            )
        ):
            raise ManagedMinerError("managed miner launch plan is invalid")
        launched_pid: int | None = None
        launched_create_time: float | None = None
        process_record_written = False
        replaced = False
        readiness: str | None = None
        # Seine plain UI writes stdout/stderr only; unlike the other native
        # miners it has no separate --log-file. Observe the same file _launch
        # opens, with its pre-launch marker, rather than a nonexistent seine.log.
        startup_log_path = runtime / (
            "managed-miner-launcher.log"
            if miner_type == "seine"
            else f"{miner_type}.log"
        )
        startup_log_marker = (
            None
            if miner_type in {"pearlminer"}
            else self._startup_log_marker(startup_log_path)
        )
        try:
            replaced = (
                self._stop_previous(
                    runtime,
                    clear_definitely_absent=True,
                )
                if stop_previous
                else False
            )
            self._write_status(
                runtime,
                document,
                "starting",
                miner_type=miner_type,
                api_port=api_port,
                gpu_mapping=gpu_mapping,
            )
            if native_plan is not None:
                native_plan.phase = "launch_attempted"
            pid, create_time, survives_agent_restart = self._launch(
                argv,
                process_executable,
                runtime,
                miner_type,
                execution_mode=execution_mode,
                wsl_token=(wsl_token if isinstance(wsl_token, str) else None),
                wsl_guest_executable=(
                    wsl_guest_executable
                    if isinstance(wsl_guest_executable, str)
                    else None
                ),
                wsl_identity=(
                    wsl_identity
                    if isinstance(wsl_identity, WslLaunchIdentity)
                    else None
                ),
            )
            launched_pid = pid
            launched_create_time = create_time
            started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            record = {
                "version": (
                    7
                    if (
                        miner_type == "seine"
                        and execution_mode == "native"
                        and config_path is not None
                    )
                    else 5
                    if execution_mode == "wsl"
                    else 2
                ),
                "pid": pid,
                "executable": str(process_executable),
                "create_time": create_time,
                "miner_type": miner_type,
                "flight_sheet_id": str(document["flight_sheet_id"]),
                "started_at": started_at,
                "survives_agent_restart": survives_agent_restart,
                "argv_sha256": managed_argv_fingerprint(
                    argv, executable=process_executable
                ),
                "api_port": api_port,
            }
            if (
                miner_type == "seine"
                and execution_mode == "native"
                and config_path is not None
            ):
                record["seine_data_dir"] = str(config_path.parent)
            if execution_mode == "wsl":
                record.update(
                    execution_mode="wsl",
                    target_executable=str(executable),
                    wsl_token=wsl_token,
                    wsl_guest_executable=wsl_guest_executable,
                    wsl_cache_key=wsl_cache_key,
                    wsl_identity={
                        "windows_session_id": wsl_identity.windows_session_id,
                        "windows_user_sid": wsl_identity.windows_user_sid,
                        "distribution": wsl_identity.distribution,
                        "linux_uid": wsl_identity.linux_uid,
                    },
                )
            _write_private_json(runtime / "managed-miner-process.json", record)
            process_record_written = True
            if native_plan is not None:
                native_plan.phase = "committed"
            should_wait_for_readiness = (
                self.enforce_startup_readiness
                if require_startup_readiness is None
                else bool(require_startup_readiness)
            )
            if should_wait_for_readiness:
                readiness = self._wait_for_managed_miner_ready(
                    runtime,
                    record,
                    log_path=startup_log_path,
                    log_marker=startup_log_marker,
                )
            self._write_status(
                runtime,
                document,
                "running",
                miner_type=miner_type,
                api_port=api_port,
                pid=pid,
                started_at=started_at,
                gpu_mapping=gpu_mapping,
            )
            # The target is already confirmed running. Obsolete immutable
            # launch configs are private cache, so pruning must not undo it.
            try:
                _prune_xmrig_session_configs(
                    runtime,
                    keep=config_path if miner_type == "xmrig" else None,
                )
            except ManagedMinerError:
                LOGGER.warning(
                    "could not prune obsolete current XMRig configuration",
                    extra={
                        "event": "managed_miner_xmrig_prune_failed",
                        "role": "current",
                        "runtime": str(runtime),
                    },
                    exc_info=True,
                )
            result = {
                "managed": True,
                "miner_type": miner_type,
                "flight_sheet_id": str(document["flight_sheet_id"]),
                "pid": pid,
                "api_port": api_port,
                "replaced_previous": replaced,
                "survives_agent_restart": survives_agent_restart,
                "execution_mode": execution_mode,
                "status_path": str(self._status_path(runtime)),
            }
            runtime_fingerprint = document.get(
                FLIGHT_RUNTIME_FINGERPRINT_FIELD
            )
            if isinstance(runtime_fingerprint, str):
                result[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = (
                    runtime_fingerprint
                )
            if readiness is not None:
                result["readiness"] = readiness
            if gpu_mapping:
                result["gpu_mapping"] = gpu_mapping
            return result
        except ManagedMinerError as exc:
            nested_transition = (
                exc if isinstance(exc, ManagedMinerTransitionError) else None
            )
            rollback_errors: list[str] = []
            if nested_transition is not None:
                rollback_errors.extend(nested_transition.rollback_errors)
            if launched_pid is not None and launched_create_time is not None:
                try:
                    if process_record_written:
                        self._stop_previous(
                            runtime,
                            clear_definitely_absent=True,
                        )
                    else:
                        if (
                            execution_mode == "wsl"
                            and isinstance(wsl_token, str)
                            and isinstance(wsl_guest_executable, str)
                            and isinstance(wsl_identity, WslLaunchIdentity)
                        ):
                            try:
                                stop_wsl_guest(
                                    process_executable,
                                    wsl_token,
                                    wsl_guest_executable,
                                    wsl_identity,
                                )
                            except WslMiningError as stop_exc:
                                rollback_errors.append(str(stop_exc))
                        self._stop_newly_launched(
                            pid=launched_pid,
                            executable=process_executable,
                            create_time=launched_create_time,
                        )
                        if native_plan is not None:
                            # The direct PID can have vanished while SRB's
                            # watchdog still uses this run. No committed
                            # record exists to perform the normal complete
                            # replacement/absence reconciliation and CAS.
                            rollback_errors.append(
                                "new native SRBMiner run retained: replacement absence "
                                "could not be verified without its process record"
                            )
                except ManagedMinerError as cleanup_exc:
                    rollback_errors.append(str(cleanup_exc))
            elif native_plan is not None and getattr(exc, "owned_launch_closed", False) is True:
                native_plan.phase = "closed"
            try:
                self._write_status(
                    runtime,
                    document,
                    "failed",
                    miner_type=miner_type,
                    api_port=api_port,
                    error=(
                        f"{exc}; rollback failed: {'; '.join(rollback_errors)}"
                        if rollback_errors
                        else str(exc)
                    ),
                    gpu_mapping=gpu_mapping,
                )
            except ManagedMinerError:
                pass
            raise ManagedMinerTransitionError(
                (
                    nested_transition.message
                    if nested_transition is not None
                    else str(exc)
                ),
                state_changed=bool(
                    replaced
                    or launched_pid is not None
                    or (
                        nested_transition is not None
                        and nested_transition.state_changed
                    )
                ),
                old_stopped=bool(
                    replaced
                    or (
                        nested_transition is not None
                        and nested_transition.old_stopped
                    )
                ),
                target_started=bool(
                    launched_pid is not None
                    or (
                        nested_transition is not None
                        and nested_transition.target_started
                    )
                ),
                target_cleanup_unconfirmed=bool(
                    rollback_errors or (nested_transition is not None
                    and nested_transition.target_cleanup_unconfirmed)
                ),
                target_oc_may_have_applied=bool(
                    (
                        launched_pid is not None
                        or (
                            nested_transition is not None
                            and nested_transition.target_started
                        )
                    )
                    and _document_srb_gpu_roles(document)
                )
                or bool(
                    nested_transition is not None
                    and nested_transition.target_oc_may_have_applied
                ),
                rollback_errors=rollback_errors,
            ) from exc

    def _stop_independent_roles(self, runtime: Path) -> bool:
        stopped = False
        errors: list[str] = []
        for role in ("primary", "secondary"):
            try:
                manager = self._role_manager(runtime, role)
                role_runtime = manager._runtime()
                stopped = (
                    manager._stop_previous(
                        role_runtime,
                        clear_definitely_absent=True,
                    )
                    or stopped
                )
            except ManagedMinerError as exc:
                errors.append(f"{role}: {exc}")
        if errors:
            raise ManagedMinerError(
                "independent managed miner stop was incomplete: "
                + "; ".join(errors)
            )
        return stopped

    def _preflight_transition_slots(
        self,
        runtime: Path,
    ) -> list[
        tuple[
            str,
            "ManagedMinerManager",
            Path,
            dict[str, Any] | None,
        ]
    ]:
        """Resolve every possible managed slot before stopping any miner.

        A switch between single and independent mining may need to stop three
        fixed runtime slots.  Preflighting all of them first ensures that one
        ambiguous role cannot leave another healthy role stopped.  Proven
        preboot/orphan records may be reconciled here; no process is
        terminated until every slot has passed the same identity checks.
        """

        targets: list[tuple[str, ManagedMinerManager, Path]] = [
            ("legacy", self, runtime)
        ]
        for role in ("primary", "secondary"):
            manager = self._role_manager(runtime, role)
            targets.append((role, manager, manager._runtime()))

        snapshots: list[
            tuple[
                str,
                ManagedMinerManager,
                Path,
                dict[str, Any] | None,
            ]
        ] = []
        for label, manager, slot_runtime in targets:
            verified = manager._verified_previous(
                slot_runtime,
                clear_definitely_absent=True,
            )
            record = dict(verified[0]) if verified is not None else None
            snapshots.append((label, manager, slot_runtime, record))

        # Close the gap between the first and last role preflight.  Actual
        # termination repeats this comparison and exact identity proof.
        for label, manager, slot_runtime, expected in snapshots:
            current = manager._read_process_state(
                slot_runtime / "managed-miner-process.json"
            )
            if current != expected:
                raise ManagedMinerError(
                    f"{label} managed miner state changed during transition preflight"
                )
        return snapshots

    @staticmethod
    def _stop_preflighted_transition_slots(
        snapshots: Sequence[
            tuple[
                str,
                "ManagedMinerManager",
                Path,
                Mapping[str, Any] | None,
            ]
        ],
    ) -> bool:
        stopped = False
        try:
            for label, manager, slot_runtime, expected in snapshots:
                if expected is None:
                    current = manager._read_process_state(
                        slot_runtime / "managed-miner-process.json"
                    )
                    if current is not None:
                        raise ManagedMinerError(
                            f"{label} managed miner state appeared after "
                            "transition preflight"
                        )
                    continue
                stopped = (
                    manager._stop_previous(
                        slot_runtime,
                        clear_definitely_absent=True,
                        expected_initial_record=expected,
                    )
                    or stopped
                )
        except ManagedMinerError as exc:
            nested = exc if isinstance(exc, ManagedMinerTransitionError) else None
            partially_stopped = bool(
                stopped or (nested is not None and nested.state_changed)
            )
            if not partially_stopped:
                raise
            raise ManagedMinerTransitionError(
                nested.message if nested is not None else str(exc),
                state_changed=True,
                old_stopped=True,
                rollback_errors=(
                    nested.rollback_errors if nested is not None else ()
                ),
            ) from exc
        return stopped

    def _write_role_active_sheet(
        self,
        manager: "ManagedMinerManager",
        document: Mapping[str, Any],
    ) -> None:
        role_document = {
            key: value
            for key, value in document.items()
            if key != "_reset_srb_gpu_oc"
        }
        role_document["applied_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        path = manager.config.flight_sheet_file
        if path is None:
            raise ManagedMinerError("managed miner role has no flight sheet path")
        _write_private_json(path, role_document)

    def snapshot_independent_metadata(
        self,
    ) -> dict[Path, dict[str, Any] | None]:
        """Capture fixed dual-role metadata before an outer flight commit."""

        runtime = self._runtime()
        paths: list[tuple[Path, str]] = []
        for role in ("primary", "secondary"):
            manager = self._role_manager(runtime, role)
            path = manager.config.flight_sheet_file
            if path is None:
                raise ManagedMinerError("managed miner role has no flight sheet path")
            paths.append((path, f"{role} active flight sheet"))
        paths.append(
            (runtime / "independent-miners.json", "independent miner manifest")
        )
        return {
            path: _snapshot_private_json(path, label)
            for path, label in paths
        }

    def begin_flight_transition(
        self,
        document: Mapping[str, Any],
        *,
        previous_was_running: bool,
    ) -> None:
        """Persist enough intent to recover an Agent crash during a switch."""

        runtime = self._runtime()
        try:
            active = self._read_active_flight_sheet()
        except ManagedMinerError as exc:
            if str(exc) not in {
                "active flight sheet does not exist",
                "active flight sheet is not configured",
            }:
                raise
            active = None
        target_id = document.get("flight_sheet_id")
        if not isinstance(target_id, str) or not target_id:
            raise ManagedMinerError("managed flight transition target is invalid")
        _write_private_json(
            runtime / "flight-transition.json",
            {
                "version": 1,
                "phase": "prepared",
                "target_flight_sheet_id": target_id,
                "previous_flight_sheet_id": (
                    str(active.get("flight_sheet_id"))
                    if isinstance(active, Mapping)
                    and isinstance(active.get("flight_sheet_id"), str)
                    else None
                ),
                "previous_was_running": bool(previous_was_running),
                "target_document": {
                    key: copy.deepcopy(value)
                    for key, value in document.items()
                    if key != "_reset_srb_gpu_oc"
                },
                "created_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            },
        )
        self._sync_transition_directory(runtime)

    @staticmethod
    def _sync_transition_directory(directory: Path) -> None:
        try:
            _fsync_directory(directory)
        except OSError as exc:
            raise ManagedMinerError("flight transition directory could not be synchronized") from exc

    def mark_flight_transition_phase(
        self,
        target_flight_sheet_id: str,
        phase: str,
    ) -> None:
        if phase not in {"mutating", "target_ready"}:
            raise ManagedMinerError("managed flight transition phase is invalid")
        runtime = self._runtime()
        path = runtime / "flight-transition.json"
        journal = _snapshot_private_json(path, "flight transition journal")
        if (
            journal is None
            or journal.get("version") != 1
            or journal.get("target_flight_sheet_id")
            != target_flight_sheet_id
        ):
            raise ManagedMinerError("managed flight transition journal changed")
        journal["phase"] = phase
        journal["updated_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        _write_private_json(path, journal)
        self._sync_transition_directory(runtime)

    def finish_flight_transition(
        self,
        target_flight_sheet_id: str,
    ) -> None:
        runtime = self._runtime()
        path = runtime / "flight-transition.json"
        journal = _snapshot_private_json(path, "flight transition journal")
        if journal is None:
            return
        if (
            journal.get("version") != 1
            or journal.get("target_flight_sheet_id")
            != target_flight_sheet_id
        ):
            raise ManagedMinerError("managed flight transition journal changed")
        # The active sheet may be in a different directory or filesystem.
        # Persist its rename before discarding the recovery intent.
        if self.config.flight_sheet_file is not None:
            self._sync_transition_directory(self.config.flight_sheet_file.parent)
        self._unlink_state(path)
        self._sync_transition_directory(runtime)

    def _live_transition_flight_sheet_ids(self) -> set[str]:
        runtime = self._runtime()
        targets: list[tuple[ManagedMinerManager, Path]] = [(self, runtime)]
        for role in ("primary", "secondary"):
            manager = self._role_manager(runtime, role)
            targets.append((manager, manager._runtime()))
        flight_sheet_ids: set[str] = set()
        for manager, slot_runtime in targets:
            verified = manager._verified_previous(
                slot_runtime,
                clear_definitely_absent=True,
            )
            if verified is not None:
                record, _process = verified
                flight_sheet_ids.add(str(record["flight_sheet_id"]))
        return flight_sheet_ids

    def recover_interrupted_flight_transition(self) -> dict[str, Any]:
        """Restore the pre-switch state after a process or host crash."""

        runtime = self._runtime()
        path = runtime / "flight-transition.json"
        journal = _snapshot_private_json(path, "flight transition journal")
        if journal is None:
            return {"recovered": False, "reason": "no_transition"}
        target_id = journal.get("target_flight_sheet_id")
        previous_id = journal.get("previous_flight_sheet_id")
        phase = journal.get("phase")
        target_document = journal.get("target_document")
        if (
            journal.get("version") != 1
            or not isinstance(target_id, str)
            or not target_id
            or (
                previous_id is not None
                and not isinstance(previous_id, str)
            )
            or phase not in {"prepared", "mutating", "target_ready"}
            or not isinstance(target_document, Mapping)
            or target_document.get("flight_sheet_id") != target_id
            or not isinstance(journal.get("previous_was_running"), bool)
        ):
            raise ManagedMinerError("flight transition journal is invalid")
        try:
            active = self._read_active_flight_sheet()
        except ManagedMinerError as exc:
            if str(exc) != "active flight sheet does not exist":
                raise
            active = None
        active_id = (
            str(active.get("flight_sheet_id"))
            if isinstance(active, Mapping)
            and isinstance(active.get("flight_sheet_id"), str)
            else None
        )
        if active_id == target_id:
            self.finish_flight_transition(target_id)
            return {"recovered": True, "reason": "target_committed"}
        if active_id != previous_id:
            raise ManagedMinerError(
                "active flight sheet changed during interrupted transition"
            )

        live_ids = self._live_transition_flight_sheet_ids()
        if (
            phase == "prepared"
            and live_ids
            and live_ids == {previous_id}
        ):
            self.finish_flight_transition(target_id)
            return {"recovered": True, "reason": "previous_untouched"}

        errors: list[str] = []
        target_may_have_started = bool(
            phase in {"mutating", "target_ready"}
            or target_id in live_ids
        )
        if (
            target_may_have_started
            and _document_srb_gpu_roles(target_document)
        ):
            try:
                reset = self.clear_srb_gpu_oc_for_document(
                    target_document,
                    restart_on_failure=False,
                )
                if not _srb_gpu_oc_reset_satisfied(reset):
                    errors.append(
                        "interrupted target SRBMiner OC reset was not confirmed"
                    )
            except ManagedMinerError as exc:
                errors.append(str(exc))
        try:
            self.stop()
        except ManagedMinerError as exc:
            errors.append(str(exc))
        if (
            journal["previous_was_running"]
            and active is not None
            and not errors
        ):
            try:
                self.restart()
            except ManagedMinerError as exc:
                errors.append(str(exc))
        if errors:
            raise ManagedMinerError(
                "interrupted flight transition recovery was incomplete: "
                + "; ".join(errors)
            )
        self.finish_flight_transition(target_id)
        pending = (
            self.config.flight_sheet_file.with_name(
                f".{self.config.flight_sheet_file.name}.pending"
            )
            if self.config.flight_sheet_file is not None
            else None
        )
        if pending is not None:
            self._unlink_state(pending)
        return {
            "recovered": True,
            "reason": (
                "previous_restarted"
                if journal["previous_was_running"]
                else "target_stopped"
            ),
        }

    @staticmethod
    def restore_independent_metadata(
        snapshots: Mapping[Path, Mapping[str, Any] | None],
    ) -> None:
        errors: list[str] = []
        for path, snapshot in snapshots.items():
            try:
                _restore_private_json(path, snapshot)
            except ManagedMinerError as exc:
                errors.append(str(exc))
        if errors:
            raise ManagedMinerError(
                "independent managed miner metadata rollback was incomplete: "
                + "; ".join(errors)
            )

    def _deferred_secondary_starter(
        self,
        runtime: Path,
        document: Mapping[str, Any],
        secondary_document: Mapping[str, Any],
        *,
        primary_pid: int,
        primary_api_port: int,
    ) -> Callable[[_DeferredSecondaryStartHandle], None]:
        """Build the delayed secondary launch with dual-session semantics."""

        flight_sheet_id = str(document["flight_sheet_id"])
        primary_manager = self._role_manager(runtime, "primary")
        secondary_manager = self._role_manager(runtime, "secondary")

        def require_primary_live() -> bool:
            live = primary_manager.running()
            return (
                live is not None
                and live.get("flight_sheet_id") == flight_sheet_id
                and live.get("pid") == primary_pid
            )

        def starter(handle: _DeferredSecondaryStartHandle) -> None:
            try:
                if not require_primary_live():
                    LOGGER.error(
                        "primary miner is not running; delayed secondary "
                        "start abandoned",
                        extra={
                            "event": "secondary_start_primary_gone",
                            "flight_sheet_id": flight_sheet_id,
                        },
                    )
                    try:
                        self._stop_independent_roles(runtime.resolve())
                    except ManagedMinerError:
                        LOGGER.error(
                            "could not stop independent roles after primary loss",
                            extra={
                                "event": "secondary_start_cleanup_failed",
                                "flight_sheet_id": flight_sheet_id,
                            },
                            exc_info=True,
                        )
                    return
                # ``stop_previous=True`` replaces any residue a cancelled
                # earlier launch may have left in the secondary slot.
                plan = secondary_manager._prepare_single(
                    secondary_document,
                    disallowed_api_ports={primary_api_port},
                )
                try:
                    secondary_manager._apply_single(
                        secondary_document,
                        prepared=plan,
                        stop_previous=True,
                    )
                finally:
                    secondary_manager._release_native_srb_plan(plan)
                if not require_primary_live():
                    raise ManagedMinerError(
                        "primary managed miner exited while the delayed "
                        "secondary miner was starting"
                    )
                LOGGER.info(
                    "delayed secondary miner started",
                    extra={
                        "event": "secondary_start_completed",
                        "flight_sheet_id": flight_sheet_id,
                    },
                )
            except Exception as exc:
                # A dual flight is one requested mining session.  When the
                # delayed second leg fails (miner errors and unexpected
                # launch failures alike), never leave the first leg running
                # half of the requested session behind.  The T0 metadata
                # snapshots are intentionally not restored here: they are
                # stale by now and the failed role status files already
                # describe the outcome to the controller.
                LOGGER.error(
                    "delayed secondary miner failed to start; stopping the "
                    "dual session (%s: %s)",
                    type(exc).__name__,
                    str(exc)[:256],
                    extra={
                        "event": "secondary_start_failed",
                        "flight_sheet_id": flight_sheet_id,
                        "error_type": type(exc).__name__,
                    },
                )
                try:
                    self._stop_independent_roles(runtime.resolve())
                except ManagedMinerError as cleanup_exc:
                    LOGGER.error(
                        "could not stop the dual session after a delayed "
                        "secondary start failure",
                        extra={
                            "event": "secondary_start_failure_cleanup_failed",
                            "flight_sheet_id": flight_sheet_id,
                            "cleanup_error": str(cleanup_exc)[:512],
                        },
                        exc_info=True,
                    )
            finally:
                if handle.cancel_event.is_set():
                    # Cancelled while firing (the cancelling stop/switch is
                    # already removing the session): revert only what this
                    # thread itself just launched.
                    try:
                        role_runtime = secondary_manager._runtime()
                        secondary_manager._stop_previous(
                            role_runtime, clear_definitely_absent=True
                        )
                    except ManagedMinerError:
                        LOGGER.error(
                            "could not revert a cancelled delayed secondary "
                            "launch",
                            extra={
                                "event": "secondary_start_cancel_cleanup_failed",
                                "flight_sheet_id": flight_sheet_id,
                            },
                            exc_info=True,
                        )
                    return
                _finish_deferred_secondary_start(handle)

        return starter

    def recover_pending_secondary_start(self) -> dict[str, Any]:
        """Resume a delayed secondary start that outlived an Agent restart."""

        runtime = self._runtime().resolve()
        marker = load_pending_secondary_marker(runtime)
        if marker is None:
            return {"recovered": False, "reason": "no_pending_start"}
        flight_sheet_id = str(marker["flight_sheet_id"])
        primary_manager = self._role_manager(runtime, "primary")
        secondary_manager = self._role_manager(runtime, "secondary")
        primary = primary_manager.running()
        if (
            primary is None
            or primary.get("flight_sheet_id") != flight_sheet_id
        ):
            _remove_pending_secondary_marker(runtime)
            return {"recovered": True, "reason": "primary_not_running"}
        secondary = secondary_manager.running()
        if (
            secondary is not None
            and secondary.get("flight_sheet_id") == flight_sheet_id
        ):
            _remove_pending_secondary_marker(runtime)
            return {"recovered": True, "reason": "secondary_already_running"}
        try:
            document = self._read_active_flight_sheet()
        except ManagedMinerError:
            document = None
        if (
            not isinstance(document, Mapping)
            or not self._independent(document)
            or str(document.get("flight_sheet_id")) != flight_sheet_id
        ):
            _remove_pending_secondary_marker(runtime)
            return {"recovered": True, "reason": "active_sheet_changed"}
        secondary_document = self._role_document(document, "secondary")
        primary_record = primary_manager._read_process_state(
            primary_manager._runtime() / "managed-miner-process.json"
        )
        if primary_record is None:
            _remove_pending_secondary_marker(runtime)
            return {"recovered": True, "reason": "primary_record_missing"}
        starter = self._deferred_secondary_starter(
            runtime,
            document,
            copy.deepcopy(secondary_document),
            primary_pid=int(primary["pid"]),
            primary_api_port=int(primary_record["api_port"]),
        )
        remaining = float(marker["deadline_epoch"]) - time.time()
        if remaining <= 0.0:
            starter(_DeferredSecondaryStartHandle(
                runtime, flight_sheet_id, 0.0,
            ))
            _remove_pending_secondary_marker(runtime)
            return {"recovered": True, "reason": "secondary_started"}
        handle = _DeferredSecondaryStartHandle(
            runtime, flight_sheet_id, remaining,
        )
        _schedule_deferred_secondary_start(
            handle,
            starter,
            packages_dir=self.config.miner_packages_dir,
        )
        return {"recovered": True, "reason": "secondary_rescheduled"}

    def _apply_independent(self, document: Mapping[str, Any]) -> dict[str, Any]:
        runtime = self._runtime()
        primary_document = self._role_document(document, "primary")
        secondary_document = self._role_document(document, "secondary")
        primary_manager = self._role_manager(runtime, "primary")
        secondary_manager = self._role_manager(runtime, "secondary")

        # Validate both packages, executables and complete argument vectors
        # before interrupting a currently healthy mining session.
        primary_plan: dict[str, Any] | None = None
        secondary_plan: dict[str, Any] | None = None
        try:
            primary_plan = primary_manager._prepare_single(primary_document)
            secondary_plan = secondary_manager._prepare_single(
                secondary_document,
                disallowed_api_ports={int(primary_plan["api_port"])},
            )
            return self._apply_independent_plans(
                document, runtime, primary_document, secondary_document,
                primary_manager, secondary_manager, primary_plan, secondary_plan,
            )
        finally:
            primary_manager._release_native_srb_plan(primary_plan)
            secondary_manager._release_native_srb_plan(secondary_plan)

    def _apply_independent_plans(
        self,
        document: Mapping[str, Any],
        runtime: Path,
        primary_document: Mapping[str, Any],
        secondary_document: Mapping[str, Any],
        primary_manager: "ManagedMinerManager",
        secondary_manager: "ManagedMinerManager",
        primary_plan: Mapping[str, Any],
        secondary_plan: Mapping[str, Any],
    ) -> dict[str, Any]:

        manifest_path = runtime / "independent-miners.json"
        metadata_snapshots = self.snapshot_independent_metadata()

        transition_slots = self._preflight_transition_slots(runtime)
        replaced_previous = self._stop_preflighted_transition_slots(
            transition_slots
        )
        primary_result: dict[str, Any] | None = None

        def require_primary_live() -> None:
            primary_still_live = primary_manager.running()
            if (
                primary_still_live is None
                or primary_still_live.get("flight_sheet_id")
                != str(document["flight_sheet_id"])
                or primary_still_live.get("pid") != primary_result.get("pid")
            ):
                raise ManagedMinerError(
                    "primary managed miner exited while the secondary miner "
                    "was starting"
                )

        secondary_delay = secondary_leg_start_delay_seconds()
        deferred_start = secondary_delay > 0.0
        secondary_result: dict[str, Any]
        try:
            primary_result = primary_manager._apply_single(
                primary_document,
                prepared=primary_plan,
                stop_previous=False,
            )
            if not deferred_start:
                secondary_result = secondary_manager._apply_single(
                    secondary_document,
                    prepared=secondary_plan,
                    stop_previous=False,
                )
                require_primary_live()
            else:
                # Delayed secondary-leg start: the primary leg is already
                # committed below and the secondary launch itself happens on
                # the deferred coordinator, so an incoming stop/switch/shutdown
                # command is never blocked by this apply.
                require_primary_live()
            self._write_role_active_sheet(primary_manager, primary_document)
            self._write_role_active_sheet(secondary_manager, secondary_document)
            _write_private_json(
                manifest_path,
                {
                    "version": 1,
                    "mode": "independent",
                    "flight_sheet_id": str(document["flight_sheet_id"]),
                    "roles": ["primary", "secondary"],
                },
            )
            if deferred_start:
                handle = _DeferredSecondaryStartHandle(
                    runtime.resolve(), str(document["flight_sheet_id"]),
                    secondary_delay,
                )
                _schedule_deferred_secondary_start(
                    handle,
                    self._deferred_secondary_starter(
                        runtime,
                        document,
                        copy.deepcopy(secondary_document),
                        primary_pid=int(primary_result["pid"]),
                        primary_api_port=int(primary_result["api_port"]),
                    ),
                    packages_dir=self.config.miner_packages_dir,
                )
                secondary_result = {
                    "managed": True,
                    "miner_type": str(secondary_document["primary"]["miner_type"]),
                    "flight_sheet_id": str(document["flight_sheet_id"]),
                    "deferred_start": True,
                    "delay_seconds": secondary_delay,
                    "replaced_previous": False,
                }
                secondary_fingerprint = secondary_document.get(
                    FLIGHT_RUNTIME_FINGERPRINT_FIELD
                )
                if isinstance(secondary_fingerprint, str):
                    secondary_result[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = (
                        secondary_fingerprint
                    )
            # A successful mode switch has stopped the legacy slot.  Its
            # former XMRig launch config is no longer an authorized identity,
            # but deleting that bounded private cache is not part of proving
            # that both new miners are healthy.
            try:
                _prune_xmrig_session_configs(runtime)
            except ManagedMinerError:
                LOGGER.warning(
                    "could not prune obsolete legacy XMRig configuration",
                    extra={
                        "event": "managed_miner_xmrig_prune_failed",
                        "role": "legacy",
                        "runtime": str(runtime),
                    },
                    exc_info=True,
                )
        except ManagedMinerError as exc:
            # A dual flight is one requested mining session.  Never leave the
            # first newly launched miner running when the second leg failed.
            rollback_errors = (
                list(exc.rollback_errors)
                if isinstance(exc, ManagedMinerTransitionError) else []
            )
            try:
                self._stop_independent_roles(runtime)
            except ManagedMinerError as cleanup_exc:
                rollback_errors.append(str(cleanup_exc))
            try:
                self.restore_independent_metadata(metadata_snapshots)
            except ManagedMinerError as restore_exc:
                rollback_errors.append(str(restore_exc))
            nested_transition = (
                exc if isinstance(exc, ManagedMinerTransitionError) else None
            )
            raise ManagedMinerTransitionError(
                (
                    nested_transition.message
                    if nested_transition is not None
                    else str(exc)
                ),
                state_changed=True,
                old_stopped=replaced_previous,
                target_started=bool(
                    primary_result is not None
                    or (
                        nested_transition is not None
                        and nested_transition.target_started
                    )
                ),
                target_cleanup_unconfirmed=bool(
                    rollback_errors or (nested_transition is not None
                    and nested_transition.target_cleanup_unconfirmed)
                ),
                target_oc_may_have_applied=bool(
                    primary_result is not None
                    and _document_srb_gpu_roles(primary_document)
                )
                or bool(
                    nested_transition is not None
                    and nested_transition.target_oc_may_have_applied
                ),
                rollback_errors=rollback_errors,
            ) from exc
        result = {
            "managed": True,
            "miner_type": primary_result["miner_type"],
            "flight_sheet_id": str(document["flight_sheet_id"]),
            "pid": primary_result["pid"],
            "api_port": primary_result["api_port"],
            "replaced_previous": replaced_previous,
            # With a deferred secondary the durable pending marker plus the
            # startup recovery hook keep the whole session alive across an
            # Agent restart, so the primary's detachment answer applies.
            "survives_agent_restart": bool(
                primary_result.get("survives_agent_restart")
                and (
                    secondary_result.get("survives_agent_restart")
                    if not deferred_start
                    else secondary_result.get("deferred_start") is True
                )
            ),
            "independent": True,
            "roles": {
                "primary": primary_result,
                "secondary": secondary_result,
            },
            "status_path": str(self._status_path(runtime)),
        }
        runtime_fingerprint = document.get(
            FLIGHT_RUNTIME_FINGERPRINT_FIELD
        )
        if isinstance(runtime_fingerprint, str):
            result[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = (
                runtime_fingerprint
            )
        return result

    def apply(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Keep package selection and launch atomic against old-cache cleanup."""
        # A new flight switch supersedes any pending delayed secondary start.
        cancel_deferred_secondary_start("apply")
        if self.config.miner_packages_dir is None:
            return self._apply_unlocked(document)
        from .miner_cache_cleanup import cache_operation_lock, MinerCacheCleanupError
        try:
            with cache_operation_lock(self.config.miner_packages_dir, wait_seconds=30.0):
                return self._apply_unlocked(document)
        except MinerCacheCleanupError as exc:
            raise ManagedMinerError("miner package cache is busy or unsafe") from exc

    def _apply_unlocked(self, document: Mapping[str, Any]) -> dict[str, Any]:
        if self._independent(document):
            return self._apply_independent(document)

        prepared = self._prepare_single(document)
        try:
            return self._apply_prepared_unlocked(document, prepared)
        finally:
            self._release_native_srb_plan(prepared)

    def _apply_prepared_unlocked(
        self, document: Mapping[str, Any], prepared: Mapping[str, Any],
    ) -> dict[str, Any]:
        runtime = prepared["runtime"]
        if not isinstance(runtime, Path):
            raise ManagedMinerError("managed miner launch plan is invalid")
        try:
            transition_slots = self._preflight_transition_slots(runtime)
            replaced_previous = self._stop_preflighted_transition_slots(
                transition_slots
            )
        except ManagedMinerError as exc:
            try:
                self._write_status(
                    runtime,
                    document,
                    "failed",
                    miner_type=str(prepared["miner_type"]),
                    api_port=int(prepared["api_port"]),
                    error=str(exc),
                    gpu_mapping=list(prepared["gpu_mapping"]),
                )
            except ManagedMinerError:
                pass
            raise
        try:
            self._unlink_state(runtime / "independent-miners.json")
            result = self._apply_single(
                document,
                prepared=prepared,
                stop_previous=False,
            )
        except ManagedMinerError as exc:
            nested_transition = (
                exc if isinstance(exc, ManagedMinerTransitionError) else None
            )
            raise ManagedMinerTransitionError(
                (
                    nested_transition.message
                    if nested_transition is not None
                    else str(exc)
                ),
                state_changed=True,
                old_stopped=replaced_previous,
                target_started=bool(
                    nested_transition is not None
                    and nested_transition.target_started
                ),
                target_cleanup_unconfirmed=bool(
                    nested_transition is not None
                    and nested_transition.target_cleanup_unconfirmed
                ),
                target_oc_may_have_applied=bool(
                    nested_transition is not None
                    and nested_transition.target_oc_may_have_applied
                ),
                rollback_errors=(
                    nested_transition.rollback_errors
                    if nested_transition is not None
                    else ()
                ),
            ) from exc
        # A successful switch back to the legacy slot has stopped both role
        # slots.  Remove only their obsolete content-addressed XMRig configs.
        for role in ("primary", "secondary"):
            role_runtime = _managed_role_runtime(runtime, role, create=False)
            if role_runtime.exists():
                try:
                    _prune_xmrig_session_configs(role_runtime)
                except ManagedMinerError:
                    # The target miner is already confirmed running. Obsolete
                    # immutable XMRig launch files are bounded private cache;
                    # a cleanup failure must not turn a successful flight
                    # switch into a rollback or leave the controller uncertain.
                    LOGGER.warning(
                        "could not prune obsolete role XMRig configuration",
                        extra={
                            "event": "managed_miner_xmrig_prune_failed",
                            "role": role,
                            "runtime": str(role_runtime),
                        },
                        exc_info=True,
                    )
        if replaced_previous:
            result["replaced_previous"] = True
        return result
