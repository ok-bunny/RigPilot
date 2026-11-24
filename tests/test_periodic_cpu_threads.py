"""Periodic CPU thread expression: true API outcomes and unchanged controls."""
import json
import pytest
from test_059_backend import app_client, auth
from test_srb_catalog_quantus import setup
from test_independent_dual_mining import enroll_linux_agent
from server.miner_adapters.srb_catalog import validate_periodic_cpu_threads


def periodic_body(wallet, primary=None, secondary=None, version="3.7.1"):
    return {"name": "Periodic CPU thread expression", "miner_type": "srbminer", "miner_version": version,
        "coin": "EPIC", "wallet_id": wallet("EPIC"), "algorithm": "randomepic", "pool_url": "stratum+tcp://epic.test:3333",
        "extra_config": {"resource_kind": "cpu", **(primary or {})},
        "secondary": {"miner_type": "srbminer", "miner_version": version, "coin": "XMR", "wallet_id": wallet("XMR"),
            "algorithm": "randomx", "pool_url": "stratum+tcp://xmr.test:3333", "extra_config": {"resource_kind": "cpu", **(secondary or {})}}}


@pytest.mark.parametrize("primary,secondary", [({"cpu_threads": 2}, {}), ({}, {"cpu_threads": 8}), ({"cpu_threads": 2}, {"cpu_threads": None}), ({"cpu_threads": None}, {"cpu_threads": 8})])
def test_shared_missing_and_null_are_automatic_and_mixed_expression_is_refused(primary, secondary):
    with pytest.raises(ValueError, match="current launcher"):
        validate_periodic_cpu_threads(primary, secondary)


@pytest.mark.parametrize("primary,secondary", [({}, {}), ({"cpu_threads": None}, {}), ({}, {"cpu_threads": None}), ({"cpu_threads": 2}, {"cpu_threads": 8}), ({"cpu_threads_reduce": 2}, {"cpu_threads_reduce": 8, "cpu_threads_intensity": 4})])
def test_shared_accepted_thread_selections_and_other_options_are_not_mutated(primary, secondary):
    before = json.dumps([primary, secondary], sort_keys=True)
    assert validate_periodic_cpu_threads(primary, secondary) is None
    assert json.dumps([primary, secondary], sort_keys=True) == before


@pytest.mark.parametrize("primary,secondary", [({"cpu_threads": 2}, {}), ({}, {"cpu_threads": 8})])
def test_API_rejects_each_mixed_direction_before_create_or_update(setup, primary, secondary):
    client, admin, wallet = setup
    body = periodic_body(wallet, primary, secondary)
    response = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert response.status_code == 422, response.text
    assert "current launcher" in response.text and "automatic on both" in response.text
    assert client.get("/api/flight-sheets", headers=auth(admin)).json() == []
    body["extra_config"] = {"resource_kind": "cpu", "cpu_threads": 2}
    body["secondary"]["extra_config"] = {"resource_kind": "cpu", "cpu_threads": 8}
    valid = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert valid.status_code == 201, valid.text
    body["extra_config"] = {"resource_kind": "cpu", **primary}
    body["secondary"]["extra_config"] = {"resource_kind": "cpu", **secondary}
    update = client.put("/api/flight-sheets/" + valid.json()["id"], headers=auth(admin), json=body)
    assert update.status_code == 422 and "current launcher" in update.text
    row = client.get("/api/flight-sheets", headers=auth(admin)).json()[0]
    assert row["extra_config"]["cpu_threads"] == 2
    assert row["secondary"]["extra_config"]["cpu_threads"] == 8


@pytest.mark.parametrize("version", ["3.4.6", "3.7.1"])
@pytest.mark.parametrize("primary,secondary", [({}, {}), ({"cpu_threads": 2}, {"cpu_threads": 8}), ({"cpu_threads": None}, {"cpu_threads": None}), ({"cpu_threads_reduce": 2}, {"cpu_threads_reduce": 8, "cpu_threads_intensity": 4})])
def test_API_preserves_automatic_explicit_and_reduce_intensity_without_version_gate(setup, version, primary, secondary):
    client, admin, wallet = setup
    body = periodic_body(wallet, primary, secondary, version)
    response = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert response.status_code == 201, response.text
    row = response.json()
    for sent, received in [(primary, row["extra_config"]), (secondary, row["secondary"]["extra_config"])]:
        for key, value in sent.items():
            if value is None:
                assert key not in received
            else:
                assert received[key] == value
        if "cpu_threads" not in sent:
            assert "cpu_threads" not in received


def test_periodic_srb_extra_arguments_remain_per_leg_through_save_edit_and_clear(setup):
    client, admin, wallet = setup
    body = periodic_body(wallet, {"cpu_threads": 2}, {"cpu_threads": 8})
    sheet_id = None
    for primary, secondary in [
        ('--futureflag 3 --label "primary CPU"', "--future-toggle --label 'secondary CPU'"),
        ("--futureflag=4", "--futureflag=5"),
        ("", ""),
    ]:
        body["extra_config"]["extra_args"] = primary
        body["secondary"]["extra_config"]["extra_args"] = secondary
        response = (
            client.post("/api/flight-sheets", headers=auth(admin), json=body)
            if sheet_id is None else
            client.put(f"/api/flight-sheets/{sheet_id}", headers=auth(admin), json=body)
        )
        assert response.status_code == (201 if sheet_id is None else 200), response.text
        sheet_id = response.json()["id"]
        row = next(item for item in client.get("/api/flight-sheets", headers=auth(admin)).json()
                   if item["id"] == sheet_id)
        assert row["extra_config"]["extra_args"] == primary
        assert row["secondary"]["extra_config"]["extra_args"] == secondary
        assert row["extra_config"]["cpu_threads"] == 2
        assert row["secondary"]["extra_config"]["cpu_threads"] == 8
    noop = client.put(f"/api/flight-sheets/{sheet_id}", headers=auth(admin), json=body)
    assert noop.status_code == 200, noop.text
    assert noop.json()["changed_fields"] == []


def pearl_srb_body(wallet):
    return {
        "name": "Synthetic SRB Pearl version boundary", "miner_type": "srbminer",
        "miner_version": "3.4.6", "coin": "PEARL", "wallet_id": wallet("PEARL"),
        "algorithm": "pearlhash", "pool_url": "stratum+tcp://pearl.example.test:3333",
        "extra_config": {"resource_kind": "gpu", "extra_args": ""},
    }


def test_saved_pearl_k1_on_old_srb_is_rejected_before_package_download(setup, monkeypatch):
    client, admin, wallet = setup
    body = pearl_srb_body(wallet)
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    body["extra_config"]["extra_args"] = "--pearl-k1"
    with client.app.state.db.connection() as db:
        db.execute("UPDATE flight_sheets SET extra_config_json=? WHERE id=?",
                   (json.dumps(body["extra_config"]), created.json()["id"]))
    agent, _ = enroll_linux_agent(client, admin, version="0.8.31")
    monkeypatch.setattr("server.main._download_https_package", lambda *args, **kwargs:
                        pytest.fail("--pearl-k1 on SRB 3.4.6 must be rejected before download"))
    applied = client.post(f"/api/agents/{agent}/flight-sheets/{created.json()['id']}/apply", headers=auth(admin))
    assert applied.status_code == 409, applied.text
    assert "--pearl-k1" in applied.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0


def test_unresolved_srb_version_rechecks_actual_prepared_package_before_queue(setup, monkeypatch):
    client, admin, wallet = setup
    body = pearl_srb_body(wallet)
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    body["extra_config"]["extra_args"] = "--pearl-k1"
    with client.app.state.db.connection() as db:
        db.execute("UPDATE flight_sheets SET miner_version=NULL,extra_config_json=? WHERE id=?",
                   (json.dumps(body["extra_config"]), created.json()["id"]))
    agent, _ = enroll_linux_agent(client, admin, version="0.8.31")
    # Model a historical unresolved selection that prepares the older real
    # package. Keep the ordinary package path/download, changing only selection.
    endpoint = next(route.endpoint for route in client.app.routes
                    if getattr(route, "path", None) == "/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply")
    prepare_cell = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__))["ensure_managed_miner_package"]
    real_prepare = prepare_cell.cell_contents
    prepared_versions = []

    def prepare_old_version(miner_type, os_family, version, user, **kwargs):
        assert version is None
        package = real_prepare(miner_type, os_family, "3.4.6", user, **kwargs)
        prepared_versions.append(package["version"])
        return package

    monkeypatch.setattr(prepare_cell, "cell_contents", prepare_old_version)
    applied = client.post(f"/api/agents/{agent}/flight-sheets/{created.json()['id']}/apply", headers=auth(admin))
    assert prepared_versions == ["3.4.6"]
    assert applied.status_code == 409, applied.text
    assert "--pearl-k1" in applied.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
        stored = db.execute("SELECT miner_version,extra_config_json FROM flight_sheets WHERE id=?",
                            (created.json()["id"],)).fetchone()
    assert stored["miner_version"] is None
    assert json.loads(stored["extra_config_json"])["extra_args"] == "--pearl-k1"


def test_old_srb_without_pearl_k1_remains_usable(setup):
    client, admin, wallet = setup
    body = pearl_srb_body(wallet)
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    agent, _ = enroll_linux_agent(client, admin, version="0.8.30", extra_args_capability=False)
    applied = client.post(f"/api/agents/{agent}/flight-sheets/{created.json()['id']}/apply", headers=auth(admin))
    assert applied.status_code == 201, applied.text
    assert applied.json()["payload"]["primary"]["miner_package"]["version"] == "3.4.6"
    assert "extra_args" not in applied.json()["payload"]["primary"]["extra_config"]


@pytest.mark.parametrize("shape", ["single_CPU", "CPU_GPU", "GPU_CPU"])
def test_API_one_CPU_leg_explicit_threads_remain_accepted(setup, shape):
    client, admin, wallet = setup
    body = periodic_body(wallet, {"cpu_threads": 2}, {})
    if shape == "single_CPU":
        body.pop("secondary")
    elif shape == "CPU_GPU":
        body["secondary"].update(coin="PEARL", wallet_id=wallet("PEARL"), algorithm="pearlhash", device_indices=[0], extra_config={"resource_kind": "gpu"})
    else:
        body.update(algorithm="progpow_epic", device_indices=[0], extra_config={"resource_kind": "gpu"})
        body["secondary"]["extra_config"]["cpu_threads"] = 8
    response = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert response.status_code == 201, response.text
    if shape == "GPU_CPU":
        assert response.json()["secondary"]["extra_config"]["cpu_threads"] == 8
    else:
        assert response.json()["extra_config"]["cpu_threads"] == 2


@pytest.mark.parametrize("fixed_role", ["primary", "secondary"])
def test_apply_refuses_saved_mixed_periodic_threads_before_package_download(setup, monkeypatch, fixed_role):
    client, admin, wallet = setup
    body = periodic_body(wallet)
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    row = created.json()
    primary = row["extra_config"]
    secondary = row["secondary"]["extra_config"]
    (primary if fixed_role == "primary" else secondary)["cpu_threads"] = 8
    with client.app.state.db.connection() as db:
        db.execute("UPDATE flight_sheets SET extra_config_json=?, secondary_extra_config_json=? WHERE id=?", (json.dumps(primary), json.dumps(secondary), row["id"]))
    def no_download(*args, **kwargs):
        raise AssertionError("Invalid stored thread expression must be rejected before package download")
    monkeypatch.setattr("server.main._download_https_package", no_download)
    agent, _ = enroll_linux_agent(client, admin, version="0.8.24")
    apply = client.post(f"/api/agents/{agent}/flight-sheets/{row['id']}/apply", headers=auth(admin))
    assert apply.status_code == 409 and "current launcher" in apply.text
    assert any(x["id"] == row["id"] for x in client.get("/api/flight-sheets", headers=auth(admin)).json())
