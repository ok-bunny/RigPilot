"""Production Manager.apply startup path with controlled owned processes.

No miner, external network, VM or device is used. The constructor's default
production readiness remains enabled; only launch/process/clock boundaries
are replaced after construction. Both the launch and readiness methods run.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.managed_miners as managed
from agent.tests.test_managed_miners import (
    FakePopen, FakeProcess, document, managed_config,
)


def startup_fixture(
    tmp_path, monkeypatch, *, system_name="Linux", output="fresh", manager_module=managed,
):
    tmp_path = tmp_path.resolve()
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-seine-test-0.2.45"
    package.mkdir()
    executable = package / ("seine.exe" if system_name == "Windows" else "seine")
    executable.write_bytes(b"controlled executable identity, never executed")
    process = FakeProcess(4110, executable.resolve(), 1_700_000_000.0)
    flight = document(package, miner_type="seine", algorithm="blocknet-argon2id")
    flight["primary"].update(
        coin="BNT", pool_password="", pool_url="stratum+tcp://pool.example.test:3333",
    )
    flight["primary"]["miner_package"]["version"] = "0.2.45"
    manager = manager_module.ManagedMinerManager(
        cfg, port_allocator=lambda: 24567, system_name=system_name,
    )
    assert manager.enforce_startup_readiness is True
    launcher = cfg.managed_miner_runtime_dir / "managed-miner-launcher.log"
    launcher.parent.mkdir()
    launcher.write_bytes(b"an older session\n")
    clock = SimpleNamespace(seconds=0.0, tick=None)
    api_probes = []
    launches = []

    def popen(argv, **kwargs):
        process.argv = list(argv)
        launches.append((list(argv), kwargs["cwd"], kwargs["stdout"].name))
        assert Path(kwargs["stdout"].name) == launcher
        if output == "fresh":
            kwargs["stdout"].write(b"new Seine startup\n")
        return FakePopen(process.pid)

    def sleep(seconds):
        clock.seconds += seconds
        if output == "fresh":
            with launcher.open("ab") as stream:
                stream.write(b"new session progress\n")
        elif output == "wrong-file":
            with (launcher.parent / "seine.log").open("ab") as stream:
                stream.write(b"unrelated progress\n")
        if clock.tick is not None:
            clock.tick()

    monkeypatch.setattr(manager_module, "time", SimpleNamespace(
        monotonic=lambda: clock.seconds, sleep=sleep,
    ))
    manager.popen_factory = popen
    manager.process_factory = lambda _pid: process
    manager.process_iter_factory = lambda: [process] if process.running else []
    manager.boot_time_factory = lambda: 0.0
    monkeypatch.setattr(manager, "_loopback_api_is_listening", lambda port: (
        api_probes.append(port) or False
    ))
    return manager, flight, process, clock, launcher, launches, api_probes


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_default_manager_apply_seine_uses_real_launcher_marker_and_ready_contract(
    tmp_path, monkeypatch, system_name,
):
    manager, flight, process, clock, launcher, launches, probes = startup_fixture(
        tmp_path, monkeypatch, system_name=system_name,
    )
    result = manager.apply(flight)
    assert result["readiness"] == "verified_process_and_fresh_log"
    assert 3 <= clock.seconds < 4
    assert len(launches) == 1 and launches[0][1] == str(launcher.parent)
    assert probes == []  # Seine does not acquire an additional API hard gate.
    assert process.terminated is False
    state = json.loads(manager.config.miner_status_file.read_text())
    assert state["state"] == "running" and state["pid"] == process.pid
    record = json.loads((launcher.parent / "managed-miner-process.json").read_text())
    assert record["pid"] == process.pid and record["miner_type"] == "seine"
    assert record["version"] == 7
    data_dir = Path(launches[0][0][launches[0][0].index("--data-dir") + 1])
    assert Path(record["seine_data_dir"]) == data_dir
    assert "Bearer " not in json.dumps(record)


def test_manager_keeps_seine_0215_on_legacy_unauthenticated_process_record(
    tmp_path, monkeypatch,
):
    manager, flight, process, clock, launcher, launches, _ = startup_fixture(
        tmp_path, monkeypatch, system_name="Linux",
    )
    flight["primary"]["miner_package"]["version"] = "0.2.15"
    result = manager.apply(flight)
    record = json.loads((launcher.parent / "managed-miner-process.json").read_text())
    assert result["pid"] == process.pid
    assert record["version"] == 2 and "seine_data_dir" not in record
    assert record["argv_sha256"] == managed.managed_argv_fingerprint(
        launches[0][0], executable=process.executable,
    )


@pytest.mark.parametrize("output", ["old-only", "wrong-file"])
def test_existing_launcher_or_seine_log_cannot_satisfy_new_launch_readiness(
    tmp_path, monkeypatch, output,
):
    manager, flight, process, clock, launcher, launches, _ = startup_fixture(
        tmp_path, monkeypatch, output=output,
    )
    with pytest.raises(managed.ManagedMinerTransitionError, match="before timeout"):
        manager.apply(flight)
    assert clock.seconds == 30.0  # Advance the production deadline, do not extend it.
    assert process.terminated is True and len(launches) == 1
    assert launcher.read_bytes() == b"an older session\n"
    assert not (launcher.parent / "managed-miner-process.json").exists()
    assert json.loads(manager.config.miner_status_file.read_text())["state"] == "failed"


def test_new_seine_exit_during_log_warmup_is_not_ready(tmp_path, monkeypatch):
    manager, flight, process, clock, launcher, _, _ = startup_fixture(tmp_path, monkeypatch)
    clock.tick = lambda: setattr(process, "running", False)
    with pytest.raises(managed.ManagedMinerTransitionError, match="exited before startup readiness"):
        manager.apply(flight)
    assert clock.seconds < 4
    assert not (launcher.parent / "managed-miner-process.json").exists()
    assert json.loads(manager.config.miner_status_file.read_text())["state"] == "failed"


@pytest.mark.parametrize("changed_identity", ["argv", "creation-time", "executable"])
def test_fresh_seine_launcher_does_not_override_owned_process_identity(
    tmp_path, monkeypatch, changed_identity,
):
    manager, flight, process, clock, _, _, _ = startup_fixture(tmp_path, monkeypatch)
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"not the managed executable")

    def change():
        if changed_identity == "argv":
            process.argv.append("--unrelated-invocation")
        elif changed_identity == "creation-time":
            process.created += 10
        else:
            process.executable = unrelated
        clock.tick = None

    clock.tick = change
    with pytest.raises(managed.ManagedMinerTransitionError, match="invocation|creation time|another path"):
        manager.apply(flight)
    assert clock.seconds < 4
    assert process.terminated is False  # Never terminate an identity that changed.
    assert json.loads(manager.config.miner_status_file.read_text())["state"] == "failed"


def test_seine_launcher_link_is_rejected_before_process_launch(tmp_path, monkeypatch):
    manager, flight, _, _, launcher, launches, _ = startup_fixture(tmp_path, monkeypatch)
    target = tmp_path / "outside.log"
    target.write_bytes(b"preserve unrelated bytes")
    launcher.unlink()
    try:
        launcher.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(managed.ManagedMinerError, match="startup log is unsafe"):
        manager.apply(flight)
    assert launches == [] and target.read_bytes() == b"preserve unrelated bytes"
