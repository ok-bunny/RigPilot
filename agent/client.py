"""HTTPS client for enrollment, heartbeats, and command results."""

from __future__ import annotations

import json
import logging
import hashlib
import os
import sys
import re
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .config import AgentConfig
from .state import AgentState, StateError, _enrollment_metadata
from .version import __version__


LOGGER = logging.getLogger(__name__)
ENROLLMENT_PROTOCOL = "rigpilot.enrollment.v2"
ENROLLMENT_NOT_CREATED_EXIT_CODE = 10
MAX_HEARTBEAT_REQUEST_TIMEOUT_SECONDS = 10.0
_HEARTBEAT_CAPABILITIES_KEY = "_heartbeat_capabilities"
_HEARTBEAT_MAINTENANCE_KEY = "_heartbeat_maintenance"


def _workload_ack_matches(ack: Any, sent: dict[str, Any]) -> bool:
    """Accept only an explicit confirmation of this request's exact snapshot."""
    fields = {"coin", "algorithm", "counter_epoch", "snapshot_seq",
              "estimated_work_total", "observed_at"}
    if not isinstance(ack, dict) or set(ack) != fields or set(sent) != fields:
        return False
    if any(not isinstance(ack[key], str) for key in ("coin", "algorithm", "counter_epoch")):
        return False
    if any(ack.get(key) != sent.get(key) for key in (
        "coin", "algorithm", "counter_epoch", "snapshot_seq",
    )) or type(ack.get("snapshot_seq")) is not int:
        return False
    if (not isinstance(ack["estimated_work_total"], str)
            or not 1 <= len(ack["estimated_work_total"]) <= 128
            or not isinstance(ack["observed_at"], str)
            or not 20 <= len(ack["observed_at"]) <= 64
            or not isinstance(sent["observed_at"], str)):
        return False
    try:
        total = Decimal(ack["estimated_work_total"])
        observed_at = datetime.fromisoformat(ack["observed_at"].replace("Z", "+00:00"))
        return (total.is_finite()
                and total == Decimal(str(sent["estimated_work_total"]))
                and observed_at.utcoffset() is not None
                and observed_at == datetime.fromisoformat(sent["observed_at"].replace("Z", "+00:00")))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return False


def _frozen_environment_transport(
    controller_url: str,
) -> tuple[dict[str, str], str | None]:
    """Snapshot proxy/CA environment settings without enabling ``.netrc``.

    ``requests.Session.trust_env`` controls unrelated behaviours as one switch:
    environment proxies and CA bundles, but also a ``~/.netrc`` lookup while
    every request is prepared (and again after redirects).  The Agent has its
    own bearer authentication, so consulting a mutable credential file is both
    unnecessary and, on a degraded USB/root filesystem, capable of stalling the
    heartbeat thread.

    The controller origin is fixed for the lifetime of this client.  Resolve
    its environment/system proxy once at startup and keep that snapshot on the
    Session before disabling ``trust_env``.  Explicit ``Session.proxies`` take
    precedence when the snapshot is merged by the caller.
    """

    no_proxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY")
    try:
        discovered = requests.utils.get_environ_proxies(
            controller_url,
            no_proxy=no_proxy,
        )
    except (OSError, ValueError):
        # Proxy discovery is optional transport configuration.  Failure must
        # not prevent a direct authenticated heartbeat.
        discovered = {}
    proxies = {
        str(scheme): str(proxy)
        for scheme, proxy in discovered.items()
        if isinstance(scheme, str)
        and isinstance(proxy, str)
        and scheme
        and proxy
    }
    ca_bundle = (
        os.environ.get("REQUESTS_CA_BUNDLE")
        or os.environ.get("CURL_CA_BUNDLE")
        or None
    )
    return proxies, ca_bundle


class ProtocolError(RuntimeError):
    """The server returned a successful but malformed response."""


class ControllerRequestError(requests.RequestException):
    """A safe, actionable controller error suitable for console/log output.

    ``requests`` exceptions include implementation details such as urllib3's
    retry wrapper and, depending on the caller, can include a complete URL.
    Keep the public error tied to the configured controller origin and API
    path so portable users can distinguish connectivity failures from an
    invalid enrollment token without echoing request data.
    """

    def __init__(
        self,
        message: str,
        *,
        response_received: bool,
        http_status: int | None = None,
        rejected_enrollment_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.response_received = bool(response_received)
        self.http_status = http_status
        self.rejected_enrollment_request_id = rejected_enrollment_request_id
        self.enrollment_attempt_cleared = False
        self._http_error = self.response_received
        self._network_error = not self.response_received


class RigPilotClient:
    def __init__(self, config: AgentConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.workload_counter_acks: list[dict[str, Any]] | None = None
        self.workload_ack_supported: bool | None = None
        # Freeze the useful environment transport settings once, then disable
        # requests' combined environment hook.  In particular this guarantees
        # prepare_request() and redirect handling never call get_netrc_auth(),
        # while retaining the proxy behaviour that was active at startup.
        use_environment_transport = bool(self.session.trust_env)
        environment_ca_bundle: str | None = None
        if use_environment_transport:
            environment_proxies, environment_ca_bundle = (
                _frozen_environment_transport(config.server_url)
            )
            for scheme, proxy in environment_proxies.items():
                self.session.proxies.setdefault(scheme, proxy)
        self.session.trust_env = False
        self._tls_verify: bool | str = config.tls_verify
        if config.ca_bundle is None and environment_ca_bundle:
            self._tls_verify = environment_ca_bundle
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": f"rigpilot-agent/{__version__}",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.config.server_url}{path}"

    def _controller_endpoint(self) -> tuple[str, int]:
        parsed = urlparse(self.config.server_url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        return (parsed.hostname or "controller", port)

    def _network_error(
        self, exc: requests.RequestException
    ) -> ControllerRequestError:
        host, port = self._controller_endpoint()
        target = f"{host}:{port}"
        raw = str(exc).lower()

        if isinstance(exc, requests.exceptions.SSLError):
            message = (
                f"TLS verification failed for controller {target}. Check the Windows "
                "date/time and the controller certificate; do not disable TLS verification."
            )
        elif isinstance(exc, requests.exceptions.ProxyError):
            message = (
                f"the configured proxy could not connect to controller {target}. "
                "Check Windows proxy settings or bypass the proxy for this controller."
            )
        elif isinstance(exc, requests.exceptions.ConnectTimeout):
            message = (
                f"connection to controller {target} timed out. Check routing, the firewall, "
                f"and whether TCP port {port} is open."
            )
        elif isinstance(exc, requests.exceptions.ReadTimeout):
            message = (
                f"controller {target} accepted the connection but did not respond in time. "
                "Check the controller and reverse-proxy service status."
            )
        elif isinstance(exc, requests.exceptions.ConnectionError):
            if any(
                marker in raw
                for marker in ("connection refused", "actively refused", "10061", "errno 111")
            ):
                message = (
                    f"connection to controller {target} was refused. The controller/reverse "
                    f"proxy is not listening on TCP port {port}, or a firewall is rejecting it. "
                    "This is a connectivity problem, not an enrollment-token error."
                )
            elif any(
                marker in raw
                for marker in (
                    "name or service not known",
                    "nodename nor servname",
                    "getaddrinfo failed",
                    "11001",
                    "temporary failure in name resolution",
                )
            ):
                message = (
                    f"controller host {host} could not be resolved. Check DNS and the "
                    "Controller URL in data\\config.json."
                )
            else:
                message = (
                    f"could not connect to controller {target}. Check the Controller URL, "
                    f"network, firewall, and TCP port {port}."
                )
        elif isinstance(exc, requests.exceptions.Timeout):
            message = f"request to controller {target} timed out."
        else:
            message = (
                f"request to controller {target} failed before a valid response was received."
            )
        return ControllerRequestError(message, response_received=False)

    @staticmethod
    def _response_detail(response: requests.Response) -> str | None:
        """Return a short server error without copying arbitrary HTML."""

        try:
            payload = response.json()
        except (ValueError, requests.RequestException):
            return None
        if not isinstance(payload, dict):
            return None
        detail = payload.get("detail")
        if isinstance(detail, str):
            detail = " ".join(detail.split())
            if detail:
                return detail[:300]
        return None

    def _http_error(
        self, method: str, path: str, response: requests.Response
    ) -> ControllerRequestError:
        status = response.status_code
        detail = self._response_detail(response)
        rejected_enrollment_request_id = None
        if method.upper() == "POST" and path == "/api/agent/enroll" and status == 401:
            try:
                rejection = response.json()
            except (ValueError, requests.RequestException):
                rejection = None
            if isinstance(rejection, dict) and rejection.get("code") == "enrollment_not_created":
                request_id = rejection.get("enrollment_request_id")
                if isinstance(request_id, str) and re.fullmatch(r"[0-9a-f]{32}", request_id):
                    rejected_enrollment_request_id = request_id
        if path == "/api/agent/enroll" and status in (401, 403):
            hint = "The one-time enrollment token is invalid, expired, or already used."
        elif path == "/api/agent/replace" and status in (401, 403):
            hint = (
                "The one-time device inheritance token is invalid, expired, or "
                "already used by another replacement request."
            )
        elif status == 404:
            hint = "Check the Controller URL and confirm the controller version is compatible."
        elif status in (502, 503, 504):
            hint = "The reverse proxy is reachable, but the RigPilot controller is unavailable."
        else:
            hint = "The controller rejected the request."
        parts = [f"controller returned HTTP {status} for {method.upper()} {path}."]
        if detail:
            parts.append(detail.rstrip(".") + ".")
        parts.append(hint)
        return ControllerRequestError(
            " ".join(parts),
            response_received=True,
            http_status=status,
            rejected_enrollment_request_id=rejected_enrollment_request_id,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        state: AgentState | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | tuple[float, float] | None = None,
    ) -> requests.Response:
        headers: dict[str, str] = {}
        if state is not None:
            headers = {
                "X-Agent-ID": state.agent_id,
                "Authorization": f"Bearer {state.agent_secret}",
            }
        try:
            response = self.session.request(
                method,
                self._url(path),
                json=body,
                headers=headers,
                timeout=(self.config.request_timeout if timeout is None else timeout),
                verify=self._tls_verify,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise self._network_error(exc) from exc
        if not 200 <= response.status_code < 300:
            raise self._http_error(method, path, response)
        return response

    @staticmethod
    def _json_object(response: requests.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise ProtocolError("server response is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ProtocolError("server response must be a JSON object")
        return value

    @staticmethod
    def enrollment_metadata(config: AgentConfig, host: dict[str, Any]) -> dict[str, str]:
        metadata = {
            "hostname": host["hostname"], "os_family": host["os_family"],
            "platform": host["platform"], "agent_version": __version__,
        }
        if config.name:
            metadata["name"] = config.name
        return _enrollment_metadata(metadata)

    def enroll(
        self, token: str | None, host: dict[str, Any], *,
        request_id: str | None = None, proposed_agent_secret: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> AgentState:
        recoverable = any(value is not None for value in (request_id, proposed_agent_secret, metadata))
        if recoverable:
            if (not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None
                    or not isinstance(proposed_agent_secret, str)
                    or re.fullmatch(r"rpa_[A-Za-z0-9_-]{48}", proposed_agent_secret) is None):
                raise ProtocolError("pending enrollment credentials are invalid")
            try:
                body = _enrollment_metadata(metadata) if metadata is not None else self.enrollment_metadata(self.config, host)
            except StateError:
                raise ProtocolError("pending enrollment metadata is invalid") from None
            # Establish compatibility before any credential-bearing POST. An
            # ambiguous request is never downgraded to legacy enrollment.
            health = self._json_object(self._request("GET", "/api/health"))
            if health.get("enrollment_protocol") != ENROLLMENT_PROTOCOL:
                raise ProtocolError("controller does not support recoverable enrollment; update the Controller before enrolling")
            body.update(enrollment_request_id=request_id, proposed_agent_secret=proposed_agent_secret)
            if token:
                body["enrollment_token"] = token
        else:
            body = {"enrollment_token": token, **self.enrollment_metadata(self.config, host)}
        response = self._request("POST", "/api/agent/enroll", body=body)
        data = self._json_object(response)
        agent_id = data.get("agent_id")
        secret = data.get("agent_secret")
        interval = data.get("heartbeat_interval")
        if not isinstance(agent_id, str) or not agent_id:
            raise ProtocolError("enrollment response has no valid agent_id")
        if not isinstance(secret, str) or not secret:
            raise ProtocolError("enrollment response has no valid agent_secret")
        if recoverable:
            if data.get("enrollment_request_id") != request_id:
                raise ProtocolError("enrollment response does not confirm the pending request")
            if secret != proposed_agent_secret:
                raise ProtocolError("enrollment response does not match the pending credentials")
        if interval is not None and (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 0 < interval <= sys.float_info.max
        ):
            raise ProtocolError("enrollment heartbeat_interval is invalid")
        return AgentState.new(
            agent_id,
            secret,
            float(interval) if interval is not None else None,
        )

    def replace_identity(
        self,
        token: str,
        host: dict[str, Any],
        *,
        request_id: str,
        proposed_agent_secret: str,
    ) -> AgentState:
        """Atomically inherit one controller device using caller-held entropy."""

        response = self._request(
            "POST",
            "/api/agent/replace",
            body={
                "replacement_token": token,
                "request_id": request_id,
                "proposed_agent_secret": proposed_agent_secret,
                "hostname": host["hostname"],
                "os_family": host["os_family"],
                "platform": host["platform"],
                "agent_version": __version__,
            },
        )
        data = self._json_object(response)
        agent_id = data.get("agent_id")
        interval = data.get("heartbeat_interval")
        if not isinstance(agent_id, str) or not agent_id:
            raise ProtocolError("device inheritance response has no valid agent_id")
        if data.get("inherited_existing_device") is not True:
            raise ProtocolError("device inheritance response was not confirmed")
        if interval is not None and (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 0 < interval <= sys.float_info.max
        ):
            raise ProtocolError("device inheritance heartbeat_interval is invalid")
        return AgentState.new(
            agent_id,
            proposed_agent_secret,
            float(interval) if interval is not None else None,
        )

    def heartbeat(self, state: AgentState, host: dict[str, Any]) -> list[Any]:
        # A failed request or a legacy/empty response cannot reuse an earlier ACK.
        self.workload_counter_acks = None
        self.workload_ack_supported = None
        reported_inventory = host.get("inventory")
        inventory = (
            dict(reported_inventory)
            if isinstance(reported_inventory, dict)
            else reported_inventory
        )
        capabilities: list[str] = []
        update_readiness: dict[str, Any] | None = None
        if isinstance(inventory, dict):
            # All local capability probes are produced by HostDataSampler's
            # bounded worker.  Never repeat lstat/resolve/systemctl/psutil
            # readiness checks on the authenticated heartbeat thread: an
            # unhealthy HDD/USB device can block those syscalls indefinitely
            # while the independently launched miner keeps running.
            raw_capabilities = inventory.pop(_HEARTBEAT_CAPABILITIES_KEY, [])
            # Runtime consumes this private worker hint.  It is neither a
            # controller capability nor public inventory metadata.
            inventory.pop(_HEARTBEAT_MAINTENANCE_KEY, None)
            if isinstance(raw_capabilities, list):
                for capability in raw_capabilities[:64]:
                    if (
                        isinstance(capability, str)
                        and 1 <= len(capability) <= 128
                        and capability.isascii()
                        and all(
                            character.isalnum() or character in "._-"
                            for character in capability
                        )
                        and capability not in capabilities
                    ):
                        capabilities.append(capability)
            reported_readiness = inventory.get("agent_update_readiness")
            if isinstance(reported_readiness, dict):
                update_readiness = reported_readiness
        remote_access = (
            inventory.get("remote_access") if isinstance(inventory, dict) else None
        )
        if isinstance(remote_access, dict):
            for protocol in ("ssh", "sftp"):
                metadata = remote_access.get(protocol)
                capability = f"remote_access.{protocol}"
                if (
                    isinstance(metadata, dict)
                    and metadata.get("available") is True
                    and capability not in capabilities
                ):
                    capabilities.append(capability)
        raw_workload_counters = host.get("workload_counters", [])
        try:
            workload_counters = json.loads(
                json.dumps(raw_workload_counters, ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError):
            workload_counters = []
        if not isinstance(workload_counters, list):
            workload_counters = []
        workload_counters = workload_counters[:16]
        body = {
            "hostname": host["hostname"],
            "os_family": host["os_family"],
            "platform": host["platform"],
            "agent_version": __version__,
            "capabilities": capabilities,
            "ip_addresses": host["ip_addresses"],
            "local_ip_addresses": host.get("local_ip_addresses", []),
            "public_ip_addresses": host.get("public_ip_addresses", []),
            "metrics": host["metrics"],
            "inventory": inventory,
            "workload_counters": workload_counters,
        }
        try:
            # The host-data worker already performs this check.  Repeat it on
            # the final body because readiness/capability metadata is added on
            # this thread.  A single NaN/Infinity must not permanently prevent
            # this Agent from reporting liveness.
            json.dumps(body, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            LOGGER.error(
                "heartbeat telemetry was not JSON-safe; sending identity-only liveness",
                extra={"event": "heartbeat_payload_degraded"},
            )
            body = {
                "hostname": str(host.get("hostname") or "unknown-host"),
                "os_family": str(host.get("os_family") or os.name or "unknown"),
                "platform": str(host.get("platform") or os.name or "unknown"),
                "agent_version": __version__,
                "capabilities": capabilities,
                "ip_addresses": [
                    item for item in host.get("ip_addresses", [])
                    if isinstance(item, str)
                ],
                "local_ip_addresses": [
                    item for item in host.get("local_ip_addresses", [])
                    if isinstance(item, str)
                ],
                "public_ip_addresses": [
                    item for item in host.get("public_ip_addresses", [])
                    if isinstance(item, str)
                ],
                "metrics": {},
                "inventory": (
                    {"agent_update_readiness": update_readiness}
                    if update_readiness is not None
                    else {}
                ),
                "workload_counters": workload_counters,
            }
        response = self._request(
            "POST",
            "/api/agent/heartbeat",
            state=state,
            body=body,
            timeout=min(
                float(self.config.request_timeout),
                MAX_HEARTBEAT_REQUEST_TIMEOUT_SECONDS,
            ),
        )
        if not response.content:
            return []
        data = self._json_object(response)
        commands = data.get("commands", [])
        if not isinstance(commands, list):
            raise ProtocolError("heartbeat commands must be an array")
        if "commands" not in data:
            return commands
        if data.get("stale_heartbeat") is True:
            # This reply explicitly says the heartbeat lost acceptance. Even
            # a matching ACK included accidentally cannot confirm its work.
            return commands
        acknowledgements = data.get("workload_counter_acks")
        if isinstance(acknowledgements, list):
            self.workload_ack_supported = True
            self.workload_counter_acks = [
                dict(sent) for sent in workload_counters
                if isinstance(sent, dict) and any(
                    _workload_ack_matches(ack, sent) for ack in acknowledgements[:16]
                )
            ]
        elif "workload_counter_acks" not in data:
            # Only a normal, non-stale JSON reply proves legacy semantics.
            # Empty or malformed replies provide no capability evidence.
            self.workload_ack_supported = False
        return commands

    def download_agent_update(
        self,
        state: AgentState,
        path: str,
        destination: Path,
        *,
        expected_size: int,
        expected_sha256: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        """Stream one authenticated, same-controller update package to disk.

        Redirects are deliberately disabled: an authenticated Agent request must
        never carry its bearer secret to another origin.  The relative path is
        validated again here even though command authorization performs the
        first validation.
        """

        if not isinstance(path, str):
            raise ProtocolError("agent update download path is not allowed")
        parsed = urlparse(path)
        if (
            not path.startswith("/api/agent/updates/")
            or path.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.path != path
        ):
            raise ProtocolError("agent update download path is not allowed")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or expected_size > max_bytes
        ):
            raise ProtocolError("agent update size is invalid")

        headers = {
            "X-Agent-ID": state.agent_id,
            "Authorization": f"Bearer {state.agent_secret}",
            "Accept": "application/zip",
        }
        response: requests.Response | None = None
        written = 0
        digest = hashlib.sha256()
        destination_created = False
        deadline = time.monotonic() + self.config.agent_update_total_timeout
        try:
            response = self.session.request(
                "GET",
                self._url(path),
                headers=headers,
                timeout=self.config.request_timeout,
                verify=self._tls_verify,
                stream=True,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise self._http_error("GET", path, response)
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    advertised = int(content_length)
                except ValueError as exc:
                    raise ProtocolError(
                        "agent update Content-Length is invalid"
                    ) from exc
                if advertised != expected_size:
                    raise ProtocolError("agent update Content-Length does not match")

            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                destination_created = True
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if time.monotonic() > deadline:
                        raise ProtocolError("agent update download exceeded its total timeout")
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > expected_size:
                        raise ProtocolError("agent update download exceeded its declared size")
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if written != expected_size:
                raise ProtocolError("agent update download size does not match")
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise ProtocolError("agent update SHA-256 does not match")
            return {"size": written, "sha256": actual_sha256}
        except requests.RequestException as exc:
            if destination_created:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            if isinstance(exc, ControllerRequestError):
                raise
            raise self._network_error(exc) from exc
        except (OSError, ProtocolError):
            if destination_created:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            if response is not None:
                response.close()

    def post_result(
        self, state: AgentState, command_id: str, result: dict[str, Any]
    ) -> str:
        safe_id = quote(command_id, safe="")
        response = self._request(
            "POST",
            f"/api/agent/commands/{safe_id}/result",
            state=state,
            body=result,
        )
        data = self._json_object(response)
        status = data.get("status")
        if not isinstance(status, str) or status not in {
            "queued",
            "dispatched",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
        }:
            raise ProtocolError("command result response has an invalid status")
        return str(status)
