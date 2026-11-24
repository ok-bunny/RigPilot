from __future__ import annotations

import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator


_MIN_SECRET_KEY_LENGTH = 32
_MAX_SIGNED_INT = 2**63 - 1
_INSTANCE_LOCKS_GUARD = threading.Lock()
_INSTANCE_LOCK_PATHS: set[Path] = set()
_SECRET_LOCKS_GUARD = threading.Lock()
_SECRET_LOCKS: dict[Path, threading.Lock] = {}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be one of: 1, true, yes, on, 0, false, no, off"
    )


@dataclass(slots=True)
class Settings:
    data_dir: Path
    database_path: Path
    secret_key: str
    admin_username: str
    admin_password: str | None
    session_hours: int = 12
    agent_online_seconds: int = 45
    require_https_for_agents: bool = False
    # Full heartbeat payloads are useful for the ten-minute device drawer and
    # short-term diagnosis, but are much larger than chart/workload history.
    telemetry_retention_days: int = 2
    hashrate_history_retention_days: int = 14
    command_batch_size: int = 10
    max_miner_package_bytes: int = 1_073_741_824
    miner_package_import_timeout_seconds: int = 120
    # Controller-hosted Agent releases are deliberately separate from miner
    # archives.  Keeping an independent ceiling lets operators accept large
    # miner bundles without also accepting unexpectedly large Agent uploads.
    max_agent_release_bytes: int = 536_870_912
    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self.database_path = Path(self.database_path).resolve()
        self.secret_key = _validate_secret_key(self.secret_key)
        self.admin_username = _validate_admin_username(self.admin_username)
        _validate_int_range("session_hours", self.session_hours, 1, 24 * 30)
        _validate_int_range(
            "agent_online_seconds", self.agent_online_seconds, 5, 60 * 60
        )
        _validate_int_range(
            "telemetry_retention_days", self.telemetry_retention_days, 1, 3650
        )
        _validate_int_range(
            "hashrate_history_retention_days",
            self.hashrate_history_retention_days,
            1,
            3650,
        )
        _validate_int_range("command_batch_size", self.command_batch_size, 1, 100)
        _validate_int_range(
            "max_miner_package_bytes",
            self.max_miner_package_bytes,
            1,
            16 * 1024**3,
        )
        _validate_int_range(
            "miner_package_import_timeout_seconds",
            self.miner_package_import_timeout_seconds,
            1,
            60 * 60,
        )
        _validate_int_range(
            "max_agent_release_bytes",
            self.max_agent_release_bytes,
            1,
            8 * 1024**3,
        )
    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("RIGPILOT_DATA_DIR", "data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        database_path = Path(
            os.getenv("RIGPILOT_DATABASE", str(data_dir / "rigpilot.db"))
        ).resolve()
        configured_secret = os.getenv("RIGPILOT_SECRET_KEY")
        if configured_secret is None:
            secret_key = _load_or_create_secret(data_dir / "secret.key")
        else:
            secret_key = _validate_secret_key(configured_secret)
        _reject_multi_worker_environment()
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            secret_key=secret_key,
            admin_username=os.getenv("RIGPILOT_ADMIN_USERNAME", "admin").strip(),
            admin_password=os.getenv("RIGPILOT_ADMIN_PASSWORD"),
            session_hours=int(os.getenv("RIGPILOT_SESSION_HOURS", "12")),
            agent_online_seconds=int(os.getenv("RIGPILOT_AGENT_ONLINE_SECONDS", "45")),
            require_https_for_agents=_env_bool("RIGPILOT_REQUIRE_HTTPS", False),
            telemetry_retention_days=int(
                os.getenv("RIGPILOT_TELEMETRY_RETENTION_DAYS", "2")
            ),
            hashrate_history_retention_days=int(
                os.getenv("RIGPILOT_HASHRATE_HISTORY_RETENTION_DAYS", "14")
            ),
            command_batch_size=int(os.getenv("RIGPILOT_COMMAND_BATCH_SIZE", "10")),
            max_miner_package_bytes=int(
                os.getenv("RIGPILOT_MAX_MINER_PACKAGE_BYTES", str(1_073_741_824))
            ),
            miner_package_import_timeout_seconds=int(
                os.getenv("RIGPILOT_MINER_IMPORT_TIMEOUT_SECONDS", "120")
            ),
            max_agent_release_bytes=int(
                os.getenv(
                    "RIGPILOT_MAX_AGENT_RELEASE_BYTES", str(536_870_912)
                )
            ),
        )


def _load_or_create_secret(path: Path) -> str:
    """Atomically publish one valid key, replacing corrupt persisted data."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with _secret_file_lock(path):
        try:
            return _validate_secret_key(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except ValueError:
            # Includes invalid UTF-8 and truncated legacy keys. A read I/O
            # error must propagate: replacing an unread key revokes sessions.
            pass
        value = secrets.token_urlsafe(48)
        fd, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return value


@contextmanager
def _secret_file_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    normalized = path.resolve()
    with _SECRET_LOCKS_GUARD:
        thread_lock = _SECRET_LOCKS.setdefault(normalized, threading.Lock())
    if not thread_lock.acquire(timeout=timeout_seconds):
        raise TimeoutError("timed out waiting to initialize controller secret")
    stream: BinaryIO | None = None
    locked = False
    try:
        lock_path = path.with_name(path.name + ".initialize.lock")
        stream = lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                locked = True
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "timed out waiting to initialize controller secret"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        if stream is not None:
            try:
                if locked:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        thread_lock.release()


def _validate_secret_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("RIGPILOT_SECRET_KEY must be text")
    if value != value.strip() or len(value) < _MIN_SECRET_KEY_LENGTH:
        raise ValueError(
            f"RIGPILOT_SECRET_KEY must contain at least {_MIN_SECRET_KEY_LENGTH} "
            "non-whitespace characters"
        )
    if len(value) > 4096 or any(ord(character) < 32 for character in value):
        raise ValueError("RIGPILOT_SECRET_KEY contains invalid characters")
    if len(set(value)) < 8:
        raise ValueError("RIGPILOT_SECRET_KEY is too weak")
    return value


def _validate_admin_username(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("RIGPILOT_ADMIN_USERNAME must be text")
    normalized = value.strip()
    if not 1 <= len(normalized) <= 64:
        raise ValueError("RIGPILOT_ADMIN_USERNAME must contain 1-64 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("RIGPILOT_ADMIN_USERNAME contains control characters")
    return normalized


def _validate_int_range(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= min(maximum, _MAX_SIGNED_INT):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _reject_multi_worker_environment() -> None:
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        raw = os.getenv(name)
        if raw is None:
            continue
        try:
            workers = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if workers < 1:
            raise ValueError(f"{name} must be at least 1")
        if workers > 1:
            raise RuntimeError(
                "RigPilot requires exactly one controller worker per data directory"
            )


class ControllerInstanceLock:
    """Cross-process singleton guard for process-local controller brokers."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir).resolve() / "controller.instance.lock"
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        if self._stream is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        normalized = self.path.resolve()
        with _INSTANCE_LOCKS_GUARD:
            if normalized in _INSTANCE_LOCK_PATHS:
                raise RuntimeError(self._failure_message())
            stream = self.path.open("a+b")
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise RuntimeError(self._failure_message()) from exc
            _INSTANCE_LOCK_PATHS.add(normalized)
            self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        normalized = self.path.resolve()
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._stream = None
            with _INSTANCE_LOCKS_GUARD:
                _INSTANCE_LOCK_PATHS.discard(normalized)

    @staticmethod
    def _failure_message() -> str:
        return (
            "another RigPilot controller worker already owns this data directory; "
            "run exactly one worker per data directory"
        )

    def __enter__(self) -> "ControllerInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
