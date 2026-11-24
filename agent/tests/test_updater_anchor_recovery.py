"""Failure-injection tests for the protocol-v1 Linux updater trust anchor.

P1-P4 (user-approved 2026-11-03, dispatch 02-anchor-fixes-0855-20261103)
changed the previously immutable ``RigPilotAgentUpdater.py``.  These tests
import the anchor as a module and exercise its recovery paths directly
against temporary roots, with systemctl and ownership checks substituted:

* P1: ENOSPC during the rollback receipt must never wedge the staged
  payload; cleanup is mandatory and precedes every durable write.
* P2: a confirmation durably written during the final sleep window of the
  activation loop must not be rolled back.
* P4: a wall clock provably behind the durable control-file mtimes (no-RTC
  power loss) makes the stored deadline count as expired.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


UPDATER_PATH = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "linux"
    / "portable"
    / "RigPilotAgentUpdater.py"
)

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork") or not hasattr(os, "symlink"),
    reason="the Linux updater anchor is only importable on POSIX hosts",
)


def _load_updater(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "rp_updater_anchor_under_test", UPDATER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Tests run as a normal user: keep every type/symlink check, drop the
    # root-ownership assertions, and neutralize chown-to-root.
    original_plain = module._plain

    def unowned_plain(path, *, directory, root_owned=True):
        return original_plain(path, directory=directory, root_owned=False)

    monkeypatch.setattr(module, "_plain", unowned_plain)
    monkeypatch.setattr("os.chown", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        module,
        "_run_systemctl",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    return module


def _completed(ok: bool = True) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(("systemctl",), 0 if ok else 1, "", "")


def _make_layout(tmp_path: Path) -> tuple[Path, Path, dict, dict]:
    program = tmp_path / "opt" / "rigpilot-agent"
    data = tmp_path / "var" / "lib" / "rigpilot-agent"
    (program / "versions" / "0.8.1").mkdir(parents=True)
    (program / "versions" / "0.8.1" / "RigPilotAgent").write_bytes(b"old-binary\n")
    (program / "versions" / "0.8.55").mkdir()
    (program / "versions" / "0.8.55" / "RigPilotAgent").write_bytes(b"new-binary\n")
    (program / "active-version").write_text("0.8.55\n", encoding="ascii")
    job = data / "agent-updates" / "staged" / ("a" * 32)
    (job / "bundle").mkdir(parents=True)
    (job / "bundle" / "RigPilotAgent").write_bytes(b"staged-binary\n")
    (job / "release.tar.gz").write_bytes(b"package" * 1024)
    (job / "updater.log").write_text("log\n", encoding="utf-8")
    (job / "updater-ready.json").write_text("{}\n", encoding="utf-8")
    (job / "activation-arm.json").write_text("{}\n", encoding="utf-8")
    pending = {
        "schema_version": 1,
        "version": "0.8.55",
        "relative_executable": "versions/0.8.55/RigPilotAgent",
        "activation_id": "a" * 32,
        "command_id": "01234567-89ab-4cde-8f01-23456789abcd",
        "agent_id": "00112233-4455-4666-8777-8899aabbccdd",
    }
    state = {
        "schema_version": 1,
        "pending": pending,
        "previous_version": "0.8.1",
        "deadline": 0.0,
        "phase": "activated",
        "slot_created": True,
        "recovery_attempts": 0,
    }
    (program / "pending-update.json").write_text(
        json.dumps(pending) + "\n", encoding="utf-8"
    )
    (program / "updater-state.json").write_text(
        json.dumps(state) + "\n", encoding="utf-8"
    )
    return program, data, pending, state


def test_p1_enospc_receipt_failure_still_completes_cleanup(monkeypatch, tmp_path):
    module = _load_updater(monkeypatch, tmp_path)
    program, data, pending, state = _make_layout(tmp_path)
    job = data / "agent-updates" / "staged" / pending["activation_id"]

    real_write_result = module._write_result
    state["recovery_attempts"] = module.MAX_RECOVERY_ATTEMPTS

    def failing_write_result(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(module, "_write_result", failing_write_result)
    with pytest.raises(OSError):
        module._rollback(program, data, pending, state, "simulated ENOSPC rollback")

    # Cleanup happened even though the receipt write failed, and the freed
    # payload is gone: nothing stays wedged on a full filesystem.
    assert not (job / "release.tar.gz").exists()
    assert not (job / "bundle").exists()
    assert not (job / "updater.log").exists()
    assert not (job / "activation-arm.json").exists()
    # The previous version was already restored and the new slot quarantined:
    # deletion-style steps precede every durable write.
    assert (program / "active-version").read_text(encoding="ascii") == "0.8.1\n"
    quarantine = (
        program / "versions" / f".rolled-back-0.8.55-{pending['activation_id']}"
    )
    assert (quarantine / "RigPilotAgent").read_bytes() == b"new-binary\n"
    # Control files remain so the recovery timer retries the receipt.
    assert (program / "pending-update.json").exists()
    assert (program / "updater-state.json").exists()

    # The retry (timer fires again, receipt write now succeeds) completes.
    monkeypatch.setattr(module, "_write_result", real_write_result)
    module._rollback(program, data, pending, state, "retry after ENOSPC")
    receipt = json.loads(
        (job / "activation-result.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "rolled_back"
    assert not (program / "pending-update.json").exists()
    assert not (program / "updater-state.json").exists()


def test_p1_rollback_restores_previous_and_reports(monkeypatch, tmp_path):
    module = _load_updater(monkeypatch, tmp_path)
    program, data, pending, state = _make_layout(tmp_path)
    module._rollback(program, data, pending, state, "deadline expired")
    job = data / "agent-updates" / "staged" / pending["activation_id"]
    receipt = json.loads((job / "activation-result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "rolled_back"
    assert receipt["activation_id"] == pending["activation_id"]
    assert (program / "active-version").read_text(encoding="ascii") == "0.8.1\n"
    assert not (job / "release.tar.gz").exists()
    assert not (program / "pending-update.json").exists()


@pytest.mark.parametrize("previous_still_running", [True, False])
def test_rollback_restarts_only_if_previous_agent_is_not_serving(monkeypatch, tmp_path, previous_still_running):
    module = _load_updater(monkeypatch, tmp_path)
    program, data, pending, state = _make_layout(tmp_path)
    calls = []

    def running(root, version):
        assert root == program and version == state["previous_version"]
        assert module._read_version(program / module.ACTIVE_VERSION_NAME) == version
        return previous_still_running

    def systemctl(*args, **kwargs):
        calls.append(args)
        return _completed()

    monkeypatch.setattr(module, "_service_runs_target", running)
    monkeypatch.setattr(module, "_run_systemctl", systemctl)
    module._rollback(program, data, pending, state, "handoff interrupted")
    assert (("restart", "rigpilot-agent.service") in calls) == (not previous_still_running)
    assert calls[-1] == ("is-active", "--quiet", "rigpilot-agent.service")
    assert not (program / module.UPDATER_STATE_NAME).exists()


def test_p1_restart_failure_keeps_control_files_retryable(monkeypatch, tmp_path):
    module = _load_updater(monkeypatch, tmp_path)
    program, data, pending, state = _make_layout(tmp_path)

    def failing_systemctl(*args, **kwargs):
        if args and args[0] == "restart":
            raise subprocess.SubprocessError("systemctl unreachable")
        return _completed()

    monkeypatch.setattr(module, "_run_systemctl", failing_systemctl)
    with pytest.raises(module.UpdateFailure, match="rollback restart failed"):
        module._rollback(program, data, pending, state, "deadline expired")

    job = data / "agent-updates" / "staged" / pending["activation_id"]
    # Even with the restart failing, cleanup and the receipt completed.
    assert not (job / "release.tar.gz").exists()
    receipt = json.loads((job / "activation-result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "rolled_back"
    # Control files stay for the timer to retry the restart.
    assert (program / "pending-update.json").exists()
    assert (program / "updater-state.json").exists()


def _activate_fixture(monkeypatch, tmp_path, module):
    program, data, pending, state = _make_layout(tmp_path)
    job = data / "agent-updates" / "staged" / pending["activation_id"]
    # Prepare the verified bundle for activate().
    import hashlib

    binary = (job / "bundle" / "RigPilotAgent").read_bytes()
    manifest = {
        "schema_version": 1,
        "version": "0.8.55",
        "platform": "linux",
        "architecture": "x86_64",
        "files": {"RigPilotAgent": hashlib.sha256(binary).hexdigest()},
    }
    (job / "bundle" / "agent-release.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (program / "active-version").write_text("0.8.1\n", encoding="ascii")
    # Remove the pre-created target slot so activate can install it.
    import shutil

    shutil.rmtree(program / "versions" / "0.8.55")
    monkeypatch.setattr(module, "FIXED_PROGRAM_ROOT", program)
    monkeypatch.setattr(module, "FIXED_DATA_ROOT", data)
    monkeypatch.setattr(module, "CONFIRMATION_SECONDS", 0.05)
    # _fixed_roots enforces real root ownership/modes; this test targets the
    # confirmation loop, so accept the fixture layout directly.
    monkeypatch.setattr(
        module,
        "_fixed_roots",
        lambda program_root, data_root: (Path(program_root), Path(data_root)),
    )
    args = SimpleNamespace(
        program_root=str(program),
        data_root=str(data),
        staged_bundle=str(job / "bundle"),
        version="0.8.55",
        activation_id=pending["activation_id"],
        agent_id=pending["agent_id"],
        command_id=pending["command_id"],
        agent_pid=999999,  # never alive: expected_start=None, process_exited
        ready_path=str(job / "updater-ready.json"),
    )
    # The staged activation is armed so _wait_for_activation returns at once.
    (job / "activation-arm.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "0.8.55",
                "activation_id": pending["activation_id"],
                "command_id": pending["command_id"],
                "agent_id": pending["agent_id"],
                "armed": True,
            }
        ),
        encoding="utf-8",
    )
    return module, program, data, job, pending, args


def test_p2_confirmation_during_final_sleep_is_not_rolled_back(
    monkeypatch, tmp_path
):
    module = _load_updater(monkeypatch, tmp_path)
    module, program, data, job, pending, args = _activate_fixture(
        monkeypatch, tmp_path, module
    )
    confirmation = (
        data / "agent-updates" / "confirmations" / f"{pending['activation_id']}.json"
    )
    confirmation.parent.mkdir(parents=True, exist_ok=True)

    real_sleep = time.sleep
    written = {"done": False}

    def sleep_stub(seconds):
        # The confirmation lands only inside the activation loop's sleep
        # window: every in-loop check misses it, exactly the B3 off-by-one.
        if not written["done"]:
            written["done"] = True
            confirmation.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "activation_id": pending["activation_id"],
                        "command_id": pending["command_id"],
                        "version": "0.8.55",
                        "agent_id": pending["agent_id"],
                        "health_heartbeats": 2,
                    }
                ),
                encoding="utf-8",
            )
        real_sleep(0.001)

    monkeypatch.setattr(time, "sleep", sleep_stub)
    exit_code = module.activate(args)
    assert exit_code == 0
    # The confirmed replacement stays active: no rollback happened.
    assert (program / "active-version").read_text(encoding="ascii") == "0.8.55\n"
    receipt = json.loads(
        (job / "activation-result.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "succeeded"
    state_value = json.loads(
        (program / "updater-state.json").read_text(encoding="utf-8")
    )
    assert state_value["phase"] == "confirmed"
    assert not (
        program / "versions" / f".rolled-back-0.8.55-{pending['activation_id']}"
    ).exists()


def test_p2_without_late_confirmation_activate_still_rolls_back(monkeypatch, tmp_path):
    module = _load_updater(monkeypatch, tmp_path)
    module, program, data, job, pending, args = _activate_fixture(
        monkeypatch, tmp_path, module
    )
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert module.activate(args) == 1
    assert (program / "active-version").read_text(encoding="ascii") == "0.8.1\n"
    receipt = json.loads((job / "activation-result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "rolled_back"
    assert not (program / "pending-update.json").exists()


def test_p4_deadline_open_uses_durable_anchors(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "rp_updater_anchor_p4", UPDATER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = time.time()
    # Wall-clock deadline far in the future.
    assert module._deadline_open(now + 3600.0, ()) is True
    # Deadline reached, or missing/invalid: expired.
    assert module._deadline_open(now - 1.0, ()) is False
    assert module._deadline_open(None, ()) is False
    assert module._deadline_open(True, ()) is False
    # No-RTC power loss: the clock is far behind the durable anchor.
    assert module._deadline_open(now + 3600.0, (now + 7200.0,)) is False
    # Small NTP slew within tolerance must not trigger the fallback.
    assert module._deadline_open(now + 3600.0, (now + 1.0,)) is True
    # Any one durable anchor suffices to prove the reset.
    assert module._deadline_open(now + 3600.0, (now - 60.0, now + 7200.0)) is False


def test_p4_durable_mtime_anchors_reads_both_control_files(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "rp_updater_anchor_p4b", UPDATER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    program = tmp_path / "program"
    program.mkdir()
    (program / "pending-update.json").write_text("{}", encoding="utf-8")
    (program / "updater-state.json").write_text("{}", encoding="utf-8")
    anchors = module._durable_mtime_anchors(program)
    assert len(anchors) == 2
    for name, anchor in zip(("pending-update.json", "updater-state.json"), anchors):
        assert anchor == (program / name).lstat().st_mtime
    assert stat.S_ISREG((program / "pending-update.json").stat().st_mode)


def test_p4_finish_or_rollback_treats_reset_clock_as_expired(monkeypatch, tmp_path):
    module = _load_updater(monkeypatch, tmp_path)
    program, data, pending, state = _make_layout(tmp_path)
    # The stored wall-clock deadline is hours in the future, but the durable
    # anchors sit even further ahead: a no-RTC board after power loss.
    state["deadline"] = time.time() + 3600.0
    state["recovery_attempts"] = module.MAX_RECOVERY_ATTEMPTS - 1
    anchors = (time.time() + 7200.0,)
    calls = []
    monkeypatch.setattr(
        module, "_rollback", lambda *args, **kwargs: calls.append(args)
    )
    code = module._finish_or_rollback(program, data, pending, state, anchors)
    assert code == 1
    assert len(calls) == 1

    # With a healthy clock the same future deadline keeps the window open.
    state["deadline"] = time.time() + 3600.0
    state["recovery_attempts"] = 0
    calls.clear()
    monkeypatch.setattr(module, "_service_runs_target", lambda *args, **kwargs: True)
    assert module._finish_or_rollback(program, data, pending, state, ()) == 0
    assert calls == []
