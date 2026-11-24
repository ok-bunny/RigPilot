from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import Settings
from server.main import create_app


PASSWORD = "correct-horse-battery-staple"
CAPABILITY = "remote_access.ssh_manage.v1"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "linux-ssh.db",
        secret_key="linux-ssh-tests-have-enough-secret-key-entropy",
        admin_username="admin",
        admin_password=PASSWORD,
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as value:
        yield value


def _admin(client: TestClient) -> dict[str, str]:
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _agent(
    client: TestClient,
    admin: dict[str, str],
    *,
    name: str = "linux-ssh",
    os_family: str = "linux",
    capabilities: list[str] | None = None,
):
    enrollment = client.post(
        "/api/enrollment-tokens",
        headers=admin,
        json={"label": name, "ttl_minutes": 30},
    ).json()["token"]
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": enrollment,
            "hostname": name,
            "os_family": os_family,
            "agent_version": "0.6.33",
        },
    ).json()
    headers = {
        "X-Agent-ID": enrolled["agent_id"],
        "Authorization": f"Bearer {enrolled['agent_secret']}",
    }
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": name,
            "os_family": os_family,
            "agent_version": "0.6.33",
            "capabilities": [CAPABILITY] if capabilities is None else capabilities,
            "inventory": {
                "linux_ssh": {
                    "schema_version": 1,
                    "manageable": True,
                    "installed": True,
                    "listening": False,
                    "managed_policy": False,
                    "effective_policy": False,
                    "port": 22,
                    "account": "user",
                    "access_scope": "private_sources_only",
                    "reason": "service_stopped",
                }
            },
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return enrolled["agent_id"], headers


def test_admin_can_queue_linux_ssh_enable_and_agent_receives_exact_action(client):
    admin = _admin(client)
    agent_id, agent_headers = _agent(client, admin)
    url = f"/api/agents/{agent_id}/linux-ssh"

    assert client.post(url, json={"enabled": True}).status_code == 401
    assert (
        client.post(url, headers=admin, json={"enabled": True, "port": 2222}).status_code
        == 422
    )
    created = client.post(url, headers=admin, json={"enabled": True})
    assert created.status_code == 201, created.text
    assert created.json()["action"] == "configure_linux_ssh.v1"
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers=agent_headers,
        json={
            "hostname": "linux-ssh",
            "os_family": "linux",
            "agent_version": "0.6.33",
            "capabilities": [CAPABILITY],
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"][0] == {
        "id": created.json()["id"],
        "action": "configure_linux_ssh.v1",
        "payload": {"enabled": True},
    }


def test_linux_ssh_management_is_linux_only_and_capability_gated(client):
    admin = _admin(client)
    windows_id, _ = _agent(client, admin, name="windows", os_family="windows")
    windows = client.post(
        f"/api/agents/{windows_id}/linux-ssh",
        headers=admin,
        json={"enabled": True},
    )
    assert windows.status_code == 409
    assert "Linux-only" in windows.json()["detail"]

    old_id, _ = _agent(client, admin, name="old-linux", capabilities=[])
    unsupported = client.post(
        f"/api/agents/{old_id}/linux-ssh",
        headers=admin,
        json={"enabled": True},
    )
    assert unsupported.status_code == 409


def test_static_ui_hides_ssh_management_card_but_keeps_backend_compatibility():
    app_js = (Path(__file__).parents[1] / "server" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    # The whole remote workspace UI was removed in 0.8.48.
    assert "linuxRemoteAccessMarkup" not in app_js
    assert "openRemoteWorkspace" not in app_js
    assert "本机 SSH 22" not in app_js
    assert "data-configure-linux-ssh" not in app_js
