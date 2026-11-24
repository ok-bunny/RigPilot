"""0.8.50 cleanup batch: dead-surface retirement and GET-ledger transactions.

Covers the schema retirement migration (old databases upgrade in place, dead
tables/columns disappear, live data survives), the retired API surface, and
the ``list_agent_updates`` snapshot/writer transaction boundary.

0.8.51 adds the startup-cost regressions for the retirement migration: the
schema-probe fast path (migrated/fresh databases run no retirement work) and
the bounded, still fail-closed, post-retirement foreign-key verification.
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login, controller_settings_kwargs

from server.config import Settings
from server.database import Database
from server.main import create_app

ROOT = Path(__file__).resolve().parents[1]


RETIRED_TABLES = (
    "nvidia_driver_cache_sets",
    "nvidia_driver_cache_objects",
    "nvidia_driver_cache_members",
    "nvidia_driver_cache_jobs",
    "nvidia_driver_update_cache_bindings",
    "hiveos_package_update_imports",
    "hiveos_flight_imports",
    "remote_sessions",
    "gpu_profiles",
)
DEAD_MINER_PACKAGE_COLUMNS = ("hiveos_custom_json", "hiveos_analysis_json", "upstream_url")
HEX64 = "ab" * 32

# A faithful pre-0.8.50 schema fragment: every retired table plus the
# miner_packages table still carrying its three dead columns.
LEGACY_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE miner_packages (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    download_url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    archive_type TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    hiveos_custom_json TEXT,
    hiveos_analysis_json TEXT,
    upstream_url TEXT
);
CREATE TABLE remote_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    username TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    closed_at TEXT,
    error TEXT
);
CREATE TABLE gpu_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    vendor TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE hiveos_package_update_imports (
    user_id INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    claim_token TEXT,
    expires_at TEXT NOT NULL,
    miner_package_id TEXT,
    package_reused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, request_id)
);
CREATE TABLE hiveos_flight_imports (
    user_id INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    claim_token TEXT,
    expires_at TEXT NOT NULL,
    flight_sheet_id TEXT,
    package_reused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, request_id)
);
CREATE TABLE nvidia_driver_cache_sets (
    id TEXT PRIMARY KEY,
    ubuntu_version TEXT NOT NULL,
    repository_distro TEXT NOT NULL,
    architecture TEXT NOT NULL,
    driver_version TEXT NOT NULL,
    artifact_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    repository_release_sha256 TEXT NOT NULL,
    packages_index_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ready_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL
);
CREATE TABLE nvidia_driver_cache_objects (
    sha256 TEXT PRIMARY KEY,
    file_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL
);
CREATE TABLE nvidia_driver_cache_members (
    cache_set_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    package_name TEXT NOT NULL,
    package_version TEXT NOT NULL,
    filename TEXT NOT NULL,
    architecture TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    object_sha256 TEXT NOT NULL,
    PRIMARY KEY(cache_set_id, package_name)
);
CREATE TABLE nvidia_driver_cache_jobs (
    id TEXT PRIMARY KEY,
    requested_agent_id TEXT NOT NULL,
    ubuntu_version TEXT NOT NULL,
    architecture TEXT NOT NULL,
    driver_version TEXT NOT NULL,
    requested_plan_fingerprint TEXT NOT NULL,
    package_plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    cache_set_id TEXT,
    reserved_bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    log_json TEXT NOT NULL DEFAULT '[]',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE nvidia_driver_update_cache_bindings (
    command_id TEXT PRIMARY KEY,
    cache_set_id TEXT,
    cache_manifest_sha256 TEXT NOT NULL,
    downloaded_at TEXT,
    completed_at TEXT
);
"""


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"] if isinstance(row, sqlite3.Row) else row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _build_legacy_database(path: Path, *, with_users: bool) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        if with_users:
            connection.execute(
                "INSERT INTO users(id,username,password_hash,role,is_active,created_at)"
                " VALUES(1,'legacy-admin','unused','admin',1,'2026-01-01T00:00:00+00:00')"
            )
        # Live row that must survive the upgrade untouched.
        connection.execute(
            """INSERT INTO miner_packages
               (id,name,version,download_url,sha256,archive_type,note,created_by,
                created_at,updated_at,hiveos_custom_json,hiveos_analysis_json,upstream_url)
               VALUES('pkg-live','xmrig','6.27.0','https://example.test/x.tar.gz',
                      ?,'tar.gz','legacy note',1,'now','now',
                      '{"dead":true}','{"dead":2}','https://upstream.example/dead')""",
            (HEX64,),
        )
        connection.execute(
            "INSERT INTO remote_sessions VALUES('legacy-session','agent','shell',"
            "'miner','closed',1,'now','later','now','later','gone')"
        )
        connection.execute(
            "INSERT INTO gpu_profiles VALUES('legacy-profile','legacy','nvidia',"
            "'[]',1,'now','now')"
        )
        for table in ("hiveos_package_update_imports", "hiveos_flight_imports"):
            connection.execute(
                f"INSERT INTO {table} VALUES(1,'r1',?,'failed',NULL,'later',"
                "NULL,0,'now','now')",
                (HEX64,),
            )
        object_sha = HEX64
        connection.execute(
            "INSERT INTO nvidia_driver_cache_sets VALUES('set-1','22.04','ubuntu',"
            "'x86_64','580',1,1,?,?,"
            "'now','now','now')",
            (HEX64, HEX64),
        )
        connection.execute(
            "INSERT INTO nvidia_driver_cache_objects VALUES(?,1,'now','now')",
            (object_sha,),
        )
        connection.execute(
            "INSERT INTO nvidia_driver_cache_members VALUES('set-1',0,'nvidia-driver',"
            "'580','driver.deb','amd64',1,?)",
            (object_sha,),
        )
        connection.execute(
            "INSERT INTO nvidia_driver_cache_jobs VALUES('job-1','agent','22.04',"
            "'x86_64','580',?,'[]','failed',NULL,0,NULL,'[]',"
            "1,'now',NULL,'now')",
            (HEX64,),
        )
        connection.execute(
            "INSERT INTO nvidia_driver_update_cache_bindings VALUES('cmd-1','set-1',"
            "?,NULL,NULL)",
            (HEX64,),
        )


def test_upgrade_drops_retired_tables_and_dead_columns_but_keeps_live_rows(
    tmp_path: Path,
):
    path = tmp_path / "legacy-0849.db"
    _build_legacy_database(path, with_users=True)

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        tables_before = _table_names(connection)
        package_columns_before = _columns(connection, "miner_packages")

    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        tables_after = _table_names(connection)
        package_columns_after = _columns(connection, "miner_packages")
        package = connection.execute(
            "SELECT name, version, note FROM miner_packages WHERE id='pkg-live'"
        ).fetchone()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        users = connection.execute(
            "SELECT username FROM users WHERE id=1"
        ).fetchone()

    # Schema diff: exactly the retired tables disappear, nothing else is lost.
    assert tables_before & set(RETIRED_TABLES) == set(RETIRED_TABLES)
    assert not tables_after & set(RETIRED_TABLES)
    assert (tables_before - set(RETIRED_TABLES)) <= tables_after
    # Column diff: exactly the three dead miner_packages columns disappear.
    assert package_columns_before & set(DEAD_MINER_PACKAGE_COLUMNS) == set(
        DEAD_MINER_PACKAGE_COLUMNS
    )
    assert not package_columns_after & set(DEAD_MINER_PACKAGE_COLUMNS)
    assert (package_columns_before - set(DEAD_MINER_PACKAGE_COLUMNS)) <= (
        package_columns_after
    )
    # Live rows survive the upgrade untouched.
    assert tuple(package) == ("xmrig", "6.27.0", "legacy note")
    assert users["username"] == "legacy-admin"
    assert violations == []

    # Idempotent: a second initialize neither recreates nor fails.
    database.initialize("admin", "correct-horse-battery-staple")
    with sqlite3.connect(path) as connection:
        assert not _table_names(connection) & set(RETIRED_TABLES)


# --- 0.8.51 startup-cost fix: fast path + bounded fail-closed check ---------

ORPHAN_SCHEMA = """
CREATE TABLE orphan_gadgets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES remote_sessions(id),
    note TEXT NOT NULL DEFAULT ''
);
"""


def _build_legacy_database_with_orphan_rows(path: Path, *, cascade: bool = False) -> None:
    """Legacy 0.8.49 shape plus a live table referencing a retired table.

    The dangling rows are written with foreign-key enforcement off, the way
    an offline or corrupted writer would leave them, so DROP-time
    enforcement alone cannot be relied on to surface them.
    """
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        references = ORPHAN_SCHEMA.replace(
            "REFERENCES remote_sessions(id)", "REFERENCES remote_sessions(id) ON DELETE CASCADE"
        ) if cascade else ORPHAN_SCHEMA
        connection.executescript(LEGACY_SCHEMA + references)
        connection.execute(
            "INSERT INTO remote_sessions VALUES('dangling','agent','shell',"
            "'miner','closed',1,'now','later','now','later','gone')"
        )
        connection.execute(
            "INSERT INTO orphan_gadgets(id,session_id) VALUES('g1','dangling')"
        )


def test_second_initialize_pays_no_migration_cost_on_a_migrated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """0.8.51 regression: after the migration, startup runs no retirement work.

    0.8.50 executed the DROP script plus a whole-database
    ``PRAGMA foreign_key_check`` on every startup (~90s on the 2.5 GB
    production database).  A traced second initialize must not touch the
    retired tables at all.
    """
    path = tmp_path / "legacy-0849-fastpath.db"
    _build_legacy_database(path, with_users=True)
    database = Database(path)
    database.initialize("admin", "correct-horse-battery-staple")

    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    database.initialize("admin", "correct-horse-battery-staple")

    executed = "\n".join(statements)
    assert "DROP TABLE" not in executed
    assert "ALTER TABLE" not in executed
    assert "foreign_key_check" not in executed
    for name in RETIRED_TABLES:
        assert name not in executed
    with sqlite3.connect(path) as connection:
        assert not _table_names(connection) & set(RETIRED_TABLES)


def test_fresh_database_initialize_pays_no_migration_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """New installs never run retirement work either (fast probe path)."""
    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    Database(tmp_path / "fresh-0851.db").initialize(
        "admin", "correct-horse-battery-staple"
    )

    executed = "\n".join(statements)
    assert "DROP TABLE" not in executed
    assert "foreign_key_check" not in executed
    for name in RETIRED_TABLES:
        assert name not in executed


def test_retirement_bounded_check_fails_closed_on_dangling_references(
    tmp_path: Path,
):
    """An unenforced FK must still protect the original rows and schema."""
    path = tmp_path / "legacy-0849-orphan.db"
    _build_legacy_database_with_orphan_rows(path)

    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        with pytest.raises(sqlite3.IntegrityError, match="foreign-key"):
            Database._retire_dead_tables(db)
    finally:
        db.close()

    # The live reference and its original target must both survive the refusal.
    with sqlite3.connect(path) as connection:
        assert set(RETIRED_TABLES) <= _table_names(connection)
        assert connection.execute("SELECT id FROM remote_sessions").fetchall() == [("dangling",)]
        orphan = connection.execute(
            "SELECT id, session_id FROM orphan_gadgets"
        ).fetchall()
    assert [(row[0], row[1]) for row in orphan] == [("g1", "dangling")]


@pytest.mark.parametrize("cascade", [False, True])
def test_retirement_fails_closed_through_initialize_when_live_rows_reference_dead_tables(
    tmp_path: Path, cascade: bool,
):
    """End to end: a dangling live reference aborts startup (0.8.50 semantics)."""
    path = tmp_path / "legacy-0849-violation.db"
    _build_legacy_database_with_orphan_rows(path, cascade=cascade)
    database = Database(path)
    for _ in range(2):
        with pytest.raises(sqlite3.IntegrityError, match="foreign-key"):
            database.initialize("admin", "correct-horse-battery-staple")
        with sqlite3.connect(path) as db:
            assert set(RETIRED_TABLES) <= _table_names(db)
            assert db.execute("SELECT id FROM remote_sessions").fetchall() == [("dangling",)]
            assert db.execute("SELECT id,session_id FROM orphan_gadgets").fetchall() == [("g1", "dangling")]


@pytest.fixture()
def upgraded_client(tmp_path: Path):
    path = tmp_path / "legacy-0849-api.db"
    _build_legacy_database(path, with_users=False)
    settings = Settings(
        **{**controller_settings_kwargs(tmp_path), "database_path": path}
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_agent_update_history_flows_work_on_an_upgraded_legacy_database(
    upgraded_client: TestClient,
):
    """The update-history page/API must stay healthy on a migrated library."""
    client = upgraded_client
    token = _login(client, "admin", "correct-horse-battery-staple")
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "upgraded-rig", "ttl_minutes": 30},
    )
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": "upgraded-rig",
            "hostname": "upgraded-rig.local",
            "os_family": "linux",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    agent_id = enrolled.json()["agent_id"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with client.app.state.db.connection() as db:
        user_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,update_protocol,notes,created_by,created_at)
               VALUES('release-0850','0.8.50','linux','x86_64','agent.tar.gz',
                      'release-0850/agent.tar.gz',1,?,'agent_update.v1','',?,?)""",
            ("1" * 64, user_id, now),
        )
        db.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at,
                expires_at,finished_at,error)
               VALUES('update-0850',?,'agent_update.v1','{}','failed',?,?,?,?,'legacy blast')""",
            (agent_id, user_id, now, now, now),
        )
        db.execute(
            """INSERT INTO agent_update_jobs
               (command_id,agent_id,release_id,source_version,target_version,
                status,created_by,created_at,updated_at)
               VALUES('update-0850',?,'release-0850','0.8.49','0.8.50',
                      'queued',?,?,?)""",
            (agent_id, user_id, now, now),
        )

    listing = client.get("/api/agent-updates", headers=auth(token))
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["command_status"] == "failed"
    assert rows[0]["error"] == "legacy blast"

    cleared = client.delete("/api/agent-updates/history", headers=auth(token))
    assert cleared.status_code == 200, cleared.text
    assert client.get("/api/agent-updates", headers=auth(token)).json() == []


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _seed_update_job(client: TestClient, *, command_status: str = "failed") -> str:
    token = _login(client, "admin", "correct-horse-battery-staple")
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "ledger-rig", "ttl_minutes": 30},
    )
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": "ledger-rig",
            "hostname": "ledger-rig.local",
            "os_family": "linux",
        },
    )
    agent_id = enrolled.json()["agent_id"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with client.app.state.db.connection() as db:
        user_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,update_protocol,notes,created_by,created_at)
               VALUES('release-ledger','0.8.50','linux','x86_64','agent.tar.gz',
                      'release-ledger/agent.tar.gz',1,?,'agent_update.v1','',?,?)""",
            ("2" * 64, user_id, now),
        )
        db.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at,
                expires_at,finished_at,error)
               VALUES('update-ledger',?,'agent_update.v1','{}',?,
                      ?,?,?,?,'terminal in ledger')""",
            (agent_id, command_status, user_id, now, now, now),
        )
        db.execute(
            """INSERT INTO agent_update_jobs
               (command_id,agent_id,release_id,source_version,target_version,
                status,created_by,created_at,updated_at)
               VALUES('update-ledger',?,'release-ledger','0.8.49','0.8.50',
                      'queued',?,?,?)""",
            (agent_id, user_id, now, now),
        )
    return token, agent_id


def test_agent_updates_listing_mirrors_terminal_command_states(client: TestClient):
    token, _agent_id = _seed_update_job(client)
    listing = client.get("/api/agent-updates", headers=auth(token))
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "terminal in ledger"


def test_clean_listing_never_writes_or_takes_the_writer_lock(client: TestClient):
    token, _agent_id = _seed_update_job(client)
    # First GET performs the terminal mirror; after that the ledger is clean.
    assert client.get("/api/agent-updates", headers=auth(token)).status_code == 200
    with client.app.state.db.connection() as db:
        audit_before = db.execute(
            "SELECT COUNT(*) AS count FROM audit_logs"
        ).fetchone()["count"]
        updated_before = db.execute(
            "SELECT COUNT(*) AS count FROM agent_update_jobs"
        ).fetchone()["count"]
    for _ in range(5):
        assert (
            client.get("/api/agent-updates", headers=auth(token)).status_code == 200
        )
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM audit_logs").fetchone()[
            "count"
        ] == audit_before
        assert db.execute("SELECT COUNT(*) AS count FROM agent_update_jobs").fetchone()[
            "count"
        ] == updated_before


def test_listing_stays_correct_under_concurrent_heartbeat_writers(client: TestClient):
    """Regression for the GET-ledger snapshot upgrade (0.8.50).

    A deferred read transaction that upgrades itself to a writer can fail
    under a concurrent heartbeat commit. The listing must take the writer
    lock deliberately (BEGIN IMMEDIATE after dropping its read snapshot) and
    keep serving 200 responses while another thread hammers the writer.
    """
    token, agent_id = _seed_update_job(client)
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "writer-rig", "ttl_minutes": 30},
    )
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": "writer-rig",
            "hostname": "writer-rig.local",
            "os_family": "linux",
        },
    )
    writer_agent = enrolled.json()["agent_id"]
    writer_secret = enrolled.json()["agent_secret"]
    heartbeat = {
        "hostname": "writer-rig.local",
        "os_family": "linux",
        "agent_version": "0.8.49",
    }
    writer_headers = {
        "X-Agent-ID": writer_agent,
        "Authorization": f"Bearer {writer_secret}",
    }

    def hammer_heartbeats() -> list[int]:
        statuses = []
        for _ in range(40):
            response = client.post(
                "/api/agent/heartbeat", headers=writer_headers, json=heartbeat
            )
            statuses.append(response.status_code)
        return statuses

    def poll_ledger() -> list[int]:
        statuses = []
        for _ in range(40):
            response = client.get("/api/agent-updates", headers=auth(token))
            statuses.append(response.status_code)
        return statuses

    with ThreadPoolExecutor(max_workers=2) as pool:
        heartbeat_future = pool.submit(hammer_heartbeats)
        ledger_future = pool.submit(poll_ledger)
        heartbeat_statuses = heartbeat_future.result()
        ledger_statuses = ledger_future.result()

    assert heartbeat_statuses == [200] * 40
    assert ledger_statuses == [200] * 40
    listing = client.get("/api/agent-updates", headers=auth(token))
    assert listing.status_code == 200
    assert {row["agent_id"] for row in listing.json()} == {agent_id}


def test_smoke_target_advertises_current_capability_vocabulary():
    """The Spark-era synthetic capability string must not come back."""
    source = (ROOT / "deploy" / "smoke-controller-target.py").read_text(
        encoding="utf-8"
    )
    assert "spark_shell" not in source
    assert "remote_access.spark_shell.v1" not in source
    assert '"remote_access.exec.v1"' not in source
    assert '"remote_access.file.v1"' not in source


def test_flight_toolbar_renamed_off_the_dead_hiveos_class():
    """index.html:187 legacy button container now uses a neutral class."""
    index_html = (ROOT / "server" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    assert "hiveos-quick-import-actions" not in index_html
    assert 'class="flight-toolbar-actions"' in index_html
    assert ".flight-toolbar-actions {" in styles
