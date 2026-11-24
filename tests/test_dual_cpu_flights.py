"""Independent CPU flights through the real offline Controller API.

All uploaded bytes, enrolled agents and commands are local synthetic fixtures;
these tests do not launch a miner or contact a device.
"""
from copy import deepcopy
import hashlib
import json

import pytest

import test_seine_flights as seine
from test_seine_flights import seine_api
from test_seine_package_pins import apply, assert_no_apply_queued, enroll, storage


PAIRS = [
    ("xmrig", "seine"), ("seine", "xmrig"),
    ("xmrig", "xmrig"), ("seine", "seine"), ("srbminer", "srbminer"),
]
# Non-catalog test versions preserve the real upload collision rule for these
# miners. Seine explicitly supports an independently uploaded same-version build.
VERSIONS = {"xmrig": ("9.0.0", "9.0.1"), "seine": ("0.2.15", "0.2.16"),
            "srbminer": ("9.0.0", "9.0.1")}


def upload_cpu(ctx, miner_type, version, role):
    raw = (f"synthetic independent {role} {miner_type} {version} archive").encode()
    response = ctx.client.post("/api/miner-packages/upload", headers={
        **ctx.headers, "Content-Type": "application/octet-stream"}, params={
            "name": f"Synthetic {role} {miner_type}", "version": version,
            "archive_type": "tar.gz", "filename": f"{miner_type}-{version}.tar.gz",
            "managed_miner_type": miner_type, "managed_os_family": "linux",
            "managed_channel": "test"}, content=raw)
    assert response.status_code == 201, response.text
    package = response.json()
    assert package["source_type"] == "uploaded" and package["managed_channel"] == "test"
    assert package["sha256"] == hashlib.sha256(raw).hexdigest()
    return package, raw


def cpu_leg(ctx, miner_type, version, package, role):
    from server.miner_adapters import MINER_ADAPTERS
    adapter = MINER_ADAPTERS.require(miner_type)
    assert "independent_instances" in adapter.capabilities
    coin = "BNT" if miner_type == "seine" else "XMR"
    options = {"cpu_threads": 2 if role == "primary" else 3,
               "extra_args": f'--future-{role} "literal {role}"\n--future-value={role}'}
    if miner_type == "srbminer":
        options["resource_kind"] = "cpu"
    return {"miner_type": miner_type, "miner_version": version, "coin": coin,
            "algorithm": adapter.normalize_algorithm(None, coin=coin),
            "wallet_id": ctx.wallet(coin), "pool_url": f"stratum+tcp://{role}.example.test:3333",
            "pool_password": "" if miner_type == "seine" else f"{role}-password",
            "miner_package_id": package["id"], "extra_config": options}


def dual_request(ctx, first, second):
    packages = {}
    legs = {}
    for index, (role, kind) in enumerate((("primary", first), ("secondary", second))):
        # Same-adapter instances deliberately bind different versions and bytes.
        version = VERSIONS[kind][index if first == second else 0]
        packages[role] = upload_cpu(ctx, kind, version, role)
        legs[role] = cpu_leg(ctx, kind, version, packages[role][0], role)
    return {"name": f"Independent CPU {first} plus {second}", **legs["primary"],
            "secondary_mode": "independent", "secondary": legs["secondary"]}, packages


@pytest.mark.parametrize("first,second", PAIRS)
def test_dual_cpu_create_update_apply_keep_each_roles_package_threads_and_literal_args(seine_api, first, second):
    ctx = seine_api
    request, packages = dual_request(ctx, first, second)
    created = seine.create(ctx, request)
    request["name"] += " edited"
    request["extra_config"]["cpu_threads"] = 5
    request["secondary"]["extra_config"]["cpu_threads"] = 7
    row = seine.update(ctx, created["id"], request)
    assert row["miner_package_id"] == packages["primary"][0]["id"]
    assert row["secondary"]["miner_package_id"] == packages["secondary"][0]["id"]
    assert row["extra_config"]["cpu_threads"] == 5
    assert row["secondary"]["extra_config"]["cpu_threads"] == 7
    identity = enroll(ctx, credentials=True)
    response = apply(ctx, row["id"], identity["agent_id"])
    assert response.status_code == 201, response.text
    command = response.json()
    assert command["action"] == "apply_flight_sheet"
    public_payload = command["payload"]
    for role in ("primary", "secondary"):
        expected = request if role == "primary" else request["secondary"]
        assert public_payload[role]["pool_password"] == ("[configured]" if expected["pool_password"] else "")
    delivered = ctx.client.post("/api/agent/heartbeat", headers={
        "X-Agent-ID": identity["agent_id"], "Authorization": "Bearer " + identity["agent_secret"]}, json={
            "hostname": "synthetic-seine-pin", "os_family": "linux", "agent_version": "0.8.39",
            "capabilities": ["managed_mining.dual_process.v1", "managed_mining.extra_args.v1"],
            "metrics": {"gpus": None}, "inventory": {}})
    assert delivered.status_code == 200, delivered.text
    assert [item["id"] for item in delivered.json()["commands"]] == [command["id"]]
    payload = delivered.json()["commands"][0]["payload"]
    assert not any(key.startswith("_controller_") for key in payload)
    assert payload["secondary_mode"] == "independent"
    assert payload.get("gpu_oc") is None
    for role in ("primary", "secondary"):
        expected = request if role == "primary" else request["secondary"]
        actual = payload[role]
        package, raw = packages[role]
        assert actual["miner_type"] == expected["miner_type"]
        assert actual["algorithm"] == expected["algorithm"]
        assert actual["device_indices"] == []
        assert actual["pool_url"] == expected["pool_url"]
        assert actual["pool_password"] == expected["pool_password"]
        assert actual["extra_config"]["cpu_threads"] == expected["extra_config"]["cpu_threads"]
        assert actual["extra_config"]["extra_args"] == expected["extra_config"]["extra_args"]
        assert (actual["miner_package"]["id"], actual["miner_package"]["version"],
                actual["miner_package"]["sha256"]) == (package["id"], package["version"], package["sha256"])
        downloaded = ctx.client.get(actual["miner_package"]["download_url"], headers={
            "X-Agent-ID": identity["agent_id"], "Authorization": "Bearer " + identity["agent_secret"]})
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == raw
    with ctx.client.app.state.db.connection() as db:
        rows = db.execute("SELECT action,payload_json FROM commands").fetchall()
    assert len(rows) == 1 and rows[0]["action"] == "apply_flight_sheet"
    stored = json.loads(rows[0]["payload_json"])
    assert {key: value for key, value in stored.items() if not key.startswith("_controller_")} == payload


def test_dual_cpu_threads_can_be_automatic_on_only_one_leg_without_forced_sharing(seine_api):
    ctx = seine_api
    request, _ = dual_request(ctx, "xmrig", "seine")
    request["extra_config"].pop("cpu_threads")
    request["secondary"]["extra_config"]["cpu_threads"] = 1025  # Seine has no invented 1024 cap.
    created = seine.create(ctx, request)
    response = apply(ctx, created["id"], enroll(ctx))
    assert response.status_code == 201, response.text
    assert response.json()["payload"]["primary"]["extra_config"].get("cpu_threads") is None
    assert response.json()["payload"]["secondary"]["extra_config"]["cpu_threads"] == 1025


@pytest.mark.parametrize("role", ["primary", "secondary"])
def test_dual_cpu_pin_sha_is_still_revalidated_per_role_before_any_command(seine_api, role):
    ctx = seine_api
    request, packages = dual_request(ctx, "seine", "seine")
    created = seine.create(ctx, request)
    raw = packages[role][1]
    storage(ctx, packages[role][0]["id"]).write_bytes(b"!" + raw[1:])
    response = apply(ctx, created["id"], enroll(ctx))
    assert response.status_code == 409, response.text
    assert "SHA256" in response.text
    assert_no_apply_queued(ctx)


@pytest.mark.parametrize("devices", [([], [1]), ([0], [0])])
def test_gpu_overlap_or_implicit_selection_is_still_rejected_without_persisting(seine_api, devices):
    ctx = seine_api
    request, _ = dual_request(ctx, "srbminer", "srbminer")
    for leg, selected in ((request, devices[0]), (request["secondary"], devices[1])):
        leg.update(coin="PEARL", algorithm="pearlhash", wallet_id=ctx.wallet("PEARL"),
                   device_indices=selected, extra_config={"resource_kind": "gpu"})
    before = ctx.client.get("/api/flight-sheets", headers=ctx.headers).json()
    response = ctx.client.post("/api/flight-sheets", headers=ctx.headers, json=request)
    assert response.status_code == 422 and "non-overlapping" in response.text
    assert ctx.client.get("/api/flight-sheets", headers=ctx.headers).json() == before
    assert_no_apply_queued(ctx)


@pytest.mark.parametrize("first,second", PAIRS)
def test_v2_two_cpu_process_groups_preserve_per_job_identity_and_options(seine_api, first, second):
    from server.miner_adapters import MINER_ADAPTERS
    from server.schemas_v2 import FlightSheetV2Create, adapter_public_version
    ctx = seine_api
    request, _ = dual_request(ctx, first, second)
    jobs = []
    for role, leg in (("primary", request), ("secondary", request["secondary"])):
        adapter = MINER_ADAPTERS.require(leg["miner_type"])
        jobs.append({"job_id": "job_" + role, "process_group": "pg_" + role,
                     "adapter_id": adapter.adapter_id, "adapter_revision": adapter_public_version(adapter),
                     "miner_version": leg["miner_version"], "miner_package_id": leg["miner_package_id"],
                     "resource": "cpu", "coin": leg["coin"], "algorithm": leg["algorithm"],
                     "wallet_id": leg["wallet_id"], "pool_url": leg["pool_url"],
                     "pool_password": leg["pool_password"], "options": deepcopy(leg["extra_config"]),
                     "devices": []})
    parsed = FlightSheetV2Create.model_validate({"name": request["name"], "worker_name_template": "dual_cpu_worker", "jobs": jobs})
    assert [job.process_group for job in parsed.jobs] == ["pg_primary", "pg_secondary"]
    for actual, expected in zip(parsed.jobs, jobs):
        assert actual.miner_package_id == expected["miner_package_id"]
        assert actual.options["cpu_threads"] == expected["options"]["cpu_threads"]
        assert actual.options["extra_args"] == expected["options"]["extra_args"]
    assert FlightSheetV2Create.model_validate_json(parsed.model_dump_json()) == parsed


def test_v2_legacy_projection_retains_both_cpu_roles_when_runtime_is_declared(seine_api, monkeypatch):
    from server.flight_v2_storage import ensure_flight_v2_storage, project_legacy_jobs
    ctx = seine_api
    request, _ = dual_request(ctx, "xmrig", "xmrig")
    created = seine.create(ctx, request)
    with ctx.client.app.state.db.connection() as db:
        row = dict(db.execute("SELECT * FROM flight_sheets WHERE id=?", (created["id"],)).fetchone())
    # Current built-in executable runtimes intentionally do not declare V2 ABI;
    # this isolates conversion once that pre-existing prerequisite is satisfied.
    monkeypatch.setattr("server.flight_v2_storage.runtime_sha256_for", lambda _adapter: "a" * 64)
    jobs = project_legacy_jobs(row)
    assert jobs is not None and len(jobs) == 2
    assert [job.resource for job in jobs] == ["cpu", "cpu"]
    assert [job.process_group for job in jobs] == ["pg_primary", "pg_secondary"]
    assert len({job.job_id for job in jobs}) == 2
    assert jobs == project_legacy_jobs(row)
    for job, leg in zip(jobs, (request, request["secondary"])):
        assert job.miner_package_id == leg["miner_package_id"]
        assert job.miner_version == leg["miner_version"]
        assert json.loads(job.options_json)["cpu_threads"] == leg["extra_config"]["cpu_threads"]
        assert json.loads(job.options_json)["extra_args"] == leg["extra_config"]["extra_args"]
    with ctx.client.app.state.db.connection() as db:
        ensure_flight_v2_storage(db)
        stored = db.execute("SELECT * FROM flight_sheet_jobs WHERE flight_sheet_id=? ORDER BY position",
                            (created["id"],)).fetchall()
        assert [tuple(item) for item in stored] == [job.sql_values(created["id"]) for job in jobs]
        migrated = dict(db.execute("SELECT * FROM flight_sheets WHERE id=?", (created["id"],)).fetchone())
        assert migrated == {**row, "schema_version": 2}
        ensure_flight_v2_storage(db)
        repeated = db.execute("SELECT * FROM flight_sheet_jobs WHERE flight_sheet_id=? ORDER BY position",
                              (created["id"],)).fetchall()
        assert [tuple(item) for item in repeated] == [tuple(item) for item in stored]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
