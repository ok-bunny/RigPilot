"""Request rejection must remain JSON-safe without accepting non-finite values."""
import json

import pytest

from test_review28_controller_cleanup import review_client
from test_device_gpu_oc import auth, login


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_request_errors_return_422_without_creating_commands(review_client, literal):
    client = review_client
    token = login(client)
    body = '{"vendor":"nvidia","gpu_uuid":"GPU-synthetic-0","setting":{"index":0,"power_limit_w":' + literal + '}}'
    response = client.post("/api/agents/synthetic-agent/gpu-oc/apply", headers={**auth(token), "Content-Type": "application/json"}, content=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(error["type"] == "finite_number" for error in detail)
    assert all("input" not in error and "ctx" not in error for error in detail)
    json.dumps(response.json(), allow_nan=False)
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0


def test_ordinary_request_error_diagnostics_are_preserved(review_client):
    token = login(review_client)
    response = review_client.post("/api/agents/synthetic-agent/gpu-oc/apply", headers=auth(token), json={
        "vendor": "nvidia", "gpu_uuid": "GPU-synthetic-0",
        "setting": {"index": 0, "power_limit_w": 0},
    })
    assert response.status_code == 422
    assert any(error["loc"][-1] == "power_limit_w" and error["type"] == "greater_than" and error["msg"] for error in response.json()["detail"])


def test_enrollment_validation_still_omits_sensitive_input_and_context(review_client):
    secret = "synthetic-bootstrap-private-sentinel"
    response = review_client.post("/api/agent/enroll", json={"enrollment_token": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])


def test_nonfinite_body_does_not_bypass_missing_auth_or_viewer_role(review_client):
    body = '{"vendor":"nvidia","gpu_uuid":"GPU-synthetic-0","setting":{"index":0,"power_limit_w":NaN}}'
    assert review_client.post("/api/agents/synthetic-agent/gpu-oc/apply", headers={"Content-Type": "application/json"}, content=body).status_code == 401
    token = login(review_client)
    password = "synthetic-viewer-password-long-enough"
    assert review_client.post("/api/users", headers=auth(token), json={"username": "response-viewer", "password": password, "role": "viewer"}).status_code == 201
    response = review_client.post("/api/auth/login", json={"username": "response-viewer", "password": password})
    assert response.status_code == 200
    assert review_client.post("/api/agents/synthetic-agent/gpu-oc/apply", headers={**auth(response.json()["token"]), "Content-Type": "application/json"}, content=body).status_code == 403


@pytest.mark.parametrize("kind", ["wallet", "pool", "replacement"])
def test_serializable_sensitive_validation_values_are_never_echoed(review_client, kind):
    token = login(review_client)
    sentinels = []
    if kind == "wallet":
        sentinels = ["xprv-synthetic-private-wallet-sentinel"]
        path = "/api/wallets"
        body = {"name": "synthetic", "coin": "XMR", "address": sentinels[0]}
    elif kind == "pool":
        sentinels = ["synthetic-private-pool-password\x00sentinel"]
        path = "/api/pools"
        body = {"name": "synthetic", "coin": "XMR", "pool_url": "stratum+tcp://synthetic.example:3333", "pool_password": sentinels[0]}
    else:
        sentinels = ["rpr_SYNTHETIC_PRIVATE_TOKEN", "rpa_SYNTHETIC_PRIVATE_SECRET"]
        path = "/api/agent/replace"
        body = {"replacement_token": sentinels[0], "proposed_agent_secret": sentinels[1],
                "request_id": "a" * 32, "hostname": "synthetic", "os_family": "linux"}
    response = review_client.post(path, headers=auth(token), json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert all(set(error) == {"loc", "type", "msg"} for error in detail)
    assert all(error["loc"] and error["type"] and error["msg"] for error in detail)
    assert all(sentinel not in json.dumps(response.json()) for sentinel in sentinels)
    if kind == "pool":
        assert "synthetic-private-pool-password" not in response.text
