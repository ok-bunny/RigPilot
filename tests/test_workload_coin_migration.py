"""Preserve real legacy SQLite workload ledgers while widening supported coins."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from server.database import Database, SCHEMA


# Historical definitions, checked against the released PEARL-only schema.
# Keep this fixture self-contained: tests must not depend on task outputs.
LEGACY_WORKLOAD_SCHEMA = """
CREATE TABLE device_work_state (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    series_key TEXT NOT NULL,
    counter_epoch TEXT NOT NULL,
    snapshot_seq INTEGER NOT NULL CHECK(snapshot_seq >= 0),
    last_total TEXT NOT NULL,
    coin TEXT NOT NULL CHECK(coin='PEARL'),
    algorithm TEXT NOT NULL,
    last_received_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, series_key)
);
CREATE TABLE device_work_buckets (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    series_key TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    coin TEXT NOT NULL CHECK(coin='PEARL'),
    algorithm TEXT NOT NULL,
    estimated_hashes REAL NOT NULL DEFAULT 0 CHECK(estimated_hashes >= 0),
    recovered_hashes REAL NOT NULL DEFAULT 0 CHECK(recovered_hashes >= 0),
    coverage_seconds REAL NOT NULL DEFAULT 0 CHECK(coverage_seconds >= 0),
    interval_count INTEGER NOT NULL DEFAULT 0 CHECK(interval_count >= 0),
    recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count >= 0),
    PRIMARY KEY(agent_id, series_key, bucket_start)
);
CREATE INDEX idx_device_work_buckets_time ON device_work_buckets(bucket_start);
CREATE INDEX idx_device_work_buckets_series_time
    ON device_work_buckets(series_key, bucket_start);
"""
TABLES = ("device_work_state", "device_work_buckets")
LEGACY_STAGES = ("pearl-only", "two-coins")


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def insert_agent(db: sqlite3.Connection, name: str) -> None:
    db.execute(
        """INSERT INTO agents
           (id,secret_hash,name,hostname,os_family,platform,agent_version,enrolled_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (name, "unused-synthetic-secret", name, name, "linux", "synthetic", "0.0.0",
         "2026-10-03T00:00:00+00:00"),
    )


def insert_state(db, agent="agent-a", key="QUANTUS:quantus", coin="QUANTUS", seq=1):
    db.execute(
        """INSERT INTO device_work_state
           (agent_id,series_key,counter_epoch,snapshot_seq,last_total,coin,algorithm,
            last_received_at,last_observed_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (agent, key, "epoch-exact", seq, "90071992547409931234567890123456789", coin,
         "quantus" if coin == "QUANTUS" else "pearlhash",
         "2026-10-03T00:05:19+00:00", "2026-10-03T00:05:11+00:00"),
    )


def insert_bucket(db, agent="agent-a", key="QUANTUS:quantus", coin="QUANTUS", **changes):
    values = dict(agent_id=agent, series_key=key, bucket_start="2026-10-03T00:05:00+00:00",
                  coin=coin, algorithm="quantus" if coin == "QUANTUS" else "pearlhash",
                  estimated_hashes=125.125, recovered_hashes=50.75, coverage_seconds=20.5,
                  interval_count=3, recovery_count=2)
    values.update(changes)
    db.execute(
        "INSERT INTO device_work_buckets (" + ",".join(values) + ") VALUES ("
        + ",".join("?" for _ in values) + ")", tuple(values.values()),
    )


def snapshot(db):
    result = {}
    for table in TABLES:
        result[table] = {
            "sql": db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0],
            "rows": [tuple(row) for row in db.execute(f'SELECT * FROM "{table}" ORDER BY agent_id,series_key' + (",bucket_start" if table.endswith("buckets") else ""))],
            "columns": [tuple(row) for row in db.execute(f'PRAGMA table_info("{table}")')],
            "foreign_keys": [tuple(row) for row in db.execute(f'PRAGMA foreign_key_list("{table}")')],
            "indexes": {row["name"]: row["sql"] for row in db.execute(
                "SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type='index' AND sql IS NOT NULL", (table,))},
        }
    return result


def legacy_database(tmp_path, stage):
    path = tmp_path / "legacy-workload.db"
    with closing(connect(path)) as db:
        # Current non-workload structures avoid copying unrelated historical DDL.
        db.executescript(SCHEMA)
        db.execute("DROP TABLE device_work_buckets")
        db.execute("DROP TABLE device_work_state")
        historical_schema = LEGACY_WORKLOAD_SCHEMA
        if stage == "two-coins":
            # Same intermediate membership, with order/whitespace/quote variants.
            historical_schema = historical_schema.replace(
                "CHECK(coin='PEARL')", "CHECK ( coin IN ( 'QUANTUS' , \"PEARL\" ) )",
            )
        db.executescript(historical_schema)
        insert_agent(db, "agent-a")
        insert_agent(db, "agent-b")
        insert_state(db, key="legacy-opaque-key", coin="PEARL", seq=71)
        insert_state(db, agent="agent-b", key="other-opaque-key", coin="PEARL", seq=9)
        insert_bucket(db, key="legacy-opaque-key", coin="PEARL")
        insert_bucket(db, key="legacy-opaque-key", coin="PEARL",
                      bucket_start="2026-10-03T00:10:00+00:00", estimated_hashes=1000000000000.0625)
        insert_bucket(db, agent="agent-b", key="other-opaque-key", coin="PEARL")
        if stage == "two-coins":
            insert_state(db, key="prior-quantus-key", seq=43)
            insert_bucket(db, key="prior-quantus-key")
        db.execute("CREATE INDEX custom_work_state_epoch ON device_work_state(counter_epoch,snapshot_seq DESC)")
        db.execute("CREATE INDEX custom_work_bucket_coin ON device_work_buckets(coin,algorithm) WHERE interval_count>0")
        db.commit()
    return Database(path)


@pytest.mark.parametrize("stage", LEGACY_STAGES)
def test_legacy_rows_keys_indexes_and_foreign_keys_survive_widening_and_repeat(tmp_path, stage):
    database = legacy_database(tmp_path, stage)
    with closing(connect(database.path)) as db:
        before = snapshot(db)
    database.initialize("admin", "synthetic-workload-password")
    with closing(connect(database.path)) as db:
        after = snapshot(db)
        for table in TABLES:
            for field in ("rows", "columns", "foreign_keys", "indexes"):
                assert after[table][field] == before[table][field]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        insert_state(db, key="new-XMR", coin="XMR")
        insert_bucket(db, key="new-XMR", coin="XMR")
        db.commit()
        widened = snapshot(db)
    database.initialize("admin", "synthetic-workload-password")
    with closing(connect(database.path)) as db:
        assert snapshot(db) == widened
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("stage", ("fresh", *LEGACY_STAGES))
def test_canonical_raw_coins_constraints_and_agent_cascade_remain(stage, tmp_path):
    database = Database(tmp_path / "fresh-workload.db") if stage == "fresh" else legacy_database(tmp_path, stage)
    database.initialize("admin", "synthetic-workload-password")
    with closing(connect(database.path)) as db:
        if stage == "fresh":
            insert_agent(db, "agent-a")
            insert_agent(db, "agent-b")
        insert_state(db)
        insert_bucket(db)
        insert_state(db, key="PEARL:fresh", coin="PEARL")
        insert_bucket(db, key="PEARL:fresh", coin="PEARL")
        insert_state(db, agent="agent-b", key="QUANTUS:other")
        insert_bucket(db, agent="agent-b", key="QUANTUS:other")
        for coin in ("XMR", "QTC", "FOO-1.2_ABC+", ".XMR", "A" * 24):
            insert_state(db, key="raw-state-" + coin, coin=coin)
            insert_bucket(db, key="raw-bucket-" + coin, coin=coin)
        assert db.execute("SELECT coin FROM device_work_state WHERE series_key='raw-state-QTC'").fetchone()[0] == "QTC"
        for coin in ("", "xmr", "BAD/COIN", "XMR\n", "XMR\x7f", "XMR\x00hidden", "A" * 25, "PRL", sqlite3.Binary(b"XMR")):
            with pytest.raises(sqlite3.IntegrityError):
                insert_state(db, key="bad-state", coin=coin)
            with pytest.raises(sqlite3.IntegrityError):
                insert_bucket(db, key="bad-bucket", coin=coin)
        with pytest.raises(sqlite3.IntegrityError):
            insert_state(db, key="negative-seq", seq=-1)
        for field in ("estimated_hashes", "recovered_hashes", "coverage_seconds", "interval_count", "recovery_count"):
            with pytest.raises(sqlite3.IntegrityError):
                insert_bucket(db, key="negative-" + field, **{field: -1})
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_state(db, agent="missing-agent")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            insert_bucket(db, agent="missing-agent")
        with pytest.raises(sqlite3.IntegrityError):
            insert_state(db)
        with pytest.raises(sqlite3.IntegrityError):
            insert_bucket(db)
        db.execute("DELETE FROM agents WHERE id='agent-a'")
        for table in TABLES:
            assert db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE agent_id=?', ("agent-a",)).fetchone()[0] == 0
            assert db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE agent_id=?', ("agent-b",)).fetchone()[0] > 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


class SecondTableFailure:
    def __init__(self, db, operation):
        self.db, self.operation, self.injected = db, operation, False

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, parameters=()):
        statement = sql.lstrip().upper()
        if statement.startswith(self.operation) and '"DEVICE_WORK_BUCKETS_COIN_MIGRATION"' in statement:
            # Both old schemas already accept QUANTUS at some phase. A real
            # XMR insert proves the first table has become generic before fault.
            insert_state(self.db, key="rollback-XMR-probe", coin="XMR")
            self.injected = True
            raise sqlite3.OperationalError("synthetic second workload table failure")
        return self.db.execute(sql, parameters)


@pytest.mark.parametrize("operation", ["CREATE TABLE", "INSERT INTO"])
@pytest.mark.parametrize("stage", LEGACY_STAGES)
def test_second_table_failure_restores_both_ledgers_and_schema(tmp_path, stage, operation):
    database = legacy_database(tmp_path, stage)
    for outer_transaction in (False, True):
        with closing(connect(database.path)) as db:
            before = snapshot(db)
            if outer_transaction:
                db.execute("UPDATE agents SET note='pending before workload migration' WHERE id='agent-a'")
            proxy = SecondTableFailure(db, operation)
            with pytest.raises(sqlite3.OperationalError, match="synthetic second workload table"):
                Database._migrate_workload_coin_tables(proxy)
            assert proxy.injected
            assert snapshot(db) == before
            assert db.in_transaction is outer_transaction
            if outer_transaction:
                assert db.execute("SELECT note FROM agents WHERE id='agent-a'").fetchone()[0] == "pending before workload migration"
            assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE '%coin_migration%'").fetchall() == []
