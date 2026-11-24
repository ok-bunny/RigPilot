"""Seine explicit compatibility-package API tests; all archive bytes are synthetic."""
import hashlib
import json
from pathlib import Path

import pytest
from miner_test_support import package_download

import test_seine_flights as _base
from test_seine_flights import seine_api

VERSION = _base.VERSION


def upload(ctx, *, os_family="linux", version=VERSION, content=b"synthetic Seine compatibility archive"):
    extension = "tar.gz" if os_family == "linux" else "zip"
    response = ctx.client.post(
        "/api/miner-packages/upload", headers={**ctx.headers, "Content-Type": "application/octet-stream"},
        params={"name": "RigPilot compatibility build (synthetic)", "version": version,
                "archive_type": extension, "filename": f"seine-compatible-{version}.{extension}",
                "managed_miner_type": "seine", "managed_os_family": os_family, "managed_channel": "test",
                "note": "RigPilot compatibility build; distinct from the GitHub official asset"},
        content=content,
    )
    assert response.status_code == 201, response.text
    package = response.json()
    assert package["source_type"] == "uploaded" and package["managed_channel"] == "test"
    assert package["sha256"] == hashlib.sha256(content).hexdigest()
    return package


def enroll(ctx, *, os_family="linux", credentials=False):
    token = ctx.client.post("/api/enrollment-tokens", headers=ctx.headers,
                            json={"label": "seine-pin-synthetic", "ttl_minutes": 30})
    assert token.status_code == 200, token.text
    response = ctx.client.post("/api/agent/enroll", json={
        "enrollment_token": token.json()["token"], "hostname": "synthetic-seine-pin",
        "os_family": os_family, "agent_version": "0.8.39",
    })
    assert response.status_code == 201, response.text
    identity = response.json()
    heartbeat = ctx.client.post("/api/agent/heartbeat", headers={
        "X-Agent-ID": identity["agent_id"], "Authorization": "Bearer " + identity["agent_secret"],
    }, json={"hostname": "synthetic-seine-pin", "os_family": os_family,
             "agent_version": "0.8.39", "capabilities": ["managed_mining.dual_process.v1", "managed_mining.extra_args.v1"],
             "metrics": {"gpus": None}})
    assert heartbeat.status_code == 200, heartbeat.text
    return identity if credentials else identity["agent_id"]


def apply(ctx, sheet_id, agent_id):
    return ctx.client.post(f"/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply", headers=ctx.headers)


def storage(ctx, package_id):
    with ctx.client.app.state.db.connection() as db:
        row = db.execute("SELECT * FROM miner_packages WHERE id=?", (package_id,)).fetchone()
    return ctx.client.app.state.settings.data_dir / "miner-packages" / row["storage_key"]


def assert_no_apply_queued(ctx):
    with ctx.client.app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM commands WHERE action='apply_flight_sheet'").fetchone()[0] == 0


def official_sentinel(monkeypatch):
    """Observe the official resolver without downloading or faking official SHA."""
    from fastapi import HTTPException
    from server import main as main_module
    calls = []
    def stop(url, *args, **kwargs):
        calls.append(url)
        raise HTTPException(503, "synthetic official download sentinel")
    monkeypatch.setattr(main_module, "_download_https_package", stop)
    return calls


def test_same_version_seine_test_upload_is_separate_and_pin_roundtrips(seine_api):
    ctx = seine_api
    from server.managed_miners import managed_test_package_id
    before = ctx.client.get("/api/managed-miners", headers=ctx.headers).json()
    before = next(row for row in before if row["id"] == "seine")
    package = upload(ctx)
    assert package["id"] == managed_test_package_id("seine", VERSION, "linux")
    duplicate = ctx.client.post("/api/miner-packages/upload", headers={
        **ctx.headers, "Content-Type": "application/octet-stream"}, params={
            "name": "must not replace", "version": VERSION, "archive_type": "tar.gz",
            "filename": "different.tar.gz", "managed_miner_type": "seine",
            "managed_os_family": "linux", "managed_channel": "test"}, content=b"different archive")
    assert duplicate.status_code == 409, duplicate.text
    after = ctx.client.get("/api/managed-miners", headers=ctx.headers).json()
    after = next(row for row in after if row["id"] == "seine")
    assert after["versions"] == before["versions"]  # Local pin never replaces official release assets/SHA.
    request = {**_base.body(ctx), "miner_package_id": package["id"]}
    row = _base.create(ctx, request)
    assert _base.get(ctx, row["id"])["miner_package_id"] == package["id"]
    row = _base.update(ctx, row["id"], {**request, "name": "Pinned compatibility build edited"})
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    selected = response.json()["payload"]["primary"]["miner_package"]
    assert (selected["id"], selected["sha256"], selected["version"]) == (
        package["id"], package["sha256"], VERSION)


def test_compatibility_archive_sha_and_agent_authenticated_download(seine_api):
    ctx = seine_api
    raw = b"synthetic compatibility archive with an independent SHA"
    package = upload(ctx, content=raw)
    official_sha = ctx.adapter.asset("linux").expected_sha256(VERSION)
    assert package["sha256"] != official_sha
    identity = enroll(ctx, credentials=True)
    url = package["download_url"]
    assert ctx.client.get(url).status_code == 401
    assert ctx.client.get(url, headers=ctx.headers).status_code == 401
    invalid = {"X-Agent-ID": identity["agent_id"], "Authorization": "Bearer invalid-synthetic-agent-secret"}
    assert ctx.client.get(url, headers=invalid).status_code == 401
    downloaded = ctx.client.get(url, headers={
        "X-Agent-ID": identity["agent_id"], "Authorization": "Bearer " + identity["agent_secret"],
    })
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == raw
    assert hashlib.sha256(downloaded.content).hexdigest() == package["sha256"]
    assert downloaded.headers["Cache-Control"] == "private, no-store"
    row = _base.create(ctx, {**_base.body(ctx), "miner_package_id": package["id"]})
    response = apply(ctx, row["id"], identity["agent_id"])
    assert response.status_code == 201, response.text
    selected = response.json()["payload"]["primary"]["miner_package"]
    assert selected["download_url"] == url and selected["sha256"] == package["sha256"]
    with ctx.client.app.state.db.connection() as db:
        audit = db.execute("SELECT details_json FROM audit_logs WHERE action='command_queued' ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(audit["details_json"])["selected_miner_packages"] == [{
        "role": "primary", "id": package["id"], "version": VERSION,
        "sha256": package["sha256"], "managed_channel": "test",
    }]


def test_uploaded_seine_test_package_list_exposes_real_availability_without_reading_archive(seine_api, monkeypatch):
    ctx = seine_api
    package = upload(ctx)
    archive = storage(ctx, package["id"])
    original_open = Path.open
    archive_reads = []
    def checked_open(path, *args, **kwargs):
        if path == archive:
            archive_reads.append(path)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked_open)
    response = ctx.client.get("/api/miner-packages", headers=ctx.headers)
    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["id"] == package["id"])
    assert row["available"] is True
    assert row["managed_channel"] == "test" and row["source_type"] == "uploaded"
    assert row["sha256"] == package["sha256"]
    assert archive_reads == []  # Polling checks regular-file/size, while write/apply paths hash bytes.


@pytest.mark.parametrize("damage", ("missing", "size", "directory", "metadata", "path", "version"))
def test_seine_test_package_list_reports_unavailable_files_or_metadata(seine_api, damage):
    ctx = seine_api
    package = upload(ctx)
    archive = storage(ctx, package["id"])
    if damage == "missing":
        archive.unlink()
    elif damage == "size":
        archive.write_bytes(b"different length")
    elif damage == "directory":
        archive.unlink()
        archive.mkdir()
    else:
        changes = {"metadata": ("source_type", "external"),
                   "path": ("storage_key", "wrong-package/archive.tar.gz"),
                   "version": ("version", "0.2.16")}
        column, value = changes[damage]
        with ctx.client.app.state.db.connection() as db:
            db.execute(f"UPDATE miner_packages SET {column}=? WHERE id=?", (value, package["id"]))
    response = ctx.client.get("/api/miner-packages", headers=ctx.headers)
    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["id"] == package["id"])
    assert row["available"] is False


@pytest.mark.parametrize("change", [
    {"managed_miner_type": "xmrig"}, {"managed_channel": "stable"},
    {"source_type": "external"}, {"version": "0.2.14"}, {"managed_variant": "nvidia"},
    {"id": "test-seine-noncanonical"}, {"managed_os_family": "macos"},
    {"archive_type": "zip"}, {"download_url": "https://example.test/not-hosted.tar.gz"},
    {"storage_key": "not-the-canonical-package/file.tar.gz"}, {"file_size": 1},
    {"sha256": "0" * 64},
])
def test_seine_pin_rejects_invalid_metadata_on_create_and_put(seine_api, change):
    ctx = seine_api
    package = upload(ctx)
    row = _base.create(ctx, _base.body(ctx))
    with ctx.client.app.state.db.connection() as db:
        db.execute("UPDATE miner_packages SET " + ",".join(key + "=?" for key in change) + " WHERE id=?",
                   (*change.values(), package["id"]))
    request = {**_base.body(ctx), "miner_package_id": change.get("id", package["id"])}
    for method, url in ((ctx.client.post, "/api/flight-sheets"),
                        (ctx.client.put, "/api/flight-sheets/" + row["id"])):
        response = method(url, headers=ctx.headers, json=request)
        assert response.status_code == 422, response.text
    assert _base.get(ctx, row["id"])["miner_package_id"] is None
    assert_no_apply_queued(ctx)


@pytest.mark.parametrize("damage", ["missing", "bytes", "sha", "classification"])
def test_seine_pin_apply_rejects_damaged_package_without_fallback(seine_api, monkeypatch, damage):
    ctx = seine_api
    package = upload(ctx)
    row = _base.create(ctx, {**_base.body(ctx), "miner_package_id": package["id"]})
    if damage == "missing":
        storage(ctx, package["id"]).unlink()
    elif damage == "bytes":
        path = storage(ctx, package["id"])
        path.write_bytes(b"X" * path.stat().st_size)  # Same length still fails SHA.
    else:
        column, value = ("sha256", "0" * 64) if damage == "sha" else ("managed_channel", "stable")
        with ctx.client.app.state.db.connection() as db:
            db.execute(f"UPDATE miner_packages SET {column}=? WHERE id=?", (value, package["id"]))
    calls = official_sentinel(monkeypatch)
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 409, response.text
    assert calls == []
    assert_no_apply_queued(ctx)


def test_seine_pin_target_os_must_match(seine_api, monkeypatch):
    ctx = seine_api
    package = upload(ctx, os_family="windows")
    row = _base.create(ctx, {**_base.body(ctx), "miner_package_id": package["id"]})
    calls = official_sentinel(monkeypatch)
    response = apply(ctx, row["id"], enroll(ctx, os_family="linux"))
    assert response.status_code == 409 and "operating system" in response.text
    assert calls == []
    assert_no_apply_queued(ctx)


@pytest.mark.parametrize("password", ("x", " "))
def test_stored_seine_nonempty_password_cannot_be_applied(seine_api, monkeypatch, password):
    ctx = seine_api
    package = upload(ctx)
    row = _base.create(ctx, {**_base.body(ctx), "miner_package_id": package["id"]})
    with ctx.client.app.state.db.connection() as db:
        db.execute("UPDATE flight_sheets SET pool_password=? WHERE id=?", (password, row["id"]))
    calls = official_sentinel(monkeypatch)
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 409 and "pool_password" in response.text
    assert calls == []
    assert_no_apply_queued(ctx)


@pytest.mark.parametrize("flag", ("--address", "--api-bind", "--data-dir", "--threads", "--cpu-profile", "--bench"))
def test_stored_seine_extra_args_conflicts_are_rejected_before_package_preparation(seine_api, monkeypatch, flag):
    ctx = seine_api
    package = upload(ctx)
    row = _base.create(ctx, {**_base.body(ctx), "miner_package_id": package["id"]})
    with ctx.client.app.state.db.connection() as db:
        db.execute("UPDATE flight_sheets SET extra_config_json=? WHERE id=?",
                   (json.dumps({"extra_args": flag + "=override"}), row["id"]))
    calls = official_sentinel(monkeypatch)
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 409 and "conflicts with managed parameter" in response.text
    assert calls == []
    assert_no_apply_queued(ctx)


def test_seine_pin_apply_preserves_profile_threads_and_benign_literal_arguments(seine_api):
    ctx = seine_api
    package = upload(ctx)
    options = {"cpu_threads": 3, "cpu_profile": "efficiency",
               "extra_args": '--future-option "two words"\n--future-number=7'}
    request = {**_base.body(ctx, options), "miner_package_id": package["id"]}
    row = _base.create(ctx, request)
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    primary = response.json()["payload"]["primary"]
    assert primary["extra_config"] == options
    assert primary["pool_password"] == ""
    assert primary["miner_package"]["id"] == package["id"]


def test_no_pin_prefers_uploaded_compatibility_group_over_official_resolver(seine_api, monkeypatch):
    ctx = seine_api
    package = upload(ctx)
    row = _base.create(ctx, _base.body(ctx))
    calls = official_sentinel(monkeypatch)
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    selected = response.json()["payload"]["primary"]["miner_package"]
    assert (selected["id"], selected["sha256"]) == (package["id"], package["sha256"])
    assert calls == []


def test_unpinned_seine_upload_wins_over_a_corrupt_official_cache(seine_api, monkeypatch):
    ctx = seine_api
    from server.managed_miners import managed_package_id
    package = upload(ctx)
    builtin_id = managed_package_id("seine", VERSION, "linux")
    # A corrupt official-cache record must fail its source pin before any
    # package download or use of the independent uploaded compatibility build.
    with ctx.client.app.state.db.connection() as db:
        cached = dict(db.execute("SELECT * FROM miner_packages WHERE id=?", (package["id"],)).fetchone())
        cached.update(id=builtin_id, managed_channel="stable")
        columns = list(cached)
        db.execute("INSERT INTO miner_packages (" + ",".join(columns) + ") VALUES (" +
                   ",".join("?" for _ in columns) + ")", tuple(cached[column] for column in columns))
    row = _base.create(ctx, _base.body(ctx))
    calls = official_sentinel(monkeypatch)
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    selected = response.json()["payload"]["primary"]["miner_package"]
    assert (selected["id"], selected["sha256"]) == (package["id"], package["sha256"])
    assert calls == []


@pytest.mark.parametrize("missing", ["version", "platform_asset"])
def test_no_pin_uses_matching_test_build_when_official_version_or_platform_is_missing(
    seine_api, monkeypatch, missing
):
    ctx = seine_api
    version = "0.2.16" if missing == "version" else VERSION
    package = upload(ctx, version=version)
    catalog = ctx.client.app.state.managed_miner_catalog
    # Exercise actual catalog discovery failures, not a patched resolver.
    if missing == "platform_asset":
        original_fetcher = catalog._fetcher
        linux_filename = ctx.adapter.asset("linux").filename(VERSION)
        def without_linux_asset(owner, repository):
            releases = original_fetcher(owner, repository)
            if (owner, repository) == ("zpalmtree", "seine"):
                for release in releases:
                    release["assets"] = [asset for asset in release["assets"] if asset["name"] != linux_filename]
            return releases
        catalog._fetcher = without_linux_asset
    catalog.invalidate()
    catalog.refresh_sync(force=False)
    request = {**_base.body(ctx), "miner_version": version}
    row = _base.create(ctx, request)
    calls = official_sentinel(monkeypatch)
    agent_id = enroll(ctx)
    response = apply(ctx, row["id"], agent_id)
    assert response.status_code == 201, response.text
    selected = response.json()["payload"]["primary"]["miner_package"]
    assert (selected["id"], selected["sha256"], selected["version"]) == (
        package["id"], package["sha256"], version
    )
    assert calls == []


def test_unpinned_seine_test_group_dispatches_the_target_platform_bytes(seine_api, monkeypatch):
    ctx = seine_api
    bytes_by_platform = {
        "linux": b"synthetic Seine Linux compatibility archive",
        "windows": b"synthetic Seine Windows compatibility archive",
    }
    packages = {
        platform: upload(ctx, os_family=platform, content=raw)
        for platform, raw in bytes_by_platform.items()
    }
    catalog = ctx.client.app.state.managed_miner_catalog
    original_asset = catalog.asset

    def without_official_seine_asset(miner_type, os_family, version=None, *, variant=None):
        if miner_type == "seine" and version == VERSION:
            raise ValueError(f"seine {VERSION} is unavailable for {os_family} x64")
        return original_asset(miner_type, os_family, version, variant=variant)

    monkeypatch.setattr(catalog, "asset", without_official_seine_asset)
    row = _base.create(ctx, _base.body(ctx))
    assert row["miner_package_id"] is None

    for platform, raw in bytes_by_platform.items():
        identity = enroll(ctx, os_family=platform, credentials=True)
        response = apply(ctx, row["id"], identity["agent_id"])
        assert response.status_code == 201, response.text
        selected = response.json()["payload"]["primary"]["miner_package"]
        package = packages[platform]
        assert (selected["id"], selected["sha256"], selected["version"]) == (
            package["id"], package["sha256"], VERSION
        )
        downloaded = ctx.client.get(
            selected["download_url"],
            headers={
                "X-Agent-ID": identity["agent_id"],
                "Authorization": "Bearer " + identity["agent_secret"],
            },
        )
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == raw


def test_unpinned_seine_group_rejects_a_missing_target_platform(seine_api, monkeypatch):
    ctx = seine_api
    upload(ctx, os_family="linux")
    catalog = ctx.client.app.state.managed_miner_catalog
    monkeypatch.setattr(
        catalog,
        "asset",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("seine 0.2.44 is unavailable for windows x64")
        ),
    )
    row = _base.create(ctx, _base.body(ctx))
    response = apply(ctx, row["id"], enroll(ctx, os_family="windows"))
    assert response.status_code == 422, response.text
    assert "no uploaded test package for windows x64" in response.text
    assert_no_apply_queued(ctx)


def test_explicit_seine_0215_pin_keeps_the_historical_package_behavior(seine_api):
    ctx = seine_api
    version = "0.2.15"
    package = upload(ctx, version=version)
    row = _base.create(ctx, {
        **_base.body(ctx),
        "miner_version": version,
        "miner_package_id": package["id"],
    })
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    selected = response.json()["payload"]["primary"]["miner_package"]
    assert (selected["id"], selected["sha256"], selected["version"]) == (
        package["id"], package["sha256"], version
    )


def test_independent_secondary_seine_pin_roundtrips_and_is_not_overwritten(seine_api, monkeypatch):
    ctx = seine_api
    from server import main as main_module
    monkeypatch.setattr(main_module, "_download_https_package", package_download(b"synthetic SRB GPU archive"))
    package = upload(ctx)
    request = {"name": "SRB GPU plus pinned Seine CPU", "miner_type": "srbminer",
               "miner_version": ctx.gpu.release.fallback_versions[0], "coin": "PEARL",
               "algorithm": "pearlhash", "wallet_id": ctx.wallet("PEARL"),
               "pool_url": "stratum+tcp://gpu.example.test:3333", "secondary_mode": "independent",
               "secondary": {key: value for key, value in _base.body(ctx).items() if key != "name"}}
    request["secondary"]["miner_package_id"] = package["id"]
    row = _base.create(ctx, request)
    row = _base.update(ctx, row["id"], {**request, "name": "Secondary pin edited"})
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    selected = response.json()["payload"]["secondary"]["miner_package"]
    assert (selected["id"], selected["sha256"]) == (package["id"], package["sha256"])


def test_two_independent_cpu_legs_preserve_explicit_seine_pin(seine_api, monkeypatch):
    ctx = seine_api
    package = upload(ctx)
    request = {**_base.body(ctx, {"cpu_threads": 2}), "miner_package_id": package["id"],
               "secondary_mode": "independent", "secondary": {
                   key: value for key, value in _base.body(ctx, {"cpu_threads": 3}).items()
                   if key != "name"}}
    request["secondary"]["miner_package_id"] = package["id"]
    calls = official_sentinel(monkeypatch)
    row = _base.create(ctx, request)
    row = _base.update(ctx, row["id"], {**request, "name": "Two independent Seine CPUs"})
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    payload = response.json()["payload"]
    assert payload["secondary_mode"] == "independent"
    for role, threads in (("primary", 2), ("secondary", 3)):
        selected = payload[role]
        assert selected["miner_type"] == "seine"
        assert selected["extra_config"]["cpu_threads"] == threads
        assert (selected["miner_package"]["id"], selected["miner_package"]["sha256"]) == (
            package["id"], package["sha256"])
    assert calls == []


@pytest.mark.parametrize("mutation", ["bytes", "valid_new_identity"])
def test_seine_pin_is_revalidated_before_final_queue_transaction(seine_api, monkeypatch, mutation):
    ctx = seine_api
    from server import main as main_module
    package = upload(ctx)
    row = _base.create(ctx, {**_base.body(ctx), "miner_package_id": package["id"]})
    path = storage(ctx, package["id"])
    original = main_module._validate_prepared_flight_hardware
    calls = 0
    def mutate_after_prepare(agent, payload):
        nonlocal calls
        original(agent, payload)
        calls += 1
        if calls == 1:
            raw = b"changed independently after initial package validation"
            path.write_bytes(raw)
            if mutation == "valid_new_identity":
                with ctx.client.app.state.db.connection() as db:
                    db.execute("UPDATE miner_packages SET sha256=?,file_size=? WHERE id=?",
                               (hashlib.sha256(raw).hexdigest(), len(raw), package["id"]))
    monkeypatch.setattr(main_module, "_validate_prepared_flight_hardware", mutate_after_prepare)
    response = apply(ctx, row["id"], enroll(ctx))
    assert response.status_code == 409, response.text
    assert_no_apply_queued(ctx)
