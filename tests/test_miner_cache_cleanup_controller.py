"""Controller admission and result binding for non-process miner-cache cleanup."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth

from server.config import Settings
from server.main import create_app

CAP = "managed_mining.cache_cleanup.v1"
SCAN = "scan_miner_cache.v1"
CLEANUP = "cleanup_miner_cache.v1"
FINGERPRINT = "a" * 64
CANDIDATE = "b" * 64


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "controller.db",
        secret_key="synthetic-cache-controller-secret-long-enough",
        admin_username="admin",
        admin_password="synthetic-cache-admin-password-long-enough",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as value:
        yield value


def admin_token(client: TestClient) -> str:
    response = client.post("/api/auth/login", json={
        "username": "admin", "password": "synthetic-cache-admin-password-long-enough",
    })
    assert response.status_code == 200, response.text
    return response.json()["token"]


def enroll(client: TestClient, token: str, *, name: str = "synthetic-cache-rig") -> tuple[str, str]:
    enrollment = client.post("/api/enrollment-tokens", headers=auth(token), json={
        "label": name, "ttl_minutes": 30,
    })
    assert enrollment.status_code == 200, enrollment.text
    response = client.post("/api/agent/enroll", json={
        "enrollment_token": enrollment.json()["token"], "name": name,
        "hostname": name, "os_family": "linux", "platform": "Ubuntu 24.04",
        "agent_version": "0.8.24",
    })
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def agent_auth(agent_id: str, secret: str) -> dict[str, str]:
    return {"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"}


def heartbeat(client: TestClient, agent_id: str, secret: str, capabilities: list[str]):
    return client.post("/api/agent/heartbeat", headers=agent_auth(agent_id, secret), json={
        "hostname": "synthetic-cache-rig", "os_family": "linux",
        "platform": "Ubuntu 24.04", "agent_version": "0.8.24",
        "capabilities": capabilities,
        "metrics": {"disks": [{"mountpoint": "/", "total_bytes": 8_000_000,
                              "used_bytes": 7_000_000, "free_bytes": 1_000_000, "percent": 87.5}]},
        "inventory": {"mining_status": {"configured": True, "state": "running"}},
    })


def queue(client: TestClient, token: str, agent_id: str, action: str, payload: dict):
    return client.post(f"/api/agents/{agent_id}/commands", headers=auth(token), json={
        "action": action, "payload": payload,
    })


def result(client: TestClient, agent_id: str, secret: str, command_id: str,
           status: str, body: dict):
    return client.post(f"/api/agent/commands/{command_id}/result",
                       headers=agent_auth(agent_id, secret),
                       json={"status": status, "result": body})


def scan_result(*, can_cleanup: bool = True) -> dict:
    candidates = [{"candidate_id": CANDIDATE, "package_id": "old-xmrig",
                   "name": "XMRig", "version": "6.20", "kind": "package_cache", "logical_bytes": 4_000_000,
                   "file_count": 3}] if can_cleanup else []
    return {"schema_version": 1,
            "scan_fingerprint": FINGERPRINT if can_cleanup else None,
            "can_cleanup": can_cleanup,
            "reason": None if can_cleanup else "unknown_reference",
            "disk": {"total_bytes": 8_000_000, "free_bytes": 1_000_000,
                     "scope": "package_cache_filesystem"},
            "candidate_logical_bytes": 4_000_000 if can_cleanup else 0,
            "candidates": candidates,
            "protected": [{"entry_id": "current-miner", "package_id": "current-srb",
                           "version": "3.6", "kind": "package_cache", "reason": "current_primary"}],
            "unclassified": []}


def cleanup_result(*, state: str = "succeeded", delta: int = 3_000_000) -> dict:
    deleted = [{"candidate_id": CANDIDATE, "package_id": "old-xmrig",
                "version": "6.20", "kind": "package_cache", "logical_bytes": 4_000_000}] if state in {"succeeded", "partial"} else []
    return {"schema_version": 1, "state": state,
            "requested_scan_fingerprint": FINGERPRINT,
            "before_scan_fingerprint": FINGERPRINT if state != "stale_scan" else "c" * 64,
            "deleted": deleted, "skipped": [],
            "failed": [{"candidate_id": CANDIDATE, "kind": "package_cache", "reason": "deletion_failed"}] if state == "failed" else [],
            "logical_bytes_removed": 4_000_000 if deleted else 0,
            "disk": {"scope": "package_cache_filesystem", "free_bytes_before": 1_000_000,
                     "free_bytes_after": 1_000_000 + delta,
                     "free_bytes_delta": delta},
            "reason": None if state == "succeeded" else state}


def test_cache_scan_cleanup_is_capability_gated_and_exact_scan_bound(client: TestClient):
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin)
    assert heartbeat(client, agent_id, secret, []).status_code == 200
    unsupported = queue(client, admin, agent_id, SCAN, {})
    assert unsupported.status_code == 409 and CAP in unsupported.text
    assert queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": FINGERPRINT}).status_code == 409
    assert heartbeat(client, agent_id, secret, [CAP]).status_code == 200
    assert queue(client, admin, agent_id, SCAN, {"path": "/"}).status_code == 422
    assert queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": "A" * 64}).status_code == 422
    scan = queue(client, admin, agent_id, SCAN, {})
    assert scan.status_code == 201, scan.text
    scan_id = scan.json()["id"]
    assert queue(client, admin, agent_id, SCAN, {}).status_code == 409
    sent = heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
    assert any(item["id"] == scan_id and item["action"] == SCAN for item in sent)
    assert result(client, agent_id, secret, scan_id, "succeeded", scan_result()).status_code == 200
    wrong = queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": "d" * 64})
    assert wrong.status_code == 409
    cleanup = queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": FINGERPRINT})
    assert cleanup.status_code == 201, cleanup.text
    cleanup_id = cleanup.json()["id"]
    assert queue(client, admin, agent_id, "stop_mining", {}).status_code == 409
    assert queue(client, admin, agent_id, SCAN, {}).status_code == 409
    assert heartbeat(client, agent_id, secret, [CAP]).status_code == 200
    bad_delta = cleanup_result(delta=3_000_000)
    bad_delta["disk"]["free_bytes_delta"] = 4_000_000
    assert result(client, agent_id, secret, cleanup_id, "succeeded", bad_delta).status_code == 409
    for field, value in [("schema_version", True), ("schema_version", 1.0),
                         ("candidate_id", []), ("candidate_id", {}),
                         ("logical_bytes", 4_000_000.0)]:
        malformed = cleanup_result()
        target = malformed if field == "schema_version" else malformed["deleted"][0]
        target[field] = value
        assert result(client, agent_id, secret, cleanup_id, "succeeded", malformed).status_code == 409
        with client.app.state.db.connection() as db:
            assert tuple(db.execute("SELECT status,result_json FROM commands WHERE id=?", (cleanup_id,)).fetchone()) == ("dispatched", None)
    good = cleanup_result()
    assert result(client, agent_id, secret, cleanup_id, "succeeded", good).status_code == 200
    shown = client.get(f"/api/commands/{cleanup_id}", headers=auth(admin)).json()
    assert shown["result"]["disk"]["free_bytes_delta"] == 3_000_000
    assert shown["result"]["logical_bytes_removed"] == 4_000_000
    audit = client.get(f"/api/audit?agent_id={agent_id}", headers=auth(admin)).json()
    assert any(row["action"] == "command_queued" and row["details"].get("scan_command_id") == scan_id for row in audit)


def test_cache_scan_result_must_be_complete_and_unsafe_scan_cannot_cleanup(client: TestClient):
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-unsafe-cache")
    heartbeat(client, agent_id, secret, [CAP])
    scan = queue(client, admin, agent_id, SCAN, {})
    scan_id = scan.json()["id"]
    heartbeat(client, agent_id, secret, [CAP])
    malformed = scan_result()
    malformed["candidate_logical_bytes"] = 1
    assert result(client, agent_id, secret, scan_id, "succeeded", malformed).status_code == 409
    for value in ([], {}, True, 1.0):
        malformed = {**scan_result(), "schema_version": value}
        assert result(client, agent_id, secret, scan_id, "succeeded", malformed).status_code == 409
        with client.app.state.db.connection() as db:
            assert tuple(db.execute("SELECT status,result_json FROM commands WHERE id=?", (scan_id,)).fetchone()) == ("dispatched", None)
    unsafe = scan_result(can_cleanup=False)
    assert result(client, agent_id, secret, scan_id, "succeeded", unsafe).status_code == 200
    assert queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": FINGERPRINT}).status_code == 409
    empty = queue(client, admin, agent_id, SCAN, {})
    assert empty.status_code == 201
    heartbeat(client, agent_id, secret, [CAP])
    complete_empty = scan_result()
    complete_empty["candidates"] = []
    complete_empty["candidate_logical_bytes"] = 0
    assert result(client, agent_id, secret, empty.json()["id"], "succeeded", complete_empty).status_code == 200
    no_op = queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": FINGERPRINT})
    assert no_op.status_code == 409 and "no safely deletable" in no_op.text


def test_queued_cache_work_is_not_sent_after_capability_disappears(client: TestClient):
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-cap-loss")
    heartbeat(client, agent_id, secret, [CAP])
    scan = queue(client, admin, agent_id, SCAN, {})
    assert scan.status_code == 201
    commands = heartbeat(client, agent_id, secret, []).json()["commands"]
    assert all(item["id"] != scan.json()["id"] for item in commands)
    shown = client.get(f"/api/commands/{scan.json()['id']}", headers=auth(admin)).json()
    assert shown["status"] == "failed" and CAP in shown["error"]


def test_cache_cleanup_partial_failure_uses_measured_delta_and_never_claims_success(client: TestClient):
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-partial-cache")
    heartbeat(client, agent_id, secret, [CAP])
    scan = queue(client, admin, agent_id, SCAN, {})
    heartbeat(client, agent_id, secret, [CAP])
    inventory = scan_result()
    second_id = "e" * 64
    inventory["candidates"].append({
        "candidate_id": second_id, "package_id": "old-srb", "name": "SRBMiner",
        "version": "2.0", "kind": "package_cache", "logical_bytes": 2_000_000, "file_count": 2,
    })
    inventory["candidate_logical_bytes"] = 6_000_000
    assert result(client, agent_id, secret, scan.json()["id"], "succeeded", inventory).status_code == 200
    cleanup = queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": FINGERPRINT})
    assert cleanup.status_code == 201, cleanup.text
    heartbeat(client, agent_id, secret, [CAP])
    partial = cleanup_result(state="partial", delta=-100_000)
    partial["failed"] = [{"candidate_id": second_id, "kind": "package_cache", "reason": "deletion_failed"}]
    partial["disk"] = {"scope": "package_cache_filesystem", "free_bytes_before": 1_000_000,
                       "free_bytes_after": 900_000,
                       "free_bytes_delta": -100_000}
    response = client.post(f"/api/agent/commands/{cleanup.json()['id']}/result",
                           headers=agent_auth(agent_id, secret),
                           json={"status": "failed", "result": partial,
                                 "error": "ENOENT /private/synthetic/cache/path"})
    assert response.status_code == 200, response.text
    shown = client.get(f"/api/commands/{cleanup.json()['id']}", headers=auth(admin)).json()
    assert shown["status"] == "failed"
    assert shown["error"] == "partial"
    assert shown["result"]["state"] == "partial"
    assert shown["result"]["logical_bytes_removed"] == 4_000_000
    assert shown["result"]["disk"]["free_bytes_delta"] == -100_000


def test_cache_scan_is_agent_and_role_scoped_and_history_clear_preserves_active_binding(client: TestClient):
    admin = admin_token(client)
    user = client.post("/api/users", headers=auth(admin), json={
        "username": "cache-viewer", "password": "synthetic-viewer-password-long-enough", "role": "viewer",
    })
    assert user.status_code == 201, user.text
    viewer = client.post("/api/auth/login", json={
        "username": "cache-viewer", "password": "synthetic-viewer-password-long-enough",
    }).json()["token"]
    first_id, first_secret = enroll(client, admin, name="synthetic-cache-one")
    second_id, second_secret = enroll(client, admin, name="synthetic-cache-two")
    heartbeat(client, first_id, first_secret, [CAP])
    heartbeat(client, second_id, second_secret, [CAP])
    assert queue(client, viewer, first_id, SCAN, {}).status_code == 403
    scan = queue(client, admin, first_id, SCAN, {})
    heartbeat(client, first_id, first_secret, [CAP])
    assert result(client, first_id, first_secret, scan.json()["id"], "succeeded", scan_result()).status_code == 200
    assert queue(client, admin, second_id, CLEANUP, {"scan_fingerprint": FINGERPRINT}).status_code == 409
    cleanup = queue(client, admin, first_id, CLEANUP, {"scan_fingerprint": FINGERPRINT})
    assert cleanup.status_code == 201
    heartbeat(client, first_id, first_secret, [CAP])
    assert result(client, second_id, second_secret, cleanup.json()["id"], "succeeded", cleanup_result()).status_code == 404
    assert result(client, first_id, first_secret, cleanup.json()["id"], "succeeded", cleanup_result()).status_code == 200


def test_hiveos_session_payload_with_unknown_version_is_bound_by_kind_and_id(client: TestClient):
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-session-cache")
    heartbeat(client, agent_id, secret, [CAP])
    scan = queue(client, admin, agent_id, SCAN, {})
    heartbeat(client, agent_id, secret, [CAP])
    inventory = scan_result()
    session_id = "f" * 64
    inventory["candidates"].append({
        "candidate_id": session_id, "kind": "hiveos_session_payload",
        "package_id": None, "name": "HiveOS session payload", "version": None,
        "logical_bytes": 2_000_000, "file_count": 4,
    })
    inventory["candidate_logical_bytes"] = 6_000_000
    inventory["disk"]["filesystems"] = [
        {"scope": "package_cache", "filesystem_key": "1" * 16, "total_bytes": 8_000_000, "free_bytes": 1_000_000},
        {"scope": "hiveos_session_payload", "filesystem_key": "2" * 16, "total_bytes": 10_000_000, "free_bytes": 2_000_000},
    ]
    assert result(client, agent_id, secret, scan.json()["id"], "succeeded", inventory).status_code == 200
    cleanup = queue(client, admin, agent_id, CLEANUP, {"scan_fingerprint": FINGERPRINT})
    assert cleanup.status_code == 201
    heartbeat(client, agent_id, secret, [CAP])
    incomplete_success = cleanup_result()
    assert result(client, agent_id, secret, cleanup.json()["id"], "succeeded", incomplete_success).status_code == 409
    completed = cleanup_result()
    completed["deleted"].append({
        "candidate_id": session_id, "kind": "hiveos_session_payload",
        "package_id": None, "version": None, "logical_bytes": 2_000_000,
    })
    completed["logical_bytes_removed"] = 6_000_000
    completed["disk"]["filesystems"] = [
        {"scope": "package_cache", "filesystem_key": "1" * 16, "free_bytes_before": 1_000_000,
         "free_bytes_after": 4_000_000, "free_bytes_delta": 3_000_000},
        {"scope": "hiveos_session_payload", "filesystem_key": "2" * 16, "free_bytes_before": 2_000_000,
         "free_bytes_after": 3_000_000, "free_bytes_delta": 1_000_000},
    ]
    wrong_kind = deepcopy(completed)
    wrong_kind["deleted"][1]["kind"] = "package_cache"
    assert result(client, agent_id, secret, cleanup.json()["id"], "succeeded", wrong_kind).status_code == 409
    missing_volume = deepcopy(completed)
    missing_volume["disk"].pop("filesystems")
    assert result(client, agent_id, secret, cleanup.json()["id"], "succeeded", missing_volume).status_code == 409
    wrong_volume = deepcopy(completed)
    wrong_volume["disk"]["filesystems"][1]["filesystem_key"] = "3" * 16
    assert result(client, agent_id, secret, cleanup.json()["id"], "succeeded", wrong_volume).status_code == 409
    assert result(client, agent_id, secret, cleanup.json()["id"], "succeeded", completed).status_code == 200
    assert client.get(f"/api/commands/{cleanup.json()['id']}", headers=auth(admin)).json()["result"]["deleted"][1]["version"] is None


def test_dispatched_cache_command_is_not_resent_to_downgraded_agent(client: TestClient):
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-cache-downgrade")
    heartbeat(client, agent_id, secret, [CAP])
    scan = queue(client, admin, agent_id, SCAN, {})
    assert scan.status_code == 201
    first_poll = heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
    assert any(item["id"] == scan.json()["id"] for item in first_poll)
    old_agent_poll = heartbeat(client, agent_id, secret, []).json()["commands"]
    assert all(item["id"] != scan.json()["id"] for item in old_agent_poll)
    shown = client.get(f"/api/commands/{scan.json()['id']}", headers=auth(admin)).json()
    assert shown["status"] == "failed" and CAP in shown["error"]


def test_cache_scan_failure_does_not_store_raw_filesystem_details(client: TestClient):
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-cache-error")
    heartbeat(client, agent_id, secret, [CAP])
    scan = queue(client, admin, agent_id, SCAN, {})
    heartbeat(client, agent_id, secret, [CAP])
    endpoint = f"/api/agent/commands/{scan.json()['id']}/result"
    leaked = client.post(endpoint, headers=agent_auth(agent_id, secret), json={
        "status": "failed", "result": {"path": "/private/synthetic/cache"},
        "error": "ENOENT /private/synthetic/cache",
    })
    assert leaked.status_code == 409
    safe = client.post(endpoint, headers=agent_auth(agent_id, secret), json={
        "status": "failed", "result": {},
        "error": "ENOENT /private/synthetic/cache",
    })
    assert safe.status_code == 200
    shown = client.get(f"/api/commands/{scan.json()['id']}", headers=auth(admin)).json()
    assert shown["status"] == "failed"
    assert shown["result"] == {}
    assert shown["error"] == "miner_cache_operation_failed"


def test_real_agent_executor_roundtrips_scan_and_cleanup_through_controller(
    client: TestClient, tmp_path: Path, monkeypatch,
):
    """A disposable Agent-owned cache exercises the actual two-phase wire format."""
    from agent import miner_cache_cleanup as agent_cache
    from agent.commands import Command, CommandExecutor
    from agent.tests.test_miner_cache_cleanup import fixture as agent_fixture
    from agent.tests.test_miner_cache_cleanup import package as agent_package

    monkeypatch.setattr(agent_cache, "_process_refs", lambda _root, _names, _deadline: (set(), False))
    config, root = agent_fixture(tmp_path)
    old_path, _ = agent_package(root, version="0.9")
    current_path, _ = agent_package(root, version="1.0")
    from agent.tests.test_miner_cache_cleanup import flight as agent_flight
    agent_flight(config, current_path)
    executor = CommandExecutor(config)

    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-real-wire")
    heartbeat(client, agent_id, secret, [CAP])
    scan = queue(client, admin, agent_id, SCAN, {})
    assert scan.status_code == 201
    sent = heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
    scan_wire = next(item for item in sent if item["id"] == scan.json()["id"])
    scan_outcome = executor.execute(Command(scan_wire["id"], scan_wire["action"], scan_wire["payload"]))
    assert scan_outcome.status == "succeeded"
    scan_ack = client.post(
        f"/api/agent/commands/{scan_wire['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": scan_outcome.status, "result": scan_outcome.result,
              "error": scan_outcome.error},
    )
    assert scan_ack.status_code == 200, scan_ack.text
    assert [item["version"] for item in scan_outcome.result["candidates"]] == ["0.9"]
    assert current_path.exists()

    cleanup = queue(client, admin, agent_id, CLEANUP, {
        "scan_fingerprint": scan_outcome.result["scan_fingerprint"],
    })
    assert cleanup.status_code == 201, cleanup.text
    sent = heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
    cleanup_wire = next(item for item in sent if item["id"] == cleanup.json()["id"])
    cleanup_outcome = executor.execute(Command(cleanup_wire["id"], cleanup_wire["action"], cleanup_wire["payload"]))
    assert cleanup_outcome.status == "succeeded"
    cleanup_ack = client.post(
        f"/api/agent/commands/{cleanup_wire['id']}/result",
        headers=agent_auth(agent_id, secret),
        json={"status": cleanup_outcome.status, "result": cleanup_outcome.result,
              "error": cleanup_outcome.error},
    )
    assert cleanup_ack.status_code == 200, cleanup_ack.text
    assert not old_path.exists() and current_path.exists()
    saved = client.get(f"/api/commands/{cleanup_wire['id']}", headers=auth(admin)).json()
    assert saved["status"] == "succeeded"
    assert saved["result"]["deleted"][0]["version"] == "0.9"


def test_real_agent_stale_reference_refuses_cleanup_and_controller_records_failure(
    client: TestClient, tmp_path: Path, monkeypatch,
):
    from agent import miner_cache_cleanup as agent_cache
    from agent.commands import Command, CommandExecutor
    from agent.tests.test_miner_cache_cleanup import fixture as agent_fixture
    from agent.tests.test_miner_cache_cleanup import flight as agent_flight
    from agent.tests.test_miner_cache_cleanup import package as agent_package

    monkeypatch.setattr(agent_cache, "_process_refs", lambda _root, _names, _deadline: (set(), False))
    config, root = agent_fixture(tmp_path)
    candidate, _ = agent_package(root, version="0.9")
    executor = CommandExecutor(config)
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-stale-wire")
    heartbeat(client, agent_id, secret, [CAP])
    queued_scan = queue(client, admin, agent_id, SCAN, {})
    scan_wire = next(item for item in heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
                     if item["id"] == queued_scan.json()["id"])
    scanned = executor.execute(Command(scan_wire["id"], scan_wire["action"], scan_wire["payload"]))
    assert scanned.status == "succeeded"
    assert result(client, agent_id, secret, scan_wire["id"], "succeeded", scanned.result).status_code == 200
    queued_cleanup = queue(client, admin, agent_id, CLEANUP, {
        "scan_fingerprint": scanned.result["scan_fingerprint"],
    })
    assert queued_cleanup.status_code == 201
    agent_flight(config, candidate)
    cleanup_wire = next(item for item in heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
                        if item["id"] == queued_cleanup.json()["id"])
    outcome = executor.execute(Command(cleanup_wire["id"], cleanup_wire["action"], cleanup_wire["payload"]))
    assert outcome.status == "failed" and outcome.result["state"] == "stale_scan"
    dishonest = deepcopy(outcome.result)
    dishonest["deleted"] = [{"candidate_id": scanned.result["candidates"][0]["candidate_id"],
                             "kind": "package_cache", "package_id": "xmrig-official",
                             "version": "0.9", "logical_bytes": len(b"miner")}]
    dishonest["logical_bytes_removed"] = len(b"miner")
    assert result(client, agent_id, secret, cleanup_wire["id"], "failed", dishonest).status_code == 409
    acknowledged = client.post(f"/api/agent/commands/{cleanup_wire['id']}/result",
                               headers=agent_auth(agent_id, secret),
                               json={"status": outcome.status, "result": outcome.result,
                                     "error": outcome.error})
    assert acknowledged.status_code == 200, acknowledged.text
    assert candidate.exists()
    saved = client.get(f"/api/commands/{cleanup_wire['id']}", headers=auth(admin)).json()
    assert saved["status"] == "failed" and saved["result"]["state"] == "stale_scan"


def test_real_agent_enospc_cleanup_fails_closed_without_deleting_package(
    client: TestClient, tmp_path: Path, monkeypatch,
):
    import errno
    from agent import miner_cache_cleanup as agent_cache
    from agent.commands import Command, CommandExecutor
    from agent.tests.test_miner_cache_cleanup import fixture as agent_fixture
    from agent.tests.test_miner_cache_cleanup import package as agent_package

    monkeypatch.setattr(agent_cache, "_process_refs", lambda _root, _names, _deadline: (set(), False))
    config, root = agent_fixture(tmp_path)
    candidate, _ = agent_package(root, version="0.9")
    executor = CommandExecutor(config)
    admin = admin_token(client)
    agent_id, secret = enroll(client, admin, name="synthetic-enospc-wire")
    heartbeat(client, agent_id, secret, [CAP])
    queued_scan = queue(client, admin, agent_id, SCAN, {})
    scan_wire = next(item for item in heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
                     if item["id"] == queued_scan.json()["id"])
    scanned = executor.execute(Command(scan_wire["id"], scan_wire["action"], scan_wire["payload"]))
    assert scanned.status == "succeeded"
    assert result(client, agent_id, secret, scan_wire["id"], "succeeded", scanned.result).status_code == 200
    queued_cleanup = queue(client, admin, agent_id, CLEANUP, {
        "scan_fingerprint": scanned.result["scan_fingerprint"],
    })
    cleanup_wire = next(item for item in heartbeat(client, agent_id, secret, [CAP]).json()["commands"]
                        if item["id"] == queued_cleanup.json()["id"])
    real_open = agent_cache.os.open

    def full(path, flags, *args, **kwargs):
        if str(path).endswith(agent_cache._LOCK_NAME):
            raise OSError(errno.ENOSPC, "synthetic full disk")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(agent_cache.os, "open", full)
    outcome = executor.execute(Command(cleanup_wire["id"], cleanup_wire["action"], cleanup_wire["payload"]))
    assert outcome.status == "failed" and outcome.result["state"] == "failed"
    acknowledged = client.post(f"/api/agent/commands/{cleanup_wire['id']}/result",
                               headers=agent_auth(agent_id, secret),
                               json={"status": outcome.status, "result": outcome.result,
                                     "error": outcome.error})
    assert acknowledged.status_code == 200, acknowledged.text
    assert candidate.exists()
    saved = client.get(f"/api/commands/{cleanup_wire['id']}", headers=auth(admin)).json()
    assert saved["status"] == "failed"
    assert saved["result"]["logical_bytes_removed"] == 0
    assert "synthetic full disk" not in json.dumps(saved)
