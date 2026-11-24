from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="miner-log-tests-secret-key-that-is-long-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def audit_snapshot(client: TestClient, token: str) -> list[dict[str, object]]:
    response = client.get("/api/audit?limit=500", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def login(client: TestClient, username: str, password: str) -> str:
    return _login(client, username, password)


def enroll(
    client: TestClient, admin_token: str, *, name: str, version: str
) -> tuple[str, str]:
    token_response = client.post(
        "/api/enrollment-tokens",
        headers=auth(admin_token),
        json={"label": name, "ttl_minutes": 30},
    )
    assert token_response.status_code == 200, token_response.text
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": token_response.json()["token"],
            "name": name,
            "hostname": f"{name}.local",
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": version,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def test_miner_log_command_enforces_role_payload_and_agent_version(client: TestClient):
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    for username, role in (("viewer", "viewer"), ("operator", "operator")):
        created = client.post(
            "/api/users",
            headers=auth(admin_token),
            json={
                "username": username,
                "password": f"a-secure-{username}-password",
                "role": role,
            },
        )
        assert created.status_code == 201, created.text
    viewer_token = login(client, "viewer", "a-secure-viewer-password")
    operator_token = login(client, "operator", "a-secure-operator-password")
    agent_id, _ = enroll(client, admin_token, name="new-agent", version="0.5.1")

    forbidden = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(viewer_token),
        json={"action": "collect_miner_logs", "payload": {}},
    )
    assert forbidden.status_code == 403

    extra_payload = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(operator_token),
        json={"action": "collect_miner_logs", "payload": {"seconds": 120}},
    )
    assert extra_payload.status_code == 422
    assert "payload must be empty" in extra_payload.json()["detail"]

    operator_request = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(operator_token),
        json={"action": "collect_miner_logs", "payload": {}},
    )
    assert operator_request.status_code == 201, operator_request.text
    assert operator_request.json()["payload"] == {}

    admin_request = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_miner_logs", "payload": {}},
    )
    assert admin_request.status_code == 201, admin_request.text

    old_agent_id, _ = enroll(client, admin_token, name="old-agent", version="0.5.0")
    needs_upgrade = client.post(
        f"/api/agents/{old_agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_miner_logs", "payload": {}},
    )
    assert needs_upgrade.status_code == 409
    assert "0.5.1" in needs_upgrade.json()["detail"]
    assert "upgrade" in needs_upgrade.json()["detail"].lower()


def test_pending_live_log_snapshot_can_be_cancelled_before_agent_receives_it(
    client: TestClient,
):
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, agent_secret = enroll(
        client, admin_token, name="live-log-agent", version="0.5.2"
    )
    audit_before = audit_snapshot(client, admin_token)
    queued = client.post(
        f"/api/agents/{agent_id}/commands?live=true",
        headers=auth(admin_token),
        json={"action": "collect_miner_logs", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]
    listed = client.get("/api/commands", headers=auth(admin_token))
    assert listed.status_code == 200, listed.text
    assert listed.json() == []
    summary = client.get("/api/summary", headers=auth(admin_token))
    assert summary.status_code == 200, summary.text
    assert summary.json()["active_commands"] == 0

    cancelled = client.post(
        f"/api/commands/{command_id}/cancel", headers=auth(admin_token)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"status": "cancelled"}

    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
        json={
            "hostname": "live-log-agent.local",
            "os_family": "windows",
            "agent_version": "0.5.2",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == []

    exact = client.get(f"/api/commands/{command_id}", headers=auth(admin_token))
    assert exact.status_code == 200, exact.text
    assert exact.json()["status"] == "cancelled"
    assert audit_snapshot(client, admin_token) == audit_before


def test_live_log_running_and_final_results_never_write_audit(client: TestClient):
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, agent_secret = enroll(
        client, admin_token, name="live-result-agent", version="0.5.2"
    )
    audit_before = audit_snapshot(client, admin_token)
    queued = client.post(
        f"/api/agents/{agent_id}/commands?live=true",
        headers=auth(admin_token),
        json={"action": "collect_miner_logs", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]
    agent_headers = {
        "X-Agent-ID": agent_id,
        "Authorization": f"Bearer {agent_secret}",
    }
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers=agent_headers,
        json={
            "hostname": "live-result-agent.local",
            "os_family": "windows",
            "agent_version": "0.5.2",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert [item["id"] for item in heartbeat.json()["commands"]] == [command_id]

    running = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=agent_headers,
        json={"status": "running", "result": {}, "error": None},
    )
    assert running.status_code == 200, running.text
    result_document = {
        "captured_at": "2026-07-15T03:04:05+00:00",
        "window_seconds": 60,
        "miner_type": "srbminer",
        "running": True,
        "logs": [{"source": "miner", "lines": ["accepted share"], "truncated": False}],
    }
    finished = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=agent_headers,
        json={"status": "succeeded", "result": result_document, "error": None},
    )
    assert finished.status_code == 200, finished.text
    assert audit_snapshot(client, admin_token) == audit_before

    exact = client.get(f"/api/commands/{command_id}", headers=auth(admin_token))
    assert exact.status_code == 200, exact.text
    assert exact.json()["status"] == "succeeded"
    assert exact.json()["result"] == result_document

    created_viewer = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={
            "username": "log-viewer",
            "password": "a-secure-log-viewer-password",
            "role": "viewer",
        },
    )
    assert created_viewer.status_code == 201, created_viewer.text
    viewer_token = login(
        client, "log-viewer", "a-secure-log-viewer-password"
    )
    viewer_exact = client.get(
        f"/api/commands/{command_id}", headers=auth(viewer_token)
    )
    assert viewer_exact.status_code == 200, viewer_exact.text
    assert viewer_exact.json()["result"]["log_output_omitted"] is True
    assert "logs" not in viewer_exact.json()["result"]


def test_live_log_creation_is_scoped_and_rate_limited(client: TestClient):
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    created = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={
            "username": "live-operator",
            "password": "a-secure-live-operator-password",
            "role": "operator",
        },
    )
    assert created.status_code == 201, created.text
    operator_token = login(
        client, "live-operator", "a-secure-live-operator-password"
    )
    agent_id, _ = enroll(client, admin_token, name="limited-agent", version="0.5.2")
    path = f"/api/agents/{agent_id}/commands?live=true"
    body = {"action": "collect_miner_logs", "payload": {}}

    first = client.post(path, headers=auth(admin_token), json=body)
    assert first.status_code == 201, first.text
    duplicate = client.post(path, headers=auth(admin_token), json=body)
    assert duplicate.status_code == 409
    assert "already in progress" in duplicate.json()["detail"]

    # The limit is per user and device, so another operator gets one slot.
    other_user = client.post(path, headers=auth(operator_token), json=body)
    assert other_user.status_code == 201, other_user.text
    other_duplicate = client.post(path, headers=auth(operator_token), json=body)
    assert other_duplicate.status_code == 409

    cancelled = client.post(
        f"/api/commands/{first.json()['id']}/cancel", headers=auth(admin_token)
    )
    assert cancelled.status_code == 200, cancelled.text
    too_fast = client.post(path, headers=auth(admin_token), json=body)
    assert too_fast.status_code == 429
    assert too_fast.headers["retry-after"] == "2"

    # Old terminal rows are removed on the next accepted live request.
    with client.app.state.db.connection() as db:
        db.execute(
            """UPDATE commands SET created_at=?, finished_at=? WHERE id=?""",
            ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:01+00:00", first.json()["id"]),
        )
    after_interval = client.post(path, headers=auth(admin_token), json=body)
    assert after_interval.status_code == 201, after_interval.text
    removed = client.get(
        f"/api/commands/{first.json()['id']}", headers=auth(admin_token)
    )
    assert removed.status_code == 404


def test_live_mode_is_rejected_for_non_log_commands(client: TestClient):
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, _ = enroll(client, admin_token, name="live-mode-agent", version="0.5.2")

    response = client.post(
        f"/api/agents/{agent_id}/commands?live=true",
        headers=auth(admin_token),
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "live mode is only valid for miner logs"


def test_miner_log_result_uses_exact_endpoint_and_is_summarized_in_lists(
    client: TestClient,
):
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, agent_secret = enroll(
        client, admin_token, name="log-agent", version="0.5.1+portable"
    )
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_miner_logs", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]
    agent_headers = {
        "X-Agent-ID": agent_id,
        "Authorization": f"Bearer {agent_secret}",
    }
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers=agent_headers,
        json={
            "hostname": "log-agent.local",
            "os_family": "windows",
            "agent_version": "0.5.1+portable",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == [
        {"id": command_id, "action": "collect_miner_logs", "payload": {}}
    ]

    log_line = '<script>alert("must be escaped by the browser")</script>'
    result_document = {
        "captured_at": "2026-07-15T03:04:05+00:00",
        "window_seconds": 60,
        "miner_type": "xmrig",
        "running": True,
        "logs": [
            {
                "source": "xmrig.log",
                "lines": [log_line, "accepted (1/0)"],
                "truncated": False,
            }
        ],
    }
    completed = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=agent_headers,
        json={"status": "succeeded", "result": result_document, "error": None},
    )
    assert completed.status_code == 200, completed.text

    listed = client.get(
        f"/api/commands?agent_id={agent_id}&limit=20", headers=auth(admin_token)
    )
    assert listed.status_code == 200, listed.text
    summary = listed.json()[0]["result"]
    assert summary["source_count"] == 1
    assert summary["log_output_omitted"] is True
    assert "logs" not in summary
    assert log_line not in json.dumps(listed.json())

    exact = client.get(f"/api/commands/{command_id}", headers=auth(admin_token))
    assert exact.status_code == 200, exact.text
    assert exact.json()["result"] == result_document
    assert client.get(f"/api/commands/{command_id}").status_code == 401
    assert (
        client.get("/api/commands/not-a-command", headers=auth(admin_token)).status_code
        == 404
    )
