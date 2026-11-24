from __future__ import annotations

from pathlib import Path
import json

import pytest
import requests

from agent import client as client_module
from agent.client import ControllerRequestError, ProtocolError, RigPilotClient
from agent.config import AgentConfig
from agent.state import AgentState


def _client() -> RigPilotClient:
    return RigPilotClient(
        AgentConfig(
            server_url="https://controller.example",
            state_file=Path("state.json"),
        )
    )


def _host() -> dict[str, str]:
    return {
        "hostname": "WIN10-RIG",
        "os_family": "windows",
        "platform": "Windows-10",
    }


def test_enrollment_connection_refused_has_actionable_safe_message(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _client()
    secret_token = "token-that-must-not-appear"

    def refused(*_args, **_kwargs):
        raise requests.ConnectionError(
            "[WinError 10061] No connection could be made because the target "
            "machine actively refused it"
        )

    monkeypatch.setattr(client.session, "request", refused)

    with pytest.raises(ControllerRequestError) as exc_info:
        client.enroll(secret_token, _host())

    message = str(exc_info.value)
    assert "controller.example:443" in message
    assert "was refused" in message
    assert "not an enrollment-token error" in message
    assert secret_token not in message
    assert "Max retries exceeded" not in message
    assert exc_info.value.response_received is False
    assert exc_info.value.http_status is None
    assert exc_info.value._network_error is True
    assert exc_info.value._http_error is False


def test_enrollment_dns_failure_identifies_config_location(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _client()

    def dns_failure(*_args, **_kwargs):
        raise requests.ConnectionError("[Errno 11001] getaddrinfo failed")

    monkeypatch.setattr(client.session, "request", dns_failure)

    with pytest.raises(ControllerRequestError) as exc_info:
        client.enroll("safe-token", _host())

    assert "could not be resolved" in str(exc_info.value)
    assert "data\\config.json" in str(exc_info.value)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (requests.ConnectTimeout("timeout"), "TCP port 443"),
        (requests.ReadTimeout("timeout"), "did not respond in time"),
        (requests.exceptions.SSLError("certificate verify failed"), "TLS verification failed"),
        (requests.exceptions.ProxyError("proxy failed"), "Windows proxy settings"),
    ],
)
def test_network_failure_types_have_specific_guidance(
    monkeypatch: pytest.MonkeyPatch,
    error: requests.RequestException,
    expected: str,
):
    client = _client()

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(client.session, "request", fail)

    with pytest.raises(ControllerRequestError, match=expected):
        client.enroll("safe-token", _host())


def _response(status: int, body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.headers["Content-Type"] = "application/json"
    return response


@pytest.mark.parametrize("redirect_status", [307, 308])
@pytest.mark.parametrize("endpoint", ["enroll", "replace", "heartbeat"])
def test_sensitive_controller_posts_never_follow_redirects_or_accept_3xx(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    redirect_status: int,
):
    client = _client()
    captured: dict[str, object] = {}

    def request(method: str, url: str, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return _response(redirect_status, b"")

    monkeypatch.setattr(client.session, "request", request)
    state = AgentState.new("agent-id", "agent-secret-that-must-not-leak")
    if endpoint == "enroll":
        invoke = lambda: client.enroll(
            "enrollment-token-that-must-not-leak", _host()
        )
    elif endpoint == "replace":
        invoke = lambda: client.replace_identity(
            "replacement-token-that-must-not-leak",
            _host(),
            request_id="c" * 32,
            proposed_agent_secret="rpa_" + "A" * 48,
        )
    else:
        invoke = lambda: client.heartbeat(
            state,
            {
                **_host(),
                "ip_addresses": [],
                "metrics": {},
                "inventory": {},
            },
        )

    with pytest.raises(ControllerRequestError) as exc_info:
        invoke()

    assert captured["method"] == "POST"
    assert captured["allow_redirects"] is False
    assert exc_info.value.http_status == redirect_status
    error = str(exc_info.value)
    assert "enrollment-token-that-must-not-leak" not in error
    assert "replacement-token-that-must-not-leak" not in error
    assert "agent-secret-that-must-not-leak" not in error


def test_enrollment_unauthorized_explains_one_time_token(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _client()
    monkeypatch.setattr(
        client.session,
        "request",
        lambda *_args, **_kwargs: _response(
            401, b'{"detail":"Invalid or expired enrollment token"}'
        ),
    )

    with pytest.raises(ControllerRequestError) as exc_info:
        client.enroll("already-used-token", _host())

    message = str(exc_info.value)
    assert "HTTP 401" in message
    assert "invalid, expired, or already used" in message
    assert "already-used-token" not in message
    assert exc_info.value.response_received is True
    assert exc_info.value.http_status == 401
    assert exc_info.value._http_error is True
    assert exc_info.value._network_error is False


def test_gateway_failure_distinguishes_proxy_from_token(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _client()
    monkeypatch.setattr(
        client.session,
        "request",
        lambda *_args, **_kwargs: _response(503, b"service unavailable"),
    )

    with pytest.raises(ControllerRequestError, match="reverse proxy.*controller is unavailable"):
        client.enroll("safe-token", _host())


def test_heartbeat_reports_only_dynamically_available_capabilities(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _client()
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return _response(200, b'{"commands":[]}')

    monkeypatch.setattr(client.session, "request", request)
    readiness = {
        "schema_version": 1,
        "protocol": "agent_update.v1",
        "available": True,
        "reason": None,
    }
    host = {
        **_host(),
        "ip_addresses": [],
        "metrics": {},
        "inventory": {
            "agent_update_readiness": readiness,
            "_heartbeat_capabilities": ["agent_update.v1"],
        },
    }
    client.heartbeat(AgentState.new("agent-id", "secret"), host)

    assert captured["json"]["capabilities"] == ["agent_update.v1"]
    assert captured["json"]["local_ip_addresses"] == []
    assert captured["json"]["public_ip_addresses"] == []
    assert captured["json"]["inventory"]["agent_update_readiness"] == readiness
    assert "_heartbeat_capabilities" not in captured["json"]["inventory"]
    assert host["inventory"]["_heartbeat_capabilities"] == ["agent_update.v1"]
    assert captured["timeout"] == 10.0


def test_heartbeat_degrades_non_finite_telemetry_to_identity_only(
    monkeypatch: pytest.MonkeyPatch,
):
    client = _client()
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return _response(200, b'{"commands":[]}')

    monkeypatch.setattr(client.session, "request", request)
    bootstrap = {
        "schema_version": 1,
        "protocol": "agent_bootstrap.v1",
        "platform": "windows",
        "launcher": {
            "name": "portable-agent.ps1",
            "protocol_version": 1,
            "sha256": "a" * 64,
        },
        "updater": {
            "name": "RigPilotAgentUpdater.ps1",
            "protocol_version": 1,
            "sha256": "b" * 64,
        },
        "immutable": True,
    }
    readiness = {
        "schema_version": 1,
        "protocol": "agent_update.v1",
        "available": True,
        "reason": None,
        "bootstrap": bootstrap,
    }
    host = {
        **_host(),
        "ip_addresses": ["192.168.1.20", 123],
        "local_ip_addresses": ["192.168.1.20"],
        "public_ip_addresses": [],
        "metrics": {"cpu_percent": float("nan")},
        "inventory": {
            "mining_status": {"hashrate": float("inf")},
            "agent_update_readiness": readiness,
            "_heartbeat_capabilities": ["agent_update.v1"],
        },
    }

    assert client.heartbeat(AgentState.new("agent-id", "secret"), host) == []

    assert captured["json"]["hostname"] == "WIN10-RIG"
    assert captured["json"]["metrics"] == {}
    assert captured["json"]["inventory"] == {
        "agent_update_readiness": {
            "schema_version": 1,
            "protocol": "agent_update.v1",
            "available": True,
            "reason": None,
            "bootstrap": bootstrap,
        }
    }
    assert captured["json"]["ip_addresses"] == ["192.168.1.20"]
    assert captured["timeout"] == 10.0


def test_heartbeat_preserves_shorter_configured_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    config = AgentConfig(
        server_url="https://controller.example",
        state_file=Path("state.json"),
        request_timeout=3,
    )
    client = RigPilotClient(config)
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return _response(200, b'{"commands":[]}')

    monkeypatch.setattr(client.session, "request", request)
    host = {
        **_host(),
        "ip_addresses": [],
        "metrics": {},
        "inventory": {},
    }
    client.heartbeat(AgentState.new("agent-id", "secret"), host)
    assert captured["timeout"] == 3.0


def test_client_freezes_environment_transport_and_never_reads_netrc(
    monkeypatch: pytest.MonkeyPatch,
):
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NETRC",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://system-proxy.example:8080")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/fixed/controller-ca.pem")

    def forbidden_netrc(*_args, **_kwargs):
        raise AssertionError("the Agent must never inspect .netrc")

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", forbidden_netrc)
    client = RigPilotClient(
        AgentConfig(
            server_url="https://controller.example",
            state_file=Path("state.json"),
        )
    )
    captured: list[tuple[requests.PreparedRequest, dict]] = []

    def send(request, **kwargs):
        captured.append((request, kwargs))
        response = _response(200, b'{"commands":[]}')
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(client.session, "send", send)
    # Changing the process environment after startup must not trigger another
    # lookup or silently change the heartbeat route.
    monkeypatch.setenv("HTTPS_PROXY", "http://changed.example:9999")
    state = AgentState.new("agent-id", "agent-secret")
    host = {
        **_host(),
        "ip_addresses": [],
        "metrics": {},
        "inventory": {},
    }

    assert client.heartbeat(state, host) == []
    assert client.heartbeat(state, host) == []

    assert client.session.trust_env is False
    assert len(captured) == 2
    for request, settings in captured:
        assert request.headers["Authorization"] == "Bearer agent-secret"
        assert settings["proxies"]["https"] == "http://system-proxy.example:8080"
        assert settings["verify"] == "/fixed/controller-ca.pem"


def test_client_preserves_explicit_session_proxy_and_auth_when_disabling_netrc(
    monkeypatch: pytest.MonkeyPatch,
):
    for name in (
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy.example:8080")

    def forbidden_netrc(*_args, **_kwargs):
        raise AssertionError("the Agent must never inspect .netrc")

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", forbidden_netrc)
    session = requests.Session()
    session.proxies["https"] = "http://explicit-proxy.example:3128"
    session.auth = ("controller-user", "controller-password")
    client = RigPilotClient(
        AgentConfig(
            server_url="https://controller.example",
            state_file=Path("state.json"),
        ),
        session=session,
    )
    captured = {}

    def send(request, **kwargs):
        captured["request"] = request
        captured["settings"] = kwargs
        response = _response(
            200,
            b'{"agent_id":"new-agent","agent_secret":"secret"}',
        )
        response.request = request
        response.url = request.url
        return response

    monkeypatch.setattr(session, "send", send)

    enrolled = client.enroll("one-time-token", _host())

    assert enrolled.agent_id == "new-agent"
    assert session.trust_env is False
    assert captured["settings"]["proxies"]["https"] == (
        "http://explicit-proxy.example:3128"
    )
    assert captured["request"].headers["Authorization"].startswith("Basic ")


def test_heartbeat_reports_classified_network_addresses(monkeypatch):
    client = _client()
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return _response(200, b'{"commands":[]}')

    monkeypatch.setattr(client.session, "request", request)
    readiness = {
        "schema_version": 1,
        "protocol": "agent_update.v1",
        "available": False,
        "reason": "marker_missing",
    }
    host = {
        **_host(),
        "ip_addresses": ["192.168.1.20", "8.8.8.8"],
        "local_ip_addresses": ["192.168.1.20"],
        "public_ip_addresses": ["8.8.8.8"],
        "metrics": {},
        "inventory": {"agent_update_readiness": readiness},
    }

    client.heartbeat(AgentState.new("agent-id", "secret"), host)

    assert captured["json"]["local_ip_addresses"] == ["192.168.1.20"]
    assert captured["json"]["public_ip_addresses"] == ["8.8.8.8"]
    assert captured["json"]["inventory"]["agent_update_readiness"]["reason"] == "marker_missing"


def test_device_inheritance_uses_caller_secret_and_requires_confirmation(monkeypatch):
    client = _client()
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return _response(
            201,
            b'{"agent_id":"existing-agent","heartbeat_interval":10,'
            b'"inherited_existing_device":true}',
        )

    monkeypatch.setattr(client.session, "request", request)
    proposed = "rpa_" + "A" * 48
    inherited = client.replace_identity(
        "rpr_" + "B" * 43,
        _host(),
        request_id="c" * 32,
        proposed_agent_secret=proposed,
    )

    assert inherited.agent_id == "existing-agent"
    assert inherited.agent_secret == proposed
    assert inherited.heartbeat_interval == 10
    assert captured["json"]["request_id"] == "c" * 32
    assert captured["json"]["proposed_agent_secret"] == proposed


def test_agent_update_download_is_authenticated_streamed_and_never_redirected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client()
    content = b"verified update bytes"
    digest = __import__("hashlib").sha256(content).hexdigest()
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        response = _response(200, content)
        response._content_consumed = True
        response.headers["Content-Length"] = str(len(content))
        return response

    monkeypatch.setattr(client.session, "request", request)
    destination = tmp_path / "release.zip"
    result = client.download_agent_update(
        AgentState.new("agent-id", "top-secret"),
        "/api/agent/updates/command-id/download",
        destination,
        expected_size=len(content),
        expected_sha256=digest,
        max_bytes=1024,
    )

    assert destination.read_bytes() == content
    assert result == {"size": len(content), "sha256": digest}
    assert captured["stream"] is True
    assert captured["allow_redirects"] is False
    assert captured["headers"]["Authorization"] == "Bearer top-secret"


def test_agent_update_download_preserves_preexisting_destination_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    client = _client()
    content = b"new"
    response = _response(200, content)
    response._content_consumed = True
    response.headers["Content-Length"] = str(len(content))
    monkeypatch.setattr(client.session, "request", lambda *_args, **_kwargs: response)
    destination = tmp_path / "release.zip"
    destination.write_bytes(b"existing")

    with pytest.raises(OSError):
        client.download_agent_update(
            AgentState.new("agent-id", "secret"),
            "/api/agent/updates/command-id/download",
            destination,
            expected_size=len(content),
            expected_sha256=__import__("hashlib").sha256(content).hexdigest(),
            max_bytes=1024,
        )

    assert destination.read_bytes() == b"existing"


@pytest.mark.parametrize("controller_status", ["expired", [], {}])
def test_post_result_validates_controller_status(
    monkeypatch: pytest.MonkeyPatch, controller_status,
):
    client = _client()
    monkeypatch.setattr(
        client.session,
        "request",
        lambda *_args, **_kwargs: _response(
            200, json.dumps({"status": controller_status}).encode(),
        ),
    )

    if not isinstance(controller_status, str):
        with pytest.raises(ProtocolError, match="invalid status"):
            client.post_result(AgentState.new("agent-id", "secret"), "command-id", {})
        return
    status = client.post_result(
        AgentState.new("agent-id", "secret"),
        "command-id",
        {"status": "succeeded", "result": {}, "error": None},
    )

    assert status == "expired"


def test_heartbeat_advertises_only_verified_remote_access_capabilities(monkeypatch):
    config = AgentConfig(
        server_url="https://controller.example",
        state_file=Path("state.json"),
    )
    client = RigPilotClient(config)
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return _response(200, b'{"commands":[]}')

    monkeypatch.setattr(client.session, "request", request)
    host = {
        **_host(),
        "ip_addresses": ["192.0.2.10"],
        "metrics": {},
        "inventory": {
            "agent_update_readiness": {
                "schema_version": 1,
                "protocol": "agent_update.v1",
                "available": False,
                "reason": "config_disabled",
            },
            "remote_access": {
                "schema_version": 1,
                "ssh": {"available": True, "port": 22},
                "sftp": {"available": False, "port": 22},
            }
        },
    }

    client.heartbeat(AgentState.new("agent-id", "secret"), host)

    assert captured["json"]["capabilities"] == ["remote_access.ssh"]


def test_heartbeat_does_not_run_block_device_capability_probes(monkeypatch):
    """The HTTP liveness path consumes the worker snapshot and touches no disk."""

    client = _client()
    captured = {}

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return _response(200, b'{"commands":[]}')

    def forbidden_probe(*_args, **_kwargs):
        raise AssertionError("local capability probe ran on heartbeat thread")

    monkeypatch.setattr(client.session, "request", request)
    monkeypatch.setattr(
        "agent.agent_updates.agent_update_readiness", forbidden_probe
    )
    host = {
        **_host(),
        "ip_addresses": [],
        "metrics": {},
        "inventory": {
            "agent_update_readiness": {
                "schema_version": 1,
                "protocol": "agent_update.v1",
                "available": True,
                "reason": None,
            },
            "_heartbeat_capabilities": [
                "agent_update.v1",
                "remote_access.spark_shell.v1",
            ],
        },
    }

    assert client.heartbeat(AgentState.new("agent-id", "secret"), host) == []
    assert captured["json"]["capabilities"] == [
        "agent_update.v1",
        "remote_access.spark_shell.v1",
    ]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("inherit", [False, True])
def test_nonfinite_controller_heartbeat_interval_is_rejected(monkeypatch, value, inherit):
    client = _client()
    response = _response(201, json.dumps({
        "agent_id": "existing-agent",
        "agent_secret": "private-agent-secret",
        "heartbeat_interval": value,
        "inherited_existing_device": True,
    }).encode("utf-8"))
    monkeypatch.setattr(client.session, "request", lambda *_args, **_kwargs: response)

    with pytest.raises(ProtocolError, match="heartbeat_interval"):
        if inherit:
            client.replace_identity(
                "replacement-token", _host(),
                request_id="a" * 32, proposed_agent_secret="rpa_" + "A" * 48,
            )
        else:
            client.enroll("enrollment-token", _host())
