from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import main as main_module
from controller_test_support import login as _login, auth

from server.config import Settings
from server.database import Database
from server.main import create_app


PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="pending-refresh-test-key-that-is-long-enough",
        admin_username="admin",
        admin_password=PASSWORD,
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as value:
        value.app.state.database = Database(settings.database_path)
        yield value


def login(client: TestClient) -> dict[str, str]:
    return auth(_login(client, 'admin', PASSWORD))


def enroll(
    client: TestClient, headers: dict[str, str], name: str, version: str = "0.6.1"
) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=headers,
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
            "agent_version": version,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def heartbeat(
    client: TestClient,
    agent_id: str,
    secret: str,
    sheet_id: str,
    state: str,
    version: str = "0.6.1",
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
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": version,
            "capabilities": capabilities or [],
            "ip_addresses": [],
            "metrics": {},
            "inventory": {
                "mining_status": {
                    "configured": True,
                    "state": state,
                    "process_verified": state == "running",
                    "flight_sheet_id": sheet_id,
                }
            },
        },
    )


def wallet_and_sheet(client: TestClient, headers: dict[str, str]):
    wallet = client.post(
        "/api/wallets",
        headers=headers,
        json={
            "name": "wallet",
            "coin": "XMR",
            "address": "47oldpublicwalletaddress0000000000000000000000",
            "note": "",
        },
    ).json()
    sheet = client.post(
        "/api/flight-sheets",
        headers=headers,
        json={
            "name": "sheet",
            "miner_type": "generic",
            "coin": "XMR",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
            "miner_profile": "default",
            "worker_name_template": "{hostname}",
            "extra_config": {},
        },
    ).json()
    return wallet, sheet


def test_offline_and_stopped_devices_keep_latest_refresh_until_running(
    client: TestClient,
):
    headers = login(client)
    wallet, sheet = wallet_and_sheet(client, headers)
    active_id, active_secret = enroll(client, headers, "active")
    stopped_id, stopped_secret = enroll(client, headers, "stopped")
    offline_id, offline_secret = enroll(client, headers, "offline")
    assert heartbeat(client, active_id, active_secret, sheet["id"], "running").status_code == 200
    assert heartbeat(client, stopped_id, stopped_secret, sheet["id"], "stopped").status_code == 200
    assert heartbeat(client, offline_id, offline_secret, sheet["id"], "running").status_code == 200

    database = client.app.state.database
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with database.connection() as db:
        db.execute("UPDATE agents SET last_seen=? WHERE id=?", (old, offline_id))

    changed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=headers,
        json={
            **{key: wallet[key] for key in ("name", "coin", "note")},
            "address": "48newpublicwalletaddress0000000000000000000000",
        },
    )
    assert changed.status_code == 200, changed.text
    result = changed.json()["reapply"]
    assert result["marked"] == 3
    assert result["matched"] == 1
    assert result["queued"] == 1
    assert result["pending"] == 2

    markers = {
        item["id"]: item["configuration_refresh"]
        for item in client.get("/api/agents", headers=headers).json()
    }
    assert markers[active_id]["status"] == "queued"
    assert markers[stopped_id]["status"] == "pending"
    assert markers[offline_id]["status"] == "pending"

    delivered = heartbeat(client, active_id, active_secret, sheet["id"], "running").json()
    command_id = delivered["commands"][0]["id"]
    running = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers={"X-Agent-ID": active_id, "Authorization": f"Bearer {active_secret}"},
        json={"status": "running", "result": {}, "error": None},
    )
    assert running.status_code == 200, running.text
    assert client.get(f"/api/agents/{active_id}", headers=headers).json()[
        "configuration_refresh"
    ]["status"] == "running"
    succeeded = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers={"X-Agent-ID": active_id, "Authorization": f"Bearer {active_secret}"},
        json={"status": "succeeded", "result": {}, "error": None},
    )
    assert succeeded.status_code == 200, succeeded.text
    assert client.get(f"/api/agents/{active_id}", headers=headers).json()[
        "configuration_refresh"
    ]["status"] == "succeeded"

    # First running heartbeat establishes truth; the next heartbeat reconciles
    # and dispatches the persisted newest configuration.
    assert heartbeat(client, stopped_id, stopped_secret, sheet["id"], "running").json()["commands"] == []
    stopped_delivery = heartbeat(
        client, stopped_id, stopped_secret, sheet["id"], "running"
    ).json()["commands"]
    assert [item["action"] for item in stopped_delivery] == ["apply_flight_sheet"]
    assert stopped_delivery[0]["payload"]["primary"]["wallet_address"].startswith("48new")

    assert heartbeat(client, offline_id, offline_secret, sheet["id"], "running").json()["commands"] == []
    offline_delivery = heartbeat(
        client, offline_id, offline_secret, sheet["id"], "running"
    ).json()["commands"]
    assert [item["action"] for item in offline_delivery] == ["apply_flight_sheet"]


def test_remove_flight_sheet_cancels_pending_refresh_and_requires_061(
    client: TestClient,
):
    headers = login(client)
    wallet, sheet = wallet_and_sheet(client, headers)
    agent_id, secret = enroll(client, headers, "remove")
    heartbeat(client, agent_id, secret, sheet["id"], "running")
    changed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=headers,
        json={
            **{key: wallet[key] for key in ("name", "coin", "note")},
            "address": "49removependingwallet00000000000000000000000",
        },
    )
    assert changed.json()["reapply"]["queued"] == 1

    removed = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=headers,
        json={"action": "remove_flight_sheet", "payload": {}},
    )
    assert removed.status_code == 201, removed.text
    assert client.get(f"/api/agents/{agent_id}", headers=headers).json()[
        "configuration_refresh"
    ] is None
    commands = heartbeat(client, agent_id, secret, sheet["id"], "running").json()["commands"]
    assert [item["action"] for item in commands] == ["remove_flight_sheet"]

    old_id, _old_secret = enroll(client, headers, "old", version="0.6.0")
    heartbeat(client, old_id, _old_secret, sheet["id"], "stopped", version="0.6.0")
    rejected = client.post(
        f"/api/agents/{old_id}/commands",
        headers=headers,
        json={"action": "remove_flight_sheet", "payload": {}},
    )
    assert rejected.status_code == 409
    assert "0.6.1" in rejected.text


def test_entity_update_rolls_back_if_refresh_outbox_write_fails_then_recovers(
    client: TestClient,
):
    headers = login(client)
    wallet, sheet = wallet_and_sheet(client, headers)
    agent_id, secret = enroll(client, headers, "atomic")
    heartbeat(client, agent_id, secret, sheet["id"], "stopped")
    database = client.app.state.database
    with database.connection() as db:
        db.execute(
            """CREATE TRIGGER fail_refresh_insert
               BEFORE INSERT ON configuration_refreshes
               BEGIN SELECT RAISE(ABORT, 'injected refresh failure'); END"""
        )
    body = {
        **{key: wallet[key] for key in ("name", "coin", "note")},
        "address": "49atomicwalletaddress000000000000000000000000",
    }
    with pytest.raises(sqlite3.IntegrityError, match="injected refresh failure"):
        client.put(f"/api/wallets/{wallet['id']}", headers=headers, json=body)
    unchanged = client.get("/api/wallets", headers=headers).json()[0]
    assert unchanged["address"] == wallet["address"]
    assert client.get(f"/api/agents/{agent_id}", headers=headers).json()[
        "configuration_refresh"
    ] is None

    with database.connection() as db:
        db.execute("DROP TRIGGER fail_refresh_insert")
    recovered = client.put(
        f"/api/wallets/{wallet['id']}", headers=headers, json=body
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["reapply"]["pending"] == 1


def test_concurrent_running_heartbeats_bind_one_live_refresh_command(
    client: TestClient,
):
    headers = login(client)
    wallet, sheet = wallet_and_sheet(client, headers)
    agent_id, secret = enroll(client, headers, "concurrent")
    heartbeat(client, agent_id, secret, sheet["id"], "stopped")
    changed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=headers,
        json={
            **{key: wallet[key] for key in ("name", "coin", "note")},
            "address": "49concurrentwallet00000000000000000000000000",
        },
    )
    assert changed.json()["reapply"]["pending"] == 1
    assert heartbeat(client, agent_id, secret, sheet["id"], "running").json()[
        "commands"
    ] == []

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _index: heartbeat(
                    client, agent_id, secret, sheet["id"], "running"
                ),
                range(2),
            )
        )
    assert all(response.status_code == 200 for response in responses)
    history = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=headers
    ).json()
    applies = [item for item in history if item["action"] == "apply_flight_sheet"]
    assert len(applies) == 1
    assert applies[0]["status"] in {"queued", "dispatched"}
    marker = client.get(f"/api/agents/{agent_id}", headers=headers).json()[
        "configuration_refresh"
    ]
    assert marker["command_id"] == applies[0]["id"]
    assert marker["status"] in {"queued", "dispatched"}


def test_current_stopped_heartbeat_cannot_reapply_from_previous_running_state(
    client: TestClient,
):
    headers = login(client)
    wallet, sheet = wallet_and_sheet(client, headers)
    agent_id, secret = enroll(client, headers, "stopped-after-running")
    assert heartbeat(
        client, agent_id, secret, sheet["id"], "stopped"
    ).status_code == 200
    changed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=headers,
        json={
            **{key: wallet[key] for key in ("name", "coin", "note")},
            "address": "49currentstopguard000000000000000000000000000",
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["reapply"]["pending"] == 1

    # The first running heartbeat updates controller truth.  If the miner is
    # already stopped by the following heartbeat, the previous running row
    # must not cause an apply command to be queued.
    assert heartbeat(
        client, agent_id, secret, sheet["id"], "running"
    ).json()["commands"] == []
    stopped = heartbeat(client, agent_id, secret, sheet["id"], "stopped")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["commands"] == []
    marker = client.get(
        f"/api/agents/{agent_id}", headers=headers
    ).json()["configuration_refresh"]
    assert marker["status"] == "pending"
    commands = client.get(
        "/api/commands", params={"agent_id": agent_id}, headers=headers
    ).json()
    assert not any(item["action"] == "apply_flight_sheet" for item in commands)


def test_stop_cancels_expired_manual_apply_intent_but_keeps_config_refreshes(
    client: TestClient,
):
    headers = login(client)
    wallet, sheet = wallet_and_sheet(client, headers)
    agent_id, secret = enroll(client, headers, "stop-expired-desired")
    assert heartbeat(
        client, agent_id, secret, sheet["id"], "running"
    ).status_code == 200
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=headers,
    )
    assert applied.status_code == 201, applied.text
    with client.app.state.database.connection() as db:
        db.execute(
            """UPDATE commands SET expires_at='2000-01-01T00:00:00+00:00'
               WHERE id=?""",
            (applied.json()["id"],),
        )

    stopped = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=headers,
        json={"action": "stop_mining", "payload": {}},
    )
    assert stopped.status_code == 201, stopped.text
    marker = client.get(
        f"/api/agents/{agent_id}", headers=headers
    ).json()["configuration_refresh"]
    assert marker["status"] == "cancelled"
    assert marker["command_id"] is None
    assert "mining stop" in marker["last_error"]

    # A wallet-triggered durable refresh is not a manual start intent and must
    # remain pending for the next deliberate mining start.
    changed = client.put(
        f"/api/wallets/{wallet['id']}",
        headers=headers,
        json={
            **{key: wallet[key] for key in ("name", "coin", "note")},
            "address": "49pendingafterstop000000000000000000000000000",
        },
    )
    assert changed.status_code == 200, changed.text
    marker = client.get(
        f"/api/agents/{agent_id}", headers=headers
    ).json()["configuration_refresh"]
    assert marker["trigger_type"] == "wallet"
    assert marker["status"] == "pending"


def test_abandoning_inflight_apply_preserves_uncertain_desired_refresh(
    client: TestClient,
):
    headers = login(client)
    _wallet, sheet = wallet_and_sheet(client, headers)
    agent_id, secret = enroll(client, headers, "abandon-apply")
    assert heartbeat(
        client, agent_id, secret, sheet["id"], "running"
    ).status_code == 200
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=headers,
    )
    assert applied.status_code == 201, applied.text
    delivered = heartbeat(
        client, agent_id, secret, sheet["id"], "running"
    ).json()["commands"]
    assert [item["id"] for item in delivered] == [applied.json()["id"]]
    old = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat(timespec="seconds")
    with client.app.state.database.connection() as db:
        db.execute("UPDATE agents SET last_seen=? WHERE id=?", (old, agent_id))

    abandoned = client.post(
        f"/api/commands/{applied.json()['id']}/abandon",
        headers=headers,
    )
    assert abandoned.status_code == 200, abandoned.text
    marker = client.get(
        f"/api/agents/{agent_id}", headers=headers
    ).json()["configuration_refresh"]
    assert marker["status"] == "pending"
    assert marker["command_id"] is None
    assert "waiting to verify" in marker["last_error"]


def test_configuration_refresh_generation_fences_a_to_b_to_a_aba(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    headers = login(client)
    wallet, sheet = wallet_and_sheet(client, headers)
    agent_id, secret = enroll(client, headers, "aba")
    assert heartbeat(
        client,
        agent_id,
        secret,
        sheet["id"],
        "running",
    ).status_code == 200

    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    original_render = main_module._render_worker_name

    def blocked_render(*args, **kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
            current = calls
        if current == 1:
            entered.set()
            assert release.wait(10)
        return original_render(*args, **kwargs)

    monkeypatch.setattr(main_module, "_render_worker_name", blocked_render)

    def update_wallet(address: str):
        return client.put(
            f"/api/wallets/{wallet['id']}",
            headers=headers,
            json={
                "name": wallet["name"],
                "coin": wallet["coin"],
                "address": address,
                "note": wallet["note"],
            },
        )

    first_address = "49aba-first-wallet000000000000000000000000000000"
    with ThreadPoolExecutor(max_workers=1) as pool:
        first_future = pool.submit(update_wallet, first_address)
        assert entered.wait(10)
        middle = update_wallet(
            "49aba-middle-wallet00000000000000000000000000000"
        )
        assert middle.status_code == 200, middle.text
        newest = update_wallet(first_address)
        assert newest.status_code == 200, newest.text
        release.set()
        first = first_future.result(timeout=10)
    assert first.status_code == 200, first.text

    with client.app.state.database.connection() as db:
        refresh = db.execute(
            """SELECT generation_id, claim_token, command_id, status
               FROM configuration_refreshes WHERE agent_id=?""",
            (agent_id,),
        ).fetchone()
        active = db.execute(
            """SELECT id,payload_json FROM commands
               WHERE agent_id=? AND action='apply_flight_sheet'
                 AND status IN ('queued','dispatched','running')""",
            (agent_id,),
        ).fetchall()
    assert refresh["generation_id"]
    assert refresh["claim_token"] is None
    assert refresh["status"] == "queued"
    assert len(active) == 1
    assert active[0]["id"] == refresh["command_id"]
    assert first_address in active[0]["payload_json"]
