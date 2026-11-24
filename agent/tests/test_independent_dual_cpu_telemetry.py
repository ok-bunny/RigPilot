"""Actual managed collectors with two owned CPU processes and isolated IO."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from agent import inventory
from agent.config import AgentConfig
from agent.pearl_workload import PearlWorkloadCounters
from agent.process_identity import managed_argv_fingerprint
from agent.seine_telemetry import SeineTelemetryObserver
from agent.state import AgentState
from agent.tests.test_seine_workload_bridge import BASE, SYSTEM, _Process, _Response, _seine_state


@pytest.fixture
def dual_cpu(tmp_path, monkeypatch):
    clock = SimpleNamespace(seconds=0)

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = BASE + timedelta(seconds=clock.seconds)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(inventory, "datetime", _Datetime)
    monkeypatch.setattr(inventory, "time", SimpleNamespace(
        monotonic=lambda: clock.seconds, time=lambda: BASE.timestamp()+clock.seconds))
    monkeypatch.setattr(inventory, "_SEINE_TELEMETRY_OBSERVER", SeineTelemetryObserver())

    def create(types):
        root = tmp_path / "-".join(types)
        root.mkdir()
        runtime, packages = root / "runtime", root / "packages"
        runtime.mkdir(); packages.mkdir()
        config = AgentConfig(server_url="https://controller.example.test", state_file=root / "state.json",
            managed_mining_enabled=True, managed_miner_runtime_dir=runtime,
            miner_packages_dir=packages, flight_sheet_file=root / "flight.json",
            miner_status_file=runtime / "miner-status.json", miner_service=None)
        roles = ("primary", "secondary")
        legs = [{"miner_type": miner, "coin": "BNT" if miner == "seine" else "XMR",
                 "algorithm": "blocknet-argon2id" if miner == "seine" else "randomx",
                 "pool_url": "stratum+tcp://pool.test:1234", "miner_profile": "default",
                 "miner_package": None} for miner in types]
        document = {"version": 4, "flight_sheet_id": "dual-cpu-telemetry", "name": "isolated",
                    "worker_name": "test-rig", "applied_at": BASE.isoformat(), "gpu_oc": None,
                    "primary": legs[0], "secondary": legs[1], "secondary_mode": "independent"}
        config.flight_sheet_file.write_text(json.dumps(document))
        processes, bodies, requests = {}, {}, []
        for index, (role, miner, leg) in enumerate(zip(roles, types, legs)):
            destination = runtime / role
            destination.mkdir()
            package = packages / ("builtin-" + miner)
            package.mkdir(exist_ok=True)
            executable = (package / (miner + (".exe" if SYSTEM == "Windows" else ""))).resolve()
            executable.write_bytes(b"fixture never executed")
            pid, port, created = 4242+index, 23456+index, BASE.timestamp()-1000+index
            argv = [str(executable), "--isolated-fixture", role]
            record = {"version": 2, "pid": pid, "executable": str(executable), "create_time": created,
                      "miner_type": miner, "flight_sheet_id": document["flight_sheet_id"],
                      "started_at": BASE.isoformat(), "survives_agent_restart": True,
                      "argv_sha256": managed_argv_fingerprint(argv, executable=executable), "api_port": port}
            (destination / "managed-miner-process.json").write_text(json.dumps(record))
            (destination / "miner-status.json").write_text(json.dumps({
                "version": 1, "state": "running", "miner_type": miner, "api_host": "127.0.0.1",
                "api_port": port, "hashrate": None, "shares": None, "updated_at": BASE.isoformat()}))
            (destination / "active-flight-sheet.json").write_text(json.dumps({
                **document, "version": 3, "primary": leg, "secondary": None, "secondary_mode": None}))
            processes[pid] = _Process(pid, executable, created, argv)
            bodies[role] = (_seine_state() if miner == "seine" else
                            {"hashrate": {"total": [100*(index+1), None, None]},
                             "results": {"shares_good": index+1, "shares_total": index+2}})

        def requester(url, **kwargs):
            requests.append(url)
            assert kwargs["allow_redirects"] is False
            for index, role in enumerate(roles):
                path = "/v1/runtime/state" if types[index] == "seine" else "/2/summary"
                if url == f"http://127.0.0.1:{23456+index}{path}":
                    return _Response(deepcopy(bodies[role]))
            pytest.fail(f"unexpected telemetry URL {url}")

        def poll(seconds, **changes):
            clock.seconds = seconds
            bodies.update(changes)
            status = inventory.collect_mining_status(config, system_name=SYSTEM, requester=requester,
                process_factory=processes.__getitem__, boot_time_factory=lambda: 0,
                process_iter_factory=lambda: (), gpu_metrics=[],
                runner=lambda *args, **kwargs: SimpleNamespace(returncode=3, stdout="inactive", stderr=""))
            assert status["state"] == "running" and status["process_verified"] is True, status
            assert status["running_roles"] == list(roles) and not status["degraded"]
            return status

        return SimpleNamespace(poll=poll, requests=requests, bodies=bodies)

    return create


def _rows(status):
    return {row["role"]: row for row in status["hashrate_details"]}


def _work(tracker, state, status, seconds):
    return {row["coin"]: row for row in tracker.observe(
        {"inventory": {"mining_status": status}}, state, now=BASE+timedelta(seconds=seconds))}


@pytest.mark.parametrize("types", [("xmrig", "seine"), ("seine", "xmrig"),
                                  ("xmrig", "xmrig"), ("seine", "seine")])
def test_collector_keeps_each_owned_cpu_role_api_session_and_shares(dual_cpu, types):
    bridge = dual_cpu(types)
    bridge.poll(0)
    bridge.poll(10, **{role: _seine_state(15, 10) for role, miner in zip(("primary", "secondary"), types)
                      if miner == "seine"})
    status = bridge.poll(20, **{role: _seine_state(25, 60) for role, miner in zip(("primary", "secondary"), types)
                               if miner == "seine"})
    rows = _rows(status)
    assert set(rows) == {"primary", "secondary"}
    assert rows["primary"]["session_id"] != rows["secondary"]["session_id"]
    assert status["hashrate"] == {role: 5 if miner == "seine" else 100*(index+1)
                                 for index, (role, miner) in enumerate(zip(rows, types))}
    assert status["shares"] is None
    for index, (role, miner) in enumerate(zip(rows, types)):
        assert rows[role]["coin"] == ("BNT" if miner == "seine" else "XMR")
        assert rows[role]["stats_updated_at"] == (BASE+timedelta(seconds=20)).isoformat()
        assert rows[role]["shares"] == (None if miner == "seine" else
                                        {"accepted": index+1, "rejected": 1})
        path = "/v1/runtime/state" if miner == "seine" else "/2/summary"
        assert f"http://127.0.0.1:{23456+index}{path}" in bridge.requests


def test_two_xmrig_cpu_roles_add_to_one_coin_counter_without_duplicate_poll_work(dual_cpu):
    bridge = dual_cpu(("xmrig", "xmrig"))
    tracker, state = PearlWorkloadCounters(), AgentState.new("dual-xmrig", "test-only")
    first = _work(tracker, state, bridge.poll(0), 0)["XMR"]
    sample = bridge.poll(10)
    counted = _work(tracker, state, sample, 10)["XMR"]
    assert counted["estimated_work_total"] == "3000"
    assert counted["counter_epoch"] == first["counter_epoch"]
    assert _work(tracker, state, sample, 10)["XMR"] == counted


def test_one_cpu_role_refresh_does_not_redate_or_revive_other_roles_old_stats(dual_cpu):
    bridge = dual_cpu(("xmrig", "seine"))
    tracker, state = PearlWorkloadCounters(), AgentState.new("dual-distinct", "test-only")
    for seconds, changes in [(0, {}), (10, {"secondary": _seine_state(15, 10)}),
                             (20, {"secondary": _seine_state(25, 60)})]:
        _work(tracker, state, bridge.poll(seconds, **changes), seconds)
    old = _rows(bridge.poll(25))["secondary"]
    after = _work(tracker, state, bridge.poll(30, secondary=_seine_state(26, 135)), 30)
    assert after["BNT"]["estimated_work_total"] == "75"
    for seconds in (40, 50, 61):
        status = bridge.poll(seconds)
        assert _rows(status)["secondary"]["stats_updated_at"] == (BASE+timedelta(seconds=30)).isoformat()
        reports = _work(tracker, state, status, seconds)
        assert reports["BNT"]["estimated_work_total"] == "75"
        assert Decimal(reports["XMR"]["estimated_work_total"]) > Decimal(after["XMR"]["estimated_work_total"])
    fresh = bridge.poll(62, secondary=_seine_state(27, 145))
    assert _rows(fresh)["secondary"]["session_id"] == old["session_id"]
    assert _work(tracker, state, fresh, 62)["BNT"]["estimated_work_total"] == "75"


def test_two_seine_same_coin_add_each_native_window_once_with_independent_clocks(dual_cpu):
    bridge = dual_cpu(("seine", "seine"))
    tracker, state = PearlWorkloadCounters(), AgentState.new("dual-seine", "test-only")
    for seconds, payload in [(0, _seine_state()), (10, _seine_state(15, 10)),
                             (20, _seine_state(25, 60))]:
        reports = _work(tracker, state, bridge.poll(seconds, primary=payload, secondary=payload), seconds)
    assert reports["BNT"]["estimated_work_total"] == "0"
    # 75 hashes over one native second, primary only. The collector waited10s;
    # neither a summed-rate trapezoid nor replaying the secondary50 is valid.
    primary_new = bridge.poll(30, primary=_seine_state(26, 135))
    assert _rows(primary_new)["primary"]["stats_updated_at"] != _rows(primary_new)["secondary"]["stats_updated_at"]
    assert _work(tracker, state, primary_new, 30)["BNT"]["estimated_work_total"] == "75"
    secondary_new = bridge.poll(40, secondary=_seine_state(27, 85))
    assert _work(tracker, state, secondary_new, 40)["BNT"]["estimated_work_total"] == "100"
    assert _work(tracker, state, bridge.poll(45), 45)["BNT"]["estimated_work_total"] == "100"


def _prime_two_seine(bridge, tracker, state):
    for seconds, payload in [(0, _seine_state()), (10, _seine_state(15, 10)),
                             (20, _seine_state(25, 60))]:
        result = _work(tracker, state, bridge.poll(seconds, primary=payload, secondary=payload), seconds)
    return result


def test_same_native_event_fingerprint_from_distinct_seine_roles_is_additive(dual_cpu):
    bridge = dual_cpu(("seine", "seine"))
    tracker, state = PearlWorkloadCounters(), AgentState.new("same-seine-event", "test-only")
    _prime_two_seine(bridge, tracker, state)
    status = bridge.poll(45, primary=_seine_state(26, 135), secondary=_seine_state(26, 135))
    rows = _rows(status)
    assert rows["primary"]["workload_sample_id"] == rows["secondary"]["workload_sample_id"]
    assert rows["primary"]["session_id"] != rows["secondary"]["session_id"]
    reports = _work(tracker, state, status, 45)
    assert reports["BNT"]["estimated_work_total"] == "150"
    assert _work(tracker, state, bridge.poll(46), 46) == reports


def test_one_seine_role_run_restart_does_not_reset_or_replay_other_roles_work(dual_cpu):
    bridge = dual_cpu(("seine", "seine"))
    tracker, state = PearlWorkloadCounters(), AgentState.new("seine-run-reset", "test-only")
    _prime_two_seine(bridge, tracker, state)
    initial = bridge.poll(30, primary=_seine_state(26, 135), secondary=_seine_state(26, 135))
    _work(tracker, state, initial, 30)
    old_sessions = {role: row["session_id"] for role, row in _rows(initial).items()}
    restarted = bridge.poll(31, primary=_seine_state(10, 1, run_id=2, log_elapsed=100),
                            secondary=_seine_state(28, 145))
    assert _rows(restarted)["primary"]["hashrate"] is None
    assert _work(tracker, state, restarted, 31)["BNT"]["estimated_work_total"] == "160"
    assert _work(tracker, state, bridge.poll(32, primary=_seine_state(15, 10, run_id=2, log_elapsed=105)), 32)["BNT"]["estimated_work_total"] == "160"
    fresh = bridge.poll(33, primary=_seine_state(20, 30, run_id=2, log_elapsed=110),
                       secondary=_seine_state(29, 152))
    rows = _rows(fresh)
    assert rows["primary"]["session_id"] != old_sessions["primary"]
    assert rows["secondary"]["session_id"] == old_sessions["secondary"]
    assert _work(tracker, state, fresh, 33)["BNT"]["estimated_work_total"] == "167"
    assert _work(tracker, state, bridge.poll(34, primary=_seine_state(21, 40, run_id=2, log_elapsed=111)), 34)["BNT"]["estimated_work_total"] == "177"


def test_expired_seine_source_requires_new_anchor_while_other_cpu_role_continues(dual_cpu):
    bridge = dual_cpu(("seine", "seine"))
    tracker, state = PearlWorkloadCounters(), AgentState.new("seine-idle-source", "test-only")
    _prime_two_seine(bridge, tracker, state)
    _work(tracker, state, bridge.poll(30, primary=_seine_state(26, 135), secondary=_seine_state(26, 135)), 30)
    # Neither the long primary gap nor the expired secondary display earns work.
    assert _work(tracker, state, bridge.poll(61, primary=_seine_state(27, 140)), 61)["BNT"]["estimated_work_total"] == "150"
    assert _work(tracker, state, bridge.poll(62, primary=_seine_state(28, 150)), 62)["BNT"]["estimated_work_total"] == "160"
    recovered = bridge.poll(63, secondary=_seine_state(27, 145))
    assert _work(tracker, state, recovered, 63)["BNT"]["estimated_work_total"] == "160"
    assert _work(tracker, state, bridge.poll(64, secondary=_seine_state(28, 165)), 64)["BNT"]["estimated_work_total"] == "180"


@pytest.mark.parametrize("invalid", [{"workload_sample_id": "bad"}, {"workload_delta_work": float("inf")}])
def test_invalid_native_window_never_falls_back_to_display_rate_integration(dual_cpu, invalid):
    bridge = dual_cpu(("seine", "seine"))
    tracker, state = PearlWorkloadCounters(), AgentState.new("seine-invalid-source", "test-only")
    _prime_two_seine(bridge, tracker, state)
    status = bridge.poll(30, primary=_seine_state(26, 135), secondary=_seine_state(27, 85))
    _rows(status)["primary"].update(invalid)
    assert _work(tracker, state, status, 30)["BNT"]["estimated_work_total"] == "25"
    # The bad source lost continuity; its next valid window is a baseline only.
    assert _work(tracker, state, bridge.poll(31, primary=_seine_state(27, 145)), 31)["BNT"]["estimated_work_total"] == "25"
