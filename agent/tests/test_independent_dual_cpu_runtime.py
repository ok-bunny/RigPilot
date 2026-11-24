"""Independent CPU roles through production Manager and collector paths.

Executables, processes, API responses and time are controlled boundaries. No
miner, socket, GPU, VM, or device operation is performed. The constructor's
default production readiness is enabled before those boundaries are replaced.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import inventory
import agent.managed_miners as managed
from agent.process_identity import managed_argv_fingerprint
from agent.pearl_workload import PearlWorkloadCounters
from agent.seine_telemetry import SeineTelemetryObserver
from agent.state import AgentState
from agent.tests.test_managed_miners import FakePopen, FakeProcess, leg, managed_config
from agent.tests.test_managed_miner_status import FakeResponse
from agent.tests.test_seine_workload_bridge import _seine_state


CPU_PAIRS = [("xmrig", "seine"), ("seine", "xmrig"), ("xmrig", "xmrig"), ("seine", "seine")]
BASE = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)


def _value(argv, option):
    return argv[argv.index(option) + 1]


def _dual_fixture(tmp_path, monkeypatch, *, pair, system_name, threads=(3, 7)):
    cfg = managed_config(tmp_path.resolve())
    packages = {}
    for miner_type in set(pair):
        package = cfg.miner_packages_dir / f"builtin-{miner_type}-dual-cpu"
        package.mkdir()
        executable = package / (miner_type + (".exe" if system_name == "Windows" else ""))
        executable.write_bytes(b"controlled identity; never executed")
        packages[miner_type] = package
    legs = []
    for index, miner_type in enumerate(pair):
        value = leg(
            packages[miner_type], miner_type=miner_type,
            algorithm="blocknet-argon2id" if miner_type == "seine" else "randomx",
            extra_config={} if threads[index] is None else {"cpu_threads": threads[index]},
        )
        value.update(
            coin="BNT" if miner_type == "seine" else "XMR",
            wallet_address=f"synthetic-wallet-{index}",
            pool_url=f"stratum+tcp://pool-{index}.example.test:3333",
            pool_password="" if miner_type == "seine" else "x",
        )
        legs.append(value)
    flight = {
        "version": 4, "secondary_mode": "independent", "flight_sheet_id": "dual-cpu",
        "name": "Independent CPU roles", "worker_name": "synthetic-rig",
        "primary": legs[0], "secondary": legs[1], "gpu_oc": None,
        "applied_at": BASE.isoformat(),
    }
    cfg.flight_sheet_file.write_text(json.dumps(flight))
    # Every secondary prepare receives the primary's already selected port.
    # Repeat the first candidate deliberately: neither process is listening yet.
    allocation = {"count": 0}

    def allocate():
        count = allocation["count"]
        allocation["count"] += 1
        return 24560 + (count // 3) * 2 + (1 if count % 3 == 2 else 0)

    manager = managed.ManagedMinerManager(cfg, port_allocator=allocate, system_name=system_name)
    assert manager.enforce_startup_readiness is True
    processes, launches = {}, []
    clock = SimpleNamespace(seconds=0.0)
    api_probes = []

    def popen(argv, **kwargs):
        pid = 4100 + len(launches)
        process = FakeProcess(pid, Path(argv[0]).resolve(), BASE.timestamp() - 100 + pid, argv)
        processes[pid] = process
        log = Path(kwargs["stdout"].name)
        kwargs["stdout"].write(b"new independent CPU invocation\n")
        if "--api-server" in argv and "--data-dir" in argv:
            data_dir = Path(_value(argv, "--data-dir"))
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "control-api.cookie").write_text(f"{pid:064x}")
        launches.append({"argv": list(argv), "cwd": Path(kwargs["cwd"]), "log": log, "pid": pid})
        return FakePopen(pid)

    def sleep(seconds):
        clock.seconds += seconds
        for launched in launches:
            if processes[launched["pid"]].running:
                with launched["log"].open("ab") as stream:
                    stream.write(b"fresh controlled progress\n")

    manager.popen_factory = popen
    manager.process_factory = processes.__getitem__
    manager.process_iter_factory = lambda: [p for p in processes.values() if p.running]
    manager.boot_time_factory = lambda: 0.0
    monkeypatch.setattr(managed, "time", SimpleNamespace(monotonic=lambda: clock.seconds, sleep=sleep))
    monkeypatch.setattr(managed.ManagedMinerManager, "_loopback_api_is_listening", staticmethod(
        lambda port: api_probes.append(port) or True,
    ))
    return SimpleNamespace(manager=manager, config=cfg, flight=flight, processes=processes,
                           launches=launches, clock=clock, probes=api_probes, allocation=allocation)


def _assert_live_roles(fixture, result, *, pair, threads):
    runtime = fixture.config.managed_miner_runtime_dir
    current_launches = fixture.launches[-2:]
    assert result["independent"] is True and set(result["roles"]) == {"primary", "secondary"}
    assert len({r["pid"] for r in result["roles"].values()}) == 2
    assert len({r["api_port"] for r in result["roles"].values()}) == 2
    assert sum(p.running for p in fixture.processes.values()) == 2
    configs, data_dirs = [], []
    for index, role in enumerate(("primary", "secondary")):
        launched, value = current_launches[index], result["roles"][role]
        role_runtime = runtime / role
        argv = launched["argv"]
        record = json.loads((role_runtime / "managed-miner-process.json").read_text())
        saved = json.loads((role_runtime / "active-flight-sheet.json").read_text())
        status = json.loads((role_runtime / "miner-status.json").read_text())
        assert record["pid"] == value["pid"] == launched["pid"]
        assert record["api_port"] == value["api_port"]
        assert record["argv_sha256"] == managed_argv_fingerprint(argv, executable=argv[0])
        assert saved["primary"] == fixture.flight[role] and saved["secondary"] is None
        assert saved["version"] == 3 and saved.get("secondary_mode") is None
        assert status["state"] == "running" and status["pid"] == launched["pid"]
        assert launched["log"] == role_runtime / "managed-miner-launcher.log"
        if threads[index] is None:
            assert "--threads" not in argv  # Retain the miner's own automatic sizing.
        else:
            assert _value(argv, "--threads") == str(threads[index])
        if pair[index] == "xmrig":
            assert value["readiness"] == "loopback_api"
            path = Path(_value(argv, "--config"))
            assert path.parent == role_runtime
            config = json.loads(path.read_text())
            assert config["http"]["port"] == value["api_port"]
            assert config["pools"][0]["user"] == fixture.flight[role]["wallet_address"]
            assert config["pools"][0]["url"] == f"pool-{index}.example.test:3333"
            assert config["cpu"]["enabled"] is True
            configs.append(path)
        else:
            assert value["readiness"] == "verified_process_and_fresh_log"
            assert launched["cwd"] == role_runtime
            assert _value(argv, "--api-bind") == f"127.0.0.1:{value['api_port']}"
            assert _value(argv, "--address") == fixture.flight[role]["wallet_address"]
            path = Path(_value(argv, "--data-dir"))
            assert path.parent == role_runtime / "seine-data"
            data_dirs.append(path)
    assert len(configs) == len(set(configs)) and len(data_dirs) == len(set(data_dirs))
    assert fixture.manager.running()["complete"] is True


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
@pytest.mark.parametrize("pair", CPU_PAIRS, ids=lambda pair: "+".join(pair))
def test_independent_cpu_manager_apply_restart_and_stop_isolate_roles(
    tmp_path, monkeypatch, pair, system_name,
):
    fixture = _dual_fixture(tmp_path, monkeypatch, pair=pair, system_name=system_name)
    first = fixture.manager.apply(fixture.flight)
    _assert_live_roles(fixture, first, pair=pair, threads=(3, 7))
    assert fixture.allocation["count"] == 3  # Duplicate during prepare was skipped.
    for _ in range(2):
        prior = set(fixture.processes)
        restarted = fixture.manager.restart()
        assert restarted["restarted"] is True and restarted["replaced_previous"] is True
        assert all(fixture.processes[pid].terminated for pid in prior)
        _assert_live_roles(fixture, restarted, pair=pair, threads=(3, 7))
    assert len(fixture.launches) == 6 and fixture.allocation["count"] == 9
    assert fixture.config.flight_sheet_file.read_text() == json.dumps(fixture.flight)
    stopped = fixture.manager.stop()
    assert stopped["stopped"] is True and all(v["stopped"] for v in stopped["roles"].values())
    assert all(p.terminated and not p.running for p in fixture.processes.values())
    assert fixture.manager.running() is None
    for role in ("primary", "secondary"):
        runtime = fixture.config.managed_miner_runtime_dir / role
        assert not (runtime / "managed-miner-process.json").exists()
        assert json.loads((runtime / "miner-status.json").read_text())["state"] == "stopped"
    assert fixture.manager.stop()["stopped"] is False


@pytest.mark.parametrize("pair", CPU_PAIRS, ids=lambda pair: "+".join(pair))
def test_independent_cpu_auto_primary_does_not_resize_fixed_secondary(tmp_path, monkeypatch, pair):
    fixture = _dual_fixture(tmp_path, monkeypatch, pair=pair, system_name="Linux", threads=(None, 11))
    result = fixture.manager.apply(fixture.flight)
    _assert_live_roles(fixture, result, pair=pair, threads=(None, 11))
    fixture.manager.stop()


@pytest.mark.parametrize("pair", CPU_PAIRS, ids=lambda pair: "+".join(pair))
def test_independent_cpu_preparation_requires_two_ports_before_stopping(tmp_path, monkeypatch, pair):
    fixture = _dual_fixture(tmp_path, monkeypatch, pair=pair, system_name="Linux")
    fixture.manager.port_allocator = lambda: 24560
    with pytest.raises(managed.ManagedMinerError, match="distinct managed miner API port"):
        fixture.manager.apply(fixture.flight)
    assert fixture.launches == [] and fixture.processes == {}


def _status_fixture(fixture, result, monkeypatch):
    bodies, endpoints = {}, {}
    for index, role in enumerate(("primary", "secondary")):
        miner_type = fixture.flight[role]["miner_type"]
        port = result["roles"][role]["api_port"]
        endpoints[role] = (f"http://127.0.0.1:{port}/v1/runtime/state" if miner_type == "seine"
                           else f"http://127.0.0.1:{port}/2/summary")
        bodies[role] = (_seine_state() if miner_type == "seine" else {
            "hashrate": {"total": [100.0 * (index + 1)]},
            "results": {"shares_good": index + 2, "shares_total": index + 3},
        })
    requests = []

    class LocalDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = BASE + timedelta(seconds=fixture.clock.seconds)
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    def requester(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        requests.append(url)
        role = next(role for role, endpoint in endpoints.items() if endpoint == url)
        return FakeResponse(deepcopy(bodies[role]))

    monkeypatch.setattr(inventory, "datetime", LocalDatetime)
    monkeypatch.setattr(inventory, "time", SimpleNamespace(
        monotonic=lambda: fixture.clock.seconds, time=lambda: BASE.timestamp() + fixture.clock.seconds,
    ))
    monkeypatch.setattr(inventory, "_SEINE_TELEMETRY_OBSERVER", SeineTelemetryObserver())

    def poll(seconds):
        fixture.clock.seconds = seconds
        return inventory.collect_mining_status(
            fixture.config, system_name="Linux", requester=requester,
            process_factory=fixture.processes.__getitem__, boot_time_factory=lambda: 0.0,
            process_iter_factory=lambda: (), gpu_metrics=[],
            runner=lambda *args, **kwargs: SimpleNamespace(returncode=3, stdout="inactive", stderr=""),
        )

    return SimpleNamespace(bodies=bodies, endpoints=endpoints, requests=requests, poll=poll)


@pytest.mark.parametrize("pair", CPU_PAIRS, ids=lambda pair: "+".join(pair))
def test_independent_cpu_collector_preserves_role_ports_sessions_and_hashrates(tmp_path, monkeypatch, pair):
    fixture = _dual_fixture(tmp_path, monkeypatch, pair=pair, system_name="Linux")
    result = fixture.manager.apply(fixture.flight)
    status = _status_fixture(fixture, result, monkeypatch)
    for seconds, elapsed, hashes in ((0, 10, 1), (10, 15, 10), (20, 25, 60)):
        for index, role in enumerate(("primary", "secondary")):
            if pair[index] == "seine":
                status.bodies[role] = _seine_state(elapsed, hashes * (index + 1))
        current = status.poll(seconds)
    assert current["role_errors"] == {}, current["role_errors"]
    assert current["state"] == "running" and current["process_verified"] is True, current
    assert current["running_roles"] == ["primary", "secondary"] and current["role_errors"] == {}
    assert current["hashrate"] == {
        role: (5 * (index + 1) if pair[index] == "seine" else 100 * (index + 1))
        for index, role in enumerate(("primary", "secondary"))
    }
    details = {detail["role"]: detail for detail in current["hashrate_details"]}
    assert set(details) == {"primary", "secondary"}
    assert len({detail["session_id"] for detail in details.values()}) == 2
    assert current["shares"] is None  # Keep per-role difficulty/counters separate.
    assert set(status.requests) == set(status.endpoints.values())
    for index, role in enumerate(("primary", "secondary")):
        assert details[role]["coin"] == fixture.flight[role]["coin"]
        if pair[index] == "seine":
            assert details[role]["shares"] is None
            assert details[role]["workload_delta_work"] == 50 * (index + 1)
        else:
            assert details[role]["shares"] == {"accepted": index + 2, "rejected": 1}
    # Retained samples stay bound to each role instead of becoming new work.
    duplicate = status.poll(25)
    for detail in duplicate["hashrate_details"]:
        if detail["coin"] == "BNT":
            assert detail["workload_delta_work"] == 0
            assert detail["stats_updated_at"] == details[detail["role"]]["stats_updated_at"]
    fixture.manager.stop()


def test_same_seine_roles_workload_uses_each_native_delta_not_poll_gap(tmp_path, monkeypatch):
    fixture = _dual_fixture(tmp_path, monkeypatch, pair=("seine", "seine"), system_name="Linux")
    status = _status_fixture(fixture, fixture.manager.apply(fixture.flight), monkeypatch)
    tracker, state = PearlWorkloadCounters(), AgentState.new("dual-cpu", "synthetic-only-secret")

    def observe(seconds, primary, secondary):
        status.bodies.update(primary=_seine_state(*primary), secondary=_seine_state(*secondary))
        current = status.poll(seconds)
        reports = tracker.observe({"inventory": {"mining_status": current}}, state,
                                  now=BASE + timedelta(seconds=seconds))
        return current, reports

    observe(0, (10, 1), (10, 1))
    observe(10, (15, 10), (15, 10))
    _, first = observe(20, (25, 60), (25, 80))
    assert first[0]["estimated_work_total"] == "0"
    # Each miner advances one native second; collector polling takes25 seconds.
    # Native75+100 hashes are the work, independent of that25-second wall gap.
    _, fresh = observe(45, (26, 135), (26, 180))
    assert fresh[0]["estimated_work_total"] == "175"
    _, duplicate = observe(50, (26, 135), (26, 180))
    assert duplicate[0]["estimated_work_total"] == "175"
    fixture.manager.stop()
