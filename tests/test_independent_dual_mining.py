from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import main as main_module
from server.config import Settings
from server.main import create_app
from server.managed_miners import ManagedMinerCatalog
from controller_test_support import auth, login as _login, controller_settings_kwargs
from miner_test_support import package_download


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def offline_catalog(owner: str, repo: str):
        del owner, repo
        raise OSError("offline test catalog")

    catalog = ManagedMinerCatalog(fetcher=offline_catalog)
    monkeypatch.setattr(main_module, "ManagedMinerCatalog", lambda: catalog)

    monkeypatch.setattr(main_module, "_download_https_package", package_download(b'managed miner test archive'))
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client




def login(client: TestClient) -> str:
    return _login(client, "admin", "correct-horse-battery-staple")


def create_wallet(
    client: TestClient, token: str, *, coin: str, name: str
) -> str:
    response = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": name,
            "coin": coin,
            "address": f"{coin.lower()}-test-wallet-address",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def enroll_linux_agent(
    client: TestClient, token: str, *, version: str,
    extra_args_capability: bool | None = None,
) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": f"dual-{version}", "ttl_minutes": 30},
    ).json()
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued["token"],
            "name": f"dual-{version}",
            "hostname": f"dual-{version}.local",
            "os_family": "linux",
            "agent_version": version,
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    credentials = enrolled.json()
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": credentials["agent_id"],
            "Authorization": f"Bearer {credentials['agent_secret']}",
        },
        json={
            "hostname": f"dual-{version}.local",
            "os_family": "linux",
            "platform": "linux",
            "agent_version": version,
            "capabilities": (
                [
                    "managed_mining.dual_process.v1",
                    *(
                        ["managed_mining.independent_srb_gpu_oc.v1"]
                        if tuple(int(part) for part in version.split("."))
                        >= (0, 6, 57)
                        else []
                    ),
                    *(
                        ["managed_mining.extra_args.v1"]
                        if (extra_args_capability if extra_args_capability is not None
                            else tuple(int(part) for part in version.split(".")) >= (0, 8, 31))
                        else []
                    ),
                ]
                if tuple(int(part) for part in version.split(".")) >= (0, 6, 48)
                else []
            ),
            "metrics": {
                "gpus": [
                    {
                        "index": 0,
                        "vendor": "nvidia",
                        "device_type": "discrete",
                        "name": "NVIDIA GeForce RTX 5080",
                    }
                ]
            },
            "inventory": {},
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return credentials["agent_id"], credentials["agent_secret"]


def independent_xmrig_pearl_payload(
    xmr_wallet_id: str, pearl_wallet_id: str
) -> dict[str, object]:
    return {
        "name": "XMR CPU + PEARL GPU",
        "secondary_mode": "independent",
        "miner_type": "xmrig",
        "miner_version": "6.26.0",
        "coin": "XMR",
        "wallet_id": xmr_wallet_id,
        "pool_url": "stratum+ssl://xmr.pool.example:443",
        "worker_name_template": "{hostname}",
        "secondary": {
            "miner_type": "pearlminer",
            "miner_version": "1.2.4",
            "coin": "PEARL",
            "wallet_id": pearl_wallet_id,
            "pool_url": "pearl://global.pearlfortune.org:443",
            "algorithm": "pearlhash",
            "extra_config": {
                "gpu_backend": "nvidia",
                "cuda_variant": "cuda13",
            },
        },
    }


@pytest.mark.parametrize("clear_value", ["", None])
def test_independent_extra_arguments_save_edit_clear_and_apply_are_per_leg(client, clear_value):
    token = login(client)
    xmr_wallet = create_wallet(client, token, coin="XMR", name="Arguments XMR wallet")
    pearl_wallet = create_wallet(client, token, coin="PEARL", name="Arguments PEARL wallet")
    body = independent_xmrig_pearl_payload(xmr_wallet, pearl_wallet)
    primary_args = '--futureflag 7 --future-toggle --label "primary quoted value"'
    secondary_args = "--futureflag=8 --label 'secondary quoted value'"
    body["extra_config"] = {"cpu_threads": 2, "extra_args": primary_args}
    body["secondary"]["extra_config"]["extra_args"] = secondary_args
    created = client.post("/api/flight-sheets", headers=auth(token), json=body)
    assert created.status_code == 201, created.text
    sheet_id = created.json()["id"]
    agent_id, _ = enroll_linux_agent(client, token, version="0.8.31")

    for primary, secondary in [
        (primary_args, secondary_args),
        ('--futureflag=9 --label "edited primary"', "--future-toggle --label 'edited secondary'"),
        (clear_value, clear_value),
    ]:
        body["extra_config"]["extra_args"] = primary
        body["secondary"]["extra_config"]["extra_args"] = secondary
        edited = client.put(f"/api/flight-sheets/{sheet_id}", headers=auth(token), json=body)
        assert edited.status_code == 200, edited.text
        listed = client.get("/api/flight-sheets", headers=auth(token))
        assert listed.status_code == 200, listed.text
        sheet = next(row for row in listed.json() if row["id"] == sheet_id)
        assert sheet["extra_config"]["extra_args"] == (primary or "")
        assert sheet["secondary"]["extra_config"]["extra_args"] == (secondary or "")
        assert sheet["extra_config"]["cpu_threads"] == 2
        assert sheet["secondary"]["extra_config"]["cuda_variant"] == "cuda13"
        applied = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply", headers=auth(token),
        )
        assert applied.status_code == 201, applied.text
        for role, expected in [("primary", primary), ("secondary", secondary)]:
            options = applied.json()["payload"][role]["extra_config"]
            if expected:
                assert options["extra_args"] == expected
            else:
                assert "extra_args" not in options
    noop = client.put(f"/api/flight-sheets/{sheet_id}", headers=auth(token), json=body)
    assert noop.status_code == 200, noop.text
    assert noop.json()["changed_fields"] == []
    assert noop.json()["reapply"]["triggered"] is False


@pytest.mark.parametrize("role,arguments", [
    ("primary", "--url=stratum+tcp://override.example.test:3333"),
    ("secondary", "--address overridden-wallet"),
    ("primary", '--label "unclosed'),
    ("secondary", 12),
])
def test_independent_extra_arguments_are_validated_before_create_and_update(client, role, arguments):
    token = login(client)
    xmr_wallet = create_wallet(client, token, coin="XMR", name="Validation XMR wallet")
    pearl_wallet = create_wallet(client, token, coin="PEARL", name="Validation PEARL wallet")
    body = independent_xmrig_pearl_payload(xmr_wallet, pearl_wallet)
    body["extra_config"] = {"extra_args": "--futureflag preserved"}
    body["secondary"]["extra_config"]["extra_args"] = "--futureflag secondary"
    valid = client.post("/api/flight-sheets", headers=auth(token), json=body)
    assert valid.status_code == 201, valid.text
    sheet_id = valid.json()["id"]
    (body if role == "primary" else body["secondary"])["extra_config"]["extra_args"] = arguments
    for method, path in [(client.post, "/api/flight-sheets"),
                         (client.put, f"/api/flight-sheets/{sheet_id}")]:
        rejected = method(path, headers=auth(token), json=body)
        assert rejected.status_code == 422, rejected.text
        assert "extra_args" in rejected.text
    listed = client.get("/api/flight-sheets", headers=auth(token))
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
    row = listed.json()[0]
    assert row["extra_config"]["extra_args"] == "--futureflag preserved"
    assert row["secondary"]["extra_config"]["extra_args"] == "--futureflag secondary"


@pytest.mark.parametrize("role", ["primary", "secondary"])
def test_invalid_historical_extra_arguments_remain_readable_but_cannot_apply(client, monkeypatch, role):
    token = login(client)
    xmr_wallet = create_wallet(client, token, coin="XMR", name="Legacy arguments XMR wallet")
    pearl_wallet = create_wallet(client, token, coin="PEARL", name="Legacy arguments PEARL wallet")
    created = client.post("/api/flight-sheets", headers=auth(token),
                          json=independent_xmrig_pearl_payload(xmr_wallet, pearl_wallet))
    assert created.status_code == 201, created.text
    sheet = created.json()
    leg = sheet if role == "primary" else sheet["secondary"]
    historical_options = {**leg["extra_config"], "extra_args": '--futureflag "unclosed'}
    column = "extra_config_json" if role == "primary" else "secondary_extra_config_json"
    with client.app.state.db.connection() as db:
        db.execute(f"UPDATE flight_sheets SET {column}=? WHERE id=?",
                   (json.dumps(historical_options), sheet["id"]))
    listed = client.get("/api/flight-sheets", headers=auth(token))
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == sheet["id"])
    assert (row if role == "primary" else row["secondary"])["extra_config"] == historical_options
    agent_id, _ = enroll_linux_agent(client, token, version="0.8.31")
    monkeypatch.setattr(main_module, "_download_https_package", lambda *args, **kwargs:
                        pytest.fail("invalid saved extra_args must fail before package download"))
    applied = client.post(f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply", headers=auth(token))
    assert applied.status_code == 409, applied.text
    assert "extra_args" in applied.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
        assert json.loads(db.execute(f"SELECT {column} FROM flight_sheets WHERE id=?",
                                    (sheet["id"],)).fetchone()[0]) == historical_options


@pytest.mark.parametrize("role", ["primary", "secondary"])
@pytest.mark.parametrize("version,capable", [("0.8.30", True), ("0.8.31", False)])
def test_extra_arguments_require_version_and_capability_before_package_download(
    client, monkeypatch, role, version, capable,
):
    token = login(client)
    xmr = create_wallet(client, token, coin="XMR", name="Compatibility XMR wallet")
    pearl = create_wallet(client, token, coin="PEARL", name="Compatibility PEARL wallet")
    body = independent_xmrig_pearl_payload(xmr, pearl)
    leg = body if role == "primary" else body["secondary"]
    leg.setdefault("extra_config", {})["extra_args"] = "--futureflag quoted-value"
    created = client.post("/api/flight-sheets", headers=auth(token), json=body)
    assert created.status_code == 201, created.text
    agent, _ = enroll_linux_agent(client, token, version=version, extra_args_capability=capable)
    monkeypatch.setattr(main_module, "_download_https_package", lambda *args, **kwargs:
                        pytest.fail("unsupported extra_args target must fail before download"))
    applied = client.post(f"/api/agents/{agent}/flight-sheets/{created.json()['id']}/apply", headers=auth(token))
    assert applied.status_code == 409, applied.text
    assert "extra_args" in applied.text or "额外参数" in applied.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0


@pytest.mark.parametrize("explicit_blank", [False, True])
def test_blank_extra_arguments_keep_old_agent_compatible_and_are_omitted_only_from_wire(client, explicit_blank):
    from test_config_reapply import heartbeat

    token = login(client)
    xmr = create_wallet(client, token, coin="XMR", name="Blank XMR wallet")
    pearl = create_wallet(client, token, coin="PEARL", name="Blank PEARL wallet")
    body = independent_xmrig_pearl_payload(xmr, pearl)
    if explicit_blank:
        body["extra_config"] = {"extra_args": ""}
        body["secondary"]["extra_config"]["extra_args"] = ""
    created = client.post("/api/flight-sheets", headers=auth(token), json=body)
    assert created.status_code == 201, created.text
    agent, secret = enroll_linux_agent(client, token, version="0.8.30", extra_args_capability=False)
    applied = client.post(f"/api/agents/{agent}/flight-sheets/{created.json()['id']}/apply", headers=auth(token))
    assert applied.status_code == 201, applied.text
    for role in ("primary", "secondary"):
        assert "extra_args" not in applied.json()["payload"][role]["extra_config"]
    delivered = heartbeat(client, agent, secret, {"configured": False, "state": "stopped"},
        os_family="linux", agent_version="0.8.30",
        capabilities=["managed_mining.dual_process.v1", "managed_mining.independent_srb_gpu_oc.v1"],
        metrics={"gpus": [{"index": 0, "vendor": "NVIDIA", "name": "Synthetic NVIDIA GPU", "device_type": "discrete"}]})
    assert delivered.status_code == 200, delivered.text
    assert [command["id"] for command in delivered.json()["commands"]] == [applied.json()["id"]]
    wire = delivered.json()["commands"][0]["payload"]
    for role in ("primary", "secondary"):
        assert "extra_args" not in wire[role]["extra_config"]
    assert not any(key.startswith("_controller_") for key in wire)
    listed = client.get("/api/flight-sheets", headers=auth(token))
    assert listed.status_code == 200, listed.text
    row = next(sheet for sheet in listed.json() if sheet["id"] == created.json()["id"])
    assert row["extra_config"]["extra_args"] == ""
    assert row["secondary"]["extra_config"]["extra_args"] == ""
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT extra_config_json,secondary_extra_config_json FROM flight_sheets WHERE id=?",
                            (created.json()["id"],)).fetchone()
        persisted_command = json.loads(db.execute("SELECT payload_json FROM commands WHERE id=?",
                                                 (applied.json()["id"],)).fetchone()[0])
    assert all(json.loads(options)["extra_args"] == "" for options in stored)
    assert any(key.startswith("_controller_") for key in persisted_command)


@pytest.mark.parametrize("role,change", [("primary", "version"), ("secondary", "capability")])
def test_extra_argument_target_is_rechecked_after_package_preparation(client, monkeypatch, role, change):
    token = login(client)
    xmr = create_wallet(client, token, coin="XMR", name="Final gate XMR wallet")
    pearl = create_wallet(client, token, coin="PEARL", name="Final gate PEARL wallet")
    body = independent_xmrig_pearl_payload(xmr, pearl)
    (body if role == "primary" else body["secondary"]).setdefault("extra_config", {})["extra_args"] = "--futureflag final"
    created = client.post("/api/flight-sheets", headers=auth(token), json=body)
    assert created.status_code == 201, created.text
    agent, _ = enroll_linux_agent(client, token, version="0.8.31")
    download = package_download(b"synthetic compatibility-race archive")
    changed = []

    def change_target_during_download(*args, **kwargs):
        result = download(*args, **kwargs)
        if not changed:
            with client.app.state.db.connection() as db:
                if change == "version":
                    db.execute("UPDATE agents SET agent_version='0.8.30' WHERE id=?", (agent,))
                else:
                    capabilities = json.loads(db.execute("SELECT capabilities_json FROM agents WHERE id=?", (agent,)).fetchone()[0])
                    capabilities.remove("managed_mining.extra_args.v1")
                    db.execute("UPDATE agents SET capabilities_json=? WHERE id=?", (json.dumps(capabilities), agent))
            changed.append(change)
        return result

    monkeypatch.setattr(main_module, "_download_https_package", change_target_during_download)
    applied = client.post(f"/api/agents/{agent}/flight-sheets/{created.json()['id']}/apply", headers=auth(token))
    assert changed == [change]
    assert applied.status_code == 409, applied.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0


@pytest.mark.parametrize("role", ["primary", "secondary"])
@pytest.mark.parametrize("change", ["version", "capability"])
def test_queued_extra_argument_command_is_failed_if_target_loses_support(client, role, change):
    from test_config_reapply import heartbeat

    token = login(client)
    xmr = create_wallet(client, token, coin="XMR", name="Dispatch gate XMR wallet")
    pearl = create_wallet(client, token, coin="PEARL", name="Dispatch gate PEARL wallet")
    body = independent_xmrig_pearl_payload(xmr, pearl)
    (body if role == "primary" else body["secondary"]).setdefault("extra_config", {})["extra_args"] = "--futureflag queued"
    created = client.post("/api/flight-sheets", headers=auth(token), json=body)
    assert created.status_code == 201, created.text
    agent, secret = enroll_linux_agent(client, token, version="0.8.31")
    applied = client.post(f"/api/agents/{agent}/flight-sheets/{created.json()['id']}/apply", headers=auth(token))
    assert applied.status_code == 201, applied.text
    command_id = applied.json()["id"]
    with client.app.state.db.connection() as db:
        payload_before = db.execute("SELECT payload_json FROM commands WHERE id=?", (command_id,)).fetchone()[0]
    capabilities = ["managed_mining.dual_process.v1", "managed_mining.independent_srb_gpu_oc.v1"]
    if change == "version":
        capabilities.append("managed_mining.extra_args.v1")
    for _ in range(2):
        delivered = heartbeat(client, agent, secret, {"configured": False, "state": "stopped"},
            os_family="linux", agent_version="0.8.30" if change == "version" else "0.8.31",
            capabilities=capabilities,
            metrics={"gpus": [{"index": 0, "vendor": "NVIDIA", "name": "Synthetic NVIDIA GPU", "device_type": "discrete"}]})
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["commands"] == []
    with client.app.state.db.connection() as db:
        stored = db.execute("SELECT status,error,payload_json FROM commands WHERE id=?", (command_id,)).fetchone()
        assert db.execute("SELECT count(*) FROM commands WHERE agent_id=? AND status IN ('queued','dispatched','running')",
                          (agent,)).fetchone()[0] == 0
    assert stored["status"] == "failed"
    assert ("0.8.31" if change == "version" else "managed_mining.extra_args.v1") in stored["error"]
    assert stored["payload_json"] == payload_before


def test_independent_xmrig_and_secondary_pearl_apply_uses_two_packages(
    client: TestClient,
):
    token = login(client)
    xmr_wallet_id = create_wallet(client, token, coin="XMR", name="XMR wallet")
    pearl_wallet_id = create_wallet(
        client, token, coin="PEARL", name="PEARL wallet"
    )
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=independent_xmrig_pearl_payload(xmr_wallet_id, pearl_wallet_id),
    )
    assert created.status_code == 201, created.text
    sheet = created.json()
    assert sheet["secondary_mode"] == "independent"
    updated = client.put(
        f"/api/flight-sheets/{sheet['id']}",
        headers=auth(token),
        json={
            **independent_xmrig_pearl_payload(xmr_wallet_id, pearl_wallet_id),
            "name": "Updated XMR CPU + PEARL GPU",
        },
    )
    assert updated.status_code == 200, updated.text
    sheet = updated.json()
    assert sheet["secondary_mode"] == "independent"
    assert "name" in sheet["changed_fields"]
    listed = client.get("/api/flight-sheets", headers=auth(token))
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["secondary_mode"] == "independent"

    agent_id, _agent_secret = enroll_linux_agent(
        client, token, version="0.6.48"
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    payload = applied.json()["payload"]
    assert payload["secondary_mode"] == "independent"
    assert payload["primary"]["miner_type"] == "xmrig"
    assert payload["secondary"]["miner_type"] == "pearlminer"
    assert (
        payload["secondary"]["miner_package"]["id"]
        == "builtin-pearlminer-1.2.4-linux-x64-nvidia"
    )
    assert payload["primary"]["miner_package"]["id"] != payload["secondary"][
        "miner_package"
    ]["id"]


def test_independent_dual_mining_requires_agent_0647_before_package_download(
    client: TestClient,
):
    token = login(client)
    xmr_wallet_id = create_wallet(client, token, coin="XMR", name="XMR wallet")
    pearl_wallet_id = create_wallet(
        client, token, coin="PEARL", name="PEARL wallet"
    )
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=independent_xmrig_pearl_payload(xmr_wallet_id, pearl_wallet_id),
    )
    assert created.status_code == 201, created.text
    agent_id, _agent_secret = enroll_linux_agent(
        client, token, version="0.6.46"
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 409, applied.text
    assert "0.6.48 or newer" in applied.text


def test_queued_independent_command_is_failed_if_device_rolls_back_before_dispatch(
    client: TestClient,
):
    token = login(client)
    xmr_wallet_id = create_wallet(client, token, coin="XMR", name="XMR wallet")
    pearl_wallet_id = create_wallet(
        client, token, coin="PEARL", name="PEARL wallet"
    )
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=independent_xmrig_pearl_payload(xmr_wallet_id, pearl_wallet_id),
    )
    assert created.status_code == 201, created.text
    agent_id, agent_secret = enroll_linux_agent(
        client, token, version="0.6.48"
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    command_id = applied.json()["id"]

    rolled_back = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
        json={
            "hostname": "dual-rollback.local",
            "os_family": "linux",
            "platform": "linux",
            "agent_version": "0.6.46",
            "metrics": {
                "gpus": [
                    {
                        "index": 0,
                        "vendor": "nvidia",
                        "device_type": "discrete",
                        "name": "NVIDIA GeForce RTX 5080",
                    }
                ]
            },
            "inventory": {},
        },
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["commands"] == []

    commands = client.get(
        "/api/commands",
        headers=auth(token),
        params={"agent_id": agent_id, "include_all_active": "true"},
    )
    assert commands.status_code == 200, commands.text
    rejected = next(item for item in commands.json() if item["id"] == command_id)
    assert rejected["status"] == "failed"
    assert "0.6.48 or newer" in rejected["error"]


def test_queued_managed_apply_waits_and_retries_after_capability_returns(
    client: TestClient,
):
    token = login(client)
    xmr_wallet_id = create_wallet(client, token, coin="XMR", name="XMR wallet")
    pearl_wallet_id = create_wallet(
        client, token, coin="PEARL", name="PEARL wallet"
    )
    sheet = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=independent_xmrig_pearl_payload(xmr_wallet_id, pearl_wallet_id),
    ).json()
    agent_id, agent_secret = enroll_linux_agent(
        client, token, version="0.6.48"
    )
    original = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert original.status_code == 201, original.text

    def report(capabilities: list[str]):
        return client.post(
            "/api/agent/heartbeat",
            headers={
                "X-Agent-ID": agent_id,
                "Authorization": f"Bearer {agent_secret}",
            },
            json={
                "hostname": "dual-capability.local",
                "os_family": "linux",
                "platform": "linux",
                "agent_version": "0.6.48",
                "capabilities": capabilities,
                "metrics": {
                    "gpus": [
                        {
                            "index": 0,
                            "vendor": "nvidia",
                            "device_type": "discrete",
                            "name": "NVIDIA GeForce RTX 5080",
                        }
                    ]
                },
                "inventory": {},
            },
        )

    missing = report([])
    assert missing.status_code == 200, missing.text
    assert missing.json()["commands"] == []
    marker = client.get(
        f"/api/agents/{agent_id}", headers=auth(token)
    ).json()["configuration_refresh"]
    assert marker["status"] == "pending"
    assert marker["command_id"] is None
    assert "advertise managed mining" in marker["last_error"]

    restored = report(["managed_mining.dual_process.v1"])
    assert restored.status_code == 200, restored.text
    replacement = restored.json()["commands"]
    assert len(replacement) == 1
    assert replacement[0]["id"] != original.json()["id"]
    assert replacement[0]["payload"]["secondary_mode"] == "independent"


def test_independent_resource_validation_accepts_cpu_gpu_and_cpu_cpu(
    client: TestClient,
):
    token = login(client)
    xmr_wallet_id = create_wallet(client, token, coin="XMR", name="XMR wallet")
    pearl_wallet_id = create_wallet(
        client, token, coin="PEARL", name="PEARL wallet"
    )
    base = independent_xmrig_pearl_payload(xmr_wallet_id, pearl_wallet_id)
    srb_gpu = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **base,
            "name": "SRB GPU + XMR CPU",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "PEARL",
            "wallet_id": pearl_wallet_id,
            "pool_url": "stratum+ssl://pearl.pool.example:443",
            "algorithm": "pearlhash",
            "device_indices": [0],
            "extra_config": {},
            "secondary": {
                "miner_type": "xmrig",
                "miner_version": "6.26.0",
                "coin": "XMR",
                "wallet_id": xmr_wallet_id,
                "pool_url": "stratum+ssl://xmr.pool.example:443",
            },
        },
    )
    assert srb_gpu.status_code == 201, srb_gpu.text

    flight_scoped_oc = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **base,
            "name": "independent OC is device scoped",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "PEARL",
            "wallet_id": pearl_wallet_id,
            "pool_url": "stratum+ssl://pearl.pool.example:443",
            "algorithm": "pearlhash",
            "device_indices": [0],
            "extra_config": {},
            "gpu_oc": {
                "vendor": "nvidia",
                "settings": [{"index": 0, "power_limit_w": 180}],
            },
            "secondary": {
                "miner_type": "xmrig",
                "miner_version": "6.26.0",
                "coin": "XMR",
                "wallet_id": xmr_wallet_id,
                "pool_url": "stratum+ssl://xmr.pool.example:443",
            },
        },
    )
    assert flight_scoped_oc.status_code == 201, flight_scoped_oc.text
    assert flight_scoped_oc.json()["gpu_oc"]["settings"] == [
        {"index": 0, "power_limit_w": 180.0}
    ]

    old_agent_id, _old_secret = enroll_linux_agent(
        client, token, version="0.6.56"
    )
    old_apply = client.post(
        f"/api/agents/{old_agent_id}/flight-sheets/"
        f"{flight_scoped_oc.json()['id']}/apply",
        headers=auth(token),
    )
    assert old_apply.status_code == 409, old_apply.text
    assert "0.6.57 or newer" in old_apply.text

    new_agent_id, _new_secret = enroll_linux_agent(
        client, token, version="0.6.57"
    )
    new_apply = client.post(
        f"/api/agents/{new_agent_id}/flight-sheets/"
        f"{flight_scoped_oc.json()['id']}/apply",
        headers=auth(token),
    )
    assert new_apply.status_code == 201, new_apply.text
    dispatched = new_apply.json()["payload"]
    assert dispatched["gpu_oc"] == flight_scoped_oc.json()["gpu_oc"]
    assert dispatched["secondary_mode"] == "independent"
    with client.app.state.db.connection() as db:
        stored_command = db.execute(
            "SELECT payload_json FROM commands WHERE id=?",
            (new_apply.json()["id"],),
        ).fetchone()
    internal_payload = json.loads(stored_command["payload_json"])
    assert (
        internal_payload["_controller_flight_dispatch_v1"]["minimum_agent_version"]
        == "0.6.57"
    )
    assert (
        "managed_mining.independent_srb_gpu_oc.v1"
        in internal_payload["_controller_flight_dispatch_v1"][
            "required_capabilities"
        ]
    )

    cpu_cpu = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **base,
            "name": "two CPU miners",
            "miner_type": "xmrig",
            "miner_version": "6.26.0",
            "coin": "XMR",
            "wallet_id": xmr_wallet_id,
            "pool_url": "stratum+ssl://xmr-primary.pool.example:443",
            "algorithm": None,
            "device_indices": [],
            "extra_config": {},
            "secondary": {
                "miner_type": "srbminer",
                "miner_version": "3.4.6",
                "coin": "XMR",
                "wallet_id": xmr_wallet_id,
                "pool_url": "stratum+ssl://xmr-secondary.pool.example:443",
                "algorithm": "randomx",
            },
        },
    )
    assert cpu_cpu.status_code == 201, cpu_cpu.text
    assert cpu_cpu.json()["secondary_mode"] == "independent"

    cpu_first_gpu_second = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **base,
            "name": "CPU first is unsafe",
            "miner_type": "xmrig",
            "miner_version": "6.26.0",
            "coin": "XMR",
            "wallet_id": xmr_wallet_id,
            "pool_url": "stratum+ssl://xmr-primary.pool.example:443",
            "algorithm": None,
            "extra_config": {},
            "secondary": {
                "miner_type": "pearlminer",
                "miner_version": "1.2.4",
                "coin": "PEARL",
                "wallet_id": pearl_wallet_id,
                "pool_url": "pearl://global.pearlfortune.org:443",
                "algorithm": "pearlhash",
                "extra_config": {
                    "gpu_backend": "nvidia",
                    "cuda_variant": "cuda13",
                },
            },
        },
    )
    assert cpu_first_gpu_second.status_code == 201, cpu_first_gpu_second.text


def test_independent_gpu_pairs_need_provably_disjoint_srb_indices(
    client: TestClient,
):
    token = login(client)
    pearl_wallet_id = create_wallet(
        client, token, coin="PEARL", name="PEARL wallet"
    )
    payload = {
        "name": "two GPU miners",
        "secondary_mode": "independent",
        "miner_type": "srbminer",
        "miner_version": "3.4.6",
        "coin": "PEARL",
        "wallet_id": pearl_wallet_id,
        "pool_url": "stratum+ssl://pearl-primary.pool.example:443",
        "algorithm": "pearlhash",
        "device_indices": [0],
        "secondary": {
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "PEARL",
            "wallet_id": pearl_wallet_id,
            "pool_url": "stratum+ssl://pearl-secondary.pool.example:443",
            "algorithm": "pearlhash",
            "device_indices": [0],
        },
    }
    overlap = client.post(
        "/api/flight-sheets", headers=auth(token), json=payload
    )
    assert overlap.status_code == 422, overlap.text
    assert "non-overlapping" in overlap.text
    from pydantic import ValidationError
    from server.miner_adapters import MINER_ADAPTERS
    from server.schemas import FlightSheetCreate
    from server.schemas_v2 import FlightSheetV2Create, adapter_public_version

    adapter = MINER_ADAPTERS.require("srbminer")
    jobs = [
        {
            "job_id": f"job_{position}", "adapter_id": adapter.adapter_id,
            "adapter_revision": adapter_public_version(adapter),
            "miner_version": leg["miner_version"], "resource": "gpu",
            "coin": leg["coin"], "wallet_id": leg["wallet_id"],
            "pool_url": leg["pool_url"], "algorithm": leg["algorithm"],
            "miner_package_id": "builtin-srbminer-3.4.6-linux-x64",
            "devices": leg["device_indices"], "options": {},
        }
        for position, leg in enumerate((payload, payload["secondary"]))
    ]
    with pytest.raises(ValidationError, match="conflicting device ownership"):
        FlightSheetV2Create.model_validate({"name": "Overlapping SRB GPU pair", "jobs": jobs})
    jobs[1]["devices"] = [1]
    assert len(FlightSheetV2Create.model_validate({"name": "Disjoint SRB GPU pair", "jobs": jobs}).jobs) == 2
    assert FlightSheetCreate.model_validate({
        **payload, "secondary": {**payload["secondary"], "device_indices": [1]},
    }).secondary is not None

    disjoint = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **payload,
            "name": "two disjoint GPU miners",
            "secondary": {**payload["secondary"], "device_indices": [1]},
        },
    )
    assert disjoint.status_code == 201, disjoint.text

    ambiguous_oc = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **payload,
            "name": "two GPU miners cannot share top-level OC",
            "secondary": {**payload["secondary"], "device_indices": [1]},
            "gpu_oc": {
                "vendor": "nvidia",
                "settings": [{"model_match": "5080", "power_limit_w": 250}],
            },
        },
    )
    assert ambiguous_oc.status_code == 422, ambiguous_oc.text
    assert "CPU-only second miner" in ambiguous_oc.text
