from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "controller-boundaries.db",
        secret_key="controller-boundaries-secret-with-sufficient-entropy",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
    )
    with TestClient(create_app(settings)) as value:
        yield value


def _identity(client: TestClient):
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    admin = {"Authorization": f"Bearer {login.json()['token']}"}
    token = client.post(
        "/api/enrollment-tokens", headers=admin, json={"label": "boundary-rig"}
    )
    assert token.status_code == 200, token.text
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": token.json()["token"],
            "hostname": "boundary-rig",
            "os_family": "linux",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    agent_id = enrolled.json()["agent_id"]
    agent = {
        "X-Agent-ID": agent_id,
        "Authorization": f"Bearer {enrolled.json()['agent_secret']}",
    }
    return admin, agent_id, agent


def test_heartbeat_rechecks_disabled_state_after_authentication(client: TestClient):
    admin, agent_id, agent_headers = _identity(client)
    route = next(
        route for route in client.app.routes
        if getattr(route, "path", None) == "/api/agent/heartbeat"
    )
    authenticate = route.dependant.dependencies[0].call
    with client.app.state.db.connection() as db:
        authenticated = dict(db.execute(
            "SELECT * FROM agents WHERE id=?", (agent_id,)
        ).fetchone())
    # Preserve the actual dependency result from a request that authenticated
    # immediately before the administrator committed the disable operation.
    client.app.dependency_overrides[authenticate] = lambda: authenticated
    try:
        disabled = client.patch(
            f"/api/agents/{agent_id}", headers=admin, json={"disabled": True}
        )
        assert disabled.status_code == 200, disabled.text
        response = client.post(
            "/api/agent/heartbeat",
            headers=agent_headers,
            json={
                "hostname": "must-not-be-persisted",
                "os_family": "linux",
                "metrics": {"cpu_percent": 99},
            },
        )
    finally:
        client.app.dependency_overrides.clear()
    assert response.status_code == 401, response.text
    with client.app.state.db.connection() as db:
        current = db.execute(
            "SELECT hostname,last_seen,metrics_json FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        telemetry_count = db.execute(
            "SELECT COUNT(*) FROM telemetry WHERE agent_id=?", (agent_id,)
        ).fetchone()[0]
    assert current["hostname"] == "boundary-rig"
    assert current["last_seen"] is None
    assert json.loads(current["metrics_json"]) == {}
    assert telemetry_count == 0


def test_command_result_rechecks_identity_after_authentication(client: TestClient):
    admin, agent_id, agent_headers = _identity(client)
    issued = client.post(f"/api/agents/{agent_id}/commands", headers=admin,
                         json={"action": "collect_diagnostics", "payload": {}})
    assert issued.status_code == 201, issued.text
    command_id = issued.json()["id"]
    route = next(route for route in client.app.routes
                 if getattr(route, "path", None) == "/api/agent/commands/{command_id}/result")
    authenticate = route.dependant.dependencies[0].call
    with client.app.state.db.connection() as db:
        authenticated = dict(db.execute(
            "SELECT * FROM agents WHERE id=?", (agent_id,)
        ).fetchone())
    # The result authenticated before disable, then waited for the writer.
    client.app.dependency_overrides[authenticate] = lambda: authenticated
    try:
        disabled = client.patch(f"/api/agents/{agent_id}", headers=admin, json={"disabled": True})
        assert disabled.status_code == 200, disabled.text
        response = client.post(f"/api/agent/commands/{command_id}/result", headers=agent_headers,
                               json={"status": "succeeded", "result": {"marker": "late"}})
    finally:
        client.app.dependency_overrides.pop(authenticate)
    assert response.status_code == 401, response.text
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT status,result_json FROM commands WHERE id=?", (command_id,)).fetchone()
    assert stored["status"] == "cancelled" and stored["result_json"] is None


@pytest.mark.parametrize("field", ["cpu_percent", "disks", "inventory"])
def test_heartbeat_rejects_overflowing_numbers_before_processing_or_persistence(
    client: TestClient, field: str,
):
    admin, agent_id, headers = _identity(client)
    payload = {"hostname": "boundary-rig", "os_family": "linux"}
    if field == "inventory":
        payload["inventory"] = {"device_counter": 10 ** 309}
    elif field == "disks":
        payload["metrics"] = {"disks": [{"percent": 10 ** 309}]}
    else:
        payload["metrics"] = {"cpu_percent": 10 ** 309}
    response = client.post("/api/agent/heartbeat", headers=headers, json=payload)
    assert response.status_code == 422, response.text
    with client.app.state.db.connection() as db:
        current = db.execute(
            "SELECT last_seen,metrics_json,inventory_json FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
    assert current["last_seen"] is None
    assert json.loads(current["metrics_json"]) == {}
    assert json.loads(current["inventory_json"]) == {}
    assert client.get("/api/agents", headers=admin).status_code == 200
    assert client.get("/api/summary", headers=admin).status_code == 200


def test_legacy_oversized_metric_cannot_break_other_devices_dashboard(client: TestClient):
    admin, agent_id, _headers = _identity(client)
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET metrics_json=?, last_seen=strftime('%Y-%m-%dT%H:%M:%f+00:00','now') WHERE id=?",
            (json.dumps({"cpu_percent": 10 ** 309}), agent_id),
        )
    for endpoint in ("/api/agents", "/api/summary", f"/api/agents/{agent_id}"):
        response = client.get(endpoint, headers=admin)
        assert response.status_code == 200, response.text
