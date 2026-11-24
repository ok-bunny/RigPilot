"""Delayed secondary-leg start for independent dual mining (Agent 0.8.53).

Covers the product semantics requested on 2026-11-01: the primary leg starts
first, the secondary leg follows after a fixed delay (30 s shipped default),
the delay is immediately interruptible by stop/switch commands without
blocking them, the pending window is a transitional "starting" state for
heartbeat collection, and a pending start outlives an Agent restart through
its durable marker.

All boundaries are controlled: executables are synthetic bytes, processes are
fakes, no miner, socket, GPU or device operation is performed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.inventory as inventory
import agent.managed_miners as managed_miners_module
from agent.managed_miners import (
    ManagedMinerManager,
    cancel_deferred_secondary_start,
    load_pending_secondary_marker,
    secondary_leg_start_delay_seconds,
    wait_for_deferred_secondary_start,
)
from agent.tests.test_managed_miners import (
    FakePopen,
    FakeProcess,
    independent_document,
    leg,
    managed_config,
)


def _dual_flight(
    xmrig_root: Path,
    pearl_root: Path,
    *,
    sheet_id: str = "sheet-dual",
    fingerprint: str | None = None,
):
    value = independent_document(xmrig_root, pearl_root)
    value["flight_sheet_id"] = sheet_id
    if fingerprint is not None:
        value["runtime_fingerprint"] = fingerprint
    return value


class _Launcher:
    """Controlled popen/process boundary for both independent roles."""

    def __init__(self, *, fail_secondary: bool = False):
        self.processes: dict[int, FakeProcess] = {}
        self.argv: list[list[str]] = []
        self.next_pid = iter((701, 702, 703, 704, 705, 706))
        self.next_port = iter((24567, 24568, 24569, 24570, 24571, 24572))
        self.fail_secondary = fail_secondary
        self.secondary_launch_attempts = 0

    def popen(self, argv, **_kwargs):
        executable = Path(argv[0])
        if self.fail_secondary and executable.name == "miner.exe":
            self.secondary_launch_attempts += 1
            raise FileNotFoundError("controlled secondary launch failure")
        pid = next(self.next_pid)
        self.argv.append(list(argv))
        self.processes[pid] = FakeProcess(
            pid, executable.resolve(), 3000.0 + pid, argv
        )
        return FakePopen(pid)

    def manager(self, cfg):
        return ManagedMinerManager(
            cfg,
            popen_factory=self.popen,
            process_factory=self.processes.__getitem__,
            boot_time_factory=lambda: 0.0,
            port_allocator=lambda: next(self.next_port),
            system_name="Windows",
        )


@pytest.fixture()
def dual_env(tmp_path: Path):
    cfg = managed_config(tmp_path)
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    pearl_root = cfg.miner_packages_dir / "builtin-pearlminer-test-1.0"
    xmrig_root.mkdir()
    pearl_root.mkdir()
    (xmrig_root / "xmrig.exe").write_bytes(b"xmrig")
    (pearl_root / "miner.exe").write_bytes(b"pearl")
    return SimpleNamespace(
        cfg=cfg,
        xmrig_root=xmrig_root,
        pearl_root=pearl_root,
        runtime=cfg.managed_miner_runtime_dir,
    )


def _marker_path(runtime: Path) -> Path:
    return runtime / "secondary-start-pending.json"


def _secondary_record(env) -> dict | None:
    path = env.runtime / "secondary" / "managed-miner-process.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _primary_record(env) -> dict | None:
    path = env.runtime / "primary" / "managed-miner-process.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _commit_root_flight_sheet(env, dual) -> None:
    """Mirror the controller command path's atomic active-sheet commit."""

    stored = dict(dual)
    stored["applied_at"] = "2026-11-01T00:00:00+00:00"
    env.cfg.flight_sheet_file.write_text(
        json.dumps(stored), encoding="utf-8"
    )


def test_shipped_default_delay_is_30_seconds():
    # The autouse suite fixture pins the runtime default to 0, so the shipped
    # constant is asserted against the module source itself.
    source = Path(managed_miners_module.__file__).read_text(encoding="utf-8")
    assert "SECONDARY_LEG_START_DELAY_SECONDS = 30.0" in source
    managed_miners_module.SECONDARY_LEG_START_DELAY_SECONDS = 30.0
    try:
        assert secondary_leg_start_delay_seconds() == 30.0
    finally:
        managed_miners_module.SECONDARY_LEG_START_DELAY_SECONDS = 0.0
    managed_miners_module.SECONDARY_LEG_START_DELAY_SECONDS = -5.0
    try:
        assert secondary_leg_start_delay_seconds() == 0.0
    finally:
        managed_miners_module.SECONDARY_LEG_START_DELAY_SECONDS = 0.0


def test_secondary_starts_after_delay_and_completes_session(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 0.3
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)
    fingerprint = "a" * 64
    dual = _dual_flight(
        dual_env.xmrig_root, dual_env.pearl_root, fingerprint=fingerprint
    )

    started = time.monotonic()
    result = manager.apply(dual)
    apply_elapsed = time.monotonic() - started

    # apply() returns as soon as the primary leg is live; it never waits for
    # the secondary delay, so the command chain stays unblocked.
    assert apply_elapsed < 0.29, apply_elapsed
    assert result["independent"] is True
    assert len(launcher.argv) == 1, "only the primary leg launches at apply time"
    secondary = result["roles"]["secondary"]
    assert secondary["deferred_start"] is True
    assert secondary["delay_seconds"] == 0.3
    assert secondary["managed"] is True
    assert secondary["flight_sheet_id"] == "sheet-dual"
    # The controller's apply-result binding requires the runtime fingerprint
    # on every independent role result, deferred or not.
    assert secondary["runtime_fingerprint"] == fingerprint
    assert result["runtime_fingerprint"] == fingerprint
    assert result["roles"]["primary"]["runtime_fingerprint"] == fingerprint
    # The synthetic launcher is not detached, so both legs report the same
    # primary detachment answer here; the flag itself is not under test.
    assert result["survives_agent_restart"] is False
    assert _primary_record(dual_env) is not None
    assert _secondary_record(dual_env) is None
    assert load_pending_secondary_marker(dual_env.runtime) is not None
    assert _marker_path(dual_env.runtime).is_file()

    running = manager.running()
    assert running is not None
    assert running["complete"] is False
    assert running["secondary_start_pending"] is True
    assert set(running["roles"]) == {"primary"}

    assert wait_for_deferred_secondary_start(timeout=10.0) is True

    assert _secondary_record(dual_env) is not None
    assert _secondary_record(dual_env)["flight_sheet_id"] == "sheet-dual"
    assert not _marker_path(dual_env.runtime).exists()
    running = manager.running()
    assert running is not None and running["complete"] is True
    assert set(running["roles"]) == {"primary", "secondary"}
    assert len(launcher.argv) == 2
    ports = {
        _primary_record(dual_env)["api_port"],
        _secondary_record(dual_env)["api_port"],
    }
    assert len(ports) == 2


def test_stop_interrupts_pending_secondary_start_immediately(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 1.5
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)

    manager.apply(_dual_flight(dual_env.xmrig_root, dual_env.pearl_root))
    assert _marker_path(dual_env.runtime).is_file()

    started = time.monotonic()
    stopped = manager.stop()
    stop_elapsed = time.monotonic() - started

    # A stop command is answered immediately; it must never wait out the delay.
    assert stop_elapsed < 1.0, stop_elapsed
    assert stopped["stopped"] is True
    assert not _marker_path(dual_env.runtime).exists()
    assert all(process.terminated for process in launcher.processes.values())
    assert _secondary_record(dual_env) is None

    time.sleep(1.8)
    # The cancelled launch never fires afterwards.
    assert _secondary_record(dual_env) is None
    assert len(launcher.argv) == 1
    assert cancel_deferred_secondary_start("already-stopped") is False


def test_new_apply_replaces_previous_pending_start(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 0.8
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)

    manager.apply(
        _dual_flight(
            dual_env.xmrig_root, dual_env.pearl_root, sheet_id="sheet-one"
        )
    )
    manager.apply(
        _dual_flight(
            dual_env.xmrig_root, dual_env.pearl_root, sheet_id="sheet-two"
        )
    )

    marker = load_pending_secondary_marker(dual_env.runtime)
    assert marker is not None and marker["flight_sheet_id"] == "sheet-two"
    assert wait_for_deferred_secondary_start(timeout=10.0) is True

    assert _primary_record(dual_env)["flight_sheet_id"] == "sheet-two"
    assert _secondary_record(dual_env)["flight_sheet_id"] == "sheet-two"
    assert not _marker_path(dual_env.runtime).exists()


def test_delayed_secondary_failure_stops_whole_dual_session(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 0.3
    )
    launcher = _Launcher(fail_secondary=True)
    manager = launcher.manager(dual_env.cfg)

    manager.apply(_dual_flight(dual_env.xmrig_root, dual_env.pearl_root))
    assert wait_for_deferred_secondary_start(timeout=10.0) is True

    # One requested session: the healthy primary is not left running behind a
    # failed second leg.
    assert launcher.secondary_launch_attempts >= 1
    assert _secondary_record(dual_env) is None
    primary_record = _primary_record(dual_env)
    assert primary_record is None
    assert all(process.terminated for process in launcher.processes.values())
    assert not _marker_path(dual_env.runtime).exists()
    status = json.loads(
        (dual_env.runtime / "secondary" / "miner-status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["state"] == "failed"


def test_inventory_reports_starting_during_pending_window(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    import requests

    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 1.5
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)
    dual = _dual_flight(dual_env.xmrig_root, dual_env.pearl_root)
    manager.apply(dual)
    _commit_root_flight_sheet(dual_env, dual)
    try:
        def requester(_url, **_kwargs):
            raise requests.RequestException("controlled offline probe")

        status = inventory.collect_mining_status(
            dual_env.cfg,
            system_name="Windows",
            requester=requester,
            process_factory=launcher.processes.__getitem__,
            boot_time_factory=lambda: 0.0,
            process_iter_factory=lambda: (),
            gpu_metrics=[],
            runner=lambda *args, **kwargs: SimpleNamespace(
                returncode=3, stdout="inactive", stderr=""
            ),
        )
        assert status["state"] == "starting", status
        assert status["process_verified"] is None
        assert "secondary" not in status["role_errors"]
        assert status["running_roles"] == ["primary"]
    finally:
        manager.stop()
    assert not _marker_path(dual_env.runtime).exists()


def _simulate_agent_restart_keep_marker() -> None:
    """Stop the in-process waiter exactly like a process crash would."""
    with managed_miners_module._secondary_start_state_lock:
        handle = managed_miners_module._deferred_secondary_start_handle
        managed_miners_module._deferred_secondary_start_handle = None
    if handle is not None:
        handle.cancel_event.set()
        if handle.thread is not None:
            handle.thread.join(timeout=5.0)


def test_recovery_starts_secondary_when_deadline_passed(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 0.2
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)
    dual = _dual_flight(dual_env.xmrig_root, dual_env.pearl_root)
    manager.apply(dual)
    _commit_root_flight_sheet(dual_env, dual)
    _simulate_agent_restart_keep_marker()
    assert _marker_path(dual_env.runtime).is_file()
    time.sleep(0.25)

    fresh_manager = launcher.manager(dual_env.cfg)
    result = fresh_manager.recover_pending_secondary_start()
    assert result == {"recovered": True, "reason": "secondary_started"}
    assert _secondary_record(dual_env) is not None
    assert not _marker_path(dual_env.runtime).exists()
    running = fresh_manager.running()
    assert running is not None and running["complete"] is True


def test_recovery_reschedules_when_deadline_in_future(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 30.0
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)
    dual = _dual_flight(dual_env.xmrig_root, dual_env.pearl_root)
    manager.apply(dual)
    _commit_root_flight_sheet(dual_env, dual)
    _simulate_agent_restart_keep_marker()

    fresh_manager = launcher.manager(dual_env.cfg)
    result = fresh_manager.recover_pending_secondary_start()
    assert result == {"recovered": True, "reason": "secondary_rescheduled"}
    assert _secondary_record(dual_env) is None
    marker = load_pending_secondary_marker(dual_env.runtime)
    assert marker is not None and marker["flight_sheet_id"] == "sheet-dual"
    # Product delays are never re-imposed in full after a restart: only the
    # remaining time is armed.
    assert marker["delay_seconds"] <= 30.0

    fresh_manager.stop()
    assert not _marker_path(dual_env.runtime).exists()


def test_recovery_clears_marker_when_primary_not_running(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 30.0
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)
    dual = _dual_flight(dual_env.xmrig_root, dual_env.pearl_root)
    manager.apply(dual)
    _commit_root_flight_sheet(dual_env, dual)
    _simulate_agent_restart_keep_marker()

    # The primary session was stopped cleanly before the new Agent starts:
    # a stopped slot leaves no process record behind.
    (dual_env.runtime / "primary" / "managed-miner-process.json").unlink()

    fresh_manager = launcher.manager(dual_env.cfg)
    result = fresh_manager.recover_pending_secondary_start()
    assert result == {"recovered": True, "reason": "primary_not_running"}
    assert not _marker_path(dual_env.runtime).exists()
    assert _secondary_record(dual_env) is None


def test_shutdown_power_command_cancels_pending_start(
    dual_env, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 1.5
    )
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)
    manager.apply(_dual_flight(dual_env.xmrig_root, dual_env.pearl_root))
    assert _marker_path(dual_env.runtime).is_file()

    assert cancel_deferred_secondary_start("shutdown") is True
    assert not _marker_path(dual_env.runtime).exists()
    time.sleep(1.8)
    assert _secondary_record(dual_env) is None
    manager.stop()


def test_stale_or_invalid_markers_are_not_trusted(dual_env):
    runtime = dual_env.runtime
    runtime.mkdir(parents=True, exist_ok=True)
    # Missing marker.
    assert load_pending_secondary_marker(runtime) is None
    # Garbage marker.
    _marker_path(runtime).write_text("{not json", encoding="utf-8")
    assert load_pending_secondary_marker(runtime) is None
    # Expired marker (deadline far in the past).
    _marker_path(runtime).write_text(
        json.dumps(
            {
                "version": 1,
                "flight_sheet_id": "sheet-dual",
                "delay_seconds": 30.0,
                "scheduled_at": "2026-11-01T00:00:00+00:00",
                "deadline_epoch": time.time() - 10_000.0,
            }
        ),
        encoding="utf-8",
    )
    assert load_pending_secondary_marker(runtime) is None
    # Without any live role and without a valid pending marker there is no
    # session and no transitional view at all.
    dual = _dual_flight(dual_env.xmrig_root, dual_env.pearl_root)
    dual["applied_at"] = "2026-11-01T00:00:00+00:00"
    dual_env.cfg.flight_sheet_file.write_text(json.dumps(dual), encoding="utf-8")
    launcher = _Launcher()
    manager = launcher.manager(dual_env.cfg)
    assert manager.running() is None


# ---------------------------------------------------------------------------
# 0.8.60 defensive identity check: a stale unlink must not destroy a newer
# handle's marker even if a future change moves cancels off the main thread.
# ---------------------------------------------------------------------------


def _write_identity_marker(runtime: Path, flight_sheet_id: str) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    _marker_path(runtime).write_text(
        json.dumps(
            {
                "version": 1,
                "flight_sheet_id": flight_sheet_id,
                "delay_seconds": 30.0,
                "scheduled_at": "2026-11-08T00:00:00+00:00",
                "deadline_epoch": time.time() - 5.0,
            }
        ),
        encoding="utf-8",
    )


def test_marker_removal_skips_marker_owned_by_newer_handle(dual_env):
    runtime = dual_env.runtime
    _write_identity_marker(runtime, "sheet-new")

    from agent.managed_miners import _remove_pending_secondary_marker

    _remove_pending_secondary_marker(
        runtime, expected_flight_sheet_id="sheet-old"
    )
    # The newer handle's restart recovery survives.
    assert _marker_path(runtime).is_file()
    assert load_pending_secondary_marker(runtime) is not None


def test_marker_removal_deletes_marker_of_same_handle(dual_env):
    runtime = dual_env.runtime
    _write_identity_marker(runtime, "sheet-mine")

    from agent.managed_miners import _remove_pending_secondary_marker

    _remove_pending_secondary_marker(
        runtime, expected_flight_sheet_id="sheet-mine"
    )
    assert not _marker_path(runtime).exists()


def test_marker_removal_unconditional_for_unreadable_marker(dual_env):
    runtime = dual_env.runtime
    runtime.mkdir(parents=True, exist_ok=True)
    _marker_path(runtime).write_text("not json", encoding="utf-8")

    from agent.managed_miners import _remove_pending_secondary_marker

    _remove_pending_secondary_marker(
        runtime, expected_flight_sheet_id="sheet-any"
    )
    assert not _marker_path(runtime).exists()
