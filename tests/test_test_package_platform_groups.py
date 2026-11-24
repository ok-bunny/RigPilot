"""Real local API upload/catalog/flight dispatch; synthetic archive bytes only."""
from copy import deepcopy
import hashlib

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, controller_settings_kwargs, login
from server import main as main_module
from server.config import Settings
from server.managed_miners import ManagedMinerCatalog, managed_package_id, managed_test_package_id
from test_managed_miner_versions import release_document, release_set


VERSION = "9.0.0"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    catalog = ManagedMinerCatalog(fetcher=release_set)
    catalog.refresh_sync()
    monkeypatch.setattr(main_module, "ManagedMinerCatalog", lambda: catalog)

    def no_download(*args, **kwargs):
        pytest.fail("test version must not download an official or external package")

    monkeypatch.setattr(main_module, "_download_https_package", no_download)
    with TestClient(main_module.create_app(Settings(**controller_settings_kwargs(tmp_path)))) as client:
        headers = auth(login(client, "admin", "correct-horse-battery-staple"))
        yield client, headers, catalog


def upload(api, kind, platform, variant=None, *, content=None, version=VERSION, status=201):
    client, headers, _ = api
    raw = content or f"synthetic {kind} {platform} {variant} {version}".encode()
    extension = "zip" if platform == "windows" else "tar.gz"
    params = {"name": "user supplied build", "version": version, "archive_type": extension,
              "filename": f"{kind}-{platform}.{extension}", "managed_miner_type": kind,
              "managed_os_family": platform, "managed_channel": "test"}
    if variant:
        params["managed_variant"] = variant
    response = client.post("/api/miner-packages/upload", params=params, content=raw,
                           headers={**headers, "Content-Type": "application/octet-stream"})
    assert response.status_code == status, response.text
    if status != 201:
        return response
    row = response.json()
    assert row["id"] == managed_test_package_id(kind, version, platform, variant=variant)
    assert row["sha256"] == hashlib.sha256(raw).hexdigest()
    return row, raw


def version_entry(api, kind, version=VERSION):
    response = api[0].get("/api/managed-miners", headers=api[1])
    assert response.status_code == 200, response.text
    miner = next(row for row in response.json() if row["id"] == kind)
    entries = [row for row in miner["versions"] if row["version"] == version]
    assert len(entries) == 1
    return entries[0]


@pytest.mark.parametrize("kind,platforms", [
    ("srbminer", [("windows", None), ("linux", None)]),
    ("srbminer", [("linux", None), ("windows", None)]),
    ("xmrig", [("windows", None), ("linux", None)]),
    ("pearlminer", [("windows", None), ("linux", "nvidia"), ("linux", "amd")]),
    ("pearlminer", [("linux", "amd"), ("linux", "nvidia"), ("windows", None)]),
])
def test_same_test_version_catalog_groups_every_uploaded_platform_and_variant(api, kind, platforms):
    packages = {}
    for platform, variant in platforms:
        key = platform + ("-" + variant if variant else "")
        packages[key] = upload(api, kind, platform, variant)[0]
    entry = version_entry(api, kind)
    assert entry["prerelease"] is True and entry["source_type"] == "controller_cache"
    assert set(entry["assets"]) == set(packages)
    for key, package in packages.items():
        assert entry["assets"][key]["package_id"] == package["id"]
        assert entry["assets"][key]["cached"] is True
        assert entry["assets"][key]["available"] is True


def enroll(api, platform, *, vendor="nvidia"):
    client, headers, _ = api
    token = client.post("/api/enrollment-tokens", headers=headers,
                        json={"label": "synthetic-platform-test", "ttl_minutes": 30})
    assert token.status_code == 200, token.text
    response = client.post("/api/agent/enroll", json={"enrollment_token": token.json()["token"],
        "hostname": "synthetic-" + platform, "os_family": platform, "agent_version": "0.8.48"})
    assert response.status_code == 201, response.text
    identity = response.json()
    agent_headers = {"X-Agent-ID": identity["agent_id"], "Authorization": "Bearer " + identity["agent_secret"]}
    heartbeat = {"hostname": "synthetic-" + platform, "os_family": platform, "agent_version": "0.8.48",
        "capabilities": ["managed_mining.dual_process.v1", "managed_mining.wsl_gpu.v1"],
        "metrics": {"gpus": [{"index": 0, "vendor": vendor, "device_type": "discrete", "name": "Synthetic GPU"}]}}
    assert client.post("/api/agent/heartbeat", headers=agent_headers, json=heartbeat).status_code == 200
    return identity["agent_id"], agent_headers, heartbeat


def wallet(api, coin):
    response = api[0].post("/api/wallets", headers=api[1], json={
        "name": coin + " synthetic", "coin": coin, "address": "public-synthetic-" + coin})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def leg(api, kind, *, version=VERSION, mode="native"):
    coin = "XMR" if kind == "xmrig" else "PEARL"
    return {"miner_type": kind, "miner_version": version, "coin": coin, "wallet_id": wallet(api, coin),
        "algorithm": "pearlhash" if kind in {"pearlminer", "srbminer"} else None,
        "pool_url": "stratum+tcp://synthetic.example.test:3333", "execution_mode": mode,
        "extra_config": {"gpu_backend": "auto"} if kind == "pearlminer" else {}}


def create_and_edit(api, body):
    client, headers, _ = api
    created = client.post("/api/flight-sheets", headers=headers, json=body)
    assert created.status_code == 201, created.text
    sheet = created.json()
    listed = next(row for row in client.get("/api/flight-sheets", headers=headers).json() if row["id"] == sheet["id"])
    assert listed["miner_version"] == body["miner_version"] and listed["miner_package_id"] is None
    renamed = {**deepcopy(body), "name": body["name"] + " edited"}
    edited = client.put("/api/flight-sheets/" + sheet["id"], headers=headers, json=renamed)
    assert edited.status_code == 200, edited.text
    assert edited.json()["id"] == sheet["id"] and edited.json()["miner_package_id"] is None
    return edited.json()


def assert_dispatch(api, sheet, identity, expected):
    client, headers, _ = api
    agent_id, agent_headers, heartbeat = identity
    response = client.post(f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply", headers=headers)
    assert response.status_code == 201, response.text
    dispatched = client.post("/api/agent/heartbeat", headers=agent_headers, json=heartbeat)
    assert dispatched.status_code == 200, dispatched.text
    commands = dispatched.json()["commands"]
    assert len(commands) == 1 and commands[0]["id"] == response.json()["id"]
    payload = commands[0]["payload"]
    for role, (package, raw) in expected.items():
        actual = payload[role]["miner_package"]
        assert (actual["id"], actual["sha256"], actual["download_url"], actual["version"]) == (
            package["id"], package["sha256"], package["download_url"], VERSION)
        downloaded = client.get(actual["download_url"], headers=agent_headers)
        assert downloaded.status_code == 200 and downloaded.content == raw
        assert hashlib.sha256(downloaded.content).hexdigest() == actual["sha256"]
    return payload


@pytest.mark.parametrize("dual", [False, True], ids=["single", "independent-dual"])
def test_one_test_version_flight_edits_and_dispatches_correct_platform_bytes_on_each_leg(api, dual):
    kinds = ["srbminer", "xmrig"] if dual else ["xmrig"]
    packages = {(kind, platform): upload(api, kind, platform) for kind in kinds for platform in ("windows", "linux")}
    body = {"name": "One cross-platform test flight", **leg(api, kinds[0])}
    if dual:
        body.update(secondary_mode="independent", secondary=leg(api, kinds[1]))
    sheet = create_and_edit(api, body)
    for platform in ("windows", "linux"):
        expected = {"primary": packages[(kinds[0], platform)]}
        if dual:
            expected["secondary"] = packages[(kinds[1], platform)]
        assert_dispatch(api, sheet, enroll(api, platform), expected)


def test_wsl_flight_uses_same_version_linux_test_package_on_windows(api):
    packages = {platform: upload(api, "srbminer", platform) for platform in ("windows", "linux")}
    sheet = create_and_edit(api, {"name": "WSL test version", **leg(api, "srbminer", mode="wsl")})
    payload = assert_dispatch(api, sheet, enroll(api, "windows"), {"primary": packages["linux"]})
    assert payload["primary"]["execution_mode"] == "wsl"
    assert payload["primary"]["miner_package"]["managed_os_family"] == "linux"


def test_integrated_srb_dual_uses_one_platform_package_for_both_algorithms(api):
    packages = {platform: upload(api, "srbminer", platform) for platform in ("windows", "linux")}
    primary = {**leg(api, "srbminer"), "coin": "XMR", "wallet_id": wallet(api, "XMR"),
               "algorithm": "randomx", "extra_config": {"resource_kind": "cpu"}}
    body = {"name": "One integrated test version", **primary, "secondary": leg(api, "srbminer")}
    sheet = create_and_edit(api, body)
    for platform in ("windows", "linux"):
        payload = assert_dispatch(api, sheet, enroll(api, platform),
                                  {"primary": packages[platform], "secondary": packages[platform]})
        assert payload["primary"]["miner_package"] == payload["secondary"]["miner_package"]


@pytest.mark.parametrize("vendor", ["nvidia", "amd"])
def test_pearl_version_group_resolves_linux_gpu_variant_without_changing_windows_identity(api, vendor):
    packages = {(platform, variant): upload(api, "pearlminer", platform, variant)
                for platform, variant in [("windows", None), ("linux", "nvidia"), ("linux", "amd")]}
    sheet = create_and_edit(api, {"name": "Pearl test group", **leg(api, "pearlminer")})
    assert_dispatch(api, sheet, enroll(api, "linux", vendor=vendor), {"primary": packages[("linux", vendor)]})
    assert_dispatch(api, sheet, enroll(api, "windows"), {"primary": packages[("windows", None)]})


@pytest.mark.parametrize("uploaded,target", [("windows", "linux"), ("linux", "windows")])
def test_missing_test_platform_fails_without_official_download_or_command(api, uploaded, target):
    upload(api, "xmrig", uploaded)
    sheet = create_and_edit(api, {"name": "Single platform test group", **leg(api, "xmrig")})
    agent_id, _, _ = enroll(api, target)
    response = api[0].post(f"/api/agents/{agent_id}/flight-sheets/{sheet['id']}/apply", headers=api[1])
    assert response.status_code == 422, response.text
    assert target in response.json()["detail"] and "9.0.0" in response.json()["detail"]
    assert "no uploaded test package" in response.json()["detail"]
    with api[0].app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM commands WHERE action='apply_flight_sheet'").fetchone()[0] == 0


def test_duplicate_same_platform_is_conflict_and_does_not_replace_either_package(api):
    packages = {platform: upload(api, "xmrig", platform) for platform in ("windows", "linux")}
    upload(api, "xmrig", "linux", content=b"replacement must be refused", status=409)
    listed = api[0].get("/api/miner-packages", headers=api[1]).json()
    assert {row["id"]: row["sha256"] for row in listed} == {
        package["id"]: package["sha256"] for package, _ in packages.values()}


def test_official_same_version_upload_collision_is_unchanged(api):
    upload(api, "xmrig", "windows", version="6.26.0", status=409)
    entry = version_entry(api, "xmrig", "6.26.0")
    assert entry["prerelease"] is False
    assert all(asset["package_id"].startswith("builtin-") for asset in entry["assets"].values())


@pytest.mark.parametrize("stable_first", [False, True])
def test_official_stable_cache_cannot_merge_with_test_group_in_either_row_order(api, stable_first):
    package, _ = upload(api, "xmrig", "windows")
    # Recreate a legacy stable cache row absent from current official discovery.
    # Both metadata rows point at real hosted bytes; only the namespace differs.
    with api[0].app.state.db.connection() as db:
        row = dict(db.execute("SELECT * FROM miner_packages WHERE id=?", (package["id"],)).fetchone())
        stable = {**row, "id": managed_package_id("xmrig", VERSION, "linux"),
                  "managed_os_family": "linux", "managed_channel": "stable"}
        if stable_first:
            db.execute("DELETE FROM miner_packages WHERE id=?", (row["id"],))
        keys = list(stable)
        sql = "INSERT INTO miner_packages (" + ",".join(keys) + ") VALUES (" + ",".join("?" for _ in keys) + ")"
        db.execute(sql, [stable[key] for key in keys])
        if stable_first:
            db.execute(sql, [row[key] for key in keys])
    entry = version_entry(api, "xmrig")
    assert entry["prerelease"] is False
    assert set(entry["assets"]) == {"linux"}
    assert entry["assets"]["linux"]["package_id"].startswith("builtin-")


@pytest.mark.parametrize("preview", [False, True])
def test_real_official_release_always_hides_colliding_uploaded_tests(api, preview):
    # Only Pearl's existing discovery supports official preview releases.
    kind = "pearlminer" if preview else "xmrig"
    owner_name = "pearlfortune" if preview else "xmrig"
    for platform in ("windows", "linux"):
        upload(api, kind, platform, "nvidia" if kind == "pearlminer" and platform == "linux" else None)
    _, _, catalog = api

    def with_official(owner, repo):
        rows = release_set(owner, repo)
        if owner == owner_name:
            rows.insert(0, release_document(owner, repo, VERSION, prerelease=preview))
        return rows

    catalog._fetcher = with_official
    catalog.refresh_sync()
    entry = version_entry(api, kind)
    assert entry["prerelease"] is preview
    assert all(asset["package_id"].startswith("builtin-") for asset in entry["assets"].values())
