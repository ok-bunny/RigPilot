"""Bounded workload delivery with temporary SQLite and synthetic samples only."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from unittest import mock

import pytest
import requests

from agent.client import ProtocolError, RigPilotClient
from agent.config import AgentConfig
from agent.pearl_workload import MAX_RETAINED_SERIES, MAX_SERIES, PearlWorkloadCounters
from agent.runtime import AgentRuntime
from agent.state import AgentState
from server.database import Database
from server.workload import acknowledged_heartbeat_counters, record_heartbeat_counters


BASE = datetime(2026, 10, 11, tzinfo=timezone.utc)


def _host(seconds, coin="C0", *, coins=None, native=False):
    moment = BASE + timedelta(seconds=seconds)
    details = [{"coin": value, "algorithm": "sharedhash", "hashrate": 100.0,
                "hashrate_unit": "H/s"} for value in (coins or [coin])]
    if native:
        details[0].update(workload_source="seine_stats_window", role="primary",
                          workload_sample_id=f"{seconds:064x}", workload_delta_work=7.0)
    return {"inventory": {"mining_status": {
        "state": "running", "process_verified": True, "session_id": "synthetic-session",
        "stats_updated_at": moment.isoformat(), "hashrate_details": details,
    }}}


def _observe(tracker, state, seconds, coin="C0", **kwargs):
    return tracker.observe(_host(seconds, coin, **kwargs), state,
                           now=BASE + timedelta(seconds=seconds))


@pytest.fixture
def ledger(tmp_path):
    database = Database(tmp_path / "workload.db")
    database.initialize("fixture", "synthetic-password")
    with database.connection() as db:
        db.execute("""INSERT INTO agents
            (id,secret_hash,name,hostname,os_family,platform,agent_version,enrolled_at)
            VALUES('fixture','unused','fixture','fixture','linux','synthetic','0',?)""",
                   (BASE.isoformat(),))
    return database


def _accept(database, reports, seconds):
    """Model ACK only for snapshots identical to the final accepted DB state."""
    assert len(reports) <= MAX_SERIES
    with database.connection() as db:
        record_heartbeat_counters(db, agent_id="fixture",
                                  received_at=(BASE + timedelta(seconds=seconds)).isoformat(),
                                  counters=reports)
        accepted = acknowledged_heartbeat_counters(db, agent_id="fixture", counters=reports)
    return accepted


def _total(database):
    with database.connection() as db:
        return db.execute("SELECT COALESCE(SUM(estimated_hashes),0) FROM device_work_buckets").fetchone()[0]


def _deliver(tracker, database, seconds):
    reports = tracker.heartbeat_reports()
    acks = _accept(database, reports, seconds)
    tracker.note_ack_support(True)
    # Real client maps normalized DB values back to the exact sent snapshot.
    from agent.client import _workload_ack_matches
    tracker.acknowledge([report for report in reports
                         if any(_workload_ack_matches(ack, report) for ack in acks)])
    return reports


@pytest.mark.parametrize("rotate", ["coin", "algorithm"])
def test_more_than_sixteen_sequential_series_are_counted_and_safely_retired(ledger, rotate):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    for index in range(33):
        for offset in (0, 10):
            seconds = index * 20 + offset
            host = _host(seconds, f"C{index}" if rotate == "coin" else "C0")
            if rotate == "algorithm":
                host["inventory"]["mining_status"]["hashrate_details"][0]["algorithm"] = f"hash-{index}"
            tracker.observe(host, state, now=BASE + timedelta(seconds=seconds))
            _deliver(tracker, ledger, seconds)
        assert len(tracker._counters) == 1
    assert _total(ledger) == pytest.approx(33000)


def test_offline_rotation_retains_all_final_increments_then_drains_bounded_batches(ledger):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    tracker.note_ack_support(True)
    for index in range(25):
        _observe(tracker, state, index * 20, f"C{index}")
        _observe(tracker, state, index * 20 + 10, f"C{index}")
        assert len(tracker.heartbeat_reports()) <= MAX_SERIES  # All requests fail.
    assert len(tracker._counters) == 25
    assert all(report["estimated_work_total"] == "1000"
               for report in state.workload_counter_state["counters"])
    for index in range(20):
        _deliver(tracker, ledger, 1000 + index)
        if len(tracker._counters) == 1:
            break
    assert len(tracker._counters) == 1
    assert _total(ledger) == pytest.approx(25000)


def test_lost_baseline_and_final_responses_retry_idempotently_without_losing_local_work(ledger):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    _observe(tracker, state, 0)
    baseline = tracker.heartbeat_reports()
    _accept(ledger, baseline, 0)  # Controller commits; response is lost.
    _observe(tracker, state, 10)
    assert tracker.heartbeat_reports() == baseline
    _deliver(tracker, ledger, 10)  # Duplicate baseline ACK releases local backlog.
    final = tracker.heartbeat_reports()
    assert final[0]["estimated_work_total"] == "1000"
    _accept(ledger, final, 11)  # Committed final response is also lost.
    _observe(tracker, state, 20, "NEW")
    assert "C0" in {report["coin"] for report in tracker.heartbeat_reports()}
    _deliver(tracker, ledger, 20)
    assert ("C0", "sharedhash") not in tracker._counters
    assert _total(ledger) == pytest.approx(1000)
    tracker.acknowledge(final)  # Late duplicate response cannot resurrect a counter.
    assert ("C0", "sharedhash") not in tracker._counters


def test_active_priority_reserves_history_slots_and_rotates_both_sets():
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    for index in range(20):
        _observe(tracker, state, index * 20, f"OLD{index}")
        _observe(tracker, state, index * 20 + 10, f"OLD{index}")
    active = [f"ACTIVE{index}" for index in range(64)]
    _observe(tracker, state, 400, coins=active)
    seen_active, seen_history = set(), set()
    for _ in range(6):
        batch = tracker.heartbeat_reports()
        assert len(batch) == 16
        current = {report["coin"] for report in batch if report["coin"].startswith("ACTIVE")}
        history = {report["coin"] for report in batch if report["coin"].startswith("OLD")}
        assert len(current) == 12
        assert len(history) == 4
        seen_active.update(current)
        seen_history.update(history)
    assert seen_active == set(active)
    assert len(seen_history) == 20


def test_return_to_retired_coin_starts_new_epoch_and_old_epoch_cannot_replay(ledger):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    _observe(tracker, state, 0)
    _deliver(tracker, ledger, 0)
    _observe(tracker, state, 10)
    old = _deliver(tracker, ledger, 10)[0]
    _observe(tracker, state, 20, "NEW")
    _deliver(tracker, ledger, 20)
    _observe(tracker, state, 30)
    returned = _deliver(tracker, ledger, 30)[0]
    assert returned["coin"] == "C0"
    assert returned["counter_epoch"] != old["counter_epoch"]
    assert returned["estimated_work_total"] == "0"
    _observe(tracker, state, 40)
    _deliver(tracker, ledger, 40)
    assert _accept(ledger, [old], 50) == []
    assert _total(ledger) == pytest.approx(2000)


def test_no_ack_or_conflicting_ack_cannot_retire_an_inactive_final_snapshot():
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    _observe(tracker, state, 0)
    tracker.heartbeat_reports()
    tracker.note_ack_support(False)
    _observe(tracker, state, 10)
    final = tracker.heartbeat_reports()[0]
    _observe(tracker, state, 20, "NEW")
    tracker.heartbeat_reports()
    for field, value in [("snapshot_seq", 0), ("estimated_work_total", "1"),
                         ("counter_epoch", "unrelated"), ("observed_at", BASE.isoformat())]:
        invalid = dict(final, **{field: value})
        tracker.acknowledge([invalid])
    tracker.acknowledge([])
    assert tracker._counters[("C0", "sharedhash")].total_work == Decimal(1000)


def test_capacity_keeps_unconfirmed_totals_and_emits_visible_status_and_warning(caplog):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    tracker.note_ack_support(False)
    for index in range(MAX_RETAINED_SERIES):
        _observe(tracker, state, index * 20, f"C{index}")
        _observe(tracker, state, index * 20 + 10, f"C{index}")
    before = deepcopy(state.workload_counter_state["counters"])
    _observe(tracker, state, 3000, "BLOCKED")
    _observe(tracker, state, 3010, "BLOCKED")
    assert state.workload_counter_state["counters"] == before
    assert state.workload_counter_state["status"] == {
        "capacity_exhausted": True, "blocked_series_count": 1,
        "retained_series": MAX_RETAINED_SERIES, "max_retained_series": MAX_RETAINED_SERIES,
    }
    assert len(tracker._counters) == MAX_RETAINED_SERIES
    assert len(tracker._active) == 1
    assert len(tracker.heartbeat_reports()) <= MAX_SERIES
    assert sum(record.message.startswith("workload counter capacity exhausted")
               for record in caplog.records) == 1
    # Confirming one final historical snapshot frees a slot for this active coin.
    tracker.acknowledge(tracker.heartbeat_reports())
    _observe(tracker, state, 3020, "BLOCKED")
    _observe(tracker, state, 3030, "BLOCKED")
    assert tracker._counters[("BLOCKED", "sharedhash")].total_work == Decimal(1000)
    assert not state.workload_counter_state["status"]["capacity_exhausted"]


def test_native_windows_keep_source_window_rules_across_ack_and_retirement(ledger):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    _observe(tracker, state, 0, "BNT", native=True)
    _deliver(tracker, ledger, 0)
    _observe(tracker, state, 10, "BNT", native=True)
    assert _deliver(tracker, ledger, 10)[0]["estimated_work_total"] == "7"
    _observe(tracker, state, 10, "BNT", native=True)
    _deliver(tracker, ledger, 11)
    _observe(tracker, state, 50, "BNT", native=True)  # Long gap only resets anchor.
    assert _deliver(tracker, ledger, 50)[0]["estimated_work_total"] == "7"
    _observe(tracker, state, 60, "OTHER")
    _deliver(tracker, ledger, 60)
    _observe(tracker, state, 70, "BNT", native=True)
    returned = _deliver(tracker, ledger, 70)[0]
    assert returned["estimated_work_total"] == "0"
    _observe(tracker, state, 80, "BNT", native=True)
    assert _deliver(tracker, ledger, 80)[0]["estimated_work_total"] == "7"
    assert _total(ledger) == pytest.approx(14)


def test_legacy_then_ack_upgrade_keeps_true_totals_and_never_rewinds_baseline(
    ledger, tmp_path, monkeypatch,
):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    client = RigPilotClient(AgentConfig(server_url="https://synthetic.test", state_file=tmp_path / "state"))
    replies = [b'{"commands":[]}']
    captured = []

    def request(*args, **kwargs):
        sent = kwargs["body"]["workload_counters"]
        captured.append(deepcopy(sent))
        acks = _accept(ledger, sent, len(captured) * 10)
        response = requests.Response()
        response.status_code = 200
        response._content = replies.pop(0) if replies else json.dumps({
            "commands": [], "workload_counter_acks": acks,
        }).encode()
        return response

    monkeypatch.setattr(client, "_request", request)

    def send():
        host = {"hostname": "fixture", "os_family": "linux", "platform": "synthetic",
                "ip_addresses": [], "metrics": {}, "inventory": {},
                "workload_counters": tracker.heartbeat_reports()}
        client.heartbeat(state, host)
        if isinstance(client.workload_ack_supported, bool):
            tracker.note_ack_support(client.workload_ack_supported)
        tracker.acknowledge(client.workload_counter_acks)

    _observe(tracker, state, 0)
    send()  # Successful legacy reply means cumulative mode, never confirmation.
    _observe(tracker, state, 10)
    send()  # Controller now supports ACK; this epoch must send 1000, never zero.
    _observe(tracker, state, 20)
    send()
    assert [batch[0]["estimated_work_total"] for batch in captured] == ["0", "1000", "2000"]
    assert _total(ledger) == pytest.approx(2000)
    # Subsequent new epochs on the now known ACK controller fence their baseline.
    _observe(tracker, state, 30, "NEW")
    _observe(tracker, state, 40, "NEW")
    send()
    assert captured[-1][0]["estimated_work_total"] == "0"
    send()
    assert captured[-1][0]["estimated_work_total"] == "1000"


def test_client_confirms_only_matching_snapshots_and_clears_ack_on_failed_or_legacy_reply(
    tmp_path, monkeypatch,
):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    _observe(tracker, state, 0)
    report = tracker.heartbeat_reports()[0]
    client = RigPilotClient(AgentConfig(server_url="https://synthetic.test", state_file=tmp_path / "state"))
    response = requests.Response()
    response.status_code = 200
    host = {"hostname": "fixture", "os_family": "linux", "platform": "synthetic",
            "ip_addresses": [], "metrics": {}, "inventory": {}, "workload_counters": [report]}
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: response)
    for invalid in [dict(report, snapshot_seq=True), dict(report, snapshot_seq=0.0),
                    dict(report, estimated_work_total=0), dict(report, estimated_work_total="0" * 129),
                    dict(report, estimated_work_total="NaN"), dict(report, observed_at=0),
                    dict(report, counter_epoch="other"), dict(report, coin="other"),
                    dict(report, observed_at="2026-10-11T00:00:00"), {"coin": "C0"}]:
        response._content = json.dumps({"commands": [], "workload_counter_acks": [invalid]}).encode()
        assert client.heartbeat(state, host) == []
        assert client.workload_counter_acks == []
    for timestamp in [BASE.isoformat(timespec="microseconds"), "2026-10-11T00:00:00Z",
                      "2026-10-11T08:00:00+08:00"]:
        normalized = dict(report, estimated_work_total="0.0", observed_at=timestamp)
        response._content = json.dumps({"commands": [], "workload_counter_acks": [normalized]}).encode()
        client.heartbeat(state, host)
        assert client.workload_counter_acks == [report]
    response._content = json.dumps({"commands": [], "stale_heartbeat": True,
                                   "workload_counter_acks": [normalized]}).encode()
    client.heartbeat(state, host)
    assert client.workload_counter_acks is None
    assert client.workload_ack_supported is None
    response._content = b'{"commands":[]}'
    client.heartbeat(state, host)
    assert client.workload_counter_acks is None
    response._content = json.dumps({"commands": [], "workload_counter_acks": [normalized]}).encode()
    client.heartbeat(state, host)
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        client.heartbeat(state, host)
    assert client.workload_counter_acks is None


def test_runtime_retries_lost_baseline_response_and_submits_full_local_backlog(
    ledger, tmp_path, monkeypatch,
):
    runtime = AgentRuntime(AgentConfig(server_url="https://synthetic.test",
                                       state_file=tmp_path / "state"))
    state = AgentState.new("fixture", "secret")
    samples = iter([dict(_host(seconds), hostname="fixture", os_family="linux",
                         platform="synthetic", ip_addresses=[], metrics={})
                    for seconds in (0, 10, 20)])
    runtime._host_data = lambda: next(samples)
    captured = []

    def request(*args, **kwargs):
        sent = kwargs["body"]["workload_counters"]
        captured.append(deepcopy(sent))
        acks = _accept(ledger, sent, len(captured) * 10)
        if len(captured) == 1:
            raise RuntimeError("response lost after commit")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"commands": [], "workload_counter_acks": acks}).encode()
        return response

    monkeypatch.setattr(runtime.client, "_request", request)
    with (mock.patch("agent.pearl_workload.datetime", wraps=datetime) as clock,
          mock.patch.object(runtime, "_reconcile_pending_agent_update")):
        clock.now.side_effect = [BASE + timedelta(seconds=seconds) for seconds in (0, 10, 20)]
        with pytest.raises(RuntimeError, match="response lost"):
            runtime.heartbeat_once(state)
        assert runtime.heartbeat_once(state) == 0
        assert runtime.heartbeat_once(state) == 0
    assert [batch[0]["estimated_work_total"] for batch in captured] == ["0", "0", "2000"]
    assert _total(ledger) == pytest.approx(2000)


@pytest.mark.parametrize("ambiguous", [
    b'', b'{"commands":[],"workload_counter_acks":null}',
    b'{"commands":[],"workload_counter_acks":{}}',
    b'{"commands":[],"workload_counter_acks":"invalid"}',
    b'{"commands":"invalid"}', b'not-json', b'{}',
    b'{"commands":[],"stale_heartbeat":true}',
    b'{"commands":[],"stale_heartbeat":true,"workload_counter_acks":[]}',
], ids=["empty", "null-acks", "object-acks", "string-acks", "bad-commands",
        "bad-json", "missing-commands", "stale-legacy", "stale-ack"])
def test_supported_new_epoch_keeps_zero_baseline_through_ambiguous_replies(
    ledger, tmp_path, monkeypatch, ambiguous,
):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    client = RigPilotClient(AgentConfig(server_url="https://synthetic.test", state_file=tmp_path / "state"))
    next_body = None
    sent = []

    def request(*args, **kwargs):
        reports = kwargs["body"]["workload_counters"]
        sent.append(deepcopy(reports))
        acks = _accept(ledger, reports, len(sent) * 10)
        response = requests.Response()
        response.status_code = 200
        response._content = next_body if next_body is not None else json.dumps({
            "commands": [], "workload_counter_acks": acks,
        }).encode()
        return response

    monkeypatch.setattr(client, "_request", request)

    def send():
        host = {"hostname": "fixture", "os_family": "linux", "platform": "synthetic",
                "ip_addresses": [], "metrics": {}, "inventory": {},
                "workload_counters": tracker.heartbeat_reports()}
        try:
            client.heartbeat(state, host)
        except ProtocolError:
            assert next_body in [b'{"commands":"invalid"}', b'not-json']
        else:
            if isinstance(client.workload_ack_supported, bool):
                tracker.note_ack_support(client.workload_ack_supported)
            tracker.acknowledge(client.workload_counter_acks)

    _observe(tracker, state, 0)
    send()
    _observe(tracker, state, 10)
    send()
    assert tracker._ack_supported is True
    _observe(tracker, state, 20, "NEW")
    _observe(tracker, state, 30, "NEW")
    baseline = tracker.heartbeat_reports()
    assert baseline[0]["estimated_work_total"] == "0"
    next_body = ambiguous
    send()
    assert client.workload_ack_supported is None
    assert tracker._ack_supported is True
    _observe(tracker, state, 40, "NEW")
    assert tracker.heartbeat_reports() == baseline
    assert tracker._counters[("NEW", "sharedhash")].total_work == Decimal(2000)
    next_body = None
    send()  # Confirm the possibly already committed zero baseline.
    send()  # Submit every accumulated interval after that baseline.
    assert sent[-1][0]["estimated_work_total"] == "2000"
    assert _total(ledger) == pytest.approx(3000)


def test_first_unknown_empty_response_does_not_claim_legacy_or_release_baseline(
    ledger, tmp_path, monkeypatch,
):
    runtime = AgentRuntime(AgentConfig(server_url="https://synthetic.test", state_file=tmp_path / "state"))
    state = AgentState.new("fixture", "secret")
    samples = iter([dict(_host(seconds), hostname="fixture", os_family="linux",
                         platform="synthetic", ip_addresses=[], metrics={})
                    for seconds in (0, 10, 20)])
    runtime._host_data = lambda: next(samples)
    captured = []

    def request(*args, **kwargs):
        reports = kwargs["body"]["workload_counters"]
        captured.append(deepcopy(reports))
        acks = _accept(ledger, reports, len(captured) * 10)
        response = requests.Response()
        response.status_code = 200
        response._content = b'' if len(captured) == 1 else json.dumps({
            "commands": [], "workload_counter_acks": acks,
        }).encode()
        return response

    monkeypatch.setattr(runtime.client, "_request", request)
    with (mock.patch("agent.pearl_workload.datetime", wraps=datetime) as clock,
          mock.patch.object(runtime, "_reconcile_pending_agent_update")):
        clock.now.side_effect = [BASE + timedelta(seconds=seconds) for seconds in (0, 10, 20)]
        assert runtime.heartbeat_once(state) == 0
        assert runtime._workload_counters._ack_supported is None
        assert runtime.heartbeat_once(state) == 0
        assert runtime.heartbeat_once(state) == 0
    assert [reports[0]["estimated_work_total"] for reports in captured] == ["0", "0", "2000"]
    assert _total(ledger) == pytest.approx(2000)


@pytest.mark.parametrize("native", [False, True], ids=["hashrate", "native-window"])
def test_returned_same_session_cached_sample_cannot_anchor_a_new_epoch(ledger, native):
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    coin = "BNT" if native else "C0"
    _observe(tracker, state, 0, coin, native=native)
    _deliver(tracker, ledger, 0)
    _observe(tracker, state, 10, coin, native=native)
    old = _deliver(tracker, ledger, 10)[0]
    stopped = _host(20, coin, native=native)
    stopped["inventory"]["mining_status"]["state"] = "stopped"
    tracker.observe(stopped, state, now=BASE + timedelta(seconds=20))
    assert not tracker._counters
    # The old process/session may still publish its last cached row on return.
    tracker.observe(_host(10, coin, native=native), state,
                    now=BASE + timedelta(seconds=30))
    assert not tracker._counters
    _observe(tracker, state, 40, coin, native=native)
    fresh = _deliver(tracker, ledger, 40)[0]
    assert fresh["counter_epoch"] != old["counter_epoch"]
    assert fresh["estimated_work_total"] == "0"
    _observe(tracker, state, 50, coin, native=native)
    final = _deliver(tracker, ledger, 50)[0]
    assert final["estimated_work_total"] == ("7" if native else "1000")
    assert _total(ledger) == pytest.approx(14 if native else 2000)


def test_retired_sample_anchors_and_eviction_floor_remain_bounded_and_expire():
    tracker, state = PearlWorkloadCounters(), AgentState.new("fixture", "secret")
    tracker.note_ack_support(True)
    for index in range(200):
        _observe(tracker, state, index / 10, f"C{index}")
        tracker.acknowledge(tracker.heartbeat_reports())
        _observe(tracker, state, index / 10 + 0.01, f"C{index}")
        tracker.acknowledge(tracker.heartbeat_reports())
        assert len(tracker._counters) == 1
        assert len(tracker._retired_samples) <= MAX_RETAINED_SERIES
    assert tracker._retired_sample_floor is not None
    # C0's exact anchor was evicted but its cached sample is still within 30s.
    assert ("C0", "sharedhash") not in tracker._retired_samples
    tracker.observe(_host(0.01), state, now=BASE + timedelta(seconds=25))
    assert not tracker._counters
    _observe(tracker, state, 25)
    assert tracker.reports()[0]["estimated_work_total"] == "0"
    _observe(tracker, state, 61)
    assert not tracker._retired_samples
    assert tracker._retired_sample_floor is None
