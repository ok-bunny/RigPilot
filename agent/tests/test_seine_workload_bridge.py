"""Real collector/observer/workload bridge with isolated HTTP/process fixtures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import inventory
from agent.config import AgentConfig
from agent.pearl_workload import PearlWorkloadCounters
from agent.process_identity import managed_argv_fingerprint
from agent.seine_telemetry import SeineTelemetryObserver
from agent.state import AgentState


BASE = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)
SYSTEM = "Windows" if os.name == "nt" else "Linux"


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.content = json.dumps(payload).encode()
        self.headers = {"Content-Length": str(len(self.content))}

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start+chunk_size]

    def close(self):
        pass


class _Process:
    def __init__(self, pid, executable, created, argv):
        self.pid, self.executable, self.created, self.argv = pid, executable, created, argv

    def is_running(self):
        return True

    def status(self):
        return "running"

    def exe(self):
        return str(self.executable)

    def create_time(self):
        return self.created

    def cmdline(self):
        return list(self.argv)


def _seine_state(elapsed=10, hashes=1, *, run_id=1, log_elapsed=None):
    return {"lifecycle": "running", "running": True, "run_id": run_id,
            "started_at_unix_ms": 1000,
            "effective_config": {"mode": "pool", "backend_specs": [{"kind": "cpu"}]},
            "metrics": {"accepted_blocks": 999, "submitted_blocks": 1000, "stale_submits": 3},
            "recent_logs": [{"tag": "STATS", "elapsed_secs": elapsed if log_elapsed is None else log_elapsed,
                "message": f"elapsed={elapsed}s hashes={hashes} rate=999999.0 H/s templates=1 submitted=7 stale=3 accepted=4 deferred=0 dropped=0"}]}


def _leg(miner_type):
    return {"miner_type": miner_type, "coin": "BNT" if miner_type == "seine" else "PEARL",
            "algorithm": "blocknet-argon2id" if miner_type == "seine" else "pearlhash",
            "pool_url": "stratum+tcp://pool.test:1234", "miner_profile": "default", "miner_package": None}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    clock = SimpleNamespace(seconds=0.0)

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz=None):
            result = BASE + timedelta(seconds=clock.seconds)
            return result.astimezone(tz) if tz is not None else result.replace(tzinfo=None)

    monkeypatch.setattr(inventory, "datetime", _Datetime)
    monkeypatch.setattr(inventory, "time", SimpleNamespace(
        monotonic=lambda: clock.seconds, time=lambda: BASE.timestamp()+clock.seconds))
    monkeypatch.setattr(inventory, "_SEINE_TELEMETRY_OBSERVER", SeineTelemetryObserver())

    def create(*, independent=False):
        root = tmp_path / ("independent" if independent else "single")
        root.mkdir()
        runtime, packages = root / "runtime", root / "packages"
        runtime.mkdir(); packages.mkdir()
        config = AgentConfig(server_url="https://controller.example.test", state_file=root / "state.json",
            managed_mining_enabled=True, managed_miner_runtime_dir=runtime,
            miner_packages_dir=packages, flight_sheet_file=root / "flight.json",
            miner_status_file=runtime / "miner-status.json", miner_service=None)
        document = {"version": 4 if independent else 3, "flight_sheet_id": "seine-bridge-sheet",
                    "name": "isolated bridge", "worker_name": "test-rig", "applied_at": BASE.isoformat(),
                    "primary": _leg("srbminer") if independent else _leg("seine"),
                    "secondary": _leg("seine") if independent else None,
                    "secondary_mode": "independent" if independent else None, "gpu_oc": None}
        config.flight_sheet_file.write_text(json.dumps(document))
        processes = {}
        roles = [("primary", document["primary"], 4242, 23456)]
        if independent:
            roles.append(("secondary", document["secondary"], 4243, 23457))
        for role, leg, pid, port in roles:
            miner = leg["miner_type"]
            destination = runtime / role if independent else runtime
            destination.mkdir(exist_ok=True)
            package = packages / f"builtin-{miner}-bridge"
            package.mkdir()
            basename = "seine" if miner == "seine" else "SRBMiner-MULTI"
            executable = package / (basename+".exe" if SYSTEM == "Windows" else basename)
            executable.write_bytes(b"fixture never executed")
            executable = executable.resolve()
            argv = [str(executable), "--isolated-fixture"]
            created = BASE.timestamp()-1000+(pid-4242)
            record = {"version": 2, "pid": pid, "executable": str(executable), "create_time": created,
                      "miner_type": miner, "flight_sheet_id": document["flight_sheet_id"],
                      "started_at": BASE.isoformat(), "survives_agent_restart": True,
                      "argv_sha256": managed_argv_fingerprint(argv, executable=executable), "api_port": port}
            (destination / "managed-miner-process.json").write_text(json.dumps(record))
            (destination / "miner-status.json").write_text(json.dumps({
                "version": 1, "state": "running", "miner_type": miner, "api_host": "127.0.0.1",
                "api_port": port, "hashrate": None, "shares": None, "updated_at": BASE.isoformat()}))
            if independent:
                (destination / "active-flight-sheet.json").write_text(json.dumps({
                    **document, "version": 3, "primary": leg, "secondary": None, "secondary_mode": None}))
            processes[pid] = _Process(pid, executable, created, argv)
        body = {"seine": _seine_state()}
        requests = []

        def requester(url, **kwargs):
            requests.append(url)
            assert kwargs["allow_redirects"] is False
            if url.endswith("/v1/runtime/state"):
                return _Response(deepcopy(body["seine"]))
            assert independent and url == "http://127.0.0.1:23456/"
            return _Response({"algorithms": [{"name": "pearlhash", "hashrate": {"now": 100+clock.seconds},
                                               "shares": {"accepted": 1, "rejected": 0}}]})

        def poll(seconds, payload=None):
            clock.seconds = seconds
            if payload is not None:
                body["seine"] = payload
            status = inventory.collect_mining_status(config, system_name=SYSTEM, requester=requester,
                process_factory=processes.__getitem__, boot_time_factory=lambda: 0,
                process_iter_factory=lambda: (), gpu_metrics=[],
                runner=lambda *args, **kwargs: SimpleNamespace(returncode=3, stdout="inactive", stderr=""))
            assert status["state"] == "running" and status["process_verified"] is True, status
            assert any(url.endswith("/v1/runtime/state") for url in requests)
            return status

        return poll

    return create


def _detail(status):
    return next(row for row in status["hashrate_details"] if row.get("coin") == "BNT")


def _counter(reports, coin="BNT"):
    return next((row for row in reports if row["coin"] == coin), None)


@pytest.mark.parametrize("independent", [False, True], ids=["single-cpu", "gpu-primary-cpu-secondary"])
def test_real_collector_keeps_seine_time_and_deduplicates_native_window_work(bridge, independent):
    poll = bridge(independent=independent)
    tracker, state = PearlWorkloadCounters(), AgentState.new("local-bridge", "test-only-secret")

    def observe(seconds, payload=None):
        mining = poll(seconds, payload)
        reports = tracker.observe({"inventory": {"mining_status": mining}}, state,
                                  now=BASE+timedelta(seconds=seconds))
        return mining, reports

    first, reports = observe(0)
    assert _detail(first)["hashrate"] is None and _counter(reports) is None
    baseline, reports = observe(10, _seine_state(15, 10))
    assert _detail(baseline)["hashrate"] is None and _counter(reports) is None
    sample, reports = observe(20, _seine_state(25, 60))
    detail = _detail(sample)
    assert detail["hashrate"] == 5 and detail["shares"] is None
    assert detail["workload_source"] == "seine_stats_window"
    assert detail["stats_updated_at"] == (BASE+timedelta(seconds=20)).isoformat()
    assert _counter(reports)["estimated_work_total"] == "0"
    fixed_time, fixed_id, fixed_session = detail["stats_updated_at"], detail["workload_sample_id"], detail["session_id"]

    for seconds in (30, 40):
        duplicate, reports = observe(seconds)
        current = _detail(duplicate)
        assert current["stats_updated_at"] == fixed_time
        assert current["workload_sample_id"] == fixed_id and current["session_id"] == fixed_session
        assert Decimal(str(current["workload_delta_work"])) == 0
        assert _counter(reports)["estimated_work_total"] == "0"
        if independent:
            assert current["role"] == "secondary"
            assert duplicate["stats_updated_at"] != current["stats_updated_at"]
            assert Decimal(_counter(reports, "PEARL")["estimated_work_total"]) > 0

    # 75 native hashes during a 1s STATS interval, observed25 wall seconds
    # after the prior window. Neither75*25 nor a rate trapezoid is valid work.
    next_sample, reports = observe(45, _seine_state(26, 135))
    assert _detail(next_sample)["hashrate"] == 75
    assert Decimal(str(_detail(next_sample)["workload_delta_work"])) == 75
    assert _counter(reports)["estimated_work_total"] == "75"
    _, duplicate_reports = observe(50)
    assert _counter(duplicate_reports)["estimated_work_total"] == "75"


def test_real_collector_new_run_unseen_retained_log_cannot_publish_rate_or_work(bridge):
    poll = bridge()
    tracker, state = PearlWorkloadCounters(), AgentState.new("run-bridge", "test-only-secret")
    previous_session = None
    for seconds, payload in [(0, _seine_state()), (10, _seine_state(15, 10)), (20, _seine_state(25, 60))]:
        mining = poll(seconds, payload)
        tracker.observe({"inventory": {"mining_status": mining}}, state, now=BASE+timedelta(seconds=seconds))
        previous_session = _detail(mining).get("session_id")
    # This old10s/1hash log has a different, previously unseen log timestamp.
    old = poll(21, _seine_state(10, 1, run_id=2, log_elapsed=100))
    assert _detail(old)["hashrate"] is None
    reports = tracker.observe({"inventory": {"mining_status": old}}, state,
                              now=BASE+timedelta(seconds=21))
    assert _counter(reports)["estimated_work_total"] == "0"
    first_new = poll(22, _seine_state(15, 10, run_id=2, log_elapsed=105))
    assert _detail(first_new)["hashrate"] is None
    reports = tracker.observe({"inventory": {"mining_status": first_new}}, state,
                              now=BASE+timedelta(seconds=22))
    assert _counter(reports)["estimated_work_total"] == "0"
    actual_new = poll(23, _seine_state(20, 30, run_id=2, log_elapsed=110))
    assert _detail(actual_new)["hashrate"] == 4
    assert _detail(actual_new)["session_id"] != previous_session
    reports = tracker.observe({"inventory": {"mining_status": actual_new}}, state,
                              now=BASE+timedelta(seconds=23))
    assert _counter(reports)["estimated_work_total"] == "0"
