from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server.main as server_main
from server.config import Settings
from server.main import (
    LoginRateLimiter,
    RequestBodyLimitMiddleware,
    UPLOAD_REQUEST_OVERHEAD_BYTES,
    create_app,
)


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "security.db",
        secret_key="security-test-secret-key-with-enough-entropy",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        agent_online_seconds=60,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def admin_token(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 200
    return response.json()["token"]


def user_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def enrolled_agent(client: TestClient, token: str) -> tuple[str, dict[str, str]]:
    enrollment = client.post(
        "/api/enrollment-tokens",
        headers=user_headers(token),
        json={"label": "security-node", "ttl_minutes": 15},
    ).json()["token"]
    response = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": enrollment,
            "hostname": "security-node",
            "os_family": "linux",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["agent_id"], {
        "X-Agent-ID": body["agent_id"],
        "Authorization": f"Bearer {body['agent_secret']}",
    }


def heartbeat(client: TestClient, headers: dict[str, str], metrics=None):
    return client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": "security-node",
            "os_family": "linux",
            "metrics": metrics or {},
        },
    )


def test_failed_login_is_committed_to_audit(client: TestClient):
    failed = client.post(
        "/api/auth/login", json={"username": "admin", "password": "incorrect"}
    )
    assert failed.status_code == 401
    token = admin_token(client)
    entries = client.get("/api/audit", headers=user_headers(token)).json()
    assert any(entry["action"] == "login_failed" for entry in entries)


def test_viewer_cannot_read_pool_password_and_command_views_are_redacted(
    client: TestClient,
):
    token = admin_token(client)
    created = client.post(
        "/api/users",
        headers=user_headers(token),
        json={
            "username": "secret-viewer",
            "password": "viewer-password-that-is-long-enough",
            "role": "viewer",
        },
    )
    assert created.status_code == 201, created.text
    viewer = client.post(
        "/api/auth/login",
        json={
            "username": "secret-viewer",
            "password": "viewer-password-that-is-long-enough",
        },
    ).json()["token"]
    wallet = client.post(
        "/api/wallets",
        headers=user_headers(token),
        json={
            "name": "Public payout",
            "coin": "XMR",
            "address": "47publicwalletaddress000000000000000000000000",
        },
    ).json()
    sheet = client.post(
        "/api/flight-sheets",
        headers=user_headers(token),
        json={
            "name": "Credential boundary",
            "coin": "XMR",
            "wallet_id": wallet["id"],
            "pool_url": "stratum+ssl://pool.example.test:443",
            "pool_password": "pool-secret-value",
        },
    )
    assert sheet.status_code == 201, sheet.text

    admin_view = client.get(
        "/api/flight-sheets", headers=user_headers(token)
    ).json()[0]
    viewer_view = client.get(
        "/api/flight-sheets", headers=user_headers(viewer)
    ).json()[0]
    assert admin_view["pool_password"] == "pool-secret-value"
    assert viewer_view["pool_password"] is None
    assert viewer_view["pool_password_configured"] is True

    agent_id, agent_headers = enrolled_agent(client, token)
    queued = client.post(
        f"/api/agents/{agent_id}/flight-sheets/{sheet.json()['id']}/apply",
        headers=user_headers(token),
    )
    assert queued.status_code == 201, queued.text
    assert (
        queued.json()["payload"]["primary"]["pool_password"] == "[configured]"
    )
    exact = client.get(
        f"/api/commands/{queued.json()['id']}",
        headers=user_headers(viewer),
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["payload"]["primary"]["pool_password"] == "[configured]"

    # Redaction is a browser/API presentation boundary.  The authenticated
    # Agent still receives the real value required by the miner.
    delivered = heartbeat(client, agent_headers).json()["commands"]
    assert delivered[0]["payload"]["primary"]["pool_password"] == "pool-secret-value"


def test_successful_viewer_login_does_not_reset_admin_failure_limit(
    client: TestClient,
):
    token = admin_token(client)
    created = client.post(
        "/api/users",
        headers=user_headers(token),
        json={
            "username": "viewer",
            "password": "viewer-password-that-is-long-enough",
            "role": "viewer",
        },
    )
    assert created.status_code == 201, created.text
    for _ in range(8):
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "incorrect"},
        ).status_code == 401
        assert client.post(
            "/api/auth/login",
            json={
                "username": "viewer",
                "password": "viewer-password-that-is-long-enough",
            },
        ).status_code == 200
    blocked = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "incorrect"},
    )
    assert blocked.status_code == 429


def test_login_rate_limit_cannot_be_bypassed_with_distinct_usernames():
    limiter = LoginRateLimiter(attempts=8, ip_attempts=4, window_seconds=60)
    for index in range(8):
        username = f"missing-{index}"
        limiter.check("192.0.2.10", username)
        limiter.record_failure("192.0.2.10", username)

    with pytest.raises(HTTPException) as caught:
        limiter.check("192.0.2.10", "another-username")
    assert caught.value.status_code == 429
    assert caught.value.headers == {"Retry-After": "60"}


def test_login_rate_limiter_tracking_is_capacity_bounded():
    limiter = LoginRateLimiter(
        attempts=100,
        ip_attempts=100,
        max_keys=3,
        max_ips=2,
    )
    for index in range(12):
        ip = f"192.0.2.{index}"
        username = f"missing-{index}"
        limiter.check(ip, username)
        limiter.record_failure(ip, username)
    assert len(set(limiter._events) | set(limiter._inflight)) <= 3
    assert len(set(limiter._ip_events) | set(limiter._ip_inflight)) <= 2


def test_missing_user_login_still_performs_password_verification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    original = server_main.verify_password
    checked_hashes: list[str] = []

    def traced_verify(password: str, encoded: str) -> bool:
        checked_hashes.append(encoded)
        return original(password, encoded)

    monkeypatch.setattr(server_main, "verify_password", traced_verify)
    response = client.post(
        "/api/auth/login",
        json={"username": "definitely-not-a-user", "password": "incorrect"},
    )
    assert response.status_code == 401
    assert len(checked_hashes) == 1
    assert checked_hashes[0].startswith("pbkdf2_sha256$")


def test_failed_login_response_does_not_reveal_the_default_or_account_existence(
    client: TestClient,
):
    existing = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "incorrect"},
    )
    missing = client.post(
        "/api/auth/login",
        json={"username": "definitely-not-a-user", "password": "incorrect"},
    )

    assert existing.status_code == missing.status_code == 401
    assert existing.json() == missing.json() == {"detail": "Invalid credentials"}
    assert "admin" not in existing.text.casefold()


def test_non_object_gpu_entry_does_not_poison_summary(client: TestClient):
    token = admin_token(client)
    _, headers = enrolled_agent(client, token)
    response = heartbeat(client, headers, {"gpus": [1, None, "bad"]})
    assert response.status_code == 200
    summary = client.get("/api/summary", headers=user_headers(token))
    assert summary.status_code == 200


def test_nonterminal_command_is_redelivered_until_result(client: TestClient):
    token = admin_token(client)
    agent_id, headers = enrolled_agent(client, token)
    command = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=user_headers(token),
        json={"action": "collect_diagnostics", "payload": {}},
    ).json()

    first = heartbeat(client, headers).json()["commands"]
    second = heartbeat(client, headers).json()["commands"]
    assert [item["id"] for item in first] == [command["id"]]
    assert [item["id"] for item in second] == [command["id"]]

    result = client.post(
        f"/api/agent/commands/{command['id']}/result",
        headers=headers,
        json={"status": "succeeded", "result": {"ok": True}, "error": None},
    )
    assert result.status_code == 200
    assert heartbeat(client, headers).json()["commands"] == []


def test_mining_control_reserves_heartbeat_batch_priority(client: TestClient):
    token = admin_token(client)
    agent_id, headers = enrolled_agent(client, token)
    client.app.state.settings.command_batch_size = 1
    assert heartbeat(client, headers).status_code == 200

    diagnostic = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=user_headers(token),
        json={"action": "collect_diagnostics", "payload": {}},
    ).json()
    assert heartbeat(client, headers).json()["commands"][0]["id"] == diagnostic["id"]
    running = client.post(
        f"/api/agent/commands/{diagnostic['id']}/result",
        headers=headers,
        json={"status": "running", "result": {}, "error": None},
    )
    assert running.status_code == 200, running.text
    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE agents SET agent_version='0.6.31' WHERE id=?", (agent_id,)
        )

    stop = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=user_headers(token),
        json={"action": "stop_mining", "payload": {}},
    )
    assert stop.status_code == 201, stop.text
    delivered = heartbeat(client, headers).json()["commands"]
    assert [command["id"] for command in delivered] == [stop.json()["id"]]


def test_dispatched_command_cannot_be_cancelled_as_if_still_queued(
    client: TestClient,
):
    token = admin_token(client)
    agent_id, headers = enrolled_agent(client, token)
    command = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=user_headers(token),
        json={"action": "collect_diagnostics", "payload": {}},
    ).json()
    assert heartbeat(client, headers).json()["commands"][0]["id"] == command["id"]
    cancelled = client.post(
        f"/api/commands/{command['id']}/cancel", headers=user_headers(token)
    )
    assert cancelled.status_code == 409
    current = client.get(
        f"/api/commands/{command['id']}", headers=user_headers(token)
    ).json()
    assert current["status"] == "dispatched"


def test_stale_heartbeat_cannot_regress_agent_or_append_telemetry(client: TestClient):
    token = admin_token(client)
    agent_id, headers = enrolled_agent(client, token)
    database_path = client.app.state.db.path
    with sqlite3.connect(database_path) as db:
        db.execute(
            """UPDATE agents SET last_seen='2999-01-01T00:00:00.000000+00:00',
               hostname='newer-state' WHERE id=?""",
            (agent_id,),
        )
        before = db.execute(
            "SELECT COUNT(*) FROM telemetry WHERE agent_id=?", (agent_id,)
        ).fetchone()[0]
    stale = heartbeat(client, headers, {"cpu_percent": 99})
    assert stale.status_code == 200
    assert stale.json()["stale_heartbeat"] is True
    assert stale.json()["commands"] == []
    with sqlite3.connect(database_path) as db:
        row = db.execute(
            "SELECT hostname FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        after = db.execute(
            "SELECT COUNT(*) FROM telemetry WHERE agent_id=?", (agent_id,)
        ).fetchone()[0]
    assert row[0] == "newer-state"
    assert after == before


def test_deep_agent_payload_is_rejected(client: TestClient):
    token = admin_token(client)
    _, headers = enrolled_agent(client, token)
    nested = {"value": 1}
    for _ in range(12):
        nested = {"next": nested}
    response = heartbeat(client, headers, nested)
    assert response.status_code == 422


def test_chunked_json_body_is_limited_without_content_length(client: TestClient):
    def oversized_body():
        yield b'{"username":"'
        for _ in range(17):
            yield b"a" * 65_536
        yield b'","password":"incorrect"}'

    response = client.post(
        "/api/auth/login",
        headers={"Content-Type": "application/json"},
        content=oversized_body(),
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "Request body too large"


def test_chunked_agent_heartbeat_obeys_agent_body_limit(client: TestClient):
    token = admin_token(client)
    _agent_id, headers = enrolled_agent(client, token)
    payload = (
        b'{"hostname":"security-node","os_family":"linux","metrics":{"padding":"'
        + b"a" * 310_000
        + b'"}}'
    )

    def chunks():
        for offset in range(0, len(payload), 16_384):
            yield payload[offset : offset + 16_384]

    response = client.post(
        "/api/agent/heartbeat",
        headers={**headers, "Content-Type": "application/json"},
        content=chunks(),
    )
    assert response.status_code == 413


def test_bootstrap_readiness_is_bounded_inventory_compatible(client: TestClient):
    token = admin_token(client)
    agent_id, headers = enrolled_agent(client, token)
    bootstrap = {
        "schema_version": 1,
        "protocol": "agent_bootstrap.v1",
        "platform": "linux",
        "launcher": {"name": "launcher", "protocol_version": 1, "sha256": None},
        "updater": {"name": "updater", "protocol_version": 1, "sha256": "a" * 64},
        "immutable": True,
    }
    response = client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": "security-node",
            "os_family": "linux",
            "inventory": {
                "agent_update_readiness": {"bootstrap": bootstrap}
            },
        },
    )
    assert response.status_code == 200, response.text
    agent = client.get(
        f"/api/agents/{agent_id}", headers=user_headers(token)
    ).json()
    assert agent["inventory"]["agent_update_readiness"]["bootstrap"] == bootstrap


def test_upload_request_limit_allows_bounded_overhead_and_rejects_excess(
    client: TestClient,
):
    async def status_for(content_lengths: list[int]) -> int:
        messages: list[dict] = []

        async def downstream(scope, receive, send):
            del scope, receive
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        middleware = RequestBodyLimitMiddleware(
            downstream, settings=client.app.state.settings
        )
        await middleware(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/api/agent-releases/upload",
                "raw_path": b"/api/agent-releases/upload",
                "query_string": b"",
                "headers": [
                    (b"content-length", str(value).encode("ascii"))
                    for value in content_lengths
                ],
                "client": ("192.0.2.10", 1234),
                "server": ("testserver", 443),
            },
            receive,
            send,
        )
        return next(message["status"] for message in messages if message["type"] == "http.response.start")

    maximum = client.app.state.settings.max_agent_release_bytes
    assert asyncio.run(
        status_for([maximum + UPLOAD_REQUEST_OVERHEAD_BYTES])
    ) == 204
    assert asyncio.run(
        status_for([maximum + UPLOAD_REQUEST_OVERHEAD_BYTES + 1])
    ) == 413
    assert asyncio.run(status_for([1, 1])) == 400


def test_forwarded_proto_header_cannot_bypass_https(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "https.db",
        secret_key="https-test-secret-key-with-enough-entropy",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
        require_https_for_agents=True,
    )
    with TestClient(create_app(settings)) as client:
        token = admin_token(client)
        enrollment = client.post(
            "/api/enrollment-tokens",
            headers=user_headers(token),
            json={"label": "https-node", "ttl_minutes": 15},
        ).json()["token"]
        response = client.post(
            "/api/agent/enroll",
            headers={"X-Forwarded-Proto": "https"},
            json={
                "enrollment_token": enrollment,
                "hostname": "https-node",
                "os_family": "linux",
            },
        )
    assert response.status_code == 400
