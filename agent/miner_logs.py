"""Bounded, on-demand collection of logs from the locally managed miner.

The controller deliberately cannot select a path or a time range.  Only the
two fixed files written by :class:`ManagedMinerManager` are considered, and
they must still be plain files directly inside the configured managed runtime
directory.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from server.miner_adapters.retired import retired_miner_type

from .config import AgentConfig
from .fsutil import is_link_or_reparse as _link_or_reparse
from .miner_packages import _retired_managed_package_type
from .managed_miners import (
    MANAGED_MINER_TYPES,
    MINER_ADAPTERS,
    ManagedMinerError,
    _managed_role_runtime,
    _wsl_identity_from_record,
)
from .process_identity import (
    managed_process_argv_matches_record,
    securely_rebind_managed_process,
)
from .wsl_mining import (
    WslMiningError,
    query_wsl_guest,
    wsl_cached_executable_matches,
    wsl_executable_path,
)


WINDOW_SECONDS = 60
MAX_LINES = 500
# Keep enough headroom for JSON keys/escaping while bounding returned log text.
MAX_TEXT_CHARS = 60 * 1024
MAX_RESULT_BYTES = 64 * 1024
MAX_TAIL_BYTES = 256 * 1024
MAX_STATE_BYTES = 32 * 1024
_MINER_TYPES = MANAGED_MINER_TYPES
_EXPECTED_EXECUTABLES = {
    adapter.legacy_miner_type: frozenset(
        name
        for platform in adapter.platforms
        for name in platform.executable_candidates
    )
    for adapter in MINER_ADAPTERS.list()
}
if frozenset(_EXPECTED_EXECUTABLES) != _MINER_TYPES:
    raise RuntimeError("managed miner log executable allowlist is incomplete")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TIMESTAMP = re.compile(
    r"(?<!\d)(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)(?!\d)"
)
_TIME_ONLY = re.compile(
    r"(?<!\d)(\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)(?!\d)"
)
# Empty usernames are valid; redact through the final @ in the authority.
_URL_USERINFO = re.compile(
    r"(?i)\b(https?|stratum(?:\+tcp|\+ssl)?|socks5)://[^\s/:?#]*:[^\s/?#]*@"
)
_AUTH_CREDENTIAL = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
# Preserve escaped quotes as part of the secret and hide an unterminated
# quoted value through end-of-line when a miner emits a partial log record.
_QUOTED_SECRET = r'''(?:"(?:\\(?:.|$)|[^"\\])*(?:"|$)|'(?:\\(?:.|$)|[^'\\])*(?:'|$))'''
_SECRET_PAIR = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|auth[_-]?token|"
    r'''password|passwd|secret|jwt|token)\b(["']?\s*[:=]\s*)'''
    + "(?:" + _QUOTED_SECRET + r"|[^\s,;&]+)"
)
_CLI_SECRET = re.compile(
    r"(?i)(?<!\S)(--(?:access[_-]?token|api[_-]?key|authorization|"
    r"auth[_-]?token|pool[_-]?password|password|passwd|pass|secret|jwt|token)"
    r"(?:\s+|=)|-p(?:\s+|=))"
    + "(?:" + _QUOTED_SECRET + r"|[^\s]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|auth[_-]?token|password|"
    r"passwd|secret|jwt|token)=)([^&#\s]+)"
)
# Quantus uses the wallet argument as a node authentication token. Log output
# may omit quotes around opaque tokens, so keep no remainder after that label.
_WALLET_CLI = re.compile(
    r"(?i)(?<!\S)((?:--wallet(?:[_-]?address)?|-u)(?:\s+|=))[^\r\n]*"
)
_WALLET_PAIR = re.compile(
    r'''(?i)\b(wallet(?:[_-]?address)?)\b(["']?\s*[:=]\s*)[^\r\n]*'''
)





def _plain_runtime(config: AgentConfig) -> Path | None:
    path = config.managed_miner_runtime_dir
    if path is None:
        return None
    try:
        info = path.lstat()
        if _link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            return None
        resolved = path.resolve(strict=True)
        resolved_info = resolved.lstat()
        if _link_or_reparse(resolved_info) or not stat.S_ISDIR(resolved_info.st_mode):
            return None
        return resolved
    except OSError:
        return None


def _read_small_json(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
        if (
            _link_or_reparse(info)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_STATE_BYTES
        ):
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _valid_process_record(runtime: Path) -> dict[str, Any] | None:
    value = _read_small_json(runtime / "managed-miner-process.json")
    if value is None:
        return None
    if value.get("miner_type") == "hiveos" or value.get("version") == 6:
        # HiveOS sessions were removed; their records carry no native identity.
        return None
    legacy = {
        "version",
        "pid",
        "executable",
        "create_time",
        "miner_type",
        "flight_sheet_id",
        "started_at",
    }
    version_one = legacy | {"survives_agent_restart"}
    version_two = version_one | {"argv_sha256", "api_port"}
    version_three = version_two | {"execution_mode", "target_executable"}
    version_four = version_three | {
        "wsl_token",
        "wsl_guest_executable",
        "wsl_identity",
    }
    version_five = version_four | {"wsl_cache_key"}
    fields = frozenset(value)
    version = value.get("version")
    if not (
        (version == 1 and fields in {frozenset(legacy), frozenset(version_one)})
        or (version == 2 and fields == frozenset(version_two))
        or (version == 3 and fields == frozenset(version_three))
        or (version == 4 and fields == frozenset(version_four))
        or (version == 5 and fields == frozenset(version_five))
    ):
        return None
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
        or not isinstance(value.get("miner_type"), str)
        or value["miner_type"] not in _MINER_TYPES
        or not isinstance(value.get("executable"), str)
        or not value["executable"]
        or not finite_created
        or not isinstance(value.get("flight_sheet_id"), str)
        or not isinstance(value.get("started_at"), str)
        or (
            version in {3, 4, 5}
            and (
                value.get("execution_mode") != "wsl"
                or not isinstance(value.get("target_executable"), str)
                or not value["target_executable"]
                or len(value["target_executable"]) > 4096
                or not Path(value["target_executable"]).is_absolute()
            )
        )
    ):
        return None
    restart_safe = value.get("survives_agent_restart", False)
    if not isinstance(restart_safe, bool):
        return None
    if version in {2, 3, 4, 5} and (
        not isinstance(value.get("argv_sha256"), str)
        or len(value["argv_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in value["argv_sha256"])
        or isinstance(value.get("api_port"), bool)
        or not isinstance(value.get("api_port"), int)
        or not 1024 <= value["api_port"] <= 65535
    ):
        return None
    if version == 5 and (
        not isinstance(value.get("wsl_cache_key"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["wsl_cache_key"]) is None
        or not isinstance(value.get("wsl_guest_executable"), str)
        or not wsl_cached_executable_matches(
            value["wsl_guest_executable"], value["wsl_cache_key"]
        )
    ):
        return None
    return value


def _status_miner_type(config: AgentConfig, runtime: Path) -> str | None:
    status_path = config.miner_status_file or (runtime / "miner-status.json")
    value = _read_small_json(status_path)
    miner_type = value.get("miner_type") if value else None
    return miner_type if isinstance(miner_type, str) and miner_type in _MINER_TYPES else None


def _is_running(
    record: dict[str, Any] | None,
    config: AgentConfig,
    runtime: Path,
    process_factory: Callable[[int], Any],
    process_iter_factory: Callable[[], Any],
) -> bool:
    if record is None:
        return False
    if record.get("miner_type") == "hiveos" or record.get("version") == 6:
        # A legacy HiveOS record cannot be verified without its runtime.
        return False
    if config.miner_packages_dir is None:
        return False
    try:
        packages = config.miner_packages_dir.resolve(strict=True)
        wsl_record = record.get("version") in {3, 4, 5}
        if wsl_record:
            expected_linux_names = {
                name
                for name in _EXPECTED_EXECUTABLES[record["miner_type"]]
                if not name.casefold().endswith(".exe")
            }
            raw_target = Path(record["target_executable"])
            if raw_target.name not in expected_linux_names:
                return False
            trusted_wrapper = wsl_executable_path()
            recorded = Path(record["executable"]).resolve(strict=True)
            if os.path.normcase(
                os.path.abspath(record["executable"])
            ) != os.path.normcase(os.path.abspath(trusted_wrapper)):
                return False
            if record.get("version") in {4, 5}:
                try:
                    return query_wsl_guest(
                        recorded,
                        str(record["wsl_token"]),
                        str(record["wsl_guest_executable"]),
                        _wsl_identity_from_record(record),
                    )
                except (KeyError, ManagedMinerError, WslMiningError):
                    return False
            # Supervised v4/v5 records returned above. Legacy v3 still
            # requires its real source target inside the package directory.
            target_info = raw_target.lstat()
            target = raw_target.resolve(strict=True)
            target.relative_to(packages)
            if _link_or_reparse(target_info) or not stat.S_ISREG(target_info.st_mode):
                return False
        else:
            recorded = Path(record["executable"]).resolve(strict=True)
            recorded.relative_to(packages)
            info = recorded.lstat()
            if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                return False
            if recorded.name not in _EXPECTED_EXECUTABLES[record["miner_type"]]:
                return False

        def try_rebind() -> bool:
            # A different wsl.exe PID could be an unrelated distro command.
            # Version-three records therefore fail closed instead of scanning
            # or rebinding host wrapper processes.
            if wsl_record:
                return False
            return securely_rebind_managed_process(
                state_path=runtime / "managed-miner-process.json",
                record=record,
                recorded_path=recorded,
                process_iter_factory=process_iter_factory,
                create_time_tolerance=0.5,
            ) is not None

        try:
            process = process_factory(record["pid"])
        except psutil.NoSuchProcess:
            return try_rebind()
        except psutil.Error:
            return False
        try:
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return try_rebind()
            if abs(float(process.create_time()) - float(record["create_time"])) > 0.5:
                return try_rebind()
            actual = Path(process.exe()).resolve(strict=True)
            if os.path.normcase(str(actual)) != os.path.normcase(str(recorded)):
                return try_rebind()
            if not managed_process_argv_matches_record(
                process,
                record=record,
                recorded_path=recorded,
            ):
                return try_rebind()
            return True
        except psutil.NoSuchProcess:
            # SRBMiner can replace its watchdog process between individual
            # psutil calls.  Re-prove the replacement instead of reporting a
            # false stopped state merely because this PID disappeared.
            return try_rebind()
        except (OSError, TypeError, ValueError, AttributeError, psutil.Error):
            return False
    except (OSError, ValueError, KeyError, WslMiningError):
        return False


def _parse_timestamp(
    line: str, local_timezone: Any, captured_at: datetime
) -> datetime | None:
    match = _TIMESTAMP.search(line)
    if match:
        raw = match.group(1).replace(",", ".")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        # Python requires a colon in a numeric UTC offset.
        if re.search(r"[+-]\d{4}$", raw):
            raw = raw[:-2] + ":" + raw[-2:]
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=local_timezone)
            return parsed.astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return None

    # SRBMiner versions commonly emit only a local wall-clock time.  Anchor it
    # to the capture date, with a midnight rollover correction.
    match = _TIME_ONLY.search(line)
    if not match:
        return None
    try:
        parsed_time = datetime.strptime(
            match.group(1).replace(",", "."),
            "%H:%M:%S.%f" if "." in match.group(1).replace(",", ".") else "%H:%M:%S",
        ).time()
    except ValueError:
        return None
    local_capture = captured_at.astimezone(local_timezone)
    candidate = datetime.combine(
        local_capture.date(), parsed_time, tzinfo=local_timezone
    )
    if candidate > local_capture + timedelta(minutes=5):
        candidate -= timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _redact(line: str) -> str:
    line = _ANSI_ESCAPE.sub("", line)
    line = _CONTROL.sub("", line)
    line = _URL_USERINFO.sub(lambda match: match.group(1) + "://[REDACTED]@", line)
    line = _AUTH_CREDENTIAL.sub(lambda match: match.group(1) + " [REDACTED]", line)
    line = _WALLET_CLI.sub(lambda match: match.group(1) + "[REDACTED]", line)
    line = _WALLET_PAIR.sub(
        lambda match: match.group(1) + match.group(2) + "[REDACTED]", line
    )
    line = _CLI_SECRET.sub(lambda match: match.group(1) + "[REDACTED]", line)
    line = _SECRET_PAIR.sub(
        lambda match: match.group(1) + match.group(2) + "[REDACTED]", line
    )
    line = _QUERY_SECRET.sub(lambda match: match.group(1) + "[REDACTED]", line)
    return line


def _safe_tail(
    path: Path, runtime: Path,
) -> tuple[list[str], float, bool] | None:
    """Read a bounded tail of a runtime-owned regular log file."""

    try:
        info = path.lstat()
        if _link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
            return None
        resolved = path.resolve(strict=True)
        if resolved.parent != runtime:
            return None
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(path, flags)
        try:
            opened_info = os.fstat(descriptor)
            if not stat.S_ISREG(opened_info.st_mode):
                return None
            # Detect a replacement between lstat and open where the platform
            # exposes stable device/inode identifiers.
            if (
                getattr(info, "st_ino", 0)
                and getattr(opened_info, "st_ino", 0)
                and (info.st_dev, info.st_ino) != (opened_info.st_dev, opened_info.st_ino)
            ):
                return None
            size = int(opened_info.st_size)
            start = max(0, size - MAX_TAIL_BYTES)
            os.lseek(descriptor, start, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = min(size, MAX_TAIL_BYTES)
            while remaining:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        return None
    raw = b"".join(chunks)
    truncated = start > 0
    if start > 0:
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline >= 0 else b""
    return raw.decode("utf-8", errors="replace").splitlines(), info.st_mtime, truncated


def _recent_lines(
    lines: list[str],
    *,
    modified_at: float,
    now: datetime,
) -> list[str]:
    cutoff = now - timedelta(seconds=WINDOW_SECONDS)
    future_limit = now + timedelta(minutes=5)
    local_timezone = datetime.now().astimezone().tzinfo or timezone.utc
    timestamps = [_parse_timestamp(line, local_timezone, now) for line in lines]
    has_timestamp = any(item is not None for item in timestamps)
    file_is_recent = modified_at >= cutoff.timestamp()

    if not has_timestamp:
        return [_redact(line) for line in lines] if file_is_recent else []

    selected: list[str] = []
    accepting_continuation = False
    for line, timestamp in zip(lines, timestamps):
        if timestamp is not None:
            accepting_continuation = cutoff <= timestamp <= future_limit
            if accepting_continuation:
                selected.append(_redact(line))
        elif accepting_continuation:
            selected.append(_redact(line))

    # Some miners prepend timestamps only intermittently.  If the file was
    # just modified, preserve the final untimestamped continuation block.
    if file_is_recent and not selected:
        trailing: list[str] = []
        for line, timestamp in reversed(list(zip(lines, timestamps))):
            if timestamp is not None:
                break
            trailing.append(_redact(line))
        selected.extend(reversed(trailing))
    return selected


def _trim_log_result(result: dict[str, Any]) -> dict[str, Any]:
    """Bound the escaped JSON result by trimming trailing log lines."""

    entries = result["logs"]
    while (
        len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        > MAX_RESULT_BYTES
        and entries
    ):
        final = entries[-1]
        lines = final.get("lines")
        if isinstance(lines, list) and lines:
            lines.pop()
            final["truncated"] = True
        if not lines:
            entries.pop()
    return result


def _retired_log_identity(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    identities = [value.get("miner_type"), value.get("adapter_id"), value.get("miner_profile")]
    package = value.get("miner_package")
    if isinstance(package, dict):
        managed_type = _retired_managed_package_type(package.get("id"))
        if managed_type is not None:
            identities.append(managed_type)
    return next(
        (miner_type for identity in identities
         if (miner_type := retired_miner_type(identity)) is not None),
        None,
    )


def collect_managed_miner_logs(
    config: AgentConfig,
    *,
    now: datetime | None = None,
    process_factory: Callable[[int], Any] = psutil.Process,
    process_iter_factory: Callable[[], Any] = psutil.process_iter,
) -> dict[str, Any]:
    """Return a small recent-log snapshot without accepting remote selectors."""

    captured = now or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)
    result: dict[str, Any] = {
        "captured_at": captured.isoformat(timespec="seconds"),
        "window_seconds": WINDOW_SECONDS,
        "miner_type": None,
        "running": False,
        "logs": [],
    }
    runtime = _plain_runtime(config)
    if runtime is None:
        return result

    manifest = _read_small_json(runtime / "independent-miners.json")
    manifest_independent = (
        isinstance(manifest, dict)
        and manifest.get("version") == 1
        and manifest.get("mode") == "independent"
        and manifest.get("roles") == ["primary", "secondary"]
    )
    active_sheet = (
        _read_small_json(config.flight_sheet_file)
        if config.flight_sheet_file is not None
        else None
    )
    saved_record = _read_small_json(runtime / "managed-miner-process.json")
    saved_status = _read_small_json(config.miner_status_file or runtime / "miner-status.json")
    identities = [saved_record, saved_status]
    if isinstance(active_sheet, dict):
        identities.extend((active_sheet.get("primary"), active_sheet.get("secondary")))
    retired_type = next(
        (miner_type for value in identities
         if (miner_type := _retired_log_identity(value)) is not None),
        None,
    )
    if retired_type is not None:
        result.update(miner_type=retired_type, retired=True, error="managed miner adapter is retired")
        return result

    active_sheet_independent = (
        isinstance(active_sheet, dict)
        and active_sheet.get("version") == 4
        and active_sheet.get("secondary_mode") == "independent"
        and isinstance(active_sheet.get("secondary"), dict)
    )
    if manifest_independent or active_sheet_independent:
        combined: list[dict[str, Any]] = []
        running_roles: list[str] = []
        miner_types: list[str] = []
        role_errors: dict[str, str] = {}
        for role in ("primary", "secondary"):
            try:
                role_runtime = _managed_role_runtime(
                    runtime,
                    role,
                    create=False,
                )
            except ManagedMinerError as exc:
                role_errors[role] = str(exc)
                continue
            try:
                role_runtime.lstat()
            except OSError:
                role_errors[role] = (
                    f"managed miner {role} runtime directory is unavailable"
                )
                continue
            role_config = replace(
                config,
                managed_miner_runtime_dir=role_runtime,
                miner_status_file=role_runtime / "miner-status.json",
                flight_sheet_file=role_runtime / "active-flight-sheet.json",
            )
            snapshot = collect_managed_miner_logs(
                role_config,
                now=captured,
                process_factory=process_factory,
                process_iter_factory=process_iter_factory,
            )
            if snapshot.get("running") is True:
                running_roles.append(role)
            miner_type = snapshot.get("miner_type")
            if isinstance(miner_type, str) and miner_type in _MINER_TYPES:
                miner_types.append(miner_type)
            for entry in snapshot.get("logs") or []:
                if isinstance(entry, dict):
                    combined.append({"role": role, **entry})
        result["miner_type"] = "+".join(miner_types) if miner_types else None
        result["running"] = len(running_roles) == 2
        result["running_roles"] = running_roles
        result["role_errors"] = role_errors
        result["degraded"] = len(running_roles) == 1
        result["logs"] = combined
        return _trim_log_result(result)

    record = _valid_process_record(runtime)
    miner_type = record.get("miner_type") if record else None
    if miner_type not in _MINER_TYPES:
        miner_type = _status_miner_type(config, runtime)
    result["miner_type"] = miner_type
    result["running"] = _is_running(
        record,
        config,
        runtime,
        process_factory,
        process_iter_factory,
    )
    if miner_type not in _MINER_TYPES:
        return result

    sources = (
        ("miner", runtime / f"{miner_type}.log", runtime),
        ("launcher", runtime / "managed-miner-launcher.log", runtime),
    )
    remaining_lines = MAX_LINES
    remaining_chars = MAX_TEXT_CHARS
    entries: list[dict[str, Any]] = []
    for source, path, log_root in sources:
        tail = _safe_tail(path, log_root)
        if tail is None:
            continue
        raw_lines, modified_at, file_truncated = tail
        recent = _recent_lines(raw_lines, modified_at=modified_at, now=captured)
        accepted: list[str] = []
        limited = False
        for line in recent:
            # Bound individual records as well as the aggregate response.
            if len(line) > 4096:
                line = line[:4096] + "…"
                limited = True
            cost = len(line) + 1
            if remaining_lines <= 0 or cost > remaining_chars:
                limited = True
                break
            accepted.append(line)
            remaining_lines -= 1
            remaining_chars -= cost
        if accepted:
            entries.append(
                {
                    "source": source,
                    "lines": accepted,
                    "truncated": bool(file_truncated or limited or len(accepted) < len(recent)),
                }
            )
        if remaining_lines <= 0 or remaining_chars <= 0:
            break
    result["logs"] = entries
    # Enforce the public bound against the actual JSON representation rather
    # than characters alone (non-ASCII text can expand when JSON-escaped).
    return _trim_log_result(result)
