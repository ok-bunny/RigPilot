"""A failed transient reset must close before the old miner may restart."""
from __future__ import annotations

from pathlib import Path

import pytest

import agent.managed_miners as miners


@pytest.mark.parametrize("cleanup", ["stopped", "already_absent", "unconfirmed"])
@pytest.mark.parametrize("restart_on_failure", [True, False])
def test_failed_reset_only_restarts_old_miner_after_confirmed_cleanup(
    monkeypatch, tmp_path: Path, cleanup: str, restart_on_failure: bool
):
    manager = object.__new__(miners.ManagedMinerManager)
    events: list[tuple] = []
    stop_calls = 0
    reset_started = False
    cleanup_closed = False
    original = {"flight_sheet_id": "saved-old-flight"}
    transient = {**original, "gpu_oc": None, "_reset_srb_gpu_oc": True}
    prepared = {
        "runtime": tmp_path / "runtime",
        "api_port": 43001,
        "gpu_mapping": [{"vendor": "nvidia"}],
        "executable": tmp_path / "SRBMiner-MULTI.exe",
        "argv": ["SRBMiner-MULTI.exe", "--gpu-reset-oc"],
    }

    def stop_previous(_runtime, **_kwargs):
        nonlocal stop_calls, cleanup_closed
        stop_calls += 1
        if stop_calls == 1:
            events.append(("old_stopped",))
            return True
        assert reset_started
        if cleanup == "unconfirmed":
            events.append(("reset_cleanup_unconfirmed",))
            raise miners.ManagedMinerError("transient exact exit was not confirmed")
        cleanup_closed = True
        events.append(("reset_closed", cleanup))
        # False can mean verified absence, not a failed stop. Both normal
        # returns are safe here; a thrown error is the unconfirmed case.
        return cleanup == "stopped"

    def apply_single(document, **kwargs):
        nonlocal reset_started
        if document is transient:
            reset_started = True
            events.append(("reset_launched",))
            return {"pid": 101}
        assert document is original
        events.append(("old_restarted", cleanup_closed, kwargs["stop_previous"]))
        return {"pid": 202}

    def fail_readiness(*_args, **_kwargs):
        assert reset_started
        events.append(("reset_readiness_failed",))
        raise miners.ManagedMinerError("transient readiness failed")

    monkeypatch.setattr(manager, "_stop_previous", stop_previous)
    monkeypatch.setattr(manager, "_snapshot_srb_oc_reset_logs", lambda _runtime: {})
    monkeypatch.setattr(manager, "_apply_single", apply_single)
    monkeypatch.setattr(manager, "_wait_for_srb_oc_reset_ready", fail_readiness)
    monkeypatch.setattr(miners, "managed_argv_fingerprint", lambda *_a, **_k: "reset-fingerprint")

    with pytest.raises(miners.ManagedMinerError, match="transient readiness failed") as failure:
        manager._clear_srb_gpu_oc_prepared(
            original, transient, prepared, restart_on_failure=restart_on_failure
        )

    assert stop_calls == 2
    restarted = [event for event in events if event[0] == "old_restarted"]
    expected_restart = restart_on_failure and cleanup != "unconfirmed"
    assert bool(restarted) == expected_restart
    if restarted:
        assert restarted == [("old_restarted", True, False)]
        assert events.index(("reset_closed", cleanup)) < events.index(restarted[0])
    if cleanup == "unconfirmed":
        assert "transient exact exit was not confirmed" in str(failure.value)
        assert not cleanup_closed
    else:
        assert cleanup_closed
