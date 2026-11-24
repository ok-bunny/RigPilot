"""Failure-injection tests for the 0.8.54 update-reliability contract.

Core promise under test: a failed update must leave the device online,
including after a restart.  Covered failure injections:

- staging on a nearly-full disk refuses before any byte is written;
- a Linux activation whose updater died past its durable deadline is handed
  to the immutable ``recover`` subcommand (cross-restart fallback);
- a portable (Windows) activation whose updater died rolls the pointer back
  from the updater's own backup, writes the rollback receipt, and starts the
  trusted launcher;
- orphaned miner resume markers keep being retried after the rollback report
  already reached the controller.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from agent.agent_updates import (
    AgentUpdateError,
    AgentUpdateManager,
    LINUX_ACTIVATION_EXTRA_GRACE_SECONDS,
    LINUX_PYTHON,
    LINUX_SYSTEMD_RUN,
    PORTABLE_ACTIVATION_EXTRA_GRACE_SECONDS,
    PORTABLE_PREVIOUS_POINTER_NAME,
    PORTABLE_UPDATER_CONFIRMATION_SECONDS,
    assert_update_disk_headroom,
    linux_pending_activation_recovery,
    linux_trigger_activation_recovery,
    pending_miner_resume_markers,
    portable_pending_activation_recovery,
    portable_trigger_activation_recovery,
    quarantine_portable_rollback_slot,
    required_update_disk_headroom,
)
from agent.config import AgentConfig
from agent.state import AgentState
from agent.version import __version__


AGENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
COMMAND_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
RELEASE_ID = "12345678-1234-4123-8123-123456789abc"
CURRENT_PARTS = tuple(int(part) for part in __version__.split("."))
TARGET_VERSION = (
    f"{CURRENT_PARTS[0]}.{CURRENT_PARTS[1]}.{CURRENT_PARTS[2] + 1}"
)
OLD_VERSION = (
    f"{CURRENT_PARTS[0]}.{CURRENT_PARTS[1]}.{max(0, CURRENT_PARTS[2] - 1)}"
)


def linux_layout(tmp_path: Path) -> tuple[Path, Path, Path, AgentConfig]:
    root = tmp_path / "opt" / "rigpilot-agent"
    data = tmp_path / "var" / "lib" / "rigpilot-agent"
    executable = root / "versions" / __version__ / "RigPilotAgent"
    executable.parent.mkdir(parents=True)
    data.mkdir(parents=True)
    executable.write_bytes(b"current-agent")
    (root / "RigPilotAgentLauncher.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "RigPilotAgentUpdater.py").write_text("# trusted updater\n", encoding="utf-8")
    (root / "active-version").write_text(f"{__version__}\n", encoding="ascii")
    state_file = data / "state.json"
    state_file.write_text("{}\n", encoding="utf-8")
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=state_file,
        allow_agent_updates=True,
        managed_miner_runtime_dir=data / "miner-runtime",
    )
    return root, data, executable, config


def write_linux_pending(
    root: Path,
    data: Path,
    *,
    activation_id: str = "c" * 32,
    version: str = __version__,
) -> str:
    (root / "pending-update.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "relative_executable": f"versions/{version}/RigPilotAgent",
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "agent_id": AGENT_ID,
            }
        ),
        encoding="utf-8",
    )
    return activation_id


# --------------------------------------------------------------------------
# Disk-space preflight (mode A root-cause prevention)
# --------------------------------------------------------------------------


def test_required_headroom_always_exceeds_the_floor():
    assert required_update_disk_headroom(1) == 512 * 1024 * 1024
    assert (
        required_update_disk_headroom(400 * 1024 * 1024)
        == int(2.5 * 400 * 1024 * 1024)
    )


def test_headroom_assertion_names_the_path_and_amounts(tmp_path: Path):
    with mock.patch(
        "agent.agent_updates._free_bytes", return_value=64 * 1024 * 1024
    ) as free:
        with pytest.raises(AgentUpdateError, match="MiB free") as caught:
            assert_update_disk_headroom((tmp_path,), 40 * 1024 * 1024)
    free.assert_called_once_with(tmp_path)
    assert str(tmp_path) in str(caught.value)


def test_linux_stage_refuses_update_without_disk_headroom(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    client = mock.Mock()
    manager = AgentUpdateManager(
        config,
        client,
        system_name="Linux",
        executable=executable,
        frozen=True,
        portable_root=root,
        popen_factory=mock.Mock(),
        preflight_runner=mock.Mock(),
    )
    payload = {
        "release_id": RELEASE_ID,
        "version": TARGET_VERSION,
        "sha256": "a" * 64,
        "size": 100,
        "download_url": f"/api/agent/updates/{COMMAND_ID}/download",
    }

    with mock.patch(
        "agent.agent_updates._free_bytes", return_value=32 * 1024 * 1024
    ):
        with pytest.raises(AgentUpdateError, match="MiB free"):
            manager.stage(payload, AgentState.new(AGENT_ID, "secret"), COMMAND_ID)

    client.download_agent_update.assert_not_called()
    assert not (data / "agent-updates" / "staged").exists()


def test_portable_stage_refuses_update_without_disk_headroom(tmp_path: Path):
    root = tmp_path / "portable"
    (root / "data").mkdir(parents=True)
    (root / "RigPilotAgent.exe").write_bytes(b"agent")
    (root / "portable-agent.ps1").write_text("# launcher\n", encoding="utf-8")
    (root / "RigPilotAgentUpdater.ps1").write_text("# updater\n", encoding="utf-8")
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=root / "data" / "state.json",
        allow_agent_updates=True,
    )
    client = mock.Mock()
    manager = AgentUpdateManager(
        config,
        client,
        system_name="Windows",
        executable=root / "RigPilotAgent.exe",
        frozen=True,
        portable_root=root,
        popen_factory=mock.Mock(),
        preflight_runner=mock.Mock(),
    )
    payload = {
        "release_id": RELEASE_ID,
        "version": TARGET_VERSION,
        "sha256": "a" * 64,
        "size": 100,
        "download_url": f"/api/agent/updates/{COMMAND_ID}/download",
    }

    with (
        mock.patch(
            "agent.agent_updates._portable_root",
            return_value=(root, root / "RigPilotAgent.exe"),
        ),
        mock.patch(
            "agent.agent_updates._free_bytes", return_value=32 * 1024 * 1024
        ),
    ):
        with pytest.raises(AgentUpdateError, match="MiB free"):
            manager.stage(payload, AgentState.new(AGENT_ID, "secret"), COMMAND_ID)

    client.download_agent_update.assert_not_called()
    assert list((root / "data").iterdir()) == []


# --------------------------------------------------------------------------
# Linux bounded self-recovery (mode A: new Agent never reconnected)
# --------------------------------------------------------------------------


def test_linux_recovery_is_overdue_only_past_updater_deadline(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    activation_id = write_linux_pending(root, data)
    state = AgentState.new(AGENT_ID, "secret")
    future = time.time() + 300.0
    (root / "updater-state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pending": {},
                "previous_version": OLD_VERSION,
                "deadline": future,
                "phase": "activated",
                "slot_created": True,
                "recovery_attempts": 0,
            }
        ),
        encoding="utf-8",
    )
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
    ):
        assert linux_pending_activation_recovery(config, state) is None

    past = time.time() - (
        LINUX_ACTIVATION_EXTRA_GRACE_SECONDS + 60.0
    )
    value = json.loads((root / "updater-state.json").read_text(encoding="utf-8"))
    value["deadline"] = past
    (root / "updater-state.json").write_text(
        json.dumps(value), encoding="utf-8"
    )
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
    ):
        recovery = linux_pending_activation_recovery(config, state)
    assert recovery is not None
    assert recovery["activation_id"] == activation_id
    assert recovery["command_id"] == COMMAND_ID
    assert recovery["root"] == root.resolve()
    assert recovery["data_root"] == data.resolve()


def test_linux_recovery_falls_back_to_pending_age_without_state(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    activation_id = write_linux_pending(root, data)
    state = AgentState.new(AGENT_ID, "secret")
    stale = time.time() - 60 * 60
    os.utime(root / "pending-update.json", (stale, stale))
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
    ):
        recovery = linux_pending_activation_recovery(config, state)
    assert recovery is not None
    assert recovery["activation_id"] == activation_id

    fresh = time.time() - 5.0
    os.utime(root / "pending-update.json", (fresh, fresh))
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
    ):
        assert linux_pending_activation_recovery(config, state) is None


def test_linux_recovery_is_suppressed_once_reported(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    activation_id = write_linux_pending(root, data)
    reported = data / "agent-updates" / "reported" / f"{activation_id}.json"
    reported.parent.mkdir(parents=True)
    reported.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "version": __version__,
                "agent_id": AGENT_ID,
                "reported": True,
            }
        ),
        encoding="utf-8",
    )
    stale = time.time() - 60 * 60
    os.utime(root / "pending-update.json", (stale, stale))
    state = AgentState.new(AGENT_ID, "secret")
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
    ):
        assert linux_pending_activation_recovery(config, state) is None


def test_linux_trigger_recovery_invokes_fixed_recover_subcommand(tmp_path: Path):
    root, data, _executable, _config = linux_layout(tmp_path)
    launches: list[tuple[list[str], dict[str, object]]] = []

    def popen(argv, **options):
        launches.append((list(argv), dict(options)))
        return mock.Mock(pid=321)

    triggered = linux_trigger_activation_recovery(
        {
            "root": root,
            "data_root": data,
            "activation_id": "c" * 32,
            "command_id": COMMAND_ID,
            "version": __version__,
            "agent_id": AGENT_ID,
        },
        popen_factory=popen,
    )
    assert triggered is True
    argv, options = launches[0]
    assert argv[0] == str(LINUX_SYSTEMD_RUN)
    assert "--collect" in argv
    assert str(LINUX_PYTHON) in argv
    assert "-I" in argv
    updater_index = argv.index(str(root / "RigPilotAgentUpdater.py"))
    assert argv[updater_index + 1] == "recover"
    assert argv[argv.index("--program-root") + 1] == str(root)
    assert argv[argv.index("--data-root") + 1] == str(data)
    assert options["stdin"] == subprocess.DEVNULL
    assert options["shell"] is False


def test_linux_trigger_recovery_requires_the_fixed_updater(tmp_path: Path):
    root, data, _executable, _config = linux_layout(tmp_path)
    (root / "RigPilotAgentUpdater.py").unlink()
    assert (
        linux_trigger_activation_recovery(
            {
                "root": root,
                "data_root": data,
                "activation_id": "c" * 32,
                "command_id": COMMAND_ID,
                "version": __version__,
                "agent_id": AGENT_ID,
            },
            popen_factory=mock.Mock(),
        )
        is False
    )


# --------------------------------------------------------------------------
# Portable (Windows) bounded self-recovery
# --------------------------------------------------------------------------


def portable_layout(tmp_path: Path) -> tuple[Path, Path, AgentConfig]:
    root = tmp_path / "portable"
    data = root / "data"
    executable = root / "versions" / __version__ / "RigPilotAgent.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"agent")
    (root / "portable-agent.ps1").write_text("# launcher\n", encoding="utf-8")
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=data / "state.json",
        allow_agent_updates=True,
    )
    return root, executable, config


def write_portable_pending(
    root: Path,
    *,
    activation_id: str = "e" * 32,
    stale_age: float | None = None,
) -> Path:
    pointer = root / "data" / "active-agent.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": __version__,
                "relative_executable": f"versions/{__version__}/RigPilotAgent.exe",
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
            }
        ),
        encoding="utf-8",
    )
    if stale_age is not None:
        moment = time.time() - stale_age
        os.utime(pointer, (moment, moment))
    return pointer


def write_portable_previous(
    root: Path,
    activation_id: str,
    *,
    version: str = OLD_VERSION,
) -> None:
    old_executable = root / "versions" / version / "RigPilotAgent.exe"
    old_executable.parent.mkdir(parents=True, exist_ok=True)
    old_executable.write_bytes(b"old-agent")
    job = root / "data" / "agent-updates" / "staged" / activation_id
    job.mkdir(parents=True, exist_ok=True)
    (job / PORTABLE_PREVIOUS_POINTER_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "relative_executable": f"versions/{version}/RigPilotAgent.exe",
            }
        ),
        encoding="utf-8",
    )


@contextlib.contextmanager
def portable_recovery_patch(root: Path, executable: Path):
    with (
        mock.patch(
            "agent.agent_updates.platform.system", return_value="Windows"
        ),
        mock.patch(
            "agent.agent_updates._portable_root",
            return_value=(root.resolve(), executable.resolve()),
        ),
    ):
        yield


def test_portable_recovery_requires_the_deadline_and_backup(tmp_path: Path):
    root, executable, config = portable_layout(tmp_path)
    state = AgentState.new(AGENT_ID, "secret")
    stale_age = (
        PORTABLE_UPDATER_CONFIRMATION_SECONDS
        + PORTABLE_ACTIVATION_EXTRA_GRACE_SECONDS
        + 60.0
    )

    # Fresh pointer: the trusted updater is still inside its own window.
    activation_id = "e" * 32
    pointer = write_portable_pending(root, activation_id=activation_id)
    with portable_recovery_patch(root, executable):
        assert portable_pending_activation_recovery(config, state) is None

    # Stale pointer but no backup: nothing safe to restore.
    moment = time.time() - stale_age
    os.utime(pointer, (moment, moment))
    with portable_recovery_patch(root, executable):
        assert portable_pending_activation_recovery(config, state) is None

    # Backup present: recovery is armed.
    write_portable_previous(root, activation_id)
    with portable_recovery_patch(root, executable):
        recovery = portable_pending_activation_recovery(config, state)
    assert recovery is not None
    assert recovery["activation_id"] == activation_id
    assert recovery["restore"]["version"] == OLD_VERSION

    # A committed two-heartbeat confirmation must never be rolled back.
    confirmation = (
        root / "data" / "agent-updates" / "confirmations" / f"{activation_id}.json"
    )
    confirmation.parent.mkdir(parents=True)
    confirmation.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "version": __version__,
                "agent_id": AGENT_ID,
                "health_heartbeats": 2,
            }
        ),
        encoding="utf-8",
    )
    with portable_recovery_patch(root, executable):
        assert portable_pending_activation_recovery(config, state) is None


def test_portable_trigger_restores_pointer_receipt_and_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root, executable, _config = portable_layout(tmp_path)
    activation_id = "e" * 32
    pointer = write_portable_pending(
        root, activation_id=activation_id, stale_age=300.0
    )
    write_portable_previous(root, activation_id)
    system_root = tmp_path / "Windows"
    powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"powershell")
    monkeypatch.setenv("SystemRoot", str(system_root))
    launches: list[list[str]] = []

    def popen(argv, **_options):
        launches.append(list(argv))
        return mock.Mock(pid=777)

    triggered = portable_trigger_activation_recovery(
        {
            "root": root,
            "pointer": pointer,
            "restore": {
                "schema_version": 1,
                "version": OLD_VERSION,
                "relative_executable": f"versions/{OLD_VERSION}/RigPilotAgent.exe",
            },
            "activation_id": activation_id,
            "command_id": COMMAND_ID,
            "version": __version__,
            "agent_id": AGENT_ID,
        },
        system_name="Windows",
        popen_factory=popen,
    )
    assert triggered is True
    restored = json.loads(pointer.read_text(encoding="utf-8"))
    assert restored["version"] == OLD_VERSION
    receipt = json.loads(
        (
            root
            / "data"
            / "agent-updates"
            / "staged"
            / activation_id
            / "activation-result.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "rolled_back"
    assert receipt["activation_id"] == activation_id
    assert receipt["command_id"] == COMMAND_ID
    assert receipt["agent_id"] == AGENT_ID
    assert receipt["error"]
    launcher_argv = " ".join(launches[0])
    assert "portable-agent.ps1" in launcher_argv
    assert "-Action Start -Scheduled" in launcher_argv


def test_portable_rollback_slot_is_quarantined_but_never_the_active_one(
    tmp_path: Path,
):
    root, executable, config = portable_layout(tmp_path)
    pointer = write_portable_pending(root)
    slot = root / "versions" / TARGET_VERSION
    slot.mkdir()
    (slot / "RigPilotAgent.exe").write_bytes(b"replacement")
    with portable_recovery_patch(root, executable):
        assert (
            quarantine_portable_rollback_slot(
                config, TARGET_VERSION, "e" * 32
            )
            is True
        )
    quarantine = (
        root / "data" / "agent-updates" / "quarantine" / f"{TARGET_VERSION}-{'e' * 32}"
    )
    assert (quarantine / "RigPilotAgent.exe").read_bytes() == b"replacement"
    assert not slot.exists()

    # The currently selected slot is never moved.
    with portable_recovery_patch(root, executable):
        assert (
            quarantine_portable_rollback_slot(config, __version__, "e" * 32)
            is False
        )
    assert executable.is_file()


# --------------------------------------------------------------------------
# Deferred miner resume retry
# --------------------------------------------------------------------------


def write_resume_marker(
    data: Path,
    *,
    activation_id: str = "1" * 32,
    agent_id: str = AGENT_ID,
    command_id: str = COMMAND_ID,
) -> Path:
    directory = data / "agent-updates" / "miner-resume"
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{activation_id}.json"
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activation_id": activation_id,
                "command_id": command_id,
                "agent_id": agent_id,
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet-1",
                "resume_required": True,
            }
        ),
        encoding="utf-8",
    )
    return marker


def test_pending_miner_resume_markers_return_only_bound_valid_markers(
    tmp_path: Path,
):
    root, data, executable, config = linux_layout(tmp_path)
    marker = write_resume_marker(data)
    write_resume_marker(
        data,
        activation_id="2" * 32,
        agent_id="99999999-9999-4999-8999-999999999999",
    )
    foreign = data / "agent-updates" / "miner-resume" / ("3" * 32 + ".json")
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("not-json", encoding="utf-8")
    state = AgentState.new(AGENT_ID, "secret")
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
    ):
        found = pending_miner_resume_markers(config, state)
    assert found == [(COMMAND_ID, "1" * 32)]
    assert marker.is_file()
