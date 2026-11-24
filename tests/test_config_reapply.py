from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login

from server.config import Settings
from server.main import create_app


ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="configuration-reapply-test-key-that-is-long-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient) -> str:
    return _login(client, 'admin', ADMIN_PASSWORD)


def enroll(client: TestClient, token: str, name: str) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": name, "ttl_minutes": 30},
    )
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued.json()["token"],
            "name": name,
            "hostname": f"{name}.local",
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": "0.6.0",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def heartbeat(
    client: TestClient,
    agent_id: str,
    secret: str,
    mining_status: dict[str, object],
    *,
    metrics: dict[str, object] | None = None,
    os_family: str = "windows",
    agent_version: str = "0.6.0",
    capabilities: list[str] | None = None,
):
    return client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={
            "hostname": f"{agent_id}.local",
            "os_family": os_family,
            "platform": "Windows 11" if os_family == "windows" else "Ubuntu",
            "agent_version": agent_version,
            "capabilities": capabilities or [],
            "ip_addresses": [],
            "metrics": metrics or {},
            "inventory": {"mining_status": mining_status},
        },
    )


def create_wallet_and_sheet(client: TestClient, token: str):
    wallet_response = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "XMR wallet",
            "coin": "XMR",
            "address": "47oldpublicwalletaddress0000000000000000000000",
            "note": "",
        },
    )
    assert wallet_response.status_code == 201, wallet_response.text
    wallet = wallet_response.json()
    sheet_response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "XMR generic",
            "miner_type": "generic",
            "coin": "XMR",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
            "miner_profile": "default",
            "worker_name_template": "{hostname}",
            "extra_config": {},
        },
    )
    assert sheet_response.status_code == 201, sheet_response.text
    return wallet, sheet_response.json()


def running_status(sheet_id: str, *, verified: bool = True) -> dict[str, object]:
    return {
        "configured": True,
        "state": "running",
        "process_verified": verified,
        "flight_sheet_id": sheet_id,
        "flight_sheet_name": "XMR generic",
    }


def test_extra_argument_edits_refresh_both_legs_and_clearing_replaces_queued_args(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    from miner_test_support import package_download
    from test_independent_dual_mining import (
        create_wallet, enroll_linux_agent, independent_xmrig_pearl_payload,
    )

    def offline_catalog(*args, **kwargs):
        raise OSError("offline extra-argument refresh fixture")

    client.app.state.managed_miner_catalog._fetcher = offline_catalog
    monkeypatch.setattr("server.main._download_https_package", package_download(b"synthetic refresh archive"))
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    pearl_wallet = create_wallet(client, token, coin="PEARL", name="Refresh PEARL wallet")
    body = independent_xmrig_pearl_payload(wallet["id"], pearl_wallet)
    body["extra_config"] = {"extra_args": ""}
    body["secondary"]["extra_config"]["extra_args"] = ""
    initial = client.put(f"/api/flight-sheets/{sheet['id']}", headers=auth(token), json=body)
    assert initial.status_code == 200, initial.text
    agent_id, secret = enroll_linux_agent(client, token, version="0.8.31")
    status = running_status(sheet["id"])
    heartbeat_kwargs = {
        "os_family": "linux", "agent_version": "0.8.31",
        "capabilities": ["managed_mining.dual_process.v1", "managed_mining.independent_srb_gpu_oc.v1", "managed_mining.extra_args.v1"],
        "metrics": {"gpus": [{"index": 0, "vendor": "NVIDIA", "device_type": "discrete", "name": "Synthetic NVIDIA GPU"}]},
    }
    active = heartbeat(client, agent_id, secret, status, **heartbeat_kwargs)
    assert active.status_code == 200, active.text

    queued_ids = []
    for primary, secondary in [
        ('--futureflag 7 --label "primary value"', "--future-toggle"),
        ('--futureflag 7 --label "primary value"', "--futureflag 10 --label 'secondary value'"),
        ("", ""),
    ]:
        body["extra_config"]["extra_args"] = primary
        body["secondary"]["extra_config"]["extra_args"] = secondary
        updated = client.put(f"/api/flight-sheets/{sheet['id']}", headers=auth(token), json=body)
        assert updated.status_code == 200, updated.text
        assert updated.json()["reapply"]["queued"] == 1
        assert updated.json()["reapply"]["failed"] == 0
        history = client.get("/api/commands", headers=auth(token), params={"agent_id": agent_id}).json()
        queued = [row for row in history if row["action"] == "apply_flight_sheet" and row["status"] == "queued"]
        assert len(queued) == 1
        command = queued[0]
        queued_ids.append(command["id"])
        for role, expected in [("primary", primary), ("secondary", secondary)]:
            options = command["payload"][role]["extra_config"]
            if expected:
                assert options["extra_args"] == expected
            else:
                assert "extra_args" not in options
        noop = client.put(f"/api/flight-sheets/{sheet['id']}", headers=auth(token), json=body)
        assert noop.status_code == 200, noop.text
        assert noop.json()["changed_fields"] == []
        assert noop.json()["reapply"]["triggered"] is False
        with client.app.state.db.connection() as db:
            assert db.execute("SELECT count(*) FROM commands WHERE agent_id=?", (agent_id,)).fetchone()[0] == len(history)

    assert len(set(queued_ids)) == 3
    delivered = heartbeat(client, agent_id, secret, status, **heartbeat_kwargs)
    assert delivered.status_code == 200, delivered.text
    assert [row["id"] for row in delivered.json()["commands"]] == [queued_ids[-1]]
    payload = delivered.json()["commands"][0]["payload"]
    assert "extra_args" not in payload["primary"]["extra_config"]
    assert "extra_args" not in payload["secondary"]["extra_config"]
    with client.app.state.db.connection() as db:
        cancelled = db.execute("SELECT id FROM commands WHERE agent_id=? AND status='cancelled'", (agent_id,)).fetchall()
    assert {row["id"] for row in cancelled} == set(queued_ids[:-1])


def insert_legacy_flight_assignment(
    client: TestClient,
    *,
    agent_id: str,
    flight_sheet_id: str,
    updated_at: str = "2000-01-01T00:00:00+00:00",
) -> None:
    """Recreate the retired Flight V2 assignment ledger from production."""

    with client.app.state.db.connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_flight_assignments (
                agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                protocol TEXT NOT NULL CHECK(protocol IN ('v1','v2')),
                flight_sheet_id TEXT NOT NULL
                    REFERENCES flight_sheets(id) ON DELETE RESTRICT,
                runtime_fingerprint TEXT,
                command_id TEXT REFERENCES commands(id) ON DELETE SET NULL,
                updated_at TEXT NOT NULL,
                CHECK(
                    (protocol='v1' AND (
                        runtime_fingerprint IS NULL OR length(runtime_fingerprint)=64
                    ))
                    OR
                    (protocol='v2' AND length(runtime_fingerprint)=64
                     AND runtime_fingerprint NOT GLOB '*[^0-9a-f]*')
                )
            );
            CREATE INDEX IF NOT EXISTS idx_agent_flight_assignments_sheet
                ON agent_flight_assignments(flight_sheet_id,protocol);
            """
        )
        db.execute(
            """INSERT INTO agent_flight_assignments
               (agent_id,protocol,flight_sheet_id,runtime_fingerprint,
                command_id,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (
                agent_id,
                "v1",
                flight_sheet_id,
                None,
                None,
                updated_at,
            ),
        )


def test_wallet_address_change_reapplies_only_confirmed_running_devices(
    client: TestClient,
):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    active_id, active_secret = enroll(client, token, "active")
    stopped_id, stopped_secret = enroll(client, token, "stopped")
    stale_id, stale_secret = enroll(client, token, "stale")
    assert heartbeat(client, active_id, active_secret, running_status(sheet["id"])).status_code == 200
    assert heartbeat(
        client,
        stopped_id,
        stopped_secret,
        {**running_status(sheet["id"]), "state": "stopped", "process_verified": False},
    ).status_code == 200
    assert heartbeat(
        client, stale_id, stale_secret, running_status(sheet["id"], verified=False)
    ).status_code == 200

    listed = client.get("/api/flight-sheets", headers=auth(token)).json()
    assert listed[0]["device_count"] == 1
    assert listed[0]["configured_device_count"] == 3

    updated = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "48newpublicwalletaddress0000000000000000000000",
            "note": wallet["note"],
        },
    )
    assert updated.status_code == 200, updated.text
    reapply = updated.json()["reapply"]
    assert reapply["triggered"] is True
    assert reapply["matched"] == 1
    assert reapply["queued"] == 1
    assert reapply["skipped"] == 0
    assert reapply["failed"] == 0

    delivered = heartbeat(client, active_id, active_secret, running_status(sheet["id"]))
    commands = delivered.json()["commands"]
    assert len(commands) == 1
    assert commands[0]["action"] == "apply_flight_sheet"
    assert commands[0]["payload"]["primary"]["wallet_address"].startswith("48new")
    assert all(command["action"] != "restart_mining" for command in commands)
    assert heartbeat(client, stopped_id, stopped_secret, {**running_status(sheet["id"]), "state": "stopped", "process_verified": False}).json()["commands"] == []
    assert heartbeat(client, stale_id, stale_secret, running_status(sheet["id"], verified=False)).json()["commands"] == []


def test_wallet_metadata_and_noop_updates_do_not_restart_miners(client: TestClient):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "metadata")
    heartbeat(client, agent_id, secret, running_status(sheet["id"]))

    renamed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={**{key: wallet[key] for key in ("coin", "address", "note")}, "name": "renamed"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["reapply"]["triggered"] is False
    assert heartbeat(client, agent_id, secret, running_status(sheet["id"])).json()["commands"] == []

    unchanged = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": "renamed",
            "coin": wallet["coin"],
            "address": wallet["address"],
            "note": wallet["note"],
        },
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["changed_fields"] == []
    assert unchanged.json()["reapply"]["triggered"] is False


def test_flight_sheet_change_queues_latest_configuration_and_noop_does_not(
    client: TestClient,
):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "flight-edit")
    heartbeat(client, agent_id, secret, running_status(sheet["id"]))
    body = {
        "name": sheet["name"],
        "miner_type": sheet["miner_type"],
        "coin": sheet["coin"],
        "wallet_id": sheet["wallet_id"],
        "pool_url": "stratum+ssl://new-pool.example.test:443",
        "miner_profile": sheet["miner_profile"],
        "pool_password": sheet["pool_password"],
        "device_indices": sheet["device_indices"],
        "worker_name_template": sheet["worker_name_template"],
        "extra_config": sheet["extra_config"],
        "gpu_oc": None,
        "secondary": None,
    }
    changed = client.put(
        f"/api/flight-sheets/{sheet['id']}", headers=auth(token), json=body
    )
    assert changed.status_code == 200, changed.text
    assert "pool_url" in changed.json()["changed_fields"]
    assert changed.json()["reapply"]["queued"] == 1

    delivered = heartbeat(client, agent_id, secret, running_status(sheet["id"]))
    assert [command["action"] for command in delivered.json()["commands"]] == [
        "apply_flight_sheet"
    ]
    assert delivered.json()["commands"][0]["payload"]["primary"]["pool_url"] == body["pool_url"]

    unchanged = client.put(
        f"/api/flight-sheets/{sheet['id']}", headers=auth(token), json=body
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["changed_fields"] == []
    assert unchanged.json()["reapply"]["triggered"] is False


def test_flight_sheet_name_only_edit_does_not_restart_miner(
    client: TestClient,
):
    token = login(client)
    _wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "flight-rename")
    heartbeat(client, agent_id, secret, running_status(sheet["id"]))
    body = {
        "name": "Renamed in controller only",
        "miner_type": sheet["miner_type"],
        "coin": sheet["coin"],
        "wallet_id": sheet["wallet_id"],
        "pool_url": sheet["pool_url"],
        "miner_profile": sheet["miner_profile"],
        "pool_password": sheet["pool_password"],
        "device_indices": sheet["device_indices"],
        "worker_name_template": sheet["worker_name_template"],
        "extra_config": sheet["extra_config"],
        "gpu_oc": None,
        "secondary": None,
    }
    renamed = client.put(
        f"/api/flight-sheets/{sheet['id']}",
        headers=auth(token),
        json=body,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["changed_fields"] == ["name"]
    assert renamed.json()["reapply"]["triggered"] is False
    assert heartbeat(
        client,
        agent_id,
        secret,
        running_status(sheet["id"]),
    ).json()["commands"] == []


def test_agent_display_name_refreshes_only_its_templated_worker(
    client: TestClient,
):
    token = login(client)
    _wallet, sheet = create_wallet_and_sheet(client, token)
    renamed_id, renamed_secret = enroll(client, token, "old-worker")
    other_id, other_secret = enroll(client, token, "other-worker")
    assert heartbeat(
        client, renamed_id, renamed_secret, running_status(sheet["id"])
    ).status_code == 200
    assert heartbeat(
        client, other_id, other_secret, running_status(sheet["id"])
    ).status_code == 200

    renamed = client.patch(
        f"/api/agents/{renamed_id}",
        headers=auth(token),
        json={"name": "new-worker"},
    )
    assert renamed.status_code == 200, renamed.text

    delivered = heartbeat(
        client, renamed_id, renamed_secret, running_status(sheet["id"])
    ).json()["commands"]
    assert [command["action"] for command in delivered] == ["apply_flight_sheet"]
    assert delivered[0]["payload"]["worker_name"] == "new-worker"
    assert heartbeat(
        client, other_id, other_secret, running_status(sheet["id"])
    ).json()["commands"] == []

    note_only = client.patch(
        f"/api/agents/{other_id}",
        headers=auth(token),
        json={"note": "机位二"},
    )
    assert note_only.status_code == 200
    assert heartbeat(
        client, other_id, other_secret, running_status(sheet["id"])
    ).json()["commands"] == []


def test_agent_display_name_does_not_refresh_system_hostname_worker(
    client: TestClient,
):
    token = login(client)
    _wallet, sheet = create_wallet_and_sheet(client, token)
    body = {
        "name": sheet["name"],
        "miner_type": sheet["miner_type"],
        "coin": sheet["coin"],
        "wallet_id": sheet["wallet_id"],
        "pool_url": sheet["pool_url"],
        "miner_profile": sheet["miner_profile"],
        "pool_password": sheet["pool_password"],
        "device_indices": sheet["device_indices"],
        "worker_name_template": "{system_hostname}",
        "extra_config": sheet["extra_config"],
        "gpu_oc": None,
        "secondary": None,
    }
    updated = client.put(
        f"/api/flight-sheets/{sheet['id']}",
        headers=auth(token),
        json=body,
    )
    assert updated.status_code == 200, updated.text

    agent_id, secret = enroll(client, token, "stable-system-host")
    assert heartbeat(
        client, agent_id, secret, running_status(sheet["id"])
    ).status_code == 200
    renamed = client.patch(
        f"/api/agents/{agent_id}",
        headers=auth(token),
        json={"name": "display-only"},
    )
    assert renamed.status_code == 200, renamed.text
    assert heartbeat(
        client, agent_id, secret, running_status(sheet["id"])
    ).json()["commands"] == []


def test_legacy_agent_requires_fresh_stats_before_automatic_reapply(client: TestClient):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "legacy")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    legacy = {
        "configured": True,
        "state": "running",
        "flight_sheet_id": sheet["id"],
        "checked_at": now,
        "stats_updated_at": now,
    }
    heartbeat(client, agent_id, secret, legacy)

    updated = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "49legacyfreshwalletaddress0000000000000000000",
            "note": wallet["note"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["reapply"]["queued"] == 1


def test_automatic_reapply_is_cancelled_if_device_stops_before_dispatch(
    client: TestClient,
):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "stop-race")
    heartbeat(client, agent_id, secret, running_status(sheet["id"]))

    updated = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "48stopracewalletaddress000000000000000000000",
            "note": wallet["note"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["reapply"]["queued"] == 1

    stopped = heartbeat(
        client,
        agent_id,
        secret,
        {
            **running_status(sheet["id"]),
            "state": "stopped",
            "process_verified": False,
        },
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["commands"] == []
    history = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=auth(token)
    ).json()
    assert history[0]["action"] == "apply_flight_sheet"
    assert history[0]["status"] == "cancelled"
    assert "_controller_auto_reapply" not in history[0]["payload"]


def test_automatic_reapply_skips_conflicting_restart_mining(client: TestClient):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "restart-conflict")
    heartbeat(client, agent_id, secret, running_status(sheet["id"]))
    restart = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert restart.status_code == 201, restart.text

    updated = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "48restartconflictwallet000000000000000000000",
            "note": wallet["note"],
        },
    )
    assert updated.status_code == 200, updated.text
    reapply = updated.json()["reapply"]
    assert reapply["matched"] == 1
    assert reapply["queued"] == 0
    assert reapply["skipped"] == 1
    assert "restart_mining" in reapply["issues"][0]["reason"]
    commands = heartbeat(
        client, agent_id, secret, running_status(sheet["id"])
    ).json()["commands"]
    assert [command["action"] for command in commands] == ["restart_mining"]


def test_empty_pool_password_is_preserved_through_edit_list_and_command(
    client: TestClient,
):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    body = {
        "name": sheet["name"],
        "miner_type": sheet["miner_type"],
        "coin": sheet["coin"],
        "wallet_id": sheet["wallet_id"],
        "pool_url": sheet["pool_url"],
        "miner_profile": sheet["miner_profile"],
        "pool_password": "",
        "device_indices": sheet["device_indices"],
        "worker_name_template": sheet["worker_name_template"],
        "extra_config": sheet["extra_config"],
        "gpu_oc": None,
        "secondary": {
            "miner_type": "generic",
            "coin": sheet["coin"],
            "wallet_id": sheet["wallet_id"],
            "pool_url": "stratum+ssl://secondary.example.test:443",
            "miner_profile": "default",
            "pool_password": "",
            "device_indices": [],
            "extra_config": {},
        },
    }
    response = client.put(
        f"/api/flight-sheets/{sheet['id']}", headers=auth(token), json=body
    )
    assert response.status_code == 200, response.text
    assert "pool_password" in response.json()["changed_fields"]
    assert response.json()["reapply"]["triggered"] is True
    assert response.json()["reapply"]["matched"] == 0

    listed = client.get("/api/flight-sheets", headers=auth(token)).json()
    assert listed[0]["pool_password"] == ""
    assert listed[0]["secondary"]["pool_password"] == ""

    unchanged = client.put(
        f"/api/flight-sheets/{sheet['id']}", headers=auth(token), json=body
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["changed_fields"] == []

    agent_id, secret = enroll(client, token, "empty-pool-password")
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    commands = heartbeat(
        client,
        agent_id,
        secret,
        {"configured": False, "state": "stopped", "process_verified": False},
    ).json()["commands"]
    assert commands[0]["payload"]["primary"]["pool_password"] == ""
    assert commands[0]["payload"]["secondary"]["pool_password"] == ""


def test_offline_queued_apply_is_replaced_with_latest_wallet_configuration(
    client: TestClient,
):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "offline-desired")

    original = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert original.status_code == 201, original.text
    changed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "49offline-latest-wallet-address000000000000000000",
            "note": wallet["note"],
        },
    )
    assert changed.status_code == 200, changed.text

    stopped = {
        "configured": False,
        "state": "stopped",
        "process_verified": False,
    }
    assert heartbeat(client, agent_id, secret, stopped).json()["commands"] == []
    delivered = heartbeat(client, agent_id, secret, stopped).json()["commands"]
    assert len(delivered) == 1
    assert delivered[0]["action"] == "apply_flight_sheet"
    assert delivered[0]["id"] != original.json()["id"]
    assert delivered[0]["payload"]["primary"]["wallet_address"].startswith(
        "49offline-latest"
    )


def test_queued_apply_revalidates_os_and_requeues_current_payload(
    client: TestClient,
):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "os-rebind")
    assert heartbeat(
        client,
        agent_id,
        secret,
        {"configured": False, "state": "stopped", "process_verified": False},
    ).status_code == 200
    original = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert original.status_code == 201, original.text

    switched = heartbeat(
        client,
        agent_id,
        secret,
        {"configured": False, "state": "stopped", "process_verified": False},
        os_family="linux",
    )
    assert switched.status_code == 200, switched.text
    replacement = switched.json()["commands"]
    assert len(replacement) == 1
    assert replacement[0]["id"] != original.json()["id"]
    assert "_controller_flight_dispatch_v1" not in replacement[0]["payload"]

    history = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=auth(token)
    ).json()
    rejected = next(item for item in history if item["id"] == original.json()["id"])
    assert rejected["status"] == "failed"
    assert "operating system changed" in rejected["error"]
    marker = client.get(
        f"/api/agents/{agent_id}", headers=auth(token)
    ).json()["configuration_refresh"]
    assert marker["command_id"] == replacement[0]["id"]
    assert marker["status"] == "dispatched"


def test_delete_sheet_cancels_queued_apply_but_rejects_configured_device(
    client: TestClient,
):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    queued_id, _queued_secret = enroll(client, token, "queued-delete")
    queued = client.post(
        f"/api/agents/{queued_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201
    deleted = client.delete(
        f"/api/flight-sheets/{sheet['id']}", headers=auth(token)
    )
    assert deleted.status_code == 200, deleted.text
    history = client.get(
        "/api/commands", params={"agent_id": queued_id}, headers=auth(token)
    ).json()
    assert history[0]["status"] == "cancelled"

    _, configured_sheet = create_wallet_and_sheet(client, token)
    configured_id, configured_secret = enroll(client, token, "configured-delete")
    assert heartbeat(
        client,
        configured_id,
        configured_secret,
        running_status(configured_sheet["id"]),
    ).status_code == 200
    rejected = client.delete(
        f"/api/flight-sheets/{configured_sheet['id']}", headers=auth(token)
    )
    assert rejected.status_code == 409
    assert "configured" in rejected.json()["detail"]


def test_delete_sheet_removes_stale_retired_assignment(
    client: TestClient,
):
    token = login(client)
    _, target_sheet = create_wallet_and_sheet(client, token)
    _, reported_sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "stale-legacy-assignment")
    insert_legacy_flight_assignment(
        client,
        agent_id=agent_id,
        flight_sheet_id=target_sheet["id"],
    )
    other_agent_id, _other_secret = enroll(
        client, token, "unrelated-legacy-assignment"
    )
    insert_legacy_flight_assignment(
        client,
        agent_id=other_agent_id,
        flight_sheet_id=reported_sheet["id"],
    )
    reported = heartbeat(
        client,
        agent_id,
        secret,
        running_status(reported_sheet["id"]),
    )
    assert reported.status_code == 200, reported.text

    deleted = client.delete(
        f"/api/flight-sheets/{target_sheet['id']}", headers=auth(token)
    )
    assert deleted.status_code == 200, deleted.text
    with client.app.state.db.connection() as db:
        target_assignment_count = db.execute(
            """SELECT COUNT(*) AS count FROM agent_flight_assignments
               WHERE flight_sheet_id=?""",
            (target_sheet["id"],),
        ).fetchone()["count"]
        unrelated_assignment_count = db.execute(
            """SELECT COUNT(*) AS count FROM agent_flight_assignments
               WHERE flight_sheet_id=?""",
            (reported_sheet["id"],),
        ).fetchone()["count"]
        audit = db.execute(
            """SELECT details_json FROM audit_logs
               WHERE action='flight_sheet_deleted' AND target_id=?
               ORDER BY id DESC LIMIT 1""",
            (target_sheet["id"],),
        ).fetchone()
    assert target_assignment_count == 0
    assert unrelated_assignment_count == 1
    assert json.loads(audit["details_json"])["legacy_assignment_rows_deleted"] == 1
    remaining_sheet_ids = {
        item["id"]
        for item in client.get("/api/flight-sheets", headers=auth(token)).json()
    }
    assert target_sheet["id"] not in remaining_sheet_ids
    assert reported_sheet["id"] in remaining_sheet_ids


def test_delete_sheet_keeps_legacy_assignment_when_device_reports_target(
    client: TestClient,
):
    token = login(client)
    _, target_sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "live-legacy-assignment")
    reported = heartbeat(
        client,
        agent_id,
        secret,
        running_status(target_sheet["id"]),
    )
    assert reported.status_code == 200, reported.text
    insert_legacy_flight_assignment(
        client,
        agent_id=agent_id,
        flight_sheet_id=target_sheet["id"],
    )

    rejected = client.delete(
        f"/api/flight-sheets/{target_sheet['id']}", headers=auth(token)
    )
    assert rejected.status_code == 409, rejected.text
    assert "configured" in rejected.json()["detail"]
    with client.app.state.db.connection() as db:
        assignment_count = db.execute(
            """SELECT COUNT(*) AS count FROM agent_flight_assignments
               WHERE agent_id=? AND flight_sheet_id=?""",
            (agent_id, target_sheet["id"]),
        ).fetchone()["count"]
    assert assignment_count == 1
    assert any(
        item["id"] == target_sheet["id"]
        for item in client.get("/api/flight-sheets", headers=auth(token)).json()
    )


def test_delete_sheet_rejects_legacy_assignment_newer_than_device_report(
    client: TestClient,
):
    token = login(client)
    _, target_sheet = create_wallet_and_sheet(client, token)
    _, reported_sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "newer-legacy-assignment")
    reported = heartbeat(
        client,
        agent_id,
        secret,
        running_status(reported_sheet["id"]),
    )
    assert reported.status_code == 200, reported.text
    insert_legacy_flight_assignment(
        client,
        agent_id=agent_id,
        flight_sheet_id=target_sheet["id"],
        updated_at="2999-01-01T00:00:00+00:00",
    )

    rejected = client.delete(
        f"/api/flight-sheets/{target_sheet['id']}", headers=auth(token)
    )
    assert rejected.status_code == 409, rejected.text
    assert "current flight sheet is unknown" in rejected.json()["detail"]
    with client.app.state.db.connection() as db:
        assignment_count = db.execute(
            """SELECT COUNT(*) AS count FROM agent_flight_assignments
               WHERE agent_id=? AND flight_sheet_id=?""",
            (agent_id, target_sheet["id"]),
        ).fetchone()["count"]
    assert assignment_count == 1


def test_delete_sheet_rejects_legacy_assignment_without_mining_report(
    client: TestClient,
):
    token = login(client)
    _, target_sheet = create_wallet_and_sheet(client, token)
    agent_id, _secret = enroll(client, token, "unknown-legacy-assignment")
    insert_legacy_flight_assignment(
        client,
        agent_id=agent_id,
        flight_sheet_id=target_sheet["id"],
    )

    rejected = client.delete(
        f"/api/flight-sheets/{target_sheet['id']}", headers=auth(token)
    )
    assert rejected.status_code == 409, rejected.text
    assert "current flight sheet is unknown" in rejected.json()["detail"]


@pytest.mark.parametrize("command_status", ["dispatched", "running"])
def test_delete_sheet_expires_stale_inflight_apply(
    client: TestClient,
    command_status: str,
):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, _secret = enroll(client, token, f"expired-{command_status}-delete")
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]

    with client.app.state.db.connection() as db:
        db.execute(
            """UPDATE commands SET status=?, expires_at=?
               WHERE id=?""",
            (command_status, "2000-01-01T00:00:00+00:00", command_id),
        )

    deleted = client.delete(
        f"/api/flight-sheets/{sheet['id']}", headers=auth(token)
    )
    assert deleted.status_code == 200, deleted.text
    history = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=auth(token)
    ).json()
    assert history[0]["id"] == command_id
    assert history[0]["status"] == "expired"


@pytest.mark.parametrize("command_status", ["dispatched", "running"])
def test_delete_sheet_rejects_unexpired_inflight_apply(
    client: TestClient,
    command_status: str,
):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, _secret = enroll(client, token, f"active-{command_status}-delete")
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert queued.status_code == 201, queued.text
    command_id = queued.json()["id"]

    with client.app.state.db.connection() as db:
        db.execute(
            """UPDATE commands SET status=?, expires_at=?
               WHERE id=?""",
            (command_status, "2999-01-01T00:00:00+00:00", command_id),
        )

    rejected = client.delete(
        f"/api/flight-sheets/{sheet['id']}", headers=auth(token)
    )
    assert rejected.status_code == 409
    assert "apply command(s) are already in flight" in rejected.json()["detail"]
    assert any(
        item["id"] == sheet["id"]
        for item in client.get("/api/flight-sheets", headers=auth(token)).json()
    )


def test_latest_configuration_waits_for_inflight_same_sheet_apply(client: TestClient):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "dispatched-conflict")
    heartbeat(client, agent_id, secret, running_status(sheet["id"]))

    first = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "48firstdispatchedwallet0000000000000000000000",
            "note": wallet["note"],
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["reapply"]["queued"] == 1
    delivered = heartbeat(
        client, agent_id, secret, running_status(sheet["id"])
    ).json()["commands"]
    assert len(delivered) == 1
    first_command_id = delivered[0]["id"]

    second = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "48seconddispatchedwallet000000000000000000000",
            "note": wallet["note"],
        },
    )
    assert second.status_code == 200, second.text
    reapply = second.json()["reapply"]
    assert reapply["queued"] == 0
    assert reapply["pending"] == 1
    assert reapply["skipped"] == 1

    # The in-flight command remains the only effective apply.  The latest
    # desired configuration stays in the durable refresh outbox until it ends.
    blocked = heartbeat(
        client, agent_id, secret, running_status(sheet["id"])
    ).json()["commands"]
    assert [command["id"] for command in blocked] == [first_command_id]
    completed = client.post(
        f"/api/agent/commands/{first_command_id}/result",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {secret}",
        },
        json={"status": "succeeded", "result": {}, "error": None},
    )
    assert completed.status_code == 200, completed.text

    latest = heartbeat(
        client, agent_id, secret, running_status(sheet["id"])
    ).json()["commands"]
    assert len(latest) == 1
    assert latest[0]["id"] != first_command_id
    assert latest[0]["payload"]["primary"]["wallet_address"].startswith("48second")


def test_concurrent_manual_applies_merge_to_one_queued_command(client: TestClient):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, _secret = enroll(client, token, "manual-merge")

    def apply_once(_index: int):
        return client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
            headers=auth(token),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(apply_once, range(2)))
    assert all(response.status_code == 201 for response in responses)

    history = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=auth(token)
    ).json()
    active = [
        command
        for command in history
        if command["action"] == "apply_flight_sheet"
        and command["status"] in {"queued", "dispatched", "running"}
    ]
    assert len(active) == 1
    assert sum(command["status"] == "cancelled" for command in history) == 1


def test_manual_apply_rejects_parallel_inflight_apply(client: TestClient):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "manual-inflight")

    first = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert first.status_code == 201, first.text
    delivered = heartbeat(
        client,
        agent_id,
        secret,
        {"configured": False, "state": "stopped", "process_verified": False},
    ).json()["commands"]
    assert [command["id"] for command in delivered] == [first.json()["id"]]

    second = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert second.status_code == 409
    assert "already in progress" in second.json()["detail"]


def test_expired_offline_manual_apply_is_requeued_after_agent_returns(
    client: TestClient,
):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "manual-offline-expiry")
    original = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert original.status_code == 201, original.text

    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE commands SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (original.json()["id"],),
        )

    stopped = {"configured": False, "state": "stopped", "process_verified": False}
    assert heartbeat(client, agent_id, secret, stopped).json()["commands"] == []
    delivered = heartbeat(client, agent_id, secret, stopped).json()["commands"]
    assert len(delivered) == 1
    assert delivered[0]["action"] == "apply_flight_sheet"
    assert delivered[0]["id"] != original.json()["id"]


def test_later_stop_command_cancels_queued_automatic_refresh(client: TestClient):
    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "stop-priority")
    heartbeat(client, agent_id, secret, running_status(sheet["id"]))

    updated = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=auth(token),
        json={
            "name": wallet["name"],
            "coin": wallet["coin"],
            "address": "48stopprioritywallet000000000000000000000000",
            "note": wallet["note"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["reapply"]["queued"] == 1
    stopped = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert stopped.status_code == 201, stopped.text

    commands = heartbeat(
        client, agent_id, secret, running_status(sheet["id"])
    ).json()["commands"]
    assert [command["action"] for command in commands] == ["stop_mining"]
    history = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=auth(token)
    ).json()
    automatic = next(
        command for command in history if command["action"] == "apply_flight_sheet"
    )
    assert automatic["status"] == "cancelled"


def test_stop_cancels_queued_manual_apply_and_keeps_stop_final(client: TestClient):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "manual-apply-then-stop")
    stopped_status = {
        "configured": False,
        "state": "stopped",
        "process_verified": True,
    }
    heartbeat(client, agent_id, secret, stopped_status)
    apply = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert apply.status_code == 201, apply.text

    stop = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert stop.status_code == 201, stop.text
    delivered = heartbeat(client, agent_id, secret, stopped_status).json()["commands"]
    assert [item["action"] for item in delivered] == ["stop_mining"]

    history = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=auth(token)
    ).json()
    cancelled_apply = next(
        item for item in history if item["id"] == apply.json()["id"]
    )
    assert cancelled_apply["status"] == "cancelled"
    detail = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()
    assert detail["configuration_refresh"]["status"] == "cancelled"


def test_restart_rejects_queued_or_inflight_flight_apply(client: TestClient):
    token = login(client)
    _, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "apply-restart-conflict")
    stopped_status = {
        "configured": False,
        "state": "stopped",
        "process_verified": True,
    }
    heartbeat(client, agent_id, secret, stopped_status)
    apply = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert apply.status_code == 201, apply.text
    blocked_queued = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert blocked_queued.status_code == 409
    assert "queued flight-sheet apply" in blocked_queued.json()["detail"]

    heartbeat(client, agent_id, secret, stopped_status)
    blocked_inflight = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(token),
        json={"action": "restart_mining", "payload": {}},
    )
    assert blocked_inflight.status_code == 409
    assert "already running" in blocked_inflight.json()["detail"]


@pytest.mark.parametrize("prebind", ["stop", "dispatched", "running", "succeeded"])
def test_automatic_refresh_binding_preserves_prebind_stop_or_agent_progress(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, prebind: str,
):
    import inspect

    token = login(client)
    wallet, sheet = create_wallet_and_sheet(client, token)
    agent_id, secret = enroll(client, token, "stop-before-bind")
    assert heartbeat(client, agent_id, secret, running_status(sheet["id"])).status_code == 200
    wallet_endpoint = next(route.endpoint for route in client.app.routes
                           if getattr(route, "path", None) == "/api/wallets/{wallet_id}"
                           and "PUT" in route.methods)
    reapply = inspect.getclosurevars(wallet_endpoint).nonlocals["reapply_active_flight_sheets"]
    apply_cell = dict(zip(reapply.__code__.co_freevars, reapply.__closure__))["apply_flight_sheet"]
    real_apply = apply_cell.cell_contents
    stopped = []
    progress = []

    def stop_before_bind(*args, **kwargs):
        command = real_apply(*args, **kwargs)
        # The apply transaction has committed, but the outbox has not bound it.
        with client.app.state.db.connection() as db:
            refresh = db.execute("SELECT status,command_id FROM configuration_refreshes WHERE agent_id=?", (agent_id,)).fetchone()
            assert tuple(refresh) == ("preparing", None)
            assert db.execute("SELECT status FROM commands WHERE id=?", (command["id"],)).fetchone()[0] == "queued"
        if prebind == "stop":
            stop = client.post(f"/api/agents/{agent_id}/commands", headers=auth(token),
                               json={"action": "stop_mining", "payload": {}})
            assert stop.status_code == 201, stop.text
            stopped.append(stop.json()["id"])
        else:
            dispatched = heartbeat(client, agent_id, secret, running_status(sheet["id"]))
            assert [item["id"] for item in dispatched.json()["commands"]] == [command["id"]]
            if prebind != "dispatched":
                receipt = client.post(f"/api/agent/commands/{command['id']}/result",
                    headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
                    json={"status": prebind, "result": {}, "error": None})
                assert receipt.status_code == 200, receipt.text
        with client.app.state.db.connection() as db:
            progress.append(dict(db.execute("SELECT * FROM commands WHERE id=?", (command["id"],)).fetchone()))
        return command

    monkeypatch.setattr(apply_cell, "cell_contents", stop_before_bind)
    response = client.put(f"/api/wallets/{wallet['id']}", headers=auth(token), json={
        **{key: wallet[key] for key in ("name", "coin", "note")},
        "address": "48stopbeforebindwallet000000000000000000000000",
    })
    assert response.status_code == 200, response.text
    marker = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["configuration_refresh"]
    assert marker["status"] == ("cancelled" if prebind == "stop" else prebind)
    if prebind != "stop":
        with client.app.state.db.connection() as db:
            bound = db.execute("SELECT * FROM commands WHERE id=?", (marker["command_id"],)).fetchone()
        assert bound["status"] == prebind
        assert datetime.fromisoformat(marker["queued_at"]) <= datetime.fromisoformat(bound["dispatched_at"])
        if prebind == "succeeded":
            assert marker["completed_at"] == bound["finished_at"] == progress[0]["finished_at"]
        else:
            assert marker["completed_at"] is None
        for _ in range(2):
            followup = heartbeat(client, agent_id, secret, running_status(sheet["id"]))
            assert followup.status_code == 200, followup.text
            assert all(item["id"] == marker["command_id"] for item in followup.json()["commands"])
        history = client.get("/api/commands", params={"agent_id": agent_id}, headers=auth(token)).json()
        assert [(item["id"], item["status"]) for item in history if item["action"] == "apply_flight_sheet"] == [(marker["command_id"], prebind)]
        return
    assert len(stopped) == 1
    assert response.json()["reapply"]["queued"] == 0
    delivered = heartbeat(client, agent_id, secret, running_status(sheet["id"]))
    assert [item["id"] for item in delivered.json()["commands"]] == stopped
    completed = client.post(f"/api/agent/commands/{stopped[0]}/result",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={"status": "succeeded", "result": {}, "error": None})
    assert completed.status_code == 200, completed.text
    # Neither stopped samples nor later running samples revive cancelled intent.
    for state in ("stopped", "running", "running"):
        sample = {**running_status(sheet["id"]), "state": state}
        assert heartbeat(client, agent_id, secret, sample).json()["commands"] == []
    history = client.get("/api/commands", params={"agent_id": agent_id}, headers=auth(token)).json()
    applies = [item for item in history if item["action"] == "apply_flight_sheet"]
    assert len(applies) == 1
    assert applies[0]["status"] == "cancelled"
