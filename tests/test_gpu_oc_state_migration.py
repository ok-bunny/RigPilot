from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import controller_settings_kwargs

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login",
        json={
            "username": "admin",
            "password": "correct-horse-battery-staple",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _enroll(client: TestClient, token: str) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=_auth(token),
        json={"label": "legacy-gpu-oc", "ttl_minutes": 30},
    )
    assert issued.status_code == 200, issued.text
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": "xy3",
            "hostname": "xy3.local",
            "os_family": "windows",
            "agent_version": "0.6.15",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def _agent_headers(agent_id: str, agent_secret: str) -> dict[str, str]:
    return {
        "X-Agent-ID": agent_id,
        "Authorization": f"Bearer {agent_secret}",
    }


def _insert_merge_history(
    client: TestClient,
    agent_id: str,
    devices: list[tuple[str, dict[str, object]]],
) -> list[str]:
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    command_ids: list[str] = []
    with client.app.state.db.connection() as db:
        creator_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        for position, (gpu_uuid, setting) in enumerate(devices):
            command_id = str(uuid.uuid4())
            command_ids.append(command_id)
            created_at = (now + timedelta(seconds=position)).isoformat(
                timespec="microseconds"
            )
            payload = {
                "gpu_profile_id": f"device-oc-history-{position}",
                "name": f"legacy GPU {position}",
                "vendor": "nvidia",
                "mode": "merge_one",
                "expected_gpu_uuid": gpu_uuid,
                "settings": [setting],
            }
            db.execute(
                """INSERT INTO commands
                   (id, agent_id, action, payload_json, status, created_by,
                    created_at, expires_at, finished_at, result_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    command_id,
                    agent_id,
                    "apply_gpu_profile",
                    json.dumps(payload, separators=(",", ":")),
                    "succeeded",
                    creator_id,
                    created_at,
                    (now + timedelta(hours=1)).isoformat(timespec="seconds"),
                    created_at,
                    "{}",
                ),
            )
    return command_ids


def _heartbeat(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
    *,
    settings: list[dict[str, object]],
    gpus: list[dict[str, object]],
):
    return client.post(
        "/api/agent/heartbeat",
        headers=_agent_headers(agent_id, agent_secret),
        json={
            "hostname": "xy3.local",
            "os_family": "windows",
            "agent_version": "0.6.17",
            "metrics": {"gpus": gpus},
            "inventory": {
                "mining_status": {
                    "gpu_oc_profiles": [
                        {
                            "source": "nvml",
                            "state": "applied_record",
                            "state_version": 1,
                            "identity_complete": False,
                            "settings": settings,
                        }
                    ]
                }
            },
        },
    )


def _migration_result(
    devices: list[tuple[str, dict[str, object]]],
    current_index: dict[str, int],
    history_ids: list[str],
) -> dict[str, object]:
    return {
        "migrated": True,
        "source_state_version": 1,
        "target_state_version": 2,
        "nvml_writes": 0,
        "history_command_ids": history_ids,
        "devices": [
            {
                "gpu_uuid": gpu_uuid,
                "index": current_index[gpu_uuid],
                "setting": {
                    key: value for key, value in setting.items() if key != "index"
                },
            }
            for gpu_uuid, setting in devices
        ],
    }


def _post_result(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
    command_id: str,
    result: dict[str, object],
    *,
    status: str = "succeeded",
):
    return client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=_agent_headers(agent_id, agent_secret),
        json={
            "status": status,
            "result": result,
            "error": "migration failed" if status == "failed" else None,
        },
    )


def test_four_card_v1_history_migrates_by_uuid_and_backfills_desired(
    client: TestClient,
):
    token = _login(client)
    agent_id, agent_secret = _enroll(client, token)
    devices = [
        (f"GPU-{letter}", {"index": index, "power_limit_w": 100 + 10 * index})
        for index, letter in enumerate("ABCD")
    ]
    history_ids = _insert_merge_history(client, agent_id, devices)
    current_index = {"GPU-A": 3, "GPU-B": 2, "GPU-C": 1, "GPU-D": 0}
    gpus = [
        {
            "uuid": gpu_uuid,
            "index": current_index[gpu_uuid],
            "name": f"NVIDIA {gpu_uuid}",
            "vendor": "nvidia",
        }
        for gpu_uuid, _setting in devices
    ]

    heartbeat = _heartbeat(
        client,
        agent_id,
        agent_secret,
        settings=[setting for _gpu_uuid, setting in devices],
        gpus=gpus,
    )

    assert heartbeat.status_code == 200, heartbeat.text
    commands = heartbeat.json()["commands"]
    assert len(commands) == 1
    command = commands[0]
    assert command["action"] == "migrate_gpu_oc_state.v1"
    assert command["payload"]["history_command_ids"] == history_ids
    assert command["payload"]["devices"] == [
        {"gpu_uuid": gpu_uuid, "setting": setting}
        for gpu_uuid, setting in devices
    ]

    completed = _post_result(
        client,
        agent_id,
        agent_secret,
        command["id"],
        _migration_result(devices, current_index, history_ids),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "succeeded"

    agent = client.get(f"/api/agents/{agent_id}", headers=_auth(token)).json()
    desired = agent["desired_gpu_oc"]
    assert [item["setting"]["index"] for item in desired] == [0, 1, 2, 3]
    desired_by_uuid = {item["gpu_uuid"]: item["setting"] for item in desired}
    assert desired_by_uuid == {
        gpu_uuid: {
            "index": current_index[gpu_uuid],
            "power_limit_w": setting["power_limit_w"],
        }
        for gpu_uuid, setting in devices
    }
    with client.app.state.db.connection() as db:
        marker = json.loads(
            db.execute(
                "SELECT gpu_oc_migration_marker_json FROM agents WHERE id=?",
                (agent_id,),
            ).fetchone()["gpu_oc_migration_marker_json"]
        )
    assert marker["status"] == "succeeded"
    assert marker["command_id"] == command["id"]


def test_failed_reconstruction_is_audited_only_once_per_state(client: TestClient):
    token = _login(client)
    agent_id, agent_secret = _enroll(client, token)
    settings = [{"index": 0, "power_limit_w": 100}]
    gpus = [
        {
            "uuid": "GPU-A",
            "index": 0,
            "name": "NVIDIA A",
            "vendor": "nvidia",
        }
    ]

    first = _heartbeat(
        client, agent_id, agent_secret, settings=settings, gpus=gpus
    )
    second = _heartbeat(
        client, agent_id, agent_secret, settings=settings, gpus=gpus
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["commands"] == second.json()["commands"] == []
    with client.app.state.db.connection() as db:
        audit_count = db.execute(
            """SELECT COUNT(*) AS count FROM audit_logs
               WHERE target_id=? AND action='legacy_gpu_oc_migration_skipped'""",
            (agent_id,),
        ).fetchone()["count"]
        marker = json.loads(
            db.execute(
                "SELECT gpu_oc_migration_marker_json FROM agents WHERE id=?",
                (agent_id,),
            ).fetchone()["gpu_oc_migration_marker_json"]
        )
    assert audit_count == 1
    assert marker["status"] == "skipped"


def test_tampered_migration_success_cannot_write_desired(client: TestClient):
    token = _login(client)
    agent_id, agent_secret = _enroll(client, token)
    devices = [("GPU-A", {"index": 0, "power_limit_w": 100})]
    history_ids = _insert_merge_history(client, agent_id, devices)
    gpus = [
        {
            "uuid": "GPU-A",
            "index": 0,
            "name": "NVIDIA A",
            "vendor": "nvidia",
        }
    ]
    heartbeat = _heartbeat(
        client,
        agent_id,
        agent_secret,
        settings=[devices[0][1]],
        gpus=gpus,
    )
    command = heartbeat.json()["commands"][0]
    tampered = _migration_result(devices, {"GPU-A": 0}, history_ids)
    tampered["devices"][0]["setting"]["power_limit_w"] = 101

    rejected = _post_result(
        client, agent_id, agent_secret, command["id"], tampered
    )

    assert rejected.status_code == 409
    with client.app.state.db.connection() as db:
        agent = db.execute(
            """SELECT desired_gpu_oc_json, gpu_oc_migration_marker_json
               FROM agents WHERE id=?""",
            (agent_id,),
        ).fetchone()
        stored_command = db.execute(
            "SELECT status FROM commands WHERE id=?", (command["id"],)
        ).fetchone()
    assert json.loads(agent["desired_gpu_oc_json"]) == []
    assert json.loads(agent["gpu_oc_migration_marker_json"])["status"] == "queued"
    assert stored_command["status"] == "dispatched"


def test_deleted_migrated_desired_is_not_reimported_from_stale_v1_report(
    client: TestClient,
):
    token = _login(client)
    agent_id, agent_secret = _enroll(client, token)
    devices = [("GPU-A", {"index": 0, "power_limit_w": 100})]
    history_ids = _insert_merge_history(client, agent_id, devices)
    gpus = [
        {
            "uuid": "GPU-A",
            "index": 0,
            "name": "NVIDIA A",
            "vendor": "nvidia",
        }
    ]
    first = _heartbeat(
        client,
        agent_id,
        agent_secret,
        settings=[devices[0][1]],
        gpus=gpus,
    )
    migration = first.json()["commands"][0]
    completed = _post_result(
        client,
        agent_id,
        agent_secret,
        migration["id"],
        _migration_result(devices, {"GPU-A": 0}, history_ids),
    )
    assert completed.status_code == 200, completed.text

    deleted = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/0",
        params={"gpu_uuid": "GPU-A"},
        headers=_auth(token),
    )
    assert deleted.status_code == 200, deleted.text
    reset_command = deleted.json()["reset_command"]
    dispatched_reset = _heartbeat(
        client,
        agent_id,
        agent_secret,
        settings=[devices[0][1]],
        gpus=gpus,
    )
    assert [item["id"] for item in dispatched_reset.json()["commands"]] == [
        reset_command["id"]
    ]
    reset_result = _post_result(
        client,
        agent_id,
        agent_secret,
        reset_command["id"],
        {
            "vendor": reset_command["payload"]["vendor"],
            "gpu_profile_id": reset_command["payload"]["gpu_profile_id"],
            "operations": [],
            "reset": [
                {
                    "index": setting["index"],
                    "reset": [key for key in setting if key != "index"],
                }
                for setting in reset_command["payload"]["settings"]
            ],
        },
    )
    assert reset_result.status_code == 200, reset_result.text

    stale_v1 = _heartbeat(
        client,
        agent_id,
        agent_secret,
        settings=[devices[0][1]],
        gpus=gpus,
    )

    assert stale_v1.status_code == 200
    assert stale_v1.json()["commands"] == []
    agent = client.get(f"/api/agents/{agent_id}", headers=_auth(token)).json()
    assert agent["desired_gpu_oc"] == []
    with client.app.state.db.connection() as db:
        migration_count = db.execute(
            """SELECT COUNT(*) AS count FROM commands
               WHERE agent_id=? AND action='migrate_gpu_oc_state.v1'""",
            (agent_id,),
        ).fetchone()["count"]
        marker = json.loads(
            db.execute(
                "SELECT gpu_oc_migration_marker_json FROM agents WHERE id=?",
                (agent_id,),
            ).fetchone()["gpu_oc_migration_marker_json"]
        )
    assert migration_count == 1
    assert marker["status"] == "succeeded"


def test_migration_waits_for_complete_uuid_topology_and_blocks_mutations(
    client: TestClient,
):
    token = _login(client)
    agent_id, agent_secret = _enroll(client, token)
    devices = [("GPU-A", {"index": 0, "power_limit_w": 100})]
    _insert_merge_history(client, agent_id, devices)

    waiting = _heartbeat(
        client,
        agent_id,
        agent_secret,
        settings=[devices[0][1]],
        gpus=[],
    )
    assert waiting.status_code == 200
    assert waiting.json()["commands"] == []
    with client.app.state.db.connection() as db:
        marker_before = db.execute(
            "SELECT gpu_oc_migration_marker_json FROM agents WHERE id=?",
            (agent_id,),
        ).fetchone()["gpu_oc_migration_marker_json"]
    assert json.loads(marker_before) == {}

    gpus = [
        {
            "uuid": "GPU-A",
            "index": 0,
            "name": "NVIDIA A",
            "vendor": "nvidia",
            "oc_capabilities": ["power_limit_w"],
        }
    ]
    ready = _heartbeat(
        client,
        agent_id,
        agent_secret,
        settings=[devices[0][1]],
        gpus=gpus,
    )
    assert ready.status_code == 200
    assert ready.json()["commands"][0]["action"] == "migrate_gpu_oc_state.v1"

    direct_oc = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=_auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-A",
            "setting": {"index": 0, "power_limit_w": 100},
        },
    )
    stop = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=_auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    reboot = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=_auth(token),
        json={"action": "reboot", "payload": {}},
    )
    assert direct_oc.status_code == stop.status_code == reboot.status_code == 409
    assert "migration" in direct_oc.json()["detail"].lower()


@pytest.mark.parametrize("history_ids", [[{}], [[]]])
def test_malformed_persisted_history_ids_fail_closed(history_ids):
    from server.main import _migration_desired_gpu_oc
    assert _migration_desired_gpu_oc({"schema_version": 1, "history_command_ids": history_ids, "devices": []}, {}, {}) is None


@pytest.mark.parametrize("gpu_uuid", [[], {}])
def test_malformed_migration_result_uuid_fails_closed(gpu_uuid):
    from server.main import _migration_desired_gpu_oc
    history = ["11111111-1111-4111-8111-111111111111"]
    payload = {"schema_version": 1, "history_command_ids": history,
               "devices": [{"gpu_uuid": "GPU-valid", "setting": {"index": 0, "power_limit_w": 200}}]}
    result = {"migrated": True, "source_state_version": 1, "target_state_version": 2, "nvml_writes": 0,
              "history_command_ids": history, "devices": [{"gpu_uuid": gpu_uuid, "index": 0, "setting": {"power_limit_w": 200}}]}
    assert _migration_desired_gpu_oc(payload, result, {}) is None
