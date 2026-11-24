"""Bounded native memory queries and static-identity cache for both platforms.

Only worker_main loads either driver backend. The Agent process parses a small
JSON response; a native crash, hang or shutdown failure stays in the child.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .external_process import external_process_environment
from .linux_gpu_telemetry import canonical_pci_id

_TIMEOUT = 2.0
_SUCCESS_TTL = 600.0
_RETRY_TTL = 30.0
_MAX_BYTES = 64 * 1024
_UUID = re.compile(r"GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_VENDORS = frozenset({"Samsung", "Qimonda", "Elpida", "Etron", "Nanya", "SK hynix",
                      "Hynix", "Mosel", "Winbond", "ESMT", "Micron"})
_TYPES = frozenset({"SDRAM", "DDR1", "DDR2", "GDDR2", "GDDR3", "GDDR4", "DDR3",
                   "GDDR5", "LPDDR2", "GDDR5X", "GDDR6", "GDDR6X", "GDDR7",
                   "SDDR2", "SDDR3", "SDDR4", "HBM", "HBM2", "HBM3"})


def _selection(selected: Any) -> list[dict[str, str]]:
    if not isinstance(selected, list) or not 0 < len(selected) <= 64:
        raise ValueError("complete NVIDIA selection required")
    result, uuids, buses = [], set(), set()
    for row in selected:
        uuid = row.get("uuid") if isinstance(row, dict) else None
        bus = canonical_pci_id(row.get("pci_bus_id")) if isinstance(row, dict) else None
        if not isinstance(uuid, str) or _UUID.fullmatch(uuid) is None or bus is None:
            raise ValueError("stable UUID and complete BDF required")
        if uuid.lower() in uuids or bus in buses:
            raise ValueError("duplicate NVIDIA identity")
        uuids.add(uuid.lower()); buses.add(bus)
        result.append({"uuid": uuid, "pci_bus_id": bus,
                       "driver_version": str(row.get("driver_version") or "")[:128],
                       "vbios_version": str(row.get("vbios_version") or "")[:128]})
    return sorted(result, key=lambda row: (row["uuid"].lower(), row["pci_bus_id"]))


def _source(system: str) -> str:
    return "nvapi" if system == "Windows" else "nvml_private"


def _unavailable(selected: list[dict[str, Any]], system: str, reason: str) -> dict[str, dict[str, Any]]:
    return {
        bus: {"uuid": row.get("uuid"), "pci_bus_id": bus,
              "memory_vendor": None, "memory_type": None,
              "memory_identity_source": _source(system),
              "memory_identity_status": "unavailable", "memory_identity_unavailable_reason": reason}
        for row in selected if isinstance(row, dict)
        if (bus := canonical_pci_id(row.get("pci_bus_id"))) is not None
    }


def _normalize_result(payload: Any, selected: list[dict[str, str]], system: str) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("invalid memory worker protocol")
    returned = payload.get("results")
    if not isinstance(returned, dict) or len(returned) > 64:
        raise ValueError("invalid memory worker result")
    expected = {row["pci_bus_id"]: row for row in selected}
    result = _unavailable(selected, system, "native_probe_unavailable")
    seen = set()
    for key, details in returned.items():
        bus = canonical_pci_id(key)
        if bus not in expected or bus in seen or not isinstance(details, dict):
            raise ValueError("unexpected or duplicate worker identity")
        seen.add(bus)
        row = expected[bus]
        identity_ok = (details.get("uuid", "").lower() == row["uuid"].lower()
                       and canonical_pci_id(details.get("pci_bus_id")) == bus)
        actual_driver = details.get("driver_version")
        if actual_driver and row["driver_version"] and actual_driver != row["driver_version"]:
            identity_ok = False
        if not identity_ok or details.get("output_layout_intact") is False:
            result[bus]["memory_identity_unavailable_reason"] = "native_probe_identity_conflict"
            continue
        vendor = details.get("memory_vendor")
        memory_type = details.get("memory_type")
        failed = (details.get("raw_status", 0) != 0
                  or details.get("memory_probe_status") == "query_failed")
        if failed:
            vendor = memory_type = None
        vendor = vendor if isinstance(vendor, str) and vendor in _VENDORS else None
        memory_type = memory_type if isinstance(memory_type, str) and memory_type in _TYPES else None
        if vendor == "Hynix":
            vendor = "SK hynix"
        status = "complete" if vendor and memory_type else "partial" if vendor or memory_type else "unavailable"
        reason = None if status == "complete" else (
            "driver_reported_partial_identity" if status == "partial" else "driver_memory_identity_unknown")
        if failed:
            reason = "native_probe_unavailable"
        result[bus].update(memory_vendor=vendor, memory_type=memory_type,
                           memory_identity_status=status, memory_identity_unavailable_reason=reason)
    return result


def _worker_command() -> tuple[list[str], str]:
    executable = str(Path(sys.executable).absolute())
    if getattr(sys, "frozen", False):
        return [executable, "--nvidia-memory-worker"], str(Path(executable).parent)
    return [executable, "-m", "agent.nvidia_memory", "--worker"], str(Path(__file__).resolve().parent.parent)


def _query(selected: list[dict[str, str]], system: str) -> dict[str, dict[str, Any]]:
    argv, cwd = _worker_command()
    # A frozen self-worker reuses this bundle, unlike an external system tool.
    # Its bootloader SUBPROCESS path needs the bundle's current loader path.
    inherited = os.environ.copy() if getattr(sys, "frozen", False) else external_process_environment(system)
    keys = {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR",
            "LANG", "LC_ALL", "LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG"}
    env = {key: value for key, value in inherited.items() if key in keys or key.startswith("_PYI_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    arguments: dict[str, Any] = {"cwd": cwd, "env": env, "stdin": subprocess.PIPE,
                                 "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
                                 "shell": False}
    if system == "Windows":
        arguments["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        # Linux Full/Online are onefile: include a possible bootloader child in
        # this fresh group. Windows's current onedir worker is one direct PID.
        arguments["start_new_session"] = True
    process = None
    timed_out = False
    try:
        process = subprocess.Popen(argv, **arguments)
        request = json.dumps({"schema_version": 1, "system": system, "selected": selected}).encode("utf-8")
        output, _ = process.communicate(request, timeout=_TIMEOUT)
        if process.returncode != 0:
            return _unavailable(selected, system, "native_probe_crashed")
        if not output or len(output) > _MAX_BYTES:
            raise ValueError("memory worker output outside limit")
        return _normalize_result(json.loads(output.decode("utf-8")), selected, system)
    except subprocess.TimeoutExpired:
        timed_out = True
        return _unavailable(selected, system, "native_probe_timeout")
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError):
        return _unavailable(selected, system, "native_probe_unavailable")
    finally:
        if process is not None and (timed_out or process.poll() is None):
            # Only this directly created native worker is signalled, never a
            # miner or an Agent process found through global process scans.
            try:
                if system == "Linux":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass
            try:
                process.communicate(timeout=0.25)
            except (OSError, subprocess.TimeoutExpired):
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()


class MemoryIdentityCache:
    """Coalesce native probes; never retain labels across identity changes."""

    def __init__(self, query: Callable[..., Any] = _query, clock: Callable[[], float] = time.monotonic):
        self._query, self._clock = query, clock
        self._lock = threading.Lock()
        # Publish one immutable entry reference, so concurrent readers cannot
        # mix a new identity key with a prior result or expiry.
        self._entry: tuple[tuple[Any, ...], float, dict[str, dict[str, Any]]] | None = None

    def collect(self, selected: list[dict[str, Any]], system: str) -> dict[str, dict[str, Any]]:
        if system not in {"Linux", "Windows"}:
            return {}
        try:
            selection = _selection(selected)
        except (ValueError, TypeError, AttributeError):
            return _unavailable(selected, system, "native_probe_identity_conflict")
        key = (system, tuple(tuple(row.values()) for row in selection))
        entry = self._entry
        if entry is not None and entry[0] == key and self._clock() < entry[1]:
            return copy.deepcopy(entry[2])
        if not self._lock.acquire(blocking=False):
            return _unavailable(selection, system, "native_probe_unavailable")
        try:
            entry = self._entry
            if entry is not None and entry[0] == key and self._clock() < entry[1]:
                return copy.deepcopy(entry[2])
            # Invalid/expired positive entries are not returned after a failed refresh.
            self._entry = None
            try:
                result = self._query(selection, system)
            except Exception:
                result = _unavailable(selection, system, "native_probe_unavailable")
            complete = bool(result) and all(row.get("memory_identity_status") == "complete" for row in result.values())
            ttl = _SUCCESS_TTL if complete and all(row["driver_version"] for row in selection) else _RETRY_TTL
            self._entry = (key, self._clock() + ttl, copy.deepcopy(result))
            return copy.deepcopy(result)
        finally:
            self._lock.release()


_CACHE = MemoryIdentityCache()


def collect_nvidia_memory(selected: list[dict[str, Any]], system: str) -> dict[str, dict[str, Any]]:
    return _CACHE.collect(selected, system)


def worker_main() -> int:
    """Fixed config-free child entry; never starts AgentRuntime or registration."""
    try:
        if os.name == "posix":
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        raw = sys.stdin.buffer.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("memory worker request too large")
        request = json.loads(raw)
        system = platform.system()
        if request.get("schema_version") != 1 or request.get("system") != system:
            raise ValueError("memory worker platform mismatch")
        selected = _selection(request.get("selected"))
        if system == "Linux":
            from .nvidia_memory_linux import collect_linux_memory
            result = collect_linux_memory(selected)
        elif system == "Windows":
            from .nvidia_nvapi import collect_windows_memory
            result = collect_windows_memory(selected)
        else:
            result = {}
        response = {"schema_version": 1, "results": result}
        output = json.dumps(response, allow_nan=False, separators=(",", ":"))
        if len(output.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("memory worker response too large")
    except Exception:
        output = '{"schema_version":1,"results":{}}'
    print(output, flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    if not parser.parse_args().worker:
        parser.error("fixed worker invocation required")
    raise SystemExit(worker_main())
