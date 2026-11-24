from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import inventory, managed_miners, miner_logs
from agent.config import AgentConfig
from agent.managed_miners import ManagedMinerError, ManagedMinerManager
from agent.tests.test_managed_miners import FakeProcess


def _manager(tmp_path: Path, *, configured_status: bool = False):
    runtime = tmp_path / "runtime"
    status_path = (
        tmp_path / "observations" / "selected-status.json"
        if configured_status
        else runtime / "miner-status.json"
    )
    cfg = AgentConfig(
        server_url="https://controller.example.test",
        state_file=tmp_path / "state.json",
        managed_mining_enabled=True,
        managed_miner_runtime_dir=runtime,
        miner_status_file=status_path if configured_status else None,
        miner_packages_dir=tmp_path / "packages",
    )
    manager = ManagedMinerManager(cfg, system_name="Linux")
    assert manager._status_path(runtime) == status_path
    return manager, runtime, status_path


def _fail_sync(_fd: int):
    raise AssertionError("observational status must not request fsync")


@pytest.mark.parametrize("miner_type", ["srbminer", "hiveos"])
@pytest.mark.parametrize("configured_status", [False, True])
def test_status_write_is_complete_private_atomic_without_fsync(
    tmp_path, monkeypatch, miner_type, configured_status
):
    manager, runtime, status_path = _manager(
        tmp_path, configured_status=configured_status
    )
    status_path.parent.mkdir(parents=True)
    old_bytes = b'{"state":"stopped"}\n'
    status_path.write_bytes(old_bytes)
    old_identity = status_path.stat().st_ino
    original_replace = os.replace
    replaced = []

    def checked_replace(source, target):
        source, target = Path(source), Path(target)
        assert target == status_path
        assert source.parent == status_path.parent
        assert status_path.read_bytes() == old_bytes
        info = source.lstat()
        assert stat.S_ISREG(info.st_mode)
        if os.name != "nt":
            assert stat.S_IMODE(info.st_mode) == 0o600
        payload = json.loads(source.read_text(encoding="utf-8"))
        assert payload["state"] == "running"
        assert payload["miner_type"] == miner_type
        assert payload["flight_sheet_id"] == "flight-中文"
        assert source.read_bytes().endswith(b"\n")
        replaced.append(source)
        return original_replace(source, target)

    monkeypatch.setattr(managed_miners.os, "fsync", _fail_sync)
    monkeypatch.setattr(managed_miners.os, "replace", checked_replace)
    manager._write_status(
        runtime,
        {"flight_sheet_id": "flight-中文"},
        "running",
        miner_type=miner_type,
        api_port=12345,
        pid=321,
        started_at="2026-10-07T00:00:00+00:00",
        gpu_mapping=[{"logical_index": 0, "uuid": "GPU-fixture"}],
    )

    assert len(replaced) == 1
    assert status_path.stat().st_ino != old_identity
    assert stat.S_ISREG(status_path.lstat().st_mode)
    if os.name != "nt":
        assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["pid"] == 321
    assert status["api_port"] == 12345
    assert status["gpu_mapping"] == [{"logical_index": 0, "uuid": "GPU-fixture"}]
    assert status["updated_at"]
    assert not list(status_path.parent.glob(f".{status_path.name}.*.tmp"))


@pytest.mark.parametrize("failure", ["write", "flush", "replace"])
def test_status_io_failure_preserves_previous_value_and_removes_private_temp(
    tmp_path, monkeypatch, failure
):
    manager, runtime, status_path = _manager(tmp_path)
    runtime.mkdir()
    old_bytes = b'{"state":"stopped","pid":null}\n'
    status_path.write_bytes(old_bytes)
    old_identity = status_path.stat().st_ino
    original_temporary_file = managed_miners.tempfile.NamedTemporaryFile

    def fail_io(*_args, **_kwargs):
        raise OSError(f"injected {failure} failure")

    if failure in {"write", "flush"}:
        @contextmanager
        def broken_temporary_file(*args, **kwargs):
            with original_temporary_file(*args, **kwargs) as output:
                yield SimpleNamespace(
                    name=output.name,
                    write=fail_io if failure == "write" else output.write,
                    flush=fail_io if failure == "flush" else output.flush,
                    fileno=output.fileno,
                )

        monkeypatch.setattr(
            managed_miners.tempfile, "NamedTemporaryFile", broken_temporary_file
        )
    else:
        monkeypatch.setattr(managed_miners.os, "replace", fail_io)
    monkeypatch.setattr(managed_miners.os, "fsync", _fail_sync)

    with pytest.raises(ManagedMinerError, match="cannot write miner-status.json"):
        manager._write_status(
            runtime, {"flight_sheet_id": "flight"}, "starting", miner_type="srbminer"
        )

    assert status_path.read_bytes() == old_bytes
    assert status_path.stat().st_ino == old_identity
    assert not list(runtime.glob(".miner-status.json.*.tmp"))


@pytest.mark.parametrize(
    "name",
    ["managed-miner-process.json", "pending-hiveos-start.json", "flight-transition.json"],
)
def test_private_identity_and_recovery_writes_default_to_fsync(tmp_path, monkeypatch, name):
    path = tmp_path / name
    old_bytes = b'{"phase":"old"}\n'
    path.write_bytes(old_bytes)
    original_sync, original_replace = os.fsync, os.replace
    events = []

    def sync(fd):
        assert path.read_bytes() == old_bytes
        events.append("fsync")
        return original_sync(fd)

    def replace(source, target):
        assert Path(target) == path
        assert events == ["fsync"]
        events.append("replace")
        return original_replace(source, target)

    monkeypatch.setattr(managed_miners.os, "fsync", sync)
    monkeypatch.setattr(managed_miners.os, "replace", replace)
    managed_miners._write_private_json(path, {"phase": "new"})

    assert events == ["fsync", "replace"]
    assert json.loads(path.read_text()) == {"phase": "new"}
    assert not list(tmp_path.glob(f".{name}.*.tmp"))


def test_private_identity_sync_failure_preserves_previous_record(tmp_path, monkeypatch):
    path = tmp_path / "managed-miner-process.json"
    old_bytes = b'{"pid":10,"identity":"old"}\n'
    path.write_bytes(old_bytes)

    def fail_sync(_fd):
        raise OSError("injected process identity fsync failure")

    monkeypatch.setattr(managed_miners.os, "fsync", fail_sync)
    with pytest.raises(ManagedMinerError, match="cannot write managed-miner-process.json"):
        managed_miners._write_private_json(path, {"pid": 20, "identity": "new"})

    assert path.read_bytes() == old_bytes
    assert not list(tmp_path.glob(".managed-miner-process.json.*.tmp"))


@pytest.mark.parametrize("sync_fails", [False, True])
def test_real_launch_process_commit_keeps_durability_and_fail_closed_cleanup(
    tmp_path, monkeypatch, sync_fails
):
    manager, runtime, status_path = _manager(tmp_path)
    runtime.mkdir()
    package = manager.config.miner_packages_dir / "xmrig-fixture"
    package.mkdir(parents=True)
    executable = package / "xmrig"
    executable.write_bytes(b"local non-executable miner fixture")
    argv = [str(executable), "--fixture"]
    process = FakeProcess(401, executable, 2401.0, argv=argv)
    manager.process_factory = lambda _pid: process
    manager.boot_time_factory = lambda: 0.0
    monkeypatch.setattr(manager, "_launch", lambda *_args, **_kwargs: (401, 2401.0, True))
    original_temporary_file = managed_miners.tempfile.NamedTemporaryFile
    original_sync = os.fsync
    fd_paths, sync_paths, readiness_calls = {}, [], []

    @contextmanager
    def observed_temporary_file(*args, **kwargs):
        with original_temporary_file(*args, **kwargs) as output:
            fd_paths[output.fileno()] = Path(output.name)
            yield output

    def sync(fd):
        temporary_path = fd_paths[fd]
        assert temporary_path.name.startswith(".managed-miner-process.json.")
        sync_paths.append(temporary_path)
        if sync_fails:
            raise OSError("injected actual process commit fsync failure")
        return original_sync(fd)

    def ready(_runtime, record, **_kwargs):
        assert sync_paths
        assert record["pid"] == 401
        readiness_calls.append(record["pid"])
        return "fixture_positive_process_readiness"

    monkeypatch.setattr(managed_miners.tempfile, "NamedTemporaryFile", observed_temporary_file)
    monkeypatch.setattr(managed_miners.os, "fsync", sync)
    monkeypatch.setattr(manager, "_wait_for_managed_miner_ready", ready)
    prepared = {
        "runtime": runtime, "miner_type": "xmrig", "api_port": 12345,
        "gpu_mapping": [], "argv": argv, "executable": executable,
    }
    document = {"flight_sheet_id": "durable-process-fixture"}
    if sync_fails:
        with pytest.raises(ManagedMinerError, match="cannot write managed-miner-process.json"):
            manager._apply_single_plan(
                document, prepared=prepared, stop_previous=False,
                require_startup_readiness=True,
            )
        assert process.terminated is True
        assert not (runtime / "managed-miner-process.json").exists()
        assert readiness_calls == []
        assert json.loads(status_path.read_text())["state"] == "failed"
    else:
        result = manager._apply_single_plan(
            document, prepared=prepared, stop_previous=False,
            require_startup_readiness=True,
        )
        assert result["pid"] == 401
        assert readiness_calls == [401]
        assert process.terminated is False
        assert json.loads((runtime / "managed-miner-process.json").read_text())["pid"] == 401
        assert json.loads(status_path.read_text())["state"] == "running"
        status_path.unlink()
        assert manager._verified_previous(runtime)[0]["pid"] == 401
    assert len(sync_paths) == 1
    assert not list(runtime.glob(".*.tmp"))


@pytest.mark.parametrize("miner_type", ["srbminer", "hiveos"])
def test_status_alone_never_authorizes_running_or_owned_process(tmp_path, monkeypatch, miner_type):
    manager, runtime, _status_path = _manager(tmp_path)
    monkeypatch.setattr(managed_miners.os, "fsync", _fail_sync)
    manager._write_status(
        runtime, {"flight_sheet_id": "unproven-flight"}, "running",
        miner_type=miner_type, pid=9999,
    )

    def unexpected_process_lookup(_pid):
        raise AssertionError("a status PID must not authorize process inspection")

    manager.process_factory = unexpected_process_lookup
    assert manager._verified_previous(runtime) is None
    assert inventory._managed_process_truth(
        manager.config, unexpected_process_lookup, system_name="Linux"
    ) == ("absent", False, None, None)
    assert miner_logs._is_running(
        None, manager.config, runtime, unexpected_process_lookup, lambda: ()
    ) is False
