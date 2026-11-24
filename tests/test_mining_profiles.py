from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login, controller_settings_kwargs

from server.config import Settings
from server.main import _render_worker_name, create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient, username: str = "admin", password: str = "correct-horse-battery-staple") -> str:
    return _login(client, username, password)


def enroll(client: TestClient, token: str) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "profile-test", "ttl_minutes": 30},
    )
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": "rig-a",
            "hostname": "rig-a.local",
            "os_family": "linux",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def test_worker_placeholder_replacement_is_single_pass():
    agent = {
        "id": "agent-123",
        "name": "{agent_id}",
        "hostname": "TEST-WORKER-01",
    }

    assert (
        _render_worker_name("{hostname}-{system_hostname}-{agent_id}", agent)
        == "{agent_id}-TEST-WORKER-01-agent-123"
    )


def test_wallet_flight_sheet_lifecycle_and_gpu_profile_api_retired(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)

    secret_field = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "must-reject",
            "coin": "BTC",
            "address": "bc1qpublicaddress",
            "private_key": "not-accepted",
        },
    )
    assert secret_field.status_code == 422
    phrase = client.post(
        "/api/wallets",
        headers=auth(token),
        json={"name": "must-reject", "coin": "BTC", "address": "seed words are not an address"},
    )
    assert phrase.status_code == 422
    for secret_like in (
        "xprv" + "A" * 100,
        "a" * 64,
        "5" + "A" * 50,
        "S" + "A" * 55,
    ):
        rejected = client.post(
            "/api/wallets",
            headers=auth(token),
            json={"name": "secret", "coin": "BTC", "address": secret_like},
        )
        assert rejected.status_code == 422

    wallet_response = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "公开收款地址",
            "coin": "btc",
            "address": "bc1qexamplepublicaddress000000000000",
            "note": "测试地址",
        },
    )
    assert wallet_response.status_code == 201, wallet_response.text
    wallet = wallet_response.json()
    assert wallet["coin"] == "BTC"

    invalid_pool = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "bad",
            "coin": "BTC",
            "wallet_id": wallet["id"],
            "pool_url": "https://pool.example.test",
            "miner_profile": "xmrig-safe",
        },
    )
    assert invalid_pool.status_code == 422

    malformed_pool = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "bad-ipv6",
            "coin": "BTC",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+tcp://[::1:3333",
            "miner_profile": "xmrig-safe",
        },
    )
    assert malformed_pool.status_code == 422

    sheet_response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "BTC 夜间",
            "coin": "BTC",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
            "miner_profile": "xmrig-safe",
            "worker_name_template": "{hostname}-{system_hostname}",
            "extra_config": {"password": "x", "tls": True},
        },
    )
    assert sheet_response.status_code == 201, sheet_response.text
    sheet = sheet_response.json()

    default_profile_response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "BTC 默认模板",
            "coin": "BTC",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
        },
    )
    assert default_profile_response.status_code == 201, default_profile_response.text
    default_sheet = default_profile_response.json()
    assert default_sheet["miner_profile"] == "default"

    referenced = client.delete(f"/api/wallets/{wallet['id']}", headers=auth(token))
    assert referenced.status_code == 409

    # 0.8.50 retired the whole-fleet GPU profile CRUD: per-GPU OC on the
    # device detail is the only supported path, so every legacy route 404s.
    assert client.get("/api/gpu-profiles", headers=auth(token)).status_code == 404
    assert client.post(
        "/api/gpu-profiles",
        headers=auth(token),
        json={"name": "gone", "vendor": "nvidia", "settings": [{"index": 0, "power_limit_w": 120}]},
    ).status_code == 404
    assert client.delete("/api/gpu-profiles/legacy-profile", headers=auth(token)).status_code == 404

    applied_sheet = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied_sheet.status_code == 201, applied_sheet.text
    assert applied_sheet.json()["payload"]["worker_name"] == "rig-a-rig-a.local"
    assert applied_sheet.json()["payload"]["primary"]["wallet_address"] == wallet["address"]
    assert applied_sheet.json()["payload"]["secondary"] is None

    assert client.post(
        f"/api/agents/{agent_id}/gpu-profiles/legacy-profile/apply",
        headers=auth(token),
    ).status_code == 404

    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={"hostname": "rig-a.local", "os_family": "linux"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    commands = heartbeat.json()["commands"]
    assert {command["action"] for command in commands} == {"apply_flight_sheet"}
    assert all(set(command) == {"id", "action", "payload"} for command in commands)
    flight_command = next(
        command for command in commands if command["action"] == "apply_flight_sheet"
    )
    assert client.post(
        f"/api/agent/commands/{flight_command['id']}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={"status": "failed", "result": {}, "error": "test cleanup"},
    ).status_code == 200

    assert len(client.get("/api/wallets", headers=auth(token)).json()) == 1
    assert len(client.get("/api/flight-sheets", headers=auth(token)).json()) == 2

    assert client.delete(f"/api/flight-sheets/{sheet['id']}", headers=auth(token)).status_code == 200
    assert client.delete(f"/api/flight-sheets/{default_sheet['id']}", headers=auth(token)).status_code == 200
    assert client.delete(f"/api/wallets/{wallet['id']}", headers=auth(token)).status_code == 200


def test_new_resources_enforce_roles(client: TestClient):
    admin_token = login(client)
    created = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={"username": "reader2", "password": "a-very-long-reader-password", "role": "viewer"},
    )
    assert created.status_code == 201
    viewer_token = login(client, "reader2", "a-very-long-reader-password")
    assert client.get("/api/wallets", headers=auth(viewer_token)).status_code == 200
    assert (
        client.get("/api/miner-packages", headers=auth(viewer_token)).status_code
        == 200
    )
    forbidden = client.post(
        "/api/wallets",
        headers=auth(viewer_token),
        json={"name": "x", "coin": "BTC", "address": "bc1qpublicaddress"},
    )
    assert forbidden.status_code == 403
    forbidden_package = client.post(
        "/api/miner-packages",
        headers=auth(viewer_token),
        json={
            "name": "x",
            "version": "1.0",
            "download_url": "https://downloads.example.test/x.zip",
            "sha256": "a" * 64,
            "archive_type": "zip",
        },
    )
    assert forbidden_package.status_code == 403
