from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server import main as main_module
from server.config import Settings
from server.main import create_app
from server.managed_miners import ManagedMinerCatalog
from server.schemas import FlightGpuOc, FlightSheetCreate
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




def login(
    client: TestClient,
    username: str = "admin",
    password: str = "correct-horse-battery-staple",
) -> str:
    return _login(client, username, password)


def wallet(client: TestClient, token: str, coin: str) -> dict[str, object]:
    response = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": f"{coin} public wallet",
            "coin": coin,
            "address": f"public-{coin.lower()}-wallet-address",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def enroll_linux_agent(
    client: TestClient,
    token: str,
    agent_version: str = "0.5.3",
) -> str:
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
            "os_family": "linux",
            "agent_version": agent_version,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"]


def srb_payload(wallet_id: str) -> dict[str, object]:
    return {
        "name": "ETC managed OC",
        "miner_type": "srbminer",
        "miner_version": "3.4.6",
        "coin": "ETC",
        "wallet_id": wallet_id,
        "pool_url": "stratum+ssl://pool.example.test:443",
        "algorithm": "etchash",
        "device_indices": [0, 2],
        "gpu_oc": {
            "vendor": "nvidia",
            "settings": [
                {
                    "index": 0,
                    "power_limit_w": 125,
                    "core_clock_offset_mhz": 100,
                    "memory_clock_offset_mhz": 800,
                },
                {
                    "index": 2,
                    "power_limit_w": 115,
                    "core_clock_offset_mhz": 80,
                    "memory_clock_offset_mhz": 700,
                },
            ],
        },
    }


@pytest.mark.parametrize("setting", [
    {"index": 0, "fan_percent": 65},
    {"model_match": "RTX 5080", "power_limit_w": 120, "fan_percent": 65},
])
@pytest.mark.parametrize("miner_type", ["srbminer", "pearlminer", "xmrig", "seine"])
def test_managed_miners_cannot_add_flight_fan_even_with_clock_or_power_setting(
    miner_type, setting,
):
    coin, algorithm = {
        "srbminer": ("PEARL", "pearlhash"),
        "pearlminer": ("PEARL", "pearlhash"),
        "xmrig": ("XMR", "randomx"),
        "seine": ("BNT", "blocknet-argon2id"),
    }[miner_type]
    with pytest.raises(ValidationError, match="fan_percent"):
        FlightSheetCreate.model_validate({
            "name": "Unsupported flight fan", "miner_type": miner_type,
            "coin": coin, "algorithm": algorithm, "wallet_id": "wallet-public-id",
            "pool_url": "stratum+tcp://pool.example.test:3333",
            "gpu_oc": {"vendor": "nvidia", "settings": [setting]},
        })


@pytest.mark.parametrize("power", [5, 120, 900])
def test_srb_retains_five_clock_and_power_fields(power):
    setting = {
        "model_match": "RTX 5080", "power_limit_w": power,
        "core_clock_offset_mhz": 0, "core_clock_lock_mhz": 1500,
        "memory_clock_offset_mhz": -200, "memory_clock_lock_mhz": 810,
    }
    payload = srb_payload("wallet-public-id")
    payload.update(coin="PEARL", algorithm="pearlhash", device_indices=[])
    payload["gpu_oc"] = {"vendor": "nvidia", "settings": [setting]}
    created = FlightSheetCreate.model_validate(payload)
    assert created.gpu_oc.settings[0].model_dump(exclude_none=True) == setting
    payload["gpu_oc"] = {"vendor": "nvidia", "settings": [{
        "model_match": "RTX 5080", "core_clock_offset_mhz": 0, "memory_clock_offset_mhz": 0,
        "core_clock_lock_mhz": None, "memory_clock_lock_mhz": None, "power_limit_w": None,
    }]}
    assert FlightSheetCreate.model_validate(payload).gpu_oc.model_dump(exclude_none=True) == {
        "vendor": "nvidia", "settings": [{"model_match": "RTX 5080",
        "core_clock_offset_mhz": 0, "memory_clock_offset_mhz": 0}],
    }


def test_flight_oc_target_rules_stay_unique_and_consistent():
    for settings in [
        [{"index": 0}],
        [{"model_match": "RTX 5080"}],
        [{"index": 0, "model_match": "RTX 5080", "power_limit_w": 120}],
        [{"index": 0, "power_limit_w": 120},
         {"model_match": "RTX 5080", "power_limit_w": 120}],
        [{"model_match": "RTX 5080", "power_limit_w": 120},
         {"model_match": "RTX-5080", "power_limit_w": 120}],
    ]:
        with pytest.raises(ValidationError):
            FlightGpuOc.model_validate({"vendor": "nvidia", "settings": settings})


def test_flight_gpu_oc_round_trip_apply_and_clear(client: TestClient):
    token = login(client)
    etc_wallet = wallet(client, token, "ETC")
    agent_id = enroll_linux_agent(client, token)
    request = srb_payload(str(etc_wallet["id"]))

    created = client.post("/api/flight-sheets", headers=auth(token), json=request)
    assert created.status_code == 201, created.text
    sheet = created.json()
    expected_oc = request["gpu_oc"]
    assert sheet["gpu_oc"] == expected_oc

    listed = client.get("/api/flight-sheets", headers=auth(token))
    assert listed.status_code == 200
    assert listed.json()[0]["gpu_oc"] == expected_oc

    audit = client.get("/api/audit", headers=auth(token)).json()
    creation_log = next(
        item
        for item in audit
        if item["action"] == "flight_sheet_created"
        and item["target_id"] == sheet["id"]
    )
    assert creation_log["details"]["gpu_oc_enabled"] is True
    assert creation_log["details"]["gpu_oc_indices"] == [0, 2]
    assert "settings" not in creation_log["details"]
    assert "pool_url" not in creation_log["details"]

    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["payload"]["gpu_oc"] == expected_oc
    command_audit = next(
        item
        for item in client.get("/api/audit", headers=auth(token)).json()
        if item["action"] == "command_queued"
        and item["details"].get("command_id") == applied.json()["id"]
    )
    assert command_audit["details"]["gpu_oc_enabled"] is True
    assert command_audit["details"]["gpu_oc_indices"] == [0, 2]
    assert "payload" not in command_audit["details"]
    assert "wallet_address" not in str(command_audit["details"])
    assert "pool_password" not in str(command_audit["details"])

    updated_request = dict(request)
    updated_request["name"] = "ETC without OC"
    updated_request["gpu_oc"] = None
    updated = client.put(
        f"/api/flight-sheets/{sheet['id']}",
        headers=auth(token),
        json=updated_request,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["gpu_oc"] is None

    applied_without_oc = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied_without_oc.status_code == 201, applied_without_oc.text
    assert "gpu_oc" not in applied_without_oc.json()["payload"]


def test_model_matched_gpu_oc_supports_multiple_models_and_automatic_selection(
    client: TestClient,
):
    token = login(client)
    pearl_wallet = wallet(client, token, "PEARL")
    payload = {
        "name": "Mixed RTX model OC",
        "miner_type": "srbminer",
        "miner_version": "3.4.6",
        "coin": "PEARL",
        "wallet_id": pearl_wallet["id"],
        "pool_url": "stratum+ssl://pearl.example.test:443",
        # Empty means the Agent selects all supported discrete GPUs after
        # filtering iGPUs, then resolves these model rules locally.
        "device_indices": [],
        "gpu_oc": {
            "vendor": "nvidia",
            "settings": [
                {"model_match": "5080", "power_limit_w": 300},
                {"model_match": "RTX 5070", "power_limit_w": 250},
                {"model_match": "5060 Ti", "power_limit_w": 200},
            ],
        },
    }

    response = client.post("/api/flight-sheets", headers=auth(token), json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["device_indices"] == []
    assert response.json()["gpu_oc"] == payload["gpu_oc"]
    audit = client.get("/api/audit", headers=auth(token)).json()
    created_audit = next(
        item for item in audit if item["action"] == "flight_sheet_created"
    )
    assert created_audit["details"]["gpu_oc_indices"] == []
    assert created_audit["details"]["gpu_oc_models"] == [
        "5080",
        "RTX 5070",
        "5060 Ti",
    ]


@pytest.mark.parametrize(
    "settings",
    [
        [
            {"model_match": "RTX 5080", "power_limit_w": 300},
            {"model_match": "rtx-5080", "power_limit_w": 290},
        ],
        [
            {"index": 0, "power_limit_w": 300},
            {"model_match": "5070", "power_limit_w": 250},
        ],
        [{"model_match": "!", "power_limit_w": 300}],
    ],
)
def test_model_matched_gpu_oc_rejects_duplicate_mixed_or_empty_matchers(
    client: TestClient,
    settings: list[dict[str, object]],
):
    token = login(client)
    pearl_wallet = wallet(client, token, "PEARL")
    response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "Invalid model matcher",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "PEARL",
            "wallet_id": pearl_wallet["id"],
            "pool_url": "stratum+ssl://pearl.example.test:443",
            "gpu_oc": {"vendor": "nvidia", "settings": settings},
        },
    )

    assert response.status_code == 422, response.text


def test_flight_gpu_oc_rejects_wrong_miner_algorithm_indices_and_settings(
    client: TestClient,
):
    token = login(client)
    xmr_wallet = wallet(client, token, "XMR")
    etc_wallet = wallet(client, token, "ETC")
    gpu_oc = {
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 125}],
    }

    xmrig = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "XMRig cannot OC",
            "miner_type": "xmrig",
            "miner_version": "6.26.0",
            "coin": "XMR",
            "wallet_id": xmr_wallet["id"],
            "pool_url": "stratum+tcp://pool.example.test:3333",
            "gpu_oc": gpu_oc,
        },
    )
    assert xmrig.status_code == 422
    assert "only for managed SRBMiner" in xmrig.text

    randomx = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "SRB RandomX cannot OC",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "XMR",
            "wallet_id": xmr_wallet["id"],
            "pool_url": "stratum+tcp://pool.example.test:3333",
            "algorithm": "randomx",
            "gpu_oc": gpu_oc,
        },
    )
    assert randomx.status_code == 422
    assert "requires an SRBMiner GPU algorithm" in randomx.text

    mismatch = srb_payload(str(etc_wallet["id"]))
    mismatch["device_indices"] = [0]
    mismatch["gpu_oc"] = {
        "vendor": "nvidia",
        "settings": [{"index": 1, "core_clock_offset_mhz": 60}],
    }
    mismatched = client.post(
        "/api/flight-sheets", headers=auth(token), json=mismatch
    )
    assert mismatched.status_code == 422
    assert "exactly one row for every selected GPU" in mismatched.text

    for invalid_oc in (
        {
            "vendor": "nvidia",
            "settings": [
                {"index": 0, "power_limit_w": 100},
                {"index": 0, "core_clock_offset_mhz": 60},
            ],
        },
        {"vendor": "nvidia", "settings": [{"index": 0}]},
        {
            "vendor": "nvidia",
            "settings": [{"index": 0, "power_limit_w": 0}],
        },
        {
            "vendor": "amd",
            "settings": [{"index": 0, "power_limit_w": 100}],
        },
        {
            "vendor": "nvidia",
            "settings": [{"index": 0, "fan_percent": 60}],
        },
    ):
        invalid = srb_payload(str(etc_wallet["id"]))
        invalid["gpu_oc"] = invalid_oc
        response = client.post(
            "/api/flight-sheets", headers=auth(token), json=invalid
        )
        assert response.status_code == 422, response.text


def test_secondary_gpu_leg_can_supply_required_algorithm(client: TestClient):
    token = login(client)
    xmr_wallet = wallet(client, token, "XMR")
    etc_wallet = wallet(client, token, "ETC")
    response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "SRB dual with GPU OC",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "XMR",
            "wallet_id": xmr_wallet["id"],
            "pool_url": "stratum+tcp://xmr.example.test:3333",
            "algorithm": "randomx",
            "secondary": {
                "miner_type": "srbminer",
                "miner_version": "3.4.6",
                "coin": "ETC",
                "wallet_id": etc_wallet["id"],
                "pool_url": "stratum+ssl://etc.example.test:443",
                "algorithm": "etchash",
                "device_indices": [1],
            },
            "gpu_oc": {
                "vendor": "nvidia",
                "settings": [{"index": 1, "memory_clock_offset_mhz": 650}],
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["gpu_oc"]["settings"] == [
        {"index": 1, "memory_clock_offset_mhz": 650}
    ]


def test_flight_gpu_oc_preserves_existing_role_boundaries(client: TestClient):
    admin_token = login(client)
    etc_wallet = wallet(client, admin_token, "ETC")
    created = client.post(
        "/api/flight-sheets",
        headers=auth(admin_token),
        json=srb_payload(str(etc_wallet["id"])),
    )
    assert created.status_code == 201, created.text

    user = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={
            "username": "gpuviewer",
            "password": "long-enough-viewer-password",
            "role": "viewer",
        },
    )
    assert user.status_code == 201, user.text
    viewer_token = login(
        client, "gpuviewer", "long-enough-viewer-password"
    )
    listed = client.get("/api/flight-sheets", headers=auth(viewer_token))
    assert listed.status_code == 200
    assert listed.json()[0]["gpu_oc"]["vendor"] == "nvidia"

    assert (
        client.post(
            "/api/flight-sheets",
            headers=auth(viewer_token),
            json=srb_payload(str(etc_wallet["id"])),
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/flight-sheets/{created.json()['id']}",
            headers=auth(viewer_token),
            json=srb_payload(str(etc_wallet["id"])),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/agents/not-an-agent/flight-sheets/{created.json()['id']}/apply",
            headers=auth(viewer_token),
        ).status_code
        == 403
    )


def test_pearlhash_lock_and_offset_fields_can_be_combined(
    client: TestClient,
):
    token = login(client)
    pearl_wallet = wallet(client, token, "PEARL")
    payload = {
        "name": "PEARL lock and offset OC",
        "miner_type": "srbminer",
        "miner_version": "3.4.6",
        "coin": "PEARL",
        "wallet_id": pearl_wallet["id"],
        "pool_url": "stratum+ssl://pearl.example.test:443",
        "device_indices": [0, 1],
        "gpu_oc": {
            "vendor": "nvidia",
            "settings": [
                {
                    "index": 0,
                    "core_clock_offset_mhz": 125,
                    "core_clock_lock_mhz": 1450,
                    "memory_clock_offset_mhz": 900,
                    "memory_clock_lock_mhz": 8000,
                    "power_limit_w": 135,
                },
                {
                    "index": 1,
                    "core_clock_offset_mhz": 100,
                    "core_clock_lock_mhz": 1500,
                    "memory_clock_offset_mhz": 850,
                    "memory_clock_lock_mhz": 8100,
                    "power_limit_w": 120,
                },
            ],
        },
    }

    created = client.post("/api/flight-sheets", headers=auth(token), json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["algorithm"] == "pearlhash"
    assert created.json()["gpu_oc"] == payload["gpu_oc"]

    typo = dict(payload)
    typo["name"] = "Misspelled algorithm"
    typo["algorithm"] = "pearlhas"
    response = client.post("/api/flight-sheets", headers=auth(token), json=typo)
    assert response.status_code == 422, response.text
    assert "Unsupported SRBMiner algorithm" in response.text

    for combined_fields in (
        {
            "core_clock_offset_mhz": 100,
            "core_clock_lock_mhz": 1500,
        },
        {
            "memory_clock_offset_mhz": 800,
            "memory_clock_lock_mhz": 8100,
        },
    ):
        combined = dict(payload)
        combined["name"] = "Combined clock controls"
        combined["device_indices"] = [0]
        combined["gpu_oc"] = {
            "vendor": "nvidia",
            "settings": [{"index": 0, **combined_fields}],
        }
        response = client.post(
            "/api/flight-sheets", headers=auth(token), json=combined
        )
        assert response.status_code == 201, response.text
        assert response.json()["gpu_oc"] == combined["gpu_oc"]


def test_srb_gpu_mapping_and_oc_require_agent_0_5_3_before_download_or_queue(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    token = login(client)
    pearl_wallet = wallet(client, token, "PEARL")
    agent_id = enroll_linux_agent(client, token, "0.5.0")

    def flight_payload(name: str, setting: dict[str, object]) -> dict[str, object]:
        return {
            "name": name,
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "PEARL",
            "wallet_id": pearl_wallet["id"],
            "pool_url": "stratum+ssl://pearl.example.test:443",
            "device_indices": [0],
            "gpu_oc": {
                "vendor": "nvidia",
                "settings": [{"index": 0, **setting}],
            },
        }

    locked = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=flight_payload("Locked clocks", {"core_clock_lock_mhz": 1500}),
    )
    assert locked.status_code == 201, locked.text
    offset = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=flight_payload("Offset clocks", {"core_clock_offset_mhz": 125}),
    )
    assert offset.status_code == 201, offset.text

    downloads: list[str] = []

    def counted_download(url: str, destination: Path, **kwargs):
        del kwargs
        downloads.append(url)
        content = b"agent compatibility test archive"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return len(content), hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(main_module, "_download_https_package", counted_download)
    rejected = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{locked.json()['id']}/apply",
        headers=auth(token),
    )
    assert rejected.status_code == 409, rejected.text
    assert "Agent 0.5.3 or newer" in rejected.text
    assert downloads == []

    old_offset = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{offset.json()['id']}/apply",
        headers=auth(token),
    )
    assert old_offset.status_code == 409, old_offset.text
    assert downloads == []

    old_mapping_agent_id = enroll_linux_agent(client, token, "0.5.2")
    plain_payload = flight_payload("Plain GPU mapping", {})
    plain_payload.pop("gpu_oc")
    plain = client.post(
        "/api/flight-sheets", headers=auth(token), json=plain_payload
    )
    assert plain.status_code == 201, plain.text
    old_mapping = client.post(
        f"/api/agents/{old_mapping_agent_id}/flight-sheets/{plain.json()['id']}/apply",
        headers=auth(token),
    )
    assert old_mapping.status_code == 409, old_mapping.text
    assert "discrete-GPU mapping" in old_mapping.text
    assert downloads == []

    compatible_agent_id = enroll_linux_agent(client, token, "0.5.3")
    compatible = client.post(
        f"/api/agents/{compatible_agent_id}/flight-sheets/{offset.json()['id']}/apply",
        headers=auth(token),
    )
    assert compatible.status_code == 201, compatible.text
    assert len(downloads) == 1


def test_srb_dual_rejects_mismatched_versions_algorithms_and_gpu_sets(
    client: TestClient,
):
    token = login(client)
    primary_wallet = wallet(client, token, "IRON")
    secondary_wallet = wallet(client, token, "QTC")

    def dual_payload(
        primary_algorithm: str = "fishhash",
        secondary_algorithm: str = "qhash",
    ) -> dict[str, object]:
        return {
            "name": "SRB dual validation",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "IRON",
            "wallet_id": primary_wallet["id"],
            "pool_url": "stratum+ssl://primary.example.test:443",
            "algorithm": primary_algorithm,
            "device_indices": [0, 1],
            "secondary": {
                "miner_type": "srbminer",
                "miner_version": "3.4.6",
                "coin": "QTC",
                "wallet_id": secondary_wallet["id"],
                "pool_url": "stratum+ssl://secondary.example.test:443",
                "algorithm": secondary_algorithm,
                "device_indices": [0, 1],
            },
        }

    valid = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=dual_payload(),
    )
    assert valid.status_code == 201, valid.text

    order_mismatch = dual_payload()
    order_mismatch["secondary"]["device_indices"] = [1, 0]
    response = client.post(
        "/api/flight-sheets", headers=auth(token), json=order_mismatch
    )
    assert response.status_code == 422, response.text
    assert "same ordered GPU indices" in response.text

    version_mismatch = dual_payload()
    version_mismatch["secondary"]["miner_version"] = "3.4.5"
    response = client.post(
        "/api/flight-sheets", headers=auth(token), json=version_mismatch
    )
    assert response.status_code == 422, response.text
    assert "same miner version" in response.text

    xmr_wallet = wallet(client, token, "XMR")
    cpu_pair = dual_payload("randomx", "randomx")
    cpu_pair.update(coin="XMR", wallet_id=xmr_wallet["id"])
    cpu_pair["secondary"].update(coin="XMR", wallet_id=xmr_wallet["id"])
    response = client.post(
        "/api/flight-sheets", headers=auth(token), json=cpu_pair,
    )
    assert response.status_code == 422, response.text
    assert "cannot use RandomX for both legs" in response.text

    response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=dual_payload("pearlhash", "qhash"),
    )
    assert response.status_code == 422, response.text
    assert "Unsupported SRBMiner dual-mining" in response.text

    gpu_set_mismatch = dual_payload()
    gpu_set_mismatch["secondary"]["device_indices"] = [1, 2]
    response = client.post(
        "/api/flight-sheets", headers=auth(token), json=gpu_set_mismatch
    )
    assert response.status_code == 422, response.text
    assert "same ordered GPU indices" in response.text


def test_srb_dual_apply_downloads_once_and_reuses_the_managed_package(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    token = login(client)
    xmr_wallet = wallet(client, token, "XMR")
    pearl_wallet = wallet(client, token, "PEARL")
    agent_id = enroll_linux_agent(client, token)
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "XMR plus PEARL",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "coin": "XMR",
            "wallet_id": xmr_wallet["id"],
            "pool_url": "stratum+tcp://xmr.example.test:3333",
            "algorithm": "randomx",
            "secondary": {
                "miner_type": "srbminer",
                "miner_version": "3.4.6",
                "coin": "PEARL",
                "wallet_id": pearl_wallet["id"],
                "pool_url": "stratum+ssl://pearl.example.test:443",
                "algorithm": "pearlhash",
                "device_indices": [0],
            },
        },
    )
    assert created.status_code == 201, created.text

    downloads: list[str] = []

    def counted_download(url: str, destination: Path, **kwargs):
        del kwargs
        downloads.append(url)
        content = b"one shared SRBMiner archive"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return len(content), hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(main_module, "_download_https_package", counted_download)
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    assert len(downloads) == 1
    payload = applied.json()["payload"]
    assert payload["primary"]["miner_package"] == payload["secondary"]["miner_package"]
