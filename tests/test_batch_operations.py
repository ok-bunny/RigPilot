from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login, gpu_oc_success_result, portable_zip as _portable_zip

from server.config import Settings
from server.database import Database
from server.main import create_app


ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        agent_online_seconds=60,
        max_agent_release_bytes=128 * 1024,
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
    os_family: str = "windows",
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
            "os_family": os_family,
            "platform": "Windows 11 x64" if os_family == "windows" else "Ubuntu 22.04",
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
    os_family: str = "windows",
    version: str = "0.6.44",
    capabilities: list[str] | None = None,
    gpus: list[dict[str, object]] | None = None,
):
    return client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={
            "hostname": f"{name}.local",
            "os_family": os_family,
            "platform": "Windows 11 x64" if os_family == "windows" else "Ubuntu 22.04",
            "agent_version": version,
            "capabilities": capabilities or [],
            "inventory": {"architecture": "AMD64"},
            "metrics": {
                "mining_status": {
                    "configured": True,
                    "state": "running",
                    "flight_sheet_id": "flight-batch-test",
                    "process_verified": True,
                },
                "gpus": gpus or [],
            },
        },
    )


def gpu(
    index: int,
    uuid: str,
    *,
    capabilities: list[str] | None = None,
) -> dict[str, object]:
    return {
        "index": index,
        "uuid": uuid,
        "name": f"NVIDIA GeForce RTX 50{index}0",
        "vendor": "nvidia",
        "oc_capabilities": capabilities
        or ["power_limit_w", "fan_percent"],
        "power_min_limit_w": 100,
        "power_max_limit_w": 450,
        "fan_min_percent": 30,
        "fan_max_percent": 100,
    }


def finish_dispatched(
    client: TestClient,
    agent_id: str,
    secret: str,
    *,
    name: str,
    gpus: list[dict[str, object]],
) -> list[dict[str, object]]:
    response = heartbeat(
        client,
        agent_id,
        secret,
        name=name,
        gpus=gpus,
    )
    assert response.status_code == 200, response.text
    commands = response.json()["commands"]
    for command in commands:
        result = client.post(
            f"/api/agent/commands/{command['id']}/result",
            headers={
                "X-Agent-ID": agent_id,
                "Authorization": f"Bearer {secret}",
            },
            json={
                "status": "succeeded",
                "result": (
                    gpu_oc_success_result(command)
                    if command["action"] == "apply_gpu_profile"
                    else {}
                ),
                "error": None,
            },
        )
        assert result.status_code == 200, result.text
    return commands


def portable_zip(version: str) -> bytes:
    return _portable_zip(version=version)


def create_generic_flight_sheet(client: TestClient, token: str) -> dict[str, object]:
    wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "Batch wallet",
            "coin": "XMR",
            "address": "47batchpublicwalletaddress000000000000000000000",
            "note": "",
        },
    )
    assert wallet.status_code == 201, wallet.text
    sheet = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "Batch generic flight",
            "miner_type": "generic",
            "coin": "XMR",
            "wallet_id": wallet.json()["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
            "miner_profile": "default",
            "worker_name_template": "{hostname}",
            "extra_config": {},
        },
    )
    assert sheet.status_code == 201, sheet.text
    return sheet.json()


def test_batch_gpu_oc_is_explicit_uuid_bound_and_preserves_unselected_cards(
    client: TestClient,
):
    token = login(client)
    agent_id, secret = enroll(client, token, name="gpu-batch")
    gpus = [
        gpu(0, "GPU-batch-A"),
        gpu(1, "GPU-batch-B"),
        gpu(2, "GPU-batch-C"),
    ]
    assert heartbeat(
        client, agent_id, secret, name="gpu-batch", gpus=gpus
    ).status_code == 200

    # Seed a saved setting on an unselected card through the normal single-card API.
    seeded = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-batch-C",
            "setting": {"index": 2, "fan_percent": 55},
        },
    )
    assert seeded.status_code == 201, seeded.text
    assert [
        command["id"]
        for command in finish_dispatched(
            client,
            agent_id,
            secret,
            name="gpu-batch",
            gpus=gpus,
        )
    ] == [seeded.json()["id"]]

    applied = client.post(
        f"/api/agents/{agent_id}/gpu-oc/batch-apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuids": ["GPU-batch-A", "GPU-batch-B"],
            "setting": {"power_limit_w": 225},
        },
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["status"] == "queued"
    assert [
        (item["gpu_uuid"], item["gpu_index"])
        for item in applied.json()["results"]
    ] == [("GPU-batch-A", 0), ("GPU-batch-B", 1)]

    dispatched = heartbeat(
        client, agent_id, secret, name="gpu-batch", gpus=gpus
    ).json()["commands"]
    assert len(dispatched) == 2
    assert {
        (
            command["payload"]["mode"],
            command["payload"]["expected_gpu_uuid"],
            command["payload"]["settings"][0]["index"],
        )
        for command in dispatched
    } == {
        ("replace_one", "GPU-batch-A", 0),
        ("replace_one", "GPU-batch-B", 1),
    }
    for command in dispatched:
        result = client.post(
            f"/api/agent/commands/{command['id']}/result",
            headers={
                "X-Agent-ID": agent_id,
                "Authorization": f"Bearer {secret}",
            },
            json={
                "status": "succeeded",
                "result": gpu_oc_success_result(command),
                "error": None,
            },
        )
        assert result.status_code == 200, result.text

    desired = client.get(
        f"/api/agents/{agent_id}", headers=auth(token)
    ).json()["desired_gpu_oc"]
    by_uuid = {entry["gpu_uuid"]: entry["setting"] for entry in desired}
    assert by_uuid == {
        "GPU-batch-A": {"index": 0, "power_limit_w": 225.0},
        "GPU-batch-B": {"index": 1, "power_limit_w": 225.0},
        "GPU-batch-C": {"index": 2, "fan_percent": 55},
    }

    cleared = client.post(
        f"/api/agents/{agent_id}/gpu-oc/batch-clear",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuids": ["GPU-batch-A", "GPU-batch-B"],
        },
    )
    assert cleared.status_code == 201, cleared.text
    resets = heartbeat(
        client, agent_id, secret, name="gpu-batch", gpus=gpus
    ).json()["commands"]
    assert len(resets) == 2
    assert {
        (command["payload"]["mode"], command["payload"]["expected_gpu_uuid"])
        for command in resets
    } == {
        ("reset_one", "GPU-batch-A"),
        ("reset_one", "GPU-batch-B"),
    }
    for command in resets:
        result = client.post(
            f"/api/agent/commands/{command['id']}/result",
            headers={
                "X-Agent-ID": agent_id,
                "Authorization": f"Bearer {secret}",
            },
            json={
                "status": "succeeded",
                "result": gpu_oc_success_result(command),
                "error": None,
            },
        )
        assert result.status_code == 200, result.text
    remaining = client.get(
        f"/api/agents/{agent_id}", headers=auth(token)
    ).json()["desired_gpu_oc"]
    assert [entry["gpu_uuid"] for entry in remaining] == ["GPU-batch-C"]


def test_batch_gpu_oc_rejects_implicit_duplicate_unauthorized_and_partial_work(
    client: TestClient,
):
    admin = login(client)
    agent_id, secret = enroll(client, admin, name="gpu-batch-guard")
    gpus = [
        gpu(0, "GPU-guard-A", capabilities=["power_limit_w"]),
        gpu(1, "GPU-guard-B", capabilities=["fan_percent"]),
    ]
    assert heartbeat(
        client,
        agent_id,
        secret,
        name="gpu-batch-guard",
        gpus=gpus,
    ).status_code == 200
    endpoint = f"/api/agents/{agent_id}/gpu-oc/batch-apply"

    assert client.post(
        endpoint,
        headers=auth(admin),
        json={"vendor": "nvidia", "gpu_uuids": [], "setting": {"power_limit_w": 200}},
    ).status_code == 422
    assert client.post(
        endpoint,
        headers=auth(admin),
        json={
            "vendor": "nvidia",
            "gpu_uuids": ["GPU-guard-A", "GPU-guard-A"],
            "setting": {"power_limit_w": 200},
        },
    ).status_code == 422

    created = client.post(
        "/api/users",
        headers=auth(admin),
        json={
            "username": "batch-viewer",
            "password": ADMIN_PASSWORD,
            "role": "viewer",
        },
    )
    assert created.status_code == 201, created.text
    viewer = login(client, "batch-viewer")
    assert client.post(
        endpoint,
        headers=auth(viewer),
        json={
            "vendor": "nvidia",
            "gpu_uuids": ["GPU-guard-A"],
            "setting": {"power_limit_w": 200},
        },
    ).status_code == 403

    rejected = client.post(
        endpoint,
        headers=auth(admin),
        json={
            "vendor": "nvidia",
            "gpu_uuids": ["GPU-guard-A", "GPU-guard-B"],
            "setting": {"power_limit_w": 200},
        },
    )
    assert rejected.status_code == 409
    assert "power_limit_w" in rejected.json()["detail"]
    with client.app.state.database.connection() as db:
        count = db.execute(
            "SELECT COUNT(*) AS count FROM commands WHERE agent_id=?",
            (agent_id,),
        ).fetchone()["count"]
    assert count == 0
    assert client.get(
        f"/api/agents/{agent_id}", headers=auth(admin)
    ).json()["desired_gpu_oc"] == []


def test_batch_agent_update_returns_per_device_partial_results_and_reuses_guards(
    client: TestClient,
):
    admin = login(client)
    version = "0.6.46"
    uploaded = client.post(
        "/api/agent-releases/upload",
        headers={
            **auth(admin),
            "Content-Type": "application/zip",
        },
        params={
            "version": version,
            "platform": "windows",
            "architecture": "x86_64",
            "file_name": f"RigPilotAgent-{version}-Windows-x64-Portable.zip",
            "notes": "batch update test",
        },
        content=portable_zip(version),
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["id"]

    windows_id, windows_secret = enroll(
        client, admin, name="batch-win", os_family="windows"
    )
    linux_id, linux_secret = enroll(
        client, admin, name="batch-linux", os_family="linux"
    )
    for agent_id, secret, name, os_family in (
        (windows_id, windows_secret, "batch-win", "windows"),
        (linux_id, linux_secret, "batch-linux", "linux"),
    ):
        response = heartbeat(
            client,
            agent_id,
            secret,
            name=name,
            os_family=os_family,
            capabilities=["agent_update.v1"],
        )
        assert response.status_code == 200, response.text

    endpoint = f"/api/agent-releases/{release_id}/deploy-batch"
    assert client.post(
        endpoint,
        headers=auth(admin),
        json={"agent_ids": []},
    ).status_code == 422
    assert client.post(
        endpoint,
        headers=auth(admin),
        json={
            "agent_ids": [windows_id, windows_id],
        },
    ).status_code == 422

    created = client.post(
        "/api/users",
        headers=auth(admin),
        json={
            "username": "batch-operator",
            "password": ADMIN_PASSWORD,
            "role": "operator",
        },
    )
    assert created.status_code == 201, created.text
    operator = login(client, "batch-operator")
    assert client.post(
        endpoint,
        headers=auth(operator),
        json={"agent_ids": [windows_id]},
    ).status_code == 403

    response = client.post(
        endpoint,
        headers=auth(admin),
        json={
            "agent_ids": [windows_id, linux_id],
        },
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["accepted_count"] == 1
    assert document["rejected_count"] == 1
    assert document["accepted"][0]["agent_id"] == windows_id
    assert document["accepted"][0]["status"] == "queued"
    assert document["rejected"][0]["agent_id"] == linux_id
    assert document["rejected"][0]["status_code"] == 409
    assert "Windows" in document["rejected"][0]["detail"]
    with client.app.state.database.connection() as db:
        jobs = db.execute(
            "SELECT agent_id, target_version, status FROM agent_update_jobs"
        ).fetchall()
    assert [
        (row["agent_id"], row["target_version"], row["status"])
        for row in jobs
    ] == [(windows_id, version, "queued")]


def test_batch_flight_apply_is_explicit_partial_and_reuses_single_device_guards(
    client: TestClient,
):
    admin = login(client)
    sheet = create_generic_flight_sheet(client, admin)
    accepted_id, accepted_secret = enroll(client, admin, name="flight-batch-ok")
    blocked_id, blocked_secret = enroll(client, admin, name="flight-batch-blocked")
    for agent_id, secret, name in (
        (accepted_id, accepted_secret, "flight-batch-ok"),
        (blocked_id, blocked_secret, "flight-batch-blocked"),
    ):
        response = heartbeat(client, agent_id, secret, name=name)
        assert response.status_code == 200, response.text

    conflict = client.post(
        f"/api/agents/{blocked_id}/commands",
        headers=auth(admin),
        json={"action": "restart_mining", "payload": {}},
    )
    assert conflict.status_code == 201, conflict.text

    endpoint = f"/api/flight-sheets/{sheet['id']}/apply-batch"
    assert client.post(
        endpoint,
        headers=auth(admin),
        json={"agent_ids": []},
    ).status_code == 422
    assert client.post(
        endpoint,
        headers=auth(admin),
        json={"agent_ids": [accepted_id, accepted_id]},
    ).status_code == 422
    assert client.post(
        f"/api/flight-sheets/{uuid.uuid4()}/apply-batch",
        headers=auth(admin),
        json={"agent_ids": [accepted_id]},
    ).status_code == 404

    viewer_created = client.post(
        "/api/users",
        headers=auth(admin),
        json={
            "username": "flight-batch-viewer",
            "password": ADMIN_PASSWORD,
            "role": "viewer",
        },
    )
    assert viewer_created.status_code == 201, viewer_created.text
    viewer = login(client, "flight-batch-viewer")
    assert client.post(
        endpoint,
        headers=auth(viewer),
        json={"agent_ids": [accepted_id]},
    ).status_code == 403

    response = client.post(
        endpoint,
        headers=auth(admin),
        json={"agent_ids": [accepted_id, blocked_id]},
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["sheet_id"] == sheet["id"]
    assert document["accepted_count"] == 1
    assert document["rejected_count"] == 1
    assert document["accepted"][0]["agent_id"] == accepted_id
    assert document["accepted"][0]["status"] == "queued"
    assert document["rejected"][0]["agent_id"] == blocked_id
    assert document["rejected"][0]["status_code"] == 409
    assert "restart_mining" in document["rejected"][0]["detail"]

    with client.app.state.database.connection() as db:
        applies = db.execute(
            """SELECT agent_id, status FROM commands
               WHERE action='apply_flight_sheet' ORDER BY created_at, rowid"""
        ).fetchall()
        refreshes = db.execute(
            "SELECT agent_id, flight_sheet_id, status FROM configuration_refreshes"
        ).fetchall()
    assert [(row["agent_id"], row["status"]) for row in applies] == [
        (accepted_id, "queued")
    ]
    assert [
        (row["agent_id"], row["flight_sheet_id"], row["status"])
        for row in refreshes
    ] == [(accepted_id, sheet["id"], "queued")]
