import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import stat
import tarfile
from types import SimpleNamespace

import pytest

from server import __version__ as CONTROLLER_VERSION
from test_release_gates_review import fake_runtime


ROOT = Path(__file__).resolve().parents[1]


def load_deploy_module(name):
    path = ROOT / "deploy" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def copied_controller_source(tmp_path):
    """Exercise the real source allowlist with a private two-wheel test closure."""
    builder = load_deploy_module("build-controller-release.py")
    source_allowlist = tuple(name for name in builder.ALLOWED_FILES if not name.endswith(".whl"))
    fake_runtime(tmp_path, builder, count=2)
    builder.ALLOWED_FILES += source_allowlist
    shutil.copytree(
        ROOT / "server", tmp_path / "server", dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "requirements.txt"),
    )
    target_path = tmp_path / builder.RUNTIME_ROOT / "runtime-target.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target["controller_version"] = builder.source_version()
    target_path.write_text(json.dumps(target), encoding="utf-8")
    return builder


def test_controller_container_includes_nvidia_cache_verifiers_and_limits() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "ca-certificates dpkg gpgv" in dockerfile
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile
    for name in (
        "RIGPILOT_NVIDIA_CACHE_MAX_BYTES",
        "RIGPILOT_NVIDIA_CACHE_MAX_SET_BYTES",
        "RIGPILOT_NVIDIA_CACHE_MIN_FREE_BYTES",
        "RIGPILOT_NVIDIA_CACHE_KEEP_VERSIONS",
        "RIGPILOT_NVIDIA_CACHE_RETENTION_DAYS",
    ):
        assert name in compose
        assert name in example


def test_caddy_example_caps_request_bodies_above_default_package_limit():
    caddy = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "request_body {" in caddy
    assert "max_size 1100MB" in caddy
    assert "RIGPILOT_MAX_MINER_PACKAGE_BYTES" in caddy
    assert "RIGPILOT_MAX_AGENT_RELEASE_BYTES" in caddy
    assert caddy.index("request_body {") < caddy.index("reverse_proxy 127.0.0.1:8080")


def test_controller_release_is_a_locked_prepared_v1_transaction_with_rollback():
    script = (ROOT / "deploy" / "deploy-controller-release.sh").read_text(
        encoding="utf-8"
    )
    prepare_index = script.index('if [[ "$mode" == "prepare" ]]')
    commit_index = script.index("state_dir=$prepared_state")
    actual_change_index = script.index("live_change_started=1", commit_index)
    rollback_index = script.index("rollback()", commit_index)
    trap_index = script.index("trap 'rollback $?' ERR", rollback_index)

    assert prepare_index < commit_index < rollback_index < trap_index < actual_change_index
    assert 'readonly lock_file="/run/lock/rigpilot-controller-deploy.lock"' in script
    assert "RIGPILOT_DEPLOY_LOCK_FD" in script
    assert "RIGPILOT_DEPLOY_LOCK_OWNER_PID" in script
    assert 'expected_lock_target=$(realpath -e -- "$lock_file")' in script
    assert 'flock -n "$lock_fd"' in script
    assert 'kill -0 "$lock_owner_pid"' in script
    assert "# BEGIN trusted-live-server-validator" in script
    assert "root_stat.st_uid != 0" in script
    assert '"expires_at": now + ttl' in script
    assert "hmac.new(token, canonical_payload(payload), hashlib.sha256)" in script
    assert '"tree_sha256": digest.hexdigest()' in script
    assert 'relative == "lib64" and target == "lib"' in script
    assert "prepared virtual environment link escapes its root" in script
    assert "prepared server tree changed" in script
    assert "prepared virtual environment tree changed" in script
    assert 'current_live_server" == "$initial_live_server' in script
    assert 'mv -T -- "$install_root/server" "$backup"' in script
    assert 'mv -T -- "$backup" "$install_root/server"' in script
    assert 'if [[ -e "$backup" || -L "$backup" ]]; then' in script
    assert 'echo "SERVER_BACKUP=$backup"' in script
    assert 'python3 -m venv --copies "$new_venv"' in script
    assert '"$new_venv/bin/python" -m pip check' in script
    assert '"$install_root/.venv/bin/pip" check' not in script
    assert "trap 'cleanup_preflight $?' EXIT" in script
    assert script.index("trap - ERR HUP INT TERM EXIT", prepare_index) < commit_index
    assert 'manifest_name = "controller-release.json"' in script
    assert "regular_files != allowed_files | {manifest_name}" in script
    assert "trap 'cleanup_preflight 129' HUP" in script
    assert "trap 'rollback 129' HUP" in script
    assert 'install -d -m 755 -- "$venv_root"' in script
    assert 'chmod -R a+rX,u+w,go-w "$new_venv"' in script
    assert "RIGPILOT_PREPARE_RESULT_FILE" in script
    assert "os.fsync(output.fileno())" in script
    assert "prepared_state_action mark-committed" in script
    assert 'state_dir / "committed.json"' in script
    assert "fsync_directory(path.parent)" in script
    assert "prepared_state_action finalize" in script

    prepare_section = script[prepare_index:commit_index]
    assert "systemctl stop" not in prepare_section
    commit_before_change = script[commit_index:actual_change_index]
    assert 'systemctl is-active --quiet "$service"' in commit_before_change
    assert (
        "Controller service must be stopped by the snapshot transaction before commit"
        in commit_before_change
    )
    assert 'systemctl stop "$service"' not in script[commit_index:rollback_index]

    full_import = script.index("from server.main import app")
    assert prepare_index < full_import < commit_index
    assert 'preflight_data="$stage/.preflight-data"' in script
    assert "env -i" in script
    assert 'RIGPILOT_DATA_DIR="$preflight_data"' in script
    assert 'RIGPILOT_DATABASE="$preflight_data/rigpilot.db"' in script
    for required in (
        '"/api/health"',
        '"/api/agents"',
        '"/api/flight-sheets"',
        '"/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply"',
    ):
        assert required in prepare_section
    for forbidden in ('"/api/flight-sheets/v2"', '"/api/miner-adapters/v2"'):
        assert forbidden in prepare_section
    assert "forbidden_paths.intersection(paths)" in prepare_section
    assert "builtin_adapter_directories" not in script
    assert "all_runtime_descriptors" not in script
    assert 'rm -rf --one-file-system -- "$preflight_data"' in script

    host_gate = script.index('platform.python_implementation() != "CPython"')
    assert prepare_index < host_gate < commit_index
    assert 'platform.system() != "Linux"' in script
    assert 'platform.machine() != "x86_64"' in script
    assert 'sys.version_info[:2] != (3, 10)' in script
    assert "controller runtime requires glibc >= 2.35" in script
    assert "if wheels != locked:" in script
    install = script.index('"$new_venv/bin/python" -m pip install')
    install_end = script.index('"$new_venv/bin/python" -m pip check', install)
    install_command = script[install:install_end]
    for argument in (
        "--no-index",
        "--only-binary=:all:",
        "--require-hashes",
        '--find-links "$wheelhouse"',
        '--requirement "$production_lock"',
    ):
        assert argument in install_command
    assert "server/requirements.txt" not in install_command


def test_controller_builder_and_installer_file_allowlists_match():
    assert_controller_allowlists_match(load_deploy_module("build-controller-release.py"), ROOT)


def assert_controller_allowlists_match(builder, root):
    builder_allowed = set(builder.ALLOWED_FILES)
    installer_source = (
        root / "deploy" / "deploy-controller-release.sh"
    ).read_text(encoding="utf-8")

    installer_match = re.search(
        r"allowed_files\s*=\s*(\{.*?\})\s*"
        r"allowed_directories:\s*set\[str\]\s*=\s*set\(\)",
        installer_source,
        re.DOTALL,
    )

    assert installer_match is not None
    installer_allowed = set(ast.literal_eval(installer_match.group(1)))
    assert installer_allowed == builder_allowed
    server_allowed = {name for name in installer_allowed if name.startswith("server/")}
    server_actual = {
        path.relative_to(root).as_posix()
        for path in (root / "server").rglob("*")
        if (path.is_file() or path.is_symlink())
        and "__pycache__" not in path.parts and path.suffix != ".pyc"
        and not (path.name == ".DS_Store" and not path.is_symlink())
    }
    assert server_allowed == server_actual
    assert "server/miner_adapters/templates.py" in server_allowed
    assert "server/static/.keep" in server_allowed
    assert "server/workload.py" in server_allowed
    assert "server/workload_projection.py" not in server_allowed
    assert "server/flight_v2_storage.py" in server_allowed
    for active_v2 in (
        "server/flight_v2.py",
        "server/flight_v2_api.py",
        "server/flight_v2_dispatch.py",
        "server/miner_adapter_v2_api.py",
        "server/static/flight-v2.js",
    ):
        assert active_v2 not in server_allowed
    runtime_allowed = {
        name for name in installer_allowed if name.startswith("deploy/controller-runtime/")
    }
    target = json.loads((root / "deploy/controller-runtime/runtime-target.json").read_text())
    approved_wheels = {name for name in runtime_allowed if name.endswith(".whl")}
    assert len(approved_wheels) == target["dependencies"]["wheel_count"]


def test_allowlists_accept_declared_growth_but_reject_unknown_source(tmp_path, copied_controller_source):
    builder = copied_controller_source
    extra = "server/miner_adapters/declared_extra_contract.py"
    (tmp_path / extra).write_text("SUPPORTED = ()\n", encoding="utf-8")
    builder.ALLOWED_FILES += (extra,)
    version = builder.source_version()
    accepted = builder.validate_sources(version)
    assert extra in accepted

    # Exercise the same builder/installer/source-set assertions with declared growth.
    installer_source = (ROOT / "deploy/deploy-controller-release.sh").read_text()
    match = re.search(r"allowed_files\s*=\s*(\{.*?\})\s*"
                      r"allowed_directories:\s*set\[str\]\s*=\s*set\(\)",
                      installer_source, re.DOTALL)
    assert match is not None
    grown_installer = (
        installer_source[:match.start(1)] + repr(set(builder.ALLOWED_FILES))
        + installer_source[match.end(1):]
    )
    (tmp_path / "deploy/deploy-controller-release.sh").write_text(grown_installer)
    assert_controller_allowlists_match(builder, tmp_path)

    (tmp_path / "server/unapproved.py").write_text("UNAPPROVED = True\n")
    with pytest.raises(RuntimeError, match="allowlist drift"):
        builder.validate_sources(version)
    with pytest.raises(AssertionError):
        assert_controller_allowlists_match(builder, tmp_path)


def test_controller_runtime_closure_is_exact_hashed_and_machine_readable():
    builder = load_deploy_module("build-controller-release.py")
    version = builder.source_version()
    with pytest.raises(RuntimeError, match="runtime target"):
        builder.validate_runtime_closure("0.0.0")

    target = json.loads(
        (ROOT / builder.RUNTIME_ROOT / "runtime-target.json").read_text(
            encoding="utf-8"
        )
    )
    assert target["controller_version"] == version == CONTROLLER_VERSION
    assert target["platform"] == {
        "system": "Linux",
        "machine": "x86_64",
        "glibc_minimum": "2.35",
    }
    assert target["python"] == {
        "implementation": "CPython",
        "major_minor": "3.10",
        "validated_patch": "3.10.12",
        "abi": "cp310",
    }
    lock_hashes = set()
    lock_path = ROOT / target["dependencies"]["lock"]
    for raw in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            match = builder.LOCK_RE.fullmatch(line)
            assert match is not None
            lock_hashes.add(match.group(3))
    declared = {Path(name).name for name in builder.ALLOWED_FILES if name.endswith(".whl")}
    assert len(declared) == len(lock_hashes) == target["dependencies"]["wheel_count"]
    wheelhouse = ROOT / target["dependencies"]["wheelhouse"]
    if wheelhouse.exists():
        builder.validate_runtime_closure(version)
        wheels = sorted(wheelhouse.glob("*.whl"))
        assert {path.name for path in wheels} == declared
        assert {hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels} == lock_hashes
    else:
        with pytest.raises(RuntimeError, match="wheelhouse file set"):
            builder.validate_runtime_closure(version)


def test_controller_builder_rejects_runtime_root_drift(tmp_path, copied_controller_source):
    rogue = tmp_path / "deploy" / "controller-runtime" / "unapproved.whl"
    rogue.write_bytes(b"not approved\n")
    with pytest.raises(RuntimeError, match="controller release allowlist drift"):
        copied_controller_source.validate_sources(CONTROLLER_VERSION)


def test_controller_builder_ignores_only_regular_finder_metadata(tmp_path, copied_controller_source):
    builder = copied_controller_source
    (tmp_path / "server/.DS_Store").write_bytes(b"Finder view metadata")
    (tmp_path / "deploy/controller-runtime/.DS_Store").write_bytes(b"Finder view metadata")
    hashes = builder.validate_sources(CONTROLLER_VERSION)
    assert set(hashes) == set(builder.ALLOWED_FILES)
    assert not any(name.endswith(".DS_Store") for name in hashes)
    (tmp_path / "server/.unapproved.py").write_text("unapproved = True\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="allowlist drift"):
        builder.validate_sources(CONTROLLER_VERSION)


@pytest.mark.parametrize("drift", ["wheel-bytes", "swapped-lock-hashes"])
def test_controller_builder_rejects_dependency_hash_drift(tmp_path, copied_controller_source, drift):
    builder = copied_controller_source
    if drift == "wheel-bytes":
        wheel = next((tmp_path / builder.WHEELHOUSE).glob("*.whl"))
        wheel.write_bytes(wheel.read_bytes() + b"damaged")
    else:
        lock = tmp_path / builder.RUNTIME_ROOT / "requirements-controller-linux-py310.lock"
        contents = lock.read_text(encoding="utf-8")
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", contents)
        assert len(hashes) >= 2 and hashes[0] != hashes[1]
        contents = contents.replace(hashes[0], "SWAPPED_HASH", 1)
        contents = contents.replace(hashes[1], hashes[0], 1)
        contents = contents.replace("SWAPPED_HASH", hashes[1], 1)
        lock.write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeError, match="identities and hashes"):
        builder.validate_runtime_closure(CONTROLLER_VERSION)

def test_controller_release_health_check_is_bounded_and_checks_status():
    script = (ROOT / "deploy" / "deploy-controller-release.sh").read_text(
        encoding="utf-8"
    )

    assert "curl -fsS --max-time 2" in script
    assert "'\"status\":\"ok\"'" in script
    assert '"\\\"version\\\":\\\"$version\\\""' in script
    assert "health_wait_seconds=${RIGPILOT_HEALTH_WAIT_SECONDS:-30}" in script
    assert 'seq 1 "$health_wait_seconds"' in script


def test_controller_builder_syncs_both_private_files_before_publication(tmp_path, monkeypatch):
    builder = load_deploy_module("build-controller-release.py")
    source = tmp_path / "source"
    source.mkdir()
    payload = b"synthetic Controller source\n"
    (source / "fixture.py").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(builder, "PROJECT_ROOT", source)
    monkeypatch.setattr(builder, "ALLOWED_FILES", ("fixture.py",))
    monkeypatch.setattr(builder, "validate_sources", lambda version: {"fixture.py": digest})
    events = []
    original_fsync, original_link = builder.os.fsync, builder.os.link

    def fsync(descriptor):
        events.append(("fsync", os.fstat(descriptor).st_ino))
        original_fsync(descriptor)

    def link(private, public):
        events.append(("link", Path(private).stat().st_ino))
        original_link(private, public)

    monkeypatch.setattr(builder.os, "fsync", fsync)
    monkeypatch.setattr(builder.os, "link", link)
    target, sidecar, archive_hash = builder.build("1.2.3", tmp_path / "output")
    expected_operations = ["fsync", "fsync", "link", "link"]
    if os.name == "posix":
        expected_operations.append("fsync")
    assert [operation for operation, _ in events] == expected_operations
    assert events[0][1] == events[2][1] == target.stat().st_ino
    assert events[1][1] == events[3][1] == sidecar.stat().st_ino
    if os.name == "posix":
        assert events[4][1] == target.parent.stat().st_ino
    assert hashlib.sha256(target.read_bytes()).hexdigest() == archive_hash
    assert sidecar.read_text() == f"{archive_hash}  {target.name}\n"
    with tarfile.open(target, "r:gz") as archive:
        assert archive.extractfile("fixture.py").read() == payload
    assert not list(target.parent.glob("*.part"))


@pytest.mark.parametrize("failed_sync", [1, 2])
def test_controller_builder_sync_failure_never_publishes(tmp_path, monkeypatch, failed_sync):
    builder = load_deploy_module("build-controller-release.py")
    source = tmp_path / "source"
    source.mkdir()
    payload = b"synthetic Controller source\n"
    (source / "fixture.py").write_bytes(payload)
    monkeypatch.setattr(builder, "PROJECT_ROOT", source)
    monkeypatch.setattr(builder, "ALLOWED_FILES", ("fixture.py",))
    monkeypatch.setattr(builder, "validate_sources", lambda version: {
        "fixture.py": hashlib.sha256(payload).hexdigest(),
    })
    calls = 0
    original_fsync = builder.os.fsync

    def fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError("fixture sync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(builder.os, "fsync", fsync)
    output = tmp_path / "output"
    with pytest.raises(OSError, match="fixture sync failure"):
        builder.build("1.2.3", output)
    assert calls == failed_sync
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("failed_cleanup", [False, True])
def test_controller_builder_directory_sync_failure_keeps_published_pair_and_original_error(
    tmp_path, monkeypatch, capsys, failed_cleanup,
):
    if os.name != "posix":
        pytest.skip("exercises POSIX output-directory fsync")
    builder = load_deploy_module("build-controller-release.py")
    source = tmp_path / "source"
    source.mkdir()
    payload = b"synthetic Controller source\n"
    (source / "fixture.py").write_bytes(payload)
    monkeypatch.setattr(builder, "PROJECT_ROOT", source)
    monkeypatch.setattr(builder, "ALLOWED_FILES", ("fixture.py",))
    monkeypatch.setattr(builder, "validate_sources", lambda version: {
        "fixture.py": hashlib.sha256(payload).hexdigest(),
    })
    failure = OSError("fixture directory sync failure")
    original_fsync, original_close, original_unlink = os.fsync, os.close, Path.unlink
    directory_descriptors = set()

    def fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_descriptors.add(descriptor)
            raise failure
        original_fsync(descriptor)

    def close(descriptor):
        original_close(descriptor)
        if failed_cleanup and descriptor in directory_descriptors:
            raise OSError("fixture directory close failure")

    def unlink(path, *args, **kwargs):
        if failed_cleanup and path.name.endswith(".part"):
            raise OSError("fixture private cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(builder.os, "fsync", fsync)
    monkeypatch.setattr(builder.os, "close", close)
    monkeypatch.setattr(Path, "unlink", unlink)
    output = tmp_path / "output"
    with pytest.raises(OSError) as raised:
        builder.build("1.2.3", output)
    assert raised.value is failure
    target = output / "RigPilotController-1.2.3.tar.gz"
    sidecar = target.with_name(target.name + ".sha256")
    assert sidecar.read_text() == f"{hashlib.sha256(target.read_bytes()).hexdigest()}  {target.name}\n"
    with tarfile.open(target, "r:gz") as archive:
        assert archive.extractfile("fixture.py").read() == payload
    warning = capsys.readouterr().err
    assert "published-but-durability-unconfirmed" in warning
    assert ("fixture private cleanup failure" in warning) is failed_cleanup
    assert ("fixture directory close failure" in warning) is failed_cleanup


def test_controller_builder_keeps_non_posix_packaging_without_directory_sync_claim(
    tmp_path, monkeypatch, capsys,
):
    builder = load_deploy_module("build-controller-release.py")
    source = tmp_path / "source"
    source.mkdir()
    payload = b"synthetic Controller source\n"
    (source / "fixture.py").write_bytes(payload)
    monkeypatch.setattr(builder, "PROJECT_ROOT", source)
    monkeypatch.setattr(builder, "ALLOWED_FILES", ("fixture.py",))
    monkeypatch.setattr(builder, "validate_sources", lambda version: {
        "fixture.py": hashlib.sha256(payload).hexdigest(),
    })
    # Model only the builder's platform branch; leave this test host and pathlib unchanged.
    def open_directory(*args, **kwargs):
        pytest.fail("non-POSIX packaging must not try opening a directory for fsync")

    monkeypatch.setattr(builder, "os", SimpleNamespace(
        name="nt", fsync=os.fsync, link=os.link, open=open_directory,
    ))
    target, sidecar, digest = builder.build("1.2.3", tmp_path / "output")
    assert sidecar.read_text() == f"{digest}  {target.name}\n"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    assert "publication durability is unconfirmed" in capsys.readouterr().err


def test_release_orchestrators_are_local_build_only():
    windows = (ROOT / "deploy/build-all-releases.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "deploy/build-all-releases.sh").read_text(encoding="utf-8")

    assert "wsl.exe" not in windows.casefold()
    assert "wslpath" not in windows.casefold()
    assert "ConvertTo-WslPath" not in windows
    assert "powershell.exe" not in linux.casefold()
    assert "wslpath" not in linux.casefold()
    for script in (windows, linux):
        assert "BUILD_ONLY=1" in script
        assert "BUILD_PLATFORMS=" in script
        assert "verify-release-artifacts.py" in script
        assert "deploy-controller-release.sh" not in script
        assert "systemctl" not in script


def test_retired_automatic_miner_recovery_is_absent_from_both_release_builds():
    for name in ("miner_self_heal", "nvidia_driver_reload"):
        assert not (ROOT / "agent" / (name + ".py")).exists()
        for builder in ("deploy/linux/build-ubuntu-portable.sh", "deploy/windows/build-windows-portable.ps1"):
            assert "--hidden-import agent." + name not in (ROOT / builder).read_text(encoding="utf-8")


@pytest.fixture
def local_release_project(tmp_path):
    project = tmp_path / "source with spaces & symbols"
    deploy = project / "deploy"
    (deploy / "windows").mkdir(parents=True)
    (deploy / "linux").mkdir()
    (project / "agent").mkdir()
    (project / "server").mkdir()
    (project / "agent/__init__.py").write_text("", encoding="utf-8")
    (project / "agent/version.py").write_text('__version__ = "0.8.3"\n', encoding="utf-8")
    (project / "server/__init__.py").write_text('__version__ = "0.8.0"\n', encoding="utf-8")
    for name in ("build-all-releases.ps1", "build-all-releases.sh"):
        shutil.copy2(ROOT / "deploy" / name, deploy / name)
    recorder = (
        "import json,sys\nfrom pathlib import Path\n"
        "p=Path(__file__).resolve()\n"
        "with (p.parent.parent/'dispatch.jsonl').open('a',encoding='utf-8') as f:\n"
        " f.write(json.dumps({'step':p.stem,'argv':sys.argv[1:]})+'\\n')\n"
    )
    for name in ("build-controller-release.py", "verify-release-artifacts.py"):
        (deploy / name).write_text(recorder, encoding="utf-8")
    (deploy / "linux/build-ubuntu-portable.sh").write_text(
        """#!/usr/bin/env bash
echo '{"step":"linux-builder"}' >> "$(dirname "$0")/../../dispatch.jsonl"
""",
        encoding="utf-8",
    )
    (deploy / "windows/build-windows-portable.ps1").write_text(
        "param($PythonPath,$DotNetPath,$OutputDir,$ReleaseVersion,[switch]$SkipTests)\n"
        "[IO.File]::AppendAllText((Join-Path $PSScriptRoot '../../dispatch.jsonl'), "
        "'{\"step\":\"windows-builder\"}' + [Environment]::NewLine)\n"
        "$global:LASTEXITCODE=0\n",
        encoding="utf-8-sig",
    )
    fake_bin = project / "mock-bin"
    fake_bin.mkdir()
    (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
    (fake_bin / "uname").chmod(0o700)
    (fake_bin / "python3").symlink_to(sys.executable)
    return project


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires Bash for isolated mock dispatch")
@pytest.mark.parametrize("skip_controller", [False, True])
def test_linux_local_orchestrator_needs_no_windows_runtime(local_release_project, skip_controller):
    project = local_release_project
    output = project.parent / "artifacts with spaces & symbols"
    command = ["bash", str(project / "deploy/build-all-releases.sh"), "--output-dir", str(output)]
    if skip_controller:
        command += ["--skip-controller", "--skip-windows"]
    result = subprocess.run(
        command, cwd=project.parent, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": str(project / "mock-bin") + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in (project / "dispatch.jsonl").read_text().splitlines()]
    expected = ["linux-builder"] + ([] if skip_controller else ["build-controller-release"]) + ["verify-release-artifacts"]
    assert [call["step"] for call in calls] == expected
    assert "--skip-windows" in calls[-1]["argv"]
    assert calls[-1]["argv"][-2:] == ["--smoke-native", "linux"]
    assert "BUILD_PLATFORMS=linux" + ("" if skip_controller else ",controller") in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="executes the Windows-local PowerShell entry point")
@pytest.mark.parametrize("skip_controller", [False, True])
def test_windows_local_orchestrator_needs_no_linux_runtime(local_release_project, skip_controller):
    project = local_release_project
    output = project.parent / "artifacts with spaces & symbols"
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(project / "deploy/build-all-releases.ps1"), "-PythonPath", sys.executable,
               "-OutputDir", str(output)]
    if skip_controller:
        command += ["-SkipController", "-SkipLinux"]
    result = subprocess.run(command, cwd=project.parent, capture_output=True, text=True, errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in (project / "dispatch.jsonl").read_text(encoding="utf-8-sig").splitlines()]
    expected = ([] if skip_controller else ["build-controller-release"]) + ["windows-builder", "verify-release-artifacts"]
    assert [call["step"] for call in calls] == expected
    assert "--skip-linux" in calls[-1]["argv"]
    assert calls[-1]["argv"][-2:] == ["--smoke-native", "windows"]
    assert "BUILD_PLATFORMS=" + ("" if skip_controller else "controller,") + "windows" in result.stdout


@pytest.mark.skipif(os.name != "nt" and shutil.which("bash") is None, reason="requires the local shell for isolated mock dispatch")
def test_local_orchestrator_rejects_an_empty_target_set(local_release_project):
    project = local_release_project
    output = project.parent / "must not be created"
    if os.name == "nt":
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(project / "deploy/build-all-releases.ps1"),
                   "-SkipWindows", "-SkipController", "-OutputDir", str(output)]
    else:
        command = ["bash", str(project / "deploy/build-all-releases.sh"),
                   "--skip-linux", "--skip-controller", "--output-dir", str(output)]
    result = subprocess.run(
        command, cwd=project.parent, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": str(project / "mock-bin") + os.pathsep + os.environ["PATH"]},
    )
    assert result.returncode != 0
    assert "No local release target selected" in result.stderr
    assert not output.exists()
    assert not (project / "dispatch.jsonl").exists()


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires Bash for isolated mock dispatch")
@pytest.mark.parametrize("component,selected", [("agent", True), ("server", True), ("agent", False), ("server", False)])
def test_local_orchestrator_validates_only_selected_canonical_versions(local_release_project, component, selected):
    project = local_release_project
    source = project / ("agent/version.py" if component == "agent" else "server/__init__.py")
    source.write_text('__version__ = "00.8.3"\n', encoding="utf-8")
    output = project.parent / "output"
    command = ["bash", str(project / "deploy/build-all-releases.sh"), "--output-dir", str(output)]
    if not selected:
        command.append("--skip-linux" if component == "agent" else "--skip-controller")
    result = subprocess.run(command, cwd=project.parent, capture_output=True, text=True, timeout=30,
                            env={**os.environ, "PATH": str(project / "mock-bin") + os.pathsep + os.environ["PATH"]})
    if selected:
        assert result.returncode != 0
        assert "source version" in result.stderr
        assert not output.exists() and not (project / "dispatch.jsonl").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        calls = [json.loads(line) for line in (project / "dispatch.jsonl").read_text().splitlines()]
        assert calls[-1]["step"] == "verify-release-artifacts"
        assert not any(call["step"] == ("linux-builder" if component == "agent" else "build-controller-release") for call in calls)


@pytest.mark.parametrize("host", ["darwin", "win32"])
def test_linux_smoke_rejects_non_linux_before_reading_an_archive(monkeypatch, host):
    verifier = load_deploy_module("verify-release-artifacts.py")
    monkeypatch.setattr(verifier.sys, "platform", host)
    with pytest.raises(RuntimeError, match="requires a Linux runtime"):
        verifier.smoke_linux(Path("unused.tar.gz"), "0.8.3")


def test_linux_smoke_uses_actual_execution_without_requiring_native_x64_host(monkeypatch):
    verifier = load_deploy_module("verify-release-artifacts.py")
    monkeypatch.setattr(verifier.sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(verifier, "regular_tar_members", lambda _path: (
        {"RigPilotAgent-Ubuntu-x64/RigPilotAgent": b"verified test executable"}, set(), {}
    ))
    calls = []

    def execute(argv, **kwargs):
        calls.append(argv)
        assert Path(argv[0]).read_bytes() == b"verified test executable"
        return subprocess.CompletedProcess(argv, 0, stdout="RigPilot Agent 0.8.3\n", stderr="")

    monkeypatch.setattr(verifier.subprocess, "run", execute)
    verifier.smoke_linux(Path("unused.tar.gz"), "0.8.3")
    assert len(calls) == 1 and calls[0][1:] == ["--version"]
    assert not Path(calls[0][0]).exists()


@pytest.mark.parametrize("fault,winerror,failures,attempts", [
    ("transient", 5, 2, 3),
    ("persistent", 32, 40, 40),
    ("unrelated", None, 1, 1),
])
def test_release_smoke_cleanup_keeps_bounded_error_policy(monkeypatch, fault, winerror, failures, attempts):
    verifier = load_deploy_module("verify-release-artifacts.py")
    calls = []
    sleeps = []
    error = PermissionError(fault) if winerror is not None else OSError("unrelated filesystem failure")
    if winerror is not None:
        error.winerror = winerror

    class Temporary:
        def cleanup(self):
            calls.append(True)
            if len(calls) <= failures:
                raise error

    monkeypatch.setattr(verifier.time, "sleep", sleeps.append)
    if failures < attempts:
        verifier.cleanup_smoke_directory(Temporary())
    else:
        with pytest.raises(type(error), match=fault) as caught:
            verifier.cleanup_smoke_directory(Temporary())
        assert caught.value is error
    assert len(calls) == attempts
    assert sleeps == [0.25] * (attempts - 1)


def test_release_dependency_inputs_are_exact_and_test_tools_are_self_contained():
    requirement_files = (
        ROOT / "server" / "requirements.txt",
        ROOT / "agent" / "requirements.txt",
        ROOT / "deploy" / "linux" / "requirements-build.txt",
        ROOT / "deploy" / "linux" / "requirements-test.txt",
        ROOT / "deploy" / "windows" / "requirements-build.txt",
    )
    for path in requirement_files:
        values = [
            line.split("#", 1)[0].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        values = [line for line in values if line]
        assert values
        assert all("==" in line and not any(token in line for token in (">=", "<=", "~=", "<", ">")) for line in values)
    development = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "paramiko==5.0.0" in development
    linux_builder = (ROOT / "deploy" / "linux" / "build-ubuntu-portable.sh").read_text(
        encoding="utf-8"
    )
    install = linux_builder.index('"${PYTHON}" -m pip install')
    check = linux_builder.index('"${PYTHON}" -m pip check')
    assert install < check and linux_builder.count('"${PYTHON}" -m pip check') == 1


def test_linux_release_shell_scripts_parse_with_bash():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")

    scripts = (
        ROOT / "deploy" / "deploy-controller-release.sh",
        ROOT / "deploy" / "build-all-releases.sh",
        ROOT / "deploy" / "linux" / "build-ubuntu-portable.sh",
        ROOT / "deploy" / "linux" / "portable" / "Install.sh",
        ROOT / "deploy" / "linux" / "portable" / "Start-Portable.sh",
        ROOT / "deploy" / "linux" / "portable" / "Remove-Systemd.sh",
    )
    for script in scripts:
        subprocess.run([bash, "-n", str(script)], check=True)


@pytest.mark.skipif(
    platform.system() != "Linux", reason="requires Linux build-host metadata"
)
def test_ubuntu_build_host_gate_executes_without_building():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")

    os_release = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip().strip('"')
    libc_name, libc_version = platform.libc_ver()
    match = re.match(r"^(\d+)\.(\d+)", libc_version)
    supported = (
        os_release.get("ID") == "ubuntu"
        and platform.machine().lower() in {"x86_64", "amd64"}
        and os_release.get("VERSION_ID") == "22.04"
        and libc_name == "glibc"
        and match is not None
        and tuple(map(int, match.groups())) <= (2, 35)
    )

    completed = subprocess.run(
        [bash, str(ROOT / "deploy" / "linux" / "build-ubuntu-portable.sh"), "--check-build-host"],
        capture_output=True,
        text=True,
    )
    assert (completed.returncode == 0) is supported


def _bash_or_skip():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    return bash


def _extract_readonly_assignment(source: str, name: str) -> str:
    prefix = f"readonly {name}="
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):]
    raise AssertionError(f"no readonly assignment for {name}")


def test_lan_ssh_dropin_marker_constants_stay_aligned() -> None:
    installer = (
        ROOT / "deploy" / "linux" / "portable" / "Install.sh"
    ).read_text(encoding="utf-8")
    remover = (
        ROOT / "deploy" / "linux" / "portable" / "Remove-Systemd.sh"
    ).read_text(encoding="utf-8")
    install_marker = _extract_readonly_assignment(installer, "LAN_SSH_DROPIN_MARKER")
    remove_marker = _extract_readonly_assignment(remover, "LAN_SSH_MARKER")
    assert install_marker == remove_marker, (
        "Install.sh and Remove-Systemd.sh must carry the identical LAN SSH "
        "drop-in first-line marker"
    )
    # Install.sh renders the drop-in through the constant, never a duplicate
    # literal.
    assert "printf '%s\\n' \"${LAN_SSH_DROPIN_MARKER}\"" in installer
    assert installer.count("# Managed by RigPilot: password SSH is LAN-only") == 1
    # Remove-Systemd.sh additionally accepts the earlier Spark wording that
    # agent-side configure_linux_ssh.v1 still writes today; tie the accepted
    # legacy constant to that writer so the two can never drift apart again.
    legacy_marker = _extract_readonly_assignment(remover, "LAN_SSH_LEGACY_MARKER")
    linux_ssh = (ROOT / "agent" / "linux_ssh.py").read_text(encoding="utf-8")
    match = re.search(r'_POLICY = \(\n\s*"([^"\\]+)\\n"', linux_ssh)
    assert match is not None, "agent/linux_ssh.py policy first line not found"
    assert legacy_marker == f'"{match.group(1)}"'


def test_installer_lan_ssh_validation_is_sigpipe_safe() -> None:
    installer = (
        ROOT / "deploy" / "linux" / "portable" / "Install.sh"
    ).read_text(encoding="utf-8")
    # Piping sshd -T into grep lost to a grep early-exit SIGPIPE under
    # pipefail on Ubuntu 26.04 (sshd 141 despite a successful match), so the
    # effective-policy views must be captured before matching.
    assert "| grep -qx" not in installer
    assert "! lan_view=\"$(sshd -T -C " in installer
    assert "grep -qx 'passwordauthentication yes' <<<" in installer
    assert "grep -qx 'passwordauthentication no' <<<" in installer
    assert "grep -qx 'permitrootlogin no' <<<" in installer


def test_ubuntu_readme_template_renders_package_version() -> None:
    template = (
        ROOT / "deploy" / "linux" / "portable" / "README-Ubuntu.txt"
    ).read_text(encoding="utf-8")
    assert template.count("%%VERSION%%") == 1
    assert template.splitlines()[0] == "RigPilot Agent %%VERSION%% for Ubuntu x86_64"
    rendered = template.replace("%%VERSION%%", "9.8.7")
    assert rendered.splitlines()[0] == "RigPilot Agent 9.8.7 for Ubuntu x86_64"
    assert "%%VERSION%%" not in rendered
    # Section headers no longer carry a per-release version tag that drifts
    # with the package version; historical facts stay in prose.
    assert "(Agent 0.8." not in template
    builder = (
        ROOT / "deploy" / "linux" / "build-ubuntu-portable.sh"
    ).read_text(encoding="utf-8")
    assert "s/%%VERSION%%/${VERSION}/g" in builder
    assert "RigPilot Agent ${VERSION} for Ubuntu x86_64" in builder
    assert "README-Ubuntu.txt template must contain exactly one %%VERSION%% token" in builder
    assert "unresolved version template token" in builder


def _extract_bash_function(source: str, name: str) -> str:
    lines = source.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if line.startswith(f"{name}() {{"):
            start = index
            break
    assert start is not None, f"function {name} not found"
    body = [lines[start]]
    for line in lines[start + 1:]:
        body.append(line)
        if line.rstrip("\n") == "}":
            return "".join(body)
    raise AssertionError(f"function {name} has no closing brace")


@pytest.mark.parametrize(
    "service_was_enabled,timer_was_enabled,enable_fails,expect_enable,expect_disable",
    [
        # Enable previously failed and must not fall through to disable.
        (1, 1, 1, True, False),
        # Previously disabled stays disabled.
        (0, 0, 0, False, True),
    ],
)
def test_installer_rollback_preserves_previous_enable_state(
    tmp_path, service_was_enabled, timer_was_enabled, enable_fails,
    expect_enable, expect_disable,
):
    bash = _bash_or_skip()
    installer = (
        ROOT / "deploy" / "linux" / "portable" / "Install.sh"
    ).read_text(encoding="utf-8")
    function = _extract_bash_function(installer, "finish_install")
    offline_warning = _extract_bash_function(installer, "warn_registered_agent_offline")
    calls = tmp_path / "systemctl.calls"
    work = tmp_path / "work"
    work.mkdir()
    harness = "\n".join([
        "set -Eeuo pipefail",
        "restore_file() { :; }",
        "install() { :; }",
        f"systemctl() {{ printf '%s\\n' \"$*\" >>{shlex_quote(str(calls))}; "
        f"if [ \"$1\" = enable ] && [ {int(enable_fails)} -eq 1 ]; then return 1; fi; return 0; }}",
        "CONFIG_STAGE=''",
        "SUPERVISION_STAGE=''",
        "SLOT_CREATED=0",
        "COMMITTED=0",
        "CHANGES_STARTED=1",
        f"SERVICE_WAS_ENABLED={service_was_enabled}",
        f"TIMER_WAS_ENABLED={timer_was_enabled}",
        "SERVICE_WAS_ACTIVE=0",
        "TIMER_WAS_ACTIVE=0",
        f"ROLLBACK_DIR='{work}/rollback'",
        f"APP_DIR='{work}/app'",
        f"ACTIVE_VERSION_FILE='{work}/app/active-version'",
        f"VERSIONS_DIR='{work}/versions'",
        f"SLOT='{work}/versions/pkg'",
        f"CONFIG_FILE='{work}/config.json'",
        f"SERVICE_FILE='{work}/agent.service'",
        f"SERVICE_DROPIN_DIR='{work}/agent.service.d'",
        f"SUPERVISION_DROPIN='{work}/agent.service.d/supervision.conf'",
        f"REMOTE_SESSION_DROPIN='{work}/agent.service.d/remote-sessions.conf'",
        f"LEGACY_WIFI_DROPIN='{work}/agent.service.d/wifi-management.conf'",
        f"RECOVERY_SERVICE='{work}/recovery.service'",
        f"RECOVERY_TIMER='{work}/recovery.timer'",
        offline_warning,
        function,
        "set +e",
        "false",
        "finish_install",
    ])
    completed = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 1, completed.stderr
    recorded = calls.read_text(encoding="utf-8").splitlines()
    assert "stop rigpilot-agent.service" in recorded
    if expect_enable:
        assert "enable rigpilot-agent.service" in recorded
        assert "enable rigpilot-agent-update-recovery.timer" in recorded
    if expect_disable:
        assert "disable rigpilot-agent.service" in recorded
        assert "disable rigpilot-agent-update-recovery.timer" in recorded
    # The regression: when enable failed (or the state was enabled), the
    # opposite action must never run on the same unit.
    if expect_enable:
        assert "disable rigpilot-agent.service" not in recorded
        assert "disable rigpilot-agent-update-recovery.timer" not in recorded
    else:
        assert "enable rigpilot-agent.service" not in recorded
        assert "enable rigpilot-agent-update-recovery.timer" not in recorded
    # SERVICE_WAS_ACTIVE=0: rollback must not start the stopped service.
    assert "start rigpilot-agent.service" not in recorded


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


@pytest.mark.parametrize("cleanup", ["early_cleanup", "finish_install"])
@pytest.mark.parametrize("has_identity,service_active", [(True, False), (True, True), (False, False)])
def test_installer_failure_recovery_command_reuses_installed_identity(
    tmp_path, cleanup, has_identity, service_active,
):
    bash = _bash_or_skip()
    installer = (ROOT / "deploy/linux/portable/Install.sh").read_text(encoding="utf-8")
    package = tmp_path / "extracted package"
    package.mkdir()
    installed = tmp_path / "system data"
    installed.mkdir()
    state = installed / "state.json"
    config = installed / "config.json"
    config.write_text(json.dumps({"state_file": str(state)}), encoding="utf-8")
    identity = {"agent_id": "fixture-existing-device", "agent_secret": "fixture-private-secret"}
    if has_identity:
        state.write_text(json.dumps(identity), encoding="utf-8")
    observed = tmp_path / "foreground-identity.json"
    binary = package / "RigPilotAgent"
    binary.write_text(
        "#!" + sys.executable + "\nimport json, pathlib, sys\n"
        "assert sys.argv[1] == '--config'\n"
        "config = json.loads(pathlib.Path(sys.argv[2]).read_text())\n"
        "state = json.loads(pathlib.Path(config['state_file']).read_text())\n"
        f"pathlib.Path({str(observed)!r}).write_text(json.dumps({{'agent_id': state['agent_id']}}))\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    variables = {
        "SOURCE_ROOT": package, "STATE_FILE": state, "CONFIG_FILE": config,
        "APP_DIR": tmp_path / "app", "VERSIONS_DIR": tmp_path / "versions",
        "SLOT": tmp_path / "versions/fixture", "ROLLBACK_DIR": tmp_path / "rollback",
        "ACTIVE_VERSION_FILE": tmp_path / "active-version",
        "SERVICE_FILE": tmp_path / "service", "SERVICE_DROPIN_DIR": tmp_path / "service.d",
        "SUPERVISION_DROPIN": tmp_path / "supervision", "REMOTE_SESSION_DROPIN": tmp_path / "remote",
        "LEGACY_WIFI_DROPIN": tmp_path / "wifi", "RECOVERY_SERVICE": tmp_path / "recovery-service",
        "RECOVERY_TIMER": tmp_path / "recovery-timer",
    }
    harness = "\n".join([
        "set -Eeuo pipefail",
        "restore_file() { :; }",
        "install() { :; }",
        f"systemctl() {{ if [ \"$1\" = is-active ]; then return {0 if service_active else 1}; fi; return 0; }}",
        "CONFIG_STAGE=''", "SLOT_STAGE=''", "SUPERVISION_STAGE=''", "SLOT_CREATED=0",
        "COMMITTED=0", "CHANGES_STARTED=1", "SERVICE_WAS_ENABLED=0", "TIMER_WAS_ENABLED=0",
        "SERVICE_WAS_ACTIVE=0", "TIMER_WAS_ACTIVE=0",
        *(name + "=" + shlex.quote(str(value)) for name, value in variables.items()),
        _extract_bash_function(installer, "warn_registered_agent_offline"),
        _extract_bash_function(installer, cleanup),
        "set +e", "false", cleanup,
    ])
    completed = subprocess.run([bash, "-c", harness], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 1, completed.stderr
    if not has_identity or service_active:
        assert "temporary foreground" not in completed.stderr
        assert not observed.exists()
        return
    command = next(line.strip() for line in completed.stderr.splitlines() if line.startswith("  sudo "))
    argv = shlex.split(command)
    assert argv == ["sudo", str(binary), "--config", str(config)]
    assert "fixture-private-secret" not in completed.stderr
    subprocess.run(argv[1:], check=True, capture_output=True, text=True, timeout=10)
    assert json.loads(observed.read_text()) == {"agent_id": identity["agent_id"]}
    assert json.loads(state.read_text()) == identity
    assert not (package / "data").exists()


@pytest.mark.parametrize("url,valid", [
    ("https://controller.example:0", False), ("https://controller.example:65536", False),
    ("https://controller.example:invalid", False), ("https://controller.example\\path", False),
    ("https://controller.example:443/controller", True), ("https://[::1]:8443", True),
])
def test_installer_validates_controller_url_before_persisting_config(tmp_path, url, valid):
    installer = (ROOT / "deploy/linux/portable/Install.sh").read_text(encoding="utf-8")
    marker = 'python3 - "${CONFIG_STAGE}" "${CONTROLLER_URL}" "${DEVICE_NAME}" <<\'PY\'\n'
    code = installer.split(marker, 1)[1].split("\nPY\n", 1)[0]
    config = tmp_path / "config.json"
    result = subprocess.run(
        [sys.executable, "-c", code, str(config), url, "fixture-device"],
        capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) is valid, result.stderr
    assert "Traceback" not in result.stderr
    if valid:
        assert json.loads(config.read_text())["server_url"] == url
    else:
        assert "credential-free HTTPS URL" in result.stderr
        assert not config.exists()


@pytest.mark.parametrize("fault", [
    None, "config-restore", "daemon-reload", "service-start",
    "missing-marker", "invalid-marker", "stop-active", "stop-unknown", "stop-inactive",
])
def test_installer_preserves_rollback_material_when_restore_fails(tmp_path, fault):
    bash = _bash_or_skip()
    installer = (ROOT / "deploy/linux/portable/Install.sh").read_text(encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    slot = app / "versions/9.8.7"
    slot.mkdir(parents=True)
    (slot / "RigPilotAgent").write_bytes(b"new binary, never executed")
    rollback = app / ".install-rollback.fixture"
    rollback.mkdir()
    config = tmp_path / "config.json"
    config.write_bytes(b"new configuration")
    backups = (
        "launcher", "hiveos-setup", "updater", "active-version", "service",
        "supervision-dropin", "remote-dropin", "legacy-wifi-dropin",
        "recovery-service", "recovery-timer", "config",
    )
    for name in backups:
        (rollback / name).write_bytes(("previous-" + name).encode())
        (rollback / (name + ".present")).write_text("1")
    if fault == "missing-marker":
        (rollback / "config.present").unlink()
    elif fault == "invalid-marker":
        (rollback / "config.present").write_text("not-a-presence-flag")
    calls = tmp_path / "systemctl.calls"
    variables = {
        "APP_DIR": app, "SLOT": slot, "ROLLBACK_DIR": rollback, "CONFIG_FILE": config,
        "ACTIVE_VERSION_FILE": app / "active-version", "SERVICE_FILE": tmp_path / "service",
        "SERVICE_DROPIN_DIR": tmp_path / "service.d", "SUPERVISION_DROPIN": tmp_path / "service.d/supervision",
        "REMOTE_SESSION_DROPIN": tmp_path / "service.d/remote", "LEGACY_WIFI_DROPIN": tmp_path / "service.d/wifi",
        "RECOVERY_SERVICE": tmp_path / "recovery-service", "RECOVERY_TIMER": tmp_path / "recovery-timer",
        "FAULT": fault or "none", "CALLS": calls,
    }
    harness = "\n".join([
        "set -Eeuo pipefail",
        *(name + "=" + shlex.quote(str(value)) for name, value in variables.items()),
        # Simulate install's file copy without requiring root ownership in
        # the ordinary-user Ubuntu test container. The real restore function
        # and trap control flow still execute against actual temporary files.
        'install() { local dest="${@: -1}"; if [ "$1" = -d ]; then mkdir -p -- "$dest"; return; fi; '
        'if [ "$FAULT" = config-restore ] && [ "$dest" = "$CONFIG_FILE" ]; then return 1; fi; '
        'cp -- "${@: -2:1}" "$dest"; }',
        'systemctl() { printf "%s\\n" "$*" >>"$CALLS"; '
        'if [[ "$FAULT" == stop-* ]] && [ "$1 ${2:-}" = "stop rigpilot-agent.service" ]; then return 1; fi; '
        'if [ "$1" = show ]; then '
        'if [ "$FAULT" = stop-unknown ]; then return 1; '
        'elif [ "$FAULT" = stop-inactive ]; then printf "inactive\\n"; else printf "active\\n"; fi; return 0; fi; '
        'if [ "$FAULT" = daemon-reload ] && [ "$1" = daemon-reload ]; then return 1; fi; '
        'if [ "$FAULT" = service-start ] && [ "$1 ${2:-}" = "start rigpilot-agent.service" ]; then return 1; fi; '
        'return 0; }',
        "CONFIG_STAGE=''", "SUPERVISION_STAGE=''", "SLOT_CREATED=1", "COMMITTED=0", "CHANGES_STARTED=1",
        "SERVICE_WAS_ENABLED=1", "TIMER_WAS_ENABLED=1", "SERVICE_WAS_ACTIVE=1", "TIMER_WAS_ACTIVE=0",
        _extract_bash_function(installer, "restore_file"),
        _extract_bash_function(installer, "warn_registered_agent_offline"),
        _extract_bash_function(installer, "finish_install"),
        "set +e", "false", "finish_install",
    ])
    completed = subprocess.run([bash, "-c", harness], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 1, completed.stderr
    recorded = calls.read_text().splitlines()
    if fault in (None, "stop-inactive"):
        assert not rollback.exists()
        assert not slot.exists()
        assert config.read_bytes() == b"previous-config"
        assert "Existing configuration and device identity were preserved." in completed.stderr
    else:
        assert rollback.is_dir() and slot.is_dir()
        assert (rollback / "config").read_bytes() == b"previous-config"
        assert (slot / "RigPilotAgent").read_bytes() == b"new binary, never executed"
        assert "automatic rollback was incomplete" in completed.stderr
        assert "Existing configuration and device identity were preserved." not in completed.stderr
        if fault != "service-start":
            assert "start rigpilot-agent.service" not in recorded
        if fault in ("missing-marker", "invalid-marker", "stop-active", "stop-unknown"):
            assert config.read_bytes() == b"new configuration"
        if fault in ("stop-active", "stop-unknown"):
            assert not (app / "RigPilotAgentLauncher.sh").exists()
            assert "no rollback files were restored" in completed.stderr


@pytest.mark.skipif(
    platform.system() != "Linux",
    reason="reclaim fragment uses GNU rm --one-file-system semantics",
)
def test_installer_reclaims_abandoned_updater_slots(tmp_path):
    bash = _bash_or_skip()
    installer = (
        ROOT / "deploy" / "linux" / "portable" / "Install.sh"
    ).read_text(encoding="utf-8")
    start = installer.index(
        "# A manual installation is an operator-confirmed boundary"
    )
    end_token = "-print0 2>/dev/null)"
    end = installer.index(end_token, start) + len(end_token)
    fragment = installer[start:end]
    versions = tmp_path / "versions"
    versions.mkdir()
    incoming = versions / (".incoming-0.8.60-" + "a" * 32)
    incoming.mkdir()
    (incoming / "RigPilotAgent").write_bytes(b"stale staging payload")
    rolled_back = versions / (".rolled-back-0.8.59-" + "b" * 32)
    rolled_back.mkdir()
    mismatched = versions / ".rolled-back-0.8.59-not-hex"
    mismatched.mkdir()
    install_stage = versions / ".install-0.8.60-4242"
    install_stage.mkdir()
    active_slot = versions / "0.8.59"
    active_slot.mkdir()
    plain_file = versions / (".incoming-0.8.60-" + "c" * 32)
    plain_file.write_bytes(b"not a directory")
    completed = subprocess.run(
        ["bash", "-c", f"set -Eeuo pipefail\nVERSIONS_DIR={shlex_quote(str(versions))}\n{fragment}\n"],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert not incoming.exists()
    assert not rolled_back.exists()
    assert mismatched.exists()
    assert install_stage.exists()
    assert active_slot.exists()
    assert plain_file.exists()


@pytest.mark.skipif(platform.system() != "Linux", reason="updater imports fcntl")
def test_linux_updater_reclaims_abandoned_update_slots(tmp_path):
    updater_path = ROOT / "deploy" / "linux" / "portable" / "RigPilotAgentUpdater.py"
    spec = importlib.util.spec_from_file_location("linux_updater_module", updater_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    versions = tmp_path / "versions"
    versions.mkdir()
    incoming = versions / (".incoming-0.8.60-" + "a" * 32)
    incoming.mkdir()
    (incoming / "RigPilotAgent").write_bytes(b"stale staging payload")
    rolled_back = versions / (".rolled-back-0.8.60-" + "d" * 32)
    rolled_back.mkdir()
    mismatched = versions / ".incoming-0.8.60-short"
    mismatched.mkdir()
    active_slot = versions / "0.8.59"
    active_slot.mkdir()
    plain_file = versions / (".rolled-back-0.8.60-" + "e" * 32)
    plain_file.write_bytes(b"not a directory")
    module._reclaim_abandoned_slots(tmp_path)
    assert not incoming.exists()
    assert not rolled_back.exists()
    assert mismatched.exists()
    assert active_slot.exists()
    assert plain_file.exists()
    # The staging phase reclaims leftovers before the slot-existence checks.
    source = updater_path.read_text(encoding="utf-8")
    assert "_reclaim_abandoned_slots(program)" in source


def test_linux_updater_recovery_check_result_is_not_discarded() -> None:
    updater_path = ROOT / "deploy" / "linux" / "portable" / "RigPilotAgentUpdater.py"
    tree = ast.parse(updater_path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_finish_or_rollback"
    )
    discarded = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_service_runs_target"
    ]
    assert discarded == [], "_service_runs_target results must feed a decision"


def test_windows_updater_reclaims_abandoned_update_slots() -> None:
    updater = (
        ROOT / "deploy" / "windows" / "portable" / "RigPilotAgentUpdater.ps1"
    ).read_text(encoding="utf-8-sig")
    assert "function Remove-AbandonedUpdateSlots {" in updater
    assert updater.count("Remove-AbandonedUpdateSlots -Root") == 2
    assert "data\\agent-updates\\quarantine" in updater
    assert r"^\.incoming-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-[0-9a-f]{32}$" in updater
    assert r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-[0-9a-f]{32}$" in updater
