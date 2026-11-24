from __future__ import annotations

from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from agent.commands import Command
from server import __version__ as server_version
from controller_test_support import auth, login as _login, controller_settings_kwargs

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings), client=("8.8.4.4", 50000)) as test_client:
        yield test_client


def login(client: TestClient, username: str = "admin", password: str = "correct-horse-battery-staple") -> str:
    return _login(client, username, password)


def enroll_agent(client: TestClient, token: str) -> tuple[str, str, str]:
    created = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "test-node", "ttl_minutes": 30},
    )
    assert created.status_code == 200, created.text
    enrollment = created.json()["token"]
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": enrollment,
            "name": "worker-01",
            "hostname": "worker-01.local",
            "os_family": "linux",
            "platform": "Ubuntu 24.04",
            "agent_version": "0.1.0",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return enrollment, body["agent_id"], body["agent_secret"]


def test_login_and_health(client: TestClient):
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["version"] == server_version
    assert client.app.version == server_version
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    token = login(client)
    assert client.get("/api/auth/me", headers=auth(token)).json()["role"] == "admin"


def test_index_cannot_reuse_a_stale_static_asset_version(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"


def test_device_workload_endpoint_requires_range_and_uses_bucket_tables(client: TestClient):
    token = login(client)
    response = client.get(
        "/api/workload",
        headers=auth(token),
    )
    assert response.status_code == 422

    with client.app.state.db.connection() as db:
        rows = db.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table'
                 AND name IN (
                    'device_work_state','device_work_buckets',
                    'telemetry_workload','workload_projection_state'
                 )
               ORDER BY name"""
        ).fetchall()
    assert [row["name"] for row in rows] == [
        "device_work_buckets",
        "device_work_state",
    ]




def test_enrollment_is_one_time_and_agent_reports_metrics(client: TestClient):
    token = login(client)
    enrollment, agent_id, agent_secret = enroll_agent(client, token)
    duplicate = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": enrollment,
            "hostname": "duplicate",
            "os_family": "windows",
        },
    )
    assert duplicate.status_code == 401

    gpu_memory = [
        {
            "index": 0,
            "uuid": "GPU-memory-known",
            "memory_vendor": "Micron",
            "memory_type": "GDDR7",
            "memory_identity_source": "nvapi",
            "memory_identity_status": "complete",
            "memory_identity_unavailable_reason": None,
        },
        {
            "index": 1,
            "uuid": "GPU-memory-unknown",
            "memory_vendor": None,
            "memory_type": None,
            "memory_identity_source": "nvml_private",
            "memory_identity_status": "unavailable",
            "memory_identity_unavailable_reason": "driver_memory_identity_unknown",
        },
    ]
    agent_headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"}
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers=agent_headers,
        json={
            "hostname": "worker-01.local",
            "os_family": "linux",
            "platform": "Ubuntu 24.04",
            "agent_version": "0.1.0",
            "ip_addresses": ["192.0.2.10"],
            "local_ip_addresses": ["192.168.50.12", "fd00::12"],
            "public_ip_addresses": ["1.1.1.1"],
            "metrics": {
                "cpu_percent": 32, "memory_percent": 48, "disk_percent": 51,
                "gpus": gpu_memory,
            },
            "inventory": {"cpu_count": 8, "gpus": gpu_memory},
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == []
    agents = client.get("/api/agents", headers=auth(token)).json()
    assert agents[0]["status"] == "online"
    assert agents[0]["metrics"]["cpu_percent"] == 32
    assert agents[0]["public_ip"] == "8.8.4.4"
    assert agents[0]["local_ip"] == "192.168.50.12"
    assert agents[0]["local_ip_addresses"] == ["192.168.50.12", "fd00::12"]
    detail = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()
    assert detail["public_ip"] == "8.8.4.4"
    assert detail["local_ip_addresses"] == ["192.168.50.12", "fd00::12"]
    telemetry = client.get(
        f"/api/agents/{agent_id}/telemetry", headers=auth(token)
    )
    assert telemetry.status_code == 200, telemetry.text
    samples = telemetry.json()
    assert len(samples) == 1
    for reported in (agents[0], detail):
        assert reported["metrics"]["gpus"] == gpu_memory
        assert reported["inventory"]["gpus"] == gpu_memory
    assert samples[0]["metrics"]["gpus"] == gpu_memory
    with client.app.state.db.connection() as db:
        stored = db.execute(
            """SELECT public_ip, local_ip_addresses_json, metrics_json, inventory_json
               FROM agents WHERE id=?""",
            (agent_id,),
        ).fetchone()
    assert stored["public_ip"] == "8.8.4.4"
    assert json.loads(stored["local_ip_addresses_json"]) == ["192.168.50.12", "fd00::12"]
    assert json.loads(stored["metrics_json"])["gpus"] == gpu_memory
    assert json.loads(stored["inventory_json"])["gpus"] == gpu_memory


def test_removed_device_tag_ui_keeps_legacy_api_compatibility(client: TestClient):
    token = login(client)
    _, agent_id, _ = enroll_agent(client, token)

    updated = client.patch(
        f"/api/agents/{agent_id}",
        headers=auth(token),
        json={"tags": ["legacy-group"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["legacy-group"]

    filtered = client.get("/api/agents?tag=legacy-group", headers=auth(token))
    assert filtered.status_code == 200, filtered.text
    assert [agent["id"] for agent in filtered.json()] == [agent_id]


def test_legacy_heartbeat_ip_addresses_still_supply_lan_network(client: TestClient):
    token = login(client)
    _, agent_id, agent_secret = enroll_agent(client, token)
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "hostname": "legacy-rig",
            "os_family": "linux",
            "ip_addresses": ["10.20.30.40", "fe80::1"],
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text

    agent = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()
    assert agent["public_ip"] == "8.8.4.4"
    assert agent["local_ip_addresses"] == ["10.20.30.40"]


def test_heartbeat_rejects_mislabeled_network_addresses(client: TestClient):
    token = login(client)
    _, agent_id, agent_secret = enroll_agent(client, token)
    headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"}

    invalid_local = client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": "rig",
            "os_family": "linux",
            "local_ip_addresses": ["8.8.8.8"],
        },
    )
    invalid_public = client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": "rig",
            "os_family": "linux",
            "public_ip_addresses": ["192.168.1.2"],
        },
    )
    assert invalid_local.status_code == 422
    assert invalid_public.status_code == 422


def test_allowlisted_command_round_trip(client: TestClient):
    token = login(client)
    _, agent_id, agent_secret = enroll_agent(client, token)
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "collect_diagnostics", "payload": {"ignored": "value"}},
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]
    agent_headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"}
    poll = client.post(
        "/api/agent/heartbeat",
        headers=agent_headers,
        json={"hostname": "worker-01", "os_family": "linux"},
    )
    assert [command["id"] for command in poll.json()["commands"]] == [command_id]
    wire_command = poll.json()["commands"][0]
    parsed = Command.parse(wire_command)
    assert parsed.command_id == command_id
    result = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=agent_headers,
        json={"status": "succeeded", "result": {"uptime_seconds": 120}, "error": None},
    )
    assert result.status_code == 200
    commands = client.get("/api/commands", headers=auth(token)).json()
    assert commands[0]["status"] == "succeeded"
    assert commands[0]["result"]["uptime_seconds"] == 120


def test_roles_and_service_name_validation(client: TestClient):
    admin_token = login(client)
    created = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={"username": "reader", "password": "a-very-long-reader-password", "role": "viewer"},
    )
    assert created.status_code == 201
    reader_token = login(client, "reader", "a-very-long-reader-password")
    _, agent_id, _ = enroll_agent(client, admin_token)
    forbidden = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(reader_token),
        json={"action": "reboot", "payload": {}},
    )
    assert forbidden.status_code == 403
    injection = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "restart_service", "payload": {"service": "safe; shutdown"}},
    )
    assert injection.status_code == 422


def test_invalid_agent_secret_is_rejected(client: TestClient):
    token = login(client)
    _, agent_id, _ = enroll_agent(client, token)
    response = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": "Bearer not-the-secret"},
        json={"hostname": "worker-01", "os_family": "linux"},
    )
    assert response.status_code == 401
