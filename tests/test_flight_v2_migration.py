"""SQLite acceptance gates for the normalized Flight V2 migration."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from server.database import Database


JOB_COLUMNS = {
    "flight_sheet_id",
    "position",
    "job_id",
    "adapter_id",
    "adapter_revision",
    "adapter_runtime_sha256",
    "miner_version",
    "resource",
    "coin",
    "wallet_id",
    "pool_url",
    "miner_package_id",
    "execution_mode",
    "algorithm",
    "pool_password",
    "device_indices_json",
    "options_json",
    "process_group",
}


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return {
        str(row["name"]): row
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    assert row is not None, f"missing normalized Flight V2 table: {table}"
    return str(row[0])


def _seed_principals(database: Database) -> int:
    with database.connection() as connection:
        return int(
            connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[
                "id"
            ]
        )


def _insert_wallet(connection: sqlite3.Connection, wallet_id: str, user_id: int) -> None:
    connection.execute(
        """INSERT INTO wallets
           (id,name,coin,address,note,created_by,created_at,updated_at)
           VALUES(?,?, 'TEST', ?, '', ?, 'now', 'now')""",
        (wallet_id, wallet_id, f"address-{wallet_id}", user_id),
    )


def _insert_package(
    connection: sqlite3.Connection, package_id: str, miner_type: str, user_id: int
) -> None:
    connection.execute(
        """INSERT INTO miner_packages
           (id,name,version,download_url,sha256,archive_type,note,source_type,
            managed_miner_type,managed_os_family,managed_channel,
            created_by,created_at,updated_at)
           VALUES(?,?,?,'https://packages.example.test/miner','', 'tar.gz','',
                  'external',?,'linux','stable',?,'now','now')""",
        (package_id, miner_type, "1.0.0", miner_type, user_id),
    )


def _insert_legacy_sheet(
    connection: sqlite3.Connection,
    *,
    sheet_id: str,
    miner_type: str,
    version: str,
    algorithm: str,
    wallet_id: str,
    package_id: str,
    user_id: int,
    secondary: dict[str, str] | None = None,
    secondary_mode: str = "integrated",
) -> None:
    values = {
        "id": sheet_id,
        "name": sheet_id,
        "miner_type": miner_type,
        "miner_version": version,
        "coin": "XMR" if algorithm == "randomx" else "PEARL",
        "wallet_id": wallet_id,
        "pool_url": "stratum+ssl://pool.example.test:443",
        "miner_profile": "default",
        "miner_package_id": package_id,
        "execution_mode": "native",
        "algorithm": algorithm,
        "pool_password": "x",
        "device_indices_json": "[]",
        "worker_name_template": "{hostname}",
        "extra_config_json": "{}",
        "gpu_oc_json": None,
        "secondary_mode": secondary_mode,
        "secondary_miner_type": None,
        "secondary_miner_version": None,
        "secondary_coin": None,
        "secondary_wallet_id": None,
        "secondary_pool_url": None,
        "secondary_miner_profile": None,
        "secondary_miner_package_id": None,
        "secondary_execution_mode": None,
        "secondary_algorithm": None,
        "secondary_pool_password": None,
        "secondary_device_indices_json": None,
        "secondary_extra_config_json": None,
        "created_by": user_id,
        "created_at": "now",
        "updated_at": "now",
    }
    if secondary is not None:
        values.update(
            secondary_miner_type=secondary["miner_type"],
            secondary_miner_version=secondary["version"],
            secondary_coin=secondary["coin"],
            secondary_wallet_id=secondary["wallet_id"],
            secondary_pool_url="stratum+tcp://secondary.example.test:3333",
            secondary_miner_profile="default",
            secondary_miner_package_id=secondary["package_id"],
            secondary_execution_mode="native",
            secondary_algorithm=secondary["algorithm"],
            secondary_pool_password="x",
            secondary_device_indices_json="[]",
            secondary_extra_config_json="{}",
        )
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO flight_sheets ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def test_flight_v2_schema_has_normalized_queryable_jobs_and_foreign_keys(
    tmp_path: Path,
):
    path = tmp_path / "flight-v2-schema.db"
    Database(path).initialize("admin", "correct-horse-battery-staple")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        sheet_columns = _columns(connection, "flight_sheets")
        job_columns = _columns(connection, "flight_sheet_jobs")
        table_sql = _table_sql(connection, "flight_sheet_jobs").lower()
        foreign_keys = {
            (str(row["from"]), str(row["table"]), str(row["to"]), str(row["on_delete"]))
            for row in connection.execute("PRAGMA foreign_key_list(flight_sheet_jobs)")
        }
        indexes = {
            str(row["name"]): bool(row["unique"])
            for row in connection.execute("PRAGMA index_list(flight_sheet_jobs)")
        }

    assert "schema_version" in sheet_columns
    assert sheet_columns["schema_version"]["notnull"] == 1
    assert str(sheet_columns["schema_version"]["dflt_value"]).strip("'\"") == "1"
    assert JOB_COLUMNS <= set(job_columns)
    for field in (
        "flight_sheet_id",
        "position",
        "job_id",
        "adapter_id",
        "adapter_revision",
        "adapter_runtime_sha256",
        "resource",
        "coin",
        "wallet_id",
        "pool_url",
        "execution_mode",
        "algorithm",
        "pool_password",
        "device_indices_json",
        "options_json",
        "process_group",
    ):
        assert job_columns[field]["notnull"] == 1, field
    assert "position" in table_sql and "between 0 and 3" in table_sql
    assert ("flight_sheet_id", "flight_sheets", "id", "CASCADE") in foreign_keys
    assert ("wallet_id", "wallets", "id", "RESTRICT") in foreign_keys
    assert ("miner_package_id", "miner_packages", "id", "RESTRICT") in foreign_keys
    assert any(unique for unique in indexes.values()), "job_id needs a per-sheet unique index"


def test_legacy_rows_stay_v1_when_declarative_runtimes_are_not_installed(tmp_path: Path):
    path = tmp_path / "flight-v2-migration.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    user_id = _seed_principals(database)
    miners = (
        ("xmrig", "6.26.0", "randomx"),
        ("srbminer", "3.4.6", "pearlhash"),
        ("pearlminer", "2.0.5", "pearlhash"),
        ("lolminer", "1.98a", "etchash"),
        ("forgeminer", "1.0.0", "pearlhash"),
    )
    with database.connection() as connection:
        for miner_type, version, algorithm in miners:
            wallet_id = f"wallet-{miner_type}"
            package_id = f"package-{miner_type}"
            _insert_wallet(connection, wallet_id, user_id)
            _insert_package(connection, package_id, miner_type, user_id)
            _insert_legacy_sheet(
                connection,
                sheet_id=f"sheet-{miner_type}",
                miner_type=miner_type,
                version=version,
                algorithm=algorithm,
                wallet_id=wallet_id,
                package_id=package_id,
                user_id=user_id,
            )
        _insert_wallet(connection, "wallet-generic", user_id)
        _insert_package(connection, "package-generic", "generic", user_id)
        _insert_legacy_sheet(
            connection,
            sheet_id="sheet-generic",
            miner_type="generic",
            version="1.0.0",
            algorithm="custom",
            wallet_id="wallet-generic",
            package_id="package-generic",
            user_id=user_id,
        )

    database.initialize("admin", "correct-horse-battery-staple")
    database.initialize("admin", "correct-horse-battery-staple")
    with database.connection() as connection:
        managed_rows = connection.execute(
            """SELECT fs.id,fs.schema_version,COUNT(j.position) AS jobs
               FROM flight_sheets fs
               LEFT JOIN flight_sheet_jobs j ON j.flight_sheet_id=fs.id
               WHERE fs.id LIKE 'sheet-%'
               GROUP BY fs.id,fs.schema_version ORDER BY fs.id"""
        ).fetchall()
        by_id = {row["id"]: (row["schema_version"], row["jobs"]) for row in managed_rows}
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert set(by_id) == {"sheet-generic", *(f"sheet-{item[0]}" for item in miners)}
    assert all(value == (1, 0) for value in by_id.values())
    assert foreign_key_errors == []


@pytest.mark.parametrize("secondary_mode,same_group", [("integrated", True), ("independent", False)])
def test_dual_legacy_rows_are_not_projected_without_declarative_runtimes(
    tmp_path: Path, secondary_mode: str, same_group: bool
):
    path = tmp_path / f"flight-v2-{secondary_mode}.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    user_id = _seed_principals(database)
    with database.connection() as connection:
        for role in ("primary", "secondary"):
            _insert_wallet(connection, f"wallet-{role}", user_id)
            _insert_package(connection, f"package-{role}", "srbminer", user_id)
        _insert_legacy_sheet(
            connection,
            sheet_id="dual",
            miner_type="srbminer",
            version="3.4.6",
            algorithm="randomx",
            wallet_id="wallet-primary",
            package_id="package-primary",
            user_id=user_id,
            secondary={
                "miner_type": "srbminer" if same_group else "pearlminer",
                "version": "3.4.6" if same_group else "2.0.5",
                "coin": "PEARL",
                "wallet_id": "wallet-secondary",
                "package_id": "package-secondary",
                "algorithm": "pearlhash",
            },
            secondary_mode=secondary_mode,
        )

    database.initialize("admin", "correct-horse-battery-staple")
    with database.connection() as connection:
        state = connection.execute(
            "SELECT schema_version FROM flight_sheets WHERE id='dual'"
        ).fetchone()
        jobs = connection.execute(
            "SELECT position FROM flight_sheet_jobs WHERE flight_sheet_id='dual'"
        ).fetchall()
    assert state is not None
    assert state["schema_version"] == 1
    assert jobs == []


def test_existing_v2_sheet_and_jobs_are_untouched_by_repeated_initialize(
    tmp_path: Path,
):
    path = tmp_path / "flight-v2-preserved.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    user_id = _seed_principals(database)
    with database.connection() as connection:
        _insert_wallet(connection, "wallet-v2", user_id)
        _insert_package(connection, "package-v2", "srbminer", user_id)
        _insert_legacy_sheet(
            connection,
            sheet_id="sheet-v2",
            miner_type="srbminer",
            version="3.4.6",
            algorithm="pearlhash",
            wallet_id="wallet-v2",
            package_id="package-v2",
            user_id=user_id,
        )
        connection.execute(
            "UPDATE flight_sheets SET schema_version=2,updated_at='v2-sentinel' "
            "WHERE id='sheet-v2'"
        )
        connection.execute(
            """INSERT INTO flight_sheet_jobs
               (flight_sheet_id,position,job_id,adapter_id,adapter_revision,
                adapter_runtime_sha256,miner_version,resource,coin,wallet_id,
                pool_url,miner_package_id,execution_mode,algorithm,pool_password,
                device_indices_json,options_json,process_group)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "sheet-v2",
                0,
                "job-v2-sentinel",
                "miner-adapter.v1/srbminer",
                "1.2.3",
                "a" * 64,
                "3.4.6",
                "gpu",
                "PEARL",
                "wallet-v2",
                "stratum+ssl://preserve.example.test:443",
                "package-v2",
                "native",
                "pearlhash",
                "sentinel-password",
                "[3,1]",
                '{"sentinel":"keep"}',
                "process-group-v2",
            ),
        )
        sheet_before = dict(
            connection.execute(
                "SELECT * FROM flight_sheets WHERE id='sheet-v2'"
            ).fetchone()
        )
        jobs_before = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM flight_sheet_jobs "
                "WHERE flight_sheet_id='sheet-v2' ORDER BY position"
            ).fetchall()
        ]

    database.initialize("admin", "correct-horse-battery-staple")
    database.initialize("admin", "correct-horse-battery-staple")
    with database.connection() as connection:
        sheet_after = dict(
            connection.execute(
                "SELECT * FROM flight_sheets WHERE id='sheet-v2'"
            ).fetchone()
        )
        jobs_after = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM flight_sheet_jobs "
                "WHERE flight_sheet_id='sheet-v2' ORDER BY position"
            ).fetchall()
        ]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert sheet_after == sheet_before
    assert jobs_after == jobs_before
    assert foreign_key_errors == []


@pytest.mark.parametrize("retired_type,version", [("lolminer", "1.98a"), ("forgeminer", "1.0.0")])
def test_retired_legacy_secondary_prevents_partial_projection_and_preserves_rows(
    tmp_path: Path, monkeypatch, retired_type: str, version: str,
):
    # A working current runtime must not turn a dual sheet's retired leg into
    # a partial, executable V2 projection or rewrite the original legacy type.
    monkeypatch.setattr("server.flight_v2_storage.runtime_sha256_for", lambda _adapter: "a" * 64)
    path = tmp_path / "retired-legacy.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    user_id = _seed_principals(database)
    with database.connection() as connection:
        for role, miner_type in (("primary", "xmrig"), ("secondary", retired_type)):
            _insert_wallet(connection, f"wallet-{role}", user_id)
            _insert_package(connection, f"package-{role}", miner_type, user_id)
        _insert_legacy_sheet(
            connection, sheet_id="retired-dual", miner_type="xmrig", version="6.26.0",
            algorithm="randomx", wallet_id="wallet-primary", package_id="package-primary",
            user_id=user_id, secondary_mode="independent",
            secondary={"miner_type": retired_type, "version": version, "coin": "PEARL",
                       "wallet_id": "wallet-secondary", "package_id": "package-secondary",
                       "algorithm": "pearlhash"},
        )
        connection.execute(
            "UPDATE flight_sheets SET secondary_extra_config_json=? WHERE id='retired-dual'",
            ('{"retired_only_option":7,"extra_args":"--future-saved"}',),
        )
        before = dict(connection.execute("SELECT * FROM flight_sheets WHERE id='retired-dual'").fetchone())
    database.initialize("admin", "correct-horse-battery-staple")
    with database.connection() as connection:
        after = dict(connection.execute("SELECT * FROM flight_sheets WHERE id='retired-dual'").fetchone())
        assert connection.execute(
            "SELECT * FROM flight_sheet_jobs WHERE flight_sheet_id='retired-dual'"
        ).fetchall() == []
    assert after == before
    assert after["schema_version"] == 1


def test_normalized_job_constraints_and_fk_delete_policy(tmp_path: Path):
    path = tmp_path / "flight-v2-constraints.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    user_id = _seed_principals(database)
    with database.connection() as connection:
        _insert_wallet(connection, "wallet", user_id)
        _insert_package(connection, "package", "xmrig", user_id)
        _insert_legacy_sheet(
            connection,
            sheet_id="sheet",
            miner_type="xmrig",
            version="6.26.0",
            algorithm="randomx",
            wallet_id="wallet",
            package_id="package",
            user_id=user_id,
        )
    database.initialize("admin", "correct-horse-battery-staple")

    with database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM wallets WHERE id='wallet'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM miner_packages WHERE id='package'")
        connection.execute("DELETE FROM flight_sheets WHERE id='sheet'")
        assert connection.execute(
            "SELECT COUNT(*) FROM flight_sheet_jobs WHERE flight_sheet_id='sheet'"
        ).fetchone()[0] == 0
