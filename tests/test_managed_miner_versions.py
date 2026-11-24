from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login

from server.config import Settings
from server.database import utcnow
from server.main import create_app
from server.managed_miners import (
    ManagedMinerCatalog,
    MINER_TYPES,
    managed_package_id,
    managed_test_package_id,
)


def release_document(
    owner: str,
    repo: str,
    version: str,
    *,
    os_families: tuple[str, ...] = ("windows", "linux"),
    draft: bool = False,
    prerelease: bool = False,
) -> dict[str, object]:
    if owner == "xmrig":
        tag = f"v{version}"
        filenames = {
            "windows": f"xmrig-{version}-windows-x64.zip",
            "linux": f"xmrig-{version}-linux-static-x64.tar.gz",
        }
    elif owner == "doktor83":
        tag = version
        dashed = version.replace(".", "-")
        filenames = {
            "windows": f"SRBMiner-Multi-{dashed}-win64.zip",
            "linux": f"SRBMiner-Multi-{dashed}-Linux.tar.gz",
        }
    elif owner == "pearlfortune":
        tag = f"v{version}"
        filenames = {
            "windows": f"miner-windows-v{version}.zip",
            "linux": f"pearlfortune-v{version}.tar.gz",
            "linux-nvidia": f"pearlfortune-v{version}.tar.gz",
            "linux-amd": f"pearlfortune-amd-v{version}.tar.gz",
        }
    else:
        raise AssertionError(f"unexpected managed miner repository: {owner}/{repo}")
    def asset(os_family: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": filenames[os_family],
            "browser_download_url": (
                f"https://github.com/{owner}/{repo}/releases/download/"
                f"{tag}/{filenames[os_family]}"
            ),
            "uploader": {"login": "must-not-leak"},
        }
        return payload

    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "name": "must not leak",
        "body": "must not leak",
        "assets": [asset(os_family) for os_family in os_families],
    }


def release_set(owner: str, repo: str) -> list[dict[str, object]]:
    if owner == "xmrig":
        versions = ("6.26.0", "6.25.0", "6.23.0", "6.22.2")
        families = ("windows", "linux")
    elif owner == "doktor83":
        versions = ("3.4.6", "3.4.5", "3.4.4", "3.4.3")
        families = ("windows", "linux")
    elif owner == "pearlfortune":
        versions = ("1.2.4", "1.2.3", "1.2.2", "1.2.1")
        families = ("windows", "linux-nvidia", "linux-amd")
    else:
        raise AssertionError(f"unexpected managed miner repository: {owner}/{repo}")
    return [
        release_document(owner, repo, version, os_families=families)
        for version in versions
    ]


def shifted_release_set(owner: str, repo: str) -> list[dict[str, object]]:
    if owner == "xmrig":
        versions = ("7.0.2", "7.0.1", "7.0.0")
    elif owner == "pearlfortune":
        versions = ("3.0.2", "3.0.1", "3.0.0")
    elif owner == "doktor83":
        versions = ("4.0.2", "4.0.1", "4.0.0")
    else:
        raise AssertionError(f"unexpected managed miner repository: {owner}/{repo}")
    return [release_document(owner, repo, version) for version in versions]


def refresh_after_snapshot(catalog, monkeypatch):
    """Complete an offline refresh after the caller obtains its old snapshot."""
    original = catalog.releases
    refreshed = False

    def releases(miner_type):
        nonlocal refreshed
        snapshot = original(miner_type)
        if not refreshed:
            refreshed = True
            catalog._fetcher = shifted_release_set
            catalog.refresh_sync()
        return snapshot

    monkeypatch.setattr(catalog, "releases", releases)


def test_default_asset_remains_in_selected_snapshot_during_refresh(monkeypatch):
    catalog = ManagedMinerCatalog(fetcher=release_set)
    catalog.refresh_sync()
    refresh_after_snapshot(catalog, monkeypatch)
    assert catalog.asset("xmrig", "linux").version == "6.26.0"
    assert catalog.latest_version("xmrig") == "7.0.2"


def test_default_flight_version_remains_in_selected_snapshot_during_refresh(tmp_path, monkeypatch):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        wallet = create_xmr_wallet(client, token)
        refresh_after_snapshot(app.state.managed_miner_catalog, monkeypatch)
        body = flight_payload(str(wallet["id"]), "6.26.0")
        body.pop("miner_version")
        response = client.post("/api/flight-sheets", headers=auth(token), json=body)
        assert response.status_code == 201, response.text
        assert response.json()["miner_version"] == "6.26.0"


def test_managed_test_package_id_reuses_validation_without_builtin_collision():
    official_id = managed_package_id(
        "pearlminer",
        "2.0.3",
        "linux",
        variant="nvidia",
    )
    test_id = managed_test_package_id(
        "pearlminer",
        "2.0.3",
        "linux",
        "nvidia",
    )

    assert official_id == "builtin-pearlminer-2.0.3-linux-x64-nvidia"
    assert test_id == "test-pearlminer-2.0.3-linux-x64-nvidia"
    assert test_id != official_id
    assert managed_test_package_id("xmrig", "6.26.0", "ubuntu") == (
        "test-xmrig-6.26.0-linux-x64"
    )
    for retired_type in ("lolminer", "forgeminer"):
        with pytest.raises(ValueError, match="unsupported managed miner type"):
            managed_test_package_id(retired_type, "1.5.16", "windows")
    with pytest.raises(ValueError, match="unsupported managed miner type"):
        managed_test_package_id("unknown", "1.0.0", "linux")
    with pytest.raises(ValueError, match="require nvidia or amd variant"):
        managed_test_package_id("pearlminer", "2.0.3", "linux")


def test_catalog_refresh_uses_ttl_and_exposes_all_trusted_releases():
    now = [100.0]
    calls: list[tuple[str, str]] = []

    def fetcher(owner: str, repo: str):
        calls.append((owner, repo))
        return [
            release_document(owner, repo, "9.9.9", draft=True),
            release_document(owner, repo, "8.8.8", prerelease=True),
            *release_set(owner, repo),
        ]

    catalog = ManagedMinerCatalog(
        ttl_seconds=10,
        failure_ttl_seconds=2,
        fetcher=fetcher,
        clock=lambda: now[0],
    )
    catalog.refresh_sync(force=False)
    first = catalog.payload()
    assert len(calls) == len(MINER_TYPES)
    assert first[0]["catalog_source"] == "github"
    assert first[0]["latest_version"] == "6.26.0"
    assert [item["version"] for item in first[0]["versions"]] == [
        "6.26.0",
        "6.25.0",
        "6.23.0",
        "6.22.2",
    ]
    assert "must not leak" not in json.dumps(first)
    assert first[0]["checked_at"]
    assert first[0]["refresh_after"]

    catalog.payload()
    assert len(calls) == len(MINER_TYPES)
    now[0] = 111.0
    catalog.refresh_sync(force=False)
    assert len(calls) == 2 * len(MINER_TYPES)


def test_catalog_payload_derives_latest_version_from_one_snapshot(monkeypatch):
    def offline(_owner: str, _repo: str):
        raise OSError("offline")

    catalog = ManagedMinerCatalog(fetcher=offline)
    catalog.refresh_sync()

    def reject_second_snapshot(_miner_type: str) -> str:
        raise AssertionError("payload must not reread the live catalog")

    monkeypatch.setattr(catalog, "latest_version", reject_second_snapshot)
    payload = catalog.payload()

    for miner in payload:
        versions = {release["version"] for release in miner["versions"]}
        assert miner["version"] == miner["latest_version"]
        assert miner["latest_version"] in versions




def test_pearl_catalog_accepts_official_prereleases_without_making_them_default():
    def fetcher(owner: str, repo: str):
        if owner != "pearlfortune":
            return release_set(owner, repo)
        return [
            release_document(
                owner,
                repo,
                "2.0.4",
                os_families=("linux-nvidia",),
                prerelease=True,
            ),
            release_document(
                owner,
                repo,
                "2.0.3",
                os_families=("linux-nvidia",),
            ),
            release_document(
                owner,
                repo,
                "1.2.5",
                os_families=("linux-nvidia",),
                prerelease=True,
            ),
            release_document(owner, repo, "1.2.4"),
        ]

    catalog = ManagedMinerCatalog(fetcher=fetcher)
    catalog.refresh_sync()

    releases = catalog.releases("pearlminer")
    assert [release.version for release in releases] == [
        "2.0.4",
        "2.0.3",
        "1.2.5",
        "1.2.4",
    ]
    assert [release.prerelease for release in releases] == [True, False, True, False]
    assert len({release.version for release in releases}) == 4
    assert all(release.source_type == "github" for release in releases)
    assert catalog.latest_version("pearlminer") == "2.0.3"

    preview = catalog.asset(
        "pearlminer",
        "linux",
        "2.0.4",
        variant="nvidia",
    )
    assert preview.download_url == (
        "https://github.com/pearlfortune/pearl-miner/releases/download/"
        "v2.0.4/pearlfortune-v2.0.4.tar.gz"
    )
    assert preview.package_id == "builtin-pearlminer-2.0.4-linux-x64-nvidia"
    with pytest.raises(ValueError, match="unavailable for windows"):
        catalog.asset("pearlminer", "windows", "2.0.4")








def test_pearl_catalog_keeps_stable_default_while_exposing_all_previews():
    def fetcher(owner: str, repo: str):
        if owner != "pearlfortune":
            return release_set(owner, repo)
        return [
            release_document(
                owner,
                repo,
                version,
                os_families=("linux-nvidia",),
                prerelease=True,
            )
            for version in ("3.0.4", "3.0.3", "3.0.2", "3.0.1")
        ] + [
            release_document(
                owner,
                repo,
                "2.0.3",
                os_families=("linux-nvidia",),
            )
        ]

    catalog = ManagedMinerCatalog(fetcher=fetcher)
    catalog.refresh_sync()

    releases = catalog.releases("pearlminer")
    assert [release.version for release in releases] == [
        "3.0.4",
        "3.0.3",
        "3.0.2",
        "3.0.1",
        "2.0.3",
    ]
    assert [release.prerelease for release in releases] == [
        True,
        True,
        True,
        True,
        False,
    ]
    assert catalog.latest_version("pearlminer") == "2.0.3"


def test_pearl_refresh_without_stable_preserves_previous_snapshot():
    prerelease_only = [False]

    def fetcher(owner: str, repo: str):
        if owner != "pearlfortune":
            return release_set(owner, repo)
        if prerelease_only[0]:
            return [
                release_document(
                    owner,
                    repo,
                    version,
                    os_families=("linux-nvidia",),
                    prerelease=True,
                )
                for version in ("3.0.4", "3.0.3", "3.0.2", "3.0.1")
            ]
        return [
            release_document(
                owner,
                repo,
                "2.0.4",
                os_families=("linux-nvidia",),
                prerelease=True,
            ),
            release_document(
                owner,
                repo,
                "2.0.3",
                os_families=("linux-nvidia",),
            ),
        ]

    catalog = ManagedMinerCatalog(fetcher=fetcher)
    catalog.refresh_sync()
    previous = catalog.releases("pearlminer")
    assert [release.version for release in previous] == ["2.0.4", "2.0.3"]

    prerelease_only[0] = True
    catalog.refresh_sync()

    assert catalog.releases("pearlminer") == previous
    pearl_payload = next(
        item for item in catalog.payload() if item["id"] == "pearlminer"
    )
    assert pearl_payload["catalog_source"] == "github"
    assert pearl_payload["latest_version"] == "2.0.3"
    assert pearl_payload["refresh_error"] == (
        "GitHub returned no supported stable Pearl Miner release"
    )


def test_catalog_semantically_sorts_all_shuffled_versions():
    def fetcher(owner: str, repo: str):
        return [
            release_document(owner, repo, version)
            for version in ("2.9.99", "9.9.9", "2.10.0", "10.0.0")
        ]

    catalog = ManagedMinerCatalog(fetcher=fetcher)
    catalog.refresh_sync()

    assert [item.version for item in catalog.releases("xmrig")] == [
        "10.0.0",
        "9.9.9",
        "2.10.0",
        "2.9.99",
    ]


def test_catalog_falls_back_and_rejects_unofficial_asset_urls():
    def unavailable(owner: str, repo: str):
        if owner == "xmrig":
            raise OSError("offline")
        document = release_document(owner, repo, "9.9.9")
        document["assets"][0]["browser_download_url"] = (
            "https://attacker.example/SRBMiner-Multi-9-9-9-win64.zip"
        )
        document["assets"][1]["browser_download_url"] = (
            "https://attacker.example/SRBMiner-Multi-9-9-9-Linux.tar.gz"
        )
        return [document]

    catalog = ManagedMinerCatalog(fetcher=unavailable)
    catalog.refresh_sync()
    payload = catalog.payload()
    by_id = {item["id"]: item for item in payload}
    assert by_id["xmrig"]["catalog_source"] == "fallback"
    assert by_id["srbminer"]["catalog_source"] == "fallback"
    assert by_id["pearlminer"]["catalog_source"] == "fallback"
    assert set(by_id) == {"xmrig", "srbminer", "pearlminer", "seine"}
    assert by_id["xmrig"]["refresh_error"] == "offline"
    assert "no supported SRBMiner-MULTI releases" in by_id["srbminer"]["refresh_error"]
    assert "no supported Pearl Miner releases" in by_id["pearlminer"]["refresh_error"]
    assert [item["version"] for item in by_id["pearlminer"]["versions"]] == [
        "2.0.3",
        "1.2.4",
        "1.2.3",
    ]
    assert [item["version"] for item in by_id["srbminer"]["versions"]] == [
        "3.7.2",
        "3.7.1",
        "3.4.6",
        "3.4.5",
        "3.4.4",
    ]
    assert "attacker.example" not in json.dumps(payload)


def test_catalog_tracks_platform_specific_assets_and_strict_versions():
    def fetcher(owner: str, repo: str):
        if owner == "doktor83":
            return [
                release_document(
                    owner, repo, "3.4.6", os_families=("linux",)
                )
            ]
        return release_set(owner, repo)

    catalog = ManagedMinerCatalog(fetcher=fetcher)
    catalog.refresh_sync()
    release = catalog.releases("srbminer")[0]
    assert set(release.assets) == {"linux"}
    assert catalog.asset("srbminer", "ubuntu", "3.4.6").os_family == "linux"
    with pytest.raises(ValueError, match="unavailable for windows"):
        catalog.asset("srbminer", "windows", "3.4.6")
    with pytest.raises(ValueError, match="numeric MAJOR.MINOR.PATCH"):
        catalog.asset("xmrig", "linux", "v6.26.0")


def login(client: TestClient) -> str:
    return _login(client, 'admin', 'correct-horse-battery-staple')


def managed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
    )
    app = create_app(settings)
    app.state.managed_miner_catalog._fetcher = release_set
    app.state.managed_miner_catalog.refresh_sync()

    def download(url: str, destination: Path, **kwargs):
        del url, kwargs
        content = b"mock official miner archive"
        destination.write_bytes(content)
        return len(content), hashlib.sha256(content).hexdigest()

    monkeypatch.setattr("server.main._download_https_package", download)
    return app


def test_miner_adapter_catalog_is_authenticated_and_data_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert client.get("/api/miner-adapters").status_code == 401
        token = login(client)
        response = client.get("/api/miner-adapters", headers=auth(token))
        assert response.status_code == 200, response.text
        catalog = response.json()
        assert {item["miner_type"] for item in catalog} == {
            "xmrig",
            "srbminer",
            "pearlminer",
            "seine",
        }
        assert {item["abi_version"] for item in catalog} == {
            "miner-adapter.v1"
        }
        by_type = {item["miner_type"]: item for item in catalog}
        assert by_type["xmrig"]["release"]["owner"] == "xmrig"
        assert by_type["srbminer"]["runtime_id"] == "builtin.srbminer.v1"
        assert "builtin-pearlminer-" in by_type["pearlminer"][
            "package_namespaces"
        ]
        serialized = json.dumps(catalog, sort_keys=True)
        for forbidden in ("shell", "powershell", "python_hook", "command_line"):
            assert forbidden not in serialized.casefold()


def test_unknown_adapter_is_rejected_at_every_controller_write_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        wallet = create_xmr_wallet(client, token)
        invalid_flight = client.post(
            "/api/flight-sheets",
            headers=auth(token),
            json={
                "name": "Unknown adapter",
                "miner_type": "vendor.untrusted-miner",
                "coin": "XMR",
                "wallet_id": wallet["id"],
                "pool_url": "stratum+tcp://pool.example.test:3333",
            },
        )
        assert invalid_flight.status_code == 422, invalid_flight.text
        assert "unsupported managed miner adapter" in invalid_flight.text

        invalid_import = client.post(
            "/api/miner-packages/import",
            headers=auth(token),
            json={
                "name": "Unknown package",
                "version": "1.0.0",
                "download_url": "https://example.test/miner.tar.gz",
                "archive_type": "tar.gz",
                "managed_miner_type": "vendor.untrusted-miner",
                "managed_os_family": "linux",
                "managed_channel": "test",
            },
        )
        assert invalid_import.status_code == 422, invalid_import.text
        assert "unsupported managed miner adapter" in invalid_import.text

        invalid_upload = client.post(
            "/api/miner-packages/upload",
            headers={**auth(token), "Content-Type": "application/octet-stream"},
            params={
                "name": "Unknown package",
                "version": "1.0.0",
                "archive_type": "tar.gz",
                "filename": "miner.tar.gz",
                "managed_miner_type": "vendor.untrusted-miner",
                "managed_os_family": "linux",
                "managed_channel": "test",
            },
            content=b"not-an-archive",
        )
        assert invalid_upload.status_code == 422, invalid_upload.text
        assert "Unsupported miner package category" in invalid_upload.text


def test_adapter_id_alias_is_canonicalized_without_changing_legacy_wire_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        wallet = create_xmr_wallet(client, token)
        body = flight_payload(str(wallet["id"]), "6.26.0")
        body["miner_type"] = "org.xmrig.xmrig"
        response = client.post(
            "/api/flight-sheets", headers=auth(token), json=body
        )
        assert response.status_code == 201, response.text
        assert response.json()["miner_type"] == "xmrig"


def enroll_linux_agent(client: TestClient, token: str) -> str:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "managed-versions", "ttl_minutes": 30},
    ).json()
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued["token"],
            "name": "managed-rig",
            "hostname": "managed-rig.local",
            "os_family": "linux",
            "agent_version": "0.5.0",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"]


def enroll_linux_agent_with_secret(
    client: TestClient,
    token: str,
    *,
    name: str,
    agent_version: str = "0.6.79",
) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": name, "ttl_minutes": 30},
    ).json()
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued["token"],
            "name": name,
            "hostname": f"{name}.local",
            "os_family": "linux",
            "agent_version": agent_version,
        },
    )
    assert response.status_code == 201, response.text
    credentials = response.json()
    agent_headers = {
        "X-Agent-ID": credentials["agent_id"],
        "Authorization": f"Bearer {credentials['agent_secret']}",
    }
    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers=agent_headers,
        json={
            "hostname": f"{name}.local",
            "os_family": "linux",
            "platform": "linux",
            "agent_version": agent_version,
            "capabilities": ["managed_mining.dual_process.v1"],
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
    assert heartbeat.json()["commands"] == []
    return credentials["agent_id"], credentials["agent_secret"]


def create_xmr_wallet(client: TestClient, token: str) -> dict[str, object]:
    response = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "XMR public",
            "coin": "XMR",
            "address": "4" + "A" * 94,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def flight_payload(wallet_id: str, version: str) -> dict[str, object]:
    return {
        "name": "Managed XMR",
        "miner_type": "xmrig",
        "miner_version": version,
        "coin": "XMR",
        "wallet_id": wallet_id,
        "pool_url": "stratum+ssl://pool.example.test:443",
        "worker_name_template": "{hostname}",
        "extra_config": {"cpu_threads": 4},
    }


def _assert_legacy_wire_has_no_adapter_metadata(value: object) -> None:
    forbidden = {
        "adapter_id",
        "adapter_revision",
        "runtime_id",
        "telemetry_id",
        "abi_version",
    }
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for nested in value.values():
            _assert_legacy_wire_has_no_adapter_metadata(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_legacy_wire_has_no_adapter_metadata(nested)


def test_all_builtin_adapters_keep_exact_legacy_apply_command_wire_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        xmr_wallet = create_xmr_wallet(client, token)
        pearl_wallet_response = client.post(
            "/api/wallets",
            headers=auth(token),
            json={
                "name": "PEARL public",
                "coin": "PEARL",
                "address": "public-pearl-address",
            },
        )
        assert pearl_wallet_response.status_code == 201, pearl_wallet_response.text
        pearl_wallet = pearl_wallet_response.json()
        flights = (
            flight_payload(str(xmr_wallet["id"]), "6.26.0"),
            {
                "name": "Managed SRB XMR",
                "miner_type": "srbminer",
                "miner_version": "3.4.6",
                "coin": "XMR",
                "wallet_id": xmr_wallet["id"],
                "pool_url": "stratum+ssl://pool.example.test:443",
                "algorithm": "randomx",
                "worker_name_template": "{hostname}",
                "extra_config": {"huge_pages": True, "keepalive": True},
            },
            {
                "name": "Managed Pearl",
                "miner_type": "pearlminer",
                "miner_version": "1.2.4",
                "coin": "PEARL",
                "wallet_id": pearl_wallet["id"],
                "pool_url": "pearl://global.pearlfortune.org:443",
                "algorithm": "pearlhash",
                "worker_name_template": "{hostname}",
                "extra_config": {
                    "gpu_backend": "nvidia",
                    "cuda_variant": "auto",
                },
            },
        )

        for request in flights:
            miner_type = str(request["miner_type"])
            agent_id, agent_secret = enroll_linux_agent_with_secret(
                client,
                token,
                name=f"wire-{miner_type}",
            )
            created = client.post(
                "/api/flight-sheets", headers=auth(token), json=request
            )
            assert created.status_code == 201, created.text
            applied = client.post(
                f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply",
                headers=auth(token),
            )
            assert applied.status_code == 201, applied.text

            heartbeat = client.post(
                "/api/agent/heartbeat",
                headers={
                    "X-Agent-ID": agent_id,
                    "Authorization": f"Bearer {agent_secret}",
                },
                json={
                    "hostname": f"wire-{miner_type}.local",
                    "os_family": "linux",
                    "platform": "linux",
                    "agent_version": "0.6.79",
                    "capabilities": ["managed_mining.dual_process.v1"],
                },
            )
            assert heartbeat.status_code == 200, heartbeat.text
            commands = heartbeat.json()["commands"]
            assert len(commands) == 1
            assert commands[0]["action"] == "apply_flight_sheet"
            queued_payload = commands[0]["payload"]
            assert set(queued_payload) == {
                "flight_sheet_id",
                "name",
                "worker_name",
                "primary",
                "secondary",
            }
            assert set(queued_payload["primary"]) == {
                "miner_type",
                "coin",
                "wallet_address",
                "pool_url",
                "miner_profile",
                "miner_package",
                "algorithm",
                "pool_password",
                "device_indices",
                "extra_config",
            }
            assert set(queued_payload["primary"]["miner_package"]) == {
                "id",
                "name",
                "version",
                "download_url",
                "sha256",
                "archive_type",
            }
            assert queued_payload["primary"]["pool_password"] == "x"
            assert applied.json()["payload"]["primary"]["pool_password"] == (
                "[configured]"
            )
            assert queued_payload["primary"]["miner_type"] == miner_type
            _assert_legacy_wire_has_no_adapter_metadata(queued_payload)


def test_flight_sheet_selects_updates_and_applies_exact_platform_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        agent_id = enroll_linux_agent(client, token)
        wallet = create_xmr_wallet(client, token)
        created = client.post(
            "/api/flight-sheets",
            headers=auth(token),
            json=flight_payload(str(wallet["id"]), "6.25.0"),
        )
        assert created.status_code == 201, created.text
        sheet = created.json()
        assert sheet["miner_version"] == "6.25.0"

        applied = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
            headers=auth(token),
        )
        assert applied.status_code == 201, applied.text
        package = applied.json()["payload"]["primary"]["miner_package"]
        assert package["id"] == "builtin-xmrig-6.25.0-linux-x64"
        assert package["version"] == "6.25.0"

        def offline(owner: str, repo: str):
            del owner, repo
            raise OSError("offline")

        app.state.managed_miner_catalog._fetcher = offline
        app.state.managed_miner_catalog.invalidate()
        cached_offline = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
            headers=auth(token),
        )
        assert cached_offline.status_code == 201, cached_offline.text
        assert (
            cached_offline.json()["payload"]["primary"]["miner_package"]["id"]
            == "builtin-xmrig-6.25.0-linux-x64"
        )
        app.state.managed_miner_catalog._fetcher = release_set
        app.state.managed_miner_catalog.invalidate()
        app.state.managed_miner_catalog.refresh_sync(force=False)

        changed = client.put(
            f"/api/flight-sheets/{sheet['id']}",
            headers=auth(token),
            json=flight_payload(str(wallet["id"]), "6.22.2"),
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["miner_version"] == "6.22.2"
        reapplied = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
            headers=auth(token),
        )
        assert reapplied.status_code == 201, reapplied.text
        assert (
            reapplied.json()["payload"]["primary"]["miner_package"]["id"]
            == "builtin-xmrig-6.22.2-linux-x64"
        )

        invalid = client.put(
            f"/api/flight-sheets/{sheet['id']}",
            headers=auth(token),
            json=flight_payload(str(wallet["id"]), "6.22.0"),
        )
        assert invalid.status_code == 422


def test_cached_official_primary_version_remains_freely_selectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        agent_id = enroll_linux_agent(client, token)
        wallet = create_xmr_wallet(client, token)
        original = flight_payload(str(wallet["id"]), "6.25.0")
        created = client.post(
            "/api/flight-sheets", headers=auth(token), json=original
        )
        assert created.status_code == 201, created.text
        sheet = created.json()
        cached = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
            headers=auth(token),
        )
        assert cached.status_code == 201, cached.text

        app.state.managed_miner_catalog._fetcher = shifted_release_set
        app.state.managed_miner_catalog.invalidate()
        app.state.managed_miner_catalog.refresh_sync(force=False)
        preserved = dict(original)
        preserved["name"] = "Renamed while pinned"
        preserved["pool_url"] = "stratum+tcp://other-pool.example.test:3333"
        updated = client.put(
            f"/api/flight-sheets/{sheet['id']}",
            headers=auth(token),
            json=preserved,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["miner_version"] == "6.25.0"
        assert updated.json()["name"] == "Renamed while pinned"

        # Apply reuses the referenced controller cache without needing the
        # rolled-out version to remain in discovery.
        reapplied = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
            headers=auth(token),
        )
        assert reapplied.status_code == 201, reapplied.text
        assert (
            reapplied.json()["payload"]["primary"]["miner_package"]["id"]
            == "builtin-xmrig-6.25.0-linux-x64"
        )

        changed_version = dict(preserved)
        changed_version["miner_version"] = "6.23.0"
        rejected_change = client.put(
            f"/api/flight-sheets/{sheet['id']}",
            headers=auth(token),
            json=changed_version,
        )
        assert rejected_change.status_code == 422
        reusable_create = client.post(
            "/api/flight-sheets",
            headers=auth(token),
            json={**original, "name": "Reuse cached old version on a new sheet"},
        )
        assert reusable_create.status_code == 201, reusable_create.text

        catalog = client.get("/api/managed-miners", headers=auth(token))
        assert catalog.status_code == 200, catalog.text
        xmrig = next(item for item in catalog.json() if item["id"] == "xmrig")
        cached_release = next(
            item for item in xmrig["versions"] if item["version"] == "6.25.0"
        )
        assert cached_release["source_type"] == "controller_cache"
        assert cached_release["assets"]["linux"]["cached"] is True


def test_put_can_preserve_rolled_out_secondary_version_only_on_same_sheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        primary_wallet = create_xmr_wallet(client, token)
        secondary_wallet_response = client.post(
            "/api/wallets",
            headers=auth(token),
            json={
                "name": "PEARL public",
                "coin": "PEARL",
                "address": "public-pearl-address",
            },
        )
        assert secondary_wallet_response.status_code == 201
        secondary_wallet = secondary_wallet_response.json()
        body: dict[str, object] = {
            "name": "Pinned SRB dual",
            "miner_type": "srbminer",
            "miner_version": "3.4.4",
            "coin": "XMR",
            "wallet_id": primary_wallet["id"],
            "pool_url": "stratum+tcp://primary-pool.example.test:3333",
            "worker_name_template": "{hostname}",
            "secondary": {
                "miner_type": "srbminer",
                "miner_version": "3.4.4",
                "coin": "PEARL",
                "wallet_id": secondary_wallet["id"],
                "pool_url": "stratum+tcp://secondary-pool.example.test:4444",
                "algorithm": "pearlhash",
                "device_indices": [0],
            },
        }
        created = client.post(
            "/api/flight-sheets", headers=auth(token), json=body
        )
        assert created.status_code == 201, created.text
        sheet = created.json()

        app.state.managed_miner_catalog._fetcher = shifted_release_set
        app.state.managed_miner_catalog.invalidate()
        app.state.managed_miner_catalog.refresh_sync(force=False)
        preserved = {**body, "name": "Pinned SRB dual renamed"}
        updated = client.put(
            f"/api/flight-sheets/{sheet['id']}",
            headers=auth(token),
            json=preserved,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["miner_version"] == "3.4.4"
        assert updated.json()["secondary"]["miner_version"] == "3.4.4"

        changed_secondary = dict(preserved)
        changed_secondary["secondary"] = {
            **body["secondary"],
            "miner_version": "3.4.3",
        }
        rejected = client.put(
            f"/api/flight-sheets/{sheet['id']}",
            headers=auth(token),
            json=changed_secondary,
        )
        assert rejected.status_code == 422


def test_cache_pruning_keeps_flight_and_active_command_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = managed_client(tmp_path, monkeypatch)
    with TestClient(app) as client:
        token = login(client)
        agent_id = enroll_linux_agent(client, token)
        wallet = create_xmr_wallet(client, token)
        sheet = client.post(
            "/api/flight-sheets",
            headers=auth(token),
            json=flight_payload(str(wallet["id"]), "6.23.0"),
        ).json()

        database = app.state.db
        now = utcnow()
        seeded: dict[str, Path] = {}
        for version in ("6.29.0", "6.28.0", "6.27.0", "6.22.0", "6.21.0"):
            package_id = f"builtin-xmrig-{version}-linux-x64"
            filename = f"xmrig-{version}-linux-static-x64.tar.gz"
            storage_key = f"{package_id}/{filename}"
            path = tmp_path / "miner-packages" / storage_key
            path.parent.mkdir(parents=True)
            path.write_bytes(version.encode())
            seeded[version] = path
            with database.connection() as db:
                db.execute(
                    """INSERT INTO miner_packages
                       (id,name,version,download_url,sha256,archive_type,note,
                        source_type,storage_key,original_filename,file_size,
                        managed_miner_type,managed_os_family,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        package_id,
                        "XMRig",
                        version,
                        f"/api/agent/miner-packages/{package_id}/download",
                        "a" * 64,
                        "tar.gz",
                        "RigPilot managed miner; cached from the official GitHub release",
                        "uploaded",
                        storage_key,
                        filename,
                        path.stat().st_size,
                        "xmrig",
                        "linux",
                        1,
                        now,
                        now,
                    ),
                )
        active_package_id = "builtin-xmrig-6.21.0-linux-x64"
        with database.connection() as db:
            db.execute(
                """INSERT INTO miner_packages
                   (id,name,version,download_url,sha256,archive_type,note,
                    source_type,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "manual-package-must-survive",
                    "manual XMRig archive",
                    "1.0.0",
                    "https://downloads.example.test/manual.zip",
                    "",
                    "zip",
                    "operator upload",
                    "external",
                    1,
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO commands
                   (id,agent_id,action,payload_json,status,created_by,created_at,expires_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "active-cache-reference",
                    agent_id,
                    "collect_diagnostics",
                    json.dumps({"primary": {"miner_package": {"id": active_package_id}}}),
                    "running",
                    1,
                    now,
                    "2999-01-01T00:00:00+00:00",
                ),
            )

        applied = client.post(
            f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply",
            headers=auth(token),
        )
        assert applied.status_code == 201, applied.text

        with database.connection() as db:
            remaining = {
                row["version"]
                for row in db.execute(
                    """SELECT version FROM miner_packages
                       WHERE managed_miner_type='xmrig' AND managed_os_family='linux'"""
                )
            }
            manual_exists = db.execute(
                "SELECT 1 FROM miner_packages WHERE id='manual-package-must-survive'"
            ).fetchone()
        assert "6.22.0" not in remaining
        assert not seeded["6.22.0"].exists()
        assert not seeded["6.22.0"].parent.exists()
        # 6.23 is protected by the flight sheet and 6.21 by the running task.
        assert {"6.23.0", "6.21.0"} <= remaining
        assert seeded["6.21.0"].is_file()
        assert remaining == {"6.29.0", "6.28.0", "6.27.0", "6.23.0", "6.21.0"}
        assert manual_exists
