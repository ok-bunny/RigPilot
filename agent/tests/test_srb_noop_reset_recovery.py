"""Exercise no-op reset receipts through real failed-flight recovery paths."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import agent.commands as commands
import agent.managed_miners as miners
from agent.config import AgentConfig


RESET_RESULTS = [
    pytest.param({"required": False, "cleared": False}, True, id="explicit-noop"),
    pytest.param({"cleared": True}, True, id="legacy-cleared"),
    pytest.param({"required": True, "cleared": True}, True, id="required-cleared"),
    pytest.param({}, False, id="missing-both"),
    pytest.param({"required": False}, False, id="missing-cleared"),
    pytest.param({"cleared": False}, False, id="missing-required-for-noop"),
    pytest.param({"required": False, "cleared": None}, False, id="null-cleared"),
    pytest.param({"required": 0, "cleared": False}, False, id="nonbool-required"),
    pytest.param({"required": False, "cleared": 0}, False, id="nonbool-cleared"),
    pytest.param({"cleared": "true"}, False, id="truthy-cleared-string"),
    pytest.param(None, False, id="nonmapping-null"),
    pytest.param(True, False, id="nonmapping-bool"),
    pytest.param([("required", False), ("cleared", False)], False, id="nonmapping-pairs"),
]


def _target_no_oc():
    return {
        "flight_sheet_id": "target-flight",
        "worker_name": "fixture",
        "primary": {
            "miner_profile": "srb-fixture",
            "miner_type": "srbminer",
            "algorithm": "ethash",
            "miner_package": None,
            "execution_mode": "native",
        },
        "secondary": None,
        "gpu_oc": None,
    }


def _config(tmp_path: Path):
    return AgentConfig(
        server_url="https://controller.example.test",
        state_file=tmp_path / "state.json",
        flight_sheet_file=tmp_path / "active-flight-sheet.json",
        managed_mining_enabled=True,
        managed_miner_runtime_dir=tmp_path / "miner-runtime",
        allow_gpu_oc=True,
    )


@pytest.mark.parametrize("reset_result,satisfied", RESET_RESULTS)
def test_command_failed_flight_rollback_accepts_only_valid_reset_contract(
    monkeypatch, tmp_path: Path, reset_result, satisfied: bool
):
    cfg = _config(tmp_path)
    old_bytes = b'{"flight_sheet_id":"old-flight"}'
    cfg.flight_sheet_file.write_bytes(old_bytes)
    target = _target_no_oc()
    events: list[str] = []

    class Manager:
        phase = None

        def snapshot_independent_metadata(self):
            return {}

        def transition_has_live_processes(self):
            return True

        def begin_flight_transition(self, _document, *, previous_was_running):
            assert previous_was_running is True
            self.phase = "prepared"

        def mark_flight_transition_phase(self, _flight_id, phase):
            self.phase = phase

        def apply(self, _document):
            raise commands.ManagedMinerTransitionError(
                "target launch failed",
                state_changed=True,
                old_stopped=True,
                target_started=True,
                target_oc_may_have_applied=True,
                target_cleanup_unconfirmed=False,
            )

        def clear_srb_gpu_oc_for_document(self, _document, *, restart_on_failure):
            assert restart_on_failure is False
            events.append("target-clear")
            return copy.deepcopy(reset_result)

        def stop(self):
            events.append("target-stop")
            return {"stopped": True}

        def restore_independent_metadata(self, _snapshot):
            events.append("metadata-restore")

        def restart(self):
            events.append("old-restart")
            return {"restarted": True}

        def finish_flight_transition(self, _flight_id):
            self.phase = "finished"
            events.append("journal-finish")

    manager = Manager()
    monkeypatch.setattr(commands, "validate_flight_sheet", lambda *_a, **_k: target)
    monkeypatch.setattr(commands, "_active_flight_uses_srb_gpu_oc", lambda *_a, **_k: False)
    monkeypatch.setattr(commands.platform, "system", lambda: "Windows")
    executor = commands.CommandExecutor(cfg, managed_miner_factory=lambda _cfg: manager)
    outcome = executor.execute(commands.Command("target-command", "apply_flight_sheet", {}))

    # Target launch still failed: a satisfied reset only permits restoration,
    # never turns the failed target command into a success.
    assert outcome.status == "failed"
    assert "target launch failed" in outcome.error
    assert events.count("target-clear") == 1
    assert events.count("target-stop") == 1
    assert cfg.flight_sheet_file.read_bytes() == old_bytes
    assert ("old-restart" in events) is satisfied
    assert ("journal-finish" in events) is satisfied
    assert manager.phase == ("finished" if satisfied else "mutating")
    if satisfied:
        assert events.index("target-stop") < events.index("old-restart")
        assert "rollback incomplete" not in outcome.error
    else:
        assert "rollback incomplete" in outcome.error


@pytest.mark.parametrize("reset_result,satisfied", RESET_RESULTS)
def test_interrupted_flight_recovery_accepts_only_valid_reset_contract(
    monkeypatch, tmp_path: Path, reset_result, satisfied: bool
):
    cfg = _config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(mode=0o700)
    target = _target_no_oc()
    journal = runtime / "flight-transition.json"
    journal_bytes = json.dumps({
        "version": 1,
        "target_flight_sheet_id": "target-flight",
        "previous_flight_sheet_id": "old-flight",
        "phase": "mutating",
        "target_document": target,
        "previous_was_running": True,
    }).encode()
    journal.write_bytes(journal_bytes)
    pending = cfg.flight_sheet_file.with_name(f".{cfg.flight_sheet_file.name}.pending")
    pending.write_bytes(b"pending target")
    manager = object.__new__(miners.ManagedMinerManager)
    manager.config = cfg
    events: list[str] = []

    def clear(_document, *, restart_on_failure):
        assert restart_on_failure is False
        events.append("target-clear")
        return copy.deepcopy(reset_result)

    def stop():
        events.append("target-stop")
        return {"stopped": True}

    def restart():
        events.append("old-restart")
        return {"restarted": True}

    def finish(flight_id):
        assert flight_id == "target-flight"
        events.append("journal-finish")
        journal.unlink()

    monkeypatch.setattr(manager, "_runtime", lambda: runtime)
    monkeypatch.setattr(manager, "_read_active_flight_sheet", lambda: {"flight_sheet_id": "old-flight"})
    monkeypatch.setattr(manager, "_live_transition_flight_sheet_ids", lambda: {"target-flight"})
    monkeypatch.setattr(manager, "clear_srb_gpu_oc_for_document", clear)
    monkeypatch.setattr(manager, "stop", stop)
    monkeypatch.setattr(manager, "restart", restart)
    monkeypatch.setattr(manager, "finish_flight_transition", finish)
    monkeypatch.setattr(miners.platform, "system", lambda: "Windows")

    if satisfied:
        result = manager.recover_interrupted_flight_transition()
        assert result == {"recovered": True, "reason": "previous_restarted"}
        assert events == ["target-clear", "target-stop", "old-restart", "journal-finish"]
        assert not journal.exists()
        assert not pending.exists()
    else:
        with pytest.raises(miners.ManagedMinerError, match="recovery was incomplete"):
            manager.recover_interrupted_flight_transition()
        assert events == ["target-clear", "target-stop"]
        assert journal.read_bytes() == journal_bytes
        assert pending.read_bytes() == b"pending target"
