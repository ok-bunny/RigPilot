from __future__ import annotations

import logging
from pathlib import Path
from unittest import mock

import pytest
import requests

from agent.agent_updates import (
    AGENT_UPDATE_ACTION,
    AgentUpdateError,
    resume_pending_agent_update_miner,
)
from agent.commands import Command, CommandOutcome
from agent.client import ControllerRequestError, ProtocolError
from agent.config import AgentConfig
from agent.host_data import minimum_host_data
from agent.managed_miners import ManagedMinerManager
from agent.runtime import AgentRuntime
from agent.state import AgentState, StateError, load_state


COMMAND_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
RELEASE_ID = "12345678-1234-4123-8123-123456789abc"


def update_command() -> Command:
    return Command(
        COMMAND_ID,
        AGENT_UPDATE_ACTION,
        {
            "release_id": RELEASE_ID,
            "version": "0.5.8",
            "sha256": "a" * 64,
            "size": 100,
            "download_url": f"/api/agent/updates/{COMMAND_ID}/download",
        },
    )


class RecordingClient:
    def __init__(self):
        self.results = []

    def post_result(self, _state, command_id, response):
        self.results.append((command_id, response))
        return response["status"]

    def heartbeat(self, _state, _host_data):
        return []


class RecordingLoopEvent:
    """Record sliced waits while a test wrapper bounds heartbeat attempts."""

    def __init__(self):
        self.delays: list[float] = []
        self.explicitly_stopped = False

    def is_set(self) -> bool:
        return self.explicitly_stopped

    def set(self) -> None:
        self.explicitly_stopped = True

    def wait(self, delay: float) -> bool:
        self.delays.append(delay)
        return self.is_set()


def run_runtime_cycles(
    runtime: AgentRuntime, state: AgentState, cycles: int
) -> list[float]:
    stop_event = RecordingLoopEvent()
    runtime.stop_event = stop_event
    heartbeat_once = runtime.heartbeat_once
    attempts = 0

    def bounded_heartbeat(current_state: AgentState) -> int:
        nonlocal attempts
        try:
            return heartbeat_once(current_state)
        finally:
            attempts += 1
            if attempts >= cycles:
                stop_event.set()

    with (
        mock.patch.object(runtime, "_host_data_sampler", mock.Mock(snapshot=minimum_host_data)),
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_start_optional_services"),
        mock.patch.object(runtime, "_stop_optional_services"),
        mock.patch.object(runtime, "heartbeat_once", side_effect=bounded_heartbeat),
    ):
        runtime.run()
    return stop_event.delays


def test_rejected_uuid_command_is_cached_reported_and_replayed(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    runtime.executor = mock.Mock()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    raw = {
        "id": COMMAND_ID,
        "action": "agent_update.v999",
        "payload": {},
    }

    runtime._handle_command(state, raw)
    first_response = runtime.client.results[0][1]
    runtime._handle_command(state, raw)

    assert first_response == {
        "status": "failed",
        "result": {"rejected": True},
        "error": "unsupported command action",
    }
    assert runtime.client.results == [
        (COMMAND_ID, first_response),
        (COMMAND_ID, first_response),
    ]
    assert state.command_history[COMMAND_ID]["response"] == first_response
    assert config.state_file.is_file()
    runtime.executor.execute.assert_not_called()


def test_linux_power_command_is_persisted_and_replayed_without_rescheduling(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
        allow_reboot=True,
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    raw = {"id": COMMAND_ID, "action": "reboot", "payload": {}}
    scheduled = []

    def runner(argv, **kwargs):
        scheduled.append((argv, kwargs))
        return mock.Mock(returncode=0, stdout="", stderr="")

    first_runtime = AgentRuntime(config)
    first_runtime.client = RecordingClient()
    first_runtime.executor.runner = runner
    with (
        mock.patch("agent.commands.platform.system", return_value="Linux"),
        mock.patch(
            "agent.commands._linux_trusted_system_executable",
            return_value=True,
        ),
        mock.patch("agent.commands.os.getpid", return_value=4321),
    ):
        first_runtime._handle_command(state, raw)

    assert len(scheduled) == 1
    assert [response["status"] for _, response in first_runtime.client.results] == [
        "running",
        "succeeded",
    ]
    restored = load_state(config.state_file)
    assert restored is not None
    assert restored.command_history[COMMAND_ID]["response"]["result"][
        "scheduled"
    ] is True

    restarted_runtime = AgentRuntime(config)
    restarted_runtime.client = RecordingClient()
    restarted_runtime.executor.runner = runner
    with (
        mock.patch("agent.commands.platform.system", return_value="Linux"),
        mock.patch(
            "agent.commands._linux_trusted_system_executable",
            return_value=True,
        ),
        mock.patch("agent.commands.os.getpid", return_value=9999),
    ):
        restarted_runtime._handle_command(restored, raw)

    assert len(scheduled) == 1
    assert [
        response["status"] for _, response in restarted_runtime.client.results
    ] == ["succeeded"]


def test_unexpected_command_exception_is_persisted_and_replayed_without_secret(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "agent-secret"
    )
    raw = {
        "id": COMMAND_ID,
        "action": "restart_service",
        "payload": {"service": "miner"},
    }
    sensitive_exception = "bearer rpa_super-secret-command-material"

    class FirstClient(RecordingClient):
        def post_result(self, _state, command_id, response):
            self.results.append((command_id, response))
            if response["status"] == "failed":
                raise requests.ConnectionError("result response was lost")
            return response["status"]

    first_runtime = AgentRuntime(config)
    first_runtime.client = FirstClient()
    first_runtime.executor = mock.Mock()
    first_runtime.executor.execute.side_effect = ValueError(sensitive_exception)

    with pytest.raises(requests.ConnectionError, match="response was lost"):
        first_runtime._handle_command(state, raw)

    restored = load_state(config.state_file)
    assert restored is not None
    failure = restored.command_history[COMMAND_ID]["response"]
    assert failure == {
        "status": "failed",
        "result": {"unexpected": True},
        "error": "unexpected local command execution failure",
    }
    assert sensitive_exception not in str(failure)

    restarted_runtime = AgentRuntime(config)
    restarted_runtime.client = RecordingClient()
    restarted_runtime.executor = mock.Mock()
    restarted_runtime._handle_command(restored, raw)

    restarted_runtime.executor.execute.assert_not_called()
    assert restarted_runtime.client.results == [(COMMAND_ID, failure)]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(7)])
def test_command_interrupts_are_not_converted_to_failed_outcomes(
    tmp_path: Path,
    interrupt: BaseException,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    runtime.executor = mock.Mock()
    runtime.executor.execute.side_effect = interrupt
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "agent-secret"
    )
    raw = {
        "id": COMMAND_ID,
        "action": "restart_service",
        "payload": {"service": "miner"},
    }

    with pytest.raises(type(interrupt)):
        runtime._handle_command(state, raw)

    assert state.command_history == {}
    assert not config.state_file.exists()


def test_rejected_command_without_uuid_is_not_acknowledged(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    runtime._handle_command(
        state,
        {"id": "legacy-id", "action": "agent_update.v999", "payload": {}},
    )

    assert runtime.client.results == []
    assert state.command_history == {}
    assert not config.state_file.exists()


def test_rejected_uuid_reuse_with_different_contents_is_terminal(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    runtime._handle_command(
        state,
        {"id": COMMAND_ID, "action": "agent_update.v999", "payload": {}},
    )

    runtime._handle_command(
        state,
        {
            "id": COMMAND_ID,
            "action": "agent_update.v998",
            "payload": {},
        },
    )

    assert runtime.client.results[-1] == (
        COMMAND_ID,
        {
            "status": "failed",
            "result": {"rejected": True},
            "error": "command id was reused with different invalid contents",
        },
    )


def test_cached_staged_update_replay_never_stops_restored_agent(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
        allow_agent_updates=True,
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    command = update_command()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    staged = {
        "status": "succeeded",
        "result": {"activation_scheduled": True, "activation_id": "a" * 32},
        "error": None,
    }
    state.command_history[COMMAND_ID] = {
        "fingerprint": command.fingerprint(),
        "response": staged,
        "completed_at": "2026-01-01T00:00:00Z",
    }

    with mock.patch("agent.runtime.platform.system", return_value="Windows"):
        runtime._handle_command(
            state,
            {"id": command.command_id, "action": command.action, "payload": command.payload},
        )

    assert runtime.client.results == [(COMMAND_ID, staged)]
    assert not runtime.stop_event.is_set()


def test_rollback_receipt_reports_terminal_failure_and_updates_cache(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    marker = tmp_path / "reported.json"
    failure = {
        "status": "failed",
        "result": {
            "activation_id": "a" * 32,
            "version": "0.5.8",
            "rolled_back": True,
        },
        "error": "replacement did not reconnect",
    }
    with (
        mock.patch(
            "agent.runtime.pending_rollback_results",
            return_value=[(marker, COMMAND_ID, failure)],
        ),
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update", return_value=False
        ) as resumed,
        mock.patch("agent.runtime.mark_rollback_reported") as marked,
    ):
        runtime._reconcile_update_rollbacks(state)

    assert runtime.client.results == [(COMMAND_ID, failure)]
    assert state.command_history[COMMAND_ID]["response"] == failure
    assert resumed.call_args_list == [
        mock.call(
            config,
            state,
            COMMAND_ID,
            "a" * 32,
            consume_marker=False,
        ),
        mock.call(
            config,
            state,
            COMMAND_ID,
            "a" * 32,
            consume_marker=True,
        ),
    ]
    marked.assert_called_once_with(marker, COMMAND_ID)
    assert not runtime.stop_event.is_set()


def test_final_activation_is_marked_reported_once_after_controller_accepts(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    activation = {
        "activation_id": "a" * 32,
        "command_id": COMMAND_ID,
        "version": "0.5.8",
        "agent_id": state.agent_id,
        "health_heartbeats": 2,
    }
    host = {
        "inventory": {
            "_heartbeat_maintenance": {"pending_agent_update": True}
        }
    }

    with (
        mock.patch.object(runtime, "_host_data", return_value=host),
        mock.patch(
            "agent.runtime.note_pending_agent_heartbeat",
            side_effect=[activation, None],
        ),
        mock.patch(
            "agent.runtime.resume_pending_agent_update_miner", return_value=False
        ) as resumed,
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update", return_value=False
        ) as consumed,
        mock.patch(
            "agent.runtime.confirm_pending_agent_update", return_value=True
        ) as confirmed,
        mock.patch(
            "agent.runtime.mark_activation_reported", return_value=True
        ) as reported,
        mock.patch("agent.runtime.save_state") as saved,
    ):
        assert runtime.heartbeat_once(state) == 0
        assert runtime.heartbeat_once(state) == 0

    assert len(runtime.client.results) == 1
    command_id, response = runtime.client.results[0]
    assert command_id == COMMAND_ID
    assert response["status"] == "succeeded"
    assert response["result"]["activation_confirmed"] is True
    confirmed.assert_called_once_with(config, state, activation)
    reported.assert_called_once_with(config, state, activation)
    assert resumed.call_count == 2
    consumed.assert_called_once_with(config, state, COMMAND_ID, "a" * 32)
    saved.assert_called_once()


def test_replacement_resumes_miner_only_after_controller_heartbeat_succeeds(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    order = []

    class Client(RecordingClient):
        def heartbeat(self, _state, _host_data):
            order.append("authenticated_heartbeat")
            return []

    runtime.client = Client()

    def resume(_config, _state):
        order.append("resume_miner")
        return True

    host = {
        "inventory": {
            "_heartbeat_maintenance": {"pending_agent_update": True}
        }
    }

    with (
        mock.patch.object(runtime, "_host_data", return_value=host),
        mock.patch(
            "agent.runtime.resume_pending_agent_update_miner",
            side_effect=resume,
        ),
        mock.patch(
            "agent.runtime.note_pending_agent_heartbeat", return_value=None
        ),
    ):
        assert runtime.heartbeat_once(state) == 0

    assert order == ["authenticated_heartbeat", "resume_miner"]


def test_ordinary_agent_heartbeat_never_restarts_mining_without_update_marker(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
        managed_mining_enabled=True,
        managed_miner_runtime_dir=tmp_path / "miner-runtime",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    manager_constructions = []
    restart_calls = []

    class Manager:
        def restart(self):
            restart_calls.append(True)
            return {"restarted": True}

    def manager_factory(_config: AgentConfig) -> Manager:
        manager_constructions.append(True)
        return Manager()

    def resume_only_bound_update(config_value: AgentConfig, state_value: AgentState):
        return resume_pending_agent_update_miner(
            config_value,
            state_value,
            managed_miner_factory=manager_factory,
        )

    with (
        mock.patch.object(runtime, "_host_data", return_value={}),
        mock.patch(
            "agent.runtime.resume_pending_agent_update_miner",
            side_effect=resume_only_bound_update,
        ),
        mock.patch("agent.runtime.note_pending_agent_heartbeat", return_value=None),
        mock.patch.object(ManagedMinerManager, "restart") as direct_restart,
    ):
        assert runtime.heartbeat_once(state) == 0

    assert manager_constructions == []
    assert restart_calls == []
    direct_restart.assert_not_called()


def test_stale_rollback_receipt_is_consumed_when_controller_already_succeeded(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    marker = tmp_path / "reported.json"
    failure = {
        "status": "failed",
        "result": {
            "activation_id": "a" * 32,
            "version": "0.5.8",
            "rolled_back": True,
        },
        "error": "replacement did not reconnect",
    }

    class AlreadySucceededClient(RecordingClient):
        def post_result(self, _state, command_id, response):
            self.results.append((command_id, response))
            return "succeeded"

    runtime.client = AlreadySucceededClient()
    with (
        mock.patch(
            "agent.runtime.pending_rollback_results",
            return_value=[(marker, COMMAND_ID, failure)],
        ),
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update", return_value=False
        ),
        mock.patch("agent.runtime.mark_rollback_reported") as marked,
        mock.patch("agent.runtime.save_state") as saved,
    ):
        runtime._reconcile_update_rollbacks(state)

    assert runtime.client.results == [(COMMAND_ID, failure)]
    marked.assert_called_once_with(marker, COMMAND_ID)
    saved.assert_not_called()
    assert COMMAND_ID not in state.command_history


def test_rollback_report_is_never_blocked_by_miner_resume_failure(
    tmp_path: Path,
):
    # suzong-class failure: the device rolled back and is online, but a
    # permanently failing miner resume used to stop the rolled_back result
    # from ever reaching the controller, wedging the job for the full
    # activation-timeout window.  Reporting is now decoupled; the retained
    # marker keeps being retried.
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    marker = tmp_path / "reported.json"
    failure = {
        "status": "failed",
        "result": {
            "activation_id": "a" * 32,
            "version": "0.5.8",
            "rolled_back": True,
        },
        "error": "replacement did not reconnect",
    }
    with (
        mock.patch(
            "agent.runtime.pending_rollback_results",
            return_value=[(marker, COMMAND_ID, failure)],
        ),
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update",
            side_effect=AgentUpdateError("temporary restart failure"),
        ),
        mock.patch("agent.runtime.mark_rollback_reported") as marked,
        mock.patch("agent.runtime.save_state") as saved,
    ):
        runtime._reconcile_update_rollbacks(state)

    assert runtime.client.results == [(COMMAND_ID, failure)]
    marked.assert_called_once_with(marker, COMMAND_ID)
    saved.assert_called_once()


def test_rollback_resume_marker_is_retried_after_report(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    with (
        mock.patch(
            "agent.runtime.pending_miner_resume_markers",
            return_value=[(COMMAND_ID, "a" * 32)],
        ) as scan,
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update",
            return_value=True,
        ) as resume,
    ):
        runtime._retry_pending_miner_resumes(state)

    scan.assert_called_once()
    resume.assert_called_once_with(
        runtime.config, state, COMMAND_ID, "a" * 32, consume_marker=True
    )


def test_activation_confirmation_terminal_result_stops_retry_loop(
    tmp_path: Path,
):
    # When the controller command was cancelled or timed out before this
    # confirmation arrived, retrying the POST forever would keep a healthy
    # replacement in the fast heartbeat window; the report is recorded
    # locally instead.
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    activation = {
        "activation_id": "a" * 32,
        "command_id": COMMAND_ID,
        "version": "0.5.8",
        "agent_id": state.agent_id,
        "health_heartbeats": 2,
    }

    class ExpiredClient(RecordingClient):
        def post_result(self, _state, command_id, response):
            self.results.append((command_id, response))
            return "expired"

    runtime.client = ExpiredClient()
    with (
        mock.patch(
            "agent.runtime.note_pending_agent_heartbeat",
            return_value=dict(activation),
        ),
        mock.patch(
            "agent.runtime.resume_pending_agent_update_miner",
            return_value=False,
        ),
        mock.patch(
            "agent.runtime.confirm_pending_agent_update",
            return_value=True,
        ),
        mock.patch(
            "agent.runtime.mark_activation_reported",
            return_value=True,
        ) as reported,
        mock.patch("agent.runtime.resume_managed_miner_after_update"),
        mock.patch("agent.runtime.save_state"),
    ):
        runtime._reconcile_pending_agent_update(state)

    reported.assert_called_once()
    assert runtime.client.results[0][0] == COMMAND_ID


def test_root_sync_os_error_does_not_block_agent_heartbeat(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch(
            "agent.runtime.reconcile_confirmed_release_root",
            side_effect=OSError("temporary root ACL failure"),
        ) as root_sync,
        mock.patch.object(runtime, "heartbeat_once", return_value=0) as heartbeat,
    ):
        run_runtime_cycles(runtime, state, 2)

    assert root_sync.call_count == 2
    assert heartbeat.call_args_list == [mock.call(state), mock.call(state)]

def _runtime_with_staged_outcome(tmp_path: Path, controller_status: str, error=None):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
        allow_agent_updates=True,
    )
    runtime = AgentRuntime(config)
    command = update_command()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    class Client(RecordingClient):
        def post_result(self, _state, command_id, response):
            self.results.append((command_id, response))
            if response["status"] == "running":
                return "running"
            if error is not None:
                raise error
            return controller_status

    runtime.client = Client()
    runtime.executor = mock.Mock()
    runtime.executor.execute.return_value = CommandOutcome(
        status="succeeded",
        result={
            "activation_scheduled": True,
            "activation_id": "a" * 32,
        },
    )
    raw = {
        "id": command.command_id,
        "action": command.action,
        "payload": command.payload,
    }
    return runtime, state, raw


def test_accepted_staged_result_stops_old_agent_for_activation(tmp_path: Path):
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, "running")

    with (
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.platform.system", return_value="Windows"),
    ):
        runtime._handle_command(state, raw)

    assert runtime.stop_event.is_set()
    assert state.command_history[COMMAND_ID]["response"]["result"][
        "activation_scheduled"
    ] is True


def test_terminal_staged_result_keeps_old_agent_running(tmp_path: Path):
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, "expired")

    with (
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.platform.system", return_value="Windows"),
    ):
        runtime._handle_command(state, raw)

    assert not runtime.stop_event.is_set()


def test_terminal_staged_result_on_windows_warns_rejection(tmp_path: Path, caplog):
    """A terminal receipt must not fall through a silent no-action path."""
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, "cancelled")

    with (
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.platform.system", return_value="Windows"),
        caplog.at_level(logging.WARNING, logger="agent.runtime"),
    ):
        runtime._handle_command(state, raw)

    assert not runtime.stop_event.is_set()
    events = [
        record for record in caplog.records
        if getattr(record, "event", None) == "agent_update_stage_rejected"
    ]
    assert events and events[0].status == "cancelled"


@pytest.mark.parametrize("stored_status", ["queued", "dispatched"])
def test_windows_pending_storage_receipt_keeps_updater_window_alive(
    tmp_path: Path, stored_status: str
):
    """A durably stored staged result must let the old Agent exit (0.8.60).

    ``queued``/``dispatched`` receipts mean the controller stored the result;
    staying alive would miss the fixed updater's 75s confirmation window and
    roll the update back without a trace.
    """
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, stored_status)

    with (
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.platform.system", return_value="Windows"),
    ):
        runtime._handle_command(state, raw)

    assert runtime.stop_event.is_set()


@pytest.mark.parametrize("stored_status", ["queued", "dispatched"])
def test_linux_pending_storage_receipt_aborts_and_resumes_miner(
    tmp_path: Path, stored_status: str
):
    """Linux keeps its conservative contract: only ``running`` arms."""
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, stored_status)
    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.arm_staged_agent_update") as arm,
        mock.patch(
            "agent.runtime.abort_staged_agent_update", return_value=True
        ) as abort,
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update", return_value=True
        ) as resume,
    ):
        runtime._handle_command(state, raw)

    arm.assert_not_called()
    abort.assert_called_once()
    resume.assert_called_once()
    assert not runtime.stop_event.is_set()


def test_lost_staged_response_still_hands_off_to_ready_updater(tmp_path: Path):
    runtime, state, raw = _runtime_with_staged_outcome(
        tmp_path,
        "running",
        requests.ConnectionError("response lost after commit"),
    )

    with (
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.platform.system", return_value="Windows"),
        pytest.raises(requests.ConnectionError, match="response lost"),
    ):
        runtime._handle_command(state, raw)

    assert runtime.stop_event.is_set()
    assert state.command_history[COMMAND_ID]["response"]["result"][
        "activation_scheduled"
    ] is True


def test_linux_running_result_is_persisted_then_armed_then_stopped(tmp_path: Path):
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, "running")
    events = []

    def persist(*_args, **_kwargs):
        events.append("persist")

    def arm(_config, state_value, command_id, response):
        assert command_id == COMMAND_ID
        assert state_value.command_history[COMMAND_ID]["response"] == response
        events.append("arm")
        return True

    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch("agent.runtime.save_state", side_effect=persist),
        mock.patch("agent.runtime.arm_staged_agent_update", side_effect=arm),
    ):
        runtime._handle_command(state, raw)

    assert events == ["persist", "arm"]
    assert runtime.stop_event.is_set()


def test_linux_lost_result_response_arms_before_stopping(tmp_path: Path):
    runtime, state, raw = _runtime_with_staged_outcome(
        tmp_path,
        "running",
        requests.ConnectionError("response lost after commit"),
    )
    events = []

    def persist(*_args, **_kwargs):
        events.append("persist")

    def arm(*_args, **_kwargs):
        events.append("arm")
        return True

    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch("agent.runtime.save_state", side_effect=persist),
        mock.patch("agent.runtime.arm_staged_agent_update", side_effect=arm),
        pytest.raises(requests.ConnectionError, match="response lost"),
    ):
        runtime._handle_command(state, raw)

    assert events == ["persist", "arm"]
    assert runtime.stop_event.is_set()


def test_linux_terminal_stage_rejection_never_arms_and_resumes_miner(tmp_path: Path):
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, "expired")
    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.arm_staged_agent_update") as arm,
        mock.patch(
            "agent.runtime.abort_staged_agent_update", return_value=True
        ) as abort,
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update", return_value=True
        ) as resume,
    ):
        runtime._handle_command(state, raw)

    arm.assert_not_called()
    abort.assert_called_once()
    resume.assert_called_once_with(
        runtime.config,
        state,
        COMMAND_ID,
        "a" * 32,
    )
    assert not runtime.stop_event.is_set()


@pytest.mark.parametrize(
    "report_error",
    [
        ControllerRequestError(
            "controller returned HTTP 409",
            response_received=True,
            http_status=409,
        ),
        ProtocolError("controller returned malformed result status"),
    ],
)
def test_linux_explicit_or_protocol_rejection_aborts_without_arming(
    tmp_path: Path, report_error: BaseException
):
    runtime, state, raw = _runtime_with_staged_outcome(
        tmp_path, "running", report_error
    )
    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch("agent.runtime.save_state"),
        mock.patch("agent.runtime.arm_staged_agent_update") as arm,
        mock.patch(
            "agent.runtime.abort_staged_agent_update", return_value=True
        ) as abort,
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update", return_value=True
        ) as resume,
        pytest.raises(type(report_error)),
    ):
        runtime._handle_command(state, raw)

    arm.assert_not_called()
    abort.assert_called_once()
    resume.assert_called_once()
    assert not runtime.stop_event.is_set()


def test_linux_cached_http_rejection_aborts_without_arming(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
        allow_agent_updates=True,
    )
    runtime = AgentRuntime(config)
    command = update_command()
    staged = {
        "status": "succeeded",
        "result": {
            "activation_scheduled": True,
            "activation_id": "a" * 32,
        },
        "error": None,
    }
    state = AgentState.new("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret")
    state.command_history[COMMAND_ID] = {
        "fingerprint": command.fingerprint(),
        "response": staged,
        "completed_at": "2026-01-01T00:00:00Z",
    }

    class RejectedClient(RecordingClient):
        def post_result(self, *_args, **_kwargs):
            raise ControllerRequestError(
                "controller returned HTTP 409",
                response_received=True,
                http_status=409,
            )

    runtime.client = RejectedClient()
    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch("agent.runtime.arm_staged_agent_update") as arm,
        mock.patch(
            "agent.runtime.abort_staged_agent_update", return_value=True
        ) as abort,
        mock.patch(
            "agent.runtime.resume_managed_miner_after_update", return_value=True
        ) as resume,
        pytest.raises(ControllerRequestError),
    ):
        runtime._handle_command(
            state,
            {
                "id": command.command_id,
                "action": command.action,
                "payload": command.payload,
            },
        )

    arm.assert_not_called()
    abort.assert_called_once()
    resume.assert_called_once()
    assert not runtime.stop_event.is_set()


def test_authenticated_heartbeat_precedes_confirmed_root_synchronization(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    order = []

    def heartbeat(_state):
        order.append("heartbeat")
        runtime.stop_event.set()
        return 0

    def synchronize(_config, _state):
        order.append("root_sync")
        return False

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_start_optional_services"),
        mock.patch.object(runtime, "_stop_optional_services"),
        mock.patch.object(runtime, "_host_data_sampler", mock.Mock(snapshot=minimum_host_data)),
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch.object(runtime, "heartbeat_once", side_effect=heartbeat),
        mock.patch(
            "agent.runtime.reconcile_confirmed_release_root",
            side_effect=synchronize,
        ),
    ):
        runtime.run()

    assert order == ["heartbeat", "root_sync"]


def test_once_skips_all_local_repair_and_update_maintenance(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    runtime.client = RecordingClient()
    runtime._host_data_sampler = mock.Mock()
    host = {
        "inventory": {
            "_heartbeat_maintenance": {
                "pending_agent_update": True,
                "rollback_receipt": True,
                "supervision_repair": True,
            }
        }
    }

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_host_data", return_value=host),
        mock.patch.object(runtime, "_reconcile_pending_agent_update") as pending,
        mock.patch.object(runtime, "_reconcile_update_rollbacks") as rollback,
        mock.patch.object(runtime, "_maybe_submit_supervision_policy_repair") as supervision,
        mock.patch(
            "agent.runtime.reconcile_confirmed_release_root", return_value=False
        ) as root_sync,
    ):
        runtime.run(once=True)

    pending.assert_not_called()
    rollback.assert_not_called()
    supervision.assert_not_called()
    root_sync.assert_not_called()


def test_post_heartbeat_maintenance_failure_keeps_success_interval(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            heartbeat_interval=30,
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    runtime.client = mock.Mock()
    runtime.client.heartbeat.return_value = []

    with (
        mock.patch.object(runtime, "_host_data", return_value={}),
        mock.patch.object(
            runtime,
            "_reconcile_pending_agent_update",
            side_effect=StateError("state disk unavailable"),
        ),
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch(
            "agent.runtime.reconcile_confirmed_release_root", return_value=False
        ),
    ):
        delays = run_runtime_cycles(runtime, state, cycles=3)

    assert runtime.client.heartbeat.call_count == 3
    assert sum(delays) == pytest.approx(60)
    assert delays and max(delays) <= 0.5


def test_unexpected_heartbeat_exception_is_retried_without_exiting(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            heartbeat_interval=30,
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    with (
        mock.patch.object(
            runtime,
            "heartbeat_once",
            side_effect=[ValueError("unexpected local failure"), 0, 0],
        ) as heartbeat,
        mock.patch.object(runtime, "_reconcile_update_rollbacks"),
        mock.patch(
            "agent.runtime.reconcile_confirmed_release_root", return_value=False
        ),
    ):
        delays = run_runtime_cycles(runtime, state, cycles=3)

    assert heartbeat.call_count == 3
    assert sum(delays) == pytest.approx(40)
    assert delays and max(delays) <= 0.5


def test_repeated_heartbeat_failures_never_use_long_backoff(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            heartbeat_interval=30,
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    with mock.patch.object(
        runtime,
        "heartbeat_once",
        side_effect=requests.ConnectionError("controller unavailable"),
    ) as heartbeat:
        delays = run_runtime_cycles(runtime, state, cycles=5)

    assert heartbeat.call_count == 5
    assert sum(delays) == pytest.approx(40)
    assert delays and max(delays) <= 0.5


def test_rollback_failure_after_successful_heartbeat_does_not_stop_loop(
    tmp_path: Path,
):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
            heartbeat_interval=30,
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    def heartbeat_with_rollback_hint(_state: AgentState) -> int:
        runtime._merge_heartbeat_maintenance_hints(
            {"rollback_receipt": True}
        )
        return 0

    with (
        mock.patch.object(
            runtime,
            "heartbeat_once",
            side_effect=heartbeat_with_rollback_hint,
        ) as heartbeat,
        mock.patch.object(
            runtime,
            "_reconcile_update_rollbacks",
            side_effect=StateError("rollback marker cannot be persisted"),
        ) as rollback,
        mock.patch(
            "agent.runtime.reconcile_confirmed_release_root", return_value=False
        ),
    ):
        delays = run_runtime_cycles(runtime, state, cycles=3)

    assert heartbeat.call_count == 3
    assert rollback.call_count == 3
    assert sum(delays) == pytest.approx(60)
    assert delays and max(delays) <= 0.5


def test_command_state_write_failure_does_not_escape_successful_heartbeat(
    tmp_path: Path,
):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )

    class CommandClient(RecordingClient):
        def heartbeat(self, _state, _host_data):
            return [
                {
                    "id": COMMAND_ID,
                    "action": "restart_service",
                    "payload": {"service": "miner"},
                }
            ]

    runtime.client = CommandClient()
    runtime.executor = mock.Mock()
    runtime.executor.execute.return_value = CommandOutcome(
        status="succeeded", result={}
    )

    with (
        mock.patch.object(runtime, "_host_data", return_value={}),
        mock.patch.object(runtime, "_reconcile_pending_agent_update"),
        mock.patch(
            "agent.runtime.save_state",
            side_effect=StateError("state disk unavailable"),
        ),
    ):
        assert runtime.heartbeat_once(state) == 1

    assert [response["status"] for _, response in runtime.client.results] == [
        "running"
    ]


def test_signal_handler_only_sets_lock_free_stop_flag(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    runtime.stop_event = mock.Mock()

    runtime._handle_signal()

    assert runtime._signal_stop_requested is True
    runtime.stop_event.set.assert_not_called()


def test_signal_stop_flag_is_polled_in_half_second_wait_slices(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    runtime.stop_event = mock.Mock()
    runtime.stop_event.is_set.return_value = False

    def signal_during_wait(_delay: float) -> bool:
        runtime._signal_stop_requested = True
        return False

    runtime.stop_event.wait.side_effect = signal_during_wait

    assert runtime._wait_for_stop(30) is True
    runtime.stop_event.wait.assert_called_once_with(0.5)


def test_request_stop_still_wakes_event_waiters(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    runtime.stop_event = mock.Mock()
    runtime._host_data_sampler = mock.Mock()

    runtime.request_stop()

    runtime.stop_event.set.assert_called_once_with()
    runtime._host_data_sampler.request_stop.assert_called_once_with()


def test_once_raises_after_one_failed_heartbeat_and_runs_cleanup(tmp_path: Path):
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    runtime.stop_event = mock.Mock()
    runtime.stop_event.is_set.return_value = False
    runtime._host_data_sampler = mock.Mock()

    with (
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(
            runtime,
            "heartbeat_once",
            side_effect=RuntimeError("unexpected heartbeat failure"),
        ) as heartbeat,
        mock.patch.object(runtime, "_stop_optional_services") as stop_services,
    ):
        with pytest.raises(RuntimeError, match="unexpected heartbeat failure"):
            runtime.run(once=True)

    heartbeat.assert_called_once_with(state)
    runtime.stop_event.wait.assert_not_called()
    stop_services.assert_called_once_with()


@pytest.mark.parametrize(
    "controller_status,response_lost",
    [("running", False), ("running", True), ("expired", False)],
)
def test_linux_cached_stage_preserves_handoff_or_abort_decision(
    tmp_path: Path, controller_status: str, response_lost: bool
):
    error = requests.ConnectionError("response lost") if response_lost else None
    runtime, state, raw = _runtime_with_staged_outcome(tmp_path, controller_status, error)
    command = Command.parse(raw)
    response = runtime.executor.execute.return_value.to_api()
    state.command_history[command.command_id] = {
        "fingerprint": command.fingerprint(),
        "response": response,
        "completed_at": "2026-10-02T00:00:00+00:00",
    }
    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch("agent.runtime.arm_staged_agent_update", return_value=True) as arm,
        mock.patch("agent.runtime.abort_staged_agent_update", return_value=True) as abort,
        mock.patch("agent.runtime.resume_managed_miner_after_update", return_value=True) as resume,
    ):
        if response_lost:
            with pytest.raises(requests.ConnectionError):
                runtime._handle_command(state, raw)
        else:
            runtime._handle_command(state, raw)
        runtime.executor.execute.assert_not_called()
        if controller_status == "running":
            arm.assert_called_once_with(runtime.config, state, COMMAND_ID, response)
            abort.assert_not_called()
            resume.assert_not_called()
            assert runtime.stop_event.is_set()
        else:
            arm.assert_not_called()
            abort.assert_called_once()
            resume.assert_called_once_with(runtime.config, state, COMMAND_ID, "a" * 32)
            assert not runtime.stop_event.is_set()


def _runtime_with_initial_running_reply(tmp_path: Path, reply):
    runtime = AgentRuntime(
        AgentConfig(server_url="https://controller.test", state_file=tmp_path / "state.json")
    )
    state = AgentState.new("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "test-secret")
    raw = {"id": COMMAND_ID, "action": "apply_gpu_profile", "payload": {}}
    runtime.executor = mock.Mock()
    runtime.executor.execute.return_value = CommandOutcome("succeeded", {"synthetic": True})

    class Client(RecordingClient):
        def post_result(self, current, command_id, response):
            if response["status"] == "running":
                self.results.append((command_id, response))
                if isinstance(reply, BaseException):
                    raise reply
                return reply
            return super().post_result(current, command_id, response)

    runtime.client = Client()
    return runtime, state, raw


@pytest.mark.parametrize(
    "status", ["succeeded", "failed", "cancelled", "expired", "queued", "dispatched", "running"]
)
def test_initial_running_reply_executes_only_nonterminal_commands(tmp_path: Path, status: str):
    runtime, state, raw = _runtime_with_initial_running_reply(tmp_path, status)
    runtime._handle_command(state, raw)
    if status in {"succeeded", "failed", "cancelled", "expired"}:
        runtime.executor.execute.assert_not_called()
        assert state.command_history == {}
        assert [result["status"] for _, result in runtime.client.results] == ["running"]
        assert not runtime.config.state_file.exists()
    else:
        runtime.executor.execute.assert_called_once_with(Command.parse(raw), agent_state=state)
        assert state.command_history[COMMAND_ID]["response"]["status"] == "succeeded"
        assert [result["status"] for _, result in runtime.client.results] == ["running", "succeeded"]


@pytest.mark.parametrize(
    "http_status,definitive",
    [(400, True), (401, True), (402, True), (403, True), (404, True), (405, True),
     (409, True), (410, True), (413, True), (415, True), (422, True), (499, True),
     (408, False), (421, False), (425, False), (429, False),
     (500, False), (502, False), (503, False), (504, False), (None, False)],
)
def test_initial_running_http_error_preserves_definitive_and_unknown_classes(
    tmp_path: Path, http_status: int | None, definitive: bool
):
    error = ControllerRequestError(
        "synthetic Controller response", response_received=True, http_status=http_status
    )
    runtime, state, raw = _runtime_with_initial_running_reply(tmp_path, error)
    if definitive:
        with pytest.raises(ControllerRequestError) as captured:
            runtime._handle_command(state, raw)
        assert captured.value is error
        runtime.executor.execute.assert_not_called()
        assert state.command_history == {}
        assert [result["status"] for _, result in runtime.client.results] == ["running"]
    else:
        runtime._handle_command(state, raw)
        runtime.executor.execute.assert_called_once()
        assert state.command_history[COMMAND_ID]["response"]["status"] == "succeeded"


def test_initial_running_transport_connection_error_keeps_existing_execution(
    tmp_path: Path, monkeypatch
):
    from agent.client import RigPilotClient

    runtime, state, raw = _runtime_with_initial_running_reply(tmp_path, "unused")
    runtime.client = RigPilotClient(runtime.config)
    attempted = []

    def request(_method, _url, **kwargs):
        attempted.append(kwargs["json"]["status"])
        if len(attempted) == 1:
            raise requests.ConnectionError("synthetic transport connection failure")
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"status":"succeeded"}'
        return response

    monkeypatch.setattr(runtime.client.session, "request", request)
    runtime._handle_command(state, raw)
    assert attempted == ["running", "succeeded"]
    runtime.executor.execute.assert_called_once()
    assert state.command_history[COMMAND_ID]["response"]["status"] == "succeeded"


@pytest.mark.parametrize("body", [b"{}", b"", b'{"status":"invalid"}'])
def test_initial_running_malformed_response_is_not_a_synthetic_terminal(
    tmp_path: Path, monkeypatch, body: bytes
):
    from agent.client import RigPilotClient

    runtime, state, raw = _runtime_with_initial_running_reply(tmp_path, "unused")
    runtime.client = RigPilotClient(runtime.config)
    response = requests.Response()
    response.status_code = 200
    response._content = body
    request = mock.Mock(return_value=response)
    monkeypatch.setattr(runtime.client.session, "request", request)
    with pytest.raises(ProtocolError):
        runtime._handle_command(state, raw)
    runtime.executor.execute.assert_not_called()
    assert state.command_history == {}
    request.assert_called_once()


def test_initial_running_rejection_is_isolated_and_next_command_can_execute(tmp_path: Path):
    runtime, state, first = _runtime_with_initial_running_reply(tmp_path, "running")
    second = {**first, "id": "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"}

    class Client(RecordingClient):
        def heartbeat(self, _state, _host):
            return [first, second]

        def post_result(self, current, command_id, response):
            if command_id == COMMAND_ID and response["status"] == "running":
                raise ControllerRequestError("command expired", response_received=True, http_status=409)
            return super().post_result(current, command_id, response)

    runtime.client = Client()
    with (
        mock.patch.object(runtime, "_host_data", return_value={}),
        mock.patch.object(runtime, "_reconcile_pending_agent_update"),
    ):
        assert runtime.heartbeat_once(state) == 2
    runtime.executor.execute.assert_called_once_with(Command.parse(second), agent_state=state)
    assert COMMAND_ID not in state.command_history
    assert state.command_history[second["id"]]["response"]["status"] == "succeeded"


@pytest.mark.parametrize("status", [True, False, "403", 403.0, [403]])
def test_initial_running_noninteger_http_status_is_not_definitive(tmp_path: Path, status):
    error = ControllerRequestError("noninteger HTTP information", response_received=True, http_status=status)
    runtime, state, raw = _runtime_with_initial_running_reply(tmp_path, error)
    runtime._handle_command(state, raw)
    runtime.executor.execute.assert_called_once_with(Command.parse(raw), agent_state=state)
    assert state.command_history[COMMAND_ID]["response"]["status"] == "succeeded"


def test_initial_running_no_response_with_403_annotation_remains_unknown(tmp_path: Path):
    error = ControllerRequestError("no Controller response", response_received=False, http_status=403)
    runtime, state, raw = _runtime_with_initial_running_reply(tmp_path, error)
    runtime._handle_command(state, raw)
    runtime.executor.execute.assert_called_once_with(Command.parse(raw), agent_state=state)
    assert state.command_history[COMMAND_ID]["response"]["status"] == "succeeded"


def test_linux_self_recovery_hands_deadline_expired_activation_to_fixed_recover(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    recovery = {
        "root": tmp_path / "opt" / "rigpilot-agent",
        "data_root": tmp_path / "var" / "lib" / "rigpilot-agent",
        "activation_id": "a" * 32,
        "command_id": COMMAND_ID,
        "version": "0.5.8",
        "agent_id": state.agent_id,
    }
    with (
        mock.patch("agent.runtime.platform.system", return_value="Linux"),
        mock.patch(
            "agent.runtime.linux_pending_activation_recovery",
            return_value=recovery,
        ) as overdue,
        mock.patch(
            "agent.runtime.linux_trigger_activation_recovery",
            return_value=True,
        ) as trigger,
    ):
        runtime._maybe_self_recover_activation(state)
        # Rate limited: an immediate second attempt must not re-trigger.
        runtime._maybe_self_recover_activation(state)

    overdue.assert_called_once()
    trigger.assert_called_once_with(recovery)


def test_portable_self_recovery_stops_the_replacement_agent(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    recovery = {
        "root": tmp_path / "portable",
        "pointer": tmp_path / "portable" / "data" / "active-agent.json",
        "restore": {"schema_version": 1, "version": "0.5.7"},
        "activation_id": "a" * 32,
        "command_id": COMMAND_ID,
        "version": "0.5.8",
        "agent_id": state.agent_id,
    }
    with (
        mock.patch("agent.runtime.platform.system", return_value="Windows"),
        mock.patch(
            "agent.runtime.portable_pending_activation_recovery",
            return_value=recovery,
        ) as overdue,
        mock.patch(
            "agent.runtime.portable_trigger_activation_recovery",
            return_value=True,
        ) as trigger,
    ):
        runtime._maybe_self_recover_activation(state)

    overdue.assert_called_once()
    trigger.assert_called_once_with(recovery)
    assert runtime._stop_requested() is True


def test_fast_activation_window_shortens_the_heartbeat_delay(tmp_path: Path):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
        heartbeat_interval=30.0,
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    state.heartbeat_interval = 30.0
    runtime._fast_activation_window = True
    with mock.patch(
        "agent.runtime.pending_activation_active", return_value=True
    ):
        delays = run_runtime_cycles(runtime, state, 2)

    # The first cycle waits one fast 10s cadence (the stop flag raised by the
    # bounded harness skips the second wait); the configured interval is 30s,
    # so only the activation window can produce this sum.
    assert sum(delays) == pytest.approx(10)
    assert delays and max(delays) <= 0.5


def test_fast_activation_window_is_cleared_when_activation_is_reported(
    tmp_path: Path,
):
    # After the report receipt lands, the sticky window must close so the
    # Agent returns to its configured heartbeat cadence.
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    runtime.client = RecordingClient()
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    runtime._fast_activation_window = True
    with (
        mock.patch(
            "agent.runtime.pending_activation_active", return_value=False
        ) as probe,
        mock.patch.object(runtime, "_reconcile_pending_agent_update"),
    ):
        runtime._update_fast_activation_window(state, pending_hint=False)
    assert runtime._fast_activation_window is False
    probe.assert_called_once()


def test_startup_run_checks_activation_self_recovery_before_first_heartbeat(
    tmp_path: Path,
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    runtime = AgentRuntime(config)
    state = AgentState.new(
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
    )
    stop_event = RecordingLoopEvent()
    runtime.stop_event = stop_event

    def one_heartbeat(current_state: AgentState) -> int:
        stop_event.set()
        return 0

    with (
        mock.patch.object(runtime, "_host_data_sampler", mock.Mock(snapshot=minimum_host_data)),
        mock.patch.object(runtime, "install_signal_handlers"),
        mock.patch.object(runtime, "ensure_enrolled", return_value=state),
        mock.patch.object(runtime, "_start_optional_services"),
        mock.patch.object(runtime, "_stop_optional_services"),
        mock.patch.object(runtime, "_maybe_self_recover_activation") as check,
        mock.patch.object(runtime, "heartbeat_once", side_effect=one_heartbeat),
    ):
        runtime.run()
    check.assert_called_once_with(state)
