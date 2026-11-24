from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[2]
LINUX = ROOT / "deploy" / "linux"
PORTABLE = LINUX / "portable"


def _load_linux_updater():
    spec = importlib.util.spec_from_file_location(
        "rigpilot_linux_updater", PORTABLE / "RigPilotAgentUpdater.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _builder_function(name: str) -> str:
    lines = (LINUX / "build-ubuntu-portable.sh").read_text().splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(name + "() {"))
    heredoc = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
        else:
            match = re.search(r"<<'([A-Z_]+)'", line)
            if match:
                heredoc = match.group(1)
            elif line.strip() == "}":
                return "".join(lines[start:index + 1])
    raise AssertionError("Incomplete builder function: " + name)


def _run_builder_functions(names, command: str, *arguments: Path):
    script = "set -Eeuo pipefail\n"
    script += "PROJECT_ROOT=" + shlex.quote(str(ROOT)) + "\n"
    script += "PYTHON=" + shlex.quote(sys.executable) + "\nVERSION=9.8.7\n"
    script += "\n".join(_builder_function(name) for name in names)
    return subprocess.run(
        ["bash", "-c", script + "\n" + command, "isolated-builder-functions", *map(str, arguments)],
        capture_output=True, text=True, check=False, timeout=20,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )


def test_release_text_normalization_is_staged_and_archive_boundary_rejects_cr(tmp_path: Path):
    root = tmp_path / "payload"
    root.mkdir()
    (root / "Install.sh").write_bytes(b"line1\r\nline2\rline3\n")
    (root / "opaque.bin").write_bytes(b"binary\r\n")
    result = _run_builder_functions(
        ["normalize_linux_text_tree", "verify_linux_text_tree_lf"],
        'normalize_linux_text_tree "$1"; verify_linux_text_tree_lf "$1"', root,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "Install.sh").read_bytes() == b"line1\nline2\nline3\n"
    assert (root / "opaque.bin").read_bytes() == b"binary\r\n"
    import tarfile
    for valid in (True, False):
        (root / "VERSION").write_bytes(b"9.8.7\n" if valid else b"9.8.7\r\n")
        archive = tmp_path / ("valid.tar.gz" if valid else "invalid.tar.gz")
        with tarfile.open(archive, "w:gz") as output:
            output.add(root, arcname="payload")
        checked = _run_builder_functions(
            ["verify_linux_archive_text_lf"], 'verify_linux_archive_text_lf "$1"', archive,
        )
        assert (checked.returncode == 0) is valid
        if not valid:
            assert "non-LF text" in checked.stderr


@pytest.mark.parametrize("collision", [None, "archive", "sidecar", "archive-directory"])
def test_release_publication_preserves_existing_outputs(tmp_path: Path, collision):
    archive, stage, hash_stage = (tmp_path / name for name in ("release.tar.gz", "archive.part", "hash.part"))
    sidecar = archive.with_name(archive.name + ".sha256")
    stage.write_bytes(b"new archive")
    hash_stage.write_bytes(b"new hash")
    if collision == "archive-directory":
        archive.mkdir()
    elif collision:
        (archive if collision == "archive" else sidecar).write_bytes(b"existing output")
    result = _run_builder_functions(
        ["fail", "publish_release_pair"], 'publish_release_pair "$1" "$2" "$3"',
        stage, archive, hash_stage,
    )
    assert (result.returncode == 0) is (collision is None), result.stderr
    if collision == "archive-directory":
        assert archive.is_dir() and not list(archive.iterdir())
        assert not sidecar.exists()
        return
    assert archive.read_bytes() == (b"existing output" if collision == "archive" else b"new archive")
    if collision != "archive":
        assert sidecar.read_bytes() == (b"existing output" if collision == "sidecar" else b"new hash")
    else:
        assert not sidecar.exists()



def test_ubuntu_package_assets_and_systemd_contract():
    for name in (
        "build-ubuntu-portable.sh",
        "linux_agent_entry.py",
        "requirements-build.txt",
        "README.md",
    ):
        assert (LINUX / name).is_file()
    for name in (
        "Install.sh",
        "Start-Portable.sh",
        "Remove-Systemd.sh",
        "RigPilotAgentLauncher.sh",
        "RigPilotAgentUpdater.py",
        "rigpilot-agent.service",
        "rigpilot-agent-supervision.conf",
        "rigpilot-agent-update-recovery.service",
        "rigpilot-agent-update-recovery.timer",
        "README-Ubuntu.txt",
    ):
        assert (PORTABLE / name).is_file()
    # Removed 0.8.58 features must not ship in the package at all.
    assert not (PORTABLE / "Setup-HiveOS-Runtime.sh").exists()
    assert not (PORTABLE / "rigpilot-agent-remote-sessions.conf").exists()

    service = (PORTABLE / "rigpilot-agent.service").read_text(encoding="utf-8")
    assert (
        "ExecStart=/opt/rigpilot-agent/RigPilotAgentLauncher.sh "
        "--config /etc/rigpilot-agent/config.json"
    ) in service
    assert "WorkingDirectory=/var/lib/rigpilot-agent" in service
    assert "ReadWritePaths=/var/lib/rigpilot-agent" in service
    assert "KillMode=process" in service
    assert "Restart=on-failure" in service
    assert "User=root" in service and "UMask=0077" in service
    assert "ProtectHome=true" in service
    assert "ProtectSystem=strict" in service
    writable_line = next(
        line for line in service.splitlines() if line.startswith("ReadWritePaths=")
    )
    assert "/opt/rigpilot-agent" not in writable_line

    supervision = (PORTABLE / "rigpilot-agent-supervision.conf").read_text(
        encoding="utf-8"
    )
    assert "Type=notify" in supervision
    assert "NotifyAccess=all" in supervision
    assert "WatchdogSec=120s" in supervision
    assert "Restart=on-failure" in supervision
    assert "KillMode=process" in supervision
    assert "TimeoutStartSec=45s" in supervision
    from agent.linux_supervision import SUPERVISION_CONTENT

    assert supervision.encode("utf-8") == SUPERVISION_CONTENT

    recovery = (PORTABLE / "rigpilot-agent-update-recovery.service").read_text(
        encoding="utf-8"
    )
    assert "RigPilotAgentUpdater.py recover" in recovery
    assert "ConditionPathExists=|/opt/rigpilot-agent/pending-update.json" in recovery
    assert "ConditionPathExists=|/opt/rigpilot-agent/updater-state.json" in recovery
    assert "TimeoutStartSec=105s" in recovery
    assert "RuntimeMaxSec=105s" in recovery
    timer = (PORTABLE / "rigpilot-agent-update-recovery.timer").read_text(
        encoding="utf-8"
    )
    assert "OnBootSec=" in timer and "OnUnitInactiveSec=" in timer
    assert "OnUnitActiveSec=" not in timer
    assert "Persistent=" not in timer

    assert not (PORTABLE / "rigpilot-agent-wifi.conf").exists()


def test_ubuntu_installer_preserves_identity_and_enables_fixed_updater():
    installer = (PORTABLE / "Install.sh").read_text(encoding="utf-8")
    assert 'CONTROLLER_DEFAULT="https://controller.example.invalid"' in installer
    assert 'STATE_FILE="${DATA_DIR}/state.json"' in installer
    assert 'if [[ ! -s "${STATE_FILE}" ]]' in installer
    assert 'RIGPILOT_ENROLLMENT_TOKEN="${ENROLLMENT_TOKEN}"' in installer
    config_block = installer.split("value = {", 1)[1].split("fd = os.open", 1)[0]
    assert "enrollment_token" not in config_block
    assert '"allow_reboot": True' in config_block
    assert '"allow_shutdown": True' in config_block
    assert '"allow_agent_updates": True' in config_block
    assert '"allow_nvidia_driver_updates"' not in config_block
    assert '"allow_remote_sessions"' not in config_block
    assert '"allow_local_web"' not in config_block
    assert '"managed_mining_enabled": True' in config_block
    assert '"advertise_ssh": True' in config_block
    assert '"advertise_sftp": True' in config_block
    assert '"ssh_username": None' in config_block
    assert '"allow_wifi_management"' not in config_block
    assert 'value.pop("allow_wifi_management", None)' in installer
    assert 'value.pop("allow_nvidia_driver_updates", None)' in installer
    assert 'value.pop("allow_remote_sessions", None)' in installer
    assert 'value.pop("allow_hiveos_runtime_preparation", None)' in installer
    assert "Enable outbound Spark Shell/SFTP sessions? [y/N]" not in installer
    assert 'value["allow_agent_updates"] = True' in installer
    assert 'value["advertise_ssh"] = True' in installer
    assert 'value["advertise_sftp"] = True' in installer
    assert "RIGPILOT_REMOTE_SESSIONS" not in installer
    assert '${SOURCE_ROOT}/Setup-HiveOS-Runtime.sh' not in installer
    assert 'rm -f -- "${REMOTE_SESSION_DROPIN}"' in installer
    assert 'rm -f -- "${APP_DIR}/Setup-HiveOS-Runtime.sh"' in installer
    assert "check_legacy_wifi_dropin" in installer
    assert "legacy-wifi-dropin" in installer
    assert '"${SOURCE_ROOT}/rigpilot-agent-supervision.conf"' in installer
    assert 'SUPERVISION_DROPIN="${SERVICE_DROPIN_DIR}/supervision.conf"' in installer
    assert 'mv -fT -- "${SUPERVISION_STAGE}" "${SUPERVISION_DROPIN}"' in installer
    assert '"${SUPERVISION_DROPIN}:supervision-dropin"' in installer
    assert 'restore_file "${SUPERVISION_DROPIN}" supervision-dropin 0644' in installer
    assert 'parsed.username is None' in installer
    assert 'parsed.password is None' in installer
    assert "sha256sum --check --strict SHA256SUMS" in installer
    assert "systemctl enable rigpilot-agent.service rigpilot-agent-update-recovery.timer" in installer
    assert "systemctl restart rigpilot-agent.service" in installer
    assert "apt-get install -y --no-install-recommends openssh-server" in installer
    assert "PermitRootLogin no" in installer
    assert "PasswordAuthentication no" in installer
    assert "AllowUsers %s@10.0.0.0/8" in installer
    assert "100.64.0.0/10" in installer
    assert "sshd -t -f /etc/ssh/sshd_config" in installer
    assert 'sshd -T -C "user=${account},addr=192.168.1.2,host=rigpilot"' in installer
    assert "systemctl reload-or-restart ssh.service" in installer
    assert 'RIGPILOT_LAN_SSH:-enable' in installer
    assert "enable) configure_lan_openssh || true" in installer
    assert "preserve) ;;" in installer
    assert installer.index("COMMITTED=1") < installer.index(
        "enable) configure_lan_openssh || true"
    )


def test_ubuntu_uninstaller_removes_only_its_validated_ssh_dropin():
    remover = (PORTABLE / "Remove-Systemd.sh").read_text(encoding="utf-8")
    assert 'LAN_SSH_DROPIN="/etc/ssh/sshd_config.d/20-rigpilot-lan.conf"' in remover
    assert 'LAN_SSH_MARKER="# Managed by RigPilot:' in remover
    # Both RigPilot-authored first lines are removed: the current constant and
    # the earlier Spark wording still written by configure_linux_ssh.v1.
    assert 'LAN_SSH_LEGACY_MARKER="# Managed by RigPilot:' in remover
    assert (
        '[[ "${first_line}" != "${LAN_SSH_MARKER}" && '
        '"${first_line}" != "${LAN_SSH_LEGACY_MARKER}" ]]' in remover
    )
    assert "Leaving non-RigPilot SSH drop-in unchanged" in remover
    assert "sshd -t -f /etc/ssh/sshd_config" in remover
    assert "systemctl reload-or-restart ssh.service" in remover
    assert 'mv -fT -- "${backup}" "${LAN_SSH_DROPIN}"' in remover
    assert "systemctl disable --now ssh.service" not in remover
    assert remover.index("remove_managed_lan_ssh_dropin") < remover.index(
        "systemctl disable --now rigpilot-agent.service"
    )


def test_ubuntu_installer_creates_version_slot_and_rolls_back_activation():
    installer = (PORTABLE / "Install.sh").read_text(encoding="utf-8")
    assert 'SLOT="${VERSIONS_DIR}/${PACKAGE_VERSION}"' in installer
    assert '"${SLOT_STAGE}/RigPilotAgent" --version' in installer
    assert 'mv -T -- "${SLOT_STAGE}" "${SLOT}"' in installer
    assert '"${SOURCE_ROOT}/RigPilotAgentLauncher.sh"' in installer
    assert '"${SOURCE_ROOT}/RigPilotAgentUpdater.py"' in installer
    assert '"${PACKAGE_VERSION}" >"${APP_DIR}/.active-version.install.$$"' in installer
    assert 'ROLLBACK_DIR=$(mktemp -d "${APP_DIR}/.install-rollback.XXXXXXXXXX")' in installer
    assert 'restore_file "${APP_DIR}/RigPilotAgentLauncher.sh" launcher 0755' in installer
    assert 'restore_file "${ACTIVE_VERSION_FILE}" active-version 0644' in installer
    assert "systemctl daemon-reload" in installer
    assert "systemctl restart rigpilot-agent.service" in installer
    assert "systemctl is-active --quiet rigpilot-agent.service" in installer
    assert "Existing configuration and device identity were preserved." in installer


def test_release_manifest_collects_full_and_minimal_online_payloads(tmp_path: Path):
    for kind in ("full", "online"):
        root = tmp_path / kind
        root.mkdir(mode=0o755)
        (root / "RigPilotAgent").write_bytes(b"synthetic binary, never executed")
        (root / "RigPilotAgent").chmod(0o755)
        if kind == "full":
            (root / "Install.sh").write_text("synthetic installation asset\n")
            (root / "Install.sh").chmod(0o755)
        result = _run_builder_functions(
            ["write_release_manifest", "verify_staged_modes"],
            'write_release_manifest "$1"; verify_staged_modes "$1"', root,
        )
        assert result.returncode == 0, result.stderr
        manifest = json.loads((root / "agent-release.json").read_text())
        expected = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in root.iterdir() if path.name != "agent-release.json"}
        assert manifest == {"schema_version": 1, "version": "9.8.7", "platform": "linux",
                            "architecture": "x86_64", "files": expected}
        assert (root / "SHA256SUMS").exists() is False
        if kind == "online":
            assert set(path.name for path in root.iterdir()) == {"RigPilotAgent", "agent-release.json"}
        (root / "RigPilotAgent").chmod(0o777)
        unsafe = _run_builder_functions(["verify_staged_modes"], 'verify_staged_modes "$1"', root)
        assert unsafe.returncode != 0 and "unsafe staged mode" in unsafe.stderr



def test_single_binary_freezing_command_uses_only_current_agent_modules(tmp_path: Path):
    builder = (LINUX / "build-ubuntu-portable.sh").read_text()
    start = builder.index("PYTHONHASHSEED=0 SOURCE_DATE_EPOCH=0")
    end = builder.index('\nAGENT_BINARY=', start)
    recorded = tmp_path / "arguments.json"
    interpreter = tmp_path / "record-python"
    interpreter.write_text(
        "#!" + sys.executable + "\nimport json, os, sys\n"
        "with open(os.environ['RECORD_ARGUMENTS'], 'x') as out:\n"
        " json.dump({'argv': sys.argv[1:], 'hash_seed': os.environ.get('PYTHONHASHSEED')}, out)\n"
    )
    interpreter.chmod(0o755)
    variables = {
        "PROJECT_ROOT": ROOT, "SCRIPT_ROOT": LINUX, "WORK_ROOT": tmp_path / "work",
        "PYTHON": interpreter,
    }
    script = "set -Eeuo pipefail\n" + "\n".join(
        name + "=" + shlex.quote(str(value)) for name, value in variables.items()
    ) + "\n" + builder[start:end]
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=20,
        env={"PATH": os.defpath, "LC_ALL": "C", "RECORD_ARGUMENTS": str(recorded)},
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(recorded.read_text())
    args = evidence["argv"]
    assert args[:2] == ["-m", "PyInstaller"] and "--onefile" in args
    data = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--add-data"]
    assert data == []
    assert args[-1] == str(LINUX / "linux_agent_entry.py")
    assert evidence["hash_seed"] == "0"
    hidden = {args[i + 1] for i, value in enumerate(args[:-1]) if value == "--hidden-import"}
    assert not {"agent.miner_self_heal", "agent.nvidia_driver_reload"} & hidden
    assert not any(name.startswith("agent.hiveos_") for name in hidden)


def test_full_and_online_staging_copy_the_same_frozen_binary(tmp_path: Path):
    builder = (LINUX / "build-ubuntu-portable.sh").read_text()
    full_start = builder.index('PORTABLE="${WORK_ROOT}/RigPilotAgent-Ubuntu-x64"')
    full_end = builder.index('\nfor asset in ', full_start)
    online_start = builder.index('ONLINE="${WORK_ROOT}/RigPilotAgent-Ubuntu-x64-OnlineUpdate"')
    online_end = builder.index('\n[[ "$(find ', online_start)
    frozen = tmp_path / "frozen-agent"
    frozen.write_bytes(b"synthetic onefile Agent with embedded assets; never executed")
    frozen.chmod(0o755)
    command = ('WORK_ROOT="$1"\nAGENT_BINARY="$2"\n'
               + builder[full_start:full_end] + '\n' + builder[online_start:online_end])
    result = _run_builder_functions(
        ["write_release_manifest", "normalize_linux_text_tree", "verify_linux_text_tree_lf",
         "verify_staged_modes"], command, tmp_path, frozen,
    )
    assert result.returncode == 0, result.stderr
    full = tmp_path / "RigPilotAgent-Ubuntu-x64"
    online = tmp_path / "RigPilotAgent-Ubuntu-x64-OnlineUpdate"
    assert (full / "RigPilotAgent").read_bytes() == frozen.read_bytes()
    assert (online / "RigPilotAgent").read_bytes() == frozen.read_bytes()
    assert {p.name for p in online.iterdir()} == {"RigPilotAgent", "agent-release.json"}
    for root in (full, online):
        assert stat.S_IMODE((root / "RigPilotAgent").stat().st_mode) == 0o755




def test_documentation_describes_ssh_security_and_update_boundaries():
    readme = " ".join((PORTABLE / "README-Ubuntu.txt").read_text(encoding="utf-8").split())
    assert "removed in Agent 0.8.58" in readme
    assert "LAN-only OpenSSH" in readme
    assert "Direct SSH root login" in readme
    assert "public-source password login remain disabled" in readme
    assert "ProtectHome=true" not in readme or "retired remote-sessions.conf" in readme
    assert "agent_update.v1 is enabled" in readme
    assert "Online packages cannot replace RigPilotAgentLauncher.sh" in readme
    assert "Portable foreground mode intentionally keeps allow_agent_updates=false" in readme


def test_portable_first_run_does_not_advertise_online_updates():
    launcher = (PORTABLE / "Start-Portable.sh").read_text(encoding="utf-8")
    assert "Spark Shell" not in launcher
    assert "allow_nvidia_driver_updates" not in launcher
    assert "allow_remote_sessions" not in launcher
    assert '[[ "${EUID}" -eq 0 ]]' in launcher
    assert '"advertise_ssh": True' in launcher
    assert '"advertise_sftp": True' in launcher
    assert '"ssh_username": None' in launcher
    assert '"allow_wifi_management"' not in launcher
    assert '"allow_agent_updates": False' in launcher
    assert 'RIGPILOT_ENROLLMENT_TOKEN="${ENROLLMENT_TOKEN}"' in launcher
    assert launcher.index("unset ENROLLMENT_TOKEN") < launcher.rindex(
        'exec "${ROOT}/RigPilotAgent"'
    )


def test_source_example_keeps_retired_features_absent():
    example = (ROOT / "agent" / "config.example.json").read_text(encoding="utf-8")
    assert '"allow_wifi_management"' not in example
    assert '"allow_remote_sessions"' not in example
    assert '"allow_remote_root_sessions"' not in example
    assert '"allow_local_web"' not in example
    assert '"allow_nvidia_driver_updates"' not in example
    assert '"allow_hiveos_runtime_preparation"' not in example
    assert '"advertise_ssh": false' in example
    assert '"advertise_sftp": false' in example


def test_linux_uninstall_is_fixed_root_bounded_and_removes_recovery_units():
    remover = (PORTABLE / "Remove-Systemd.sh").read_text(encoding="utf-8")
    assert "rm -f -- /etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf" in remover
    assert "rm -f -- /etc/systemd/system/rigpilot-agent.service.d/wifi-management.conf" in remover
    assert "rm -f -- /etc/systemd/system/rigpilot-agent.service.d/supervision.conf" in remover
    assert "rm -rf -- /etc/systemd/system/rigpilot-agent.service.d" not in remover
    assert "rmdir -- /etc/systemd/system/rigpilot-agent.service.d" in remover
    assert "rigpilot-agent-update-recovery.timer" in remover
    assert '"$(realpath -e -- /opt/rigpilot-agent)" == "/opt/rigpilot-agent"' in remover
    assert "rm -rf --one-file-system -- /opt/rigpilot-agent" in remover


def test_linux_launcher_checks_root_owned_nonwritable_directory_chain():
    launcher = (PORTABLE / "RigPilotAgentLauncher.sh").read_text(encoding="utf-8")
    assert 'plain_root_directory "${APP_DIR}"' in launcher
    assert 'plain_root_directory "${VERSIONS_DIR}"' in launcher
    assert 'plain_root_directory "${SLOT_DIR}"' in launcher
    assert "stat -c '%u'" in launcher
    assert "8#022" in launcher
    assert 'exec "${RESOLVED_AGENT}" "$@"' in launcher


def test_linux_updater_contract_retains_helpers_and_state_until_reported():
    updater = (PORTABLE / "RigPilotAgentUpdater.py").read_text(encoding="utf-8")
    assert "IMMUTABLE_V1_COMPONENTS = frozenset" in updater
    assert 'set(actual) != {MANIFEST_NAME, "RigPilotAgent"}' in updater
    assert 'set(declared) != {"RigPilotAgent"}' in updater
    assert 'relative_executable": f"versions/{args.version}/RigPilotAgent"' in updater
    assert updater.index('_atomic_json(program / UPDATER_STATE_NAME, updater_state)') < updater.index(
        '_atomic_json(\n            ready,'
    )
    finish = updater.split("def _finish_or_rollback", 1)[1].split(
        "def _activation_armed", 1
    )[0]
    assert finish.index("_reported_valid") < finish.index("_remove_control_files")
    assert 'status == "succeeded"' in updater
    assert '_write_result(data, pending, "rolled_back", error)' in updater
    assert "online update was interrupted before activation" in updater
    assert 'path = job / "activation-arm.json"' in updater
    assert '"armed": True' in updater
    assert 'path = job / "activation-abort.json"' in updater
    assert '"aborted": True' in updater
    assert "MAX_RECOVERY_ATTEMPTS = 5" in updater
    assert "UPDATE_LOCK_WAIT_SECONDS = 20" in updater
    assert "another install, update, or" in updater
    assert "time.monotonic() >= deadline" in updater
    assert '"--property=MainPID"' in updater
    activate = updater.split("def activate", 1)[1].split("def recover", 1)[0]
    assert activate.index("_wait_for_activation(job, pending") < activate.index(
        'incoming = program / "versions"'
    )


@pytest.mark.skipif(os.name == "nt", reason="Linux updater imports fcntl")
def test_linux_updater_confirmation_waits_for_reported_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_linux_updater()
    # Exercise the real atomic writes without requiring the test user to chown
    # temporary files to root, as the production updater does.
    ownership_changes: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        module.os,
        "chown",
        lambda path, uid, gid: ownership_changes.append((Path(path), uid, gid)),
    )
    program = tmp_path / "program"
    data = tmp_path / "data"
    program.mkdir()
    real_lstat = Path.lstat

    def control_lstat(path):
        info = real_lstat(path)
        if path in (program / "pending-update.json", program / "updater-state.json"):
            fields = list(info)
            fields[stat.ST_UID] = fields[stat.ST_GID] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", control_lstat)
    activation = "a" * 32
    command = "12345678-1234-4234-8234-123456789abc"
    agent = "87654321-4321-4321-8321-cba987654321"
    pending = {
        "schema_version": 1,
        "version": "9.8.7",
        "relative_executable": "versions/9.8.7/RigPilotAgent",
        "activation_id": activation,
        "command_id": command,
        "agent_id": agent,
    }
    state = {
        "schema_version": 1,
        "pending": pending,
        "previous_version": "9.8.6",
        "deadline": 0,
        "phase": "activated",
        "slot_created": True,
    }
    (program / "pending-update.json").write_text(json.dumps(pending), encoding="utf-8")
    (program / "updater-state.json").write_text(json.dumps(state), encoding="utf-8")
    confirmation = data / "agent-updates" / "confirmations" / f"{activation}.json"
    confirmation.parent.mkdir(parents=True)
    confirmation.write_text(
        json.dumps({**pending, "health_heartbeats": 2}), encoding="utf-8"
    )
    (data / "agent-updates" / "staged" / activation).mkdir(parents=True)

    assert module._finish_or_rollback(program, data, pending, state) == 0
    assert (program / "pending-update.json").exists()
    assert (program / "updater-state.json").exists()

    reported = data / "agent-updates" / "reported" / f"{activation}.json"
    reported.parent.mkdir(parents=True)
    reported.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "activation_id": activation,
                "command_id": command,
                "version": "9.8.7",
                "agent_id": agent,
                "reported": True,
            }
        ),
        encoding="utf-8",
    )
    assert module._finish_or_rollback(program, data, pending, state) == 0
    assert not (program / "pending-update.json").exists()
    assert not (program / "updater-state.json").exists()
    assert ownership_changes
    assert all(
        path.is_relative_to(tmp_path) and (uid, gid) == (0, 0)
        for path, uid, gid in ownership_changes
    )


@pytest.mark.skipif(os.name == "nt", reason="Linux updater imports fcntl")
def test_linux_updater_rollback_quarantines_slot_even_before_slot_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_linux_updater()
    program = tmp_path / "program"
    data = tmp_path / "data"
    previous = program / "versions" / "1.0.0"
    target = program / "versions" / "1.0.1"
    previous.mkdir(parents=True)
    target.mkdir()
    (previous / "RigPilotAgent").write_bytes(b"old")
    (target / "RigPilotAgent").write_bytes(b"new")
    activation = "b" * 32
    pending = {
        "schema_version": 1,
        "version": "1.0.1",
        "relative_executable": "versions/1.0.1/RigPilotAgent",
        "activation_id": activation,
        "command_id": "12345678-1234-4234-8234-123456789abc",
        "agent_id": "87654321-4321-4321-8321-cba987654321",
    }
    state = {
        "schema_version": 1,
        "pending": pending,
        "previous_version": "1.0.0",
        "deadline": 0,
        "phase": "slot_installing",
        # Simulate power loss after rename but before slot_ready persistence.
        "slot_created": False,
    }
    (program / "pending-update.json").write_text(json.dumps(pending), encoding="utf-8")
    (program / "updater-state.json").write_text(json.dumps(state), encoding="utf-8")
    (data / "agent-updates" / "staged" / activation).mkdir(parents=True)

    monkeypatch.setattr(module, "_plain", lambda path, **_kwargs: path.lstat())
    monkeypatch.setattr(
        module,
        "_write_active",
        lambda root, version: (root / "active-version").write_text(
            f"{version}\n", encoding="ascii"
        ),
    )
    monkeypatch.setattr(
        module,
        "_run_systemctl",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    monkeypatch.setattr(
        module,
        "_atomic_json",
        lambda path, value: path.write_text(json.dumps(value), encoding="utf-8"),
    )

    module._rollback(program, data, pending, state, "simulated crash")
    quarantine = program / "versions" / f".rolled-back-1.0.1-{activation}"
    assert quarantine.is_dir()
    assert not target.exists()
    assert (program / "active-version").read_text(encoding="ascii") == "1.0.0\n"
    assert not (program / "pending-update.json").exists()
    assert not (program / "updater-state.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux updater imports fcntl")
def test_linux_updater_keeps_recovery_state_when_rollback_receipt_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_linux_updater()
    program = tmp_path / "program"
    data = tmp_path / "data"
    previous = program / "versions" / "2.0.0"
    previous.mkdir(parents=True)
    (previous / "RigPilotAgent").write_bytes(b"old")
    activation = "c" * 32
    pending = {
        "schema_version": 1,
        "version": "2.0.1",
        "relative_executable": "versions/2.0.1/RigPilotAgent",
        "activation_id": activation,
        "command_id": "12345678-1234-4234-8234-123456789abc",
        "agent_id": "87654321-4321-4321-8321-cba987654321",
    }
    state = {
        "schema_version": 1,
        "pending": pending,
        "previous_version": "2.0.0",
        "deadline": 0,
        "phase": "prepared",
        "slot_created": False,
    }
    (program / "pending-update.json").write_text(json.dumps(pending), encoding="utf-8")
    (program / "updater-state.json").write_text(json.dumps(state), encoding="utf-8")
    job = data / "agent-updates" / "staged" / activation
    (job / "bundle").mkdir(parents=True)
    (job / "bundle" / "RigPilotAgent").write_bytes(b"staged")
    (job / "release.tar.gz").write_bytes(b"package")
    monkeypatch.setattr(module, "_plain", lambda path, **_kwargs: path.lstat())
    monkeypatch.setattr(module, "_write_active", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_run_systemctl",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    monkeypatch.setattr(
        module,
        "_write_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        module._rollback(program, data, pending, state, "simulated failure")
    assert (program / "pending-update.json").exists()
    assert (program / "updater-state.json").exists()
    # P1 (user-approved 2026-11-03): cleanup is mandatory and precedes the
    # receipt, so a full filesystem can never wedge the staged payload even
    # though the receipt failure stays retryable by the recovery timer.
    assert not (job / "release.tar.gz").exists()
    assert not (job / "bundle").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux updater imports fcntl")
def test_linux_updater_requires_exact_arm_even_after_old_pid_exits(
    tmp_path: Path,
):
    module = _load_linux_updater()
    job = tmp_path / "job"
    job.mkdir()
    pending = {
        "schema_version": 1,
        "version": "3.0.1",
        "relative_executable": "versions/3.0.1/RigPilotAgent",
        "activation_id": "d" * 32,
        "command_id": "12345678-1234-4234-8234-123456789abc",
        "agent_id": "87654321-4321-4321-8321-cba987654321",
    }
    with pytest.raises(module.UpdateFailure, match="not armed"):
        module._wait_for_activation(job, pending, 2_147_483_647, None, timeout=0.01)
    (job / "activation-abort.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "3.0.1",
                "activation_id": "d" * 32,
                "command_id": "12345678-1234-4234-8234-123456789abc",
                "agent_id": "87654321-4321-4321-8321-cba987654321",
                "aborted": True,
                "error": "controller rejected activation",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(module.UpdateFailure, match="controller rejected"):
        module._wait_for_activation(job, pending, 2_147_483_647, None, timeout=0.1)
    (job / "activation-abort.json").unlink()
    (job / "activation-arm.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "3.0.1",
                "activation_id": "d" * 32,
                "command_id": "12345678-1234-4234-8234-123456789abc",
                "agent_id": "87654321-4321-4321-8321-cba987654321",
                "armed": True,
            }
        ),
        encoding="utf-8",
    )
    module._wait_for_activation(job, pending, 2_147_483_647, None, timeout=0.1)


@pytest.mark.skipif(os.name == "nt", reason="Linux updater imports fcntl")
def test_linux_updater_wall_clock_rollback_is_attempt_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_linux_updater()
    pending = {
        "schema_version": 1,
        "version": "4.0.1",
        "relative_executable": "versions/4.0.1/RigPilotAgent",
        "activation_id": "e" * 32,
        "command_id": "12345678-1234-4234-8234-123456789abc",
        "agent_id": "87654321-4321-4321-8321-cba987654321",
    }
    state = {
        "schema_version": 1,
        "pending": pending,
        "previous_version": "4.0.0",
        # Simulate a wall clock moved far backwards relative to this deadline.
        "deadline": 9_999_999_999,
        "phase": "activated",
        "slot_created": True,
        "recovery_attempts": 4,
    }
    calls: list[str] = []
    monkeypatch.setattr(module, "_confirmation_valid", lambda *_args: False)
    monkeypatch.setattr(module, "_write_state", lambda *_args: None)
    monkeypatch.setattr(
        module,
        "_rollback",
        lambda *_args: calls.append("rollback"),
    )
    assert module._finish_or_rollback(tmp_path, tmp_path, pending, state) == 1
    assert calls == ["rollback"]


@pytest.mark.skipif(os.name == "nt", reason="Linux updater imports fcntl")
@pytest.mark.parametrize("failure_point", ["_write_result", "_cleanup_staged_payload"])
def test_linux_updater_confirmed_activation_keeps_target_on_finalization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
):
    module = _load_linux_updater()
    program, data = tmp_path / "program", tmp_path / "data"
    (program / "versions").mkdir(parents=True)
    activation = "f" * 32
    job = data / "agent-updates" / "staged" / activation
    bundle = job / "bundle"
    bundle.mkdir(parents=True)
    binary = bundle / "RigPilotAgent"
    binary.write_bytes(b"synthetic executable; never run")
    args = SimpleNamespace(
        program_root=str(program), data_root=str(data), version="5.0.1",
        activation_id=activation, staged_bundle=str(bundle),
        ready_path=str(job / "updater-ready.json"), agent_pid=2_147_483_647,
        command_id="12345678-1234-4234-8234-123456789abc",
        agent_id="87654321-4321-4321-8321-cba987654321",
    )
    monkeypatch.setattr(module, "_fixed_roots", lambda *_: (program, data))
    monkeypatch.setattr(module, "_plain", lambda path, **_: path.lstat())
    monkeypatch.setattr(module, "verify_bundle", lambda *_: {"RigPilotAgent": binary})
    monkeypatch.setattr(module, "_read_version", lambda *_: "5.0.0")
    monkeypatch.setattr(module.os, "chown", lambda *_: None)
    monkeypatch.setattr(module, "_wait_for_activation", lambda *_: None)
    monkeypatch.setattr(module, "_run_systemctl", lambda *_: None)
    monkeypatch.setattr(module, "_confirmation_valid", lambda *_: True)
    monkeypatch.setattr(module, "_reported_valid", lambda *_: True)
    rollbacks = []
    monkeypatch.setattr(module, "_rollback", lambda *_: rollbacks.append(True))

    def fail(*_):
        raise OSError("simulated finalization I/O failure")

    monkeypatch.setattr(module, failure_point, fail)
    with pytest.raises(module.UpdateFailure, match="confirmed update finalization failed"):
        module.activate(args)
    assert not rollbacks
    assert (program / "active-version").read_text() == "5.0.1\n"
    assert (program / "versions" / "5.0.1" / "RigPilotAgent").read_bytes() == binary.read_bytes()
    assert (program / "pending-update.json").is_file()
    assert (program / "updater-state.json").is_file()
