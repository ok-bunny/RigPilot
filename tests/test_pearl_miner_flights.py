from __future__ import annotations

import hashlib
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

    monkeypatch.setattr(main_module, "_download_https_package", package_download(b'official pearl miner test archive'))
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as test_client:
        yield test_client




def login(client: TestClient) -> str:
    return _login(client, "admin", "correct-horse-battery-staple")


def create_wallet(client: TestClient, token: str, coin: str = "PEARL") -> str:
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
    return response.json()["id"]


def upload_pearl_test_package(
    client: TestClient,
    token: str,
    *,
    version: str = "2.1.1",
) -> str:
    archive = f"locally supplied Pearl Miner {version} test build".encode()
    response = client.post(
        "/api/miner-packages/upload",
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        params={
            "name": "Pearl Miner direct Stratum test build",
            "version": version,
            "archive_type": "tar.gz",
            "filename": f"pearl-local-v{version}.tar.gz",
            "managed_miner_type": "pearlminer",
            "managed_os_family": "linux",
            "managed_variant": "nvidia",
            "managed_channel": "test",
        },
        content=archive,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def pearl_payload(wallet_id: str) -> dict[str, object]:
    return {
        "name": "Managed PEARL",
        "miner_type": "pearlminer",
        "miner_version": "1.2.4",
        "coin": "PEARL",
        "wallet_id": wallet_id,
        "pool_url": "pearl://global.pearlfortune.org:443",
        "algorithm": "pearlhash",
        "worker_name_template": "{hostname}",
        "extra_config": {
            "gpu_backend": "auto",
            "cuda_variant": "auto",
        },
    }


def enroll_agent(
    client: TestClient,
    token: str,
    *,
    os_family: str = "linux",
    version: str = "0.6.39",
    gpus: list[dict[str, object]] | None = None,
    capabilities: list[str] | None = None,
) -> str:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "pearl-test", "ttl_minutes": 30},
    ).json()
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued["token"],
            "name": f"pearl-{os_family}",
            "hostname": f"pearl-{os_family}.local",
            "os_family": os_family,
            "agent_version": version,
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    body = enrolled.json()
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers={
            "X-Agent-ID": body["agent_id"],
            "Authorization": f"Bearer {body['agent_secret']}",
        },
        json={
            "hostname": f"pearl-{os_family}.local",
            "os_family": os_family,
            "platform": os_family,
            "agent_version": version,
            "capabilities": (
                capabilities
                if capabilities is not None
                else (
                    ["managed_mining.dual_process.v1"]
                    if tuple(int(part) for part in version.split("."))
                    >= (0, 6, 48)
                    else []
                )
            ),
            "metrics": {"gpus": gpus or []},
            "inventory": {},
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return body["agent_id"]


def test_pearl_flight_create_edit_and_structured_linux_nvidia_apply(client: TestClient):
    token = login(client)
    wallet_id = create_wallet(client, token)
    request = pearl_payload(wallet_id)
    created = client.post("/api/flight-sheets", headers=auth(token), json=request)
    assert created.status_code == 201, created.text
    sheet = created.json()
    assert sheet["coin"] == "PEARL"
    assert sheet["algorithm"] == "pearlhash"
    assert sheet["pool_url"] == "pearl://global.pearlfortune.org:443"
    assert sheet["device_indices"] == []
    assert sheet["extra_config"] == {
        "gpu_backend": "auto",
        "cuda_variant": "auto",
        "extra_args": "",
    }

    edited_request = {
        **request,
        "name": "Managed PEARL CUDA 13",
        "extra_config": {
            "gpu_backend": "nvidia",
            "cuda_variant": "cuda13",
        },
    }
    edited = client.put(
        f"/api/flight-sheets/{sheet['id']}",
        headers=auth(token),
        json=edited_request,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["extra_config"] == {**edited_request["extra_config"], "extra_args": ""}

    agent_id = enroll_agent(
        client,
        token,
        gpus=[
            {
                "index": 0,
                "vendor": "nvidia",
                "device_type": "discrete",
                "name": "NVIDIA GeForce RTX 5080",
            }
        ],
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    primary = applied.json()["payload"]["primary"]
    assert primary["miner_type"] == "pearlminer"
    assert primary["pool_url"] == "pearl://global.pearlfortune.org:443"
    assert primary["algorithm"] == "pearlhash"
    assert primary["device_indices"] == []
    assert primary["extra_config"] == edited_request["extra_config"]
    assert (
        primary["miner_package"]["id"]
        == "builtin-pearlminer-1.2.4-linux-x64-nvidia"
    )


@pytest.mark.parametrize(
    "pool_url",
    [
        "stratum+tcp://pool.example.test:3333",
        "stratum+ssl://pool.example.test:443",
    ],
)
def test_pearl_flight_accepts_direct_stratum_without_version_gate(
    client: TestClient,
    pool_url: str,
):
    token = login(client)
    wallet_id = create_wallet(client, token)
    request = {
        **pearl_payload(wallet_id),
        "pool_url": pool_url,
    }

    created = client.post("/api/flight-sheets", headers=auth(token), json=request)
    assert created.status_code == 201, created.text
    sheet = created.json()
    assert sheet["pool_url"] == request["pool_url"]

    agent_id = enroll_agent(client, token, version="0.6.39", gpus=[
        {
            "index": 0,
            "vendor": "nvidia",
            "device_type": "discrete",
            "name": "NVIDIA GeForce RTX 5080",
        }
    ])
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    primary = applied.json()["payload"]["primary"]
    assert primary["pool_url"] == request["pool_url"]
    assert primary["miner_package"]["id"] == "builtin-pearlminer-1.2.4-linux-x64-nvidia"
    with client.app.state.db.connection() as db:
        stored_command = db.execute(
            "SELECT payload_json FROM commands WHERE id=?",
            (applied.json()["id"],),
        ).fetchone()
    internal_payload = json.loads(stored_command["payload_json"])
    assert (
        internal_payload["_controller_flight_dispatch_v1"]
        ["minimum_agent_version"]
        == "0.6.39"
    )


def test_pearl_stratum_ssl_is_supported_for_edits_and_secondary_leg(
    client: TestClient,
):
    token = login(client)
    pearl_wallet = create_wallet(client, token)

    proxy_request = pearl_payload(pearl_wallet)
    proxy_sheet = client.post(
        "/api/flight-sheets", headers=auth(token), json=proxy_request
    )
    assert proxy_sheet.status_code == 201, proxy_sheet.text
    edited = client.put(
        f"/api/flight-sheets/{proxy_sheet.json()['id']}",
        headers=auth(token),
        json={
            **proxy_request,
            "pool_url": "stratum+ssl://edited-pool.example.test:443",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["pool_url"] == "stratum+ssl://edited-pool.example.test:443"

    xmr_wallet = create_wallet(client, token, "XMR")
    secondary_request = {
        "name": "XMR plus SSL Stratum PEARL",
        "secondary_mode": "independent",
        "miner_type": "xmrig",
        "miner_version": "6.26.0",
        "coin": "XMR",
        "wallet_id": xmr_wallet,
        "pool_url": "stratum+tcp://xmr.example.test:3333",
        "algorithm": "randomx",
        "worker_name_template": "{hostname}",
        "secondary": {
            "miner_type": "pearlminer",
            "miner_version": "2.0.3",
            "coin": "PEARL",
            "wallet_id": pearl_wallet,
            "pool_url": "stratum+tcp://pearl.example.test:4444",
            "algorithm": "pearlhash",
            "extra_config": {
                "gpu_backend": "nvidia",
                "cuda_variant": "auto",
            },
        },
    }
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=secondary_request,
    )
    assert created.status_code == 201, created.text
    assert created.json()["secondary"]["pool_url"] == (
        secondary_request["secondary"]["pool_url"]
    )


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"coin": "XMR"}, "supports PEARL only"),
        ({"algorithm": "randomx"}, "requires the pearlhash algorithm"),
        (
            {"pool_url": "stratum+tls://pool.example.test:443"},
            "must use pearl://host:port, stratum+tcp://host:port, or stratum+ssl://host:port",
        ),
        ({"device_indices": [0]}, "does not support device_indices"),
        (
            {"extra_config": {"cpu_threads": 8}},
            "Unsupported Pearl Miner options",
        ),
        (
            {
                "extra_config": {
                    "gpu_backend": "amd",
                    "cuda_variant": "cuda12",
                }
            },
            "available only with the NVIDIA backend",
        ),
    ],
)
def test_pearl_flight_rejects_unsupported_options(
    client: TestClient,
    change: dict[str, object],
    expected: str,
):
    token = login(client)
    wallet_id = create_wallet(client, token)
    response = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={**pearl_payload(wallet_id), **change},
    )
    assert response.status_code == 422, response.text
    assert expected in response.text


def test_pearl_flight_rejects_secondary_and_old_agent(client: TestClient):
    token = login(client)
    wallet_id = create_wallet(client, token)
    request = pearl_payload(wallet_id)
    dual = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **request,
            "secondary": {
                key: value
                for key, value in request.items()
                if key not in {"name", "worker_name_template"}
            },
        },
    )
    assert dual.status_code == 422
    assert "dual mining is supported only by SRBMiner" in dual.text

    created = client.post("/api/flight-sheets", headers=auth(token), json=request)
    assert created.status_code == 201, created.text
    old_agent = enroll_agent(
        client,
        token,
        version="0.6.38",
        gpus=[{"index": 0, "vendor": "nvidia", "device_type": "discrete"}],
    )
    rejected = client.post(
        f"/api/agents/{old_agent}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert rejected.status_code == 409
    assert "0.6.39 or newer" in rejected.text


def test_pearl_linux_auto_backend_rejects_mixed_or_unknown_inventory(client: TestClient):
    token = login(client)
    wallet_id = create_wallet(client, token)
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=pearl_payload(wallet_id),
    )
    assert created.status_code == 201, created.text
    mixed_agent = enroll_agent(
        client,
        token,
        gpus=[
            {"index": 0, "vendor": "nvidia", "device_type": "discrete"},
            {"index": 1, "vendor": "amd", "device_type": "discrete"},
        ],
    )
    mixed = client.post(
        f"/api/agents/{mixed_agent}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert mixed.status_code == 409
    assert "Mixed NVIDIA/AMD" in mixed.text

    unknown_agent = enroll_agent(client, token, gpus=[])
    unknown = client.post(
        f"/api/agents/{unknown_agent}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert unknown.status_code == 409
    assert "Cannot determine" in unknown.text


def test_pearl_linux_auto_backend_selects_official_amd_variant(client: TestClient):
    token = login(client)
    wallet_id = create_wallet(client, token)
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=pearl_payload(wallet_id),
    )
    assert created.status_code == 201, created.text
    agent_id = enroll_agent(
        client,
        token,
        gpus=[
            {
                "index": 0,
                "vendor": "Advanced Micro Devices",
                "device_type": "discrete",
                "name": "AMD Radeon RX 7900 XTX",
            }
        ],
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    assert (
        applied.json()["payload"]["primary"]["miner_package"]["id"]
        == "builtin-pearlminer-1.2.4-linux-x64-amd"
    )


def test_pearl_windows_uses_official_miner_exe_package_and_rejects_amd(client: TestClient):
    token = login(client)
    wallet_id = create_wallet(client, token)
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json=pearl_payload(wallet_id),
    )
    assert created.status_code == 201, created.text
    windows_agent = enroll_agent(client, token, os_family="windows")
    applied = client.post(
        f"/api/agents/{windows_agent}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    assert (
        applied.json()["payload"]["primary"]["miner_package"]["id"]
        == "builtin-pearlminer-1.2.4-windows-x64"
    )

    amd_request = {
        **pearl_payload(wallet_id),
        "name": "PEARL AMD",
        "extra_config": {
            "gpu_backend": "amd",
            "cuda_variant": "auto",
        },
    }
    amd_sheet = client.post(
        "/api/flight-sheets", headers=auth(token), json=amd_request
    )
    assert amd_sheet.status_code == 201, amd_sheet.text
    rejected = client.post(
        f"/api/agents/{windows_agent}/flight-sheets/{amd_sheet.json()['id']}/apply",
        headers=auth(token),
    )
    assert rejected.status_code == 409
    assert "Windows is available only" in rejected.text


def test_pearl_wsl_round_trip_uses_linux_nvidia_asset_on_windows(client: TestClient):
    token = login(client)
    wallet_id = create_wallet(client, token)
    request = {
        **pearl_payload(wallet_id),
        "name": "PEARL through WSL",
        "execution_mode": "wsl",
        "extra_config": {
            "gpu_backend": "nvidia",
            "cuda_variant": "cuda13",
        },
    }
    created = client.post("/api/flight-sheets", headers=auth(token), json=request)
    assert created.status_code == 201, created.text
    sheet = created.json()
    assert sheet["execution_mode"] == "wsl"
    listed = client.get("/api/flight-sheets", headers=auth(token))
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["execution_mode"] == "wsl"

    edited = client.put(
        f"/api/flight-sheets/{sheet['id']}",
        headers=auth(token),
        json={**request, "name": "PEARL through WSL edited"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["execution_mode"] == "wsl"

    agent_id = enroll_agent(
        client,
        token,
        os_family="windows",
        version="0.6.59",
        capabilities=[
            "managed_mining.dual_process.v1",
            "managed_mining.wsl_gpu.v1",
        ],
        gpus=[
            {
                "index": 0,
                "vendor": "NVIDIA",
                "device_type": "discrete",
                "name": "NVIDIA GeForce RTX 5080",
            }
        ],
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    payload = applied.json()["payload"]
    primary = payload["primary"]
    assert primary["execution_mode"] == "wsl"
    assert primary["miner_package"]["managed_os_family"] == "linux"
    assert (
        primary["miner_package"]["id"]
        == "builtin-pearlminer-1.2.4-linux-x64-nvidia"
    )


def test_wsl_apply_requires_supported_windows_agent_and_nvidia_inventory(
    client: TestClient,
):
    token = login(client)
    wallet_id = create_wallet(client, token)
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **pearl_payload(wallet_id),
            "execution_mode": "wsl",
            "extra_config": {
                "gpu_backend": "nvidia",
                "cuda_variant": "auto",
            },
        },
    )
    assert created.status_code == 201, created.text
    sheet_id = created.json()["id"]

    cases = [
        (
            enroll_agent(
                client,
                token,
                os_family="windows",
                version="0.6.58",
                capabilities=[
                    "managed_mining.dual_process.v1",
                    "managed_mining.wsl_gpu.v1",
                ],
                gpus=[{"index": 0, "vendor": "nvidia", "device_type": "discrete"}],
            ),
            "0.6.59",
        ),
        (
            enroll_agent(
                client,
                token,
                os_family="windows",
                version="0.6.59",
                capabilities=["managed_mining.dual_process.v1"],
                gpus=[{"index": 0, "vendor": "nvidia", "device_type": "discrete"}],
            ),
            "WSL GPU 挖矿能力",
        ),
        (
            enroll_agent(
                client,
                token,
                os_family="linux",
                version="0.6.59",
                capabilities=[
                    "managed_mining.dual_process.v1",
                    "managed_mining.wsl_gpu.v1",
                ],
                gpus=[{"index": 0, "vendor": "nvidia", "device_type": "discrete"}],
            ),
            "只能下发到 Windows",
        ),
        (
            enroll_agent(
                client,
                token,
                os_family="windows",
                version="0.6.59",
                capabilities=[
                    "managed_mining.dual_process.v1",
                    "managed_mining.wsl_gpu.v1",
                ],
                gpus=[{"index": 0, "vendor": "amd", "device_type": "discrete"}],
            ),
            "NVIDIA",
        ),
    ]
    for agent_id, expected in cases:
        response = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply",
            headers=auth(token),
        )
        assert response.status_code == 409, response.text
        assert expected in response.text


def test_independent_native_cpu_and_wsl_gpu_use_per_leg_packages(client: TestClient):
    token = login(client)
    pearl_wallet = create_wallet(client, token)
    xmr_wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={"name": "XMR dual", "coin": "XMR", "address": "xmr-dual-wallet"},
    ).json()["id"]
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "Native XMR plus WSL PEARL",
            "secondary_mode": "independent",
            "miner_type": "xmrig",
            "miner_version": "6.26.0",
            "coin": "XMR",
            "wallet_id": xmr_wallet,
            "pool_url": "stratum+ssl://xmr.example.test:443",
            "algorithm": "randomx",
            "secondary": {
                "miner_type": "pearlminer",
                "miner_version": "1.2.4",
                "execution_mode": "wsl",
                "coin": "PEARL",
                "wallet_id": pearl_wallet,
                "pool_url": "pearl://global.pearlfortune.org:443",
                "algorithm": "pearlhash",
                "extra_config": {
                    "gpu_backend": "nvidia",
                    "cuda_variant": "auto",
                },
            },
        },
    )
    assert created.status_code == 201, created.text
    sheet = created.json()
    assert sheet["execution_mode"] == "native"
    assert sheet["secondary"]["execution_mode"] == "wsl"

    agent_id = enroll_agent(
        client,
        token,
        os_family="windows",
        version="0.6.59",
        capabilities=[
            "managed_mining.dual_process.v1",
            "managed_mining.wsl_gpu.v1",
        ],
        gpus=[
            {
                "index": 0,
                "vendor": "NVIDIA",
                "device_type": "discrete",
                "name": "NVIDIA GeForce RTX 5080",
            }
        ],
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    payload = applied.json()["payload"]
    assert "execution_mode" not in payload["primary"]
    assert "managed_os_family" not in payload["primary"]["miner_package"]
    assert payload["primary"]["miner_package"]["id"].endswith("-windows-x64")
    assert payload["secondary"]["execution_mode"] == "wsl"
    assert payload["secondary"]["miner_package"]["managed_os_family"] == "linux"
    assert payload["secondary"]["miner_package"]["id"].endswith(
        "-linux-x64-nvidia"
    )


def test_wsl_flight_rejects_cpu_amd_integrated_dual_and_srb_oc(client: TestClient):
    token = login(client)
    pearl_wallet = create_wallet(client, token)
    amd = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **pearl_payload(pearl_wallet),
            "execution_mode": "wsl",
            "extra_config": {"gpu_backend": "amd", "cuda_variant": "auto"},
        },
    )
    assert amd.status_code == 422, amd.text
    assert "不能选择 AMD" in amd.text

    xmr_wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={"name": "XMR", "coin": "XMR", "address": "xmr-public-wallet"},
    ).json()["id"]
    cpu = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "XMR WSL prohibited",
            "miner_type": "xmrig",
            "miner_version": "6.26.0",
            "execution_mode": "wsl",
            "coin": "XMR",
            "wallet_id": xmr_wallet,
            "pool_url": "stratum+ssl://xmr.example.test:443",
            "algorithm": "randomx",
        },
    )
    assert cpu.status_code == 422, cpu.text
    assert "XMRig" in cpu.text

    etc_wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={"name": "ETC", "coin": "ETC", "address": "etc-public-wallet"},
    ).json()["id"]
    base_srb = {
        "name": "SRB WSL",
        "miner_type": "srbminer",
        "miner_version": "3.4.6",
        "execution_mode": "wsl",
        "coin": "ETC",
        "wallet_id": etc_wallet,
        "pool_url": "stratum+ssl://etc.example.test:443",
        "algorithm": "etchash",
    }
    with_oc = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **base_srb,
            "gpu_oc": {
                "vendor": "nvidia",
                "settings": [{"index": 0, "power_limit_w": 180}],
            },
        },
    )
    assert with_oc.status_code == 422, with_oc.text
    assert "暂不接收飞行表 SRBMiner OC" in with_oc.text

    integrated = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            **base_srb,
            "secondary_mode": "integrated",
            "secondary": {
                "miner_type": "srbminer",
                "miner_version": "3.4.6",
                "coin": "XMR",
                "wallet_id": xmr_wallet,
                "pool_url": "stratum+ssl://xmr.example.test:443",
                "algorithm": "randomx",
            },
        },
    )
    assert integrated.status_code == 422, integrated.text
    assert "单进程内置双挖" in integrated.text


def test_srbminer_gpu_wsl_uses_linux_package_without_flight_oc(client: TestClient):
    token = login(client)
    etc_wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={"name": "ETC WSL", "coin": "ETC", "address": "etc-wsl-wallet"},
    ).json()["id"]
    created = client.post(
        "/api/flight-sheets",
        headers=auth(token),
        json={
            "name": "SRBMiner WSL GPU",
            "miner_type": "srbminer",
            "miner_version": "3.4.6",
            "execution_mode": "wsl",
            "coin": "ETC",
            "wallet_id": etc_wallet,
            "pool_url": "stratum+ssl://etc.example.test:443",
            "algorithm": "etchash",
        },
    )
    assert created.status_code == 201, created.text
    agent_id = enroll_agent(
        client,
        token,
        os_family="windows",
        version="0.6.59",
        capabilities=[
            "managed_mining.dual_process.v1",
            "managed_mining.wsl_gpu.v1",
        ],
        gpus=[
            {
                "index": 0,
                "vendor": "nvidia",
                "device_type": "discrete",
                "name": "NVIDIA GeForce RTX 5080",
            }
        ],
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    primary = applied.json()["payload"]["primary"]
    assert primary["execution_mode"] == "wsl"
    assert primary["miner_package"]["managed_os_family"] == "linux"
    assert primary["miner_package"]["id"] == "builtin-srbminer-3.4.6-linux-x64"


def test_pearl_current_stable_is_the_default(client: TestClient):
    token = login(client)
    wallet_id = create_wallet(client, token)
    stable_request = pearl_payload(wallet_id)
    stable_request.pop("miner_version")
    stable = client.post(
        "/api/flight-sheets", headers=auth(token), json=stable_request
    )
    assert stable.status_code == 201, stable.text
    assert stable.json()["miner_version"] == "2.0.3"


def test_categorized_pearl_test_package_is_canonical_cached_and_selectable(
    client: TestClient,
):
    token = login(client)
    archive = b"locally supplied Pearl Miner test build"
    params = {
        "name": "arbitrary user label",
        "version": "9.0.0",
        "archive_type": "tar.gz",
        "filename": "pearl-local-v9.0.0.tar.gz",
        "managed_miner_type": "pearlminer",
        "managed_os_family": "linux",
        "managed_variant": "nvidia",
        "managed_channel": "test",
    }
    collision = client.post(
        "/api/miner-packages/upload",
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        params={**params, "version": "2.0.3"},
        content=archive,
    )
    assert collision.status_code == 409
    assert "already exists in the managed GitHub catalog" in collision.text
    uploaded = client.post(
        "/api/miner-packages/upload",
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        params=params,
        content=archive,
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["id"] == "test-pearlminer-9.0.0-linux-x64-nvidia"
    assert uploaded.json()["name"] == "Pearl Miner"
    assert uploaded.json()["managed_miner_type"] == "pearlminer"
    assert uploaded.json()["managed_os_family"] == "linux"
    assert uploaded.json()["managed_variant"] == "nvidia"
    assert uploaded.json()["managed_channel"] == "test"
    assert uploaded.json()["sha256"] == hashlib.sha256(archive).hexdigest()

    catalog = client.get("/api/managed-miners", headers=auth(token))
    assert catalog.status_code == 200, catalog.text
    pearl = next(item for item in catalog.json() if item["id"] == "pearlminer")
    test_release = next(
        item for item in pearl["versions"] if item["version"] == "9.0.0"
    )
    assert test_release["prerelease"] is True
    assert test_release["source_type"] == "controller_cache"
    assert test_release["assets"]["linux-nvidia"]["cached"] is True
    assert test_release["assets"]["linux-nvidia"]["available"] is True

    wallet_id = create_wallet(client, token)
    flight_request = {
        **pearl_payload(wallet_id),
        "name": "Local categorized Pearl test",
        "miner_version": "9.0.0",
        "extra_config": {
            "gpu_backend": "nvidia",
            "cuda_variant": "auto",
        },
    }
    flight = client.post(
        "/api/flight-sheets", headers=auth(token), json=flight_request
    )
    assert flight.status_code == 201, flight.text
    old_agent_id = enroll_agent(
        client,
        token,
        version="0.6.47",
        gpus=[{"index": 0, "vendor": "nvidia", "device_type": "discrete"}],
    )
    blocked_old_agent = client.post(
        f"/api/agents/{old_agent_id}/flight-sheets/{flight.json()['id']}/apply",
        headers=auth(token),
    )
    assert blocked_old_agent.status_code == 409
    assert "test miner packages require RigPilot Agent 0.6.48" in blocked_old_agent.text

    agent_id = enroll_agent(
        client,
        token,
        version="0.6.48",
        gpus=[{"index": 0, "vendor": "nvidia", "device_type": "discrete"}],
    )
    applied = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{flight.json()['id']}/apply",
        headers=auth(token),
    )
    assert applied.status_code == 201, applied.text
    assert (
        applied.json()["payload"]["primary"]["miner_package"]["id"]
        == "test-pearlminer-9.0.0-linux-x64-nvidia"
    )

    blocked = client.delete(
        "/api/miner-packages/test-pearlminer-9.0.0-linux-x64-nvidia",
        headers=auth(token),
    )
    assert blocked.status_code == 409
    assert "used by a flight sheet" in blocked.text


def test_categorized_test_package_validation(client: TestClient):
    token = login(client)
    common = {
        "name": "test",
        "version": "9.0.1",
        "archive_type": "tar.gz",
        "filename": "test.tar.gz",
        "managed_miner_type": "pearlminer",
        "managed_os_family": "linux",
        "managed_channel": "test",
    }
    missing_variant = client.post(
        "/api/miner-packages/upload",
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        params=common,
        content=b"package",
    )
    assert missing_variant.status_code == 422
    assert "require an NVIDIA or AMD variant" in missing_variant.text

    wrong_archive = client.post(
        "/api/miner-packages/upload",
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        params={
            **common,
            "managed_os_family": "windows",
        },
        content=b"package",
    )
    assert wrong_archive.status_code == 422
    assert "windows test packages must use zip" in wrong_archive.text


@pytest.mark.parametrize("phase", ["before_commit", "before_dispatch"])
def test_pearl_auto_backend_rechecks_bound_variant_after_package_preparation(client, monkeypatch, phase):
    token = login(client)
    created = client.post("/api/flight-sheets", headers=auth(token),
                         json=pearl_payload(create_wallet(client, token)))
    assert created.status_code == 201, created.text
    issued = client.post("/api/enrollment-tokens", headers=auth(token),
                         json={"label": "pearl-inventory-race", "ttl_minutes": 30})
    assert issued.status_code == 200, issued.text
    enrolled = client.post("/api/agent/enroll", json={
        "enrollment_token": issued.json()["token"], "hostname": "pearl-inventory-race",
        "os_family": "linux", "agent_version": "0.6.39",
    })
    assert enrolled.status_code == 201, enrolled.text
    identity = enrolled.json()
    headers = {"X-Agent-ID": identity["agent_id"], "Authorization": f"Bearer {identity['agent_secret']}"}

    def heartbeat(vendor):
        response = client.post("/api/agent/heartbeat", headers=headers, json={
            "hostname": "pearl-inventory-race", "os_family": "linux", "agent_version": "0.6.39",
            "metrics": {"gpus": [{"index": 0, "vendor": vendor, "device_type": "discrete"}]},
        })
        assert response.status_code == 200, response.text
        return response.json()

    heartbeat("nvidia")
    if phase == "before_commit":
        download = main_module._download_https_package
        def change_during_download(*args, **kwargs):
            result = download(*args, **kwargs)
            heartbeat("amd")
            return result
        monkeypatch.setattr(main_module, "_download_https_package", change_during_download)
    applied = client.post(
        f"/api/agents/{identity['agent_id']}/flight-sheets/{created.json()['id']}/apply", headers=auth(token)
    )
    if phase == "before_commit":
        assert applied.status_code == 409, applied.text
        with client.app.state.db.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM commands WHERE action='apply_flight_sheet'").fetchone()[0] == 0
    else:
        assert applied.status_code == 201, applied.text
        assert applied.json()["payload"]["primary"]["miner_package"]["id"].endswith("-nvidia")
        delivered = heartbeat("amd")
        assert not any(item["id"] == applied.json()["id"] for item in delivered["commands"])
        with client.app.state.db.connection() as db:
            assert db.execute("SELECT status FROM commands WHERE id=?", (applied.json()["id"],)).fetchone()[0] == "failed"
