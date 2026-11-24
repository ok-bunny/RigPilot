from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
import psutil

import agent.managed_miners as managed_miners_module
import agent.miner_packages as miner_packages_module
from agent.config import AgentConfig
from agent.miner_packages import install_miner_package, validate_package_metadata
from agent.tests.test_miner_packages import (
    config as package_config,
    metadata as package_metadata,
    requester_for,
    zip_bytes,
)
from agent.managed_miners import (
    ManagedMinerError,
    ManagedMinerManager,
    ManagedMinerTransitionError,
    TRUSTED_MINER_RUNTIME_HANDLERS,
    build_pearlminer_argv,
    build_srbminer_argv,
    build_xmrig_argv,
    build_xmrig_config,
    discover_srbminer_devices,
    parse_srbminer_devices,
    resolve_srbminer_gpu_indices,
    find_miner_executable,
    validate_managed_extra_config,
)
from agent.process_identity import managed_argv_fingerprint
from agent.wsl_mining import WslLaunchIdentity
from server.miner_adapters import MINER_ADAPTERS


def managed_config(tmp_path: Path) -> AgentConfig:
    packages = tmp_path / "packages"
    runtime = tmp_path / "runtime"
    packages.mkdir()
    return AgentConfig(
        server_url="https://example.test",
        state_file=tmp_path / "state.json",
        flight_sheet_file=tmp_path / "flight.json",
        miner_status_file=runtime / "miner-status.json",
        managed_mining_enabled=True,
        managed_miner_runtime_dir=runtime,
        allow_miner_downloads=True,
        miner_packages_dir=packages,
    )


def sealed_srb_fixture_metadata(package_root: Path, package: dict) -> dict:
    """Attest existing synthetic bytes through the real in-memory installer."""
    marker = package_root / ".rigpilot-package.json"
    if marker.is_file():
        installed = json.loads(marker.read_text(encoding="utf-8"))
        return package | {key: installed[key] for key in (
            "download_url", "archive_type", "sha256", "managed_os_family"
        )}
    if not package_root.is_dir() or not any(
        path.name in {"SRBMiner-MULTI", "SRBMiner-MULTI.exe"}
        for path in package_root.rglob("*")
    ):
        return package
    entries = {
        path.relative_to(package_root).as_posix(): path.read_bytes()
        for path in package_root.rglob("*") if path.is_file()
    }
    body = zip_bytes(entries)
    family = "windows" if any(name.endswith(".exe") for name in entries) else "linux"
    with tempfile.TemporaryDirectory(prefix=".fixture-seal-", dir=package_root.parent) as staging:
        cfg = package_config(Path(staging))
        metadata = validate_package_metadata(package_metadata(
            body, id=package["id"], name=package["name"],
            version=package["version"], managed_os_family=family,
        ), cfg)
        assert metadata is not None
        request, _, _ = requester_for(body)
        installed = Path(install_miner_package(metadata, cfg, requester=request)["path"])
        # The marker originates from an archive whose SHA was actually checked;
        # never fake or mock the production baseline verifier.
        marker.write_bytes((installed / ".rigpilot-package.json").read_bytes())
    return package | metadata


def leg(
    package_root: Path,
    *,
    miner_type: str = "xmrig",
    package_id: str | None = None,
    extra_config: dict | None = None,
    algorithm: str = "randomx",
) -> dict:
    package_version = "1.0"
    package = {
        "id": package_id or f"builtin-{miner_type}-test",
        "name": miner_type,
        "version": package_version,
        "installed_path": str(package_root),
    }
    if miner_type == "srbminer":
        package = sealed_srb_fixture_metadata(package_root, package)
    return {
        "coin": "XMR",
        "wallet_address": "48A-public-wallet",
        "pool_url": "stratum+ssl://pool.example.test:443",
        "miner_profile": "default",
        "miner_type": miner_type,
        "miner_package": package,
        "extra_config": extra_config or {},
        "algorithm": algorithm,
        "pool_password": "x",
        "device_indices": [],
    }


def document(package_root: Path, **leg_overrides) -> dict:
    return {
        "version": 3,
        "flight_sheet_id": "sheet-managed",
        "name": "XMR",
        "worker_name": "rig-01",
        "primary": leg(package_root, **leg_overrides),
        "secondary": None,
    }


def independent_document(xmrig_root: Path, pearl_root: Path) -> dict:
    value = document(xmrig_root)
    value.update(
        {
            "version": 4,
            "secondary_mode": "independent",
            "secondary": {
                **leg(
                    pearl_root,
                    miner_type="pearlminer",
                    package_id="builtin-pearlminer-test",
                    extra_config={
                        "gpu_backend": "nvidia",
                        "cuda_variant": "auto",
                    },
                    algorithm="pearlhash",
                ),
                "coin": "PEARL",
                "pool_url": "pearl://pool.example.test:4444",
                "device_indices": [],
            },
        }
    )
    return value


def test_trusted_runtime_registry_covers_all_builtin_adapters():
    expected = {adapter.runtime_id for adapter in MINER_ADAPTERS.list()}

    assert TRUSTED_MINER_RUNTIME_HANDLERS.runtime_ids == expected
    assert all(
        callable(TRUSTED_MINER_RUNTIME_HANDLERS.get(runtime_id))
        for runtime_id in expected
    )
    assert TRUSTED_MINER_RUNTIME_HANDLERS.get("builtin.untrusted.v1") is None


def test_prepare_single_rejects_unknown_adapter_runtime_before_package_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class UntrustedAdapter:
        runtime_id = "builtin.untrusted.v1"

    class UntrustedAdapterRegistry:
        @staticmethod
        def require(_miner_type: object) -> UntrustedAdapter:
            return UntrustedAdapter()

    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Linux")
    monkeypatch.setattr(
        managed_miners_module,
        "MINER_ADAPTERS",
        UntrustedAdapterRegistry(),
    )

    with pytest.raises(
        ManagedMinerError,
        match="managed miner adapter runtime is not trusted",
    ):
        manager._prepare_single(document(tmp_path / "package-not-inspected"))


def test_independent_role_document_routes_top_level_srb_oc_to_primary_only(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    srb_root = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    srb_root.mkdir()
    xmrig_root.mkdir()
    value = document(
        srb_root,
        miner_type="srbminer",
        algorithm="pearlhash",
    )
    value["primary"]["coin"] = "PRL"
    value.update(
        {
            "version": 4,
            "secondary_mode": "independent",
            "secondary": leg(xmrig_root),
            "gpu_oc": {
                "vendor": "nvidia",
                "settings": [
                    {"model_match": "5080", "power_limit_w": 250}
                ],
            },
        }
    )
    manager = ManagedMinerManager(cfg)

    primary = manager._role_document(value, "primary")
    secondary = manager._role_document(value, "secondary")

    assert primary["gpu_oc"] == value["gpu_oc"]
    assert primary["gpu_oc"] is not value["gpu_oc"]
    assert secondary["gpu_oc"] is None


def test_exact_entry_search_rejects_missing_duplicate_and_links(tmp_path: Path):
    root = tmp_path / "package"
    (root / "nested").mkdir(parents=True)
    expected = root / "nested" / "xmrig"
    expected.write_bytes(b"miner")
    (root / "xmrig-helper").write_bytes(b"decoy")

    found = find_miner_executable(root, "xmrig", system_name="Linux")

    assert found == expected.resolve()
    if os.name != "nt":
        assert os.stat(found).st_mode & 0o100

    duplicate = root / "xmrig"
    duplicate.write_bytes(b"second")
    with pytest.raises(ManagedMinerError, match="multiple xmrig entries"):
        find_miner_executable(root, "xmrig", system_name="Linux")

    duplicate.unlink()
    expected.unlink()
    with pytest.raises(ManagedMinerError, match="no exact xmrig entry"):
        find_miner_executable(root, "xmrig", system_name="Linux")

    if hasattr(os, "symlink"):
        target = root / "real"
        target.write_bytes(b"real")
        link = root / "xmrig"
        try:
            link.symlink_to(target)
        except OSError:
            return
        with pytest.raises(ManagedMinerError, match="contains a link"):
            find_miner_executable(root, "xmrig", system_name="Linux")


def test_pearlminer_selects_cuda_build_and_rejects_mixed_vendor_package(
    tmp_path: Path,
):
    nvidia = tmp_path / "nvidia"
    nvidia.mkdir()
    cuda12 = nvidia / "miner-cuda12"
    cuda13 = nvidia / "miner-cuda13"
    cuda12.write_bytes(b"cuda12")
    cuda13.write_bytes(b"cuda13")

    def cuda14_runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            ["nvidia-smi"], 0, "NVIDIA-SMI 700.00  CUDA Version: 14.1", ""
        )

    assert find_miner_executable(
        nvidia,
        "pearlminer",
        system_name="Linux",
        runner=cuda14_runner,
    ) == cuda13.resolve()
    assert find_miner_executable(
        nvidia,
        "pearlminer",
        system_name="Linux",
        runner=cuda14_runner,
        gpu_backend="nvidia",
        cuda_variant="12",
    ) == cuda12.resolve()

    (nvidia / "miner").write_bytes(b"amd")
    with pytest.raises(ManagedMinerError, match="mixes AMD and NVIDIA"):
        find_miner_executable(
            nvidia,
            "pearlminer",
            system_name="Linux",
            runner=cuda14_runner,
        )


def test_pearlminer_argv_matches_official_structured_cli(tmp_path: Path):
    executable = tmp_path / "miner.exe"
    document_value = document(tmp_path, miner_type="pearlminer")
    document_value["primary"].update(
        {
            "coin": "PEARL",
            "algorithm": "pearlhash",
            "pool_url": "pearl://pool.example.test:4444",
            "extra_config": {},
        }
    )

    assert build_pearlminer_argv(executable, document_value) == [
        str(executable),
        "--proxy",
        "pool.example.test:4444",
        "--address",
        "48A-public-wallet",
        "--worker",
        "rig-01",
        "-gpu",
    ]
    document_value["primary"]["device_indices"] = [0]
    with pytest.raises(ManagedMinerError, match="GPU index selector"):
        build_pearlminer_argv(executable, document_value)


@pytest.mark.parametrize("pool_url,version", [
    ("stratum+ssl://pool.example.test:443", "2.1.1"),
    ("stratum+ssl://pool.example.test:443", "2.1.0"),
    ("stratum+tcp://pool.example.test:3333", "2.0.3"),
])
def test_pearlminer_stratum_pool_preserves_full_url_without_version_gate(
    tmp_path: Path, pool_url: str, version: str
):
    executable = tmp_path / "miner-cuda13"
    document_value = document(tmp_path, miner_type="pearlminer")
    document_value["primary"].update(
        {"coin": "PEARL", "algorithm": "pearlhash", "pool_url": pool_url,
         "extra_config": {}}
    )
    document_value["primary"]["miner_package"]["version"] = version

    assert build_pearlminer_argv(executable, document_value) == [
        str(executable), "--proxy", pool_url,
        "--stratum-dialect", "kryptex_prl",
        "--address", "48A-public-wallet", "--worker", "rig-01", "-gpu",
    ]


@pytest.mark.parametrize("version", ["2.0.2", "2.0.3"])
def test_pearlminer_controlled_beta_argv_preserves_worker_name(
    tmp_path: Path, version: str
):
    executable = tmp_path / "miner-cuda12"
    document_value = document(tmp_path, miner_type="pearlminer")
    document_value["primary"].update(
        {
            "coin": "PEARL",
            "algorithm": "pearlhash",
            "pool_url": "pearl://pool.example.test:4444",
            "extra_config": {},
        }
    )
    document_value["primary"]["miner_package"]["version"] = version

    assert build_pearlminer_argv(executable, document_value) == [
        str(executable),
        "--proxy",
        "pool.example.test:4444",
        "--address",
        "48A-public-wallet",
        "--worker",
        "rig-01",
        "-gpu",
    ]


def test_pearlminer_amd_environment_uses_bundled_lib_directory(tmp_path: Path):
    executable = tmp_path / "miner"
    executable.write_bytes(b"amd")
    library_directory = tmp_path / "lib"
    library_directory.mkdir()

    environment = managed_miners_module._pearl_environment(executable, "Linux")

    assert environment["LD_LIBRARY_PATH"].split(os.pathsep)[0] == str(library_directory)


def test_pearlminer_launch_truncates_only_its_session_log(tmp_path: Path):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    executable = tmp_path / "miner.exe"
    executable.write_bytes(b"miner")
    process = FakeProcess(202, executable.resolve(), 2000.5)
    pearl_log = runtime / "pearlminer.log"
    pearl_log.write_text("old-session-hashrate\n", encoding="utf-8")
    shared_log = runtime / "managed-miner-launcher.log"
    shared_log.write_text("keep-existing-miner-log\n", encoding="utf-8")
    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *_args, **_kwargs: FakePopen(),
        process_factory=lambda _pid: process,
        system_name="Windows",
    )

    manager._launch(
        [str(executable)],
        executable.resolve(),
        runtime,
        "pearlminer",
    )

    assert pearl_log.read_bytes() == b""
    assert shared_log.read_text(encoding="utf-8") == "keep-existing-miner-log\n"


def test_windows_wsl_pearl_plan_uses_linux_package_and_fixed_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-pearlminer-2.0.5-linux-nvidia"
    package.mkdir()
    target = package / "miner-cuda12"
    target.write_bytes(b"ELF")
    flight = document(
        package,
        miner_type="pearlminer",
        package_id="builtin-pearlminer-2.0.5-linux-nvidia",
        algorithm="pearlhash",
        extra_config={"gpu_backend": "nvidia", "cuda_variant": "12"},
    )
    flight["primary"].update(
        coin="PEARL",
        pool_url="pearl://pool.example.test:4444",
        execution_mode="wsl",
    )
    flight["primary"]["miner_package"]["managed_os_family"] = "linux"
    wsl = tmp_path / "System32" / "wsl.exe"
    wsl.parent.mkdir()
    wsl.write_bytes(b"MZ")

    class FakeWslRuntime:
        wsl_executable = wsl.resolve()
        cache_key = "c" * 64
        linux_executable = (
            "/home/user/.rigpilot/miner-cache-v1/" + cache_key + "/miner-cuda12"
        )
        identity = WslLaunchIdentity(7, "S-1-5-21-1000", "Ubuntu-22.04", 1000)

        def supervised_command(self, argv, *, token):
            return [
                str(self.wsl_executable),
                "--distribution",
                self.identity.distribution,
                "--exec",
                "/usr/bin/python3",
                token,
                self.linux_executable,
                *argv[1:],
            ]

    acl_calls = []
    monkeypatch.setattr(
        managed_miners_module,
        "ensure_wsl_package_read_access",
        lambda root, *, packages_root: acl_calls.append(
            (root, packages_root)
        ),
    )
    monkeypatch.setattr(
        managed_miners_module,
        "prepare_wsl_gpu_runtime",
        lambda *_args, **_kwargs: FakeWslRuntime(),
    )
    manager = ManagedMinerManager(
        cfg,
        system_name="Windows",
        port_allocator=lambda: 23456,
        cuda_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["nvidia-smi"], 0, "CUDA Version: 12.8", ""
        ),
    )

    plan = manager._prepare_single(flight)

    assert plan["execution_mode"] == "wsl"
    assert plan["executable"] == target.resolve()
    assert plan["process_executable"] == wsl.resolve()
    assert plan["argv"][:3] == [
        str(wsl.resolve()),
        "--distribution",
        "Ubuntu-22.04",
    ]
    assert plan["wsl_guest_executable"] == FakeWslRuntime.linux_executable
    assert plan["wsl_cache_key"] == FakeWslRuntime.cache_key
    assert plan["wsl_identity"] == FakeWslRuntime.identity
    assert acl_calls == [(package.resolve(), cfg.miner_packages_dir)]


def test_windows_wsl_srb_rejects_selected_non_nvidia_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-srbminer-3.4.6-linux-x64"
    package.mkdir()
    target = package / "SRBMiner-MULTI"
    target.write_bytes(b"ELF")
    flight = document(
        package,
        miner_type="srbminer",
        package_id="builtin-srbminer-3.4.6-linux-x64",
        algorithm="etchash",
    )
    flight["primary"].update(
        coin="ETC",
        execution_mode="wsl",
        device_indices=[4],
    )
    flight["primary"]["miner_package"]["managed_os_family"] = "linux"
    wsl = tmp_path / "System32" / "wsl.exe"
    wsl.parent.mkdir()
    wsl.write_bytes(b"MZ")

    class FakeWslRuntime:
        wsl_executable = wsl.resolve()
        run_linux = staticmethod(completed_device_list(SRB_MIXED_DEVICE_LIST))
        cache_key = "d" * 64
        linux_executable = (
            "/home/user/.rigpilot/miner-cache-v1/"
            + cache_key
            + "/SRBMiner-MULTI"
        )
        identity = WslLaunchIdentity(7, "S-1-5-21-1000", "Ubuntu-22.04", 1000)

        def supervised_command(self, argv, *, token):
            return [str(self.wsl_executable), "--exec", self.linux_executable, *argv[1:]]

    monkeypatch.setattr(
        managed_miners_module,
        "ensure_wsl_package_read_access",
        lambda _root, *, packages_root: None,
    )
    monkeypatch.setattr(
        managed_miners_module,
        "prepare_wsl_gpu_runtime",
        lambda *_args, **_kwargs: FakeWslRuntime(),
    )
    manager = ManagedMinerManager(cfg, system_name="Windows", port_allocator=lambda: 24567)

    with pytest.raises(ManagedMinerError, match="supports NVIDIA GPUs only"):
        manager._prepare_single(flight)


def test_wsl_launch_uses_active_console_user_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    wsl = tmp_path / "wsl.exe"
    wsl.write_bytes(b"MZ")
    observed = FakeProcess(909, wsl.resolve(), 3000.25)
    calls = []

    def active_popen(argv, *, stdout):
        calls.append(list(argv))
        return FakePopen(pid=909)

    monkeypatch.setattr(managed_miners_module, "active_user_popen", active_popen)
    monkeypatch.setattr(
        managed_miners_module,
        "wait_wsl_guest_ready",
        lambda *_args, **_kwargs: None,
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda _pid: observed,
        system_name="Windows",
    )

    pid, created, _restart_safe = manager._launch(
        [str(wsl), "--exec", "/mnt/c/miner"],
        wsl.resolve(),
        runtime,
        "pearlminer",
        execution_mode="wsl",
        wsl_token="a" * 64,
        wsl_guest_executable="/mnt/c/miner",
        wsl_identity=WslLaunchIdentity(
            7, "S-1-5-21-1000", "Ubuntu-22.04", 1000
        ),
    )

    assert (pid, created) == (909, 3000.25)
    assert calls == [[str(wsl), "--exec", "/mnt/c/miner"]]


def test_wsl_v3_missing_wrapper_never_clears_record_or_allows_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    package = cfg.miner_packages_dir / "builtin-pearlminer-legacy-linux-nvidia"
    package.mkdir()
    target = package / "miner-cuda13"
    target.write_bytes(b"ELF")
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"MZ")
    record = {
        "version": 3,
        "pid": 808,
        "executable": str(wrapper.resolve()),
        "create_time": 2900.25,
        "miner_type": "pearlminer",
        "flight_sheet_id": "sheet-wsl-v3",
        "started_at": "2026-08-03T00:00:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": "a" * 64,
        "api_port": 45001,
        "execution_mode": "wsl",
        "target_executable": str(target.resolve()),
    }
    state_path = runtime / "managed-miner-process.json"
    managed_miners_module._write_private_json(state_path, record)
    monkeypatch.setattr(
        managed_miners_module, "wsl_executable_path", lambda: wrapper.resolve()
    )

    def missing(_pid):
        raise psutil.NoSuchProcess(_pid)

    manager = ManagedMinerManager(
        cfg,
        process_factory=missing,
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="legacy WSL miner launcher"):
        manager._stop_previous(runtime, clear_definitely_absent=True)
    assert json.loads(state_path.read_text(encoding="utf-8")) == record
    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    assert legacy["status"] == "ambiguous" and not legacy["clearable"]
    assert manager.cleanup_residue(scan["scan_fingerprint"])["blocked"] is True
    assert json.loads(state_path.read_text(encoding="utf-8")) == record


def test_wsl_v4_stop_controls_verified_guest_when_host_wrapper_disappeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    package = cfg.miner_packages_dir / "builtin-pearlminer-test-linux-nvidia"
    package.mkdir()
    target = package / "miner-cuda13"
    target.write_bytes(b"ELF")
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"MZ")
    identity = WslLaunchIdentity(
        7, "S-1-5-21-1000", "Ubuntu-22.04", 1000
    )
    record = {
        "version": 4,
        "pid": 909,
        "executable": str(wrapper.resolve()),
        "create_time": 3000.25,
        "miner_type": "pearlminer",
        "flight_sheet_id": "sheet-wsl",
        "started_at": "2026-08-03T00:00:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": "a" * 64,
        "api_port": 45001,
        "execution_mode": "wsl",
        "target_executable": str(target.resolve()),
        "wsl_token": "b" * 64,
        "wsl_guest_executable": "/mnt/c/package/miner-cuda13",
        "wsl_identity": {
            "windows_session_id": identity.windows_session_id,
            "windows_user_sid": identity.windows_user_sid,
            "distribution": identity.distribution,
            "linux_uid": identity.linux_uid,
        },
    }
    managed_miners_module._write_private_json(
        runtime / "managed-miner-process.json", record
    )
    monkeypatch.setattr(
        managed_miners_module, "wsl_executable_path", lambda: wrapper.resolve()
    )
    monkeypatch.setattr(
        managed_miners_module, "query_wsl_guest", lambda *_args, **_kwargs: True
    )
    stopped_calls = []

    def stop_guest(*args, **kwargs):
        stopped_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(managed_miners_module, "stop_wsl_guest", stop_guest)

    def missing(_pid):
        raise psutil.NoSuchProcess(_pid)

    manager = ManagedMinerManager(
        cfg,
        process_factory=missing,
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    assert manager._stop_previous(runtime, clear_definitely_absent=True) is True
    assert len(stopped_calls) == 1
    assert stopped_calls[0][0][1:3] == (
        "b" * 64,
        "/mnt/c/package/miner-cuda13",
    )
    assert not (runtime / "managed-miner-process.json").exists()


def test_wsl_v5_stop_uses_cached_guest_after_windows_package_was_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    package = cfg.miner_packages_dir / "builtin-pearlminer-linux-nvidia"
    package.mkdir()
    target = package / "miner-cuda13"
    # The host source was safely cleaned after the ext4 cache was populated.
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"MZ")
    identity = WslLaunchIdentity(
        7, "S-1-5-21-1000", "Ubuntu-22.04", 1000
    )
    key = "e" * 64
    guest_target = (
        f"/home/user/.rigpilot/miner-cache-v1/{key}/miner-cuda13"
    )
    record = {
        "version": 5,
        "pid": 919,
        "executable": str(wrapper.resolve()),
        "create_time": 3100.25,
        "miner_type": "pearlminer",
        "flight_sheet_id": "sheet-wsl-cache",
        "started_at": "2026-08-03T00:00:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": "a" * 64,
        "api_port": 45001,
        "execution_mode": "wsl",
        "target_executable": str(target),
        "wsl_token": "b" * 64,
        "wsl_guest_executable": guest_target,
        "wsl_identity": {
            "windows_session_id": identity.windows_session_id,
            "windows_user_sid": identity.windows_user_sid,
            "distribution": identity.distribution,
            "linux_uid": identity.linux_uid,
        },
        "wsl_cache_key": key,
    }
    managed_miners_module._write_private_json(
        runtime / "managed-miner-process.json", record
    )
    monkeypatch.setattr(
        managed_miners_module, "wsl_executable_path", lambda: wrapper.resolve()
    )
    monkeypatch.setattr(
        managed_miners_module, "query_wsl_guest", lambda *_args, **_kwargs: True
    )
    stopped_calls = []

    def stop_guest(*args, **kwargs):
        stopped_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(managed_miners_module, "stop_wsl_guest", stop_guest)

    def missing(_pid):
        raise psutil.NoSuchProcess(_pid)

    manager = ManagedMinerManager(
        cfg,
        process_factory=missing,
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    assert manager._stop_previous(runtime, clear_definitely_absent=True) is True
    assert len(stopped_calls) == 1
    assert stopped_calls[0][0][1:3] == ("b" * 64, guest_target)
    assert not (runtime / "managed-miner-process.json").exists()


def test_linux_external_miner_environment_restores_pre_bundle_loader_path(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123:/agent-libs")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/cuda/lib64:/usr/lib")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    monkeypatch.setenv("WATCHDOG_USEC", "120000000")
    monkeypatch.setenv("WATCHDOG_PID", "1234")

    environment = managed_miners_module._external_miner_environment("Linux")

    assert environment["LD_LIBRARY_PATH"] == "/usr/local/cuda/lib64:/usr/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in environment
    assert "NOTIFY_SOCKET" not in environment
    assert "WATCHDOG_USEC" not in environment
    assert "WATCHDOG_PID" not in environment
    # Preparing a child must never mutate the running Agent's environment.
    assert os.environ["LD_LIBRARY_PATH"] == "/tmp/_MEI123:/agent-libs"


def test_frozen_linux_agent_removes_bootloader_loader_path_without_original(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI456")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setattr(managed_miners_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        managed_miners_module.sys,
        "_MEIPASS",
        "/tmp/_MEI456",
        raising=False,
    )

    environment = managed_miners_module._external_miner_environment("Linux")

    assert "LD_LIBRARY_PATH" not in environment


def test_source_linux_agent_preserves_operator_loader_path(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/operator-libs")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.delattr(managed_miners_module.sys, "frozen", raising=False)
    monkeypatch.delattr(managed_miners_module.sys, "_MEIPASS", raising=False)

    environment = managed_miners_module._external_miner_environment("Linux")

    assert environment["LD_LIBRARY_PATH"] == "/opt/operator-libs"


def test_linux_srbminer_discovery_uses_pre_bundle_loader_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI999:/agent-libs")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib")
    calls = []

    def pty_runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            "GPU0 [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [16384 MB]",
            "",
        )

    monkeypatch.setattr(
        managed_miners_module,
        "_run_linux_pty_command",
        pty_runner,
    )
    devices = discover_srbminer_devices(
        tmp_path / "SRBMiner-MULTI",
        system_name="Linux",
    )

    assert devices[0]["name"] == "nvidia_geforce_rtx_5080"
    assert calls[0][1]["env"]["LD_LIBRARY_PATH"] == "/usr/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in calls[0][1]["env"]
    assert calls[0][1]["timeout"] == 15
    assert calls[0][1]["max_output_bytes"] == 256 * 1024


def test_windows_srbminer_discovery_keeps_non_pty_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            "GPU0 [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [16384 MB]",
            "",
        )

    def unexpected_pty(*args, **kwargs):
        raise AssertionError("Windows discovery must not open a PTY")

    monkeypatch.setattr(
        managed_miners_module,
        "_run_linux_pty_command",
        unexpected_pty,
    )
    devices = discover_srbminer_devices(
        tmp_path / "SRBMiner-MULTI.exe",
        runner=runner,
        system_name="Windows",
    )

    assert devices[0]["index"] == 0
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 15


def test_linux_srbminer_discovery_keeps_explicit_injected_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            "GPU0 [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [16384 MB]",
            "",
        )

    monkeypatch.setattr(
        managed_miners_module,
        "_run_linux_pty_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an explicit test runner must remain injectable")
        ),
    )

    devices = discover_srbminer_devices(
        tmp_path / "SRBMiner-MULTI",
        runner=runner,
        system_name="Linux",
    )

    assert devices[0]["index"] == 0
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 15


@pytest.mark.skipif(os.name != "posix", reason="Linux PTY behavior is POSIX-only")
def test_linux_srbminer_discovery_captures_real_tty_only_output(tmp_path: Path):
    executable = tmp_path / "SRBMiner-MULTI"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if sys.argv[1:] == ['--list-devices'] and os.isatty(1):\n"
        "    print('GPU0 [CUDA][0] [0000:01:00.0] : "
        "nvidia_geforce_rtx_5080 [16384 MB]')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    devices = discover_srbminer_devices(executable, system_name="Linux")

    assert [(item["index"], item["name"]) for item in devices] == [
        (0, "nvidia_geforce_rtx_5080")
    ]


def _assert_posix_child_reaped(pid_file: Path) -> None:
    pid = int(pid_file.read_text(encoding="ascii"))
    assert not psutil.pid_exists(pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.skipif(os.name != "posix", reason="Linux PTY behavior is POSIX-only")
def test_linux_pty_timeout_terminates_and_reaps_child(tmp_path: Path):
    pid_file = tmp_path / "timeout.pid"
    code = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='ascii'); "
        "time.sleep(60)"
    )

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        managed_miners_module._run_linux_pty_command(
            [sys.executable, "-c", code],
            cwd=str(tmp_path),
            env=os.environ.copy(),
            timeout=0.2,
            max_output_bytes=1024,
        )

    _assert_posix_child_reaped(pid_file)
    assert caught.value.owned_direct_child_closed is True


def test_linux_pty_reap_double_timeout_is_not_child_exit_proof():
    calls = []
    class UnreapedChild:
        def poll(self):
            return None
        def terminate(self):
            calls.append("terminate")
        def kill(self):
            calls.append("kill")
        def wait(self, timeout):
            calls.append(("wait", timeout))
            raise subprocess.TimeoutExpired(["owned-child"], timeout)
    assert managed_miners_module._reap_pty_process(UnreapedChild()) is False
    assert calls == ["terminate", ("wait", 1), "kill", ("wait", 1)]


@pytest.mark.parametrize("exitcode", [0, 7, -9])
def test_linux_pty_reap_owned_integer_wait_confirms_direct_child(exitcode):
    class ExitedChild:
        def poll(self):
            return exitcode
        def wait(self, timeout):
            assert timeout == 0
            return exitcode
    assert managed_miners_module._reap_pty_process(ExitedChild()) is True


@pytest.mark.skipif(os.name != "posix", reason="Linux PTY behavior is POSIX-only")
def test_linux_pty_keeps_original_failure_when_child_cleanup_cannot_be_confirmed(tmp_path, monkeypatch):
    original_failure = OSError("original PTY failure")
    class FailedChild:
        polls = 0
        def poll(self):
            self.polls += 1
            if self.polls == 1:
                raise original_failure
            raise OSError("cleanup cannot inspect the direct child")
    child = FailedChild()
    monkeypatch.setattr(managed_miners_module.subprocess, "Popen", lambda *_a, **_k: child)
    with pytest.raises(OSError) as caught:
        managed_miners_module._run_linux_pty_command(
            ["synthetic-child-never-executed"], cwd=str(tmp_path), env={},
            timeout=1, max_output_bytes=1024,
        )
    assert caught.value is original_failure
    assert caught.value.owned_direct_child_closed is False


@pytest.mark.skipif(os.name != "posix", reason="Linux PTY behavior is POSIX-only")
def test_linux_pty_rejects_oversize_output_and_reaps_child(tmp_path: Path):
    pid_file = tmp_path / "oversize.pid"
    code = (
        "import os, pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='ascii'); "
        "os.write(1, b'x' * 4096)"
    )

    with pytest.raises(ManagedMinerError, match="device list is invalid"):
        managed_miners_module._run_linux_pty_command(
            [sys.executable, "-c", code],
            cwd=str(tmp_path),
            env=os.environ.copy(),
            timeout=2,
            max_output_bytes=128,
        )

    _assert_posix_child_reaped(pid_file)


@pytest.mark.skipif(os.name != "posix", reason="Linux PTY behavior is POSIX-only")
def test_linux_pty_reports_nonzero_exit_and_reaps_child(tmp_path: Path):
    pid_file = tmp_path / "nonzero.pid"
    code = (
        "import os, pathlib, sys; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='ascii'); "
        "sys.exit(7)"
    )

    completed = managed_miners_module._run_linux_pty_command(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        env=os.environ.copy(),
        timeout=2,
        max_output_bytes=1024,
    )

    assert completed.returncode == 7
    assert completed.owned_direct_child_closed is True
    _assert_posix_child_reaped(pid_file)


def test_linux_srbminer_discovery_rejects_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        managed_miners_module,
        "_run_linux_pty_command",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 7, "failed", ""),
    )

    with pytest.raises(ManagedMinerError, match="GPU discovery failed"):
        discover_srbminer_devices(
            tmp_path / "SRBMiner-MULTI",
            system_name="Linux",
        )


def test_linux_srbminer_discovery_rejects_success_without_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        managed_miners_module,
        "_run_linux_pty_command",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            0,
            "SRBMiner started but returned no device rows",
            "",
        ),
    )

    with pytest.raises(ManagedMinerError, match="did not report any GPU devices"):
        discover_srbminer_devices(
            tmp_path / "SRBMiner-MULTI",
            system_name="Linux",
        )


def test_managed_extra_config_is_small_typed_and_miner_specific():
    # Omitted values remain omitted: OptionSpec defaults describe the adapter
    # and UI, but must not change the legacy Agent wire or runtime defaults.
    assert validate_managed_extra_config("xmrig", {}) == {}
    assert validate_managed_extra_config(
        "xmrig",
        {
            "cpu_threads": None,
            "cpu_max_threads_hint": 75,
            "huge_pages": True,
            "pause_on_battery": False,
        },
    )["cpu_threads"] is None
    assert validate_managed_extra_config(
        "srbminer", {"cpu_threads_intensity": 4, "cpu_threads_reduce": 1}
    )["cpu_threads_intensity"] == 4
    assert validate_managed_extra_config(
        "pearlminer", {"gpu_backend": " NVIDIA ", "cuda_variant": "cuda13"}
    ) == {"gpu_backend": "nvidia", "cuda_variant": "13"}
    assert validate_managed_extra_config(
        "pearlminer", {"cuda_variant": 12}
    ) == {"cuda_variant": "12"}
    with pytest.raises(ManagedMinerError, match="auto, cuda12, or cuda13"):
        validate_managed_extra_config("pearlminer", {"cuda_variant": 12.0})
    with pytest.raises(ManagedMinerError, match="unsupported xmrig keys"):
        validate_managed_extra_config("xmrig", {"api_port": 8080})
    with pytest.raises(ManagedMinerError, match="must be from 1 to 1024"):
        validate_managed_extra_config("xmrig", {"cpu_threads": 0})
    with pytest.raises(ManagedMinerError, match="must be an integer"):
        validate_managed_extra_config("xmrig", {"cpu_threads": True})
    with pytest.raises(ManagedMinerError, match="must be a boolean"):
        validate_managed_extra_config("xmrig", {"huge_pages": None})
    with pytest.raises(ManagedMinerError, match="must be 1, 2, or 4"):
        validate_managed_extra_config("srbminer", {"cpu_threads_intensity": 3})
    with pytest.raises(ManagedMinerError, match="unsupported srbminer keys"):
        validate_managed_extra_config("srbminer", {"pause_on_battery": True})


def test_xmrig_config_and_argv_keep_remote_paths_out_of_command_line(tmp_path: Path):
    doc = document(
        tmp_path,
        extra_config={
            "cpu_threads": 6,
            "cpu_max_threads_hint": 80,
            "huge_pages": True,
            "randomx_1gb_pages": False,
            "keepalive": True,
        },
    )
    config_path = tmp_path / "runtime" / "xmrig-config.json"
    log_path = tmp_path / "runtime" / "xmrig.log"

    value = build_xmrig_config(doc, api_port=23123, log_path=log_path)
    argv = build_xmrig_argv(tmp_path / "xmrig.exe", config_path, doc["primary"])

    assert value["pools"][0] == {
        "algo": "rx/0",
        "coin": "monero",
        "url": "pool.example.test:443",
        "user": "48A-public-wallet",
        "pass": "x",
        "rig-id": "rig-01",
        "keepalive": True,
        "tls": True,
        "enabled": True,
    }
    assert value["http"] == {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 23123,
        "access-token": None,
        "restricted": True,
    }
    assert value["cpu"]["max-threads-hint"] == 80
    assert argv == [
        str(tmp_path / "xmrig.exe"),
        "--config",
        str(config_path),
        "--no-color",
        "--threads",
        "6",
    ]
    assert "48A-public-wallet" not in argv
    assert "pool.example.test" not in " ".join(argv)


def test_xmrig_prepare_uses_immutable_content_bound_config_paths(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    (package / "xmrig.exe").write_bytes(b"test")
    manager = ManagedMinerManager(
        cfg,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )
    first_document = document(package)
    first = manager._prepare_single(first_document)
    first_path = Path(first["argv"][2])
    first_contents = first_path.read_text(encoding="utf-8")
    second_document = json.loads(json.dumps(first_document))
    second_document["primary"]["wallet_address"] = "49B-different-wallet"

    second = manager._prepare_single(second_document)
    second_path = Path(second["argv"][2])

    assert first_path != second_path
    assert first_path.name.startswith("xmrig-config-")
    assert second_path.name.startswith("xmrig-config-")
    assert first_path.read_text(encoding="utf-8") == first_contents
    assert "48A-public-wallet" in first_contents
    assert "49B-different-wallet" in second_path.read_text(encoding="utf-8")
    assert managed_argv_fingerprint(
        first["argv"], executable=first["executable"]
    ) != managed_argv_fingerprint(
        second["argv"], executable=second["executable"]
    )


def test_srbminer_builds_only_structured_randomx_arguments(tmp_path: Path):
    doc = document(
        tmp_path,
        miner_type="srbminer",
        extra_config={
            "cpu_threads": 8,
            "cpu_threads_reduce": 1,
            "cpu_threads_intensity": 2,
            "huge_pages": False,
            "randomx_1gb_pages": True,
            "keepalive": True,
            "extended_log": True,
        },
    )

    argv = build_srbminer_argv(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        api_port=23456,
        log_path=tmp_path / "srbminer.log",
    )

    assert argv[:3] == [str(tmp_path / "SRBMiner-MULTI.exe"), "--algorithm", "randomx"]
    assert ["--pool", "pool.example.test:443"] == argv[3:5]
    assert "--disable-gpu" in argv
    assert argv[argv.index("--tls") + 1] == "true"
    assert argv[argv.index("--keepalive") + 1] == "true"
    assert argv[argv.index("--cpu-threads") + 1] == "8"
    assert "--disable-huge-pages" in argv
    assert "--randomx-use-1gb-pages" in argv
    assert argv[argv.index("--api-port") + 1] == "23456"
    assert argv[argv.index("--log-file") + 1] == str(tmp_path / "srbminer.log")


def test_srbminer_emits_combined_per_gpu_oc_in_gpu_id_order(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="etchash")
    doc["primary"]["coin"] = "ETC"
    doc["primary"]["device_indices"] = [0, 2]
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [
            {
                "index": 0,
                "core_clock_offset_mhz": 100,
                "core_clock_lock_mhz": 1450,
                "memory_clock_offset_mhz": 700,
                "memory_clock_lock_mhz": 8100,
                "power_limit_w": 125,
            },
            {
                "index": 2,
                "core_clock_offset_mhz": 80,
                "core_clock_lock_mhz": 1375,
                "memory_clock_offset_mhz": 650,
                "memory_clock_lock_mhz": 7900,
                "power_limit_w": 112.5,
            },
        ],
    }

    argv = build_srbminer_argv(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        api_port=23456,
        log_path=tmp_path / "srbminer.log",
    )

    assert "--gpu-reset-oc" in argv
    assert argv[argv.index("--gpu-id") + 1] == "0,2"
    assert argv[argv.index("--gpu-coffset0") + 1] == "100,80"
    assert argv[argv.index("--gpu-cclock0") + 1] == "1450,1375"
    assert argv[argv.index("--gpu-moffset0") + 1] == "700,650"
    assert argv[argv.index("--gpu-mclock0") + 1] == "8100,7900"
    assert argv[argv.index("--gpu-plimit0") + 1] == "125,112.5"


def test_srbminer_cpu_gpu_dual_targets_actual_gpu_algorithm_slot_zero(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="randomx")
    gpu_leg = leg(tmp_path, miner_type="srbminer", algorithm="etchash")
    gpu_leg["coin"] = "ETC"
    gpu_leg["wallet_address"] = "etc-public-wallet"
    gpu_leg["device_indices"] = [1]
    doc["secondary"] = gpu_leg
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [
            {
                "index": 1,
                "core_clock_offset_mhz": 75,
                "core_clock_lock_mhz": 1400,
            }
        ],
    }

    argv = build_srbminer_argv(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        api_port=23456,
        log_path=tmp_path / "srbminer.log",
    )

    assert "--gpu-coffset1" not in argv
    assert "--gpu-cclock1" not in argv
    assert argv[argv.index("--gpu-coffset0") + 1] == "75"
    assert argv[argv.index("--gpu-cclock0") + 1] == "1400"
    assert argv[argv.index("--gpu-id") + 1] == "1"


SRB_MIXED_DEVICE_LIST = """
OpenCL platform 0: AMD
OpenCL platform 1: NVIDIA
GPU0 [0][0] [10:00.0] : amd_radeon_tm__graphics [gfx1036] [512 MB]
GPU1 [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [blackwell] [CC: 12.0] [16384 MB]
GPU2 [1][0] [0000:02:00.0] : intel_r__arc_tm__a770_graphics [16384 MB]
GPU3 [1][1] [0000:03:00.0] : intel_r__arc_tm__graphics [8192 MB]
GPU4 [0][1] [0000:04:00.0] : amd_radeon_rx_7900_xtx [gfx1100] [24576 MB]
"""

SRB_MULTI_NVIDIA_DEVICE_LIST = """
OpenCL platform 0: AMD
OpenCL platform 1: NVIDIA
GPU0 [0][0] [10:00.0] : amd_radeon_tm__graphics [gfx1036] [512 MB]
GPU1 [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [blackwell] [CC: 12.0] [16384 MB]
GPU2 [CUDA][1] [0000:02:00.0] : nvidia_geforce_rtx_5070 [blackwell] [CC: 12.0] [12288 MB]
GPU3 [CUDA][2] [0000:03:00.0] : nvidia_geforce_rtx_5060_ti [blackwell] [CC: 12.0] [16384 MB]
GPU4 [CUDA][3] [0000:04:00.0] : nvidia_geforce_rtx_5080 [blackwell] [CC: 12.0] [16384 MB]
"""


def completed_device_list(output: str = SRB_MIXED_DEVICE_LIST):
    def runner(argv, **kwargs):
        assert argv[1:] == ["--list-devices"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 15
        result = subprocess.CompletedProcess(argv, 0, output, "")
        # This injected runner creates no children; model positive exit proof.
        result.owned_direct_child_closed = True
        return result

    return runner


def test_srbminer_device_parser_excludes_amd_and_intel_integrated_gpus():
    devices = parse_srbminer_devices(SRB_MIXED_DEVICE_LIST)

    assert [(item["index"], item["vendor"], item["discrete"]) for item in devices] == [
        (0, "amd", False),
        (1, "nvidia", True),
        (2, "intel", True),
        (3, "intel", False),
        (4, "amd", True),
    ]
    assert devices[0]["pci_bus_id"] == "0000:10:00.0"
    assert devices[1]["pci_bus_id"] == "0000:01:00.0"


def test_srbminer_device_parser_recognizes_amd_bc_250_as_discrete():
    devices = parse_srbminer_devices(
        "GPU6 [0][0] [0000:06:00.0] : amd_bc-250 [gfx1013] [16384 MB]"
    )

    assert len(devices) == 1
    assert devices[0]["index"] == 6
    assert devices[0]["vendor"] == "amd"
    assert devices[0]["discrete"] is True


def test_srbminer_pearlhash_auto_selection_excludes_intel_arc(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = []

    resolved = resolve_srbminer_gpu_indices(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        runner=completed_device_list(),
    )

    assert resolved["primary"]["device_indices"] == [1, 4]
    assert doc["primary"]["device_indices"] == []


def test_srbminer_pearlhash_rejects_explicit_intel_arc_id(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = [2]

    with pytest.raises(
        ManagedMinerError,
        match=r"pearlhash does not support the selected GPU vendor:.*intel",
    ):
        resolve_srbminer_gpu_indices(
            doc,
            executable=tmp_path / "SRBMiner-MULTI.exe",
            runner=completed_device_list(),
        )


def test_srbminer_other_algorithms_keep_intel_arc_eligible(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="etchash")
    doc["primary"]["coin"] = "ETC"
    doc["primary"]["device_indices"] = []

    resolved = resolve_srbminer_gpu_indices(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        runner=completed_device_list(),
    )

    assert resolved["primary"]["device_indices"] == [1, 2, 4]


def test_srbminer_explicit_id_and_legacy_oc_keep_real_srb_device_id(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = [1]
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"index": 1, "power_limit_w": 250}],
    }

    resolved = resolve_srbminer_gpu_indices(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        runner=completed_device_list(),
    )

    assert resolved["primary"]["device_indices"] == [1]
    assert resolved["gpu_oc"]["settings"][0]["index"] == 1
    argv = build_srbminer_argv(
        resolved,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        api_port=23456,
        log_path=tmp_path / "srbminer.log",
    )
    assert argv[argv.index("--gpu-id") + 1] == "1"
    assert argv[argv.index("--gpu-plimit0") + 1] == "250"


def test_srbminer_model_oc_matches_mixed_models_and_same_model_cards(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = []
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [
            {"model_match": "5080", "power_limit_w": 300},
            {"model_match": "RTX 5070", "power_limit_w": 250},
            {"model_match": "5060 Ti", "power_limit_w": 200},
            # A reusable profile may contain models absent from this rig.
            {"model_match": "RTX 4090", "power_limit_w": 280},
        ],
    }

    resolved = resolve_srbminer_gpu_indices(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        runner=completed_device_list(SRB_MULTI_NVIDIA_DEVICE_LIST),
    )

    assert resolved["primary"]["device_indices"] == [1, 2, 3, 4]
    assert resolved["gpu_oc"]["settings"] == [
        {"index": 1, "power_limit_w": 300},
        {"index": 2, "power_limit_w": 250},
        {"index": 3, "power_limit_w": 200},
        {"index": 4, "power_limit_w": 300},
    ]
    argv = build_srbminer_argv(
        resolved,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        api_port=23456,
        log_path=tmp_path / "srbminer.log",
    )
    assert argv[argv.index("--gpu-id") + 1] == "1,2,3,4"
    assert argv[argv.index("--gpu-plimit0") + 1] == "300,250,200,300"


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            [{"model_match": "50", "power_limit_w": 250}],
            "is ambiguous",
        ),
        (
            [
                {"model_match": "5080", "power_limit_w": 300},
                {"model_match": "RTX 5080", "power_limit_w": 290},
                {"model_match": "5070", "power_limit_w": 250},
                {"model_match": "5060 Ti", "power_limit_w": 200},
            ],
            "overlap",
        ),
    ],
)
def test_srbminer_model_oc_fails_closed_on_ambiguous_or_overlapping_rules(
    tmp_path: Path,
    settings: list[dict[str, object]],
    message: str,
):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = []
    doc["gpu_oc"] = {"vendor": "nvidia", "settings": settings}

    with pytest.raises(ManagedMinerError, match=message):
        resolve_srbminer_gpu_indices(
            doc,
            executable=tmp_path / "SRBMiner-MULTI.exe",
            runner=completed_device_list(SRB_MULTI_NVIDIA_DEVICE_LIST),
        )


def test_srbminer_unmatched_models_run_with_default_oc_placeholders(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = []
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"model_match": "5080", "power_limit_w": 300}],
    }

    resolved = resolve_srbminer_gpu_indices(
        doc,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        runner=completed_device_list(SRB_MULTI_NVIDIA_DEVICE_LIST),
    )
    argv = build_srbminer_argv(
        resolved,
        executable=tmp_path / "SRBMiner-MULTI.exe",
        api_port=23456,
        log_path=tmp_path / "srbminer.log",
    )

    assert resolved["gpu_oc"]["settings"] == [
        {"index": 1, "power_limit_w": 300},
        {"index": 4, "power_limit_w": 300},
    ]
    assert argv[argv.index("--gpu-id") + 1] == "1,2,3,4"
    assert argv[argv.index("--gpu-plimit0") + 1] == "300,0,0,300"


def test_srbminer_index_errors_list_filtered_discrete_devices(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = [3]

    with pytest.raises(ManagedMinerError, match=r"GPU1=nvidia_geforce_rtx_5080"):
        resolve_srbminer_gpu_indices(
            doc,
            executable=tmp_path / "SRBMiner-MULTI.exe",
            runner=completed_device_list(),
        )


def test_srbminer_rejects_nvidia_oc_on_selected_amd_discrete_gpu(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = [4]
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"index": 4, "power_limit_w": 200}],
    }

    with pytest.raises(ManagedMinerError, match="supports only NVIDIA"):
        resolve_srbminer_gpu_indices(
            doc,
            executable=tmp_path / "SRBMiner-MULTI.exe",
            runner=completed_device_list(),
        )


class FakeProcess:
    def __init__(self, pid: int, executable: Path, create_time: float, argv=None):
        self.argv = list(argv) if argv is not None else [str(executable)]
        self.pid = pid
        self.executable = executable
        self.created = create_time
        self.running = True
        self.terminated = False
        self.killed = False

    def name(self):
        return self.executable.name

    def cmdline(self):
        return list(self.argv)

    def popen(self, argv, **_kwargs):
        self.argv = list(argv)
        return FakePopen(self.pid)

    def is_running(self):
        return self.running

    def status(self):
        return "running"

    def exe(self):
        return str(self.executable)

    def create_time(self):
        return self.created

    def terminate(self):
        self.terminated = True
        self.running = False

    def wait(self, timeout):
        return 0

    def kill(self):
        self.killed = True
        self.running = False


class ZombieWatchdogProcess(FakeProcess):
    def status(self):
        return psutil.STATUS_ZOMBIE


class FakePopen:
    def __init__(self, pid: int = 202):
        self.pid = pid
        self.terminated = False

    def terminate(self):
        self.terminated = True


def write_process_record(
    path: Path,
    executable: Path,
    *,
    pid: int,
    created: float,
    miner_type: str = "xmrig",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "pid": pid,
                "executable": str(executable.resolve()),
                "create_time": created,
                "miner_type": miner_type,
                "flight_sheet_id": "old-sheet",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def upgrade_process_record_to_v2(path: Path, argv: list[str], api_port: int = 24567):
    value = json.loads(path.read_text(encoding="utf-8"))
    value.update(
        {
            "version": 2,
            "survives_agent_restart": True,
            "argv_sha256": managed_argv_fingerprint(
                argv, executable=value["executable"]
            ),
            "api_port": api_port,
        }
    )
    path.write_text(json.dumps(value), encoding="utf-8")


def test_manager_runs_controller_classified_pearl_test_package_with_cuda_alias(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = (
        cfg.miner_packages_dir / "test-pearlminer-2.0.5-linux-x64-nvidia-2.0.5"
    )
    package.mkdir()
    executable = package / "miner-cuda13"
    executable.write_bytes(b"test")
    flight = document(
        package,
        miner_type="pearlminer",
        package_id="test-pearlminer-2.0.5-linux-x64-nvidia",
        algorithm="pearlhash",
        extra_config={"gpu_backend": "nvidia", "cuda_variant": "cuda13"},
    )
    flight["primary"].update(
        {
            "coin": "PEARL",
            "pool_url": "pearl://pool.example.test:4444",
            "miner_package": {
                **flight["primary"]["miner_package"],
                "version": "2.0.5",
            },
        }
    )
    processes: dict[int, FakeProcess] = {}
    launch_argv: list[str] = []

    def popen(argv, **_kwargs):
        launch_argv.extend(argv)
        processes[505] = FakeProcess(505, Path(argv[0]).resolve(), 2505.0)
        return FakePopen(505)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Linux",
    )

    result = manager.apply(flight)

    assert result["pid"] == 505
    assert launch_argv[0] == str(executable.resolve())


def test_manager_replaces_only_validated_process_and_uses_shell_false(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    old = FakeProcess(101, executable, 1000.25)
    new = FakeProcess(202, executable, 2000.5)
    processes = {101: old, 202: new}
    popen_calls = []

    def popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return FakePopen()

    write_process_record(
        cfg.managed_miner_runtime_dir / "managed-miner-process.json",
        executable,
        pid=101,
        created=1000.25,
    )
    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    result = manager.apply(document(package, extra_config={"cpu_threads": 4}))

    assert result["pid"] == 202
    assert result["replaced_previous"] is True
    assert old.terminated is True
    assert popen_calls[0][1]["shell"] is False
    assert popen_calls[0][1]["cwd"] == str(package)
    assert popen_calls[0][0][0] == str(executable.resolve())
    status_text = cfg.miner_status_file.read_text(encoding="utf-8")
    status = json.loads(status_text)
    assert status["state"] == "running"
    assert status["api_host"] == "127.0.0.1"
    assert "48A-public-wallet" not in status_text
    assert "pool.example.test" not in status_text
    process_state = json.loads(
        (cfg.managed_miner_runtime_dir / "managed-miner-process.json").read_text(
            encoding="utf-8"
        )
    )
    assert process_state["create_time"] == 2000.5
    assert process_state["version"] == 2
    assert process_state["api_port"] == 24567
    assert len(process_state["argv_sha256"]) == 64
    assert process_state["survives_agent_restart"] is False
    assert "argv" not in process_state


def test_production_startup_waits_for_exact_loopback_api_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    process = FakeProcess(211, executable.resolve(), 2211.0)
    manager = ManagedMinerManager(
        cfg,
        popen_factory=process.popen,
        process_factory=lambda _pid: process,
        process_iter_factory=lambda: [process],
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
        enforce_startup_readiness=True,
    )
    monkeypatch.setattr(
        manager,
        "_loopback_api_is_listening",
        lambda _port: True,
    )

    result = manager.apply(document(package))

    assert result["readiness"] == "loopback_api"
    assert process.terminated is False


def test_startup_readiness_timeout_stops_new_exact_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    process = FakeProcess(212, executable.resolve(), 2212.0)
    manager = ManagedMinerManager(
        cfg,
        popen_factory=process.popen,
        process_factory=lambda _pid: process,
        process_iter_factory=lambda: [process],
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
        enforce_startup_readiness=True,
    )
    monkeypatch.setattr(
        managed_miners_module,
        "_MINER_READY_TIMEOUT_SECONDS",
        0.0,
    )

    with pytest.raises(ManagedMinerTransitionError, match="did not become ready"):
        manager.apply(document(package))

    assert process.terminated is True
    assert not (
        cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    ).exists()


def test_interrupted_transition_restarts_previous_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    (package / "xmrig.exe").write_bytes(b"xmrig")
    previous = document(package)
    previous["applied_at"] = "2026-07-30T00:00:00+00:00"
    assert cfg.flight_sheet_file is not None
    cfg.flight_sheet_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.flight_sheet_file.write_text(json.dumps(previous), encoding="utf-8")
    target = document(package)
    target["flight_sheet_id"] = "flight-target"
    manager = ManagedMinerManager(cfg, system_name="Windows")
    manager.begin_flight_transition(
        target,
        previous_was_running=True,
    )
    manager.mark_flight_transition_phase("flight-target", "mutating")
    events: list[str] = []
    monkeypatch.setattr(
        manager,
        "_live_transition_flight_sheet_ids",
        lambda: set(),
    )
    monkeypatch.setattr(
        manager,
        "stop",
        lambda: events.append("stop") or {"stopped": False},
    )
    monkeypatch.setattr(
        manager,
        "restart",
        lambda: events.append("restart") or {"restarted": True},
    )

    result = manager.recover_interrupted_flight_transition()

    assert result == {
        "recovered": True,
        "reason": "previous_restarted",
    }
    assert events == ["stop", "restart"]
    assert not (
        cfg.managed_miner_runtime_dir / "flight-transition.json"
    ).exists()


@pytest.mark.parametrize("sync_failure", [False, True])
def test_committed_target_consumes_transition_journal_without_stopping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_failure: bool,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    (package / "xmrig.exe").write_bytes(b"xmrig")
    previous = document(package)
    previous["applied_at"] = "2026-07-30T00:00:00+00:00"
    target = document(package)
    target["flight_sheet_id"] = "flight-target"
    target["applied_at"] = "2026-07-30T00:01:00+00:00"
    assert cfg.flight_sheet_file is not None
    cfg.flight_sheet_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.flight_sheet_file.write_text(json.dumps(previous), encoding="utf-8")
    manager = ManagedMinerManager(cfg, system_name="Windows")
    journal = cfg.managed_miner_runtime_dir / "flight-transition.json"
    syncs = []
    real_sync = managed_miners_module._fsync_directory

    def sync(directory):
        phase = json.loads(journal.read_text())["phase"] if journal.exists() else None
        syncs.append((directory, phase))
        if directory == cfg.flight_sheet_file.parent:
            assert phase == "target_ready"
            assert json.loads(cfg.flight_sheet_file.read_text())["flight_sheet_id"] == "flight-target"
            if sync_failure:
                raise OSError("synthetic active-directory sync failure")
        real_sync(directory)

    monkeypatch.setattr(managed_miners_module, "_fsync_directory", sync)
    manager.begin_flight_transition(
        target,
        previous_was_running=True,
    )
    manager.mark_flight_transition_phase("flight-target", "target_ready")
    cfg.flight_sheet_file.write_text(json.dumps(target), encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "stop",
        lambda: pytest.fail("committed target must not be stopped"),
    )

    if sync_failure:
        with pytest.raises(ManagedMinerError, match="could not be synchronized"):
            manager.recover_interrupted_flight_transition()
        assert journal.exists()
        sync_failure = False
    result = manager.recover_interrupted_flight_transition()

    assert result == {"recovered": True, "reason": "target_committed"}
    assert not (
        cfg.managed_miner_runtime_dir / "flight-transition.json"
    ).exists()
    assert syncs[:2] == [(cfg.managed_miner_runtime_dir, "prepared"),
                         (cfg.managed_miner_runtime_dir, "target_ready")]
    assert syncs[-2:] == [(cfg.flight_sheet_file.parent, "target_ready"),
                          (cfg.managed_miner_runtime_dir, None)]


def test_independent_dual_launches_and_stops_two_owned_processes(tmp_path: Path):
    cfg = managed_config(tmp_path)
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    pearl_root = cfg.miner_packages_dir / "builtin-pearlminer-test-1.0"
    xmrig_root.mkdir()
    pearl_root.mkdir()
    (xmrig_root / "xmrig.exe").write_bytes(b"xmrig")
    (pearl_root / "miner.exe").write_bytes(b"pearl")

    dual = document(xmrig_root)
    dual.update(
        {
            "version": 4,
            "secondary_mode": "independent",
            "secondary": {
                **leg(
                    pearl_root,
                    miner_type="pearlminer",
                    package_id="builtin-pearlminer-test",
                    extra_config={
                        "gpu_backend": "nvidia",
                        "cuda_variant": "auto",
                    },
                    algorithm="pearlhash",
                ),
                "coin": "PEARL",
                "pool_url": "pearl://pool.example.test:4444",
                "device_indices": [],
            },
        }
    )
    processes: dict[int, FakeProcess] = {}
    launch_argv: list[list[str]] = []
    next_pid = iter((301, 302))
    next_port = iter((24567, 24568))

    def popen(argv, **_kwargs):
        pid = next(next_pid)
        launch_argv.append(list(argv))
        processes[pid] = FakeProcess(
            pid, Path(argv[0]).resolve(), 2000.0 + pid, argv
        )
        return FakePopen(pid)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: next(next_port),
        system_name="Windows",
    )

    result = manager.apply(dual)

    assert result["independent"] is True
    assert set(result["roles"]) == {"primary", "secondary"}
    assert len(launch_argv) == 2
    for role in ("primary", "secondary"):
        role_runtime = cfg.managed_miner_runtime_dir / role
        assert (role_runtime / "managed-miner-process.json").is_file()
        assert (role_runtime / "miner-status.json").is_file()
        assert (role_runtime / "active-flight-sheet.json").is_file()
    current = manager.running()
    assert current is not None
    assert current["complete"] is True
    assert set(current["roles"]) == {"primary", "secondary"}

    stopped = manager.stop()

    assert stopped["stopped"] is True
    assert all(process.terminated for process in processes.values())
    assert all(
        not (
            cfg.managed_miner_runtime_dir
            / role
            / "managed-miner-process.json"
        ).exists()
        for role in ("primary", "secondary")
    )


def test_process_record_write_failure_stops_just_launched_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    process = FakeProcess(401, executable.resolve(), 2401.0)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *_args, **_kwargs: FakePopen(401),
        process_factory=lambda _pid: process,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )
    original_write = managed_miners_module._write_private_json

    def fail_process_record(path: Path, value, **kwargs):
        if path.name == "managed-miner-process.json":
            raise ManagedMinerError("simulated process record write failure")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(
        managed_miners_module,
        "_write_private_json",
        fail_process_record,
    )

    with pytest.raises(ManagedMinerError, match="process record write failure"):
        manager.apply(document(package))

    assert process.terminated is True
    assert not (cfg.managed_miner_runtime_dir / "managed-miner-process.json").exists()


def test_independent_dual_allocates_distinct_api_ports(tmp_path: Path):
    cfg = managed_config(tmp_path)
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    pearl_root = cfg.miner_packages_dir / "builtin-pearlminer-test-1.0"
    xmrig_root.mkdir()
    pearl_root.mkdir()
    (xmrig_root / "xmrig.exe").write_bytes(b"xmrig")
    (pearl_root / "miner.exe").write_bytes(b"pearl")
    processes: dict[int, FakeProcess] = {}
    next_pid = iter((411, 412))
    next_port = iter((24567, 24567, 24568))

    def popen(argv, **_kwargs):
        pid = next(next_pid)
        processes[pid] = FakeProcess(
            pid,
            Path(argv[0]).resolve(),
            2400.0 + pid, argv,
        )
        return FakePopen(pid)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: next(next_port),
        system_name="Windows",
    )

    manager.apply(independent_document(xmrig_root, pearl_root))

    ports = {
        json.loads(
            (
                cfg.managed_miner_runtime_dir
                / role
                / "managed-miner-process.json"
            ).read_text(encoding="utf-8")
        )["api_port"]
        for role in ("primary", "secondary")
    }
    assert ports == {24567, 24568}


def test_independent_metadata_failure_stops_both_new_processes_and_restores_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    pearl_root = cfg.miner_packages_dir / "builtin-pearlminer-test-1.0"
    xmrig_root.mkdir()
    pearl_root.mkdir()
    (xmrig_root / "xmrig.exe").write_bytes(b"xmrig")
    (pearl_root / "miner.exe").write_bytes(b"pearl")
    processes: dict[int, FakeProcess] = {}
    next_pid = iter((421, 422))
    next_port = iter((24567, 24568))

    def popen(argv, **_kwargs):
        pid = next(next_pid)
        processes[pid] = FakeProcess(
            pid,
            Path(argv[0]).resolve(),
            2500.0 + pid, argv,
        )
        return FakePopen(pid)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: next(next_port),
        system_name="Windows",
    )
    original_write = managed_miners_module._write_private_json

    def fail_manifest(path: Path, value, **kwargs):
        if path.name == "independent-miners.json":
            raise ManagedMinerError("simulated manifest write failure")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(managed_miners_module, "_write_private_json", fail_manifest)

    with pytest.raises(ManagedMinerError, match="manifest write failure"):
        manager.apply(independent_document(xmrig_root, pearl_root))

    assert len(processes) == 2
    assert all(process.terminated for process in processes.values())
    for role in ("primary", "secondary"):
        role_runtime = cfg.managed_miner_runtime_dir / role
        assert not (role_runtime / "managed-miner-process.json").exists()
        assert not (role_runtime / "active-flight-sheet.json").exists()
    assert not (cfg.managed_miner_runtime_dir / "independent-miners.json").exists()


def test_independent_stop_attempts_secondary_after_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    secondary = FakeProcess(431, executable.resolve(), 2931.0)
    write_process_record(
        runtime / "secondary" / "managed-miner-process.json",
        executable,
        pid=431,
        created=2931.0,
    )
    original_stop = ManagedMinerManager._stop_previous

    def fail_primary(self, role_runtime, **kwargs):
        if role_runtime.name == "primary":
            raise ManagedMinerError("simulated primary stop failure")
        return original_stop(self, role_runtime, **kwargs)

    monkeypatch.setattr(ManagedMinerManager, "_stop_previous", fail_primary)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: secondary if pid == 431 else (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="primary stop failure"):
        manager._stop_independent_roles(runtime.resolve())

    assert secondary.terminated is True


def test_running_rejects_incomplete_independent_session(tmp_path: Path):
    cfg = managed_config(tmp_path)
    assert cfg.flight_sheet_file is not None
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    root_document = independent_document(package, package)
    root_document["applied_at"] = "2026-07-29T00:00:00+00:00"
    cfg.flight_sheet_file.write_text(json.dumps(root_document), encoding="utf-8")
    primary = FakeProcess(441, executable.resolve(), 2941.0)
    write_process_record(
        runtime / "primary" / "managed-miner-process.json",
        executable,
        pid=441,
        created=2941.0,
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: primary if pid == 441 else (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="session is incomplete"):
        manager.running()


def test_switch_to_single_prepares_before_stopping_existing_roles(tmp_path: Path):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    old_package = cfg.miner_packages_dir / "builtin-xmrig-old-1.0"
    old_package.mkdir()
    executable = old_package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    old_process = FakeProcess(451, executable.resolve(), 2951.0)
    write_process_record(
        runtime / "secondary" / "managed-miner-process.json",
        executable,
        pid=451,
        created=2951.0,
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: old_process if pid == 451 else (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="installed managed miner package"):
        manager.apply(document(tmp_path / "missing-package"))

    assert old_process.terminated is False


def test_switch_preflights_all_existing_roles_before_stopping_any(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")

    primary = FakeProcess(461, executable.resolve(), 2961.0)
    write_process_record(
        runtime / "primary" / "managed-miner-process.json",
        executable,
        pid=461,
        created=2961.0,
    )
    secondary_state = runtime / "secondary" / "managed-miner-process.json"
    expected_secondary_argv = [
        str(executable.resolve()),
        "--config",
        "expected-secondary.json",
    ]
    write_process_record(
        secondary_state,
        executable,
        pid=462,
        created=2962.0,
    )
    upgrade_process_record_to_v2(
        secondary_state,
        expected_secondary_argv,
    )
    unrelated_same_name = FakeProcess(
        463,
        executable.resolve(),
        2963.0,
        [str(executable.resolve()), "--config", "different.json"],
    )
    popen_calls = []

    def process_factory(pid: int):
        if pid == 461:
            return primary
        raise psutil.NoSuchProcess(pid)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        process_factory=process_factory,
        process_iter_factory=lambda: [unrelated_same_name],
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="refusing to start a duplicate"):
        manager.apply(document(package))

    assert primary.terminated is False
    assert unrelated_same_name.terminated is False
    assert popen_calls == []


def test_partial_slot_stop_is_reported_as_mutating_transition(tmp_path: Path):
    events: list[str] = []
    record = {
        "version": 2,
        "pid": 41,
        "executable": str(tmp_path / "miner"),
        "create_time": 100.0,
        "miner_type": "xmrig",
        "flight_sheet_id": "old-sheet",
        "argv_sha256": "a" * 64,
    }

    class Slot:
        def __init__(self, name: str, *, fail: bool = False):
            self.name = name
            self.fail = fail

        def _read_process_state(self, _path):
            return dict(record)

        def _stop_previous(self, _runtime, **_kwargs):
            events.append(self.name)
            if self.fail:
                raise ManagedMinerError(f"{self.name} stop failed")
            return True

    with pytest.raises(
        ManagedMinerTransitionError, match="secondary stop failed"
    ) as captured:
        ManagedMinerManager._stop_preflighted_transition_slots(
            [
                ("primary", Slot("primary"), tmp_path / "primary", record),
                (
                    "secondary",
                    Slot("secondary", fail=True),
                    tmp_path / "secondary",
                    record,
                ),
            ]
        )

    assert events == ["primary", "secondary"]
    assert captured.value.state_changed is True
    assert captured.value.old_stopped is True


def test_single_switch_manifest_failure_preserves_transition_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Windows")
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    target = document(tmp_path / "package")
    prepared = {
        "runtime": runtime,
        "miner_type": "xmrig",
        "api_port": 24567,
        "gpu_mapping": [],
    }
    monkeypatch.setattr(manager, "_prepare_single", lambda _document: prepared)
    monkeypatch.setattr(manager, "_preflight_transition_slots", lambda _runtime: [])
    monkeypatch.setattr(
        manager, "_stop_preflighted_transition_slots", lambda _slots: True
    )
    monkeypatch.setattr(manager, "_write_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        manager,
        "_unlink_state",
        lambda _path: (_ for _ in ()).throw(
            ManagedMinerError("independent manifest cannot be removed")
        ),
    )

    with pytest.raises(
        ManagedMinerTransitionError,
        match="independent manifest cannot be removed",
    ) as captured:
        manager.apply(target)

    assert captured.value.state_changed is True
    assert captured.value.old_stopped is True
    assert captured.value.target_started is False


@pytest.mark.parametrize("algorithm", ["cpupower", "ghostrider", "argon2id_exfer"])
def test_srb_cpu_resources_prepare_without_gpu_discovery(tmp_path: Path, algorithm: str):
    doc = document(tmp_path, miner_type="srbminer", algorithm=algorithm)
    doc["primary"]["coin"] = "TEST"
    doc["primary"]["miner_package"]["version"] = "3.7.1"

    def reject_gpu_probe(*_args, **_kwargs):
        pytest.fail("CPU task must not discover GPUs")

    plan = managed_miners_module._prepare_srbminer_runtime(
        managed_miners_module.MinerRuntimePrepareContext(
            document=doc, primary=doc["primary"], runtime=tmp_path,
            executable=tmp_path / "SRBMiner-MULTI.exe", execution_mode="native",
            api_port=24567, miner_log=tmp_path / "srbminer.log", wsl_runtime=None,
            system_name="Windows", device_runner=reject_gpu_probe,
            privilege_checker=lambda _system: True,
        )
    )
    assert "--disable-gpu" in plan.argv
    assert "--disable-cpu" not in plan.argv
    assert plan.gpu_mapping == []


@pytest.mark.parametrize("resource", [None, "cpu", "gpu"])
def test_srb_mixed_task_resource_controls_oc_roles(tmp_path: Path, resource):
    doc = document(tmp_path, miner_type="srbminer", algorithm="argon2id_exfer")
    doc["primary"]["coin"] = "TEST"
    if resource is not None:
        doc["primary"]["extra_config"]["resource_kind"] = resource
    doc["gpu_oc"] = {"vendor": "nvidia", "settings": [{"index": 0, "core_clock_offset_mhz": 0}]}
    doc["_reset_srb_gpu_oc"] = True
    gpu = resource == "gpu"
    assert managed_miners_module._document_srb_gpu_roles(doc) == (("primary",) if gpu else ())
    role = ManagedMinerManager._role_document(doc, "primary")
    assert (role["gpu_oc"] is not None) is gpu
    assert (role.get("_reset_srb_gpu_oc") is True) is gpu


def test_srb_opaque_pin_option_uses_shared_typed_string_validation():
    pin = "node:opaque/pin=value-not-hex"
    assert validate_managed_extra_config("srbminer", {
        "tls_cert_sha256": pin, "resource_kind": "gpu", "cpu_threads_intensity": None,
    }) == {"tls_cert_sha256": pin, "resource_kind": "gpu", "cpu_threads_intensity": None}
    for invalid in ("x" * 513, "pin\x7fvalue", 123):
        with pytest.raises(ManagedMinerError):
            validate_managed_extra_config("srbminer", {"tls_cert_sha256": invalid})


@pytest.mark.parametrize("pool,expected", [
    ("quic://node.example.test:3333", "node.example.test:3333"),
    ("quic://[2001:db8::4]:3344", "[2001:db8::4]:3344"),
])
def test_srb_quantus_quic_argv_uses_secret_auth_token_and_opaque_pin(tmp_path: Path, pool, expected):
    doc = document(tmp_path, miner_type="srbminer", algorithm="quantus")
    primary = doc["primary"]
    primary.update(coin="QUANTUS", pool_url=pool, pool_password="node-auth-token", wallet_address="public-payment-wallet")
    primary["miner_package"]["version"] = "3.7.1"
    primary["extra_config"].update(tls_cert_sha256="node:opaque/pin=not-hex", quic_wallet_mode="token")
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    assert argv[argv.index("--pool") + 1] == expected
    assert argv[argv.index("--wallet") + 1] == "node-auth-token"
    assert argv[argv.index("--tls-cert-sha256") + 1] == "node:opaque/pin=not-hex"
    assert "public-payment-wallet" not in argv
    assert argv.count("node-auth-token") == 1
    assert "--worker" not in argv


@pytest.mark.parametrize("version,allowed", [("3.6.3", False), ("3.6.4", True)])
def test_srb_quantus_known_algorithm_floor_applies_to_cli_context(tmp_path: Path, version, allowed):
    doc = document(tmp_path, miner_type="srbminer", algorithm="quantus")
    doc["primary"]["coin"] = "QUANTUS"
    doc["primary"]["miner_package"]["version"] = version
    call = lambda: build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    if allowed:
        assert "quantus" in call()
    else:
        with pytest.raises(ManagedMinerError, match="3.6.4"):
            call()


@pytest.mark.parametrize("name,version,allowed", [
    ("intel_arc_a770", "3.6.9", False), ("intel_arc_a770", "3.7.0", True),
    ("amd_radeon_rx580", "3.6.7", False), ("amd_radeon_rx580", "3.6.8", True),
])
def test_srb_quantus_verified_device_backend_floor(tmp_path: Path, name, version, allowed):
    doc = document(tmp_path, miner_type="srbminer", algorithm="quantus")
    doc["primary"].update(coin="QUANTUS", device_indices=[0])
    doc["primary"]["miner_package"]["version"] = version
    runner = lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, f"GPU0 [0][0] [0000:01:00.0] : {name} [8192 MB]", "")
    call = lambda: resolve_srbminer_gpu_indices(doc, executable=tmp_path / "SRBMiner-MULTI", runner=runner)
    if allowed:
        assert call()["primary"]["device_indices"] == [0]
    else:
        with pytest.raises(ManagedMinerError, match="requires SRBMiner"):
            call()


@pytest.mark.parametrize("mode", [None, "wallet"])
def test_srb_quantus_public_quic_preserves_exact_wallet_without_worker_suffix(tmp_path: Path, mode):
    doc = document(tmp_path, miner_type="srbminer", algorithm="quantus")
    primary = doc["primary"]
    primary.update(coin="QUANTUS", pool_url="quic://pool.example.test:3333",
                   pool_password="must-not-be-inferred-as-token", wallet_address="public-payout-wallet")
    primary["miner_package"]["version"] = "3.7.1"
    primary["extra_config"]["tls_cert_sha256"] = "opaque-public-pin"
    if mode is not None:
        primary["extra_config"]["quic_wallet_mode"] = mode
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    assert argv[argv.index("--wallet") + 1] == "public-payout-wallet"
    assert "--worker" not in argv
    assert "must-not-be-inferred-as-token" not in argv


@pytest.mark.parametrize("algorithm,resource,indices,expected", [
    ("curvehash", "gpu", [], [4]),
    ("yespower", "gpu", [], [1]),
    ("curvehash", "cpu", [], []),
])
def test_srb_shared_vendor_facts_filter_gpu_auto_selection_only(tmp_path: Path, algorithm, resource, indices, expected):
    doc = document(tmp_path, miner_type="srbminer", algorithm=algorithm)
    doc["primary"].update(coin="TEST", device_indices=indices)
    doc["primary"]["miner_package"]["version"] = "3.7.1"
    doc["primary"]["extra_config"]["resource_kind"] = resource
    result = resolve_srbminer_gpu_indices(doc, executable=tmp_path / "SRBMiner-MULTI", runner=completed_device_list())
    assert result["primary"]["device_indices"] == expected


def test_srb_shared_vendor_facts_reject_explicit_incompatible_gpu(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="curvehash")
    doc["primary"].update(coin="TEST", device_indices=[1])
    doc["primary"]["miner_package"]["version"] = "3.7.1"
    doc["primary"]["extra_config"]["resource_kind"] = "gpu"
    with pytest.raises(ManagedMinerError, match="does not support the selected GPU vendor"):
        resolve_srbminer_gpu_indices(doc, executable=tmp_path / "SRBMiner-MULTI", runner=completed_device_list())


def test_srb_cpu_task_cannot_gain_wsl_runtime_from_algorithm_capabilities(tmp_path: Path, monkeypatch):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-srbminer-test-linux"
    package.mkdir()
    (package / "SRBMiner-MULTI").write_bytes(b"miner")
    doc = document(package, miner_type="srbminer", algorithm="cpupower")
    primary = doc["primary"]
    primary.update(coin="TEST", execution_mode="wsl")
    primary["miner_package"].update(version="3.7.1", managed_os_family="linux")
    monkeypatch.setattr(managed_miners_module, "prepare_wsl_gpu_runtime",
                        lambda *_args, **_kwargs: pytest.fail("CPU task must not prepare WSL"))
    with pytest.raises(ManagedMinerError, match="GPU miners"):
        ManagedMinerManager(cfg, system_name="Windows")._prepare_single(doc)


@pytest.mark.parametrize("primary_algorithm,primary_resource,secondary_algorithm,secondary_resource,disabled", [
    ("progpow_epic", "gpu", "curvehash", "gpu", "--disable-cpu"),
    ("cpupower", "cpu", "curvehash", "gpu", None),
    ("randomepic", "cpu", "cpupower", "cpu", "--disable-gpu"),
])
def test_srb_dual_actual_resources_disable_only_unused_kind(
    tmp_path: Path, primary_algorithm, primary_resource, secondary_algorithm, secondary_resource, disabled,
):
    doc = document(tmp_path, miner_type="srbminer", algorithm=primary_algorithm,
                   extra_config={"resource_kind": primary_resource})
    doc["primary"]["coin"] = "TEST"
    doc["primary"]["miner_package"]["version"] = "3.7.1"
    doc["secondary"] = leg(tmp_path, miner_type="srbminer", algorithm=secondary_algorithm,
                           extra_config={"resource_kind": secondary_resource})
    doc["secondary"]["coin"] = "TEST"
    doc["secondary"]["miner_package"]["version"] = "3.7.1"
    if primary_resource == secondary_resource == "gpu":
        doc["primary"]["device_indices"] = [4]
        doc["secondary"]["device_indices"] = [4]
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    if disabled:
        assert disabled in argv
    else:
        assert "--disable-cpu" not in argv and "--disable-gpu" not in argv


@pytest.mark.parametrize("entry", ["resource", "context", "connection"])
@pytest.mark.parametrize("extra", [None, [], "", 0, False])
def test_srb_context_helpers_reject_explicit_non_object_options(tmp_path: Path, entry, extra):
    primary = leg(tmp_path, miner_type="srbminer", algorithm="cpupower")
    primary.update(coin="TEST", extra_config=extra)
    primary["miner_package"]["version"] = "3.7.1"
    calls = {
        "resource": managed_miners_module.leg_effective_resource,
        "context": managed_miners_module.validate_managed_algorithm_context,
        "connection": managed_miners_module._srb_connection_args,
    }
    with pytest.raises(ManagedMinerError, match="must be an object"):
        calls[entry](primary)


@pytest.mark.parametrize("entry", ["resource", "context", "connection"])
def test_srb_context_helpers_keep_missing_options_default(tmp_path: Path, entry):
    primary = leg(tmp_path, miner_type="srbminer", algorithm="cpupower")
    primary["coin"] = "TEST"
    primary["miner_package"]["version"] = "3.7.1"
    primary.pop("extra_config")
    calls = {
        "resource": managed_miners_module.leg_effective_resource,
        "context": managed_miners_module.validate_managed_algorithm_context,
        "connection": managed_miners_module._srb_connection_args,
    }
    assert calls[entry](primary)


@pytest.mark.parametrize("algorithm", ["cpupower", "pearlhash"])
def test_srb_xmr_constraint_is_consistent_across_context_dual_and_argv(tmp_path: Path, algorithm):
    doc = document(tmp_path, miner_type="srbminer", algorithm=algorithm)
    primary = doc["primary"]
    primary["miner_package"]["version"] = "3.7.1"
    secondary = leg(tmp_path, miner_type="srbminer", algorithm="randomx")
    secondary["miner_package"]["version"] = "3.7.1"
    calls = [
        lambda: managed_miners_module.validate_managed_algorithm_context(primary),
        lambda: managed_miners_module.validate_srb_dual_configuration(primary, secondary),
        lambda: build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log"),
    ]
    for call in calls:
        with pytest.raises(ManagedMinerError, match="(?i)randomx"):
            call()


@pytest.mark.parametrize("algorithm", ["rx", "rx/0", " rx/0 "])
def test_srb_shared_xmr_alias_context_and_dual_normalization_agree(tmp_path: Path, algorithm):
    primary = leg(tmp_path, miner_type="srbminer", algorithm=algorithm)
    primary["miner_package"]["version"] = "3.7.1"
    secondary = leg(tmp_path, miner_type="srbminer", algorithm="curvehash", extra_config={"resource_kind": "gpu"})
    secondary["coin"] = "GPUCHAIN"
    secondary["miner_package"]["version"] = "3.7.1"
    assert managed_miners_module.validate_managed_algorithm_context(primary) == "randomx"
    assert managed_miners_module.validate_srb_dual_configuration(primary, secondary) == ("randomx", "curvehash")


def test_srb_other_chain_cpu_and_ordered_epic_wildcard_remain_legal(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="cpupower")
    primary = doc["primary"]
    primary.update(coin="CPUCHAIN")
    primary["miner_package"]["version"] = "3.7.1"
    assert managed_miners_module.leg_effective_resource(primary) == "cpu"
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    assert "cpupower" in argv and "--disable-gpu" in argv
    primary.update(coin="EPIC", algorithm="randomepic")
    secondary = leg(tmp_path, miner_type="srbminer", algorithm="randomx")
    secondary["miner_package"]["version"] = "3.7.1"
    doc["secondary"] = secondary
    assert managed_miners_module.validate_srb_dual_configuration(primary, secondary) == ("randomepic", "randomx")
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    assert "randomepic" in argv and "randomx" in argv and "--disable-gpu" in argv


def test_srb_shared_gpu_pair_keeps_both_oc_algorithm_slots(tmp_path: Path):
    doc = document(tmp_path, miner_type="srbminer", algorithm="fishhash")
    primary = doc["primary"]
    primary.update(coin="GPUCHAIN", device_indices=[1])
    primary["miner_package"]["version"] = "3.7.1"
    secondary = leg(tmp_path, miner_type="srbminer", algorithm="qhash")
    secondary.update(coin="GPUCHAIN", device_indices=[1])
    secondary["miner_package"]["version"] = "3.7.1"
    doc["secondary"] = secondary
    doc["gpu_oc"] = {"vendor": "nvidia", "settings": [{"index": 1, "core_clock_offset_mhz": 0}]}
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    assert "--gpu-coffset0" in argv and "--gpu-coffset1" in argv
    assert "--disable-cpu" in argv


@pytest.mark.parametrize("key,first,second,flag,encoded", [
    ("cpu_threads", 2, 8, "--cpu-threads", "2;8"),
    ("cpu_threads_reduce", 2, 8, "--cpu-threads-reduce", "2;8"),
    ("cpu_threads_reduce", None, 8, "--cpu-threads-reduce", "0;8"),
    ("cpu_threads_reduce", 2, None, "--cpu-threads-reduce", "2;0"),
    ("cpu_threads_intensity", 2, 4, "--cpu-threads-intensity", "2;4"),
    ("cpu_threads_intensity", None, 4, "--cpu-threads-intensity", "1;4"),
    ("cpu_threads_intensity", 2, None, "--cpu-threads-intensity", "2;1"),
])
def test_srb_cpu_dual_preserves_both_numeric_algorithm_options(tmp_path: Path, key, first, second, flag, encoded):
    doc = document(tmp_path, miner_type="srbminer", algorithm="randomepic")
    doc["primary"]["coin"] = "EPIC"
    doc["primary"]["miner_package"]["version"] = "3.7.1"
    doc["secondary"] = leg(tmp_path, miner_type="srbminer", algorithm="randomx")
    doc["secondary"]["miner_package"]["version"] = "3.7.1"
    if first is not None:
        doc["primary"]["extra_config"][key] = first
    if second is not None:
        doc["secondary"]["extra_config"][key] = second
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    assert argv[argv.index(flag) + 1] == encoded
    assert argv.count(flag) == 1
    assert "--disable-gpu" in argv


@pytest.mark.parametrize("first,second", [(2, None), (None, 8)])
def test_srb_cpu_dual_partial_threads_reports_current_formatter_limitation(tmp_path: Path, first, second):
    doc = document(tmp_path, miner_type="srbminer", algorithm="randomepic")
    doc["primary"]["coin"] = "EPIC"
    doc["primary"]["miner_package"]["version"] = "3.7.1"
    doc["secondary"] = leg(tmp_path, miner_type="srbminer", algorithm="randomx")
    doc["secondary"]["miner_package"]["version"] = "3.7.1"
    if first is not None:
        doc["primary"]["extra_config"]["cpu_threads"] = first
    if second is not None:
        doc["secondary"]["extra_config"]["cpu_threads"] = second
    with pytest.raises(ManagedMinerError, match="(?i)current launcher.*represent|formatter|表达|格式|编码"):
        build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")


@pytest.mark.parametrize("mode,first,second,expected", [
    ("dual_cpu", None, None, None),
    ("dual_cpu", 2, 8, "2;8"),
    ("single", 2, None, "2"),
    ("single", None, None, None),
    ("cpu_gpu", 2, None, "2"),
])
def test_srb_thread_formatter_limitation_keeps_supported_task_shapes(tmp_path: Path, mode, first, second, expected):
    doc = document(tmp_path, miner_type="srbminer", algorithm="randomepic")
    doc["primary"]["coin"] = "EPIC"
    doc["primary"]["miner_package"]["version"] = "3.7.1"
    if first is not None:
        doc["primary"]["extra_config"]["cpu_threads"] = first
    if mode != "single":
        doc["secondary"] = leg(tmp_path, miner_type="srbminer", algorithm="randomx" if mode == "dual_cpu" else "curvehash")
        doc["secondary"]["miner_package"]["version"] = "3.7.1"
        if mode == "cpu_gpu":
            doc["secondary"]["coin"] = "GPUCHAIN"
            doc["secondary"]["extra_config"]["resource_kind"] = "gpu"
        if second is not None:
            doc["secondary"]["extra_config"]["cpu_threads"] = second
    argv = build_srbminer_argv(doc, executable=tmp_path / "SRBMiner-MULTI", api_port=24567, log_path=tmp_path / "miner.log")
    if expected is None:
        assert "--cpu-threads" not in argv
    else:
        assert argv[argv.index("--cpu-threads") + 1] == expected


class OwnedFailedLaunchHandle:
    def __init__(self, pid, *, cleanup_succeeds):
        self.pid = pid
        self.cleanup_succeeds = cleanup_succeeds
        self.calls = []

    def terminate(self):
        self.calls.append("terminate")
        if not self.cleanup_succeeds:
            raise PermissionError("owned terminate unavailable")

    def wait(self, timeout):
        self.calls.append(("wait", timeout))
        if not self.cleanup_succeeds:
            raise OSError("owned wait unavailable")
        return 0

    def kill(self):
        self.calls.append("kill")
        if not self.cleanup_succeeds:
            raise PermissionError("owned kill unavailable")


@pytest.mark.parametrize("cleanup_succeeds", [False, True])
def test_created_native_child_probe_failure_preserves_cleanup_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_succeeds: bool,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    owned = OwnedFailedLaunchHandle(721, cleanup_succeeds=cleanup_succeeds)
    probe_calls = []

    def unavailable_identity(pid):
        probe_calls.append(pid)
        raise psutil.AccessDenied(pid)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *_args, **_kwargs: owned,
        process_factory=unavailable_identity,
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerTransitionError) as caught:
        manager.apply(document(package))

    error = caught.value
    assert error.state_changed is True
    assert error.target_started is True
    assert error.target_cleanup_unconfirmed is (not cleanup_succeeds)
    assert bool(error.rollback_errors) is (not cleanup_succeeds)
    assert "could not be started" in error.message
    assert probe_calls == [721]
    assert owned.calls[:2] == ["terminate", ("wait", 10)]
    if not cleanup_succeeds:
        assert owned.calls[2:] == ["kill", ("wait", 5)]
    assert not (cfg.managed_miner_runtime_dir / "managed-miner-process.json").exists()


@pytest.mark.parametrize("cleanup_succeeds", [False, True])
def test_independent_secondary_probe_failure_preserves_owned_child_cleanup_phase(
    tmp_path: Path,
    cleanup_succeeds: bool,
):
    cfg = managed_config(tmp_path)
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    pearl_root = cfg.miner_packages_dir / "builtin-pearlminer-test-1.0"
    xmrig_root.mkdir()
    pearl_root.mkdir()
    xmrig_executable = xmrig_root / "xmrig.exe"
    xmrig_executable.write_bytes(b"xmrig")
    (pearl_root / "miner.exe").write_bytes(b"pearl")
    primary = FakeProcess(724, xmrig_executable.resolve(), 2724.0)
    secondary = OwnedFailedLaunchHandle(725, cleanup_succeeds=cleanup_succeeds)
    def popen(argv, **kwargs):
        return primary.popen(argv, **kwargs) if Path(argv[0]) == xmrig_executable.resolve() else secondary
    ports = iter((24567, 24568))

    def inspect_process(pid):
        if pid == 724:
            return primary
        raise psutil.AccessDenied(pid)

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=inspect_process,
        process_iter_factory=lambda: [primary],
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: next(ports),
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerTransitionError) as caught:
        manager.apply(independent_document(xmrig_root, pearl_root))

    error = caught.value
    assert error.state_changed and error.target_started
    assert error.target_cleanup_unconfirmed is (not cleanup_succeeds)
    assert bool(error.rollback_errors) is (not cleanup_succeeds)
    assert primary.terminated is True
    for role in ("primary", "secondary"):
        assert not (cfg.managed_miner_runtime_dir / role / "managed-miner-process.json").exists()


@pytest.mark.parametrize(
    "host_cleanup_succeeds,guest_cleanup_succeeds",
    [(True, True), (False, True), (True, False)],
)
def test_created_wsl_child_probe_failure_keeps_host_and_guest_cleanup_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_cleanup_succeeds: bool,
    guest_cleanup_succeeds: bool,
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    executable = tmp_path / "wsl.exe"
    executable.write_bytes(b"wrapper")
    owned = OwnedFailedLaunchHandle(722, cleanup_succeeds=host_cleanup_succeeds)
    identity = WslLaunchIdentity(1, "S-1-5-21-test", "Ubuntu", 1000)
    token = "a" * 64
    guest = "/verified/cache/xmrig"
    guest_calls = []
    monkeypatch.setattr(managed_miners_module, "active_user_popen", lambda *_args, **_kwargs: owned)

    def stop_guest(*args):
        guest_calls.append(args)
        if not guest_cleanup_succeeds:
            raise managed_miners_module.WslMiningError("guest cleanup unavailable")
        return False  # Already absent, independently verified by the control path.

    monkeypatch.setattr(managed_miners_module, "stop_wsl_guest", stop_guest)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.AccessDenied(pid)),
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerTransitionError) as caught:
        manager._launch(
            [str(executable)], executable, runtime,
            execution_mode="wsl", wsl_token=token,
            wsl_guest_executable=guest, wsl_identity=identity,
        )

    error = caught.value
    unconfirmed = not (host_cleanup_succeeds and guest_cleanup_succeeds)
    assert error.state_changed and error.target_started
    assert error.target_cleanup_unconfirmed is unconfirmed
    assert bool(error.rollback_errors) is unconfirmed
    assert guest_calls == [(executable, token, guest, identity)]
    assert owned.calls[:2] == ["terminate", ("wait", 10)]


def test_partial_explicit_residue_group_failure_reports_changed_without_full_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-pearlminer-test-2.0"
    package.mkdir()
    executable = package / "miner-cuda13"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    write_process_record(state_path, executable, pid=589, created=1000.25, miner_type="pearlminer")
    upgrade_process_record_to_v2(state_path, [str(executable.resolve()), "--pool", "secret.example"])
    leader = FakeProcess(589, executable.resolve(), 1000.25, [str(executable.resolve()), ""])
    child = FakeProcess(617, executable.resolve(), 1001.0, [str(executable.resolve()), ""])
    identities = {589: (1, 589, 589), 617: (589, 589, 589)}
    monkeypatch.setattr("agent.process_identity._native_process_group_identity", lambda _process, pid: identities[pid])

    def deny_leader_termination():
        raise psutil.AccessDenied(589)

    monkeypatch.setattr(leader, "terminate", deny_leader_termination)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: [leader, child],
        boot_time_factory=lambda: 900.0,
        system_name="Linux",
    )
    scan = manager.scan_residue()
    finding = next(item for item in scan["findings"] if item["role"] == "legacy")
    candidate = finding["manual_candidates"][0]

    result = manager.cleanup_residue(scan["scan_fingerprint"], [{
        "role": "legacy", "target_fingerprint": finding["target_fingerprint"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
    }])

    assert child.terminated and not leader.terminated
    assert result["blocked"] is True
    assert result["changed"] is True
    assert result["processes_stopped"] == 0
    assert result["records_removed"] == 0
    assert result["results"][0]["status"] == "cleanup_failed"
    assert result["results"][0]["process_stopped"] is False
    assert state_path.exists()


def test_target_xmrig_prune_failure_keeps_ready_miner_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"xmrig")
    process = FakeProcess(213, executable.resolve(), 2213.0)
    manager = ManagedMinerManager(
        cfg,
        popen_factory=process.popen,
        process_factory=lambda _pid: process,
        process_iter_factory=lambda: [process],
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
        enforce_startup_readiness=True,
    )
    monkeypatch.setattr(manager, "_loopback_api_is_listening", lambda _port: True)
    prune_calls = []

    def fail_prune(runtime, *, keep=None):
        prune_calls.append((runtime, keep))
        raise ManagedMinerError("obsolete configuration cleanup unavailable")

    monkeypatch.setattr(managed_miners_module, "_prune_xmrig_session_configs", fail_prune)

    result = manager.apply(document(package))

    assert result["pid"] == 213
    assert result["readiness"] == "loopback_api"
    assert process.terminated is False
    assert json.loads(cfg.miner_status_file.read_text())["state"] == "running"
    record = json.loads(
        (cfg.managed_miner_runtime_dir / "managed-miner-process.json").read_text()
    )
    assert record["pid"] == 213
    current_calls = [item for item in prune_calls if item[0] == cfg.managed_miner_runtime_dir]
    assert len(current_calls) == 1
    runtime, keep = current_calls[0]
    assert runtime == cfg.managed_miner_runtime_dir
    assert keep.is_file() and keep.name.startswith("xmrig-config-")
    assert any(
        getattr(item, "event", None) == "managed_miner_xmrig_prune_failed"
        for item in caplog.records
    )


def test_obsolete_xmrig_prune_does_not_fail_ready_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Windows")
    runtime = cfg.managed_miner_runtime_dir
    (runtime / "primary").mkdir(parents=True)
    target = document(tmp_path / "package")
    prepared = {
        "runtime": runtime,
        "miner_type": "xmrig",
        "api_port": 24567,
        "gpu_mapping": [],
    }
    monkeypatch.setattr(manager, "_prepare_single", lambda _document: prepared)
    monkeypatch.setattr(manager, "_preflight_transition_slots", lambda _runtime: [])
    monkeypatch.setattr(
        manager, "_stop_preflighted_transition_slots", lambda _slots: True
    )
    monkeypatch.setattr(manager, "_unlink_state", lambda _path: None)
    monkeypatch.setattr(
        manager,
        "_apply_single",
        lambda *_args, **_kwargs: {
            "managed": True,
            "miner_type": "xmrig",
            "flight_sheet_id": target["flight_sheet_id"],
            "pid": 42,
        },
    )
    monkeypatch.setattr(
        managed_miners_module,
        "_prune_xmrig_session_configs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ManagedMinerError("cleanup unavailable")
        ),
    )

    result = manager.apply(target)

    assert result["pid"] == 42
    assert result["replaced_previous"] is True


def test_role_runtime_symlink_is_rejected(tmp_path: Path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (runtime / "primary").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating a directory symlink is not permitted")
    manager = ManagedMinerManager(cfg)

    with pytest.raises(ManagedMinerError, match="must be a real local directory"):
        manager.snapshot_independent_metadata()


def test_windows_miner_restart_survival_is_recorded_only_after_job_query_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    executable = tmp_path / "miner.exe"
    executable.write_bytes(b"miner")
    process = FakeProcess(202, executable.resolve(), 2000.5)
    monkeypatch.setattr(
        ManagedMinerManager,
        "_outside_windows_job",
        staticmethod(lambda _handle: True),
    )
    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *_args, **_kwargs: FakePopen(),
        process_factory=lambda _pid: process,
        system_name="Windows",
    )

    pid, created, survives = manager._launch(
        [str(executable)], executable.resolve(), runtime
    )

    assert (pid, created, survives) == (202, 2000.5, True)


def test_windows_miner_breakaway_denial_falls_back_but_is_not_restart_safe(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    executable = tmp_path / "miner.exe"
    executable.write_bytes(b"miner")
    process = FakeProcess(202, executable.resolve(), 2000.5)
    calls = []

    def popen(*_args, **kwargs):
        calls.append(kwargs["creationflags"])
        if len(calls) == 1:
            raise PermissionError("job does not permit breakaway")
        return FakePopen()

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=lambda _pid: process,
        system_name="Windows",
    )

    _pid, _created, survives = manager._launch(
        [str(executable)], executable.resolve(), runtime
    )

    assert survives is False
    assert len(calls) == 2
    assert calls[0] & 0x01000000
    assert not calls[1] & 0x01000000


def test_linux_miner_launch_uses_pre_bundle_dynamic_loader_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    executable = tmp_path / "xmrig"
    executable.write_bytes(b"miner")
    process = FakeProcess(202, executable.resolve(), 2000.5)
    calls = []
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI789:/agent-libs")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib:/usr/local/cuda/lib64")

    def popen(*_args, **kwargs):
        calls.append(kwargs)
        return FakePopen()

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=lambda _pid: process,
        system_name="Linux",
    )

    _pid, _created, survives = manager._launch(
        [str(executable)], executable.resolve(), runtime
    )

    assert survives is True
    assert calls[0]["start_new_session"] is True
    assert calls[0]["env"]["LD_LIBRARY_PATH"] == "/usr/lib:/usr/local/cuda/lib64"
    assert "LD_LIBRARY_PATH_ORIG" not in calls[0]["env"]


def test_manager_rejects_srbminer_oc_before_discovery_when_not_elevated(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    package.mkdir()
    executable = package / "SRBMiner-MULTI.exe"
    executable.write_bytes(b"test")
    doc = document(package, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = [1]
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"index": 1, "power_limit_w": 250}],
    }
    discovery_calls = []
    launch_calls = []
    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *args, **kwargs: launch_calls.append((args, kwargs)),
        process_iter_factory=lambda: [],
        port_allocator=lambda: 24567,
        system_name="Windows",
        device_runner=lambda *args, **kwargs: discovery_calls.append((args, kwargs)),
        privilege_checker=lambda system_name: False,
    )

    with pytest.raises(ManagedMinerError, match="run as administrator/root"):
        manager.apply(doc)

    assert discovery_calls == []
    assert launch_calls == []


def test_manager_omits_nvidia_only_reset_for_amd_only_srb_flight(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    package.mkdir()
    executable = package / "SRBMiner-MULTI.exe"
    executable.write_bytes(b"test")
    doc = document(package, miner_type="srbminer", algorithm="etchash")
    doc["primary"]["coin"] = "ETC"
    doc["primary"]["device_indices"] = [4]
    doc["_reset_srb_gpu_oc"] = True
    manager = ManagedMinerManager(
        cfg,
        port_allocator=lambda: 24567,
        system_name="Windows",
        device_runner=completed_device_list(
            "GPU4 [0][1] [0000:04:00.0] : "
            "amd_radeon_rx_7900_xtx [gfx1100] [24576 MB]"
        ),
        process_iter_factory=lambda: [],
        privilege_checker=lambda _system_name: False,
    )

    prepared = manager._prepare_single(doc)

    assert prepared["gpu_mapping"][0]["vendor"] == "amd"
    assert "--gpu-reset-oc" not in prepared["argv"]
    # The saved/controller document is desired state and is never mutated by
    # local device-specific command construction.
    assert doc["_reset_srb_gpu_oc"] is True


def test_manager_returns_safe_gpu_mapping_and_launches_real_srb_id(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    package.mkdir()
    executable = package / "SRBMiner-MULTI.exe"
    executable.write_bytes(b"test")
    doc = document(package, miner_type="srbminer", algorithm="pearlhash")
    doc["primary"]["coin"] = "PRL"
    doc["primary"]["device_indices"] = [1]
    doc["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"index": 1, "power_limit_w": 250}],
    }
    launched = []
    process = FakeProcess(202, executable.resolve(), 2000.5)

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        process.executable = Path(argv[0])
        return FakePopen()

    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen,
        process_factory=lambda pid: process,
        process_iter_factory=lambda: [process],
        port_allocator=lambda: 24567,
        system_name="Windows",
        device_runner=completed_device_list(),
        privilege_checker=lambda system_name: True,
    )

    result = manager.apply(doc)

    assert result["gpu_mapping"] == [
        {
            "logical_index": 0,
            "miner_id": 1,
            "vendor": "nvidia",
            "name": "nvidia_geforce_rtx_5080",
            "pci_bus_id": "0000:01:00.0",
        }
    ]
    status = json.loads(cfg.miner_status_file.read_text(encoding="utf-8"))
    assert status["gpu_mapping"] == result["gpu_mapping"]
    argv = launched[0][0]
    assert argv[argv.index("--gpu-id") + 1] == "1"
    assert argv[argv.index("--gpu-plimit0") + 1] == "250"


def native_srb_fixture(tmp_path: Path, *, system_name: str = "Linux"):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    package.mkdir()
    name = "SRBMiner-MULTI.exe" if system_name == "Windows" else "SRBMiner-MULTI"
    (package / name).write_bytes(b"synthetic native SRBMiner")
    (package / "lib-resource.bin").write_bytes(b"verified resource")
    flight = document(package, miner_type="srbminer", algorithm="pearlhash")
    flight["primary"]["coin"] = "PRL"
    flight["primary"]["device_indices"] = [1]
    return cfg, package, flight


def test_native_srb_probe_launch_and_transient_reset_keep_source_sealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    cfg, package, flight = native_srb_fixture(tmp_path)
    original_marker = (package / ".rigpilot-package.json").read_bytes()
    original_tree = miner_packages_module._tree_fingerprint(package, 1024 * 1024)
    probes, launches, processes, observed_runtime_bytes = [], [], {}, {}

    def probe(argv, **kwargs):
        probes.append((argv, kwargs))
        kernels = Path(kwargs["cwd"]) / "Cache" / "Kernels"
        kernels.mkdir(parents=True, exist_ok=True)
        (kernels / "generated.bin").write_bytes(b"runtime kernel")
        observed_runtime_bytes[argv[0]] = (kernels / "generated.bin").read_bytes()
        assert not (Path(kwargs["cwd"]) / ".rigpilot-package.json").exists()
        return completed_device_list()(argv, **kwargs)

    def popen(argv, **kwargs):
        launches.append((argv, kwargs))
        assert (Path(kwargs["cwd"]) / "Cache/Kernels/generated.bin").read_bytes() == b"runtime kernel"
        assert not (Path(kwargs["cwd"]) / ".rigpilot-package.json").exists()
        pid = 200 + len(launches)
        processes[pid] = FakeProcess(pid, Path(argv[0]), 2000.0 + pid, argv)
        return FakePopen(pid)

    def process(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]

    manager = ManagedMinerManager(
        cfg, system_name="Linux", popen_factory=popen, process_factory=process,
        process_iter_factory=lambda: list(processes.values()), boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567, device_runner=probe, privilege_checker=lambda _system: True,
    )
    first = manager.apply(flight)
    monkeypatch.setattr(manager, "_wait_for_srb_oc_reset_ready", lambda *_args: "verified_reset")
    reset = manager._clear_srb_gpu_oc_document(flight)

    assert reset["cleared"] is True
    assert first["pid"] != reset["temporary_pid"]
    assert len(probes) == len(launches) == 2
    assert "--gpu-reset-oc" in launches[1][0]
    assert probes[0][0][0] == launches[0][0][0]
    assert probes[1][0][0] == launches[1][0][0]
    assert launches[0][0][0] != launches[1][0][0]
    namespace = cfg.miner_packages_dir / "rigpilot-native-srb-runtime-v2"
    for argv, kwargs in probes + launches:
        executable = Path(argv[0])
        assert executable.is_relative_to(namespace / "single")
        assert kwargs["cwd"] == str(executable.parent)
        assert observed_runtime_bytes[argv[0]] == b"runtime kernel"
        assert not executable.parent.parent.exists()
    assert flight["primary"]["miner_package"]["installed_path"] == str(package)
    assert (package / ".rigpilot-package.json").read_bytes() == original_marker
    assert miner_packages_module._tree_fingerprint(package, 1024 * 1024) == original_tree
    assert manager._native_srb_registry_key() not in managed_miners_module._NATIVE_SRB_PREPARED
    assert all(not process.running for process in processes.values())
    assert list((namespace / "single").iterdir()) == []


def test_native_srb_corrupt_source_rejected_before_probe_or_stop(tmp_path: Path):
    cfg, package, flight = native_srb_fixture(tmp_path)
    probes, processes = [], {}

    def probe(argv, **kwargs):
        probes.append(argv)
        return completed_device_list()(argv, **kwargs)

    def popen(argv, **_kwargs):
        processes[202] = FakeProcess(202, Path(argv[0]), 2000.5)
        return FakePopen()

    manager = ManagedMinerManager(
        cfg, system_name="Linux", popen_factory=popen,
        process_factory=processes.__getitem__, process_iter_factory=lambda: list(processes.values()),
        boot_time_factory=lambda: 0.0, port_allocator=lambda: 24567, device_runner=probe,
    )
    manager.apply(flight)
    marker = (package / ".rigpilot-package.json").read_bytes()
    (package / "lib-resource.bin").write_bytes(b"corrupt source")

    with pytest.raises(ManagedMinerError, match="could not be verified"):
        manager.apply(flight)

    assert len(probes) == 1
    assert processes[202].running and not processes[202].terminated
    assert (package / ".rigpilot-package.json").read_bytes() == marker
    assert manager._native_srb_registry_key() not in managed_miners_module._NATIVE_SRB_PREPARED


@pytest.mark.parametrize("proof", [False, True])
@pytest.mark.parametrize("failure", ["returncode", "exception"])
def test_native_srb_failed_probe_releases_lease_and_cleans_only_with_exit_proof(tmp_path, proof, failure):
    cfg, package, flight = native_srb_fixture(tmp_path)
    observed = []
    def failed_probe(argv, **kwargs):
        bundle = Path(kwargs["cwd"])
        observed.append(bundle)
        (bundle / "runtime-kernel.bin").write_bytes(b"probe generated bytes")
        if failure == "exception":
            error = OSError("injected probe exception")
            error.owned_direct_child_closed = proof
            raise error
        result = subprocess.CompletedProcess(argv, 1, "", "synthetic failure")
        result.owned_direct_child_closed = proof
        return result
    manager = ManagedMinerManager(cfg, system_name="Linux", process_iter_factory=lambda: [],
                                  port_allocator=lambda: 24567, device_runner=failed_probe)
    marker = (package / ".rigpilot-package.json").read_bytes()
    for _ in range(2):
        with pytest.raises(ManagedMinerError, match="GPU discovery failed"):
            manager.apply(flight)
        assert manager._native_srb_registry_key() not in managed_miners_module._NATIVE_SRB_PREPARED
    assert observed[0] != observed[1]
    for bundle in observed:
        if proof:
            assert not bundle.parent.exists()
        else:
            assert (bundle / "runtime-kernel.bin").read_bytes() == b"probe generated bytes"
    assert (package / ".rigpilot-package.json").read_bytes() == marker


def test_native_srb_prepared_plans_are_shared_across_managers(tmp_path: Path):
    cfg, _package, flight = native_srb_fixture(tmp_path)
    options = dict(system_name="Linux", process_iter_factory=lambda: [],
                   port_allocator=lambda: 24567, device_runner=completed_device_list())
    first = ManagedMinerManager(cfg, **options)
    second = ManagedMinerManager(cfg, **options)
    plans = []
    try:
        plans.append(first._prepare_single(flight))
        plans.append(second._prepare_single(flight))
        assert plans[0]["executable"] != plans[1]["executable"]
        retained = plans[1]["executable"].read_bytes()
        first._release_native_srb_plan(plans[0])
        plans.append(first._prepare_single(flight))
        assert plans[2]["executable"] != plans[0]["executable"]
        assert plans[1]["executable"].read_bytes() == retained
    finally:
        for plan in plans:
            first._release_native_srb_plan(plan)


def test_native_srb_role_namespace_uses_explicit_original_runtime(tmp_path: Path):
    cfg, _package, flight = native_srb_fixture(tmp_path)
    base = tmp_path / "primary"
    cfg = replace(cfg, managed_miner_runtime_dir=base, miner_status_file=base / "miner-status.json")
    manager = ManagedMinerManager(
        cfg, system_name="Linux", process_iter_factory=lambda: [],
        port_allocator=lambda: 24567, device_runner=completed_device_list(),
    )
    single = manager._prepare_single(flight)
    role = manager._role_manager(manager._runtime(), "primary")
    prepared = None
    try:
        prepared = role._prepare_single(flight)
        assert single["executable"].is_relative_to(cfg.miner_packages_dir / "rigpilot-native-srb-runtime-v2/single")
        assert prepared["executable"].is_relative_to(cfg.miner_packages_dir / "rigpilot-native-srb-runtime-v2/primary")
        control = json.loads((prepared["executable"].parent.parent / "control.json").read_text())
        assert control["role"] == "primary"
        assert control["runtime"] == str(base / "primary")
    finally:
        manager._release_native_srb_plan(single)
        role._release_native_srb_plan(prepared)


def native_generation_manager(cfg, *, system_name="Linux", probe=None):
    processes, launches = {}, []
    def popen(argv, **kwargs):
        pid = 300 + len(launches)
        launches.append((list(argv), dict(kwargs)))
        processes[pid] = FakeProcess(pid, Path(argv[0]), 1000.0 + pid, argv)
        return FakePopen(pid)
    def lookup(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]
    manager = ManagedMinerManager(cfg, system_name=system_name, popen_factory=popen,
        process_factory=lookup, process_iter_factory=lambda: list(processes.values()),
        boot_time_factory=lambda: 0.0, port_allocator=lambda: 24567,
        device_runner=probe or completed_device_list(), privilege_checker=lambda _system: True)
    return manager, processes, launches


def test_native_srb_fresh_preparation_preserves_committed_active_and_peer_plan(tmp_path):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, processes, _ = native_generation_manager(cfg)
    result = manager.apply(flight)
    record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    record = record_path.read_bytes()
    active = Path(json.loads(record)["executable"])
    (active.parent / "active-kernel.bin").write_bytes(b"active generated bytes")
    plans = [manager._prepare_single(flight), manager._prepare_single(flight)]
    try:
        assert len({active, *(plan["executable"] for plan in plans)}) == 3
        assert record_path.read_bytes() == record
        assert (active.parent / "active-kernel.bin").read_bytes() == b"active generated bytes"
        assert processes[result["pid"]].running and not processes[result["pid"]].terminated
        manager._release_native_srb_plan(plans[0])
        assert active.exists() and plans[1]["executable"].exists()
    finally:
        for plan in plans:
            manager._release_native_srb_plan(plan)


def test_native_srb_legacy_bad_controls_reclaiming_and_unknown_system_do_not_block_new_plan(tmp_path):
    cfg, _, flight = native_srb_fixture(tmp_path, system_name="Windows")
    role_root = cfg.miner_packages_dir / "rigpilot-native-srb-runtime-v2/single"
    saved = {}
    for name in ("slot-0", "slot-1", "reclaiming"):
        old = role_root / name
        old.mkdir(parents=True)
        (old / "control.json").write_bytes(b"{}")
        (old / "keep").write_bytes(name.encode())
        saved[old] = ((old / "control.json").read_bytes(), (old / "keep").read_bytes())
    def unknown_inventory():
        pytest.fail("fresh preparation must not enumerate unrelated system processes")
    manager = ManagedMinerManager(cfg, system_name="Windows", process_iter_factory=unknown_inventory,
                                  port_allocator=lambda: 24567, device_runner=completed_device_list())
    plans = []
    try:
        for _ in range(3):
            plans.append(manager._prepare_single(flight))
        assert len({plan["executable"] for plan in plans}) == len(plans)
        for old, bytes_before in saved.items():
            assert ((old / "control.json").read_bytes(), (old / "keep").read_bytes()) == bytes_before
    finally:
        for plan in plans:
            manager._release_native_srb_plan(plan)


@pytest.mark.parametrize("proof", [False, True])
def test_native_srb_prepared_primary_secondary_probe_failure_requires_explicit_closed_proof(tmp_path, proof):
    cfg, _, flight = native_srb_fixture(tmp_path)
    flight.update(version=4, secondary_mode="independent", secondary=dict(flight["primary"]))
    calls = []
    def probe(argv, **kwargs):
        calls.append(Path(kwargs["cwd"]))
        result = completed_device_list()(argv, **kwargs)
        result.owned_direct_child_closed = proof
        if len(calls) == 2:
            result.returncode = 1
        return result
    ports = iter((24567, 24568))
    manager = ManagedMinerManager(cfg, system_name="Linux", process_iter_factory=lambda: [],
                                  port_allocator=lambda: next(ports), device_runner=probe)
    with pytest.raises(ManagedMinerError, match="GPU discovery failed"):
        manager.apply(flight)
    assert len(calls) == 2
    for bundle in calls:
        assert bundle.exists() is (not proof)
    assert manager._native_srb_registry_key() not in managed_miners_module._NATIVE_SRB_PREPARED


@pytest.mark.parametrize("proof", [False, True])
def test_native_srb_launch_failure_retains_run_without_positive_owned_close(tmp_path, monkeypatch, proof):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, _, _ = native_generation_manager(cfg)
    prepared = manager._prepare_single(flight)
    executable = prepared["executable"]
    def failed_launch(*args, **kwargs):
        error = ManagedMinerError("injected launch failure")
        error.owned_launch_closed = proof
        raise error
    monkeypatch.setattr(manager, "_launch", failed_launch)
    try:
        with pytest.raises(ManagedMinerError, match="injected launch failure"):
            manager._apply_single(flight, prepared=prepared, stop_previous=True)
    finally:
        manager._release_native_srb_plan(prepared)
    assert executable.exists() is (not proof)


def test_native_srb_normal_stop_cleans_committed_generation_without_accumulation(tmp_path):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, processes, _ = native_generation_manager(cfg)
    role_root = cfg.miner_packages_dir / "rigpilot-native-srb-runtime-v2/single"
    for _ in range(3):
        result = manager.apply(flight)
        record = json.loads((cfg.managed_miner_runtime_dir / "managed-miner-process.json").read_text())
        executable = Path(record["executable"])
        assert executable.exists()
        assert manager.stop()["stopped"] is True
        assert processes[result["pid"]].terminated
        assert not executable.parent.parent.exists()
        assert list(role_root.iterdir()) == []


@pytest.mark.parametrize("closure", ["stop", "absence"])
def test_native_srb_confirmed_stop_or_absence_closes_another_session_generation(tmp_path, closure):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, processes, _ = native_generation_manager(cfg)
    result = manager.apply(flight)
    record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    executable = Path(json.loads(record_path.read_text())["executable"])
    control_path = executable.parent.parent / "control.json"
    control = json.loads(control_path.read_text())
    control["owner"] = "f" * 32 if control["owner"] != "f" * 32 else "e" * 32
    control_path.write_text(json.dumps(control))
    if closure == "absence":
        processes[result["pid"]].running = False
    outcome = manager.stop()
    # This boolean reports an actual live process stop, not closure authority.
    assert outcome["stopped"] is (closure == "stop")
    assert not record_path.exists() and not executable.parent.parent.exists()


def test_native_srb_valid_cross_session_preboot_record_cleans_only_bound_generation_without_pid_inspection(tmp_path):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, processes, _ = native_generation_manager(cfg)
    result = manager.apply(flight)
    record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(record_path.read_text())
    old_executable = Path(record["executable"])
    control_path = old_executable.parent.parent / "control.json"
    control = json.loads(control_path.read_text())
    control["owner"] = "f" * 32 if control["owner"] != "f" * 32 else "e" * 32
    control_path.write_text(json.dumps(control))
    peer = manager._prepare_single(flight)
    manager.boot_time_factory = lambda: record["create_time"] + 100
    manager.process_factory = lambda _pid: pytest.fail("a preboot reused PID must not be inspected or killed")
    manager.process_iter_factory = lambda: pytest.fail("trusted preboot record cleanup needs no current PID scan")
    try:
        outcome = manager.stop()
        assert outcome["stopped"] is False and not record_path.exists()
        assert not old_executable.parent.parent.exists()
        assert peer["executable"].exists()
        assert not processes[result["pid"]].terminated and not processes[result["pid"]].killed
    finally:
        manager._release_native_srb_plan(peer)


@pytest.mark.parametrize("boot_time", [None, float("nan"), 0.0, -1.0], ids=["unknown", "nan", "zero", "negative"])
def test_native_srb_unknown_boot_and_missing_pid_do_not_prove_generation_closed(tmp_path, boot_time):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, _, _ = native_generation_manager(cfg)
    manager.apply(flight)
    record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    before = record_path.read_bytes()
    executable = Path(json.loads(before)["executable"])
    manager.boot_time_factory = lambda: boot_time
    manager.process_factory = lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid))
    manager.process_iter_factory = lambda: (_ for _ in ()).throw(psutil.AccessDenied(1))
    with pytest.raises(ManagedMinerError):
        manager.stop()
    assert record_path.read_bytes() == before and executable.exists()


@pytest.mark.parametrize("guard", ["bad-control", "other-current-record", "current-prepared-lease"])
def test_native_srb_preboot_cleanup_preserves_bad_or_currently_bound_generation(tmp_path, guard):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, _, _ = native_generation_manager(cfg)
    held_plan = None
    if guard == "current-prepared-lease":
        held_plan = manager._prepare_single(flight)
        executable = held_plan["executable"]
        record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
        write_process_record(record_path, executable, pid=101, created=1000.0, miner_type="srbminer")
        upgrade_process_record_to_v2(record_path, held_plan["argv"])
    else:
        manager.apply(flight)
        record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
        executable = Path(json.loads(record_path.read_text())["executable"])
    control_path = executable.parent.parent / "control.json"
    protected_record = None
    if guard == "bad-control":
        control = json.loads(control_path.read_text())
        control["runtime"] = str(tmp_path / "unrelated-runtime")
        control_path.write_text(json.dumps(control))
    elif guard == "other-current-record":
        protected_record = cfg.managed_miner_runtime_dir / "primary/managed-miner-process.json"
        write_process_record(protected_record, executable, pid=999, created=5000.0, miner_type="srbminer")
        protected_before = protected_record.read_bytes()
    control_before = control_path.read_bytes()
    manager.boot_time_factory = lambda: 4000.0
    manager.process_factory = lambda _pid: pytest.fail("preboot cleanup must not inspect a reused PID")
    manager.process_iter_factory = lambda: pytest.fail("preboot cleanup must not enumerate current processes")
    try:
        assert manager._verified_previous(cfg.managed_miner_runtime_dir) is None
        assert not record_path.exists()
        assert executable.exists() and control_path.read_bytes() == control_before
        if protected_record is not None:
            assert protected_record.read_bytes() == protected_before
    finally:
        manager._release_native_srb_plan(held_plan)


def test_native_srb_uninspectable_record_never_disposes_committed_run(tmp_path):
    cfg, _, flight = native_srb_fixture(tmp_path)
    manager, _, _ = native_generation_manager(cfg)
    manager.apply(flight)
    record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    record = record_path.read_bytes()
    executable = Path(json.loads(record)["executable"])
    manager.process_factory = lambda pid: (_ for _ in ()).throw(psutil.AccessDenied(pid))
    with pytest.raises(ManagedMinerError, match="inspect|verif"):
        manager.stop()
    assert executable.exists() and record_path.read_bytes() == record


def test_manager_fails_closed_on_reused_pid_without_starting(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    unrelated = tmp_path / "unrelated.exe"
    unrelated.write_bytes(b"other")
    old = FakeProcess(101, unrelated, 1000.25)
    popen_calls = []
    write_process_record(
        cfg.managed_miner_runtime_dir / "managed-miner-process.json",
        executable,
        pid=101,
        created=1000.25,
    )
    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        process_factory=lambda pid: old,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="belongs to another path"):
        manager.apply(document(package))

    assert old.terminated is False
    assert popen_calls == []
    status = json.loads(cfg.miner_status_file.read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    assert "48A-public-wallet" not in json.dumps(status)


def test_manager_stop_uses_recorded_identity_and_writes_stopped_status(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    process = FakeProcess(101, executable, 1000.25)
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    write_process_record(state_path, executable, pid=101, created=1000.25)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: process,
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is True
    assert result["miner_type"] == "xmrig"
    assert result["flight_sheet_id"] == "old-sheet"
    assert process.terminated is True
    assert not state_path.exists()
    status = json.loads(cfg.miner_status_file.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"
    assert status["pid"] is None
    assert status["api_port"] is None


def test_manager_stop_rebinds_unique_watchdog_restart_before_next_heartbeat(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "managed-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    replacement = FakeProcess(303, executable.resolve(), 1100.5, argv)

    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: [replacement],
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is True
    assert replacement.terminated is True
    assert not state_path.exists()


def test_manager_stop_rebinds_watchdog_replacement_from_same_time_bucket(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    package.mkdir()
    executable = package / "SRBMiner-MULTI.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [
        str(executable.resolve()),
        "--algorithm",
        "pearlhash",
        "--api-port",
        "24567",
    ]
    write_process_record(
        state_path,
        executable,
        pid=101,
        created=1000.25,
        miner_type="srbminer",
    )
    upgrade_process_record_to_v2(state_path, argv)
    replacement = FakeProcess(303, executable.resolve(), 1000.30, argv)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: [replacement],
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is True
    assert replacement.terminated is True
    assert not state_path.exists()


def test_manager_stop_rebinds_same_pid_after_creation_time_clock_shift(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "managed-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    shifted = FakeProcess(101, executable.resolve(), 1000.5, argv)

    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: shifted,
        process_iter_factory=lambda: [shifted],
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is True
    assert shifted.terminated is True
    assert not state_path.exists()


def test_manager_stop_rejects_reused_same_pid_outside_creation_tolerance(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "managed-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    reused = FakeProcess(101, executable.resolve(), 1100.5, argv)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: reused,
        process_iter_factory=lambda: [reused],
        boot_time_factory=lambda: 0.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="creation time no longer matches"):
        manager.stop()

    assert reused.terminated is False
    assert state_path.exists()


def test_manager_restart_rebinds_and_stops_watchdog_process_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    old_argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, old_argv)
    replacement = FakeProcess(303, executable.resolve(), 1100.5, old_argv)
    launched = FakeProcess(202, executable.resolve(), 2000.5)
    stored = document(package, extra_config={"cpu_threads": 4})
    stored["gpu_oc"] = None
    stored["applied_at"] = "2026-07-15T00:00:00+00:00"
    cfg.flight_sheet_file.write_text(json.dumps(stored), encoding="utf-8")

    def process_factory(pid: int):
        if pid == 101:
            raise psutil.NoSuchProcess(pid)
        if pid == 303:
            return replacement
        assert pid == 202
        return launched

    scans = iter(([], [replacement]))
    clock = iter((0.0, 0.0))
    monkeypatch.setattr(
        managed_miners_module.time,
        "monotonic",
        lambda: next(clock),
    )
    monkeypatch.setattr(
        managed_miners_module.time,
        "sleep",
        lambda _seconds: None,
    )
    monkeypatch.setattr(
        ManagedMinerManager,
        "_loopback_api_is_listening",
        staticmethod(lambda _port: False),
    )

    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda argv, **kwargs: FakePopen(),
        process_factory=process_factory,
        process_iter_factory=lambda: next(scans, [replacement]),
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    result = manager.restart()

    assert result["restarted"] is True
    assert result["replaced_previous"] is True
    assert result["pid"] == 202
    assert replacement.terminated is True


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
@pytest.mark.parametrize("recorded_oc", [False, True])
def test_manager_restart_injects_srb_reset_only_for_recorded_cli_oc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
    recorded_oc: bool,
):
    cfg = replace(managed_config(tmp_path), allow_gpu_oc=True)
    package = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    package.mkdir()
    active = document(
        package,
        miner_type="srbminer",
        algorithm="pearlhash",
    )
    active["primary"]["coin"] = "PRL"
    active["gpu_oc"] = ({"vendor": "nvidia", "settings": [
        {"index": 0, "power_limit_w": 200.0},
    ]} if recorded_oc else None)
    active["applied_at"] = "2026-07-30T00:00:00+00:00"
    cfg.flight_sheet_file.write_text(json.dumps(active), encoding="utf-8")
    manager = ManagedMinerManager(cfg, system_name=system_name)
    launched: list[dict] = []
    monkeypatch.setattr(
        manager,
        "apply",
        lambda value: launched.append(json.loads(json.dumps(value)))
        or {"managed": True},
    )

    result = manager.restart()

    assert result["restarted"] is True
    assert (launched[0].get("_reset_srb_gpu_oc") is True) is recorded_oc
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert "_reset_srb_gpu_oc" not in stored


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_independent_no_oc_restart_leaves_reset_unset_and_routes_explicit_flag_only_to_gpu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system_name: str,
):
    cfg = replace(managed_config(tmp_path), allow_gpu_oc=True)
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    srb_root = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    xmrig_root.mkdir()
    srb_root.mkdir()
    active = document(xmrig_root)
    active.update(
        {
            "version": 4,
            "secondary_mode": "independent",
            "secondary": leg(
                srb_root,
                miner_type="srbminer",
                algorithm="pearlhash",
            ),
            "gpu_oc": None,
            "applied_at": "2026-07-30T00:00:00+00:00",
        }
    )
    active["secondary"]["coin"] = "PRL"
    cfg.flight_sheet_file.write_text(json.dumps(active), encoding="utf-8")
    manager = ManagedMinerManager(cfg, system_name=system_name)
    launched: list[dict] = []
    monkeypatch.setattr(
        manager,
        "apply",
        lambda value: launched.append(json.loads(json.dumps(value)))
        or {"managed": True},
    )

    manager.restart()

    assert "_reset_srb_gpu_oc" not in launched[0]
    primary = manager._role_document(launched[0], "primary")
    secondary = manager._role_document(launched[0], "secondary")
    assert "_reset_srb_gpu_oc" not in primary
    assert "_reset_srb_gpu_oc" not in secondary
    explicit_reset = {**launched[0], "_reset_srb_gpu_oc": True}
    primary = manager._role_document(explicit_reset, "primary")
    secondary = manager._role_document(explicit_reset, "secondary")
    assert "_reset_srb_gpu_oc" not in primary
    assert secondary["_reset_srb_gpu_oc"] is True


@pytest.mark.parametrize("record_version", [1, 2])
def test_manager_restart_keeps_unverifiable_stale_proof_and_refuses_duplicate(
    tmp_path: Path, record_version: int
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    candidates = [
        FakeProcess(
            303,
            executable.resolve(),
            1100.5,
            [str(executable.resolve()), "--config", "different.json"],
        )
    ]
    if record_version == 2:
        upgrade_process_record_to_v2(state_path, expected_argv)
    stored = document(package, extra_config={"cpu_threads": 4})
    stored["gpu_oc"] = None
    stored["applied_at"] = "2026-07-15T00:00:00+00:00"
    cfg.flight_sheet_file.write_text(json.dumps(stored), encoding="utf-8")
    popen_calls = []
    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: candidates,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="refusing to start a duplicate"):
        manager.restart()

    assert state_path.exists()
    assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == 101
    assert popen_calls == []


def test_manager_apply_after_host_reboot_discards_preboot_record_without_pid_lookup(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(
        state_path,
        [str(executable.resolve()), "--config", "old-config.json"],
    )
    launched = FakeProcess(202, executable.resolve(), 2000.5)
    looked_up: list[int] = []

    def process_factory(pid: int):
        looked_up.append(pid)
        if pid == 101:
            raise AssertionError("a pre-reboot PID must never be inspected")
        assert pid == 202
        return launched

    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda argv, **kwargs: FakePopen(),
        process_factory=process_factory,
        boot_time_factory=lambda: 1500.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    result = manager.apply(document(package, extra_config={"cpu_threads": 4}))

    assert result["pid"] == 202
    assert result["replaced_previous"] is False
    assert looked_up == [202]
    current = json.loads(state_path.read_text(encoding="utf-8"))
    assert current["pid"] == 202
    assert current["create_time"] == 2000.5


@pytest.mark.parametrize("record_version", [1, 2])
def test_manager_same_boot_missing_process_is_reconciled_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_version: int,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    if record_version == 2:
        upgrade_process_record_to_v2(state_path, argv)
    launched = FakeProcess(202, executable.resolve(), 2000.5)
    looked_up: list[int] = []

    def process_factory(pid: int):
        looked_up.append(pid)
        if pid == 101:
            raise psutil.NoSuchProcess(pid)
        assert pid == 202
        return launched

    popen_calls = []

    def popen_factory(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return FakePopen()

    monkeypatch.setattr(
        managed_miners_module,
        "_ORPHAN_CONFIRMATION_SECONDS",
        0.01,
    )
    manager = ManagedMinerManager(
        cfg,
        popen_factory=popen_factory,
        process_factory=process_factory,
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 900.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    result = manager.apply(document(package, extra_config={"cpu_threads": 4}))

    assert state_path.exists()
    assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == 202
    assert result["pid"] == 202
    assert result["replaced_previous"] is False
    assert looked_up[0] == 101
    assert looked_up[-1] == 202
    assert len(popen_calls) == 1


def test_manager_apply_refuses_orphan_cleanup_while_recorded_api_is_listening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    popen_calls = []
    monkeypatch.setattr(
        ManagedMinerManager,
        "_loopback_api_is_listening",
        staticmethod(lambda _port: True),
    )
    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 900.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="API is still listening"):
        manager.apply(document(package, extra_config={"cpu_threads": 4}))

    assert state_path.exists()
    assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == 101
    assert popen_calls == []


def test_manager_stop_clears_proven_absent_same_boot_record(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is False
    assert result["miner_type"] == "xmrig"
    assert not state_path.exists()
    status = json.loads(cfg.miner_status_file.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"
    assert status["error"] is None


def test_manager_remove_sheet_clears_proven_absent_same_boot_record(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    stored = document(package, extra_config={"cpu_threads": 4})
    stored["gpu_oc"] = None
    stored["applied_at"] = "2026-07-15T00:00:00+00:00"
    cfg.flight_sheet_file.write_text(json.dumps(stored), encoding="utf-8")
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    result = manager.remove_flight_sheet()

    assert result["configuration_cleared"] is True
    assert result["miner_stopped"] is False
    assert not state_path.exists()
    assert not cfg.flight_sheet_file.exists()
    assert not cfg.miner_status_file.exists()


def test_manager_remove_sheet_after_exact_stop_deletes_corrupt_safe_active_json(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    runtime = cfg.managed_miner_runtime_dir
    state_path = runtime / "managed-miner-process.json"
    write_process_record(state_path, executable, pid=101, created=1000.25)
    process = FakeProcess(101, executable.resolve(), 1000.25)
    cfg.flight_sheet_file.write_text("{corrupt-json", encoding="utf-8")
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: process,
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    stopped = manager.stop()
    removed = manager.remove_flight_sheet()

    assert stopped["stopped"] is True
    assert process.terminated is True
    assert not state_path.exists()
    assert removed["configuration_cleared"] is True
    assert removed["miner_stopped"] is False
    assert not cfg.flight_sheet_file.exists()
    assert not cfg.miner_status_file.exists()


def test_manager_stop_preserves_xmrig_configs_and_remove_prunes_all_roles(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    primary_runtime = runtime / "primary"
    primary_runtime.mkdir(parents=True)
    legacy_config = runtime / "xmrig-config.json"
    content_config = runtime / f"xmrig-config-{'a' * 64}.json"
    role_config = primary_runtime / f"xmrig-config-{'b' * 64}.json"
    unrelated = runtime / "operator-note.json"
    for path in (legacy_config, content_config, role_config, unrelated):
        path.write_text("{}", encoding="utf-8")
    manager = ManagedMinerManager(cfg, system_name="Windows")

    manager.stop()

    assert legacy_config.exists()
    assert content_config.exists()
    assert role_config.exists()
    result = manager.remove_flight_sheet()

    assert result["configuration_cleared"] is True
    assert not legacy_config.exists()
    assert not content_config.exists()
    assert not role_config.exists()
    assert unrelated.exists()


def test_manager_remove_rejects_xmrig_config_link_before_stopping(
    tmp_path: Path,
):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir(parents=True)
    state_path = runtime / "managed-miner-process.json"
    write_process_record(state_path, executable, pid=101, created=1000.25)
    process = FakeProcess(101, executable.resolve(), 1000.25)
    target = tmp_path / "outside-config.json"
    target.write_text("{}", encoding="utf-8")
    config_link = runtime / f"xmrig-config-{'c' * 64}.json"
    try:
        config_link.symlink_to(target)
    except OSError:
        pytest.skip("creating a file symlink is not permitted")
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: process,
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="not a safe regular file"):
        manager.remove_flight_sheet()

    assert process.terminated is False
    assert state_path.exists()
    assert target.exists()


def test_manager_stop_keeps_ambiguous_same_name_process_record(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, expected_argv)
    different = FakeProcess(
        303,
        executable.resolve(),
        1100.5,
        [str(executable.resolve()), "--config", "different.json"],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: [different],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="refusing to start a duplicate"):
        manager.stop()

    assert state_path.exists()
    assert different.terminated is False


def test_manager_stop_treats_zombie_only_same_name_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    zombie = ZombieWatchdogProcess(
        303,
        executable.resolve(),
        1100.5,
        argv,
    )
    monkeypatch.setattr(
        managed_miners_module,
        "_ORPHAN_CONFIRMATION_SECONDS",
        0.0,
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [zombie],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is False
    assert not state_path.exists()
    assert zombie.terminated is False


def test_manager_stop_ignores_zombie_and_rebinds_one_live_exact_process(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    zombie = ZombieWatchdogProcess(
        303,
        executable.resolve(),
        1100.5,
        argv,
    )
    replacement = FakeProcess(
        404,
        executable.resolve(),
        1100.5,
        argv,
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [zombie, replacement],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is True
    assert replacement.terminated is True
    assert zombie.terminated is False
    assert not state_path.exists()


def test_manager_stop_still_rejects_two_live_exact_processes_beside_zombie(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    candidates = [
        ZombieWatchdogProcess(303, executable.resolve(), 1100.5, argv),
        FakeProcess(404, executable.resolve(), 1100.5, argv),
        FakeProcess(505, executable.resolve(), 1100.5, argv),
    ]
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: candidates,
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="refusing to start a duplicate"):
        manager.stop()

    assert state_path.exists()
    assert not any(process.terminated for process in candidates)


def test_manager_stop_does_not_delete_concurrently_rebound_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)

    def concurrent_rebind(path: Path, expected: dict) -> bool:
        updated = {**expected, "pid": 303, "create_time": 1100.5}
        path.write_text(json.dumps(updated), encoding="utf-8")
        return False

    monkeypatch.setattr(
        managed_miners_module,
        "remove_managed_process_record_if_unchanged",
        concurrent_rebind,
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="changed during orphan cleanup"):
        manager.stop()

    assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == 303


def test_manager_stop_follows_concurrently_rebound_watchdog_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    original = FakeProcess(101, executable, 1000.25, argv)
    replacement = FakeProcess(303, executable, 1100.5, argv)
    real_remove = managed_miners_module.remove_managed_process_record_if_unchanged
    calls = 0

    def rebind_once(path: Path, expected: dict) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            updated = {**expected, "pid": 303, "create_time": 1100.5}
            path.write_text(json.dumps(updated), encoding="utf-8")
            return False
        return real_remove(path, expected)

    monkeypatch.setattr(
        managed_miners_module,
        "remove_managed_process_record_if_unchanged",
        rebind_once,
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory={101: original, 303: replacement}.__getitem__,
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is True
    assert original.terminated is True
    assert replacement.terminated is True
    assert calls == 2
    assert not state_path.exists()


def test_manager_creation_time_mismatch_never_stops_reused_pid_after_absence_proof(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    reused = FakeProcess(
        101,
        executable.resolve(),
        2000.5,
        [str(executable.resolve()), "--config", "unrelated.json"],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda _pid: reused,
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    result = manager.stop()

    assert result["stopped"] is False
    assert reused.terminated is False
    assert not state_path.exists()


def test_residue_scan_and_cleanup_stop_only_verified_v2_process(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "managed-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    process = FakeProcess(101, executable.resolve(), 1000.25, argv)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda _pid: process,
        process_iter_factory=lambda: [process],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    scan = manager.scan_residue()

    assert scan["cleanup_available"] is True
    assert scan["clearable_count"] == 1
    assert scan["ambiguous_count"] == 0
    assert len(scan["scan_fingerprint"]) == 64
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    assert legacy == {
        "role": "legacy",
        "status": "verified_process",
        "miner_type": "xmrig",
        "flight_sheet_id": "old-sheet",
        "record_pid": 101,
        "verified_pid": 101,
        "process_verified": True,
        "clearable": True,
        "reason": "recorded PID identity is verified",
        "manual_candidates": [],
        "target_fingerprint": legacy["target_fingerprint"],
    }
    assert re.fullmatch(r"[0-9a-f]{64}", legacy["target_fingerprint"])
    assert process.terminated is False
    assert state_path.exists()

    cleanup = manager.cleanup_residue(scan["scan_fingerprint"])

    assert cleanup["blocked"] is False
    assert cleanup["processes_stopped"] == 1
    assert cleanup["records_removed"] == 1
    assert cleanup["results"] == [
        {
            "role": "legacy",
            "status": "verified_process_stopped",
            "process_stopped": True,
            "record_removed": True,
            "reason": None,
        }
    ]
    assert process.terminated is True
    assert not state_path.exists()
    status = json.loads(cfg.miner_status_file.read_text(encoding="utf-8"))
    assert status["state"] == "stopped"


@pytest.mark.parametrize("same_pid", [False, True], ids=["replacement", "same-pid-reexec"])
def test_residue_scan_blocks_same_name_process_with_different_argv(
    tmp_path: Path, same_pid: bool,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, expected_argv)
    different = FakeProcess(
        101 if same_pid else 303,
        executable.resolve(),
        1000.25 if same_pid else 1100.5,
        [str(executable.resolve()), "--config", "different.json"],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: different if same_pid else (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [different],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError):
        manager.running()
    with pytest.raises(ManagedMinerError):
        manager.stop()
    assert not different.terminated
    scan = manager.scan_residue()
    cleanup = manager.cleanup_residue(scan["scan_fingerprint"])

    assert scan["cleanup_available"] is True
    assert scan["clearable_count"] == 0
    assert scan["ambiguous_count"] == 1
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    assert legacy["manual_candidates"] == [
        {
            "pid": different.pid,
            "candidate_fingerprint": legacy["manual_candidates"][0][
                "candidate_fingerprint"
            ],
        }
    ]
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        legacy["manual_candidates"][0]["candidate_fingerprint"],
    )
    assert "different.json" not in json.dumps(scan)
    assert cleanup["blocked"] is True
    assert cleanup["changed"] is False
    assert different.terminated is False
    assert state_path.exists()


def test_explicit_residue_cleanup_stops_unique_snapshot_bound_candidate(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-pearlminer-test-2.0"
    package.mkdir()
    executable = package / "miner-cuda13"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--pool", "secret.example"]
    write_process_record(
        state_path,
        executable,
        pid=101,
        created=1000.25,
        miner_type="pearlminer",
    )
    upgrade_process_record_to_v2(state_path, expected_argv)
    candidate = FakeProcess(
        303,
        executable.resolve(),
        1100.5,
        [str(executable.resolve()), ""],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [candidate],
        boot_time_factory=lambda: 900.0,
        system_name="Linux",
    )

    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    manual = legacy["manual_candidates"][0]
    cleanup = manager.cleanup_residue(
        scan["scan_fingerprint"],
        [
            {
                "role": "legacy",
                "target_fingerprint": legacy["target_fingerprint"],
                "candidate_fingerprint": manual["candidate_fingerprint"],
            }
        ],
    )

    assert cleanup["blocked"] is False
    assert cleanup["processes_stopped"] == 1
    assert cleanup["records_removed"] == 1
    assert cleanup["selected_targets"] == [
        {
            "role": "legacy",
            "target_fingerprint": legacy["target_fingerprint"],
            "candidate_fingerprint": manual["candidate_fingerprint"],
        }
    ]
    assert cleanup["results"] == [
        {
            "role": "legacy",
            "status": "explicit_candidate_stopped",
            "process_stopped": True,
            "record_removed": True,
            "reason": None,
        }
    ]
    assert candidate.terminated is True
    assert not state_path.exists()


def test_explicit_residue_cleanup_rejects_changed_candidate_snapshot(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, expected_argv)
    candidate = FakeProcess(
        303,
        executable.resolve(),
        1100.5,
        [str(executable.resolve()), "--config", "different.json"],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [candidate],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )
    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    manual = legacy["manual_candidates"][0]
    candidate.argv[-1] = "changed-again.json"

    with pytest.raises(ManagedMinerError, match="changed after scan"):
        manager.cleanup_residue(
            scan["scan_fingerprint"],
            [
                {
                    "role": "legacy",
                    "target_fingerprint": legacy["target_fingerprint"],
                    "candidate_fingerprint": manual["candidate_fingerprint"],
                }
            ],
        )
    assert candidate.terminated is False
    assert state_path.exists()


def test_explicit_residue_candidate_requires_unique_live_same_name_process(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, expected_argv)
    candidates = [
        FakeProcess(
            pid,
            executable.resolve(),
            created,
            [str(executable.resolve()), "--config", config_name],
        )
        for pid, created, config_name in (
            (303, 1100.5, "first.json"),
            (404, 1101.0, "second.json"),
        )
    ]
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: candidates,
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")

    assert legacy["status"] == "ambiguous"
    assert legacy["manual_candidates"] == []


def test_explicit_residue_candidate_rejects_wrong_path(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, expected_argv)

    wrong_path = FakeProcess(
        303,
        tmp_path / "outside" / "xmrig.exe",
        1100.5,
        [str(tmp_path / "outside" / "xmrig.exe"), "--config", "other.json"],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [wrong_path],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )
    scan = manager.scan_residue()
    legacy = next(
        item for item in scan["findings"] if item["role"] == "legacy"
    )
    assert legacy["status"] == "ambiguous"
    assert legacy["manual_candidates"] == []


def test_explicit_residue_cleanup_allows_selected_reused_pid_snapshot(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, expected_argv)
    reused_pid = FakeProcess(
        101,
        executable.resolve(),
        1100.5,
        [str(executable.resolve()), "--config", "other.json"],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [reused_pid] if reused_pid.running else [],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    scan = manager.scan_residue()
    legacy = next(
        item for item in scan["findings"] if item["role"] == "legacy"
    )
    manual = legacy["manual_candidates"][0]
    cleanup = manager.cleanup_residue(
        scan["scan_fingerprint"],
        [
            {
                "role": "legacy",
                "target_fingerprint": legacy["target_fingerprint"],
                "candidate_fingerprint": manual["candidate_fingerprint"],
            }
        ],
    )

    assert legacy["status"] == "ambiguous"
    assert manual["pid"] == 101
    assert cleanup["blocked"] is False
    assert cleanup["results"][0]["status"] == "explicit_candidate_stopped"
    assert reused_pid.terminated is True
    assert not state_path.exists()


def test_explicit_residue_cleanup_keeps_record_when_candidate_respawns(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, expected_argv)
    candidate = FakeProcess(
        303,
        executable.resolve(),
        1100.5,
        [str(executable.resolve()), "--config", "different.json"],
    )
    replacement = FakeProcess(
        404,
        executable.resolve(),
        1101.0,
        [str(executable.resolve()), "--config", "different.json"],
    )

    def current_processes():
        return [candidate] if candidate.running else [replacement]

    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=current_processes,
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )
    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    manual = legacy["manual_candidates"][0]

    cleanup = manager.cleanup_residue(
        scan["scan_fingerprint"],
        [
            {
                "role": "legacy",
                "target_fingerprint": legacy["target_fingerprint"],
                "candidate_fingerprint": manual["candidate_fingerprint"],
            }
        ],
    )

    assert cleanup["blocked"] is True
    assert cleanup["processes_stopped"] == 1
    assert cleanup["records_removed"] == 0
    assert candidate.terminated is True
    assert replacement.terminated is False
    assert state_path.exists()


def test_explicit_residue_cleanup_stops_one_trusted_pearl_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-pearlminer-test-2.0"
    package.mkdir()
    executable = package / "miner-cuda13"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--pool", "secret.example"]
    write_process_record(
        state_path,
        executable,
        pid=589,
        created=1000.25,
        miner_type="pearlminer",
    )
    upgrade_process_record_to_v2(state_path, expected_argv)
    leader = FakeProcess(
        589, executable.resolve(), 1000.25, [str(executable.resolve()), ""]
    )
    child = FakeProcess(
        617, executable.resolve(), 1001.0, [str(executable.resolve()), ""]
    )
    identities = {
        589: (1, 589, 589),
        617: (589, 589, 589),
    }
    monkeypatch.setattr(
        "agent.process_identity._native_process_group_identity",
        lambda _process, pid: identities[pid],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [leader, child],
        boot_time_factory=lambda: 900.0,
        system_name="Linux",
    )

    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    manual = legacy["manual_candidates"][0]
    assert manual["pid"] == 589

    cleanup = manager.cleanup_residue(
        scan["scan_fingerprint"],
        [
            {
                "role": "legacy",
                "target_fingerprint": legacy["target_fingerprint"],
                "candidate_fingerprint": manual["candidate_fingerprint"],
            }
        ],
    )

    assert cleanup["blocked"] is False
    assert cleanup["results"][0]["status"] == "explicit_candidate_stopped"
    assert leader.terminated is True
    assert child.terminated is True
    assert not state_path.exists()


@pytest.mark.parametrize(
    "identities",
    [
        {589: (1, 589, 589), 617: (589, 617, 617)},
        {589: (1, 589, 589), 617: (1, 589, 589)},
        {589: (1, 589, 589), 617: None},
    ],
)
def test_explicit_residue_group_rejects_cross_group_non_descendant_or_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identities,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-pearlminer-test-2.0"
    package.mkdir()
    executable = package / "miner-cuda13"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable.resolve()), "--pool", "secret.example"]
    write_process_record(
        state_path,
        executable,
        pid=589,
        created=1000.25,
        miner_type="pearlminer",
    )
    upgrade_process_record_to_v2(state_path, expected_argv)
    processes = [
        FakeProcess(
            589, executable.resolve(), 1000.25, [str(executable.resolve()), ""]
        ),
        FakeProcess(
            617, executable.resolve(), 1001.0, [str(executable.resolve()), ""]
        ),
    ]
    monkeypatch.setattr(
        "agent.process_identity._native_process_group_identity",
        lambda _process, pid: identities[pid],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: processes,
        boot_time_factory=lambda: 900.0,
        system_name="Linux",
    )

    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")

    assert legacy["status"] == "ambiguous"
    assert legacy["manual_candidates"] == []


def test_selective_residue_cleanup_skips_ambiguous_role_and_cleans_only_selected(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    legacy_state = runtime / "managed-miner-process.json"
    primary_state = runtime / "primary" / "managed-miner-process.json"
    legacy_argv = [str(executable.resolve()), "--config", "legacy.json"]
    primary_argv = [str(executable.resolve()), "--config", "primary.json"]
    write_process_record(
        legacy_state, executable, pid=101, created=1000.25
    )
    upgrade_process_record_to_v2(legacy_state, legacy_argv)
    write_process_record(
        primary_state, executable, pid=202, created=1000.5
    )
    upgrade_process_record_to_v2(primary_state, primary_argv)
    legacy_process = FakeProcess(
        101, executable.resolve(), 1000.25, legacy_argv
    )
    ambiguous = FakeProcess(
        303,
        executable.resolve(),
        1100.5,
        [str(executable.resolve()), "--config", "unrelated.json"],
    )

    def process_factory(pid):
        if pid == 101:
            return legacy_process
        raise psutil.NoSuchProcess(pid)

    manager = ManagedMinerManager(
        cfg,
        process_factory=process_factory,
        process_iter_factory=lambda: [legacy_process, ambiguous],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )
    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    primary = next(item for item in scan["findings"] if item["role"] == "primary")

    assert scan["schema_version"] == 3
    assert scan["ambiguous_count"] == 1
    assert primary["status"] == "ambiguous"
    cleanup = manager.cleanup_residue(
        scan["scan_fingerprint"],
        [
            {
                "role": "legacy",
                "target_fingerprint": legacy["target_fingerprint"],
            }
        ],
    )

    assert cleanup["blocked"] is False
    assert cleanup["selected_targets"] == [
        {
            "role": "legacy",
            "target_fingerprint": legacy["target_fingerprint"],
        }
    ]
    assert legacy_process.terminated is True
    assert not legacy_state.exists()
    assert primary_state.exists()
    assert ambiguous.terminated is False


def test_selective_residue_cleanup_rejects_ambiguous_or_wrong_target_fingerprint(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "expected.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    different = FakeProcess(
        303,
        executable.resolve(),
        1100.5,
        [str(executable.resolve()), "--config", "different.json"],
    )
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [different],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )
    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")

    with pytest.raises(ManagedMinerError, match="not selectable"):
        manager.cleanup_residue(
            scan["scan_fingerprint"],
            [
                {
                    "role": "legacy",
                    "target_fingerprint": legacy["target_fingerprint"],
                }
            ],
        )
    with pytest.raises(ManagedMinerError, match="not selectable"):
        manager.cleanup_residue(
            scan["scan_fingerprint"],
            [{"role": "secondary", "target_fingerprint": "f" * 64}],
        )
    assert different.terminated is False
    assert state_path.exists()


def test_selective_residue_cleanup_preflights_every_target_before_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = ManagedMinerManager(
        managed_config(tmp_path),
        system_name="Windows",
    )
    runtime = tmp_path / "runtime"

    def probe(role: str, fingerprint: str, pid: int) -> dict:
        role_runtime = runtime / role
        return {
            "role": role,
            "runtime": role_runtime,
            "state_path": role_runtime / "managed-miner-process.json",
            "record": {
                "version": 2,
                "pid": pid,
                "create_time": 1000.0 + pid,
                "executable": str(tmp_path / f"{role}.exe"),
                "argv_sha256": "a" * 64,
                "miner_type": "xmrig",
                "flight_sheet_id": f"sheet-{role}",
            },
            "finding": {
                "role": role,
                "status": "verified_process",
                "miner_type": "xmrig",
                "flight_sheet_id": f"sheet-{role}",
                "record_pid": pid,
                "verified_pid": pid,
                "process_verified": True,
                "clearable": True,
                "reason": "recorded PID identity is verified",
            },
            "target_fingerprint": fingerprint,
        }

    legacy = probe("legacy", "b" * 64, 101)
    primary = probe("primary", "c" * 64, 202)
    report = {
        "schema_version": 2,
        "scan_fingerprint": "d" * 64,
        "cleanup_available": True,
        "clearable_count": 2,
        "ambiguous_count": 0,
        "findings": [
            {**legacy["finding"], "target_fingerprint": legacy["target_fingerprint"]},
            {**primary["finding"], "target_fingerprint": primary["target_fingerprint"]},
        ],
    }
    monkeypatch.setattr(
        manager,
        "_scan_residue_internal",
        lambda: {**report, "_probes": [legacy, primary]},
    )

    def preflight(role: str, _runtime: Path) -> dict:
        if role == "legacy":
            return legacy
        changed = dict(primary)
        changed["target_fingerprint"] = "e" * 64
        return changed

    stopped: list[str] = []
    monkeypatch.setattr(manager, "_probe_residue_target", preflight)
    monkeypatch.setattr(
        manager,
        "_stop_previous",
        lambda role_runtime, **_kwargs: stopped.append(str(role_runtime)) or True,
    )

    result = manager.cleanup_residue(
        report["scan_fingerprint"],
        [
            {"role": "legacy", "target_fingerprint": legacy["target_fingerprint"]},
            {"role": "primary", "target_fingerprint": primary["target_fingerprint"]},
        ],
    )

    assert result["blocked"] is True
    assert result["changed"] is False
    assert result["processes_stopped"] == 0
    assert stopped == []


def test_residue_scan_does_not_rebind_but_cleanup_stops_exact_replacement(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "managed-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    replacement = FakeProcess(303, executable.resolve(), 1100.5, argv)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [replacement],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")

    assert legacy["status"] == "exact_replacement"
    assert legacy["record_pid"] == 101
    assert legacy["verified_pid"] == 303
    assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == 101

    cleanup = manager.cleanup_residue(scan["scan_fingerprint"])

    assert cleanup["blocked"] is False
    assert cleanup["processes_stopped"] == 1
    assert replacement.terminated is True
    assert not state_path.exists()


def test_preboot_residue_never_inspects_pid_or_same_name_processes(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("preboot residue must not inspect processes")

    manager = ManagedMinerManager(
        cfg,
        process_factory=forbidden,
        process_iter_factory=forbidden,
        boot_time_factory=lambda: 1500.0,
        system_name="Windows",
    )

    scan = manager.scan_residue()
    legacy = next(item for item in scan["findings"] if item["role"] == "legacy")
    assert legacy["status"] == "preboot_record"
    cleanup = manager.cleanup_residue(scan["scan_fingerprint"])

    assert cleanup["blocked"] is False
    assert cleanup["processes_stopped"] == 0
    assert cleanup["records_removed"] == 1
    assert not state_path.exists()


def _write_forward_incompatible_seine_record(
    state_path: Path, executable: Path, *, pid: int = 703764
) -> dict[str, Any]:
    record = {
        "version": 99,
        "pid": pid,
        "executable": str(executable.resolve()),
        "create_time": 2000.25,
        "miner_type": "seine",
        "flight_sheet_id": "old-sheet",
        "started_at": "2026-01-01T00:00:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": "a" * 64,
        "api_port": 52887,
        "seine_data_dir": str(executable.parent / "seine-data"),
        "future_field": "written-by-a-newer-agent",
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(record), encoding="utf-8")
    return record


def test_residue_cleanup_removes_invalid_record_only_after_process_absence_proof(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "test-seine-0.2.45-linux-x64"
    package.mkdir()
    executable = package / "seine"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "primary" / "managed-miner-process.json"
    record = _write_forward_incompatible_seine_record(state_path, executable)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 2500.0,
        system_name="Linux",
    )

    scan = manager.scan_residue()
    primary = next(item for item in scan["findings"] if item["role"] == "primary")
    cleanup = manager.cleanup_residue(scan["scan_fingerprint"])

    assert primary["status"] == "invalid_record"
    assert primary["clearable"] is True
    assert primary["process_verified"] is False
    assert primary["miner_type"] == "seine"
    assert primary["record_pid"] == record["pid"]
    assert cleanup["blocked"] is False
    assert cleanup["processes_stopped"] == 0
    assert cleanup["records_removed"] == 1
    assert cleanup["results"] == [
        {
            "role": "primary",
            "status": "invalid_record_removed",
            "process_stopped": False,
            "record_removed": True,
            "reason": None,
        }
    ]
    assert not state_path.exists()


def test_invalid_residue_record_stays_blocked_while_same_name_process_is_live(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "test-seine-0.2.45-linux-x64"
    package.mkdir()
    executable = package / "seine"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "primary" / "managed-miner-process.json"
    _write_forward_incompatible_seine_record(state_path, executable)
    live = FakeProcess(801, executable.resolve(), 2100.5)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [live],
        boot_time_factory=lambda: 2500.0,
        system_name="Linux",
    )

    scan = manager.scan_residue()
    primary = next(item for item in scan["findings"] if item["role"] == "primary")
    cleanup = manager.cleanup_residue(scan["scan_fingerprint"])

    assert primary["status"] == "ambiguous"
    assert primary["clearable"] is False
    assert primary["manual_candidates"] == []
    assert "trusted-path process candidate is live" in primary["reason"]
    assert cleanup["blocked"] is True
    assert live.terminated is False
    assert state_path.exists()


def test_invalid_residue_cleanup_rejects_record_changed_after_scan(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "test-seine-0.2.45-linux-x64"
    package.mkdir()
    executable = package / "seine"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "primary" / "managed-miner-process.json"
    _write_forward_incompatible_seine_record(state_path, executable)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 2500.0,
        system_name="Linux",
    )
    scan = manager.scan_residue()
    changed = json.loads(state_path.read_text(encoding="utf-8"))
    changed["future_field"] = "changed"
    state_path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ManagedMinerError, match="changed after scan"):
        manager.cleanup_residue(scan["scan_fingerprint"])
    assert state_path.exists()


def test_residue_cleanup_rejects_record_changed_after_scan(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable.resolve()), "--config", "old-config.json"]
    write_process_record(state_path, executable, pid=101, created=1000.25)
    upgrade_process_record_to_v2(state_path, argv)
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [],
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )
    scan = manager.scan_residue()
    changed = json.loads(state_path.read_text(encoding="utf-8"))
    changed["pid"] = 303
    changed["create_time"] = 1100.5
    state_path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ManagedMinerError, match="changed after scan"):
        manager.cleanup_residue(scan["scan_fingerprint"])

    assert json.loads(state_path.read_text(encoding="utf-8"))["pid"] == 303


def test_empty_residue_scan_still_authorizes_idempotent_oc_cleanup(
    tmp_path: Path,
):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(
        cfg,
        boot_time_factory=lambda: 900.0,
        system_name="Windows",
    )

    scan = manager.scan_residue()
    cleanup = manager.cleanup_residue(scan["scan_fingerprint"])

    assert scan["cleanup_available"] is True
    assert scan["clearable_count"] == 0
    assert scan["ambiguous_count"] == 0
    assert all(
        finding["reason"] == "no managed miner process record"
        for finding in scan["findings"]
    )
    assert cleanup["blocked"] is False
    assert cleanup["changed"] is False
    assert cleanup["processes_stopped"] == 0
    assert cleanup["records_removed"] == 0
    assert cleanup["results"] == []


def test_manager_stop_is_idempotent_without_process_or_active_sheet(tmp_path: Path):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Windows")

    result = manager.stop()

    assert result["stopped"] is False
    assert result["miner_type"] is None
    assert result["flight_sheet_id"] is None


def test_manager_restart_reads_only_safe_active_flight_sheet(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    old = FakeProcess(101, executable, 1000.25)
    new = FakeProcess(202, executable, 2000.5)
    processes = {101: old, 202: new}
    write_process_record(
        cfg.managed_miner_runtime_dir / "managed-miner-process.json",
        executable,
        pid=101,
        created=1000.25,
    )
    stored = document(package, extra_config={"cpu_threads": 4})
    stored["gpu_oc"] = None
    stored["applied_at"] = "2026-07-15T00:00:00+00:00"
    cfg.flight_sheet_file.write_text(json.dumps(stored), encoding="utf-8")
    manager = ManagedMinerManager(
        cfg,
        popen_factory=lambda argv, **kwargs: FakePopen(),
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
        port_allocator=lambda: 24567,
        system_name="Windows",
    )

    result = manager.restart()

    assert result["restarted"] is True
    assert result["replaced_previous"] is True
    assert result["pid"] == 202
    assert old.terminated is True
    assert cfg.flight_sheet_file.exists()


def test_manager_restart_rejects_tampered_active_sheet_before_stopping(tmp_path: Path):
    cfg = managed_config(tmp_path)
    package = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    package.mkdir()
    executable = package / "xmrig.exe"
    executable.write_bytes(b"test")
    old = FakeProcess(101, executable, 1000.25)
    write_process_record(
        cfg.managed_miner_runtime_dir / "managed-miner-process.json",
        executable,
        pid=101,
        created=1000.25,
    )
    stored = document(package)
    stored["applied_at"] = "2026-07-15T00:00:00+00:00"
    stored["unexpected"] = "must fail closed"
    cfg.flight_sheet_file.write_text(json.dumps(stored), encoding="utf-8")
    manager = ManagedMinerManager(
        cfg,
        process_factory=lambda pid: old,
        system_name="Windows",
    )

    with pytest.raises(ManagedMinerError, match="invalid fields"):
        manager.restart()

    assert old.terminated is False


def test_clear_srb_oc_in_mixed_independent_flight_targets_secondary_role_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-test-1.0"
    srb_root = cfg.miner_packages_dir / "builtin-srbminer-test-1.0"
    xmrig_root.mkdir()
    srb_root.mkdir()
    stored = document(xmrig_root)
    stored.update(
        {
            "version": 4,
            "secondary_mode": "independent",
            "secondary": {
                **leg(
                    srb_root,
                    miner_type="srbminer",
                    package_id="builtin-srbminer-test",
                    algorithm="pearlhash",
                ),
                "coin": "PEARL",
                "device_indices": [0],
            },
            # Legacy metadata can carry this field even though new independent
            # flights use per-device NVML OC. It still proves that a reset is
            # required and must be routed to the SRBMiner role runtime.
            "gpu_oc": {
                "vendor": "nvidia",
                "settings": [{"index": 0, "power_limit_w": 200.0}],
            },
            "applied_at": "2026-07-30T00:00:00+00:00",
        }
    )
    cfg.flight_sheet_file.write_text(json.dumps(stored), encoding="utf-8")
    calls: list[tuple[str, dict, bool]] = []

    class RoleManager:
        def __init__(self, role: str):
            self.role = role

        def _clear_srb_gpu_oc_document(
            self,
            role_document,
            *,
            restart_on_failure=True,
        ):
            calls.append(
                (
                    self.role,
                    json.loads(json.dumps(role_document)),
                    restart_on_failure,
                )
            )
            return {
                "required": True,
                "cleared": True,
                "previous_was_running": True,
            }

    manager = ManagedMinerManager(cfg, system_name="Linux")
    monkeypatch.setattr(
        manager,
        "_role_manager",
        lambda _runtime, role: RoleManager(role),
    )

    result = manager.clear_active_srb_gpu_oc()

    assert result["cleared"] is True
    assert result["independent"] is True
    assert list(result["roles"]) == ["secondary"]
    assert [role for role, _document, _restart in calls] == ["secondary"]
    assert calls[0][1]["primary"]["miner_type"] == "srbminer"
    assert calls[0][1]["gpu_oc"] is None
    assert calls[0][2] is True


def test_srb_oc_clear_runs_verified_empty_reset_then_stops_transient_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Linux")
    active = document(
        tmp_path / "srb",
        miner_type="srbminer",
        package_id="builtin-srbminer-test",
        algorithm="pearlhash",
    )
    active["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 200.0}],
    }
    runtime = cfg.managed_miner_runtime_dir
    events: list[str] = []
    launched: list[dict] = []
    launch_options: list[dict] = []

    monkeypatch.setattr(
        manager,
        "_prepare_single",
        lambda reset_document: {
            "runtime": runtime,
            "api_port": 24567,
            "argv": [str(tmp_path / "srb" / "SRBMiner-MULTI"), "--gpu-reset-oc"],
            "executable": tmp_path / "srb" / "SRBMiner-MULTI",
            "gpu_mapping": [{"vendor": "nvidia"}],
        },
    )
    monkeypatch.setattr(
        manager,
        "running",
        lambda: {"managed": True, "miner_type": "srbminer", "pid": 41},
    )
    monkeypatch.setattr(
        manager,
        "_stop_previous",
        lambda _runtime, **_kwargs: events.append("stop") or True,
    )

    def apply_reset(reset_document, **kwargs):
        events.append("launch-reset")
        launched.append(json.loads(json.dumps(reset_document)))
        launch_options.append(dict(kwargs))
        return {"pid": 42}

    monkeypatch.setattr(manager, "_apply_single", apply_reset)
    monkeypatch.setattr(
        manager,
        "_wait_for_srb_oc_reset_ready",
        lambda _runtime, _port, _logs, _fingerprint: (
            events.append("ready") or "loopback_api"
        ),
    )
    monkeypatch.setattr(
        manager,
        "_write_status",
        lambda *_args, **_kwargs: events.append("status-stopped"),
    )

    result = manager._clear_srb_gpu_oc_document(active)

    assert result["cleared"] is True
    assert result["temporary_pid"] == 42
    assert events == [
        "stop",
        "launch-reset",
        "ready",
        "stop",
        "status-stopped",
    ]
    assert launched[0]["gpu_oc"] is None
    assert launched[0]["_reset_srb_gpu_oc"] is True
    assert launch_options == [
        {
            "prepared": {
                "runtime": runtime,
                "api_port": 24567,
                "argv": [
                    str(tmp_path / "srb" / "SRBMiner-MULTI"),
                    "--gpu-reset-oc",
                ],
                "executable": tmp_path / "srb" / "SRBMiner-MULTI",
                "gpu_mapping": [{"vendor": "nvidia"}],
            },
            "stop_previous": False,
            "require_startup_readiness": False,
        }
    ]


def test_srb_oc_clear_is_noop_for_amd_only_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Linux")
    active = document(
        tmp_path / "srb",
        miner_type="srbminer",
        package_id="builtin-srbminer-test",
        algorithm="etchash",
    )
    active["gpu_oc"] = None
    runtime = cfg.managed_miner_runtime_dir
    monkeypatch.setattr(
        manager,
        "_prepare_single",
        lambda _document: {
            "runtime": runtime,
            "api_port": 24567,
            "argv": [str(tmp_path / "srb" / "SRBMiner-MULTI")],
            "executable": tmp_path / "srb" / "SRBMiner-MULTI",
            "gpu_mapping": [{"vendor": "amd"}],
        },
    )
    monkeypatch.setattr(manager, "running", lambda: None)
    monkeypatch.setattr(
        manager,
        "_apply_single",
        lambda *_args, **_kwargs: pytest.fail(
            "AMD-only reset must not launch a transient SRBMiner process"
        ),
    )

    result = manager._clear_srb_gpu_oc_document(active)

    assert result == {
        "required": False,
        "cleared": True,
        "method": "not_applicable_no_nvidia",
        "previous_was_running": False,
        "stopped_previous": False,
    }


def test_srb_oc_clear_readiness_failure_restarts_original_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Linux")
    active = document(
        tmp_path / "srb",
        miner_type="srbminer",
        package_id="builtin-srbminer-test",
        algorithm="pearlhash",
    )
    active["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"index": 0, "core_clock_lock_mhz": 2700.0}],
    }
    runtime = cfg.managed_miner_runtime_dir
    events: list[str] = []
    applied: list[dict] = []

    monkeypatch.setattr(
        manager,
        "_prepare_single",
        lambda _document: {
            "runtime": runtime,
            "api_port": 24567,
            "argv": [str(tmp_path / "srb" / "SRBMiner-MULTI"), "--gpu-reset-oc"],
            "executable": tmp_path / "srb" / "SRBMiner-MULTI",
            "gpu_mapping": [{"vendor": "nvidia"}],
        },
    )
    monkeypatch.setattr(
        manager,
        "running",
        lambda: {"managed": True, "miner_type": "srbminer", "pid": 41},
    )
    monkeypatch.setattr(
        manager,
        "_stop_previous",
        lambda _runtime, **_kwargs: events.append("stop") or True,
    )

    def apply_document(value, **_kwargs):
        applied.append(json.loads(json.dumps(value)))
        events.append("apply")
        return {"pid": 42 + len(applied)}

    monkeypatch.setattr(manager, "_apply_single", apply_document)
    monkeypatch.setattr(
        manager,
        "_wait_for_srb_oc_reset_ready",
        lambda _runtime, _port, _logs, _fingerprint: (_ for _ in ()).throw(
            ManagedMinerError("reset API unavailable")
        ),
    )

    with pytest.raises(ManagedMinerError, match="reset API unavailable"):
        manager._clear_srb_gpu_oc_document(active)

    # First apply is the empty reset process; after its bounded cleanup the
    # exact original flight (including OC) is relaunched.
    assert events == ["stop", "apply", "stop", "apply"]
    assert applied[0]["gpu_oc"] is None
    assert applied[0]["_reset_srb_gpu_oc"] is True
    assert applied[1]["gpu_oc"] == active["gpu_oc"]


@pytest.mark.parametrize(
    ("log_progressed", "expected_confirmation"),
    [
        (True, "verified_reset_invocation_stable"),
        (False, "verified_exact_reset_invocation_stable"),
    ],
)
def test_srb_oc_reset_uses_stable_exact_invocation_when_api_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    log_progressed: bool,
    expected_confirmation: str,
):
    cfg = managed_config(tmp_path)
    manager = ManagedMinerManager(cfg, system_name="Windows")
    fingerprint = "a" * 64
    record = {
        "miner_type": "srbminer",
        "api_port": 24567,
        "argv_sha256": fingerprint,
    }
    monotonic_value = 0.0

    def monotonic():
        nonlocal monotonic_value
        monotonic_value += 0.5
        return monotonic_value

    def unavailable(*_args, **_kwargs):
        raise OSError("timed out")

    monkeypatch.setattr(
        manager,
        "_verified_previous",
        lambda *_args, **_kwargs: (record, object()),
    )
    monkeypatch.setattr(
        manager,
        "_srb_oc_reset_log_progressed",
        lambda *_args, **_kwargs: log_progressed,
    )
    monkeypatch.setattr(managed_miners_module.time, "monotonic", monotonic)
    monkeypatch.setattr(managed_miners_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        managed_miners_module.socket,
        "create_connection",
        unavailable,
    )

    confirmation = manager._wait_for_srb_oc_reset_ready(
        cfg.managed_miner_runtime_dir,
        24567,
        {},
        fingerprint,
    )

    assert confirmation == expected_confirmation


@pytest.mark.parametrize("created", [10 ** 1000, True, float("nan"), float("inf")],
                         ids=["overflow", "boolean", "nan", "infinity"])
def test_process_record_invalid_creation_time_fails_closed_without_mutation(tmp_path, created):
    cfg = managed_config(tmp_path)
    path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    write_process_record(path, tmp_path / "xmrig.exe", pid=123, created=created)
    before = path.read_bytes()
    manager = ManagedMinerManager(cfg, system_name="Windows")
    with pytest.raises(ManagedMinerError, match="invalid values"):
        manager._read_process_state(path)
    assert path.read_bytes() == before


def test_process_record_json_integer_parser_limit_fails_closed_without_mutation(tmp_path):
    cfg = managed_config(tmp_path)
    path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    write_process_record(path, tmp_path / "xmrig.exe", pid=123, created=1000.25)
    path.write_text(path.read_text().replace('"create_time": 1000.25', '"create_time": ' + "1" * 5000))
    before = path.read_bytes()
    manager = ManagedMinerManager(cfg, system_name="Windows")
    with pytest.raises(ManagedMinerError, match="invalid"):
        manager._read_process_state(path)
    assert path.read_bytes() == before
