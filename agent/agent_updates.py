"""Authenticated, version-slotted Windows portable Agent updates.

The running Agent only downloads and preflights a release.  A fixed updater
script copied from the *currently trusted* portable installation performs the
switch after the command result has reached the controller.  No archive member
is ever interpreted as a command.
"""

from __future__ import annotations

import hashlib
import ctypes
from ctypes import wintypes
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import psutil

from .config import AgentConfig
from .fsutil import is_link_or_reparse
from .external_process import external_process_environment
from .managed_miners import ManagedMinerError, ManagedMinerManager
from .release_contract import (
    WINDOWS_REQUIRED_PAYLOAD,
    WindowsArchivePathError,
    canonical_windows_archive_parts,
)
from .state import AgentState, _fsync_directory
from .version import __version__


AGENT_UPDATE_ACTION = "agent_update.v1"
AGENT_UPDATE_READINESS_SCHEMA_VERSION = 1
AGENT_BOOTSTRAP_PROTOCOL = "agent_bootstrap.v1"
AGENT_BOOTSTRAP_SCHEMA_VERSION = 1
AGENT_LAUNCHER_PROTOCOL_VERSION = 1
AGENT_UPDATER_PROTOCOL_VERSION = 1
AGENT_UPDATE_PAYLOAD_FIELDS = {
    "release_id",
    "version",
    "sha256",
    "size",
    "download_url",
}
UPDATE_MANIFEST_NAME = "agent-release.json"
UPDATE_SCRIPT_NAME = "RigPilotAgentUpdater.ps1"
PORTABLE_LAUNCHER_NAME = "portable-agent.ps1"
LINUX_UPDATE_SCRIPT_NAME = "RigPilotAgentUpdater.py"
LINUX_LAUNCHER_NAME = "RigPilotAgentLauncher.sh"
LINUX_PROGRAM_ROOT = Path("/opt/rigpilot-agent")
LINUX_DATA_ROOT = Path("/var/lib/rigpilot-agent")
LINUX_SYSTEMD_RUN = Path("/usr/bin/systemd-run")
LINUX_SYSTEMCTL = Path("/usr/bin/systemctl")
LINUX_PYTHON = Path("/usr/bin/python3")
_IMMUTABLE_V1_PROGRAM_FILES = frozenset(
    {UPDATE_SCRIPT_NAME.casefold(), PORTABLE_LAUNCHER_NAME.casefold()}
)
_VERSION = re.compile(
    r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$"
)
_DOWNLOAD_PATH = re.compile(
    r"^/api/agent/updates/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/download$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MANAGED_MINER_TYPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ACTIVATION_ID = re.compile(r"^[0-9a-f]{32}$")
_COMMAND_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MAX_ARCHIVE_ENTRIES = 16_384
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
# Free-space preflight before an update is staged.  A full root filesystem is
# the leading offline-after-update failure: the fixed updater cannot free the
# staged payload while its rollback receipt write keeps failing, and neither
# Agent version can unpack.  Refuse to enter that state instead.
UPDATE_DISK_HEADROOM_FLOOR_BYTES = 512 * 1024 * 1024
UPDATE_DISK_HEADROOM_SIZE_MULTIPLIER = 2.5
# The fixed v1 updaters wait 90s (Linux) / 75s (Windows) for the replacement's
# confirmation receipt.  These bounds mirror them for the Agent-side fallback
# recovery: act only well after the trusted updater itself would have rolled
# the activation back, so a live updater is never raced.
LINUX_UPDATER_CONFIRMATION_SECONDS = 90.0
LINUX_UPDATER_PREPARE_SECONDS = 120.0
LINUX_ACTIVATION_EXTRA_GRACE_SECONDS = 120.0
PORTABLE_UPDATER_CONFIRMATION_SECONDS = 75.0
PORTABLE_ACTIVATION_EXTRA_GRACE_SECONDS = 120.0
LINUX_SELF_RECOVERY_MIN_INTERVAL_SECONDS = 60.0
# Bounded heartbeat cadence while an activation is pending but unconfirmed.
# Two authenticated heartbeats must land inside the updater's confirmation
# window even on slow disks; the normal interval only applies once the slot is
# confirmed and reported.
ACTIVATION_HEARTBEAT_SECONDS = 10.0
_MAX_RESUME_MARKER_SCAN = 8


class AgentUpdateError(RuntimeError):
    """An update was rejected or could not be staged safely."""


def _is_managed_miner_type(value: object) -> bool:
    """Accept a bounded canonical adapter name without a baked-in catalog.

    Update recovery is deliberately miner-agnostic.  The active process and
    resume marker are still tied to the Agent's verified local identity, but a
    future declarative adapter must not require shipping a new updater merely
    to pass an enum membership check.
    """

    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value == value.strip().lower()
        and _MANAGED_MINER_TYPE.fullmatch(value) is not None
    )


def _linux_update_log_detail(path: Path, *, limit: int = 4096) -> str:
    """Return one bounded single-line diagnostic suitable for command results."""

    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
            raw = stream.read(limit)
    except OSError:
        return ""
    return " ".join(raw.decode("utf-8", errors="replace").split())[-1200:]


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value)
    if match is None:
        raise AgentUpdateError("agent update version must use strict X.Y.Z format")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def validate_agent_update_payload(
    payload: Mapping[str, Any], *, current_version: str = __version__
) -> dict[str, Any]:
    if set(payload) != AGENT_UPDATE_PAYLOAD_FIELDS:
        raise AgentUpdateError(
            "agent update payload must contain exactly: "
            + ", ".join(sorted(AGENT_UPDATE_PAYLOAD_FIELDS))
        )
    release_id = payload.get("release_id")
    size = payload.get("size")
    version = payload.get("version")
    sha256 = payload.get("sha256")
    download_url = payload.get("download_url")
    if (
        not isinstance(release_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            release_id,
        )
        is None
    ):
        raise AgentUpdateError("agent update release_id must be a lowercase UUID")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise AgentUpdateError("agent update size must be a positive integer")
    if not isinstance(version, str):
        raise AgentUpdateError("agent update version must be a string")
    if _version_tuple(version) <= _version_tuple(current_version):
        raise AgentUpdateError("agent update target must be newer than the running version")
    if not isinstance(sha256, str) or _DIGEST.fullmatch(sha256) is None:
        raise AgentUpdateError("agent update sha256 must be lowercase SHA-256")
    if not isinstance(download_url, str):
        raise AgentUpdateError("agent update download_url must be a relative path")
    parsed = urlsplit(download_url)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path != download_url
        or download_url.startswith("//")
        or _DOWNLOAD_PATH.fullmatch(download_url) is None
    ):
        raise AgentUpdateError(
            "agent update download_url must be an allowed same-controller path"
        )
    return {
        "release_id": release_id,
        "version": version,
        "sha256": sha256,
        "size": size,
        "download_url": download_url,
    }


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return is_link_or_reparse(info)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _free_bytes(path: Path) -> int:
    """Return free bytes visible to this process on one local filesystem."""

    usage = shutil.disk_usage(path)
    return int(usage.free)


def required_update_disk_headroom(package_size: int) -> int:
    """Staging + activation + unpack headroom for one online update."""

    return max(
        UPDATE_DISK_HEADROOM_FLOOR_BYTES,
        int(UPDATE_DISK_HEADROOM_SIZE_MULTIPLIER * float(package_size)),
    )


def assert_update_disk_headroom(
    paths: tuple[Path, ...], package_size: int
) -> None:
    """Refuse to stage an update that could fill the installation disk.

    The staged package, its extracted bundle, the new version slot and the
    running Agent's own unpack directory all share the mining rig's small root
    filesystem.  Entering an update without headroom is exactly how a device
    ends up offline with both versions unable to start, so this check fails
    the command while the old Agent is still healthy and online.
    """

    required = required_update_disk_headroom(package_size)
    for path in paths:
        free = _free_bytes(path)
        if free < required:
            raise AgentUpdateError(
                "Agent update needs at least "
                f"{required // (1024 * 1024)} MiB free on {path} "
                f"but only {free // (1024 * 1024)} MiB is available"
            )


def _windows_program_data() -> Path:
    """Return Windows CommonApplicationData without trusting process environment."""

    buffer = ctypes.create_unicode_buffer(32_768)
    try:
        result = ctypes.windll.shell32.SHGetFolderPathW(
            None, 0x0023, None, 0, buffer
        )
    except (AttributeError, OSError) as exc:
        raise AgentUpdateError("Windows CommonApplicationData is unavailable") from exc
    if result != 0 or not buffer.value:
        raise AgentUpdateError("Windows CommonApplicationData is unavailable")
    return Path(os.path.abspath(buffer.value))

def _windows_acl_is_update_safe(path: Path) -> bool:
    """Fail closed unless a Windows path has a protected administrator DACL."""

    if platform.system() != "Windows":
        return False
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi = ctypes.windll.advapi32
    kernel = ctypes.windll.kernel32
    result = advapi.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001 | 0x00000004,  # OWNER + DACL
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0 or not descriptor.value or not owner.value or not dacl.value:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        return False

    def sid_string(pointer: int) -> str | None:
        output = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(
            ctypes.c_void_p(pointer), ctypes.byref(output)
        ):
            return None
        try:
            return str(output.value)
        finally:
            kernel.LocalFree(output)

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    try:
        owner_sid = sid_string(int(owner.value))
        if owner_sid != "S-1-5-32-544":
            return False
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ) or not (int(control.value) & 0x1000):  # SE_DACL_PROTECTED
            return False
        size = AclSizeInformation()
        if not advapi.GetAclInformation(
            dacl, ctypes.byref(size), ctypes.sizeof(size), 2
        ):
            return False
        dangerous = (
            0x00000040  # FILE_DELETE_CHILD
            | 0x00010000  # DELETE
            | 0x00040000  # WRITE_DAC
            | 0x00080000  # WRITE_OWNER
            | 0x00000116  # writable file/directory rights
            | 0x10000000  # GENERIC_ALL
            | 0x40000000  # GENERIC_WRITE
        )
        full_control = 0x001F01FF
        trusted_full = {"S-1-5-18": False, "S-1-5-32-544": False}
        for index in range(int(size.AceCount)):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                return False
            header = AceHeader.from_address(int(ace.value))
            if header.AceType == 1:  # ACCESS_DENIED_ACE_TYPE
                continue
            if header.AceType != 0 or header.AceSize < 12:
                # Object/callback/custom allow ACEs are not emitted by the
                # portable ACL hardener and are rejected instead of parsed
                # optimistically.
                return False
            mask = ctypes.c_uint32.from_address(int(ace.value) + 4).value
            sid = sid_string(int(ace.value) + 8)
            if sid is None:
                return False
            if sid in trusted_full and mask & full_control == full_control:
                trusted_full[sid] = True
            elif mask & dangerous:
                return False
        return all(trusted_full.values())
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    finally:
        kernel.LocalFree(descriptor)


def _portable_root(
    config: AgentConfig,
    *,
    system_name: str | None = None,
    executable: Path | None = None,
    frozen: bool | None = None,
    root_override: Path | None = None,
) -> tuple[Path, Path]:
    if (system_name or platform.system()) != "Windows":
        raise AgentUpdateError("agent updates are supported only by Windows portable builds")
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if root_override is None and not is_frozen:
        raise AgentUpdateError("source and non-frozen Agents cannot self-update")
    raw_executable = Path(os.path.abspath(os.fspath(executable or sys.executable)))
    if root_override is None and raw_executable.name.casefold() != "rigpilotagent.exe":
        raise AgentUpdateError("the running executable is not a portable RigPilot Agent")

    if root_override is not None:
        raw_root = Path(os.path.abspath(os.fspath(root_override)))
    elif raw_executable.parent.parent.name.casefold() == "versions":
        raw_root = raw_executable.parent.parent.parent
    else:
        raw_root = raw_executable.parent
    if root_override is None:
        raw_program_data = _windows_program_data()
        protected_root = raw_program_data / "RigPilotAgentPortable"
        if (
            not raw_program_data.is_dir()
            or _is_link_or_reparse(raw_program_data)
            or os.path.normcase(str(raw_root))
            != os.path.normcase(str(protected_root))
        ):
            raise AgentUpdateError(
                "SYSTEM updates require the portable root at "
                r"%ProgramData%\RigPilotAgentPortable"
            )
    raw_data_dir = raw_root / "data"
    raw_versions_root = raw_root / "versions"
    unsafe_paths = [raw_root, raw_data_dir, raw_executable]
    if os.path.lexists(raw_versions_root):
        if not raw_versions_root.is_dir():
            raise AgentUpdateError("portable versions path is not a directory")
        unsafe_paths.append(raw_versions_root)
    in_version_slot = (
        os.path.normcase(str(raw_executable.parent.parent))
        == os.path.normcase(str(raw_versions_root))
    )
    if in_version_slot:
        raw_slot = raw_executable.parent
        if not raw_slot.is_dir():
            raise AgentUpdateError("active Agent version slot is missing")
        unsafe_paths.append(raw_slot)
    if (
        not raw_root.is_dir()
        or not raw_data_dir.is_dir()
        or not raw_executable.is_file()
        or any(_is_link_or_reparse(path) for path in unsafe_paths)
    ):
        raise AgentUpdateError("portable root, data, executable, or version slot is unsafe")
    root = raw_root.resolve(strict=True)
    data_dir = raw_data_dir.resolve(strict=True)
    executable_path = raw_executable.resolve(strict=True)
    if config.state_file.parent.resolve() != data_dir:
        raise AgentUpdateError("agent state is not in this portable folder's data directory")
    for required in (root / PORTABLE_LAUNCHER_NAME, root / UPDATE_SCRIPT_NAME):
        if (
            not required.is_file()
            or _is_link_or_reparse(required)
            or required.resolve().parent != root
        ):
            raise AgentUpdateError(f"trusted portable update component is missing: {required.name}")
    if root_override is None and not all(
        _windows_acl_is_update_safe(path)
        for path in (
            root,
            data_dir,
            root / PORTABLE_LAUNCHER_NAME,
            root / UPDATE_SCRIPT_NAME,
        )
    ):
        raise AgentUpdateError("portable folder ACL is not safe for SYSTEM updates")
    ready_path = data_dir / "agent-update-ready.json"
    try:
        ready_info = ready_path.lstat()
        if _is_link_or_reparse(ready_path) or ready_info.st_size > 32_768:
            raise AgentUpdateError("portable update ACL marker is unsafe")
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentUpdateError(
            "portable autostart ACL protection has not been installed"
        ) from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpdateError("portable update ACL marker is invalid") from exc
    updater_digest = _sha256_file(root / UPDATE_SCRIPT_NAME)
    if (
        not isinstance(ready, dict)
        or set(ready) != {"schema_version", "acl_protected", "updater_sha256"}
        or ready.get("schema_version") != 1
        or ready.get("acl_protected") is not True
        or ready.get("updater_sha256") != updater_digest
    ):
        raise AgentUpdateError("portable update ACL marker does not match the updater")
    return root, executable_path


def _linux_plain_root_owned(
    path: Path,
    *,
    directory: bool,
    enforce_owner: bool,
) -> bool:
    """Return whether *path* is a non-link, non-writable trusted Linux node."""

    try:
        info = path.lstat()
    except OSError:
        return False
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or stat.S_ISLNK(info.st_mode):
        return False
    if not enforce_owner:
        return True
    return (
        int(getattr(info, "st_uid", -1)) == 0
        and info.st_mode & 0o022 == 0
    )


def _linux_protected_directory_chain(directory: Path) -> bool:
    current = directory
    while True:
        if not _linux_plain_root_owned(
            current, directory=True, enforce_owner=True
        ):
            return False
        if current.parent == current:
            return True
        current = current.parent


def _linux_trusted_system_executable(path: Path) -> bool:
    """Validate an absolute root-owned executable, allowing a safe final symlink.

    Ubuntu exposes ``/usr/bin/python3`` as a root-owned symlink.  Symlink mode
    bits are not meaningful on Linux, so the link owner and both lexical and
    resolved directory chains are checked while the resolved target must be a
    protected regular executable.
    """

    if not path.is_absolute() or not _linux_protected_directory_chain(path.parent):
        return False
    try:
        lexical = path.lstat()
        if stat.S_ISLNK(lexical.st_mode):
            if int(getattr(lexical, "st_uid", -1)) != 0:
                return False
        elif not _linux_plain_root_owned(
            path, directory=False, enforce_owner=True
        ):
            return False
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return (
        _linux_protected_directory_chain(resolved.parent)
        and _linux_plain_root_owned(
            resolved, directory=False, enforce_owner=True
        )
        and os.access(resolved, os.X_OK)
    )


def _linux_ensure_protected_data_directories(
    data_root: Path,
    names: tuple[str, ...],
    *,
    enforce_owner: bool,
) -> Path:
    current = data_root
    for name in names:
        candidate = current / name
        created = False
        try:
            candidate.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise AgentUpdateError("Linux Agent staging directory cannot be created") from exc
        if not _linux_plain_root_owned(
            candidate, directory=True, enforce_owner=enforce_owner
        ):
            raise AgentUpdateError("Linux Agent staging directory is unsafe")
        if enforce_owner and stat.S_IMODE(candidate.lstat().st_mode) != 0o700:
            raise AgentUpdateError("Linux Agent staging directory permissions are unsafe")
        if created:
            _fsync_directory(candidate)
            _fsync_directory(current)
        current = candidate
    return current


def _linux_update_layout(
    config: AgentConfig,
    *,
    system_name: str | None = None,
    executable: Path | None = None,
    frozen: bool | None = None,
    root_override: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Validate the fixed, root-protected systemd installation boundary.

    The override exists only for deterministic unit tests.  Production Agents
    must run from the active version slot below ``/opt/rigpilot-agent`` and
    keep all mutable receipts below ``/var/lib/rigpilot-agent``.
    """

    if (system_name or platform.system()) != "Linux":
        raise AgentUpdateError("Linux Agent updates require Linux")
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if root_override is None and not is_frozen:
        raise AgentUpdateError("source and non-frozen Agents cannot self-update")
    if root_override is None and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        raise AgentUpdateError("Linux Agent updates require the root systemd service")

    root = Path(os.path.abspath(os.fspath(root_override or LINUX_PROGRAM_ROOT)))
    data_root = (
        Path(os.path.abspath(os.fspath(config.state_file.parent)))
        if root_override is not None
        else LINUX_DATA_ROOT
    )
    active_path = root / "active-version"
    try:
        active_info = active_path.lstat()
        if (
            stat.S_ISLNK(active_info.st_mode)
            or not stat.S_ISREG(active_info.st_mode)
            or active_info.st_size > 64
        ):
            raise AgentUpdateError("Linux active-version pointer is unsafe")
        active_version = active_path.read_text(encoding="ascii").strip()
    except FileNotFoundError as exc:
        raise AgentUpdateError("Linux active-version pointer is missing") from exc
    except (OSError, UnicodeError) as exc:
        raise AgentUpdateError("Linux active-version pointer is invalid") from exc
    if _VERSION.fullmatch(active_version) is None or active_version != __version__:
        raise AgentUpdateError("Linux active-version pointer is invalid")

    expected_executable = root / "versions" / active_version / "RigPilotAgent"
    raw_executable = Path(os.path.abspath(os.fspath(executable or sys.executable)))
    enforce_owner = root_override is None
    required_directories = (root, root / "versions", expected_executable.parent, data_root)
    required_files = (
        expected_executable,
        root / LINUX_LAUNCHER_NAME,
        root / LINUX_UPDATE_SCRIPT_NAME,
        active_path,
        config.state_file,
    )
    if not all(
        _linux_plain_root_owned(path, directory=True, enforce_owner=enforce_owner)
        for path in required_directories
    ) or not all(
        _linux_plain_root_owned(path, directory=False, enforce_owner=enforce_owner)
        for path in required_files
    ):
        raise AgentUpdateError("Linux Agent update layout is missing, linked, or writable")
    if raw_executable.resolve(strict=True) != expected_executable.resolve(strict=True):
        raise AgentUpdateError("Linux Agent is not running from its active version slot")
    if config.state_file.resolve(strict=True).parent != data_root.resolve(strict=True):
        raise AgentUpdateError("Linux Agent state is outside its protected data directory")
    if root_override is None:
        data_info = data_root.lstat()
        if (
            config.state_file != data_root / "state.json"
            or int(getattr(data_info, "st_uid", -1)) != 0
            or stat.S_IMODE(data_info.st_mode) != 0o700
            or not all(
            _linux_protected_directory_chain(path.parent)
            for path in (root, data_root)
            )
        ):
            raise AgentUpdateError("Linux Agent fixed installation boundary is unsafe")
        for command in (LINUX_SYSTEMD_RUN, LINUX_SYSTEMCTL, LINUX_PYTHON):
            if not _linux_trusted_system_executable(command):
                raise AgentUpdateError(f"required Linux updater command is unsafe: {command}")
    return root.resolve(strict=True), data_root.resolve(strict=True), expected_executable.resolve(strict=True)


def _agent_update_readiness_error_code(exc: AgentUpdateError) -> str:
    """Classify a local update failure without exposing paths or ACL details."""

    message = str(exc).casefold()
    if "autostart acl protection has not been installed" in message:
        return "marker_missing"
    if "acl marker" in message:
        return "marker_invalid"
    if "fixed protected programdata root" in message or "portable root at" in message:
        return "root_mismatch"
    if (
        "trusted portable update component is missing" in message
        or "required linux updater command is unsafe" in message
        or "linux agent update layout is missing" in message
    ):
        return "helper_missing"
    if (
        "acl is not safe" in message
        or "linked, or writable" in message
        or "installation boundary is unsafe" in message
    ):
        return "acl_or_owner_mismatch"
    if "agent state is not" in message:
        return "state_mismatch"
    if "active-version pointer" in message or "active version pointer" in message:
        return "active_version_mismatch"
    if (
        "source and non-frozen" in message
        or "updates require linux" in message
        or "updates require the root systemd service" in message
        or "supported only by windows portable builds" in message
        or "running executable is not a portable" in message
    ):
        return "unsupported_runtime"
    return "unsafe_layout"


def _agent_update_bootstrap_metadata(
    *,
    system_name: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Describe fixed update components without exposing installation paths."""

    system = system_name or platform.system()
    if system == "Windows":
        platform_name = "windows"
        launcher_name = PORTABLE_LAUNCHER_NAME
        updater_name = UPDATE_SCRIPT_NAME
    elif system == "Linux":
        platform_name = "linux"
        launcher_name = LINUX_LAUNCHER_NAME
        updater_name = LINUX_UPDATE_SCRIPT_NAME
    else:
        platform_name = "unsupported"
        launcher_name = None
        updater_name = None

    launcher_sha256: str | None = None
    updater_sha256: str | None = None
    if root is not None and launcher_name is not None and updater_name is not None:
        try:
            launcher_sha256 = _sha256_file(root / launcher_name)
            updater_sha256 = _sha256_file(root / updater_name)
        except OSError as exc:
            raise AgentUpdateError(
                "trusted bootstrap component could not be hashed"
            ) from exc
    return {
        "schema_version": AGENT_BOOTSTRAP_SCHEMA_VERSION,
        "protocol": AGENT_BOOTSTRAP_PROTOCOL,
        "platform": platform_name,
        "launcher": {
            "name": launcher_name,
            "protocol_version": AGENT_LAUNCHER_PROTOCOL_VERSION,
            "sha256": launcher_sha256,
        },
        "updater": {
            "name": updater_name,
            "protocol_version": AGENT_UPDATER_PROTOCOL_VERSION,
            "sha256": updater_sha256,
        },
        "immutable": True,
    }


def agent_update_readiness(config: AgentConfig) -> dict[str, Any]:
    """Return a bounded, secret-free online-update readiness diagnostic."""

    system_name = platform.system()
    bootstrap = _agent_update_bootstrap_metadata(system_name=system_name)
    if not config.allow_agent_updates:
        return {
            "schema_version": AGENT_UPDATE_READINESS_SCHEMA_VERSION,
            "protocol": AGENT_UPDATE_ACTION,
            "available": False,
            "reason": "config_disabled",
            "bootstrap": bootstrap,
        }
    try:
        if system_name == "Linux":
            root, _data_root, _executable = _linux_update_layout(config)
        else:
            root, _executable = _portable_root(config)
        bootstrap = _agent_update_bootstrap_metadata(
            system_name=system_name,
            root=root,
        )
    except AgentUpdateError as exc:
        return {
            "schema_version": AGENT_UPDATE_READINESS_SCHEMA_VERSION,
            "protocol": AGENT_UPDATE_ACTION,
            "available": False,
            "reason": _agent_update_readiness_error_code(exc),
            "bootstrap": bootstrap,
        }
    return {
        "schema_version": AGENT_UPDATE_READINESS_SCHEMA_VERSION,
        "protocol": AGENT_UPDATE_ACTION,
        "available": True,
        "reason": None,
        "bootstrap": bootstrap,
    }


def _safe_archive_name(name: str) -> tuple[str, ...]:
    try:
        return canonical_windows_archive_parts(name)
    except WindowsArchivePathError:
        raise AgentUpdateError("agent update archive contains an unsafe path") from None


def _zip_entry_is_regular(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    return file_type in {0, stat.S_IFREG}


def _load_manifest(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, expected_version: str
) -> dict[str, Any]:
    if info.file_size > _MAX_MANIFEST_BYTES:
        raise AgentUpdateError("agent update manifest is too large")
    try:
        manifest = json.loads(archive.read(info).decode("utf-8"))
    except (UnicodeError, ValueError, OSError, RuntimeError) as exc:
        raise AgentUpdateError("agent update manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "version",
        "platform",
        "architecture",
        "files",
    }:
        raise AgentUpdateError("agent update manifest has invalid fields")
    if (
        isinstance(manifest.get("schema_version"), bool)
        or manifest.get("schema_version") != 1
        or manifest.get("version") != expected_version
        or manifest.get("platform") != "windows"
        or manifest.get("architecture") != "x86_64"
        or not isinstance(manifest.get("files"), dict)
    ):
        raise AgentUpdateError("agent update manifest does not match this release")
    files = manifest["files"]
    for name, digest in files.items():
        if (
            not isinstance(name, str)
            or not name
            or "/".join(_safe_archive_name(name)) != name
            or name.split("/", 1)[0].casefold() in {"data", "versions"}
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            raise AgentUpdateError("agent update manifest contains an invalid file entry")
    return manifest


def _validate_and_extract_release(
    package_path: Path,
    destination: Path,
    *,
    expected_version: str,
    expanded_limit: int,
) -> Path:
    try:
        archive = zipfile.ZipFile(package_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise AgentUpdateError("agent update package is not a valid ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise AgentUpdateError("agent update archive entry count is invalid")
        seen: set[str] = set()
        regular: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        expanded = 0
        for info in infos:
            original_name = getattr(info, "orig_filename", info.filename)
            if "\\" in original_name or original_name != info.filename:
                raise AgentUpdateError("agent update archive contains an unsafe path")
            parts = _safe_archive_name(original_name.rstrip("/"))
            key = "/".join(parts).casefold()
            if key in seen:
                raise AgentUpdateError("agent update archive contains duplicate paths")
            seen.add(key)
            if info.flag_bits & 0x1:
                raise AgentUpdateError("encrypted agent update entries are not allowed")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise AgentUpdateError(
                    "agent update archive uses an unsupported compression method"
                )
            if info.is_dir() or info.filename.endswith("/"):
                continue
            if not _zip_entry_is_regular(info):
                raise AgentUpdateError("agent update archive contains a special file")
            expanded += info.file_size
            if expanded > expanded_limit:
                raise AgentUpdateError("agent update expanded size exceeds the local limit")
            regular.append((info, parts))

        manifest_matches = [
            (info, parts)
            for info, parts in regular
            if parts[-1] == UPDATE_MANIFEST_NAME
        ]
        if len(manifest_matches) != 1:
            raise AgentUpdateError("agent update archive must contain one manifest")
        manifest_info, manifest_parts = manifest_matches[0]
        if len(manifest_parts) not in {1, 2}:
            raise AgentUpdateError("agent update archive has an unsupported root layout")
        prefix = manifest_parts[:-1]
        relative: dict[str, zipfile.ZipInfo] = {}
        for info, parts in regular:
            if parts[: len(prefix)] != prefix or len(parts) <= len(prefix):
                raise AgentUpdateError("agent update files must share one bundle root")
            name = "/".join(parts[len(prefix) :])
            relative[name] = info
        manifest = _load_manifest(archive, manifest_info, expected_version)
        actual_files = set(relative) - {UPDATE_MANIFEST_NAME}
        declared_files = set(manifest["files"])
        if actual_files != declared_files or not WINDOWS_REQUIRED_PAYLOAD <= actual_files:
            raise AgentUpdateError("agent update manifest file list does not match the archive")
        destination.mkdir(parents=True, exist_ok=False)
        for name, info in relative.items():
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            written = 0
            try:
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise AgentUpdateError("agent update archive could not be extracted") from exc
            if written != info.file_size:
                raise AgentUpdateError("agent update archive member size changed")
            if name != UPDATE_MANIFEST_NAME and digest.hexdigest() != manifest["files"][name]:
                raise AgentUpdateError("agent update manifest SHA-256 does not match")
        return destination


def _safe_linux_archive_name(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or "\x00" in name:
        raise AgentUpdateError("Linux Agent archive contains an unsafe path")
    path = PurePosixPath(name)
    parts = path.parts
    if (
        path.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            len(part.encode("utf-8")) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        )
    ):
        raise AgentUpdateError("Linux Agent archive contains an unsafe path")
    return tuple(parts)


def _load_linux_manifest(
    archive: tarfile.TarFile,
    info: tarfile.TarInfo,
    expected_version: str,
) -> dict[str, Any]:
    if info.size > _MAX_MANIFEST_BYTES:
        raise AgentUpdateError("Linux Agent update manifest is too large")
    try:
        source = archive.extractfile(info)
        if source is None:
            raise OSError("manifest has no data stream")
        with source:
            raw = source.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) != info.size or len(raw) > _MAX_MANIFEST_BYTES:
            raise OSError("manifest size changed")
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, tarfile.TarError) as exc:
        raise AgentUpdateError("Linux Agent update manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "version",
        "platform",
        "architecture",
        "files",
    }:
        raise AgentUpdateError("Linux Agent update manifest has invalid fields")
    if (
        isinstance(manifest.get("schema_version"), bool)
        or manifest.get("schema_version") != 1
        or manifest.get("version") != expected_version
        or manifest.get("platform") != "linux"
        or manifest.get("architecture") != "x86_64"
        or not isinstance(manifest.get("files"), dict)
    ):
        raise AgentUpdateError("Linux Agent update manifest does not match this release")
    for name, digest in manifest["files"].items():
        if (
            not isinstance(name, str)
            or not name
            or "/".join(_safe_linux_archive_name(name)) != name
            or name.split("/", 1)[0] in {"data", "versions"}
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            raise AgentUpdateError("Linux Agent manifest contains an invalid file entry")
    return manifest


def _validate_and_extract_linux_release(
    package_path: Path,
    destination: Path,
    *,
    expected_version: str,
    expanded_limit: int,
) -> Path:
    """Verify and manually extract a bounded Linux ``tar.gz`` release.

    ``TarFile.extract`` is intentionally never used.  Every path, entry type,
    byte count and digest is checked before a file can enter the staging tree.
    """

    try:
        archive = tarfile.open(package_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise AgentUpdateError(
            "agent update package is not a valid gzip-compressed TAR archive"
        ) from exc
    with archive:
        try:
            members: list[tarfile.TarInfo] = []
            for info in archive:
                if len(members) >= _MAX_ARCHIVE_ENTRIES:
                    raise AgentUpdateError("Linux Agent archive entry count is invalid")
                members.append(info)
        except (OSError, tarfile.TarError) as exc:
            raise AgentUpdateError("Linux Agent archive cannot be read") from exc
        if not members:
            raise AgentUpdateError("Linux Agent archive entry count is invalid")
        seen: set[str] = set()
        regular: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
        expanded = 0
        for info in members:
            raw_name = info.name.rstrip("/")
            parts = _safe_linux_archive_name(raw_name)
            key = "/".join(parts)
            if key in seen:
                raise AgentUpdateError("Linux Agent archive contains duplicate paths")
            seen.add(key)
            if info.isdir():
                continue
            if (
                not info.isreg()
                or info.issym()
                or info.islnk()
                or bool(getattr(info, "sparse", None))
                or info.size < 0
            ):
                raise AgentUpdateError("Linux Agent archive contains a link or special file")
            expanded += info.size
            if expanded > expanded_limit:
                raise AgentUpdateError("agent update expanded size exceeds the local limit")
            regular.append((info, parts))

        manifests = [
            (info, parts)
            for info, parts in regular
            if parts[-1] == UPDATE_MANIFEST_NAME
        ]
        if len(manifests) != 1:
            raise AgentUpdateError("Linux Agent archive must contain one manifest")
        manifest_info, manifest_parts = manifests[0]
        if len(manifest_parts) not in {1, 2}:
            raise AgentUpdateError("Linux Agent archive has an unsupported root layout")
        prefix = manifest_parts[:-1]
        relative: dict[str, tarfile.TarInfo] = {}
        for info, parts in regular:
            if parts[: len(prefix)] != prefix or len(parts) <= len(prefix):
                raise AgentUpdateError("Linux Agent files must share one bundle root")
            relative["/".join(parts[len(prefix) :])] = info
        manifest = _load_linux_manifest(archive, manifest_info, expected_version)
        actual_files = set(relative) - {UPDATE_MANIFEST_NAME}
        declared_files = set(manifest["files"])
        # Protocol v1 is deliberately a program-only payload.  The stable
        # root-owned launcher/updater, installer and systemd policy can only
        # change through an explicit manual bootstrap, never from the mutable
        # staging directory.
        if actual_files != {"RigPilotAgent"} or declared_files != actual_files:
            raise AgentUpdateError("Linux Agent manifest file list does not match the archive")
        try:
            destination.mkdir(parents=True, exist_ok=False, mode=0o700)
            for name, info in relative.items():
                target = destination.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(info)
                if source is None:
                    raise OSError("regular member has no data stream")
                digest = hashlib.sha256()
                written = 0
                with source, target.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.size:
                            raise OSError("archive member exceeded its declared size")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if written != info.size:
                    raise OSError("archive member size changed")
                if name != UPDATE_MANIFEST_NAME and digest.hexdigest() != manifest["files"][name]:
                    raise AgentUpdateError("Linux Agent manifest SHA-256 does not match")
            (destination / "RigPilotAgent").chmod(0o700)
        except AgentUpdateError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise AgentUpdateError("Linux Agent archive could not be extracted") from exc
        return destination


def _json_document_bytes(payload: Mapping[str, Any]) -> bytes:
    document = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    return document.encode("utf-8")


def _atomic_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    durable_directory: bool = False,
    durable_file: bool = True,
) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            if durable_file:
                os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if durable_directory:
            _fsync_directory(path.parent)
            if not parent_existed:
                _fsync_directory(path.parent.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _miner_resume_marker_path(
    root: Path,
    activation_id: str,
    *,
    data_root: Path | None = None,
) -> Path:
    if _ACTIVATION_ID.fullmatch(activation_id) is None:
        raise AgentUpdateError("agent update activation id is invalid")
    return (
        (data_root if data_root is not None else root / "data")
        / "agent-updates"
        / "miner-resume"
        / f"{activation_id}.json"
    )


def _load_miner_resume_marker(
    path: Path,
    *,
    state: AgentState,
    command_id: str,
    activation_id: str,
) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentUpdateError("managed miner resume marker cannot be inspected") from exc
    if (
        _is_link_or_reparse(path)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > 32_768
    ):
        raise AgentUpdateError("managed miner resume marker is unsafe")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpdateError("managed miner resume marker is invalid") from exc
    common_fields = {
        "schema_version",
        "activation_id",
        "command_id",
        "agent_id",
        "miner_type",
        "flight_sheet_id",
        "resume_required",
    }
    if (
        not isinstance(marker, dict)
        or marker.get("activation_id") != activation_id
        or marker.get("command_id") != command_id
        or marker.get("agent_id") != state.agent_id
        or not _is_managed_miner_type(marker.get("miner_type"))
        or not isinstance(marker.get("flight_sheet_id"), str)
        or not marker["flight_sheet_id"].strip()
        or len(marker["flight_sheet_id"]) > 64
        or marker.get("resume_required") is not True
    ):
        raise AgentUpdateError(
            "managed miner resume marker does not match this Agent update"
        )
    schema_version = marker.get("schema_version")
    if schema_version == 1:
        if set(marker) != common_fields:
            raise AgentUpdateError(
                "managed miner resume marker does not match this Agent update"
            )
        return marker
    runtime_fingerprint = marker.get("runtime_fingerprint")
    if (
        schema_version != 2
        or set(marker) != common_fields | {"protocol", "runtime_fingerprint"}
        or marker.get("protocol") != "v1"
        or marker.get("miner_type") == "declarative"
        or marker["flight_sheet_id"] != marker["flight_sheet_id"].strip()
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in marker["flight_sheet_id"]
        )
        or (
            runtime_fingerprint is not None
            and (
                not isinstance(runtime_fingerprint, str)
                or _DIGEST.fullmatch(runtime_fingerprint) is None
            )
        )
    ):
        raise AgentUpdateError(
            "managed miner resume marker does not match this Agent update"
        )
    return marker


def _remove_miner_resume_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AgentUpdateError("managed miner resume marker cannot be removed") from exc


def _managed_miner_update_residue_plan(
    manager: Any,
) -> tuple[dict[str, str], str, list[dict[str, str]], bool]:
    """Build a fail-closed cleanup plan for an authenticated Agent update.

    ``running()`` intentionally refuses a stale PID or a changed argv.  An
    Agent update is nevertheless allowed to stop one *uniquely proved*
    trusted-path candidate, using the same scan/fingerprint/reverification
    contract as the operator residue-repair flow.  It never adopts that
    process: after activation the saved flight sheet is started afresh.
    """

    try:
        identity = manager.update_resume_identity()
        scan = manager.scan_residue()
    except (AttributeError, ManagedMinerError) as exc:
        raise AgentUpdateError(
            "managed miner residue cannot be inspected before update"
        ) from exc
    if (
        not isinstance(identity, Mapping)
        or not _is_managed_miner_type(identity.get("miner_type"))
        or not isinstance(identity.get("flight_sheet_id"), str)
        or not str(identity["flight_sheet_id"]).strip()
        or len(str(identity["flight_sheet_id"])) > 64
    ):
        raise AgentUpdateError("managed miner identity is invalid before update")
    scan_fingerprint = scan.get("scan_fingerprint") if isinstance(scan, Mapping) else None
    findings = scan.get("findings") if isinstance(scan, Mapping) else None
    if (
        not isinstance(scan_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", scan_fingerprint) is None
        or not isinstance(findings, list)
        or len(findings) != 3
    ):
        raise AgentUpdateError("managed miner residue scan is invalid before update")

    targets: list[dict[str, str]] = []
    live_process_seen = False
    seen_roles: set[str] = set()
    for finding in findings:
        if not isinstance(finding, Mapping):
            raise AgentUpdateError("managed miner residue scan is invalid before update")
        role = finding.get("role")
        status = finding.get("status")
        if role not in {"legacy", "primary", "secondary"} or role in seen_roles:
            raise AgentUpdateError("managed miner residue scan is invalid before update")
        seen_roles.add(str(role))
        if status == "no_record":
            continue
        if (
            finding.get("flight_sheet_id") != identity["flight_sheet_id"]
            or not _is_managed_miner_type(finding.get("miner_type"))
        ):
            raise AgentUpdateError(
                "managed miner residue disagrees with the active flight sheet"
            )
        target_fingerprint = finding.get("target_fingerprint")
        if (
            not isinstance(target_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", target_fingerprint) is None
        ):
            raise AgentUpdateError("managed miner residue scan is invalid before update")
        target = {
            "role": str(role),
            "target_fingerprint": target_fingerprint,
        }
        if finding.get("clearable") is True:
            if status in {"verified_process", "exact_replacement"}:
                if finding.get("process_verified") is not True:
                    raise AgentUpdateError(
                        "managed miner residue scan is invalid before update"
                    )
                live_process_seen = True
            elif status in {"preboot_record", "orphan_record", "invalid_record"}:
                if finding.get("process_verified") is True:
                    raise AgentUpdateError(
                        "managed miner residue scan is invalid before update"
                    )
            else:
                raise AgentUpdateError(
                    "managed miner residue is not safe to clean before update"
                )
        elif status == "ambiguous":
            candidates = finding.get("manual_candidates")
            if not isinstance(candidates, list) or len(candidates) != 1:
                raise AgentUpdateError(
                    "managed miner residue is not uniquely identifiable before update"
                )
            candidate = candidates[0]
            candidate_fingerprint = (
                candidate.get("candidate_fingerprint")
                if isinstance(candidate, Mapping)
                else None
            )
            if (
                not isinstance(candidate_fingerprint, str)
                or re.fullmatch(r"[0-9a-f]{64}", candidate_fingerprint) is None
            ):
                raise AgentUpdateError(
                    "managed miner residue is not uniquely identifiable before update"
                )
            target["candidate_fingerprint"] = candidate_fingerprint
            live_process_seen = True
        else:
            raise AgentUpdateError(
                "managed miner residue is not safe to clean before update"
            )
        targets.append(target)
    if seen_roles != {"legacy", "primary", "secondary"}:
        raise AgentUpdateError("managed miner residue scan is invalid before update")
    return (
        {
            "miner_type": str(identity["miner_type"]),
            "flight_sheet_id": str(identity["flight_sheet_id"]),
        },
        scan_fingerprint,
        targets,
        live_process_seen,
    )


def _cleanup_managed_miner_residue_for_update(
    manager: Any,
    *,
    expected_identity: Mapping[str, str] | None = None,
    plan: tuple[dict[str, str], str, list[dict[str, str]], bool] | None = None,
) -> tuple[dict[str, str], bool]:
    identity, scan_fingerprint, targets, live_process_seen = (
        plan if plan is not None else _managed_miner_update_residue_plan(manager)
    )
    if expected_identity is not None and dict(expected_identity) != identity:
        raise AgentUpdateError(
            "managed miner identity changed while preparing the update"
        )
    try:
        result = manager.cleanup_residue(scan_fingerprint, targets)
    except (AttributeError, ManagedMinerError) as exc:
        raise AgentUpdateError(
            "managed miner residue could not be stopped for update"
        ) from exc
    results = result.get("results") if isinstance(result, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("blocked") is not False
        or result.get("selected_targets") != targets
        or not isinstance(results, list)
        or len(results) != len(targets)
        or any(
            not isinstance(item, Mapping)
            or item.get("record_removed") is not True
            or item.get("status")
            not in {
                "verified_process_stopped",
                "explicit_candidate_stopped",
                "orphan_record_removed",
                "invalid_record_removed",
            }
            for item in results
        )
    ):
        raise AgentUpdateError(
            "managed miner residue cleanup was incomplete before update"
        )
    try:
        confirmation = manager.scan_residue()
    except (AttributeError, ManagedMinerError) as exc:
        raise AgentUpdateError(
            "managed miner residue cannot be confirmed after update cleanup"
        ) from exc
    confirmation_findings = (
        confirmation.get("findings")
        if isinstance(confirmation, Mapping)
        else None
    )
    if (
        not isinstance(confirmation_findings, list)
        or len(confirmation_findings) != 3
        or any(
            not isinstance(item, Mapping) or item.get("status") != "no_record"
            for item in confirmation_findings
        )
    ):
        raise AgentUpdateError(
            "managed miner residue remained after update cleanup"
        )
    return identity, live_process_seen


def _stop_managed_miner_for_update(
    config: AgentConfig,
    root: Path,
    state: AgentState,
    command_id: str,
    activation_id: str,
    manager: Any,
    *,
    data_root: Path | None = None,
) -> bool:
    """Stop a live managed miner and durably remember that it must resume.

    The intent is written before process termination, closing the crash window
    where a stopped miner could otherwise be forgotten.  A machine with no
    verified live managed miner receives no marker and is never started later.
    """

    if not config.managed_mining_enabled or config.managed_miner_runtime_dir is None:
        return False
    marker_path = _miner_resume_marker_path(
        root, activation_id, data_root=data_root
    )
    try:
        current = manager.running()
    except ManagedMinerError as inspect_error:
        try:
            residue_plan = _managed_miner_update_residue_plan(manager)
        except AgentUpdateError as recovery_error:
            raise AgentUpdateError(
                "managed miner cannot be inspected before update: "
                f"{recovery_error}"
            ) from inspect_error
        identity, _scan_fingerprint, _targets, live_process_seen = residue_plan
        miner_type = identity["miner_type"]
        flight_sheet_id = identity["flight_sheet_id"]
        if live_process_seen:
            _atomic_json(
                marker_path,
                {
                    "schema_version": 1,
                    "activation_id": activation_id,
                    "command_id": command_id,
                    "agent_id": state.agent_id,
                    "miner_type": miner_type,
                    "flight_sheet_id": flight_sheet_id,
                    "resume_required": True,
                },
                durable_directory=data_root is not None,
            )
        _identity, cleaned_live_process = _cleanup_managed_miner_residue_for_update(
            manager,
            expected_identity=identity,
            plan=residue_plan,
        )
        if cleaned_live_process != live_process_seen:
            raise AgentUpdateError(
                "managed miner liveness changed while preparing the update"
            )
        return live_process_seen
    if current is None:
        return False
    miner_type = current.get("miner_type") if isinstance(current, Mapping) else None
    flight_sheet_id = (
        current.get("flight_sheet_id") if isinstance(current, Mapping) else None
    )
    if (
        not _is_managed_miner_type(miner_type)
        or not isinstance(flight_sheet_id, str)
        or not flight_sheet_id.strip()
        or len(flight_sheet_id) > 64
    ):
        raise AgentUpdateError("managed miner identity is invalid before update")
    _atomic_json(
        marker_path,
        {
            "schema_version": 1,
            "activation_id": activation_id,
            "command_id": command_id,
            "agent_id": state.agent_id,
            "miner_type": miner_type,
            "flight_sheet_id": flight_sheet_id,
            "resume_required": True,
        },
        durable_directory=data_root is not None,
    )
    try:
        stopped = manager.stop()
    except ManagedMinerError as stop_error:
        try:
            _cleanup_managed_miner_residue_for_update(
                manager,
                expected_identity={
                    "miner_type": str(miner_type),
                    "flight_sheet_id": flight_sheet_id,
                },
            )
        except AgentUpdateError as recovery_error:
            raise AgentUpdateError(
                "managed miner could not be stopped for update: "
                f"{recovery_error}"
            ) from stop_error
        return True
    if not isinstance(stopped, Mapping) or stopped.get("stopped") is not True:
        # It exited between inspection and stop.  The Agent did not stop it,
        # so it must not later infer permission to start it.
        _remove_miner_resume_marker(marker_path)
        return False
    if (
        stopped.get("miner_type") != miner_type
        or stopped.get("flight_sheet_id") != flight_sheet_id
    ):
        raise AgentUpdateError("stopped managed miner identity changed during update")
    return True


def _resume_managed_miner_marker(
    config: AgentConfig,
    root: Path,
    state: AgentState,
    command_id: str,
    activation_id: str,
    manager: Any,
    *,
    consume_marker: bool = True,
    data_root: Path | None = None,
) -> bool:
    marker_path = _miner_resume_marker_path(
        root, activation_id, data_root=data_root
    )
    marker = _load_miner_resume_marker(
        marker_path,
        state=state,
        command_id=command_id,
        activation_id=activation_id,
    )
    if marker is None:
        return False
    if marker.get("schema_version") == 2:
        try:
            configured_identity = manager.update_resume_identity()
        except (AttributeError, ManagedMinerError) as exc:
            raise AgentUpdateError(
                "managed miner update identity cannot be verified before resume"
            ) from exc
        if (
            not isinstance(configured_identity, Mapping)
            or set(configured_identity)
            != {"miner_type", "flight_sheet_id", "runtime_fingerprint"}
            or configured_identity.get("miner_type") != marker["miner_type"]
            or configured_identity.get("flight_sheet_id")
            != marker["flight_sheet_id"]
            or configured_identity.get("runtime_fingerprint")
            != marker["runtime_fingerprint"]
        ):
            raise AgentUpdateError(
                "managed miner update identity does not match the resume marker"
            )
    try:
        current = manager.running()
    except ManagedMinerError as exc:
        raise AgentUpdateError("managed miner cannot be inspected before resume") from exc
    if current is not None:
        if (
            not isinstance(current, Mapping)
            or current.get("miner_type") != marker["miner_type"]
            or current.get("flight_sheet_id") != marker["flight_sheet_id"]
        ):
            raise AgentUpdateError(
                "a different managed miner is running; update resume was not applied"
            )
        # A previous restart succeeded (or cleanup is deliberately deferred
        # until activation commits).  Treat the matching live process as proof
        # and never disrupt it a second time.
        if consume_marker:
            _remove_miner_resume_marker(marker_path)
        return True
    try:
        restarted = manager.restart()
    except ManagedMinerError as exc:
        # Deliberately retain the marker.  The next heartbeat (or the restored
        # old Agent after rollback) will retry from the same bounded sheet.
        raise AgentUpdateError("managed miner could not be resumed after update") from exc
    if (
        not isinstance(restarted, Mapping)
        or restarted.get("restarted") is not True
        or restarted.get("miner_type") != marker["miner_type"]
        or restarted.get("flight_sheet_id") != marker["flight_sheet_id"]
    ):
        raise AgentUpdateError("resumed managed miner identity does not match update marker")
    if consume_marker:
        _remove_miner_resume_marker(marker_path)
    return True


def _linux_pending_activation(
    config: AgentConfig,
    state: AgentState,
) -> tuple[Path, Path, Path, dict[str, str]] | None:
    try:
        root, data_root, executable = _linux_update_layout(config)
    except AgentUpdateError:
        return None
    pending_path = root / "pending-update.json"
    try:
        info = pending_path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > 32_768
        ):
            return None
        value = json.loads(pending_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return None
    expected_relative = f"versions/{__version__}/RigPilotAgent"
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "version",
            "relative_executable",
            "activation_id",
            "command_id",
            "agent_id",
        }
        or value.get("schema_version") != 1
        or value.get("version") != __version__
        or value.get("relative_executable") != expected_relative
        or not isinstance(value.get("activation_id"), str)
        or _ACTIVATION_ID.fullmatch(value["activation_id"]) is None
        or not isinstance(value.get("command_id"), str)
        or _COMMAND_ID.fullmatch(value["command_id"]) is None
        or value.get("agent_id") != state.agent_id
        or (root / expected_relative).resolve(strict=True) != executable
    ):
        return None
    metadata = {
        "activation_id": value["activation_id"],
        "command_id": value["command_id"],
        "version": __version__,
        "agent_id": state.agent_id,
    }
    reported = (
        data_root
        / "agent-updates"
        / "reported"
        / f"{metadata['activation_id']}.json"
    )
    try:
        reported_info = reported.lstat()
        if (
            stat.S_ISLNK(reported_info.st_mode)
            or not stat.S_ISREG(reported_info.st_mode)
            or reported_info.st_size > 32_768
        ):
            return None
        receipt = json.loads(reported.read_text(encoding="utf-8"))
        if receipt == {"schema_version": 1, **metadata, "reported": True}:
            return None
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError):
        return None
    return root, data_root, executable, metadata


def _linux_staged_response_metadata(
    state: AgentState,
    command_id: str,
    response: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return a persisted Linux activation identity from a staged response."""

    if _COMMAND_ID.fullmatch(command_id) is None:
        raise AgentUpdateError("agent update command id is invalid")
    result = response.get("result")
    if (
        response.get("status") != "succeeded"
        or response.get("error") is not None
        or not isinstance(result, Mapping)
        or result.get("activation_scheduled") is not True
    ):
        return None
    activation_id = result.get("activation_id")
    version = result.get("version")
    if (
        not isinstance(activation_id, str)
        or _ACTIVATION_ID.fullmatch(activation_id) is None
        or not isinstance(version, str)
        or _VERSION.fullmatch(version) is None
        or _version_tuple(version) <= _version_tuple(__version__)
    ):
        raise AgentUpdateError("staged Linux Agent response identity is invalid")
    history = state.command_history.get(command_id)
    if not isinstance(history, dict) or history.get("response") != response:
        raise AgentUpdateError(
            "Linux Agent activation cannot be armed before its result is persisted"
        )
    return {
        "version": version,
        "activation_id": activation_id,
        "command_id": command_id,
        "agent_id": state.agent_id,
    }


def _write_linux_activation_abort(
    job: Path,
    metadata: Mapping[str, str],
    error: str,
) -> None:
    clean_error = " ".join(str(error).split())[:2048]
    if not clean_error:
        clean_error = "Linux Agent activation was cancelled"
    _atomic_json(
        job / "activation-abort.json",
        {
            "schema_version": 1,
            "version": metadata["version"],
            "activation_id": metadata["activation_id"],
            "command_id": metadata["command_id"],
            "agent_id": metadata["agent_id"],
            "aborted": True,
            "error": clean_error,
        },
        durable_directory=True,
    )


def abort_staged_agent_update(
    config: AgentConfig,
    state: AgentState,
    command_id: str,
    response: Mapping[str, Any],
    error: str,
) -> bool:
    """Tell a ready Linux helper that controller activation was rejected."""

    if platform.system() != "Linux":
        return False
    metadata = _linux_staged_response_metadata(state, command_id, response)
    if metadata is None:
        return False
    _root, data_root, _executable = _linux_update_layout(config)
    job = data_root / "agent-updates" / "staged" / metadata["activation_id"]
    enforce_owner = data_root == LINUX_DATA_ROOT
    if not _linux_plain_root_owned(
        job, directory=True, enforce_owner=enforce_owner
    ) or (enforce_owner and stat.S_IMODE(job.lstat().st_mode) != 0o700):
        raise AgentUpdateError("Linux Agent activation job is unsafe")
    _write_linux_activation_abort(job, metadata, error)
    return True


def arm_staged_agent_update(
    config: AgentConfig,
    state: AgentState,
    command_id: str,
    response: Mapping[str, Any],
) -> bool:
    """Arm one ready Linux updater after its staged result is durable.

    The fixed updater treats this exact, job-local document as the only
    authorization to switch the active version.  Merely observing the old PID
    exit is never sufficient.
    """

    if platform.system() != "Linux":
        return False
    metadata = _linux_staged_response_metadata(state, command_id, response)
    if metadata is None:
        return False
    _root, data_root, _executable = _linux_update_layout(config)
    job = (
        data_root
        / "agent-updates"
        / "staged"
        / metadata["activation_id"]
    )
    # `_linux_update_layout` can return a non-fixed root only when explicitly
    # overridden by unit tests.  Production always enforces the root-owned
    # 0700 job boundary below the fixed data root.
    enforce_owner = data_root == LINUX_DATA_ROOT
    if not _linux_plain_root_owned(
        job, directory=True, enforce_owner=enforce_owner
    ) or (enforce_owner and stat.S_IMODE(job.lstat().st_mode) != 0o700):
        raise AgentUpdateError("Linux Agent activation job is unsafe")
    ready_path = job / "updater-ready.json"
    try:
        ready_info = ready_path.lstat()
        if (
            stat.S_ISLNK(ready_info.st_mode)
            or not stat.S_ISREG(ready_info.st_mode)
            or ready_info.st_size > 32_768
        ):
            raise AgentUpdateError("Linux updater readiness marker is unsafe")
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentUpdateError("Linux updater is not ready to be armed") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpdateError("Linux updater readiness marker is invalid") from exc
    updater_pid = ready.get("updater_pid") if isinstance(ready, dict) else None
    if (
        not isinstance(ready, dict)
        or set(ready)
        != {
            "schema_version",
            "activation_id",
            "command_id",
            "agent_id",
            "updater_pid",
        }
        or ready.get("schema_version") != 1
        or ready.get("activation_id") != metadata["activation_id"]
        or ready.get("command_id") != command_id
        or ready.get("agent_id") != state.agent_id
        or isinstance(updater_pid, bool)
        or not isinstance(updater_pid, int)
        or updater_pid <= 1
    ):
        raise AgentUpdateError("Linux updater readiness marker does not match activation")

    arm_path = job / "activation-arm.json"
    arm = {
        "schema_version": 1,
        "version": metadata["version"],
        "activation_id": metadata["activation_id"],
        "command_id": command_id,
        "agent_id": state.agent_id,
        "armed": True,
    }
    try:
        info = arm_path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > 32_768
            or json.loads(arm_path.read_text(encoding="utf-8")) != arm
        ):
            raise AgentUpdateError("Linux activation arm marker is unsafe or mismatched")
        # A previous atomic replace may be visible after directory fsync
        # failed. Reconfirm durability before authorizing the Agent to exit.
        _fsync_directory(job)
        return True
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpdateError("Linux activation arm marker is invalid") from exc
    _atomic_json(arm_path, arm, durable_directory=True)
    return True


def _linux_resume_managed_miner_after_update(
    config: AgentConfig,
    state: AgentState,
    command_id: str,
    activation_id: str,
    *,
    managed_miner_factory: Callable[[AgentConfig], Any],
    consume_marker: bool,
) -> bool:
    root, data_root, _executable = _linux_update_layout(config)
    return _resume_managed_miner_marker(
        config,
        root,
        state,
        command_id,
        activation_id,
        managed_miner_factory(config),
        consume_marker=consume_marker,
        data_root=data_root,
    )


def resume_managed_miner_after_update(
    config: AgentConfig,
    state: AgentState,
    command_id: str,
    activation_id: str,
    *,
    portable_root: Path | None = None,
    managed_miner_factory: Callable[[AgentConfig], Any] = ManagedMinerManager,
    consume_marker: bool = True,
) -> bool:
    """Resume the miner bound to one activation, if that update stopped it."""

    if _COMMAND_ID.fullmatch(command_id) is None:
        raise AgentUpdateError("agent update command id is invalid")
    if portable_root is None and platform.system() == "Linux":
        return _linux_resume_managed_miner_after_update(
            config,
            state,
            command_id,
            activation_id,
            managed_miner_factory=managed_miner_factory,
            consume_marker=consume_marker,
        )
    if portable_root is None:
        root, _executable = _portable_root(config)
    else:
        root = portable_root.resolve()
    manager = managed_miner_factory(config)
    return _resume_managed_miner_marker(
        config,
        root,
        state,
        command_id,
        activation_id,
        manager,
        consume_marker=consume_marker,
    )


def resume_pending_agent_update_miner(
    config: AgentConfig,
    state: AgentState,
    *,
    managed_miner_factory: Callable[[AgentConfig], Any] = ManagedMinerManager,
) -> bool:
    """Resume only after the replacement completed an authenticated heartbeat."""

    if platform.system() == "Linux":
        pending_linux = _linux_pending_activation(config, state)
        if pending_linux is None:
            return False
        _root, _data_root, _executable, metadata = pending_linux
        return _linux_resume_managed_miner_after_update(
            config,
            state,
            metadata["command_id"],
            metadata["activation_id"],
            managed_miner_factory=managed_miner_factory,
            consume_marker=False,
        )
    pending = _pending_activation(config, state)
    if pending is None:
        return False
    root, _executable, metadata = pending
    return resume_managed_miner_after_update(
        config,
        state,
        metadata["command_id"],
        metadata["activation_id"],
        portable_root=root,
        managed_miner_factory=managed_miner_factory,
        consume_marker=False,
    )


class AgentUpdateManager:
    """Stage one release and launch the fixed out-of-process activator."""

    def __init__(
        self,
        config: AgentConfig,
        client: Any,
        *,
        system_name: str | None = None,
        executable: Path | None = None,
        frozen: bool | None = None,
        portable_root: Path | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        preflight_runner: Callable[..., Any] = subprocess.run,
        process_factory: Callable[[int], Any] = psutil.Process,
        managed_miner_factory: Callable[[AgentConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.system_name = system_name
        self.executable = executable
        self.frozen = frozen
        self.portable_root = portable_root
        self.popen_factory = popen_factory
        self.preflight_runner = preflight_runner
        self.process_factory = process_factory
        self.managed_miner_factory = managed_miner_factory or (
            lambda agent_config: ManagedMinerManager(
                agent_config,
                process_factory=self.process_factory,
                system_name=self.system_name,
            )
        )

    def _stage_linux(
        self,
        metadata: Mapping[str, Any],
        state: AgentState,
        command_id: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        root, data_root, _running_executable = _linux_update_layout(
            self.config,
            system_name=self.system_name,
            executable=self.executable,
            frozen=self.frozen,
            root_override=self.portable_root,
        )
        activation_id = uuid.uuid4().hex
        jobs_root = data_root / "agent-updates" / "staged"
        job = jobs_root / activation_id
        package_path = job / "release.tar.gz"
        bundle_path = job / "bundle"
        log_path = job / "updater.log"
        ready_path = job / "updater-ready.json"
        unit_name = f"rigpilot-agent-update-{activation_id}"
        updater_process: Any | None = None
        updater_ready = False
        miner_manager: Any | None = None
        try:
            # A nearly-full root filesystem must fail the command here, while
            # the running Agent is still healthy, rather than mid-activation
            # where the fixed updater cannot free its staged payload.
            assert_update_disk_headroom((data_root, root), int(metadata["size"]))
            jobs_root = _linux_ensure_protected_data_directories(
                data_root,
                ("agent-updates", "staged"),
                enforce_owner=self.portable_root is None,
            )
            job.mkdir(parents=False, exist_ok=False, mode=0o700)
            if not _linux_plain_root_owned(
                job,
                directory=True,
                enforce_owner=self.portable_root is None,
            ):
                raise AgentUpdateError("Linux Agent activation directory is unsafe")
            if self.portable_root is None:
                if stat.S_IMODE(job.lstat().st_mode) != 0o700:
                    raise AgentUpdateError(
                        "Linux Agent activation directory permissions are unsafe"
                    )
                _fsync_directory(job)
                _fsync_directory(jobs_root)
            self.client.download_agent_update(
                state,
                metadata["download_url"],
                package_path,
                expected_size=metadata["size"],
                expected_sha256=metadata["sha256"],
                max_bytes=max_bytes,
            )
            try:
                package_info = package_path.lstat()
            except OSError as exc:
                raise AgentUpdateError("downloaded Linux Agent package is unavailable") from exc
            if (
                stat.S_ISLNK(package_info.st_mode)
                or not stat.S_ISREG(package_info.st_mode)
                or package_info.st_size != metadata["size"]
            ):
                raise AgentUpdateError("downloaded Linux Agent package is unsafe")
            _validate_and_extract_linux_release(
                package_path,
                bundle_path,
                expected_version=str(metadata["version"]),
                expanded_limit=max_bytes * 4,
            )
            completed = self.preflight_runner(
                [str(bundle_path / "RigPilotAgent"), "--version"],
                cwd=str(bundle_path),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=external_process_environment("Linux"),
            )
            if (
                completed.returncode != 0
                or (completed.stdout or "").strip()
                != f"RigPilot Agent {metadata['version']}"
            ):
                raise AgentUpdateError("staged Agent failed its version preflight")

            updater = root / LINUX_UPDATE_SCRIPT_NAME
            argv = [
                str(LINUX_SYSTEMD_RUN),
                "--unit",
                unit_name,
                "--collect",
                "--quiet",
                "--wait",
                "--property=Type=exec",
                "--property=User=root",
                "--property=Group=root",
                "--property=UMask=0077",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectSystem=strict",
                f"--property=ReadWritePaths={root} {data_root} /run/lock",
                f"--property=StandardOutput=append:{log_path}",
                f"--property=StandardError=append:{log_path}",
                f"--working-directory={data_root}",
                str(LINUX_PYTHON),
                "-I",
                str(updater),
                "activate",
                "--program-root",
                str(root),
                "--data-root",
                str(data_root),
                "--staged-bundle",
                str(bundle_path),
                "--version",
                str(metadata["version"]),
                "--activation-id",
                activation_id,
                "--agent-id",
                state.agent_id,
                "--command-id",
                command_id,
                "--agent-pid",
                str(os.getpid()),
                "--ready-path",
                str(ready_path),
            ]
            with log_path.open("ab", buffering=0) as log:
                updater_process = self.popen_factory(
                    argv,
                    cwd=str(data_root),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                    env=external_process_environment("Linux"),
                )
            # The helper verifies and fsyncs the staged executable before it
            # advertises readiness.  Give slower mining disks enough time and
            # keep systemd-run attached with --wait so an early unit failure is
            # observable instead of looking like a silent readiness timeout.
            deadline = time.monotonic() + 60.0
            while True:
                poll = getattr(updater_process, "poll", None)
                exit_code = poll() if callable(poll) else None
                if exit_code not in {None, 0}:
                    detail = _linux_update_log_detail(log_path)
                    message = (
                        "independent Linux Agent updater exited before readiness "
                        f"(status {exit_code})"
                    )
                    if detail:
                        message = f"{message}: {detail}"
                    raise AgentUpdateError(message)
                try:
                    ready_info = ready_path.lstat()
                    if (
                        stat.S_ISLNK(ready_info.st_mode)
                        or not stat.S_ISREG(ready_info.st_mode)
                        or ready_info.st_size > 32_768
                    ):
                        raise AgentUpdateError("Linux updater readiness marker is unsafe")
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                    updater_pid = ready.get("updater_pid") if isinstance(ready, dict) else None
                    if (
                        not isinstance(ready, dict)
                        or set(ready)
                        != {
                            "schema_version",
                            "activation_id",
                            "command_id",
                            "agent_id",
                            "updater_pid",
                        }
                        or ready.get("schema_version") != 1
                        or ready.get("activation_id") != activation_id
                        or ready.get("command_id") != command_id
                        or ready.get("agent_id") != state.agent_id
                        or isinstance(updater_pid, bool)
                        or not isinstance(updater_pid, int)
                        or updater_pid <= 1
                    ):
                        raise AgentUpdateError(
                            "Linux updater readiness marker does not match this activation"
                        )
                    break
                except FileNotFoundError:
                    pass
                except (OSError, UnicodeError, ValueError) as exc:
                    raise AgentUpdateError("Linux updater readiness marker is invalid") from exc
                if time.monotonic() >= deadline:
                    detail = _linux_update_log_detail(log_path)
                    message = "independent Linux Agent updater did not become ready"
                    if detail:
                        message = f"{message}: {detail}"
                    raise AgentUpdateError(message)
                time.sleep(0.1)
            updater_ready = True
            # The trusted helper must own durable recovery state before a live
            # miner is touched.  It still cannot activate: Runtime writes the
            # separate arm marker only after the staged outcome is persisted
            # and accepted (or its response is ambiguous) at the controller.
            if (
                self.config.managed_mining_enabled
                and self.config.managed_miner_runtime_dir is not None
            ):
                miner_manager = self.managed_miner_factory(self.config)
                _stop_managed_miner_for_update(
                    self.config,
                    root,
                    state,
                    command_id,
                    activation_id,
                    miner_manager,
                    data_root=data_root,
                )
        except Exception as stage_error:
            if updater_process is not None and not updater_ready:
                try:
                    poll = getattr(updater_process, "poll", None)
                    if not callable(poll) or poll() is None:
                        updater_process.terminate()
                except (OSError, AttributeError):
                    pass
                if self.portable_root is None:
                    try:
                        subprocess.run(
                            [str(LINUX_SYSTEMCTL), "stop", f"{unit_name}.service"],
                            shell=False,
                            check=False,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=15,
                            env=external_process_environment("Linux"),
                        )
                    except (OSError, subprocess.SubprocessError):
                        pass
            if updater_ready:
                try:
                    _write_linux_activation_abort(
                        job,
                        {
                            "version": str(metadata["version"]),
                            "activation_id": activation_id,
                            "command_id": command_id,
                            "agent_id": state.agent_id,
                        },
                        f"Agent staging failed after updater readiness: {stage_error}",
                    )
                except (AgentUpdateError, OSError):
                    # The helper still fails closed without an arm marker and
                    # its persisted recovery deadline remains authoritative.
                    pass
            resume_error: AgentUpdateError | None = None
            try:
                marker = _load_miner_resume_marker(
                    _miner_resume_marker_path(
                        root, activation_id, data_root=data_root
                    ),
                    state=state,
                    command_id=command_id,
                    activation_id=activation_id,
                )
            except AgentUpdateError as exc:
                resume_error = exc
                marker = None
            if marker is not None:
                try:
                    _atomic_json(
                        job / "activation-result.json",
                        {
                            "schema_version": 1,
                            "status": "rolled_back",
                            "version": metadata["version"],
                            "activation_id": activation_id,
                            "command_id": command_id,
                            "agent_id": state.agent_id,
                            "error": (
                                "Agent update staging failed before activation; "
                                    "managed miner resume is pending."
                                ),
                            },
                            durable_directory=True,
                        )
                    if miner_manager is None:
                        miner_manager = self.managed_miner_factory(self.config)
                    _resume_managed_miner_marker(
                        self.config,
                        root,
                        state,
                        command_id,
                        activation_id,
                        miner_manager,
                        data_root=data_root,
                    )
                except AgentUpdateError as exc:
                    resume_error = exc
            if resume_error is not None:
                shutil.rmtree(bundle_path, ignore_errors=True)
                for disposable in (package_path, ready_path, log_path):
                    try:
                        disposable.unlink()
                    except OSError:
                        pass
                raise AgentUpdateError(
                    "Agent update staging failed and managed miner resume remains pending"
                ) from stage_error
            # Once ready was observed the fixed helper owns recovery state and
            # paths below this job.  Never remove them underneath that helper;
            # without an arm marker it must fail closed and roll back locally.
            if not updater_ready:
                shutil.rmtree(job, ignore_errors=True)
            raise
        return {
            "state": "staged",
            "release_id": metadata["release_id"],
            "version": metadata["version"],
            "sha256": metadata["sha256"],
            "activation_id": activation_id,
            "activation_scheduled": True,
            "restart_required": True,
            "miner_continuity_checked": True,
        }

    def stage(
        self,
        payload: Mapping[str, Any],
        state: AgentState,
        command_id: str,
    ) -> dict[str, Any]:
        if _COMMAND_ID.fullmatch(command_id) is None:
            raise AgentUpdateError("agent update command id must be a lowercase UUID")
        if not self.config.allow_agent_updates:
            raise AgentUpdateError("agent updates are disabled by local configuration")
        metadata = validate_agent_update_payload(payload)
        max_bytes = self.config.agent_update_max_mb * 1024 * 1024
        if metadata["size"] > max_bytes:
            raise AgentUpdateError("agent update exceeds the local size limit")
        if (self.system_name or platform.system()) == "Linux":
            return self._stage_linux(metadata, state, command_id, max_bytes)
        root, _running_executable = _portable_root(
            self.config,
            system_name=self.system_name,
            executable=self.executable,
            frozen=self.frozen,
            root_override=self.portable_root,
        )

        activation_id = uuid.uuid4().hex
        jobs_root = root / "data" / "agent-updates" / "staged"
        job = jobs_root / activation_id
        package_path = job / "release.zip"
        bundle_path = job / "bundle"
        updater_copy = job / UPDATE_SCRIPT_NAME
        log_path = job / "updater.log"
        ready_path = job / "updater-ready.json"
        updater_process: Any | None = None
        updater_ready = False
        miner_manager: Any | None = None
        try:
            # See the Linux staging path: refuse before any byte is written
            # when the portable volume cannot hold staging plus the new slot
            # plus the unpack headroom both versions need.
            assert_update_disk_headroom((root,), int(metadata["size"]))
            job.mkdir(parents=True, exist_ok=False)
            self.client.download_agent_update(
                state,
                metadata["download_url"],
                package_path,
                expected_size=metadata["size"],
                expected_sha256=metadata["sha256"],
                max_bytes=max_bytes,
            )
            _validate_and_extract_release(
                package_path,
                bundle_path,
                expected_version=metadata["version"],
                expanded_limit=max_bytes * 4,
            )
            creationflags = 0
            if (self.system_name or platform.system()) == "Windows":
                creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            completed = self.preflight_runner(
                [str(bundle_path / "RigPilotAgent.exe"), "--version"],
                cwd=str(bundle_path),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=creationflags,
            )
            if (
                completed.returncode != 0
                or (completed.stdout or "").strip()
                != f"RigPilot Agent {metadata['version']}"
            ):
                raise AgentUpdateError("staged Agent failed its version preflight")

            trusted_updater = root / UPDATE_SCRIPT_NAME
            shutil.copyfile(trusted_updater, updater_copy)
            system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
            if not system_root:
                raise AgentUpdateError("Windows SystemRoot is unavailable")
            powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if not powershell.is_file():
                raise AgentUpdateError("Windows PowerShell is unavailable")
            argv = [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(updater_copy),
                "-PortableRoot",
                str(root),
                "-StagedBundle",
                str(bundle_path),
                "-Version",
                metadata["version"],
                "-ActivationId",
                activation_id,
                "-AgentId",
                state.agent_id,
                "-CommandId",
                command_id,
                "-AgentPid",
                str(os.getpid()),
                "-LauncherPid",
                str(os.getppid()),
                "-ReadyPath",
                str(ready_path),
            ]
            # Windows PowerShell 5.1 can exit successfully without executing
            # its script when launched with DETACHED_PROCESS.  A new process
            # group plus job breakaway is sufficient for the updater to
            # outlive the Agent and launcher, while still executing reliably.
            detached_flags = (
                int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
                | int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
            )
            if (
                self.config.managed_mining_enabled
                and self.config.managed_miner_runtime_dir is not None
            ):
                miner_manager = self.managed_miner_factory(self.config)
                _stop_managed_miner_for_update(
                    self.config,
                    root,
                    state,
                    command_id,
                    activation_id,
                    miner_manager,
                )
            with log_path.open("ab", buffering=0) as log:
                updater_process = self.popen_factory(
                    argv,
                    cwd=str(job),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    creationflags=detached_flags,
                )
            deadline = time.monotonic() + 15.0
            while True:
                poll = getattr(updater_process, "poll", None)
                if callable(poll) and poll() is not None:
                    raise AgentUpdateError(
                        "independent Agent updater exited before taking ownership"
                    )
                try:
                    ready_info = ready_path.lstat()
                    if (
                        stat.S_ISLNK(ready_info.st_mode)
                        or bool(getattr(ready_info, "st_file_attributes", 0) & 0x400)
                        or ready_info.st_size > 32_768
                    ):
                        raise AgentUpdateError("Agent updater readiness marker is unsafe")
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                    if (
                        not isinstance(ready, dict)
                        or set(ready)
                        != {
                            "schema_version",
                            "activation_id",
                            "command_id",
                            "agent_id",
                            "updater_pid",
                        }
                        or ready.get("schema_version") != 1
                        or ready.get("activation_id") != activation_id
                        or ready.get("command_id") != command_id
                        or ready.get("agent_id") != state.agent_id
                        or ready.get("updater_pid") != int(updater_process.pid)
                    ):
                        raise AgentUpdateError(
                            "Agent updater readiness marker does not match this activation"
                        )
                    break
                except FileNotFoundError:
                    pass
                except (OSError, UnicodeError, ValueError) as exc:
                    raise AgentUpdateError(
                        "Agent updater readiness marker is invalid"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise AgentUpdateError(
                        "independent Agent updater did not become ready"
                    )
                time.sleep(0.1)
            updater_ready = True
        except Exception as stage_error:
            if updater_process is not None:
                try:
                    poll = getattr(updater_process, "poll", None)
                    if not callable(poll) or poll() is None:
                        updater_process.terminate()
                except (OSError, AttributeError):
                    pass
            resume_error: AgentUpdateError | None = None
            retry_receipt_error: AgentUpdateError | None = None
            if not updater_ready:
                try:
                    marker = _load_miner_resume_marker(
                        _miner_resume_marker_path(root, activation_id),
                        state=state,
                        command_id=command_id,
                        activation_id=activation_id,
                    )
                except AgentUpdateError as exc:
                    resume_error = exc
                else:
                    if marker is not None:
                        # If the immediate restart fails, the ordinary
                        # rollback reconciliation loop will see this durable
                        # receipt and retry the same bound marker before it
                        # reports failure to the controller.
                        try:
                            _atomic_json(
                                job / "activation-result.json",
                                {
                                    "schema_version": 1,
                                    "status": "rolled_back",
                                    "version": metadata["version"],
                                    "activation_id": activation_id,
                                    "command_id": command_id,
                                    "agent_id": state.agent_id,
                                    "error": (
                                        "Agent update staging failed before activation; "
                                        "managed miner resume is pending."
                                    ),
                                },
                            )
                        except OSError as exc:
                            retry_receipt_error = AgentUpdateError(
                                "managed miner retry receipt could not be written"
                            )
                        try:
                            if miner_manager is None:
                                miner_manager = self.managed_miner_factory(self.config)
                            _resume_managed_miner_marker(
                                self.config,
                                root,
                                state,
                                command_id,
                                activation_id,
                                miner_manager,
                            )
                        except AgentUpdateError as exc:
                            resume_error = exc
                            if retry_receipt_error is not None:
                                resume_error = retry_receipt_error
            if resume_error is not None:
                # Keep only the tiny rollback receipt needed by the retry
                # loop; a transient miner restart failure must not strand the
                # downloaded archive or expanded runtime on every device.
                shutil.rmtree(bundle_path, ignore_errors=True)
                for disposable in (
                    package_path,
                    updater_copy,
                    ready_path,
                    log_path,
                ):
                    try:
                        disposable.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        # A just-terminated updater may still hold its log for
                        # a moment.  It is bounded and never executable.
                        pass
                raise AgentUpdateError(
                    "Agent update staging failed and managed miner resume remains pending"
                ) from stage_error
            shutil.rmtree(job, ignore_errors=True)
            raise
        return {
            "state": "staged",
            "release_id": metadata["release_id"],
            "version": metadata["version"],
            "sha256": metadata["sha256"],
            "activation_id": activation_id,
            "activation_scheduled": True,
            "restart_required": True,
            "miner_continuity_checked": True,
        }


def _pending_activation(
    config: AgentConfig, state: AgentState
) -> tuple[Path, Path, dict[str, str]] | None:
    try:
        root, executable = _portable_root(config)
    except AgentUpdateError:
        return None
    pointer = root / "data" / "active-agent.json"
    try:
        if _is_link_or_reparse(pointer) or pointer.stat().st_size > 32_768:
            return None
        value = json.loads(pointer.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "version",
        "relative_executable",
        "activation_id",
        "command_id",
    }:
        return None
    activation_id = value.get("activation_id")
    command_id = value.get("command_id")
    relative = value.get("relative_executable")
    if (
        value.get("schema_version") != 1
        or value.get("version") != __version__
        or not isinstance(activation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", activation_id) is None
        or not isinstance(command_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            command_id,
        )
        is None
        or relative != f"versions/{__version__}/RigPilotAgent.exe"
    ):
        return None
    expected = (root / Path(relative)).resolve()
    if expected != executable:
        return None
    reported = (
        root
        / "data"
        / "agent-updates"
        / "reported"
        / f"{activation_id}.json"
    )
    try:
        reported_info = reported.lstat()
        if (
            stat.S_ISLNK(reported_info.st_mode)
            or bool(getattr(reported_info, "st_file_attributes", 0) & 0x400)
            or reported_info.st_size > 32_768
        ):
            return None
        receipt = json.loads(reported.read_text(encoding="utf-8"))
        if receipt == {
            "schema_version": 1,
            "activation_id": activation_id,
            "command_id": command_id,
            "version": __version__,
            "agent_id": state.agent_id,
            "reported": True,
        }:
            return None
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError):
        return None
    return (
        root,
        executable,
        {
            "activation_id": activation_id,
            "command_id": command_id,
            "version": __version__,
            "agent_id": state.agent_id,
        },
    )


def _linux_note_pending_agent_heartbeat(
    config: AgentConfig, state: AgentState
) -> dict[str, Any] | None:
    pending = _linux_pending_activation(config, state)
    if pending is None:
        return None
    _root, data_root, _executable, metadata = pending
    progress_path = (
        data_root
        / "agent-updates"
        / "health"
        / f"{metadata['activation_id']}.json"
    )
    count = 0
    try:
        info = progress_path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > 32_768
        ):
            return None
        previous = json.loads(progress_path.read_text(encoding="utf-8"))
        if (
            isinstance(previous, dict)
            and type(previous.get("schema_version")) is int
            and previous.get("schema_version") == 1
            and previous.get("version") == metadata["version"]
            and previous.get("activation_id") == metadata["activation_id"]
            and previous.get("command_id") == metadata["command_id"]
            and previous.get("agent_id") == state.agent_id
            and isinstance(previous.get("health_heartbeats"), int)
            and not isinstance(previous.get("health_heartbeats"), bool)
            and 0 <= previous["health_heartbeats"] <= 2
        ):
            count = int(previous["health_heartbeats"])
    except FileNotFoundError:
        pass
    except (UnicodeError, ValueError):
        # Advisory progress can be lost on power failure; count this heartbeat
        # as the first one again, never as a completed activation.
        pass
    except OSError:
        return None
    count = min(count + 1, 2)
    _atomic_json(
        progress_path,
        {"schema_version": 1, **metadata, "health_heartbeats": count},
        # Reconstructible progress: publish a complete file without waiting
        # for the filesystem journal. Final confirmation remains durable.
        durable_file=False,
    )
    if count < 2:
        return None
    return {**metadata, "health_heartbeats": count}


def _linux_confirm_pending_agent_update(
    config: AgentConfig,
    state: AgentState,
    metadata: Mapping[str, Any],
) -> bool:
    pending = _linux_pending_activation(config, state)
    if pending is None:
        return False
    _root, data_root, _executable, expected = pending
    if any(metadata.get(key) != expected[key] for key in expected):
        return False
    if metadata.get("health_heartbeats") != 2:
        return False
    _atomic_json(
        data_root
        / "agent-updates"
        / "confirmations"
        / f"{expected['activation_id']}.json",
        {
            "schema_version": 1,
            "activation_id": expected["activation_id"],
            "command_id": expected["command_id"],
            "version": __version__,
            "agent_id": state.agent_id,
            "health_heartbeats": 2,
        },
        durable_directory=True,
    )
    # A slow durable write may outlast the updater deadline. Do not report
    # success if the active slot or pending activation changed during it.
    return _linux_pending_activation(config, state) == pending


def _linux_mark_activation_reported(
    config: AgentConfig,
    state: AgentState,
    metadata: Mapping[str, Any],
) -> bool:
    pending = _linux_pending_activation(config, state)
    if pending is None:
        return False
    _root, data_root, _executable, expected = pending
    if any(metadata.get(key) != expected[key] for key in expected):
        return False
    _atomic_json(
        data_root
        / "agent-updates"
        / "reported"
        / f"{expected['activation_id']}.json",
        {"schema_version": 1, **expected, "reported": True},
        durable_directory=True,
    )
    return True


def note_pending_agent_heartbeat(
    config: AgentConfig, state: AgentState
) -> dict[str, Any] | None:
    """Count authenticated heartbeats and return an activation after two."""

    if platform.system() == "Linux":
        return _linux_note_pending_agent_heartbeat(config, state)
    pending = _pending_activation(config, state)
    if pending is None:
        return None
    root, _executable, metadata = pending
    progress_path = (
        root
        / "data"
        / "agent-updates"
        / "health"
        / f"{metadata['activation_id']}.json"
    )
    count = 0
    try:
        progress_info = progress_path.lstat()
        if (
            stat.S_ISLNK(progress_info.st_mode)
            or bool(getattr(progress_info, "st_file_attributes", 0) & 0x400)
            or progress_info.st_size > 32_768
        ):
            return None
        previous = json.loads(progress_path.read_text(encoding="utf-8"))
        if (
            isinstance(previous, dict)
            and previous.get("activation_id") == metadata["activation_id"]
            and previous.get("command_id") == metadata["command_id"]
            and previous.get("agent_id") == state.agent_id
            and isinstance(previous.get("health_heartbeats"), int)
            and not isinstance(previous.get("health_heartbeats"), bool)
        ):
            count = int(previous["health_heartbeats"])
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError):
        return None
    count = min(count + 1, 2)
    _atomic_json(
        progress_path,
        {
            "schema_version": 1,
            **metadata,
            "health_heartbeats": count,
        },
    )
    if count < 2:
        return None
    return {**metadata, "health_heartbeats": count}


def confirm_pending_agent_update(
    config: AgentConfig, state: AgentState, metadata: Mapping[str, Any]
) -> bool:
    """Commit the locally healthy slot before reporting final success."""

    if platform.system() == "Linux":
        return _linux_confirm_pending_agent_update(config, state, metadata)
    pending = _pending_activation(config, state)
    if pending is None:
        return False
    root, _executable, expected = pending
    if any(metadata.get(key) != expected[key] for key in expected):
        return False
    if metadata.get("health_heartbeats") != 2:
        return False
    confirmation = (
        root
        / "data"
        / "agent-updates"
        / "confirmations"
        / f"{expected['activation_id']}.json"
    )
    _atomic_json(
        confirmation,
        {
            "schema_version": 1,
            "activation_id": expected["activation_id"],
            "command_id": expected["command_id"],
            "version": __version__,
            "agent_id": state.agent_id,
            "health_heartbeats": 2,
        },
    )
    return True


def mark_activation_reported(
    config: AgentConfig, state: AgentState, metadata: Mapping[str, Any]
) -> bool:
    """Durably suppress final-result retries after the controller accepted it."""

    if platform.system() == "Linux":
        return _linux_mark_activation_reported(config, state, metadata)
    pending = _pending_activation(config, state)
    if pending is None:
        return False
    root, _executable, expected = pending
    if any(metadata.get(key) != expected[key] for key in expected):
        return False
    path = (
        root
        / "data"
        / "agent-updates"
        / "reported"
        / f"{expected['activation_id']}.json"
    )
    _atomic_json(
        path,
        {
            "schema_version": 1,
            **expected,
            "reported": True,
        },
    )
    return True


def pending_rollback_results(
    config: AgentConfig, state: AgentState
) -> list[tuple[Path, str, dict[str, Any]]]:
    """Return trusted, not-yet-reported rollback receipts for this Agent."""

    if platform.system() == "Linux":
        try:
            _root, data_root, _executable = _linux_update_layout(config)
        except AgentUpdateError:
            return []
        staged = data_root / "agent-updates" / "staged"
    else:
        try:
            root, _executable = _portable_root(config)
        except AgentUpdateError:
            return []
        staged = root / "data" / "agent-updates" / "staged"
    if not staged.is_dir() or _is_link_or_reparse(staged):
        return []
    results: list[tuple[Path, str, dict[str, Any]]] = []
    for job in staged.iterdir():
        if not job.is_dir() or re.fullmatch(r"[0-9a-f]{32}", job.name) is None:
            continue
        receipt = job / "activation-result.json"
        reported = job / "rollback-reported.json"
        if reported.exists():
            continue
        try:
            if _is_link_or_reparse(receipt) or receipt.stat().st_size > 32_768:
                continue
            value = json.loads(receipt.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            continue
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "status",
            "version",
            "activation_id",
            "command_id",
            "agent_id",
            "error",
        }:
            continue
        command_id = value.get("command_id")
        error = value.get("error")
        if (
            value.get("schema_version") != 1
            or value.get("status") != "rolled_back"
            or value.get("activation_id") != job.name
            or value.get("agent_id") != state.agent_id
            or not isinstance(command_id, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                command_id,
            )
            is None
            or not isinstance(value.get("version"), str)
            or _VERSION.fullmatch(value["version"]) is None
            or not isinstance(error, str)
            or not error
        ):
            continue
        results.append(
            (
                reported,
                command_id,
                {
                    "status": "failed",
                    "result": {
                        "activation_id": job.name,
                        "version": value["version"],
                        "rolled_back": True,
                    },
                    "error": " ".join(error.split())[:1000],
                },
            )
        )
    return results


def mark_rollback_reported(path: Path, command_id: str) -> None:
    _atomic_json(
        path,
        {"schema_version": 1, "command_id": command_id, "reported": True},
    )


def pending_activation_active(config: AgentConfig, state: AgentState) -> bool:
    """Return True while this Agent runs an unreported pending activation."""

    if platform.system() == "Linux":
        return _linux_pending_activation(config, state) is not None
    return _pending_activation(config, state) is not None


def linux_pending_activation_recovery(
    config: AgentConfig, state: AgentState
) -> dict[str, Any] | None:
    """Return an unreported Linux activation whose updater deadline expired.

    The fixed recovery timer stays the primary driver.  This check is the
    replacement Agent's own bounded fallback for a dead updater unit, a
    missing/disabled timer, or a reboot that orphaned an activation: once the
    updater's durable deadline plus grace has passed without a reported
    result, handing the activation to the immutable ``recover`` subcommand can
    only finalize a confirmed update or roll back an unconfirmed one.
    """

    if platform.system() != "Linux":
        return None
    pending = _linux_pending_activation(config, state)
    if pending is None:
        return None
    root, data_root, _executable, metadata = pending
    deadline: float | None = None
    state_path = root / "updater-state.json"
    try:
        info = state_path.lstat()
        if (
            not stat.S_ISLNK(info.st_mode)
            and stat.S_ISREG(info.st_mode)
            and info.st_size <= 32_768
        ):
            value = json.loads(state_path.read_text(encoding="utf-8"))
            raw_deadline = (
                value.get("deadline") if isinstance(value, dict) else None
            )
            if (
                isinstance(value, dict)
                and value.get("schema_version") == 1
                and isinstance(raw_deadline, (int, float))
                and not isinstance(raw_deadline, bool)
            ):
                deadline = float(raw_deadline)
    except (OSError, UnicodeError, ValueError, AttributeError):
        deadline = None
    if deadline is None:
        # The updater writes updater-state.json before it is ready, so its
        # absence plus a live pending pointer means an interrupted legacy or
        # partial cleanup.  Bound the fallback by the pending file's own age
        # using the updater's worst-case prepare+exit+confirmation window.
        try:
            deadline = (root / "pending-update.json").stat().st_mtime + (
                LINUX_UPDATER_PREPARE_SECONDS + LINUX_UPDATER_CONFIRMATION_SECONDS
            )
        except OSError:
            return None
    if time.time() <= deadline + LINUX_ACTIVATION_EXTRA_GRACE_SECONDS:
        return None
    return {
        "root": root,
        "data_root": data_root,
        "deadline": deadline,
        **metadata,
    }


def linux_trigger_activation_recovery(
    recovery: Mapping[str, Any],
    *,
    popen_factory: Callable[..., Any] | None = None,
) -> bool:
    """Hand one dead-line-expired Linux activation to the immutable updater.

    Uses the same fixed ``recover`` subcommand and unit hardening as the
    recovery timer; never waits, because a rollback restarts this very
    service while a finalized activation leaves it running.
    """

    root = Path(str(recovery["root"]))
    data_root = Path(str(recovery["data_root"]))
    updater = root / LINUX_UPDATE_SCRIPT_NAME
    unit_name = (
        f"rigpilot-agent-self-recovery-{str(recovery['activation_id'])[:12]}"
    )
    try:
        if not updater.is_file():
            return False
    except OSError:
        return False
    argv = [
        str(LINUX_SYSTEMD_RUN),
        "--unit",
        unit_name,
        "--collect",
        "--quiet",
        "--property=Type=exec",
        "--property=User=root",
        "--property=Group=root",
        "--property=UMask=0077",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectHome=yes",
        "--property=ProtectSystem=strict",
        f"--property=ReadWritePaths={root} {data_root} /run/lock",
        f"--working-directory={data_root}",
        str(LINUX_PYTHON),
        "-I",
        str(updater),
        "recover",
        "--program-root",
        str(root),
        "--data-root",
        str(data_root),
    ]
    factory = popen_factory or subprocess.Popen
    try:
        process = factory(
            argv,
            cwd=str(data_root),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=external_process_environment("Linux"),
        )
    except (OSError, ValueError):
        return False
    # The transient unit belongs to systemd; a rollback inside it restarts
    # this service, so never join or terminate the child from here.
    try:
        process.poll()
    except Exception:  # pragma: no cover - defensive for test doubles
        pass
    return True


PORTABLE_PREVIOUS_POINTER_NAME = "active-agent.previous.json"


def portable_pending_activation_recovery(
    config: AgentConfig, state: AgentState
) -> dict[str, Any] | None:
    """Return a portable (Windows) activation past every recovery bound.

    The trusted Windows updater waits PORTABLE_UPDATER_CONFIRMATION_SECONDS
    and then rolls back synchronously.  If it died (logoff, reboot, crash) the
    pointer keeps selecting a replacement that never confirmed; nothing else
    exists on Windows to bound that state.  This check arms an Agent-side
    rollback only when the updater deadline plus grace provably expired, the
    confirmation receipt is absent, and the updater's own previous-pointer
    backup can safely select a still-present older slot.
    """

    pending = _pending_activation(config, state)
    if pending is None:
        return None
    root, _executable, metadata = pending
    pointer = root / "data" / "active-agent.json"
    try:
        pointer_info = pointer.lstat()
    except OSError:
        return None
    if _is_link_or_reparse(pointer) or pointer_info.st_size > 32_768:
        return None
    confirmation = (
        root
        / "data"
        / "agent-updates"
        / "confirmations"
        / f"{metadata['activation_id']}.json"
    )
    try:
        confirmation_info = confirmation.lstat()
        if (
            not _is_link_or_reparse(confirmation)
            and confirmation_info.st_size <= 32_768
        ):
            value = json.loads(confirmation.read_text(encoding="utf-8"))
            if (
                isinstance(value, dict)
                and value.get("activation_id") == metadata["activation_id"]
                and value.get("health_heartbeats") == 2
            ):
                # Two authenticated heartbeats were committed; only the final
                # controller report may still be pending, and rolling back now
                # would discard a healthy replacement.
                return None
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        time.time() - pointer_info.st_mtime
        <= PORTABLE_UPDATER_CONFIRMATION_SECONDS + PORTABLE_ACTIVATION_EXTRA_GRACE_SECONDS
    ):
        return None
    previous = (
        root
        / "data"
        / "agent-updates"
        / "staged"
        / metadata["activation_id"]
        / PORTABLE_PREVIOUS_POINTER_NAME
    )
    try:
        previous_info = previous.lstat()
    except OSError:
        return None
    if _is_link_or_reparse(previous) or previous_info.st_size > 32_768:
        return None
    try:
        restore = json.loads(previous.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(restore, dict)
        or restore.get("schema_version") != 1
        or not isinstance(restore.get("version"), str)
        or _VERSION.fullmatch(restore["version"]) is None
        or restore.get("relative_executable")
        != f"versions/{restore['version']}/RigPilotAgent.exe"
    ):
        return None
    target = root / Path(str(restore["relative_executable"]))
    try:
        target_info = target.lstat()
    except OSError:
        return None
    if _is_link_or_reparse(target) or not stat.S_ISREG(target_info.st_mode):
        return None
    return {
        "root": root,
        "pointer": pointer,
        "restore": restore,
        **metadata,
    }


def portable_trigger_activation_recovery(
    recovery: Mapping[str, Any],
    *,
    system_name: str | None = None,
    popen_factory: Callable[..., Any] | None = None,
) -> bool:
    """Restore the previous portable slot, receipt the rollback, restart old.

    Mirrors the trusted updater's own rollback sequence (receipt, pointer
    restore, launcher start) for the one case it cannot cover: the updater
    process itself is gone.  The restored Agent reports the rolled_back
    receipt through the normal pending-rollback path.
    """

    root = Path(str(recovery["root"]))
    job = (
        root
        / "data"
        / "agent-updates"
        / "staged"
        / str(recovery["activation_id"])
    )
    _atomic_json(
        job / "activation-result.json",
        {
            "schema_version": 1,
            "status": "rolled_back",
            "version": str(recovery["version"]),
            "activation_id": str(recovery["activation_id"]),
            "command_id": str(recovery["command_id"]),
            "agent_id": str(recovery["agent_id"]),
            "error": (
                "replacement Agent exceeded the bounded activation deadline "
                "without confirming two authenticated heartbeats"
            ),
        },
    )
    _atomic_json(Path(recovery["pointer"]), recovery["restore"])
    system = system_name or platform.system()
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        powershell = (
            Path(system_root)
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        launcher = root / PORTABLE_LAUNCHER_NAME
        try:
            if powershell.is_file() and launcher.is_file():
                argv = [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(launcher),
                    "-Action",
                    "Start",
                    "-Scheduled",
                ]
                factory = popen_factory or subprocess.Popen
                creationflags = (
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if system == "Windows"
                    else 0
                )
                factory(
                    argv,
                    cwd=str(root),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=creationflags,
                )
        except (OSError, ValueError):
            # The pointer already selects the old slot; the autostart task or
            # an operator reboot still brings it up.
            pass
    return True


def quarantine_portable_rollback_slot(
    config: AgentConfig,
    version: str,
    activation_id: str,
) -> bool:
    """Move an orphaned rolled-back portable slot out of ``versions``.

    The updater quarantines its own rollbacks; an Agent-side rollback leaves
    the failed slot in place (its executable was running), which would make a
    retry of the same version fail forever with "slot already exists".  The
    restored Agent moves it beside the updater's quarantine entries, without
    deleting anything.
    """

    if _VERSION.fullmatch(version) is None or _ACTIVATION_ID.fullmatch(
        activation_id
    ) is None:
        return False
    if platform.system() == "Linux":
        return False
    try:
        root, running = _portable_root(config)
    except AgentUpdateError:
        return False
    slot = root / "versions" / version
    try:
        info = slot.lstat()
    except OSError:
        return False
    if _is_link_or_reparse(slot) or not stat.S_ISDIR(info.st_mode):
        return False
    pointer = root / "data" / "active-agent.json"
    try:
        current = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    if (
        isinstance(current, dict)
        and current.get("relative_executable")
        == f"versions/{version}/RigPilotAgent.exe"
    ):
        return False
    try:
        if running.resolve() == slot.resolve():
            return False
    except OSError:
        return False
    quarantine_root = root / "data" / "agent-updates" / "quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    quarantine = quarantine_root / f"{version}-{activation_id}"
    if quarantine.exists():
        return True
    slot.rename(quarantine)
    return True


def pending_miner_resume_markers(
    config: AgentConfig, state: AgentState
) -> list[tuple[str, str]]:
    """Return valid, unconsumed miner resume marker identities."""

    if platform.system() == "Linux":
        try:
            root, data_root, _executable = _linux_update_layout(config)
        except AgentUpdateError:
            return []
    else:
        try:
            root, _executable = _portable_root(config)
        except AgentUpdateError:
            return []
        data_root = root / "data"
    directory = data_root / "agent-updates" / "miner-resume"
    if not directory.is_dir() or _is_link_or_reparse(directory):
        return []
    results: list[tuple[str, str]] = []
    try:
        entries = sorted(
            entry
            for entry in directory.iterdir()
            if entry.name.endswith(".json")
            and _ACTIVATION_ID.fullmatch(entry.name[: -len(".json")]) is not None
        )
    except OSError:
        return []
    for entry in entries[:_MAX_RESUME_MARKER_SCAN]:
        try:
            info = entry.lstat()
            if (
                _is_link_or_reparse(entry)
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > 32_768
            ):
                continue
            value = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            not isinstance(value, dict)
            or value.get("agent_id") != state.agent_id
            or not isinstance(value.get("command_id"), str)
            or _COMMAND_ID.fullmatch(value["command_id"]) is None
            or value.get("activation_id") != entry.name[: -len(".json")]
        ):
            continue
        try:
            _load_miner_resume_marker(
                entry,
                state=state,
                command_id=value["command_id"],
                activation_id=value["activation_id"],
            )
        except AgentUpdateError:
            continue
        results.append((value["command_id"], value["activation_id"]))
    return results


def _read_installed_release_manifest(
    bundle: Path,
    *,
    expected_version: str | None,
    verify_files: bool,
) -> tuple[dict[str, tuple[str, str]], str]:
    """Read a portable manifest without ever following a bundle link."""

    manifest_path = bundle / UPDATE_MANIFEST_NAME
    try:
        info = manifest_path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            or info.st_size > _MAX_MANIFEST_BYTES
        ):
            raise AgentUpdateError("installed Agent release manifest is unsafe")
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except FileNotFoundError as exc:
        raise AgentUpdateError("installed Agent release manifest is missing") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpdateError("installed Agent release manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "version",
        "platform",
        "architecture",
        "files",
    }:
        raise AgentUpdateError("installed Agent release manifest has invalid fields")
    version = manifest.get("version")
    if (
        isinstance(manifest.get("schema_version"), bool)
        or manifest.get("schema_version") != 1
        or not isinstance(version, str)
        or _VERSION.fullmatch(version) is None
        or (expected_version is not None and version != expected_version)
        or manifest.get("platform") != "windows"
        or manifest.get("architecture") != "x86_64"
        or not isinstance(manifest.get("files"), dict)
    ):
        raise AgentUpdateError("installed Agent release manifest does not match")

    files: dict[str, tuple[str, str]] = {}
    for name, digest in manifest["files"].items():
        if (
            not isinstance(name, str)
            or not name
            or "/".join(_safe_archive_name(name)) != name
            or name.split("/", 1)[0].casefold() in {"data", "versions"}
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or name.casefold() in files
        ):
            raise AgentUpdateError("installed Agent release manifest contains an unsafe file")
        files[name.casefold()] = (name, digest)
        if not verify_files:
            continue
        candidate = bundle.joinpath(*PurePosixPath(name).parts)
        try:
            candidate_info = candidate.lstat()
        except FileNotFoundError as exc:
            raise AgentUpdateError("installed Agent release file is missing") from exc
        if (
            not stat.S_ISREG(candidate_info.st_mode)
            or _is_link_or_reparse(candidate)
        ):
            raise AgentUpdateError("installed Agent release file is unsafe")
        actual = _sha256_file(candidate)
        if actual != digest:
            raise AgentUpdateError("installed Agent release file failed SHA-256 verification")

    if verify_files:
        actual_files: set[str] = set()
        for directory, directory_names, file_names in os.walk(bundle, followlinks=False):
            current = Path(directory)
            if _is_link_or_reparse(current):
                raise AgentUpdateError("installed Agent release contains a linked directory")
            for directory_name in directory_names:
                if _is_link_or_reparse(current / directory_name):
                    raise AgentUpdateError("installed Agent release contains a linked directory")
            for file_name in file_names:
                candidate = current / file_name
                if _is_link_or_reparse(candidate) or not candidate.is_file():
                    raise AgentUpdateError("installed Agent release contains a special file")
                relative = candidate.relative_to(bundle).as_posix()
                if relative != UPDATE_MANIFEST_NAME:
                    actual_files.add(relative.casefold())
        if actual_files != set(files):
            raise AgentUpdateError("installed Agent release file set does not match its manifest")
    return files, hashlib.sha256(manifest_bytes).hexdigest()


def _checked_root_program_path(
    root: Path, relative: str, *, create_parents: bool = True
) -> Path:
    parts = _safe_archive_name(relative)
    if parts[0].casefold() in {"data", "versions"}:
        raise AgentUpdateError("Agent program synchronization cannot enter data or versions")
    candidate = root.joinpath(*parts)
    try:
        common = os.path.commonpath((str(root.resolve()), str(candidate.resolve(strict=False))))
    except (OSError, ValueError) as exc:
        raise AgentUpdateError("Agent program synchronization path is invalid") from exc
    if os.path.normcase(common) != os.path.normcase(str(root.resolve())):
        raise AgentUpdateError("Agent program synchronization escaped the portable root")
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.exists():
            if not current.is_dir() or _is_link_or_reparse(current):
                raise AgentUpdateError("Agent program synchronization parent is unsafe")
        else:
            if not create_parents:
                return candidate
            current.mkdir()
    return candidate


def _copy_program_file_atomically(
    source: Path, destination: Path, expected_digest: str
) -> None:
    if destination.exists() and (
        not destination.is_file() or _is_link_or_reparse(destination)
    ):
        raise AgentUpdateError("Agent program synchronization destination is unsafe")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".sync",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if _sha256_file(temporary) != expected_digest:
            raise AgentUpdateError("Agent program synchronization copy failed verification")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def reconcile_confirmed_release_root(
    config: AgentConfig,
    state: AgentState,
    *,
    system_name: str | None = None,
    executable: Path | None = None,
    frozen: bool | None = None,
    root_override: Path | None = None,
) -> bool:
    """Align the protected program root after a confirmed slotted update.

    Device data and every version slot are outside the deletion boundary.  An
    obsolete file is removed only when the previous root manifest declared it
    or it is the one explicitly retired 0.5.x compatibility shortcut.
    """

    if (system_name or platform.system()) != "Windows":
        return False
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if root_override is None and not is_frozen:
        return False
    raw_executable = Path(os.path.abspath(os.fspath(executable or sys.executable)))
    if root_override is not None:
        raw_root = Path(os.path.abspath(os.fspath(root_override)))
    elif raw_executable.parent.parent.name.casefold() == "versions":
        raw_root = raw_executable.parent.parent.parent
    else:
        return False

    if root_override is None:
        raw_program_data = _windows_program_data()
        protected_root = raw_program_data / "RigPilotAgentPortable"
        if (
            not raw_program_data.is_dir()
            or _is_link_or_reparse(raw_program_data)
            or os.path.normcase(str(raw_root))
            != os.path.normcase(str(protected_root))
        ):
            raise AgentUpdateError(
                "Agent program synchronization requires a plain protected ProgramData root"
            )

    raw_data_root = raw_root / "data"
    raw_versions_root = raw_root / "versions"
    raw_slot = raw_versions_root / __version__
    raw_expected_executable = raw_slot / "RigPilotAgent.exe"
    if (
        os.path.normcase(str(raw_executable))
        != os.path.normcase(str(raw_expected_executable))
    ):
        return False
    unsafe_paths = (
        raw_root,
        raw_data_root,
        raw_versions_root,
        raw_slot,
        raw_executable,
    )
    if (
        not raw_root.is_dir()
        or not raw_data_root.is_dir()
        or not raw_versions_root.is_dir()
        or not raw_slot.is_dir()
        or not raw_executable.is_file()
        or any(_is_link_or_reparse(path) for path in unsafe_paths)
    ):
        raise AgentUpdateError(
            "Agent program synchronization path is missing, linked, or unsafe"
        )

    root = raw_root.resolve(strict=True)
    data_root = raw_data_root.resolve(strict=True)
    versions_root = raw_versions_root.resolve(strict=True)
    slot = raw_slot.resolve(strict=True)
    executable_path = raw_executable.resolve(strict=True)
    if config.state_file.parent.resolve() != data_root:
        return False
    if root_override is None and not all(
        _windows_acl_is_update_safe(path)
        for path in (root, data_root, root / PORTABLE_LAUNCHER_NAME, root / UPDATE_SCRIPT_NAME)
    ):
        raise AgentUpdateError("protected Agent program synchronization ACL is unsafe")

    pointer_path = data_root / "active-agent.json"
    try:
        pointer_info = pointer_path.lstat()
        if _is_link_or_reparse(pointer_path) or pointer_info.st_size > 32_768:
            raise AgentUpdateError("active Agent pointer is unsafe during program synchronization")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return False
    activation_id = pointer.get("activation_id") if isinstance(pointer, dict) else None
    command_id = pointer.get("command_id") if isinstance(pointer, dict) else None
    if (
        not isinstance(pointer, dict)
        or set(pointer) != {
            "schema_version", "version", "relative_executable", "activation_id", "command_id"
        }
        or pointer.get("schema_version") != 1
        or pointer.get("version") != __version__
        or pointer.get("relative_executable") != f"versions/{__version__}/RigPilotAgent.exe"
        or not isinstance(activation_id, str)
        or _ACTIVATION_ID.fullmatch(activation_id) is None
        or not isinstance(command_id, str)
        or _COMMAND_ID.fullmatch(command_id) is None
    ):
        return False

    reported_path = data_root / "agent-updates" / "reported" / f"{activation_id}.json"
    result_path = data_root / "agent-updates" / "staged" / activation_id / "activation-result.json"
    try:
        confirmation_info = {
            path: path.lstat() for path in (reported_path, result_path)
        }
        if any(
            _is_link_or_reparse(path) or info.st_size > 32_768
            for path, info in confirmation_info.items()
        ):
            raise AgentUpdateError("Agent update confirmation receipt is unsafe")
        reported = json.loads(reported_path.read_text(encoding="utf-8"))
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if time.time() - confirmation_info[result_path].st_mtime < 2.0:
            return False
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpdateError("Agent update confirmation receipt is invalid") from exc
    expected_reported = {
        "schema_version": 1,
        "activation_id": activation_id,
        "command_id": command_id,
        "version": __version__,
        "agent_id": state.agent_id,
        "reported": True,
    }
    if reported != expected_reported or not isinstance(result, dict) or any(
        result.get(key) != value
        for key, value in {
            "schema_version": 1,
            "status": "succeeded",
            "version": __version__,
            "activation_id": activation_id,
            "command_id": command_id,
            "agent_id": state.agent_id,
            "stable_launcher_retained": True,
            "stable_updater_retained": True,
        }.items()
    ):
        return False

    new_files, manifest_digest = _read_installed_release_manifest(
        slot, expected_version=__version__, verify_files=False
    )
    if not _IMMUTABLE_V1_PROGRAM_FILES <= set(new_files):
        raise AgentUpdateError("confirmed Agent release is missing a trusted v1 component")

    # Protocol v1 deliberately never replaces these two ACL-locked root files.
    # Record their installed hashes in the synthesized root manifest while all
    # other program files follow the newly confirmed release.
    installed_manifest_files = {
        relative: digest for relative, digest in new_files.values()
    }
    for relative_key in sorted(_IMMUTABLE_V1_PROGRAM_FILES):
        relative = new_files[relative_key][0]
        installed = _checked_root_program_path(root, relative)
        if not installed.is_file() or _is_link_or_reparse(installed):
            raise AgentUpdateError("trusted v1 Agent update component is unsafe")
        if root_override is None and not _windows_acl_is_update_safe(installed):
            raise AgentUpdateError("trusted v1 Agent update component ACL is unsafe")
        installed_manifest_files[relative] = _sha256_file(installed)
    installed_manifest = {
        "schema_version": 1,
        "version": __version__,
        "platform": "windows",
        "architecture": "x86_64",
        "files": dict(
            sorted(installed_manifest_files.items(), key=lambda item: item[0].casefold())
        ),
    }
    installed_manifest_digest = hashlib.sha256(
        _json_document_bytes(installed_manifest)
    ).hexdigest()

    manifest_destination = root / UPDATE_MANIFEST_NAME
    receipt_path = data_root / "agent-updates" / "root-sync" / f"{activation_id}.json"
    try:
        receipt_info = receipt_path.lstat()
        if _is_link_or_reparse(receipt_path) or receipt_info.st_size > 32_768:
            raise AgentUpdateError("Agent program synchronization receipt is unsafe")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt == {
            "schema_version": 1,
            "activation_id": activation_id,
            "command_id": command_id,
            "version": __version__,
            "agent_id": state.agent_id,
            "release_manifest_sha256": manifest_digest,
            "installed_manifest_sha256": installed_manifest_digest,
            "synchronized": True,
        } and (
            manifest_destination.is_file()
            and not _is_link_or_reparse(manifest_destination)
            and _sha256_file(manifest_destination) == installed_manifest_digest
        ):
            return False
        raise AgentUpdateError("Agent program synchronization receipt is invalid")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentUpdateError("Agent program synchronization receipt is invalid") from exc

    verified_files, verified_manifest_digest = _read_installed_release_manifest(
        slot, expected_version=__version__, verify_files=True
    )
    if verified_files != new_files or verified_manifest_digest != manifest_digest:
        raise AgentUpdateError(
            "confirmed Agent release changed during program synchronization"
        )
    try:
        old_files, _old_digest = _read_installed_release_manifest(
            root, expected_version=None, verify_files=False
        )
    except AgentUpdateError:
        old_files = {}

    # Copy every non-bootstrap program file.  The active executable remains in
    # its immutable version slot, so a partial root copy cannot interrupt it;
    # the ACL-locked protocol-v1 launcher/updater remain the trusted pair.
    for relative_key in sorted(new_files):
        if relative_key in _IMMUTABLE_V1_PROGRAM_FILES:
            continue
        relative, digest = new_files[relative_key]
        source = slot.joinpath(*PurePosixPath(relative).parts)
        destination = _checked_root_program_path(root, relative)
        _copy_program_file_atomically(source, destination, digest)

    updater_digest = _sha256_file(root / UPDATE_SCRIPT_NAME)
    _atomic_json(
        data_root / "agent-update-ready.json",
        {"schema_version": 1, "acl_protected": True, "updater_sha256": updater_digest},
    )

    obsolete = (set(old_files) - set(new_files)) | {"enable-power-actions.bat"}
    obsolete -= set(new_files)
    obsolete -= _IMMUTABLE_V1_PROGRAM_FILES
    empty_parents: set[Path] = set()
    for relative_key in sorted(obsolete):
        relative = (
            old_files[relative_key][0] if relative_key in old_files else "Enable-Power-Actions.bat"
        )
        destination = _checked_root_program_path(root, relative, create_parents=False)
        if not destination.exists():
            continue
        if not destination.is_file() or _is_link_or_reparse(destination):
            raise AgentUpdateError("obsolete Agent program file is unsafe")
        destination.unlink()
        empty_parents.add(destination.parent)
    for directory in sorted(empty_parents, key=lambda value: len(value.parts), reverse=True):
        current = directory
        while current != root and current not in {data_root, versions_root}:
            if _is_link_or_reparse(current):
                raise AgentUpdateError("obsolete Agent program directory is unsafe")
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    _atomic_json(manifest_destination, installed_manifest)
    if _sha256_file(manifest_destination) != installed_manifest_digest:
        raise AgentUpdateError("installed Agent root manifest failed verification")
    _atomic_json(
        receipt_path,
        {
            "schema_version": 1,
            "activation_id": activation_id,
            "command_id": command_id,
            "version": __version__,
            "agent_id": state.agent_id,
            "release_manifest_sha256": manifest_digest,
            "installed_manifest_sha256": installed_manifest_digest,
            "synchronized": True,
        },
    )
    return True
