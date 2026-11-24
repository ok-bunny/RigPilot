"""Activation regressions for reconstructible progress and durable commit."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from agent import agent_updates as updates
from agent.runtime import AgentRuntime
from agent.state import AgentState
from agent.tests.test_linux_agent_updates import AGENT_ID, COMMAND_ID, linux_layout
from agent.version import __version__


@pytest.fixture
def activation(tmp_path: Path, monkeypatch):
    root, data, executable, config = linux_layout(tmp_path)
    metadata = {
        "activation_id": "c" * 32,
        "command_id": COMMAND_ID,
        "version": __version__,
        "agent_id": AGENT_ID,
    }
    (root / "pending-update.json").write_text(json.dumps({
        "schema_version": 1, **metadata,
        "relative_executable": f"versions/{__version__}/RigPilotAgent",
    }))
    state = AgentState.new(AGENT_ID, "test-only-secret")
    actual_layout = updates._linux_update_layout
    monkeypatch.setattr(updates.platform, "system", lambda: "Linux")
    monkeypatch.setattr(updates, "_linux_update_layout", lambda cfg: actual_layout(
        cfg, system_name="Linux", executable=executable, frozen=True, root_override=root,
    ))
    progress = data / "agent-updates/health" / f"{metadata['activation_id']}.json"
    return root, data, config, state, metadata, progress


def test_health_is_atomic_private_progress_without_file_or_directory_sync(activation):
    _, data, config, state, metadata, progress = activation
    with (
        mock.patch.object(updates.os, "fsync", side_effect=AssertionError("health fsync")),
        mock.patch.object(updates, "_fsync_directory", side_effect=AssertionError("health dir sync")),
    ):
        assert updates.note_pending_agent_heartbeat(config, state) is None
        assert json.loads(progress.read_text())["health_heartbeats"] == 1
        assert updates.note_pending_agent_heartbeat(config, state) == {**metadata, "health_heartbeats": 2}
    assert progress.stat().st_mode & 0o777 == 0o600
    assert not list(progress.parent.glob("*.tmp"))
    assert not (data / "agent-updates/confirmations").exists()


@pytest.mark.parametrize("raw", [b"", b"{", b"\xff", b"null", b"[]"])
def test_safe_corrupt_progress_restarts_at_one(activation, raw):
    _, _, config, state, metadata, progress = activation
    progress.parent.mkdir(parents=True)
    progress.write_bytes(raw)
    assert updates.note_pending_agent_heartbeat(config, state) is None
    assert json.loads(progress.read_text()) == {"schema_version": 1, **metadata, "health_heartbeats": 1}
    assert updates.note_pending_agent_heartbeat(config, state)["health_heartbeats"] == 2


@pytest.mark.parametrize("change", [
    {"activation_id": "d" * 32}, {"command_id": "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"},
    {"agent_id": "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"}, {"version": "0.8.31"},
    {"schema_version": True}, {"health_heartbeats": True},
    {"health_heartbeats": -10}, {"health_heartbeats": 3},
])
def test_unbound_or_invalid_progress_never_supplies_a_prior_heartbeat(activation, change):
    _, _, config, state, metadata, progress = activation
    progress.parent.mkdir(parents=True)
    progress.write_text(json.dumps({"schema_version": 1, **metadata, "health_heartbeats": 2, **change}))
    assert updates.note_pending_agent_heartbeat(config, state) is None
    assert json.loads(progress.read_text())["health_heartbeats"] == 1


@pytest.mark.parametrize("kind", ["symlink", "directory", "oversize", "io_error"])
def test_unsafe_or_unreadable_progress_is_not_replaced(activation, kind, monkeypatch):
    root, _, config, state, _, progress = activation
    progress.parent.mkdir(parents=True)
    if kind == "symlink":
        progress.symlink_to(root / "active-version")
    elif kind == "directory":
        progress.mkdir()
    elif kind == "oversize":
        progress.write_bytes(b"x" * 32769)
    else:
        progress.write_text("{}")
        actual_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == progress:
                raise PermissionError("progress unreadable")
            return actual_read(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read)
    with mock.patch.object(updates, "_atomic_json", side_effect=AssertionError("unsafe progress write")):
        assert updates.note_pending_agent_heartbeat(config, state) is None


def test_lost_progress_requires_two_new_heartbeats(activation):
    _, _, config, state, _, progress = activation
    assert updates.note_pending_agent_heartbeat(config, state) is None
    progress.unlink()
    assert updates.note_pending_agent_heartbeat(config, state) is None
    assert updates.note_pending_agent_heartbeat(config, state)["health_heartbeats"] == 2


def test_failed_progress_replace_retains_previous_count_and_removes_temp(activation):
    _, _, config, state, _, progress = activation
    assert updates.note_pending_agent_heartbeat(config, state) is None
    old = progress.read_bytes()
    with mock.patch.object(updates.os, "replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            updates.note_pending_agent_heartbeat(config, state)
    assert progress.read_bytes() == old
    assert not list(progress.parent.glob("*.tmp"))


def test_final_confirmation_remains_file_and_directory_durable(activation):
    _, data, config, state, _, _ = activation
    updates.note_pending_agent_heartbeat(config, state)
    metadata = updates.note_pending_agent_heartbeat(config, state)
    with (
        mock.patch.object(updates.os, "fsync") as file_sync,
        mock.patch.object(updates, "_fsync_directory") as directory_sync,
    ):
        assert updates.confirm_pending_agent_update(config, state, metadata)
    assert file_sync.call_count == 1
    assert directory_sync.call_args_list == [
        mock.call(data / "agent-updates/confirmations"), mock.call(data / "agent-updates"),
    ]


@pytest.mark.parametrize("fault", ["file_sync_failure", "directory_sync_failure", "rollback_during_sync", "binding_changed"])
def test_actual_runtime_never_posts_success_after_failed_or_stale_commit(activation, fault):
    root, data, config, state, _, _ = activation
    runtime = AgentRuntime(config)
    runtime.client = mock.Mock()
    runtime.client.post_result.return_value = "succeeded"
    marker = data / "resume-intent-test.json"
    marker.write_text("retain")
    def file_sync(_fd):
        if fault == "file_sync_failure":
            raise OSError("confirmation file sync failed")
        if fault == "rollback_during_sync":
            (root / "active-version").write_text("0.8.31\n")
        if fault == "binding_changed":
            pending_path = root / "pending-update.json"
            pending = json.loads(pending_path.read_text())
            pending["command_id"] = "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"
            pending_path.write_text(json.dumps(pending))
    def directory_sync(_path):
        if fault == "directory_sync_failure":
            raise OSError("confirmation directory sync failed")
    with (
        mock.patch("agent.runtime.resume_pending_agent_update_miner", return_value=False),
        mock.patch("agent.runtime.resume_managed_miner_after_update") as consume,
        mock.patch("agent.runtime.save_state") as saved,
        mock.patch.object(updates.os, "fsync", side_effect=file_sync),
        mock.patch.object(updates, "_fsync_directory", side_effect=directory_sync),
    ):
        runtime._reconcile_pending_agent_update(state)
        with pytest.raises(OSError):
            runtime._reconcile_pending_agent_update(state)
    runtime.client.post_result.assert_not_called()
    consume.assert_not_called()
    saved.assert_not_called()
    assert marker.read_text() == "retain"
    assert not state.command_history


def test_actual_runtime_commits_before_success_and_allows_reported_cleanup(activation):
    root, data, config, state, metadata, _ = activation
    runtime = AgentRuntime(config)
    runtime.client = mock.Mock()
    order = []
    def post(*_args):
        order.append("post")
        return "succeeded"
    runtime.client.post_result.side_effect = post
    def file_sync(_fd):
        order.append("file_sync")
    def directory_sync(path):
        order.append("directory_sync")
        # The stable updater can finish once the reported receipt appears.
        # Do not put the post-commit binding check on reported writes.
        if path.name == "reported":
            (root / "pending-update.json").unlink()
    with (
        mock.patch("agent.runtime.resume_pending_agent_update_miner", return_value=False),
        mock.patch("agent.runtime.resume_managed_miner_after_update", return_value=False),
        mock.patch("agent.runtime.save_state"),
        mock.patch.object(updates.os, "fsync", side_effect=file_sync),
        mock.patch.object(updates, "_fsync_directory", side_effect=directory_sync),
    ):
        runtime._reconcile_pending_agent_update(state)
        assert order == []
        runtime._reconcile_pending_agent_update(state)
    assert order[:4] == ["file_sync", "directory_sync", "directory_sync", "post"]
    runtime.client.post_result.assert_called_once()
    assert state.command_history[COMMAND_ID]["response"]["status"] == "succeeded"
    assert json.loads((data / "agent-updates/reported" / f"{metadata['activation_id']}.json").read_text())["reported"] is True


def test_durable_confirmation_survives_failed_post_and_retries_same_activation(activation):
    root, data, config, state, metadata, _ = activation
    runtime = AgentRuntime(config)
    runtime.client = mock.Mock()
    runtime.client.post_result.side_effect = [OSError("response lost"), "succeeded"]
    confirmation = data / "agent-updates/confirmations" / f"{metadata['activation_id']}.json"
    reported = data / "agent-updates/reported" / f"{metadata['activation_id']}.json"
    with (
        mock.patch("agent.runtime.resume_pending_agent_update_miner", return_value=False),
        mock.patch("agent.runtime.resume_managed_miner_after_update", return_value=False) as consume,
        mock.patch("agent.runtime.save_state"),
    ):
        runtime._reconcile_pending_agent_update(state)
        with pytest.raises(OSError, match="response lost"):
            runtime._reconcile_pending_agent_update(state)
        assert json.loads(confirmation.read_text()) == {"schema_version": 1, **metadata, "health_heartbeats": 2}
        assert (root / "pending-update.json").is_file()
        assert not reported.exists()
        assert not state.command_history
        consume.assert_not_called()
        runtime._reconcile_pending_agent_update(state)
    assert runtime.client.post_result.call_count == 2
    assert runtime.client.post_result.call_args_list[0] == runtime.client.post_result.call_args_list[1]
    assert json.loads(reported.read_text())["reported"] is True
    assert state.command_history[COMMAND_ID]["response"]["status"] == "succeeded"
