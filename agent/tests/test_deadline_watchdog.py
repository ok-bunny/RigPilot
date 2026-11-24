from __future__ import annotations

import threading
import time
from unittest import mock

import pytest

from agent.config import AgentConfig
from agent.deadline_watchdog import DeadlineWatchdog, ExpiredDeadline
from agent.runtime import AgentRuntime, HEARTBEAT_STALL_EXIT_CODE
from agent.state import AgentState


@pytest.fixture(autouse=True)
def isolate_runtime_transport_and_telemetry(monkeypatch):
    # These tests exercise watchdog leases, not environment proxy discovery or
    # inventory collection. Keep runtime startup and heartbeat fixtures local.
    monkeypatch.setattr("agent.runtime.RigPilotClient", mock.Mock())
    monkeypatch.setattr(
        "agent.runtime.HostDataSampler",
        lambda _collector: mock.Mock(snapshot=lambda: {"inventory": {}}),
    )


def test_watchdog_fires_once_after_wall_clock_deadline() -> None:
    fired = threading.Event()
    expirations: list[ExpiredDeadline] = []

    def on_expired(item: ExpiredDeadline) -> None:
        expirations.append(item)
        fired.set()

    with DeadlineWatchdog(on_expired) as watchdog:
        token = watchdog.arm("heartbeat_http", 0.05)
        assert fired.wait(1.0)
        assert watchdog.disarm(token) is False

    assert len(expirations) == 1
    assert expirations[0].token == token
    assert expirations[0].phase == "heartbeat_http"
    assert expirations[0].timeout_seconds == pytest.approx(0.05)
    assert expirations[0].elapsed_seconds >= 0.04


def test_disarm_prevents_expiry_callback() -> None:
    fired = threading.Event()
    watchdog = DeadlineWatchdog(lambda _item: fired.set())
    token = watchdog.arm("command_result_http", 0.05)

    assert watchdog.disarm(token) is True
    assert watchdog.disarm(token) is False
    assert not fired.wait(0.15)
    watchdog.close()


def test_old_token_cannot_cancel_newer_lease() -> None:
    fired = threading.Event()
    expirations: list[ExpiredDeadline] = []

    def on_expired(item: ExpiredDeadline) -> None:
        expirations.append(item)
        fired.set()

    with DeadlineWatchdog(on_expired) as watchdog:
        old_token = watchdog.arm("old", 0.2)
        assert watchdog.disarm(old_token) is True
        new_token = watchdog.arm("new", 0.05)
        assert watchdog.disarm(old_token) is False
        assert fired.wait(1.0)

    assert [item.token for item in expirations] == [new_token]


def test_close_discards_armed_deadlines_and_rejects_new_ones() -> None:
    fired = threading.Event()
    watchdog = DeadlineWatchdog(lambda _item: fired.set())
    watchdog.arm("heartbeat_http", 0.05)
    watchdog.close()

    time.sleep(0.1)
    assert not fired.is_set()
    with pytest.raises(RuntimeError, match="closed"):
        watchdog.arm("heartbeat_http", 1.0)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_arm_rejects_invalid_deadlines(timeout: float) -> None:
    with DeadlineWatchdog(lambda _item: None) as watchdog:
        with pytest.raises(ValueError):
            watchdog.arm("heartbeat_http", timeout)


def test_runtime_enables_watchdog_only_for_linux_systemd(
    tmp_path, monkeypatch
) -> None:
    factory = mock.Mock()
    watchdog = mock.Mock()
    factory.return_value = watchdog
    monkeypatch.setattr("agent.runtime.platform.system", lambda: "Linux")
    monkeypatch.setenv("INVOCATION_ID", "systemd-invocation")
    monkeypatch.setattr("agent.runtime.DeadlineWatchdog", factory)

    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )

    assert runtime._heartbeat_watchdog is watchdog
    factory.assert_called_once_with(runtime._heartbeat_deadline_expired)


@pytest.mark.parametrize(
    ("system_name", "invocation_id"),
    [("Windows", "systemd-invocation"), ("Linux", "")],
)
def test_runtime_disables_watchdog_without_linux_systemd(
    tmp_path, monkeypatch, system_name: str, invocation_id: str
) -> None:
    factory = mock.Mock()
    monkeypatch.setattr("agent.runtime.platform.system", lambda: system_name)
    if invocation_id:
        monkeypatch.setenv("INVOCATION_ID", invocation_id)
    else:
        monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr("agent.runtime.DeadlineWatchdog", factory)

    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )

    assert runtime._heartbeat_watchdog is None
    factory.assert_not_called()


def test_runtime_watchdog_start_failure_does_not_block_agent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("agent.runtime.platform.system", lambda: "Linux")
    monkeypatch.setenv("INVOCATION_ID", "systemd-invocation")
    monkeypatch.setattr(
        "agent.runtime.DeadlineWatchdog",
        mock.Mock(side_effect=RuntimeError("thread unavailable")),
    )

    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )

    assert runtime._heartbeat_watchdog is None


def test_runtime_arms_only_around_controller_heartbeat(tmp_path) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            request_timeout=20,
        )
    )
    watchdog = mock.Mock()
    watchdog.arm.return_value = 41
    runtime._heartbeat_watchdog = watchdog
    runtime.client = mock.Mock()
    runtime.client.heartbeat.return_value = []
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    try:
        assert runtime.heartbeat_once(state) == 0
    finally:
        runtime._host_data_sampler.close(timeout=1)

    watchdog.arm.assert_called_once_with("heartbeat_http", 90.0)
    watchdog.disarm.assert_called_once_with(41)


def test_runtime_disarms_watchdog_when_heartbeat_raises(tmp_path) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    watchdog = mock.Mock()
    watchdog.arm.return_value = 9
    runtime._heartbeat_watchdog = watchdog
    runtime.client = mock.Mock()
    runtime.client.heartbeat.side_effect = RuntimeError("network failed")
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    try:
        with pytest.raises(RuntimeError, match="network failed"):
            runtime.heartbeat_once(state)
    finally:
        runtime._host_data_sampler.close(timeout=1)

    watchdog.disarm.assert_called_once_with(9)


def test_runtime_normal_cleanup_closes_watchdog(tmp_path) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    watchdog = mock.Mock()
    runtime._heartbeat_watchdog = watchdog
    systemd_watchdog = mock.Mock()
    runtime._systemd_watchdog = systemd_watchdog
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "heartbeat_once", return_value=0),
    ):
        runtime.run(once=True)

    watchdog.close.assert_called_once_with()
    assert runtime._heartbeat_watchdog is None
    systemd_watchdog.close.assert_called_once_with()


@pytest.mark.parametrize("command_id,action,lease_seconds", [
    ("update-1", "agent_update.v1", 1200.0),
    ("flight-1", "apply_flight_sheet", 420.0),
])
def test_runtime_uses_bounded_systemd_lease_for_command(
    tmp_path, command_id: str, action: str, lease_seconds: float,
) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    systemd_watchdog = mock.MagicMock()
    runtime._systemd_watchdog = systemd_watchdog
    runtime.client = mock.Mock()
    raw = {"id": command_id, "action": action, "payload": {}}
    runtime.client.heartbeat.return_value = [raw]
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    try:
        with (
            mock.patch.object(runtime, "_host_data", return_value={}),
            mock.patch.object(runtime, "_reconcile_pending_agent_update"),
            mock.patch.object(runtime, "_handle_command") as handle,
        ):
            assert runtime.heartbeat_once(state) == 1
    finally:
        runtime._host_data_sampler.close(timeout=1)

    systemd_watchdog.lease.assert_called_once_with(lease_seconds)
    handle.assert_called_once_with(state, raw)


def test_authenticated_heartbeat_submits_supervision_before_commands(tmp_path) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    runtime.client = mock.Mock()
    raw = {"id": "flight-1", "action": "apply_flight_sheet", "payload": {}}
    runtime.client.heartbeat.return_value = [raw]
    runtime._allow_supervision_repair = True
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    order: list[str] = []
    host = {
        "inventory": {
            "_heartbeat_maintenance": {"supervision_repair": True}
        }
    }

    try:
        with (
            mock.patch.object(runtime, "_host_data", return_value=host),
            mock.patch.object(runtime, "_reconcile_pending_agent_update"),
            mock.patch.object(
                runtime,
                "_maybe_submit_supervision_policy_repair",
                side_effect=lambda: order.append("repair"),
            ),
            mock.patch.object(
                runtime,
                "_handle_command",
                side_effect=lambda *_args: order.append("command"),
            ),
        ):
            assert runtime.heartbeat_once(state) == 1
    finally:
        runtime._host_data_sampler.close(timeout=1)

    assert order == ["repair", "command"]


def test_supervision_bootstrap_disk_path_has_its_own_wall_clock_watchdog(
    tmp_path,
) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            request_timeout=20,
        )
    )
    watchdog = mock.Mock()
    watchdog.arm.side_effect = [71, 72]
    runtime._heartbeat_watchdog = watchdog
    runtime._allow_supervision_repair = True
    runtime.client = mock.Mock()
    runtime.client.heartbeat.return_value = []
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    host = {
        "inventory": {
            "_heartbeat_maintenance": {
                "pending_agent_update": False,
                "rollback_receipt": False,
                "supervision_repair": True,
            }
        }
    }

    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch.object(runtime, "_host_data", return_value=host),
        mock.patch.object(runtime, "_maybe_submit_supervision_policy_repair") as repair,
    ):
        assert runtime.heartbeat_once(state) == 0

    repair.assert_called_once_with()
    assert watchdog.arm.call_args_list == [
        mock.call("heartbeat_http", 90.0),
        mock.call("supervision_policy_repair", 90.0),
    ]
    assert watchdog.disarm.call_args_list == [mock.call(71), mock.call(72)]


def test_runtime_supervises_success_logging_and_post_heartbeat_maintenance(
    tmp_path,
) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            request_timeout=20,
        )
    )
    watchdog = mock.Mock()
    watchdog.arm.return_value = 51
    runtime._heartbeat_watchdog = watchdog
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "heartbeat_once", return_value=0),
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch("agent.runtime.reconcile_confirmed_release_root", return_value=False),
    ):
        runtime.run(once=True)

    watchdog.arm.assert_called_once_with("post_heartbeat_maintenance", 90.0)
    watchdog.disarm.assert_called_once_with(51)


def test_runtime_does_not_supervise_in_memory_heartbeat_interval_wait(tmp_path) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            request_timeout=20,
        )
    )
    watchdog = mock.Mock()
    watchdog.arm.return_value = 61
    runtime._heartbeat_watchdog = watchdog
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    state.heartbeat_interval = 10

    def finish_wait(_delay: float) -> bool:
        runtime.stop_event.set()
        return True

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_start_optional_services"),
        mock.patch.object(runtime, "_stop_optional_services"),
        mock.patch.object(runtime, "heartbeat_once", return_value=0),
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch.object(runtime, "_wait_for_stop", side_effect=finish_wait),
        mock.patch("agent.runtime.reconcile_confirmed_release_root", return_value=False),
    ):
        runtime.run()

    watchdog.arm.assert_called_once_with("post_heartbeat_maintenance", 90.0)
    watchdog.disarm.assert_called_once_with(61)


def test_runtime_startup_does_not_synchronously_probe_supervision_disk(tmp_path) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    state.heartbeat_interval = 10

    def finish_wait(_delay: float) -> bool:
        runtime.stop_event.set()
        return True

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_start_optional_services"),
        mock.patch.object(runtime, "_stop_optional_services"),
        mock.patch.object(runtime, "heartbeat_once", return_value=0),
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch.object(runtime, "_maybe_submit_supervision_policy_repair") as repair,
        mock.patch.object(runtime, "_wait_for_stop", side_effect=finish_wait),
        mock.patch("agent.runtime.reconcile_confirmed_release_root", return_value=False),
    ):
        runtime.run()

    # The HostDataSampler worker supplies a repair hint to the real
    # heartbeat_once implementation.  Startup itself must remain disk-free so
    # a degraded HDD cannot prevent the first authenticated heartbeat.
    repair.assert_not_called()


def test_handled_network_failure_renews_external_watchdog_lease(tmp_path) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    systemd_watchdog = mock.Mock()
    runtime._systemd_watchdog = systemd_watchdog
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    def finish_wait(_delay: float) -> bool:
        runtime.stop_event.set()
        return True

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_start_optional_services"),
        mock.patch.object(runtime, "_stop_optional_services"),
        mock.patch.object(runtime, "heartbeat_once", side_effect=RuntimeError("offline")),
        mock.patch.object(runtime, "_wait_for_stop", side_effect=finish_wait),
    ):
        runtime.run()

    assert mock.call(90.0) in systemd_watchdog.progress.call_args_list
    systemd_watchdog.close.assert_called_once_with()


def test_runtime_stall_callback_uses_nonzero_exit_without_real_exit(
    monkeypatch,
) -> None:
    exits: list[int] = []
    write = mock.Mock(side_effect=AssertionError("deadline callback must not write"))
    monkeypatch.setattr("agent.runtime.os.write", write)
    monkeypatch.setattr("agent.runtime.os._exit", lambda code: exits.append(code))

    AgentRuntime._heartbeat_deadline_expired(
        ExpiredDeadline(
            token=1,
            phase="heartbeat_http",
            timeout_seconds=30,
            elapsed_seconds=30.5,
        )
    )

    assert exits == [HEARTBEAT_STALL_EXIT_CODE]
    write.assert_not_called()
