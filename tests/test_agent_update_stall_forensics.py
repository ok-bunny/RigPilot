"""Agent-update UX reliability (Controller 0.8.44).

Covers the four 02-receipt follow-ups:

* R1 - flag replacements that reached two target heartbeats and then went
  silent as ``activation_stalled`` instead of a normal wait.
* R2 - activation confirmation trusts the authenticated target-heartbeat
  count even when the agents.version row still lags one beat behind.
* R3 - the 45-minute activation timeout queues a fixed, read-only forensic
  command that runs when the device reconnects; the action stays internal.
* R4 - the API exposes the stall and failure reasons the UI renders.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login, portable_zip

from server.config import Settings
from server.main import (
    AGENT_UPDATE_FORENSICS_ACTION,
    AGENT_UPDATE_FORENSICS_COMMANDS,
    AGENT_UPDATE_FORENSICS_TTL_SECONDS,
    create_app,
)

ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def release_app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        max_agent_release_bytes=64 * 1024,
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


def _drive_to_activating(
    client: TestClient,
    settings: Settings,
    *,
    target_version: str = "0.5.7",
    source_version: str = "0.5.6",
    target_heartbeats: int = 2,
) -> tuple[str, str, str, str]:
    """Upload a release, dispatch it, reach status=activating, count beats."""

    token = login(client, "admin", ADMIN_PASSWORD)
    release = client.post(
        "/api/agent-releases/upload",
        headers={
            **auth(token),
            "Content-Type": "application/zip",
        },
        params={
            "version": target_version,
            "platform": "windows",
            "architecture": "x86_64",
            "file_name": (
                f"RigPilotAgent-{target_version}-Windows-x64-Portable.zip"
            ),
            "notes": "stall/forensics coverage release",
        },
        content=portable_zip(version=target_version),
    )
    assert release.status_code == 201, release.text
    release_id = release.json()["id"]
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": client.post(
                "/api/enrollment-tokens",
                headers=auth(token),
                json={"label": "stall-canary", "ttl_minutes": 30},
            ).json()["token"],
            "name": "stall-canary",
            "hostname": "stall-canary.local",
            "os_family": "windows",
            "platform": "Windows 10 x64",
            "agent_version": source_version,
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    agent_id = enrolled.json()["agent_id"]
    secret = enrolled.json()["agent_secret"]
    headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"}

    def heartbeat(version: str):
        response = client.post(
            "/api/agent/heartbeat",
            headers=headers,
            json={
                "hostname": "stall-canary.local",
                "os_family": "windows",
                "platform": "Windows 10 x64",
                "agent_version": version,
                "capabilities": ["agent_update.v1"],
                "inventory": {"architecture": "AMD64"},
            },
        )
        assert response.status_code == 200, response.text
        return response

    heartbeat(source_version)
    queued = client.post(
        f"/api/agent-releases/{release_id}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["command_id"]
    dispatched = heartbeat(source_version).json()["commands"]
    assert any(row["id"] == command_id for row in dispatched)

    claimed = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=headers,
        json={"status": "running", "result": {"stage": "downloading"}},
    )
    assert claimed.status_code == 200, claimed.text
    staged = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=headers,
        json={
            "status": "succeeded",
            "result": {
                "state": "staged",
                "release_id": release_id,
                "version": target_version,
                "sha256": release.json()["sha256"],
                "activation_id": "b" * 32,
                "activation_scheduled": True,
                "restart_required": True,
                "miner_continuity_checked": True,
            },
        },
    )
    assert staged.status_code == 200, staged.text
    for _ in range(target_heartbeats):
        heartbeat(target_version)
    return token, agent_id, command_id, secret


def _backdate(
    settings: Settings,
    command_id: str,
    *,
    minutes: int,
    column: str = "target_heartbeat_at",
) -> None:
    stamp = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat(timespec="microseconds")
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            f"UPDATE agent_update_jobs SET {column}=? WHERE command_id=?",
            (stamp, command_id),
        )


def _job_row(settings: Settings, command_id: str) -> sqlite3.Row:
    with sqlite3.connect(settings.database_path) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM agent_update_jobs WHERE command_id=?", (command_id,)
        ).fetchone()


def _commands(settings: Settings, action: str) -> list[sqlite3.Row]:
    with sqlite3.connect(settings.database_path) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM commands WHERE action=? ORDER BY created_at", (action,)
        ).fetchall()


def _audit_actions(client: TestClient, token: str) -> list[str]:
    return [
        item["action"]
        for item in client.get(
            "/api/audit?limit=200", headers=auth(token)
        ).json()
    ]


def test_stall_is_flagged_once_after_target_heartbeats_go_silent(release_app):
    client, settings = release_app
    token, agent_id, command_id, _secret = _drive_to_activating(client, settings)

    # Fresh target heartbeats never look stalled.
    fresh = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert fresh["status"] == "activating"
    assert fresh["activation_stalled"] is False
    assert fresh["stalled_at"] is None
    assert fresh["target_heartbeat_at"] is not None

    _backdate(settings, command_id, minutes=6)
    stalled = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert stalled["status"] == "activating"
    assert stalled["activation_stalled"] is True
    assert stalled["stalled_at"] is not None
    assert "agent_update_replacement_stalled" in _audit_actions(client, token)

    details = client.get(
        "/api/audit?limit=200", headers=auth(token)
    ).json()
    stall_entries = [
        item
        for item in details
        if item["action"] == "agent_update_replacement_stalled"
    ]
    assert len(stall_entries) == 1
    assert stall_entries[0]["details"]["target_heartbeat_count"] == 2
    assert "rolled back or dead" in stall_entries[0]["details"]["reason"]

    # A repeated sweep must not duplicate the audit event.
    client.get("/api/agent-updates", headers=auth(token))
    assert (
        len(
            [
                item
                for item in client.get(
                    "/api/audit?limit=200", headers=auth(token)
                ).json()
                if item["action"] == "agent_update_replacement_stalled"
            ]
        )
        == 1
    )


def test_stall_requires_two_target_heartbeats_and_skips_workspace_phase(
    release_app,
):
    client, settings = release_app

    # Fewer than two authenticated target heartbeats: no stall flag.
    token, agent_id, command_id, _secret = _drive_to_activating(
        client, settings, target_heartbeats=0
    )
    _backdate(settings, command_id, minutes=6)
    row = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert row["activation_stalled"] is False
    assert "agent_update_replacement_stalled" not in _audit_actions(
        client, token
    )

    # Post-confirmation workspace policy phase: the activation was already
    # confirmed, so a silent target is not an unconfirmed replacement stall.
    token, agent_id, command_id, _secret = _drive_to_activating(
        client, settings, target_version="0.5.8"
    )
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            """UPDATE agent_update_jobs
               SET activate_remote_workspace=1, post_update_command_id=?,
                   target_heartbeat_at='2000-01-01T00:00:00+00:00'
               WHERE command_id=?""",
            (command_id, command_id),
        )
    row = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert row["activation_stalled"] is False
    assert "agent_update_replacement_stalled" not in _audit_actions(
        client, token
    )


def test_activation_confirmation_accepts_version_row_lag(release_app):
    client, settings = release_app
    token, agent_id, command_id, secret = _drive_to_activating(client, settings)

    # Simulate a late in-flight heartbeat of the pre-update process winning
    # the agents row: the version column lags behind the counted heartbeats.
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "UPDATE agents SET agent_version='0.5.6' WHERE id=?", (agent_id,)
        )

    confirmed = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={
            "status": "succeeded",
            "result": {
                "activation_confirmed": True,
                "activation_id": "b" * 32,
                "version": "0.5.7",
                "health_heartbeats": 2,
            },
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"status": "succeeded"}
    finished = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert finished["status"] == "succeeded"
    assert finished["completed_at"] is not None


def test_activation_timeout_queues_fixed_read_only_forensics(release_app):
    client, settings = release_app
    token, agent_id, command_id, secret = _drive_to_activating(client, settings)

    _backdate(
        settings, command_id, minutes=50, column="updated_at"
    )
    expired = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert expired["status"] == "failed"
    assert "45 minutes" in expired["error"]

    forensic_rows = _commands(settings, AGENT_UPDATE_FORENSICS_ACTION)
    assert len(forensic_rows) == 1
    row = forensic_rows[0]
    assert row["agent_id"] == agent_id
    assert row["status"] == "queued"
    job_row = _job_row(settings, command_id)
    assert row["created_by"] == job_row["created_by"]
    payload = json.loads(row["payload_json"])
    marker = payload["_controller_auto_forensics"]
    assert marker["commands"] == list(AGENT_UPDATE_FORENSICS_COMMANDS)
    assert marker["trigger"] == "agent_update_activation_timeout"
    assert marker["command_id"] == command_id
    assert set(payload) == {"_controller_auto_forensics"}
    created = datetime.fromisoformat(row["created_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    assert 0 < (expires - created).total_seconds() <= (
        AGENT_UPDATE_FORENSICS_TTL_SECONDS
    )
    assert "agent_update_forensics_queued" in _audit_actions(client, token)

    # Repeated sweeps never stack forensic commands for one device.
    client.get("/api/agent-updates", headers=auth(token))
    assert (
        len(_commands(settings, AGENT_UPDATE_FORENSICS_ACTION)) == 1
    )

    # The offline device reconnects: the forensic command is delivered on
    # the next authenticated heartbeat, and a legacy Agent that does not
    # know the action simply reports a rejected (failed) command.
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "hostname": "stall-canary.local",
            "os_family": "windows",
            "platform": "Windows 10 x64",
            "agent_version": "0.5.6",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    delivered = [
        row for row in heartbeat.json()["commands"]
        if row["action"] == AGENT_UPDATE_FORENSICS_ACTION
    ]
    assert len(delivered) == 1
    forensic_id = delivered[0]["id"]

    rejected = client.post(
        f"/api/agent/commands/{forensic_id}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "status": "failed",
            "result": {"rejected": True},
            "error": "unknown command action",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json() == {"status": "failed"}


def test_forensics_action_is_not_publicly_dispatchable(release_app):
    client, settings = release_app
    token, agent_id, command_id, _secret = _drive_to_activating(
        client, settings, target_heartbeats=0
    )
    del command_id
    response = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": AGENT_UPDATE_FORENSICS_ACTION, "payload": {}},
    )
    assert response.status_code == 422, response.text
    assert not _commands(settings, AGENT_UPDATE_FORENSICS_ACTION)
