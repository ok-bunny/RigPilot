from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login

from server.config import Settings
from server.main import (
    _mining_residue_cleanup_result_is_bound,
    _mining_residue_scan_result_is_valid,
    create_app,
)


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="mining-control-tests-secret-key-that-is-long-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


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


def heartbeat(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
    *,
    version: str,
    capabilities: list[str] | None = None,
):
    return client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
        json={
            "hostname": f"{agent_id}.local",
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": version,
            "ip_addresses": [],
            "metrics": {},
            "inventory": {
                "mining_status": {
                    "configured": True,
                    "state": "running",
                    "flight_sheet_id": "sheet-1",
                }
            },
            "capabilities": capabilities or [],
        },
    )


def test_mining_control_enforces_role_payload_online_state_and_version(
    client: TestClient,
):
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

    offline_id, _ = enroll(
        client, admin_token, name="offline-agent", version="0.5.2"
    )
    offline = client.post(
        f"/api/agents/{offline_id}/commands",
        headers=auth(admin_token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert offline.status_code == 409
    assert "online" in offline.json()["detail"].lower()

    old_id, old_secret = enroll(
        client, admin_token, name="old-agent", version="0.5.1"
    )
    assert heartbeat(client, old_id, old_secret, version="0.5.1").status_code == 200
    old_version = client.post(
        f"/api/agents/{old_id}/commands",
        headers=auth(admin_token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert old_version.status_code == 409
    assert "0.5.2" in old_version.json()["detail"]

    agent_id, agent_secret = enroll(
        client, admin_token, name="current-agent", version="0.5.2+portable"
    )
    assert (
        heartbeat(client, agent_id, agent_secret, version="0.5.2+portable").status_code
        == 200
    )
    forbidden = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(viewer_token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert forbidden.status_code == 403
    extra_payload = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(operator_token),
        json={"action": "stop_mining", "payload": {"force": True}},
    )
    assert extra_payload.status_code == 422
    assert "payload must be empty" in extra_payload.json()["detail"]

    stopped = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(operator_token),
        json={"action": "stop_mining", "payload": {}},
    )
    conflict = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert stopped.status_code == 201, stopped.text
    assert conflict.status_code == 409
    assert "already in progress" in conflict.json()["detail"]
    assert stopped.json()["payload"] == {}
    dispatched = heartbeat(
        client, agent_id, agent_secret, version="0.5.2+portable"
    )
    assert dispatched.status_code == 200, dispatched.text
    assert [item["action"] for item in dispatched.json()["commands"]] == [
        "stop_mining"
    ]
    assert all(item["payload"] == {} for item in dispatched.json()["commands"])
    command_id = stopped.json()["id"]
    completed = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
        json={"status": "succeeded", "result": {"stopped": True}, "error": None},
    )
    assert completed.status_code == 200, completed.text
    restarted = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert restarted.status_code == 201, restarted.text
    restart_dispatch = heartbeat(
        client, agent_id, agent_secret, version="0.5.2+portable"
    )
    assert [item["action"] for item in restart_dispatch.json()["commands"]] == [
        "restart_mining"
    ]


def _residue_scan_result(fingerprint: str = "a" * 64) -> dict:
    return {
        "schema_version": 1,
        "scan_fingerprint": fingerprint,
        "cleanup_available": True,
        "clearable_count": 1,
        "ambiguous_count": 0,
        "findings": [
            {
                "role": "legacy",
                "status": "orphan_record",
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet-1",
                "record_pid": 123,
                "verified_pid": None,
                "process_verified": False,
                "clearable": True,
                "reason": "recorded process is definitely absent",
            }
        ],
    }


def _selective_residue_scan_result(
    fingerprint: str = "d" * 64,
    target_fingerprint: str = "e" * 64,
) -> dict:
    result = _residue_scan_result(fingerprint)
    result["schema_version"] = 2
    result["findings"][0]["target_fingerprint"] = target_fingerprint
    return result


def _explicit_candidate_residue_scan_result(
    fingerprint: str = "1" * 64,
    target_fingerprint: str = "2" * 64,
    candidate_fingerprint: str = "3" * 64,
) -> dict:
    result = _selective_residue_scan_result(fingerprint, target_fingerprint)
    result["schema_version"] = 3
    result["findings"][0].update(
        {
            "status": "ambiguous",
            "verified_pid": None,
            "process_verified": False,
            "clearable": False,
            "reason": "same-name process is not an exact replacement",
            "manual_candidates": [
                {
                    "pid": 456,
                    "candidate_fingerprint": candidate_fingerprint,
                }
            ],
        }
    )
    result["clearable_count"] = 0
    result["ambiguous_count"] = 1
    return result


def test_residue_scan_rejects_status_identity_contradictions():
    ambiguous = _residue_scan_result()
    ambiguous["findings"][0].update(
        {
            "status": "ambiguous",
            "clearable": False,
            "reason": "same-name process is not an exact replacement",
        }
    )
    ambiguous["clearable_count"] = 0
    ambiguous["ambiguous_count"] = 1
    ambiguous["cleanup_available"] = False
    assert _mining_residue_scan_result_is_valid(ambiguous)

    contradictory = deepcopy(ambiguous)
    contradictory["findings"][0]["clearable"] = True
    contradictory["clearable_count"] = 1
    assert not _mining_residue_scan_result_is_valid(contradictory)

    contradictory = deepcopy(ambiguous)
    contradictory["findings"][0].update(
        {
            "process_verified": True,
            "verified_pid": 456,
        }
    )
    assert not _mining_residue_scan_result_is_valid(contradictory)

    exact_replacement = _residue_scan_result()
    exact_replacement["findings"][0].update(
        {
            "status": "exact_replacement",
            "verified_pid": 456,
            "process_verified": True,
            "reason": "one exact replacement was verified",
        }
    )
    assert _mining_residue_scan_result_is_valid(exact_replacement)

    contradictory = deepcopy(exact_replacement)
    contradictory["findings"][0].update(
        {
            "verified_pid": None,
            "process_verified": False,
        }
    )
    assert not _mining_residue_scan_result_is_valid(contradictory)

    no_record = deepcopy(exact_replacement)
    no_record["findings"][0].update(
        {
            "status": "no_record",
            "miner_type": None,
            "flight_sheet_id": None,
            "record_pid": 123,
            "verified_pid": None,
            "process_verified": False,
            "clearable": False,
            "reason": "no managed miner process record",
        }
    )
    no_record["clearable_count"] = 0
    assert not _mining_residue_scan_result_is_valid(no_record)


def test_residue_scan_rejects_duplicate_role_and_target_bindings():
    duplicate_roles = _residue_scan_result()
    duplicate_roles["findings"].append(deepcopy(duplicate_roles["findings"][0]))
    duplicate_roles["clearable_count"] = 2
    assert not _mining_residue_scan_result_is_valid(duplicate_roles)

    duplicate_targets = _selective_residue_scan_result()
    second = deepcopy(duplicate_targets["findings"][0])
    second["role"] = "primary"
    duplicate_targets["findings"].append(second)
    duplicate_targets["clearable_count"] = 2
    assert not _mining_residue_scan_result_is_valid(duplicate_targets)


def test_explicit_candidate_residue_scan_contract_is_strict():
    scan = _explicit_candidate_residue_scan_result()
    assert _mining_residue_scan_result_is_valid(scan)

    malformed = deepcopy(scan)
    malformed["findings"][0]["manual_candidates"][0]["candidate_fingerprint"] = (
        "A" * 64
    )
    assert not _mining_residue_scan_result_is_valid(malformed)

    malformed = deepcopy(scan)
    malformed["findings"][0]["manual_candidates"][0]["pid"] = True
    assert not _mining_residue_scan_result_is_valid(malformed)

    malformed = deepcopy(scan)
    malformed["findings"][0]["manual_candidates"][0]["executable"] = "miner"
    assert not _mining_residue_scan_result_is_valid(malformed)

    malformed = deepcopy(scan)
    malformed["findings"][0]["status"] = "orphan_record"
    malformed["findings"][0]["clearable"] = True
    malformed["clearable_count"] = 1
    malformed["ambiguous_count"] = 0
    assert not _mining_residue_scan_result_is_valid(malformed)

    malformed = deepcopy(scan)
    malformed["findings"][0]["manual_candidates"].append(
        {"pid": 789, "candidate_fingerprint": "4" * 64}
    )
    assert not _mining_residue_scan_result_is_valid(malformed)


def test_residue_receipt_contract_accepts_clean_and_exact_replacement_scans():
    clean = {
        "schema_version": 1,
        "scan_fingerprint": "b" * 64,
        "cleanup_available": True,
        "clearable_count": 0,
        "ambiguous_count": 0,
        "findings": [
            {
                "role": role,
                "status": "no_record",
                "miner_type": None,
                "flight_sheet_id": None,
                "record_pid": None,
                "verified_pid": None,
                "process_verified": False,
                "clearable": False,
                "reason": "no managed miner process record",
            }
            for role in ("legacy", "primary", "secondary")
        ],
    }
    assert _mining_residue_scan_result_is_valid(clean)

    replacement = _residue_scan_result("c" * 64)
    replacement["findings"][0].update(
        {
            "status": "exact_replacement",
            "verified_pid": 456,
            "process_verified": True,
            "reason": "unique exact replacement process was verified",
        }
    )
    assert _mining_residue_scan_result_is_valid(replacement)
    cleanup = {
        **replacement,
        "blocked": False,
        "changed": True,
        "processes_stopped": 1,
        "records_removed": 1,
        "results": [
            {
                "role": "legacy",
                "status": "verified_process_stopped",
                "process_stopped": True,
                "record_removed": True,
                "reason": None,
            }
        ],
        "gpu_oc_reset": {
            "srbminer": {"required": True, "cleared": True},
            "nvml": {"state_cleared": True},
        },
    }
    assert _mining_residue_cleanup_result_is_bound(
        {"scan_fingerprint": "c" * 64},
        cleanup,
    )
    assert not _mining_residue_cleanup_result_is_bound(
        {"scan_fingerprint": "c" * 64},
        {**cleanup, "srb_gpu_oc_reset": {"cleared": True}},
    )


def test_selective_residue_receipt_binds_targets_and_independent_oc_choices():
    scan = _selective_residue_scan_result()
    payload = {
        "scan_fingerprint": "d" * 64,
        "targets": [
            {
                "role": "legacy",
                "target_fingerprint": "e" * 64,
            }
        ],
        "reset_srbminer_oc": False,
        "reset_nvml_oc": True,
    }
    cleanup = {
        **scan,
        "blocked": False,
        "changed": True,
        "processes_stopped": 0,
        "records_removed": 1,
        "results": [
            {
                "role": "legacy",
                "status": "orphan_record_removed",
                "process_stopped": False,
                "record_removed": True,
                "reason": None,
            }
        ],
        "selected_targets": payload["targets"],
        "reset_options": {"srbminer": False, "nvml": True},
        "gpu_oc_reset": {
            "srbminer": {
                "required": False,
                "cleared": False,
                "status": "not_selected",
            },
            "nvml": {"state_cleared": True},
        },
    }
    assert _mining_residue_scan_result_is_valid(scan)
    assert _mining_residue_cleanup_result_is_bound(payload, cleanup)
    assert not _mining_residue_cleanup_result_is_bound(
        {**payload, "targets": []},
        cleanup,
    )
    assert not _mining_residue_cleanup_result_is_bound(
        payload,
        {
            **cleanup,
            "reset_options": {"srbminer": True, "nvml": True},
        },
    )


def test_explicit_candidate_cleanup_receipt_is_scan_and_target_bound():
    scan = _explicit_candidate_residue_scan_result()
    payload = {
        "scan_fingerprint": scan["scan_fingerprint"],
        "targets": [
            {
                "role": "legacy",
                "target_fingerprint": "2" * 64,
                "candidate_fingerprint": "3" * 64,
            }
        ],
        "reset_srbminer_oc": True,
        "reset_nvml_oc": True,
    }
    cleanup = {
        **scan,
        "blocked": False,
        "changed": True,
        "processes_stopped": 1,
        "records_removed": 1,
        "results": [
            {
                "role": "legacy",
                "status": "explicit_candidate_stopped",
                "process_stopped": True,
                "record_removed": True,
                "reason": None,
            }
        ],
        "selected_targets": payload["targets"],
        "reset_options": {"srbminer": True, "nvml": True},
        "gpu_oc_reset": {
            "srbminer": {"required": True, "cleared": True},
            "nvml": {"state_cleared": True},
        },
    }
    assert _mining_residue_cleanup_result_is_bound(payload, cleanup)

    mismatched = deepcopy(payload)
    mismatched["targets"][0]["candidate_fingerprint"] = "4" * 64
    assert not _mining_residue_cleanup_result_is_bound(mismatched, cleanup)

    malformed = deepcopy(cleanup)
    malformed["results"][0]["status"] = "verified_process_stopped"
    assert not _mining_residue_cleanup_result_is_bound(payload, malformed)

    malformed = deepcopy(cleanup)
    malformed["results"][0]["process_stopped"] = False
    malformed["processes_stopped"] = 0
    assert not _mining_residue_cleanup_result_is_bound(payload, malformed)

    malformed = deepcopy(cleanup)
    malformed["findings"][0]["manual_candidates"] = []
    assert not _mining_residue_cleanup_result_is_bound(payload, malformed)


def test_residue_cleanup_rejects_status_and_scan_finding_mismatches():
    scan = _residue_scan_result()
    payload = {"scan_fingerprint": scan["scan_fingerprint"]}
    valid = {
        **scan,
        "blocked": False,
        "changed": True,
        "processes_stopped": 0,
        "records_removed": 1,
        "results": [
            {
                "role": "legacy",
                "status": "orphan_record_removed",
                "process_stopped": False,
                "record_removed": True,
                "reason": None,
            }
        ],
        "gpu_oc_reset": {
            "srbminer": {"required": False, "cleared": False},
            "nvml": {"state_cleared": True},
        },
    }
    assert _mining_residue_cleanup_result_is_bound(payload, valid)

    contradictory = deepcopy(valid)
    contradictory["results"][0].update(
        {
            "status": "verified_process_stopped",
            "process_stopped": True,
        }
    )
    contradictory["processes_stopped"] = 1
    assert not _mining_residue_cleanup_result_is_bound(payload, contradictory)

    contradictory = deepcopy(valid)
    contradictory["results"][0]["process_stopped"] = True
    contradictory["processes_stopped"] = 1
    assert not _mining_residue_cleanup_result_is_bound(payload, contradictory)

    verified_scan = deepcopy(scan)
    verified_scan["findings"][0].update(
        {
            "status": "verified_process",
            "verified_pid": verified_scan["findings"][0]["record_pid"],
            "process_verified": True,
            "reason": "recorded PID identity is verified",
        }
    )
    verified_cleanup = {
        **verified_scan,
        "blocked": False,
        "changed": True,
        "processes_stopped": 0,
        "records_removed": 1,
        "results": [
            {
                "role": "legacy",
                "status": "verified_process_stopped",
                "process_stopped": False,
                "record_removed": True,
                "reason": None,
            }
        ],
        "gpu_oc_reset": {
            "srbminer": {"required": False, "cleared": False},
            "nvml": {"state_cleared": True},
        },
    }
    assert not _mining_residue_cleanup_result_is_bound(
        payload,
        verified_cleanup,
    )

    contradictory = deepcopy(valid)
    contradictory["results"][0]["reason"] = "cleanup reported success with an error"
    assert not _mining_residue_cleanup_result_is_bound(payload, contradictory)

    contradictory = deepcopy(valid)
    contradictory["gpu_oc_reset"]["nvml"]["state_cleared"] = "yes"
    assert not _mining_residue_cleanup_result_is_bound(payload, contradictory)


def test_selective_residue_cleanup_rejects_target_not_bound_to_finding():
    scan = _selective_residue_scan_result()
    payload = {
        "scan_fingerprint": scan["scan_fingerprint"],
        "targets": [
            {
                "role": "legacy",
                "target_fingerprint": "f" * 64,
            }
        ],
        "reset_srbminer_oc": False,
        "reset_nvml_oc": False,
    }
    cleanup = {
        **scan,
        "blocked": False,
        "changed": True,
        "processes_stopped": 0,
        "records_removed": 1,
        "results": [
            {
                "role": "legacy",
                "status": "orphan_record_removed",
                "process_stopped": False,
                "record_removed": True,
                "reason": None,
            }
        ],
        "selected_targets": payload["targets"],
        "reset_options": {"srbminer": False, "nvml": False},
        "gpu_oc_reset": {
            "srbminer": {
                "required": False,
                "cleared": False,
                "status": "not_selected",
            },
            "nvml": {"status": "not_selected", "state_cleared": False},
        },
    }
    assert not _mining_residue_cleanup_result_is_bound(payload, cleanup)


def test_residue_repair_requires_capability_and_serializes_scan_cleanup(
    client: TestClient,
):
    token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(
        client, token, name="residue-repair", version="0.6.49"
    )
    heartbeat(client, agent_id, secret, version="0.6.49")
    unsupported = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "scan_mining_residue.v1", "payload": {}},
    )
    assert unsupported.status_code == 409
    assert "managed_mining.residue_repair.v1" in unsupported.json()["detail"]

    heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.49",
        capabilities=["managed_mining.residue_repair.v1"],
    )
    invalid_cleanup = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={
            "action": "cleanup_mining_residue.v1",
            "payload": {"scan_fingerprint": "A" * 64},
        },
    )
    assert invalid_cleanup.status_code == 422

    scan = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "scan_mining_residue.v1", "payload": {}},
    )
    assert scan.status_code == 201, scan.text
    duplicate = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "scan_mining_residue.v1", "payload": {}},
    )
    assert duplicate.status_code == 409
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.49",
        capabilities=["managed_mining.residue_repair.v1"],
    ).json()["commands"]
    assert delivered == [
        {
            "id": scan.json()["id"],
            "action": "scan_mining_residue.v1",
            "payload": {},
        }
    ]

    malformed = client.post(
        f"/api/agent/commands/{scan.json()['id']}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "status": "succeeded",
            "result": {**_residue_scan_result(), "clearable_count": 2},
            "error": None,
        },
    )
    assert malformed.status_code == 409
    for field, value in [
        ("schema_version", []), ("schema_version", {}),
        ("schema_version", True), ("schema_version", 1.0),
        *[(field, value) for field in ("role", "status", "miner_type") for value in ([], {})],
        *[(field, int("1" * 64)) for field in
          ("scan_fingerprint", "target_fingerprint", "candidate_fingerprint")],
    ]:
        invalid = (_explicit_candidate_residue_scan_result()
                   if field in {"target_fingerprint", "candidate_fingerprint"}
                   else _residue_scan_result())
        target = invalid if field in {"schema_version", "scan_fingerprint"} else invalid["findings"][0]
        if field == "candidate_fingerprint":
            target = target["manual_candidates"][0]
        target[field] = value
        rejected = client.post(f"/api/agent/commands/{scan.json()['id']}/result",
            headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
            json={"status": "succeeded", "result": invalid})
        assert rejected.status_code == 409, (field, value, rejected.text)
        with client.app.state.db.connection() as db:
            assert tuple(db.execute("SELECT status,result_json FROM commands WHERE id=?", (scan.json()["id"],)).fetchone()) == ("dispatched", None)
    completed = client.post(
        f"/api/agent/commands/{scan.json()['id']}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "status": "succeeded",
            "result": _residue_scan_result(),
            "error": None,
        },
    )
    assert completed.status_code == 200, completed.text

    cleanup = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={
            "action": "cleanup_mining_residue.v1",
            "payload": {"scan_fingerprint": "a" * 64},
        },
    )
    assert cleanup.status_code == 201, cleanup.text
    cleanup_delivery = heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.49",
        capabilities=["managed_mining.residue_repair.v1"],
    ).json()["commands"]
    assert cleanup_delivery[0]["payload"] == {"scan_fingerprint": "a" * 64}
    cleanup_result = {
        **_residue_scan_result(),
        "blocked": False,
        "changed": True,
        "processes_stopped": 0,
        "records_removed": 1,
        "results": [
            {
                "role": "legacy",
                "status": "orphan_record_removed",
                "process_stopped": False,
                "record_removed": True,
                "reason": None,
            }
        ],
        "gpu_oc_reset": {
            "srbminer": {"required": False, "cleared": False},
            "nvml": {"state_cleared": True},
        },
    }
    cleaned = client.post(
        f"/api/agent/commands/{cleanup.json()['id']}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={"status": "succeeded", "result": cleanup_result, "error": None},
    )
    assert cleaned.status_code == 200, cleaned.text


def test_selective_residue_cleanup_requires_selective_agent_capability(
    client: TestClient,
):
    token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(
        client, token, name="selective-residue", version="0.6.50"
    )
    old_capability = ["managed_mining.residue_repair.v1"]
    heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.50",
        capabilities=old_capability,
    )
    payload = {
        "scan_fingerprint": "d" * 64,
        "targets": [
            {
                "role": "legacy",
                "target_fingerprint": "e" * 64,
            }
        ],
        "reset_srbminer_oc": False,
        "reset_nvml_oc": True,
    }
    malformed_payload = deepcopy(payload)
    malformed_payload["targets"][0]["candidate_fingerprint"] = "A" * 64
    malformed = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={
            "action": "cleanup_mining_residue.v1",
            "payload": malformed_payload,
        },
    )
    assert malformed.status_code == 422
    unsupported = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "cleanup_mining_residue.v1", "payload": payload},
    )
    assert unsupported.status_code == 409
    assert "managed_mining.residue_repair.selective.v1" in unsupported.text

    capabilities = [
        *old_capability,
        "managed_mining.residue_repair.selective.v1",
    ]
    heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.50",
        capabilities=capabilities,
    )
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "cleanup_mining_residue.v1", "payload": payload},
    )
    assert queued.status_code == 201, queued.text
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.50",
        capabilities=capabilities,
    ).json()["commands"]
    assert delivered[0]["payload"] == payload


def test_explicit_candidate_cleanup_requires_dedicated_agent_capability(
    client: TestClient,
):
    token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(
        client, token, name="explicit-residue", version="0.6.63"
    )
    base_capabilities = [
        "managed_mining.residue_repair.v1",
        "managed_mining.residue_repair.selective.v1",
    ]
    heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.63",
        capabilities=base_capabilities,
    )
    payload = {
        "scan_fingerprint": "1" * 64,
        "targets": [
            {
                "role": "legacy",
                "target_fingerprint": "2" * 64,
                "candidate_fingerprint": "3" * 64,
            }
        ],
        "reset_srbminer_oc": True,
        "reset_nvml_oc": True,
    }
    unsupported = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "cleanup_mining_residue.v1", "payload": payload},
    )
    assert unsupported.status_code == 409
    assert "managed_mining.residue_repair.explicit_candidate.v1" in unsupported.text

    capabilities = [
        *base_capabilities,
        "managed_mining.residue_repair.explicit_candidate.v1",
    ]
    heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.63",
        capabilities=capabilities,
    )
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "cleanup_mining_residue.v1", "payload": payload},
    )
    assert queued.status_code == 201, queued.text
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.63",
        capabilities=capabilities,
    ).json()["commands"]
    assert delivered[0]["payload"] == payload


def test_explicit_candidate_cleanup_accepts_only_latest_matching_schema_v3_scan(
    client: TestClient,
):
    token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(
        client, token, name="explicit-residue-scan-fallback", version="0.6.64"
    )
    capabilities = [
        "managed_mining.residue_repair.v1",
        "managed_mining.residue_repair.selective.v1",
    ]
    heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.64",
        capabilities=capabilities,
    )

    def complete_scan(result: dict) -> None:
        queued = client.post(
            f"/api/agents/{agent_id}/commands",
            headers=auth(token),
            json={"action": "scan_mining_residue.v1", "payload": {}},
        )
        assert queued.status_code == 201, queued.text
        delivered = heartbeat(
            client,
            agent_id,
            secret,
            version="0.6.64",
            capabilities=capabilities,
        ).json()["commands"]
        assert [command["id"] for command in delivered] == [queued.json()["id"]]
        completed = client.post(
            f"/api/agent/commands/{queued.json()['id']}/result",
            headers={
                "X-Agent-ID": agent_id,
                "Authorization": f"Bearer {secret}",
            },
            json={"status": "succeeded", "result": result, "error": None},
        )
        assert completed.status_code == 200, completed.text

    stale_scan = _explicit_candidate_residue_scan_result(
        "1" * 64,
        "2" * 64,
        "3" * 64,
    )
    complete_scan(stale_scan)
    latest_scan = _explicit_candidate_residue_scan_result(
        "4" * 64,
        "5" * 64,
        "6" * 64,
    )
    complete_scan(latest_scan)
    payload = {
        "scan_fingerprint": latest_scan["scan_fingerprint"],
        "targets": [
            {
                "role": "legacy",
                "target_fingerprint": "5" * 64,
                "candidate_fingerprint": "6" * 64,
            }
        ],
        "reset_srbminer_oc": True,
        "reset_nvml_oc": True,
    }

    stale_payload = deepcopy(payload)
    stale_payload.update(
        scan_fingerprint=stale_scan["scan_fingerprint"],
        targets=[
            {
                "role": "legacy",
                "target_fingerprint": "2" * 64,
                "candidate_fingerprint": "3" * 64,
            }
        ],
    )
    mismatched_target = deepcopy(payload)
    mismatched_target["targets"][0]["target_fingerprint"] = "7" * 64
    mismatched_candidate = deepcopy(payload)
    mismatched_candidate["targets"][0]["candidate_fingerprint"] = "8" * 64
    mismatched_role = deepcopy(payload)
    mismatched_role["targets"][0]["role"] = "primary"
    for rejected_payload in (
        stale_payload,
        mismatched_target,
        mismatched_candidate,
        mismatched_role,
    ):
        rejected = client.post(
            f"/api/agents/{agent_id}/commands",
            headers=auth(token),
            json={
                "action": "cleanup_mining_residue.v1",
                "payload": rejected_payload,
            },
        )
        assert rejected.status_code == 409, rejected.text
        assert "managed_mining.residue_repair.explicit_candidate.v1" in rejected.text

    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "cleanup_mining_residue.v1", "payload": payload},
    )
    assert queued.status_code == 201, queued.text
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.64",
        capabilities=capabilities,
    ).json()["commands"]
    assert delivered[0]["payload"] == payload


def test_residue_repair_expires_stale_conflicts_before_queueing(client: TestClient):
    token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(
        client, token, name="residue-expired-conflict", version="0.6.49"
    )
    heartbeat(
        client,
        agent_id,
        secret,
        version="0.6.49",
        capabilities=["managed_mining.residue_repair.v1"],
    )
    stop = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert stop.status_code == 201, stop.text
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE commands SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (stop.json()["id"],),
        )

    scan = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "scan_mining_residue.v1", "payload": {}},
    )
    assert scan.status_code == 201, scan.text
    history = client.get(
        "/api/commands", headers=auth(token), params={"agent_id": agent_id}
    ).json()
    assert next(item for item in history if item["id"] == stop.json()["id"])[
        "status"
    ] == "expired"


def test_power_commands_are_online_only_and_cannot_overtake_mining_transitions(
    client: TestClient,
):
    token = login(client, "admin", "correct-horse-battery-staple")
    offline_id, _offline_secret = enroll(
        client, token, name="offline-power", version="0.6.50"
    )
    offline = client.post(
        f"/api/agents/{offline_id}/commands",
        headers=auth(token),
        json={"action": "reboot", "payload": {}},
    )
    assert offline.status_code == 409
    assert "online" in offline.json()["detail"].lower()

    agent_id, secret = enroll(
        client, token, name="power-conflict", version="0.6.50"
    )
    assert heartbeat(client, agent_id, secret, version="0.6.50").status_code == 200
    with client.app.state.db.connection() as db:
        user_id = db.execute(
            "SELECT id FROM users WHERE username='admin'"
        ).fetchone()["id"]
        db.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at,expires_at)
               VALUES('queued-apply-before-reboot',?,'apply_flight_sheet','{}',
                      'queued',?,'2026-07-30T00:00:00+00:00',
                      '9999-12-31T23:59:59+00:00')""",
            (agent_id, user_id),
        )

    reboot = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "reboot", "payload": {}},
    )
    service = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_service", "payload": {"service": "RigPilotAgent"}},
    )
    assert reboot.status_code == service.status_code == 409
    assert "apply_flight_sheet" in reboot.json()["detail"]

    # Stop is the one deliberate exception: it atomically cancels a queued
    # apply, then becomes the only command eligible for dispatch.
    stop = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert stop.status_code == 201, stop.text
    delivered = heartbeat(client, agent_id, secret, version="0.6.50").json()[
        "commands"
    ]
    assert [item["action"] for item in delivered] == ["stop_mining"]


def test_mining_control_cannot_overtake_queued_power_command(client: TestClient):
    token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(
        client, token, name="mining-after-power", version="0.6.50"
    )
    assert heartbeat(client, agent_id, secret, version="0.6.50").status_code == 200
    reboot = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "reboot", "payload": {}},
    )
    assert reboot.status_code == 201, reboot.text
    stop = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert stop.status_code == 409
    assert "reboot" in stop.json()["detail"]
