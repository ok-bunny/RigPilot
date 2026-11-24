from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .auth import hash_password
from .workload import workload_bucket_start
from .flight_v2_storage import ensure_flight_v2_storage
from .miner_adapters.workload import WORKLOAD_COIN_SQL_CHECK


_INITIALIZE_LOCKS_GUARD = threading.Lock()
_INITIALIZE_LOCKS: dict[Path, threading.Lock] = {}

INSTALLATION_SETUP_KEY = "installation_setup"


_WORKLOAD_COIN_CHECK = "typeof(coin)='text' AND " + WORKLOAD_COIN_SQL_CHECK.format(column="coin")

# Drop retired children before their parents during the one-time migration.
_RETIRED_TABLES = (
    "nvidia_driver_cache_members",
    "nvidia_driver_cache_jobs",
    "nvidia_driver_update_cache_bindings",
    "nvidia_driver_cache_sets",
    "nvidia_driver_cache_objects",
    "hiveos_package_update_imports",
    "hiveos_flight_imports",
    "remote_sessions",
    "gpu_profiles",
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    session_version INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL CHECK(role IN ('viewer', 'operator', 'admin')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollment_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_replacement_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    target_agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    request_id TEXT,
    replacement_secret_hash TEXT,
    activated_at TEXT,
    resume_flight_sheet_id TEXT REFERENCES flight_sheets(id) ON DELETE SET NULL,
    resumed_command_id TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    CHECK(
        (used_at IS NULL AND request_id IS NULL AND replacement_secret_hash IS NULL)
        OR
        (used_at IS NOT NULL AND request_id IS NOT NULL AND replacement_secret_hash IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_agent_replacement_target
    ON agent_replacement_tokens(target_agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS controller_preferences (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_by INTEGER NOT NULL REFERENCES users(id),
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    hostname TEXT NOT NULL,
    os_family TEXT NOT NULL,
    platform TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    inventory_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    desired_gpu_oc_json TEXT NOT NULL DEFAULT '[]',
    desired_gpu_oc_pending_json TEXT NOT NULL DEFAULT '[]',
    gpu_oc_migration_marker_json TEXT NOT NULL DEFAULT '{}',
    mining_autostart_enabled INTEGER NOT NULL DEFAULT 0
        CHECK(mining_autostart_enabled IN (0,1)),
    mining_autostart_last_boot_time TEXT,
    last_ip TEXT,
    public_ip TEXT,
    local_ip_addresses_json TEXT NOT NULL DEFAULT '[]',
    last_seen TEXT,
    enrolled_at TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_enrollment_attempts (
    request_id_hash TEXT PRIMARY KEY,
    token_id TEXT NOT NULL UNIQUE REFERENCES enrollment_tokens(id) ON DELETE CASCADE,
    agent_id TEXT UNIQUE REFERENCES agents(id) ON DELETE SET NULL,
    secret_hash TEXT NOT NULL UNIQUE,
    metadata_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_agent_time
    ON telemetry(agent_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_time ON telemetry(recorded_at);

CREATE TABLE IF NOT EXISTS hashrate_history_buckets (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    bucket_start TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    samples_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(agent_id, bucket_start)
);
CREATE INDEX IF NOT EXISTS idx_hashrate_history_buckets_time
    ON hashrate_history_buckets(bucket_start);

CREATE TABLE IF NOT EXISTS hashrate_history_rollup_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_work_state (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    series_key TEXT NOT NULL,
    counter_epoch TEXT NOT NULL,
    snapshot_seq INTEGER NOT NULL CHECK(snapshot_seq >= 0),
    last_total TEXT NOT NULL,
    coin TEXT NOT NULL CHECK(__WORKLOAD_COIN_CHECK__),
    algorithm TEXT NOT NULL,
    last_received_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, series_key)
);

CREATE TABLE IF NOT EXISTS device_work_buckets (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    series_key TEXT NOT NULL,
    bucket_start TEXT NOT NULL,
    coin TEXT NOT NULL CHECK(__WORKLOAD_COIN_CHECK__),
    algorithm TEXT NOT NULL,
    estimated_hashes REAL NOT NULL DEFAULT 0 CHECK(estimated_hashes >= 0),
    recovered_hashes REAL NOT NULL DEFAULT 0 CHECK(recovered_hashes >= 0),
    coverage_seconds REAL NOT NULL DEFAULT 0 CHECK(coverage_seconds >= 0),
    interval_count INTEGER NOT NULL DEFAULT 0 CHECK(interval_count >= 0),
    recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count >= 0),
    PRIMARY KEY(agent_id, series_key, bucket_start)
);
CREATE INDEX IF NOT EXISTS idx_device_work_buckets_time
    ON device_work_buckets(bucket_start);
CREATE INDEX IF NOT EXISTS idx_device_work_buckets_series_time
    ON device_work_buckets(series_key, bucket_start);

CREATE TABLE IF NOT EXISTS device_work_retired_epochs (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    series_key TEXT NOT NULL,
    counter_epoch TEXT NOT NULL,
    PRIMARY KEY(agent_id, series_key, counter_epoch)
);


CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN
        ('queued', 'dispatched', 'running', 'succeeded', 'failed', 'cancelled', 'expired')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    hard_expires_at TEXT,
    dispatched_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    error TEXT,
    is_transient INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_commands_agent_status
    ON commands(agent_id, status, created_at);

CREATE TABLE IF NOT EXISTS wallets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    coin TEXT NOT NULL,
    address TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wallets_coin_name
    ON wallets(coin, name);

CREATE TABLE IF NOT EXISTS pools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    coin TEXT NOT NULL,
    pool_url TEXT NOT NULL,
    pool_password TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pools_coin_name ON pools(coin, name);

CREATE TABLE IF NOT EXISTS miner_packages (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    download_url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    archive_type TEXT NOT NULL CHECK(archive_type IN ('zip', 'tar.gz')),
    note TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'external'
        CHECK(source_type IN ('external', 'uploaded')),
    storage_key TEXT,
    original_filename TEXT,
    file_size INTEGER,
    managed_miner_type TEXT,
    managed_os_family TEXT,
    managed_variant TEXT,
    managed_channel TEXT NOT NULL DEFAULT 'stable',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_miner_packages_name_version
    ON miner_packages(name, version);

CREATE TABLE IF NOT EXISTS agent_releases (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    platform TEXT NOT NULL CHECK(platform IN ('windows', 'linux')),
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
CREATE INDEX IF NOT EXISTS idx_agent_releases_created_at
    ON agent_releases(created_at DESC);

CREATE TABLE IF NOT EXISTS flight_sheets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    miner_type TEXT NOT NULL DEFAULT 'generic',
    miner_version TEXT,
    coin TEXT NOT NULL,
    wallet_id TEXT NOT NULL REFERENCES wallets(id) ON DELETE RESTRICT,
    pool_url TEXT NOT NULL,
    miner_profile TEXT NOT NULL,
    miner_package_id TEXT REFERENCES miner_packages(id) ON DELETE RESTRICT,
    execution_mode TEXT NOT NULL DEFAULT 'native'
        CHECK(execution_mode IN ('native', 'wsl')),
    algorithm TEXT,
    pool_password TEXT NOT NULL DEFAULT 'x',
    device_indices_json TEXT NOT NULL DEFAULT '[]',
    worker_name_template TEXT NOT NULL,
    extra_config_json TEXT NOT NULL DEFAULT '{}',
    gpu_oc_json TEXT,
    secondary_mode TEXT NOT NULL DEFAULT 'integrated'
        CHECK(secondary_mode IN ('integrated', 'independent')),
    secondary_miner_type TEXT,
    secondary_miner_version TEXT,
    secondary_coin TEXT,
    secondary_wallet_id TEXT REFERENCES wallets(id) ON DELETE RESTRICT,
    secondary_pool_url TEXT,
    secondary_miner_profile TEXT,
    secondary_miner_package_id TEXT REFERENCES miner_packages(id) ON DELETE RESTRICT,
    secondary_execution_mode TEXT
        CHECK(secondary_execution_mode IN ('native', 'wsl')),
    secondary_algorithm TEXT,
    secondary_pool_password TEXT,
    secondary_device_indices_json TEXT,
    secondary_extra_config_json TEXT,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flight_sheets_wallet
    ON flight_sheets(wallet_id);

CREATE TABLE IF NOT EXISTS configuration_refreshes (
    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    flight_sheet_id TEXT NOT NULL REFERENCES flight_sheets(id) ON DELETE CASCADE,
    desired_fingerprint TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    claim_token TEXT,
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('wallet', 'flight_sheet')),
    trigger_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('pending', 'preparing', 'queued', 'dispatched', 'running', 'succeeded',
         'failed', 'cancelled', 'expired')),
    command_id TEXT REFERENCES commands(id) ON DELETE SET NULL,
    requested_by INTEGER NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    queued_at TEXT,
    completed_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_configuration_refresh_command
    ON configuration_refreshes(command_id) WHERE command_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_configuration_refresh_status
    ON configuration_refreshes(status, updated_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(created_at DESC);
""".replace("__WORKLOAD_COIN_CHECK__", _WORKLOAD_COIN_CHECK)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _database_initialization_lock(
    database_path: Path, *, timeout_seconds: float = 30.0
) -> Iterator[None]:
    """Serialize schema/bootstrap work across threads and controller processes."""

    normalized = database_path.resolve()
    with _INITIALIZE_LOCKS_GUARD:
        thread_lock = _INITIALIZE_LOCKS.setdefault(normalized, threading.Lock())
    if not thread_lock.acquire(timeout=timeout_seconds):
        raise TimeoutError("timed out waiting for RigPilot database initialization")
    stream = None
    locked = False
    try:
        lock_path = normalized.with_name(normalized.name + ".initialize.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stream = lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                locked = True
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "timed out waiting for RigPilot database initialization"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        if stream is not None:
            try:
                if locked:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        thread_lock.release()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self, admin_username: str, admin_password: str | None) -> str | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _database_initialization_lock(self.path):
            return self._initialize_unlocked(admin_username, admin_password)

    def _initialize_unlocked(
        self, admin_username: str, admin_password: str | None
    ) -> str | None:
        with self.connection() as db:
            db.executescript(SCHEMA)
            user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
            if "session_version" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
            # This migration may need to rebuild a table referenced by the
            # update ledger.  Run it before any ALTER/UPDATE below opens an
            # implicit transaction so PRAGMA foreign_keys can be changed
            # deliberately and restored safely around the rebuild.
            self._migrate_agent_releases_table(db)
            self._migrate_agents_table(db)
            self._migrate_miner_packages_table(db)
            self._retire_dead_tables(db)
            self._migrate_flight_sheets_table(db)
            ensure_flight_v2_storage(db)
            self._migrate_commands_table(db)
            self._migrate_agent_update_jobs_table(db)
            self._migrate_configuration_refreshes_table(db)
            self._migrate_controller_preferences_table(db)
            self._migrate_workload_coin_tables(db)
            # This timestamp is the boundary between legacy raw telemetry and
            # the compact five-minute history.  Creating it is O(1): existing
            # multi-gigabyte telemetry is intentionally not backfilled during
            # startup, so a deployment cannot be held up by a full-table scan.
            db.execute(
                """INSERT OR IGNORE INTO hashrate_history_rollup_state
                   (singleton, started_at) VALUES(1, ?)""",
                (utcnow(),),
            )
            existing = db.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
            if existing:
                return None
            generated_password = None
            if not admin_password:
                import secrets

                generated_password = secrets.token_urlsafe(18)
                admin_password = generated_password
            created_admin = db.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                (
                    admin_username,
                    hash_password(admin_password, min_length=1),
                    "admin",
                    utcnow(),
                ),
            )
            db.execute(
                """INSERT INTO controller_preferences
                   (key,value,updated_by,updated_at,revision) VALUES(?,?,?,?,1)""",
                (INSTALLATION_SETUP_KEY,
                 json.dumps({"status": "pending", "step": "password", "password_changed": False}),
                 created_admin.lastrowid, utcnow()),
            )
            self.audit(
                db,
                actor="system",
                action="bootstrap_admin",
                target_type="user",
                target_id=admin_username,
            )
            return generated_password


    @staticmethod
    def _migrate_workload_coin_tables(db: sqlite3.Connection) -> None:
        """Widen legacy one/two-coin constraints while preserving stored ledgers."""
        legacy_check = re.compile(
            r"CHECK\s*\(\s*coin\s*(?:=\s*['\"]PEARL['\"]|IN\s*\(\s*(?:"
            r"['\"]PEARL['\"]\s*,\s*['\"]QUANTUS['\"]|"
            r"['\"]QUANTUS['\"]\s*,\s*['\"]PEARL['\"])\s*\))\s*\)", re.IGNORECASE,
        )
        tables = []
        for table in ("device_work_state", "device_work_buckets"):
            row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()
            if row and legacy_check.search(row["sql"] or ""):
                tables.append((table, row["sql"]))
        if not tables:
            return

        # Both are child tables of agents with no incoming references. Keep
        # foreign keys enabled and replace both within one rollback boundary.
        db.execute("SAVEPOINT workload_coin_migration")
        try:
            for table, old_sql in tables:
                objects = db.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name=? "
                    "AND type IN ('index','trigger') AND sql IS NOT NULL", (table,),
                ).fetchall()
                columns = [row["name"] for row in db.execute(f'PRAGMA table_info("{table}")')]
                column_sql = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
                widened_sql = legacy_check.sub(f"CHECK({_WORKLOAD_COIN_CHECK})", old_sql)
                new_table = f"{table}_coin_migration"
                create_sql, replacements = re.subn(
                    rf'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"{table}"|`{table}`|\[{table}\]|{table})',
                    f'CREATE TABLE "{new_table}"', widened_sql, count=1, flags=re.IGNORECASE,
                )
                if replacements != 1:
                    raise sqlite3.IntegrityError("cannot identify legacy workload table")
                db.execute(create_sql)
                db.execute(
                    f'INSERT INTO "{new_table}" ({column_sql}) SELECT {column_sql} FROM "{table}"'
                )
                db.execute(f'DROP TABLE "{table}"')
                db.execute(f'ALTER TABLE "{new_table}" RENAME TO "{table}"')
                for schema_object in objects:
                    db.execute(schema_object["sql"])
                if db.execute(f'PRAGMA foreign_key_check("{table}")').fetchone():
                    raise sqlite3.IntegrityError("workload migration left invalid foreign keys")
        except Exception:
            db.execute("ROLLBACK TO SAVEPOINT workload_coin_migration")
            db.execute("RELEASE SAVEPOINT workload_coin_migration")
            raise
        db.execute("RELEASE SAVEPOINT workload_coin_migration")

    @staticmethod
    def _retire_dead_tables(db: sqlite3.Connection) -> None:
        """Retire removed features atomically without scanning telemetry on startup."""

        existing = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if existing.isdisjoint(_RETIRED_TABLES):
            return

        db.execute("SAVEPOINT retire_dead_tables")
        try:
            # Dropping a parent implicitly deletes its rows. A cascading FK
            # from a live table can silently erase its data before a post-drop
            # foreign_key_check gets a chance to see the original reference.
            for table in existing.difference(_RETIRED_TABLES):
                if table.startswith("sqlite_"):
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                for fk in db.execute(f"PRAGMA foreign_key_list({quoted})"):
                    if fk["table"].lower() in _RETIRED_TABLES:
                        raise sqlite3.IntegrityError(
                            f"Cannot retire {fk['table']}: foreign-key reference from {table}"
                        )
            for table in _RETIRED_TABLES:
                if table in existing:
                    db.execute(f'DROP TABLE "{table}"')
        except Exception:
            db.execute("ROLLBACK TO SAVEPOINT retire_dead_tables")
            db.execute("RELEASE SAVEPOINT retire_dead_tables")
            raise
        db.execute("RELEASE SAVEPOINT retire_dead_tables")

    @staticmethod
    def _migrate_configuration_refreshes_table(
        db: sqlite3.Connection,
    ) -> None:
        """Add generation/claim fencing to the durable configuration outbox."""

        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(configuration_refreshes)")
        }
        if "generation_id" not in columns:
            db.execute(
                "ALTER TABLE configuration_refreshes ADD COLUMN "
                "generation_id TEXT NOT NULL DEFAULT ''"
            )
            db.execute(
                """UPDATE configuration_refreshes
                   SET generation_id=agent_id || ':' || requested_at
                   WHERE generation_id=''"""
            )
        if "claim_token" not in columns:
            db.execute(
                "ALTER TABLE configuration_refreshes ADD COLUMN claim_token TEXT"
            )

    @staticmethod
    def _migrate_controller_preferences_table(db: sqlite3.Connection) -> None:
        """Add optimistic-concurrency fencing to persistent UI preferences."""

        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(controller_preferences)")
        }
        if "revision" not in columns:
            db.execute(
                "ALTER TABLE controller_preferences ADD COLUMN "
                "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)"
            )

    @staticmethod
    def _migrate_agents_table(db: sqlite3.Connection) -> None:
        """Add operator, capability and network fields to older databases."""
        columns = {row["name"] for row in db.execute("PRAGMA table_info(agents)")}
        if "note" not in columns:
            db.execute("ALTER TABLE agents ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        if "capabilities_json" not in columns:
            # Old Agents never advertised the update protocol.  An empty list
            # is deliberately fail-closed so a controller upgrade cannot send
            # an unknown command to them.
            db.execute(
                "ALTER TABLE agents ADD COLUMN capabilities_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "public_ip" not in columns:
            db.execute("ALTER TABLE agents ADD COLUMN public_ip TEXT")
        if "local_ip_addresses_json" not in columns:
            db.execute(
                "ALTER TABLE agents ADD COLUMN local_ip_addresses_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "desired_gpu_oc_json" not in columns:
            # This is the controller-owned desired NVML state.  It is kept
            # independently from the Agent's report of what is currently
            # applied, because a host reboot or an explicit mining stop resets
            # hardware state without deleting the operator's saved settings.
            db.execute(
                "ALTER TABLE agents ADD COLUMN desired_gpu_oc_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "desired_gpu_oc_pending_json" not in columns:
            # UUIDs left here are desired controller-owned OC settings that
            # could not yet be mapped/applied (for example while GPU inventory
            # is still warming up after boot). Heartbeats retry only this
            # bounded set once mining is active.
            db.execute(
                "ALTER TABLE agents ADD COLUMN desired_gpu_oc_pending_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "gpu_oc_migration_marker_json" not in columns:
            # A successful legacy-state adoption is a permanent tombstone:
            # deleting the controller-owned desired OC later must never make
            # an old v1 Agent report silently recreate it.  Failed/skipped
            # fingerprints also suppress duplicate heartbeat audits.
            db.execute(
                "ALTER TABLE agents ADD COLUMN gpu_oc_migration_marker_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        if "mining_autostart_enabled" not in columns:
            db.execute(
                "ALTER TABLE agents ADD COLUMN mining_autostart_enabled "
                "INTEGER NOT NULL DEFAULT 0 "
                "CHECK(mining_autostart_enabled IN (0,1))"
            )
        if "mining_autostart_last_boot_time" not in columns:
            # This controller-owned boot marker makes the transition
            # edge-triggered.  A successfully queued restart is recorded in
            # the same transaction so duplicate heartbeats cannot queue the
            # same automatic start twice.
            db.execute(
                "ALTER TABLE agents ADD COLUMN mining_autostart_last_boot_time TEXT"
            )

    @staticmethod
    def _migrate_miner_packages_table(db: sqlite3.Connection) -> None:
        """Add controller-upload metadata without disturbing existing URL packages."""
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(miner_packages)")
        }
        additions = {
            "source_type": "TEXT NOT NULL DEFAULT 'external'",
            "storage_key": "TEXT",
            "original_filename": "TEXT",
            "file_size": "INTEGER",
            # Explicit markers let cache cleanup distinguish controller-managed
            # archives from every manually uploaded/imported package.
            "managed_miner_type": "TEXT",
            "managed_os_family": "TEXT",
            "managed_variant": "TEXT",
            "managed_channel": "TEXT NOT NULL DEFAULT 'stable'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE miner_packages ADD COLUMN {name} {declaration}")
        # HiveOS import analysis and the never-written upstream URL were
        # retired with their features; no code path reads them.
        for name in ("hiveos_custom_json", "hiveos_analysis_json", "upstream_url"):
            if name in columns:
                db.execute(f"ALTER TABLE miner_packages DROP COLUMN {name}")

    @staticmethod
    def _migrate_agent_releases_table(db: sqlite3.Connection) -> None:
        """Add Linux releases without breaking the update-ledger foreign key.

        SQLite cannot widen a CHECK constraint in place.  Copying into a new
        table and then replacing the old parent (rather than renaming the old
        parent) keeps ``agent_update_jobs.release_id`` pointed at the canonical
        table name.  The migration is fail-closed and verifies all foreign keys
        before normal initialization continues.
        """

        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(agent_releases)")
        }
        table_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_releases'"
        ).fetchone()
        table_sql = str(table_row["sql"] or "") if table_row else ""
        supports_linux = bool(
            re.search(
                r"platform\s+in\s*\([^)]*['\"]linux['\"]",
                table_sql,
                flags=re.IGNORECASE,
            )
        )
        if supports_linux:
            if "update_protocol" not in columns:
                db.execute(
                    "ALTER TABLE agent_releases ADD COLUMN update_protocol TEXT"
                )
            return

        update_protocol = (
            "update_protocol" if "update_protocol" in columns else "NULL"
        )
        try:
            db.executescript(
                f"""
                PRAGMA foreign_keys=OFF;
                BEGIN IMMEDIATE;
                DROP TABLE IF EXISTS agent_releases_new;
                CREATE TABLE agent_releases_new (
                    id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    platform TEXT NOT NULL CHECK(platform IN ('windows', 'linux')),
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
                INSERT INTO agent_releases_new
                    (id,version,platform,architecture,file_name,storage_key,
                     file_size,sha256,update_protocol,notes,created_by,created_at)
                SELECT id,version,platform,architecture,file_name,storage_key,
                       file_size,sha256,{update_protocol},notes,created_by,created_at
                FROM agent_releases;
                DROP TABLE agent_releases;
                ALTER TABLE agent_releases_new RENAME TO agent_releases;
                CREATE INDEX idx_agent_releases_created_at
                    ON agent_releases(created_at DESC);
                """
            )
            # The later update-ledger migration repairs this one obsolete FK.
            # Defer a violation only when this exact ledger row already has
            # its intended canonical command; unrelated failures still abort.
            old_command_fk = db.execute(
                """SELECT id FROM pragma_foreign_key_list('agent_update_jobs')
                   GROUP BY id HAVING COUNT(*)=1 AND MIN("table")='commands_legacy'
                   AND MIN("from")='command_id' AND MIN("to")='id'"""
            ).fetchone()
            for violation in db.execute("PRAGMA foreign_key_check"):
                if (
                    old_command_fk is not None
                    and violation["table"] == "agent_update_jobs"
                    and violation["parent"] == "commands_legacy"
                    and violation["fkid"] == old_command_fk["id"]
                    and db.execute(
                        """SELECT 1 FROM agent_update_jobs j
                           JOIN commands c ON c.id=j.command_id WHERE j.rowid=?""",
                        (violation["rowid"],),
                    ).fetchone()
                ):
                    continue
                raise sqlite3.IntegrityError(
                    "agent release migration left invalid foreign-key references"
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _migrate_flight_sheets_table(db: sqlite3.Connection) -> None:
        """Add optional package and second-miner fields to 0.2 databases."""
        columns = {row["name"] for row in db.execute("PRAGMA table_info(flight_sheets)")}
        additions = {
            # Existing rows predate managed miners.  Mark them generic so an
            # upgrade never starts a downloaded executable unexpectedly.
            "miner_type": "TEXT NOT NULL DEFAULT 'generic'",
            "miner_version": "TEXT",
            "miner_package_id": (
                "TEXT REFERENCES miner_packages(id) ON DELETE RESTRICT"
            ),
            "execution_mode": (
                "TEXT NOT NULL DEFAULT 'native' "
                "CHECK(execution_mode IN ('native', 'wsl'))"
            ),
            "algorithm": "TEXT",
            "pool_password": "TEXT NOT NULL DEFAULT 'x'",
            "device_indices_json": "TEXT NOT NULL DEFAULT '[]'",
            "secondary_mode": (
                "TEXT NOT NULL DEFAULT 'integrated' "
                "CHECK(secondary_mode IN ('integrated', 'independent'))"
            ),
            "secondary_miner_type": "TEXT",
            "secondary_miner_version": "TEXT",
            "secondary_coin": "TEXT",
            "secondary_wallet_id": (
                "TEXT REFERENCES wallets(id) ON DELETE RESTRICT"
            ),
            "secondary_pool_url": "TEXT",
            "secondary_miner_profile": "TEXT",
            "secondary_miner_package_id": (
                "TEXT REFERENCES miner_packages(id) ON DELETE RESTRICT"
            ),
            "secondary_execution_mode": (
                "TEXT CHECK(secondary_execution_mode IN ('native', 'wsl'))"
            ),
            "secondary_algorithm": "TEXT",
            "secondary_pool_password": "TEXT",
            "secondary_device_indices_json": "TEXT",
            "secondary_extra_config_json": "TEXT",
            "gpu_oc_json": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE flight_sheets ADD COLUMN {name} {declaration}")
        # The optional secondary column is nullable only when no second miner
        # exists.  Normalize upgraded dual sheets so ``NULL`` never means two
        # different things (legacy native versus no secondary leg).
        db.execute(
            """UPDATE flight_sheets
               SET execution_mode='native'
               WHERE execution_mode IS NULL OR execution_mode NOT IN ('native','wsl')"""
        )
        db.execute(
            """UPDATE flight_sheets
               SET secondary_execution_mode='native'
               WHERE secondary_coin IS NOT NULL
                 AND secondary_execution_mode IS NULL"""
        )
        # Managed flight sheets created by the first 0.5 development build did
        # not persist a version. Pin them to that build's known-good release
        # instead of silently following a future GitHub latest tag.
        db.execute(
            """UPDATE flight_sheets
               SET miner_version=CASE miner_type
                   WHEN 'xmrig' THEN '6.26.0'
                   WHEN 'srbminer' THEN '3.4.6'
                   ELSE miner_version END
               WHERE miner_version IS NULL"""
        )
        db.execute(
            """UPDATE flight_sheets
               SET secondary_miner_version=CASE secondary_miner_type
                   WHEN 'xmrig' THEN '6.26.0'
                   WHEN 'srbminer' THEN '3.4.6'
                   ELSE secondary_miner_version END
               WHERE secondary_miner_version IS NULL"""
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_flight_sheets_miner_package "
            "ON flight_sheets(miner_package_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_flight_sheets_secondary_wallet "
            "ON flight_sheets(secondary_wallet_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_flight_sheets_secondary_package "
            "ON flight_sheets(secondary_miner_package_id)"
        )
        # Pre-0.5.1 SRBMiner development builds allowed fan control inside
        # flight-sheet OC. Keep SRBMiner's five clock/power controls while
        # preserving other adapters' own fan settings during later startups.
        legacy_rows = db.execute(
            """SELECT id, gpu_oc_json FROM flight_sheets
               WHERE miner_type='srbminer' AND gpu_oc_json IS NOT NULL"""
        ).fetchall()
        for row in legacy_rows:
            try:
                gpu_oc = json.loads(row["gpu_oc_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(gpu_oc, dict) or not isinstance(
                gpu_oc.get("settings"), list
            ):
                continue
            changed = False
            cleaned_settings: list[dict[str, Any]] = []
            valid = True
            for setting in gpu_oc["settings"]:
                if not isinstance(setting, dict):
                    valid = False
                    break
                cleaned = dict(setting)
                if "fan_percent" in cleaned:
                    cleaned.pop("fan_percent")
                    changed = True
                if set(cleaned) != {"index"}:
                    cleaned_settings.append(cleaned)
            if not valid or not changed:
                continue
            if cleaned_settings:
                gpu_oc["settings"] = cleaned_settings
                encoded: str | None = json.dumps(
                    gpu_oc, separators=(",", ":"), ensure_ascii=False
                )
            else:
                encoded = None
            db.execute(
                "UPDATE flight_sheets SET gpu_oc_json=? WHERE id=?",
                (encoded, row["id"]),
            )

    @staticmethod
    def _migrate_commands_table(db: sqlite3.Connection) -> None:
        """Upgrade pre-release task expiry and transient-command metadata."""
        columns = {row["name"] for row in db.execute("PRAGMA table_info(commands)")}
        if "expires_at" not in columns:
            # Build the replacement under a new name. Renaming the old parent
            # retargets every incoming FK, including freshly created cache and
            # refresh tables, to a legacy name that will then be dropped.
            db.executescript(
                """
            PRAGMA foreign_keys=OFF;
            BEGIN IMMEDIATE;
            CREATE TABLE commands_new (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL CHECK(status IN
                    ('queued', 'dispatched', 'running', 'succeeded', 'failed',
                     'cancelled', 'expired')),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                hard_expires_at TEXT,
                dispatched_at TEXT,
                finished_at TEXT,
                result_json TEXT,
                error TEXT,
                is_transient INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO commands_new
                (id, agent_id, action, payload_json, status, created_by, created_at,
                 expires_at, hard_expires_at, dispatched_at, finished_at,
                 result_json, error,
                 is_transient)
            SELECT id, agent_id, action, payload_json, status, created_by, created_at,
                   created_at, created_at, dispatched_at, finished_at,
                   result_json, error, 0
            FROM commands;
            DROP TABLE commands;
            ALTER TABLE commands_new RENAME TO commands;
            CREATE INDEX idx_commands_agent_status
                ON commands(agent_id, status, created_at);
            COMMIT;
            PRAGMA foreign_keys=ON;
            """
            )
            columns.add("is_transient")
            columns.add("hard_expires_at")
        if "is_transient" not in columns:
            db.execute(
                "ALTER TABLE commands ADD COLUMN is_transient INTEGER NOT NULL DEFAULT 0"
            )
        if "hard_expires_at" not in columns:
            db.execute("ALTER TABLE commands ADD COLUMN hard_expires_at TEXT")
            db.execute(
                """UPDATE commands SET hard_expires_at=expires_at
                   WHERE hard_expires_at IS NULL"""
            )

    @staticmethod
    def _migrate_agent_update_jobs_table(db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE IF NOT EXISTS agent_update_jobs (
                command_id TEXT PRIMARY KEY REFERENCES commands(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                release_id TEXT NOT NULL REFERENCES agent_releases(id) ON DELETE RESTRICT,
                source_version TEXT NOT NULL,
                target_version TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('queued','dispatched','activating','succeeded','failed',
                     'cancelled','expired')),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                downloaded_at TEXT,
                activation_id TEXT,
                target_heartbeat_count INTEGER NOT NULL DEFAULT 0,
                activate_remote_workspace INTEGER NOT NULL DEFAULT 0,
                post_update_command_id TEXT,
                completed_at TEXT,
                last_error TEXT
            )"""
        )
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(agent_update_jobs)")
        }
        additions = {
            "downloaded_at": "TEXT",
            "activation_id": "TEXT",
            "target_heartbeat_count": "INTEGER NOT NULL DEFAULT 0",
            "target_heartbeat_at": "TEXT",
            "stalled_at": "TEXT",
            "activate_remote_workspace": "INTEGER NOT NULL DEFAULT 0",
            "post_update_command_id": "TEXT",
            "completed_at": "TEXT",
            "last_error": "TEXT",
            "oc_recovery_json": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(
                    f"ALTER TABLE agent_update_jobs ADD COLUMN {name} {declaration}"
                )
        foreign_targets = {
            row["table"]
            for row in db.execute("PRAGMA foreign_key_list(agent_update_jobs)")
        }
        if "commands_legacy" in foreign_targets:
            # On a pre-expiry database SCHEMA used to create this table before
            # commands was rebuilt; SQLite then retargeted its FK during the
            # rename. Rebuild the empty/new ledger after commands migration.
            try:
                db.executescript(
                    """
                    PRAGMA foreign_keys=OFF;
                    BEGIN IMMEDIATE;
                    DROP INDEX IF EXISTS idx_agent_update_jobs_agent;
                    DROP INDEX IF EXISTS idx_agent_update_jobs_release;
                    DROP INDEX IF EXISTS idx_agent_update_jobs_one_active;
                    ALTER TABLE agent_update_jobs RENAME TO agent_update_jobs_legacy;
                    CREATE TABLE agent_update_jobs (
                        command_id TEXT PRIMARY KEY REFERENCES commands(id) ON DELETE CASCADE,
                        agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                        release_id TEXT NOT NULL REFERENCES agent_releases(id) ON DELETE RESTRICT,
                        source_version TEXT NOT NULL,
                        target_version TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN
                            ('queued','dispatched','activating','succeeded','failed',
                             'cancelled','expired')),
                        created_by INTEGER NOT NULL REFERENCES users(id),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        downloaded_at TEXT,
                        activation_id TEXT,
                        target_heartbeat_count INTEGER NOT NULL DEFAULT 0,
                        target_heartbeat_at TEXT,
                        stalled_at TEXT,
                        activate_remote_workspace INTEGER NOT NULL DEFAULT 0,
                        post_update_command_id TEXT,
                        completed_at TEXT,
                        last_error TEXT,
                        oc_recovery_json TEXT
                    );
                    INSERT INTO agent_update_jobs
                        (command_id,agent_id,release_id,source_version,target_version,
                         status,created_by,created_at,updated_at,downloaded_at,
                         activation_id,target_heartbeat_count,target_heartbeat_at,stalled_at,
                         activate_remote_workspace,post_update_command_id,completed_at,last_error,
                         oc_recovery_json)
                    SELECT command_id,agent_id,release_id,source_version,target_version,
                           status,created_by,created_at,updated_at,downloaded_at,
                           activation_id,target_heartbeat_count,target_heartbeat_at,stalled_at,
                           activate_remote_workspace,post_update_command_id,
                           completed_at,last_error,oc_recovery_json
                    FROM agent_update_jobs_legacy;
                    DROP TABLE agent_update_jobs_legacy;
                    """
                )
                if db.execute("PRAGMA foreign_key_check(agent_update_jobs)").fetchone():
                    raise sqlite3.IntegrityError(
                        "agent update ledger migration left invalid foreign-key references"
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_agent_update_jobs_agent
               ON agent_update_jobs(agent_id, created_at DESC)"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_agent_update_jobs_release
               ON agent_update_jobs(release_id, created_at DESC)"""
        )
        db.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_update_jobs_one_active
               ON agent_update_jobs(agent_id)
               WHERE status IN ('queued','dispatched','activating')"""
        )

    @staticmethod
    def audit(
        db: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            """INSERT INTO audit_logs
               (actor, action, target_type, target_id, details_json, created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                actor,
                action,
                target_type,
                target_id,
                json.dumps(details or {}, separators=(",", ":")),
                utcnow(),
            ),
        )

    def prune_telemetry(
        self,
        retention_days: int,
        *,
        max_rows: int = 10_000,
    ) -> int:
        """Delete one bounded batch of expired raw heartbeat telemetry.

        The controller used to remove every expired row in one transaction.
        Lowering retention on an existing multi-gigabyte database could then
        block startup and grow a very large WAL.  A time-indexed id subquery
        keeps each transaction bounded; the maintenance loop schedules extra
        batches while a backlog remains.
        """

        if (
            isinstance(max_rows, bool)
            or not isinstance(max_rows, int)
            or max_rows < 1
        ):
            raise ValueError("max_rows must be a positive integer")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.connection() as db:
            cursor = db.execute(
                """DELETE FROM telemetry
                   WHERE id IN (
                       SELECT id FROM telemetry
                       WHERE recorded_at < ?
                       ORDER BY recorded_at, id
                       LIMIT ?
                   )""",
                (cutoff, max_rows),
            )
            return cursor.rowcount

    def prune_hashrate_history(self, retention_days: int) -> int:
        """Retain compact five-minute hashrate buckets independently."""

        cutoff_moment = datetime.now(timezone.utc) - timedelta(days=retention_days)
        cutoff = workload_bucket_start(cutoff_moment).isoformat(timespec="seconds")
        with self.connection() as db:
            cursor = db.execute(
                "DELETE FROM hashrate_history_buckets WHERE bucket_start < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def prune_device_work(self, retention_days: int) -> int:
        """Delete buckets before the oldest complete five-minute boundary."""

        cutoff_moment = datetime.now(timezone.utc) - timedelta(days=retention_days)
        cutoff = workload_bucket_start(cutoff_moment).isoformat(timespec="seconds")
        with self.connection() as db:
            cursor = db.execute(
                "DELETE FROM device_work_buckets WHERE bucket_start < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def prune_enrollment_tokens(self, retention_days: int) -> int:
        """Delete only long-expired or long-used one-time token metadata."""

        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.connection() as db:
            cursor = db.execute(
                """DELETE FROM enrollment_tokens
                   WHERE expires_at < ?
                      OR (used_at IS NOT NULL AND used_at < ?)""",
                (cutoff, cutoff),
            )
            return cursor.rowcount

    def prune_agent_replacement_tokens(self, retention_days: int) -> int:
        """Delete only long-expired or long-used device inheritance metadata."""

        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat(
            timespec="seconds"
        )
        with self.connection() as db:
            cursor = db.execute(
                """DELETE FROM agent_replacement_tokens
                   WHERE expires_at < ?
                      OR (used_at IS NOT NULL AND used_at < ?)""",
                (cutoff, cutoff),
            )
            return cursor.rowcount
