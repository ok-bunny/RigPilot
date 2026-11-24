"""Small, permission-restricted persistent state store."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import re
import secrets
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .fsutil import fsync_directory


LOGGER = logging.getLogger(__name__)


class StateError(RuntimeError):
    """Raised when the persisted state cannot be safely used."""


_AGENT_SECRET_RE = re.compile(r"rpa_[A-Za-z0-9_-]{48}\Z")

# The 200-entry command history may legitimately carry results up to the
# controller's 131,072-byte body budget (~26 MiB); 64 MiB bounds pathology
# with more than 2x margin.
_MAX_STATE_FILE_BYTES = 64 * 1024 * 1024


def _fsync_directory(path: Path) -> None:
    """Durably publish a replaced state file on POSIX filesystems."""

    if os.name == "nt":
        return
    fsync_directory(path, strict=True)


@dataclass
class AgentState:
    agent_id: str
    agent_secret: str
    enrolled_at: str
    heartbeat_interval: float | None = None
    command_history: dict[str, dict[str, Any]] = field(default_factory=dict)
    workload_counter_state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        agent_id: str,
        agent_secret: str,
        heartbeat_interval: float | None = None,
    ) -> "AgentState":
        return cls(
            agent_id=agent_id,
            agent_secret=agent_secret,
            enrolled_at=datetime.now(timezone.utc).isoformat(),
            heartbeat_interval=heartbeat_interval,
        )


@dataclass(frozen=True)
class PendingReplacement:
    """Private, controller-bound credentials for one identity replacement."""

    request_id: str
    proposed_agent_secret: str = field(repr=False)
    controller_url: str
    token_sha256: str = field(repr=False)
    version: int = field(default=1, init=False)


def replacement_pending_path(state_path: Path) -> Path:
    return state_path.with_name(f".{state_path.name}.replacement-pending")


def clear_pending_replacement(state_path: Path) -> None:
    """Remove the replacement journal only after the main state is durable."""
    _clear_pending(replacement_pending_path(state_path), "replacement")


@dataclass(frozen=True)
class PendingEnrollment:
    """Private, controller-bound credentials for one ordinary enrollment."""

    request_id: str
    proposed_agent_secret: str = field(repr=False)
    controller_url: str
    token_sha256: str
    metadata: dict[str, str] = field(repr=False)
    version: int = field(default=1, init=False)


_MAX_PENDING_ENROLLMENT_BYTES = 64 * 1024


def enrollment_pending_path(state_path: Path) -> Path:
    return state_path.with_name(f".{state_path.name}.enrollment-pending")


def _enrollment_controller_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.rstrip("/")
        or len(value) > 4096
        or any(ord(character) < 32 for character in value)
    ):
        raise StateError("pending enrollment controller binding is invalid")
    return value.rstrip("/")


def _enrollment_token_digest(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise StateError("pending enrollment token binding is invalid")
    try:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise StateError("pending enrollment token binding is invalid") from None


def _enrollment_metadata(value: dict[str, str]) -> dict[str, str]:
    required = {"hostname", "os_family", "platform", "agent_version"}
    if (
        not isinstance(value, dict)
        or not required <= set(value) <= required | {"name"}
        or any(not isinstance(item, str) or not item for item in value.values())
    ):
        raise StateError("pending enrollment metadata is invalid")
    try:
        if any(len(item.encode("utf-8")) > 4096 for item in value.values()):
            raise StateError("pending enrollment metadata exceeds its size limit")
        if len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > 16 * 1024:
            raise StateError("pending enrollment metadata exceeds its size limit")
    except UnicodeError:
        raise StateError("pending enrollment metadata is invalid") from None
    return dict(value)


def _enrollment_file_identity(info: Any) -> tuple[Any, ...]:
    if (
        not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not 0 < info.st_size <= _MAX_PENDING_ENROLLMENT_BYTES
        or (
            os.name != "nt"
            and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600)
        )
    ):
        raise StateError("pending enrollment file is not private and regular")
    # A fully written temporary file is hard-linked into place atomically.
    # A crash before temporary-name cleanup may legitimately leave nlink > 1.
    return tuple(
        getattr(info, name)
        for name in (
            "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size",
            "st_mtime_ns",
        )
    )


def _replacement_controller_url(value: str) -> str:
    try:
        return _enrollment_controller_url(value)
    except StateError:
        raise StateError("pending replacement controller binding is invalid") from None


def _replacement_token_digest(token: str) -> str:
    # Match the Controller's one-time replacement-code schema before creating
    # a durable nonce. A malformed interactive code must not strand one.
    if not isinstance(token, str) or re.fullmatch(r"rpr_[A-Za-z0-9_-]{43}", token) is None:
        raise StateError("replacement token must contain rpr_ and 43 URL-safe characters")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _replacement_file_identity(info: Any) -> tuple[Any, ...]:
    try:
        return _enrollment_file_identity(info)
    except StateError:
        raise StateError(
            "pending replacement file is not private and regular; manual review required"
        ) from None


def _read_pending_payload(
    path: Path, kind: str, file_identity: Callable[[Any], tuple[Any, ...]],
) -> bytes | None:
    """Read one bounded private journal while preserving every identity barrier."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise StateError(f"pending {kind} state could not be read") from None
    identity = file_identity(before)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        if file_identity(os.fstat(descriptor)) != identity:
            raise StateError(f"pending {kind} file changed while reading")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            payload = handle.read(_MAX_PENDING_ENROLLMENT_BYTES + 1)
            if (
                len(payload) != before.st_size
                or file_identity(os.fstat(handle.fileno())) != identity
                or file_identity(path.lstat()) != identity
            ):
                raise StateError(f"pending {kind} file changed while reading")
    except OSError:
        raise StateError(f"pending {kind} state could not be read") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return payload


def _publish_pending(
    path: Path, kind: str, payload: dict[str, Any],
    pending: Any, load_winner: Callable[[], Any],
) -> Any:
    """Publish fully synced bytes exclusively; preserve a concurrent winner."""
    descriptor: int | None = None
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".tmp", dir=path.parent,
        )
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pending = load_winner()
            if pending is None:
                raise StateError(f"pending {kind} publication changed concurrently")
        os.unlink(temporary)
        temporary = None
        _fsync_directory(path.parent)
    except OSError:
        raise StateError(f"pending {kind} state could not be saved durably") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return pending


def _clear_pending(path: Path, kind: str) -> None:
    try:
        path.unlink(missing_ok=True)
        if path.parent.exists():
            _fsync_directory(path.parent)
    except OSError:
        raise StateError(f"pending {kind} state could not be cleared durably") from None


def load_pending_replacement(
    state_path: Path,
    controller_url: str,
    token: str | None = None,
) -> PendingReplacement | None:
    """Read only a private journal bound to this controller and original token."""
    controller = _replacement_controller_url(controller_url)
    expected_token = _replacement_token_digest(token) if token is not None else None
    path = replacement_pending_path(state_path)
    payload = _read_pending_payload(path, "replacement", _replacement_file_identity)
    if payload is None:
        return None
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise StateError("pending replacement state is not valid JSON") from None
    if isinstance(raw, dict) and set(raw) == {"request_id", "proposed_agent_secret"}:
        # Old journals cannot prove which one-time token or Controller created
        # the nonce. Retrying or deleting one before main state exists is unsafe.
        raise StateError("legacy replacement journal is unbound; manual review required")
    if not isinstance(raw, dict) or set(raw) != {
        "version", "request_id", "proposed_agent_secret", "controller_url", "token_sha256",
    }:
        raise StateError("pending replacement state has an invalid shape")
    request_id = raw.get("request_id")
    secret = raw.get("proposed_agent_secret")
    digest = raw.get("token_sha256")
    if (
        type(raw.get("version")) is not int or raw["version"] != 1
        or not isinstance(request_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
        or not isinstance(secret, str) or _AGENT_SECRET_RE.fullmatch(secret) is None
        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or raw.get("controller_url") != controller
        or (expected_token is not None and not secrets.compare_digest(digest, expected_token))
    ):
        raise StateError("pending replacement credentials or bindings do not match")
    return PendingReplacement(request_id, secret, controller, digest)


def load_or_create_pending_replacement(
    state_path: Path, controller_url: str, token: str,
) -> PendingReplacement:
    """Durably publish one bound nonce before any replacement request is sent."""
    controller = _replacement_controller_url(controller_url)
    pending = load_pending_replacement(state_path, controller, token)
    path = replacement_pending_path(state_path)
    if pending is not None:
        try:
            _fsync_directory(path.parent)
        except OSError:
            raise StateError("pending replacement state could not be made durable") from None
        return pending
    digest = _replacement_token_digest(token)
    pending = PendingReplacement(
        uuid.uuid4().hex, "rpa_" + secrets.token_urlsafe(36), controller, digest,
    )
    return _publish_pending(
        path, "replacement",
        {
            "version": pending.version,
            "request_id": pending.request_id,
            "proposed_agent_secret": pending.proposed_agent_secret,
            "controller_url": pending.controller_url,
            "token_sha256": pending.token_sha256,
        },
        pending,
        lambda: load_pending_replacement(state_path, controller, token),
    )


def load_pending_enrollment(
    state_path: Path,
    controller_url: str,
    token: str | None = None,
) -> PendingEnrollment | None:
    """Load only matching durable credentials; never replace an unsafe journal."""
    controller = _enrollment_controller_url(controller_url)
    expected_token = _enrollment_token_digest(token) if token is not None else None
    path = enrollment_pending_path(state_path)
    payload = _read_pending_payload(path, "enrollment", _enrollment_file_identity)
    if payload is None:
        return None
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise StateError("pending enrollment state is not valid JSON") from None
    if not isinstance(raw, dict) or set(raw) != {
        "version", "request_id", "proposed_agent_secret", "controller_url", "token_sha256", "metadata",
    }:
        raise StateError("pending enrollment state has an invalid shape")
    request_id = raw.get("request_id")
    secret = raw.get("proposed_agent_secret")
    digest = raw.get("token_sha256")
    if (
        type(raw.get("version")) is not int or raw["version"] != 1
        or not isinstance(request_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
        or not isinstance(secret, str) or _AGENT_SECRET_RE.fullmatch(secret) is None
        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or raw.get("controller_url") != controller
        or (expected_token is not None and not secrets.compare_digest(digest, expected_token))
    ):
        raise StateError("pending enrollment credentials or bindings do not match")
    metadata = _enrollment_metadata(raw["metadata"])
    return PendingEnrollment(request_id, secret, controller, digest, metadata)


def load_or_create_pending_enrollment(
    state_path: Path,
    controller_url: str,
    token: str | None,
    metadata: dict[str, str],
) -> PendingEnrollment:
    """Publish a complete private journal without ever replacing another nonce.

    Hard-link publication is atomic and exclusive on supported POSIX and NTFS
    filesystems.  An unsupported filesystem fails closed; a concurrent caller
    reads the winning complete file.  Both paths fsync the directory before
    returning credentials that the caller may send to the controller.
    """
    controller = _enrollment_controller_url(controller_url)
    pending = load_pending_enrollment(state_path, controller, token)
    path = enrollment_pending_path(state_path)
    if pending is not None:
        try:
            _fsync_directory(path.parent)
        except OSError:
            raise StateError("pending enrollment state could not be made durable") from None
        return pending
    if token is None:
        raise StateError("pending enrollment is absent and requires a token")
    digest = _enrollment_token_digest(token)
    frozen_metadata = _enrollment_metadata(metadata)
    pending = PendingEnrollment(
        uuid.uuid4().hex, "rpa_" + secrets.token_urlsafe(36), controller, digest, frozen_metadata,
    )
    return _publish_pending(
        path, "enrollment",
        {
            "version": pending.version,
            "request_id": pending.request_id,
            "proposed_agent_secret": pending.proposed_agent_secret,
            "controller_url": pending.controller_url,
            "token_sha256": pending.token_sha256,
            "metadata": pending.metadata,
        },
        pending,
        lambda: load_pending_enrollment(state_path, controller, token),
    )


def clear_pending_enrollment(state_path: Path) -> None:
    """Remove only the ordinary-enrollment journal after main state is durable."""
    _clear_pending(enrollment_pending_path(state_path), "enrollment")


def _verify_main_state_file_identity(path: Path) -> None:
    """Align the main state file with the pending-journal privacy standard.

    The enrolled credentials include ``agent_secret``; a swapped or
    foreign-owned file must never be read (fail closed, like the pending
    enrollment journal).  A widened mode with the correct owner is repaired
    in place so a legitimate manual copy-restore keeps working instead of
    crash-looping the Agent; an unrepairable mode stays a hard failure.
    """

    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not 0 < info.st_size <= _MAX_STATE_FILE_BYTES
    ):
        raise StateError("state file must be a private regular local file")
    if os.name == "nt":
        return
    if info.st_uid != os.geteuid():
        raise StateError("state file is owned by another user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise StateError(
                "state file permissions are unsafe and cannot be repaired"
            ) from exc
        LOGGER.warning(
            "widened state file permissions were repaired to 0600",
            extra={"event": "state_file_mode_repaired", "path": str(path)},
        )


def load_state(path: Path) -> AgentState | None:
    if not path.exists():
        return None
    try:
        _verify_main_state_file_identity(path)
    except OSError as exc:
        raise StateError(f"could not read state file {path}: {exc}") from exc
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"could not read state file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise StateError("state file root must be a JSON object")
    agent_id = raw.get("agent_id")
    secret = raw.get("agent_secret")
    enrolled_at = raw.get("enrolled_at")
    history = raw.get("command_history", {})
    workload_counter_state = raw.get("workload_counter_state", {})
    interval = raw.get("heartbeat_interval")
    if not all(isinstance(value, str) and value for value in (agent_id, secret, enrolled_at)):
        raise StateError("state file is missing valid enrollment credentials")
    if not isinstance(history, dict):
        raise StateError("state command_history must be an object")
    if not isinstance(workload_counter_state, dict):
        raise StateError("state workload_counter_state must be an object")
    if interval is not None and (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not 0 < interval <= sys.float_info.max
    ):
        raise StateError("state heartbeat_interval must be a positive finite number")
    clean_history = {
        key: value
        for key, value in history.items()
        if isinstance(key, str) and isinstance(value, dict)
    }
    return AgentState(
        agent_id=agent_id,
        agent_secret=secret,
        enrolled_at=enrolled_at,
        heartbeat_interval=float(interval) if interval is not None else None,
        command_history=clean_history,
        workload_counter_state=workload_counter_state,
    )


def save_state(path: Path, state: AgentState, history_limit: int = 200) -> None:
    """Atomically save credentials and bounded idempotency history."""

    path.parent.mkdir(parents=True, exist_ok=True)
    history_items = sorted(
        state.command_history.items(),
        key=lambda item: str(item[1].get("completed_at", "")),
    )[-history_limit:]
    state.command_history = dict(history_items)
    payload = {
        "agent_id": state.agent_id,
        "agent_secret": state.agent_secret,
        "enrolled_at": state.enrolled_at,
        "heartbeat_interval": state.heartbeat_interval,
        "command_history": state.command_history,
        "workload_counter_state": state.workload_counter_state,
    }
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        temp_name = None
        if os.name != "nt":
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
    except OSError as exc:
        raise StateError(f"could not save state file {path}: {exc}") from exc
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
