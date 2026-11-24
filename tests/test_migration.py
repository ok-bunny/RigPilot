from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from server.database import Database
from server.auth import hash_password, verify_password


def test_legacy_user_session_revision_migration_preserves_existing_credentials(tmp_path: Path):
    path = tmp_path / "legacy-user-session.db"
    encoded = hash_password("synthetic-existing-admin-password")
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,role TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL)""")
        connection.execute("INSERT INTO users(id,username,password_hash,role,is_active,created_at) VALUES(?,?,?,?,?,?)",
                           (7, "existing-admin", encoded, "admin", 1, "2026-01-01T00:00:00+00:00"))
    database = Database(path)
    database.initialize("bootstrap-admin", "synthetic-bootstrap-password")
    with database.connection() as db:
        users = [dict(row) for row in db.execute("SELECT * FROM users")]
        assert db.execute("SELECT 1 FROM controller_preferences WHERE key='installation_setup'").fetchone() is None
    assert users == [{"id": 7, "username": "existing-admin", "password_hash": encoded, "role": "admin",
                      "is_active": 1, "created_at": "2026-01-01T00:00:00+00:00", "session_version": 0}]


def test_fresh_admin_and_setup_progress_share_the_bootstrap_transaction(tmp_path: Path, monkeypatch):
    path = tmp_path / "atomic-bootstrap.db"
    original_audit = Database.audit

    def fail_bootstrap_audit(db, **kwargs):
        if kwargs.get("action") == "bootstrap_admin":
            raise RuntimeError("synthetic bootstrap commit failure")
        return original_audit(db, **kwargs)

    monkeypatch.setattr(Database, "audit", staticmethod(fail_bootstrap_audit))
    with pytest.raises(RuntimeError, match="synthetic bootstrap commit failure"):
        Database(path).initialize("admin", "synthetic-bootstrap-password")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM controller_preferences WHERE key='installation_setup'").fetchone()[0] == 0


def test_bootstrap_admin_accepts_explicit_short_password(tmp_path: Path):
    path = tmp_path / "short-admin.db"
    explicit_password = "short123"
    Database(path).initialize("admin", explicit_password)

    with sqlite3.connect(path) as connection:
        encoded = connection.execute(
            "SELECT password_hash FROM users WHERE username='admin'"
        ).fetchone()[0]

    assert verify_password(explicit_password, encoded)


def test_database_initialization_is_idempotent_under_thread_races(tmp_path: Path):
    path = tmp_path / "concurrent-initialize.db"
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def initialize() -> None:
        try:
            barrier.wait()
            Database(path).initialize(
                "admin", "correct-horse-battery-staple"
            )
        except BaseException as exc:  # captured for assertion in main thread
            errors.append(exc)

    workers = [threading.Thread(target=initialize) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)

    assert not errors
    assert all(not worker.is_alive() for worker in workers)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_remote_sessions_ledger_is_dropped_on_initialize(tmp_path: Path):
    """0.8.50 retires the reader-less remote-session metadata table."""

    path = tmp_path / "legacy-remote-sessions.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE remote_sessions (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('shell','files')),
                username TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('pending','open','closed','failed','expired')),
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                closed_at TEXT,
                error TEXT
            );
            """
        )

    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert "remote_sessions" not in tables


def test_pre_release_command_table_is_migrated(tmp_path: Path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL CHECK(status IN
                    ('queued', 'dispatched', 'running', 'succeeded', 'failed', 'cancelled')),
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                dispatched_at TEXT,
                finished_at TEXT,
                result_json TEXT,
                error TEXT
            );
            """
        )

    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")

    with database.connection() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(commands)")
        }
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='commands'"
        ).fetchone()["sql"]
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        update_job_foreign_targets = {
            row["table"]
            for row in connection.execute("PRAGMA foreign_key_list(agent_update_jobs)")
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert "expires_at" in columns
    assert "is_transient" in columns
    assert "expired" in sql
    assert {
        "wallets",
        "miner_packages",
        "agent_releases",
        "agent_update_jobs",
        "flight_sheets",
    } <= tables
    assert not {
        "gpu_profiles",
        "remote_sessions",
        "nvidia_driver_cache_sets",
        "nvidia_driver_cache_objects",
        "nvidia_driver_cache_members",
        "nvidia_driver_cache_jobs",
        "nvidia_driver_update_cache_bindings",
        "hiveos_package_update_imports",
        "hiveos_flight_imports",
    } & tables
    assert "commands" in update_job_foreign_targets
    assert "commands_legacy" not in update_job_foreign_targets
    assert foreign_key_errors == []
    with database.connection() as connection:
        assert "commands_legacy" not in {
            row["table"]
            for row in connection.execute(
                "PRAGMA foreign_key_list(configuration_refreshes)"
            )
        }


@pytest.mark.parametrize("legacy_command_fk,missing_command", [
    (False, False), (True, False), (True, True),
])
def test_command_fk_rebuild_preserves_update_history(
    tmp_path: Path, legacy_command_fk: bool, missing_command: bool,
):
    path = tmp_path / "legacy-update-ledger.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    now = "2026-07-19T00:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        legacy_schema = """
            DROP INDEX IF EXISTS idx_agent_update_jobs_agent;
            DROP INDEX IF EXISTS idx_agent_update_jobs_release;
            DROP INDEX IF EXISTS idx_agent_update_jobs_one_active;
            DROP TABLE agent_update_jobs;
            DROP INDEX IF EXISTS idx_commands_agent_status;
            DROP TABLE commands;
            CREATE TABLE commands (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL CHECK(status IN
                    ('queued','dispatched','running','succeeded','failed','cancelled')),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                dispatched_at TEXT,
                finished_at TEXT,
                result_json TEXT,
                error TEXT
            );
            CREATE TABLE agent_update_jobs (
                command_id TEXT PRIMARY KEY REFERENCES commands(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                release_id TEXT NOT NULL REFERENCES agent_releases(id) ON DELETE RESTRICT,
                source_version TEXT NOT NULL,
                target_version TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                downloaded_at TEXT,
                activation_id TEXT,
                target_heartbeat_count INTEGER NOT NULL DEFAULT 0,
                target_heartbeat_at TEXT,
                stalled_at TEXT,
                oc_recovery_json TEXT,
                activate_remote_workspace INTEGER NOT NULL DEFAULT 0,
                post_update_command_id TEXT,
                completed_at TEXT,
                last_error TEXT
            );
            """
        if legacy_command_fk:
            legacy_schema = legacy_schema.replace("REFERENCES commands(id)", "REFERENCES commands_legacy(id)")
        connection.executescript(legacy_schema)
        user_id = connection.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO agents
               (id,secret_hash,name,hostname,os_family,platform,agent_version,
                enrolled_at)
               VALUES('agent-policy','hash','policy','policy','linux','linux',
                      '0.6.30',?)""",
            (now,),
        )
        connection.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,update_protocol,notes,created_by,created_at)
               VALUES('release-policy','0.6.31','linux','x86_64','agent.tar.gz',
                      'release-policy/agent.tar.gz',1,?,'agent_update.v1','',?,?)""",
            ("0" * 64, user_id, now),
        )
        connection.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at)
               VALUES('update-policy','agent-policy','agent_update.v1','{}',
                      'queued',?,?)""",
            (user_id, now),
        )
        connection.execute(
            """INSERT INTO agent_update_jobs
               (command_id,agent_id,release_id,source_version,target_version,
                status,created_by,created_at,updated_at,downloaded_at,
                activation_id,target_heartbeat_count,activate_remote_workspace,
                post_update_command_id,completed_at,last_error)
               VALUES('update-policy','agent-policy','release-policy','0.6.30',
                      '0.6.31','queued',?,?,?,?,?,2,1,'post-policy',NULL,NULL)""",
            (user_id, now, now, now, "activation-policy"),
        )
        recovery = '{"stage":"restoring"}'
        connection.execute(
            """UPDATE agent_update_jobs SET target_heartbeat_at=?, stalled_at=?, oc_recovery_json=?
               WHERE command_id='update-policy'""",
            (now, now, recovery),
        )
        if missing_command:
            connection.execute("UPDATE agent_update_jobs SET command_id='missing' WHERE command_id='update-policy'")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")

    if missing_command:
        for _ in range(2):
            with pytest.raises(sqlite3.IntegrityError, match="foreign-key"):
                database.initialize("admin", "correct-horse-battery-staple")
            with sqlite3.connect(path) as connection:
                assert "commands_legacy" in {
                    fk[2] for fk in connection.execute("PRAGMA foreign_key_list(agent_update_jobs)")
                }
                assert connection.execute(
                    "SELECT command_id,oc_recovery_json FROM agent_update_jobs"
                ).fetchall() == [("missing", recovery)]
        return

    database.initialize("admin", "correct-horse-battery-staple")
    database.initialize("admin", "correct-horse-battery-staple")

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """SELECT activate_remote_workspace,post_update_command_id,
                      activation_id,target_heartbeat_count,target_heartbeat_at,stalled_at,oc_recovery_json
               FROM agent_update_jobs WHERE command_id='update-policy'"""
        ).fetchone()
        assert row == (1, "post-policy", "activation-policy", 2, now, now, recovery)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v02_flight_sheet_table_gains_package_and_second_miner_columns(
    tmp_path: Path,
):
    path = tmp_path / "v02.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE flight_sheets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                coin TEXT NOT NULL,
                wallet_id TEXT NOT NULL,
                pool_url TEXT NOT NULL,
                miner_profile TEXT NOT NULL,
                worker_name_template TEXT NOT NULL,
                extra_config_json TEXT NOT NULL DEFAULT '{}',
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    Database(path).initialize("admin", "correct-horse-battery-staple")

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(flight_sheets)")
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(flight_sheets)")
        }

    assert {
        "miner_type",
        "miner_version",
        "miner_package_id",
        "execution_mode",
        "algorithm",
        "pool_password",
        "device_indices_json",
        "secondary_mode",
        "secondary_coin",
        "secondary_miner_type",
        "secondary_miner_version",
        "secondary_wallet_id",
        "secondary_pool_url",
        "secondary_miner_profile",
        "secondary_miner_package_id",
        "secondary_execution_mode",
        "secondary_algorithm",
        "secondary_pool_password",
        "secondary_device_indices_json",
        "secondary_extra_config_json",
        "gpu_oc_json",
    } <= columns
    assert {
        "idx_flight_sheets_miner_package",
        "idx_flight_sheets_secondary_wallet",
        "idx_flight_sheets_secondary_package",
    } <= indexes


def test_legacy_agents_and_packages_gain_management_columns(tmp_path: Path):
    path = tmp_path / "legacy-management.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE agents (
                id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, name TEXT NOT NULL,
                hostname TEXT NOT NULL, os_family TEXT NOT NULL, platform TEXT NOT NULL,
                agent_version TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                inventory_json TEXT NOT NULL DEFAULT '{}', metrics_json TEXT NOT NULL DEFAULT '{}',
                last_ip TEXT, last_seen TEXT, enrolled_at TEXT NOT NULL, disabled_at TEXT
            );
            CREATE TABLE miner_packages (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL,
                download_url TEXT NOT NULL, sha256 TEXT NOT NULL, archive_type TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '', created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )

    Database(path).initialize("admin", "correct-horse-battery-staple")

    with sqlite3.connect(path) as connection:
        agent_column_info = {
            row[1]: row for row in connection.execute("PRAGMA table_info(agents)")
        }
        agent_columns = set(agent_column_info)
        package_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(miner_packages)")
        }

    assert "note" in agent_columns
    assert "capabilities_json" in agent_columns
    assert "public_ip" in agent_columns
    assert "local_ip_addresses_json" in agent_columns
    assert "desired_gpu_oc_json" in agent_columns
    assert "gpu_oc_migration_marker_json" in agent_columns
    assert "mining_autostart_enabled" in agent_columns
    assert "mining_autostart_last_boot_time" in agent_columns
    assert agent_column_info["mining_autostart_enabled"][3] == 1
    assert agent_column_info["mining_autostart_enabled"][4] == "0"
    assert agent_column_info["mining_autostart_last_boot_time"][4] is None
    assert {
        "source_type",
        "storage_key",
        "original_filename",
        "file_size",
        "managed_miner_type",
        "managed_os_family",
    } <= package_columns

    with sqlite3.connect(path) as connection:
        release_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_releases)")
        }
        release_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(agent_releases)")
        }
    assert {
        "id",
        "version",
        "platform",
        "architecture",
        "file_name",
        "storage_key",
        "file_size",
        "sha256",
        "update_protocol",
        "notes",
        "created_by",
        "created_at",
    } <= release_columns
    assert "idx_agent_releases_created_at" in release_indexes

    with sqlite3.connect(path) as connection:
        update_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(agent_update_jobs)")
        }
        update_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(agent_update_jobs)")
        }
    assert {
        "command_id",
        "agent_id",
        "release_id",
        "activation_id",
        "target_heartbeat_count",
        "downloaded_at",
        "completed_at",
    } <= update_columns
    assert "idx_agent_update_jobs_one_active" in update_indexes


@pytest.mark.parametrize("legacy_command_fk,missing", [
    (False, None), (False, "release"),
    (True, None), (True, "command"), (True, "release"),
])
def test_windows_only_release_table_migrates_to_linux_without_breaking_ledger_fk(
    tmp_path: Path, legacy_command_fk: bool, missing: str | None,
):
    path = tmp_path / "windows-only-releases.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    now = "2026-07-17T00:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript(
            """
            DROP INDEX IF EXISTS idx_agent_releases_created_at;
            DROP TABLE agent_releases;
            CREATE TABLE agent_releases (
                id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                platform TEXT NOT NULL CHECK(platform IN ('windows')),
                architecture TEXT NOT NULL CHECK(architecture IN ('x86_64')),
                file_name TEXT NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                file_size INTEGER NOT NULL CHECK(file_size > 0),
                sha256 TEXT NOT NULL,
                update_protocol TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                UNIQUE(version, platform, architecture)
            );
            CREATE INDEX idx_agent_releases_created_at
                ON agent_releases(created_at DESC);
            """
        )
        if legacy_command_fk:
            # An older migration renamed the original commands table. The
            # populated ledger can retain that name after commands is rebuilt.
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='agent_update_jobs'"
            ).fetchone()[0]
            assert "REFERENCES commands(id)" in ddl
            connection.execute("DROP TABLE agent_update_jobs")
            connection.execute(ddl.replace("REFERENCES commands(id)", "REFERENCES commands_legacy(id)"))
        user_id = connection.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO agents
               (id,secret_hash,name,hostname,os_family,platform,agent_version,
                inventory_json,metrics_json,last_seen,enrolled_at)
               VALUES('agent-1','secret','canary','canary','windows','Windows',
                      '0.6.5','{}','{}',?,?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,update_protocol,notes,created_by,created_at)
               VALUES('release-1','0.6.6','windows','x86_64','agent.zip',
                      'release-1/agent.zip',1,?,'agent_update.v1','',?,?)""",
            ("0" * 64, user_id, now),
        )
        connection.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at,
                expires_at,is_transient)
               VALUES('command-1','agent-1','agent_update.v1','{}','succeeded',
                      ?,?,?,0)""",
            (user_id, now, now),
        )
        connection.execute(
            """INSERT INTO agent_update_jobs
               (command_id,agent_id,release_id,source_version,target_version,
                status,created_by,created_at,updated_at)
               VALUES('command-1','agent-1','release-1','0.6.5','0.6.6',
                      'succeeded',?,?,?)""",
            (user_id, now, now),
        )
        if missing == "command":
            connection.execute("UPDATE agent_update_jobs SET command_id='missing'")
        if missing == "release":
            connection.execute("UPDATE agent_update_jobs SET release_id='missing' WHERE command_id='command-1'")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")

    if missing:
        for _ in range(2):
            with pytest.raises(sqlite3.IntegrityError, match="foreign-key"):
                database.initialize("admin", "correct-horse-battery-staple")
            with sqlite3.connect(path) as connection:
                schema = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='agent_releases'"
                ).fetchone()[0]
                assert "CHECK(platform IN ('windows'))" in schema
                assert connection.execute("SELECT id FROM agent_releases").fetchall() == [("release-1",)]
                expected_job = ("missing", "release-1") if missing == "command" else ("command-1", "missing")
                assert connection.execute(
                    "SELECT command_id,release_id FROM agent_update_jobs"
                ).fetchall() == [expected_job]
                if legacy_command_fk:
                    assert "commands_legacy" in {
                        fk[2] for fk in connection.execute("PRAGMA foreign_key_list(agent_update_jobs)")
                    }
        return

    database.initialize("admin", "correct-horse-battery-staple")

    with sqlite3.connect(path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='agent_releases'"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT release_id FROM agent_update_jobs WHERE command_id='command-1'"
        ).fetchone() == ("release-1",)
        assert "commands" in {
            fk[2] for fk in connection.execute("PRAGMA foreign_key_list(agent_update_jobs)")
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        user_id = connection.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,update_protocol,notes,created_by,created_at)
               VALUES('release-linux','0.6.6','linux','x86_64','agent.tar.gz',
                      'release-linux/agent.tar.gz',1,?,'agent_update.v1','',?,?)""",
            ("1" * 64, user_id, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO agent_releases
                   (id,version,platform,architecture,file_name,storage_key,
                    file_size,sha256,notes,created_by,created_at)
                   VALUES('bad','0.6.7','macos','x86_64','bad.tar.gz',
                          'bad/bad.tar.gz',1,?,'',?,?)""",
                ("2" * 64, user_id, now),
            )


def test_pre_version_managed_flight_sheets_are_pinned_during_migration(
    tmp_path: Path,
):
    path = tmp_path / "managed-pre-version.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE flight_sheets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                miner_type TEXT NOT NULL DEFAULT 'generic',
                coin TEXT NOT NULL,
                wallet_id TEXT NOT NULL,
                pool_url TEXT NOT NULL,
                miner_profile TEXT NOT NULL,
                worker_name_template TEXT NOT NULL,
                extra_config_json TEXT NOT NULL DEFAULT '{}',
                secondary_miner_type TEXT,
                secondary_coin TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO flight_sheets
                (id,name,miner_type,coin,wallet_id,pool_url,miner_profile,
                 worker_name_template,extra_config_json,secondary_miner_type,
                 secondary_coin,created_by,created_at,updated_at)
            VALUES
                ('managed','old managed','xmrig','XMR','wallet',
                 'stratum+tcp://pool.example:3333','default','{hostname}','{}',
                 'srbminer','XMR',1,'2026-01-01','2026-01-01');
            """
        )

    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    with database.connection() as connection:
        row = connection.execute(
            """SELECT miner_version, secondary_miner_version
               FROM flight_sheets WHERE id='managed'"""
        ).fetchone()

    assert row["miner_version"] == "6.26.0"
    assert row["secondary_miner_version"] == "3.4.6"


def test_legacy_flight_fan_settings_are_removed_without_losing_clock_oc(
    tmp_path: Path,
):
    path = tmp_path / "legacy-flight-fan.db"
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO wallets
               (id,name,coin,address,note,created_by,created_at,updated_at)
               VALUES ('wallet','wallet','ETC','public-address','',1,'now','now')"""
        )
        base = (
            "sheet", "sheet", "srbminer", "3.4.6", "ETC", "wallet",
            "stratum+tcp://pool.example:3333", "default", "{hostname}",
            "{}", "etchash", "[0]", 1, "now", "now",
        )
        connection.execute(
            """INSERT INTO flight_sheets
               (id,name,miner_type,miner_version,coin,wallet_id,pool_url,
                miner_profile,worker_name_template,extra_config_json,algorithm,
                device_indices_json,created_by,created_at,updated_at,gpu_oc_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            base
            + (
                json.dumps(
                    {
                        "vendor": "nvidia",
                        "settings": [
                            {
                                "index": 0,
                                "fan_percent": 65,
                                "power_limit_w": 120,
                            },
                            {"index": 1, "fan_percent": 70},
                        ],
                    }
                ),
            ),
        )


    for _ in range(2):
        database.initialize("admin", "correct-horse-battery-staple")
        with database.connection() as connection:
            rows = {
                row["id"]: row["gpu_oc_json"]
                for row in connection.execute(
                    "SELECT id, gpu_oc_json FROM flight_sheets WHERE id='sheet'"
                )
            }
        assert json.loads(rows["sheet"]) == {
            "vendor": "nvidia",
            "settings": [{"index": 0, "power_limit_w": 120}],
        }
