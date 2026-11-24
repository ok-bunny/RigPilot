"""Runtime command cancellation (Controller 0.8.47).

An operator may cancel a running ``apply_gpu_profile`` command.  This is a
server-side ledger close: ``cancelled`` is terminal, the operation channel is
released for new commands, and any late Agent result is safely ignored by the
result handler's terminal-status path (no 409).  The device-side execution is
not interrupted, which the UI confirmation states truthfully.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import (
    auth,
    controller_settings_kwargs,
    create_user,
    login as _login,
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


def enroll(client: TestClient, token: str, *, version: str = "0.6.16") -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "runtime-cancel-test", "ttl_minutes": 30},
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


def apply_payload() -> dict[str, object]:
    return {
        "vendor": "nvidia",
        "gpu_uuid": "GPU-5080-stable-uuid",
        "setting": {
            "index": 2,
            "power_limit_w": 250,
            "core_clock_offset_mhz": 100,
            "core_clock_lock_mhz": 2490,
            "memory_clock_offset_mhz": 800,
            "memory_clock_lock_mhz": 7001,
            "fan_percent": 65,
        },
    }


def gpu_metrics() -> dict[str, object]:
    return {
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
    }


def agent_heartbeat(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
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
            "metrics": {
                "mining_status": {
                    "configured": True,
                    "state": "running",
                    "flight_sheet_id": "flight-gpu-oc-test",
                    "process_verified": True,
                },
                **gpu_metrics(),
            },
        },
    )


def queue_running_gpu_oc(
    client: TestClient,
    token: str,
    agent_id: str,
    agent_secret: str,
) -> dict[str, object]:
    """Queue one GPU OC apply and drive it to a confirmed running state."""

    assert agent_heartbeat(client, agent_id, agent_secret).status_code == 200
    queued = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=apply_payload(),
    )
    assert queued.status_code == 201, queued.text
    command = queued.json()
    delivered = agent_heartbeat(client, agent_id, agent_secret)
    assert delivered.status_code == 200
    assert [item["id"] for item in delivered.json()["commands"]] == [command["id"]]
    running = client.post(
        f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={"status": "running", "result": {}, "error": None},
    )
    assert running.status_code == 200, running.text
    current = client.get(f"/api/commands/{command['id']}", headers=auth(token)).json()
    assert current["status"] == "running"
    return current


def test_running_gpu_oc_cancel_closes_ledger_with_audit(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)

    cancelled = client.post(
        f"/api/commands/{command['id']}/cancel", headers=auth(token)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"status": "cancelled"}

    current = client.get(f"/api/commands/{command['id']}", headers=auth(token)).json()
    assert current["status"] == "cancelled"
    assert current["finished_at"]

    audit = client.get(
        f"/api/audit?agent_id={agent_id}&limit=50", headers=auth(token)
    ).json()
    events = [
        entry
        for entry in audit
        if entry["action"] == "command_cancelled"
        and entry["target_type"] == "command"
        and entry["target_id"] == command["id"]
    ]
    assert events, "expected a command_cancelled audit event for the runtime cancel"
    details = events[0].get("details") or {}
    assert details.get("runtime_cancel") is True
    assert details.get("prior_status") in {"running", "dispatched"}
    assert details.get("device_execution_interrupted") is False


def test_cancel_releases_operation_channel_for_new_commands(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)

    duplicate = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=apply_payload(),
    )
    assert duplicate.status_code == 409

    assert (
        client.post(
            f"/api/commands/{command['id']}/cancel", headers=auth(token)
        ).status_code
        == 200
    )

    # The operation channel is free again: the very same OC apply is accepted
    # as a new command and delivered to the Agent.
    reapplied = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=apply_payload(),
    )
    assert reapplied.status_code == 201, reapplied.text
    assert reapplied.json()["id"] != command["id"]
    delivered = agent_heartbeat(client, agent_id, agent_secret)
    assert delivered.status_code == 200
    delivered_ids = [item["id"] for item in delivered.json()["commands"]]
    assert reapplied.json()["id"] in delivered_ids
    assert command["id"] not in delivered_ids


def test_late_agent_results_after_cancel_are_ignored_without_409(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)
    assert (
        client.post(
            f"/api/commands/{command['id']}/cancel", headers=auth(token)
        ).status_code
        == 200
    )

    agent_headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"}
    for report in (
        {"status": "running", "result": {}, "error": None},
        {"status": "succeeded", "result": {"late": True}, "error": None},
        {"status": "failed", "result": {}, "error": "stuck NVML call finished late"},
    ):
        late = client.post(
            f"/api/agent/commands/{command['id']}/result",
            headers=agent_headers,
            json=report,
        )
        assert late.status_code == 200, late.text
        assert late.json() == {"status": "cancelled"}

    current = client.get(f"/api/commands/{command['id']}", headers=auth(token)).json()
    assert current["status"] == "cancelled"
    assert current["result"] == {}


def test_stop_mining_lock_is_released_by_running_gpu_oc_cancel(client: TestClient):
    """The production lock: stop_mining 409s while a GPU OC apply runs."""

    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)

    blocked = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert blocked.status_code == 409
    assert "GPU OC" in blocked.json()["detail"]

    assert (
        client.post(
            f"/api/commands/{command['id']}/cancel", headers=auth(token)
        ).status_code
        == 200
    )
    allowed = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert allowed.status_code == 201, allowed.text


def test_running_non_cancellable_command_still_rejects_cancel(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    assert agent_heartbeat(client, agent_id, agent_secret).status_code == 200
    queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert queued.status_code == 201, queued.text
    command = queued.json()
    delivered = agent_heartbeat(client, agent_id, agent_secret)
    assert [item["id"] for item in delivered.json()["commands"]] == [command["id"]]

    rejected = client.post(
        f"/api/commands/{command['id']}/cancel", headers=auth(token)
    )
    assert rejected.status_code == 409

    running = client.post(
        f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={"status": "running", "result": {}, "error": None},
    )
    assert running.status_code == 200
    rejected_running = client.post(
        f"/api/commands/{command['id']}/cancel", headers=auth(token)
    )
    assert rejected_running.status_code == 409


def test_terminal_command_cancel_rejects_and_queued_cancel_unchanged(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)

    # Succeed the command, then confirm the terminal row rejects cancel.
    success = client.post(
        f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={
            "status": "succeeded",
            "result": {
                "vendor": command["payload"]["vendor"],
                "gpu_profile_id": command["payload"]["gpu_profile_id"],
                "operations": [
                    {
                        "index": setting["index"],
                        "applied": [key for key in setting if key != "index"],
                    }
                    for setting in command["payload"]["settings"]
                ],
            },
            "error": None,
        },
    )
    assert success.status_code == 200, success.text
    assert (
        client.post(f"/api/commands/{command['id']}/cancel", headers=auth(token)).status_code
        == 409
    )

    # The queued path keeps its original semantics: cancel before dispatch and
    # the Agent must never receive that command.  The controller's declarative
    # OC convergence may independently queue a fresh reapply for the still
    # pending desired state (existing heartbeat behavior, identical to the
    # pre-0.8.47 queued-cancel path); only the cancelled row itself must stay
    # closed and undelivered.
    assert agent_heartbeat(client, agent_id, agent_secret).status_code == 200
    queued = client.post(
        f"/api/agents/{agent_id}/gpu-oc/apply",
        headers=auth(token),
        json=apply_payload(),
    )
    assert queued.status_code == 201, queued.text
    queued_id = queued.json()["id"]
    queued_cancel = client.post(
        f"/api/commands/{queued_id}/cancel", headers=auth(token)
    )
    assert queued_cancel.status_code == 200
    assert queued_cancel.json() == {"status": "cancelled"}
    delivered = agent_heartbeat(client, agent_id, agent_secret)
    assert delivered.status_code == 200
    delivered_items = delivered.json()["commands"]
    assert queued_id not in {item["id"] for item in delivered_items}
    assert all(item["action"] == "apply_gpu_profile" for item in delivered_items)
    cancelled_row = client.get(f"/api/commands/{queued_id}", headers=auth(token)).json()
    assert cancelled_row["status"] == "cancelled"
    assert cancelled_row["finished_at"]


def test_viewer_cannot_cancel_running_gpu_oc(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)

    viewer_token = create_user(client, token, "runtime-cancel-viewer", "viewer")
    rejected = client.post(
        f"/api/commands/{command['id']}/cancel", headers=auth(viewer_token)
    )
    assert rejected.status_code == 403

    operator_token = create_user(client, token, "runtime-cancel-operator", "operator")
    allowed = client.post(
        f"/api/commands/{command['id']}/cancel", headers=auth(operator_token)
    )
    assert allowed.status_code == 200


def test_cancelled_command_is_readable_in_history(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)
    assert (
        client.post(
            f"/api/commands/{command['id']}/cancel", headers=auth(token)
        ).status_code
        == 200
    )

    history = client.get(
        f"/api/commands?agent_id={agent_id}&limit=20&include_all_active=true",
        headers=auth(token),
    ).json()
    rows = {row["id"]: row for row in history}
    assert command["id"] in rows
    assert rows[command["id"]]["status"] == "cancelled"
    assert rows[command["id"]]["finished_at"]
    assert rows[command["id"]]["agent_id"] == agent_id


def test_cancelled_gpu_oc_row_survives_history_clear(client: TestClient):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)
    assert (
        client.post(
            f"/api/commands/{command['id']}/cancel", headers=auth(token)
        ).status_code
        == 200
    )
    # Cancelled is terminal.  0.8.50 retired the bulk history wipe endpoint,
    # so the finished row stays in the ledger like every other terminal row.
    history = client.get(
        f"/api/commands?agent_id={agent_id}&limit=20&include_all_active=true",
        headers=auth(token),
    ).json()
    row = next(item for item in history if item["id"] == command["id"])
    assert row["status"] == "cancelled"


def test_no_agent_command_status_polling_endpoint_exists(client: TestClient):
    """0.8.57 Agent cancellation was cancelled; no dead polling route ships."""

    routes = {
        getattr(route, "path", "") for route in client.app.routes
    }
    assert "/api/agent/commands/{command_id}/status" not in routes


def test_result_schema_still_rejects_cancelled_status_reports(client: TestClient):
    """Agents never report "cancelled"; the result schema must not accept it."""

    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)
    rejected = client.post(
        f"/api/agent/commands/{command['id']}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {agent_secret}"},
        json={"status": "cancelled", "result": {"cancelled": True}, "error": None},
    )
    assert rejected.status_code == 422
    current = client.get(f"/api/commands/{command['id']}", headers=auth(token)).json()
    assert current["status"] == "running"


def test_runtime_cancel_json_payload_is_json_serializable_evidence(
    client: TestClient,
):
    """Smoke: the audit details the receipt quotes serialize to plain JSON."""

    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    command = queue_running_gpu_oc(client, token, agent_id, agent_secret)
    assert (
        client.post(
            f"/api/commands/{command['id']}/cancel", headers=auth(token)
        ).status_code
        == 200
    )
    audit = client.get(
        f"/api/audit?agent_id={agent_id}&limit=50", headers=auth(token)
    ).json()
    event = next(
        entry
        for entry in audit
        if entry["action"] == "command_cancelled"
        and entry["target_id"] == command["id"]
    )
    assert json.dumps(event["details"])
