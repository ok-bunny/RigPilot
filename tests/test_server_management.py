from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server.main as server_main
import server.auth as auth_module
from controller_test_support import auth, login as _login, controller_settings_kwargs

from server.config import Settings
from server.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
        max_miner_package_bytes=1024,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient, username: str = "admin", password: str = "correct-horse-battery-staple") -> str:
    return _login(client, username, password)


def enroll(client: TestClient, token: str) -> tuple[str, str]:
    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "package-test", "ttl_minutes": 30},
    ).json()
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued["token"],
            "name": "managed-rig",
            "hostname": "managed-rig.local",
            "os_family": "linux",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"], response.json()["agent_secret"]


def test_device_note_is_editable_and_serialized(client: TestClient):
    token = login(client)
    agent_id, _ = enroll(client, token)
    updated = client.patch(
        f"/api/agents/{agent_id}",
        headers=auth(token),
        json={"note": "  机架 A / 顶层  "},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["note"] == "机架 A / 顶层"
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["note"] == "机架 A / 顶层"


def test_only_admin_can_delete_users_and_self_is_protected(client: TestClient):
    admin_token = login(client)
    for username, role in (("operator1", "operator"), ("viewer1", "viewer")):
        created = client.post(
            "/api/users",
            headers=auth(admin_token),
            json={
                "username": username,
                "password": f"long-enough-password-for-{username}",
                "role": role,
            },
        )
        assert created.status_code == 201, created.text
    users = client.get("/api/users", headers=auth(admin_token)).json()
    ids = {user["username"]: user["id"] for user in users}
    operator_token = login(
        client, "operator1", "long-enough-password-for-operator1"
    )

    forbidden = client.delete(
        f"/api/users/{ids['viewer1']}", headers=auth(operator_token)
    )
    assert forbidden.status_code == 403
    deleted = client.delete(
        f"/api/users/{ids['viewer1']}", headers=auth(admin_token)
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "deleted"}
    assert (
        client.post(
            "/api/auth/login",
            json={
                "username": "viewer1",
                "password": "long-enough-password-for-viewer1",
            },
        ).status_code
        == 401
    )
    assert client.delete(f"/api/users/{ids['admin']}", headers=auth(admin_token)).status_code == 409


def test_user_deletion_reassigns_history_on_a_retired_table_database(client):
    """0.8.50 retired the HiveOS import/NVIDIA driver job ledgers; user
    deletion must still reassign every surviving referenced history."""
    token = login(client)
    assert client.post("/api/users", headers=auth(token), json={
        "username": "history-owner", "password": "synthetic-owner-password", "role": "admin",
    }).status_code == 201
    with client.app.state.db.connection() as db:
        tables = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not {
            "hiveos_package_update_imports", "hiveos_flight_imports",
            "remote_sessions", "gpu_profiles", "nvidia_driver_cache_jobs",
        } & tables
        admin_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        owner_id = db.execute("SELECT id FROM users WHERE username='history-owner'").fetchone()[0]
        db.execute("INSERT INTO wallets (id,name,coin,address,note,created_by,created_at,updated_at)"
                   " VALUES('owned-wallet','w','XMR','synthetic-public-address','',?,'now','now')",
                   (owner_id,))
    response = client.delete(f"/api/users/{owner_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT created_by FROM wallets WHERE id='owned-wallet'").fetchone()[0] == admin_id
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_uploaded_package_digest_is_metadata_and_download_requires_agent_auth(
    client: TestClient,
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    payload = b"PK\x03\x04rigpilot-package"
    response = client.post(
        "/api/miner-packages/upload",
        params={
            "name": "safe-miner",
            "version": "1.2.3",
            "archive_type": "zip",
            "filename": "../safe miner.zip",
            "note": "controller hosted",
        },
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        content=payload,
    )
    assert response.status_code == 201, response.text
    package = response.json()
    assert package["sha256"] == hashlib.sha256(payload).hexdigest()
    assert package["source_type"] == "uploaded"
    assert package["original_filename"] == "safe_miner.zip"
    assert package["file_size"] == len(payload)
    assert package["download_url"].startswith("/api/agent/miner-packages/")

    assert client.get(package["download_url"]).status_code == 401
    downloaded = client.get(
        package["download_url"],
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == payload
    assert "x-rigpilot-sha256" not in downloaded.headers


def test_upload_size_limit_is_enforced(client: TestClient):
    token = login(client)
    response = client.post(
        "/api/miner-packages/upload",
        params={
            "name": "too-large",
            "version": "1.0",
            "archive_type": "zip",
            "filename": "too-large.zip",
        },
        headers={**auth(token), "Content-Type": "application/octet-stream"},
        content=b"x" * 1025,
    )
    assert response.status_code == 413


def test_upload_requires_octet_stream(client: TestClient):
    token = login(client)
    response = client.post(
        "/api/miner-packages/upload",
        params={
            "name": "wrong-content-type",
            "version": "1.0",
            "archive_type": "zip",
            "filename": "package.zip",
        },
        headers={**auth(token), "Content-Type": "text/plain"},
        content=b"not accepted",
    )
    assert response.status_code == 415


def test_https_import_is_stored_and_served_without_user_sha(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    token = login(client)
    agent_id, agent_secret = enroll(client, token)
    payload = b"\x1f\x8bhosted-tarball"

    def fake_download(url: str, destination: Path, **kwargs):
        assert url == "https://downloads.example.test/miner-2.0.tar.gz"
        staging_name = destination.name.split(".")
        assert staging_name[0] == ""
        assert staging_name[-1] == "importing"
        assert len(staging_name) == 4 and len(staging_name[2]) == 32
        assert kwargs["max_bytes"] == 1024
        destination.write_bytes(payload)
        return len(payload), hashlib.sha256(payload).hexdigest()

    monkeypatch.setattr(server_main, "_download_https_package", fake_download)
    response = client.post(
        "/api/miner-packages/import",
        headers=auth(token),
        json={
            "name": "remote-miner",
            "version": "2.0",
            "download_url": "https://downloads.example.test/miner-2.0.tar.gz",
            "archive_type": "tar.gz",
            "note": "imported once",
        },
    )
    assert response.status_code == 201, response.text
    package = response.json()
    assert package["sha256"] == hashlib.sha256(payload).hexdigest()
    assert package["download_url"].startswith("/api/agent/miner-packages/")
    downloaded = client.get(
        package["download_url"],
        headers={
            "X-Agent-ID": agent_id,
            "Authorization": f"Bearer {agent_secret}",
        },
    )
    assert downloaded.content == payload


def test_import_dns_rejects_private_and_special_addresses(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        server_main.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (server_main.socket.AF_INET, server_main.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(HTTPException) as error:
        server_main._public_addresses("example.test", 443)
    assert error.value.status_code == 422


def password_change_body(**changes):
    return {"current_password": "correct-horse-battery-staple",
            "new_password": "synthetic-updated-admin-password",
            "confirm_password": "synthetic-updated-admin-password", **changes}


def test_password_change_requires_authenticated_admin_and_cannot_choose_another_user(client):
    before = None
    with client.app.state.db.connection() as db:
        before = [dict(row) for row in db.execute("SELECT * FROM users ORDER BY id")]
    denied = client.post("/api/auth/password", json=password_change_body())
    assert denied.status_code == 401, denied.text
    with client.app.state.db.connection() as db:
        assert [dict(row) for row in db.execute("SELECT * FROM users ORDER BY id")] == before
    admin = login(client)
    for role in ("viewer", "operator"):
        password = f"synthetic-{role}-password"
        created = client.post("/api/users", headers=auth(admin),
                              json={"username": role, "password": password, "role": role})
        assert created.status_code == 201, created.text
        token = login(client, role, password)
        denied = client.post("/api/auth/password", headers=auth(token),
                             json=password_change_body(current_password=password))
        assert denied.status_code == 403, denied.text
    rejected = client.post("/api/auth/password", headers=auth(admin),
                           json=password_change_body(user_id=2))
    assert rejected.status_code == 422, rejected.text
    assert client.get("/api/auth/me", headers=auth(admin)).status_code == 200


@pytest.mark.parametrize("changes", [
    {"current_password": ""}, {"current_password": "x" * 257},
    {"new_password": "too-short", "confirm_password": "too-short"},
    {"new_password": "x" * 257, "confirm_password": "x" * 257},
    {"confirm_password": "a-different-confirmation"},
    {"new_password": True}, {"confirm_password": None},
])
def test_password_change_rejects_invalid_shape_without_changing_credentials(client, changes):
    token = login(client)
    with client.app.state.db.connection() as db:
        before = dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone())
    rejected = client.post("/api/auth/password", headers=auth(token), json=password_change_body(**changes))
    assert rejected.status_code == 422, rejected.text
    for value in password_change_body(**changes).values():
        if isinstance(value, str) and value:
            assert value not in rejected.text
    with client.app.state.db.connection() as db:
        assert dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone()) == before
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200


def test_wrong_current_password_changes_neither_hash_nor_session_and_has_separate_rate_limit(client):
    token = login(client)
    with client.app.state.db.connection() as db:
        before = dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone())
    for _ in range(server_main.LoginRateLimiter().attempts):
        rejected = client.post("/api/auth/password", headers=auth(token),
                               json=password_change_body(current_password="synthetic-wrong-current-password"))
        assert rejected.status_code == 400, rejected.text
    limited = client.post("/api/auth/password", headers=auth(token),
                          json=password_change_body(current_password="synthetic-wrong-current-password"))
    assert limited.status_code == 429, limited.text
    with client.app.state.db.connection() as db:
        assert dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone()) == before
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 200
    assert login(client), "password-change guesses must not consume the login limiter"


def test_password_change_reauthenticates_all_old_own_sessions_and_preserves_other_users(client):
    first = login(client)
    second = login(client)
    with client.app.state.db.connection() as db:
        before = dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone())
    legacy = auth_module.create_session_token(
        {"uid": before["id"], "sub": "admin", "role": "admin"},
        client.app.state.settings.secret_key, 3600,
    )
    assert client.get("/api/auth/me", headers=auth(legacy)).status_code == 200
    other_password = "synthetic-unaffected-viewer-password"
    created = client.post("/api/users", headers=auth(first),
                          json={"username": "unaffected", "password": other_password, "role": "viewer"})
    assert created.status_code == 201, created.text
    other = login(client, "unaffected", other_password)
    changed = client.post("/api/auth/password", headers=auth(first), json=password_change_body())
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"changed": True, "reauthenticate": True}
    with client.app.state.db.connection() as db:
        after = dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone())
        audits = "\n".join(row[0] for row in db.execute("SELECT details_json FROM audit_logs"))
    assert after["password_hash"] != before["password_hash"]
    assert after["session_version"] == before["session_version"] + 1
    assert auth_module.verify_password(password_change_body()["new_password"], after["password_hash"])
    assert after["username"] == before["username"] and after["role"] == before["role"]
    for token in (first, second, legacy):
        assert client.get("/api/auth/me", headers=auth(token)).status_code == 401
    assert client.get("/api/auth/me", headers=auth(other)).status_code == 200
    old_login = client.post("/api/auth/login", json={"username": "admin", "password": password_change_body()["current_password"]})
    assert old_login.status_code == 401
    fresh = login(client, "admin", password_change_body()["new_password"])
    assert client.get("/api/auth/me", headers=auth(fresh)).status_code == 200
    for secret in password_change_body().values():
        assert secret not in changed.text and secret not in audits
    assert after["password_hash"] not in changed.text and after["password_hash"] not in audits


@pytest.mark.parametrize("version", [True, False, -1, "0", 0.0, None, {}, [], 2**63, 1])
def test_session_version_is_strict_and_cannot_authorize_an_incorrect_revision(client, version):
    with client.app.state.db.connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    token = auth_module.create_session_token(
        {"uid": user_id, "sub": "admin", "role": "admin", "sv": version},
        client.app.state.settings.secret_key, 3600,
    )
    assert client.get("/api/auth/me", headers=auth(token)).status_code == 401


def test_password_change_survives_restart_with_original_bootstrap_environment(tmp_path, monkeypatch):
    settings = Settings(**controller_settings_kwargs(tmp_path))
    replacement = "  synthetic-password-with-spaces  "
    with TestClient(create_app(settings)) as current:
        token = login(current)
        changed = current.post("/api/auth/password", headers=auth(token),
                               json=password_change_body(new_password=replacement, confirm_password=replacement))
        assert changed.status_code == 200, changed.text
        with current.app.state.db.connection() as db:
            before = dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone())
    monkeypatch.setenv("RIGPILOT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("RIGPILOT_DATABASE", str(settings.database_path))
    monkeypatch.setenv("RIGPILOT_SECRET_KEY", settings.secret_key)
    monkeypatch.setenv("RIGPILOT_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("RIGPILOT_ADMIN_PASSWORD", password_change_body()["current_password"])
    with TestClient(create_app(Settings.from_env())) as reopened:
        with reopened.app.state.db.connection() as db:
            assert dict(db.execute("SELECT * FROM users WHERE username='admin'").fetchone()) == before
            assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 1
        rejected = reopened.post("/api/auth/login", json={"username": "admin", "password": password_change_body()["current_password"]})
        assert rejected.status_code == 401
        fresh = login(reopened, "admin", replacement)
        assert reopened.get("/api/auth/me", headers=auth(fresh)).status_code == 200
        assert reopened.post("/api/auth/login", json={"username": "admin", "password": replacement.strip()}).status_code == 401


