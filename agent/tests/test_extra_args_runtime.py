"""Literal extra arguments and retired-process transitions, with no real miner."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import psutil
import pytest

import agent.managed_miners as managed_miners_module
from agent.commands import Command, CommandExecutor
from agent.managed_miners import ManagedMinerError, ManagedMinerManager
from agent.mining_commands import MiningCommandError, validate_flight_sheet
from agent.process_identity import managed_argv_fingerprint
from agent.tests.test_managed_miners import (
    FakePopen,
    FakeProcess,
    completed_device_list,
    document,
    independent_document,
    leg,
    managed_config,
    upgrade_process_record_to_v2,
    write_process_record,
)
from agent.tests.test_managed_mining_commands import (
    config as command_config,
    payload,
    pearl_payload,
    srb_gpu_payload,
    srb_payload,
)
from agent.tests.test_mining_commands import (
    config as legacy_config,
    flight_payload as legacy_payload,
)
from agent.wsl_mining import WslLaunchIdentity
from server.miner_adapters import MINER_ADAPTERS


RICH_EXTRA_ARGS = (
    '--software-level 17 --software-toggle --software-label "two words" '
    "--software-empty ''\n"
    '--future-vendor-option=value --software-path "C:\\miner files\\cache" '
    '--literal "$(touch should-not-exist); & | >"'
)
RICH_ARGV = [
    "--software-level", "17", "--software-toggle", "--software-label", "two words",
    "--software-empty", "", "--future-vendor-option=value", "--software-path",
    "C:\\miner files\\cache", "--literal", "$(touch should-not-exist); & | >",
]


def _runtime_flight(cfg, miner_type: str, system_name: str, extra_args: str):
    package = cfg.miner_packages_dir / f"builtin-{miner_type}-test-1.0"
    package.mkdir()
    entry = {
        ("xmrig", "Linux"): "xmrig",
        ("xmrig", "Windows"): "xmrig.exe",
        ("srbminer", "Linux"): "SRBMiner-MULTI",
        ("srbminer", "Windows"): "SRBMiner-MULTI.exe",
        ("pearlminer", "Linux"): "miner-cuda13",
        ("pearlminer", "Windows"): "miner.exe",
    }[miner_type, system_name]
    (package / entry).write_bytes(b"synthetic miner; never executed")
    extra = {"extra_args": extra_args}
    if miner_type == "pearlminer":
        extra.update(gpu_backend="nvidia", cuda_variant="13" if system_name == "Linux" else "auto")
    flight = document(
        package, miner_type=miner_type,
        algorithm="pearlhash" if miner_type == "pearlminer" else "randomx",
        extra_config=extra,
    )
    if miner_type == "pearlminer":
        flight["primary"].update(coin="PEARL", pool_url="pearl://pool.example.test:4444")
    return flight


def _capturing_manager(cfg, *, system_name="Linux", existing=None):
    processes = dict(existing or {})
    launches = []
    pids = iter(range(301, 320))
    ports = iter(range(24567, 24587))

    def popen(argv, **kwargs):
        pid = next(pids)
        launches.append((list(argv), kwargs))
        processes[pid] = FakeProcess(pid, Path(argv[0]).resolve(), 2000.0 + pid, argv)
        return FakePopen(pid)

    manager = ManagedMinerManager(
        cfg, popen_factory=popen, process_factory=processes.__getitem__,
        process_iter_factory=lambda: [p for p in processes.values() if p.running],
        boot_time_factory=lambda: 0.0, port_allocator=lambda: next(ports),
        system_name=system_name, device_runner=completed_device_list(),
    )
    return manager, launches, processes


def _assert_record_matches_launch(runtime: Path, argv: list[str]):
    record = json.loads((runtime / "managed-miner-process.json").read_text())
    assert record["argv_sha256"] == managed_argv_fingerprint(
        argv, executable=record["executable"],
    )
    assert "argv" not in record
    return record


@pytest.mark.parametrize("miner_type", ["xmrig", "srbminer", "pearlminer"])
@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
@pytest.mark.parametrize("extra_args", ["", RICH_EXTRA_ARGS], ids=["cleared", "literal-options"])
def test_native_launch_preserves_literal_extra_argv_and_full_fingerprint(
    tmp_path, miner_type, system_name, extra_args,
):
    cfg = managed_config(tmp_path)
    flight = _runtime_flight(cfg, miner_type, system_name, extra_args)
    manager, launches, processes = _capturing_manager(cfg, system_name=system_name)

    result = manager.apply(flight)

    assert result["pid"] == 301
    assert len(launches) == 1
    argv, options = launches[0]
    assert options["shell"] is False
    assert options["cwd"] == str(Path(argv[0]).parent)
    if extra_args:
        assert argv[-len(RICH_ARGV):] == RICH_ARGV
        record = _assert_record_matches_launch(cfg.managed_miner_runtime_dir, argv)
        assert record["argv_sha256"] != managed_argv_fingerprint(
            argv[:-len(RICH_ARGV)], executable=record["executable"],
        )
    else:
        assert not any(argument.startswith("--software-") for argument in argv)
        _assert_record_matches_launch(cfg.managed_miner_runtime_dir, argv)
    assert flight["primary"]["extra_config"]["extra_args"] == extra_args
    if extra_args:
        cleared = copy.deepcopy(flight)
        cleared["primary"]["extra_config"]["extra_args"] = ""
        manager.apply(cleared)
        cleared_argv, cleared_options = launches[1]
        assert processes[301].terminated is True
        assert cleared_options["shell"] is False
        assert not any(argument.startswith("--software-") for argument in cleared_argv)
        assert "--future-vendor-option=value" not in cleared_argv
        _assert_record_matches_launch(cfg.managed_miner_runtime_dir, cleared_argv)


@pytest.mark.parametrize("gpu_primary", [False, True], ids=["cpu-primary", "gpu-primary"])
def test_integrated_srb_appends_both_legs_in_declared_order_after_cpu_gpu_reorder(
    tmp_path, gpu_primary,
):
    cfg = managed_config(tmp_path)
    flight = _runtime_flight(cfg, "srbminer", "Linux", '--software-primary "first leg"')
    gpu_leg = leg(
        Path(flight["primary"]["miner_package"]["installed_path"]),
        miner_type="srbminer", algorithm="pearlhash",
        extra_config={"extra_args": '--software-secondary "second leg" --secondary-toggle'},
    )
    gpu_leg.update(coin="PRL", device_indices=[1])
    flight["secondary"] = gpu_leg
    if gpu_primary:
        flight["primary"], flight["secondary"] = flight["secondary"], flight["primary"]
        flight["primary"]["extra_config"]["extra_args"] = '--software-primary "first leg"'
        flight["secondary"]["extra_config"]["extra_args"] = '--software-secondary "second leg" --secondary-toggle'
    manager, launches, _processes = _capturing_manager(cfg)

    manager.apply(flight)

    assert len(launches) == 1
    argv, options = launches[0]
    assert options["shell"] is False
    assert argv.index("--algorithm-cpu") < argv.index("--algorithm-gpu")
    assert argv[argv.index("--algorithm-cpu") + 1] == "randomx"
    assert argv[argv.index("--algorithm-gpu") + 1] == "pearlhash"
    assert argv[-5:] == [
        "--software-primary", "first leg", "--software-secondary", "second leg", "--secondary-toggle",
    ]
    _assert_record_matches_launch(cfg.managed_miner_runtime_dir, argv)


@pytest.mark.parametrize("gpu_primary", [False, True], ids=["gpu-secondary", "gpu-primary"])
def test_independent_roles_promote_each_legs_extra_args_to_its_own_launch(tmp_path, gpu_primary):
    cfg = managed_config(tmp_path)
    cpu_flight = _runtime_flight(cfg, "xmrig", "Linux", '--software-cpu "cpu value"')
    gpu_flight = _runtime_flight(cfg, "pearlminer", "Linux", '--software-gpu "gpu value"')
    flight = independent_document(
        Path(cpu_flight["primary"]["miner_package"]["installed_path"]),
        Path(gpu_flight["primary"]["miner_package"]["installed_path"]),
    )
    flight["primary"], flight["secondary"] = cpu_flight["primary"], gpu_flight["primary"]
    if gpu_primary:
        flight["primary"], flight["secondary"] = flight["secondary"], flight["primary"]
    manager, launches, _processes = _capturing_manager(cfg)

    result = manager.apply(flight)

    assert result["independent"] is True
    assert len(launches) == 2
    for role, (argv, options) in zip(("primary", "secondary"), launches):
        kind = flight[role]["miner_type"]
        assert argv[-2:] == (["--software-cpu", "cpu value"] if kind == "xmrig"
                            else ["--software-gpu", "gpu value"])
        assert options["shell"] is False
        _assert_record_matches_launch(cfg.managed_miner_runtime_dir / role, argv)
        saved = json.loads((cfg.managed_miner_runtime_dir / role / "active-flight-sheet.json").read_text())
        assert saved["primary"]["extra_config"]["extra_args"] == flight[role]["extra_config"]["extra_args"]


@pytest.mark.parametrize("factory", [payload, srb_payload, pearl_payload], ids=["xmrig", "srbminer", "pearlminer"])
@pytest.mark.parametrize("alphabet", ["x", "参", "😀"], ids=["ascii", "bmp", "non-bmp"])
def test_agent_wire_accepts_multiline_4096_and_rejects_oversize_or_controls(tmp_path, factory, alphabet):
    cfg = command_config(tmp_path, allow_gpu_oc=True)
    prefix, suffix = '--future-option "', '"\r\n--future-toggle\tvalue'
    text = prefix + alphabet * (4096 - len(prefix) - len(suffix)) + suffix
    raw = factory()
    raw["primary"]["extra_config"]["extra_args"] = text

    validated = validate_flight_sheet(raw, cfg)

    assert len(text) == 4096
    assert validated["primary"]["extra_config"]["extra_args"] == text.replace("\r\n", "\n")
    assert raw["primary"]["extra_config"]["extra_args"] == text
    for clear_value in (None, " \r\n\t "):
        raw["primary"]["extra_config"]["extra_args"] = clear_value
        assert validate_flight_sheet(raw, cfg)["primary"]["extra_config"]["extra_args"] == ""
    for invalid, reason in [(text + "x", "at most 4096")]+[
        ("--future-option before" + control + "after", "control characters")
        for control in ("\0", "\x1f", "\x7f", "\x85")
    ]:
        raw["primary"]["extra_config"]["extra_args"] = invalid
        with pytest.raises(MiningCommandError, match=reason):
            validate_flight_sheet(raw, cfg)


def test_extra_args_exception_does_not_relax_legacy_wire(tmp_path):
    cfg = legacy_config(tmp_path, flight_sheet_extra_key_allowlist=frozenset({"extra_args"}))
    raw = legacy_payload(primary_overrides={"extra_config": {"extra_args": "x" * 1024}})
    assert validate_flight_sheet(raw, cfg)["primary"]["extra_config"]["extra_args"] == "x" * 1024
    for value in ("x" * 1025, "line one\nline two"):
        raw["primary"]["extra_config"]["extra_args"] = value
        with pytest.raises(MiningCommandError, match="invalid string"):
            validate_flight_sheet(raw, cfg)


def _conflicting_wire(case):
    if case == "xmrig-primary":
        raw, role, flag = payload(), "primary", "-opool.other.test:3333"
    elif case == "pearl-primary":
        raw, role, flag = pearl_payload(), "primary", "--worker=other-worker"
    elif case == "srb-primary":
        raw, role, flag = srb_payload(), "primary", "--api-port=5555"
    elif case == "srb-integrated-secondary":
        raw, role, flag = srb_payload(), "secondary", "--gpu-coffset0=50"
        raw["secondary"] = copy.deepcopy(raw["primary"])
        raw["secondary"].update(coin="XMR", algorithm="randomx", device_indices=[])
    elif case == "srb-independent-secondary":
        raw, role, flag = payload(), "secondary", "--pool=other.test:3333"
        raw.update(secondary_mode="independent", secondary=srb_gpu_payload()["primary"])
    elif case == "xmrig-independent-secondary":
        raw, role, flag = pearl_payload(), "secondary", "--threads=8"
        raw.update(secondary_mode="independent", secondary=payload()["primary"])
    else:
        raw, role, flag = payload(), "secondary", "--address=other-wallet"
        raw.update(secondary_mode="independent", secondary=pearl_payload()["primary"])
    raw[role]["extra_config"]["extra_args"] = flag
    return raw, role


@pytest.mark.parametrize("case", [
    "xmrig-primary", "srb-primary", "pearl-primary", "srb-integrated-secondary",
    "srb-independent-secondary", "pearl-independent-secondary", "xmrig-independent-secondary",
])
def test_generated_parameter_conflict_fails_before_installer_manager_or_old_flight_write(tmp_path, case):
    cfg = command_config(tmp_path, allow_gpu_oc=True)
    previous = validate_flight_sheet(payload(), cfg)
    cfg.flight_sheet_file.write_text(json.dumps(previous))
    before = cfg.flight_sheet_file.read_bytes()
    events = []

    def forbidden_install(*_args, **_kwargs):
        events.append("install")
        pytest.fail("conflicting extra arguments reached the installer")

    def forbidden_manager(*_args, **_kwargs):
        events.append("manager")
        pytest.fail("conflicting extra arguments reached the running miner")

    raw, role = _conflicting_wire(case)
    outcome = CommandExecutor(
        cfg, package_installer=forbidden_install, managed_miner_factory=forbidden_manager,
    ).execute(Command(f"conflict-{case}", "apply_flight_sheet", raw))

    assert outcome.status == "failed"
    assert f"{role}.extra_config" in outcome.error
    assert "conflicts with managed parameter" in outcome.error
    assert events == []
    assert cfg.flight_sheet_file.read_bytes() == before


@pytest.mark.parametrize("k1_args", ["--pearl-k1", "--pearl-k1=0,2"], ids=["standalone", "equals-indices"])
def test_agent_checks_known_srb_extra_version_floor_before_install_or_old_stop(tmp_path, k1_args):
    cfg = command_config(tmp_path)
    previous = validate_flight_sheet(payload(), cfg)
    installed = tmp_path / "mock-installed-srb"  # The installer never touches this path.

    for version, extra_args, accepted in (
        ("3.7.1", k1_args, False),
        ("3.7.2", k1_args, True),
        ("3.7.1", "--future-vendor-option=0,2", True),
    ):
        cfg.flight_sheet_file.write_text(json.dumps(previous))
        before = cfg.flight_sheet_file.read_bytes()
        raw = srb_payload()
        raw.pop("gpu_oc")
        raw["primary"]["miner_package"].update(
            id=f"builtin-srbminer-{version}", version=version,
        )
        raw["primary"]["device_indices"] = []
        raw["primary"]["extra_config"]["extra_args"] = extra_args
        original_leg = copy.deepcopy(raw["primary"])
        events, applied = [], []

        def installer(metadata, _config, **_kwargs):
            events.append("install")
            assert metadata["version"] == version
            return {"package_id": metadata["id"], "version": metadata["version"],
                    "path": str(installed), "sha256": None, "downloaded": False}

        class Manager:
            def running(self):
                events.append("running")
                return {"managed": True, "miner_type": "xmrig", "pid": 42}

            def apply(self, value):
                events.append("apply")
                applied.append(value)
                return {"managed": True, "miner_type": "srbminer", "pid": 123}

            def stop(self):
                events.append("stop")
                pytest.fail("validation must never stop the previous miner")

        def manager_factory(_config):
            events.append("manager")
            return Manager()

        outcome = CommandExecutor(
            cfg, package_installer=installer, managed_miner_factory=manager_factory,
        ).execute(Command(f"srb-floor-{version}", "apply_flight_sheet", raw))

        assert raw["primary"] == original_leg
        assert not installed.exists()
        if not accepted:
            assert outcome.status == "failed"
            assert "--pearl-k1 requires SRBMiner 3.7.2 or newer" in outcome.error
            assert events == []
            assert applied == []
            assert cfg.flight_sheet_file.read_bytes() == before
        else:
            assert outcome.status == "succeeded", outcome.error
            assert "install" in events and "apply" in events and "stop" not in events
            target = applied[0]["primary"]
            assert target["device_indices"] == []
            assert target["extra_config"]["extra_args"] == extra_args
            for field in ("coin", "wallet_address", "pool_url", "pool_password", "algorithm"):
                assert target[field] == original_leg[field]


def _retired_process(cfg, miner_type, system_name, *, reused=False):
    name = {"lolminer": "lolMiner", "forgeminer": "forge"}[miner_type]
    if system_name == "Windows":
        name += ".exe"
    package = cfg.miner_packages_dir / f"builtin-{miner_type}-old-1.0"
    package.mkdir()
    executable = package / name
    executable.write_bytes(b"old fixture; never executed")
    argv = [str(executable.resolve()), "--legacy-session", "owned-session"]
    record_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    write_process_record(record_path, executable, pid=101, created=1000.25, miner_type=miner_type)
    upgrade_process_record_to_v2(record_path, argv)
    process = FakeProcess(101, executable.resolve(), 2000.5 if reused else 1000.25,
                          [str(executable.resolve()), "--legacy-session", "unrelated"] if reused else argv)
    return record_path, process


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_retired_exact_old_record_can_stop_but_cannot_start(tmp_path, miner_type, system_name):
    cfg = managed_config(tmp_path)
    state_path, process = _retired_process(cfg, miner_type, system_name)
    manager, launches, _processes = _capturing_manager(cfg, system_name=system_name, existing={101: process})

    assert MINER_ADAPTERS.get(miner_type) is None
    stopped = manager.stop()

    assert stopped["stopped"] is True
    assert stopped["miner_type"] == miner_type
    assert process.terminated is True
    assert not state_path.exists()
    with pytest.raises(ManagedMinerError, match="retired"):
        manager.apply(document(process.executable.parent, miner_type=miner_type))
    assert launches == []


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
def test_retired_record_never_stops_a_reused_pid(tmp_path, miner_type):
    cfg = managed_config(tmp_path)
    state_path, reused = _retired_process(cfg, miner_type, "Linux", reused=True)
    manager = ManagedMinerManager(
        cfg, process_factory=lambda _pid: reused, process_iter_factory=lambda: [],
        boot_time_factory=lambda: 0.0, system_name="Linux",
    )

    result = manager.stop()

    assert result["stopped"] is False
    assert reused.terminated is False
    assert reused.killed is False
    assert not state_path.exists()


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
def test_active_flight_can_replace_exact_retired_record(tmp_path, miner_type):
    cfg = managed_config(tmp_path)
    state_path, old = _retired_process(cfg, miner_type, "Linux")
    flight = _runtime_flight(cfg, "xmrig", "Linux", '--future-option "new session"')
    manager, launches, _processes = _capturing_manager(cfg, existing={101: old})

    result = manager.apply(flight)

    assert result["replaced_previous"] is True
    assert old.terminated is True
    assert len(launches) == 1
    assert launches[0][0][-2:] == ["--future-option", "new session"]
    assert json.loads(state_path.read_text())["miner_type"] == "xmrig"


@pytest.mark.parametrize("miner_type,entry", [("lolminer", "lolMiner"), ("forgeminer", "forge")])
def test_retired_wsl_v5_record_keeps_cached_guest_and_user_identity(tmp_path, monkeypatch, miner_type, entry):
    cfg = managed_config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    package = cfg.miner_packages_dir / f"builtin-{miner_type}-old"
    package.mkdir()
    target = package / entry  # The old Windows-side package file is already absent.
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"MZ fixture")
    identity = WslLaunchIdentity(7, "S-1-5-21-1000", "Ubuntu-22.04", 1000)
    cache_key = "e" * 64
    guest_target = f"/home/user/.rigpilot/miner-cache-v1/{cache_key}/{entry}"
    record = {
        "version": 5, "pid": 919, "executable": str(wrapper.resolve()),
        "create_time": 3100.25, "miner_type": miner_type,
        "flight_sheet_id": "old-wsl-flight", "started_at": "2026-08-03T00:00:00+00:00",
        "survives_agent_restart": True, "argv_sha256": "a" * 64, "api_port": 45001,
        "execution_mode": "wsl", "target_executable": str(target), "wsl_token": "b" * 64,
        "wsl_guest_executable": guest_target, "wsl_cache_key": cache_key,
        "wsl_identity": {"windows_session_id": 7, "windows_user_sid": identity.windows_user_sid,
                         "distribution": identity.distribution, "linux_uid": identity.linux_uid},
    }
    state_path = runtime / "managed-miner-process.json"
    managed_miners_module._write_private_json(state_path, record)
    monkeypatch.setattr(managed_miners_module, "wsl_executable_path", lambda: wrapper.resolve())
    queries, stopped = [], []

    def query(*args, **kwargs):
        queries.append((args, kwargs))
        return True

    def stop(*args, **kwargs):
        stopped.append((args, kwargs))
        return True

    def absent(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(managed_miners_module, "query_wsl_guest", query)
    monkeypatch.setattr(managed_miners_module, "stop_wsl_guest", stop)
    manager = ManagedMinerManager(cfg, process_factory=absent, boot_time_factory=lambda: 0.0,
                                  system_name="Windows")

    assert manager._stop_previous(runtime, clear_definitely_absent=True) is True
    assert queries and len(stopped) == 1
    assert stopped[0][0][1:3] == ("b" * 64, guest_target)
    assert stopped[0][0][3] == identity
    assert not state_path.exists()
