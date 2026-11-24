from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login, create_user as _create_user

from server.config import Settings
from server import main as server_main
from server.main import create_app


ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def app_client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="0.5.9-backend-tests-secret-key-that-is-long-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


def login(client: TestClient, username: str = "admin", password: str = ADMIN_PASSWORD) -> str:
    return _login(client, username, password)


def create_user(client: TestClient, admin_token: str, username: str, role: str) -> str:
    return _create_user(client, admin_token, username, role)


def enroll(client: TestClient, admin_token: str, name: str) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(admin_token),
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
            "agent_version": "0.5.9",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def heartbeat(
    client: TestClient,
    agent_id: str,
    agent_secret: str,
    mining_status: dict[str, object],
    *,
    metrics: dict[str, object] | None = None,
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
            "agent_version": "0.5.9",
            "ip_addresses": [],
            "metrics": metrics or {},
            "inventory": {"mining_status": mining_status},
        },
    )


def insert_command(
    db: sqlite3.Connection,
    *,
    command_id: str,
    agent_id: str,
    user_id: int,
    action: str,
    command_status: str,
) -> None:
    now = datetime.now(timezone.utc)
    db.execute(
        """INSERT INTO commands
           (id,agent_id,action,payload_json,status,created_by,created_at,
            expires_at,finished_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            command_id,
            agent_id,
            action,
            "{}",
            command_status,
            user_id,
            now.isoformat(timespec="seconds"),
            (now + timedelta(hours=1)).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds")
            if command_status in {"succeeded", "failed", "cancelled", "expired"}
            else None,
        ),
    )


def test_deleted_users_are_removed_reusable_and_old_tokens_stop_working(app_client):
    client, _settings = app_client
    admin_token = login(client)
    old_token = create_user(client, admin_token, "fixture-viewer", "viewer")
    users = client.get("/api/users", headers=auth(admin_token)).json()
    retired_viewer_id = next(user["id"] for user in users if user["username"] == "fixture-viewer")

    deleted = client.delete(f"/api/users/{retired_viewer_id}", headers=auth(admin_token))
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}
    assert client.get("/api/auth/me", headers=auth(old_token)).status_code == 401
    assert all(
        user["username"] != "fixture-viewer"
        for user in client.get("/api/users", headers=auth(admin_token)).json()
    )
    including_disabled = client.get(
        "/api/users?include_disabled=true", headers=auth(admin_token)
    ).json()
    assert all(user["username"] != "fixture-viewer" for user in including_disabled)
    assert any(
        item["action"] == "user_deleted" and item["target_id"] == str(retired_viewer_id)
        for item in client.get("/api/audit", headers=auth(admin_token)).json()
    )

    recreated = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={
            "username": "fixture-viewer",
            "password": "replacement-password-for-fixture-viewer",
            "role": "viewer",
        },
    )
    assert recreated.status_code == 201, recreated.text
    assert login(client, "fixture-viewer", "replacement-password-for-fixture-viewer")


def test_historical_inactive_admin_can_be_deleted_with_one_active_admin(app_client):
    client, _settings = app_client
    admin_token = login(client)
    create_user(client, admin_token, "old-admin", "admin")
    users = client.get("/api/users", headers=auth(admin_token)).json()
    old_admin_id = next(user["id"] for user in users if user["username"] == "old-admin")
    with client.app.state.db.connection() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (old_admin_id,))

    deleted = client.delete(
        f"/api/users/{old_admin_id}", headers=auth(admin_token)
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted"}


def test_operations_history_endpoint_is_retired(app_client):
    """0.8.50 removed the UI-less bulk history/audit wipe endpoint."""

    client, _settings = app_client
    admin_token = login(client)
    operator_token = create_user(client, admin_token, "cleanup-operator", "operator")
    assert (
        client.delete("/api/operations/history", headers=auth(admin_token)).status_code
        == 404
    )
    assert (
        client.delete("/api/operations/history", headers=auth(operator_token)).status_code
        == 404
    )


def test_agent_update_cleanup_cascades_only_terminal_update_jobs(app_client):
    client, _settings = app_client
    admin_token = login(client)
    viewer_token = create_user(client, admin_token, "update-viewer", "viewer")
    terminal_agent, _ = enroll(client, admin_token, "updated-rig")
    active_agent, _ = enroll(client, admin_token, "updating-rig")
    database = client.app.state.db
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with database.connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.execute(
            """INSERT INTO agent_releases
               (id,version,platform,architecture,file_name,storage_key,file_size,
                sha256,update_protocol,notes,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "release-059",
                "0.5.9",
                "windows",
                "x86_64",
                "agent-059.zip",
                "agent-releases/release-059.zip",
                100,
                "a" * 64,
                "agent_update.v1",
                "test",
                user_id,
                now,
            ),
        )
        for command_id, agent_id, status in (
            ("finished-update", terminal_agent, "succeeded"),
            ("active-update", active_agent, "queued"),
        ):
            insert_command(
                db,
                command_id=command_id,
                agent_id=agent_id,
                user_id=user_id,
                action="agent_update.v1",
                command_status=status,
            )
            db.execute(
                """INSERT INTO agent_update_jobs
                   (command_id,agent_id,release_id,source_version,target_version,
                    status,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    command_id,
                    agent_id,
                    "release-059",
                    "0.5.8",
                    "0.5.9",
                    status,
                    user_id,
                    now,
                    now,
                ),
            )
        insert_command(
            db,
            command_id="ordinary-finished",
            agent_id=terminal_agent,
            user_id=user_id,
            action="collect_diagnostics",
            command_status="failed",
        )

    assert (
        client.delete("/api/agent-updates/history", headers=auth(viewer_token)).status_code
        == 403
    )
    response = client.delete(
        "/api/agent-updates/history", headers=auth(admin_token)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "cleared",
        "update_jobs_deleted": 1,
        "commands_deleted": 1,
    }
    with database.connection() as db:
        commands = {row["id"] for row in db.execute("SELECT id FROM commands")}
        jobs = {
            row["command_id"]
            for row in db.execute("SELECT command_id FROM agent_update_jobs")
        }
    assert "finished-update" not in commands
    assert "finished-update" not in jobs
    assert {"active-update", "ordinary-finished"} <= commands
    assert jobs == {"active-update"}
    assert any(
        item["action"] == "agent_update_history_cleared"
        and item["details"]["active_updates_preserved"] is True
        for item in client.get("/api/audit", headers=auth(admin_token)).json()
    )


def test_heartbeat_stores_per_series_hashrates_and_fails_closed(app_client):
    client, _settings = app_client
    admin_token = login(client)
    agent_id, secret = enroll(client, admin_token, "hashrate-rig")
    database = client.app.state.db
    sampled_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    running = {
        "configured": True,
        "state": "running",
        # Legacy Agents do not report process_verified.  They remain
        # compatible only when a live miner API refreshed stats in the same
        # collection window.
        "checked_at": sampled_at,
        "stats_updated_at": sampled_at,
        "primary": {"coin": "XMR", "algorithm": "randomx"},
        "secondary": {"coin": "PRL", "algorithm": "pearlhash"},
        "hashrate_details": [
            {
                "role": "primary",
                "coin": "XMR",
                # Some XMRig releases omitted this field.  The controller
                # derives RandomX from XMR before storing chart telemetry.
                "algorithm": "unknown",
                "hashrate": 1.25,
                "hashrate_unit": "kH/s",
            },
            {
                "role": "secondary",
                "coin": "PRL",
                "algorithm": "pearlhash",
                "hashrate": 2_000,
                "hashrate_unit": "H/s",
            },
        ],
    }
    response = heartbeat(
        client,
        agent_id,
        secret,
        running,
        metrics={
            "hashrate_samples": [
                {"role": "primary", "coin": "FAKE", "hashrate": 999_999}
            ]
        },
    )
    assert response.status_code == 200, response.text
    with database.connection() as db:
        metrics = json.loads(
            db.execute(
                "SELECT metrics_json FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()["metrics_json"]
        )
    assert metrics["mining_state"] == "running"
    assert {
        (sample["role"], sample["coin"], sample["algorithm"], sample["hashrate"])
        for sample in metrics["hashrate_samples"]
    } == {
        ("primary", "XMR", "randomx", 1250.0),
        ("secondary", "PRL", "pearlhash", 2000.0),
    }

    degraded = {
        **running,
        "state": "stale",
        "process_verified": False,
        "process_error": "secondary: managed runtime is unavailable",
        "error": "secondary: managed runtime is unavailable",
        "degraded": True,
        "running_roles": ["primary"],
        "role_errors": {"secondary": "managed runtime is unavailable"},
        "hashrate": {"primary": 1.5},
        "hashrate_unit": "kH/s",
        "hashrate_details": [
            {**running["hashrate_details"][0], "hashrate": 1.5}
        ],
    }
    assert heartbeat(client, agent_id, secret, degraded).status_code == 200
    with database.connection() as db:
        degraded_metrics = json.loads(
            db.execute(
                "SELECT metrics_json FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()["metrics_json"]
        )
    assert degraded_metrics["mining_state"] == "degraded"
    assert {
        (sample["role"], sample["coin"], sample["algorithm"], sample["hashrate"])
        for sample in degraded_metrics["hashrate_samples"]
    } == {
        ("primary", "XMR", "randomx", 1500.0),
        ("secondary", "PRL", "pearlhash", 0.0),
    }

    stopped = {
        "configured": True,
        "state": "stopped",
        "primary": {"coin": "XMR", "algorithm": "randomx"},
        "secondary": {"coin": "PRL", "algorithm": "pearlhash"},
        "hashrate": None,
        "hashrate_details": None,
    }
    assert heartbeat(client, agent_id, secret, stopped).status_code == 200
    with database.connection() as db:
        stopped_metrics = json.loads(
            db.execute(
                "SELECT metrics_json FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()["metrics_json"]
        )
    assert {sample["hashrate"] for sample in stopped_metrics["hashrate_samples"]} == {0.0}
    assert {sample["role"] for sample in stopped_metrics["hashrate_samples"]} == {
        "primary",
        "secondary",
    }

    stale_error = {**running, "error": "local srbminer API is unavailable"}
    assert heartbeat(client, agent_id, secret, stale_error).status_code == 200
    unverified = {**running, "error": None, "process_verified": False}
    assert heartbeat(client, agent_id, secret, unverified).status_code == 200
    with database.connection() as db:
        last_two = db.execute(
            "SELECT metrics_json FROM telemetry ORDER BY id DESC LIMIT 2"
        ).fetchall()
    assert all(json.loads(row["metrics_json"])["hashrate_samples"] == [] for row in last_two)

    legacy = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "hostname": "legacy.local",
            "os_family": "windows",
            "platform": "Windows 10",
            "agent_version": "0.5.6",
            "ip_addresses": [],
            "metrics": {"mining_status": running},
            "inventory": {},
        },
    )
    assert legacy.status_code == 200, legacy.text
    with database.connection() as db:
        legacy_metrics = json.loads(
            db.execute(
                "SELECT metrics_json FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()["metrics_json"]
        )
    assert len(legacy_metrics["hashrate_samples"]) == 2

    legacy_stale = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "hostname": "legacy.local",
            "os_family": "windows",
            "platform": "Windows 10",
            "agent_version": "0.5.6",
            "ip_addresses": [],
            "metrics": {},
            "inventory": {
                "mining_status": {
                    **running,
                    "stats_updated_at": "2026-07-15T23:55:00+00:00",
                }
            },
        },
    )
    assert legacy_stale.status_code == 200, legacy_stale.text
    with database.connection() as db:
        stale_metrics = json.loads(
            db.execute(
                "SELECT metrics_json FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()["metrics_json"]
        )
    assert stale_metrics["hashrate_samples"] == []

    legacy_old_block = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "hostname": "legacy.local",
            "os_family": "windows",
            "platform": "Windows 10",
            "agent_version": "0.5.6",
            "ip_addresses": [],
            "metrics": {},
            "inventory": {
                "mining_status": {
                    **running,
                    "checked_at": "2026-07-15T23:50:00+00:00",
                    "stats_updated_at": "2026-07-15T23:50:00+00:00",
                }
            },
        },
    )
    assert legacy_old_block.status_code == 200, legacy_old_block.text
    with database.connection() as db:
        old_block_metrics = json.loads(
            db.execute(
                "SELECT metrics_json FROM telemetry ORDER BY id DESC LIMIT 1"
            ).fetchone()["metrics_json"]
        )
    assert old_block_metrics["hashrate_samples"] == []


def telemetry_metrics(samples: list[dict[str, object]], state: str = "running") -> str:
    return json.dumps(
        {"mining_state": state, "hashrate_samples": samples},
        separators=(",", ":"),
    )


def sample(role: str, coin: str, algorithm: str, value: float) -> dict[str, object]:
    return {
        "series": "ignored-and-recomputed",
        "role": role,
        "coin": coin,
        "algorithm": algorithm,
        "hashrate": value,
        "unit": "H/s",
    }


def test_hashrate_history_uses_last_heartbeat_and_merges_roles_by_coin(
    app_client,
):
    client, _settings = app_client
    admin_token = login(client)
    viewer_token = create_user(client, admin_token, "chart-viewer", "viewer")
    first_agent, _ = enroll(client, admin_token, "chart-a")
    second_agent, _ = enroll(client, admin_token, "chart-b")
    database = client.app.state.db
    now = datetime.now(timezone.utc)
    bucket = now.replace(second=0, microsecond=0) - timedelta(minutes=10)
    rows = (
        (
            first_agent,
            bucket + timedelta(seconds=10),
            telemetry_metrics(
                [
                    sample("primary", "XMR", "randomx", 100),
                    sample("secondary", "XMR", "randomx", 25),
                    sample("secondary", "PRL", "pearlhash", 1_000),
                ]
            ),
        ),
        (
            first_agent,
            bucket + timedelta(seconds=20),
            telemetry_metrics(
                [
                    sample("primary", "XMR", "randomx", 150),
                    sample("secondary", "XMR", "randomx", 25),
                    sample("secondary", "PRL", "pearlhash", 1_100),
                ]
            ),
        ),
        (
            second_agent,
            bucket + timedelta(seconds=15),
            # Older XMRig telemetry omitted the algorithm.  It must join the
            # same XMR / randomx series without rewriting the stored row.
            telemetry_metrics([sample("primary", "XMR", "unknown", 200)]),
        ),
    )
    with database.connection() as db:
        for agent_id, recorded_at, metrics_json in rows:
            db.execute(
                "INSERT INTO telemetry(agent_id,recorded_at,metrics_json) VALUES(?,?,?)",
                (agent_id, recorded_at.isoformat(timespec="seconds"), metrics_json),
            )

    response = client.get(
        "/api/hashrate-history?hours=24&bucket_minutes=5",
        headers=auth(viewer_token),
    )
    assert response.status_code == 200, response.text
    history = response.json()
    assert history["hours"] == 24
    assert history["bucket_minutes"] == 5
    by_identity = {
        (series["coin"], series["algorithm"]): series
        for series in history["series"]
    }
    assert all("role" not in series for series in history["series"])
    xmr = by_identity[("XMR", "randomx")]
    prl = by_identity[("PRL", "pearlhash")]
    assert xmr["points"] == [
        {
            "timestamp": xmr["points"][0]["timestamp"],
            "value": 375,
            "agent_count": 2,
        }
    ]
    assert prl["points"][0]["value"] == 1_100
    assert prl["points"][0]["agent_count"] == 1

    # A later unknown heartbeat replaces the earlier row for that device and
    # bucket.  Its old rate must not be retained in the aggregate.
    with database.connection() as db:
        db.execute(
            "INSERT INTO telemetry(agent_id,recorded_at,metrics_json) VALUES(?,?,?)",
            (
                first_agent,
                (bucket + timedelta(seconds=30)).isoformat(timespec="seconds"),
                telemetry_metrics([], "unknown"),
            ),
        )
    refreshed = client.get(
        "/api/hashrate-history?hours=24&bucket_minutes=5",
        headers=auth(admin_token),
    ).json()
    refreshed_xmr = next(
        series
        for series in refreshed["series"]
        if (series["coin"], series["algorithm"])
        == ("XMR", "randomx")
    )
    assert refreshed_xmr["points"][0]["value"] == 200
    assert refreshed_xmr["points"][0]["agent_count"] == 1
    assert client.get("/api/hashrate-history").status_code == 401
    assert (
        client.get("/api/hashrate-history?hours=0", headers=auth(admin_token)).status_code
        == 422
    )
    assert (
        client.get("/api/hashrate-history?hours=169", headers=auth(admin_token)).status_code
        == 422
    )
    assert (
        client.get(
            "/api/hashrate-history?bucket_minutes=0", headers=auth(admin_token)
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/hashrate-history?bucket_minutes=1", headers=auth(admin_token)
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/hashrate-history?bucket_minutes=61", headers=auth(admin_token)
        ).status_code
        == 422
    )


def test_hashrate_history_survives_raw_telemetry_pruning(app_client):
    client, _settings = app_client
    admin_token = login(client)
    agent_id, secret = enroll(client, admin_token, "compact-history")
    sampled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    response = heartbeat(
        client,
        agent_id,
        secret,
        {
            "configured": True,
            "state": "running",
            "checked_at": sampled_at,
            "stats_updated_at": sampled_at,
            "primary": {"coin": "PRL", "algorithm": "pearlhash"},
            "hashrate_details": [
                {
                    "role": "primary",
                    "coin": "PRL",
                    "algorithm": "pearlhash",
                    "hashrate": 12.5,
                    "hashrate_unit": "TH/s",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text

    database = client.app.state.db
    with database.connection() as db:
        compact = db.execute(
            """SELECT recorded_at,samples_json
               FROM hashrate_history_buckets WHERE agent_id=?""",
            (agent_id,),
        ).fetchall()
        assert len(compact) == 1
        assert len(json.loads(compact[0]["samples_json"])) == 1
        db.execute("DELETE FROM telemetry WHERE agent_id=?", (agent_id,))

    history = client.get(
        "/api/hashrate-history?hours=24&bucket_minutes=5",
        headers=auth(admin_token),
    )
    assert history.status_code == 200, history.text
    pearl = next(
        item
        for item in history.json()["series"]
        if (item["coin"], item["algorithm"]) == ("PRL", "pearlhash")
    )
    assert pearl["points"][-1]["value"] == pytest.approx(12.5e12)
    assert pearl["points"][-1]["agent_count"] == 1


def _current_srb_role_status(now: datetime, *, dual: bool, gpu_role: str = "primary") -> dict:
    """Synthetic Agent observation; no claim about native SRB API field meanings."""
    sampled = now.isoformat()
    gpu = {"miner_type": "srbminer", "coin": "PEARL", "algorithm": "pearlhash"}
    cpu = {"miner_type": "xmrig", "coin": "XMR", "algorithm": "randomx"}
    roles = {"primary": gpu}
    if dual:
        roles = {"primary": cpu, "secondary": cpu, gpu_role: gpu}
    rates = {role: 100.0 if leg is gpu else 10.0 for role, leg in roles.items()}
    return {
        "configured": True,
        "flight_sheet_id": "synthetic-srb-reconnect-sheet",
        "state": "running",
        "miner_type": roles["primary"]["miner_type"],
        "secondary_mode": "independent" if dual else None,
        "process_verified": True,
        "process_error": None,
        "error": None,
        "checked_at": sampled,
        "stats_state": "fresh",
        "stats_error": None,
        "stats_updated_at": sampled,
        "hashrate_unit": "H/s",
        "hashrate": rates,
        "shares": {"accepted": 500, "rejected": 0},
        **roles,
        "hashrate_details": [
            {"role": role, **leg, "hashrate": rates[role], "hashrate_unit": "H/s",
             "stats_updated_at": sampled, "shares": {"accepted": 500, "rejected": 0}}
            for role, leg in roles.items()
        ],
    }


@pytest.mark.parametrize("case,dual,gpu_role,expected", [
    ("positive", False, "primary", 1),
    ("tiny_positive", False, "primary", 1),
    ("zero", False, "primary", 0),
    ("null", False, "primary", 0),
    ("blank", False, "primary", 0),
    ("boolean", False, "primary", 0),
    ("false_boolean", False, "primary", 0),
    ("positive", True, "primary", 1),
    ("zero", True, "primary", 0),
    ("zero", True, "secondary", 0),
    ("missing_role", True, "secondary", 0),
    ("stale_role", True, "primary", 0),
    ("null_role_time", True, "secondary", 0),
    ("legacy_role_time_absent", True, "primary", 1),
    ("legacy_agent", False, "primary", 1),
    ("top_stats_before_boot", False, "primary", 0),
    ("role_stats_before_boot", True, "secondary", 0),
    ("warming_up", False, "primary", 0),
    ("average_and_shares_only", False, "primary", 0),
])
def test_normal_mining_requires_current_positive_rate_for_each_configured_role(
    app_client, case, dual, gpu_role, expected,
):
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "srb-role-summary")
    now = datetime.now(timezone.utc)
    boot = now - timedelta(hours=1)
    mining = _current_srb_role_status(now, dual=dual, gpu_role=gpu_role)
    gpu_detail = next(detail for detail in mining["hashrate_details"] if detail["role"] == gpu_role)
    if case in {"tiny_positive", "zero", "null", "blank", "boolean", "false_boolean"}:
        value = {"tiny_positive": 0.01, "zero": 0, "null": None, "blank": "", "boolean": True, "false_boolean": False}[case]
        gpu_detail["hashrate"] = value
        mining["hashrate"][gpu_role] = value
    elif case == "missing_role":
        mining["hashrate_details"].remove(gpu_detail)
        # A healthy CPU detail must not let the aggregate fill in a missing
        # configured GPU leg when the Agent is already reporting per-leg data.
    elif case == "stale_role":
        gpu_detail["stats_updated_at"] = (now - timedelta(minutes=2)).isoformat()
    elif case == "null_role_time":
        gpu_detail["stats_updated_at"] = None
    elif case in {"legacy_role_time_absent", "legacy_agent"}:
        for detail in mining["hashrate_details"]:
            detail.pop("stats_updated_at")
        if case == "legacy_agent":
            mining.pop("process_verified")
            mining.pop("stats_state")
    elif case in {"top_stats_before_boot", "role_stats_before_boot"}:
        boot = now - timedelta(seconds=1)
        key = mining if case == "top_stats_before_boot" else gpu_detail
        key["stats_updated_at"] = (now - timedelta(seconds=2)).isoformat()
    elif case == "warming_up":
        mining["stats_state"] = "warming_up"
        mining["stats_updated_at"] = None
    elif case == "average_and_shares_only":
        mining["hashrate"][gpu_role] = 0.0
        gpu_detail["hashrate"] = 0.0
        gpu_detail["hashrate_average"] = 100.0
        mining["hashrate_average"] = 100.0

    response = heartbeat(client, agent_id, secret, mining, metrics={"boot_time": boot.isoformat()})
    assert response.status_code == 200, response.text
    agent_response = client.get(f"/api/agents/{agent_id}", headers=auth(token))
    assert agent_response.status_code == 200, agent_response.text
    agent = agent_response.json()
    if case in {"null", "blank", "boolean", "false_boolean"}:
        assert agent["metrics"]["hashrate_samples"] == []
    # Activity summary must not weaken process ownership or imply restart.
    assert server_main._active_serialized_agent_flight_sheet_id(agent) == mining["flight_sheet_id"]
    summary = client.get("/api/summary", headers=auth(token))
    assert summary.status_code == 200, summary.text
    assert summary.json()["normal_mining"] == expected


@pytest.mark.parametrize("gpu_role", ["primary", "secondary"])
@pytest.mark.parametrize("sample_state", ["stale", "null_time", "missing", "fresh_zero"])
def test_running_role_history_cannot_replay_old_gpu_rate_through_parent_fallback(
    app_client, gpu_role, sample_state,
):
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "srb-role-history")
    now = datetime.now(timezone.utc)
    boot = now - timedelta(hours=1)
    mining = _current_srb_role_status(now, dual=True, gpu_role=gpu_role)
    assert heartbeat(client, agent_id, secret, mining, metrics={"boot_time": boot.isoformat()}).status_code == 200
    gpu_detail = next(detail for detail in mining["hashrate_details"] if detail["role"] == gpu_role)
    if sample_state == "stale":
        gpu_detail["stats_updated_at"] = (now - timedelta(minutes=2)).isoformat()
    elif sample_state == "null_time":
        gpu_detail["stats_updated_at"] = None
    elif sample_state == "missing":
        mining["hashrate_details"].remove(gpu_detail)
        mining["hashrate"][gpu_role] = 1234.0
    else:
        gpu_detail["hashrate"] = 0.0
        mining["hashrate"][gpu_role] = 0.0
    # Fresh top timestamps, an unchanged old top rate and cumulative shares
    # cannot make an old/null/missing GPU detail current. The CPU keeps its
    # independent fresh observation; missing GPU data is not synthetic 0.
    response = heartbeat(client, agent_id, secret, mining, metrics={"boot_time": boot.isoformat()})
    assert response.status_code == 200, response.text
    agent_response = client.get(f"/api/agents/{agent_id}", headers=auth(token))
    assert agent_response.status_code == 200, agent_response.text
    expected = {("XMR", "secondary" if gpu_role == "primary" else "primary"): 10.0}
    if sample_state == "fresh_zero":
        expected[("PEARL", gpu_role)] = 0.0
    samples = agent_response.json()["metrics"]["hashrate_samples"]
    assert {(sample["coin"], sample["role"]): sample["hashrate"] for sample in samples} == expected
    with client.app.state.db.connection() as db:
        latest = db.execute(
            "SELECT samples_json FROM hashrate_history_buckets WHERE agent_id=? ORDER BY recorded_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    stored = json.loads(latest["samples_json"])
    assert {(sample["coin"], sample["role"]): sample["hashrate"] for sample in stored} == expected


@pytest.mark.parametrize("top_hashrate", [100.0, {"primary": 100.0}, {"total": 100.0}])
def test_single_old_detail_cannot_be_revived_by_scalar_or_total_hashrate(app_client, top_hashrate):
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "srb-scalar-history")
    now = datetime.now(timezone.utc)
    mining = _current_srb_role_status(now, dual=False)
    mining["hashrate"] = top_hashrate
    mining["hashrate_details"][0]["stats_updated_at"] = (now - timedelta(minutes=2)).isoformat()
    response = heartbeat(client, agent_id, secret, mining)
    assert response.status_code == 200, response.text
    agent_response = client.get(f"/api/agents/{agent_id}", headers=auth(token))
    assert agent_response.status_code == 200, agent_response.text
    assert agent_response.json()["metrics"]["hashrate_samples"] == []
    assert client.get("/api/summary", headers=auth(token)).json()["normal_mining"] == 0


@pytest.mark.parametrize("case,expected", [
    ("same_role_positive", 1),
    ("same_role_zero", 0),
    ("same_role_missing", 0),
    ("same_role_stale", 0),
    ("unconfigured_secondary_zero", 0),
    ("algorithm_label_only", 0),
    ("parent_secondary_zero", 0),
    ("parent_only_positive", 1),
    ("parent_only_zero", 0),
])
def test_normal_mining_does_not_hide_an_inactive_algorithm_or_explicit_role(app_client, case, expected):
    client, _settings = app_client
    token = login(client)
    agent_id, secret = enroll(client, token, "srb-multi-algorithm-summary")
    now = datetime.now(timezone.utc)
    mining = _current_srb_role_status(now, dual=False)
    if case.startswith("same_role"):
        other = {**mining["hashrate_details"][0], "coin": "SYNTH", "algorithm": "synthetic-second", "hashrate": 50.0}
        if case == "same_role_zero":
            other["hashrate"] = 0.0
        elif case == "same_role_missing":
            other.pop("hashrate")
        elif case == "same_role_stale":
            other["stats_updated_at"] = (now - timedelta(minutes=2)).isoformat()
        mining["hashrate_details"].append(other)
    elif case == "unconfigured_secondary_zero":
        mining["hashrate_details"].append({
            **mining["hashrate_details"][0], "role": "secondary", "hashrate": 0.0,
        })
    elif case == "algorithm_label_only":
        mining["hashrate_details"][0]["role"] = "algorithm_1"
        mining["hashrate"] = None
    elif case == "parent_secondary_zero":
        mining["hashrate_details"] = []
        mining["hashrate"]["secondary"] = 0.0
    elif case in {"parent_only_positive", "parent_only_zero"}:
        mining["hashrate_details"] = []
        mining["hashrate"] = 100.0 if case == "parent_only_positive" else 0.0
    response = heartbeat(client, agent_id, secret, mining)
    assert response.status_code == 200, response.text
    summary = client.get("/api/summary", headers=auth(token))
    assert summary.status_code == 200, summary.text
    assert summary.json()["normal_mining"] == expected
    if case == "parent_only_positive":
        # Pure legacy payloads without details still retain parent fallback
        # for current telemetry and persisted history.
        agent_response = client.get(f"/api/agents/{agent_id}", headers=auth(token))
        assert agent_response.status_code == 200, agent_response.text
        samples = agent_response.json()["metrics"]["hashrate_samples"]
        assert [(sample["role"], sample["hashrate"]) for sample in samples] == [("primary", 100.0)]
        with client.app.state.db.connection() as db:
            latest = db.execute(
                "SELECT samples_json FROM hashrate_history_buckets WHERE agent_id=? ORDER BY recorded_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        assert json.loads(latest["samples_json"]) == samples


@pytest.mark.parametrize("with_parent_clock", [False, True])
@pytest.mark.parametrize("detail_time", ["absent", "null", "old", "fresh"])
def test_legacy_history_keeps_parent_policy_but_respects_explicit_detail_timestamps(
    with_parent_clock, detail_time,
):
    now = datetime.now(timezone.utc)
    mining = _current_srb_role_status(now, dual=False)
    mining.pop("stats_state")
    if not with_parent_clock:
        mining.pop("checked_at")
        mining.pop("stats_updated_at")
    detail = mining["hashrate_details"][0]
    if detail_time == "absent":
        detail.pop("stats_updated_at")
    elif detail_time == "null":
        detail["stats_updated_at"] = None
    elif detail_time == "old":
        detail["stats_updated_at"] = (now - timedelta(minutes=2)).isoformat()
    state, samples = server_main._mining_hashrate_samples(
        {"mining_status": mining}, observed_at=now if with_parent_clock else None,
    )
    assert state == "running"
    expected = [100.0] if detail_time == "absent" or (detail_time == "fresh" and with_parent_clock) else []
    assert [sample["hashrate"] for sample in samples] == expected
