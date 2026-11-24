"""Actual heartbeat/API coverage for supported-coin workload counters."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import main as main_module
from server.config import Settings
from server.miner_adapters.workload import SUPPORTED_WORKLOAD_COINS
from test_device_gpu_oc import auth, enroll, login


@pytest.fixture()
def coin_client(tmp_path: Path, monkeypatch):
    catalog = main_module.ManagedMinerCatalog(fetcher=lambda *_: [])
    monkeypatch.setattr(main_module, "ManagedMinerCatalog", lambda: catalog)
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "coin-api.db",
        secret_key="task31-synthetic-controller-secret-long-enough",
        admin_username="admin", admin_password="correct-horse-battery-staple",
    )
    with TestClient(main_module.create_app(settings)) as client:
        yield client


def snapshot(coin, sequence, total, observed_at):
    return {
        "coin": coin, "algorithm": {"QUANTUS": "quantus", "XMR": "randomx", "RVN": "kawpow"}.get(coin, "pearlhash"),
        "counter_epoch": "task31-epoch", "snapshot_seq": sequence,
        "estimated_work_total": total, "observed_at": observed_at.isoformat(),
    }


def heartbeat(client, agent, counters):
    agent_id, secret = agent
    return client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": "Bearer " + secret},
        json={"hostname": "synthetic", "os_family": "windows", "agent_version": "0.8.26",
              "metrics": {}, "inventory": {}, "capabilities": [], "workload_counters": counters},
    )


def query(client, token, now, **extra):
    return client.get("/api/workload", headers=auth(token), params={
        "from": (now - timedelta(minutes=5)).isoformat(),
        "to": (now + timedelta(minutes=5)).isoformat(), **extra,
    })


def test_two_coin_heartbeats_persist_and_api_keeps_filtered_fleet_denominator(coin_client):
    client = coin_client
    token = login(client)
    first, second = enroll(client, token), enroll(client, token)
    now = datetime.now(timezone.utc)
    for agent, pearl, quantus in ((first, 30, 100), (second, 70, 300)):
        assert heartbeat(client, agent, [snapshot(coin, 0, "0", now) for coin in ("PEARL", "QUANTUS")]).status_code == 200
        values = [snapshot("PEARL", 1, str(pearl), now + timedelta(seconds=1)),
                  snapshot("QUANTUS", 1, str(quantus), now + timedelta(seconds=1))]
        assert heartbeat(client, agent, values).status_code == 200
        assert heartbeat(client, agent, values).status_code == 200  # Absolute duplicate adds no work.

    response = query(client, token, now)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["supported_coins"] == list(SUPPORTED_WORKLOAD_COINS)
    assert body["available_coins"] == ["PEARL", "QUANTUS"]
    assert body["estimated"] is True and body["basis"] == "agent_hashrate_integral"
    by_coin = {item["coin"]: item for item in body["series"]}
    assert by_coin["PEARL"]["total"] == pytest.approx(100)
    assert by_coin["QUANTUS"]["total"] == pytest.approx(400)
    assert all(item["unit"] == "H" and item["estimated"] is True for item in by_coin.values())

    filtered = query(client, token, now, agent_id=first[0], coin="QUANTUS").json()
    assert len(filtered["series"]) == 1
    quantus = filtered["series"][0]
    assert quantus["coin"] == "QUANTUS" and quantus["total"] == pytest.approx(400)
    assert quantus["series_total"] == pytest.approx(400)
    assert quantus["devices"][0]["value"] == pytest.approx(100)
    assert quantus["devices"][0]["percentage"] == pytest.approx(25)
    with client.app.state.db.connection() as db:
        states = db.execute("SELECT coin,snapshot_seq FROM device_work_state WHERE agent_id=? ORDER BY coin", (first[0],)).fetchall()
        assert [(row["coin"], row["snapshot_seq"]) for row in states] == [("PEARL", 1), ("QUANTUS", 1)]


@pytest.mark.parametrize("change", [
    {"coin": "bad/coin"}, {"coin": "X" * 25}, {"coin": "quantus"}, {"coin": True},
    {"coin": "XMR\x00hidden"}, {"coin": "\nXMR"},
    {"algorithm": "ß" * 64}, {"algorithm": "\nrandomx"}, {"algorithm": "randomx\x7f"},
    {"snapshot_seq": True}, {"snapshot_seq": -1}, {"estimated_work_total": True},
    {"estimated_work_total": "Infinity"}, {"estimated_work_total": "-1"},
    {"unexpected": 160},
])
def test_invalid_counter_rejects_entire_heartbeat_without_writing(coin_client, change):
    client = coin_client
    token = login(client)
    agent = enroll(client, token)
    now = datetime.now(timezone.utc)
    invalid = {**snapshot("QUANTUS", 0, "100", now), **change}
    response = heartbeat(client, agent, [snapshot("PEARL", 0, "300", now), invalid])
    assert response.status_code == 422, response.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM device_work_state WHERE agent_id=?", (agent[0],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM telemetry WHERE agent_id=?", (agent[0],)).fetchone()[0] == 0


def test_empty_api_exposes_supported_coins_without_inventing_observed_work(coin_client):
    token = login(coin_client)
    response = query(coin_client, token, datetime.now(timezone.utc))
    assert response.status_code == 200
    body = response.json()
    assert body["supported_coins"] == list(SUPPORTED_WORKLOAD_COINS)
    assert body["available_coins"] == [] and body["series"] == []
    assert coin_client.get("/api/workload", params={
        "from": "2026-10-02T00:00:00+00:00", "to": "2026-10-03T00:00:00+00:00",
    }).status_code == 401


def test_generic_coin_counters_multifilter_and_missing_device_coin_fleet_totals(coin_client):
    client = coin_client
    token = login(client)
    first, second = enroll(client, token), enroll(client, token)
    now = datetime.now(timezone.utc)
    for agent, totals in ((first, {"PEARL": 40, "QUANTUS": 50}),
                          (second, {"PEARL": 60, "QUANTUS": 150, "XMR": 300, "RVN": 400})):
        for sequence in (0, 1):
            counters = [snapshot(coin, sequence, str(total * 10**9 if sequence else 0),
                                 now + timedelta(seconds=sequence)) for coin, total in totals.items()]
            response = heartbeat(client, agent, counters)
            assert response.status_code == 200, response.text
    params = {"from": (now - timedelta(minutes=5)).isoformat(), "to": (now + timedelta(minutes=5)).isoformat(),
              "agent_id": first[0], "coins": ["PEARL", "QUANTUS", "XMR", "XMR"]}
    response = client.get("/api/workload", headers=auth(token), params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["coin"] for item in body["series"]} == {"PEARL", "QUANTUS"}
    assert body["fleet_totals_by_coin"] == pytest.approx({"PEARL": 100e9, "QUANTUS": 200e9, "XMR": 300e9})
    assert sum(item["devices"][0]["value"] for item in body["series"]) == pytest.approx(90e9)
    # The local display's explicit per-GH weights 1/2/3 give 140 / 1400 = 10%.
    folded_total = sum(body["fleet_totals_by_coin"][coin] / 1e9 * weight for coin, weight in [("PEARL", 1), ("QUANTUS", 2), ("XMR", 3)])
    folded_device = sum(item["devices"][0]["value"] / 1e9 * {"PEARL": 1, "QUANTUS": 2}[item["coin"]] for item in body["series"])
    assert folded_total == pytest.approx(1400) and folded_device / folded_total == pytest.approx(.1)
    assert query(client, token, now, coin="XMR").json()["series"][0]["total"] == pytest.approx(300e9)
    assert query(client, token, now, coin="RVN").json()["series"][0]["total"] == pytest.approx(400e9)
    all_coins = query(client, token, now).json()
    assert "XMR" in all_coins["available_coins"] and "RVN" in all_coins["available_coins"]
    malformed = dict(params, coins=["XMR\x00hidden"])
    assert client.get("/api/workload", headers=auth(token), params=malformed).status_code == 422
    oversized = dict(params, coins=["PEARL"] * 65)
    assert client.get("/api/workload", headers=auth(token), params=oversized).status_code == 422


def test_single_and_multi_coin_filters_intersect_and_keep_legacy_alias(coin_client):
    client = coin_client
    token = login(client)
    agent = enroll(client, token)
    now = datetime.now(timezone.utc)
    for sequence in (0, 1):
        values = [snapshot(coin, sequence, str(100 * sequence), now + timedelta(seconds=sequence))
                  for coin in ("PEARL", "XMR")]
        assert heartbeat(client, agent, values).status_code == 200
    both = query(client, token, now, coin="PRL", coins=["PEARL", "XMR", "PEARL"])
    assert both.status_code == 200
    assert [item["coin"] for item in both.json()["series"]] == ["PEARL"]
    # The integral anchors on heartbeat receive times, so the exact float can
    # land one ulp below the nominal value under load; compare like siblings.
    assert both.json()["fleet_totals_by_coin"] == pytest.approx({"PEARL": 100})
    disjoint = query(client, token, now, coin="PEARL", coins=["XMR"])
    assert disjoint.status_code == 200
    assert disjoint.json()["series"] == [] and disjoint.json()["fleet_totals_by_coin"] == {}


@pytest.mark.parametrize("field,value", [
    ("from", "0001-01-01T00:00:00+14:00"),
    ("to", "9999-12-31T23:59:59-14:00"),
    ("from", "9999-12-31T23:50:00+00:00"),
])
def test_workload_query_rejects_utc_conversion_or_bucket_ceiling_overflow(coin_client, field, value):
    token = login(coin_client)
    params = {"from": "2026-10-05T00:00:00+00:00", "to": "2026-10-05T00:05:00+00:00", field: value}
    if value == "9999-12-31T23:50:00+00:00":
        params["to"] = "9999-12-31T23:59:59+00:00"
    response = coin_client.get("/api/workload", headers=auth(token), params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
@pytest.mark.parametrize("endpoint", ["heartbeat", "settlement"])
def test_heartbeat_and_settlement_endpoint_reject_utc_overflow_without_500(coin_client, value, endpoint):
    token = login(coin_client)
    if endpoint == "heartbeat":
        agent = enroll(coin_client, token)
        counter = {"coin": "PEARL", "algorithm": "pearlhash", "counter_epoch": "epoch", "snapshot_seq": 0,
                   "estimated_work_total": "0", "observed_at": value}
        response = heartbeat(coin_client, agent, [counter])
    else:
        response = coin_client.put("/api/workload-settlement-note", headers=auth(token),
                                   json={"settlement_at": value, "expected_revision": 0})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("year", [1, 9999])
def test_representable_utc_bucket_ranges_remain_valid_at_calendar_edges(coin_client, year):
    token = login(coin_client)
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    response = coin_client.get("/api/workload", headers=auth(token), params={"from": start.isoformat(), "to": (start + timedelta(minutes=5)).isoformat()})
    assert response.status_code == 200, response.text


def test_actual_tracker_resume_anchor_keeps_stopped_buckets_empty(coin_client, monkeypatch):
    from agent.pearl_workload import PearlWorkloadCounters
    from agent.state import AgentState
    tracker = PearlWorkloadCounters()
    token = login(coin_client)
    agent = enroll(coin_client, token)
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    state = AgentState(agent_id=agent[0], agent_secret=agent[1], enrolled_at=start.isoformat())
    totals = []
    for seconds, running in [(0, True), (10, True), (20, False), (3600, True), (3610, True)]:
        moment = start + timedelta(seconds=seconds)
        host = {"inventory": {"mining_status": {"state": "running" if running else "stopped", "process_verified": True,
            "session_id": "same-session", "stats_updated_at": moment.isoformat(), "hashrate_details": [
                {"coin": "PEARL", "algorithm": "pearlhash", "hashrate_unit": "H/s", "hashrate": 100}]}}}
        counters = tracker.observe(host, state, now=moment)
        monkeypatch.setattr(main_module.time, "time_ns", lambda moment=moment: int(moment.timestamp()) * 1_000_000_000)
        response = heartbeat(coin_client, agent, counters)
        assert response.status_code == 200, response.text
        totals.append(counters[0]["estimated_work_total"])
    assert totals == ["0", "1000", "1000", "1000", "2000"]
    with coin_client.app.state.db.connection() as db:
        rows = db.execute("SELECT bucket_start,estimated_hashes,coverage_seconds,recovery_count FROM device_work_buckets WHERE agent_id=? ORDER BY bucket_start", (agent[0],)).fetchall()
    assert [row["bucket_start"] for row in rows] == [start.isoformat(timespec="seconds"), (start+timedelta(hours=1)).isoformat(timespec="seconds")]
    assert [row["estimated_hashes"] for row in rows] == [1000, 1000]
    assert [row["coverage_seconds"] for row in rows] == [10, 10]
    assert all(row["recovery_count"] == 0 for row in rows)
    queried = coin_client.get("/api/workload", headers=auth(token), params={"from": start.isoformat(), "to": (start+timedelta(hours=1,seconds=15)).isoformat(), "include_points": True})
    assert queried.status_code == 200, queried.text
    series = queried.json()["series"][0]
    assert series["total_number"] == 2000
    detail = series["individual_devices"][0]
    assert detail["coverage_seconds"] == 20 and detail["has_coverage_gap"] is False
    assert [point["value"] for point in detail["points"]] == [1000, 1000]


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_legacy_unrepresentable_settlement_is_returned_as_note(coin_client, value):
    token = login(coin_client)
    with coin_client.app.state.db.connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        db.execute("INSERT INTO controller_preferences(key,value,updated_by,updated_at,revision) VALUES('workload_settlement_note',?,?,?,1)", (value, user_id, datetime.now(timezone.utc).isoformat()))
    response = coin_client.get("/api/workload-settlement-note", headers=auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["settlement_at"] is None and response.json()["legacy_note"] == value
