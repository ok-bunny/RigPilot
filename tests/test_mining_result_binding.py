from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login

from server.config import Settings
from server.main import (
    CONTROLLER_MINING_AUTOSTART_MARKER,
    CONTROLLER_MINING_RESULT_BINDING_MARKER,
    CONTROLLER_FLIGHT_RUNTIME_MARKER,
    FLIGHT_RUNTIME_FINGERPRINT_FIELD,
    MINING_CONFIG_FINGERPRINT_CAPABILITY,
    MINING_RESULT_BINDING_CAPABILITY,
    _successful_mining_result_is_bound,
    create_app,
)


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="mining-result-binding-tests-secret-key-long-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def agent_auth(agent_id: str, secret: str) -> dict[str, str]:
    return {
        "X-Agent-ID": agent_id,
        "Authorization": f"Bearer {secret}",
    }


def login(client: TestClient) -> str:
    return _login(client, 'admin', 'correct-horse-battery-staple')


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
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": "0.6.56",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def heartbeat(
    client: TestClient,
    agent_id: str,
    secret: str,
    *,
    capabilities: list[str],
) -> dict:
    response = client.post(
        "/api/agent/heartbeat",
        headers=agent_auth(agent_id, secret),
        json={
            "hostname": f"{agent_id}.local",
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": "0.6.56",
            "ip_addresses": [],
            "metrics": {},
            "inventory": {
                "mining_status": {
                    "configured": True,
                    "state": "running",
                    "flight_sheet_id": "sheet-1",
                    "process_verified": True,
                }
            },
            "capabilities": capabilities,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_generic_sheet(
    client: TestClient,
    token: str,
    *,
    suffix: str,
) -> tuple[dict, dict]:
    wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": f"Binding wallet {suffix}",
            "coin": "XMR",
            "address": f"47{suffix}000000000000000000000000000000000000",
            "note": "",
        },
    )
    assert wallet.status_code == 201, wallet.text
    sheet = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": f"Binding flight {suffix}",
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
    return wallet.json(), sheet.json()


def test_success_result_validator_binds_every_mining_action():
    apply_payload = {
        "flight_sheet_id": "sheet-1",
        "primary": {"miner_type": "xmrig"},
    }
    apply_result = {
        "flight_sheet_id": "sheet-1",
        "managed_miner": {
            "managed": True,
            "flight_sheet_id": "sheet-1",
        },
    }
    assert _successful_mining_result_is_bound(
        "apply_flight_sheet", apply_payload, apply_result
    )
    assert not _successful_mining_result_is_bound(
        "apply_flight_sheet", apply_payload, {}
    )
    assert not _successful_mining_result_is_bound(
        "apply_flight_sheet",
        apply_payload,
        {
            **apply_result,
            "managed_miner": {
                "managed": True,
                "flight_sheet_id": "other-sheet",
            },
        },
    )

    control_payload = {
        CONTROLLER_MINING_RESULT_BINDING_MARKER: "sheet-1"
    }
    assert _successful_mining_result_is_bound(
        "restart_mining",
        control_payload,
        {
            "managed": True,
            "flight_sheet_id": "sheet-1",
            "restarted": True,
        },
    )
    stopped = {
        "managed": True,
        "flight_sheet_id": "sheet-1",
        "stopped": True,
        "gpu_oc_reset": {
            "state_cleared": False,
            "deferred_gpu_uuids": ["GPU-offline"],
        },
        "srb_gpu_oc_reset": {"cleared": True},
    }
    assert _successful_mining_result_is_bound(
        "stop_mining", control_payload, stopped
    )
    assert _successful_mining_result_is_bound(
        "remove_flight_sheet",
        control_payload,
        {**stopped, "configuration_cleared": True},
    )
    assert not _successful_mining_result_is_bound(
        "stop_mining",
        control_payload,
        {**stopped, "srb_gpu_oc_reset": {"cleared": False}},
    )
    assert not _successful_mining_result_is_bound(
        "remove_flight_sheet", control_payload, stopped
    )


def test_runtime_fingerprint_binds_both_independent_mining_slots():
    fingerprint = "a" * 64
    payload = {
        "flight_sheet_id": "sheet-dual",
        "primary": {"miner_type": "pearlminer"},
        "secondary": {"miner_type": "xmrig"},
        "secondary_mode": "independent",
        CONTROLLER_FLIGHT_RUNTIME_MARKER: {
            "version": 1,
            "runtime_fingerprint": fingerprint,
            "configuration_fingerprint": "b" * 64,
            "result_binding_required": True,
            "roles": ["primary", "secondary"],
        },
    }
    role = {
        "managed": True,
        "flight_sheet_id": "sheet-dual",
        FLIGHT_RUNTIME_FINGERPRINT_FIELD: fingerprint,
    }
    result = {
        "flight_sheet_id": "sheet-dual",
        FLIGHT_RUNTIME_FINGERPRINT_FIELD: fingerprint,
        "managed_miner": {
            **role,
            "roles": {
                "primary": dict(role),
                "secondary": dict(role),
            },
        },
    }
    assert _successful_mining_result_is_bound(
        "apply_flight_sheet", payload, result
    )
    missing_secondary = {
        **result,
        "managed_miner": {
            **result["managed_miner"],
            "roles": {"primary": dict(role)},
        },
    }
    assert not _successful_mining_result_is_bound(
        "apply_flight_sheet", payload, missing_secondary
    )
    wrong_secondary = {
        **result,
        "managed_miner": {
            **result["managed_miner"],
            "roles": {
                "primary": dict(role),
                "secondary": {
                    **role,
                    FLIGHT_RUNTIME_FINGERPRINT_FIELD: "c" * 64,
                },
            },
        },
    }
    assert not _successful_mining_result_is_bound(
        "apply_flight_sheet", payload, wrong_secondary
    )


def test_new_agent_rejects_unbound_restart_without_advancing_command(
    client: TestClient,
):
    token = login(client)
    agent_id, secret = enroll(client, token, "bound-restart")
    heartbeat(
        client,
        agent_id,
        secret,
        capabilities=[MINING_RESULT_BINDING_CAPABILITY],
    )
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    assert queued.json()["payload"] == {}
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        capabilities=[MINING_RESULT_BINDING_CAPABILITY],
    )["commands"]
    assert delivered == [
        {
            "id": queued.json()["id"],
            "action": "restart_mining",
            "payload": {},
        }
    ]
    # Attach the two controller-owned follow-up states to this exact command.
    # A forged success must not consume the autostart boot marker or enqueue
    # desired OC; a correctly bound retry should do both atomically.
    with client.app.state.db.connection() as db:
        stored = db.execute(
            "SELECT payload_json FROM commands WHERE id=?",
            (queued.json()["id"],),
        ).fetchone()
        payload = json.loads(stored["payload_json"])
        payload[CONTROLLER_MINING_AUTOSTART_MARKER] = {
            "boot_time": "2026-07-30T02:00:00+00:00",
            "previous_boot_time": "2026-07-30T01:00:00+00:00",
        }
        db.execute(
            "UPDATE commands SET payload_json=? WHERE id=?",
            (
                json.dumps(payload, separators=(",", ":")),
                queued.json()["id"],
            ),
        )
        db.execute(
            """UPDATE agents
               SET mining_autostart_last_boot_time=?,
                   metrics_json=?, desired_gpu_oc_json=?
               WHERE id=?""",
            (
                "2026-07-30T01:00:00+00:00",
                json.dumps(
                    {
                        "boot_time": "2026-07-30T02:00:00+00:00",
                        "mining_status": {
                            "configured": True,
                            "state": "running",
                            "flight_sheet_id": "sheet-1",
                            "process_verified": True,
                        },
                        "gpus": [
                            {
                                "index": 0,
                                "name": "NVIDIA Test GPU",
                                "uuid": "GPU-result-binding",
                                "vendor": "nvidia",
                                "oc_capabilities": ["power_limit_w"],
                                "power_min_limit_w": 100,
                                "power_max_limit_w": 300,
                            }
                        ],
                    },
                    separators=(",", ":"),
                ),
                json.dumps(
                    [
                        {
                            "gpu_uuid": "GPU-result-binding",
                            "name": "NVIDIA Test GPU · GPU 0",
                            "setting": {
                                "index": 0,
                                "power_limit_w": 200.0,
                            },
                        }
                    ],
                    separators=(",", ":"),
                ),
                agent_id,
            ),
        )

    rejected = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": "succeeded", "result": {}, "error": None},
    )
    assert rejected.status_code == 409, rejected.text
    command = client.get(
        f"/api/commands/{queued.json()['id']}",
        headers=auth(token),
    ).json()
    assert command["status"] == "dispatched"
    with client.app.state.db.connection() as db:
        assert db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0] == "2026-07-30T01:00:00+00:00"
        assert db.execute(
            """SELECT COUNT(*) FROM commands
               WHERE agent_id=? AND action='apply_gpu_profile'""",
            (agent_id,),
        ).fetchone()[0] == 0

    accepted = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={
            "status": "succeeded",
            "result": {
                "managed": True,
                "flight_sheet_id": "sheet-1",
                "restarted": True,
            },
            "error": None,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "succeeded"
    with client.app.state.db.connection() as db:
        assert db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0] == "2026-07-30T02:00:00+00:00"
        assert db.execute(
            """SELECT COUNT(*) FROM commands
               WHERE agent_id=? AND action='apply_gpu_profile'
                 AND status='queued'""",
            (agent_id,),
        ).fetchone()[0] == 1


def test_old_agent_keeps_legacy_result_compatibility(client: TestClient):
    token = login(client)
    agent_id, secret = enroll(client, token, "legacy-restart")
    heartbeat(client, agent_id, secret, capabilities=[])
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    heartbeat(client, agent_id, secret, capabilities=[])
    accepted = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": "succeeded", "result": {}, "error": None},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "succeeded"


def test_unbound_apply_cannot_complete_configuration_refresh(
    client: TestClient,
):
    token = login(client)
    agent_id, secret = enroll(client, token, "bound-apply")
    heartbeat(
        client,
        agent_id,
        secret,
        capabilities=[MINING_RESULT_BINDING_CAPABILITY],
    )
    wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "Binding wallet",
            "coin": "XMR",
            "address": "47bindingwallet000000000000000000000000000000000",
            "note": "",
        },
    )
    assert wallet.status_code == 201, wallet.text
    sheet = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "Binding flight",
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
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet.json()['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201, queued.text
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        capabilities=[MINING_RESULT_BINDING_CAPABILITY],
    )["commands"]
    assert [item["id"] for item in delivered] == [queued.json()["id"]]

    rejected = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": "succeeded", "result": {}, "error": None},
    )
    assert rejected.status_code == 409, rejected.text
    with client.app.state.db.connection() as db:
        refresh = db.execute(
            """SELECT status, completed_at FROM configuration_refreshes
               WHERE command_id=?""",
            (queued.json()["id"],),
        ).fetchone()
        assert refresh["status"] == "dispatched"
        assert refresh["completed_at"] is None

    accepted = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={
            "status": "succeeded",
            "result": {
                "flight_sheet_id": sheet.json()["id"],
                "managed_miner": None,
            },
            "error": None,
        },
    )
    assert accepted.status_code == 200, accepted.text
    with client.app.state.db.connection() as db:
        refresh = db.execute(
            """SELECT status, completed_at FROM configuration_refreshes
               WHERE command_id=?""",
            (queued.json()["id"],),
        ).fetchone()
        assert refresh["status"] == "succeeded"
        assert isinstance(refresh["completed_at"], str)


def test_fingerprint_capable_agent_must_echo_exact_runtime_document(
    client: TestClient,
):
    token = login(client)
    agent_id, secret = enroll(client, token, "fingerprint-apply")
    capabilities = [
        MINING_RESULT_BINDING_CAPABILITY,
        MINING_CONFIG_FINGERPRINT_CAPABILITY,
    ]
    heartbeat(
        client,
        agent_id,
        secret,
        capabilities=capabilities,
    )
    _, sheet = create_generic_sheet(
        client,
        token,
        suffix="fingerprint",
    )
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201, queued.text
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        capabilities=capabilities,
    )["commands"][0]
    fingerprint = delivered["payload"][FLIGHT_RUNTIME_FINGERPRINT_FIELD]
    assert len(fingerprint) == 64
    assert FLIGHT_RUNTIME_FINGERPRINT_FIELD not in queued.json()["payload"]

    rejected = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={
            "status": "succeeded",
            "result": {
                "flight_sheet_id": sheet["id"],
                "managed_miner": None,
            },
            "error": None,
        },
    )
    assert rejected.status_code == 409, rejected.text

    accepted = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={
            "status": "succeeded",
            "result": {
                "flight_sheet_id": sheet["id"],
                FLIGHT_RUNTIME_FINGERPRINT_FIELD: fingerprint,
                "managed_miner": None,
            },
            "error": None,
        },
    )
    assert accepted.status_code == 200, accepted.text


def test_legacy_agent_result_cannot_advance_modified_same_id_refresh(
    client: TestClient,
):
    token = login(client)
    agent_id, secret = enroll(client, token, "legacy-stale-apply")
    heartbeat(client, agent_id, secret, capabilities=[])
    wallet, sheet = create_generic_sheet(
        client,
        token,
        suffix="legacy-stale",
    )
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201, queued.text
    heartbeat(client, agent_id, secret, capabilities=[])

    changed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "48changed0000000000000000000000000000000000000",
            "note": wallet["note"],
        },
    )
    assert changed.status_code == 200, changed.text

    stale = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": "succeeded", "result": {}, "error": None},
    )
    assert stale.status_code == 200, stale.text
    command = client.get(
        f"/api/commands/{queued.json()['id']}",
        headers=auth(token),
    ).json()
    assert command["status"] == "succeeded"
    with client.app.state.db.connection() as db:
        refresh = db.execute(
            """SELECT status, command_id, completed_at
               FROM configuration_refreshes WHERE agent_id=?""",
            (agent_id,),
        ).fetchone()
        assert refresh["status"] == "pending"
        assert refresh["command_id"] is None
        assert refresh["completed_at"] is None


def test_running_command_lease_extends_only_to_hard_deadline(
    client: TestClient,
):
    token = login(client)
    agent_id, secret = enroll(client, token, "running-lease")
    heartbeat(client, agent_id, secret, capabilities=[])
    _, sheet = create_generic_sheet(
        client,
        token,
        suffix="running-lease",
    )
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201, queued.text
    heartbeat(client, agent_id, secret, capabilities=[])
    with client.app.state.db.connection() as db:
        before = db.execute(
            """SELECT expires_at, hard_expires_at FROM commands
               WHERE id=?""",
            (queued.json()["id"],),
        ).fetchone()
        assert before["expires_at"] < before["hard_expires_at"]

    running = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": "running", "result": {}, "error": None},
    )
    assert running.status_code == 200, running.text
    with client.app.state.db.connection() as db:
        renewed = db.execute(
            """SELECT expires_at, hard_expires_at FROM commands
               WHERE id=?""",
            (queued.json()["id"],),
        ).fetchone()
        assert renewed["expires_at"] == renewed["hard_expires_at"]

    repeated = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": "running", "result": {}, "error": None},
    )
    assert repeated.status_code == 200, repeated.text
    with client.app.state.db.connection() as db:
        final = db.execute(
            """SELECT expires_at, hard_expires_at FROM commands
               WHERE id=?""",
            (queued.json()["id"],),
        ).fetchone()
        assert final["expires_at"] == renewed["expires_at"]
        assert final["hard_expires_at"] == renewed["hard_expires_at"]


def test_temporary_gpu_inventory_failure_keeps_refresh_pending(
    client: TestClient,
):
    token = login(client)
    agent_id, secret = enroll(client, token, "retry-inventory")
    heartbeat(client, agent_id, secret, capabilities=[])
    _, sheet = create_generic_sheet(
        client,
        token,
        suffix="retry-inventory",
    )
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201, queued.text
    heartbeat(client, agent_id, secret, capabilities=[])

    failed = client.post(
        f"/api/agent/commands/{queued.json()['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={
            "status": "failed",
            "result": {},
            "error": "SRBMiner did not report any GPU devices",
        },
    )
    assert failed.status_code == 200, failed.text
    with client.app.state.db.connection() as db:
        refresh = db.execute(
            """SELECT status, command_id, completed_at, last_error
               FROM configuration_refreshes WHERE agent_id=?""",
            (agent_id,),
        ).fetchone()
        assert refresh["status"] == "pending"
        assert refresh["command_id"] is None
        assert refresh["completed_at"] is None
        assert "did not report any GPU" in refresh["last_error"]
