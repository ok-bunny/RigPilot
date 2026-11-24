"""Synthetic heartbeat samples covering per-series spike rejection and recovery."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from test_059_backend import app_client, auth, enroll, heartbeat, login  # noqa: F401
from server.main import _hashrate_series_identity


def _running_status(details: list[dict], *, unit: str = "H/s") -> dict:
    sampled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "configured": True,
        "state": "running",
        "process_verified": True,
        "checked_at": sampled_at,
        "stats_updated_at": sampled_at,
        "primary": {"coin": details[0]["coin"], "algorithm": details[0]["algorithm"]},
        "hashrate_details": details,
        "hashrate_unit": unit,
    }


def _detail(value_hps: float, *, coin: str = "PEARL", algorithm: str = "pearlhash",
            role: str = "primary", unit: str = "H/s") -> dict:
    return {
        "role": role,
        "coin": coin,
        "algorithm": algorithm,
        "hashrate": value_hps,
        "hashrate_unit": unit,
    }


def _series(coin: str, algorithm: str, role: str = "primary") -> str:
    return _hashrate_series_identity(role, coin, algorithm)[0]


def _bucket_sample(rate: float, coin: str, algorithm: str, role: str = "primary") -> dict:
    return {
        "series": _series(coin, algorithm, role),
        "role": role,
        "coin": coin,
        "algorithm": algorithm,
        "hashrate": rate,
        "unit": "H/s",
    }


def _insert_bucket(client, agent_id: str, samples: list[dict], bucket_start: datetime,
                   recorded_at: datetime | None = None) -> None:
    with client.app.state.db.connection() as db:
        db.execute(
            "INSERT INTO hashrate_history_buckets(agent_id,bucket_start,recorded_at,samples_json)"
            " VALUES(?,?,?,?)"
            " ON CONFLICT(agent_id,bucket_start) DO UPDATE SET"
            "   recorded_at=excluded.recorded_at,samples_json=excluded.samples_json",
            (
                agent_id,
                bucket_start.isoformat(timespec="seconds"),
                (recorded_at or bucket_start).isoformat(timespec="seconds"),
                json.dumps(samples, separators=(",", ":"), ensure_ascii=False),
            ),
        )


def _stored_samples(client, agent_id: str) -> list[dict]:
    with client.app.state.db.connection() as db:
        row = db.execute(
            "SELECT metrics_json FROM telemetry WHERE agent_id=? ORDER BY id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    metrics = json.loads(row["metrics_json"])
    return metrics.get("hashrate_samples", [])


def _rejection_audits(client, token: str, agent_id: str) -> list[dict]:
    entries = client.get(f"/api/audit?agent_id={agent_id}", headers=auth(token)).json()
    return [
        entry for entry in entries
        if entry["action"] == "hashrate_spike_rejected"
    ]


def _bucket_row(client, agent_id: str, bucket_start: datetime) -> tuple | None:
    with client.app.state.db.connection() as db:
        row = db.execute(
            "SELECT recorded_at,samples_json FROM hashrate_history_buckets"
            " WHERE agent_id=? AND bucket_start=?",
            (agent_id, bucket_start.isoformat(timespec="seconds")),
        ).fetchone()
    return (row["recorded_at"], row["samples_json"]) if row else None


def _now_bucket_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)


def test_normal_th_level_heartbeats_are_never_rejected(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-normal-th")
    for rate in (128.0, 144.0, 132.0, 136.0, 140.0):
        status = _running_status([_detail(rate, unit="TH/s")])
        response = heartbeat(client, agent_id, secret, status)
        assert response.status_code == 200, response.text
        stored = _stored_samples(client, agent_id)
        assert len(stored) == 1
        assert stored[0]["hashrate"] == rate * 1e12
    assert _rejection_audits(client, token, agent_id) == []


def test_absolute_cap_refuses_first_heartbeat_ph_sample(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-first-ph")
    status = _running_status([_detail(9.0, unit="PH/s")])
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    assert _stored_samples(client, agent_id) == []
    rejections = _rejection_audits(client, token, agent_id)
    assert len(rejections) == 1
    details = rejections[0]["details"]
    assert details["rejected_samples"][0]["rule"] == "absolute_cap"
    assert details["rejected_samples"][0]["hashrate"] == 9.0e15
    assert details["rejected_samples"][0]["coin"] == "PEARL"
    assert details["accepted_samples"] == 0
    # No bucket row is created for a refused-only heartbeat.
    assert _bucket_row(client, agent_id, _now_bucket_start()) is None


def test_first_sample_below_cap_passes_without_baseline(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-first-subcap")
    # An initial value below the cap needs no baseline.
    status = _running_status([_detail(2.0e15)])
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    stored = _stored_samples(client, agent_id)
    assert stored and stored[0]["hashrate"] == 2.0e15
    assert _rejection_audits(client, token, agent_id) == []


def test_legitimate_ph_level_rig_is_never_clipped(app_client):  # noqa: F811
    """A stable high-rate rig remains below the absolute cap."""
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-ph-rig")
    base = _now_bucket_start()
    for offset, rate in ((9, 1.25e15), (6, 1.30e15), (3, 1.35e15)):
        _insert_bucket(
            client, agent_id,
            [_bucket_sample(rate, "PEARL", "pearlhash")],
            base - timedelta(minutes=5 * offset),
        )
    for rate in (1.24e15, 1.27e15, 1.32e15):
        status = _running_status([_detail(rate)])
        response = heartbeat(client, agent_id, secret, status)
        assert response.status_code == 200, response.text
        stored = _stored_samples(client, agent_id)
        assert stored and stored[0]["hashrate"] == rate
    assert _rejection_audits(client, token, agent_id) == []
    # An above-cap spike is still rejected
    # by the absolute cap even though it sits inside the 10x band.
    status = _running_status([_detail(6.0e15)])
    assert heartbeat(client, agent_id, secret, status).status_code == 200
    assert _stored_samples(client, agent_id) == []
    assert len(_rejection_audits(client, token, agent_id)) == 1


def test_baseline_band_refuses_sub_cap_spike(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-cpu-band")
    base = _now_bucket_start()
    cpu = [_bucket_sample(85_000.0, "XMR", "randomx")]
    for offset in (4, 3, 2):
        _insert_bucket(client, agent_id, cpu, base - timedelta(minutes=5 * offset))
    # 5 MH/s is below the 1 PH/s absolute cap but 58x the device baseline.
    status = _running_status([_detail(5_000_000.0, coin="XMR", algorithm="randomx")])
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    assert _stored_samples(client, agent_id) == []
    rejections = _rejection_audits(client, token, agent_id)
    assert len(rejections) == 1
    details = rejections[0]["details"]
    assert details["rejected_samples"][0]["rule"] == "baseline_band"
    assert details["rejected_samples"][0]["baseline_hps"] == 85_000.0
    assert details["rejected_samples"][0]["threshold_hps"] == 850_000.0


def test_partial_rejection_keeps_healthy_series(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-partial")
    base = _now_bucket_start()
    gpu = _bucket_sample(200e12, "PEARL", "pearlhash")
    for offset in (10, 5, 0):
        _insert_bucket(client, agent_id, [gpu], base - timedelta(minutes=5 * offset))
    status = _running_status(
        [
            _detail(7.2, unit="PH/s"),
            _detail(12_500.0, coin="XMR", algorithm="randomx", role="secondary"),
        ]
    )
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    kept = _stored_samples(client, agent_id)
    assert [(item["coin"], item["hashrate"]) for item in kept] == [("XMR", 12_500.0)]
    rejections = _rejection_audits(client, token, agent_id)
    details = rejections[0]["details"]
    assert details["accepted_samples"] == 1
    assert len(details["rejected_samples"]) == 1
    assert details["rejected_samples"][0]["coin"] == "PEARL"
    # The bucket is rewritten with the healthy series only.
    row = _bucket_row(client, agent_id, _now_bucket_start())
    assert row is not None
    assert {item["coin"] for item in json.loads(row[1])} == {"XMR"}


def test_all_refused_heartbeat_preserves_bucket_last_good_snapshot(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-keep-good")
    current = _now_bucket_start()
    good = [_bucket_sample(205e12, "PEARL", "pearlhash")]
    _insert_bucket(
        client, agent_id, good, current,
        recorded_at=datetime.now(timezone.utc) - timedelta(seconds=90),
    )
    before = _bucket_row(client, agent_id, current)
    status = _running_status([_detail(12.0, unit="PH/s")])
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    assert _bucket_row(client, agent_id, current) == before
    assert _stored_samples(client, agent_id) == []
    assert len(_rejection_audits(client, token, agent_id)) == 1


def test_zero_baseline_allows_full_restart_rate(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-restart")
    base = _now_bucket_start()
    stopped = [_bucket_sample(0.0, "PEARL", "pearlhash")]
    for offset in (3, 2, 1):
        _insert_bucket(client, agent_id, stopped, base - timedelta(minutes=5 * offset))
    status = _running_status([_detail(220, unit="TH/s")])
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    stored = _stored_samples(client, agent_id)
    assert stored and stored[0]["hashrate"] == 220e12
    assert _rejection_audits(client, token, agent_id) == []


def test_new_series_first_sample_only_faces_absolute_cap(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-new-series")
    base = _now_bucket_start()
    pearl = [_bucket_sample(200e12, "PEARL", "pearlhash")]
    for offset in (6, 4, 2):
        _insert_bucket(client, agent_id, pearl, base - timedelta(minutes=5 * offset))
    # A brand new coin/algorithm series at 900 TH/s has no baseline and is
    # under the cap, so it must pass even though the device's other series
    # mines at 200 TH/s.
    status = _running_status(
        [
            _detail(200, unit="TH/s"),
            _detail(900, unit="TH/s", coin="QUANTUS", algorithm="qubic", role="secondary"),
        ]
    )
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    kept = {(item["coin"], item["hashrate"]) for item in _stored_samples(client, agent_id)}
    assert kept == {("PEARL", 200e12), ("QUANTUS", 900e12)}
    assert _rejection_audits(client, token, agent_id) == []


def test_poisoned_history_cannot_raise_the_baseline(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-poisoned")
    base = _now_bucket_start()
    for offset, rate in ((4, 9.0e15), (3, 6.0e15), (2, 12.0e15), (1, 200e12)):
        _insert_bucket(
            client, agent_id,
            [_bucket_sample(rate, "PEARL", "pearlhash")],
            base - timedelta(minutes=5 * offset),
        )
    status = _running_status([_detail(6.4, unit="PH/s")])
    response = heartbeat(client, agent_id, secret, status)
    assert response.status_code == 200, response.text
    assert _stored_samples(client, agent_id) == []
    rejections = _rejection_audits(client, token, agent_id)
    assert rejections[0]["details"]["rejected_samples"][0]["rule"] == "absolute_cap"
    # A return to the real rate is accepted immediately.
    status = _running_status([_detail(198, unit="TH/s")])
    assert heartbeat(client, agent_id, secret, status).status_code == 200
    stored = _stored_samples(client, agent_id)
    assert stored and stored[0]["hashrate"] == 198e12


def test_workload_counters_survive_hashrate_rejection(app_client):  # noqa: F811
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "spike-workload")
    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counters = [
        {
            "coin": "PEARL",
            "algorithm": "pearlhash",
            "counter_epoch": "epoch-1",
            "snapshot_seq": 1,
            "estimated_work_total": 0,
            "observed_at": observed,
        }
    ]
    first = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={
            "hostname": f"{agent_id}.local",
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": "0.8.55",
            "ip_addresses": [],
            "metrics": {},
            "inventory": {"mining_status": _running_status([_detail(200, unit="TH/s")])},
            "workload_counters": counters,
        },
    )
    assert first.status_code == 200, first.text
    counters[0] = {**counters[0], "snapshot_seq": 2, "estimated_work_total": "6e13"}
    response = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={
            "hostname": f"{agent_id}.local",
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": "0.8.55",
            "ip_addresses": [],
            "metrics": {},
            "inventory": {"mining_status": _running_status([_detail(8.0, unit="PH/s")])},
            "workload_counters": counters,
        },
    )
    assert response.status_code == 200, response.text
    assert _stored_samples(client, agent_id) == []
    assert len(_rejection_audits(client, token, agent_id)) == 1
    # The hashrate refusal must not touch the independent delta-work ledger.
    with client.app.state.db.connection() as db:
        rows = db.execute(
            "SELECT estimated_hashes FROM device_work_buckets WHERE agent_id=?",
            (agent_id,),
        ).fetchall()
        state = db.execute(
            "SELECT last_total FROM device_work_state WHERE agent_id=?",
            (agent_id,),
        ).fetchall()
    assert rows and rows[0]["estimated_hashes"] == 6.0e13
    assert state and Decimal(str(state[0]["last_total"])) == Decimal("6e13")


def test_gate_recovers_after_a_series_of_above_cap_spikes(app_client):  # noqa: F811
    """Simulate stable buckets, a burst above the cap, then normal samples."""
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "synthetic-spike-series")
    # Seed several stable five-minute buckets.
    base = _now_bucket_start() - timedelta(minutes=5 * 24)
    for index in range(24):
        _insert_bucket(
            client, agent_id,
            [_bucket_sample(128e12 + (index % 4) * 4e12, "PEARL", "pearlhash")],
            base + timedelta(minutes=5 * index),
        )
    refused = 0
    for spike in (6.0, 9.0, 12.0, 7.0, 8.0):
        status = _running_status([_detail(spike, unit="PH/s")])
        assert heartbeat(client, agent_id, secret, status).status_code == 200
        refused += 1
    assert refused == 5
    assert len(_rejection_audits(client, token, agent_id)) == 5
    # Values in the synthetic baseline band are accepted again.
    for rate in (128.0, 136.0, 144.0):
        status = _running_status([_detail(rate, unit="TH/s")])
        assert heartbeat(client, agent_id, secret, status).status_code == 200
        stored = _stored_samples(client, agent_id)
        assert stored and stored[0]["hashrate"] == rate * 1e12
