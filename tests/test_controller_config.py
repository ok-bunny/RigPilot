from __future__ import annotations

import errno
import threading
from pathlib import Path

import pytest

from server.config import (
    ControllerInstanceLock,
    Settings,
    _load_or_create_secret,
)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "database_path": tmp_path / "rigpilot.db",
        "secret_key": "controller-config-test-secret-with-enough-entropy",
        "admin_username": "admin",
        "admin_password": "password",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"secret_key": "short"},
        {"secret_key": "a" * 64},
        {"admin_username": ""},
        {"admin_username": "a" * 65},
        {"session_hours": 0},
        {"agent_online_seconds": 4},
        {"telemetry_retention_days": 0},
        {"hashrate_history_retention_days": 0},
        {"command_batch_size": 0},
        {"max_miner_package_bytes": 0},
        {"miner_package_import_timeout_seconds": 0},
        {"max_agent_release_bytes": 0},
    ],
)
def test_settings_reject_weak_or_out_of_range_values(
    tmp_path: Path, overrides: dict[str, object]
):
    with pytest.raises(ValueError):
        _settings(tmp_path, **overrides)


def test_history_defaults_separate_raw_and_compact_retention(tmp_path: Path):
    settings = _settings(tmp_path)
    assert settings.telemetry_retention_days == 2
    assert settings.hashrate_history_retention_days == 14


def test_secret_file_creation_is_atomic_across_racing_threads(tmp_path: Path):
    path = tmp_path / "secret.key"
    barrier = threading.Barrier(8)
    values: list[str] = []
    errors: list[BaseException] = []

    def create() -> None:
        try:
            barrier.wait()
            values.append(_load_or_create_secret(path))
        except BaseException as exc:  # captured for assertion in main thread
            errors.append(exc)

    workers = [threading.Thread(target=create) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)

    assert not errors
    assert len(values) == 8
    assert len(set(values)) == 1
    assert path.read_text(encoding="utf-8") == values[0]


def test_corrupt_secret_file_is_atomically_replaced_for_racing_readers(
    tmp_path: Path,
):
    path = tmp_path / "secret.key"
    path.write_text("truncated", encoding="utf-8")
    barrier = threading.Barrier(6)
    values: list[str] = []
    errors: list[BaseException] = []

    def repair() -> None:
        try:
            barrier.wait()
            values.append(_load_or_create_secret(path))
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=repair) for _ in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)

    assert not errors
    assert len(set(values)) == 1
    assert values[0] != "truncated"
    assert len(values[0]) >= 32
    assert path.read_text(encoding="utf-8") == values[0]


def test_transient_secret_read_failure_preserves_existing_sessions(tmp_path, monkeypatch):
    path = tmp_path / "secret.key"
    original = _load_or_create_secret(path)
    old_read = Path.read_text

    def fail_read(self, *args, **kwargs):
        if self == path:
            raise OSError(errno.EIO, "temporary I/O failure")
        return old_read(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail_read)
        with pytest.raises(OSError) as error:
            _load_or_create_secret(path)
    assert error.value.errno == errno.EIO
    assert _load_or_create_secret(path) == original, "signing key changed after transient I/O failure"


def test_controller_instance_lock_rejects_a_second_local_worker(tmp_path: Path):
    first = ControllerInstanceLock(tmp_path)
    second = ControllerInstanceLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="exactly one"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_environment_worker_count_must_be_one(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RIGPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="exactly one"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["treu", "", "enabled", "2"])
def test_unknown_https_boolean_environment_value_is_rejected(
    tmp_path: Path, monkeypatch, value: str
):
    monkeypatch.setenv("RIGPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "RIGPILOT_SECRET_KEY",
        "controller-config-test-secret-with-enough-entropy",
    )
    monkeypatch.setenv("RIGPILOT_REQUIRE_HTTPS", value)

    with pytest.raises(ValueError, match="RIGPILOT_REQUIRE_HTTPS"):
        Settings.from_env()


@pytest.mark.parametrize("value,expected", [("YES", True), ("off", False)])
def test_known_https_boolean_environment_values_are_explicit(
    tmp_path: Path, monkeypatch, value: str, expected: bool
):
    monkeypatch.setenv("RIGPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "RIGPILOT_SECRET_KEY",
        "controller-config-test-secret-with-enough-entropy",
    )
    monkeypatch.setenv("RIGPILOT_REQUIRE_HTTPS", value)
    assert Settings.from_env().require_https_for_agents is expected
