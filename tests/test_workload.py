from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent.pearl_workload import PearlWorkloadCounters
from agent.state import AgentState

from server.config import Settings
from server.database import Database
from server.main import create_app
from server.schemas import HeartbeatRequest
from server.workload import (
    WORKLOAD_RETENTION_DAYS,
    _series_key,
    acknowledged_heartbeat_counters,
    record_heartbeat_counters,
    workload_summary,
)


def _insert_agent(db, agent_id: str, name: str) -> None:
    db.execute(
        """INSERT INTO agents
           (id,secret_hash,name,hostname,os_family,platform,agent_version,enrolled_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            agent_id,
            "unused-test-secret",
            name,
            f"{name}.local",
            "linux",
            "test",
            "0.0.0",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def _counter(
    *, epoch: str, sequence: int, total: int | str, observed_at: datetime,
    coin: str = "PEARL", algorithm: str = "pearlhash",
) -> dict[str, object]:
    return {
        "coin": coin,
        "algorithm": algorithm,
        "counter_epoch": epoch,
        "snapshot_seq": sequence,
        "estimated_work_total": total,
        "observed_at": observed_at.isoformat(timespec="seconds"),
    }


@pytest.fixture()
def work_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "workload.db")
    database.initialize("admin", "test-password")
    with database.connection() as db:
        _insert_agent(db, "agent-a", "Alpha")
        _insert_agent(db, "agent-b", "Beta")
    return database


@pytest.fixture()
def workload_heartbeat_client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "controller.db",
        secret_key="workload-ack-tests-have-a-long-random-enough-secret-key",
        admin_username="admin", admin_password="test-password",
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        assert login.status_code == 200, login.text
        user_headers = {"Authorization": f"Bearer {login.json()['token']}"}
        token = client.post("/api/enrollment-tokens", headers=user_headers,
                            json={"label": "ack-test", "ttl_minutes": 30}).json()["token"]
        enrolled = client.post("/api/agent/enroll", json={
            "enrollment_token": token, "hostname": "ack-worker", "os_family": "linux",
        })
        assert enrolled.status_code == 201, enrolled.text
        identity = enrolled.json()
        yield client, identity["agent_id"], {
            "X-Agent-ID": identity["agent_id"], "Authorization": f"Bearer {identity['agent_secret']}",
        }


def test_absolute_counter_baseline_duplicate_rewind_and_epoch_rules(
    work_database: Database,
) -> None:
    start = datetime(2026, 7, 27, 0, 4, 50, tzinfo=timezone.utc)
    with work_database.connection() as db:
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=start.isoformat(timespec="seconds"),
            counters=[_counter(epoch="boot-a", sequence=0, total=5000, observed_at=start)],
        )
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 0

        second = start + timedelta(seconds=20)
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=second.isoformat(timespec="seconds"),
            counters=[_counter(epoch="boot-a", sequence=1, total=5200, observed_at=second)],
        )
        rows = db.execute(
            """SELECT bucket_start,estimated_hashes,recovered_hashes
               FROM device_work_buckets ORDER BY bucket_start"""
        ).fetchall()
        assert len(rows) == 2
        assert sum(float(row["estimated_hashes"]) for row in rows) == pytest.approx(200)
        assert [float(row["estimated_hashes"]) for row in rows] == pytest.approx([100, 100])
        assert sum(float(row["recovered_hashes"]) for row in rows) == 0

        # A heartbeat can repeat the most recent collector snapshot. It must
        # neither count twice nor move the accepted baseline's receive time.
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=(second + timedelta(seconds=10)).isoformat(timespec="seconds"),
            counters=[_counter(epoch="boot-a", sequence=1, total=5200, observed_at=second)],
        )
        # A receding absolute counter in the same epoch is also ignored.
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=(second + timedelta(seconds=20)).isoformat(timespec="seconds"),
            counters=[
                _counter(
                    epoch="boot-a",
                    sequence=2,
                    total=5100,
                    observed_at=second + timedelta(seconds=20),
                )
            ],
        )
        fourth = second + timedelta(seconds=40)
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=fourth.isoformat(timespec="seconds"),
            counters=[_counter(epoch="boot-a", sequence=3, total=5300, observed_at=fourth)],
        )
        total = db.execute(
            "SELECT SUM(estimated_hashes) FROM device_work_buckets WHERE agent_id='agent-a'"
        ).fetchone()[0]
        assert float(total) == pytest.approx(300)

        # A new process epoch establishes a fresh absolute baseline; its large
        # initial total must never be interpreted as newly completed work.
        fifth = fourth + timedelta(seconds=10)
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=fifth.isoformat(timespec="seconds"),
            counters=[_counter(epoch="boot-b", sequence=7, total=9000, observed_at=start - timedelta(days=1))],
        )
        state = db.execute(
            "SELECT counter_epoch,snapshot_seq,last_total FROM device_work_state WHERE agent_id='agent-a'"
        ).fetchone()
        assert dict(state) == {
            "counter_epoch": "boot-b",
            "snapshot_seq": 7,
            "last_total": "9000",
        }
        total = db.execute(
            "SELECT SUM(estimated_hashes) FROM device_work_buckets WHERE agent_id='agent-a'"
        ).fetchone()[0]
        assert float(total) == pytest.approx(300)


    # Retirement must survive controller restart, and must not depend on the
    # device wall clock or seeing the new epoch's first snapshot.
    work_database.initialize("admin", None)
    with work_database.connection() as db:
        for seconds, epoch, sequence, total, observed in (
            (80, "boot-b", 8, 9100, start - timedelta(days=1)),
            (81, "boot-a", 3, 5300, fourth),
            (90, "boot-b", 9, 9200, start - timedelta(days=1)),
            (100, "boot-b", 10, 9300, start - timedelta(days=1)),
        ):
            record_heartbeat_counters(
                db, agent_id="agent-a",
                received_at=(start + timedelta(seconds=seconds)).isoformat(),
                counters=[_counter(epoch=epoch, sequence=sequence, total=total, observed_at=observed)],
            )
        assert db.execute("SELECT counter_epoch FROM device_work_state WHERE agent_id='agent-a'").fetchone()[0] == "boot-b"
        assert db.execute("SELECT SUM(estimated_hashes) FROM device_work_buckets WHERE agent_id='agent-a'").fetchone()[0] == pytest.approx(600)
        db.execute("DELETE FROM agents WHERE id='agent-a'")
        assert not db.execute("SELECT 1 FROM device_work_retired_epochs WHERE agent_id='agent-a'").fetchone()


def test_long_gap_is_one_recovered_bucket_and_never_fans_out(
    work_database: Database,
) -> None:
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=7)
    with work_database.connection() as db:
        record_heartbeat_counters(
            db,
            agent_id="agent-b",
            received_at=start.isoformat(timespec="seconds"),
            counters=[_counter(epoch="boot-gap", sequence=0, total=0, observed_at=start)],
        )
        record_heartbeat_counters(
            db,
            agent_id="agent-b",
            received_at=end.isoformat(timespec="seconds"),
            counters=[_counter(epoch="boot-gap", sequence=1, total=700, observed_at=end)],
        )
        rows = db.execute(
            """SELECT estimated_hashes,recovered_hashes,coverage_seconds,recovery_count
               FROM device_work_buckets WHERE agent_id='agent-b'"""
        ).fetchall()
    assert len(rows) == 1
    assert float(rows[0]["estimated_hashes"]) == pytest.approx(700)
    assert float(rows[0]["recovered_hashes"]) == pytest.approx(700)
    assert float(rows[0]["coverage_seconds"]) == 0
    assert int(rows[0]["recovery_count"]) == 1


def test_bucket_write_budget_is_global_across_all_series(
    work_database: Database,
) -> None:
    start = datetime(2026, 7, 27, 0, 2, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)

    def snapshots(sequence: int, total: int, observed_at: datetime):
        values = []
        for index in range(16):
            value = _counter(
                epoch=f"wide-{index}",
                sequence=sequence,
                total=total,
                observed_at=observed_at,
            )
            value["algorithm"] = f"pearlhash-{index:02d}"
            values.append(value)
        return values

    with work_database.connection() as db:
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=start.isoformat(timespec="seconds"),
            counters=snapshots(0, 0, start),
        )
        record_heartbeat_counters(
            db,
            agent_id="agent-a",
            received_at=end.isoformat(timespec="seconds"),
            counters=snapshots(1, 100, end),
        )
        aggregate = db.execute(
            """SELECT COUNT(*) AS bucket_count,
                      SUM(estimated_hashes) AS total_work,
                      SUM(recovery_count) AS recoveries
               FROM device_work_buckets WHERE agent_id='agent-a'"""
        ).fetchone()
    assert int(aggregate["bucket_count"]) <= 72
    assert float(aggregate["total_work"]) == pytest.approx(1600)
    assert int(aggregate["recoveries"]) > 0

@pytest.mark.parametrize("agent_filter", [None, "agent-a"])
def test_coin_points_stay_chronological_across_algorithm_changes(work_database, agent_filter):
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    times = [(start + timedelta(minutes=5*i)).isoformat(timespec="seconds") for i in range(3)]
    with work_database.connection() as db:
        # The alphabetically first algorithm appears late and overlaps the
        # other algorithm for one bucket. Both device projections must sum it.
        for algorithm, index, hashes in (
            ("old", 0, 10), ("old", 1, 7), ("new", 1, 13), ("new", 2, 30),
        ):
            db.execute(
                """INSERT INTO device_work_buckets
                   (agent_id,series_key,bucket_start,coin,algorithm,estimated_hashes,
                    recovered_hashes,coverage_seconds,interval_count,recovery_count)
                   VALUES('agent-a',?,?,'PEARL',?,?,0,300,1,0)""",
                (_series_key("PEARL", algorithm), times[index], algorithm, hashes),
            )
        series = workload_summary(db, start=start, end=start+timedelta(minutes=15),
                                  agent_id=agent_filter, algorithm=None, include_points=True)["series"][0]
    assert series["total"] == 60
    for projection in ("individual_devices", "devices"):
        points = series[projection][0]["points"]
        assert [(p["timestamp"], p["value"]) for p in points] == list(zip(times, [10, 20, 30]))


def test_summary_groups_coin_and_filtered_device_uses_fleet_denominator(
    work_database: Database,
) -> None:
    bucket = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    pearlhash = _series_key("PEARL", "pearlhash")
    pearlhash_v2 = _series_key("PEARL", "pearlhash-v2")
    with work_database.connection() as db:
        for agent_id, key, algorithm, value in (
            ("agent-a", pearlhash, "pearlhash", 30.0),
            ("agent-b", pearlhash, "pearlhash", 70.0),
            ("agent-a", pearlhash_v2, "pearlhash-v2", 25.0),
        ):
            db.execute(
                """INSERT INTO device_work_buckets
                   (agent_id,series_key,bucket_start,coin,algorithm,
                    estimated_hashes,recovered_hashes,coverage_seconds,
                    interval_count,recovery_count)
                   VALUES(?,?,?,?,?,?,0,300,1,0)""",
                (
                    agent_id,
                    key,
                    bucket.isoformat(timespec="seconds"),
                    "PEARL",
                    algorithm,
                    value,
                ),
            )
        response = workload_summary(
            db,
            start=bucket,
            end=bucket + timedelta(minutes=5),
            agent_id="agent-a",
            algorithm=None,
            include_points=True,
        )
    assert response["group_by"] == "coin"
    assert len(response["series"]) == 1
    first = response["series"][0]
    assert first["coin"] == "PEARL" and first["algorithm"] is None
    assert first["algorithms"] == ["pearlhash", "pearlhash-v2"]
    assert set(first["source_series_keys"]) == {pearlhash, pearlhash_v2}
    assert first["series_total"] == pytest.approx(125)
    assert [device["agent_id"] for device in first["devices"]] == ["agent-a"]
    assert first["devices"][0]["value"] == pytest.approx(55)
    assert first["devices"][0]["percentage"] == pytest.approx(44)
    assert len(first["devices"][0]["points"]) == 1
    assert first["devices"][0]["points"][0]["value"] == pytest.approx(55)
    assert first["devices"][0]["points"][0]["coverage_seconds"] == pytest.approx(600)
    with work_database.connection() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM device_work_buckets ORDER BY agent_id,series_key")]
        state = [dict(row) for row in db.execute("SELECT * FROM device_work_state ORDER BY agent_id,series_key")]
        fleet = workload_summary(db, start=bucket, end=bucket+timedelta(minutes=5), agent_id=None, algorithm=None, include_points=False)
        assert fleet["series"][0]["total"] == 125
        assert {row["agent_id"]:row["percentage"] for row in fleet["series"][0]["devices"]} == pytest.approx({"agent-a":44,"agent-b":56})
        assert rows == [dict(row) for row in db.execute("SELECT * FROM device_work_buckets ORDER BY agent_id,series_key")]
        assert state == [dict(row) for row in db.execute("SELECT * FROM device_work_state ORDER BY agent_id,series_key")]
        assert len(rows) == 3 and sum(row["estimated_hashes"] for row in rows) == 125


def test_summary_merges_exact_same_custom_names_without_mixing_agent_filter(
    work_database: Database,
) -> None:
    bucket = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    key = _series_key("PEARL", "pearlhash")
    with work_database.connection() as db:
        db.execute("UPDATE agents SET name=? WHERE id=?", ("Alpha", "agent-b"))
        for agent_id, value, recovered, coverage, recoveries in (
            ("agent-a", 30.0, 3.0, 270.0, 0),
            ("agent-b", 70.0, 7.0, 240.0, 1),
        ):
            db.execute(
                """INSERT INTO device_work_buckets
                   (agent_id,series_key,bucket_start,coin,algorithm,
                    estimated_hashes,recovered_hashes,coverage_seconds,
                    interval_count,recovery_count)
                   VALUES(?,?,?,?,?,?,?,?,1,?)""",
                (
                    agent_id,
                    key,
                    bucket.isoformat(timespec="seconds"),
                    "PEARL",
                    "pearlhash",
                    value,
                    recovered,
                    coverage,
                    recoveries,
                ),
            )
        fleet = workload_summary(
            db,
            start=bucket,
            end=bucket + timedelta(minutes=5),
            agent_id=None,
            algorithm=None,
            include_points=True,
        )
        selected = workload_summary(
            db,
            start=bucket,
            end=bucket + timedelta(minutes=5),
            agent_id="agent-a",
            algorithm=None,
            include_points=True,
        )

    devices = fleet["series"][0]["devices"]
    assert len(devices) == 1
    merged = devices[0]
    assert merged["agent_name"] == "Alpha"
    assert merged["agent_ids"] == ["agent-a", "agent-b"]
    assert merged["device_count"] == 2
    assert merged["hostnames"] == ["Alpha.local", "Beta.local"]
    assert merged["hostname"] == ""
    assert merged["value"] == pytest.approx(100)
    assert merged["recovered_value"] == pytest.approx(10)
    assert merged["coverage_seconds"] == pytest.approx(510)
    assert merged["has_coverage_gap"] is True
    assert merged["percentage"] == pytest.approx(100)
    assert len(merged["points"]) == 1
    assert merged["points"][0]["value"] == pytest.approx(100)
    assert merged["points"][0]["recovered_value"] == pytest.approx(10)
    assert merged["points"][0]["coverage_seconds"] == pytest.approx(510)
    raw = {device["agent_id"]: device for device in fleet["series"][0]["individual_devices"]}
    assert {key: device["value"] for key, device in raw.items()} == {"agent-a": 30, "agent-b": 70}
    assert raw["agent-a"]["recovered_value"] == 3 and raw["agent-b"]["recovered_value"] == 7
    assert raw["agent-a"]["coverage_seconds"] == 270 and raw["agent-b"]["coverage_seconds"] == 240
    assert raw["agent-a"]["has_coverage_gap"] is False and raw["agent-b"]["has_coverage_gap"] is True
    assert raw["agent-a"]["points"][0]["value"] == 30 and raw["agent-b"]["points"][0]["value"] == 70

    selected_device = selected["series"][0]["devices"][0]
    assert selected_device["agent_id"] == "agent-a"
    assert selected_device["value"] == pytest.approx(30)
    assert selected_device["percentage"] == pytest.approx(30)
    selected_raw = selected["series"][0]["individual_devices"]
    assert [device["agent_id"] for device in selected_raw] == ["agent-a"]
    assert selected_raw[0]["value"] == 30 and selected_raw[0]["percentage"] == 30
    assert selected["fleet_totals_by_coin"] == {"PEARL": 100}


@pytest.mark.parametrize("include_points", [False, True])
def test_individual_agents_keep_same_name_and_multialgorithm_coin_work_separate(work_database, include_points):
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    with work_database.connection() as db:
        db.execute("UPDATE agents SET name='Same remark'")
        for agent, coin, algorithm, value in [
            ("agent-a", "PEARL", "pearlhash", 15), ("agent-a", "PEARL", "pearlhash-v2", 5),
            ("agent-b", "PEARL", "pearlhash", 80),
            ("agent-a", "QUANTUS", "quantus", 30), ("agent-b", "QUANTUS", "quantus", 70),
        ]:
            db.execute("""INSERT INTO device_work_buckets
                (agent_id,series_key,bucket_start,coin,algorithm,estimated_hashes,
                 recovered_hashes,coverage_seconds,interval_count,recovery_count)
                VALUES(?,?,?,?,?,?,0,300,1,0)""",
                (agent, _series_key(coin, algorithm), start.isoformat(timespec="seconds"), coin, algorithm, value))
        def summary(agent=None):
            return workload_summary(db, start=start, end=start + timedelta(minutes=5),
                agent_id=agent, algorithm=None, coins=["PEARL", "QUANTUS"], include_points=include_points)
        all_agents = summary()
        selected = summary("agent-a")
    assert all_agents["fleet_totals_by_coin"] == selected["fleet_totals_by_coin"] == {"PEARL": 100, "QUANTUS": 100}
    for document in (all_agents, selected):
        for series in document["series"]:
            raw = {row["agent_id"]: row for row in series["individual_devices"]}
            expected = {"agent-a": 20, "agent-b": 80} if series["coin"] == "PEARL" else {"agent-a": 30, "agent-b": 70}
            if document is selected:
                expected = {"agent-a": expected["agent-a"]}
            assert {key: row["value"] for key, row in raw.items()} == expected
            for key, value in expected.items():
                assert raw[key]["agent_name"] == "Same remark"
                assert raw[key]["value_number"] == value and raw[key]["percentage"] == value
                assert raw[key]["value_text"]
                if include_points:
                    assert raw[key]["points"][0]["value"] == value
                else:
                    assert "points" not in raw[key]
            if document is all_agents:
                assert series["devices"][0]["value"] == 100
                assert series["devices"][0]["agent_ids"] == ["agent-a", "agent-b"]
            if include_points:
                series["devices"][0]["points"][0]["value"] = -1
                assert all(row["points"][0]["value"] == expected[key] for key, row in raw.items())


def test_schema_rejects_control_character_workload_coin() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with pytest.raises(ValidationError):
        HeartbeatRequest.model_validate(
            {
                "hostname": "worker",
                "os_family": "linux",
                "workload_counters": [
                    {
                        "coin": "XMR\x00",
                        "algorithm": "randomx",
                        "counter_epoch": "boot-a",
                        "snapshot_seq": 1,
                        "estimated_work_total": "100",
                        "observed_at": now,
                    }
                ],
            }
        )


def test_real_agent_report_matches_strict_heartbeat_schema_and_merges_roles() -> None:
    state = AgentState.new("agent-id", "secret")
    tracker = PearlWorkloadCounters()
    started = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)

    def host(moment: datetime) -> dict[str, object]:
        return {
            "inventory": {
                "mining_status": {
                    "state": "running",
                    "process_verified": True,
                    "session_id": "managed-pearl-session",
                    "stats_updated_at": moment.isoformat(),
                    "hashrate_details": [
                        {
                            "role": "primary",
                            "coin": "PEARL",
                            "algorithm": "pearlhash",
                            "hashrate": 100.0,
                            "hashrate_unit": "H/s",
                        },
                        {
                            "role": "secondary",
                            "coin": "PRL",
                            "algorithm": "PearlHash",
                            "hashrate": 200.0,
                            "hashrate_unit": "H/s",
                        },
                        {
                            "role": "secondary",
                            "coin": "XMR",
                            "algorithm": "randomx",
                            "hashrate": 999.0,
                            "hashrate_unit": "H/s",
                        },
                    ],
                }
            }
        }

    tracker.observe(host(started), state, now=started)
    reports = tracker.observe(
        host(started + timedelta(seconds=10)),
        state,
        now=started + timedelta(seconds=10),
    )
    by_series = {(row["coin"], row["algorithm"]): row for row in reports}
    assert set(by_series) == {("PEARL", "pearlhash"), ("XMR", "randomx")}
    for row in reports:
        assert set(row) == {"coin", "algorithm", "counter_epoch", "snapshot_seq", "estimated_work_total", "observed_at"}
    assert by_series[("PEARL", "pearlhash")]["estimated_work_total"] == "3000"
    assert by_series[("XMR", "randomx")]["estimated_work_total"] == "9990"

    heartbeat = HeartbeatRequest.model_validate(
        {
            "hostname": "worker",
            "os_family": "linux",
            "workload_counters": reports,
        }
    )
    assert {(row.coin, row.algorithm): row.estimated_work_total for row in heartbeat.workload_counters} == {
        ("PEARL", "pearlhash"): "3000", ("XMR", "randomx"): "9990",
    }

@pytest.mark.parametrize("coin,algorithm", [("PEARL", "pearlhash"), ("QUANTUS", "quantus"), ("XMR", "randomx"), ("RVN", "kawpow")])
def test_heartbeat_ingests_counter_and_api_enforces_fourteen_day_range(tmp_path: Path, coin, algorithm) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "controller.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password="test-password",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings), client=("8.8.4.4", 50000)) as client:
        login = client.post(
            "/api/auth/login", json={"username": "admin", "password": "test-password"}
        )
        token = login.json()["token"]
        user_headers = {"Authorization": f"Bearer {token}"}
        enrollment = client.post(
            "/api/enrollment-tokens",
            headers=user_headers,
            json={"label": "work-test", "ttl_minutes": 30},
        ).json()["token"]
        enrolled = client.post(
            "/api/agent/enroll",
            json={
                "enrollment_token": enrollment,
                "name": "Worker",
                "hostname": "worker.local",
                "os_family": "linux",
                "platform": "test",
                "agent_version": "0.0.0",
            },
        ).json()
        agent_headers = {
            "X-Agent-ID": enrolled["agent_id"],
            "Authorization": f"Bearer {enrolled['agent_secret']}",
        }
        now = datetime.now(timezone.utc)
        base = {
            "hostname": "worker.local",
            "os_family": "linux",
            "platform": "test",
            "agent_version": "0.0.0",
            "metrics": {},
            "inventory": {},
            "capabilities": [],
        }
        for sequence, total in ((0, "1000"), (1, "1125")):
            payload = dict(base)
            payload["workload_counters"] = [
                _counter(
                    coin=coin,
                    algorithm=algorithm,
                    epoch="runtime-a",
                    sequence=sequence,
                    total=total,
                    observed_at=now + timedelta(seconds=sequence),
                )
            ]
            response = client.post(
                "/api/agent/heartbeat", headers=agent_headers, json=payload
            )
            assert response.status_code == 200, response.text

        query = client.get(
            "/api/workload",
            headers=user_headers,
            params={
                "from": (now - timedelta(minutes=1)).isoformat(),
                "to": (now + timedelta(minutes=1)).isoformat(),
                "include_points": "true",
            },
        )
        assert query.status_code == 200, query.text
        body = query.json()
        assert body["basis"] == "agent_hashrate_integral"
        assert body["estimated"] is True
        assert len(body["series"]) == 1
        assert body["series"][0]["coin"] == coin
        assert body["series"][0]["total"] == pytest.approx(125)
        assert body["series"][0]["devices"][0]["percentage"] == pytest.approx(100)

        # Raw heartbeat retention is independent from the cumulative work
        # state/buckets. Removing every telemetry row must leave the workload
        # API result byte-for-byte equivalent.
        with client.app.state.db.connection() as db:
            db.execute(
                """UPDATE telemetry SET recorded_at=
                   '2000-01-01T00:00:00+00:00'"""
            )
        assert client.app.state.db.prune_telemetry(2, max_rows=100) == 2
        after_prune = client.get(
            "/api/workload",
            headers=user_headers,
            params={
                "from": (now - timedelta(minutes=1)).isoformat(),
                "to": (now + timedelta(minutes=1)).isoformat(),
                "include_points": "true",
            },
        )
        assert after_prune.status_code == 200, after_prune.text
        assert after_prune.json() == body

        too_long = client.get(
            "/api/workload",
            headers=user_headers,
            params={
                "from": (now - timedelta(days=WORKLOAD_RETENTION_DAYS, seconds=1)).isoformat(),
                "to": now.isoformat(),
            },
        )
        assert too_long.status_code == 422


def test_prune_keeps_the_first_bucket_at_the_fourteen_day_boundary(
    work_database: Database,
) -> None:
    now = datetime(2026, 7, 27, 12, 2, 17, tzinfo=timezone.utc)
    raw_cutoff = now - timedelta(days=WORKLOAD_RETENTION_DAYS)
    cutoff_epoch = int(raw_cutoff.timestamp())
    boundary = datetime.fromtimestamp(
        cutoff_epoch - (cutoff_epoch % 300), timezone.utc
    )
    key = _series_key("PEARL", "pearlhash")
    with work_database.connection() as db:
        for moment, value in (
            (boundary - timedelta(minutes=5), 5.0),
            (boundary, 10.0),
            (now - timedelta(days=1), 20.0),
        ):
            db.execute(
                """INSERT INTO device_work_buckets
                   (agent_id,series_key,bucket_start,coin,algorithm,
                    estimated_hashes,recovered_hashes,coverage_seconds,
                    interval_count,recovery_count)
                   VALUES('agent-a',?,?, 'PEARL','pearlhash',?,0,300,1,0)""",
                (key, moment.isoformat(timespec="seconds"), value),
            )
    with mock.patch("server.database.datetime", wraps=datetime) as clock:
        clock.now.return_value = now
        assert work_database.prune_device_work(WORKLOAD_RETENTION_DAYS) == 1
    with work_database.connection() as db:
        remaining = db.execute(
            """SELECT bucket_start,estimated_hashes
               FROM device_work_buckets ORDER BY bucket_start"""
        ).fetchall()
    assert [row["bucket_start"] for row in remaining] == [
        boundary.isoformat(timespec="seconds"),
        (now - timedelta(days=1)).isoformat(timespec="seconds"),
    ]
    assert [float(row["estimated_hashes"]) for row in remaining] == [10.0, 20.0]


def test_two_coin_counters_keep_independent_baselines_sequences_and_epochs(work_database):
    start = datetime(2026, 10, 3, 0, 4, 50, tzinfo=timezone.utc)
    # The deliberately shared algorithm label tests namespace isolation;
    # collector validation of real coin/algorithm samples is separate.
    def snapshot(coin, sequence, total, moment, epoch="same-epoch"):
        return _counter(coin=coin, algorithm="quantus", epoch=epoch,
                        sequence=sequence, total=total, observed_at=moment)
    with work_database.connection() as db:
        def receive(seconds, snapshots):
            record_heartbeat_counters(db, agent_id="agent-a",
                received_at=(start + timedelta(seconds=seconds)).isoformat(), counters=snapshots)
        receive(0, [snapshot("PEARL", 0, 1000, start), snapshot("QUANTUS", 0, 2000, start)])
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 0
        moment = start + timedelta(seconds=20)
        receive(20, [snapshot("PEARL", 1, 1200, moment), snapshot("QUANTUS", 1, 2600, moment)])
        moment = start + timedelta(seconds=30)
        receive(30, [snapshot("PEARL", 2, 1250, moment), snapshot("QUANTUS", 1, 2700, moment)])
        moment = start + timedelta(seconds=40)
        receive(40, [snapshot("QUANTUS", 2, 2500, moment)])
        moment = start + timedelta(seconds=50)
        receive(50, [snapshot("QUANTUS", 3, 2630, moment)])
        moment = start + timedelta(seconds=60)
        receive(60, [snapshot("QUANTUS", 0, 999_999, moment, epoch="new-quantus-epoch")])
        totals = {row["coin"]: row["value"] for row in db.execute(
            "SELECT coin,SUM(estimated_hashes) AS value FROM device_work_buckets GROUP BY coin")}
        assert totals == pytest.approx({"PEARL": 250, "QUANTUS": 630})
        moment = start + timedelta(seconds=70)
        receive(70, [snapshot("PEARL", 3, 1275, moment),
                     snapshot("QUANTUS", 1, 1_000_069, moment, epoch="new-quantus-epoch")])
        totals = {row["coin"]: row["value"] for row in db.execute(
            "SELECT coin,SUM(estimated_hashes) AS value FROM device_work_buckets GROUP BY coin")}
        states = db.execute("SELECT coin,series_key FROM device_work_state").fetchall()
    assert totals == pytest.approx({"PEARL": 275, "QUANTUS": 700})
    assert {row["series_key"] for row in states} == {
        _series_key("PEARL", "quantus"), _series_key("QUANTUS", "quantus"),
    }


def test_mixed_coin_recovery_stays_within_one_heartbeat_write_budget(work_database):
    from server.workload import WORKLOAD_MAX_BUCKETS_PER_HEARTBEAT
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    end = start + timedelta(hours=3)
    series = [(coin, f"synthetic-algorithm-{index}")
              for index in range(8) for coin in ("PEARL", "QUANTUS")]
    with work_database.connection() as db:
        for moment, sequence, total in [(start, 0, 0), (end, 1, 100)]:
            record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(),
                counters=[_counter(coin=coin, algorithm=algorithm, epoch="same-epoch",
                    sequence=sequence, total=total, observed_at=moment) for coin, algorithm in series])
        rows = db.execute("SELECT coin,estimated_hashes,recovered_hashes,coverage_seconds,recovery_count FROM device_work_buckets").fetchall()
    assert len(rows) <= WORKLOAD_MAX_BUCKETS_PER_HEARTBEAT
    for coin in ("PEARL", "QUANTUS"):
        selected = [row for row in rows if row["coin"] == coin]
        assert sum(row["estimated_hashes"] for row in selected) == pytest.approx(800)
    recovered = [row for row in rows if row["recovery_count"]]
    assert recovered
    assert all(row["coverage_seconds"] == 0 and row["recovered_hashes"] == row["estimated_hashes"] for row in recovered)


@pytest.mark.parametrize("include_points", [False, True])
def test_two_coin_summaries_do_not_mix_same_names_or_fleet_denominators(work_database, include_points):
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    with work_database.connection() as db:
        db.execute("UPDATE agents SET name='Shared Box'")
        for agent, coin, algorithm, value in [
            ("agent-a", "PEARL", "pearlhash", 100), ("agent-b", "PEARL", "pearlhash", 300),
            ("agent-a", "QUANTUS", "quantus", 30), ("agent-b", "QUANTUS", "quantus", 70),
        ]:
            db.execute("""INSERT INTO device_work_buckets
                (agent_id,series_key,bucket_start,coin,algorithm,estimated_hashes,
                 recovered_hashes,coverage_seconds,interval_count,recovery_count)
                VALUES(?,?,?,?,?,?,0,300,1,0)""",
                (agent, _series_key(coin, algorithm), start.isoformat(timespec="seconds"), coin, algorithm, value))
        def summary(**filters):
            return workload_summary(db, start=start, end=start + timedelta(minutes=5),
                agent_id=filters.get("agent_id"), algorithm=filters.get("algorithm"),
                coin=filters.get("coin"), include_points=include_points)
        all_coins = summary()
        groups = {row["coin"]: row for row in all_coins["series"]}
        assert all_coins["available_coins"] == ["PEARL", "QUANTUS"]
        for coin, expected in [("PEARL", 400), ("QUANTUS", 100)]:
            group = groups[coin]
            assert group["total"] == expected and group["unit"] == "H"
            assert group["basis"] == "agent_hashrate_integral" and group["estimated"] is True
            assert group["devices"][0]["device_count"] == 2
            assert group["devices"][0]["value"] == expected
            if include_points:
                assert group["devices"][0]["points"][0]["value"] == expected
        filtered = summary(coin="quantus", agent_id="agent-a")["series"][0]
        assert filtered["total"] == 100
        assert filtered["devices"][0]["value"] == 30
        assert filtered["devices"][0]["percentage"] == pytest.approx(30)
        assert [row["coin"] for row in summary(algorithm="QUANTUS")["series"]] == ["QUANTUS"]
        assert summary(coin="QTC")["series"] == []
        assert summary(coin="XMR")["series"] == []
        assert summary(agent_id="absent-agent")["series"] == []


def test_reopened_database_uses_only_existing_work_buckets_for_quantus(work_database):
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    with work_database.connection() as db:
        for seconds, sequence, total in [(0, 0, 5_000_000_000), (10, 1, 38_600_000_000)]:
            moment = start + timedelta(seconds=seconds)
            record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(),
                counters=[_counter(coin="QUANTUS", algorithm="quantus", epoch="quantus-session",
                    sequence=sequence, total=total, observed_at=moment)])
    with work_database.connection() as db:
        queries = []
        db.set_trace_callback(queries.append)
        result = workload_summary(db, start=start, end=start + timedelta(minutes=5),
            agent_id=None, coin="QUANTUS", algorithm=None, include_points=True)
    assert result["series"][0]["total"] == pytest.approx(33_600_000_000)
    assert not any("telemetry" in query.casefold() or "hashrate_history" in query.casefold() for query in queries)


def test_fresh_agent_two_coin_reports_pass_schema_and_feed_separate_work_buckets(work_database):
    state = AgentState.new("agent-a", "synthetic-local-secret")
    tracker = PearlWorkloadCounters()
    start = datetime(2026, 10, 3, 0, 4, 55, tzinfo=timezone.utc)
    with work_database.connection() as db:
        for seconds in (0, 10):
            moment = start + timedelta(seconds=seconds)
            host = {"inventory": {"mining_status": {
                "state": "running", "process_verified": True,
                "session_id": "synthetic-mixed-mining-session",
                "stats_updated_at": moment.isoformat(),
                "shares": {"accepted": 1_000_000 + seconds, "rejected": 0},
                "proofs": None,
                "hashrate_details": [
                    {"coin": "PEARL", "algorithm": "pearlhash", "hashrate": 100, "hashrate_unit": "H/s"},
                    {"coin": "PRL", "algorithm": "PearlHash", "hashrate": 200, "hashrate_unit": "H/s"},
                    {"coin": "QUANTUS", "algorithm": "quantus", "hashrate": 3_360_000_000, "hashrate_unit": "H/s"},
                ],
            }}}
            reports = tracker.observe(host, state, now=moment)
            heartbeat = HeartbeatRequest.model_validate({"hostname": "synthetic-worker", "os_family": "linux", "workload_counters": reports})
            record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(),
                counters=[counter.model_dump() for counter in heartbeat.workload_counters])
            if seconds == 0:
                assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 0
        result = workload_summary(db, start=start - timedelta(seconds=5), end=start + timedelta(seconds=15),
            agent_id=None, algorithm=None, include_points=True)
    groups = {series["coin"]: series for series in result["series"]}
    assert groups["PEARL"]["total"] == pytest.approx(3000)
    assert groups["QUANTUS"]["total"] == pytest.approx(33_600_000_000)
    for series in groups.values():
        assert series["basis"] == "agent_hashrate_integral"
        assert series["estimated"] is True and series["unit"] == "H"
        points = series["devices"][0]["points"]
        assert sum(point["value"] for point in points) == pytest.approx(series["total"])


def test_multicoin_selection_keeps_hidden_device_coin_in_fleet_denominator(work_database):
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    with work_database.connection() as db:
        for agent, coin, algorithm, value in [
            ("agent-a", "PEARL", "pearlhash", 10),
            ("agent-a", "QUANTUS", "quantus", 30), ("agent-b", "QUANTUS", "quantus", 70),
            ("agent-b", "XMR", "randomx", 1000), ("agent-b", "RVN", "kawpow", 4000),
        ]:
            db.execute("""INSERT INTO device_work_buckets
                (agent_id,series_key,bucket_start,coin,algorithm,estimated_hashes,
                 recovered_hashes,coverage_seconds,interval_count,recovery_count)
                VALUES(?,?,?,?,?,?,0,300,1,0)""",
                (agent, _series_key(coin, algorithm), start.isoformat(timespec="seconds"), coin, algorithm, value))
        def summary(**filters):
            return workload_summary(db, start=start, end=start + timedelta(minutes=5),
                agent_id=filters.get("agent_id"), algorithm=filters.get("algorithm"),
                coin=filters.get("coin"), coins=filters.get("coins"), include_points=True)
        selected = summary(coins=["quantus", "XMR", "QUANTUS", "xmr"], agent_id="agent-a")
        assert selected["fleet_totals_by_coin"] == {"QUANTUS": 100, "XMR": 1000}
        assert [series["coin"] for series in selected["series"]] == ["QUANTUS"]
        assert selected["series"][0]["devices"][0]["value"] == 30
        assert selected["series"][0]["devices"][0]["percentage"] == pytest.approx(30)
        assert summary(coins=["RVN"])["fleet_totals_by_coin"] == {"RVN": 4000}
        assert summary(coin="XMR", coins=["XMR", "RVN"])["fleet_totals_by_coin"] == {"XMR": 1000}
        assert summary(coin="PEARL", coins=["XMR"])["series"] == []
        for choices in [[], ["NO_BUCKET"], ["XMR", "bad/coin"], "XMR"]:
            empty = summary(coins=choices)
            assert empty["series"] == [] and empty["fleet_totals_by_coin"] == {}
        assert set(summary()["fleet_totals_by_coin"]) == {"PEARL", "QUANTUS", "XMR", "RVN"}


@pytest.mark.parametrize("changes", [
    {"coin": True}, {"coin": "XMR\x00"}, {"coin": "XMR\x7f"}, {"coin": "X" * 25},
    {"algorithm": "\x00randomx"}, {"algorithm": "randomx\x7f"}, {"algorithm": "ß" * 64},
    {"estimated_work_total": True}, {"estimated_work_total": "1e101"},
])
def test_generic_counter_bad_raw_fields_cannot_create_a_baseline(work_database, changes):
    moment = datetime(2026, 10, 3, tzinfo=timezone.utc)
    snapshot = _counter(coin="XMR", algorithm="randomx", epoch="fresh-epoch",
                        sequence=0, total=0, observed_at=moment)
    snapshot.update(changes)
    with work_database.connection() as db:
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=[snapshot])
        assert db.execute("SELECT COUNT(*) FROM device_work_state").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 0


def test_fresh_generic_agent_counters_reach_schema_and_buckets_without_aliasing_coins(work_database):
    state = AgentState.new("agent-a", "synthetic-local-secret")
    tracker = PearlWorkloadCounters()
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    rates = {"XMR": 100, "RVN": 200, "C00": 300, "QTC": 400, "QUANTUS": 500}
    with work_database.connection() as db:
        for seconds in (0, 10):
            moment = start + timedelta(seconds=seconds)
            host = {"inventory": {"mining_status": {
                "state": "running", "process_verified": True,
                "session_id": "synthetic-generic-session", "stats_updated_at": moment.isoformat(),
                "hashrate_details": [{"coin": coin, "algorithm": "synthetic-shared-algorithm",
                    "hashrate": rate, "hashrate_unit": "H/s"} for coin, rate in rates.items()],
            }}}
            reports = tracker.observe(host, state, now=moment)
            heartbeat = HeartbeatRequest.model_validate({"hostname": "synthetic-worker", "os_family": "linux", "workload_counters": reports})
            record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(),
                counters=[counter.model_dump() for counter in heartbeat.workload_counters])
        result = workload_summary(db, start=start, end=start + timedelta(minutes=5),
            agent_id=None, algorithm=None, coins=list(rates), include_points=True)
    assert result["fleet_totals_by_coin"] == {coin: rate * 10 for coin, rate in rates.items()}
    assert {series["coin"] for series in result["series"]} == set(rates)
    assert result["basis"] == "agent_hashrate_integral" and result["estimated"] is True


def test_workload_summary_reports_an_unrepresentable_last_bucket_as_invalid(work_database):
    start = datetime(9999, 12, 31, 23, 50, tzinfo=timezone.utc)
    end = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    with work_database.connection() as db, pytest.raises(ValueError, match="represent"):
        workload_summary(db, start=start, end=end, agent_id=None, algorithm=None, include_points=False)


def test_workload_ack_only_confirms_submitted_final_state_and_exact_replays(work_database):
    start = datetime(2026, 10, 11, tzinfo=timezone.utc)
    first = _counter(epoch="ack-epoch", sequence=0, total=100, observed_at=start)
    latest = _counter(epoch="ack-epoch", sequence=1, total=150, observed_at=start + timedelta(seconds=10))
    with work_database.connection() as db:
        for snapshot in (first, latest):
            assert record_heartbeat_counters(
                db, agent_id="agent-a", received_at=snapshot["observed_at"], counters=[snapshot],
            ) is None
        expected = {**latest, "estimated_work_total": "150"}
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[latest]) == [expected]
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[first]) == []
        assert acknowledged_heartbeat_counters(db, agent_id="agent-b", counters=[latest]) == []
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[]) == []
        record_heartbeat_counters(
            db, agent_id="agent-a", received_at=(start + timedelta(seconds=20)).isoformat(), counters=[latest],
        )
        changes = db.total_changes
        queries = []
        db.set_trace_callback(queries.append)
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[latest]) == [expected]
        db.set_trace_callback(None)
        assert db.total_changes == changes
        assert queries and all(query.lstrip().startswith("SELECT") for query in queries)
        assert db.execute("SELECT SUM(estimated_hashes) FROM device_work_buckets").fetchone()[0] == 50


@pytest.mark.parametrize("stored_total,submitted_total", [
    ("100.00", "1e2"), ("100", 100), ("100", 100.0),
    ("9007199254740993.000", "9007199254740993"),
])
def test_workload_ack_compares_decimal_and_utc_but_echoes_stored_fields(work_database, stored_total, submitted_total):
    moment = datetime(2026, 10, 11, 8, tzinfo=timezone(timedelta(hours=8)))
    snapshot = _counter(coin="PRL", algorithm=" PearlHash ", epoch=" ack-epoch ", sequence=5,
                        total=stored_total, observed_at=moment)
    submitted = {**snapshot, "coin": "PEARL", "algorithm": "pearlhash", "counter_epoch": "ack-epoch",
                 "estimated_work_total": submitted_total, "observed_at": "2026-10-11T00:00:00+00:00"}
    with work_database.connection() as db:
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=[snapshot])
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=[submitted])
        acknowledged = acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[submitted])
        assert acknowledged == [{
            "coin": "PEARL", "algorithm": "pearlhash", "counter_epoch": "ack-epoch", "snapshot_seq": 5,
            "estimated_work_total": stored_total, "observed_at": snapshot["observed_at"],
        }]
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [
    {"coin": "QUANTUS"}, {"algorithm": "pearlhash-v2"}, {"counter_epoch": "different-epoch"},
    {"snapshot_seq": 4}, {"snapshot_seq": 6},
    {"estimated_work_total": "9007199254740992"}, {"estimated_work_total": "9007199254740994"},
    {"observed_at": "2026-10-11T00:00:01+00:00"},
    {"observed_at": "2026-10-11T00:00:00"}, {"snapshot_seq": True},
    {"estimated_work_total": True}, {"extra": "not-a-counter-field"},
])
def test_workload_ack_excludes_field_conflicts_and_invalid_snapshots(work_database, changes):
    moment = datetime(2026, 10, 11, tzinfo=timezone.utc)
    snapshot = _counter(epoch="ack-epoch", sequence=5, total="9007199254740993", observed_at=moment)
    with work_database.connection() as db:
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=[snapshot])
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[{**snapshot, **changes}]) == []


@pytest.mark.parametrize("column,value", [("coin", "QUANTUS"), ("algorithm", "different-algorithm")])
def test_workload_ack_checks_stored_coin_and_algorithm_beyond_series_key(work_database, column, value):
    moment = datetime(2026, 10, 11, tzinfo=timezone.utc)
    snapshot = _counter(epoch="ack-epoch", sequence=0, total=100, observed_at=moment)
    with work_database.connection() as db:
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=[snapshot])
        db.execute(f"UPDATE device_work_state SET {column}=? WHERE agent_id='agent-a'", (value,))
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[snapshot]) == []


def test_workload_ack_excludes_old_and_explicitly_retired_epochs(work_database):
    moment = datetime(2026, 10, 11, tzinfo=timezone.utc)
    old = _counter(epoch="old-epoch", sequence=9, total=900, observed_at=moment)
    current = _counter(epoch="current-epoch", sequence=0, total=0, observed_at=moment)
    with work_database.connection() as db:
        for snapshot in (old, current, old):
            record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=[snapshot])
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[old]) == []
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[current]) == [{**current, "estimated_work_total": "0"}]
        db.execute("""INSERT INTO device_work_retired_epochs(agent_id,series_key,counter_epoch)
                      VALUES(?,?,?)""", ("agent-a", _series_key("PEARL", "pearlhash"), "current-epoch"))
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[current]) == []


@pytest.mark.parametrize("duplicate_changes", [{}, {"coin": "PRL", "algorithm": " PearlHash "}, {"estimated_work_total": "101"}])
def test_workload_ack_rejects_duplicate_submitted_series_without_suppressing_others(work_database, duplicate_changes):
    moment = datetime(2026, 10, 11, tzinfo=timezone.utc)
    snapshot = _counter(epoch="ack-epoch", sequence=0, total=100, observed_at=moment)
    independent = _counter(coin="XMR", algorithm="randomx", epoch="xmr-epoch", sequence=0, total=200, observed_at=moment)
    with work_database.connection() as db:
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=[snapshot, independent])
        submitted = [snapshot, {**snapshot, **duplicate_changes}, independent]
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=submitted) == [{**independent, "estimated_work_total": "200"}]


def test_workload_ack_obeys_first_sixteen_submitted_entries_even_with_stored_later_series(work_database):
    moment = datetime(2026, 10, 11, tzinfo=timezone.utc)
    counters = [_counter(coin=f"C{index:02d}", algorithm="synthetic", epoch=f"ack-{index}", sequence=0,
                         total=index, observed_at=moment) for index in range(17)]
    with work_database.connection() as db:
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=counters)
        assert db.execute("SELECT COUNT(*) FROM device_work_state").fetchone()[0] == 16
        record_heartbeat_counters(db, agent_id="agent-a", received_at=moment.isoformat(), counters=counters[16:])
        assert db.execute("SELECT COUNT(*) FROM device_work_state").fetchone()[0] == 17
        expected = [{**snapshot, "estimated_work_total": str(snapshot["estimated_work_total"])} for snapshot in counters[:16]]
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=counters) == expected
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=counters[:16] + [counters[0]]) == expected
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=[None] * 16 + counters[16:]) == []
        duplicated_first = [counters[0], counters[0]] + counters[1:]
        assert acknowledged_heartbeat_counters(db, agent_id="agent-a", counters=duplicated_first) == expected[1:15]


def test_workload_ack_http_confirms_only_accepted_and_duplicate_snapshots(workload_heartbeat_client):
    client, agent_id, headers = workload_heartbeat_client
    snapshot = _counter(epoch="http-ack", sequence=5, total="100", observed_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
    payload = {"hostname": "ack-worker", "os_family": "linux", "workload_counters": [snapshot]}
    for _ in range(2):
        response = client.post("/api/agent/heartbeat", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["workload_counter_acks"] == [{**snapshot, "observed_at": "2026-10-11T00:00:00.000000+00:00"}]
        assert response.json().get("stale_heartbeat") is not True
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT snapshot_seq,last_total FROM device_work_state WHERE agent_id=?", (agent_id,)).fetchone()["last_total"] == "100"
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 0


def test_workload_heartbeat_accepts_zulu_and_retries_same_instant(workload_heartbeat_client):
    client, agent_id, headers = workload_heartbeat_client
    snapshot = _counter(epoch="utc-wire", sequence=1, total="100",
                        observed_at=datetime(2026, 11, 21, tzinfo=timezone.utc))
    for timestamp in ("2026-11-21T00:00:00.123456Z", "2026-11-21T08:00:00.123456+08:00"):
        response = client.post("/api/agent/heartbeat", headers=headers, json={
            "hostname": "ack-worker", "os_family": "linux",
            "workload_counters": [{**snapshot, "observed_at": timestamp}],
        })
        assert response.status_code == 200, response.text
        assert response.json()["workload_counter_acks"] == [
            {**snapshot, "observed_at": "2026-11-21T00:00:00.123456+00:00"}
        ]
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM device_work_state WHERE agent_id=?", (agent_id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [
    {"snapshot_seq": 4}, {"estimated_work_total": "101"}, {"observed_at": "2026-10-11T00:00:01+00:00"},
])
def test_workload_ack_http_rejected_counter_conflicts_have_no_ack(workload_heartbeat_client, changes):
    client, _, headers = workload_heartbeat_client
    snapshot = _counter(epoch="http-ack", sequence=5, total="100", observed_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
    payload = {"hostname": "ack-worker", "os_family": "linux", "workload_counters": [snapshot]}
    assert client.post("/api/agent/heartbeat", headers=headers, json=payload).json()["workload_counter_acks"] == [{**snapshot, "observed_at": "2026-10-11T00:00:00.000000+00:00"}]
    response = client.post("/api/agent/heartbeat", headers=headers, json={**payload, "workload_counters": [{**snapshot, **changes}]})
    assert response.status_code == 200, response.text
    assert response.json()["workload_counter_acks"] == []


def test_workload_ack_http_same_batch_duplicate_series_has_no_ack(workload_heartbeat_client):
    client, _, headers = workload_heartbeat_client
    snapshot = _counter(epoch="http-ack", sequence=5, total="100", observed_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
    payload = {"hostname": "ack-worker", "os_family": "linux", "workload_counters": [snapshot]}
    assert client.post("/api/agent/heartbeat", headers=headers, json=payload).json()["workload_counter_acks"] == [{**snapshot, "observed_at": "2026-10-11T00:00:00.000000+00:00"}]
    response = client.post("/api/agent/heartbeat", headers=headers, json={**payload, "workload_counters": [snapshot, snapshot]})
    assert response.status_code == 422, response.text
    assert "workload_counter_acks" not in response.json()


def test_workload_ack_http_stale_heartbeat_cannot_confirm_even_matching_counter(workload_heartbeat_client):
    client, agent_id, headers = workload_heartbeat_client
    snapshot = _counter(epoch="http-ack", sequence=5, total="100", observed_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
    payload = {"hostname": "ack-worker", "os_family": "linux", "workload_counters": [snapshot]}
    assert client.post("/api/agent/heartbeat", headers=headers, json=payload).json()["workload_counter_acks"] == [{**snapshot, "observed_at": "2026-10-11T00:00:00.000000+00:00"}]
    with client.app.state.db.connection() as db:
        db.execute("UPDATE agents SET last_seen=? WHERE id=?", ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), agent_id))
    response = client.post("/api/agent/heartbeat", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["stale_heartbeat"] is True
    assert response.json()["workload_counter_acks"] == []


def test_workload_ack_http_commit_failure_rolls_back_and_never_publishes_computed_ack(workload_heartbeat_client):
    client, agent_id, headers = workload_heartbeat_client
    snapshot = _counter(epoch="http-ack", sequence=5, total="100", observed_at=datetime(2026, 10, 11, tzinfo=timezone.utc))
    payload = {"hostname": "ack-worker", "os_family": "linux", "workload_counters": [snapshot]}
    computed = []
    connect = sqlite3.connect

    class FailingCommit(sqlite3.Connection):
        def commit(self):
            if self.in_transaction and self.execute("SELECT 1 FROM device_work_state WHERE agent_id=?", (agent_id,)).fetchone():
                raise sqlite3.OperationalError("synthetic accepted-heartbeat commit failure")
            return super().commit()

    def capture_ack(*args, **kwargs):
        value = acknowledged_heartbeat_counters(*args, **kwargs)
        computed.append(value)
        return value

    with mock.patch("server.database.sqlite3.connect", side_effect=lambda *args, **kwargs: connect(*args, factory=FailingCommit, **kwargs)), \
         mock.patch("server.main.acknowledged_heartbeat_counters", side_effect=capture_ack):
        response = client.post("/api/agent/heartbeat", headers=headers, json=payload)
    assert computed == [[{**snapshot, "observed_at": "2026-10-11T00:00:00.000000+00:00"}]]
    assert response.status_code == 500 and "workload_counter_acks" not in response.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM device_work_state WHERE agent_id=?", (agent_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM telemetry WHERE agent_id=?", (agent_id,)).fetchone()[0] == 0
