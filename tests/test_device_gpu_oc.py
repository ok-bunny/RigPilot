from __future__ import annotations

from pathlib import Path
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from controller_test_support import (
    auth,
    login as _login,
    gpu_oc_success_result,
    controller_settings_kwargs,
)

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient, username: str = "admin", password: str = "correct-horse-battery-staple") -> str:
    return _login(client, username, password)


def enroll(
    client: TestClient, token: str, *, version: str = "0.6.16"
) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "gpu-oc-test", "ttl_minutes": 30},
    )
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": "gpu-rig",
            "hostname": "gpu-rig.local",
            "os_family": "windows",
            "agent_version": version,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def apply_payload(index: int = 2) -> dict[str, object]:
    return {
        "vendor": "nvidia",
        "gpu_uuid": "GPU-5080-stable-uuid",
        "setting": {
            "index": index,
            "power_limit_w": 250,
            "core_clock_offset_mhz": 100,
            "core_clock_lock_mhz": 2490,
            "memory_clock_offset_mhz": 800,
            "memory_clock_lock_mhz": 7001,
            "fan_percent": 65,
        },
    }


def mining_status(state: str = "running") -> dict[str, object]:
    return {
        "configured": True,
        "state": state,
        "flight_sheet_id": "flight-gpu-oc-test",
        "process_verified": state in {"running", "active"},
    }


def gpu_heartbeat(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
    *,
    version: str = "0.6.16",
    fan_min_percent: int = 30,
    fan_max_percent: int = 100,
    mining_state: str = "running",
):
    return client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "hostname": "gpu-rig.local",
            "os_family": "windows",
            "agent_version": version,
            "metrics": {
                "mining_status": mining_status(mining_state),
                "gpus": [
                    {
                        "index": 2,
                        "name": "NVIDIA GeForce RTX 5080",
                        "uuid": "GPU-5080-stable-uuid",
                        "vendor": "nvidia",
                        "oc_capabilities": [
                            "power_limit_w",
                            "core_clock_offset_mhz",
                            "core_clock_lock_mhz",
                            "memory_clock_offset_mhz",
                            "memory_clock_lock_mhz",
                            "fan_percent",
                        ],
                        "fan_min_percent": fan_min_percent,
                        "fan_max_percent": fan_max_percent,
                    }
                ]
            },
        },
    )


def gpu_inventory_heartbeat(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
    gpus: list[dict[str, object]],
    *,
    version: str = "0.6.16",
):
    return client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "hostname": "gpu-rig.local",
            "os_family": "windows",
            "agent_version": version,
            "metrics": {"mining_status": mining_status(), "gpus": gpus},
        },
    )


def post_result(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
    command_id: str,
    *,
    status: str = "succeeded",
    result: dict[str, object] | None = None,
    gpu_oc_command: dict[str, object] | None = None,
):
    if result is None and gpu_oc_command is not None:
        result = gpu_oc_success_result(gpu_oc_command)
    return client.post(
        f"/api/agent/commands/{command_id}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "status": status,
            "result": result or {},
            "error": "simulated failure" if status == "failed" else None,
        },
    )


def gpu_oc_fault_result(command, **changes):
    """Build the Agent's bound recovery receipt from the actual dispatch wire."""
    payload = command["payload"]
    settings = [
        {key: int(value) if isinstance(value, float) and value.is_integer() else value
         for key, value in setting.items()}
        for setting in payload["settings"]
    ]
    settings.sort(key=lambda setting: setting["index"])

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()

    uuid = payload["expected_gpu_uuid"]
    mode = payload["mode"]
    intent = {"vendor": "nvidia", "mode": mode, "settings": settings,
              "expected_gpu_uuid": uuid}
    recovery = {
        "status": "partial", "confirmed_reset": [],
        "hardware_reset_required": True, "reset_required": True,
        "nvml_codes": [16], "write_attempted": False,
        "state_reconciled": True, "recovered": False, "errors": [],
        "command_id": command["id"], "request_fingerprint": digest(intent),
        "targets": [{"index": setting["index"], "requested_gpu_uuid": uuid,
                     "identity_verified": False,
                     "setting_fingerprint": digest({"vendor": "nvidia", "mode": mode,
                         "index": setting["index"], "setting": setting,
                         "expected_gpu_uuid": uuid})} for setting in settings],
    }
    recovery.update(changes)
    recovery["hardware_fault"] = bool({15, 16}.intersection(recovery["nvml_codes"]))
    return {"gpu_oc_recovery": recovery}


def gpu_oc_replay_case(client):
    token = login(client)
    agent_id, secret = enroll(client, token, version="0.8.47")
    assert gpu_heartbeat(client, agent_id, secret, version="0.8.47").status_code == 200
    saved = client.post(f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token), json=apply_payload())
    assert saved.status_code == 201, saved.text
    command = gpu_heartbeat(client, agent_id, secret, version="0.8.47").json()["commands"][0]
    assert post_result(client, agent_id, secret, command["id"],
        gpu_oc_command=command).status_code == 200
    restart = client.post(f"/api/agents/{agent_id}/commands", headers=auth(token),
        json={"action": "restart_mining", "payload": {}})
    assert restart.status_code == 201, restart.text
    assert gpu_heartbeat(client, agent_id, secret, version="0.8.47").json()["commands"][0]["id"] == restart.json()["id"]
    assert post_result(client, agent_id, secret, restart.json()["id"],
        result={"restarted": True}).status_code == 200
    command = gpu_heartbeat(client, agent_id, secret, version="0.8.47").json()["commands"][0]
    return token, agent_id, secret, command


def desired_oc(client, token, agent_id):
    return client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["desired_gpu_oc"]


def test_gpu_oc_hardware_fault_is_not_requeued_by_heartbeat(client: TestClient):
    token, agent_id, secret, command = gpu_oc_replay_case(client)
    failed = post_result(client, agent_id, secret, command["id"], status="failed",
        result=gpu_oc_fault_result(command))
    assert failed.status_code == 200, failed.text
    replay = gpu_heartbeat(client, agent_id, secret, version="0.8.47").json()["commands"]
    assert replay == [], "a bound NVML reset-required failure must not create a new OC command"
    entry = desired_oc(client, token, agent_id)[0]
    assert entry["replay_block"]["intent_id"] == entry["intent_id"]
    assert entry["replay_block"]["reason"] == "hardware_reset_required"
    assert entry["replay_block"]["command_id"] == command["id"]


@pytest.mark.parametrize("changes,reason", [
    ({"status": "failed", "hardware_reset_required": False, "reset_required": False,
      "nvml_codes": [], "write_attempted": True, "errors": ["default restoration failed"]}, "recovery_failed"),
    ({"status": "failed", "hardware_reset_required": False, "reset_required": False,
      "nvml_codes": [], "write_attempted": True, "state_reconciled": False,
      "persistence_failed": True, "hardware_state": "requested_settings_applied"}, "state_persistence_failed"),
    ({"status": "rolled_back", "write_attempted": True}, "hardware_reset_required"),
    ({"status": "defaults_restored", "write_attempted": True}, "hardware_reset_required"),
    ({"status": "rolled_back", "hardware_reset_required": False, "reset_required": False,
      "nvml_codes": [], "write_attempted": True, "errors": ["state reconciliation failed"]}, "recovery_failed"),
    ({"status": "defaults_restored", "hardware_reset_required": False, "reset_required": False,
      "nvml_codes": [], "write_attempted": True, "state_reconciled": False}, "recovery_failed"),
], ids=["recovery-failed", "persistence-failed", "rollback-needs-reset", "defaults-need-reset",
        "rollback-has-recovery-error", "ownership-not-reconciled"])
def test_gpu_oc_recovery_failures_hold_the_same_intent(client, changes, reason):
    token, aid, secret, command = gpu_oc_replay_case(client)
    original = desired_oc(client, token, aid)[0]
    result = gpu_oc_fault_result(command, **changes)
    assert post_result(client, aid, secret, command["id"], status="failed", result=result).status_code == 200
    entry = desired_oc(client, token, aid)[0]
    assert entry["setting"] == original["setting"]
    assert entry["replay_block"]["reason"] == reason
    for _ in range(2):
        assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []


@pytest.mark.parametrize("receipt", ["transient", "fully-recovered"])
def test_gpu_oc_ordinary_failures_keep_existing_automatic_retry(client, receipt):
    token, aid, secret, command = gpu_oc_replay_case(client)
    result = {} if receipt == "transient" else gpu_oc_fault_result(command,
        status="rolled_back", hardware_reset_required=False, reset_required=False,
        nvml_codes=[], write_attempted=True, recovered=True)
    assert post_result(client, aid, secret, command["id"], status="failed", result=result).status_code == 200
    assert "replay_block" not in desired_oc(client, token, aid)[0]
    retry = gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"]
    assert len(retry) == 1 and retry[0]["id"] != command["id"]
    assert retry[0]["payload"]["expected_gpu_uuid"] == command["payload"]["expected_gpu_uuid"]


@pytest.mark.parametrize("corrupt", ["command", "request", "uuid", "setting", "index", "multiple-targets"])
def test_gpu_oc_fault_receipt_must_bind_the_single_dispatched_target(client, corrupt):
    token, aid, secret, command = gpu_oc_replay_case(client)
    result = gpu_oc_fault_result(command)
    recovery = result["gpu_oc_recovery"]
    if corrupt == "command": recovery["command_id"] = "another-command"
    elif corrupt == "request": recovery["request_fingerprint"] = "0" * 64
    elif corrupt == "uuid": recovery["targets"][0]["requested_gpu_uuid"] = "GPU-another-card"
    elif corrupt == "setting": recovery["targets"][0]["setting_fingerprint"] = "0" * 64
    elif corrupt == "index": recovery["targets"][0]["index"] += 1
    else: recovery["targets"].append(dict(recovery["targets"][0]))
    response = post_result(client, aid, secret, command["id"], status="failed", result=result)
    assert response.status_code == 200, response.text
    assert "replay_block" not in desired_oc(client, token, aid)[0]


def test_gpu_oc_block_survives_command_row_loss_and_miner_success(client):
    token, aid, secret, command = gpu_oc_replay_case(client)
    assert post_result(client, aid, secret, command["id"], status="failed",
        result=gpu_oc_fault_result(command)).status_code == 200
    blocked = desired_oc(client, token, aid)
    # The replay block lives on the agent row; losing the finished command
    # row through any future pruning path must not lift it.
    with client.app.state.db.connection() as db:
        db.execute("DELETE FROM commands WHERE status IN ('succeeded','failed','cancelled','expired')")
    assert desired_oc(client, token, aid) == blocked
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []
    restart = client.post(f"/api/agents/{aid}/commands", headers=auth(token),
        json={"action": "restart_mining", "payload": {}})
    assert restart.status_code == 201, restart.text
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"][0]["id"] == restart.json()["id"]
    assert post_result(client, aid, secret, restart.json()["id"], result={"restarted": True}).status_code == 200
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []
    wallet = client.post("/api/wallets", headers=auth(token),
        json={"name": "OC hold wallet", "coin": "BTC", "address": "synthetic-public-wallet"}).json()
    sheet = client.post("/api/flight-sheets", headers=auth(token),
        json={"name": "OC hold flight", "coin": "BTC", "wallet_id": wallet["id"],
              "pool_url": "stratum+tcp://pool.example.test:3333"}).json()
    applied = client.post(f"/api/agents/{aid}/flight-sheets/{sheet['id']}/apply", headers=auth(token))
    assert applied.status_code == 201, applied.text
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"][0]["id"] == applied.json()["id"]
    assert post_result(client, aid, secret, applied.json()["id"], result={"applied": True}).status_code == 200
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []
    assert desired_oc(client, token, aid) == blocked


@pytest.mark.parametrize("change_settings", [False, True], ids=["same-values", "changed-values"])
def test_gpu_oc_explicit_save_creates_new_intent_and_old_terminal_failure_is_noop(client, change_settings):
    token, aid, secret, command = gpu_oc_replay_case(client)
    failure = gpu_oc_fault_result(command)
    assert post_result(client, aid, secret, command["id"], status="failed", result=failure).status_code == 200
    old = desired_oc(client, token, aid)[0]
    payload = apply_payload()
    if change_settings: payload["setting"]["power_limit_w"] = 255
    saved = client.post(f"/api/agents/{aid}/gpu-oc/apply", headers=auth(token), json=payload)
    assert saved.status_code == 201, saved.text
    new = desired_oc(client, token, aid)[0]
    assert new["intent_id"] != old["intent_id"]
    assert "replay_block" not in new
    assert post_result(client, aid, secret, command["id"], status="failed", result=failure).status_code == 200
    assert desired_oc(client, token, aid)[0] == new
    queued = gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"]
    assert [item["id"] for item in queued] == [saved.json()["id"]]


def test_gpu_oc_private_marker_is_not_sent_to_agent_and_stale_marker_cannot_block_new_intent(client):
    token, aid, secret, command = gpu_oc_replay_case(client)
    assert "_controller_gpu_oc_intent" not in command["payload"]
    with client.app.state.db.connection() as db:
        old_payload = json.loads(db.execute("SELECT payload_json FROM commands WHERE id=?", (command["id"],)).fetchone()[0])
    old_marker = old_payload["_controller_gpu_oc_intent"]
    assert old_marker["intent_id"] == desired_oc(client, token, aid)[0]["intent_id"]
    assert post_result(client, aid, secret, command["id"], gpu_oc_command=command).status_code == 200
    saved = client.post(f"/api/agents/{aid}/gpu-oc/apply", headers=auth(token), json=apply_payload())
    assert saved.status_code == 201, saved.text
    current = gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"][0]
    assert "_controller_gpu_oc_intent" not in current["payload"]
    assert "_controller_gpu_oc_intent" not in saved.json()["payload"]
    assert "_controller_gpu_oc_intent" not in client.get(f"/api/commands/{current['id']}", headers=auth(token)).json()["payload"]
    # Model a durable older-generation binding. The actual request still has
    # identical hardware values and a valid receipt; intent identity decides.
    with client.app.state.db.connection() as db:
        row = db.execute("SELECT payload_json FROM commands WHERE id=?", (current["id"],)).fetchone()
        payload = json.loads(row[0]); payload["_controller_gpu_oc_intent"] = old_marker
        db.execute("UPDATE commands SET payload_json=? WHERE id=?", (json.dumps(payload), current["id"]))
    assert post_result(client, aid, secret, current["id"], status="failed",
        result=gpu_oc_fault_result(current)).status_code == 200
    assert "replay_block" not in desired_oc(client, token, aid)[0]


@pytest.mark.parametrize("error,blocked", [
    ("query GPU 2 core clock offset limits failed: replacement query: GPU requires reset; legacy query: GPU requires reset", True),
    ("GPU OC transition failed: set GPU 2 memory_clock_lock_mhz failed: GPU requires reset; rollback also failed: reset GPU 2 core clock lock failed: GPU requires reset", True),
    ("GPU OC transition failed: set GPU 3 memory_clock_lock_mhz failed: GPU requires reset", False),
    ("set GPU 2 memory_clock_lock_mhz failed: Not Supported", False),
    ("set GPU 2 memory_clock_lock_mhz failed: operator suspects GPU requires reset", False),
    ("GPU OC transition failed: set GPU 2 fans failed: set GPU 2 fan 0 failed: GPU requires reset; restore GPU 2 fan 0 failed: Not Supported", True),
    ("query GPU 2 fan 0 speed failed: GPU requires reset", True),
    ("set GPU 2 fans failed: set GPU 3 fan 0 failed: GPU requires reset", False),
], ids=["range-query", "setter-and-rollback", "other-card", "unsupported", "untyped-speculation",
        "fan-wrapper-and-restore", "fan-speed-query", "fan-wrapper-other-card"])
def test_gpu_oc_legacy047_reset_required_is_precise_and_never_claims_recovery(client, error, blocked):
    token, aid, secret, command = gpu_oc_replay_case(client)
    response = client.post(f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": aid, "Authorization": f"Bearer {secret}"},
        json={"status": "failed", "result": {}, "error": error})
    assert response.status_code == 200, response.text
    entry = desired_oc(client, token, aid)[0]
    assert ("replay_block" in entry) is blocked
    if blocked:
        block = entry["replay_block"]
        assert block["reason"] == "legacy_reset_required"
        assert block["error"] == error
        assert block.get("recovered") is not True
        assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []


def test_gpu_oc_legacy_entry_and_markerless_inflight_fault_get_a_durable_hold(client):
    token, aid, secret, command = gpu_oc_replay_case(client)
    with client.app.state.db.connection() as db:
        entry = desired_oc(client, token, aid)[0]
        legacy_entry = {key: entry[key] for key in ("gpu_uuid", "name", "setting")}
        payload = json.loads(db.execute("SELECT payload_json FROM commands WHERE id=?", (command["id"],)).fetchone()[0])
        payload.pop("_controller_gpu_oc_intent")
        db.execute("UPDATE agents SET desired_gpu_oc_json=? WHERE id=?", (json.dumps([legacy_entry]), aid))
        db.execute("UPDATE commands SET payload_json=? WHERE id=?", (json.dumps(payload), command["id"]))
    error = "query GPU 2 core clock offset limits failed: replacement query: GPU requires reset; legacy query: GPU requires reset"
    receipt = client.post(f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": aid, "Authorization": f"Bearer {secret}"},
        json={"status": "failed", "result": {}, "error": error})
    assert receipt.status_code == 200, receipt.text
    entry = desired_oc(client, token, aid)[0]
    assert entry["intent_id"].startswith("legacy-")
    assert entry["replay_block"]["intent_id"] == entry["intent_id"]
    assert entry["replay_block"]["reason"] == "legacy_reset_required"
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []


def test_gpu_oc_autonomous_replay_bootstraps_legacy_fault_even_after_receipt_loss(client):
    """0.8.50 retired the bulk history wipe; the durable-hold guarantee now
    rides on the autonomous-replay reconcile itself, with or without the
    original receipt row."""
    token, aid, secret, command = gpu_oc_replay_case(client)
    error = "GPU OC transition failed: set GPU 2 memory_clock_lock_mhz failed: GPU requires reset; rollback also failed: reset GPU 2 core clock lock failed: GPU requires reset"
    with client.app.state.db.connection() as db:
        entry = json.loads(db.execute("SELECT desired_gpu_oc_json FROM agents WHERE id=?", (aid,)).fetchone()[0])[0]
        legacy_entry = {key: entry[key] for key in ("gpu_uuid", "name", "setting")}
        payload = json.loads(db.execute("SELECT payload_json FROM commands WHERE id=?", (command["id"],)).fetchone()[0])
        payload.pop("_controller_gpu_oc_intent")
        db.execute("UPDATE agents SET desired_gpu_oc_json=? WHERE id=?", (json.dumps([legacy_entry]), aid))
        # Historical 0.8.47 state: failure/pending predates Controller upgrade;
        # no new result endpoint or heartbeat has bootstrapped the hold yet.
        db.execute("UPDATE commands SET status='failed',result_json='{}',error=?,payload_json=?,finished_at=datetime('now') WHERE id=?",
            (error, json.dumps(payload), command["id"]))
    # The next autonomous-replay attempt must reconcile the legacy fault
    # into a durable hold instead of queueing an unsafe reapply.
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []
    entry = desired_oc(client, token, aid)[0]
    assert entry["replay_block"]["reason"] == "legacy_reset_required"
    assert entry["replay_block"]["error"] == error
    assert entry["replay_block"]["intent_id"] == entry["intent_id"]
    # Model the receipt row disappearing through any future pruning path:
    # the adopted hold is durable on the agent row, not the command row.
    with client.app.state.db.connection() as db:
        db.execute("DELETE FROM commands WHERE id=?", (command["id"],))
    assert client.get(f"/api/commands/{command['id']}", headers=auth(token)).status_code == 404
    assert desired_oc(client, token, aid)[0] == entry
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []


@pytest.mark.parametrize("malformed", [None, [], "invalid"])
def test_gpu_oc_explicit_malformed_recovery_does_not_fall_back_to_legacy_error_matching(client, malformed):
    token, aid, secret, command = gpu_oc_replay_case(client)
    response = client.post(f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": aid, "Authorization": f"Bearer {secret}"},
        json={"status": "failed", "result": {"gpu_oc_recovery": malformed},
              "error": "set GPU 2 memory_clock_lock_mhz failed: GPU requires reset"})
    assert response.status_code == 200, response.text
    assert "replay_block" not in desired_oc(client, token, aid)[0]


@pytest.mark.parametrize("failure_phase,code", [
    ("bounds-query", 16), ("identity-query", 16), ("identity-query", 15),
], ids=["bounds-nvml16", "early-uuid-nvml16", "early-uuid-nvml15"])
def test_real_agent_query_only_fault_receipt_holds_controller_intent_then_new_save_executes(client, tmp_path, monkeypatch, failure_phase, code):
    from contextlib import nullcontext
    from agent.commands import Command
    from agent.gpu_oc import NvmlGpuOcBackend, gpu_oc_state_path
    from agent.tests.test_gpu_oc_backend import FakeFunction
    from agent.tests.test_gpu_oc_clear_integration import RecordingNvml
    from agent.tests.test_gpu_oc_failure_recovery import config, executor, save_owned

    token = login(client)
    aid, secret = enroll(client, token, version="0.8.47")
    uuid = "GPU-stable-0"
    gpus = [{"index": 0, "uuid": uuid, "vendor": "nvidia", "name": "NVIDIA synthetic",
             "oc_capabilities": ["core_clock_offset_mhz"]}]

    def heartbeat():
        response = gpu_inventory_heartbeat(client, aid, secret, gpus, version="0.8.47")
        assert response.status_code == 200, response.text
        return response.json()["commands"]

    assert heartbeat() == []
    body = {"vendor": "nvidia", "gpu_uuid": uuid, "setting": {"index": 0, "core_clock_offset_mhz": 500}}
    saved = client.post(f"/api/agents/{aid}/gpu-oc/apply", headers=auth(token), json=body)
    assert saved.status_code == 201, saved.text
    command = heartbeat()[0]
    cfg = config(tmp_path / "fake-agent")
    save_owned(cfg, {uuid: {"index": 0, "core_clock_offset_mhz": 300}})
    state_before = gpu_oc_state_path(cfg).read_bytes()
    library = RecordingNvml()
    library.hardware[0]["core_clock_offset_mhz"] = 300
    if failure_phase == "identity-query":
        library.nvmlDeviceGetUUID = FakeFunction(lambda *_args: code)
    else:
        library.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(lambda *_args: code)
    monkeypatch.setattr("agent.commands.platform.system", lambda: "Windows")
    outcome = executor(cfg, NvmlGpuOcBackend(library)).execute(
        Command(command["id"], command["action"], command["payload"]))
    assert outcome.status == "failed"
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["nvml_codes"] == [code] and recovery["write_attempted"] is False
    assert recovery["hardware_fault"] is True and recovery["recovered"] is False
    assert recovery["hardware_reset_required"] is (code == 16)
    assert recovery["reset_required"] is (code == 16)
    assert recovery["command_id"] == command["id"]
    assert recovery["targets"][0]["requested_gpu_uuid"] == uuid
    assert recovery["targets"][0]["identity_verified"] is False
    assert "expected_gpu_uuid" not in recovery["targets"][0]
    if failure_phase == "identity-query":
        assert recovery["state_unchanged"] is True and recovery["state_reconciled"] is True
        assert recovery["phase"] == "identity_verification" and recovery["identity_verified"] is False
        assert recovery["confirmed_reset"] == []
        assert recovery["failure_target"] == {"index": 0, "operation": "query", "phase": "identity_verification",
            "requested_gpu_uuid": uuid, "identity_verified": False}
    else:
        assert recovery["failure_target"] == {"index": 0, "operation": "query", "requested_gpu_uuid": uuid,
            "identity_verified": False, "field": "core_clock_offset_mhz"}
    assert library.writes == [] and gpu_oc_state_path(cfg).read_bytes() == state_before
    result_body = {"status": outcome.status, "result": outcome.result, "error": outcome.error}
    headers = {"X-Agent-ID": aid, "Authorization": f"Bearer {secret}"}
    receipt = client.post(f"/api/agent/commands/{command['id']}/result", headers=headers, json=result_body)
    assert receipt.status_code == 200, receipt.text
    held = desired_oc(client, token, aid)[0]
    assert held["replay_block"]["reason"] == ("hardware_reset_required" if code == 16 else "hardware_fault")
    assert held["replay_block"]["nvml_codes"] == [code]
    assert held["replay_block"]["hardware_reset_required"] is (code == 16)
    assert held["replay_block"]["hardware_identity_confirmed"] is False
    assert heartbeat() == []
    newer = client.post(f"/api/agents/{aid}/gpu-oc/apply", headers=auth(token), json=body)
    assert newer.status_code == 201, newer.text
    current = desired_oc(client, token, aid)[0]
    assert current["intent_id"] != held["intent_id"] and "replay_block" not in current
    assert client.post(f"/api/agent/commands/{command['id']}/result", headers=headers, json=result_body).status_code == 200
    # A later heartbeat and repeated old terminal receipt must both preserve
    # the explicit new intent; modern entries do not rescan failed history.
    assert [item["id"] for item in heartbeat()] == [newer.json()["id"]]
    assert desired_oc(client, token, aid)[0] == current


def test_gpu_oc_clear_old_owned_field_reset_fault_holds_the_new_remaining_setting(client):
    token, aid, secret, command = gpu_oc_replay_case(client)
    assert post_result(client, aid, secret, command["id"], gpu_oc_command=command).status_code == 200
    clear = client.post(f"/api/agents/{aid}/gpu-oc/apply", headers=auth(token), json={
        "vendor": "nvidia", "gpu_uuid": "GPU-5080-stable-uuid",
        "setting": {"index": 2, "memory_clock_lock_mhz": None},
    })
    assert clear.status_code == 201, clear.text
    command = gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"][0]
    assert command["payload"]["mode"] == "replace_one"
    assert "memory_clock_lock_mhz" not in command["payload"]["settings"][0]
    response = client.post(f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": aid, "Authorization": f"Bearer {secret}"},
        json={"status": "failed", "result": {}, "error": "GPU OC transition failed: reset GPU 2 memory clock lock failed: GPU requires reset"})
    assert response.status_code == 200, response.text
    entry = desired_oc(client, token, aid)[0]
    assert "memory_clock_lock_mhz" not in entry["setting"]
    assert entry["replay_block"]["reason"] == "legacy_reset_required"
    assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []


@pytest.mark.parametrize("legacy", [False, True], ids=["modern", "adopted-legacy"])
def test_gpu_oc_heartbeats_do_not_rescan_failed_history_after_intent_is_known(client, monkeypatch, legacy):
    from contextlib import contextmanager
    from server.database import Database

    token, aid, secret, command = gpu_oc_replay_case(client)
    failure = gpu_oc_fault_result(command)
    assert post_result(client, aid, secret, command["id"], status="failed", result=failure).status_code == 200
    if legacy:
        with client.app.state.db.connection() as db:
            entry = json.loads(db.execute("SELECT desired_gpu_oc_json FROM agents WHERE id=?", (aid,)).fetchone()[0])[0]
            legacy_entry = {key: entry[key] for key in ("gpu_uuid", "name", "setting")}
            payload = json.loads(db.execute("SELECT payload_json FROM commands WHERE id=?", (command["id"],)).fetchone()[0])
            payload.pop("_controller_gpu_oc_intent")
            db.execute("UPDATE agents SET desired_gpu_oc_json=? WHERE id=?", (json.dumps([legacy_entry]), aid))
            db.execute("UPDATE commands SET payload_json=?,result_json='{}',error=? WHERE id=?",
                (json.dumps(payload), "set GPU 2 memory_clock_lock_mhz failed: GPU requires reset", command["id"]))
        assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []
        assert desired_oc(client, token, aid)[0]["intent_id"].startswith("legacy-")
    statements = []
    original_connection = Database.connection

    @contextmanager
    def traced_connection(database):
        with original_connection(database) as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(Database, "connection", traced_connection)
    for _ in range(2):
        assert gpu_heartbeat(client, aid, secret, version="0.8.47").json()["commands"] == []
    failed_history_reads = [statement for statement in statements
        if "select * from commands" in " ".join(statement.lower().split())
        and "action='apply_gpu_profile'" in statement
        and "status='failed'" in statement]
    assert failed_history_reads == []


@pytest.mark.parametrize("later_success", ["same-request", "other-uuid", "other-setting", "reset-mode"])
def test_gpu_oc_legacy_adoption_stops_old_fault_only_at_matching_later_success(client, later_success):
    token, aid, secret, command = gpu_oc_replay_case(client)
    error = "set GPU 2 memory_clock_lock_mhz failed: GPU requires reset"
    with client.app.state.db.connection() as db:
        entry = json.loads(db.execute("SELECT desired_gpu_oc_json FROM agents WHERE id=?", (aid,)).fetchone()[0])[0]
        legacy_entry = {key: entry[key] for key in ("gpu_uuid", "name", "setting")}
        payload = json.loads(db.execute("SELECT payload_json FROM commands WHERE id=?", (command["id"],)).fetchone()[0])
        payload.pop("_controller_gpu_oc_intent")
        db.execute("UPDATE agents SET desired_gpu_oc_json=? WHERE id=?", (json.dumps([legacy_entry]), aid))
        db.execute("UPDATE commands SET status='failed',result_json='{}',error=?,payload_json=?,finished_at=datetime('now') WHERE id=?",
            (error, json.dumps(payload), command["id"]))
        succeeded_payload = json.loads(json.dumps(payload))
        succeeded_payload["gpu_profile_id"] = "device-oc-later-historical-success"
        if later_success == "other-uuid": succeeded_payload["expected_gpu_uuid"] = "GPU-other-history"
        elif later_success == "other-setting": succeeded_payload["settings"][0]["power_limit_w"] += 1
        elif later_success == "reset-mode": succeeded_payload["mode"] = "reset_one"
        later = {"id": "historical-later-success", "payload": succeeded_payload}
        db.execute("""INSERT INTO commands(id,agent_id,action,payload_json,status,created_by,created_at,expires_at,finished_at,result_json)
            SELECT ?,agent_id,action,?,'succeeded',created_by,created_at,expires_at,datetime('now'),?
            FROM commands WHERE id=?""",
            (later["id"], json.dumps(succeeded_payload), json.dumps(gpu_oc_success_result(later)), command["id"]))
    heartbeat = gpu_heartbeat(client, aid, secret, version="0.8.47")
    assert heartbeat.status_code == 200, heartbeat.text
    current = desired_oc(client, token, aid)[0]
    assert current["intent_id"].startswith("legacy-")
    if later_success == "same-request":
        assert "replay_block" not in current
    else:
        assert current["replay_block"]["reason"] == "legacy_reset_required"
        assert current["replay_block"]["command_id"] == command["id"]
        assert heartbeat.json()["commands"] == []


def test_direct_device_gpu_oc_is_online_single_card_and_structured(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    endpoint = f"/api/agents/{agent_id}/gpu-oc/apply"

    offline = client.post(endpoint, headers=auth(token), json=apply_payload())
    assert offline.status_code == 409
    assert "online" in offline.json()["detail"]

    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "hostname": "gpu-rig.local",
            "os_family": "windows",
            "agent_version": "0.6.16",
            "metrics": {
                "mining_status": mining_status(),
                "gpus": [
                    {
                        "index": 2,
                        "name": "NVIDIA GeForce RTX 5080",
                        "uuid": "GPU-5080-stable-uuid",
                        "vendor": "nvidia",
                        "temperature_c": 45,
                        "oc_capabilities": [
                            "power_limit_w",
                            "core_clock_offset_mhz",
                            "core_clock_lock_mhz",
                            "memory_clock_offset_mhz",
                            "memory_clock_lock_mhz",
                            "fan_percent",
                        ],
                        "fan_min_percent": 30,
                        "fan_max_percent": 100,
                    }
                ]
            },
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text

    wrong_card = client.post(endpoint, headers=auth(token), json=apply_payload(1))
    assert wrong_card.status_code == 409
    assert "no longer reported" in wrong_card.json()["detail"]

    wrong_identity = apply_payload()
    wrong_identity["gpu_uuid"] = "GPU-another-card"
    stale = client.post(endpoint, headers=auth(token), json=wrong_identity)
    assert stale.status_code == 409
    assert "identity" in stale.json()["detail"]

    applied = client.post(endpoint, headers=auth(token), json=apply_payload())
    assert applied.status_code == 201, applied.text
    command = applied.json()
    assert command["action"] == "apply_gpu_profile"
    assert command["payload"]["vendor"] == "nvidia"
    assert command["payload"]["name"] == "NVIDIA GeForce RTX 5080 · GPU 2"
    assert command["payload"]["gpu_profile_id"].startswith("device-oc-")
    assert command["payload"]["mode"] == "replace_one"
    assert command["payload"]["previous_index"] == 2
    assert command["payload"]["expected_gpu_uuid"] == "GPU-5080-stable-uuid"
    assert command["payload"]["settings"] == [apply_payload()["setting"]]

    duplicate = client.post(endpoint, headers=auth(token), json=apply_payload())
    assert duplicate.status_code == 409
    assert "already in progress" in duplicate.json()["detail"]

    dispatched = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "hostname": "gpu-rig.local",
            "os_family": "windows",
            "agent_version": "0.6.16",
            "metrics": {
                "gpus": [
                    {
                        "index": 2,
                        "name": "NVIDIA GeForce RTX 5080",
                        "uuid": "GPU-5080-stable-uuid",
                        "vendor": "nvidia",
                        "oc_capabilities": [
                            "power_limit_w",
                            "core_clock_offset_mhz",
                            "core_clock_lock_mhz",
                            "memory_clock_offset_mhz",
                            "memory_clock_lock_mhz",
                            "fan_percent",
                        ],
                        "fan_min_percent": 30,
                        "fan_max_percent": 100,
                    }
                ]
            },
        },
    )
    assert dispatched.status_code == 200
    assert dispatched.json()["commands"] == [
        {
            "id": command["id"],
            "action": "apply_gpu_profile",
            "payload": command["payload"],
        }
    ]


def test_gpu_oc_is_saved_but_not_applied_while_mining_is_stopped(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    assert gpu_heartbeat(
        client,
        agent_id,
        agent_secret,
        mining_state="stopped",
    ).status_code == 200

    saved = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=apply_payload(),
    )

    assert saved.status_code == 201, saved.text
    assert saved.json()["status"] == "saved"
    assert saved.json()["deferred"] is True
    assert saved.json()["desired_gpu_oc"][0]["gpu_uuid"] == "GPU-5080-stable-uuid"
    assert gpu_heartbeat(
        client,
        agent_id,
        agent_secret,
        mining_state="stopped",
    ).json()["commands"] == []
    agent = client.get(
        f"/api/agents/{agent_id}",
        headers=auth(token),
    ).json()
    assert agent["desired_gpu_oc"][0]["setting"]["power_limit_w"] == 250.0


def test_gpu_oc_success_receipt_is_bound_before_success_or_desired_delete(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    assert gpu_heartbeat(client, agent_id, agent_secret).status_code == 200
    queued = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=apply_payload(),
    )
    assert queued.status_code == 201, queued.text
    command = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"][0]
    valid = gpu_oc_success_result(command)
    operation = valid["operations"][0]
    assert isinstance(operation, dict)
    applied_fields = operation["applied"]
    assert isinstance(applied_fields, list)
    invalid_results = [
        {**valid, "vendor": "amd"},
        {**valid, "gpu_profile_id": "different-profile"},
        {
            **valid,
            "operations": [{**operation, "index": int(operation["index"]) + 1}],
        },
        {
            **valid,
            "operations": [{**operation, "applied": applied_fields[:-1]}],
        },
        {
            **valid,
            "operations": [
                {**operation, "applied": [*applied_fields, "unsupported_field"]}
            ],
        },
        {
            **valid,
            "operations": [
                {**operation, "applied": [*applied_fields, applied_fields[0]]}
            ],
        },
    ]
    saved = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ]
    for invalid in invalid_results:
        rejected = post_result(
            client,
            agent_id,
            agent_secret,
            command["id"],
            result=invalid,
        )
        assert rejected.status_code == 409, rejected.text
        assert client.get(
            f"/api/agents/{agent_id}", headers=auth(token)
        ).json()["desired_gpu_oc"] == saved
        with client.app.state.db.connection() as db:
            assert db.execute(
                "SELECT status FROM commands WHERE id=?",
                (command["id"],),
            ).fetchone()["status"] == "dispatched"

    accepted = post_result(
        client,
        agent_id,
        agent_secret,
        command["id"],
        result=valid,
    )
    assert accepted.status_code == 200, accepted.text

    deleted = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/2",
        headers=auth(token),
        params={"gpu_uuid": "GPU-5080-stable-uuid"},
    )
    assert deleted.status_code == 200, deleted.text
    reset_command = gpu_heartbeat(
        client, agent_id, agent_secret
    ).json()["commands"][0]
    valid_reset = gpu_oc_success_result(reset_command)
    reset_row = valid_reset["reset"][0]
    assert isinstance(reset_row, dict)
    reset_fields = reset_row["reset"]
    assert isinstance(reset_fields, list)
    invalid_resets = [
        {
            **valid_reset,
            "operations": [
                {
                    "index": reset_row["index"],
                    "applied": list(reset_fields),
                }
            ],
        },
        {
            **valid_reset,
            "reset": [{**reset_row, "reset": reset_fields[:-1]}],
        },
    ]
    for invalid in invalid_resets:
        rejected = post_result(
            client,
            agent_id,
            agent_secret,
            reset_command["id"],
            result=invalid,
        )
        assert rejected.status_code == 409, rejected.text
        assert client.get(
            f"/api/agents/{agent_id}", headers=auth(token)
        ).json()["desired_gpu_oc"] == saved
        with client.app.state.db.connection() as db:
            assert db.execute(
                "SELECT status FROM commands WHERE id=?",
                (reset_command["id"],),
            ).fetchone()["status"] == "dispatched"

    accepted_reset = post_result(
        client,
        agent_id,
        agent_secret,
        reset_command["id"],
        result=valid_reset,
    )
    assert accepted_reset.status_code == 200, accepted_reset.text
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ] == []


def test_direct_device_gpu_oc_rejects_bad_ranges_vendor_and_viewer(client: TestClient):
    admin_token = login(client)
    agent_id, _agent_secret = enroll(client, admin_token)
    endpoint = f"/api/agents/{agent_id}/gpu-oc/apply"

    outside_range = apply_payload()
    outside_range["setting"]["fan_percent"] = 101
    assert client.post(endpoint, headers=auth(admin_token), json=outside_range).status_code == 422

    below_safe_minimum = apply_payload()
    below_safe_minimum["setting"]["fan_percent"] = 29
    assert client.post(endpoint, headers=auth(admin_token), json=below_safe_minimum).status_code == 422

    wrong_vendor = apply_payload()
    wrong_vendor["vendor"] = "amd"
    assert client.post(endpoint, headers=auth(admin_token), json=wrong_vendor).status_code == 422

    created = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={
            "username": "gpuviewer",
            "password": "a-very-long-viewer-password",
            "role": "viewer",
        },
    )
    assert created.status_code == 201
    viewer_token = login(client, "gpuviewer", "a-very-long-viewer-password")
    assert client.post(endpoint, headers=auth(viewer_token), json=apply_payload()).status_code == 403


def test_direct_device_gpu_oc_rejects_fan_outside_reported_hardware_range(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    heartbeat = gpu_heartbeat(
        client,
        agent_id,
        agent_secret,
        fan_min_percent=45,
        fan_max_percent=90,
    )
    assert heartbeat.status_code == 200, heartbeat.text

    payload = apply_payload()
    payload["setting"]["fan_percent"] = 40
    response = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=payload,
    )
    assert response.status_code == 409
    assert "hardware range 45-90%" in response.json()["detail"]


def test_direct_device_gpu_oc_rejects_fields_not_confirmed_by_runtime_probe(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "hostname": "gpu-rig.local",
            "os_family": "windows",
            "agent_version": "0.6.16",
            "metrics": {
                "gpus": [
                    {
                        "index": 2,
                        "name": "NVIDIA GeForce RTX 5080",
                        "uuid": "GPU-5080-stable-uuid",
                        "vendor": "nvidia",
                        "oc_capabilities": ["power_limit_w"],
                    }
                ]
            },
        },
    )
    assert heartbeat.status_code == 200

    response = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=apply_payload(),
    )
    assert response.status_code == 409
    assert "core_clock_offset_mhz" in response.json()["detail"]
    assert "power_limit_w" not in response.json()["detail"]


def test_legacy_agent_must_upgrade_before_direct_per_gpu_oc(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "hostname": "gpu-rig.local",
            "os_family": "windows",
            "agent_version": "0.6.13",
            "metrics": {
                "gpus": [
                    {
                        "index": 2,
                        "name": "NVIDIA GeForce RTX 5080",
                        "uuid": "GPU-5080-stable-uuid",
                        "vendor": "nvidia",
                        "power_min_limit_w": 250,
                        "power_max_limit_w": 400,
                    }
                ]
            },
        },
    )
    assert heartbeat.status_code == 200
    payload = apply_payload()
    payload["setting"] = {"index": 2, "power_limit_w": 300}

    response = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=payload,
    )
    assert response.status_code == 409, response.text
    assert "0.6.16 or newer" in response.json()["detail"]


def test_desired_gpu_oc_persists_reapplies_after_success_and_stop_cancels_queue(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.6.16")
    assert gpu_heartbeat(client, agent_id, agent_secret).status_code == 200

    first_payload = {
        "vendor": "nvidia",
        "gpu_uuid": "GPU-5080-stable-uuid",
        "setting": {"index": 2, "power_limit_w": 250},
    }
    first = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=first_payload,
    )
    assert first.status_code == 201, first.text
    assert first.json()["payload"]["mode"] == "replace_one"
    assert gpu_heartbeat(client, agent_id, agent_secret).json()["commands"][0]["id"] == first.json()["id"]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        first.json()["id"],
        gpu_oc_command=first.json(),
    ).status_code == 200

    # A later partial edit merges against the controller copy and the command
    # replaces that UUID-verified card authoritatively on Agent 0.6.16.
    edited = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-5080-stable-uuid",
            "setting": {"index": 2, "fan_percent": 70},
        },
    )
    assert edited.status_code == 201, edited.text
    assert edited.json()["payload"]["settings"] == [
        {"index": 2, "power_limit_w": 250.0, "fan_percent": 70}
    ]
    dispatched_edit = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["id"] for item in dispatched_edit] == [edited.json()["id"]]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        edited.json()["id"],
        gpu_oc_command=edited.json(),
    ).status_code == 200

    saved = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ]
    assert saved == [
        {
            "gpu_uuid": "GPU-5080-stable-uuid",
            "name": "NVIDIA GeForce RTX 5080 · GPU 2",
            "setting": {"index": 2, "power_limit_w": 250.0, "fan_percent": 70},
            "intent_id": saved[0]["intent_id"],
        }
    ]
    assert len(saved[0]["intent_id"]) == 32

    restart = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert restart.status_code == 201, restart.text
    assert [item["action"] for item in gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]] == [
        "restart_mining"
    ]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        restart.json()["id"],
        result={"restarted": True},
    ).status_code == 200

    # The restart success queued a desired-OC follow-up.  Stop cancels it
    # before dispatch but deliberately leaves desired_gpu_oc untouched.
    stop = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert stop.status_code == 201, stop.text
    blocked_after_stop = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-5080-stable-uuid",
            "setting": {"index": 2, "power_limit_w": 255},
        },
    )
    assert blocked_after_stop.status_code == 409
    assert "stop_mining" in blocked_after_stop.json()["detail"]
    after_stop = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["action"] for item in after_stop] == ["stop_mining"]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        stop.json()["id"],
        result={"stopped": True},
    ).status_code == 200
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ] == saved

    wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "OC wallet",
            "coin": "BTC",
            "address": "bc1qexamplepublicaddress000000000000",
        },
    ).json()
    sheet = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "OC flight",
            "coin": "BTC",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
        },
    ).json()
    apply = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert apply.status_code == 201, apply.text
    dispatched_apply = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["id"] for item in dispatched_apply] == [apply.json()["id"]]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        apply.json()["id"],
        result={"applied": True},
    ).status_code == 200
    followup = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["action"] for item in followup] == ["apply_gpu_profile"]
    assert followup[0]["payload"]["mode"] == "replace_one"
    assert followup[0]["payload"]["expected_gpu_uuid"] == "GPU-5080-stable-uuid"
    assert followup[0]["payload"]["settings"] == [saved[0]["setting"]]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        followup[0]["id"],
        gpu_oc_command=followup[0],
    ).status_code == 200

    deleted = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/2",
        headers=auth(token),
        params={"gpu_uuid": "GPU-5080-stable-uuid"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "reset_pending"
    assert deleted.json()["desired_gpu_oc"] == saved
    blocked_flight = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert blocked_flight.status_code == 409, blocked_flight.text
    assert "apply_gpu_profile" in blocked_flight.json()["detail"]
    reset = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["action"] for item in reset] == ["apply_gpu_profile"]
    assert reset[0]["payload"]["mode"] == "reset_one"
    assert reset[0]["payload"]["expected_gpu_uuid"] == "GPU-5080-stable-uuid"
    assert reset[0]["payload"]["previous_index"] == 2
    assert reset[0]["payload"]["settings"] == [saved[0]["setting"]]
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ] == saved
    assert post_result(
        client,
        agent_id,
        agent_secret,
        reset[0]["id"],
        gpu_oc_command=reset[0],
    ).status_code == 200
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ] == []


def test_srb_apply_success_replays_saved_oc_despite_unsupported_miner_observation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from server import main as main_module

    def offline_catalog(*args, **kwargs):
        raise OSError("offline SRB replay fixture")

    def forbidden_download(*args, **kwargs):
        pytest.fail("the uploaded synthetic SRB package must not trigger a download")

    client.app.state.managed_miner_catalog._fetcher = offline_catalog
    monkeypatch.setattr(main_module, "_download_https_package", forbidden_download)
    token = login(client)
    uploaded = client.post(
        "/api/miner-packages/upload",
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        params={"name": "Synthetic SRB replay package", "version": "9.0.0",
                "archive_type": "zip", "filename": "synthetic.zip",
                "managed_miner_type": "srbminer", "managed_os_family": "windows",
                "managed_channel": "test"},
        content=b"synthetic-controller-only-srbminer-replay",
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["id"].startswith("test-")
    wallet = client.post(
        "/api/wallets", headers=auth(token),
        json={"name": "SRB replay wallet", "coin": "XMR", "address": "public-synthetic-xmr"},
    )
    assert wallet.status_code == 201, wallet.text
    created = client.post(
        "/api/flight-sheets", headers=auth(token),
        json={"name": "SRB CPU replacement", "miner_type": "srbminer",
              "miner_version": "9.0.0", "coin": "XMR", "wallet_id": wallet.json()["id"],
              "pool_url": "stratum+tcp://cpu.example.test:3333", "algorithm": "randomx"},
    )
    assert created.status_code == 201, created.text
    sheet_id = created.json()["id"]
    agent_id, agent_secret = enroll(client, token, version="0.8.44")
    agent_headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"}
    stale_status = {**mining_status(), "flight_sheet_id": "historical-retired-flight",
                    "miner_type": "synthetic-unsupported-miner"}

    def stale_heartbeat():
        response = client.post(
            "/api/agent/heartbeat", headers=agent_headers,
            json={"hostname": "gpu-rig.local", "os_family": "windows", "agent_version": "0.8.44",
                  "capabilities": ["managed_mining.dual_process.v1", "managed_mining.result_binding.v1",
                                   "managed_mining.config_fingerprint.v1"],
                  "metrics": {"mining_status": stale_status, "gpus": [{
                      "index": 2, "name": "NVIDIA GeForce RTX 5080", "vendor": "nvidia",
                      "uuid": "GPU-5080-stable-uuid", "oc_capabilities": ["power_limit_w", "core_clock_offset_mhz"],
                  }]}},
        )
        assert response.status_code == 200, response.text
        return response.json()

    assert stale_heartbeat()["commands"] == []
    saved = {"gpu_uuid": "GPU-5080-stable-uuid", "name": "Synthetic saved GPU",
             "setting": {"index": 2, "power_limit_w": 250, "core_clock_offset_mhz": 0}}
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET desired_gpu_oc_json=?,desired_gpu_oc_pending_json=? WHERE id=?",
            (json.dumps([saved]), json.dumps([saved["gpu_uuid"]]), agent_id),
        )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply", headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    command = applied.json()
    assert "device_gpu_oc" not in command["payload"]
    assert [item["id"] for item in stale_heartbeat()["commands"]] == [command["id"]]
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT payload_json FROM commands WHERE id=?", (command["id"],)).fetchone()
    marker = json.loads(stored["payload_json"])[main_module.CONTROLLER_FLIGHT_RUNTIME_MARKER]
    fingerprint_field = main_module.FLIGHT_RUNTIME_FINGERPRINT_FIELD
    bound = {"managed": True, "flight_sheet_id": sheet_id,
             fingerprint_field: marker["runtime_fingerprint"]}
    finished = post_result(
        client, agent_id, agent_secret, command["id"],
        result={"flight_sheet_id": sheet_id, fingerprint_field: marker["runtime_fingerprint"],
                "managed_miner": bound},
    )
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "succeeded", finished.text
    # The bound SRB apply is authoritative even while the accepted observation
    # still names the former, unsupported miner. Saved device OC follows the SRB path.
    with client.app.state.db.connection() as db:
        rows = db.execute("SELECT * FROM commands WHERE agent_id=? ORDER BY rowid", (agent_id,)).fetchall()
    assert [row["action"] for row in rows] == ["apply_flight_sheet", "apply_gpu_profile"]
    replay = json.loads(rows[1]["payload_json"])
    assert replay["expected_gpu_uuid"] == saved["gpu_uuid"]
    assert replay["settings"] == [saved["setting"]]


def test_delete_gpu_oc_failed_reset_preserves_desired_for_retry(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.6.16")
    assert gpu_heartbeat(client, agent_id, agent_secret).status_code == 200

    applied = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-5080-stable-uuid",
            "setting": {"index": 2, "power_limit_w": 250},
        },
    )
    assert applied.status_code == 201, applied.text
    assert gpu_heartbeat(client, agent_id, agent_secret).json()["commands"][0][
        "id"
    ] == applied.json()["id"]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        applied.json()["id"],
        gpu_oc_command=applied.json(),
    ).status_code == 200
    saved = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ]

    first_delete = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/2",
        headers=auth(token),
        params={"gpu_uuid": "GPU-5080-stable-uuid"},
    )
    assert first_delete.status_code == 200, first_delete.text
    assert first_delete.json()["status"] == "reset_pending"
    first_reset = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["id"] for item in first_reset] == [
        first_delete.json()["reset_command"]["id"]
    ]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        first_reset[0]["id"],
        status="failed",
    ).status_code == 200
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ] == saved

    retry = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/2",
        headers=auth(token),
        params={"gpu_uuid": "GPU-5080-stable-uuid"},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["desired_gpu_oc"] == saved
    retry_reset = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["id"] for item in retry_reset] == [
        retry.json()["reset_command"]["id"]
    ]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        retry_reset[0]["id"],
        gpu_oc_command=retry_reset[0],
    ).status_code == 200
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ] == []


def test_consecutive_multi_gpu_deletes_keep_each_queued_reset(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.6.16")
    gpus = [
        {
            "index": 0,
            "name": "NVIDIA GPU A",
            "uuid": "GPU-stable-A",
            "vendor": "nvidia",
            "oc_capabilities": ["power_limit_w"],
        },
        {
            "index": 1,
            "name": "NVIDIA GPU B",
            "uuid": "GPU-stable-B",
            "vendor": "nvidia",
            "oc_capabilities": ["power_limit_w"],
        },
    ]
    assert gpu_inventory_heartbeat(
        client, agent_id, agent_secret, gpus
    ).status_code == 200

    for gpu_uuid, gpu_index, power_limit in (
        ("GPU-stable-A", 0, 100),
        ("GPU-stable-B", 1, 110),
    ):
        applied = client.post(
            f"/api/agents/{agent_id}/gpu-oc/apply",
            headers=auth(token),
            json={
                "vendor": "nvidia",
                "gpu_uuid": gpu_uuid,
                "setting": {
                    "index": gpu_index,
                    "power_limit_w": power_limit,
                },
            },
        )
        assert applied.status_code == 201, applied.text
        dispatched = gpu_inventory_heartbeat(
            client, agent_id, agent_secret, gpus
        ).json()["commands"]
        assert [item["id"] for item in dispatched] == [applied.json()["id"]]
        assert post_result(
            client,
            agent_id,
            agent_secret,
            applied.json()["id"],
            gpu_oc_command=applied.json(),
        ).status_code == 200

    saved = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ]
    assert [entry["gpu_uuid"] for entry in saved] == [
        "GPU-stable-A",
        "GPU-stable-B",
    ]

    first_delete = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/0",
        headers=auth(token),
        params={"gpu_uuid": "GPU-stable-A"},
    )
    second_delete = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/1",
        headers=auth(token),
        params={"gpu_uuid": "GPU-stable-B"},
    )
    assert first_delete.status_code == 200, first_delete.text
    assert second_delete.status_code == 200, second_delete.text
    assert first_delete.json()["desired_gpu_oc"] == saved
    assert second_delete.json()["desired_gpu_oc"] == saved

    resets = gpu_inventory_heartbeat(
        client, agent_id, agent_secret, gpus
    ).json()["commands"]
    assert len(resets) == 2
    resets_by_uuid = {
        item["payload"]["expected_gpu_uuid"]: item for item in resets
    }
    assert set(resets_by_uuid) == {"GPU-stable-A", "GPU-stable-B"}
    assert all(item["payload"]["mode"] == "reset_one" for item in resets)

    assert post_result(
        client,
        agent_id,
        agent_secret,
        resets_by_uuid["GPU-stable-A"]["id"],
        gpu_oc_command=resets_by_uuid["GPU-stable-A"],
    ).status_code == 200
    after_first = client.get(
        f"/api/agents/{agent_id}", headers=auth(token)
    ).json()["desired_gpu_oc"]
    assert [entry["gpu_uuid"] for entry in after_first] == ["GPU-stable-B"]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        resets_by_uuid["GPU-stable-B"]["id"],
        gpu_oc_command=resets_by_uuid["GPU-stable-B"],
    ).status_code == 200
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()[
        "desired_gpu_oc"
    ] == []


def test_delete_gpu_oc_forget_stale_requires_uuid_to_be_entirely_absent(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.6.16")
    assert gpu_heartbeat(client, agent_id, agent_secret).status_code == 200
    applied = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-5080-stable-uuid",
            "setting": {"index": 2, "power_limit_w": 250},
        },
    )
    assert applied.status_code == 201, applied.text
    assert gpu_heartbeat(client, agent_id, agent_secret).json()["commands"][0][
        "id"
    ] == applied.json()["id"]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        applied.json()["id"],
        gpu_oc_command=applied.json(),
    ).status_code == 200

    moved_gpu = {
        "index": 3,
        "name": "NVIDIA GeForce RTX 5080",
        "uuid": "GPU-5080-stable-uuid",
        "vendor": "nvidia",
        "oc_capabilities": ["power_limit_w"],
    }
    assert gpu_inventory_heartbeat(
        client, agent_id, agent_secret, [moved_gpu]
    ).status_code == 200
    still_present = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/2",
        headers=auth(token),
        params={
            "gpu_uuid": "GPU-5080-stable-uuid",
            "forget_stale": True,
        },
    )
    assert still_present.status_code == 409

    assert gpu_inventory_heartbeat(
        client, agent_id, agent_secret, []
    ).status_code == 200
    implicit_forget = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/2",
        headers=auth(token),
        params={"gpu_uuid": "GPU-5080-stable-uuid"},
    )
    assert implicit_forget.status_code == 409
    forgotten = client.delete(
        f"/api/agents/{agent_id}/gpu-oc/2",
        headers=auth(token),
        params={
            "gpu_uuid": "GPU-5080-stable-uuid",
            "forget_stale": True,
        },
    )
    assert forgotten.status_code == 200, forgotten.text
    assert forgotten.json()["status"] == "deleted"
    assert forgotten.json()["reset_command"] is None
    assert forgotten.json()["desired_gpu_oc"] == []
    assert gpu_inventory_heartbeat(
        client, agent_id, agent_secret, []
    ).json()["commands"] == []


def test_desired_gpu_oc_reapply_remaps_uuid_when_gpu_indices_swap(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.6.16")

    def heartbeat(gpus: list[dict[str, object]]):
        return client.post(
            "/api/agent/heartbeat",
            headers={
                "X-Agent-ID": agent_id,
                "Authorization": f"Bearer {agent_secret}",
            },
            json={
                "hostname": "gpu-rig.local",
                "os_family": "windows",
                "agent_version": "0.6.16",
                "metrics": {"mining_status": mining_status(), "gpus": gpus},
            },
        )

    initial = [
        {
            "index": 0,
            "name": "NVIDIA GPU A",
            "uuid": "GPU-stable-A",
            "vendor": "nvidia",
            "oc_capabilities": ["power_limit_w"],
        },
        {
            "index": 1,
            "name": "NVIDIA GPU B",
            "uuid": "GPU-stable-B",
            "vendor": "nvidia",
            "oc_capabilities": ["power_limit_w"],
        },
    ]
    swapped = [
        {**initial[0], "index": 1},
        {**initial[1], "index": 0},
    ]
    assert heartbeat(initial).status_code == 200

    for gpu_uuid, gpu_index, power_limit in (
        ("GPU-stable-A", 0, 100),
        ("GPU-stable-B", 1, 110),
    ):
        applied = client.post(
            f"/api/agents/{agent_id}/gpu-oc/apply",
            headers=auth(token),
            json={
                "vendor": "nvidia",
                "gpu_uuid": gpu_uuid,
                "setting": {
                    "index": gpu_index,
                    "power_limit_w": power_limit,
                },
            },
        )
        assert applied.status_code == 201, applied.text
        dispatched = heartbeat(initial).json()["commands"]
        assert [item["id"] for item in dispatched] == [applied.json()["id"]]
        assert post_result(
            client,
            agent_id,
            agent_secret,
            applied.json()["id"],
            gpu_oc_command=applied.json(),
        ).status_code == 200

    # A reboot can enumerate the same physical cards in the opposite order.
    # The next successful start/restart must target current indices by UUID,
    # never replay the old numeric index onto the wrong card.
    assert heartbeat(swapped).json()["commands"] == []
    restart = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert restart.status_code == 201, restart.text
    dispatched_restart = heartbeat(swapped).json()["commands"]
    assert [item["id"] for item in dispatched_restart] == [restart.json()["id"]]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        restart.json()["id"],
        result={"restarted": True},
    ).status_code == 200

    reapplies = heartbeat(swapped).json()["commands"]
    assert len(reapplies) == 2
    by_uuid = {
        item["payload"]["expected_gpu_uuid"]: item["payload"]["settings"][0]
        for item in reapplies
    }
    assert by_uuid == {
        "GPU-stable-A": {"index": 1, "power_limit_w": 100.0},
        "GPU-stable-B": {"index": 0, "power_limit_w": 110.0},
    }
    assert all(item["payload"]["mode"] == "replace_one" for item in reapplies)

    synchronized = client.get(
        f"/api/agents/{agent_id}", headers=auth(token)
    ).json()["desired_gpu_oc"]
    assert {
        entry["gpu_uuid"]: entry["setting"]["index"] for entry in synchronized
    } == {"GPU-stable-A": 1, "GPU-stable-B": 0}


def test_desired_gpu_oc_retries_after_inventory_recovers(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.6.16")
    assert gpu_heartbeat(client, agent_id, agent_secret).status_code == 200

    applied = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-5080-stable-uuid",
            "setting": {"index": 2, "power_limit_w": 250},
        },
    )
    assert applied.status_code == 201, applied.text
    first_dispatch = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["id"] for item in first_dispatch] == [applied.json()["id"]]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        applied.json()["id"],
        gpu_oc_command=applied.json(),
    ).status_code == 200

    restart = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert restart.status_code == 201, restart.text
    restart_dispatch = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["id"] for item in restart_dispatch] == [restart.json()["id"]]

    # A driver/inventory race immediately after a restart must not discard the
    # controller-owned desired state.  The successful miner restart records a
    # durable per-UUID retry instead of guessing from the stale numeric index.
    missing_inventory = gpu_inventory_heartbeat(
        client, agent_id, agent_secret, []
    )
    assert missing_inventory.status_code == 200
    assert post_result(
        client,
        agent_id,
        agent_secret,
        restart.json()["id"],
        result={"restarted": True},
    ).status_code == 200
    with client.app.state.db.connection() as db:
        pending = db.execute(
            "SELECT desired_gpu_oc_pending_json FROM agents WHERE id=?",
            (agent_id,),
        ).fetchone()["desired_gpu_oc_pending_json"]
    assert json.loads(pending) == ["GPU-5080-stable-uuid"]
    assert gpu_inventory_heartbeat(
        client, agent_id, agent_secret, []
    ).json()["commands"] == []

    recovered = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert len(recovered) == 1
    retry = recovered[0]
    assert retry["action"] == "apply_gpu_profile"
    assert retry["payload"]["expected_gpu_uuid"] == "GPU-5080-stable-uuid"
    assert retry["payload"]["settings"] == [
        {"index": 2, "power_limit_w": 250.0}
    ]

    # Repeated heartbeats may redeliver the same dispatched command, but must
    # never create a second OC transition for the same card.
    redelivered = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert [item["id"] for item in redelivered] == [retry["id"]]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        retry["id"],
        gpu_oc_command=retry,
    ).status_code == 200
    assert gpu_heartbeat(client, agent_id, agent_secret).json()["commands"] == []
    with client.app.state.db.connection() as db:
        pending = db.execute(
            "SELECT desired_gpu_oc_pending_json FROM agents WHERE id=?",
            (agent_id,),
        ).fetchone()["desired_gpu_oc_pending_json"]
    assert json.loads(pending) == []


def test_desired_gpu_oc_followup_survives_creator_deactivation(
    client: TestClient,
):
    admin_token = login(client)
    created = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={
            "username": "ocoperator",
            "password": "operator-password-long-enough",
            "role": "operator",
        },
    )
    assert created.status_code == 201, created.text
    operator_token = login(client, "ocoperator", "operator-password-long-enough")
    operator_id = next(
        user["id"]
        for user in client.get("/api/users", headers=auth(admin_token)).json()
        if user["username"] == "ocoperator"
    )
    agent_id, agent_secret = enroll(client, admin_token, version="0.6.16")
    assert gpu_heartbeat(client, agent_id, agent_secret).status_code == 200

    applied = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(operator_token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-5080-stable-uuid",
            "setting": {"index": 2, "power_limit_w": 250},
        },
    )
    assert applied.status_code == 201, applied.text
    assert gpu_heartbeat(client, agent_id, agent_secret).json()["commands"][0]["id"] == applied.json()["id"]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        applied.json()["id"],
        gpu_oc_command=applied.json(),
    ).status_code == 200

    restart = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(operator_token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert restart.status_code == 201, restart.text
    assert gpu_heartbeat(client, agent_id, agent_secret).json()["commands"][0]["id"] == restart.json()["id"]
    assert client.delete(
        f"/api/users/{operator_id}", headers=auth(admin_token)
    ).status_code == 200
    assert post_result(
        client,
        agent_id,
        agent_secret,
        restart.json()["id"],
        result={"restarted": True},
    ).status_code == 200
    followup = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert len(followup) == 1
    assert followup[0]["action"] == "apply_gpu_profile"
    assert followup[0]["payload"]["mode"] == "replace_one"


def test_desired_gpu_oc_auto_reapply_requires_agent_0616(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.6.16")
    assert gpu_heartbeat(client, agent_id, agent_secret).status_code == 200
    applied = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json={
            "vendor": "nvidia",
            "gpu_uuid": "GPU-5080-stable-uuid",
            "setting": {"index": 2, "power_limit_w": 250},
        },
    )
    dispatched = gpu_heartbeat(client, agent_id, agent_secret).json()["commands"]
    assert dispatched[0]["id"] == applied.json()["id"]
    assert post_result(
        client,
        agent_id,
        agent_secret,
        applied.json()["id"],
        gpu_oc_command=applied.json(),
    ).status_code == 200

    assert gpu_heartbeat(
        client, agent_id, agent_secret, version="0.6.15"
    ).status_code == 200
    restart = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    restart_commands = gpu_heartbeat(
        client, agent_id, agent_secret, version="0.6.15"
    ).json()["commands"]
    assert restart_commands[0]["id"] == restart.json()["id"]
    assert post_result(
        client, agent_id, agent_secret, restart.json()["id"], result={"restarted": True}
    ).status_code == 200
    assert gpu_heartbeat(
        client, agent_id, agent_secret, version="0.6.15"
    ).json()["commands"] == []


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("supported", [False, True], ids=["unreported", "reported"])
@pytest.mark.parametrize("field,mhz", [
    ("memory_clock_lock_mhz", 7001), ("core_clock_lock_mhz", 2490),
], ids=["memory", "core"])
def test_windows_clock_lock_uses_each_gpu_capability_and_preserves_failure(
    client: TestClient, batch: bool, supported: bool, field: str, mhz: int,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.8.42")
    gpu_uuids = ["GPU-5080-stable-uuid", "GPU-second-clock-lock"][:2 if batch else 1]
    gpus = [
        {
            "index": index,
            "name": "NVIDIA clock-lock test",
            "uuid": gpu_uuid,
            "vendor": "nvidia",
            # In a batch the first card is supported even when the second is not.
            "oc_capabilities": [field]
            if supported or (batch and position == 0) else [],
        }
        for position, (index, gpu_uuid) in enumerate(zip([2, 5], gpu_uuids))
    ]
    assert gpu_inventory_heartbeat(
        client, agent_id, agent_secret, gpus, version="0.8.42"
    ).status_code == 200

    def stored_state():
        with client.app.state.db.connection() as db:
            return {
                "agent": dict(db.execute(
                    "SELECT desired_gpu_oc_json,desired_gpu_oc_pending_json FROM agents WHERE id=?",
                    (agent_id,),
                ).fetchone()),
                "commands": [dict(row) for row in db.execute(
                    "SELECT * FROM commands WHERE agent_id=? ORDER BY id", (agent_id,),
                )],
                "audit_count": db.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0],
            }

    before = stored_state()
    payload = {"vendor": "nvidia", "setting": {field: mhz}}
    if batch:
        payload["gpu_uuids"] = gpu_uuids
    else:
        payload["gpu_uuid"] = gpu_uuids[0]
        payload["setting"]["index"] = 2
    response = client.post(
        f"/api/agents/{agent_id}/gpu-oc/{'batch-apply' if batch else 'apply'}",
        headers=auth(token), json=payload,
    )
    if not supported:
        assert response.status_code == 409, response.text
        assert f"did not confirm NVML support for: {field}" in response.json()["detail"]
        assert stored_state() == before, "unsupported cards must not save desired state or queue any batch member"
        return

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["deferred"] is False
    saved = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["desired_gpu_oc"]
    assert [(entry["gpu_uuid"], entry["setting"]) for entry in saved] == [
        (gpu["uuid"], {"index": gpu["index"], field: mhz})
        for gpu in gpus
    ]
    # Observe the actual Agent-authenticated dispatch wire, not the operator projection.
    dispatched = gpu_inventory_heartbeat(
        client, agent_id, agent_secret, gpus, version="0.8.42"
    ).json()["commands"]
    assert len(dispatched) == len(gpus)
    by_uuid = {command["payload"]["expected_gpu_uuid"]: command for command in dispatched}
    for gpu in gpus:
        command = by_uuid[gpu["uuid"]]
        assert command["action"] == "apply_gpu_profile"
        assert command["payload"]["mode"] == "replace_one"
        assert command["payload"]["settings"] == [{"index": gpu["index"], field: mhz}]
        failed = post_result(client, agent_id, agent_secret, command["id"], status="failed")
        assert failed.status_code == 200, failed.text
        receipt = client.get(f"/api/commands/{command['id']}", headers=auth(token)).json()
        assert receipt["status"] == "failed" and receipt["error"] == "simulated failure"
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["desired_gpu_oc"] == saved
    assert set(json.loads(stored_state()["agent"]["desired_gpu_oc_pending_json"])) == set(gpu_uuids)


def test_windows_memory_only_clear_waits_for_success_and_can_retry(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token, version="0.8.42")
    assert gpu_heartbeat(client, agent_id, agent_secret, version="0.8.42").status_code == 200
    endpoint = f"/api/agents/{agent_id}/gpu-oc/apply"
    payload = {
        "vendor": "nvidia", "gpu_uuid": "GPU-5080-stable-uuid",
        "setting": {"index": 2, "memory_clock_lock_mhz": 7001},
    }
    assert client.post(endpoint, headers=auth(token), json=payload).status_code == 201
    applied = gpu_heartbeat(client, agent_id, agent_secret, version="0.8.42").json()["commands"][0]
    assert post_result(client, agent_id, agent_secret, applied["id"], gpu_oc_command=applied).status_code == 200
    saved = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["desired_gpu_oc"]
    payload["setting"]["memory_clock_lock_mhz"] = None
    for succeeds in [False, True]:
        cleared = client.post(endpoint, headers=auth(token), json=payload)
        assert cleared.status_code == 201, cleared.text
        assert cleared.json()["status"] == "queued"
        assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["desired_gpu_oc"] == saved
        reset = gpu_heartbeat(client, agent_id, agent_secret, version="0.8.42").json()["commands"][0]
        assert reset["payload"]["mode"] == "reset_one"
        assert reset["payload"]["settings"] == [{"index": 2, "memory_clock_lock_mhz": 7001}]
        result = post_result(
            client, agent_id, agent_secret, reset["id"], gpu_oc_command=reset,
            status="succeeded" if succeeds else "failed",
        )
        assert result.status_code == 200, result.text
        receipt = client.get(f"/api/commands/{reset['id']}", headers=auth(token)).json()
        assert receipt["status"] == ("succeeded" if succeeds else "failed")
        assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["desired_gpu_oc"] == ([] if succeeds else saved)
