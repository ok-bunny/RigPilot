from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login, controller_settings_kwargs

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient) -> str:
    return _login(client, 'admin', 'correct-horse-battery-staple')


def enroll(client: TestClient, token: str) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "dual-test", "ttl_minutes": 30},
    ).json()
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued["token"],
            "name": "dual-rig",
            "hostname": "dual-rig.local",
            "os_family": "linux",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def create_wallet(client: TestClient, token: str, coin: str, address: str):
    response = client.post(
        "/api/wallets",
        headers=auth(token),
        json={"name": f"{coin} public", "coin": coin, "address": address},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_catalog_rejects_unusable_download_urls_before_saving(client):
    headers = auth(login(client))
    package = {"name": "example", "version": "1.0", "archive_type": "zip"}
    for url in (
        "https://packages.example.test:not-a-port/miner.zip",
        "https://packages.example.test:65536/miner.zip",
        "https://packages.example.test:0/miner.zip",
        "https://packages.example.test/miner build.zip",
    ):
        response = client.post("/api/miner-packages", headers=headers, json={**package, "download_url": url})
        assert response.status_code == 422
    assert client.get("/api/miner-packages", headers=headers).json() == []
    valid_url = "https://packages.example.test:8443/miner%20build.zip?revision=2"
    created = client.post("/api/miner-packages", headers=headers, json={**package, "download_url": valid_url})
    assert created.status_code == 201
    assert created.json()["download_url"] == valid_url


def test_package_catalog_dual_miner_and_wire_payload(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    primary_wallet = create_wallet(
        client, token, "BTC", "bc1qdualprimarypublicaddress00000000"
    )
    secondary_wallet = create_wallet(
        client, token, "KAS", "kaspa:qdualsecondarypublicaddress000000"
    )

    insecure = client.post(
        "/api/miner-packages",
        headers=auth(token),
        json={
            "name": "bad",
            "version": "1.0",
            "download_url": "http://downloads.example.test/miner.zip",
            "sha256": "a" * 64,
            "archive_type": "zip",
        },
    )
    assert insecure.status_code == 422

    # SHA-256 is optional metadata: registering a legacy external URL no
    # longer requires the operator to calculate a digest.
    without_hash = client.post(
        "/api/miner-packages",
        headers=auth(token),
        json={
            "name": "no-hash-required",
            "version": "1.0",
            "download_url": "https://downloads.example.test/miner.zip",
            "archive_type": "zip",
        },
    )
    assert without_hash.status_code == 201, without_hash.text
    assert without_hash.json()["sha256"] is None
    assert (
        client.get("/api/miner-packages", headers=auth(token)).json()[0]["sha256"]
        is None
    )
    assert (
        client.delete(
            f"/api/miner-packages/{without_hash.json()['id']}", headers=auth(token)
        ).status_code
        == 200
    )

    # A digest supplied by an older API client is still accepted as metadata.
    bad_hash = client.post(
        "/api/miner-packages",
        headers=auth(token),
        json={
            "name": "bad",
            "version": "1.0",
            "download_url": "https://downloads.example.test/miner.zip",
            "sha256": "not-a-hash",
            "archive_type": "zip",
        },
    )
    assert bad_hash.status_code == 422
    malformed_url = client.post(
        "/api/miner-packages",
        headers=auth(token),
        json={
            "name": "bad",
            "version": "1.0",
            "download_url": "https://[::1/miner.zip",
            "sha256": "a" * 64,
            "archive_type": "zip",
        },
    )
    assert malformed_url.status_code == 422

    package_response = client.post(
        "/api/miner-packages",
        headers=auth(token),
        json={
            "name": "safe-miner",
            "version": "1.2.3",
            "download_url": "https://downloads.example.test/safe-miner-1.2.3.tar.gz",
            "sha256": "A" * 64,
            "archive_type": "tar.gz",
            "note": "Linux x86_64; test metadata only",
        },
    )
    assert package_response.status_code == 201, package_response.text
    package = package_response.json()
    assert package["sha256"] == "a" * 64

    sheet_response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "BTC + KAS two miners",
            "coin": "BTC",
            "wallet_id": primary_wallet["id"],
            "pool_url": "stratum+ssl://primary.example.test:443",
            "miner_profile": "primary-safe",
            "miner_package_id": package["id"],
            "algorithm": "sha256d",
            "pool_password": "x",
            "device_indices": [0, 1],
            "worker_name_template": "{hostname}",
            "extra_config": {"tls": True},
            "secondary": {
                "coin": "KAS",
                "wallet_id": secondary_wallet["id"],
                "pool_url": "stratum+tcp://secondary.example.test:4444",
                "miner_profile": "secondary-safe",
                "miner_package_id": package["id"],
                "algorithm": "kheavyhash",
                "pool_password": "x",
                "device_indices": [2],
                "extra_config": {"intensity": 5},
            },
        },
    )
    assert sheet_response.status_code == 201, sheet_response.text
    sheet = sheet_response.json()
    assert sheet["miner_package"]["version"] == "1.2.3"
    assert sheet["secondary"]["coin"] == "KAS"
    assert sheet["secondary"]["device_indices"] == [2]

    assert (
        client.delete(
            f"/api/miner-packages/{package['id']}", headers=auth(token)
        ).status_code
        == 409
    )
    assert (
        client.delete(
            f"/api/wallets/{secondary_wallet['id']}", headers=auth(token)
        ).status_code
        == 409
    )

    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    payload = applied.json()["payload"]
    assert set(payload) == {
        "flight_sheet_id",
        "name",
        "worker_name",
        "primary",
        "secondary",
    }
    assert set(payload["primary"]["miner_package"]) == {
        "id",
        "name",
        "version",
        "download_url",
        "sha256",
        "archive_type",
    }
    assert payload["primary"]["miner_package"]["sha256"] == "a" * 64
    assert payload["primary"]["algorithm"] == "sha256d"
    assert payload["secondary"]["wallet_address"] == secondary_wallet["address"]
    assert payload["secondary"]["device_indices"] == [2]
    assert set(payload["secondary"]["miner_package"]) == {
        "id",
        "name",
        "version",
        "download_url",
        "sha256",
        "archive_type",
    }
    assert payload["secondary"]["miner_package"]["sha256"] == "a" * 64

    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={"hostname": "dual-rig.local", "os_family": "linux"},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["commands"][0]["action"] == "apply_flight_sheet"
    assert client.post(
        f"/api/agent/commands/{heartbeat.json()['commands'][0]['id']}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={"status": "failed", "result": {}, "error": "test cleanup"},
    ).status_code == 200

    listed = client.get("/api/flight-sheets", headers=auth(token)).json()[0]
    assert listed["secondary"]["miner_package"]["id"] == package["id"]
    assert listed["coin"] == "BTC"

    assert (
        client.delete(f"/api/flight-sheets/{sheet['id']}", headers=auth(token)).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/wallets/{primary_wallet['id']}", headers=auth(token)
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/wallets/{secondary_wallet['id']}", headers=auth(token)
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/miner-packages/{package['id']}", headers=auth(token)
        ).status_code
        == 200
    )
