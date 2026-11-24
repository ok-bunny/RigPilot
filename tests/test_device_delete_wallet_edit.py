from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import (
    auth,
    login as _login,
    create_user,
    controller_settings_kwargs,
)

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(
    client: TestClient,
    username: str = "admin",
    password: str = "correct-horse-battery-staple",
) -> str:
    return _login(client, username, password)


def enroll(client: TestClient, token: str, name: str) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": name, "ttl_minutes": 30},
    )
    assert issued.status_code == 200, issued.text
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": name,
            "hostname": f"{name}.local",
            "os_family": "linux",
            "platform": "Ubuntu 24.04",
            "agent_version": "0.5.0",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def heartbeat(client: TestClient, agent_id: str, secret: str):
    return client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "hostname": "offline-delete.local",
            "os_family": "linux",
            "platform": "Ubuntu 24.04",
            "agent_version": "0.5.0",
            "metrics": {"cpu_percent": 10},
            "inventory": {},
        },
    )


def test_only_admin_can_delete_offline_agent_without_unfinished_commands(
    client: TestClient,
):
    admin_token = login(client)
    operator_token = create_user(client, admin_token, "operator1", "operator")
    agent_id, agent_secret = enroll(client, admin_token, "offline-delete")

    assert heartbeat(client, agent_id, agent_secret).status_code == 200
    online = client.delete(f"/api/agents/{agent_id}", headers=auth(admin_token))
    assert online.status_code == 409
    assert "offline" in online.json()["detail"].lower()

    # Make the heartbeat old while retaining its telemetry row, as happens
    # naturally after a machine has been powered off for the online timeout.
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET last_seen=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", agent_id),
        )
    assert (
        client.get(f"/api/agents/{agent_id}", headers=auth(admin_token)).json()[
            "status"
        ]
        == "offline"
    )

    forbidden = client.delete(
        f"/api/agents/{agent_id}", headers=auth(operator_token)
    )
    assert forbidden.status_code == 403

    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    protected = client.delete(f"/api/agents/{agent_id}", headers=auth(admin_token))
    assert protected.status_code == 409
    assert "unfinished commands" in protected.json()["detail"]

    cancelled = client.post(
        f"/api/commands/{queued.json()['id']}/cancel", headers=auth(admin_token)
    )
    assert cancelled.status_code == 200

    expired = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert expired.status_code == 201, expired.text
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE commands SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", expired.json()["id"]),
        )
    deleted = client.delete(f"/api/agents/{agent_id}", headers=auth(admin_token))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted"}
    assert client.get(f"/api/agents/{agent_id}", headers=auth(admin_token)).status_code == 404
    assert heartbeat(client, agent_id, agent_secret).status_code == 401
    assert client.get("/api/commands", headers=auth(admin_token)).json() == []

    audit = client.get("/api/audit", headers=auth(admin_token)).json()
    deletion = next(item for item in audit if item["action"] == "agent_deleted")
    assert deletion["target_id"] == agent_id
    assert deletion["details"]["name"] == "offline-delete"
    assert deletion["details"]["terminal_commands_deleted"] == 2
    assert deletion["details"]["telemetry_points_deleted"] == 1
    assert deletion["details"]["hashrate_history_buckets_deleted"] == 1


def test_admin_can_delete_disabled_agent_without_waiting_for_online_timeout(
    client: TestClient,
):
    admin_token = login(client)
    agent_id, agent_secret = enroll(client, admin_token, "disabled-delete")
    assert heartbeat(client, agent_id, agent_secret).status_code == 200

    disabled = client.patch(
        f"/api/agents/{agent_id}",
        headers=auth(admin_token),
        json={"disabled": True},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"

    deleted = client.delete(f"/api/agents/{agent_id}", headers=auth(admin_token))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted"}
    assert heartbeat(client, agent_id, agent_secret).status_code == 401


@pytest.mark.parametrize("in_flight_status", ["dispatched", "running"])
def test_admin_can_abandon_only_offline_in_flight_command(
    client: TestClient, in_flight_status: str
):
    admin_token = login(client)
    operator_token = create_user(client, admin_token, "abandon-operator", "operator")
    agent_id, agent_secret = enroll(client, admin_token, "abandon-offline")
    assert heartbeat(client, agent_id, agent_secret).status_code == 200

    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]
    delivered = heartbeat(client, agent_id, agent_secret)
    assert delivered.status_code == 200
    assert delivered.json()["commands"][0]["id"] == command_id
    if in_flight_status == "running":
        running = client.post(
            f"/api/agent/commands/{command_id}/result",
            headers={
                "X-Agent-ID": agent_id,
                "Authorization": f"Bearer {agent_secret}",
            },
            json={"status": "running", "result": {}, "error": None},
        )
        assert running.status_code == 200, running.text

    online = client.post(
        f"/api/commands/{command_id}/abandon", headers=auth(admin_token)
    )
    assert online.status_code == 409
    assert "仍在线" in online.json()["detail"]

    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET last_seen=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", agent_id),
        )

    forbidden = client.post(
        f"/api/commands/{command_id}/abandon", headers=auth(operator_token)
    )
    assert forbidden.status_code == 403
    abandoned = client.post(
        f"/api/commands/{command_id}/abandon", headers=auth(admin_token)
    )
    assert abandoned.status_code == 200, abandoned.text
    assert abandoned.json() == {
        "status": "cancelled",
        "previous_status": in_flight_status,
        "effect": "controller_record_abandoned",
    }
    current = client.get(
        f"/api/commands/{command_id}", headers=auth(admin_token)
    ).json()
    assert current["status"] == "cancelled"
    assert "remote execution may already have occurred" in current["error"]

    late = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
        json={"status": "succeeded", "result": {"late": True}, "error": None},
    )
    assert late.status_code == 200, late.text
    assert late.json() == {"status": "cancelled"}
    assert (
        client.get(f"/api/commands/{command_id}", headers=auth(admin_token)).json()[
            "status"
        ]
        == "cancelled"
    )

    audit = client.get("/api/audit", headers=auth(admin_token)).json()
    abandoned_audit = next(
        item
        for item in audit
        if item["action"] == "command_abandoned" and item["target_id"] == command_id
    )
    assert abandoned_audit["details"]["previous_status"] == in_flight_status


def test_abandoning_post_update_policy_closes_parent_activating_update(
    client: TestClient,
):
    admin_token = login(client)
    agent_id, _agent_secret = enroll(client, admin_token, "abandon-update-policy")
    with client.app.state.db.connection() as db:
        admin_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """UPDATE agents SET last_seen=? WHERE id=?""",
            ("2000-01-01T00:00:00+00:00", agent_id),
        )
        db.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,update_protocol,notes,created_by,created_at)
               VALUES('abandon-release','0.6.50','linux','x86_64','agent.tar.gz',
                      'abandon-release.tar.gz',1,?,'agent_update.v1','',?,?)""",
            ("a" * 64, admin_id, "2026-07-30T00:00:00+00:00"),
        )
        for command_id, action in (
            ("abandon-update-parent", "agent_update.v1"),
            ("abandon-update-policy", "configure_remote_access.v1"),
        ):
            db.execute(
                """INSERT INTO commands
                   (id,agent_id,action,payload_json,status,created_by,created_at,
                    expires_at,dispatched_at)
                   VALUES(?,?,?,'{}','running',?,?,
                          '9999-12-31T23:59:59+00:00',?)""",
                (
                    command_id,
                    agent_id,
                    action,
                    admin_id,
                    "2026-07-30T00:00:00+00:00",
                    "2026-07-30T00:00:01+00:00",
                ),
            )
        db.execute(
            """INSERT INTO agent_update_jobs
               (command_id,agent_id,release_id,source_version,target_version,
                status,created_by,created_at,updated_at,
                activate_remote_workspace,post_update_command_id)
               VALUES('abandon-update-parent',?,'abandon-release','0.6.49','0.6.50',
                      'activating',?,?,?,1,'abandon-update-policy')""",
            (
                agent_id,
                admin_id,
                "2026-07-30T00:00:00+00:00",
                "2026-07-30T00:00:01+00:00",
            ),
        )

    abandoned = client.post(
        "/api/commands/abandon-update-policy/abandon",
        headers=auth(admin_token),
    )
    assert abandoned.status_code == 200, abandoned.text
    with client.app.state.db.connection() as db:
        parent_status = db.execute(
            "SELECT status FROM commands WHERE id='abandon-update-parent'"
        ).fetchone()["status"]
        job_status = db.execute(
            "SELECT status FROM agent_update_jobs WHERE command_id='abandon-update-parent'"
        ).fetchone()["status"]
    assert parent_status == "failed"
    assert job_status == "failed"


def test_confirmed_delete_atomically_abandons_offline_running_command(
    client: TestClient,
):
    admin_token = login(client)
    agent_id, agent_secret = enroll(client, admin_token, "delete-with-running")
    assert heartbeat(client, agent_id, agent_secret).status_code == 200
    command = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_diagnostics", "payload": {}},
    ).json()
    assert heartbeat(client, agent_id, agent_secret).json()["commands"][0]["id"] == command["id"]
    running = client.post(
        f"/api/agent/commands/{command['id']}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
        json={"status": "running", "result": {}, "error": None},
    )
    assert running.status_code == 200, running.text
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET last_seen=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", agent_id),
        )

    blocked = client.delete(f"/api/agents/{agent_id}", headers=auth(admin_token))
    assert blocked.status_code == 409
    assert "unfinished commands" in blocked.json()["detail"]
    deleted = client.delete(
        f"/api/agents/{agent_id}?abandon_unfinished=true",
        headers=auth(admin_token),
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted"}
    assert client.get(f"/api/agents/{agent_id}", headers=auth(admin_token)).status_code == 404
    assert heartbeat(client, agent_id, agent_secret).status_code == 401

    audit = client.get("/api/audit", headers=auth(admin_token)).json()
    deletion = next(item for item in audit if item["action"] == "agent_deleted")
    assert deletion["target_id"] == agent_id
    assert deletion["details"]["abandoned_unfinished_commands"] == [
        {
            "id": command["id"],
            "action": "collect_diagnostics",
            "previous_status": "running",
        }
    ]
    assert any(
        item["action"] == "command_abandoned" and item["target_id"] == command["id"]
        for item in audit
    )



def test_command_list_can_include_all_active_beyond_terminal_limit(client: TestClient):
    admin_token = login(client)
    agent_id, _agent_secret = enroll(client, admin_token, "old-active-command")
    with client.app.state.db.connection() as db:
        user_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO commands(
                   id,agent_id,action,payload_json,status,created_by,created_at,
                   expires_at,is_transient
               ) VALUES(?,?,?,?,?,?,?,?,0)""",
            (
                "old-running",
                agent_id,
                "collect_diagnostics",
                "{}",
                "running",
                user_id,
                "2000-01-01T00:00:00+00:00",
                "2099-01-01T00:00:00+00:00",
            ),
        )
        for index in range(4):
            timestamp = f"2026-01-01T00:00:0{index}+00:00"
            db.execute(
                """INSERT INTO commands(
                       id,agent_id,action,payload_json,status,created_by,
                       created_at,expires_at,finished_at,is_transient
                   ) VALUES(?,?,?,?,?,?,?,?,?,0)""",
                (
                    f"terminal-{index}",
                    agent_id,
                    "collect_diagnostics",
                    "{}",
                    "succeeded",
                    user_id,
                    timestamp,
                    "2099-01-01T00:00:00+00:00",
                    timestamp,
                ),
            )

    limited = client.get(
        "/api/commands?limit=3", headers=auth(admin_token)
    ).json()
    assert len(limited) == 3
    assert all(item["id"] != "old-running" for item in limited)
    with_active = client.get(
        "/api/commands?limit=3&include_all_active=true",
        headers=auth(admin_token),
    ).json()
    assert len(with_active) == 4
    assert any(
        item["id"] == "old-running" and item["status"] == "running"
        for item in with_active
    )


def test_command_list_expires_stale_rows_before_rendering_active_tasks(
    client: TestClient,
):
    admin_token = login(client)
    agent_id, _secret = enroll(client, admin_token, "list-stale-command")
    command = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert command.status_code == 201, command.text
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE commands SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", command.json()["id"]),
        )

    listed = client.get(
        "/api/commands",
        params={
            "agent_id": agent_id,
            "include_all_active": "true",
            "limit": 10,
        },
        headers=auth(admin_token),
    )
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == command.json()["id"])
    assert row["status"] == "expired"


def test_operator_can_edit_wallet_but_not_change_referenced_coin(
    client: TestClient,
):
    admin_token = login(client)
    operator_token = create_user(client, admin_token, "operator2", "operator")
    viewer_token = create_user(client, admin_token, "viewer2", "viewer")

    created = client.post(
        "/api/wallets",
        headers=auth(operator_token),
        json={
            "name": "Old wallet",
            "coin": "btc",
            "address": "bc1qpublicexampleaddress000000000",
            "note": "old note",
        },
    )
    assert created.status_code == 201, created.text
    wallet = created.json()

    edited = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(operator_token),
        json={
            "name": "Dogecoin payout",
            "coin": "doge",
            "address": "DPublicAddressForPayoutOnly12345",
            "note": "updated",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["coin"] == "DOGE"
    assert edited.json()["created_by"] == "operator2"

    secret_rejected = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(operator_token),
        json={
            "name": "bad",
            "coin": "DOGE",
            "address": "xprv" + "A" * 100,
            "note": "",
        },
    )
    assert secret_rejected.status_code == 422

    sheet = client.post(
        "/api/flight-sheets",
        headers=auth(operator_token),
        json={
            "name": "referencing sheet",
            "coin": "DOGE",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
        },
    )
    assert sheet.status_code == 201, sheet.text

    coin_change = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(operator_token),
        json={
            "name": "Dogecoin payout",
            "coin": "LTC",
            "address": "LPublicAddressForPayoutOnly12345",
            "note": "updated again",
        },
    )
    assert coin_change.status_code == 409
    assert "flight sheet" in coin_change.json()["detail"]

    address_change = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(operator_token),
        json={
            "name": "Dogecoin main payout",
            "coin": "doge",
            "address": "DAnotherPublicAddressForPayoutOnly987",
            "note": "safe public address replacement",
        },
    )
    assert address_change.status_code == 200, address_change.text
    assert address_change.json()["name"] == "Dogecoin main payout"
    assert address_change.json()["coin"] == "DOGE"

    forbidden = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(viewer_token),
        json={
            "name": "viewer edit",
            "coin": "DOGE",
            "address": "DViewerPublicAddressNotAllowed123",
            "note": "",
        },
    )
    assert forbidden.status_code == 403

    fetched = client.get("/api/wallets", headers=auth(viewer_token)).json()
    assert fetched[0]["address"] == "DAnotherPublicAddressForPayoutOnly987"
    audit = client.get("/api/audit", headers=auth(admin_token)).json()
    updates = [item for item in audit if item["action"] == "wallet_updated"]
    assert len(updates) == 2
    assert all("address" not in item["details"] for item in updates)
