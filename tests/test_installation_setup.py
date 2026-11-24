"""Persistent installation guidance in the existing isolated Controller fixture.

All users, secrets and business data are synthetic; no device command is sent.
"""
from __future__ import annotations

import sqlite3
import pytest
from fastapi.testclient import TestClient

from test_server_management import client, enroll, login, password_change_body  # noqa: F401
from controller_test_support import auth, controller_settings_kwargs
from server.config import Settings
from server.main import create_app


def setup_state(client, token):
    response = client.get("/api/setup", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def setup_action(client, token, state, operation, **changes):
    return client.patch("/api/setup", headers=auth(token),
                        json={"expected_revision": state["revision"], "action": operation, **changes})


def business_snapshot(client):
    with client.app.state.db.connection() as db:
        return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("agents", "wallets", "pools", "flight_sheets", "commands")}


def test_fresh_bootstrap_has_pending_guidance_and_get_does_not_mutate_it(client):
    token = login(client)
    before = business_snapshot(client)
    state = setup_state(client, token)
    assert {key: state[key] for key in ("status", "step", "password_changed", "revision")} == {
        "status": "pending", "step": "password", "password_changed": False, "revision": 1,
    }
    assert isinstance(state["updated_at"], str) and state["updated_at"]
    assert setup_state(client, token) == state
    assert business_snapshot(client) == before
    for secret in password_change_body().values():
        assert secret not in str(state)


def test_setup_password_fact_survives_reauthentication_and_late_tab_cannot_overwrite_it(client):
    token = login(client)
    stale_tab = setup_state(client, token)
    before = business_snapshot(client)
    changed = client.post("/api/auth/password", headers=auth(token), json=password_change_body())
    assert changed.status_code == 200, changed.text
    assert client.get("/api/setup", headers=auth(token)).status_code == 401
    fresh = login(client, "admin", password_change_body()["new_password"])
    state = setup_state(client, fresh)
    assert state["status"] == "pending" and state["step"] == "device" and state["password_changed"] is True
    assert state["revision"] == stale_tab["revision"] + 1
    late = setup_action(client, fresh, stale_tab, "step", step="password")
    assert late.status_code == 409, late.text
    assert setup_state(client, fresh) == state
    moved = setup_action(client, fresh, state, "step", step="configuration")
    assert moved.status_code == 200, moved.text
    assert moved.json()["step"] == "configuration" and moved.json()["password_changed"] is True
    assert setup_state(client, fresh) == moved.json()
    completed = setup_action(client, fresh, moved.json(), "complete")
    assert completed.status_code == 200 and completed.json()["status"] == "complete"
    assert completed.json()["step"] == "done"
    reopened = setup_action(client, fresh, completed.json(), "reopen")
    assert reopened.status_code == 200 and reopened.json()["step"] == "device"
    assert reopened.json()["password_changed"] is True
    assert business_snapshot(client) == before


def test_later_skip_complete_and_manual_reopen_persist_without_creating_resources(client):
    token = login(client)
    enroll(client, token)
    wallet = client.post("/api/wallets", headers=auth(token),
                         json={"name": "Existing synthetic wallet", "coin": "XMR", "address": "synthetic-existing-wallet"})
    assert wallet.status_code == 201, wallet.text
    pool = client.post("/api/pools", headers=auth(token),
                       json={"name": "Existing synthetic pool", "coin": "XMR", "pool_url": "stratum+tcp://pool.example.test:3333"})
    assert pool.status_code == 201, pool.text
    flight = client.post("/api/flight-sheets", headers=auth(token),
                         json={"name": "Existing synthetic flight", "miner_type": "generic", "coin": "XMR",
                               "wallet_id": wallet.json()["id"], "pool_url": "stratum+tcp://pool.example.test:3333",
                               "algorithm": "randomx"})
    assert flight.status_code == 201, flight.text
    before = business_snapshot(client)
    state = setup_state(client, token)
    skipped = setup_action(client, token, state, "skip")
    assert skipped.status_code == 200 and skipped.json()["step"] == "device"
    assert skipped.json()["password_changed"] is False
    deferred = setup_action(client, token, skipped.json(), "later")
    assert deferred.status_code == 200 and deferred.json()["status"] == "deferred"
    assert deferred.json()["step"] == "device" and deferred.json()["password_changed"] is False
    fresh_login = login(client)
    assert setup_state(client, fresh_login) == deferred.json()
    reopened = setup_action(client, fresh_login, deferred.json(), "reopen")
    assert reopened.status_code == 200 and reopened.json()["status"] == "pending" and reopened.json()["step"] == "device"
    configuration = setup_action(client, fresh_login, reopened.json(), "skip")
    assert configuration.status_code == 200 and configuration.json()["step"] == "configuration"
    completed = setup_action(client, fresh_login, configuration.json(), "skip")
    assert completed.status_code == 200 and completed.json()["status"] == "complete" and completed.json()["step"] == "done"
    assert completed.json()["password_changed"] is False
    assert setup_state(client, login(client)) == completed.json()
    manual = setup_action(client, fresh_login, completed.json(), "reopen")
    assert manual.status_code == 200 and manual.json()["status"] == "pending" and manual.json()["step"] == "password"
    assert manual.json()["password_changed"] is False
    assert business_snapshot(client) == before


@pytest.mark.parametrize("status", ["deferred", "complete"])
def test_password_success_preserves_an_explicitly_deferred_or_completed_setup_choice(client, status):
    token = login(client)
    state = setup_state(client, token)
    chosen = setup_action(client, token, state, "later" if status == "deferred" else "complete")
    assert chosen.status_code == 200, chosen.text
    changed = client.post("/api/auth/password", headers=auth(token), json=password_change_body())
    assert changed.status_code == 200, changed.text
    fresh = login(client, "admin", password_change_body()["new_password"])
    state = setup_state(client, fresh)
    assert state["status"] == status and state["password_changed"] is True
    assert state["step"] == ("device" if status == "deferred" else "done")
    assert state["revision"] == chosen.json()["revision"] + 1


def test_existing_user_without_setup_key_is_handled_and_password_change_does_not_create_pending_state(client):
    token = login(client)
    with client.app.state.db.connection() as db:
        db.execute("DELETE FROM controller_preferences WHERE key='installation_setup'")
    legacy = setup_state(client, token)
    assert legacy["status"] == "complete" and legacy["step"] == "done"
    assert legacy["password_changed"] is False and legacy["revision"] == 0
    assert setup_state(client, token) == legacy
    changed = client.post("/api/auth/password", headers=auth(token), json=password_change_body())
    assert changed.status_code == 200, changed.text
    fresh = login(client, "admin", password_change_body()["new_password"])
    assert setup_state(client, fresh) == legacy
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT 1 FROM controller_preferences WHERE key='installation_setup'").fetchone() is None
    manual = setup_action(client, fresh, legacy, "reopen")
    assert manual.status_code == 200 and manual.json()["status"] == "pending" and manual.json()["step"] == "password"
    assert manual.json()["revision"] == 1 and manual.json()["password_changed"] is False


def test_setup_deferred_and_completed_state_survives_controller_restart(tmp_path):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    with TestClient(create_app(settings)) as first:
        token = login(first)
        deferred = setup_action(first, token, setup_state(first, token), "later")
        assert deferred.status_code == 200, deferred.text
    with TestClient(create_app(settings)) as second:
        token = login(second)
        assert setup_state(second, token) == deferred.json()
        completed = setup_action(second, token, deferred.json(), "complete")
        assert completed.status_code == 200, completed.text
    with TestClient(create_app(settings)) as third:
        assert setup_state(third, login(third)) == completed.json()


def test_password_and_setup_fact_roll_back_together_if_progress_cannot_be_saved(client):
    token = login(client)
    before = setup_state(client, token)
    with client.app.state.db.connection() as db:
        user = dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone())
        db.execute("""CREATE TRIGGER synthetic_setup_write_failure BEFORE UPDATE ON controller_preferences
                   WHEN OLD.key='installation_setup'
                   BEGIN SELECT RAISE(ABORT,'synthetic setup write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic setup write failure"):
        client.post("/api/auth/password", headers=auth(token), json=password_change_body())
    with client.app.state.db.connection() as db:
        assert dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone()) == user
        db.execute("DROP TRIGGER synthetic_setup_write_failure")
    assert setup_state(client, token) == before
    assert login(client)
    denied = client.post("/api/auth/login", json={"username": "admin", "password": password_change_body()["new_password"]})
    assert denied.status_code == 401


def test_setup_is_admin_only_for_read_and_progress_updates(client):
    assert client.get("/api/setup").status_code == 401
    assert client.patch("/api/setup", json={"expected_revision": 1, "action": "later"}).status_code == 401
    admin = login(client)
    before = setup_state(client, admin)
    for role in ("viewer", "operator"):
        password = f"synthetic-setup-{role}-password"
        created = client.post("/api/users", headers=auth(admin), json={"username": role, "password": password, "role": role})
        assert created.status_code == 201, created.text
        token = login(client, role, password)
        assert client.get("/api/setup", headers=auth(token)).status_code == 403
        assert setup_action(client, token, before, "later").status_code == 403
    assert setup_state(client, admin) == before


@pytest.mark.parametrize("changes", [
    {"expected_revision": True}, {"expected_revision": -1}, {"expected_revision": 1.0},
    {"action": "unknown"}, {"status": "complete"}, {"password_changed": True},
    {"step": "password"}, {"action": "step"}, {"action": "step", "step": "done"},
    {"secret": "synthetic-forged-progress-secret"},
])
def test_setup_progress_whitelist_rejects_invalid_updates_without_mutation_or_secret_echo(client, changes):
    token = login(client)
    before = setup_state(client, token)
    response = setup_action(client, token, before, "later", **changes)
    assert response.status_code == 422, response.text
    assert "synthetic-forged-progress-secret" not in response.text
    assert setup_state(client, token) == before
    with client.app.state.db.connection() as db:
        raw = db.execute("SELECT value FROM controller_preferences WHERE key='installation_setup'").fetchone()[0]
    assert "synthetic-forged-progress-secret" not in raw
    for secret in password_change_body().values():
        assert secret not in raw
