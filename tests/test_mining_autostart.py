from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import main as main_module
from controller_test_support import auth, login as _login

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def controller(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="mining-autostart-tests-secret-key-that-is-long-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


def login(client: TestClient, username: str, password: str) -> str:
    return _login(client, username, password)


def enroll(
    client: TestClient,
    admin_token: str,
    *,
    name: str,
    version: str = "0.6.46",
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
            "os_family": "linux",
            "platform": "Ubuntu 22.04",
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
    boot_time: str,
    mining_state: str,
    configured: bool = True,
    process_verified: bool | None = True,
    process_error: str | None = None,
    capabilities: list[str] | None = None,
) -> dict:
    response = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "hostname": f"{agent_id}.local",
            "os_family": "linux",
            "platform": "Ubuntu 22.04",
            "agent_version": "0.6.46",
            "metrics": {
                "boot_time": boot_time,
                "uptime_seconds": 30,
            },
            "inventory": {
                "mining_status": {
                    "configured": configured,
                    "state": mining_state,
                    "flight_sheet_id": "sheet-1" if configured else None,
                    "process_verified": process_verified,
                    "process_error": process_error,
                    "error": None,
                }
            },
            "capabilities": capabilities or [],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def command_rows(
    client: TestClient, token: str, agent_id: str
) -> list[dict]:
    response = client.get(
        "/api/commands",
        headers=auth(token),
        params={"agent_id": agent_id, "include_all_active": "true"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_toggle_is_strict_role_checked_offline_safe_and_baselines_current_boot(
    controller,
):
    client, _settings = controller
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
    agent_id, secret = enroll(client, admin_token, name="autostart-toggle")

    forbidden = client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(viewer_token),
        json={"enabled": True},
    )
    assert forbidden.status_code == 403
    invalid = client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(operator_token),
        json={"enabled": 1, "unexpected": True},
    )
    assert invalid.status_code == 422

    offline_enabled = client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(operator_token),
        json={"enabled": True},
    )
    assert offline_enabled.status_code == 200, offline_enabled.text
    assert offline_enabled.json()["mining_autostart_enabled"] is True
    assert offline_enabled.json()["mining_autostart_last_boot_time"] is None
    assert command_rows(client, admin_token, agent_id) == []

    # The first accepted boot establishes a baseline and never starts mining.
    first = heartbeat(
        client,
        agent_id,
        secret,
        boot_time="2026-07-29T00:00:00+00:00",
        mining_state="stopped",
    )
    assert first["commands"] == []
    detail = client.get(
        f"/api/agents/{agent_id}", headers=auth(admin_token)
    ).json()
    assert detail["mining_autostart_last_boot_time"] == "2026-07-29T00:00:00+00:00"

    # Re-enabling an already-enabled switch cannot move its boot marker.
    enabled_again = client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(operator_token),
        json={"enabled": True},
    )
    assert (
        enabled_again.json()["mining_autostart_last_boot_time"]
        == "2026-07-29T00:00:00+00:00"
    )


def test_enabling_autostart_rejects_old_agent_but_disabling_remains_available(
    controller,
):
    client, _settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, _secret = enroll(
        client,
        admin_token,
        name="autostart-old-agent",
        version="0.5.1",
    )

    enabled = client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )
    assert enabled.status_code == 409, enabled.text
    assert "0.5.2 or newer" in enabled.text

    disabled = client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["mining_autostart_enabled"] is False


def test_new_boot_queues_one_restart_and_reuses_normal_command_path(controller):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-restart")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_a,
        mining_state="stopped",
    )
    enabled = client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["mining_autostart_last_boot_time"] == boot_a

    same_boot = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_a,
        mining_state="stopped",
    )
    assert same_boot["commands"] == []
    new_boot = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
    )
    assert [item["action"] for item in new_boot["commands"]] == [
        "restart_mining"
    ]
    assert new_boot["commands"][0]["payload"] == {}
    rows = command_rows(client, admin_token, agent_id)
    assert [item["action"] for item in rows].count("restart_mining") == 1

    # Repeated heartbeats may redeliver the same unfinished command, but they
    # must never create a second command for the same boot.
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
    )
    rows = command_rows(client, admin_token, agent_id)
    assert [item["action"] for item in rows].count("restart_mining") == 1
    with sqlite3.connect(settings.database_path) as db:
        marker = db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0]
    # The boot marker is advanced only after Agent success; merely dispatching
    # a restart must remain retryable if the command fails or expires.
    assert marker == boot_a
    command_id = next(
        item["id"] for item in rows if item["action"] == "restart_mining"
    )
    completed = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={"status": "succeeded", "result": {"restarted": True}, "error": None},
    )
    assert completed.status_code == 200, completed.text
    with sqlite3.connect(settings.database_path) as db:
        confirmed_marker = db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0]
    assert confirmed_marker == boot_b


def test_new_boot_accepts_only_the_agent_verified_host_reboot_stop_reason(
    controller,
):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-reboot-reason")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_a,
        mining_state="stopped",
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )

    unsafe = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
        process_error="managed miner process cannot be inspected",
    )
    assert unsafe["commands"] == []
    with sqlite3.connect(settings.database_path) as db:
        marker = db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0]
    assert marker == boot_a

    rebooted = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
        process_error="host rebooted; managed miner was not restarted",
    )
    assert [item["action"] for item in rebooted["commands"]] == [
        "restart_mining"
    ]


def test_new_boot_accepts_only_complete_independent_reboot_stop_reasons(
    controller,
):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(
        client, admin_token, name="autostart-independent-reboot"
    )
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_a,
        mining_state="stopped",
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )

    incomplete = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
        process_error=(
            "primary: host rebooted; managed miner was not restarted; "
            "secondary: managed miner process cannot be inspected"
        ),
    )
    assert incomplete["commands"] == []
    with sqlite3.connect(settings.database_path) as db:
        marker = db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0]
    assert marker == boot_a

    complete = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
        process_error=(
            "primary: host rebooted; managed miner was not restarted; "
            "secondary: host rebooted; managed miner was not restarted"
        ),
    )
    assert [item["action"] for item in complete["commands"]] == [
        "restart_mining"
    ]
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
        process_error=(
            "primary: host rebooted; managed miner was not restarted; "
            "secondary: host rebooted; managed miner was not restarted"
        ),
    )
    rows = command_rows(client, admin_token, agent_id)
    assert [item["action"] for item in rows].count("restart_mining") == 1
    with sqlite3.connect(settings.database_path) as db:
        marker = db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0]
    assert marker == boot_a


def test_new_boot_requires_explicit_process_verification(controller):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-unverified")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_a,
        mining_state="stopped",
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )

    unverified = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
        process_verified=None,
    )
    assert unverified["commands"] == []
    with sqlite3.connect(settings.database_path) as db:
        marker = db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0]
    assert marker == boot_a


def test_summary_serializes_each_agent_only_once(controller, monkeypatch):
    client, _settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    enroll(client, admin_token, name="summary-one-pass")
    original = main_module._serialize_agent
    calls = 0

    def counted_serialize_agent(row, settings):
        nonlocal calls
        calls += 1
        return original(row, settings)

    monkeypatch.setattr(main_module, "_serialize_agent", counted_serialize_agent)
    response = client.get("/api/summary", headers=auth(admin_token))
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert calls == 1


def test_running_new_boot_does_not_restart_or_count_warmup_as_normal_mining(
    controller,
):
    client, _settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-running")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_a,
        mining_state="stopped",
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )
    running = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="running",
    )
    assert running["commands"] == []
    assert command_rows(client, admin_token, agent_id) == []
    summary = client.get("/api/summary", headers=auth(admin_token))
    assert summary.status_code == 200, summary.text
    # A verified process suppresses a duplicate restart, but without current
    # hashrate it does not prove that this rig is actively mining.
    assert summary.json()["normal_mining"] == 0


def test_new_boot_waits_for_conflicting_command_then_retries(controller):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-conflict")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_a,
        mining_state="stopped",
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )
    reboot = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "reboot", "payload": {}},
    )
    assert reboot.status_code == 201, reboot.text
    conflicted = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
    )
    assert [item["action"] for item in conflicted["commands"]] == ["reboot"]
    with sqlite3.connect(settings.database_path) as db:
        marker = db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0]
        db.execute(
            """UPDATE commands SET status='succeeded', finished_at=?
               WHERE id=?""",
            ("2026-07-29T01:00:10+00:00", reboot.json()["id"]),
        )
        db.commit()
    assert marker == boot_a

    retried = heartbeat(
        client,
        agent_id,
        secret,
        boot_time=boot_b,
        mining_state="stopped",
    )
    assert [item["action"] for item in retried["commands"]] == [
        "restart_mining"
    ]


def test_failed_autostart_restart_does_not_consume_boot_and_retries(controller):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-failure-retry")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client, agent_id, secret, boot_time=boot_a, mining_state="stopped"
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )
    queued = heartbeat(
        client, agent_id, secret, boot_time=boot_b, mining_state="stopped"
    )["commands"][0]
    failed = client.post(
        f"/api/agent/commands/{queued['id']}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "status": "failed",
            "result": {},
            "error": "managed miner process cannot be inspected",
        },
    )
    assert failed.status_code == 200, failed.text
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0] == boot_a

    retried = heartbeat(
        client, agent_id, secret, boot_time=boot_b, mining_state="stopped"
    )
    assert [item["action"] for item in retried["commands"]] == [
        "restart_mining"
    ]
    assert retried["commands"][0]["id"] != queued["id"]


@pytest.mark.parametrize("action", ["restart_service", "reboot", "shutdown"])
def test_new_boot_expires_preboot_one_shot_command_instead_of_replaying_it(
    controller, action: str
):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-preboot-power")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client, agent_id, secret, boot_time=boot_a, mining_state="stopped"
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )
    one_shot = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={
            "action": action,
            "payload": (
                {"service": "RigPilotAgent"}
                if action == "restart_service"
                else {}
            ),
        },
    )
    assert one_shot.status_code == 201, one_shot.text
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            """UPDATE commands
               SET status='dispatched',
                   created_at='2026-07-29T00:30:00+00:00',
                   dispatched_at='2026-07-29T00:30:01+00:00',
                   expires_at='9999-12-31T23:59:59+00:00'
               WHERE id=?""",
            (one_shot.json()["id"],),
        )
        db.commit()

    after_boot = heartbeat(
        client, agent_id, secret, boot_time=boot_b, mining_state="stopped"
    )
    assert [item["action"] for item in after_boot["commands"]] == [
        "restart_mining"
    ]
    rows = command_rows(client, admin_token, agent_id)
    stale = next(item for item in rows if item["id"] == one_shot.json()["id"])
    assert stale["status"] == "expired"
    assert "will not be replayed" in stale["error"]


def test_autostart_result_is_idempotent_after_running_heartbeat_advances_marker(
    controller,
):
    client, settings = controller
    admin_token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, admin_token, name="autostart-result-order")
    boot_a = "2026-07-29T00:00:00+00:00"
    boot_b = "2026-07-29T01:00:00+00:00"
    heartbeat(
        client, agent_id, secret, boot_time=boot_a, mining_state="stopped"
    )
    client.patch(
        f"/api/agents/{agent_id}/mining-autostart",
        headers=auth(admin_token),
        json={"enabled": True},
    )
    queued = heartbeat(
        client, agent_id, secret, boot_time=boot_b, mining_state="stopped"
    )["commands"][0]

    # The process can become visible in telemetry before its result POST.
    heartbeat(
        client, agent_id, secret, boot_time=boot_b, mining_state="running"
    )
    completed = client.post(
        f"/api/agent/commands/{queued['id']}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={"status": "succeeded", "result": {"restarted": True}, "error": None},
    )
    assert completed.status_code == 200, completed.text
    with sqlite3.connect(settings.database_path) as db:
        assert db.execute(
            """SELECT mining_autostart_last_boot_time FROM agents
               WHERE id=?""",
            (agent_id,),
        ).fetchone()[0] == boot_b


def test_actual_autostart_keeps_result_binding_and_only_bound_success_consumes_boot(controller):
    client, _settings = controller
    token = login(client, "admin", "correct-horse-battery-staple")
    agent_id, secret = enroll(client, token, name="autostart-bound-result")
    capabilities = [main_module.MINING_RESULT_BINDING_CAPABILITY]
    boot_a, boot_b = "2026-07-29T00:00:00+00:00", "2026-07-29T01:00:00+00:00"
    heartbeat(client, agent_id, secret, boot_time=boot_a, mining_state="stopped", capabilities=capabilities)
    assert client.patch(f"/api/agents/{agent_id}/mining-autostart", headers=auth(token), json={"enabled": True}).status_code == 200
    delivered = heartbeat(client, agent_id, secret, boot_time=boot_b, mining_state="stopped", capabilities=capabilities)["commands"]
    assert len(delivered) == 1 and delivered[0]["action"] == "restart_mining"
    assert delivered[0]["payload"] == {}  # Controller markers stay private.
    command_id = delivered[0]["id"]
    headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"}
    rejected = client.post(f"/api/agent/commands/{command_id}/result", headers=headers,
                           json={"status": "succeeded", "result": {}})
    assert rejected.status_code == 409, rejected.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT status FROM commands WHERE id=?", (command_id,)).fetchone()[0] == "dispatched"
        assert db.execute("SELECT mining_autostart_last_boot_time FROM agents WHERE id=?", (agent_id,)).fetchone()[0] == boot_a
        assert db.execute("SELECT COUNT(*) FROM commands WHERE agent_id=? AND action='apply_gpu_profile'", (agent_id,)).fetchone()[0] == 0
    accepted = client.post(f"/api/agent/commands/{command_id}/result", headers=headers,
                           json={"status": "succeeded", "result": {"managed": True, "flight_sheet_id": "sheet-1", "restarted": True}})
    assert accepted.status_code == 200, accepted.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT mining_autostart_last_boot_time FROM agents WHERE id=?", (agent_id,)).fetchone()[0] == boot_b
