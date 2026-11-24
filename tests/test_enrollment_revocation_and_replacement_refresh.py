"""Registration-audit fixes: enrollment-token revocation and stale refresh.

Covers two controller-side registration lifecycle gaps found in the
2026-11-17 registration API audit:

1. ``DELETE /api/enrollment-tokens/{id}`` revokes an unused one-time token
   before expiry.  Used tokens stay: they anchor enrollment-recovery receipts
   (``agent_enrollment_attempts``) through a cascading foreign key.
2. Inheriting a device cancels a pending/preparing ``configuration_refreshes``
   row left behind by the previous identity.  The clean installation compiles
   fresh payloads, so such a row could otherwise never resolve and would show
   a permanent "configuration pending" badge - while an autostart resume must
   still be able to re-create the row on the first authenticated heartbeat.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, create_user, login as _login

from server.config import Settings
from server.main import create_app


ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "registration-audit.db",
        secret_key="registration-audit-test-secret-with-sufficient-entropy",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as value:
        yield value


def admin_headers(client: TestClient) -> dict[str, str]:
    return auth(_login(client, "admin", ADMIN_PASSWORD))


def issue_token(client: TestClient, headers: dict[str, str]) -> dict[str, object]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=headers,
        json={"label": "audit-token", "ttl_minutes": 30},
    )
    assert issued.status_code == 200, issued.text
    return issued.json()


def enroll(
    client: TestClient, token: str, *, name: str = "audit-device"
) -> tuple[str, str]:
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": token,
            "name": name,
            "hostname": "audit-host",
            "os_family": "linux",
            "platform": "audit-platform",
            "agent_version": "0.8.60",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def audit_actions(client: TestClient, action: str) -> list[dict[str, object]]:
    listed = client.get("/api/audit", headers=admin_headers(client))
    assert listed.status_code == 200, listed.text
    return [entry for entry in listed.json() if entry.get("action") == action]


# ---------------------------------------------------------------------------
# Enrollment token revocation
# ---------------------------------------------------------------------------


def test_unused_enrollment_token_can_be_revoked_before_expiry(client: TestClient):
    headers = admin_headers(client)
    token = issue_token(client, headers)

    revoked = client.delete(f"/api/enrollment-tokens/{token['id']}", headers=headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() == {"status": "revoked"}

    # A revoked token can no longer enroll a device.
    denied = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": token["token"],
            "hostname": "late-host",
            "os_family": "linux",
            "platform": "late-platform",
            "agent_version": "0.8.60",
        },
    )
    assert denied.status_code == 401, denied.text

    listed = client.get("/api/enrollment-tokens", headers=headers)
    assert listed.status_code == 200, listed.text
    assert all(row["id"] != token["id"] for row in listed.json())

    entries = audit_actions(client, "enrollment_token_revoked")
    assert any(
        entry.get("target_id") == token["id"] and entry.get("actor") == "admin"
        for entry in entries
    )


def test_used_enrollment_token_cannot_be_revoked_and_recovery_survives(client: TestClient):
    headers = admin_headers(client)
    token = issue_token(client, headers)

    # A recoverable (v2) enrollment consumes the token and leaves a receipt
    # anchored to it through agent_enrollment_attempts.token_id.
    request_id = secrets.token_hex(16)
    proposed = "rpa_" + secrets.token_urlsafe(36)
    first = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": token["token"],
            "enrollment_request_id": request_id,
            "proposed_agent_secret": proposed,
            "hostname": "audit-host",
            "os_family": "linux",
            "platform": "audit-platform",
            "agent_version": "0.8.60",
        },
    )
    assert first.status_code == 201, first.text
    agent_id = first.json()["agent_id"]

    revoked = client.delete(f"/api/enrollment-tokens/{token['id']}", headers=headers)
    assert revoked.status_code == 409, revoked.text
    listed = client.get("/api/enrollment-tokens", headers=headers)
    assert any(row["id"] == token["id"] for row in listed.json())

    # The refusal keeps the recoverable-enrollment receipt replayable: the
    # same request id and proposed secret still recover the same identity.
    replay = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": token["token"],
            "enrollment_request_id": request_id,
            "proposed_agent_secret": proposed,
            "hostname": "audit-host",
            "os_family": "linux",
            "platform": "audit-platform",
            "agent_version": "0.8.60",
        },
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["agent_id"] == agent_id
    assert replay.json()["agent_secret"] == proposed
    # The recovered identity still authenticates.
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {proposed}"},
        json={
            "hostname": "audit-host",
            "os_family": "linux",
            "platform": "audit-platform",
            "agent_version": "0.8.60",
            "metrics": {},
            "inventory": {},
            "capabilities": [],
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text


def test_enrollment_token_revocation_requires_admin_role(client: TestClient):
    headers = admin_headers(client)
    token = issue_token(client, headers)
    viewer_token = create_user(client, _login(client, "admin", ADMIN_PASSWORD), "audit-viewer", "viewer")

    anonymous = client.delete(f"/api/enrollment-tokens/{token['id']}")
    assert anonymous.status_code == 401, anonymous.text
    forbidden = client.delete(
        f"/api/enrollment-tokens/{token['id']}", headers=auth(viewer_token)
    )
    assert forbidden.status_code == 403, forbidden.text

    # The refused attempts consumed nothing.
    still_valid = client.delete(f"/api/enrollment-tokens/{token['id']}", headers=headers)
    assert still_valid.status_code == 200, still_valid.text


def test_revoking_unknown_enrollment_token_returns_404(client: TestClient):
    missing = client.delete(
        f"/api/enrollment-tokens/{uuid.uuid4()}", headers=admin_headers(client)
    )
    assert missing.status_code == 404, missing.text


# ---------------------------------------------------------------------------
# Identity replacement closes the previous identity's pending refresh
# ---------------------------------------------------------------------------


def _insert_sheet(client: TestClient, *, name: str) -> tuple[str, str]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with client.app.state.db.connection() as db:
        admin_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        wallet_id = str(uuid.uuid4())
        sheet_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO wallets
               (id,name,coin,address,note,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (wallet_id, name, "XMR", "public-address", "", admin_id, now, now),
        )
        db.execute(
            """INSERT INTO flight_sheets
               (id,name,miner_type,miner_version,coin,wallet_id,pool_url,
                miner_profile,algorithm,pool_password,device_indices_json,
                worker_name_template,extra_config_json,
                created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sheet_id,
                f"{name} flight",
                "generic",
                "1.0.0",
                "XMR",
                wallet_id,
                "stratum+tcp://pool.example.invalid:3333",
                "default",
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
    return wallet_id, sheet_id


def _backdate_last_seen(client: TestClient, agent_id: str) -> None:
    stale = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat(timespec="microseconds")
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET last_seen=? WHERE id=?", (stale, agent_id)
        )


def _reported_running(client: TestClient, agent_id: str, sheet_id: str) -> None:
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET inventory_json=?, last_seen=? WHERE id=?",
            (
                json.dumps(
                    {
                        "mining_status": {
                            "configured": True,
                            "state": "running",
                            "process_verified": True,
                            "flight_sheet_id": sheet_id,
                        }
                    }
                ),
                datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                agent_id,
            ),
        )


def _refresh_row(client: TestClient, agent_id: str):
    with client.app.state.db.connection() as db:
        return db.execute(
            "SELECT * FROM configuration_refreshes WHERE agent_id=?", (agent_id,)
        ).fetchone()


def _replace_identity(client: TestClient, headers: dict[str, str], agent_id: str):
    issued = client.post(
        f"/api/agents/{agent_id}/replacement-token",
        headers=headers,
        json={"confirm_name": "audit-device", "ttl_minutes": 15},
    )
    assert issued.status_code == 201, issued.text
    proposed = "rpa_" + secrets.token_urlsafe(36)
    claimed = client.post(
        "/api/agent/replace",
        json={
            "replacement_token": issued.json()["token"],
            "request_id": uuid.uuid4().hex,
            "proposed_agent_secret": proposed,
            "hostname": "new-host",
            "os_family": "linux",
            "platform": "new-platform",
            "agent_version": "0.8.60",
        },
    )
    assert claimed.status_code == 201, claimed.text
    return proposed


def test_replacement_cancels_stale_pending_refresh_without_autostart(client: TestClient):
    headers = admin_headers(client)
    token = issue_token(client, headers)
    agent_id, _secret = enroll(client, token["token"])
    wallet_id, sheet_id = _insert_sheet(client, name="stale")
    _reported_running(client, agent_id, sheet_id)

    # The device goes offline, then an operator edits its wallet: the refresh
    # outbox keeps a pending row waiting for the old identity to mine again.
    _backdate_last_seen(client, agent_id)
    updated = client.put(
        f"/api/wallets/{wallet_id}",
        headers=headers,
        json={
            "name": "stale",
            "coin": "XMR",
            "address": "changed-public-address-0000000000000000000",
            "note": "",
        },
    )
    assert updated.status_code == 200, updated.text
    before = _refresh_row(client, agent_id)
    assert before is not None
    assert before["status"] == "pending"

    _replace_identity(client, headers, agent_id)

    after = _refresh_row(client, agent_id)
    assert after is not None
    assert after["status"] == "cancelled"
    assert "replaced" in (after["last_error"] or "")
    assert after["completed_at"] is not None

    # The device detail surface (which drives the badge) reflects the close.
    detail = client.get(f"/api/agents/{agent_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["configuration_refresh"]["status"] == "cancelled"


def test_replacement_refresh_cancel_does_not_block_autostart_resume(client: TestClient):
    headers = admin_headers(client)
    token = issue_token(client, headers)
    agent_id, _secret = enroll(client, token["token"])
    wallet_id, sheet_id = _insert_sheet(client, name="resume")
    _reported_running(client, agent_id, sheet_id)
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET mining_autostart_enabled=1 WHERE id=?", (agent_id,)
        )

    _backdate_last_seen(client, agent_id)
    updated = client.put(
        f"/api/wallets/{wallet_id}",
        headers=headers,
        json={
            "name": "resume",
            "coin": "XMR",
            "address": "changed-public-address-0000000000000000000",
            "note": "",
        },
    )
    assert updated.status_code == 200, updated.text
    stale = _refresh_row(client, agent_id)
    assert stale is not None
    assert stale["status"] == "pending"
    assert not stale["trigger_id"].startswith("desired:replacement:")

    new_secret = _replace_identity(client, headers, agent_id)
    cancelled = _refresh_row(client, agent_id)
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"

    # The first authenticated heartbeat re-creates the resume refresh on top
    # of the cancelled row, and the second heartbeat queues the apply.
    agent_auth = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {new_secret}"}
    heartbeat_body = {
        "hostname": "new-host",
        "os_family": "linux",
        "platform": "new-platform",
        "agent_version": "0.8.60",
        "metrics": {},
        "inventory": {},
        "capabilities": [],
    }
    first = client.post("/api/agent/heartbeat", headers=agent_auth, json=heartbeat_body)
    assert first.status_code == 200, first.text
    resumed = _refresh_row(client, agent_id)
    assert resumed is not None
    assert resumed["status"] == "pending"
    assert resumed["flight_sheet_id"] == sheet_id
    assert resumed["trigger_id"].startswith("desired:replacement:")

    second = client.post(
        "/api/agent/heartbeat", headers=agent_auth, json=heartbeat_body
    )
    assert second.status_code == 200, second.text
    applies = [
        command
        for command in second.json()["commands"]
        if command["action"] == "apply_flight_sheet"
    ]
    assert len(applies) == 1
    assert applies[0]["payload"]["flight_sheet_id"] == sheet_id
