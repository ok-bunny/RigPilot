from __future__ import annotations

import json
import errno
from pathlib import Path

import pytest

from agent.commands import Command, CommandExecutor
from agent.config import AgentConfig
from agent.inventory import collect_mining_status
from agent.managed_miners import ManagedMinerError, ManagedMinerManager


def config(tmp_path: Path) -> AgentConfig:
    runtime = tmp_path / "runtime"
    packages = tmp_path / "packages"
    runtime.mkdir()
    packages.mkdir()
    return AgentConfig(
        server_url="https://example.test",
        state_file=tmp_path / "state.json",
        miner_packages_dir=packages,
        managed_mining_enabled=True,
        managed_miner_runtime_dir=runtime,
        flight_sheet_file=runtime / "active-flight-sheet.json",
    )


def active_document() -> dict[str, object]:
    return {
        "version": 3,
        "flight_sheet_id": "sheet-1",
        "name": "XMR",
        "worker_name": "worker",
        "primary": {"miner_type": "xmrig"},
        "secondary": None,
        "applied_at": "2026-01-01T00:00:00+00:00",
    }


class Process:
    def __init__(self, executable: Path):
        self.executable = executable
        self.terminated = False

    def is_running(self):
        return not self.terminated

    def status(self):
        return "running"

    def exe(self):
        return str(self.executable)

    def create_time(self):
        return 1000.25

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        return 0

    def kill(self):
        self.terminated = True


def test_executor_delegates_remove_and_rejects_payload(tmp_path: Path):
    calls: list[str] = []

    class Manager:
        def remove_flight_sheet(self):
            calls.append("remove")
            return {"configuration_cleared": True}

        def stop(self):
            calls.append("stop")
            return {"managed": True, "stopped": False}

        def restart(self):
            raise AssertionError("wrong method")

    executor = CommandExecutor(
        config(tmp_path), managed_miner_factory=lambda _config: Manager()
    )
    removed = executor.execute(Command("remove", "remove_flight_sheet", {}))
    rejected = executor.execute(
        Command("extra", "remove_flight_sheet", {"path": "caller-controlled"})
    )
    assert removed.status == "succeeded"
    assert removed.result["configuration_cleared"] is True
    assert rejected.status == "failed"
    assert rejected.result["rejected"] is True
    assert calls == ["stop", "remove"]


def test_manager_remove_stops_running_miner_and_reports_not_configured(tmp_path: Path):
    value = config(tmp_path)
    assert value.flight_sheet_file is not None
    assert value.managed_miner_runtime_dir is not None
    assert value.miner_packages_dir is not None
    package = value.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    value.flight_sheet_file.write_text(json.dumps(active_document()), encoding="utf-8")
    process = Process(executable.resolve())
    (value.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 101,
                "executable": str(executable.resolve()),
                "create_time": 1000.25,
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet-1",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    manager = ManagedMinerManager(
        value,
        process_factory=lambda _pid: process,
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    result = manager.remove_flight_sheet()

    assert result["configuration_cleared"] is True
    assert result["miner_stopped"] is True
    assert process.terminated is True
    assert not value.flight_sheet_file.exists()
    assert not (value.managed_miner_runtime_dir / "miner-status.json").exists()
    status = collect_mining_status(value, system_name="Windows")
    assert status["configured"] is False
    assert status["state"] == "not_configured"


def test_manager_remove_clears_stopped_config_and_is_idempotent(tmp_path: Path):
    value = config(tmp_path)
    assert value.flight_sheet_file is not None
    value.flight_sheet_file.write_text(json.dumps(active_document()), encoding="utf-8")
    manager = ManagedMinerManager(value, system_name="Windows")

    first = manager.remove_flight_sheet()
    second = manager.remove_flight_sheet()

    assert first["removed"] is True
    assert first["miner_stopped"] is False
    assert second["removed"] is False
    assert second["configuration_cleared"] is True
    status = collect_mining_status(value, system_name="Windows")
    assert status["configured"] is False
    assert status["state"] == "not_configured"


@pytest.mark.parametrize("target_name", ["active", "pending", "status"])
def test_manager_remove_rejects_links_before_stopping(tmp_path: Path, target_name: str):
    value = config(tmp_path)
    assert value.flight_sheet_file is not None
    assert value.managed_miner_runtime_dir is not None
    value.flight_sheet_file.write_text(json.dumps(active_document()), encoding="utf-8")
    paths = {
        "active": value.flight_sheet_file,
        "pending": value.flight_sheet_file.with_name(
            f".{value.flight_sheet_file.name}.pending"
        ),
        "status": value.managed_miner_runtime_dir / "miner-status.json",
    }
    selected = paths[target_name]
    if selected.exists():
        selected.unlink()
    target = tmp_path / f"{target_name}-target"
    target.write_text("do not delete", encoding="utf-8")
    try:
        selected.symlink_to(target)
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP} or getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"symlink creation is unavailable for this user: {exc}")
        raise
    manager = ManagedMinerManager(value, system_name="Windows")
    stop_calls: list[bool] = []
    manager.stop = lambda: stop_calls.append(True)  # type: ignore[method-assign]

    with pytest.raises(ManagedMinerError, match="safe regular file"):
        manager.remove_flight_sheet()

    assert stop_calls == []
    assert target.read_text(encoding="utf-8") == "do not delete"
