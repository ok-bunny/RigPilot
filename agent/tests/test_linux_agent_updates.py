from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from unittest import mock

import pytest

from agent.agent_updates import (
    AGENT_UPDATE_ACTION,
    AgentUpdateError,
    AgentUpdateManager,
    _atomic_json,
    _linux_update_layout,
    _validate_and_extract_linux_release,
    arm_staged_agent_update,
    abort_staged_agent_update,
    confirm_pending_agent_update,
    mark_activation_reported,
    note_pending_agent_heartbeat,
    pending_rollback_results,
    agent_update_readiness,
)
from agent.config import AgentConfig
from agent.managed_miners import ManagedMinerError
from agent.state import AgentState
from agent.version import __version__


AGENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
COMMAND_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
RELEASE_ID = "12345678-1234-4123-8123-123456789abc"
_CURRENT_VERSION_PARTS = tuple(int(part) for part in __version__.split("."))
TARGET_VERSION = (
    f"{_CURRENT_VERSION_PARTS[0]}.{_CURRENT_VERSION_PARTS[1]}."
    f"{_CURRENT_VERSION_PARTS[2] + 1}"
)


def linux_release(
    *,
    version: str = TARGET_VERSION,
    platform_name: str = "linux",
    architecture: str = "x86_64",
    program: bytes = b"linux-agent",
    declared_digest: str | None = None,
    extra_files: dict[str, bytes] | None = None,
    extra_members: list[tarfile.TarInfo] | None = None,
) -> bytes:
    files = {"RigPilotAgent": program, **(extra_files or {})}
    manifest = {
        "schema_version": 1,
        "version": version,
        "platform": platform_name,
        "architecture": architecture,
        "files": {
            name: (
                declared_digest
                if name == "RigPilotAgent" and declared_digest is not None
                else hashlib.sha256(value).hexdigest()
            )
            for name, value in files.items()
        },
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, value in files.items():
            info = tarfile.TarInfo(f"RigPilotAgent-Online/{name}")
            info.size = len(value)
            info.mode = 0o755 if name == "RigPilotAgent" else 0o600
            archive.addfile(info, io.BytesIO(value))
        manifest_bytes = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo("RigPilotAgent-Online/agent-release.json")
        info.size = len(manifest_bytes)
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for info in extra_members or []:
            payload = b"duplicate" if info.isreg() else None
            if info.isreg():
                info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    return output.getvalue()


def linux_layout(tmp_path: Path) -> tuple[Path, Path, Path, AgentConfig]:
    root = tmp_path / "opt" / "rigpilot-agent"
    data = tmp_path / "var" / "lib" / "rigpilot-agent"
    executable = root / "versions" / __version__ / "RigPilotAgent"
    executable.parent.mkdir(parents=True)
    data.mkdir(parents=True)
    executable.write_bytes(b"current-agent")
    executable.chmod(0o700)
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


def update_payload(archive: bytes) -> dict[str, object]:
    return {
        "release_id": RELEASE_ID,
        "version": TARGET_VERSION,
        "sha256": hashlib.sha256(archive).hexdigest(),
        "size": len(archive),
        "download_url": f"/api/agent/updates/{COMMAND_ID}/download",
    }


def test_linux_tar_release_is_exact_and_program_only(tmp_path: Path):
    package = tmp_path / "release.tar.gz"
    package.write_bytes(linux_release())
    bundle = _validate_and_extract_linux_release(
        package,
        tmp_path / "bundle",
        expected_version=TARGET_VERSION,
        expanded_limit=1024 * 1024,
    )
    assert (bundle / "RigPilotAgent").read_bytes() == b"linux-agent"
    assert (bundle / "agent-release.json").is_file()
    assert set(path.name for path in bundle.iterdir()) == {
        "RigPilotAgent",
        "agent-release.json",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"platform_name": "windows"}, "does not match"),
        ({"architecture": "aarch64"}, "does not match"),
        ({"version": __version__}, "does not match"),
        ({"declared_digest": "0" * 64}, "SHA-256"),
        ({"extra_files": {"Install.sh": b"unsafe"}}, "file list"),
        ({"extra_files": {"RigPilotAgentUpdater.py": b"unsafe"}}, "file list"),
    ],
)
def test_linux_tar_rejects_wrong_identity_hash_or_bootstrap_files(
    tmp_path: Path, kwargs: dict[str, object], message: str
):
    package = tmp_path / "release.tar.gz"
    package.write_bytes(linux_release(**kwargs))
    with pytest.raises(AgentUpdateError, match=message):
        _validate_and_extract_linux_release(
            package,
            tmp_path / "bundle",
            expected_version=TARGET_VERSION,
            expanded_limit=1024 * 1024,
        )


@pytest.mark.parametrize("kind", ["traversal", "symlink", "hardlink", "fifo", "duplicate"])
def test_linux_tar_rejects_traversal_links_special_and_duplicate(
    tmp_path: Path, kind: str
):
    if kind == "traversal":
        info = tarfile.TarInfo("RigPilotAgent-Online/../escape")
    elif kind == "symlink":
        info = tarfile.TarInfo("RigPilotAgent-Online/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
    elif kind == "hardlink":
        info = tarfile.TarInfo("RigPilotAgent-Online/hard")
        info.type = tarfile.LNKTYPE
        info.linkname = "RigPilotAgent-Online/RigPilotAgent"
    elif kind == "fifo":
        info = tarfile.TarInfo("RigPilotAgent-Online/fifo")
        info.type = tarfile.FIFOTYPE
    else:
        info = tarfile.TarInfo("RigPilotAgent-Online/RigPilotAgent")
    package = tmp_path / "release.tar.gz"
    package.write_bytes(linux_release(extra_members=[info]))
    with pytest.raises(AgentUpdateError):
        _validate_and_extract_linux_release(
            package,
            tmp_path / "bundle",
            expected_version=TARGET_VERSION,
            expanded_limit=1024 * 1024,
        )
    assert not (tmp_path / "escape").exists()


def test_linux_tar_rejects_expansion_beyond_limit(tmp_path: Path):
    package = tmp_path / "release.tar.gz"
    package.write_bytes(linux_release(program=b"A" * 4096))
    with pytest.raises(AgentUpdateError, match="expanded size"):
        _validate_and_extract_linux_release(
            package,
            tmp_path / "bundle",
            expected_version=TARGET_VERSION,
            expanded_limit=1024,
        )


def test_linux_layout_requires_active_slot_and_stable_components(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    assert _linux_update_layout(
        config,
        system_name="Linux",
        executable=executable,
        frozen=True,
        root_override=root,
    ) == (root.resolve(), data.resolve(), executable.resolve())
    (root / "RigPilotAgentUpdater.py").unlink()
    with pytest.raises(AgentUpdateError, match="layout"):
        _linux_update_layout(
            config,
            system_name="Linux",
            executable=executable,
            frozen=True,
            root_override=root,
        )


def test_linux_capability_is_opt_in_and_layout_gated(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root, data, executable),
        ) as layout,
    ):
        assert agent_update_readiness(config)["available"] is True
        layout.side_effect = AgentUpdateError("unsafe")
        assert agent_update_readiness(config)["available"] is False
        assert agent_update_readiness(
            AgentConfig(
                server_url=config.server_url,
                state_file=config.state_file,
                allow_agent_updates=False,
            )
        )["available"] is False


def test_linux_stage_preflights_program_and_launches_only_fixed_updater(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    archive = linux_release()
    metadata = update_payload(archive)
    launches: list[tuple[list[str], dict[str, object]]] = []

    class Client:
        def download_agent_update(self, _state, path, destination, **expected):
            assert path == metadata["download_url"]
            assert expected["expected_size"] == len(archive)
            destination.write_bytes(archive)

    class Submitted:
        pid = 123

        def poll(self):
            # systemd-run exits after the transient unit has been submitted.
            return 0

        def terminate(self):
            raise AssertionError("accepted transient unit must not be terminated")

    def launch(argv, **options):
        launches.append((argv, options))
        ready = Path(argv[argv.index("--ready-path") + 1])
        ready.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "activation_id": argv[argv.index("--activation-id") + 1],
                    "command_id": argv[argv.index("--command-id") + 1],
                    "agent_id": argv[argv.index("--agent-id") + 1],
                    # Deliberately not the systemd-run client PID.
                    "updater_pid": 456,
                }
            ),
            encoding="utf-8",
        )
        return Submitted()

    runner = mock.Mock(
        return_value=subprocess.CompletedProcess(
            ["RigPilotAgent", "--version"],
            0,
            f"RigPilot Agent {TARGET_VERSION}\n",
            "",
        )
    )
    manager = AgentUpdateManager(
        config,
        Client(),
        system_name="Linux",
        executable=executable,
        frozen=True,
        portable_root=root,
        popen_factory=launch,
        preflight_runner=runner,
    )
    result = manager.stage(metadata, AgentState.new(AGENT_ID, "secret"), COMMAND_ID)

    assert result["activation_scheduled"] is True
    assert len(launches) == 1
    argv, options = launches[0]
    assert Path(argv[0]).as_posix() == "/usr/bin/systemd-run"
    assert "--wait" in argv
    assert any(Path(value).as_posix() == "/usr/bin/python3" for value in argv)
    assert str(root / "RigPilotAgentUpdater.py") in argv
    assert not any("bundle/RigPilotAgentUpdater.py" in value for value in argv)
    read_write_property = next(
        value for value in argv if value.startswith("--property=ReadWritePaths=")
    )
    assert read_write_property.endswith(f"{root} {data} /run/lock")
    assert any(
        value.startswith("--property=StandardOutput=append:")
        and value.endswith("updater.log")
        for value in argv
    )
    assert any(
        value.startswith("--property=StandardError=append:")
        and value.endswith("updater.log")
        for value in argv
    )
    assert "activate" in argv
    assert options["shell"] is False
    assert options["start_new_session"] is True
    runner.assert_called_once()
    assert runner.call_args.args[0][1] == "--version"


def test_linux_stage_reports_early_transient_unit_failure_detail(tmp_path: Path):
    root, _data, executable, config = linux_layout(tmp_path)
    archive = linux_release()
    metadata = update_payload(archive)

    class Client:
        def download_agent_update(self, _state, _path, destination, **_expected):
            destination.write_bytes(archive)

    class FailedUnit:
        pid = 123

        def poll(self):
            return 75

        def terminate(self):
            return None

    def launch(_argv, **options):
        options["stdout"].write(
            b"RigPilot Agent update failed: another update process owns the lock\n"
        )
        options["stdout"].flush()
        return FailedUnit()

    manager = AgentUpdateManager(
        config,
        Client(),
        system_name="Linux",
        executable=executable,
        frozen=True,
        portable_root=root,
        popen_factory=launch,
        preflight_runner=mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, f"RigPilot Agent {TARGET_VERSION}\n", ""
            )
        ),
    )

    with pytest.raises(AgentUpdateError, match="status 75.*owns the lock"):
        manager.stage(metadata, AgentState.new(AGENT_ID, "secret"), COMMAND_ID)


def test_linux_stage_waits_for_ready_before_stopping_live_miner(tmp_path: Path):
    root, _data, executable, base_config = linux_layout(tmp_path)
    config = AgentConfig(
        **{
            **base_config.__dict__,
            "managed_mining_enabled": True,
        }
    )
    archive = linux_release()
    metadata = update_payload(archive)
    events: list[str] = []

    class Client:
        def download_agent_update(self, _state, _path, destination, **_expected):
            destination.write_bytes(archive)

    class Submitted:
        pid = 123

        def poll(self):
            return 0

    def launch(argv, **_options):
        events.append("ready")
        Path(argv[argv.index("--ready-path") + 1]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "activation_id": argv[argv.index("--activation-id") + 1],
                    "command_id": COMMAND_ID,
                    "agent_id": AGENT_ID,
                    "updater_pid": 456,
                }
            ),
            encoding="utf-8",
        )
        return Submitted()

    class Miner:
        def running(self):
            events.append("inspect-miner")
            return {"miner_type": "xmrig", "flight_sheet_id": "sheet-1"}

        def stop(self):
            events.append("stop-miner")
            return {
                "stopped": True,
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet-1",
            }

    manager = AgentUpdateManager(
        config,
        Client(),
        system_name="Linux",
        executable=executable,
        frozen=True,
        portable_root=root,
        popen_factory=launch,
        preflight_runner=mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, f"RigPilot Agent {TARGET_VERSION}\n", ""
            )
        ),
        managed_miner_factory=lambda _config: Miner(),
    )
    manager.stage(metadata, AgentState.new(AGENT_ID, "secret"), COMMAND_ID)
    assert events == ["ready", "inspect-miner", "stop-miner"]


def test_linux_ready_stage_failure_aborts_and_restores_miner_intent(tmp_path: Path):
    root, data, executable, base_config = linux_layout(tmp_path)
    config = AgentConfig(
        **{**base_config.__dict__, "managed_mining_enabled": True}
    )
    archive = linux_release()
    metadata = update_payload(archive)

    class Client:
        def download_agent_update(self, _state, _path, destination, **_expected):
            destination.write_bytes(archive)

    class Submitted:
        pid = 123

        def poll(self):
            return 0

    def launch(argv, **_options):
        Path(argv[argv.index("--ready-path") + 1]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "activation_id": argv[argv.index("--activation-id") + 1],
                    "command_id": COMMAND_ID,
                    "agent_id": AGENT_ID,
                    "updater_pid": 456,
                }
            ),
            encoding="utf-8",
        )
        return Submitted()

    class Miner:
        def running(self):
            return {"miner_type": "xmrig", "flight_sheet_id": "sheet-1"}

        def stop(self):
            raise ManagedMinerError("stop failed")

    manager = AgentUpdateManager(
        config,
        Client(),
        system_name="Linux",
        executable=executable,
        frozen=True,
        portable_root=root,
        popen_factory=launch,
        preflight_runner=mock.Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, f"RigPilot Agent {TARGET_VERSION}\n", ""
            )
        ),
        managed_miner_factory=lambda _config: Miner(),
    )
    with pytest.raises(AgentUpdateError, match="could not be stopped"):
        manager.stage(metadata, AgentState.new(AGENT_ID, "secret"), COMMAND_ID)

    jobs = list((data / "agent-updates" / "staged").iterdir())
    assert len(jobs) == 1
    abort = json.loads(
        (jobs[0] / "activation-abort.json").read_text(encoding="utf-8")
    )
    assert abort["aborted"] is True
    assert abort["activation_id"] == jobs[0].name
    assert not (
        data / "agent-updates" / "miner-resume" / f"{jobs[0].name}.json"
    ).exists()


@pytest.mark.parametrize("retry_after_failed_sync", [False, True])
def test_linux_activation_arm_is_exact_job_bound_persisted_and_directory_synced(
    tmp_path: Path, retry_after_failed_sync,
):
    root, data, executable, config = linux_layout(tmp_path)
    activation_id = "e" * 32
    job = data / "agent-updates" / "staged" / activation_id
    job.mkdir(parents=True)
    (job / "updater-ready.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "agent_id": AGENT_ID,
                "updater_pid": 456,
            }
        ),
        encoding="utf-8",
    )
    response = {
        "status": "succeeded",
        "result": {
            "version": TARGET_VERSION,
            "activation_id": activation_id,
            "activation_scheduled": True,
        },
        "error": None,
    }
    state = AgentState.new(AGENT_ID, "secret")
    state.command_history[COMMAND_ID] = {"response": response}
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
        mock.patch("agent.agent_updates._fsync_directory") as directory_sync,
    ):
        if retry_after_failed_sync:
            directory_sync.side_effect = OSError("directory sync failed")
            with pytest.raises(OSError, match="directory sync failed"):
                arm_staged_agent_update(config, state, COMMAND_ID, response)
            assert (job / "activation-arm.json").is_file()
            directory_sync.side_effect = None
            directory_sync.reset_mock()
        assert arm_staged_agent_update(config, state, COMMAND_ID, response) is True
    arm_path = job / "activation-arm.json"
    assert json.loads(arm_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "version": TARGET_VERSION,
        "activation_id": activation_id,
        "command_id": COMMAND_ID,
        "agent_id": AGENT_ID,
        "armed": True,
    }
    directory_sync.assert_called_once_with(job)


def test_atomic_json_syncs_new_marker_directory_and_its_parent(tmp_path: Path):
    path = tmp_path / "new-marker-dir" / "marker.json"
    with mock.patch("agent.agent_updates._fsync_directory") as directory_sync:
        _atomic_json(path, {"schema_version": 1}, durable_directory=True)
    assert directory_sync.call_args_list == [
        mock.call(path.parent),
        mock.call(tmp_path),
    ]


def test_linux_activation_abort_is_exact_and_job_bound(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    activation_id = "f" * 32
    job = data / "agent-updates" / "staged" / activation_id
    job.mkdir(parents=True)
    response = {
        "status": "succeeded",
        "result": {
            "version": TARGET_VERSION,
            "activation_id": activation_id,
            "activation_scheduled": True,
        },
        "error": None,
    }
    state = AgentState.new(AGENT_ID, "secret")
    state.command_history[COMMAND_ID] = {"response": response}
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
        mock.patch("agent.agent_updates._fsync_directory"),
    ):
        assert abort_staged_agent_update(
            config,
            state,
            COMMAND_ID,
            response,
            "controller returned HTTP 409",
        ) is True
    assert json.loads((job / "activation-abort.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "version": TARGET_VERSION,
        "activation_id": activation_id,
        "command_id": COMMAND_ID,
        "agent_id": AGENT_ID,
        "aborted": True,
        "error": "controller returned HTTP 409",
    }


def test_linux_two_heartbeats_confirm_and_report_activation(tmp_path: Path):
    root, data, _old_executable, config = linux_layout(tmp_path)
    new_executable = root / "versions" / TARGET_VERSION / "RigPilotAgent"
    new_executable.parent.mkdir()
    new_executable.write_bytes(b"new-agent")
    new_executable.chmod(0o700)
    (root / "active-version").write_text(f"{TARGET_VERSION}\n", encoding="ascii")
    activation_id = "c" * 32
    pending = {
        "schema_version": 1,
        "version": TARGET_VERSION,
        "relative_executable": f"versions/{TARGET_VERSION}/RigPilotAgent",
        "activation_id": activation_id,
        "command_id": COMMAND_ID,
        "agent_id": AGENT_ID,
    }
    (root / "pending-update.json").write_text(json.dumps(pending), encoding="utf-8")
    state = AgentState.new(AGENT_ID, "secret")

    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), new_executable.resolve()),
        ),
        mock.patch("agent.agent_updates.__version__", TARGET_VERSION),
    ):
        assert note_pending_agent_heartbeat(config, state) is None
        metadata = note_pending_agent_heartbeat(config, state)
        assert metadata is not None and metadata["health_heartbeats"] == 2
        assert confirm_pending_agent_update(config, state, metadata) is True
        confirmation = json.loads(
            (
                data
                / "agent-updates"
                / "confirmations"
                / f"{activation_id}.json"
            ).read_text(encoding="utf-8")
        )
        assert confirmation["health_heartbeats"] == 2
        assert mark_activation_reported(config, state, metadata) is True
        assert note_pending_agent_heartbeat(config, state) is None


def test_linux_rollback_receipt_is_discovered_in_data_root(tmp_path: Path):
    root, data, executable, config = linux_layout(tmp_path)
    activation_id = "d" * 32
    job = data / "agent-updates" / "staged" / activation_id
    job.mkdir(parents=True)
    (job / "activation-result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "rolled_back",
                "version": TARGET_VERSION,
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "agent_id": AGENT_ID,
                "error": "replacement failed health checks",
            }
        ),
        encoding="utf-8",
    )
    state = AgentState.new(AGENT_ID, "secret")
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Linux"),
        mock.patch(
            "agent.agent_updates._linux_update_layout",
            return_value=(root.resolve(), data.resolve(), executable.resolve()),
        ),
    ):
        receipts = pending_rollback_results(config, state)
    assert len(receipts) == 1
    marker, command_id, response = receipts[0]
    assert marker == job / "rollback-reported.json"
    assert command_id == COMMAND_ID
    assert response["status"] == "failed"
    assert response["result"]["rolled_back"] is True
