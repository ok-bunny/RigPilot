"""Linux systemd watchdog policy and progress-gated notifications.

The online Linux update protocol replaces only the version-slotted Agent
executable.  This module lets that already-trusted executable repair one fixed
systemd drop-in by asking PID 1 to run the same active executable in a tightly
bounded transient unit.  No controller supplied path or command is accepted.

The watchdog sender is deliberately progress gated.  Its helper thread never
keeps a wedged main loop alive indefinitely: the main loop must renew a short
monotonic lease after making local progress, including a handled network
failure.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import socket
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .agent_updates import (
    AgentUpdateError,
    LINUX_DATA_ROOT,
    LINUX_SYSTEMCTL,
    LINUX_SYSTEMD_RUN,
    _linux_plain_root_owned,
    _linux_protected_directory_chain,
    _linux_trusted_system_executable,
    _linux_update_layout,
)
from .config import AgentConfig
from .external_process import external_process_environment
from .version import __version__


SUPERVISION_CONFIG = Path("/etc/rigpilot-agent/config.json")
SUPERVISION_SERVICE_FILE = Path("/etc/systemd/system/rigpilot-agent.service")
SUPERVISION_DROPIN_DIRECTORY = Path("/etc/systemd/system/rigpilot-agent.service.d")
SUPERVISION_DROPIN = SUPERVISION_DROPIN_DIRECTORY / "supervision.conf"
SUPERVISION_SERVICE = "rigpilot-agent.service"
SUPERVISION_STATUS = LINUX_DATA_ROOT / "supervision-status.json"
UPDATE_LOCK = Path("/run/lock/rigpilot-agent-update.lock")
WATCHDOG_SECONDS = 120
NOTIFY_SOCKET_TIMEOUT_SECONDS = 0.2
START_TIMEOUT_SECONDS = 45
SYSTEMCTL_RESTART_TIMEOUT_SECONDS = 90
SUPERVISION_CONTENT = (
    b"[Service]\n"
    b"# PyInstaller onefile keeps a bootloader parent around the Python process.\n"
    b"# The Python child claims MAINPID before feeding the progress watchdog.\n"
    b"Type=notify\n"
    b"NotifyAccess=all\n"
    b"Restart=on-failure\n"
    b"KillMode=process\n"
    b"TimeoutStartSec=45s\n"
    b"WatchdogSec=120s\n"
)

_UNIT = re.compile(
    r"^rigpilot-agent-supervision-[1-9][0-9]{0,9}-[1-9][0-9]{0,9}$"
)
_STATUS_STATES = frozenset({"submitted", "running", "failed", "succeeded"})
_STATUS_REASONS = frozenset(
    {
        "none",
        "update_in_progress",
        "active_layout_unsafe",
        "fixed_path_unsafe",
        "systemd_run_unsafe",
        "systemctl_unsafe",
        "transient_submit_failed",
        "transient_submit_rejected",
        "maintenance_not_authorized",
        "dropin_write_failed",
        "daemon_reload_failed",
        "service_restart_failed",
        "policy_verification_failed",
        "rollback_failed",
    }
)


class LinuxSupervisionError(RuntimeError):
    """The fixed Linux Agent watchdog policy could not be verified/repaired."""

    def __init__(self, message: str, *, reason: str = "fixed_path_unsafe") -> None:
        super().__init__(message)
        self.reason = reason if reason in _STATUS_REASONS else "fixed_path_unsafe"


def _production_linux_root() -> bool:
    return bool(
        platform.system() == "Linux"
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
    )


def _trusted_paths(*, require_dropin: bool) -> bool:
    directories = (
        SUPERVISION_CONFIG.parent,
        SUPERVISION_SERVICE_FILE.parent,
    )
    files = (SUPERVISION_CONFIG, SUPERVISION_SERVICE_FILE)
    if not all(
        _linux_plain_root_owned(path, directory=True, enforce_owner=True)
        and _linux_protected_directory_chain(path)
        for path in directories
    ):
        return False
    try:
        directory_info = SUPERVISION_DROPIN_DIRECTORY.lstat()
    except FileNotFoundError:
        # Online upgrades from an older package may not have a drop-in
        # directory yet.  Only its fixed, already-validated systemd parent is
        # allowed to be absent here; the root repair worker creates it using a
        # directory fd and validates it again before writing the fixed file.
        if require_dropin:
            return False
    except OSError:
        return False
    else:
        if not (
            stat.S_ISDIR(directory_info.st_mode)
            and not stat.S_ISLNK(directory_info.st_mode)
            and int(getattr(directory_info, "st_uid", -1)) == 0
            and directory_info.st_mode & 0o022 == 0
            and _linux_protected_directory_chain(SUPERVISION_DROPIN_DIRECTORY)
        ):
            return False
    if not all(
        _linux_plain_root_owned(path, directory=False, enforce_owner=True)
        and int(getattr(path.lstat(), "st_nlink", 0)) == 1
        for path in files
    ):
        return False
    try:
        info = SUPERVISION_DROPIN.lstat()
    except FileNotFoundError:
        return not require_dropin
    except OSError:
        return False
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and int(getattr(info, "st_uid", -1)) == 0
        and int(getattr(info, "st_nlink", 0)) == 1
        and info.st_mode & 0o022 == 0
        and int(getattr(info, "st_size", 0)) <= 4096
    )


def _read_effective_policy(
    *, runner: Callable[..., Any] = subprocess.run
) -> dict[str, str] | None:
    if not _linux_trusted_system_executable(LINUX_SYSTEMCTL):
        return None
    try:
        completed = runner(
            [
                str(LINUX_SYSTEMCTL),
                "show",
                SUPERVISION_SERVICE,
                "--property=NotifyAccess",
                "--property=Type",
                "--property=WatchdogUSec",
                "--property=Restart",
                "--property=KillMode",
                "--property=TimeoutStartUSec",
                "--property=DropInPaths",
                "--property=ActiveState",
            ],
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            env=external_process_environment("Linux"),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    values: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            return None
        values[key] = value.strip()
    expected = {
        "NotifyAccess", "Type", "WatchdogUSec", "Restart", "KillMode",
        "TimeoutStartUSec",
        "DropInPaths", "ActiveState",
    }
    return values if set(values) == expected else None


def _systemd_timespan_seconds(value: str) -> float | None:
    """Parse the small normalized timespan subset systemctl may emit."""

    units = {"h": 3600.0, "min": 60.0, "s": 1.0, "ms": 0.001, "us": 0.000001}
    position = 0
    total = 0.0
    token = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(h|min|ms|us|s)")
    while position < len(value):
        match = token.match(value, position)
        if match is None:
            return None
        total += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    return total


def supervision_policy_ready(
    config: AgentConfig, *, runner: Callable[..., Any] = subprocess.run
) -> bool:
    """Return true only for the exact file and effective PID-1 properties."""

    del config  # reserved for a future explicit local enable switch
    if not _production_linux_root() or not _trusted_paths(require_dropin=True):
        return False
    try:
        if SUPERVISION_DROPIN.read_bytes() != SUPERVISION_CONTENT:
            return False
    except OSError:
        return False
    effective = _read_effective_policy(runner=runner)
    seconds = (
        _systemd_timespan_seconds(effective.get("WatchdogUSec", ""))
        if effective is not None
        else None
    )
    start_seconds = (
        _systemd_timespan_seconds(effective.get("TimeoutStartUSec", ""))
        if effective is not None
        else None
    )
    return bool(
        effective is not None
        and effective.get("NotifyAccess") == "all"
        and effective.get("Type") == "notify"
        and effective.get("Restart") == "on-failure"
        and effective.get("KillMode") == "process"
        and start_seconds is not None
        and abs(start_seconds - START_TIMEOUT_SECONDS) < 0.001
        and seconds is not None
        and abs(seconds - WATCHDOG_SECONDS) < 0.001
        and str(SUPERVISION_DROPIN) in effective.get("DropInPaths", "").split()
        and effective.get("ActiveState") in {"active", "activating"}
    )


def _status_boundary_trusted(*, require_status: bool) -> bool:
    if not (
        _linux_plain_root_owned(
            SUPERVISION_STATUS.parent, directory=True, enforce_owner=True
        )
        and _linux_protected_directory_chain(SUPERVISION_STATUS.parent)
    ):
        return False
    try:
        info = SUPERVISION_STATUS.lstat()
    except FileNotFoundError:
        return not require_status
    except OSError:
        return False
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and int(getattr(info, "st_uid", -1)) == 0
        and int(getattr(info, "st_nlink", 0)) == 1
        and info.st_mode & 0o077 == 0
        and info.st_size <= 4096
    )


def _status_document(state: str, reason: str, unit: str, attempt: int) -> dict[str, Any]:
    if (
        state not in _STATUS_STATES
        or reason not in _STATUS_REASONS
        or _UNIT.fullmatch(unit) is None
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or not 1 <= attempt <= 9_999_999_999
    ):
        raise LinuxSupervisionError("supervision repair status is invalid")
    return {
        "schema_version": 1,
        "state": state,
        "reason": reason,
        "unit": unit,
        "version": __version__,
        "attempt": attempt,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_status(state: str, reason: str, unit: str, attempt: int) -> None:
    document = _status_document(state, reason, unit, attempt)
    if not _status_boundary_trusted(require_status=False):
        raise LinuxSupervisionError("supervision status boundary is unsafe")
    parent_fd = os.open(
        SUPERVISION_STATUS.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    name = f".supervision-status.tmp.{os.getpid()}"
    fd: int | None = None
    temporary_created = False
    try:
        fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        temporary_created = True
        payload = (json.dumps(document, separators=(",", ":")) + "\n").encode("ascii")
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short write")
            view = view[count:]
        os.fchmod(fd, 0o600)
        os.fchown(fd, 0, 0)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(name, SUPERVISION_STATUS.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_created = False
        os.fsync(parent_fd)
    finally:
        try:
            if fd is not None:
                os.close(fd)
            try:
                if temporary_created:
                    os.unlink(name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(parent_fd)


def _load_status() -> dict[str, Any] | None:
    if not _status_boundary_trusted(require_status=True):
        return None
    try:
        value = json.loads(SUPERVISION_STATUS.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("state"), str)
        or value["state"] not in _STATUS_STATES
        or not isinstance(value.get("reason"), str)
        or value["reason"] not in _STATUS_REASONS
    ):
        return None
    return value


def _status_age(status: Mapping[str, Any]) -> float:
    try:
        stamp = datetime.fromisoformat(str(status["updated_at"]))
        if stamp.tzinfo is None:
            return float("inf")
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        # A future receipt is malformed or reflects an unsafe clock jump.  It
        # must not suppress repairs as if it had just been written.
        return age if age >= 0.0 else float("inf")
    except (KeyError, TypeError, ValueError, OverflowError):
        return float("inf")


def _pending_update_exists(root: Path) -> bool:
    marker = root / "pending-update.json"
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    # A valid marker is an active activation transaction.  An invalid marker
    # is treated as active as well (fail closed) and must be repaired by the
    # updater/recovery path rather than by supervision.
    return True


def _trusted_update_lock_parent() -> bool:
    """Accept Ubuntu's root-owned sticky /run/lock without following links."""

    try:
        info = UPDATE_LOCK.parent.lstat()
    except OSError:
        return False
    writable = bool(info.st_mode & 0o022)
    return bool(
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and int(getattr(info, "st_uid", -1)) == 0
        and (not writable or bool(info.st_mode & stat.S_ISVTX))
        and _linux_protected_directory_chain(UPDATE_LOCK.parent.parent)
    )


@contextlib.contextmanager
def _update_guard(root: Path) -> Iterator[bool]:
    """Hold a shared update lock; yield false if an updater owns it."""

    if _pending_update_exists(root):
        yield False
        return
    import fcntl

    fd: int | None = None
    available = False
    try:
        if _trusted_update_lock_parent():
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(UPDATE_LOCK, flags, 0o600)
            info = os.fstat(fd)
            if (
                stat.S_ISREG(info.st_mode)
                and int(getattr(info, "st_uid", -1)) == 0
                and int(getattr(info, "st_nlink", 0)) == 1
                # Installer-created root-owned 0644 is protected; normalize it.
                and info.st_mode & 0o022 == 0
            ):
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                available = True
    except OSError:
        # Only lock setup can become "unavailable". Exceptions from the
        # protected caller below must propagate, without a second yield.
        available = False
    try:
        yield available
    finally:
        if fd is not None:
            try:
                if available:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)



def schedule_supervision_policy_repair(
    config: AgentConfig, *, runner: Callable[..., Any] = subprocess.run
) -> bool:
    """Submit fixed repair attempts forever with a bounded low-frequency delay."""

    if not _production_linux_root():
        return False
    if supervision_policy_ready(config, runner=runner):
        # The repair unit intentionally restarts the Agent before it can mark
        # itself complete.  The newly started process is the authority that
        # observes PID 1's effective policy and promotes the receipt.
        ready_status = _load_status()
        if (
            isinstance(ready_status, dict)
            and ready_status.get("version") == __version__
            and ready_status.get("state") in {"submitted", "running"}
            and isinstance(ready_status.get("unit"), str)
            and _UNIT.fullmatch(ready_status["unit"]) is not None
            and isinstance(ready_status.get("attempt"), int)
            and not isinstance(ready_status.get("attempt"), bool)
        ):
            try:
                _write_status(
                    "succeeded",
                    "none",
                    ready_status["unit"],
                    ready_status["attempt"],
                )
            except (OSError, LinuxSupervisionError):
                pass
        return False
    try:
        root, data_root, executable = _linux_update_layout(config)
    except AgentUpdateError as exc:
        raise LinuxSupervisionError(str(exc), reason="active_layout_unsafe") from exc
    status = _load_status()
    attempt = 1
    if isinstance(status, dict) and status.get("version") == __version__:
        age = _status_age(status)
        previous = status.get("attempt")
        if not (
            isinstance(previous, int)
            and not isinstance(previous, bool)
            and 1 <= previous <= 9_999_999_999
        ):
            previous = 0
        # 1, 2, 4, 8, 16, 32, then 60 minute retries forever.  A submitted
        # worker receives at least five minutes to finish before replacement.
        retry_delay = min(
            3600.0, 60.0 * (2 ** min(max(previous - 1, 0), 6))
        )
        if status.get("state") in {"submitted", "running"}:
            retry_delay = max(300.0, retry_delay)
        if age < retry_delay:
            return False
        attempt = min(previous + 1, 9_999_999_999)
    unit = f"rigpilot-agent-supervision-{os.getpid()}-{attempt}"
    if not _trusted_paths(require_dropin=False):
        raise LinuxSupervisionError("supervision policy path is unsafe")
    if not _linux_trusted_system_executable(LINUX_SYSTEMD_RUN):
        raise LinuxSupervisionError("systemd-run is unsafe", reason="systemd_run_unsafe")
    with _update_guard(root) as available:
        if not available:
            return False
        argv = [
            str(LINUX_SYSTEMD_RUN),
            "--unit", unit,
            "--collect", "--quiet", "--no-block",
            "--property=Type=oneshot",
            "--property=TimeoutStartSec=240s",
            "--property=User=root", "--property=Group=root", "--property=UMask=0077",
            "--property=NoNewPrivileges=no", "--property=PrivateTmp=yes",
            "--property=ProtectHome=yes", "--property=ProtectSystem=strict",
            f"--property=ReadWritePaths={SUPERVISION_SERVICE_FILE.parent} {data_root} {UPDATE_LOCK}",
            f"--working-directory={data_root}",
            str(executable), "--config", str(SUPERVISION_CONFIG),
            "--repair-supervision-policy",
        ]
        _write_status("submitted", "none", unit, attempt)
        try:
            completed = runner(
                argv, shell=False, check=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                env=external_process_environment("Linux"),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _write_status("failed", "transient_submit_failed", unit, attempt)
            raise LinuxSupervisionError(
                "could not submit supervision repair", reason="transient_submit_failed"
            ) from exc
        if completed.returncode != 0:
            _write_status("failed", "transient_submit_rejected", unit, attempt)
            raise LinuxSupervisionError(
                "systemd rejected supervision repair", reason="transient_submit_rejected"
            )
    return True


def _ensure_dropin_directory() -> None:
    """Create the one fixed systemd drop-in directory without link traversal."""

    parent = SUPERVISION_DROPIN_DIRECTORY.parent
    if not (
        _linux_plain_root_owned(parent, directory=True, enforce_owner=True)
        and _linux_protected_directory_chain(parent)
    ):
        raise LinuxSupervisionError("systemd unit directory is unsafe")
    parent_fd = os.open(
        parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        try:
            os.mkdir(SUPERVISION_DROPIN_DIRECTORY.name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
    finally:
        os.close(parent_fd)
    if not _trusted_paths(require_dropin=False):
        raise LinuxSupervisionError("supervision drop-in directory is unsafe")


def _read_dropin_backup() -> bytes | None:
    if not _trusted_paths(require_dropin=False):
        raise LinuxSupervisionError("supervision policy path is unsafe")
    try:
        return SUPERVISION_DROPIN.read_bytes()
    except FileNotFoundError:
        return None


def _replace_dropin(payload: bytes | None) -> None:
    _ensure_dropin_directory()
    if not _trusted_paths(require_dropin=False):
        raise LinuxSupervisionError("supervision policy path is unsafe")
    directory_fd = os.open(
        SUPERVISION_DROPIN_DIRECTORY,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".supervision.conf.tmp.{os.getpid()}"
    fd: int | None = None
    temporary_created = False
    try:
        if payload is None:
            try:
                os.unlink(SUPERVISION_DROPIN.name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.fsync(directory_fd)
            return
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        temporary_created = True
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(fd, 0o644)
        os.fchown(fd, 0, 0)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, SUPERVISION_DROPIN.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)
    finally:
        try:
            if fd is not None:
                os.close(fd)
            try:
                if temporary_created:
                    os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(directory_fd)


def _write_dropin() -> None:
    _replace_dropin(SUPERVISION_CONTENT)


def _systemctl_action(
    action: str,
    *,
    runner: Callable[..., Any],
    reason: str,
) -> None:
    argv = [str(LINUX_SYSTEMCTL), action]
    timeout = 30
    if action == "restart":
        argv.append(SUPERVISION_SERVICE)
        # Must exceed the exact systemd TimeoutStartSec written above so the
        # caller can observe a notify-start failure and roll back deterministically.
        timeout = SYSTEMCTL_RESTART_TIMEOUT_SECONDS
    try:
        completed = runner(
            argv,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=external_process_environment("Linux"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LinuxSupervisionError(
            f"systemctl {action} failed", reason=reason
        ) from exc
    if completed.returncode != 0:
        raise LinuxSupervisionError(f"systemctl {action} failed", reason=reason)


def _rollback_supervision_policy(
    previous: bytes | None, *, runner: Callable[..., Any]
) -> bool:
    """Best-effort restoration after a new notify policy failed activation."""

    try:
        _replace_dropin(previous)
        _systemctl_action(
            "daemon-reload", runner=runner, reason="rollback_failed"
        )
        _systemctl_action("restart", runner=runner, reason="rollback_failed")
    except (OSError, LinuxSupervisionError):
        return False
    return True


def repair_supervision_policy(
    config: AgentConfig,
    config_path: str | os.PathLike[str],
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Hidden root-only action for the fixed systemd-run repair unit."""

    status = _load_status()
    unit = status.get("unit") if isinstance(status, dict) else None
    attempt = status.get("attempt") if isinstance(status, dict) else None
    if (
        not _production_linux_root()
        or Path(os.path.abspath(os.fspath(config_path))) != SUPERVISION_CONFIG
        or not isinstance(unit, str)
        or _UNIT.fullmatch(unit) is None
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or status.get("version") != __version__
        or status.get("state") != "submitted"
        or _status_age(status) > 180
    ):
        raise LinuxSupervisionError(
            "supervision repair is not authorized", reason="maintenance_not_authorized"
        )
    try:
        root, _data_root, _executable = _linux_update_layout(config)
    except AgentUpdateError as exc:
        _write_status("failed", "active_layout_unsafe", unit, attempt)
        raise LinuxSupervisionError(str(exc), reason="active_layout_unsafe") from exc
    with _update_guard(root) as available:
        if not available:
            _write_status("failed", "update_in_progress", unit, attempt)
            return
        _write_status("running", "none", unit, attempt)
        if not _linux_trusted_system_executable(LINUX_SYSTEMCTL):
            _write_status("failed", "systemctl_unsafe", unit, attempt)
            raise LinuxSupervisionError("systemctl is unsafe", reason="systemctl_unsafe")
        try:
            previous = _read_dropin_backup()
        except (OSError, LinuxSupervisionError) as exc:
            _write_status("failed", "dropin_write_failed", unit, attempt)
            raise LinuxSupervisionError(
                "could not back up supervision drop-in", reason="dropin_write_failed"
            ) from exc
        try:
            _write_dropin()
            _systemctl_action(
                "daemon-reload", runner=runner, reason="daemon_reload_failed"
            )
            _systemctl_action(
                "restart", runner=runner, reason="service_restart_failed"
            )
            if not supervision_policy_ready(config, runner=runner):
                raise LinuxSupervisionError(
                    "effective supervision policy did not verify",
                    reason="policy_verification_failed",
                )
        except (OSError, LinuxSupervisionError) as exc:
            original_reason = (
                exc.reason if isinstance(exc, LinuxSupervisionError)
                else "dropin_write_failed"
            )
            rollback_ok = _rollback_supervision_policy(previous, runner=runner)
            final_reason = original_reason if rollback_ok else "rollback_failed"
            _write_status("failed", final_reason, unit, attempt)
            raise LinuxSupervisionError(
                "supervision policy activation failed and was rolled back"
                if rollback_ok
                else "supervision policy activation and rollback both failed",
                reason=final_reason,
            ) from exc
        # The restarted process verifies effective properties and promotes this.


def supervision_diagnostics(
    config: AgentConfig, *, runner: Callable[..., Any] = subprocess.run
) -> dict[str, Any]:
    supported = platform.system() == "Linux"
    configured = supported and _production_linux_root()
    ready = supervision_policy_ready(config, runner=runner) if configured else False
    receipt = _load_status() if configured else None
    repair_state = receipt.get("state") if isinstance(receipt, dict) else None
    repair_reason = receipt.get("reason") if isinstance(receipt, dict) else "none"
    if ready:
        state = "ready"
    elif not supported:
        state = "unsupported"
    elif not configured:
        state = "disabled"
    elif repair_state in {"submitted", "running"} and _status_age(receipt) < 300:
        state = "repair_pending"
    elif _trusted_paths(require_dropin=False):
        state = "misconfigured"
    else:
        state = "unavailable"
    return {
        "schema_version": 1,
        "configured": bool(configured),
        "ready": bool(ready),
        "state": state,
        "service_name": "systemd watchdog",
        "watchdog_seconds": WATCHDOG_SECONDS,
        "repair_state": repair_state,
        "repair_reason": repair_reason,
    }


def notify_watchdog(
    *,
    environment: Mapping[str, str] | None = None,
    socket_factory: Callable[..., Any] = socket.socket,
    payload: bytes = b"WATCHDOG=1",
) -> bool:
    """Send one bounded sd_notify datagram; silently disable outside systemd."""

    if platform.system() != "Linux":
        return False
    ready_payload = f"MAINPID={os.getpid()}\nREADY=1".encode("ascii")
    if payload not in {b"WATCHDOG=1", b"READY=1", b"STOPPING=1", ready_payload}:
        raise ValueError("unsupported sd_notify payload")
    env = os.environ if environment is None else environment
    address = env.get("NOTIFY_SOCKET", "")
    if not address or len(address.encode("utf-8", errors="ignore")) > 107:
        return False
    if not (address.startswith("/") or address.startswith("@")):
        return False
    destination = "\0" + address[1:] if address.startswith("@") else address
    family = getattr(socket, "AF_UNIX", None)
    if family is None:
        return False
    try:
        channel = socket_factory(family, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        # READY is also sent on the startup thread. A full notify queue must
        # not keep enrollment or the progress sender blocked indefinitely.
        channel.settimeout(NOTIFY_SOCKET_TIMEOUT_SECONDS)
        channel.sendto(payload, destination)
    except OSError:
        return False
    finally:
        try:
            channel.close()
        except OSError:
            pass
    return True


class SystemdWatchdog:
    """Feed systemd only while the main loop renews a bounded progress lease."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        notifier: Callable[..., bool] = notify_watchdog,
        autostart: bool = True,
    ) -> None:
        self._environment = dict(os.environ if environment is None else environment)
        self._monotonic = monotonic
        self._notifier = notifier
        self._deadline = 0.0
        self._lock = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        usec_text = self._environment.get("WATCHDOG_USEC", "")
        try:
            usec = int(usec_text)
        except (TypeError, ValueError):
            usec = 0
        pid_text = self._environment.get("WATCHDOG_PID", "")
        # With a PyInstaller onefile executable systemd initially sees the
        # bootloader parent as MainPID while this Python process is its direct
        # child.  Accept only that exact relationship, then securely transfer
        # MainPID to this process through the notify socket.  Arbitrary PIDs
        # are still rejected.
        pid_valid = bool(
            not pid_text
            or (
                pid_text.isdigit()
                and int(pid_text) in {os.getpid(), os.getppid()}
            )
        )
        self.enabled = bool(
            platform.system() == "Linux"
            and self._environment.get("NOTIFY_SOCKET")
            and usec > 0
            and pid_valid
        )
        self.interval_seconds = max(0.25, usec / 2_000_000.0) if self.enabled else 0.0
        self._ready_notified = False
        if self.enabled and autostart:
            self._notify_ready()
            self._thread = threading.Thread(
                target=self._run, name="rigpilot-systemd-watchdog", daemon=True
            )
            try:
                self._thread.start()
            except RuntimeError:
                self.enabled = False
                self._thread = None

    def _notify_ready(self) -> bool:
        if not self.enabled:
            return False
        if self._ready_notified:
            return True
        payload = f"MAINPID={os.getpid()}\nREADY=1".encode("ascii")
        self._ready_notified = bool(
            self._notifier(environment=self._environment, payload=payload)
        )
        return self._ready_notified

    def progress(self, ttl_seconds: float = 90.0) -> bool:
        if isinstance(ttl_seconds, bool) or not 1.0 <= float(ttl_seconds) <= 1800.0:
            raise ValueError("watchdog progress TTL must be from 1 to 1800 seconds")
        if not self.enabled:
            return False
        with self._lock:
            if self._closed:
                return False
            # Replace rather than extend the old lease.  A normal heartbeat
            # immediately shrinks a previous long-operation allowance.
            self._deadline = self._monotonic() + float(ttl_seconds)
        return True

    @contextlib.contextmanager
    def lease(self, ttl_seconds: float) -> Iterator["SystemdWatchdog"]:
        self.progress(ttl_seconds)
        try:
            yield self
        finally:
            # A download or update may legitimately need a long allowance,
            # but once it returns a later main-loop wedge must again be caught
            # on the ordinary 90-second progress horizon.
            self.progress(90.0)

    def feed_if_progressing(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            allowed = not self._closed and self._monotonic() <= self._deadline
        return bool(
            allowed
            and self._notify_ready()
            and self._notifier(
                environment=self._environment, payload=b"WATCHDOG=1"
            )
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.feed_if_progressing()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(max(self.interval_seconds + 0.5, 1.0), 5.0))

    def __enter__(self) -> "SystemdWatchdog":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
