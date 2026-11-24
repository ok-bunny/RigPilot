from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import server.auth as auth_module
import server.main as main_module
from controller_test_support import login as _login, auth

from server.config import Settings
from server.database import Database
from server.main import create_app
from server.schemas import (
    AgentEnrollRequest,
    AgentUpdate,
    EnrollmentTokenCreate,
    FlightMinerCreate,
    HeartbeatRequest,
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "controller-hardening.db",
        secret_key="controller-hardening-secret-with-sufficient-entropy",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        max_miner_package_bytes=1024,
    )
    catalog_type = main_module.ManagedMinerCatalog
    monkeypatch.setattr(main_module, "ManagedMinerCatalog", lambda: catalog_type(fetcher=lambda *_: []))
    with TestClient(create_app(settings)) as value:
        yield value


def _admin_headers(client: TestClient) -> dict[str, str]:
    return auth(_login(client, 'admin', 'correct-horse-battery-staple'))


def _enroll_agent(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=headers,
        json={"label": "hardening-agent", "ttl_minutes": 30},
    )
    assert issued.status_code == 200, issued.text
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "hostname": "hardening-rig",
            "os_family": "linux",
            "agent_version": "0.6.50",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json()["agent_id"], enrolled.json()["agent_secret"]


def test_session_token_is_expired_at_its_exact_expiry_second(
    monkeypatch: pytest.MonkeyPatch,
):
    now = [1_000.0]
    monkeypatch.setattr(auth_module.time, "time", lambda: now[0])
    token = auth_module.create_session_token(
        {"uid": 1},
        "boundary-secret",
        expires_in_seconds=1,
    )

    now[0] = 1_000.999
    assert auth_module.verify_session_token(token, "boundary-secret") is not None
    now[0] = 1_001.0
    assert auth_module.verify_session_token(token, "boundary-secret") is None


def test_main_page_csp_blocks_embedding_plugins_and_base_tag_rewrites(
    client: TestClient,
):
    response = client.get("/")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "base-uri 'none'" in policy
    assert "form-action 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "object-src 'none'" in policy


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            AgentEnrollRequest,
            {
                "enrollment_token": "x" * 20,
                "hostname": "   ",
                "os_family": "linux",
            },
        ),
        (
            AgentEnrollRequest,
            {
                "enrollment_token": "x" * 20,
                "name": "   ",
                "hostname": "rig",
                "os_family": "linux",
            },
        ),
        (
            HeartbeatRequest,
            {"hostname": "rig", "os_family": "   "},
        ),
        (
            AgentUpdate,
            {"name": "   "},
        ),
    ],
)
def test_agent_identity_models_reject_whitespace_only_values(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_agent_identity_models_strip_values_before_persistence_validation():
    enroll = AgentEnrollRequest.model_validate(
        {
            "enrollment_token": "  " + "x" * 20 + "  ",
            "name": "  rack-01  ",
            "hostname": "  rack-01.local  ",
            "os_family": "  linux  ",
        }
    )
    assert enroll.enrollment_token == "x" * 20
    assert enroll.name == "rack-01"
    assert enroll.hostname == "rack-01.local"
    assert enroll.os_family == "linux"

    update = AgentUpdate.model_validate({"note": "  top shelf  "})
    assert update.note == "top shelf"


def test_enrollment_label_rejects_whitespace_only_value():
    with pytest.raises(ValidationError):
        EnrollmentTokenCreate.model_validate({"label": "   "})


def test_authentication_and_pool_passwords_preserve_meaningful_spaces():
    miner = FlightMinerCreate.model_validate(
        {
            "coin": "PEARL",
            "wallet_id": "wallet",
            "pool_url": "pearl://pool.example:443",
            "pool_password": "  pool password  ",
        }
    )
    assert miner.pool_password == "  pool password  "
    assert FlightMinerCreate.model_validate(
        {
            "coin": "PEARL",
            "wallet_id": "wallet",
            "pool_url": "pearl://pool.example:443",
            "pool_password": "",
        }
    ).pool_password == ""


def test_enrollment_token_is_expired_at_its_exact_expiry_second(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    created = client.post(
        "/api/enrollment-tokens",
        headers=_admin_headers(client),
        json={"label": "boundary", "ttl_minutes": 30},
    )
    assert created.status_code == 200, created.text
    boundary = "2030-01-01T00:00:00+00:00"
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE enrollment_tokens SET expires_at=? WHERE id=?",
            (boundary, created.json()["id"]),
        )
    monkeypatch.setattr(main_module, "utcnow", lambda: boundary)

    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": created.json()["token"],
            "hostname": "boundary-rig",
            "os_family": "linux",
        },
    )
    assert response.status_code == 401


def test_stale_deterministic_upload_file_does_not_block_managed_test_upload(
    client: TestClient,
):
    headers = {
        **_admin_headers(client),
        "Content-Type": "application/octet-stream",
    }
    root = client.app.state.settings.data_dir / "miner-packages"
    root.mkdir(parents=True, exist_ok=True)
    stale = root / ".test-xmrig-99.0.0-windows-x64.uploading"
    stale.write_bytes(b"interrupted old upload")

    response = client.post(
        "/api/miner-packages/upload",
        headers=headers,
        params={
            "name": "ignored label",
            "version": "99.0.0",
            "archive_type": "zip",
            "filename": "xmrig-99.0.0-windows-x64.zip",
            "managed_miner_type": "xmrig",
            "managed_os_family": "windows",
            "managed_channel": "test",
        },
        content=b"PK\x03\x04managed-test-build",
    )

    assert response.status_code == 201, response.text
    assert response.json()["id"] == "test-xmrig-99.0.0-windows-x64"
    # The current request owns a unique staging file and always removes it.
    assert list(root.glob(".test-xmrig-99.0.0-windows-x64.*.uploading")) == []


def test_disabling_agent_closes_unfinished_ledgers_and_prevents_replay(
    client: TestClient,
):
    headers = _admin_headers(client)
    agent_id, agent_secret = _enroll_agent(client, headers)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    with client.app.state.db.connection() as db:
        user_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO wallets
               (id,name,coin,address,note,created_by,created_at,updated_at)
               VALUES('wallet','wallet','PEARL','public-wallet','',?,?,?)""",
            (user_id, now, now),
        )
        db.execute(
            """INSERT INTO flight_sheets
               (id,name,coin,wallet_id,pool_url,miner_profile,
                worker_name_template,created_by,created_at,updated_at)
               VALUES('sheet','sheet','PEARL','wallet',
                      'pearl://pool.example:443','default','{hostname}',?,?,?)""",
            (user_id, now, now),
        )
        for command_id, action, command_status in (
            ("queued-command", "collect_diagnostics", "queued"),
            ("apply-command", "apply_flight_sheet", "dispatched"),
            ("update-command", "agent_update.v1", "dispatched"),
        ):
            db.execute(
                """INSERT INTO commands
                   (id,agent_id,action,payload_json,status,created_by,
                    created_at,expires_at,dispatched_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    command_id,
                    agent_id,
                    action,
                    "{}",
                    command_status,
                    user_id,
                    now,
                    future,
                    now if command_status != "queued" else None,
                ),
            )
        db.execute(
            """INSERT INTO configuration_refreshes
               (agent_id,flight_sheet_id,desired_fingerprint,generation_id,
                trigger_type,
                 trigger_id,status,command_id,requested_by,requested_at,
                 updated_at,queued_at,attempt_count)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (
                agent_id,
                "sheet",
                "fingerprint",
                "test-generation",
                "flight_sheet",
                "sheet",
                "dispatched",
                "apply-command",
                user_id,
                now,
                now,
                now,
            ),
        )
        db.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,
                file_size,sha256,update_protocol,notes,created_by,created_at)
               VALUES('release','0.6.51','linux','x86_64','release.tar.gz',
                      'release.tar.gz',1,?,'agent_update.v1','',?,?)""",
            ("0" * 64, user_id, now),
        )
        db.execute(
            """INSERT INTO agent_update_jobs
               (command_id,agent_id,release_id,source_version,target_version,
                status,created_by,created_at,updated_at)
               VALUES('update-command',?,'release','0.6.50','0.6.51',
                      'dispatched',?,?,?)""",
            (agent_id, user_id, now, now),
        )

    disabled = client.patch(
        f"/api/agents/{agent_id}",
        headers=headers,
        json={"disabled": True},
    )
    assert disabled.status_code == 200, disabled.text
    with client.app.state.db.connection() as db:
        command_statuses = {
            row["id"]: row["status"]
            for row in db.execute(
                "SELECT id,status FROM commands WHERE agent_id=?", (agent_id,)
            )
        }
        refresh = db.execute(
            """SELECT status,command_id FROM configuration_refreshes
               WHERE agent_id=?""",
            (agent_id,),
        ).fetchone()
        update = db.execute(
            "SELECT status FROM agent_update_jobs WHERE command_id='update-command'"
        ).fetchone()
    assert set(command_statuses.values()) == {"cancelled"}
    assert dict(refresh) == {"status": "pending", "command_id": None}
    assert update["status"] == "cancelled"
    enabled = client.patch(
        f"/api/agents/{agent_id}",
        headers=headers,
        json={"disabled": False},
    )
    assert enabled.status_code == 200, enabled.text
    # Re-enable permits fresh work, but pre-disable commands remain terminal
    # and can never be replayed to the Agent.
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
        json={"hostname": "hardening-rig", "os_family": "linux"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == []


def test_summary_does_not_count_expired_unfinished_commands_before_maintenance(
    client: TestClient,
):
    headers = _admin_headers(client)
    agent_id, _secret = _enroll_agent(client, headers)
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=headers,
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE commands SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", queued.json()["id"]),
        )

    summary = client.get("/api/summary", headers=headers)
    assert summary.status_code == 200, summary.text
    assert summary.json()["active_commands"] == 0


def test_remote_sessions_ledger_is_retired_on_initialize(tmp_path: Path):
    """0.8.50 drops the reader-less remote-session metadata table entirely."""

    database = Database(tmp_path / "remote-retired.db")
    database.initialize("admin", "correct-horse-battery-staple")
    with database.connection() as db:
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "remote_sessions" not in tables
    assert not hasattr(database, "prune_remote_sessions")


def test_enrollment_token_pruning_preserves_active_and_recent_metadata(
    tmp_path: Path,
):
    database = Database(tmp_path / "token-retention.db")
    database.initialize("admin", "correct-horse-battery-staple")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=31)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=1)).isoformat(timespec="seconds")
    future = (now + timedelta(days=1)).isoformat(timespec="seconds")
    with database.connection() as db:
        user_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        for token_id, expires_at, used_at in (
            ("old-expired", old, None),
            ("old-used", future, old),
            ("recent-expired", recent, None),
            ("active", future, None),
        ):
            db.execute(
                """INSERT INTO enrollment_tokens
                   (id,token_hash,label,expires_at,used_at,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    token_id,
                    f"hash-{token_id}",
                    token_id,
                    expires_at,
                    used_at,
                    user_id,
                    old,
                ),
            )

    assert database.prune_enrollment_tokens(30) == 2
    with database.connection() as db:
        remaining = {
            row["id"] for row in db.execute("SELECT id FROM enrollment_tokens")
        }
    assert remaining == {"recent-expired", "active"}


