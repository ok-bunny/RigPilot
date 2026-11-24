#!/usr/bin/env python3
"""Fixed protocol-v1 updater for the installed Ubuntu Agent.

The downloaded release is data, never update logic.  This root-owned helper
validates the staged bundle and copies only ``RigPilotAgent`` into a new
version slot.  In particular, a launcher or updater included for fresh/manual
installation is never copied over the installed trust anchors.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any


SCHEMA_VERSION = 1
FIXED_PROGRAM_ROOT = Path("/opt/rigpilot-agent")
FIXED_DATA_ROOT = Path("/var/lib/rigpilot-agent")
ACTIVE_VERSION_NAME = "active-version"
PENDING_NAME = "pending-update.json"
UPDATER_STATE_NAME = "updater-state.json"
UPDATE_LOCK = Path("/run/lock/rigpilot-agent-update.lock")
MANIFEST_NAME = "agent-release.json"
IMMUTABLE_V1_COMPONENTS = frozenset(
    {"RigPilotAgentLauncher.sh", "RigPilotAgentUpdater.py"}
)
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
ACTIVATION_RE = re.compile(r"^[0-9a-f]{32}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
CONFIRMATION_SECONDS = 90
AGENT_EXIT_SECONDS = 120
MAX_RECOVERY_ATTEMPTS = 5
UPDATE_LOCK_WAIT_SECONDS = 20
# P4 (user-approved 2026-11-03): a board without RTC power restarts with its
# wall clock behind the durable mtime of files this updater itself wrote.
# Anything beyond this tolerance proves the clock moved backwards, so the
# stored wall-clock deadline can only be reached after NTP; recovery must not
# depend on network time.
CLOCK_RESET_TOLERANCE_SECONDS = 5.0


class UpdateFailure(RuntimeError):
    pass


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if match is None:
        raise UpdateFailure("Agent version is invalid")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _plain(path: Path, *, directory: bool, root_owned: bool = True) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise UpdateFailure(f"required path is unavailable: {path}") from exc
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected:
        raise UpdateFailure(f"path is not a plain {'directory' if directory else 'file'}: {path}")
    if root_owned and (info.st_uid != 0 or info.st_mode & 0o022):
        raise UpdateFailure(f"trusted path is not protected by root ownership: {path}")
    return info


def _fixed_roots(program_root: str, data_root: str) -> tuple[Path, Path]:
    program = Path(program_root)
    data = Path(data_root)
    if program != FIXED_PROGRAM_ROOT or data != FIXED_DATA_ROOT:
        raise UpdateFailure("the Linux updater requires the fixed installation roots")
    _plain(program, directory=True)
    _plain(program / "versions", directory=True)
    # Runtime data is writable by the root Agent, so only its type/owner is a
    # trust decision; mode 0700 is enforced by Install.sh.
    data_info = _plain(data, directory=True, root_owned=False)
    if data_info.st_uid != 0 or data_info.st_mode & 0o077:
        raise UpdateFailure("Agent data root permissions are unsafe")
    for name in IMMUTABLE_V1_COMPONENTS:
        _plain(program / name, directory=False)
    return program, data


def _safe_relative(name: str) -> str:
    if not name or "\\" in name or "\x00" in name:
        raise UpdateFailure("release manifest contains an unsafe path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateFailure("release manifest contains an unsafe path")
    if any(len(part) > 255 or any(ord(char) < 32 for char in part) for part in path.parts):
        raise UpdateFailure("release manifest contains an unsafe path")
    return path.as_posix()


def _bundle_files(bundle: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for parent, directories, names in os.walk(bundle, followlinks=False):
        parent_path = Path(parent)
        if directories:
            raise UpdateFailure("online update payload must not contain subdirectories")
        for name in names:
            candidate = parent_path / name
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise UpdateFailure("staged release contains a link or special file")
            relative = candidate.relative_to(bundle).as_posix()
            if relative in files:
                raise UpdateFailure("staged release contains duplicate files")
            files[relative] = candidate
    return files


def verify_bundle(bundle: Path, expected_version: str) -> dict[str, Path]:
    _plain(bundle, directory=True, root_owned=False)
    actual = _bundle_files(bundle)
    manifest_path = actual.get(MANIFEST_NAME)
    if manifest_path is None:
        raise UpdateFailure("staged release metadata is incomplete")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise UpdateFailure("staged release manifest is too large")
    try:
        def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON field")
                value[key] = item
            return value

        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=no_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise UpdateFailure("staged release manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "version", "platform", "architecture", "files"
    }:
        raise UpdateFailure("staged release manifest fields are invalid")
    if (
        isinstance(manifest.get("schema_version"), bool)
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("version") != expected_version
        or manifest.get("platform") != "linux"
        or manifest.get("architecture") != "x86_64"
        or not isinstance(manifest.get("files"), dict)
    ):
        raise UpdateFailure("staged release manifest does not match this update")
    declared: dict[str, str] = {}
    for raw_name, raw_digest in manifest["files"].items():
        if not isinstance(raw_name, str) or not isinstance(raw_digest, str):
            raise UpdateFailure("staged release manifest entry is invalid")
        name = _safe_relative(raw_name)
        if name == MANIFEST_NAME or not DIGEST_RE.fullmatch(raw_digest):
            raise UpdateFailure("staged release manifest entry is invalid")
        declared[name] = raw_digest
    if set(actual) & IMMUTABLE_V1_COMPONENTS:
        raise UpdateFailure("online release may not contain fixed update helpers")
    if set(actual) != {MANIFEST_NAME, "RigPilotAgent"} or set(declared) != {"RigPilotAgent"}:
        raise UpdateFailure("staged release files do not exactly match its manifest")
    for name, digest in declared.items():
        if _sha256(actual[name]) != digest:
            raise UpdateFailure(f"staged release file failed SHA-256 verification: {name}")

    return actual


def _atomic_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def _read_version(path: Path) -> str:
    _plain(path, directory=False)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UpdateFailure("cannot read active Agent version") from exc
    try:
        value = raw.decode("ascii")
    except UnicodeError as exc:
        raise UpdateFailure("active Agent version is invalid") from exc
    if not value.endswith("\n") or value.count("\n") != 1:
        raise UpdateFailure("active Agent version is invalid")
    version = value[:-1]
    if VERSION_RE.fullmatch(version) is None:
        raise UpdateFailure("active Agent version is invalid")
    return version


def _write_active(program: Path, version: str) -> None:
    _atomic_bytes(program / ACTIVE_VERSION_NAME, f"{version}\n".encode("ascii"), 0o644)


def _run_systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/systemctl", *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=check,
    )


def _result_path(data: Path, activation_id: str) -> Path:
    return data / "agent-updates" / "staged" / activation_id / "activation-result.json"


def _cleanup_staged_payload(data: Path, pending: dict[str, Any]) -> None:
    job = data / "agent-updates" / "staged" / pending["activation_id"]
    _plain(job, directory=True, root_owned=False)
    for name in (
        "release.tar.gz",
        "updater.log",
        "updater-ready.json",
        "activation-arm.json",
        "activation-abort.json",
    ):
        path = job / name
        if not os.path.lexists(path):
            continue
        _plain(path, directory=False, root_owned=False)
        path.unlink()
    bundle = job / "bundle"
    if os.path.lexists(bundle):
        _plain(bundle, directory=True, root_owned=False)
        # verify_bundle already established that the online bundle has no
        # nested directories or special entries.  Re-check before deletion so
        # cleanup never follows a post-validation link.
        _bundle_files(bundle)
        shutil.rmtree(bundle)
    _fsync_directory(job)


def _write_result(data: Path, pending: dict[str, Any], status: str, error: str | None = None) -> None:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "version": pending["version"],
        "activation_id": pending["activation_id"],
        "command_id": pending["command_id"],
        "agent_id": pending["agent_id"],
    }
    if status == "succeeded":
        value.update(stable_launcher_retained=True, stable_updater_retained=True)
    elif error is not None:
        value["error"] = error[:2048]
    _atomic_json(_result_path(data, pending["activation_id"]), value)


def _confirmation_valid(data: Path, pending: dict[str, Any]) -> bool:
    path = data / "agent-updates" / "confirmations" / f"{pending['activation_id']}.json"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32_768:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("schema_version") == SCHEMA_VERSION
        and value.get("activation_id") == pending["activation_id"]
        and value.get("command_id") == pending["command_id"]
        and value.get("agent_id") == pending["agent_id"]
        and value.get("version") == pending["version"]
        and isinstance(value.get("health_heartbeats"), int)
        and not isinstance(value.get("health_heartbeats"), bool)
        and value["health_heartbeats"] >= 2
    )


def _reported_valid(data: Path, pending: dict[str, Any]) -> bool:
    path = data / "agent-updates" / "reported" / f"{pending['activation_id']}.json"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32_768:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    return value == {
        "schema_version": SCHEMA_VERSION,
        "activation_id": pending["activation_id"],
        "command_id": pending["command_id"],
        "version": pending["version"],
        "agent_id": pending["agent_id"],
        "reported": True,
    }


_ABANDONED_SLOT_RE = re.compile(
    r"^\.(?:incoming|rolled-back)-"
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-[0-9a-f]{32}$"
)


def _reclaim_abandoned_slots(program: Path) -> None:
    """Reclaim update staging/quarantine slots a concluded update left behind.

    A rollback deliberately preserves the failed ``.rolled-back-*`` slot and a
    hard-crashed activation can leave an orphaned ``.incoming-*`` staging slot.
    Without reclamation each failed attempt permanently consumed roughly twice
    the package size.  The next activation's staging phase reclaims every
    leftover, so at most one abandoned slot set exists at any time.  This is
    best-effort disk hygiene: failures are ignored and never gate an otherwise
    healthy activation.
    """

    try:
        entries = list((program / "versions").iterdir())
    except OSError:
        return
    for entry in entries:
        if _ABANDONED_SLOT_RE.fullmatch(entry.name) is None:
            continue
        try:
            info = entry.lstat()
            # lstat-based S_ISDIR already excludes symlinks and special files.
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(entry)
        except OSError:
            continue


def _remove_control_files(program: Path) -> None:
    # pending remains visible until the replacement has posted and durably
    # marked its final controller result.  Delete it first; a crash between
    # these unlinks leaves state-only recovery, which is unambiguous.
    for name in (PENDING_NAME, UPDATER_STATE_NAME):
        path = program / name
        if not os.path.lexists(path):
            continue
        _plain(path, directory=False)
        path.unlink()
    descriptor = os.open(program, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback(program: Path, data: Path, pending: dict[str, Any], state: dict[str, Any], error: str) -> None:
    previous = state.get("previous_version")
    if not isinstance(previous, str) or VERSION_RE.fullmatch(previous) is None:
        raise UpdateFailure(f"rollback has no valid previous version: {error}")
    previous_binary = program / "versions" / previous / "RigPilotAgent"
    _plain(previous_binary, directory=False)
    target = program / "versions" / pending["version"]
    incoming = program / "versions" / f".incoming-{pending['version']}-{pending['activation_id']}"
    quarantine = program / "versions" / f".rolled-back-{pending['version']}-{pending['activation_id']}"
    if pending["version"] != previous and os.path.lexists(target) and not quarantine.exists():
        try:
            _plain(target, directory=True)
            if target.parent != program / "versions":
                raise UpdateFailure("rollback target slot escapes the versions directory")
            target.rename(quarantine)
        except (OSError, UpdateFailure) as exc:
            error = f"{error}; failed to quarantine target slot: {exc}"
    if incoming.is_dir() and not incoming.is_symlink():
        shutil.rmtree(incoming)
    # P1 (user-approved 2026-11-03): cleanup is mandatory and precedes every
    # remaining durable write.  The previous order wrote the receipt first,
    # so ENOSPC failed the whole rollback and wedged the ~2x-package staged
    # payload while the recovery timer retried the same failing write
    # forever.  Deleting payload files and renaming slots consumes no free
    # space; performing them first guarantees the pointer write, the receipt,
    # and the previous-version restart can complete on a full filesystem.
    # Every step is idempotent, so a crash mid-rollback stays retryable.
    _cleanup_staged_payload(data, pending)
    _write_active(program, previous)
    restart_error: Exception | None = None
    try:
        # An aborted handoff or a retried receipt may find the original
        # process still serving. Only its exact installed executable counts.
        if not _service_runs_target(program, previous):
            _run_systemctl("restart", "rigpilot-agent.service")
        _run_systemctl("is-active", "--quiet", "rigpilot-agent.service")
    except (OSError, subprocess.SubprocessError) as exc:
        restart_error = exc
        error = f"{error}; rollback restart failed: {exc}"
    # The old Agent consumes this receipt to resume the miner stopped before
    # staging.  Recovery state is erased only once the receipt is durable; a
    # receipt failure after cleanup still raises so the timer retries the
    # remaining writes (cleanup above is a no-op on retry).
    _write_result(data, pending, "rolled_back", error)
    if restart_error is not None:
        raise UpdateFailure(error) from restart_error
    _remove_control_files(program)


def _validate_pending(pending: Any) -> dict[str, Any]:
    required = {
        "schema_version", "version", "relative_executable", "activation_id",
        "command_id", "agent_id",
    }
    if not isinstance(pending, dict) or set(pending) != required:
        raise UpdateFailure("pending update metadata fields are invalid")
    if (
        pending.get("schema_version") != SCHEMA_VERSION
        or not isinstance(pending.get("version"), str)
        or VERSION_RE.fullmatch(pending["version"]) is None
        or pending.get("relative_executable") != f"versions/{pending['version']}/RigPilotAgent"
        or not isinstance(pending.get("activation_id"), str)
        or ACTIVATION_RE.fullmatch(pending["activation_id"]) is None
        or not isinstance(pending.get("command_id"), str)
        or UUID_RE.fullmatch(pending["command_id"]) is None
        or not isinstance(pending.get("agent_id"), str)
        or UUID_RE.fullmatch(pending["agent_id"]) is None
    ):
        raise UpdateFailure("pending update metadata is invalid")
    return pending


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    try:
        info = _plain(path, directory=False)
        if info.st_size > 32_768:
            raise UpdateFailure("update recovery metadata is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateFailure("update recovery metadata is invalid") from exc
    if not isinstance(value, dict):
        raise UpdateFailure("update recovery metadata is invalid")
    return value


def _read_control(program: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    pending_value = _read_optional_json(program / PENDING_NAME)
    state = _read_optional_json(program / UPDATER_STATE_NAME)
    pending = _validate_pending(pending_value) if pending_value is not None else None
    if state is not None:
        if state.get("schema_version") != SCHEMA_VERSION:
            raise UpdateFailure("updater recovery metadata is invalid")
        state_pending = _validate_pending(state.get("pending"))
        if pending is not None and state_pending != pending:
            raise UpdateFailure("pending and recovery metadata do not match")
        pending = state_pending
    return pending, state


def _write_state(program: Path, state: dict[str, Any], phase: str) -> None:
    updated = dict(state)
    updated["phase"] = phase
    _atomic_json(program / UPDATER_STATE_NAME, updated)
    state.clear()
    state.update(updated)


def _service_runs_target(program: Path, version: str) -> bool:
    try:
        completed = _run_systemctl(
            "show",
            "--property=MainPID",
            "--value",
            "rigpilot-agent.service",
            check=False,
        )
        if completed.returncode != 0 or not isinstance(completed.stdout, str):
            return False
        raw_pid = completed.stdout.strip()
        if not raw_pid.isascii() or not raw_pid.isdigit():
            return False
        pid = int(raw_pid)
        if pid <= 1:
            return False
        actual = Path(f"/proc/{pid}/exe").resolve(strict=True)
        expected = (program / "versions" / version / "RigPilotAgent").resolve(strict=True)
        return actual == expected
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _deadline_open(deadline: Any, durable_anchors: tuple[float, ...]) -> bool:
    """Return True only while the confirmation window can still be open.

    Missing or non-numeric deadlines count as expired, exactly like the
    previous inline comparison.  P4 adds the durable-anchor guard: when the
    current wall clock is provably behind a filesystem timestamp this update
    itself wrote, the wall-clock deadline is unreachable until NTP corrects
    the clock, so treat it as expired and keep recovery bounded.
    """

    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return False
    now = time.time()
    if now >= deadline:
        return False
    for anchor in durable_anchors:
        if now < anchor - CLOCK_RESET_TOLERANCE_SECONDS:
            return False
    return True


def _durable_mtime_anchors(program: Path) -> tuple[float, ...]:
    """Capture control-file mtimes before recovery rewrites them.

    ``updater-state.json`` is refreshed on every recovery attempt, so an
    anchor captured after that write would inherit a reset clock.  The
    pending pointer is never rewritten during recovery and both files were
    written by the healthy pre-power-loss clock, making their mtimes the
    reliable fallback clock for no-RTC devices.
    """

    anchors: list[float] = []
    for name in (PENDING_NAME, UPDATER_STATE_NAME):
        try:
            info = (program / name).lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            anchors.append(info.st_mtime)
    return tuple(anchors)


def _finish_or_rollback(
    program: Path,
    data: Path,
    pending: dict[str, Any],
    state: dict[str, Any],
    durable_anchors: tuple[float, ...] = (),
) -> int:
    if _confirmation_valid(data, pending) or state.get("phase") == "confirmed":
        _write_result(data, pending, "succeeded")
        if state.get("phase") != "confirmed":
            _write_state(program, state, "confirmed")
        if _reported_valid(data, pending):
            _cleanup_staged_payload(data, pending)
            _remove_control_files(program)
        return 0
    attempts = state.get("recovery_attempts", 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise UpdateFailure("updater recovery attempt counter is invalid")
    attempts += 1
    state["recovery_attempts"] = attempts
    _write_state(program, state, str(state.get("phase", "activated")))
    if attempts >= MAX_RECOVERY_ATTEMPTS:
        _rollback(
            program,
            data,
            pending,
            state,
            "replacement Agent exceeded the bounded recovery attempt limit",
        )
        return 1
    deadline = state.get("deadline")
    if _deadline_open(deadline, durable_anchors):
        if _service_runs_target(program, pending["version"]):
            return 0
        # A stale old MainPID or an inactive unit is not evidence that the
        # replacement is healthy.  Restart the launcher now instead of wasting
        # the entire confirmation window; the next recovery tick re-runs the
        # executable check above against the restarted MainPID.
        try:
            _run_systemctl("restart", "rigpilot-agent.service")
        except (OSError, subprocess.SubprocessError):
            return 0
        return 0
    _rollback(program, data, pending, state, "replacement Agent did not confirm two authenticated heartbeats")
    return 1


def _activation_armed(job: Path, pending: dict[str, Any]) -> bool:
    path = job / "activation-arm.json"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32_768:
            raise UpdateFailure("Agent activation arm marker is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateFailure("Agent activation arm marker is invalid") from exc
    expected = {
        "schema_version": SCHEMA_VERSION,
        "version": pending["version"],
        "activation_id": pending["activation_id"],
        "command_id": pending["command_id"],
        "agent_id": pending["agent_id"],
        "armed": True,
    }
    if value != expected:
        raise UpdateFailure("Agent activation arm marker does not match this update")
    return True


def _activation_abort_error(job: Path, pending: dict[str, Any]) -> str | None:
    path = job / "activation-abort.json"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32_768:
            raise UpdateFailure("Agent activation abort marker is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateFailure("Agent activation abort marker is invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "version",
        "activation_id",
        "command_id",
        "agent_id",
        "aborted",
        "error",
    }:
        raise UpdateFailure("Agent activation abort marker is invalid")
    error = value.get("error")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "version": pending["version"],
        "activation_id": pending["activation_id"],
        "command_id": pending["command_id"],
        "agent_id": pending["agent_id"],
        "aborted": True,
    }
    if (
        any(value.get(key) != item for key, item in expected.items())
        or not isinstance(error, str)
        or not error.strip()
        or len(error) > 2048
    ):
        raise UpdateFailure("Agent activation abort marker does not match this update")
    return error


def _wait_for_activation(
    job: Path,
    pending: dict[str, Any],
    pid: int,
    expected_start: str | None,
    *,
    timeout: float = AGENT_EXIT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    process_exited = expected_start is None
    while time.monotonic() < deadline:
        abort_error = _activation_abort_error(job, pending)
        if abort_error is not None:
            raise UpdateFailure(f"Agent aborted activation: {abort_error}")
        if not process_exited:
            try:
                current = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[21]
            except (FileNotFoundError, ProcessLookupError):
                process_exited = True
            except (OSError, UnicodeError, IndexError) as exc:
                raise UpdateFailure("cannot verify the old Agent process") from exc
            else:
                if expected_start is not None and current != expected_start:
                    process_exited = True
        armed = _activation_armed(job, pending)
        if process_exited and armed:
            return
        time.sleep(0.2)
    raise UpdateFailure(
        "activation was not armed by the Agent and completed before the deadline"
    )


def activate(args: argparse.Namespace) -> int:
    program, data = _fixed_roots(args.program_root, args.data_root)
    if VERSION_RE.fullmatch(args.version) is None:
        raise UpdateFailure("target version is invalid")
    if ACTIVATION_RE.fullmatch(args.activation_id) is None:
        raise UpdateFailure("activation id is invalid")
    if UUID_RE.fullmatch(args.agent_id) is None or UUID_RE.fullmatch(args.command_id) is None:
        raise UpdateFailure("activation identity is invalid")
    if args.agent_pid <= 1 or args.agent_pid == os.getpid():
        raise UpdateFailure("old Agent PID is invalid")
    job = data / "agent-updates" / "staged" / args.activation_id
    bundle = Path(args.staged_bundle)
    ready = Path(args.ready_path)
    if bundle != job / "bundle" or ready != job / "updater-ready.json":
        raise UpdateFailure("update staging path escapes its activation job")
    _plain(job, directory=True, root_owned=False)
    files = verify_bundle(bundle, args.version)
    # The running Agent already performed the executable --version preflight
    # while /opt was read-only.  Never execute downloaded bytes inside this
    # updater's transient unit: it necessarily has write access to version
    # slots and the fixed v1 trust-anchor directory during the atomic switch.

    previous = _read_version(program / ACTIVE_VERSION_NAME)
    if _version_tuple(args.version) <= _version_tuple(previous):
        raise UpdateFailure("target Agent version must be newer than the active version")
    pending = {
        "schema_version": SCHEMA_VERSION,
        "version": args.version,
        "relative_executable": f"versions/{args.version}/RigPilotAgent",
        "activation_id": args.activation_id,
        "command_id": args.command_id,
        "agent_id": args.agent_id,
    }
    updater_state = {
        "schema_version": SCHEMA_VERSION,
        "pending": pending,
        "previous_version": previous,
        "deadline": time.time() + AGENT_EXIT_SECONDS + CONFIRMATION_SECONDS,
        "phase": "prepared",
        "slot_created": False,
        "recovery_attempts": 0,
    }
    # State precedes readiness.  If power is lost after the Agent accepts the
    # ready marker but before pending/active are published, the recovery timer
    # can unambiguously restart the previous version and write a rollback
    # receipt so the stopped miner is resumed.
    _atomic_json(program / UPDATER_STATE_NAME, updater_state)
    confirmed = False
    try:
        try:
            expected_start = Path(f"/proc/{args.agent_pid}/stat").read_text(encoding="ascii").split()[21]
        except FileNotFoundError:
            expected_start = None
        except (OSError, UnicodeError, IndexError) as exc:
            raise UpdateFailure("cannot verify the old Agent process") from exc
        _atomic_json(
            ready,
            {
                "schema_version": SCHEMA_VERSION,
                "activation_id": args.activation_id,
                "command_id": args.command_id,
                "agent_id": args.agent_id,
                "updater_pid": os.getpid(),
            },
        )
        _wait_for_activation(job, pending, args.agent_pid, expected_start)
        _write_state(program, updater_state, "armed")

        slot = program / "versions" / args.version
        incoming = program / "versions" / f".incoming-{args.version}-{args.activation_id}"
        # Reclaim staging/quarantine leftovers from earlier concluded updates
        # before the existence checks below: an orphaned .incoming-* slot from
        # a hard-crashed activation would otherwise wedge the same version
        # forever, and abandoned .rolled-back-* quarantine slots would keep
        # consuming roughly twice the package size per failed attempt.
        _reclaim_abandoned_slots(program)
        if slot.exists() or incoming.exists():
            raise UpdateFailure("target Agent version slot already exists")
        incoming.mkdir(mode=0o700)
        try:
            shutil.copyfile(files["RigPilotAgent"], incoming / "RigPilotAgent")
            os.chown(incoming / "RigPilotAgent", 0, 0)
            os.chmod(incoming / "RigPilotAgent", 0o755)
            if _sha256(incoming / "RigPilotAgent") != _sha256(files["RigPilotAgent"]):
                raise UpdateFailure("installed Agent slot failed its copy verification")
            with (incoming / "RigPilotAgent").open("rb") as copied_binary:
                os.fsync(copied_binary.fileno())
            os.chmod(incoming, 0o755)
            _fsync_directory(incoming)
            updater_state["slot_created"] = True
            _write_state(program, updater_state, "slot_installing")
            incoming.rename(slot)
            _fsync_directory(program / "versions")
            _write_state(program, updater_state, "slot_ready")
        finally:
            if incoming.exists() and incoming.is_dir() and not incoming.is_symlink():
                shutil.rmtree(incoming)

        _atomic_json(program / PENDING_NAME, pending)
        _write_active(program, args.version)
        updater_state["deadline"] = time.time() + CONFIRMATION_SECONDS
        _write_state(program, updater_state, "activated")
        _run_systemctl("restart", "rigpilot-agent.service")
        deadline = time.monotonic() + CONFIRMATION_SECONDS
        while time.monotonic() < deadline:
            if _confirmation_valid(data, pending):
                if not confirmed:
                    confirmed = True
                    _write_result(data, pending, "succeeded")
                    _write_state(program, updater_state, "confirmed")
                if _reported_valid(data, pending):
                    _cleanup_staged_payload(data, pending)
                    _remove_control_files(program)
                    return 0
            time.sleep(1)
        if not confirmed and _confirmation_valid(data, pending):
            # P2 (user-approved 2026-11-03): a confirmation durably written
            # during the final sleep window was previously never rechecked,
            # so a healthy replacement could be rolled back off-by-one.
            # Re-examine once after the loop deadline before rolling back.
            confirmed = True
            _write_result(data, pending, "succeeded")
            _write_state(program, updater_state, "confirmed")
            if _reported_valid(data, pending):
                _cleanup_staged_payload(data, pending)
                _remove_control_files(program)
                return 0
        if confirmed:
            # Never roll back a replacement that already proved two
            # authenticated heartbeats.  The recovery timer retains pending
            # until mark_activation_reported publishes its exact receipt.
            return 0
        raise UpdateFailure("replacement Agent did not reconnect before the rollback deadline")
    except Exception as exc:
        if confirmed:
            # Receipt/cleanup failures remain retryable by recovery.  A
            # replacement that proved its health must not be rolled back
            # merely because final bookkeeping could not be persisted.
            raise UpdateFailure(f"confirmed update finalization failed: {exc}") from exc
        _rollback(program, data, pending, updater_state, str(exc))
        return 1


def recover(args: argparse.Namespace) -> int:
    program, data = _fixed_roots(args.program_root, args.data_root)
    if not os.path.lexists(program / PENDING_NAME) and not os.path.lexists(program / UPDATER_STATE_NAME):
        return 0
    pending, state = _read_control(program)
    if pending is None:
        # Both files absent was handled above; reaching this branch means a
        # malformed partial cleanup and must not be silently ignored.
        raise UpdateFailure("update recovery metadata has no bound activation")
    # P4: capture the durable control-file mtimes before any recovery write
    # refreshes them, so a reset clock cannot launder its own timestamps.
    durable_anchors = _durable_mtime_anchors(program)
    active = _read_version(program / ACTIVE_VERSION_NAME)
    if state is None:
        # The normal cleanup order removes pending before state.  Therefore a
        # pending-only record can only be a partial legacy/manual mutation.
        # If controller reporting is complete it is safe to finish cleanup;
        # otherwise synthesize a bounded state so recovery reaches a terminal
        # receipt instead of remaining wedged forever.
        if _reported_valid(data, pending):
            _write_result(data, pending, "succeeded")
            _cleanup_staged_payload(data, pending)
            _remove_control_files(program)
            return 0
        state = {
            "schema_version": SCHEMA_VERSION,
            "pending": pending,
            "previous_version": active,
            "deadline": time.time(),
            "phase": "confirmed" if _confirmation_valid(data, pending) else "activated",
            "slot_created": False,
            "recovery_attempts": 0,
        }
        _atomic_json(program / UPDATER_STATE_NAME, state)
    previous = state.get("previous_version")
    if not isinstance(previous, str) or VERSION_RE.fullmatch(previous) is None:
        raise UpdateFailure("update recovery has no valid previous version")
    if active == previous and state.get("phase") in {"prepared", "slot_ready"}:
        _rollback(
            program,
            data,
            pending,
            state,
            "online update was interrupted before activation",
        )
        return 1
    if active == pending["version"]:
        if not os.path.lexists(program / PENDING_NAME):
            _atomic_json(program / PENDING_NAME, pending)
        return _finish_or_rollback(program, data, pending, state, durable_anchors)
    _rollback(
        program,
        data,
        pending,
        state,
        "online update active-version pointer was inconsistent after restart",
    )
    return 1


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="RigPilot fixed Linux Agent updater")
    commands = value.add_subparsers(dest="action", required=True)
    activate_parser = commands.add_parser("activate")
    recover_parser = commands.add_parser("recover")
    for command in (activate_parser, recover_parser):
        command.add_argument("--program-root", required=True)
        command.add_argument("--data-root", required=True)
    activate_parser.add_argument("--staged-bundle", required=True)
    activate_parser.add_argument("--version", required=True)
    activate_parser.add_argument("--activation-id", required=True)
    activate_parser.add_argument("--agent-id", required=True)
    activate_parser.add_argument("--command-id", required=True)
    activate_parser.add_argument("--agent-pid", required=True, type=int)
    activate_parser.add_argument("--ready-path", required=True)
    return value


def main() -> int:
    args = parser().parse_args()
    UPDATE_LOCK.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with UPDATE_LOCK.open("a+b") as lock:
        deadline = time.monotonic() + UPDATE_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if args.action == "recover":
                    return 0
                if time.monotonic() >= deadline:
                    print(
                        "RigPilot Agent update failed: another install, update, or "
                        "recovery process still owns the update lock",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 75
                time.sleep(0.2)
        try:
            return activate(args) if args.action == "activate" else recover(args)
        except UpdateFailure as exc:
            print(f"RigPilot Agent update failed: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
