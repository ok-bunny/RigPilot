from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import login as _login, auth

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "replacement.db",
        secret_key="replacement-test-secret-with-sufficient-entropy",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
    )
    with TestClient(create_app(settings)) as value:
        yield value


def _admin_headers(client: TestClient) -> dict[str, str]:
    return auth(_login(client, 'admin', 'correct-horse-battery-staple'))


def _enroll(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=headers,
        json={"label": "keep-this-device", "ttl_minutes": 30},
    )
    assert issued.status_code == 200, issued.text
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": "keep-this-device",
            "hostname": "old-host",
            "os_family": "linux",
            "platform": "old-platform",
            "agent_version": "0.6.57",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json()["agent_id"], enrolled.json()["agent_secret"]


def _replacement_body(token: str, request_id: str, secret: str) -> dict[str, str]:
    return {
        "replacement_token": token,
        "request_id": request_id,
        "proposed_agent_secret": secret,
        "hostname": "new-host",
        "os_family": "linux",
        "platform": "new-platform",
        "agent_version": "0.6.58",
    }


def test_offline_device_can_be_inherited_idempotently_with_rotated_secret(
    client: TestClient,
):
    headers = _admin_headers(client)
    agent_id, old_secret = _enroll(client, headers)
    with client.app.state.db.connection() as db:
        db.execute(
            """UPDATE agents SET note='rack note', mining_autostart_enabled=1,
               desired_gpu_oc_json='[{"index":0,"power_limit_w":200}]'
               WHERE id=?""",
            (agent_id,),
        )

    issued = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    assert issued.status_code == 201, issued.text
    token = issued.json()["token"]
    request_id = uuid.uuid4().hex
    new_secret = "rpa_" + secrets.token_urlsafe(36)
    body = _replacement_body(token, request_id, new_secret)

    first = client.post("/api/agent/replace", json=body)
    assert first.status_code == 201, first.text
    assert first.json()["agent_id"] == agent_id
    assert first.json()["inherited_existing_device"] is True
    retry = client.post("/api/agent/replace", json=body)
    assert retry.status_code == 201, retry.text
    assert retry.json() == first.json()

    old_heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {old_secret}",
        },
        json={"hostname": "old-host", "os_family": "linux"},
    )
    assert old_heartbeat.status_code == 401
    new_heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {new_secret}",
        },
        json={
            "hostname": "new-host",
            "os_family": "linux",
            "platform": "new-platform",
            "agent_version": "0.6.58",
            "metrics": {},
            "inventory": {},
            "capabilities": [],
        },
    )
    assert new_heartbeat.status_code == 200, new_heartbeat.text
    with client.app.state.db.connection() as db:
        agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        replacement = db.execute(
            """SELECT activated_at FROM agent_replacement_tokens
               WHERE target_agent_id=? ORDER BY created_at DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
    assert agent["name"] == "keep-this-device"
    assert agent["note"] == "rack note"
    assert agent["mining_autostart_enabled"] == 1
    assert "power_limit_w" in agent["desired_gpu_oc_json"]
    assert agent["hostname"] == "new-host"
    assert replacement["activated_at"] is not None


def test_inheritance_retry_rejects_device_disabled_after_claim(client: TestClient):
    headers = _admin_headers(client)
    agent_id, _ = _enroll(client, headers)
    issued = client.post(
        f"/api/agents/{agent_id}/replacement-token", headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    assert issued.status_code == 201
    body = _replacement_body(issued.json()["token"], uuid.uuid4().hex, "rpa_" + secrets.token_urlsafe(36))
    assert client.post("/api/agent/replace", json=body).status_code == 201
    # A lost-response retry may outlive issuance expiry, but an administrator
    # disabling the device means even the correct replacement identity is unusable.
    disabled = client.patch(f"/api/agents/{agent_id}", headers=headers, json={"disabled": True})
    assert disabled.status_code == 200, disabled.text
    unavailable = client.post("/api/agent/replace", json=body)
    assert unavailable.status_code == 409 and "disabled" in unavailable.json()["detail"]


def test_replacement_requires_exact_name_and_offline_device(client: TestClient):
    headers = _admin_headers(client)
    agent_id, secret = _enroll(client, headers)
    wrong = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "wrong-name", "ttl_minutes": 15},
    )
    assert wrong.status_code == 422

    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={"hostname": "old-host", "os_family": "linux"},
    )
    assert heartbeat.status_code == 200
    issued = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    assert issued.status_code == 201
    rejected = client.post(
        "/api/agent/replace",
        json=_replacement_body(
            issued.json()["token"],
            uuid.uuid4().hex,
            "rpa_" + secrets.token_urlsafe(36),
        ),
    )
    assert rejected.status_code == 409
    assert "still online" in rejected.json()["detail"]


def test_unicode_device_name_can_issue_replacement_code(client: TestClient):
    headers = _admin_headers(client)
    agent_id, _secret = _enroll(client, headers)
    with client.app.state.db.connection() as db:
        db.execute("UPDATE agents SET name='矿机😀一号' WHERE id=?", (agent_id,))
    issued = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "矿机😀一号", "ttl_minutes": 15},
    )
    assert issued.status_code == 201, issued.text


def test_used_replacement_code_remains_idempotent_after_issuance_expiry(
    client: TestClient,
):
    headers = _admin_headers(client)
    agent_id, _secret = _enroll(client, headers)
    issued = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    request_id = uuid.uuid4().hex
    replacement_secret = "rpa_" + secrets.token_urlsafe(36)
    body = _replacement_body(issued.json()["token"], request_id, replacement_secret)
    first = client.post("/api/agent/replace", json=body)
    assert first.status_code == 201, first.text
    with client.app.state.db.connection() as db:
        db.execute(
            """UPDATE agent_replacement_tokens
               SET expires_at='2000-01-01T00:00:00+00:00'
               WHERE target_agent_id=?""",
            (agent_id,),
        )
    retry = client.post("/api/agent/replace", json=body)
    assert retry.status_code == 201, retry.text
    assert retry.json() == first.json()


def test_inherited_autostart_device_recompiles_and_queues_preserved_flight(
    client: TestClient,
):
    headers = _admin_headers(client)
    agent_id, _secret = _enroll(client, headers)
    sheet_id = str(uuid.uuid4())
    wallet_id = str(uuid.uuid4())
    package_id = str(uuid.uuid4())
    now = "2026-08-01T00:00:00+00:00"
    with client.app.state.db.connection() as db:
        admin_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO wallets
               (id,name,coin,address,note,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (wallet_id, "wallet", "TEST", "public-address", "", admin_id, now, now),
        )
        db.execute(
            """INSERT INTO miner_packages
               (id,name,version,download_url,sha256,archive_type,note,
                source_type,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                package_id,
                "Test Miner",
                "1.0.0",
                "https://example.invalid/miner.tar.gz",
                "0" * 64,
                "tar.gz",
                "",
                "external",
                admin_id,
                now,
                now,
            ),
        )
        db.execute(
            """INSERT INTO flight_sheets
               (id,name,miner_type,miner_version,coin,wallet_id,pool_url,
                miner_profile,miner_package_id,algorithm,pool_password,
                device_indices_json,worker_name_template,extra_config_json,
                created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sheet_id,
                "preserved flight",
                "generic",
                "1.0.0",
                "TEST",
                wallet_id,
                "stratum+tcp://pool.example.invalid:3333",
                "default",
                package_id,
                "testhash",
                "x",
                "[]",
                "{agent_name}",
                "{}",
                admin_id,
                now,
                now,
            ),
        )
        mining = {
            "mining_status": {
                "configured": True,
                "state": "running",
                "flight_sheet_id": sheet_id,
            }
        }
        db.execute(
            """UPDATE agents SET mining_autostart_enabled=1,
               inventory_json=? WHERE id=?""",
            (json.dumps(mining), agent_id),
        )

    issued = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    assert issued.status_code == 201, issued.text
    replacement_secret = "rpa_" + secrets.token_urlsafe(36)
    claimed = client.post(
        "/api/agent/replace",
        json=_replacement_body(
            issued.json()["token"], uuid.uuid4().hex, replacement_secret
        ),
    )
    assert claimed.status_code == 201, claimed.text
    auth = {
        "X-Agent-ID": agent_id,
        "Authorization": f"Bearer {replacement_secret}",
    }
    heartbeat_body = {
        "hostname": "new-host",
        "os_family": "linux",
        "platform": "new-platform",
        "agent_version": "0.6.58",
        "metrics": {},
        "inventory": {},
        "capabilities": [],
    }
    first_heartbeat = client.post(
        "/api/agent/heartbeat", headers=auth, json=heartbeat_body
    )
    assert first_heartbeat.status_code == 200, first_heartbeat.text
    with client.app.state.db.connection() as db:
        refresh = db.execute(
            "SELECT * FROM configuration_refreshes WHERE agent_id=?", (agent_id,)
        ).fetchone()
    assert refresh is not None
    assert refresh["flight_sheet_id"] == sheet_id
    assert refresh["status"] == "pending"
    assert refresh["trigger_id"].startswith("desired:replacement:")

    second_heartbeat = client.post(
        "/api/agent/heartbeat", headers=auth, json=heartbeat_body
    )
    assert second_heartbeat.status_code == 200, second_heartbeat.text
    applies = [
        command
        for command in second_heartbeat.json()["commands"]
        if command["action"] == "apply_flight_sheet"
    ]
    assert len(applies) == 1
    assert applies[0]["payload"]["flight_sheet_id"] == sheet_id
    with client.app.state.db.connection() as db:
        replacement = db.execute(
            """SELECT resumed_command_id FROM agent_replacement_tokens
               WHERE target_agent_id=? ORDER BY created_at DESC LIMIT 1""",
            (agent_id,),
        ).fetchone()
    assert replacement["resumed_command_id"] == applies[0]["id"]


def test_new_code_revokes_unused_code_and_old_completed_retry_cannot_lie(
    client: TestClient,
):
    headers = _admin_headers(client)
    agent_id, _old_secret = _enroll(client, headers)
    first_code = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    second_code = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    assert first_code.status_code == 201
    assert second_code.status_code == 201
    revoked = client.post(
        "/api/agent/replace",
        json=_replacement_body(
            first_code.json()["token"],
            uuid.uuid4().hex,
            "rpa_" + secrets.token_urlsafe(36),
        ),
    )
    assert revoked.status_code == 401

    request_id = uuid.uuid4().hex
    replacement_secret = "rpa_" + secrets.token_urlsafe(36)
    second_body = _replacement_body(
        second_code.json()["token"], request_id, replacement_secret
    )
    claimed = client.post("/api/agent/replace", json=second_body)
    assert claimed.status_code == 201
    activated = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {replacement_secret}",
        },
        json={"hostname": "new-host", "os_family": "linux"},
    )
    assert activated.status_code == 200
    with client.app.state.db.connection() as db:
        db.execute("UPDATE agents SET last_seen=NULL WHERE id=?", (agent_id,))

    third_code = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "keep-this-device", "ttl_minutes": 15},
    )
    assert third_code.status_code == 201
    third_claim = client.post(
        "/api/agent/replace",
        json=_replacement_body(
            third_code.json()["token"],
            uuid.uuid4().hex,
            "rpa_" + secrets.token_urlsafe(36),
        ),
    )
    assert third_claim.status_code == 201

    stale_retry = client.post("/api/agent/replace", json=second_body)
    assert stale_retry.status_code == 409
    assert "superseded" in stale_retry.json()["detail"]


def test_workload_settlement_time_is_persistent_and_legacy_compatible(
    client: TestClient,
):
    headers = _admin_headers(client)
    empty = client.get("/api/workload-settlement-note", headers=headers)
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "private, no-store, max-age=0"
    assert empty.json() == {
        "settlement_at": None,
        "legacy_note": None,
        "note": "",
        "updated_at": None,
        "updated_by": None,
        "revision": 0,
    }

    with client.app.state.db.connection() as db:
        admin_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO controller_preferences
               (key, value, updated_by, updated_at, revision)
               VALUES('workload_settlement_note', ?, ?, ?, 1)""",
            ("上次发放：2026-08-01 19:30", admin_id, "2026-08-01T11:30:00+00:00"),
        )

    legacy = client.get("/api/workload-settlement-note", headers=headers)
    assert legacy.status_code == 200
    assert legacy.json()["settlement_at"] is None
    assert legacy.json()["legacy_note"] == "上次发放：2026-08-01 19:30"
    assert legacy.json()["note"] == "上次发放：2026-08-01 19:30"
    assert legacy.json()["revision"] == 1

    saved = client.put(
        "/api/workload-settlement-note",
        headers=headers,
        json={
            "settlement_at": "2026-08-01T19:30:00+08:00",
            "expected_revision": 1,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.headers["cache-control"] == "private, no-store, max-age=0"
    assert saved.json()["settlement_at"] == "2026-08-01T11:30:00+00:00"
    assert saved.json()["legacy_note"] is None
    assert saved.json()["note"] == "2026-08-01T11:30:00+00:00"
    assert saved.json()["updated_by"] == "admin"
    assert saved.json()["revision"] == 2

    repeated = client.put(
        "/api/workload-settlement-note",
        headers=headers,
        json={
            "settlement_at": "2026-08-01T11:30:00Z",
            "expected_revision": 2,
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["revision"] == 3
    assert repeated.json()["updated_at"] != saved.json()["updated_at"]

    loaded = client.get("/api/workload-settlement-note", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["settlement_at"] == "2026-08-01T11:30:00+00:00"
    assert loaded.json()["legacy_note"] is None
    assert loaded.json()["revision"] == 3
    with client.app.state.db.connection() as db:
        stored = db.execute(
            """SELECT value FROM controller_preferences
               WHERE key='workload_settlement_note'"""
        ).fetchone()
        audits = db.execute(
            """SELECT details_json FROM audit_logs
               WHERE action='workload_settlement_note_updated'
                 AND target_id='workload_settlement_note'
               ORDER BY id"""
        ).fetchall()
    assert stored["value"] == "2026-08-01T11:30:00+00:00"
    assert len(audits) == 2
    assert json.loads(audits[0]["details_json"]) == {
        "settlement_at": "2026-08-01T11:30:00+00:00",
        "replaced_legacy_note": True,
    }
    assert json.loads(audits[1]["details_json"]) == {
        "settlement_at": "2026-08-01T11:30:00+00:00",
        "replaced_legacy_note": False,
    }

    stale = client.put(
        "/api/workload-settlement-note",
        headers=headers,
        json={
            "settlement_at": "2026-08-01T11:35:00+00:00",
            "expected_revision": 2,
        },
    )
    assert stale.status_code == 409

    for invalid_time in (
        "2026-08-01T11:30:00",
        "2026-08-01T11:32:00+00:00",
        "2026-08-01T11:30:01+00:00",
    ):
        invalid = client.put(
            "/api/workload-settlement-note",
            headers=headers,
            json={"settlement_at": invalid_time, "expected_revision": 3},
        )
        assert invalid.status_code == 422

    future = datetime.now(timezone.utc) + timedelta(minutes=15)
    future = future.replace(
        minute=(future.minute // 5) * 5,
        second=0,
        microsecond=0,
    )
    future_response = client.put(
        "/api/workload-settlement-note",
        headers=headers,
        json={"settlement_at": future.isoformat(), "expected_revision": 3},
    )
    assert future_response.status_code == 422
    assert "不能明显晚于" in future_response.json()["detail"]

    legacy_payload = client.put(
        "/api/workload-settlement-note",
        headers=headers,
        json={"note": "旧客户端写入", "expected_revision": 3},
    )
    assert legacy_payload.status_code == 422

    created = client.post(
        "/api/users",
        headers=headers,
        json={
            "username": "settlement-reader",
            "password": "a-very-long-reader-password",
            "role": "viewer",
        },
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/auth/login",
        json={
            "username": "settlement-reader",
            "password": "a-very-long-reader-password",
        },
    )
    assert login.status_code == 200, login.text
    viewer_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    viewer_read = client.get(
        "/api/workload-settlement-note", headers=viewer_headers
    )
    assert viewer_read.status_code == 200
    assert viewer_read.json()["revision"] == 3
    viewer_write = client.put(
        "/api/workload-settlement-note",
        headers=viewer_headers,
        json={
            "settlement_at": "2026-08-01T11:30:00+00:00",
            "expected_revision": 3,
        },
    )
    assert viewer_write.status_code == 403
