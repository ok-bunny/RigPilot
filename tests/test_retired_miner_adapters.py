"""Keep unsupported miner history readable without authorizing execution.

All records and queued commands are synthetic and exist only in pytest's DB.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import main as main_module
from server.config import Settings
from server.database import utcnow
from server.main import create_app
from server.managed_miners import ManagedMinerCatalog


HISTORICAL_UNSUPPORTED_MINERS = {
    "quantusminer": ("QUANTUS", "qpow", "1.3.0"),
    "lolminer": ("RVN", "kawpow", "1.98"),
    "forgeminer": ("PEARL", "pearlhash", "1.0.11"),
    "synthetic-unsupported-miner": ("XMR", "randomx", "1.0.0"),
}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def offline(*args, **kwargs):
        raise OSError("offline retired-miner fixture")

    monkeypatch.setattr(
        main_module, "ManagedMinerCatalog", lambda: ManagedMinerCatalog(fetcher=offline)
    )
    def forbidden_download(*args, **kwargs):
        pytest.fail("retired miner requests must be rejected before any package download")

    monkeypatch.setattr(main_module, "_download_https_package", forbidden_download)
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "retired.db",
        secret_key="retired-miner-test-secret-long-enough",
        admin_username="admin", admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as value:
        yield value


@pytest.fixture(params=tuple(HISTORICAL_UNSUPPORTED_MINERS))
def retired(client, request):
    miner_type = request.param
    coin, algorithm, version = HISTORICAL_UNSUPPORTED_MINERS[miner_type]
    package_id = f"builtin-{miner_type}-{version}-linux-x64"
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['token']}"}
    wallet = client.post(
        "/api/wallets", headers=auth,
        json={"name": "Historical public wallet", "coin": coin, "address": "synthetic-historical-wallet"},
    )
    assert wallet.status_code == 201, wallet.text
    body = {
        "name": "Synthetic historical flight", "miner_type": "generic",
        "coin": coin, "wallet_id": wallet.json()["id"],
        "pool_url": "stratum+tcp://pool.example.test:34567", "algorithm": algorithm,
        "pool_password": "", "worker_name_template": "{hostname}",
    }
    created = client.post("/api/flight-sheets", headers=auth, json=body)
    assert created.status_code == 201, created.text
    sheet_id = created.json()["id"]
    now = utcnow()
    with client.app.state.db.connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.execute(
            """INSERT INTO miner_packages
               (id,name,version,download_url,sha256,archive_type,
                managed_miner_type,managed_os_family,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (package_id, "Historical retired miner", version,
             "https://downloads.example.test/retired-miner.tar.gz",
             "a" * 64, "tar.gz", miner_type, "linux", user_id, now, now),
        )
        db.execute(
            """UPDATE flight_sheets
               SET miner_type=?,miner_version=?,miner_package_id=? WHERE id=?""",
            (miner_type, version, package_id, sheet_id),
        )
    issued = client.post(
        "/api/enrollment-tokens", headers=auth,
        json={"label": "retired-fixture", "ttl_minutes": 30},
    )
    assert issued.status_code == 200, issued.text
    enrolled = client.post(
        "/api/agent/enroll",
        json={"enrollment_token": issued.json()["token"], "name": "retired-fixture",
              "hostname": "retired-fixture", "os_family": "linux", "agent_version": "0.8.5"},
    )
    assert enrolled.status_code == 201, enrolled.text
    agent = enrolled.json()
    agent_headers = {
        "X-Agent-ID": agent["agent_id"],
        "Authorization": f"Bearer {agent['agent_secret']}",
    }
    heartbeat = {
        "hostname": "retired-fixture", "os_family": "linux", "platform": "linux",
        "agent_version": "0.8.5", "capabilities": [f"managed_mining.{miner_type}.v1"],
        "metrics": {"gpus": [{"index": 0, "vendor": "NVIDIA", "name": "NVIDIA Synthetic GPU", "device_type": "discrete"}]},
        "inventory": {},
    }
    accepted = client.post("/api/agent/heartbeat", headers=agent_headers, json=heartbeat)
    assert accepted.status_code == 200, accepted.text
    return {
        "miner_type": miner_type, "package_id": package_id,
        "auth": auth, "wallet": wallet.json(), "sheet_id": sheet_id, "body": body,
        "agent_id": agent["agent_id"], "agent_headers": agent_headers,
        "heartbeat": heartbeat, "user_id": user_id,
        "miner_type": miner_type, "package_id": package_id, "version": version,
    }


def insert_retired_command(client, retired, *, role="primary", status="queued"):
    retired_leg = {
        "miner_type": retired["miner_type"], "coin": retired["body"]["coin"],
        "algorithm": retired["body"]["algorithm"],
        "wallet_address": retired["wallet"]["address"],
        "pool_url": retired["body"]["pool_url"], "pool_password": "",
        "device_indices": [], "extra_config": {}, "miner_profile": "default",
        "miner_package": {
            "id": retired["package_id"], "name": "Historical retired miner", "version": retired["version"],
            "download_url": "https://downloads.example.test/retired-miner.tar.gz",
            "sha256": "a" * 64, "archive_type": "tar.gz",
        },
    }
    generic_leg = {**retired_leg, "miner_type": "generic", "miner_package": None}
    payload = {
        "flight_sheet_id": retired["sheet_id"], "name": "Historical queued flight",
        "worker_name": "retired-fixture",
        "primary": retired_leg if role == "primary" else generic_leg,
        "secondary": None if role == "primary" else retired_leg,
    }
    command_id = f"retired-{role}-{status}"
    now = utcnow()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    with client.app.state.db.connection() as db:
        db.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at,expires_at,dispatched_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (command_id, retired["agent_id"], "apply_flight_sheet", json.dumps(payload), status,
             retired["user_id"], now, future, now if status != "queued" else None),
        )
    return command_id, payload


def test_retired_flight_remains_readable_without_automatic_hiveos_migration(client, retired):
    response = client.get("/api/flight-sheets", headers=retired["auth"])
    assert response.status_code == 200, response.text
    sheet = next(row for row in response.json() if row["id"] == retired["sheet_id"])
    assert sheet["miner_type"] == retired["miner_type"]
    assert sheet["miner_version"] == retired["version"]
    assert sheet["miner_package_id"] == retired["package_id"]
    assert sheet["miner_package"]["id"] == retired["package_id"]
    assert sheet["wallet_id"] == retired["wallet"]["id"]
    assert sheet["coin"] == retired["wallet"]["coin"]
    with client.app.state.db.connection() as db:
        count_before = db.execute("SELECT count(*) FROM commands").fetchone()[0]
    applied = client.post(
        f"/api/agents/{retired['agent_id']}/flight-sheets/{retired['sheet_id']}/apply",
        headers=retired["auth"],
    )
    assert applied.status_code == 409, applied.text
    assert "不受支持的矿工类型" in applied.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == count_before
        persisted = db.execute(
            "SELECT miner_type,miner_package_id,wallet_id FROM flight_sheets WHERE id=?",
            (retired["sheet_id"],),
        ).fetchone()
    assert tuple(persisted) == (retired["miner_type"], retired["package_id"], retired["wallet"]["id"])


@pytest.mark.parametrize("endpoint", ["flight", "update", "upload", "import"])
def test_new_retired_flights_and_package_classifications_are_rejected(client, retired, endpoint):
    classification = {
        "name": "Retired dedicated registration", "version": retired["version"], "archive_type": "tar.gz",
        "managed_miner_type": retired["miner_type"], "managed_os_family": "linux", "managed_channel": "test",
    }
    if endpoint == "flight":
        response = client.post(
            "/api/flight-sheets", headers=retired["auth"],
            json={**retired["body"], "miner_type": retired["miner_type"], "miner_version": retired["version"]},
        )
    elif endpoint == "update":
        response = client.put(
            f"/api/flight-sheets/{retired['sheet_id']}", headers=retired["auth"],
            json={**retired["body"], "name": "Rename cannot revive retired miner",
                  "miner_type": retired["miner_type"], "miner_version": retired["version"]},
        )
    elif endpoint == "upload":
        response = client.post(
            "/api/miner-packages/upload",
            headers={**retired["auth"], "Content-Type": "application/octet-stream"},
            params={**classification, "filename": "retired.tar.gz"}, content=b"never executed",
        )
    else:
        response = client.post(
            "/api/miner-packages/import", headers=retired["auth"],
            json={**classification, "download_url": "https://downloads.example.test/retired.tar.gz"},
        )
    assert response.status_code == 422, response.text
    with client.app.state.db.connection() as db:
        stored = db.execute(
            "SELECT name,miner_type,miner_package_id FROM flight_sheets WHERE id=?",
            (retired["sheet_id"],),
        ).fetchone()
        assert tuple(stored) == (
            retired["body"]["name"], retired["miner_type"], retired["package_id"],
        )


@pytest.mark.parametrize("role", ["primary", "secondary"])
def test_queued_retired_miner_in_either_leg_is_failed_before_dispatch(client, retired, role):
    command_id, payload = insert_retired_command(client, retired, role=role)
    response = client.post(
        "/api/agent/heartbeat", headers=retired["agent_headers"], json=retired["heartbeat"]
    )
    assert response.status_code == 200, response.text
    assert not any(command["id"] == command_id for command in response.json()["commands"])
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
    assert stored["status"] == "failed"
    assert stored["error"] == "Unsupported or retired miner type; reconfigure the flight sheet"
    assert stored["finished_at"] is not None
    assert json.loads(stored["payload_json"]) == payload


@pytest.mark.parametrize("status", ["dispatched", "running"])
def test_retired_inflight_commands_are_not_resent_or_reclassified(client, retired, status):
    command_id, payload = insert_retired_command(client, retired, status=status)
    response = client.post(
        "/api/agent/heartbeat", headers=retired["agent_headers"], json=retired["heartbeat"]
    )
    assert response.status_code == 200, response.text
    assert not any(command["id"] == command_id for command in response.json()["commands"])
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
    assert stored["status"] == status
    assert stored["finished_at"] is None
    assert stored["error"] is None
    assert json.loads(stored["payload_json"]) == payload


@pytest.mark.parametrize("role", ["primary", "secondary"])
def test_retired_inflight_success_receipt_preserves_the_historical_type(client, retired, role):
    command_id, payload = insert_retired_command(client, retired, role=role, status="running")
    accepted = client.post(
        f"/api/agent/commands/{command_id}/result", headers=retired["agent_headers"],
        json={"status": "succeeded", "result": {}, "error": None},
    )
    assert accepted.status_code == 200, accepted.text
    history = client.get("/api/commands", headers=retired["auth"], params={"agent_id": retired["agent_id"]})
    assert history.status_code == 200, history.text
    command = next(row for row in history.json() if row["id"] == command_id)
    assert command["status"] == "succeeded"
    assert command["payload"][role]["miner_type"] == retired["miner_type"]
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT status,finished_at,payload_json FROM commands WHERE id=?", (command_id,)).fetchone()
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 1
        sheet = db.execute("SELECT miner_type,miner_package_id FROM flight_sheets WHERE id=?", (retired["sheet_id"],)).fetchone()
    assert stored["status"] == "succeeded" and stored["finished_at"] is not None
    assert json.loads(stored["payload_json"]) == payload
    assert tuple(sheet) == (retired["miner_type"], retired["package_id"])


def test_secondary_retired_flight_remains_readable_and_cannot_apply(client, retired):
    with client.app.state.db.connection() as db:
        db.execute(
            """UPDATE flight_sheets SET miner_type='generic',miner_version=NULL,
               miner_package_id=NULL,secondary_miner_type=?,secondary_miner_version=?,
               secondary_miner_package_id=?,secondary_coin=?,secondary_wallet_id=?,
               secondary_pool_url=?,secondary_algorithm=?,secondary_extra_config_json='{}'
               WHERE id=?""",
            (retired["miner_type"], retired["version"], retired["package_id"],
             retired["wallet"]["coin"], retired["wallet"]["id"], retired["body"]["pool_url"],
             retired["body"]["algorithm"], retired["sheet_id"]),
        )
    listed = client.get("/api/flight-sheets", headers=retired["auth"])
    assert listed.status_code == 200, listed.text
    sheet = next(row for row in listed.json() if row["id"] == retired["sheet_id"])
    assert sheet["miner_type"] == "generic"
    assert sheet["secondary"]["miner_type"] == retired["miner_type"]
    assert sheet["secondary"]["miner_version"] == retired["version"]
    assert sheet["secondary"]["miner_package"]["managed_miner_type"] == retired["miner_type"]
    applied = client.post(
        f"/api/agents/{retired['agent_id']}/flight-sheets/{retired['sheet_id']}/apply",
        headers=retired["auth"],
    )
    assert applied.status_code == 409, applied.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
        row = db.execute(
            "SELECT secondary_miner_type,secondary_miner_package_id FROM flight_sheets WHERE id=?",
            (retired["sheet_id"],),
        ).fetchone()
    assert tuple(row) == (retired["miner_type"], retired["package_id"])


def test_retirement_keeps_existing_device_inventory_and_workload_history(client, retired):
    mining_status = {
        "configured": True, "state": "running", "process_verified": True,
        "flight_sheet_id": retired["sheet_id"], "miner_type": retired["miner_type"],
    }
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    bucket = (now - timedelta(minutes=2)).isoformat()
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET inventory_json=? WHERE id=?",
            (json.dumps({"mining_status": mining_status}), retired["agent_id"]),
        )
        db.execute(
            """INSERT INTO device_work_buckets
               (agent_id,series_key,bucket_start,coin,algorithm,estimated_hashes,
                coverage_seconds,interval_count) VALUES(?,?,?,?,?,?,?,?)""",
            (retired["agent_id"], "historical-series", bucket, retired["wallet"]["coin"],
             retired["body"]["algorithm"], 123456, 60, 1),
        )
        before = dict(db.execute(
            "SELECT * FROM device_work_buckets WHERE agent_id=?", (retired["agent_id"],),
        ).fetchone())
    packages = client.get("/api/miner-packages", headers=retired["auth"])
    assert packages.status_code == 200, packages.text
    package = next(row for row in packages.json() if row["id"] == retired["package_id"])
    assert package["managed_miner_type"] == retired["miner_type"]
    device = client.get(f"/api/agents/{retired['agent_id']}", headers=retired["auth"])
    assert device.status_code == 200, device.text
    assert device.json()["inventory"]["mining_status"] == mining_status
    workload = client.get("/api/workload", headers=retired["auth"], params={
        "from": (now - timedelta(minutes=5)).isoformat(),
        "to": (now + timedelta(minutes=5)).isoformat(), "agent_id": retired["agent_id"],
    })
    assert workload.status_code == 200, workload.text
    series = next(row for row in workload.json()["series"] if row["coin"] == retired["wallet"]["coin"])
    assert series["total"] == pytest.approx(123456)
    assert series["devices"][0]["agent_id"] == retired["agent_id"]
    with client.app.state.db.connection() as db:
        after = dict(db.execute(
            "SELECT * FROM device_work_buckets WHERE agent_id=?", (retired["agent_id"],),
        ).fetchone())
        assert db.execute("SELECT count(*) FROM agents WHERE id=?", (retired["agent_id"],)).fetchone()[0] == 1
    assert after == before


def test_explicit_supported_replacement_is_required_and_preserves_old_package_record(client, retired):
    # Changing to the supported local profile is an explicit user choice.
    replacement = client.put(
        f"/api/flight-sheets/{retired['sheet_id']}", headers=retired["auth"],
        json={**retired["body"], "miner_type": "generic", "miner_package_id": None},
    )
    assert replacement.status_code == 200, replacement.text
    sheet = replacement.json()
    assert sheet["miner_type"] == "generic"
    assert sheet["miner_version"] is None
    assert sheet["miner_package_id"] is None
    assert sheet["wallet_id"] == retired["wallet"]["id"]
    assert sheet["pool_url"] == retired["body"]["pool_url"]
    assert "miner_type" in sheet["changed_fields"]
    applied = client.post(
        f"/api/agents/{retired['agent_id']}/flight-sheets/{retired['sheet_id']}/apply",
        headers=retired["auth"],
    )
    assert applied.status_code == 201, applied.text
    primary = applied.json()["payload"]["primary"]
    assert primary.get("miner_type", "generic") == "generic"
    assert primary.get("miner_package") is None
    with client.app.state.db.connection() as db:
        assert db.execute(
            "SELECT managed_miner_type FROM miner_packages WHERE id=?", (retired["package_id"],),
        ).fetchone()[0] == retired["miner_type"]
        assert db.execute("SELECT count(*) FROM agents WHERE id=?", (retired["agent_id"],)).fetchone()[0] == 1


def historical_restart_heartbeat(retired, *, identity_location="top_level", boot_time=None):
    body = deepcopy(retired["heartbeat"])
    mining = {
        "configured": True, "state": "stopped", "process_verified": True,
        "process_error": None, "flight_sheet_id": retired["sheet_id"],
        "checked_at": utcnow(),
    }
    if identity_location == "top_level":
        mining["miner_type"] = retired["miner_type"]
    elif identity_location == "primary":
        mining.update(miner_type="generic", primary={"miner_type": retired["miner_type"]})
    elif identity_location == "secondary":
        mining.update(miner_type="generic", primary={"miner_type": "generic"},
                      secondary={"miner_type": retired["miner_type"]})
    elif identity_location == "supported":
        mining.update(miner_type="generic", primary={"miner_type": "generic"})
    else:
        assert identity_location == "sheet"
    body["inventory"]["mining_status"] = mining
    if boot_time is not None:
        body["metrics"].update(boot_time=boot_time, uptime_seconds=60)
    return body


def post_historical_heartbeat(client, retired, body):
    response = client.post("/api/agent/heartbeat", headers=retired["agent_headers"], json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("identity_location", ["top_level", "primary", "secondary", "sheet"])
def test_restart_rejects_unsupported_saved_configuration_before_queueing(client, retired, identity_location):
    if identity_location != "sheet":
        # An edited Controller draft cannot hide the actual unsupported local
        # configuration reported by this Agent.
        with client.app.state.db.connection() as db:
            db.execute("UPDATE flight_sheets SET miner_type='generic',miner_package_id=NULL WHERE id=?",
                       (retired["sheet_id"],))
    post_historical_heartbeat(client, retired, historical_restart_heartbeat(retired, identity_location=identity_location))
    response = client.post(f"/api/agents/{retired['agent_id']}/commands", headers=retired["auth"],
                           json={"action": "restart_mining", "payload": {}})
    assert response.status_code == 409, response.text
    assert "不受支持的矿工类型" in response.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0


def test_queued_restart_is_failed_before_dispatch_when_local_configuration_is_unsupported(client, retired):
    # A supported local configuration has precedence over an obsolete draft.
    post_historical_heartbeat(client, retired, historical_restart_heartbeat(retired, identity_location="supported"))
    queued = client.post(f"/api/agents/{retired['agent_id']}/commands", headers=retired["auth"],
                         json={"action": "restart_mining", "payload": {}})
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]
    with client.app.state.db.connection() as db:
        payload = db.execute("SELECT payload_json FROM commands WHERE id=?", (command_id,)).fetchone()[0]
    accepted = post_historical_heartbeat(client, retired, historical_restart_heartbeat(retired))
    assert not any(command["id"] == command_id for command in accepted["commands"])
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
    assert stored["status"] == "failed"
    assert "不受支持的矿工类型" in stored["error"] or stored["error"] == "Unsupported or retired miner type; reconfigure the flight sheet"
    assert stored["finished_at"] is not None
    assert stored["payload_json"] == payload


@pytest.mark.parametrize("supported_snapshot", [True, False])
def test_restart_uses_last_saved_apply_identity_before_an_edited_controller_draft(client, retired, supported_snapshot):
    command_id, payload = insert_retired_command(client, retired, status="succeeded")
    with client.app.state.db.connection() as db:
        if supported_snapshot:
            payload["primary"] = {**payload["primary"], "miner_type": "generic", "miner_package": None}
        else:
            db.execute("UPDATE flight_sheets SET miner_type='generic',miner_package_id=NULL WHERE id=?",
                       (retired["sheet_id"],))
        db.execute("UPDATE commands SET payload_json=?,finished_at=? WHERE id=?",
                   (json.dumps(payload), utcnow(), command_id))
    post_historical_heartbeat(client, retired, historical_restart_heartbeat(retired, identity_location="sheet"))
    response = client.post(f"/api/agents/{retired['agent_id']}/commands", headers=retired["auth"],
                           json={"action": "restart_mining", "payload": {}})
    assert response.status_code == (201 if supported_snapshot else 409), response.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands WHERE action='restart_mining'").fetchone()[0] == int(supported_snapshot)


def test_unsupported_saved_configuration_never_queues_autostart_restart(client, retired):
    old_boot = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    new_boot = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    post_historical_heartbeat(client, retired, historical_restart_heartbeat(retired, boot_time=old_boot))
    enabled = client.patch(f"/api/agents/{retired['agent_id']}/mining-autostart", headers=retired["auth"],
                           json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    for _ in range(2):
        body = post_historical_heartbeat(client, retired, historical_restart_heartbeat(retired, boot_time=new_boot))
        assert not any(command["action"] == "restart_mining" for command in body["commands"])
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands WHERE action='restart_mining'").fetchone()[0] == 0
        assert db.execute("SELECT mining_autostart_last_boot_time FROM agents WHERE id=?",
                          (retired["agent_id"],)).fetchone()[0] == old_boot


@pytest.mark.parametrize("action", ["stop_mining", "remove_flight_sheet", "collect_miner_logs"])
def test_unsupported_configuration_keeps_stop_remove_and_logs_available(client, retired, action):
    heartbeat = historical_restart_heartbeat(retired)
    post_historical_heartbeat(client, retired, heartbeat)
    queued = client.post(f"/api/agents/{retired['agent_id']}/commands", headers=retired["auth"],
                         json={"action": action, "payload": {}})
    assert queued.status_code == 201, queued.text
    accepted = post_historical_heartbeat(client, retired, heartbeat)
    assert any(command["id"] == queued.json()["id"] for command in accepted["commands"])


@pytest.mark.parametrize("status", ["dispatched", "running"])
def test_unsupported_inflight_restart_is_not_resent_and_keeps_its_history_receipt(client, retired, status):
    command_id, _ = insert_retired_command(client, retired, status=status)
    with client.app.state.db.connection() as db:
        db.execute("UPDATE commands SET action='restart_mining',payload_json='{}' WHERE id=?", (command_id,))
    accepted = post_historical_heartbeat(client, retired, historical_restart_heartbeat(retired))
    assert not any(command["id"] == command_id for command in accepted["commands"])
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
    assert stored["status"] == status and stored["finished_at"] is None and stored["error"] is None
    assert stored["payload_json"] == "{}"
    finished = client.post(f"/api/agent/commands/{command_id}/result", headers=retired["agent_headers"],
                           json={"status": "succeeded", "result": {}, "error": None})
    assert finished.status_code == 200, finished.text
    history = client.get("/api/commands", headers=retired["auth"], params={"agent_id": retired["agent_id"]})
    assert history.status_code == 200, history.text
    command = next(row for row in history.json() if row["id"] == command_id)
    assert command["action"] == "restart_mining" and command["status"] == "succeeded" and command["payload"] == {}
