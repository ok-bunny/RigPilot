"""Stable, secret-free identity helpers for Agent-owned child processes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import psutil

from .fsutil import is_link_or_reparse as _link_or_reparse
from .fsutil import normalized_path as _normalized_path


_PROCESS_RECORD_LOCK = threading.RLock()


@dataclass(frozen=True)
class ManagedProcessScan:
    """Result of a complete managed-miner replacement scan.

    ``definitely_absent`` is intentionally narrower than "no exact
    replacement": it means the process table was read completely and no live
    process even used the expected executable name.  Callers may use that
    proof for an explicit stop/remove request, while start/restart operations
    must continue to fail closed.
    """

    status: str
    rebound: tuple[dict[str, Any], Any] | None = None
    reason: str = ""
    explicit_candidate: ManagedProcessCandidate | None = None


@dataclass(frozen=True)
class ManagedProcessCandidateMember:
    """One immutable member of an explicit cleanup process snapshot."""

    process: Any
    pid: int
    create_time: float
    argv_sha256: str
    ppid: int | None = None
    sid: int | None = None
    pgid: int | None = None


@dataclass(frozen=True)
class ManagedProcessCandidate:
    """One snapshot-bound process or trusted process group for manual cleanup.

    The candidate is never sufficient for automatic adoption or termination.
    Callers must bind it to an authenticated scan receipt and reverify it
    immediately before an operator-authorized stop.
    """

    process: Any
    pid: int
    create_time: float
    argv_sha256: str
    members: tuple[ManagedProcessCandidateMember, ...] = ()


def _native_process_group_identity(
    process: Any,
    pid: int,
) -> tuple[int, int, int] | None:
    """Return Linux parent/session/group identity, or ``None`` if unavailable."""

    if os.name == "nt" or not hasattr(os, "getsid") or not hasattr(os, "getpgid"):
        return None
    try:
        ppid = int(process.ppid())
        sid = int(os.getsid(pid))
        pgid = int(os.getpgid(pid))
    except (psutil.Error, OSError, TypeError, ValueError, OverflowError, AttributeError):
        return None
    if ppid < 0 or sid <= 0 or pgid <= 0:
        return None
    return ppid, sid, pgid


def _candidate_members(
    candidate: ManagedProcessCandidate,
) -> tuple[ManagedProcessCandidateMember, ...]:
    if candidate.members:
        return candidate.members
    return (
        ManagedProcessCandidateMember(
            process=candidate.process,
            pid=candidate.pid,
            create_time=candidate.create_time,
            argv_sha256=candidate.argv_sha256,
        ),
    )


def _candidate_group_is_valid(
    members: Sequence[ManagedProcessCandidateMember],
) -> bool:
    if not members:
        return False
    if len(members) == 1:
        return True
    if any(
        member.ppid is None or member.sid is None or member.pgid is None
        for member in members
    ):
        return False
    leaders = [
        member
        for member in members
        if member.pid == member.sid == member.pgid
    ]
    if len(leaders) != 1:
        return False
    leader = leaders[0]
    if any(
        member.sid != leader.pid or member.pgid != leader.pid
        for member in members
    ):
        return False
    by_pid = {member.pid: member for member in members}
    if len(by_pid) != len(members):
        return False
    for member in members:
        if member.pid == leader.pid:
            continue
        current = member
        visited = {member.pid}
        while current.ppid != leader.pid:
            parent = by_pid.get(int(current.ppid or -1))
            if parent is None or parent.pid in visited:
                return False
            visited.add(parent.pid)
            current = parent
    return True


def managed_process_candidate_is_current(
    candidate: ManagedProcessCandidate,
    *,
    recorded_path: Path,
    create_time_tolerance: float = 0.5,
) -> bool:
    """Reverify an explicit-cleanup candidate without relaxing identity.

    Unlike :func:`managed_process_argv_matches_record`, this compares the
    candidate's observed argv fingerprint, not the stale record fingerprint.
    That lets an operator stop one explicitly selected process while still
    preventing a PID-reused, re-execed or moved process from being touched.
    """

    current_members: list[ManagedProcessCandidateMember] = []
    for member in _candidate_members(candidate):
        process = member.process
        try:
            pid = int(process.pid)
            live = process.is_running() and process.status() != psutil.STATUS_ZOMBIE
            executable = process.exe() if live else None
            created = float(process.create_time()) if live else math.nan
            argv = process.cmdline() if live else None
        except (
            psutil.Error,
            OSError,
            TypeError,
            ValueError,
            OverflowError,
            AttributeError,
        ):
            return False
        if (
            not live
            or pid != member.pid
            or not isinstance(executable, str)
            or _normalized_path(executable) != _normalized_path(recorded_path)
            or not math.isfinite(created)
            or abs(created - member.create_time) > create_time_tolerance
            or not isinstance(argv, list)
        ):
            return False
        try:
            observed_fingerprint = managed_argv_fingerprint(
                argv,
                executable=recorded_path,
            )
        except ValueError:
            return False
        if observed_fingerprint != member.argv_sha256:
            return False
        group_identity = _native_process_group_identity(process, pid)
        if member.sid is not None:
            if group_identity != (member.ppid, member.sid, member.pgid):
                return False
        current_members.append(
            ManagedProcessCandidateMember(
                process=process,
                pid=pid,
                create_time=created,
                argv_sha256=observed_fingerprint,
                ppid=(group_identity[0] if group_identity else None),
                sid=(group_identity[1] if group_identity else None),
                pgid=(group_identity[2] if group_identity else None),
            )
        )
    return _candidate_group_is_valid(current_members)


def managed_argv_fingerprint(
    argv: Sequence[str], *, executable: str | os.PathLike[str] | None = None
) -> str:
    """Hash an exact miner argv while normalizing only its executable path.

    The digest lets the Agent recognize an SRBMiner watchdog replacement
    without writing wallet/password arguments to its process record.  All
    non-executable arguments remain byte-for-byte significant, including the
    loopback API port, so another invocation of the same binary cannot be
    adopted accidentally.
    """

    if not argv or len(argv) > 256:
        raise ValueError("managed process argv is invalid")
    if any(not isinstance(item, str) or len(item) > 16_384 for item in argv):
        raise ValueError("managed process argv is invalid")
    # ``psutil.cmdline()[0]`` may be relative even though ``Process.exe()`` is
    # the exact file.  Callers that already proved the executable supply it so
    # harmless path spelling changes do not defeat an otherwise exact match.
    executable_value = os.fspath(executable) if executable is not None else argv[0]
    normalized = [os.path.normcase(os.path.realpath(executable_value)), *argv[1:]]
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def managed_process_argv_matches_record(
    process: Any,
    *,
    record: Mapping[str, Any],
    recorded_path: Path,
) -> bool:
    """Verify the live argv for a fingerprinted process record.

    Version-one records predate the non-secret argv fingerprint and retain
    their legacy path/create-time identity contract.  Version-two native and
    version-three WSL and version-seven Seine records must reproduce the complete launch fingerprint
    even when the recorded PID, executable and creation time still match; this
    prevents a same-executable re-exec or PID reuse from being accepted as the
    controller-owned miner.

    Pearl Miner's Linux builds deliberately erase their argument storage after
    startup, leaving argv[0] followed by NUL padding.  Depending on psutil and
    the original argc, that padding is exposed either as no further entries or
    as one or more empty strings.  Accept only that narrow post-start shape for
    the already-recorded process.  Callers must prove PID,
    create-time and exact executable path before calling this helper.  The
    replacement scanner intentionally does not use this exception, so a
    missing/replaced PID can never be rebound merely because another
    Pearl Miner process has the same executable name.

    Process inspection errors intentionally propagate so callers can preserve
    their existing distinction between an identity mismatch and an
    uninspectable process.
    """

    # HiveOS is a cgroup/supervisor session, never a native executable that
    # may be adopted merely because its shell argv happens to match.
    if record.get("miner_type") == "hiveos" or record.get("version") == 6:
        return False
    version = record.get("version")
    if version not in {2, 3, 7}:
        return True
    expected = record.get("argv_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    argv = process.cmdline()
    if not isinstance(argv, list):
        return False
    try:
        actual = managed_argv_fingerprint(argv, executable=recorded_path)
    except ValueError:
        return False
    if actual == expected:
        return True
    if (
        version == 2
        and record.get("miner_type") == "pearlminer"
        and recorded_path.name in {"miner", "miner-cuda12", "miner-cuda13"}
        and 1 <= len(argv) <= 256
        and isinstance(argv[0], str)
        and argv[0]
        and Path(argv[0]).name.casefold() == recorded_path.name.casefold()
        and all(isinstance(item, str) and item == "" for item in argv[1:])
    ):
        return True
    return False




def _read_process_record(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if (
            _link_or_reparse(info)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > 64 * 1024
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _replace_process_record(
    path: Path,
    expected: Mapping[str, Any],
    updated: Mapping[str, Any],
) -> bool:
    """Atomically rebind only while the original Agent record is unchanged."""

    if _read_process_record(path) != dict(expected):
        return False
    temporary_path: Path | None = None
    payload = json.dumps(
        dict(updated),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        if _read_process_record(path) != dict(expected):
            return False
        os.replace(temporary_path, path)
        temporary_path = None
        return True
    except OSError:
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _scan_managed_process_replacement_locked(
    *,
    state_path: Path,
    record: Mapping[str, Any],
    recorded_path: Path,
    process_iter_factory: Callable[[], Any],
    create_time_tolerance: float = 0.5,
    rebind_record: bool = True,
) -> ManagedProcessScan:
    """Classify and optionally rebind a managed watchdog replacement.

    The executable was already validated by the caller.  A replacement
    candidate must not be provably older, must use that exact executable, and
    must reproduce the complete non-secret argv fingerprint saved at launch.
    A different watchdog PID may appear in the same creation-time bucket as
    its parent.  The original PID itself is accepted only for a bounded clock
    representation drift; a much later same-PID process is PID reuse, not a
    replacement. ``definitely_absent`` is returned only after a complete scan found
    no live process with the expected executable name.  Every inspection
    error and every same-name non-exact process is classified as
    ``ambiguous``.
    """

    if record.get("miner_type") == "hiveos" or record.get("version") == 6:
        return ManagedProcessScan(
            "ambiguous", reason="HiveOS sessions require supervisor identity verification"
        )
    fingerprinted_record = record.get("version") in {2, 3, 7}
    expected_fingerprint = record.get("argv_sha256")
    original_pid = record.get("pid")
    original_created = record.get("create_time")
    try:
        valid_created = (
            not isinstance(original_created, bool)
            and isinstance(original_created, (int, float))
            and math.isfinite(float(original_created))
        )
    except OverflowError:
        valid_created = False
    if (
        isinstance(original_pid, bool)
        or not isinstance(original_pid, int)
        or not valid_created
    ):
        return ManagedProcessScan("ambiguous", reason="invalid process record")
    if fingerprinted_record and (
        not isinstance(expected_fingerprint, str)
        or len(expected_fingerprint) != 64
    ):
        return ManagedProcessScan("ambiguous", reason="invalid argv fingerprint")
    expected_name = recorded_path.name.casefold()
    candidates: list[tuple[Any, int, float]] = []
    explicit_members: list[ManagedProcessCandidateMember] = []
    same_name_seen = False
    live_same_name_count = 0
    ineligible_exact_seen = False
    try:
        for position, process in enumerate(process_iter_factory()):
            if position >= 8192:
                return ManagedProcessScan(
                    "ambiguous", reason="process table scan limit exceeded"
                )
            try:
                pid = int(process.pid)
            except (TypeError, ValueError, AttributeError):
                return ManagedProcessScan(
                    "ambiguous", reason="process identity cannot be read"
                )
            try:
                process_name = process.name()
            except (psutil.Error, OSError, AttributeError):
                # Without a name we cannot prove that the inaccessible process
                # is unrelated to the managed executable.
                return ManagedProcessScan(
                    "ambiguous", reason="process name cannot be read"
                )
            if not isinstance(process_name, str) or process_name.casefold() != expected_name:
                continue
            try:
                # A zombie (or another already-dead table entry) cannot be a
                # running duplicate and must not prevent proving absence or
                # rebinding the one live exact watchdog replacement.  Inspect
                # liveness before recording that a same-name process exists;
                # an inaccessible live candidate still fails closed below.
                if (
                    not process.is_running()
                    or process.status() == psutil.STATUS_ZOMBIE
                ):
                    continue
            except (psutil.Error, OSError, AttributeError):
                return ManagedProcessScan(
                    "ambiguous", reason="same-name process cannot be inspected"
                )
            same_name_seen = True
            live_same_name_count += 1
            if not fingerprinted_record:
                continue
            try:
                executable = process.exe()
                created = float(process.create_time())
                argv = process.cmdline()
            except (psutil.Error, OSError, TypeError, ValueError, OverflowError, AttributeError):
                return ManagedProcessScan(
                    "ambiguous", reason="same-name process cannot be inspected"
                )
            if (
                not isinstance(executable, str)
                or _normalized_path(executable) != _normalized_path(recorded_path)
                or not math.isfinite(created)
                or not isinstance(argv, list)
            ):
                continue
            try:
                fingerprint = managed_argv_fingerprint(
                    argv, executable=recorded_path
                )
            except ValueError:
                return ManagedProcessScan(
                    "ambiguous", reason="same-name process argv is invalid"
                )
            automatic_eligible_time = bool(
                (
                    pid == original_pid
                    and abs(created - float(original_created))
                    <= create_time_tolerance
                )
                or (
                    pid != original_pid
                    and created + create_time_tolerance
                    >= float(original_created)
                )
            )
            if fingerprint == expected_fingerprint:
                if not automatic_eligible_time:
                    ineligible_exact_seen = True
                else:
                    candidates.append((process, pid, created))
            # Manual cleanup is intentionally bound to the *current* process
            # snapshot rather than the stale record's creation time.  A PID
            # may have been reused, or a watchdog may have relaunched the
            # miner with different arguments; neither is safe for automatic
            # adoption.  It is nevertheless safe to offer the process for an
            # explicit operator choice after its exact trusted executable
            # path was proved above.  The cleanup command carries a digest of
            # this PID/create-time/argv/group snapshot and reverifies every
            # field immediately before termination, so a later PID reuse or
            # re-exec remains fail-closed.
            group_identity = _native_process_group_identity(process, pid)
            explicit_members.append(
                ManagedProcessCandidateMember(
                    process=process,
                    pid=pid,
                    create_time=created,
                    argv_sha256=fingerprint,
                    ppid=(group_identity[0] if group_identity else None),
                    sid=(group_identity[1] if group_identity else None),
                    pgid=(group_identity[2] if group_identity else None),
                )
            )
    except (psutil.Error, OSError, TypeError, ValueError):
        return ManagedProcessScan(
            "ambiguous", reason="process table cannot be inspected"
        )
    # Automatic adoption remains limited to one exact invocation and no other
    # live same-name process.  A multi-process miner must be selected
    # explicitly as one fully verified process group.
    if (
        len(candidates) != 1
        or live_same_name_count != 1
        or ineligible_exact_seen
    ):
        if same_name_seen:
            explicit_candidate = None
            # Every live same-name process must have the exact trusted path.
            # A single process needs no native group metadata.  Multiple
            # processes are offered only
            # when one unique leader owns the shared SID/PGID and every other
            # member has an in-set ancestry chain back to that leader.
            if (
                live_same_name_count == len(explicit_members)
                and _candidate_group_is_valid(explicit_members)
            ):
                if len(explicit_members) == 1:
                    leader = explicit_members[0]
                else:
                    leader = next(
                        member
                        for member in explicit_members
                        if member.pid == member.sid == member.pgid
                    )
                ordered_members = tuple(
                    sorted(explicit_members, key=lambda member: member.pid)
                )
                candidate = ManagedProcessCandidate(
                    process=leader.process,
                    pid=leader.pid,
                    create_time=leader.create_time,
                    argv_sha256=leader.argv_sha256,
                    members=ordered_members,
                )
                if not managed_process_candidate_is_current(
                    candidate,
                    recorded_path=recorded_path,
                    create_time_tolerance=create_time_tolerance,
                ):
                    return ManagedProcessScan(
                        "ambiguous",
                        reason="explicit cleanup candidate changed during verification",
                    )
                explicit_candidate = candidate
            return ManagedProcessScan(
                "ambiguous",
                reason=(
                    "multiple exact invocations found"
                    if candidates and ineligible_exact_seen
                    else "same-name process is not an exact replacement"
                ),
                explicit_candidate=explicit_candidate,
            )
        return ManagedProcessScan(
            "definitely_absent",
            reason="complete process scan found no same-name miner",
        )
    process, pid, created = candidates[0]
    try:
        if (
            not process.is_running()
            or process.status() == psutil.STATUS_ZOMBIE
            or _normalized_path(process.exe()) != _normalized_path(recorded_path)
            or abs(float(process.create_time()) - created) > create_time_tolerance
            or managed_argv_fingerprint(
                process.cmdline(), executable=recorded_path
            )
            != expected_fingerprint
        ):
            return ManagedProcessScan(
                "ambiguous", reason="replacement changed during verification"
            )
        started_at = datetime.fromtimestamp(created, timezone.utc).isoformat(
            timespec="seconds"
        )
    except (psutil.Error, OSError, TypeError, ValueError, OverflowError, AttributeError):
        return ManagedProcessScan(
            "ambiguous", reason="replacement cannot be reverified"
        )
    updated = {
        **dict(record),
        "pid": pid,
        "create_time": created,
        "started_at": started_at,
    }
    if not rebind_record:
        return ManagedProcessScan(
            "exact_replacement",
            rebound=(updated, process),
            reason="one exact replacement was verified",
        )
    if not _replace_process_record(state_path, record, updated):
        return ManagedProcessScan(
            "ambiguous", reason="process record changed during rebind"
        )
    return ManagedProcessScan("rebound", rebound=(updated, process))


def scan_managed_process_replacement(
    *,
    state_path: Path,
    record: Mapping[str, Any],
    recorded_path: Path,
    process_iter_factory: Callable[[], Any],
    create_time_tolerance: float = 0.5,
) -> ManagedProcessScan:
    """Serialize process-record rebinding with orphan cleanup in this Agent."""

    with _PROCESS_RECORD_LOCK:
        return _scan_managed_process_replacement_locked(
            state_path=state_path,
            record=record,
            recorded_path=recorded_path,
            process_iter_factory=process_iter_factory,
            create_time_tolerance=create_time_tolerance,
            rebind_record=True,
        )


def inspect_managed_process_replacement(
    *,
    state_path: Path,
    record: Mapping[str, Any],
    recorded_path: Path,
    process_iter_factory: Callable[[], Any],
    create_time_tolerance: float = 0.5,
) -> ManagedProcessScan:
    """Classify a replacement without changing the process record.

    ``status == "exact_replacement"`` carries the fully reverified candidate
    in ``rebound`` for an immediate, separately authorized cleanup operation.
    The name of that legacy field is retained to keep the public scan result
    backward compatible; this function itself never writes, deletes, or
    terminates anything.
    """

    with _PROCESS_RECORD_LOCK:
        return _scan_managed_process_replacement_locked(
            state_path=state_path,
            record=record,
            recorded_path=recorded_path,
            process_iter_factory=process_iter_factory,
            create_time_tolerance=create_time_tolerance,
            rebind_record=False,
        )


def remove_managed_process_record_if_unchanged(
    state_path: Path,
    expected: Mapping[str, Any],
) -> bool:
    """Remove an orphan record only if no concurrent rebind replaced it."""

    with _PROCESS_RECORD_LOCK:
        current = _read_process_record(state_path)
        expected_value = dict(expected)
        # Legacy v1 records omitted this field; readers materialize its
        # conservative default without rewriting the file.
        if (
            current is not None
            and "survives_agent_restart" not in current
            and expected_value.get("survives_agent_restart") is False
        ):
            expected_value.pop("survives_agent_restart", None)
        if current != expected_value:
            return False
        try:
            state_path.unlink()
        except (FileNotFoundError, OSError):
            return False
        return True


def securely_rebind_managed_process(
    *,
    state_path: Path,
    record: Mapping[str, Any],
    recorded_path: Path,
    process_iter_factory: Callable[[], Any],
    create_time_tolerance: float = 0.5,
) -> tuple[dict[str, Any], Any] | None:
    """Rebind one provable watchdog replacement, otherwise fail closed."""

    result = scan_managed_process_replacement(
        state_path=state_path,
        record=record,
        recorded_path=recorded_path,
        process_iter_factory=process_iter_factory,
        create_time_tolerance=create_time_tolerance,
    )
    return result.rebound if result.status == "rebound" else None
