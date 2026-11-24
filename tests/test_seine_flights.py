"""Seine Controller schemas and ASGI roundtrips using synthetic offline fixtures."""
from copy import deepcopy
from types import SimpleNamespace
import socket

import pytest


VERSION = "0.2.45"
COIN = "BNT"
PROFILES = ("balanced", "throughput", "efficiency")


@pytest.fixture()
def seine_api(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Seine API fixtures permit no socket network access")

    for method in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, method, no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setenv("RIGPILOT_DATA_DIR", str(tmp_path / "global-data"))
    monkeypatch.setenv("RIGPILOT_DATABASE", str(tmp_path / "global-data/controller.db"))
    monkeypatch.setenv("RIGPILOT_SECRET_KEY", "seine-api-draft-synthetic-secret-with-enough-entropy")
    monkeypatch.setenv("RIGPILOT_ADMIN_PASSWORD", "seine-api-draft-synthetic-password")

    from fastapi.testclient import TestClient
    from server import main as main_module
    from server.config import Settings
    from server.managed_miners import ManagedMinerCatalog
    from server.miner_adapters import MINER_ADAPTERS

    matches = [a for a in MINER_ADAPTERS.list()
               if (a.release.owner, a.release.repository) == ("zpalmtree", "seine")]
    assert len(matches) == 1, "Seine must be declared exactly once in the real registry"
    adapter = matches[0]
    algorithm = adapter.normalize_algorithm(None, coin=COIN)
    assert (adapter.legacy_miner_type, adapter.adapter_id, algorithm) == (
        "seine", "org.zpalmtree.seine", "blocknet-argon2id")
    assert adapter.algorithm_resource(algorithm, coin=COIN) == "cpu"
    assert set(adapter.pool_schemes) == {"stratum+tcp"}
    options = {spec.name: spec for spec in adapter.options}
    assert options["cpu_threads"].minimum == 1 and options["cpu_threads"].maximum is None
    assert options["cpu_threads"].nullable is True
    assert set(options["cpu_profile"].choices) == set(PROFILES)
    assert options["cpu_profile"].default == "balanced"

    def offline_catalog(owner, repository):
        if (owner, repository) != (adapter.release.owner, adapter.release.repository):
            raise OSError("Other miners use their existing offline fallback")
        tag = adapter.release.tag_for_version(VERSION)
        assets = []
        for asset in adapter.assets:
            name = asset.filename(VERSION)
            entry = {"name": name, "browser_download_url":
                     f"https://github.com/{owner}/{repository}/releases/download/{tag}/{name}"}
            digest = asset.expected_sha256(VERSION)
            if digest is not None:
                entry["digest"] = "sha256:" + digest
            assets.append(entry)
        return [{"tag_name": tag, "draft": False, "prerelease": False, "assets": assets}]

    catalog = ManagedMinerCatalog(fetcher=offline_catalog)
    catalog.refresh_sync()
    monkeypatch.setattr(main_module, "ManagedMinerCatalog", lambda: catalog)
    monkeypatch.setattr(main_module, "_download_https_package", no_network)
    settings = Settings(data_dir=tmp_path / "api-data", database_path=tmp_path / "api-data/controller.db",
                        secret_key="seine-api-draft-synthetic-secret-with-enough-entropy",
                        admin_username="admin", admin_password="seine-api-draft-synthetic-password")
    with TestClient(main_module.create_app(settings)) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": settings.admin_password})
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["token"]}

        def wallet(coin):
            response = client.post("/api/wallets", headers=headers,
                                   json={"name": coin + " synthetic", "coin": coin,
                                         "address": "public-synthetic-" + coin.lower()})
            assert response.status_code == 201, response.text
            return response.json()["id"]

        yield SimpleNamespace(client=client, headers=headers, adapter=adapter,
                              algorithm=algorithm, wallet=wallet, bnt_wallet=wallet(COIN),
                              gpu=MINER_ADAPTERS.require("srbminer"))


def body(ctx, options=None):
    return {"name": "Seine synthetic roundtrip", "miner_type": ctx.adapter.legacy_miner_type,
            "miner_version": VERSION, "algorithm": ctx.algorithm, "coin": COIN,
            "wallet_id": ctx.bnt_wallet, "pool_url": "stratum+tcp://pool.example.test:3333",
            "extra_config": deepcopy(options or {})}


def create(ctx, request):
    response = ctx.client.post("/api/flight-sheets", headers=ctx.headers, json=request)
    assert response.status_code == 201, response.text
    return response.json()


def get(ctx, sheet_id):
    response = ctx.client.get("/api/flight-sheets", headers=ctx.headers)
    assert response.status_code == 200, response.text
    return next(row for row in response.json() if row["id"] == sheet_id)


def update(ctx, sheet_id, request):
    response = ctx.client.put("/api/flight-sheets/" + sheet_id, headers=ctx.headers, json=request)
    assert response.status_code == 200, response.text
    return get(ctx, sheet_id)


def v2_job(ctx, *, identity="org.zpalmtree.seine", options=None):
    from server.schemas_v2 import adapter_public_version
    return {"adapter_id": identity, "adapter_revision": adapter_public_version(ctx.adapter),
            "miner_version": VERSION, "resource": "cpu", "coin": COIN,
            "wallet_id": ctx.bnt_wallet, "pool_url": "stratum+tcp://pool.example.test:3333",
            "miner_package_id": f"test-seine-{VERSION}-linux-x64", "algorithm": ctx.algorithm,
            "options": deepcopy(options or {})}


@pytest.mark.parametrize("identity", ("seine", "org.zpalmtree.seine"))
def test_v1_and_v2_default_password_empty_and_reject_any_nonempty_value(seine_api, identity):
    ctx = seine_api
    from fastapi import HTTPException
    from pydantic import ValidationError
    from server.main import _normalize_managed_options
    from server.schemas import FlightMinerCreate
    from server.schemas_v2 import FlightJobV2Create
    legacy = {key: value for key, value in body(ctx).items() if key != "name"}
    legacy["miner_type"] = identity
    v2 = v2_job(ctx, identity=identity)
    for payload, model in ((legacy, FlightMinerCreate), (v2, FlightJobV2Create)):
        assert model.model_validate(payload).pool_password == ""
        assert model.model_validate({**payload, "pool_password": ""}).pool_password == ""
        for password in ("x", " ", "arbitrary-nonempty"):
            with pytest.raises(ValidationError, match="does not support pool_password"):
                model.model_validate({**payload, "pool_password": password})
    for password in ("x", " ", "arbitrary-nonempty"):
        with pytest.raises(HTTPException, match="does not support pool_password") as rejected:
            _normalize_managed_options(miner_type=identity, coin=COIN, algorithm=ctx.algorithm,
                                       pool_url=legacy["pool_url"], pool_password=password, extra_config={})
        assert rejected.value.status_code == 422


@pytest.mark.parametrize("flag", (
    "--mode", "--backend", "--address", "--pool-url", "--pool-worker",
    "--threads", "--cpu-threads", "--cpu-profile", "--ui", "--api-server",
    "--api-bind", "--api-allow-unsafe-bind", "--service", "--data-dir",
    "--daemon-dir", "--daemon-url", "--daemon-token", "--wallet-password",
    "--bench", "--bench-kind", "--bench-secs", "--bench-rounds",
    "--self-update", "--auto-update",
))
def test_seine_extra_args_cannot_override_managed_fields_in_v1_v2_or_api(seine_api, flag):
    ctx = seine_api
    from pydantic import ValidationError
    from server.miner_adapters.validation import EXTRA_ARGS_RESERVED_FLAGS
    from server.schemas import FlightMinerCreate
    from server.schemas_v2 import FlightJobV2Create
    assert flag in EXTRA_ARGS_RESERVED_FLAGS["seine"]
    row = create(ctx, body(ctx))
    for arguments in (flag + "=override", flag.upper() + " override"):
        request = body(ctx, {"extra_args": arguments})
        with pytest.raises(ValidationError, match="conflicts with managed parameter"):
            FlightMinerCreate.model_validate({key: value for key, value in request.items() if key != "name"})
        with pytest.raises(ValidationError, match="conflicts with managed parameter"):
            FlightJobV2Create.model_validate(v2_job(ctx, options={"extra_args": arguments}))
        for method, url in ((ctx.client.post, "/api/flight-sheets"),
                            (ctx.client.put, "/api/flight-sheets/" + row["id"])):
            rejected = method(url, headers=ctx.headers, json=request)
            assert rejected.status_code == 422 and "conflicts with managed parameter" in rejected.text
    assert get(ctx, row["id"])["extra_config"]["extra_args"] == ""


def test_v1_and_v2_preserve_unknown_benign_literal_arguments(seine_api):
    ctx = seine_api
    from server.schemas import FlightMinerCreate
    from server.schemas_v2 import FlightJobV2Create
    arguments = '--future-software-flag "two words"\n--future-number=7 --future-label "$(literal)"'
    legacy = body(ctx, {"extra_args": arguments})
    assert FlightMinerCreate.model_validate({key: value for key, value in legacy.items() if key != "name"}).extra_config["extra_args"] == arguments
    assert FlightJobV2Create.model_validate(v2_job(ctx, options={"extra_args": arguments})).options["extra_args"] == arguments
    row = create(ctx, legacy)
    assert update(ctx, row["id"], legacy)["extra_config"]["extra_args"] == arguments


def test_threads_arguments_default_password_and_native_auto_roundtrip(seine_api):
    ctx = seine_api
    arguments = '--future-software-flag "two words"\n--future-number=7'
    request = body(ctx, {"cpu_threads": 4, "extra_args": arguments})
    created = create(ctx, request)
    row = get(ctx, created["id"])
    assert row["pool_password"] == ""
    assert row["extra_config"]["cpu_profile"] == "balanced"
    assert row["extra_config"]["cpu_threads"] == 4
    assert row["extra_config"]["extra_args"] == arguments
    request["extra_config"]["cpu_threads"] = 1025  # No inherited SRBMiner1024 cap; no launch.
    row = update(ctx, row["id"], request)
    assert row["extra_config"]["cpu_threads"] == 1025
    request["pool_password"] = ""
    request["extra_config"] = {"extra_args": ""}
    row = update(ctx, row["id"], request)
    assert "cpu_threads" not in row["extra_config"]
    assert row["extra_config"]["extra_args"] == "" and row["pool_password"] == ""
    for unsupported in ("huge_pages", "randomx_1gb_pages", "cpu_max_threads_hint"):
        if unsupported not in ctx.adapter.option_keys:
            assert unsupported not in row["extra_config"]


@pytest.mark.parametrize("profile", PROFILES)
def test_confirmed_profile_roundtrips(seine_api, profile):
    ctx = seine_api
    spec = next(s for s in ctx.adapter.options if s.name == "cpu_profile")
    assert profile in spec.choices
    request = body(ctx, {"cpu_threads": 2, "cpu_profile": profile, "extra_args": "--future-toggle"})
    row = create(ctx, request)
    row = update(ctx, row["id"], {**request, "name": "Seine profile edited"})
    assert row["extra_config"]["cpu_profile"] == profile
    assert row["extra_config"]["cpu_threads"] == 2
    assert row["extra_config"]["extra_args"] == "--future-toggle"


def test_nonempty_password_rejected_on_create_and_update(seine_api):
    ctx = seine_api
    request = body(ctx)
    row = create(ctx, request)
    for password in ("x", " ", "synthetic-not-supported"):
        changed = {**request, "pool_password": password}
        for method, url in ((ctx.client.post, "/api/flight-sheets"),
                            (ctx.client.put, "/api/flight-sheets/" + row["id"])):
            rejected = method(url, headers=ctx.headers, json=changed)
            assert rejected.status_code == 422 and "pool_password" in rejected.text
        assert get(ctx, row["id"])["pool_password"] == ""


def test_unsupported_controls_and_invalid_thread_profile_values_are_rejected(seine_api):
    ctx = seine_api
    invalid = [{"device_indices": [0]},
               {"gpu_oc": {"vendor": "nvidia", "settings": [{"model_match": "RTX 5080", "power_limit_w": 120}]}},
               {"pool_url": "stratum+ssl://pool.example.test:443"},
               {"extra_config": {"cpu_profile": "unsupported-synthetic-profile"}}]
    invalid.extend({"extra_config": {"cpu_threads": value}} for value in (0, -1, 1.5, True))
    for name in ("huge_pages", "randomx_1gb_pages", "cpu_max_threads_hint", "tls"):
        if name not in ctx.adapter.option_keys:
            invalid.append({"extra_config": {name: True}})
    for change in invalid:
        response = ctx.client.post("/api/flight-sheets", headers=ctx.headers, json={**body(ctx), **change})
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("cpu_role", ("primary", "secondary"))
def test_independent_cpu_and_existing_gpu_leg_remain_isolated(seine_api, cpu_role):
    ctx = seine_api
    cpu = body(ctx, {"cpu_threads": 4, "extra_args": '--future-cpu "CPU words"'})
    gpu = {"miner_type": ctx.gpu.legacy_miner_type, "miner_version": ctx.gpu.release.fallback_versions[0],
           "algorithm": ctx.gpu.normalize_algorithm(None, coin="PEARL"), "coin": "PEARL",
           "wallet_id": ctx.wallet("PEARL"), "pool_url": "stratum+tcp://gpu.example.test:3333",
           "pool_password": " ", "extra_config": {"extra_args": '--future-gpu "GPU words"'}}
    primary, secondary = (cpu, gpu) if cpu_role == "primary" else (gpu, cpu)
    secondary = {key: value for key, value in secondary.items() if key != "name"}
    request = {**primary, "name": "Seine independent isolation", "secondary_mode": "independent", "secondary": secondary}
    row = get(ctx, create(ctx, request)["id"])
    gpu_role = "secondary" if cpu_role == "primary" else "primary"
    leg = lambda value, role: value if role == "primary" else value["secondary"]
    gpu_keys = ("miner_type", "miner_version", "coin", "wallet_id", "pool_url", "pool_password", "extra_config")
    saved_gpu = {key: deepcopy(leg(row, gpu_role)[key]) for key in gpu_keys}
    assert leg(row, cpu_role)["pool_password"] == ""
    leg(request, cpu_role)["extra_config"] = {"extra_args": ""}
    row = update(ctx, row["id"], request)
    assert "cpu_threads" not in leg(row, cpu_role)["extra_config"]
    assert leg(row, cpu_role)["extra_config"]["extra_args"] == ""
    assert {key: leg(row, gpu_role)[key] for key in gpu_keys} == saved_gpu
