from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login

from server.config import Settings
from server.database import Database
from server.main import create_app


ADMIN_PASSWORD = "correct-horse-battery-staple"
ENDPOINT = "/api/agents/commands/batch"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="batch-mining-control-tests-secret-key-that-is-long-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.database = Database(settings.database_path)
        yield test_client


def login(
    client: TestClient,
    username: str = "admin",
    password: str = ADMIN_PASSWORD,
) -> str:
    return _login(client, username, password)


def enroll(
    client: TestClient,
    token: str,
    *,
    name: str,
    version: str = "0.6.44",
) -> tuple[str, str]:
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
            "os_family": "windows",
            "platform": "Windows 11 x64",
            "agent_version": version,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def heartbeat(
    client: TestClient,
    agent_id: str,
    secret: str,
    *,
    name: str,
    version: str = "0.6.44",
):
    return client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={
            "hostname": f"{name}.local",
            "os_family": "windows",
            "platform": "Windows 11 x64",
            "agent_version": version,
            "capabilities": [],
            "inventory": {"architecture": "AMD64"},
            "metrics": {
                "mining_status": {
                    "configured": True,
                    "state": "running",
                    "flight_sheet_id": "sheet-batch-mining",
                    "process_verified": True,
                },
                "gpus": [],
            },
        },
    )


def test_batch_stop_mining_returns_per_device_partial_results_and_reuses_gates(
    client: TestClient,
):
    admin = login(client)
    online_id, online_secret = enroll(client, admin, name="batch-stop-ok")
    offline_id, _ = enroll(client, admin, name="batch-stop-offline")
    old_id, old_secret = enroll(client, admin, name="batch-stop-old", version="0.5.1")
    busy_id, busy_secret = enroll(client, admin, name="batch-stop-busy")
    for agent_id, secret, name, version in (
        (online_id, online_secret, "batch-stop-ok", "0.6.44"),
        (old_id, old_secret, "batch-stop-old", "0.5.1"),
        (busy_id, busy_secret, "batch-stop-busy", "0.6.44"),
    ):
        response = heartbeat(
            client, agent_id, secret, name=name, version=version
        )
        assert response.status_code == 200, response.text

    existing = client.post(
        f"/api/agents/{busy_id}/commands",
        headers=auth(admin),
        json={"action": "restart_mining", "payload": {}},
    )
    assert existing.status_code == 201, existing.text

    response = client.post(
        ENDPOINT,
        headers=auth(admin),
        json={
            "action": "stop_mining",
            "agent_ids": [online_id, offline_id, old_id, busy_id],
        },
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["action"] == "stop_mining"
    assert document["accepted_count"] == 1
    assert document["rejected_count"] == 3
    assert document["accepted"][0]["agent_id"] == online_id
    assert document["accepted"][0]["accepted"] is True
    assert document["accepted"][0]["status"] == "queued"
    assert document["accepted"][0]["action"] == "stop_mining"
    assert document["accepted"][0]["payload"] == {}
    rejected_by_agent = {
        item["agent_id"]: item for item in document["rejected"]
    }
    assert rejected_by_agent[offline_id]["accepted"] is False
    assert rejected_by_agent[offline_id]["status_code"] == 409
    assert "online" in rejected_by_agent[offline_id]["detail"].lower()
    assert rejected_by_agent[old_id]["status_code"] == 409
    assert "0.5.2" in rejected_by_agent[old_id]["detail"]
    assert rejected_by_agent[busy_id]["status_code"] == 409
    assert "already in progress" in rejected_by_agent[busy_id]["detail"]

    with client.app.state.database.connection() as db:
        rows = db.execute(
            """SELECT agent_id, action, status FROM commands
               WHERE agent_id IN (?, ?, ?, ?)""",
            (online_id, offline_id, old_id, busy_id),
        ).fetchall()
    assert {(row["agent_id"], row["action"], row["status"]) for row in rows} == {
        (busy_id, "restart_mining", "queued"),
        (online_id, "stop_mining", "queued"),
    }


def test_batch_restart_mining_rejects_duplicate_queue_and_audits_batch(
    client: TestClient,
):
    admin = login(client)
    first_id, first_secret = enroll(client, admin, name="batch-restart-ok")
    second_id, second_secret = enroll(client, admin, name="batch-restart-second")
    for agent_id, secret, name in (
        (first_id, first_secret, "batch-restart-ok"),
        (second_id, second_secret, "batch-restart-second"),
    ):
        response = heartbeat(client, agent_id, secret, name=name)
        assert response.status_code == 200, response.text

    first = client.post(
        ENDPOINT,
        headers=auth(admin),
        json={"action": "restart_mining", "agent_ids": [first_id, second_id]},
    )
    assert first.status_code == 201, first.text
    document = first.json()
    assert document["accepted_count"] == 2
    assert document["rejected_count"] == 0
    assert {
        item["agent_id"] for item in document["accepted"]
    } == {first_id, second_id}

    duplicate = client.post(
        ENDPOINT,
        headers=auth(admin),
        json={"action": "restart_mining", "agent_ids": [first_id, second_id]},
    )
    assert duplicate.status_code == 201, duplicate.text
    duplicate_document = duplicate.json()
    assert duplicate_document["accepted_count"] == 0
    assert duplicate_document["rejected_count"] == 2
    for item in duplicate_document["rejected"]:
        assert item["status_code"] == 409
        assert "already in progress" in item["detail"]

    with client.app.state.database.connection() as db:
        commands = db.execute(
            """SELECT agent_id, action, status FROM commands
               WHERE agent_id IN (?, ?)""",
            (first_id, second_id),
        ).fetchall()
        audits = db.execute(
            """SELECT actor, action, target_type, details_json FROM audit_logs
               WHERE action='mining_control_batch_requested' ORDER BY id"""
        ).fetchall()
    assert {(row["agent_id"], row["action"], row["status"]) for row in commands} == {
        (first_id, "restart_mining", "queued"),
        (second_id, "restart_mining", "queued"),
    }
    assert len(audits) == 2
    assert all(row["actor"] == "admin" for row in audits)
    assert all(row["target_type"] == "agent" for row in audits)
    first_details = json.loads(audits[0]["details_json"])
    assert first_details["action"] == "restart_mining"
    assert first_details["selected_agent_ids"] == [first_id, second_id]
    assert first_details["accepted_agent_ids"] == [first_id, second_id]
    assert first_details["rejected_agent_ids"] == []
    second_details = json.loads(audits[1]["details_json"])
    assert second_details["accepted_agent_ids"] == []
    assert second_details["rejected_agent_ids"] == [first_id, second_id]


def test_batch_mining_control_requires_operator_role_and_explicit_unique_selection(
    client: TestClient,
):
    admin = login(client)
    agent_id, secret = enroll(client, admin, name="batch-contract")
    assert heartbeat(client, agent_id, secret, name="batch-contract").status_code == 200
    missing_id = str(uuid.uuid4())

    assert client.post(
        ENDPOINT,
        headers=auth(admin),
        json={"action": "stop_mining", "agent_ids": []},
    ).status_code == 422
    assert client.post(
        ENDPOINT,
        headers=auth(admin),
        json={"action": "stop_mining", "agent_ids": [agent_id, agent_id]},
    ).status_code == 422
    assert client.post(
        ENDPOINT,
        headers=auth(admin),
        json={"action": "stop_mining", "agent_ids": ["not-a-uuid"]},
    ).status_code == 422
    assert client.post(
        ENDPOINT,
        headers=auth(admin),
        json={"action": "reboot", "agent_ids": [agent_id]},
    ).status_code == 422
    assert client.post(
        ENDPOINT,
        headers=auth(admin),
        json={"agent_ids": [agent_id]},
    ).status_code == 422

    for username, role in (("viewer", "viewer"), ("operator", "operator")):
        created = client.post(
            "/api/users",
            headers=auth(admin),
            json={
                "username": username,
                "password": f"a-secure-{username}-password",
                "role": role,
            },
        )
        assert created.status_code == 201, created.text
    viewer = login(client, "viewer", "a-secure-viewer-password")
    operator = login(client, "operator", "a-secure-operator-password")

    assert client.post(
        ENDPOINT,
        headers=auth(viewer),
        json={"action": "stop_mining", "agent_ids": [agent_id]},
    ).status_code == 403

    response = client.post(
        ENDPOINT,
        headers=auth(operator),
        json={"action": "stop_mining", "agent_ids": [agent_id, missing_id]},
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["accepted_count"] == 1
    assert document["rejected_count"] == 1
    assert document["accepted"][0]["agent_id"] == agent_id
    assert document["rejected"][0]["agent_id"] == missing_id
    assert document["rejected"][0]["status_code"] == 404
    assert "Agent not found" in document["rejected"][0]["detail"]
