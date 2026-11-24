"""Exercise the actual default probe through the generation preparation path."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from agent.managed_miners import ManagedMinerManager
from agent.miner_packages import _MARKER_NAME, _tree_fingerprint
from agent.tests.test_managed_miners import native_srb_fixture
from agent.tests.test_managed_miners import FakeProcess, native_generation_manager
from agent.managed_miners import ManagedMinerError, ManagedMinerTransitionError
import agent.managed_miners as managed
from agent.native_miner_runtime import NativeMinerRuntimeError, discard_native_srb_bundle


@pytest.mark.skipif(os.name != "posix", reason="Actual PTY requires POSIX")
def test_default_linux_generation_probe_uses_real_pty_and_closes_its_run(tmp_path, monkeypatch):
    config, source, flight = native_srb_fixture(tmp_path)
    executable = source / "SRBMiner-MULTI"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "assert sys.argv[1:] == ['--list-devices'] and os.isatty(1)\n"
        "print('GPU1 [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [16384 MB]')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    marker = source / _MARKER_NAME
    value = json.loads(marker.read_text())
    value.update(_tree_fingerprint(source, config.miner_download_max_mb * 1024 * 1024))
    marker.write_text(json.dumps(value), encoding="utf-8")
    before = marker.read_bytes()
    from agent import native_srb_probe
    monkeypatch.setattr(native_srb_probe, "run_native_srb_probe",
                        lambda *a, **k: pytest.fail("Linux must not invoke the Windows adapter"))
    manager = ManagedMinerManager(config, system_name="Linux", device_runner=subprocess.run,
                                  port_allocator=lambda: 24567,
                                  privilege_checker=lambda _: True)
    prepared = manager._prepare_single(flight)
    run = Path(prepared["executable"]).parent.parent
    try:
        assert prepared["gpu_mapping"][0]["miner_id"] == 1
        assert not (run / "probe-pending").exists()
        assert manager._native_srb_plan(prepared).probe_closed is True
        assert marker.read_bytes() == before
    finally:
        manager._release_native_srb_plan(prepared)
    assert not run.exists()


def test_record_commit_failure_keeps_run_when_original_pid_exited_with_replacement(tmp_path, monkeypatch):
    config, _, flight = native_srb_fixture(tmp_path)
    manager, processes, launches = native_generation_manager(config)
    prepared = manager._prepare_single(flight)
    executable = prepared["executable"]
    original_write = managed._write_private_json
    def fail_commit(path, record, **kwargs):
        if path.name == "managed-miner-process.json":
            processes[record["pid"]].running = False
            processes[999] = FakeProcess(999, executable, 1999.0, prepared["argv"])
            raise ManagedMinerError("record commit failed")
        return original_write(path, record, **kwargs)
    monkeypatch.setattr(managed, "_write_private_json", fail_commit)
    try:
        with pytest.raises(ManagedMinerTransitionError) as caught:
            manager._apply_single(flight, prepared=prepared)
    finally:
        manager._release_native_srb_plan(prepared)
    assert caught.value.target_cleanup_unconfirmed is True
    assert processes[999].running and not processes[999].terminated
    assert executable.exists()


def test_confirmed_stop_reconciles_watchdog_before_record_cas_and_run_disposal(tmp_path):
    config, _, flight = native_srb_fixture(tmp_path)
    manager, processes, launches = native_generation_manager(config)
    result = manager.apply(flight)
    original = processes[result["pid"]]
    executable = original.executable
    def replace_during_stop():
        original.running = False
        original.terminated = True
        processes[999] = FakeProcess(999, executable, 1999.0, launches[0][0])
    original.terminate = replace_during_stop
    manager.stop()
    assert processes[999].terminated and not processes[999].running
    assert not (config.managed_miner_runtime_dir / "managed-miner-process.json").exists()
    assert not executable.exists()


def test_invalid_run_control_uses_runtime_error_contract_and_preserves_run(tmp_path):
    config, _, flight = native_srb_fixture(tmp_path)
    manager, _, _ = native_generation_manager(config)
    prepared = manager._prepare_single(flight)
    bundle = prepared["executable"].parent
    (bundle.parent / "control.json").write_bytes(b"{not-json")
    try:
        with pytest.raises(NativeMinerRuntimeError, match="ownership record is invalid"):
            discard_native_srb_bundle(bundle, config, config.managed_miner_runtime_dir, role="single")
    finally:
        manager._release_native_srb_plan(prepared)
    assert bundle.exists() and (bundle.parent / "control.json").read_bytes() == b"{not-json"
