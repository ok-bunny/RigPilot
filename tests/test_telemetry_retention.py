from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from server.database import Database
from server.workload import _series_key


def _insert_agent(db, agent_id: str) -> None:
    db.execute(
        """INSERT INTO agents
           (id,secret_hash,name,hostname,os_family,platform,agent_version,enrolled_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            agent_id,
            "unused",
            agent_id,
            f"{agent_id}.local",
            "linux",
            "test",
            "0.0.0",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def test_raw_telemetry_pruning_is_bounded_and_does_not_touch_workload(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "retention.db")
    database.initialize("admin", "test-password")
    key = _series_key("PEARL", "pearlhash")
    with database.connection() as db:
        _insert_agent(db, "agent-a")
        db.executemany(
            """INSERT INTO telemetry(agent_id,recorded_at,metrics_json)
               VALUES('agent-a',?,'{}')""",
            [(f"2000-01-01T00:00:{second:02d}+00:00",) for second in range(25)],
        )
        db.execute(
            """INSERT INTO device_work_state
               (agent_id,series_key,counter_epoch,snapshot_seq,last_total,coin,
                algorithm,last_received_at,last_observed_at)
               VALUES('agent-a',?,'epoch',1,'100','PEARL','pearlhash',?,?)""",
            (
                key,
                "2026-07-30T00:00:00+00:00",
                "2026-07-30T00:00:00+00:00",
            ),
        )
        db.execute(
            """INSERT INTO device_work_buckets
               (agent_id,series_key,bucket_start,coin,algorithm,estimated_hashes,
                recovered_hashes,coverage_seconds,interval_count,recovery_count)
               VALUES('agent-a',?,'2026-07-30T00:00:00+00:00','PEARL',
                      'pearlhash',100,0,300,1,0)""",
            (key,),
        )

    assert database.prune_telemetry(2, max_rows=10) == 10
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 15
        assert db.execute("SELECT COUNT(*) FROM device_work_state").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 1

    assert database.prune_telemetry(2, max_rows=10) == 10
    assert database.prune_telemetry(2, max_rows=10) == 5
    assert database.prune_telemetry(2, max_rows=10) == 0
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM device_work_buckets").fetchone()[0] == 1


def test_rollup_migration_creates_boundary_without_backfilling_legacy_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "migration.db"
    database = Database(path)
    database.initialize("admin", "test-password")
    with database.connection() as db:
        _insert_agent(db, "agent-a")
        db.execute(
            """INSERT INTO telemetry(agent_id,recorded_at,metrics_json)
               VALUES('agent-a','2000-01-01T00:00:00+00:00','{}')"""
        )
        db.execute("DELETE FROM hashrate_history_rollup_state")

    # Re-initialization models upgrading a legacy database. It only creates a
    # boundary row and never scans/re-writes the existing telemetry table.
    database.initialize("admin", None)
    with database.connection() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM hashrate_history_rollup_state"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM hashrate_history_buckets"
        ).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 1


def test_compact_history_pruning_has_independent_fourteen_day_boundary(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "history-retention.db")
    database.initialize("admin", "test-password")
    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    recent_bucket = datetime.fromtimestamp(epoch - (epoch % 300), timezone.utc)
    with database.connection() as db:
        _insert_agent(db, "agent-a")
        for bucket in (
            "2000-01-01T00:00:00+00:00",
            recent_bucket.isoformat(timespec="seconds"),
        ):
            db.execute(
                """INSERT INTO hashrate_history_buckets
                   (agent_id,bucket_start,recorded_at,samples_json)
                   VALUES('agent-a',?,?, '[]')""",
                (bucket, bucket),
            )

    assert database.prune_hashrate_history(14) == 1
    with database.connection() as db:
        remaining = db.execute(
            "SELECT bucket_start FROM hashrate_history_buckets"
        ).fetchall()
    assert [row["bucket_start"] for row in remaining] == [
        recent_bucket.isoformat(timespec="seconds")
    ]
