"""Bounded, local-only cleanup of verified obsolete RigPilot miner cache slots.

A scan is read-only. Cleanup never accepts a path from the controller: it
reconstructs candidates from a protected cache root and a fresh local reference
snapshot under the same operation lock used by package installation and launch.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import threading
import time
from typing import Any, Iterator, Mapping

import psutil

from .config import AgentConfig
from .miner_packages import (
    _MARKER_NAME, _VERIFIED_ARCHIVE_SHA256_FIELD, _verified_cache_target,
    MinerPackageError,
)

CAPABILITY = "managed_mining.cache_cleanup.v1"
SCAN_ACTION = "scan_miner_cache.v1"
CLEANUP_ACTION = "cleanup_miner_cache.v1"
_SCHEMA_VERSION = 1
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9_.+-]{1,80}\Z")
_MAX_CHILDREN = 256
_MAX_TREE_ENTRIES = 20000
_MAX_TOTAL_ENTRIES = 60000
_MAX_SCAN_SECONDS = 12.0
_MAX_CLEANUP_SECONDS = 180.0
_MAX_MARKER_BYTES = 65536
_MAX_FLIGHT_BYTES = 2 * 1024 * 1024
_LOCK_NAME = ".rigpilot-cache-operation.lock"
_PROCESS_NAMES = frozenset({"wsl.exe", "wslhost.exe"})
_THREAD_LOCK = threading.RLock()
_LOCK_LOCAL = threading.local()


class MinerCacheCleanupError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _fail(reason: str) -> None:
    raise MinerCacheCleanupError(reason)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        (getattr(info, "st_file_attributes", 0) or 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_nlink),
            int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns))


def _regular(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        _fail("unsafe_entry")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) != info.st_size or _identity(path.lstat()) != _identity(info):
        _fail("directory_changed")
    return data


def _root(config: AgentConfig) -> tuple[Path, tuple[int, ...]]:
    root = config.miner_packages_dir
    state = config.state_file
    if not config.managed_mining_enabled or root is None or state is None:
        _fail("config_disabled")
    root = Path(root).absolute()
    data = Path(state).absolute().parent
    if root != data / "packages":
        _fail("unsupported_layout")
    for path in (data, root):
        info = path.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            _fail("unsafe_cache_root")
        if platform.system() == "Windows":
            from .agent_updates import _windows_acl_is_update_safe
            if not _windows_acl_is_update_safe(path):
                _fail("unsafe_cache_root")
        elif info.st_uid != os.geteuid() or info.st_mode & 0o022:
            _fail("unsafe_cache_root")
    # A lock file creation or one completed deletion changes directory mtime;
    # device/inode/type are the stable containment identity we need here.
    return root, _identity(root.lstat())[:3]


def _within(path: Path, root: Path) -> str | None:
    try:
        # Compare lexical absolute paths before resolving: a link is never an
        # authority to turn an external path into a cache child.
        candidate = Path(os.path.abspath(path))
        if os.name == "nt":
            left = os.path.normcase(str(candidate))
            prefix = os.path.normcase(str(root)) + os.sep
            if left.startswith(prefix):
                return left[len(prefix):].split(os.sep, 1)[0]
            return None
        return candidate.relative_to(root).parts[0]
    except (ValueError, IndexError, OSError):
        return None


def _read_flights(config: AgentConfig, root: Path) -> tuple[set[str], set[tuple[str, str]], list[str]]:
    protected: set[str] = set()
    identities: set[tuple[str, str]] = set()
    tokens: list[str] = []
    path = config.flight_sheet_file
    if path is None:
        _fail("flight_sheet_unavailable")
    paths = [Path(path), Path(path).with_name("." + Path(path).name + ".pending")]
    if config.managed_miner_runtime_dir is not None:
        runtime = Path(config.managed_miner_runtime_dir)
        paths.extend(runtime / role / "active-flight-sheet.json" for role in ("primary", "secondary"))
    for candidate in paths:
        try:
            raw = _regular(candidate, _MAX_FLIGHT_BYTES)
        except FileNotFoundError:
            tokens.append(candidate.name + ":absent")
            continue
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict):
            _fail("flight_sheet_invalid")
        tokens.append(candidate.name + ":" + hashlib.sha256(raw).hexdigest())
        for role in ("primary", "secondary"):
            leg = document.get(role)
            if leg is None:
                continue
            if not isinstance(leg, dict):
                _fail("flight_sheet_invalid")
            package = leg.get("miner_package")
            if package is None:
                continue
            if not isinstance(package, dict):
                _fail("flight_sheet_invalid")
            package_id, version = package.get("id"), package.get("version")
            if not isinstance(package_id, str) or not isinstance(version, str):
                _fail("flight_sheet_invalid")
            identities.add((package_id, version))
            installed = package.get("installed_path")
            if installed is not None:
                if not isinstance(installed, str) or not installed:
                    _fail("flight_sheet_invalid")
                child = _within(Path(installed), root)
                if child is None:
                    _fail("external_reference")
                protected.add(child)
    return protected, identities, tokens


def _runtime_refs(config: AgentConfig, root: Path) -> tuple[set[str], list[str]]:
    protected: set[str] = set()
    runtime = config.managed_miner_runtime_dir
    if runtime is None:
        _fail("runtime_unavailable")
    runtime = Path(runtime)
    try:
        info = runtime.lstat()
    except FileNotFoundError:
        return protected, ["runtime:absent"]
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        _fail("runtime_unsafe")
    tokens: list[str] = []
    for path in (runtime / "pending-hiveos-start.json",
                 runtime / "flight-transition.json",
                 runtime / "managed-miner-process.json",
                 runtime / "primary/managed-miner-process.json",
                 runtime / "secondary/managed-miner-process.json",
                 runtime / "independent-miners.json"):
        try:
            raw = _regular(path, 128 * 1024)
        except FileNotFoundError:
            tokens.append(path.name + ":absent")
            continue
        # A pending start or malformed process record is an ambiguous active
        # reference. Do not infer that it is safe merely because PID is gone.
        if path.name in {"pending-hiveos-start.json", "flight-transition.json"}:
            _fail("active_session")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            _fail("runtime_unsafe")
        tokens.append(str(path) + ":" + hashlib.sha256(raw).hexdigest())
        if path.name == "independent-miners.json":
            if (value.get("version") != 1 or value.get("mode") != "independent"
                    or value.get("roles") != ["primary", "secondary"]
                    or not isinstance(value.get("flight_sheet_id"), str)):
                _fail("runtime_unsafe")
            for role in ("primary", "secondary"):
                role_sheet = runtime / role / "active-flight-sheet.json"
                if not role_sheet.is_file() or _is_link(role_sheet.lstat()):
                    _fail("unknown_reference")
            # The two role flights and process records are read separately.
            # A normal dual-miner manifest is not a reason to block all cleanup.
            continue
        if value.get("miner_type") == "hiveos":
            # HiveOS sessions were removed; a legacy record cannot be verified
            # well enough to prove which cache entries stay protected.
            _fail("unknown_reference")
        for key in ("executable", "path", "executable_path", "package_path"):
            item = value.get(key)
            if isinstance(item, str) and item:
                child = _within(Path(item), root)
                if child is not None:
                    protected.add(child)
    return protected, tokens


def _marker(target: Path, root: Path) -> dict[str, Any]:
    value = json.loads(_regular(target / _MARKER_NAME, _MAX_MARKER_BYTES).decode("utf-8"))
    if not isinstance(value, dict):
        _fail("invalid_marker")
    for field in ("id", "name", "version", "archive_type", "file_count", "extracted_bytes", "tree_sha256"):
        if field not in value:
            _fail("invalid_marker")
    if (not all(isinstance(value[key], str) and _COMPONENT.fullmatch(value[key])
                for key in ("id", "version"))
            or not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 120
            or value["archive_type"] not in {"zip", "tar.gz"}
            or type(value["file_count"]) is not int or not 1 <= value["file_count"] <= _MAX_TREE_ENTRIES
            or type(value["extracted_bytes"]) is not int or value["extracted_bytes"] < 0
            or not isinstance(value["tree_sha256"], str) or not _HEX.fullmatch(value["tree_sha256"])):
        _fail("invalid_marker")
    family = value.get("managed_os_family")
    if family not in (None, "windows", "linux"):
        _fail("invalid_marker")
    digest = value.get("sha256")
    if digest is not None and (not isinstance(digest, str) or not _HEX.fullmatch(digest)):
        _fail("invalid_marker")
    suffix = "-" + family if family else ""
    legacy_name = f"{value['id']}-{value['version']}{suffix}" + ("-" + digest[:12] if digest else "")
    verified = value.get(_VERIFIED_ARCHIVE_SHA256_FIELD)
    side_name = None
    if isinstance(verified, str) and _HEX.fullmatch(verified):
        side_name = _verified_cache_target(root, value, verified).name
    if target.name not in {legacy_name, side_name}:
        _fail("invalid_marker")
    return value


def _tree(target: Path, deadline: float, budget: list[int]) -> tuple[int, int, str, set[str]]:
    info = target.lstat()
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        _fail("unsafe_reparse")
    total = count = 0
    executables: set[str] = set()
    digest = hashlib.sha256()
    stack = [target]
    while stack:
        if time.monotonic() > deadline:
            _fail("scan_timeout")
        directory = stack.pop()
        before = directory.lstat()
        if _is_link(before) or not stat.S_ISDIR(before.st_mode):
            _fail("unsafe_reparse")
        with os.scandir(directory) as stream:
            entries = sorted(stream, key=lambda item: item.name)
        for entry in entries:
            if time.monotonic() > deadline:
                _fail("scan_timeout")
            budget[0] += 1
            if budget[0] > _MAX_TOTAL_ENTRIES or count >= _MAX_TREE_ENTRIES:
                _fail("scan_limit_exceeded")
            path = Path(entry.path)
            relative = path.relative_to(target).as_posix()
            current = path.lstat()
            if current.st_dev != info.st_dev:
                _fail("external_reference")
            if _is_link(current):
                _fail("unsafe_link")
            elif stat.S_ISDIR(current.st_mode):
                stack.append(path)
            elif stat.S_ISREG(current.st_mode) and current.st_nlink == 1:
                if relative != _MARKER_NAME:
                    count += 1
                    total += current.st_size
                    if current.st_mode & 0o111 or path.suffix.casefold() in {".exe", ".bat", ".cmd"}:
                        executables.add(path.name.casefold())
            else:
                _fail("unsafe_entry")
            encoded_path = relative.encode("utf-8")
            digest.update(len(encoded_path).to_bytes(4, "big") + encoded_path)
            digest.update(repr(_identity(current)).encode("ascii"))
        if _identity(directory.lstat()) != _identity(before):
            _fail("directory_changed")
    if _identity(target.lstat()) != _identity(info):
        _fail("directory_changed")
    return total, count, digest.hexdigest(), executables


def _process_refs(root: Path, candidate_executables: set[str], deadline: float) -> tuple[set[str], bool]:
    protected: set[str] = set()
    wsl_active = False
    try:
        for process in psutil.process_iter(attrs=["name"], ad_value=""):
            if time.monotonic() > deadline:
                _fail("scan_timeout")
            name = (process.info.get("name") or "").casefold()
            if name in _PROCESS_NAMES:
                wsl_active = True
            try:
                executable = process.exe()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError):
                if name in candidate_executables or name in _PROCESS_NAMES:
                    _fail("unknown_reference")
                continue
            child = _within(Path(executable), root) if isinstance(executable, str) and executable else None
            if child is not None:
                protected.add(child)
            elif name in candidate_executables:
                # Rosetta and other executable shims can report their own
                # interpreter in /proc/PID/exe while the process name still
                # matches a cached miner. Unknown ownership must block delete.
                _fail("unknown_reference")
    except (psutil.Error, OSError, ValueError) as exc:
        raise MinerCacheCleanupError("process_state_unavailable") from exc
    return protected, wsl_active


_NOT_SAMPLED: Any = object()


def _disk_usage_sample(root: Path) -> tuple[int, int] | None:
    """Sample total/free bytes exactly once for one report.

    macOS APFS reports purgeable space whose free-byte total drifts between
    consecutive ``disk_usage`` calls on the same volume (observed +431 MiB
    within minutes).  The controller validates every package_cache row against
    the top-level disk fields byte-for-byte, so one sample must back both.
    """
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        return None
    return int(usage.total), int(usage.free)


def _disk(root: Path, usage: Any = _NOT_SAMPLED) -> dict[str, int | str | None]:
    if usage is _NOT_SAMPLED:
        usage = _disk_usage_sample(root)
    total, free = usage if usage is not None else (None, None)
    return {"scope": "package_cache_filesystem", "total_bytes": total, "free_bytes": free}


def _filesystems(config: AgentConfig, root: Path, usage: Any = _NOT_SAMPLED) -> list[dict[str, int | str | None]]:
    rows: list[dict[str, int | str | None]] = []
    if usage is _NOT_SAMPLED:
        usage = _disk_usage_sample(root)
    if usage is None:
        return rows
    try:
        cache_dev = root.stat().st_dev
    except OSError:
        return rows
    total, free = usage
    rows.append({"scope": "package_cache", "filesystem_key": hashlib.sha256(str(cache_dev).encode()).hexdigest()[:16],
                 "total_bytes": total,
                 "free_bytes": free})
    return rows


def _scan(config: AgentConfig, *, deadline: float | None = None) -> tuple[dict[str, Any], dict[str, tuple[str, Path | str, str]]]:
    report: dict[str, Any] = {"schema_version": _SCHEMA_VERSION, "scan_fingerprint": None,
        "can_cleanup": False, "reason": None, "disk": {"scope": "package_cache_filesystem", "total_bytes": None, "free_bytes": None},
        "candidate_logical_bytes": 0, "candidates": [], "protected": [], "unclassified": []}
    selected: dict[str, tuple[str, Path | str, str]] = {}
    try:
        scan_deadline = time.monotonic() + _MAX_SCAN_SECONDS
        deadline = scan_deadline if deadline is None else min(deadline, scan_deadline)
        if time.monotonic() >= deadline:
            _fail("scan_timeout")
        root, root_identity = _root(config)
        usage = _disk_usage_sample(root)
        report["disk"] = _disk(root, usage)
        report["disk"]["filesystems"] = _filesystems(config, root, usage)
        flight_paths, flight_ids, flight_tokens = _read_flights(config, root)
        runtime_paths, runtime_tokens = _runtime_refs(config, root)
        with os.scandir(root) as stream:
            children = sorted(stream, key=lambda item: item.name)
        if len(children) > _MAX_CHILDREN:
            _fail("scan_limit_exceeded")
        budget = [0]
        snapshots = []
        executable_names: set[str] = set()
        entries: list[tuple[Path, dict[str, Any], int, int, str]] = []
        for child in children:
            path = Path(child.path)
            if path.name == _LOCK_NAME:
                info = path.lstat()
                if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    _fail("unsafe_cache_lock")
                continue
            if path.name.startswith(".") and not path.name.startswith(".rigpilot-verified-sha256-v1-"):
                # A previous failed cleanup is not an active installer. Keep
                # that unverified object, but permit unrelated verified old
                # packages to be removed through their own fresh checks.
                residue = re.fullmatch(r"\.rigpilot-cleanup-[0-9a-f]{64}", path.name) is not None
                reason = "cleanup_residue_retained" if residue else "unknown_entry"
                report["unclassified"].append({"kind": "package_cache", "entry_id": path.name[:96], "reason": reason})
                snapshots.append((path.name, reason))
                continue
            try:
                value = _marker(path, root)
                size, count, token, executables = _tree(path, deadline, budget)
                if size != value["extracted_bytes"] or count != value["file_count"]:
                    _fail("content_changed")
            except (OSError, ValueError, UnicodeError, MinerCacheCleanupError, MinerPackageError) as exc:
                reason = getattr(exc, "reason", "invalid_marker" if isinstance(exc, MinerPackageError) else "unreadable")
                if reason in {"scan_limit_exceeded", "scan_timeout"}:
                    raise
                report["unclassified"].append({"kind": "package_cache", "entry_id": path.name[:96], "reason": reason})
                snapshots.append((path.name, reason))
                continue
            executable_names.update(executables)
            entries.append((path, value, size, count, token))
            snapshots.append((path.name, token))
        process_paths, wsl_active = _process_refs(root, executable_names, deadline)
        if time.monotonic() > deadline:
            _fail("scan_timeout")
        for path, value, size, count, token in entries:
            name = path.name
            reason = None
            if name in flight_paths:
                reason = "current_primary"  # Both roles are independently protected below by identity.
            elif (value["id"], value["version"]) in flight_ids:
                reason = "current_secondary"
            elif name in runtime_paths or name in process_paths:
                reason = "active_process"
            elif wsl_active and value.get("managed_os_family") == "linux":
                reason = "active_session"
            if reason:
                report["protected"].append({"kind": "package_cache", "entry_id": name[:96], "package_id": value["id"],
                    "version": value["version"], "reason": reason})
                continue
            candidate_id = hashlib.sha256((name + "\0" + token).encode("utf-8")).hexdigest()
            report["candidates"].append({"kind": "package_cache", "candidate_id": candidate_id, "package_id": value["id"],
                "name": value["name"], "version": value["version"],
                "logical_bytes": size, "file_count": count})
            selected[candidate_id] = ("package_cache", path, token)
            report["candidate_logical_bytes"] += size
        if any(len(report[group]) > _MAX_CHILDREN for group in ("candidates", "protected", "unclassified")):
            _fail("scan_limit_exceeded")
        fingerprint_body = {"root": root_identity, "flights": flight_tokens,
            "flight_ids": sorted(flight_ids), "runtime": runtime_tokens,
            "process_paths": sorted(process_paths),
            "wsl_active": wsl_active, "entries": snapshots,
            "candidates": report["candidates"], "protected": report["protected"],
            "unclassified": report["unclassified"]}
        report["scan_fingerprint"] = hashlib.sha256(json.dumps(fingerprint_body,
            sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
        # Unknown entries do not prevent removal of independently verified old
        # packages, except active install stages whose ownership is ambiguous.
        report["can_cleanup"] = not any(row["entry_id"].startswith(".") and row["reason"] != "cleanup_residue_retained"
            and not row["entry_id"].startswith(".rigpilot-verified-sha256-v1-")
            for row in report["unclassified"])
        if not report["can_cleanup"]:
            report["reason"] = "installation_in_progress"
    except (OSError, ValueError, UnicodeError, MinerCacheCleanupError, MinerPackageError) as exc:
        report["reason"] = getattr(exc, "reason", "scan_unavailable")
    return report, selected


def scan(config: AgentConfig) -> dict[str, Any]:
    """Read metadata only; never download, extract or write a scan journal."""
    return _scan(config)[0]


@contextmanager
def cache_operation_lock(root: Path, *, wait_seconds: float = 3.0) -> Iterator[None]:
    """Serialize Agent-owned package installs, starts and cleanup across processes."""
    root = Path(root)
    if not _THREAD_LOCK.acquire(timeout=wait_seconds):
        _fail("cache_busy")
    try:
        key = os.path.normcase(str(root.absolute()))
        if getattr(_LOCK_LOCAL, "key", None) == key:
            _LOCK_LOCAL.depth += 1
            try:
                yield
            finally:
                _LOCK_LOCAL.depth -= 1
            return
        root.mkdir(parents=True, exist_ok=True)
        parent = root.parent
        parent_info = parent.lstat()
        if _is_link(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
            _fail("unsafe_cache_lock")
        # Keep the persistent lock outside package slots. A failed install
        # must leave an empty cache root, while all installers and starts still
        # serialize on the same Agent-owned data directory file.
        lock = parent / _LOCK_NAME
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or _is_link(lock.lstat()):
                _fail("unsafe_cache_lock")
            deadline = time.perf_counter() + wait_seconds
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.perf_counter() >= deadline:
                        raise MinerCacheCleanupError("cache_busy") from exc
                    time.sleep(0.05)
            try:
                _LOCK_LOCAL.key = key
                _LOCK_LOCAL.depth = 1
                yield
            finally:
                _LOCK_LOCAL.key = None
                _LOCK_LOCAL.depth = 0
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    finally:
        _THREAD_LOCK.release()


def cleanup(config: AgentConfig, scan_fingerprint: str) -> dict[str, Any]:
    if not isinstance(scan_fingerprint, str) or not _HEX.fullmatch(scan_fingerprint):
        _fail("invalid_scan_fingerprint")
    result: dict[str, Any] = {"schema_version": _SCHEMA_VERSION, "state": "failed",
        "requested_scan_fingerprint": scan_fingerprint, "before_scan_fingerprint": None,
        "deleted": [], "skipped": [], "failed": [], "logical_bytes_removed": 0,
        "disk": {"scope": "package_cache_filesystem", "free_bytes_before": None, "free_bytes_after": None, "free_bytes_delta": None},
        "reason": None}
    try:
        root, root_identity = _root(config)
        cleanup_deadline = time.monotonic() + _MAX_CLEANUP_SECONDS
        with cache_operation_lock(root):
            before, selected = _scan(config, deadline=cleanup_deadline)
            result["before_scan_fingerprint"] = before["scan_fingerprint"]
            result["disk"]["free_bytes_before"] = before["disk"]["free_bytes"]
            before_filesystems = before["disk"].get("filesystems", [])
            if not before["can_cleanup"]:
                result["reason"] = before["reason"] or "scan_unavailable"
                return result
            if before["scan_fingerprint"] != scan_fingerprint:
                result.update(state="stale_scan", reason="scan_changed")
                return result
            for position, candidate in enumerate(before["candidates"]):
                candidate_id = candidate["candidate_id"]
                kind, location, token = selected[candidate_id]
                try:
                    if time.monotonic() >= cleanup_deadline:
                        _fail("cleanup_timeout")
                    current_root, current_identity = _root(config)
                    if current_root != root or current_identity != root_identity:
                        _fail("directory_changed")
                    # A late flight or process switch invalidates the whole
                    # deletion plan; never infer continued absence from the old scan.
                    fresh, fresh_selected = _scan(config, deadline=cleanup_deadline)
                    if time.monotonic() >= cleanup_deadline:
                        _fail("cleanup_timeout")
                    if not fresh["can_cleanup"] or candidate_id not in fresh_selected:
                        _fail("reference_changed")
                    if fresh_selected[candidate_id] != (kind, location, token):
                        _fail("directory_changed")
                    path = Path(location)
                    quarantine = root / (".rigpilot-cleanup-" + candidate_id)
                    if quarantine.exists() or quarantine.is_symlink():
                        _fail("unsafe_entry")
                    before_identity = _identity(path.lstat())
                    marker = _marker(path, root)
                    if time.monotonic() >= cleanup_deadline:
                        _fail("cleanup_timeout")
                    os.replace(path, quarantine)
                    if (_is_link(quarantine.lstat()) or
                            _identity(quarantine.lstat())[:2] != before_identity[:2]):
                        _fail("directory_changed")
                    _size, _count, fresh_token, _names = _tree(quarantine,
                        min(cleanup_deadline, time.monotonic() + _MAX_SCAN_SECONDS), [0])
                    if fresh_token != token:
                        _fail("directory_changed")
                    if time.monotonic() >= cleanup_deadline:
                        _fail("cleanup_timeout")
                    shutil.rmtree(quarantine)
                    result["deleted"].append({key: candidate[key] for key in
                        ("kind", "candidate_id", "package_id", "version", "logical_bytes")})
                    result["logical_bytes_removed"] += candidate["logical_bytes"]
                except (OSError, ValueError, MinerCacheCleanupError) as exc:
                    reason = getattr(exc, "reason", None) or (
                        "permission_denied" if isinstance(exc, PermissionError) else
                        "deletion_failed")
                    result["failed"].append({"kind": kind, "candidate_id": candidate_id, "reason": reason})
                    result["skipped"].extend({"kind": later["kind"],
                        "candidate_id": later["candidate_id"], "reason": "not_attempted"}
                        for later in before["candidates"][position + 1:])
                    break
            result["state"] = "partial" if result["failed"] else "succeeded"
            result["reason"] = result["failed"][0]["reason"] if result["failed"] else None
            after_usage = _disk_usage_sample(root)
            after = _disk(root, after_usage)["free_bytes"]
            result["disk"]["free_bytes_after"] = after
            previous = result["disk"]["free_bytes_before"]
            if after is not None and previous is not None:
                result["disk"]["free_bytes_delta"] = after - previous
            after_filesystems = _filesystems(config, root, after_usage)
            by_scope = {row["filesystem_key"]: row for row in after_filesystems}
            result["disk"]["filesystems"] = []
            for row in before_filesystems:
                last = by_scope.get(row["filesystem_key"], {})
                initial_free, final_free = row.get("free_bytes"), last.get("free_bytes")
                result["disk"]["filesystems"].append({"scope": row["scope"],
                    "filesystem_key": row["filesystem_key"],
                    "free_bytes_before": initial_free, "free_bytes_after": final_free,
                    "free_bytes_delta": final_free - initial_free if isinstance(initial_free, int) and isinstance(final_free, int) else None})
    except (OSError, ValueError, MinerCacheCleanupError) as exc:
        result["reason"] = getattr(exc, "reason", "cleanup_unavailable")
    return result
