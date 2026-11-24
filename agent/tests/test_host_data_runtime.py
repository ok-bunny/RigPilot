from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from agent.config import AgentConfig
from agent.host_data import (
    HostDataSampler,
    ONCE_HOST_DATA_PREWARM_TIMEOUT_SECONDS,
    _normalized_snapshot,
    minimum_host_data,
)
from agent.runtime import AgentRuntime
from agent.state import AgentState, load_state, replacement_pending_path


AGENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def full_host_data(hostname: str = "rig-full") -> dict[str, object]:
    return {
        "hostname": hostname,
        "os_family": "Linux",
        "platform": "Linux-test",
        "ip_addresses": ["192.0.2.10"],
        "local_ip_addresses": ["192.0.2.10"],
        "public_ip_addresses": [],
        "metrics": {"cpu_percent": 12.5},
        "inventory": {"architecture": "x86_64"},
    }


class EnrollmentHeartbeatClient:
    def __init__(self) -> None:
        self.enrollment_hosts: list[dict[str, object]] = []
        self.heartbeat_hosts: list[dict[str, object]] = []

    def enroll(self, _token: str, host: dict[str, object], **pending) -> AgentState:
        self.enrollment_hosts.append(host)
        return AgentState.new(AGENT_ID, pending.get("proposed_agent_secret", "secret"))

    def heartbeat(
        self, _state: AgentState, host: dict[str, object]
    ) -> list[object]:
        self.heartbeat_hosts.append(host)
        return []


class ReplacementClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def replace_identity(
        self,
        token: str,
        host: dict[str, object],
        *,
        request_id: str,
        proposed_agent_secret: str,
    ) -> AgentState:
        self.calls.append(
            {
                "token": token,
                "host": host,
                "request_id": request_id,
                "proposed_agent_secret": proposed_agent_secret,
            }
        )
        return AgentState.new(AGENT_ID, proposed_agent_secret, 10)


def test_service_reports_stale_miner_without_using_retired_recovery_state(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "miner-runtime"
    runtime_dir.mkdir()
    old_state = runtime_dir / "self-heal-state.json"
    old_bytes = b'{"schema_version":1,"last_action":{"level":"l2_gpu_set_change"}}\n'
    old_state.write_bytes(old_bytes)
    runtime = AgentRuntime(AgentConfig(
        server_url="https://controller.test", state_file=tmp_path / "state.json",
        managed_mining_enabled=True, managed_miner_runtime_dir=runtime_dir,
        flight_sheet_file=tmp_path / "flight.json", miner_status_file=runtime_dir / "miner-status.json",
    ))
    state = AgentState.new(AGENT_ID, "test-secret")
    host = full_host_data()
    host["inventory"]["mining_status"] = {
        "configured": True, "state": "stale", "miner_type": "srbminer",
        "process_verified": False, "process_error": "managed miner process no longer exists",
    }
    opened_old_state = []
    real_open = Path.open

    def track_state(path, *args, **kwargs):
        if path == old_state:
            opened_old_state.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track_state)
    runtime.client = EnrollmentHeartbeatClient()
    monkeypatch.setattr(runtime, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(runtime, "ensure_enrolled", lambda *_: state)
    monkeypatch.setattr(runtime._host_data_sampler, "request_refresh", lambda: None)
    monkeypatch.setattr(runtime, "_host_data", lambda: host)
    monkeypatch.setattr(runtime, "_wait_for_stop", lambda _: runtime.request_stop())
    executor = mock.Mock()
    runtime.executor = executor
    runtime.run()

    assert len(runtime.client.heartbeat_hosts) == 1
    mining = runtime.client.heartbeat_hosts[0]["inventory"]["mining_status"]
    assert mining["state"] == "stale" and mining["process_verified"] is False
    assert "self_heal" not in mining
    executor.execute.assert_not_called()
    assert opened_old_state == []
    assert old_state.read_bytes() == old_bytes


def test_blocked_collection_uses_minimum_data_for_enrollment_and_heartbeat(
    tmp_path: Path, monkeypatch
):
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_collection(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        release.wait()
        return full_host_data()

    monkeypatch.setattr("agent.runtime.collect_host_data", blocked_collection)
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    client = EnrollmentHeartbeatClient()
    runtime.client = client

    try:
        with mock.patch("agent.runtime.save_state"):
            before = time.monotonic()
            state = runtime.ensure_enrolled("enrollment-token-for-test")
            enrollment_elapsed = time.monotonic() - before
        assert started.wait(1)
        assert enrollment_elapsed < 0.5
        assert client.enrollment_hosts == [minimum_host_data()]

        with (
            mock.patch(
                "agent.runtime.resume_pending_agent_update_miner",
                return_value=False,
            ),
            mock.patch("agent.runtime.note_pending_agent_heartbeat", return_value=None),
        ):
            before = time.monotonic()
            assert runtime.heartbeat_once(state) == 0
            heartbeat_elapsed = time.monotonic() - before
        assert heartbeat_elapsed < 0.5
        assert client.heartbeat_hosts == [minimum_host_data()]
        # Repeated refresh requests are coalesced behind the one blocked probe.
        assert calls == 1
    finally:
        release.set()
        assert runtime._host_data_sampler.close(timeout=1)


def test_device_inheritance_persists_new_identity_and_clears_pending_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "agent.runtime.collect_host_data",
        lambda *_args, **_kwargs: full_host_data(),
    )
    state_path = tmp_path / "state.json"
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=state_path,
        )
    )
    client = ReplacementClient()
    runtime.client = client
    try:
        inherited = runtime.ensure_enrolled(
            cli_replacement_token="rpr_" + "A" * 43
        )
        loaded = load_state(state_path)
        assert inherited.agent_id == AGENT_ID
        assert loaded is not None
        assert loaded.agent_id == AGENT_ID
        assert loaded.agent_secret == inherited.agent_secret
        assert len(client.calls) == 1
        assert not replacement_pending_path(state_path).exists()
    finally:
        assert runtime._host_data_sampler.close(timeout=1)


def test_linux_idle_heartbeat_skips_disk_maintenance_without_worker_hint(
    tmp_path: Path,
):
    """A wedged telemetry/disk worker cannot pull liveness down with it."""

    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    client = EnrollmentHeartbeatClient()
    runtime.client = client
    runtime._allow_supervision_repair = True
    state = AgentState.new(AGENT_ID, "secret")

    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch.object(runtime, "_host_data", return_value=minimum_host_data()),
        mock.patch.object(
            runtime,
            "_reconcile_pending_agent_update",
            side_effect=AssertionError("pending update touched disk"),
        ) as pending,
        mock.patch.object(
            runtime,
            "_maybe_submit_supervision_policy_repair",
            side_effect=AssertionError("supervision repair touched disk"),
        ) as supervision,
    ):
        before = time.monotonic()
        assert runtime.heartbeat_once(state) == 0
        assert time.monotonic() - before < 0.5

    pending.assert_not_called()
    supervision.assert_not_called()
    assert client.heartbeat_hosts == [minimum_host_data()]


def test_linux_worker_hint_preserves_pending_update_reconciliation(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    client = EnrollmentHeartbeatClient()
    runtime.client = client
    state = AgentState.new(AGENT_ID, "secret")
    host = full_host_data()
    host["inventory"] = {
        "_heartbeat_maintenance": {
            "pending_agent_update": True,
            "rollback_receipt": False,
            "remote_policy_repair": False,
            "supervision_repair": True,
        }
    }
    runtime._allow_supervision_repair = True

    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch.object(runtime, "_host_data", return_value=host),
        mock.patch.object(runtime, "_reconcile_pending_agent_update") as pending,
        mock.patch.object(
            runtime, "_maybe_submit_supervision_policy_repair"
        ) as supervision,
    ):
        assert runtime.heartbeat_once(state) == 0

    pending.assert_called_once_with(state)
    supervision.assert_called_once_with()


def test_unknown_worker_hint_never_authorizes_idle_main_thread_disk_scan(
    tmp_path: Path,
):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    client = EnrollmentHeartbeatClient()
    runtime.client = client
    runtime._allow_supervision_repair = True
    state = AgentState.new(AGENT_ID, "secret")
    host = full_host_data()
    host["inventory"] = {
        "_heartbeat_maintenance": {
            "pending_agent_update": "unknown",
            "rollback_receipt": "unknown",
            "remote_policy_repair": False,
            "supervision_repair": "unknown",
        }
    }

    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch.object(runtime, "_host_data", return_value=host),
        mock.patch.object(
            runtime,
            "_reconcile_pending_agent_update",
            side_effect=AssertionError("unknown hint touched update disk"),
        ) as pending,
        mock.patch.object(
            runtime,
            "_maybe_submit_supervision_policy_repair",
            side_effect=AssertionError("unknown hint touched supervision disk"),
        ) as supervision,
    ):
        assert runtime.heartbeat_once(state) == 0

    pending.assert_not_called()
    supervision.assert_not_called()


def test_true_maintenance_hint_is_sticky_across_unknown_worker_snapshot(
    tmp_path: Path,
):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    runtime._merge_heartbeat_maintenance_hints(
        {
            "pending_agent_update": True,
            "rollback_receipt": True,
            "remote_policy_repair": False,
            "supervision_repair": False,
        }
    )
    runtime._merge_heartbeat_maintenance_hints(
        {
            "pending_agent_update": "unknown",
            "rollback_receipt": "unknown",
        }
    )

    assert runtime._heartbeat_maintenance_hints["pending_agent_update"] is True
    assert runtime._heartbeat_maintenance_hints["rollback_receipt"] is True

    runtime._merge_heartbeat_maintenance_hints(
        {"pending_agent_update": False, "rollback_receipt": False}
    )
    assert runtime._heartbeat_maintenance_hints["pending_agent_update"] is False
    assert runtime._heartbeat_maintenance_hints["rollback_receipt"] is False


def test_last_good_snapshot_remains_available_when_next_probe_blocks():
    first_returned = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    calls = 0

    def collector() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_returned.set()
            return full_host_data("cached-rig")
        second_started.set()
        release_second.wait()
        return full_host_data("new-rig")

    sampler = HostDataSampler(collector)
    try:
        assert sampler.snapshot()["metrics"] == {}
        assert first_returned.wait(1)

        deadline = time.monotonic() + 1
        cached = sampler.snapshot()
        while cached["hostname"] != "cached-rig" and time.monotonic() < deadline:
            time.sleep(0.01)
            cached = sampler.snapshot()
        assert cached["hostname"] == "cached-rig"
        assert second_started.wait(1)

        worker = sampler.worker_thread
        assert worker is not None and worker.is_alive()
        for _ in range(25):
            assert sampler.snapshot()["hostname"] == "cached-rig"
            assert sampler.worker_thread is worker
        assert calls == 2
    finally:
        release_second.set()
        assert sampler.close(timeout=1)
    assert sampler.worker_thread is None


def test_stale_snapshot_drops_metrics_and_mining_but_preserves_identity():
    now = [100.0]
    first_returned = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()

    def collector() -> dict[str, object]:
        if not first_returned.is_set():
            first_returned.set()
            value = full_host_data("cached-rig")
            value["inventory"] = {
                "architecture": "x86_64",
                "mining_status": {"state": "running", "hashrate": 123.0},
            }
            return value
        second_started.set()
        release_second.wait()
        return full_host_data("new-rig")

    sampler = HostDataSampler(
        collector,
        max_age_seconds=45,
        clock=lambda: now[0],
    )
    try:
        sampler.snapshot()
        assert first_returned.wait(1)
        deadline = time.monotonic() + 1
        value = sampler.snapshot()
        while value["hostname"] != "cached-rig" and time.monotonic() < deadline:
            time.sleep(0.01)
            value = sampler.snapshot()
        assert value["inventory"]["mining_status"]["state"] == "running"
        assert second_started.wait(1)

        now[0] += 46
        stale = sampler.snapshot()
        assert stale["hostname"] == "cached-rig"
        assert stale["ip_addresses"] == ["192.0.2.10"]
        assert stale["local_ip_addresses"] == ["192.0.2.10"]
        assert stale["metrics"] == {}
        assert stale["inventory"] == {}
    finally:
        release_second.set()
        assert sampler.close(timeout=1)


def test_failed_refresh_does_not_evict_cached_snapshot():
    first_returned = threading.Event()
    failed = threading.Event()
    calls = 0

    def collector() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_returned.set()
            return full_host_data("stable-rig")
        failed.set()
        raise RuntimeError("sensor failed")

    sampler = HostDataSampler(collector)
    try:
        sampler.snapshot()
        assert first_returned.wait(1)
        deadline = time.monotonic() + 1
        value = sampler.snapshot()
        while value["hostname"] != "stable-rig" and time.monotonic() < deadline:
            time.sleep(0.01)
            value = sampler.snapshot()
        assert value["hostname"] == "stable-rig"
        assert failed.wait(1)
        assert sampler.snapshot()["hostname"] == "stable-rig"
    finally:
        assert sampler.close(timeout=1)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_snapshot_normalization_recursively_rejects_non_finite_numbers(invalid):
    value = full_host_data()
    value["metrics"] = {"nested": [{"invalid": invalid}]}

    with pytest.raises(ValueError, match="Out of range float values"):
        _normalized_snapshot(value)


def test_thread_start_failure_uses_fallback_and_does_not_pollute_worker():
    collector = mock.Mock(return_value=full_host_data())
    sampler = HostDataSampler(collector)

    with mock.patch.object(
        threading.Thread,
        "start",
        side_effect=RuntimeError("can't start new thread"),
    ):
        assert sampler.snapshot() == minimum_host_data()

    assert sampler.worker_thread is None
    assert sampler.close(timeout=0)
    collector.assert_not_called()


def test_close_does_not_join_an_unstarted_worker():
    sampler = HostDataSampler(lambda: full_host_data())
    unstarted = threading.Thread(target=lambda: None)
    sampler._thread = unstarted

    assert sampler.close(timeout=0)
    assert sampler.worker_thread is None


def test_run_once_joins_responsive_host_data_worker(tmp_path: Path):
    collected = threading.Event()

    def collector() -> dict[str, object]:
        collected.set()
        return full_host_data()

    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    runtime._host_data_sampler = HostDataSampler(collector)
    runtime._host_data()
    assert collected.wait(1)
    state = AgentState.new(AGENT_ID, "secret")

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch.object(runtime, "heartbeat_once", return_value=0),
    ):
        runtime.run(once=True)

    assert runtime._host_data_sampler.worker_thread is None


def test_run_once_local_prewarm_is_not_truncated_by_short_http_timeout(
    tmp_path: Path,
):
    """A slow bounded Windows probe still supplies enrollment inventory.

    The release smoke config intentionally uses a short HTTP request timeout.
    Before the regression fix that unrelated value also capped local telemetry
    prewarming, so enrollment raced ahead with the minimum snapshot and an
    empty CPU model.  The short delay below straddles the former 0.1-second
    floor without making the test wait for the real 12-second Windows path.
    """

    started = threading.Event()
    release = threading.Event()

    def collector() -> dict[str, object]:
        started.set()
        assert release.wait(1)
        value = full_host_data()
        value["inventory"] = {
            "architecture": "x86_64",
            "processor": "Expected Windows CPU",
        }
        return value

    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            request_timeout=0.05,
        )
    )
    runtime._host_data_sampler = HostDataSampler(collector)
    client = EnrollmentHeartbeatClient()
    runtime.client = client
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        with (
            mock.patch.object(runtime, "install_signal_handlers"),
            mock.patch("agent.runtime.save_state"),
            mock.patch.object(runtime, "heartbeat_once", return_value=0),
        ):
            runtime.run("enrollment-token-for-test", once=True)
    finally:
        release.set()
        timer.cancel()

    assert started.is_set()
    assert (
        ONCE_HOST_DATA_PREWARM_TIMEOUT_SECONDS >= 3 * 4.0
    ), "prewarm budget must cover helper plus both WMI probe timeouts"
    assert client.enrollment_hosts[0]["inventory"]["processor"] == (
        "Expected Windows CPU"
    )
    assert runtime._host_data_sampler.worker_thread is None


def test_stop_before_collection_never_starts_a_worker():
    collector = mock.Mock(return_value=full_host_data())
    sampler = HostDataSampler(collector)

    sampler.request_stop()

    assert sampler.snapshot() == minimum_host_data()
    assert sampler.worker_thread is None
    collector.assert_not_called()


def test_bounded_first_snapshot_wait_never_hangs_on_blocked_probe():
    started = threading.Event()
    release = threading.Event()

    def collector() -> dict[str, object]:
        started.set()
        release.wait()
        return full_host_data()

    sampler = HostDataSampler(collector)
    try:
        before = time.monotonic()
        assert sampler.wait_for_first(0.02) is False
        assert time.monotonic() - before < 0.25
        assert started.is_set()
        assert sampler.snapshot() == minimum_host_data()
    finally:
        release.set()
        assert sampler.close(timeout=1)
