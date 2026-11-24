"""Exercise corrected Controller behavior through real routes and their closures."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, controller_settings_kwargs, login
from server import main as main_module
from server.config import Settings
from server.database import Database
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    app = create_app(settings)
    # Initialize the same temporary DB as lifespan, without unrelated timer
    # tasks racing SQL tracing. Maintenance itself is exercised below.
    app.state.db.initialize(settings.admin_username, settings.admin_password)
    value = TestClient(app)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture()
def admin(client):
    return auth(login(client, "admin", "correct-horse-battery-staple"))


def _agent(client, admin, name="glm-rig"):
    issued = client.post(
        "/api/enrollment-tokens", headers=admin, json={"label": name}
    )
    assert issued.status_code == 200, issued.text
    enrolled = client.post(
        "/api/agent/enroll",
        json={"enrollment_token": issued.json()["token"],
              "hostname": name, "os_family": "linux", "agent_version": "0.8.31"},
    )
    assert enrolled.status_code == 201, enrolled.text
    identity = enrolled.json()
    response = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": identity["agent_id"],
                 "Authorization": f"Bearer {identity['agent_secret']}"},
        json={"hostname": name, "os_family": "linux", "agent_version": "0.8.31",
              "capabilities": ["managed_mining.dual_process.v1"]},
    )
    assert response.status_code == 200, response.text
    return identity["agent_id"]


def _endpoint(client, path):
    return next(route.endpoint for route in client.app.routes
                if getattr(route, "path", None) == path)


def _expiry_helper(client):
    return inspect.getclosurevars(_endpoint(client, "/api/commands")).nonlocals[
        "expire_stale_agent_commands"
    ]


def _maintenance(client):
    lifespan = inspect.unwrap(client.app.router.lifespan_context)
    return inspect.getclosurevars(lifespan).nonlocals["maintenance_loop"]


def _one_maintenance_iteration(client, monkeypatch):
    calls = 0

    async def next_iteration(_delay):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(main_module.asyncio, "sleep", next_iteration)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_maintenance(client)())


def _trace(database, monkeypatch):
    statements = []
    original = database.connection

    @contextmanager
    def traced_connection():
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(database, "connection", traced_connection)
    return statements


def _writes(statements):
    return [sql for sql in statements
            if sql.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE "))]


def _seed_ledger(client, agents):
    """Represent persisted commands from before maintenance or a list read."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    past = "2000-01-01T00:00:00+00:00"
    with client.app.state.db.connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        db.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("glm-release", "0.8.31", "linux", "x86_64", "synthetic.tar.gz",
             "glm-synthetic-release", 1, "0" * 64, user_id, now),
        )
        db.execute(
            "INSERT INTO wallets(id,name,coin,address,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("glm-wallet", "fixture", "XMR", "synthetic-wallet", user_id, now, now),
        )
        db.execute(
            """INSERT INTO flight_sheets
               (id,name,coin,wallet_id,pool_url,miner_profile,worker_name_template,
                created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("glm-sheet", "fixture", "XMR", "glm-wallet", "pool.invalid:3333",
             "{}", "%WORKER_NAME%", user_id, now, now),
        )
        for kind, agent_id in agents.items():
            job_status = "dispatched" if kind == "update" else "queued" if kind == "update-error" else "activating"
            command_status = "running" if job_status == "activating" else job_status
            commands = [
                ("ordinary", "collect_diagnostics", "queued"),
                ("refresh", "apply_flight_sheet", "running"),
                (kind, "agent_update.v1", command_status),
            ]
            if kind == "activating-post":
                commands.append(("post", "configure_remote_access.v1", "queued"))
            for name, action, status in commands:
                db.execute(
                    """INSERT INTO commands
                       (id,agent_id,action,status,created_by,created_at,expires_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (f"{agent_id}/{name}", agent_id, action, status, user_id, past, past),
                )
            db.execute(
                """INSERT INTO agent_update_jobs
                   (command_id,agent_id,release_id,source_version,target_version,
                    status,created_by,created_at,updated_at,last_error,post_update_command_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{agent_id}/{kind}", agent_id, "glm-release", "0.8.30", "0.8.31",
                 job_status, user_id, past, now,
                 "earlier download error" if kind == "update-error" else None,
                 f"{agent_id}/post" if kind == "activating-post" else None),
            )
            db.execute(
                """INSERT INTO configuration_refreshes
                   (agent_id,flight_sheet_id,desired_fingerprint,generation_id,
                    trigger_type,trigger_id,status,command_id,requested_by,
                    requested_at,updated_at,queued_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (agent_id, "glm-sheet", "f" * 64, f"generation/{agent_id}",
                 "flight_sheet", "glm-sheet", "running", f"{agent_id}/refresh",
                 user_id, past, past, past),
            )


def _seed_scenarios(client, admin):
    # Respect the real partial unique index: one active update per device.
    agents = {kind: _agent(client, admin, f"glm-{kind}") for kind in
              ("update", "update-error", "activating", "activating-post")}
    _seed_ledger(client, agents)
    return agents


def _ledger_state(client):
    with client.app.state.db.connection() as db:
        return {
            "commands": {row["id"]: dict(row) for row in db.execute(
                "SELECT id,status,finished_at,error FROM commands ORDER BY id")},
            "jobs": {row["command_id"]: dict(row) for row in db.execute(
                "SELECT command_id,status,completed_at,last_error FROM agent_update_jobs "
                "ORDER BY command_id")},
            "refreshes": {row["agent_id"]: dict(row) for row in db.execute(
                "SELECT agent_id,status,command_id,queued_at,completed_at,last_error "
                "FROM configuration_refreshes ORDER BY agent_id")},
        }


def _assert_expiry_outcome(state, agents):
    for kind, agent_id in agents.items():
        for name in ("ordinary", "refresh"):
            assert state["commands"][f"{agent_id}/{name}"]["status"] == "expired"
        command = state["commands"][f"{agent_id}/{kind}"]
        job = state["jobs"][f"{agent_id}/{kind}"]
        if kind == "activating":
            assert command["status"] == "running" and job["status"] == "activating"
        elif kind == "activating-post":
            assert command["status"] == "failed" and job["status"] == "failed"
            assert state["commands"][f"{agent_id}/post"]["status"] == "expired"
            assert job["completed_at"] and "post-update" in job["last_error"]
            assert "expired" in job["last_error"]
        else:
            assert command["status"] == "expired" and job["status"] == "expired"
            assert job["completed_at"]
            assert job["last_error"] == ("earlier download error" if kind == "update-error"
                                         else "Agent update command expired")
        refresh = state["refreshes"][agent_id]
        assert refresh["status"] == "pending" and refresh["command_id"] is None
        assert refresh["queued_at"] is None and refresh["completed_at"] is None
        assert "expired" in refresh["last_error"]


def test_duplicate_username_is_409_without_changing_the_original_user(client, admin):
    payload = {"username": "glm-viewer", "password": "temporary-test-password", "role": "viewer"}
    assert client.post("/api/users", headers=admin, json=payload).status_code == 201
    response = client.post("/api/users", headers=admin, json={**payload, "role": "admin"})
    assert response.status_code == 409 and response.json()["detail"] == "Username already exists"
    with client.app.state.db.connection() as db:
        rows = db.execute("SELECT role FROM users WHERE username=?", (payload["username"],)).fetchall()
    assert [row["role"] for row in rows] == ["viewer"]


def test_audit_integrity_failure_remains_a_database_error_and_rolls_back_user_creation(client, admin, monkeypatch):
    original_audit = Database.audit

    def invalid_audit(db, **fields):
        if fields["action"] == "user_created":
            # A genuine SQLite non-UNIQUE error after the user insert.
            db.execute(
                "INSERT INTO audit_logs(actor,action,target_type,created_at) "
                "VALUES(NULL,'user_created','user','fixture')"
            )
        else:
            original_audit(db, **fields)

    monkeypatch.setattr(Database, "audit", staticmethod(invalid_audit))
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed: audit_logs.actor"):
        client.post("/api/users", headers=admin,
                    json={"username": "glm-rollback", "password": "temporary-test-password", "role": "viewer"})
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE username='glm-rollback'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_logs WHERE action='user_created'").fetchone()[0] == 0


def test_command_list_without_stale_rows_uses_a_read_snapshot_without_reserving_the_writer(client, admin, monkeypatch):
    agent_id = _agent(client, admin)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with client.app.state.db.connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        db.execute(
            "INSERT INTO commands(id,agent_id,action,status,created_by,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            ("glm-future-command", agent_id, "collect_diagnostics", "queued", user_id,
             now, "2999-01-01T00:00:00+00:00"),
        )
    statements = _trace(client.app.state.db, monkeypatch)
    response = client.get("/api/commands", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()[0]["status"] == "queued"
    assert any(sql.strip().upper() == "BEGIN" for sql in statements)
    assert not any(sql.strip().upper() == "BEGIN IMMEDIATE" for sql in statements)
    assert not _writes(statements)


def test_command_list_with_stale_rows_returns_expired_after_synchronizing_both_ledgers(client, admin, monkeypatch):
    agents = _seed_scenarios(client, admin)
    agent_id = agents["update"]
    post_agent = agents["activating-post"]
    statements = _trace(client.app.state.db, monkeypatch)
    response = client.get("/api/commands", headers=admin)
    assert response.status_code == 200, response.text
    returned = {command["id"]: command["status"] for command in response.json()}
    assert returned[f"{agent_id}/refresh"] == returned[f"{agent_id}/update"] == "expired"
    assert returned[f"{post_agent}/activating-post"] == "failed"
    assert any(sql.strip().upper() == "BEGIN IMMEDIATE" for sql in statements)
    _assert_expiry_outcome(_ledger_state(client), agents)
    statements.clear()
    assert client.get("/api/commands", headers=admin).status_code == 200
    assert not _writes(statements)
    assert not any(sql.strip().upper() == "BEGIN IMMEDIATE" for sql in statements)


@pytest.mark.parametrize("scope", ["global", "per_agent", "maintenance"])
def test_global_agent_and_maintenance_expiry_share_outcomes_and_are_idempotent(client, admin, monkeypatch, scope):
    agents = _seed_scenarios(client, admin)
    helper = _expiry_helper(client)
    if scope == "per_agent":
        with client.app.state.db.connection() as db:
            helper(db, agent_id=agents["update"],
                   now=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        scoped = _ledger_state(client)
        assert scoped["jobs"][f"{agents['update']}/update"]["status"] == "expired"
        for kind, agent_id in agents.items():
            if kind != "update":
                assert scoped["commands"][f"{agent_id}/ordinary"]["status"] == "queued"
                assert scoped["refreshes"][agent_id]["status"] == "running"

    def expire():
        if scope == "maintenance":
            _one_maintenance_iteration(client, monkeypatch)
        else:
            with client.app.state.db.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                for agent_id in agents.values() if scope == "per_agent" else [None]:
                    helper(db, agent_id=agent_id, now=now)

    expire()
    first = _ledger_state(client)
    _assert_expiry_outcome(first, agents)
    expire()
    assert _ledger_state(client) == first


def test_maintenance_preserves_fresh_activation_but_enforces_the_activation_time_limit(client, admin, monkeypatch):
    agents = _seed_scenarios(client, admin)
    agent_id = agents["activating"]
    _one_maintenance_iteration(client, monkeypatch)
    assert _ledger_state(client)["jobs"][f"{agent_id}/activating"]["status"] == "activating"
    with client.app.state.db.connection() as db:
        db.execute("UPDATE agent_update_jobs SET updated_at=? WHERE command_id=?",
                   ("2000-01-01T00:00:00+00:00", f"{agent_id}/activating"))
    _one_maintenance_iteration(client, monkeypatch)
    state = _ledger_state(client)
    assert state["commands"][f"{agent_id}/activating"]["status"] == "failed"
    job = state["jobs"][f"{agent_id}/activating"]
    assert job["status"] == "failed" and job["completed_at"]
    assert f"{main_module.AGENT_UPDATE_ACTIVATION_MAX_MINUTES} minutes" in job["last_error"]


def test_telemetry_limit_selects_the_latest_id_at_a_tied_timestamp_and_returns_chronological_order(client, admin):
    agent_id = _agent(client, admin)
    now = datetime.now(timezone.utc)
    latest = now.isoformat(timespec="seconds")
    oldest = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    with client.app.state.db.connection() as db:
        db.execute("DELETE FROM telemetry WHERE agent_id=?", (agent_id,))
        for stamp, value in [(latest, 11), (latest, 22), (oldest, 33)]:
            db.execute("INSERT INTO telemetry(agent_id,recorded_at,metrics_json) VALUES(?,?,?)",
                       (agent_id, stamp, json.dumps({"cpu_percent": value})))
    limited = client.get(f"/api/agents/{agent_id}/telemetry?limit=1", headers=admin)
    assert limited.status_code == 200 and limited.json()[0]["metrics"]["cpu_percent"] == 22
    full = client.get(f"/api/agents/{agent_id}/telemetry?limit=10", headers=admin)
    assert [point["metrics"]["cpu_percent"] for point in full.json()] == [33, 11, 22]
