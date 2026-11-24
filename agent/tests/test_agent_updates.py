from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import time
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from agent.agent_updates import (
    AgentUpdateError,
    AgentUpdateManager,
    _is_managed_miner_type,
    _stop_managed_miner_for_update,
    _validate_and_extract_release,
    _portable_root,
    _agent_update_bootstrap_metadata,
    agent_update_readiness,
    confirm_pending_agent_update,
    pending_rollback_results,
    mark_activation_reported,
    reconcile_confirmed_release_root,
    resume_managed_miner_after_update,
    validate_agent_update_payload,
)
from agent.config import AgentConfig
from agent.managed_miners import ManagedMinerError
from agent.state import AgentState
from agent.release_contract import (
    WINDOWS_REQUIRED_PAYLOAD,
    WindowsArchivePathError,
    canonical_windows_archive_parts,
)


RELEASE_ID = "12345678-1234-4123-8123-123456789abc"
AGENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
COMMAND_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
from agent.version import __version__ as CURRENT_VERSION

_current_version_parts = CURRENT_VERSION.split(".")
NEXT_VERSION = ".".join([*_current_version_parts[:2], str(int(_current_version_parts[2]) + 1)])


@pytest.mark.parametrize("name", [
    "", "/absolute", "//server/share", "../escape", "safe/../escape",
    "C:/drive", "C:relative", "safe/name:stream", "safe\\name", "safe/CON.txt",
    "nul", "AUX", "PRN", "CONIN$", "CONOUT$", "com9.exe", "lpt1.log",
    *(f"safe/a{character}b" for character in '?*|<>"'),
    *(f"safe/{device}{digit}.txt" for device in ("COM", "lpt") for digit in "¹²³"),
    "name.", "name ", "safe/na\x00me", "safe/na\x1fme", "safe/na\x7fme", "x" * 256,
    "😀" * 128, "😀" * 128 + ".txt",
])
def test_shared_windows_path_contract_rejects_ambiguous_names_with_agent_error_preserved(name):
    from agent.agent_updates import _safe_archive_name

    with pytest.raises(WindowsArchivePathError):
        canonical_windows_archive_parts(name)
    with pytest.raises(AgentUpdateError) as caught:
        _safe_archive_name(name)
    assert str(caught.value) == "agent update archive contains an unsafe path"


@pytest.mark.parametrize("name,parts", [
    ("RigPilotAgent.exe", ("RigPilotAgent.exe",)),
    ("bundle/_internal/library.zip", ("bundle", "_internal", "library.zip")),
    ("bundle//./file.txt", ("bundle", "file.txt")),
    ("COM10.txt", ("COM10.txt",)), ("x" * 255, ("x" * 255,)),
    ("矿工/é¹.txt", ("矿工", "é¹.txt")),
    ("😀" * 100 + ".txt", ("😀" * 100 + ".txt",)),
    ("😀" * 127 + "a", ("😀" * 127 + "a",)),
    ("\ud800", ("\ud800",)),
])
def test_shared_windows_path_contract_keeps_existing_normalization(name, parts):
    from agent.agent_updates import _safe_archive_name

    assert canonical_windows_archive_parts(name) == parts
    assert _safe_archive_name(name) == parts


def test_release_contract_imports_from_an_arbitrary_cwd_with_only_stdlib(tmp_path):
    module_path = Path(__file__).parents[1] / "release_contract.py"
    source = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location('offline_release_contract', sys.argv[1])
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
assert not any(name in sys.modules for name in ('requests','psutil','agent.config','agent.agent_updates','agent.managed_miners'))
assert contract.LINUX_INSTALLER_REQUIRED_PAYLOAD <= contract.LINUX_FULL_REQUIRED_PAYLOAD
assert contract.LINUX_FULL_EXECUTABLE_PAYLOAD <= contract.LINUX_FULL_REQUIRED_PAYLOAD
assert {'README-Ubuntu.txt','BUILD-DEPENDENCIES.txt'}.isdisjoint(contract.LINUX_FULL_REQUIRED_PAYLOAD)
print(json.dumps({'required':sorted(contract.WINDOWS_REQUIRED_PAYLOAD),'parts':contract.canonical_windows_archive_parts('bundle/RigPilotAgent.exe')}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", source, str(module_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    assert json.loads(completed.stdout) == {
        "required": sorted(WINDOWS_REQUIRED_PAYLOAD),
        "parts": ["bundle", "RigPilotAgent.exe"],
    }
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("xmrig", True),
        ("future-gpu-miner", True),
        ("FutureMiner", False),
        ("../miner", False),
        ("miner name", False),
        ("", False),
        (None, False),
    ],
)
def test_update_recovery_accepts_future_canonical_adapter_names(value, accepted):
    assert _is_managed_miner_type(value) is accepted


def payload(**changes):
    value = {
        "release_id": RELEASE_ID,
        "version": NEXT_VERSION,
        "sha256": "a" * 64,
        "size": 100,
        "download_url": f"/api/agent/updates/{COMMAND_ID}/download",
    }
    value.update(changes)
    return value


def release_zip(
    version: str = NEXT_VERSION,
    files: dict[str, bytes] | None = None,
    *,
    missing: str | None = None,
) -> bytes:
    release_files = {
        "RigPilotAgent.exe": b"agent",
        "RigPilotAgentUpdater.ps1": b"# updater\r\n",
        "portable-agent.ps1": b"# launcher\r\n",
        "Start-RigPilotAgent.bat": b"@echo off\r\n",
        "_internal/library.zip": b"runtime",
        **(files or {}),
    }
    if missing is not None:
        release_files.pop(missing)
    manifest = {
        "schema_version": 1,
        "version": version,
        "platform": "windows",
        "architecture": "x86_64",
        "files": {
            name: hashlib.sha256(value).hexdigest()
            for name, value in release_files.items()
        },
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        root = "RigPilotAgent-Portable/"
        for name, value in release_files.items():
            archive.writestr(root + name, value)
        archive.writestr(root + "agent-release.json", json.dumps(manifest))
    return output.getvalue()


def portable_config(root: Path, **changes) -> AgentConfig:
    values = {
        "server_url": "https://controller.test",
        "state_file": root / "data" / "state.json",
        "allow_agent_updates": True,
        "managed_miner_runtime_dir": root / "data" / "miner-runtime",
    }
    values.update(changes)
    return AgentConfig(**values)


def prepare_root(root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "RigPilotAgent.exe").write_bytes(b"agent")
    (root / "portable-agent.ps1").write_text("# launcher\n", encoding="utf-8")
    updater = root / "RigPilotAgentUpdater.ps1"
    updater.write_text("# updater\n", encoding="utf-8")
    (root / "data" / "agent-update-ready.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "acl_protected": True,
                "updater_sha256": hashlib.sha256(updater.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


def test_payload_requires_uuid_strict_newer_version_and_relative_bound_path():
    assert validate_agent_update_payload(payload())["release_id"] == RELEASE_ID
    assert validate_agent_update_payload(
        payload(version="0.5.9"), current_version="0.5.8"
    )["version"] == "0.5.9"
    for invalid in (
        payload(release_id=1),
        payload(version=CURRENT_VERSION),
        payload(version="0.0.0"),
        payload(version=f"v{NEXT_VERSION}"),
        payload(version="01.2.3"),
        payload(version=f"1.{'9' * 5000}.0"),
        payload(download_url="https://evil.test/update.zip"),
        payload(download_url="/api/agent/updates/id/download?token=x"),
        payload(download_url="//evil.test/api/agent/updates/id/download"),
        payload(sha256="A" * 64),
    ):
        with pytest.raises(AgentUpdateError):
            validate_agent_update_payload(invalid)


@pytest.mark.parametrize("name", ["_internal/library.zip", "矿工/é¹.txt"])
def test_release_manifest_exact_file_hashes_are_extracted(tmp_path: Path, name: str):
    package = tmp_path / "release.zip"
    package.write_bytes(release_zip(files={name: b"runtime"}))

    bundle = _validate_and_extract_release(
        package,
        tmp_path / "bundle",
        expected_version=NEXT_VERSION,
        expanded_limit=10 * 1024 * 1024,
    )

    assert (bundle / "RigPilotAgent.exe").read_bytes() == b"agent"
    assert bundle.joinpath(*name.split("/")).read_bytes() == b"runtime"


@pytest.mark.parametrize("missing", sorted(WINDOWS_REQUIRED_PAYLOAD))
def test_shared_required_payload_rejects_manifest_bound_missing_program(tmp_path, missing):
    package = tmp_path / "release.zip"
    package.write_bytes(release_zip(missing=missing))
    destination = tmp_path / "bundle"
    with pytest.raises(AgentUpdateError, match="manifest file list does not match"):
        _validate_and_extract_release(package, destination, expected_version=NEXT_VERSION, expanded_limit=1024 * 1024)
    assert not destination.exists()


@pytest.mark.parametrize("directory", ["data", "VERSIONS"])
@pytest.mark.parametrize("declared", [False, True])
def test_manifest_and_exact_file_set_reject_device_data_and_slots_before_extraction(tmp_path, directory, declared):
    member = f"{directory}/private.json"
    content = release_zip(files={member: b"forbidden"}) if declared else release_zip()
    package = tmp_path / "release.zip"
    package.write_bytes(content)
    if not declared:
        with zipfile.ZipFile(package, "a") as archive:
            archive.writestr(f"RigPilotAgent-Portable/{member}", b"forbidden")
    destination = tmp_path / "bundle"
    with pytest.raises(AgentUpdateError, match="invalid file entry|file list does not match"):
        _validate_and_extract_release(package, destination, expected_version=NEXT_VERSION, expanded_limit=1024 * 1024)
    assert not destination.exists()


def test_empty_reserved_zip_directories_are_counted_but_never_materialized(tmp_path, monkeypatch):
    import agent.agent_updates as updates

    package = tmp_path / "release.zip"
    package.write_bytes(release_zip())
    with zipfile.ZipFile(package, "a") as archive:
        for directory in ("data", "versions"):
            archive.writestr(f"RigPilotAgent-Portable/{directory}/", b"")
        member_count = len(archive.infolist())
    monkeypatch.setattr(updates, "_MAX_ARCHIVE_ENTRIES", member_count - 1)
    with pytest.raises(AgentUpdateError, match="entry count"):
        _validate_and_extract_release(package, tmp_path / "bundle", expected_version=NEXT_VERSION, expanded_limit=1024 * 1024)
    assert not (tmp_path / "bundle").exists()
    monkeypatch.setattr(updates, "_MAX_ARCHIVE_ENTRIES", member_count)
    bundle = _validate_and_extract_release(package, tmp_path / "bundle", expected_version=NEXT_VERSION, expanded_limit=1024 * 1024)
    assert not (bundle / "data").exists()
    assert not (bundle / "versions").exists()
    assert (bundle / "RigPilotAgent.exe").read_bytes() == b"agent"


@pytest.mark.parametrize("unsafe", [
    "../escape.exe", "CON", "name. ",
    *(f"a{character}b" for character in '?*|<>"'),
    *(f"{device}{digit}.txt" for device in ("COM", "lpt") for digit in "¹²³"),
    "😀" * 128 + ".txt",
])
def test_release_rejects_windows_unsafe_paths(tmp_path: Path, unsafe: str):
    package = tmp_path / "bad.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("RigPilotAgent-Portable/" + unsafe, b"bad")
        archive.writestr("RigPilotAgent-Portable/agent-release.json", "{}")
    with pytest.raises(AgentUpdateError, match="unsafe path"):
        _validate_and_extract_release(
            package,
            tmp_path / "bundle",
            expected_version="0.6.2",
            expanded_limit=1024 * 1024,
        )
    assert not (tmp_path / "bundle").exists()


def test_release_rejects_raw_backslash_even_when_zipfile_normalizes_filename(
    tmp_path: Path,
):
    package = tmp_path / "backslash.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo("RigPilotAgent-Portable/normalized-file")
        entry.filename = "RigPilotAgent-Portable/dir\\file"
        archive.writestr(entry, b"bad")
        archive.writestr("RigPilotAgent-Portable/agent-release.json", "{}")
    with pytest.raises(AgentUpdateError, match="unsafe path"):
        _validate_and_extract_release(
            package,
            tmp_path / "bundle",
            expected_version="0.6.2",
            expanded_limit=1024 * 1024,
        )


def test_capability_is_opt_in_and_requires_supported_protected_layout(tmp_path: Path):
    root = tmp_path / "portable"
    prepare_root(root)
    disabled = portable_config(root, allow_agent_updates=False)
    enabled = portable_config(root)
    assert agent_update_readiness(disabled)["available"] is False
    # A source test process must never advertise merely because the config is on.
    assert agent_update_readiness(enabled)["available"] is False
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Windows"),
        mock.patch(
            "agent.agent_updates._portable_root",
            return_value=(root, root / "RigPilotAgent.exe"),
        ),
    ):
        assert agent_update_readiness(enabled)["available"] is True


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            "portable autostart ACL protection has not been installed",
            "marker_missing",
        ),
        ("portable update ACL marker does not match the updater", "marker_invalid"),
        (
            r"SYSTEM updates require the portable root at %ProgramData%\RigPilotAgentPortable",
            "root_mismatch",
        ),
        ("trusted portable update component is missing: portable-agent.ps1", "helper_missing"),
        ("portable folder ACL is not safe for SYSTEM updates", "acl_or_owner_mismatch"),
        ("agent state is not in this portable folder's data directory", "state_mismatch"),
        ("source and non-frozen Agents cannot self-update", "unsupported_runtime"),
        ("unexpected local update failure", "unsafe_layout"),
    ],
)
def test_update_readiness_reports_bounded_secret_free_reason_codes(
    tmp_path: Path,
    message: str,
    reason: str,
):
    config = portable_config(tmp_path)
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Windows"),
        mock.patch(
            "agent.agent_updates._portable_root",
            side_effect=AgentUpdateError(message),
        ),
    ):
        readiness = agent_update_readiness(config)

    assert readiness == {
        "schema_version": 1,
        "protocol": "agent_update.v1",
        "available": False,
        "reason": reason,
        "bootstrap": _agent_update_bootstrap_metadata(system_name="Windows"),
    }
    assert message not in json.dumps(readiness)


def test_update_readiness_distinguishes_disabled_policy_and_ready_layout(tmp_path: Path):
    disabled = portable_config(tmp_path, allow_agent_updates=False)
    assert agent_update_readiness(disabled)["reason"] == "config_disabled"

    enabled = portable_config(tmp_path)
    (tmp_path / "portable-agent.ps1").write_text(
        "# launcher\\n", encoding="utf-8"
    )
    (tmp_path / "RigPilotAgentUpdater.ps1").write_text(
        "# updater\\n", encoding="utf-8"
    )
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Windows"),
        mock.patch(
            "agent.agent_updates._portable_root",
            return_value=(tmp_path, tmp_path / "RigPilotAgent.exe"),
        ),
    ):
        readiness = agent_update_readiness(enabled)

    assert readiness == {
        "schema_version": 1,
        "protocol": "agent_update.v1",
        "available": True,
        "reason": None,
        "bootstrap": {
            "schema_version": 1,
            "protocol": "agent_bootstrap.v1",
            "platform": "windows",
            "launcher": {
                "name": "portable-agent.ps1",
                "protocol_version": 1,
                "sha256": hashlib.sha256(b"# launcher\\n").hexdigest(),
            },
            "updater": {
                "name": "RigPilotAgentUpdater.ps1",
                "protocol_version": 1,
                "sha256": hashlib.sha256(b"# updater\\n").hexdigest(),
            },
            "immutable": True,
        },
    }


def test_system_update_rejects_portable_root_under_user_writable_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    desktop_root = tmp_path / "Desktop" / "RigPilot"
    prepare_root(desktop_root)
    executable = desktop_root / "RigPilotAgent.exe"
    executable.write_bytes(b"agent")
    fake_environment_root = tmp_path / "AttackerControlledProgramData"
    monkeypatch.setenv("ProgramData", str(fake_environment_root))
    known_folder_root = tmp_path / "WindowsCommonApplicationData"
    known_folder_root.mkdir()

    with (
        mock.patch(
            "agent.agent_updates._windows_program_data",
            return_value=known_folder_root,
        ),
        pytest.raises(AgentUpdateError, match="ProgramData"),
    ):
        _portable_root(
            portable_config(desktop_root),
            system_name="Windows",
            executable=executable,
            frozen=True,
        )


def test_stage_rejects_over_budget_before_download_or_helper(tmp_path: Path):
    client = mock.Mock()
    helper = mock.Mock()
    preflight = mock.Mock()
    manager = AgentUpdateManager(
        portable_config(tmp_path, agent_update_max_mb=1),
        client,
        popen_factory=helper,
        preflight_runner=preflight,
    )

    with pytest.raises(AgentUpdateError, match="local size limit"):
        manager.stage(
            payload(size=1024 * 1024 + 1),
            AgentState.new(AGENT_ID, "secret"),
            COMMAND_ID,
        )

    client.download_agent_update.assert_not_called()
    helper.assert_not_called()
    preflight.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_stage_downloads_preflights_and_launches_only_fixed_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "portable"
    prepare_root(root)
    archive = release_zip()
    metadata = payload(size=len(archive), sha256=hashlib.sha256(archive).hexdigest())
    calls = []

    class Client:
        def download_agent_update(self, _state, path, destination, **expected):
            assert path == metadata["download_url"]
            assert expected["expected_size"] == len(archive)
            assert expected["max_bytes"] == 1024 * 1024
            destination.write_bytes(archive)

    class Launched:
        pid = 999

        def poll(self):
            return None

        def terminate(self):
            return None

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        ready_path = Path(argv[argv.index("-ReadyPath") + 1])
        ready_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "activation_id": argv[argv.index("-ActivationId") + 1],
                    "command_id": argv[argv.index("-CommandId") + 1],
                    "agent_id": argv[argv.index("-AgentId") + 1],
                    "updater_pid": 999,
                }
            ),
            encoding="utf-8",
        )
        return Launched()

    system_root = tmp_path / "Windows"
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"powershell")
    monkeypatch.setenv("SystemRoot", str(system_root))
    runner = mock.Mock(
        return_value=subprocess.CompletedProcess(
            ["RigPilotAgent.exe", "--version"],
            0,
                        f"RigPilot Agent {NEXT_VERSION}\n",
            "",
        )
    )
    manager = AgentUpdateManager(
        portable_config(root, agent_update_max_mb=1),
        Client(),
        system_name="Windows",
        executable=root / "RigPilotAgent.exe",
        frozen=True,
        portable_root=root,
        popen_factory=popen,
        preflight_runner=runner,
    )

    result = manager.stage(
        metadata, AgentState.new(AGENT_ID, "secret"), COMMAND_ID
    )

    assert result["state"] == "staged"
    assert result["activation_scheduled"] is True
    assert result["restart_required"] is True
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[0] == str(powershell)
    assert "RigPilotAgentUpdater.ps1" in " ".join(argv)
    assert options["shell"] is False
    assert options["creationflags"] & 0x01000000
    assert options["creationflags"] & 0x00000008 == 0


class _RunningUpdateMiner:
    """Same owned miner state for successful handoff and failed staging recovery."""

    def __init__(self):
        self.running_now = True
        self.stop_calls = 0
        self.restart_calls = 0

    def running(self):
        return (
            {"miner_type": "xmrig", "flight_sheet_id": "sheet"}
            if self.running_now else None
        )

    def stop(self):
        self.stop_calls += 1
        self.running_now = False
        return {"stopped": True, "miner_type": "xmrig", "flight_sheet_id": "sheet"}

    def restart(self):
        self.restart_calls += 1
        self.running_now = True
        return {"restarted": True, "miner_type": "xmrig", "flight_sheet_id": "sheet"}


def test_stage_stops_running_miner_and_persists_bound_resume_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "portable"
    prepare_root(root)
    archive = release_zip()
    metadata = payload(size=len(archive), sha256=hashlib.sha256(archive).hexdigest())
    state = AgentState.new(AGENT_ID, "secret")

    miner = _RunningUpdateMiner()

    class Client:
        def download_agent_update(self, _state, _path, destination, **_expected):
            destination.write_bytes(archive)

    class Launched:
        pid = 999

        def poll(self):
            return None

        def terminate(self):
            return None

    def popen(argv, **_kwargs):
        ready_path = Path(argv[argv.index("-ReadyPath") + 1])
        ready_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "activation_id": argv[argv.index("-ActivationId") + 1],
                    "command_id": COMMAND_ID,
                    "agent_id": AGENT_ID,
                    "updater_pid": 999,
                }
            ),
            encoding="utf-8",
        )
        return Launched()

    system_root = tmp_path / "Windows"
    powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"powershell")
    monkeypatch.setenv("SystemRoot", str(system_root))

    manager = AgentUpdateManager(
        portable_config(root, managed_mining_enabled=True),
        Client(),
        system_name="Windows",
        executable=root / "RigPilotAgent.exe",
        frozen=True,
        portable_root=root,
        popen_factory=popen,
        preflight_runner=mock.Mock(
                return_value=subprocess.CompletedProcess(
                            [], 0, f"RigPilot Agent {NEXT_VERSION}\n", ""
                )
        ),
        managed_miner_factory=lambda _config: miner,
    )

    result = manager.stage(metadata, state, COMMAND_ID)

    marker = json.loads(
        (
            root
            / "data/agent-updates/miner-resume"
            / f"{result['activation_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert marker == {
        "schema_version": 1,
        "activation_id": result["activation_id"],
        "command_id": COMMAND_ID,
        "agent_id": AGENT_ID,
        "miner_type": "xmrig",
        "flight_sheet_id": "sheet",
        "resume_required": True,
    }
    assert miner.stop_calls == 1
    assert miner.restart_calls == 0


def test_failed_stage_immediately_resumes_and_removes_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "portable"
    prepare_root(root)
    archive = release_zip()
    metadata = payload(size=len(archive), sha256=hashlib.sha256(archive).hexdigest())

    class Client:
        def download_agent_update(self, _state, _path, destination, **_expected):
            destination.write_bytes(archive)

    miner = _RunningUpdateMiner()
    system_root = tmp_path / "Windows"
    powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"powershell")
    monkeypatch.setenv("SystemRoot", str(system_root))
    manager = AgentUpdateManager(
        portable_config(root, managed_mining_enabled=True),
        Client(),
        system_name="Windows",
        executable=root / "RigPilotAgent.exe",
        frozen=True,
        portable_root=root,
        popen_factory=mock.Mock(side_effect=OSError("launch failed")),
        preflight_runner=mock.Mock(
                return_value=subprocess.CompletedProcess(
                        [], 0, f"RigPilot Agent {NEXT_VERSION}\n", ""
            )
        ),
        managed_miner_factory=lambda _config: miner,
    )

    with pytest.raises(OSError, match="launch failed"):
        manager.stage(metadata, AgentState.new(AGENT_ID, "secret"), COMMAND_ID)

    assert miner.stop_calls == 1
    assert miner.restart_calls == 1
    assert miner.running_now is True
    assert list((root / "data/agent-updates/miner-resume").glob("*.json")) == []


def test_resume_failure_keeps_marker_for_retry(tmp_path: Path):
    root = tmp_path / "portable"
    prepare_root(root)
    state = AgentState.new(AGENT_ID, "secret")
    activation_id = "c" * 32
    marker = root / "data/agent-updates/miner-resume" / f"{activation_id}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "agent_id": AGENT_ID,
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet",
                "resume_required": True,
            }
        ),
        encoding="utf-8",
    )

    class Miner:
        attempts = 0
        running_now = False

        def running(self):
            return (
                {"miner_type": "xmrig", "flight_sheet_id": "sheet"}
                if self.running_now
                else None
            )

        def restart(self):
            self.attempts += 1
            if self.attempts == 1:
                raise ManagedMinerError("temporary start failure")
            self.running_now = True
            return {
                "restarted": True,
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet",
            }

    miner = Miner()
    factory = lambda _config: miner
    with pytest.raises(AgentUpdateError, match="could not be resumed"):
        resume_managed_miner_after_update(
            portable_config(root, managed_mining_enabled=True),
            state,
            COMMAND_ID,
            activation_id,
            portable_root=root,
            managed_miner_factory=factory,
        )
    assert marker.exists()

    assert resume_managed_miner_after_update(
        portable_config(root, managed_mining_enabled=True),
        state,
        COMMAND_ID,
        activation_id,
        portable_root=root,
        managed_miner_factory=factory,
    ) is True
    assert not marker.exists()


def test_resume_accepts_newer_v1_only_schema2_marker(tmp_path: Path):
    root = tmp_path / "portable"
    prepare_root(root)
    state = AgentState.new(AGENT_ID, "secret")
    activation_id = "e" * 32
    marker = root / "data/agent-updates/miner-resume" / f"{activation_id}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "agent_id": AGENT_ID,
                "protocol": "v1",
                "miner_type": "srbminer",
                "flight_sheet_id": "sheet",
                "runtime_fingerprint": "a" * 64,
                "resume_required": True,
            }
        ),
        encoding="utf-8",
    )

    class Miner:
        def update_resume_identity(self):
            return {
                "miner_type": "srbminer",
                "flight_sheet_id": "sheet",
                "runtime_fingerprint": "a" * 64,
            }

        def running(self):
            return None

        def restart(self):
            return {
                "restarted": True,
                "miner_type": "srbminer",
                "flight_sheet_id": "sheet",
            }

    assert resume_managed_miner_after_update(
        portable_config(root, managed_mining_enabled=True),
        state,
        COMMAND_ID,
        activation_id,
        portable_root=root,
        managed_miner_factory=lambda _config: Miner(),
    ) is True
    assert not marker.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol": "v2", "miner_type": "declarative"},
        {"runtime_fingerprint": "not-a-digest"},
        {"unexpected": True},
        {"flight_sheet_id": "sheet\nother"},
    ],
)
def test_resume_rejects_non_v1_or_malformed_schema2_marker(
    tmp_path: Path, changes: dict[str, object]
):
    root = tmp_path / "portable"
    prepare_root(root)
    state = AgentState.new(AGENT_ID, "secret")
    activation_id = "f" * 32
    marker = root / "data/agent-updates/miner-resume" / f"{activation_id}.json"
    marker.parent.mkdir(parents=True)
    document = {
        "schema_version": 2,
        "activation_id": activation_id,
        "command_id": COMMAND_ID,
        "agent_id": AGENT_ID,
        "protocol": "v1",
        "miner_type": "srbminer",
        "flight_sheet_id": "sheet",
        "runtime_fingerprint": None,
        "resume_required": True,
    }
    document.update(changes)
    marker.write_text(json.dumps(document), encoding="utf-8")

    class Miner:
        def running(self):
            raise AssertionError("an invalid marker must not inspect mining state")

    with pytest.raises(AgentUpdateError, match="does not match"):
        resume_managed_miner_after_update(
            portable_config(root, managed_mining_enabled=True),
            state,
            COMMAND_ID,
            activation_id,
            portable_root=root,
            managed_miner_factory=lambda _config: Miner(),
        )
    assert marker.exists()


@pytest.mark.parametrize(
    "identity_changes",
    [
        {"runtime_fingerprint": "b" * 64},
        {"miner_type": "xmrig"},
        {"flight_sheet_id": "other-sheet"},
    ],
)
def test_schema2_resume_rejects_wrong_configured_v1_identity_before_restart(
    tmp_path: Path, identity_changes: dict[str, object]
):
    root = tmp_path / "portable"
    prepare_root(root)
    state = AgentState.new(AGENT_ID, "secret")
    activation_id = "9" * 32
    marker = root / "data/agent-updates/miner-resume" / f"{activation_id}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "activation_id": activation_id,
                "command_id": COMMAND_ID,
                "agent_id": AGENT_ID,
                "protocol": "v1",
                "miner_type": "srbminer",
                "flight_sheet_id": "sheet",
                "runtime_fingerprint": "a" * 64,
                "resume_required": True,
            }
        ),
        encoding="utf-8",
    )
    configured_identity = {
        "miner_type": "srbminer",
        "flight_sheet_id": "sheet",
        "runtime_fingerprint": "a" * 64,
    }
    configured_identity.update(identity_changes)

    class Miner:
        def update_resume_identity(self):
            return configured_identity

        def running(self):
            raise AssertionError("identity drift must fail before process inspection")

        def restart(self):
            raise AssertionError("identity drift must fail before restart")

    with pytest.raises(AgentUpdateError, match="does not match"):
        resume_managed_miner_after_update(
            portable_config(root, managed_mining_enabled=True),
            state,
            COMMAND_ID,
            activation_id,
            portable_root=root,
            managed_miner_factory=lambda _config: Miner(),
        )
    assert marker.exists()


def test_update_never_creates_resume_permission_when_miner_was_not_running(
    tmp_path: Path,
):
    root = tmp_path / "portable"
    prepare_root(root)
    state = AgentState.new(AGENT_ID, "secret")

    class Miner:
        def running(self):
            return None

        def stop(self):
            raise AssertionError("a stopped miner must not be stopped or restarted")

    assert _stop_managed_miner_for_update(
        portable_config(root, managed_mining_enabled=True),
        root,
        state,
        COMMAND_ID,
        "d" * 32,
        Miner(),
    ) is False
    assert not (root / "data/agent-updates/miner-resume").exists()


def _update_residue_finding(
    role: str,
    *,
    status: str = "no_record",
    clearable: bool = False,
    process_verified: bool = False,
    candidate: str | None = None,
) -> dict:
    has_record = status != "no_record"
    return {
        "role": role,
        "status": status,
        "miner_type": "pearlminer" if has_record else None,
        "flight_sheet_id": "sheet" if has_record else None,
        "process_verified": process_verified,
        "clearable": clearable,
        "manual_candidates": (
            [{"pid": 303, "candidate_fingerprint": candidate}]
            if candidate is not None
            else []
        ),
        "target_fingerprint": (
            {"legacy": "a", "primary": "b", "secondary": "c"}[role] * 64
        ),
    }


class _UpdateResidueMiner:
    def __init__(self, findings: list[dict]) -> None:
        self.findings = findings
        self.cleaned = False
        self.cleanup_calls: list[tuple[str, list[dict[str, str]]]] = []

    def running(self):
        raise ManagedMinerError("stale managed miner PID creation time")

    def update_resume_identity(self):
        return {"miner_type": "pearlminer", "flight_sheet_id": "sheet"}

    def scan_residue(self):
        findings = (
            [
                _update_residue_finding("legacy"),
                _update_residue_finding("primary"),
                _update_residue_finding("secondary"),
            ]
            if self.cleaned
            else self.findings
        )
        return {"scan_fingerprint": "f" * 64, "findings": findings}

    def cleanup_residue(self, fingerprint, targets):
        self.cleanup_calls.append((fingerprint, targets))
        self.cleaned = True
        return {
            "blocked": False,
            "selected_targets": targets,
            "results": [
                {
                    "role": target["role"],
                    "status": (
                        "explicit_candidate_stopped"
                        if "candidate_fingerprint" in target
                        else "invalid_record_removed"
                        if target["role"] == "legacy"
                        and self.findings[0]["status"] == "invalid_record"
                        else "orphan_record_removed"
                    ),
                    "process_stopped": "candidate_fingerprint" in target,
                    "record_removed": True,
                }
                for target in targets
            ],
        }


def test_update_stops_unique_trusted_path_residue_and_preserves_resume_intent(
    tmp_path: Path,
):
    root = tmp_path / "portable"
    prepare_root(root)
    state = AgentState.new(AGENT_ID, "secret")
    candidate = "c" * 64
    miner = _UpdateResidueMiner(
        [
            _update_residue_finding(
                "legacy",
                status="ambiguous",
                candidate=candidate,
            ),
            _update_residue_finding("primary"),
            _update_residue_finding("secondary"),
        ]
    )

    assert _stop_managed_miner_for_update(
        portable_config(root, managed_mining_enabled=True),
        root,
        state,
        COMMAND_ID,
        "e" * 32,
        miner,
    ) is True

    assert miner.cleanup_calls == [
        (
            "f" * 64,
            [
                {
                    "role": "legacy",
                    "target_fingerprint": "a" * 64,
                    "candidate_fingerprint": candidate,
                }
            ],
        )
    ]
    marker = json.loads(
        (
            root
            / "data/agent-updates/miner-resume"
            / f"{'e' * 32}.json"
        ).read_text(encoding="utf-8")
    )
    assert marker["miner_type"] == "pearlminer"
    assert marker["flight_sheet_id"] == "sheet"
    assert marker["resume_required"] is True


def test_update_rejects_ambiguous_residue_without_unique_candidate(
    tmp_path: Path,
):
    root = tmp_path / "portable"
    prepare_root(root)
    miner = _UpdateResidueMiner(
        [
            _update_residue_finding("legacy", status="ambiguous"),
            _update_residue_finding("primary"),
            _update_residue_finding("secondary"),
        ]
    )

    with pytest.raises(AgentUpdateError, match="not uniquely identifiable"):
        _stop_managed_miner_for_update(
            portable_config(root, managed_mining_enabled=True),
            root,
            AgentState.new(AGENT_ID, "secret"),
            COMMAND_ID,
            "1" * 32,
            miner,
        )

    assert miner.cleanup_calls == []
    assert not (root / "data/agent-updates/miner-resume").exists()


def test_update_clears_proven_orphan_without_creating_resume_permission(
    tmp_path: Path,
):
    root = tmp_path / "portable"
    prepare_root(root)
    miner = _UpdateResidueMiner(
        [
            _update_residue_finding(
                "legacy",
                status="orphan_record",
                clearable=True,
            ),
            _update_residue_finding("primary"),
            _update_residue_finding("secondary"),
        ]
    )

    assert _stop_managed_miner_for_update(
        portable_config(root, managed_mining_enabled=True),
        root,
        AgentState.new(AGENT_ID, "secret"),
        COMMAND_ID,
        "2" * 32,
        miner,
    ) is False

    assert miner.cleanup_calls
    assert not (root / "data/agent-updates/miner-resume").exists()


def test_update_clears_invalid_record_only_with_absence_proof_and_no_resume_permission(
    tmp_path: Path,
):
    root = tmp_path / "portable"
    prepare_root(root)
    miner = _UpdateResidueMiner(
        [
            _update_residue_finding(
                "legacy",
                status="invalid_record",
                clearable=True,
            ),
            _update_residue_finding("primary"),
            _update_residue_finding("secondary"),
        ]
    )

    assert _stop_managed_miner_for_update(
        portable_config(root, managed_mining_enabled=True),
        root,
        AgentState.new(AGENT_ID, "secret"),
        COMMAND_ID,
        "4" * 32,
        miner,
    ) is False

    assert miner.cleanup_calls == [
        ("f" * 64, [{"role": "legacy", "target_fingerprint": "a" * 64}])
    ]
    assert not (root / "data/agent-updates/miner-resume").exists()


def test_update_recovers_when_verified_miner_changes_pid_during_stop(
    tmp_path: Path,
):
    root = tmp_path / "portable"
    prepare_root(root)
    candidate = "d" * 64
    miner = _UpdateResidueMiner(
        [
            _update_residue_finding(
                "legacy",
                status="ambiguous",
                candidate=candidate,
            ),
            _update_residue_finding("primary"),
            _update_residue_finding("secondary"),
        ]
    )
    miner.running = lambda: {
        "miner_type": "pearlminer",
        "flight_sheet_id": "sheet",
    }
    miner.stop = lambda: (_ for _ in ()).throw(
        ManagedMinerError("PID creation time no longer matches")
    )

    assert _stop_managed_miner_for_update(
        portable_config(root, managed_mining_enabled=True),
        root,
        AgentState.new(AGENT_ID, "secret"),
        COMMAND_ID,
        "3" * 32,
        miner,
    ) is True
    assert miner.cleanup_calls
    assert (
        root / "data/agent-updates/miner-resume" / f"{'3' * 32}.json"
    ).exists()


def test_confirmation_binds_activation_version_and_real_agent_identity(tmp_path: Path):
    root = tmp_path / "portable"
    prepare_root(root)
    executable = root / "versions" / CURRENT_VERSION / "RigPilotAgent.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"agent")
    activation = "a" * 32
    (root / "data" / "active-agent.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": CURRENT_VERSION,
                "relative_executable": f"versions/{CURRENT_VERSION}/RigPilotAgent.exe",
                "activation_id": activation,
                "command_id": COMMAND_ID,
            }
        ),
        encoding="utf-8",
    )
    state = AgentState.new(AGENT_ID, "secret")
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Windows"),
        mock.patch(
            "agent.agent_updates._portable_root",
            return_value=(root, executable.resolve()),
        ),
    ):
        from agent.agent_updates import note_pending_agent_heartbeat

        assert note_pending_agent_heartbeat(portable_config(root), state) is None
        metadata = note_pending_agent_heartbeat(portable_config(root), state)
        assert metadata is not None
        assert confirm_pending_agent_update(portable_config(root), state, metadata) is True
        assert mark_activation_reported(portable_config(root), state, metadata) is True
        assert note_pending_agent_heartbeat(portable_config(root), state) is None
    confirmation = json.loads(
        (
            root
            / "data"
            / "agent-updates"
            / "confirmations"
            / f"{activation}.json"
        ).read_text(encoding="utf-8")
    )
    assert confirmation["agent_id"] == AGENT_ID


def test_rollback_receipt_is_bound_to_command_and_agent(tmp_path: Path):
    root = tmp_path / "portable"
    prepare_root(root)
    executable = root / "RigPilotAgent.exe"
    executable.write_bytes(b"old agent")
    activation = "c" * 32
    job = root / "data" / "agent-updates" / "staged" / activation
    job.mkdir(parents=True)
    (job / "activation-result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "rolled_back",
                "version": "0.5.8",
                "activation_id": activation,
                "command_id": COMMAND_ID,
                "agent_id": AGENT_ID,
                "error": "replacement did not reconnect",
            }
        ),
        encoding="utf-8",
    )
    state = AgentState.new(AGENT_ID, "secret")
    with (
        mock.patch("agent.agent_updates.platform.system", return_value="Windows"),
        mock.patch(
            "agent.agent_updates._portable_root",
            return_value=(root, executable.resolve()),
        ),
    ):
        receipts = pending_rollback_results(portable_config(root), state)

    assert len(receipts) == 1
    marker, command_id, response = receipts[0]
    assert marker == job / "rollback-reported.json"
    assert command_id == COMMAND_ID
    assert response["status"] == "failed"
    assert response["result"]["rolled_back"] is True


def test_confirmed_release_root_sync_is_manifest_bounded_and_idempotent(
    tmp_path: Path,
):
    root = tmp_path / "portable"
    prepare_root(root)
    state = AgentState.new(AGENT_ID, "secret")
    version = CURRENT_VERSION
    activation = "e" * 32
    slot = root / "versions" / version
    slot.mkdir(parents=True)

    old_launcher = (root / "portable-agent.ps1").read_bytes()
    old_updater = (root / "RigPilotAgentUpdater.ps1").read_bytes()
    (root / "Enable-Power-Actions.bat").write_bytes(b"old shortcut")
    (root / "old.dll").write_bytes(b"old program")
    (root / "unknown-user-file.txt").write_bytes(b"preserve me")
    (root / "data" / "identity.sentinel").write_bytes(b"identity")
    old_slot = root / "versions" / "0.5.9"
    old_slot.mkdir(parents=True)
    (old_slot / "preserve.sentinel").write_bytes(b"old version")

    old_files = {
        "portable-agent.ps1": hashlib.sha256(old_launcher).hexdigest(),
        "RigPilotAgentUpdater.ps1": hashlib.sha256(old_updater).hexdigest(),
        "Enable-Power-Actions.bat": hashlib.sha256(b"old shortcut").hexdigest(),
        "old.dll": hashlib.sha256(b"old program").hexdigest(),
        "missing/tree/old.dll": hashlib.sha256(b"missing").hexdigest(),
    }
    (root / "agent-release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "0.5.9",
                "platform": "windows",
                "architecture": "x86_64",
                "files": old_files,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    release_files = {
        "RigPilotAgent.exe": b"new agent",
        "portable-agent.ps1": b"new launcher that v1 must retain",
        "RigPilotAgentUpdater.ps1": b"new updater that v1 must retain",
        "Start-RigPilotAgent.bat": b"@echo off\r\n",
        "Install-Autostart.bat": b"@echo off\r\n",
        "_internal/library.zip": b"runtime",
    }
    for relative, value in release_files.items():
        destination = slot.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(value)
    release_manifest = {
        "schema_version": 1,
        "version": version,
        "platform": "windows",
        "architecture": "x86_64",
        "files": {
            relative: hashlib.sha256(value).hexdigest()
            for relative, value in release_files.items()
        },
    }
    (slot / "agent-release.json").write_text(
        json.dumps(release_manifest, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    (root / "data" / "active-agent.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "relative_executable": f"versions/{version}/RigPilotAgent.exe",
                "activation_id": activation,
                "command_id": COMMAND_ID,
            }
        ),
        encoding="utf-8",
    )
    reported = root / "data" / "agent-updates" / "reported" / f"{activation}.json"
    reported.parent.mkdir(parents=True)
    reported.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activation_id": activation,
                "command_id": COMMAND_ID,
                "version": version,
                "agent_id": state.agent_id,
                "reported": True,
            }
        ),
        encoding="utf-8",
    )
    result = (
        root
        / "data"
        / "agent-updates"
        / "staged"
        / activation
        / "activation-result.json"
    )
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "succeeded",
                "version": version,
                "activation_id": activation,
                "command_id": COMMAND_ID,
                "agent_id": state.agent_id,
                "stable_launcher_retained": True,
                "stable_updater_retained": True,
            }
        ),
        encoding="utf-8",
    )
    old_time = time.time() - 3
    os.utime(result, (old_time, old_time))

    config = portable_config(root)
    executable = slot / "RigPilotAgent.exe"
    assert (
        reconcile_confirmed_release_root(
            config,
            state,
            system_name="Windows",
            executable=executable,
            frozen=True,
            root_override=root,
        )
        is True
    )

    assert (root / "RigPilotAgent.exe").read_bytes() == b"new agent"
    assert (root / "portable-agent.ps1").read_bytes() == old_launcher
    assert (root / "RigPilotAgentUpdater.ps1").read_bytes() == old_updater
    assert not (root / "Enable-Power-Actions.bat").exists()
    assert not (root / "old.dll").exists()
    assert not (root / "missing").exists()
    assert (root / "unknown-user-file.txt").read_bytes() == b"preserve me"
    assert (root / "data" / "identity.sentinel").read_bytes() == b"identity"
    assert (old_slot / "preserve.sentinel").read_bytes() == b"old version"

    installed = json.loads((root / "agent-release.json").read_text(encoding="utf-8"))
    assert installed["files"]["portable-agent.ps1"] == hashlib.sha256(
        old_launcher
    ).hexdigest()
    assert installed["files"]["RigPilotAgentUpdater.ps1"] == hashlib.sha256(
        old_updater
    ).hexdigest()
    ready = json.loads(
        (root / "data" / "agent-update-ready.json").read_text(encoding="utf-8")
    )
    assert ready["updater_sha256"] == hashlib.sha256(old_updater).hexdigest()

    import agent.agent_updates as update_module

    original_reader = update_module._read_installed_release_manifest
    with mock.patch.object(
        update_module,
        "_read_installed_release_manifest",
        wraps=original_reader,
    ) as reader:
        assert (
            reconcile_confirmed_release_root(
                config,
                state,
                system_name="Windows",
                executable=executable,
                frozen=True,
                root_override=root,
            )
            is False
        )
    assert reader.call_count == 1
    assert reader.call_args.kwargs["verify_files"] is False


def test_confirmed_release_root_sync_rejects_raw_versions_reparse(tmp_path: Path):
    root = tmp_path / "portable"
    prepare_root(root)
    slot = root / "versions" / CURRENT_VERSION
    slot.mkdir(parents=True)
    executable = slot / "RigPilotAgent.exe"
    executable.write_bytes(b"agent")
    protected = root / "data" / "protected.sentinel"
    protected.write_bytes(b"must remain untouched")

    config = portable_config(root)
    state = AgentState.new(AGENT_ID, "secret")

    import agent.agent_updates as update_module

    original_check = update_module._is_link_or_reparse
    versions_root = root / "versions"

    def report_versions_as_reparse(path: Path) -> bool:
        if Path(path) == versions_root:
            return True
        return original_check(path)

    with mock.patch.object(
        update_module,
        "_is_link_or_reparse",
        side_effect=report_versions_as_reparse,
    ) as reparse_check:
        with pytest.raises(
            AgentUpdateError,
            match="program synchronization path is missing, linked, or unsafe",
        ):
            reconcile_confirmed_release_root(
                config,
                state,
                system_name="Windows",
                executable=executable,
                frozen=True,
                root_override=root,
            )

    assert any(Path(call.args[0]) == versions_root for call in reparse_check.call_args_list)
    assert protected.read_bytes() == b"must remain untouched"
    assert executable.read_bytes() == b"agent"


def test_linux_update_diagnostic_reads_only_a_bounded_tail():
    import agent.agent_updates as updates

    raw = b"old progress\n" * 2000 + b"important final error\n"
    reads = []

    class Stream(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 128
            reads.append(size)
            return super().read(size)

    class LogPath:
        def open(self, mode):
            assert mode == "rb"
            return Stream(raw)

        def read_bytes(self):
            pytest.fail("diagnostic must not read the entire log")

    result = updates._linux_update_log_detail(LogPath(), limit=128)
    assert result == " ".join(raw[-128:].decode().split())[-1200:]
    assert reads == [128]


def test_linux_tar_header_limit_rejects_after_max_plus_one_before_extraction(tmp_path, monkeypatch):
    import tarfile
    import agent.agent_updates as updates

    seen = []

    class Archive:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def __iter__(self):
            for index in range(40):
                seen.append(index)
                info = tarfile.TarInfo(f"bundle/directory-{index}")
                info.type = tarfile.DIRTYPE
                yield info

        def getmembers(self):
            return list(self)

        def extractfile(self, _info):
            pytest.fail("over-limit archive must not extract a file")

    monkeypatch.setattr(updates, "_MAX_ARCHIVE_ENTRIES", 4)
    monkeypatch.setattr(updates.tarfile, "open", lambda *_args, **_kwargs: Archive())
    destination = tmp_path / "stage"
    with pytest.raises(AgentUpdateError, match="entry count"):
        updates._validate_and_extract_linux_release(
            tmp_path / "package.tar.gz", destination,
            expected_version=NEXT_VERSION, expanded_limit=1024,
        )
    assert seen == list(range(5))
    assert not destination.exists()


def _pending_review_context(tmp_path, monkeypatch):
    import agent.agent_updates as module
    root = tmp_path / "portable"
    prepare_root(root)
    executable = root / "versions" / CURRENT_VERSION / "RigPilotAgent.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"agent")
    metadata = {"activation_id": "a" * 32, "command_id": COMMAND_ID,
                "version": CURRENT_VERSION, "agent_id": AGENT_ID}
    pointer = {"schema_version": 1, "relative_executable": f"versions/{CURRENT_VERSION}/RigPilotAgent.exe",
               **{key: value for key, value in metadata.items() if key != "agent_id"}}
    (root / "data/active-agent.json").write_text(json.dumps(pointer))
    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(module, "_portable_root", lambda _config: (root, executable.resolve()))
    return module, root, portable_config(root), AgentState.new(AGENT_ID, "secret"), metadata


@pytest.mark.parametrize("previous, expected", [(True, 1), (0, 1), (1, 2)])
def test_pending_health_counts_only_integer_heartbeats(tmp_path, monkeypatch, previous, expected):
    module, root, config, state, metadata = _pending_review_context(tmp_path, monkeypatch)
    health = root / "data/agent-updates/health" / f"{metadata['activation_id']}.json"
    health.parent.mkdir(parents=True)
    health.write_text(json.dumps({**metadata, "health_heartbeats": previous}))
    result = module.note_pending_agent_heartbeat(config, state)
    assert json.loads(health.read_text())["health_heartbeats"] == expected
    assert (result is not None) is (expected == 2)


@pytest.mark.parametrize("reader", ["activation", "reported", "health", "rollback", "manifest", "resume"])
def test_unparseable_update_integer_keeps_existing_invalid_classification(tmp_path, monkeypatch, reader):
    module, root, config, state, metadata = _pending_review_context(tmp_path, monkeypatch)
    raw = '{"invalid":' + '9' * 5000 + '}'
    paths = {
        "activation": root / "data/active-agent.json",
        "reported": root / "data/agent-updates/reported" / f"{metadata['activation_id']}.json",
        "health": root / "data/agent-updates/health" / f"{metadata['activation_id']}.json",
        "rollback": root / "data/agent-updates/staged" / metadata['activation_id'] / "activation-result.json",
        "manifest": root / module.UPDATE_MANIFEST_NAME,
        "resume": root / "data/agent-updates/miner-resume" / f"{metadata['activation_id']}.json",
    }
    path = paths[reader]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    if reader in {"activation", "reported"}:
        assert module._pending_activation(config, state) is None
    elif reader == "health":
        assert module.note_pending_agent_heartbeat(config, state) is None
    elif reader == "rollback":
        assert module.pending_rollback_results(config, state) == []
    else:
        with pytest.raises(AgentUpdateError, match="invalid"):
            if reader == "manifest":
                module._read_installed_release_manifest(root, expected_version=CURRENT_VERSION, verify_files=False)
            else:
                module._load_miner_resume_marker(path, state=state, command_id=COMMAND_ID, activation_id=metadata['activation_id'])
    assert path.read_text() == raw
