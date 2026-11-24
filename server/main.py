from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import statistics
import tarfile
import threading
import time
import uuid
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .auth import (
    create_session_token,
    hash_api_secret,
    hash_password,
    verify_password,
    verify_session_token,
)
from .config import ControllerInstanceLock, Settings
from .database import Database, INSTALLATION_SETUP_KEY, utcnow
from .miner_adapters.workload import SUPPORTED_WORKLOAD_COINS, WORKLOAD_COIN_MAX_LENGTH, canonical_workload_coin
from .workload import (
    WORKLOAD_RETENTION_DAYS,
    workload_bucket_start,
    WorkloadTimeRangeError,
    _utc_time,
    record_heartbeat_counters,
    acknowledged_heartbeat_counters,
    workload_summary,
)
from .managed_miners import (
    MANAGED_MINER_DISPLAY_NAMES,
    MINER_TYPES,
    ManagedMinerCatalog,
    SRB_GPU_ALGORITHMS,
    managed_package_id,
    managed_test_package_id,
    managed_version_sort_key,
    normalize_algorithm,
    normalize_os_family,
    normalize_version,
    public_adapter_catalog,
)
from .miner_adapters.srb_catalog import srb_dual_pair_supported, validate_periodic_cpu_threads
from .miner_adapters.validation import (
    validate_option_value, parse_pool_endpoint, parse_extra_args,
)
from .miner_adapters.contract import is_retired_miner, retired_miner_type
from .miner_adapters import MINER_ADAPTERS
from .miner_adapters.templates import render_worker_name_template, worker_template_uses_agent_name
from .system_metrics import ControllerMetricsSampler
from .schemas import (
    LAN_NETWORKS,
    AgentReplaceRequest,
    AgentReplacementTokenCreate,
    AgentGpuOcApply,
    AgentGpuOcBatchApply,
    AgentGpuOcBatchClear,
    AgentEnrollRequest,
    AgentReleaseBatchDeployRequest,
    AgentReleaseDeployRequest,
    AgentUpdate,
    CommandCreate,
    CommandResult,
    EnrollmentTokenCreate,
    FlightSheetBatchApplyRequest,
    FlightSheetCreate,
    HeartbeatRequest,
    LoginRequest,
    PasswordChangeRequest,
    InstallationSetupUpdate,
    LinuxSshConfigureRequest,
    MiningAutostartUpdate,
    MinerPackageCreate,
    MinerPackageImport,
    MiningControlBatchRequest,
    UserCreate,
    WalletCreate,
    WalletUpdate,
    PoolCreate,
    PoolUpdate,
    WorkloadSettlementNoteUpdate,
    WorkloadOwnerGroupsUpdate,
)


LOGGER = logging.getLogger("rigpilot.server")
ROLE_LEVEL = {"viewer": 10, "operator": 20, "admin": 30}
DESTRUCTIVE_ACTIONS = {"reboot", "shutdown"}
MINING_CONTROL_ACTIONS = {"stop_mining", "restart_mining", "remove_flight_sheet"}
MINING_RESIDUE_SCAN_ACTION = "scan_mining_residue.v1"
MINING_RESIDUE_CLEANUP_ACTION = "cleanup_mining_residue.v1"
MINING_RESIDUE_ACTIONS = {
    MINING_RESIDUE_SCAN_ACTION,
    MINING_RESIDUE_CLEANUP_ACTION,
}
MINER_CACHE_SCAN_ACTION = "scan_miner_cache.v1"
MINER_CACHE_CLEANUP_ACTION = "cleanup_miner_cache.v1"
MINER_CACHE_ACTIONS = {MINER_CACHE_SCAN_ACTION, MINER_CACHE_CLEANUP_ACTION}
MINER_CACHE_CAPABILITY = "managed_mining.cache_cleanup.v1"
MINING_RESIDUE_REPAIR_CAPABILITY = "managed_mining.residue_repair.v1"
MINING_RESIDUE_SELECTIVE_CAPABILITY = (
    "managed_mining.residue_repair.selective.v1"
)
MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY = (
    "managed_mining.residue_repair.explicit_candidate.v1"
)
MINING_RESULT_BINDING_CAPABILITY = "managed_mining.result_binding.v1"
MINING_CONFIG_FINGERPRINT_CAPABILITY = (
    "managed_mining.config_fingerprint.v1"
)
MANAGED_MINING_CAPABILITY = "managed_mining.dual_process.v1"
EXTRA_ARGS_CAPABILITY = "managed_mining.extra_args.v1"
EXTRA_ARGS_TARGET_MESSAGE = (
    "Extra miner arguments require RigPilot Agent 0.8.31 or newer with "
    "managed_mining.extra_args.v1; upgrade this device first"
)
WSL_GPU_MINING_CAPABILITY = "managed_mining.wsl_gpu.v1"
INDEPENDENT_SRB_GPU_OC_CAPABILITY = (
    "managed_mining.independent_srb_gpu_oc.v1"
)
CONTROLLER_MINING_AUTOSTART_MARKER = "_controller_mining_autostart"
CONTROLLER_MINING_RESULT_BINDING_MARKER = (
    "_controller_expected_flight_sheet_id"
)
CONTROLLER_FLIGHT_DISPATCH_MARKER = "_controller_flight_dispatch_v1"
CONTROLLER_FLIGHT_RUNTIME_MARKER = "_controller_flight_runtime_v1"
CONTROLLER_GPU_OC_INTENT_MARKER = "_controller_gpu_oc_intent"
FLIGHT_RUNTIME_FINGERPRINT_FIELD = "runtime_fingerprint"
MINING_AUTOSTART_REBOOTED_PROCESS_ERROR = (
    "host rebooted; managed miner was not restarted"
)
GPU_OC_STATE_MIGRATION_ACTION = "migrate_gpu_oc_state.v1"
# Actions an operator may cancel while the Agent is already executing them.
# This closes the controller-side ledger and releases the operation channel
# (new commands can be created immediately); it does not interrupt whatever
# the device-side Agent thread is already doing.  Late Agent results for the
# cancelled row are safely ignored by the terminal-status path of the result
# handler (no 409), matching the manual server-side cancellation precedent.
CANCELLABLE_RUNNING_COMMAND_ACTIONS = frozenset({"apply_gpu_profile"})
GPU_OC_CONTROL_RANGES: dict[str, tuple[float | None, float | None, bool]] = {
    "power_limit_w": (None, None, False),
    "core_clock_offset_mhz": (-2000, 2000, True),
    "core_clock_lock_mhz": (100, 5000, True),
    "memory_clock_offset_mhz": (-5000, 5000, True),
    "memory_clock_lock_mhz": (100, 20000, True),
    "fan_percent": (30, 100, True),
}
PACKAGE_CHUNK_BYTES = 1024 * 1024
UPLOAD_REQUEST_OVERHEAD_BYTES = 2 * 1024 * 1024
AGENT_RELEASE_PLATFORMS = frozenset({"windows", "linux"})
AGENT_RELEASE_ARCHITECTURE = "x86_64"
AGENT_UPDATE_CAPABILITY = "agent_update.v1"
AGENT_UPDATE_ACTIVATION_MAX_MINUTES = 45
# Once a replacement Agent reached two authenticated target-version heartbeats,
# a live replacement confirms its activation within seconds.  When the target
# heartbeats stop arriving for this long the replacement has rolled back or
# died, and the UI must stop presenting the job as a normal wait.
AGENT_UPDATE_REPLACEMENT_STALL_MINUTES = 5
# Internal, controller-queued-only forensic follow-up for activation timeouts.
# The payload is a fixed read-only script; operators cannot dispatch this
# action through the public command API, so it widens no command surface.
AGENT_UPDATE_FORENSICS_ACTION = "collect_update_forensics.v1"
AGENT_UPDATE_FORENSICS_TTL_SECONDS = 24 * 60 * 60
AGENT_UPDATE_FORENSICS_COMMANDS = (
    "df -h / /var /tmp",
    "systemctl list-timers | grep rigpilot",
    "journalctl -u rigpilot-agent --since -2h | tail -200",
)
# Downloaded archives are a cache, not the version-selection catalog.  Keep
# disk use bounded while allowing every discovered/categorized version to
# remain selectable and to be downloaded again on demand.
MANAGED_MINER_CACHE_VERSIONS = 3


def _is_verified_rebooted_miner_stop_error(value: object) -> bool:
    """Accept only the fixed single- or two-role host reboot sentinel."""

    if value == MINING_AUTOSTART_REBOOTED_PROCESS_ERROR:
        return True
    if not isinstance(value, str):
        return False
    primary = f"primary: {MINING_AUTOSTART_REBOOTED_PROCESS_ERROR}"
    secondary = f"secondary: {MINING_AUTOSTART_REBOOTED_PROCESS_ERROR}"
    return value in {
        f"{primary}; {secondary}",
        f"{secondary}; {primary}",
    }


def _manual_managed_classification(
    miner_type: str | None,
    os_family: str | None,
    variant: str | None,
    channel: str | None,
    version: str,
    archive_type: str,
) -> tuple[str, str, str | None, str] | None:
    if str(miner_type or "").strip().lower() == "hiveos":
        # HiveOS support was removed in Controller 0.8.48; historical
        # packages and flight sheets stay readable, but no new ones are made.
        raise HTTPException(
            422,
            "HiveOS custom packages are no longer supported; use a managed miner package",
        )
    values = (miner_type, os_family, variant, channel)
    if all(value is None for value in values):
        return None
    if miner_type is None or os_family is None or channel is None:
        raise HTTPException(
            422,
            "Categorized test packages require miner type, target platform and test channel",
        )
    normalized_type = miner_type.strip().lower()
    if normalized_type not in MINER_TYPES:
        raise HTTPException(422, "Unsupported miner package category")
    try:
        normalized_os = normalize_os_family(os_family)
        normalize_version(version, normalized_type)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if channel != "test":
        raise HTTPException(422, "Manually categorized packages must use the test channel")
    expected_archive = "zip" if normalized_os == "windows" else "tar.gz"
    if archive_type != expected_archive:
        raise HTTPException(
            422,
            f"{normalized_os} test packages must use {expected_archive}",
        )
    normalized_variant = (variant or "").strip().lower() or None
    if normalized_type == "pearlminer" and normalized_os == "linux":
        if normalized_variant not in {"nvidia", "amd"}:
            raise HTTPException(
                422,
                "Pearl Miner Linux test packages require an NVIDIA or AMD variant",
            )
    elif normalized_variant is not None:
        raise HTTPException(
            422,
            "Only Pearl Miner Linux packages use a GPU variant",
        )
    try:
        MINER_ADAPTERS.require(normalized_type).asset(normalized_os, normalized_variant)
    except ValueError as exc:
        raise HTTPException(422, "Miner package platform is unsupported") from exc
    return normalized_type, normalized_os, normalized_variant, "test"
LINUX_SSH_MANAGE_CAPABILITY = "remote_access.ssh_manage.v1"
LINUX_SSH_CONFIGURE_ACTION = "configure_linux_ssh.v1"
# Keep old workspace commands out of the dispatch queue and redact their contents.
_RETIRED_WORKSPACE_ACTIONS = ("remote_exec.v1", "remote_file.v1")
DEVICE_STATE_TRANSITION_ACTIONS = frozenset(
    {
        "apply_flight_sheet",
        "stop_mining",
        "restart_mining",
        "remove_flight_sheet",
        MINING_RESIDUE_SCAN_ACTION,
        MINING_RESIDUE_CLEANUP_ACTION,
        MINER_CACHE_SCAN_ACTION,
        MINER_CACHE_CLEANUP_ACTION,
        "apply_gpu_profile",
        GPU_OC_STATE_MIGRATION_ACTION,
        "restart_service",
        "reboot",
        "shutdown",
        AGENT_UPDATE_CAPABILITY,
        LINUX_SSH_CONFIGURE_ACTION,
    }
)


class _RequestBodyTooLarge(Exception):
    pass


AGENT_REQUEST_BODY_LIMIT_BYTES = 300_000


class RequestBodyLimitMiddleware:
    """Enforce body ceilings while ASGI request chunks are being consumed."""

    def __init__(self, app: ASGIApp, *, settings: Settings):
        self.app = app
        self.settings = settings

    def _limit_for(self, method: str, path: str, query_string: bytes = b"") -> int | None:
        if method not in {"POST", "PUT", "PATCH"} or not path.startswith("/api/"):
            return None
        if path == "/api/agent-releases/upload":
            return max(1, self.settings.max_agent_release_bytes) + UPLOAD_REQUEST_OVERHEAD_BYTES
        if path == "/api/miner-packages/upload":
            return max(1, self.settings.max_miner_package_bytes) + UPLOAD_REQUEST_OVERHEAD_BYTES
        if path.startswith("/api/agent/"):
            return AGENT_REQUEST_BODY_LIMIT_BYTES
        return 1_000_000

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(
            str(scope.get("method") or "").upper(), str(scope.get("path") or ""),
            scope.get("query_string", b""),
        )
        if limit is None:
            await self.app(scope, receive, send)
            return

        declared_values = [
            value
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if len(declared_values) > 1:
            response = JSONResponse(
                status_code=400,
                content={"detail": "Duplicate Content-Length"},
            )
            await response(scope, receive, send)
            return
        declared = declared_values[0] if declared_values else None
        if declared is not None:
            try:
                declared_size = int(declared)
                if declared_size < 0:
                    raise ValueError
            except (TypeError, ValueError):
                response = JSONResponse(
                    status_code=400, content={"detail": "Invalid Content-Length"}
                )
                await response(scope, receive, send)
                return
            if declared_size > limit:
                response = JSONResponse(
                    status_code=413, content={"detail": "Request body too large"}
                )
                await response(scope, receive, send)
                return

        path = str(scope.get("path") or "")
        if path not in {"/api/agent-releases/upload", "/api/miner-packages/upload"}:
            buffered: list[Message] = []
            consumed = 0
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] == "http.request":
                    consumed += len(message.get("body", b""))
                    if consumed > limit:
                        response = JSONResponse(
                            status_code=413,
                            content={"detail": "Request body too large"},
                        )
                        await response(scope, receive, send)
                        return
                    if not message.get("more_body", False):
                        break
                elif message["type"] == "http.disconnect":
                    break

            next_message = 0

            async def replay_receive() -> Message:
                nonlocal next_message
                if next_message < len(buffered):
                    message = buffered[next_message]
                    next_message += 1
                    return message
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send)
            return

        consumed = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            response = JSONResponse(
                status_code=413, content={"detail": "Request body too large"}
            )
            await response(scope, receive, send)
# Two different Linux binaries were distributed as 0.6.6 while the updater
# bridge was being repaired.  The original build cannot start its protected
# helper and cannot be distinguished from the corrected build by semantic
# version alone.  Fail closed for every 0.6.6 device instead of offering an
# update that may download successfully and then stall before readiness.
LINUX_AGENT_ONLINE_UPDATE_MIN_SOURCE = (0, 6, 7)
WINDOWS_AGENT_RELEASE_REQUIRED_FILES = frozenset(
    {
        "RigPilotAgent.exe",
        "RigPilotAgentUpdater.ps1",
        "portable-agent.ps1",
        "Start-RigPilotAgent.bat",
    }
)
LINUX_AGENT_RELEASE_REQUIRED_FILES = frozenset({"RigPilotAgent"})
AGENT_RELEASE_MANIFEST = "agent-release.json"
AGENT_RELEASE_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
AGENT_RELEASE_ALLOWED_COMPRESSION = frozenset(
    {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
)
WINDOWS_RESERVED_PATH_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
LIVE_LOG_MIN_CREATE_INTERVAL_SECONDS = 2
LIVE_LOG_TRANSIENT_RETENTION_MINUTES = 15
ENROLLMENT_TOKEN_METADATA_RETENTION_DAYS = 30
TERMINAL_COMMAND_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "expired"}
)
_HASHRATE_UNIT_FACTORS = {
    "h/s": 1.0,
    "kh/s": 1_000.0,
    "mh/s": 1_000_000.0,
    "gh/s": 1_000_000_000.0,
    "th/s": 1_000_000_000_000.0,
    "ph/s": 1_000_000_000_000_000.0,
    "eh/s": 1_000_000_000_000_000_000.0,
}


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that pins a pre-validated IP while preserving SNI."""

    def __init__(self, hostname: str, port: int, connect_ip: str, timeout: float):
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_ip = connect_ip
        self._package_deadline: float | None = None

    def connect(self) -> None:
        timeout = self.timeout
        if self._package_deadline is not None:
            timeout = min(timeout, _package_time_remaining(self._package_deadline))
        sock = socket.create_connection(
            (self._connect_ip, self.port),
            timeout=timeout,
            source_address=self.source_address,
        )
        try:
            if self._package_deadline is not None:
                # TLS copies the raw socket timeout and applies it to the whole
                # handshake. Deduct TCP time before handing that budget to TLS.
                sock.settimeout(min(self.timeout, _package_time_remaining(self._package_deadline)))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _public_addresses(hostname: str, port: int) -> list[str]:
    """Resolve once and reject hosts with any non-public address."""
    try:
        answers = socket.getaddrinfo(
            hostname, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise HTTPException(422, "download_url hostname could not be resolved") from exc
    addresses: list[str] = []
    for answer in answers:
        value = answer[4][0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise HTTPException(422, "download_url resolved to an invalid address") from exc
        if (not address.is_global or address.is_multicast or address.is_reserved
                or address.is_unspecified or address.is_loopback or address.is_link_local):
            raise HTTPException(
                422, "download_url must not resolve to a private or special-use address"
            )
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise HTTPException(422, "download_url hostname has no usable address")
    return addresses


_PACKAGE_DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="package-dns")
_PACKAGE_DNS_SLOTS = threading.BoundedSemaphore(4)


def _package_time_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HTTPException(504, "miner package import timed out")
    return remaining


def _package_addresses_before_deadline(hostname: str, port: int, deadline: float) -> list[str]:
    if not _PACKAGE_DNS_SLOTS.acquire(timeout=_package_time_remaining(deadline)):
        raise HTTPException(504, "miner package DNS lookup timed out")
    try:
        future = _PACKAGE_DNS_POOL.submit(_public_addresses, hostname, port)
    except BaseException:
        _PACKAGE_DNS_SLOTS.release()
        raise
    future.add_done_callback(lambda completed: _PACKAGE_DNS_SLOTS.release())
    try:
        return future.result(timeout=_package_time_remaining(deadline))
    except FutureTimeout as exc:
        future.cancel()
        raise HTTPException(504, "miner package DNS lookup timed out") from exc


def _package_https_target(value: str):
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value) or "\\\\" in value:
        raise HTTPException(422, "download_url must not contain whitespace or controls")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = 443 if parsed.port is None else parsed.port
    except ValueError as exc:
        raise HTTPException(422, "download_url is malformed") from exc
    if (not hostname or parsed.scheme.lower() != "https"
            or parsed.username or parsed.password or parsed.fragment or not 1 <= port <= 65535):
        raise HTTPException(422, "download_url must be credential-free HTTPS")
    return parsed, hostname, port


def _download_https_package(
    download_url: str,
    destination: Path,
    *,
    max_bytes: int,
    timeout_seconds: int,
    redirect_hosts: frozenset[str] = frozenset(),
    max_redirects: int = 0,
    storage_check: Callable[[int], None] | None = None,
    allow_public_redirects: bool = False,
) -> tuple[int, str]:
    """Pinned public HTTPS; one deadline covers DNS, headers, body and redirects.

    Existing callers retain default no-redirect or exact catalog-host policies.
    Only the explicit Custom quick-create flow enables public HTTPS redirects.
    No ambient requests/proxy configuration participates.
    """
    deadline = time.monotonic() + max(1, timeout_seconds)
    current_url = download_url
    allowed_hosts = frozenset(item.lower().rstrip(".") for item in redirect_hosts)
    succeeded = False
    try:
        for redirect_count in range(max_redirects + 1):
            parsed, hostname, port = _package_https_target(current_url)
            _package_time_remaining(deadline)
            addresses = _package_addresses_before_deadline(hostname, port, deadline)
            request_path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
            if parsed.query:
                request_path += "?" + quote(parsed.query, safe="=&%:@!$'()*+,;/?-._~")
            authority_host = f"[{hostname}]" if ":" in hostname else hostname
            authority = authority_host if port == 443 else f"{authority_host}:{port}"
            last_error: Exception | None = None
            next_url = None
            for address in addresses:
                connection = None
                timer = None
                expired = threading.Event()
                socket_holder = {}
                try:
                    remaining = _package_time_remaining(deadline)
                    connection = _PinnedHTTPSConnection(hostname, port, address, timeout=min(15.0, remaining))
                    connection._package_deadline = deadline
                    remaining = _package_time_remaining(deadline)
                    def abort_connection(conn=connection, holder=socket_holder, flag=expired):
                        flag.set()
                        # Keep the socket object even if HTTPConnection closes its
                        # reference for a Connection: close response's file reader.
                        sock = holder.get("socket") or getattr(conn, "sock", None)
                        if sock is not None:
                            with suppress(OSError, AttributeError):
                                sock.shutdown(socket.SHUT_RDWR)
                        with suppress(Exception):
                            conn.close()
                    timer = threading.Timer(remaining, abort_connection)
                    timer.daemon = True
                    timer.start()
                    connection.request("GET", request_path, headers={
                        "Host": authority, "User-Agent": "RigPilot-Package-Importer/1.0",
                        "Accept": "application/octet-stream", "Accept-Encoding": "identity",
                        "Connection": "close",
                    })
                    socket_holder["socket"] = connection.sock
                    _package_time_remaining(deadline)
                    response = connection.getresponse()
                    _package_time_remaining(deadline)
                    if 300 <= response.status < 400:
                        location = response.getheader("Location")
                        if not location or not (allow_public_redirects or allowed_hosts):
                            raise HTTPException(422, "miner package URL redirects are not allowed")
                        if redirect_count >= max_redirects:
                            raise HTTPException(422, "miner package URL has too many redirects")
                        if any(c.isspace() or ord(c) < 32 for c in location):
                            raise HTTPException(422, "miner package redirect target is malformed")
                        try:
                            candidate = urljoin(current_url, location)
                        except ValueError as exc:
                            raise HTTPException(422, "miner package redirect target is malformed") from exc
                        target, target_host, _ = _package_https_target(candidate)
                        if not allow_public_redirects and target_host.lower().rstrip(".") not in allowed_hosts:
                            raise HTTPException(422, "miner package redirect target is not allowed")
                        next_url = candidate
                        break
                    if response.status != 200:
                        raise HTTPException(502, f"miner package server returned HTTP {response.status}")
                    if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                        raise HTTPException(502, "miner package server used content encoding")
                    declared = response.getheader("Content-Length")
                    declared_size = None
                    if declared is not None:
                        if not re.fullmatch(r"[0-9]+", declared):
                            raise HTTPException(502, "invalid package Content-Length")
                        declared_size = int(declared)
                        if declared_size > max_bytes:
                            raise HTTPException(413, "miner package exceeds the upload limit")
                    storage_budget = declared_size if declared_size is not None else max_bytes
                    if storage_check is not None:
                        storage_check(storage_budget)
                    checksum, size = hashlib.sha256(), 0
                    with destination.open("wb") as stream:
                        while True:
                            remaining = _package_time_remaining(deadline)
                            sock = socket_holder.get("socket")
                            if sock is not None:
                                sock.settimeout(min(15.0, remaining))
                            # HTTPResponse.read may aggregate many slow receives.
                            # read1 returns after a bounded receive to recheck time.
                            reader = getattr(response, "read1", None) or response.read
                            chunk = reader(PACKAGE_CHUNK_BYTES)
                            _package_time_remaining(deadline)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                raise HTTPException(413, "miner package exceeds the upload limit")
                            if declared_size is not None and size > declared_size:
                                raise HTTPException(502, "package Content-Length does not match its body")
                            if storage_check is not None:
                                storage_check(max(len(chunk), storage_budget - size + len(chunk)))
                            checksum.update(chunk)
                            stream.write(chunk)
                    if declared_size is not None and size != declared_size:
                        raise HTTPException(502, "package Content-Length does not match its body")
                    if size == 0:
                        raise HTTPException(422, "miner package is empty")
                    succeeded = True
                    return size, checksum.hexdigest()
                except HTTPException:
                    raise
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    if expired.is_set() or time.monotonic() >= deadline:
                        raise HTTPException(504, "miner package import timed out") from exc
                    last_error = exc
                    destination.unlink(missing_ok=True)
                finally:
                    if timer is not None:
                        timer.cancel()
                    if connection is not None:
                        with suppress(Exception):
                            connection.close()
            if next_url is None:
                raise HTTPException(502, "could not download miner package over HTTPS") from last_error
            current_url = next_url
        raise HTTPException(422, "miner package URL has too many redirects")
    finally:
        if not succeeded:
            destination.unlink(missing_ok=True)


def _safe_package_filename(
    value: str,
    archive_type: Literal["zip", "tar.gz"],
    *,
    require_matching_extension: bool,
) -> str:
    raw_name = unquote(value).replace("\\", "/").rsplit("/", 1)[-1].strip()
    expected = ".zip" if archive_type == "zip" else ".tar.gz"
    lowered = raw_name.lower()
    matches = lowered.endswith(".zip") if archive_type == "zip" else lowered.endswith(
        (".tar.gz", ".tgz")
    )
    if require_matching_extension and not matches:
        raise HTTPException(422, f"filename must end with {expected}")
    if not raw_name or not matches:
        raw_name = "miner-package" + expected
    safe = "".join(
        character if (character.isalnum() or character in "._+-") else "_"
        for character in raw_name
    ).lstrip(".")
    suffix = ".tgz" if safe.lower().endswith(".tgz") else expected
    if safe.lower().endswith(suffix):
        stem = safe[: -len(suffix)]
    else:
        stem = safe
    stem = (stem[:140].strip(". ") or "miner-package")
    if stem.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        stem = "_" + stem
    return stem + suffix


class LoginRateLimiter:
    def __init__(
        self,
        attempts: int = 8,
        window_seconds: int = 60,
        *,
        ip_attempts: int = 32,
        max_keys: int = 4_096,
        max_ips: int = 2_048,
    ):
        self.attempts = attempts
        self.ip_attempts = max(attempts, ip_attempts)
        self.window_seconds = window_seconds
        self.max_keys = max(1, max_keys)
        self.max_ips = max(1, max_ips)
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._inflight: defaultdict[str, int] = defaultdict(int)
        self._ip_events: dict[str, deque[float]] = defaultdict(deque)
        self._ip_inflight: defaultdict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    @staticmethod
    def _key(ip: str, username: str) -> str:
        return f"{ip}\0{username.strip().casefold()}"

    def _prune_locked(self, now: float) -> None:
        stale_keys: list[str] = []
        for key, events in self._events.items():
            while events and now - events[0] > self.window_seconds:
                events.popleft()
            if not events and self._inflight.get(key, 0) == 0:
                stale_keys.append(key)
        for key in stale_keys:
            self._events.pop(key, None)

        stale_ips: list[str] = []
        for ip, events in self._ip_events.items():
            while events and now - events[0] > self.window_seconds:
                events.popleft()
            if not events and self._ip_inflight.get(ip, 0) == 0:
                stale_ips.append(ip)
        for ip in stale_ips:
            self._ip_events.pop(ip, None)

    @staticmethod
    def _reserve_capacity_locked(
        events_by_key: dict[str, deque[float]],
        inflight_by_key: dict[str, int],
        key: str,
        maximum: int,
    ) -> bool:
        tracked = set(events_by_key) | set(inflight_by_key)
        if key in tracked or len(tracked) < maximum:
            return True
        candidates = [
            candidate
            for candidate, events in events_by_key.items()
            if events and inflight_by_key.get(candidate, 0) == 0
        ]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda candidate: events_by_key[candidate][0])
        events_by_key.pop(oldest, None)
        return True

    def check(self, ip: str, username: str) -> None:
        now = time.monotonic()
        key = self._key(ip, username)
        with self._lock:
            self._prune_locked(now)
            capacity_available = self._reserve_capacity_locked(
                self._events, self._inflight, key, self.max_keys
            ) and self._reserve_capacity_locked(
                self._ip_events, self._ip_inflight, ip, self.max_ips
            )
            if (
                not capacity_available
                or len(self._events.get(key, ())) + self._inflight.get(key, 0)
                >= self.attempts
                or len(self._ip_events.get(ip, ())) + self._ip_inflight.get(ip, 0)
                >= self.ip_attempts
            ):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts. Try again shortly.",
                    headers={"Retry-After": str(self.window_seconds)},
                )
            self._inflight[key] += 1
            self._ip_inflight[ip] += 1

    @staticmethod
    def _finish_attempt_locked(counters: dict[str, int], key: str) -> None:
        remaining = counters.get(key, 0) - 1
        if remaining > 0:
            counters[key] = remaining
        else:
            counters.pop(key, None)

    def record_failure(self, ip: str, username: str) -> None:
        now = time.monotonic()
        key = self._key(ip, username)
        with self._lock:
            self._prune_locked(now)
            self._finish_attempt_locked(self._inflight, key)
            self._finish_attempt_locked(self._ip_inflight, ip)
            self._events[key].append(now)
            self._ip_events[ip].append(now)

    def record_success(self, ip: str, username: str) -> None:
        # Clear only the successful account/IP pair.  A valid low-privilege
        # account must not reset failed attempts against another username.
        key = self._key(ip, username)
        with self._lock:
            self._finish_attempt_locked(self._inflight, key)
            self._finish_attempt_locked(self._ip_inflight, ip)
            self._events.pop(key, None)

    def abandon(self, ip: str, username: str) -> None:
        key = self._key(ip, username)
        with self._lock:
            self._finish_attempt_locked(self._inflight, key)
            self._finish_attempt_locked(self._ip_inflight, ip)


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _effective_pool_password(value: str | None) -> str:
    """Apply the legacy default without rewriting an explicit empty secret."""

    return "x" if value is None else value


def _desired_gpu_oc_entries(value: str | None) -> list[dict[str, Any]]:
    """Decode only the bounded controller-owned per-GPU desired state."""

    raw = _json(value, [])
    if not isinstance(raw, list) or len(raw) > 32:
        return []
    result: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()
    for entry in raw:
        if (
            not isinstance(entry, dict)
            or not {"gpu_uuid", "name", "setting"} <= set(entry)
            or set(entry) - {"gpu_uuid", "name", "setting", "intent_id", "replay_block"}
        ):
            return []
        gpu_uuid = entry.get("gpu_uuid")
        name = entry.get("name")
        setting = entry.get("setting")
        if (
            not isinstance(gpu_uuid, str)
            or not 4 <= len(gpu_uuid) <= 128
            or any(ord(character) < 32 or ord(character) == 127 for character in gpu_uuid)
            or not isinstance(name, str)
            or not 1 <= len(name) <= 80
            or not isinstance(setting, dict)
        ):
            return []
        normalized = _normalized_legacy_gpu_oc_setting(setting)
        if normalized is None or gpu_uuid in seen_uuids:
            return []
        seen_uuids.add(gpu_uuid)
        decoded = {"gpu_uuid": gpu_uuid, "name": name, "setting": normalized}
        if "intent_id" in entry:
            intent_id = entry["intent_id"]
            if not isinstance(intent_id, str) or re.fullmatch(r"(?:[0-9a-f]{32}|legacy-[0-9a-f]{64})", intent_id) is None:
                return []
            decoded["intent_id"] = intent_id
        if "replay_block" in entry:
            block = entry["replay_block"]
            if (
                not isinstance(block, dict)
                or not isinstance(block.get("reason"), str)
                or block["reason"] not in {"hardware_reset_required", "hardware_fault", "recovery_failed", "state_persistence_failed", "legacy_reset_required"}
                or not all(isinstance(block.get(key), str) for key in ("intent_id", "desired_fingerprint", "command_id", "error", "recovery_status"))
                or not all(type(block.get(key)) is bool for key in ("recovered", "state_reconciled", "hardware_reset_required"))
                or not isinstance(block.get("confirmed_reset"), list)
                or not isinstance(block.get("nvml_codes"), list)
            ):
                return []
            decoded["replay_block"] = block
        result.append(decoded)
    return sorted(result, key=lambda item: int(item["setting"]["index"]))


def _desired_gpu_oc_pending_uuids(value: str | None) -> list[str]:
    """Decode the bounded UUID-only OC retry set."""

    raw = _json(value, [])
    if not isinstance(raw, list) or len(raw) > 32:
        return []
    result: list[str] = []
    for gpu_uuid in raw:
        if (
            not isinstance(gpu_uuid, str)
            or not 4 <= len(gpu_uuid) <= 128
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in gpu_uuid
            )
        ):
            return []
        if gpu_uuid not in result:
            result.append(gpu_uuid)
    return result


def _normalized_legacy_gpu_oc_setting(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) - ({"index"} | set(GPU_OC_CONTROL_RANGES)):
        return None
    index = value.get("index")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or not 0 <= index <= 31
        or not set(value) - {"index"}
    ):
        return None
    normalized: dict[str, Any] = {"index": index}
    for field in GPU_OC_CONTROL_RANGES:
        if field not in value:
            continue
        minimum, maximum, integer_only = GPU_OC_CONTROL_RANGES[field]
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        try:
            numeric = float(item)
        except OverflowError:
            return None
        if (
            not math.isfinite(numeric)
            or (field == "power_limit_w" and numeric <= 0)
            or (minimum is not None and numeric < minimum)
            or (maximum is not None and numeric > maximum)
            or (integer_only and not isinstance(item, int))
        ):
            return None
        normalized[field] = item
    return normalized


def _gpu_oc_digest(value: Any) -> str:
    def canonical(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: canonical(value) for key, value in item.items()}
        if isinstance(item, list):
            return [canonical(value) for value in item]
        if isinstance(item, float) and item.is_integer():
            return int(item)
        return item
    return hashlib.sha256(json.dumps(
        canonical(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _gpu_oc_desired_fingerprint(entry: dict[str, Any]) -> str:
    return _gpu_oc_digest({
        "gpu_uuid": entry["gpu_uuid"],
        "setting": {key: value for key, value in entry["setting"].items() if key != "index"},
    })


def _gpu_oc_intent_marker(entry: dict[str, Any]) -> dict[str, str]:
    fingerprint = _gpu_oc_desired_fingerprint(entry)
    return {
        "gpu_uuid": entry["gpu_uuid"],
        "intent_id": entry.get("intent_id") or f"legacy-{fingerprint}",
        "desired_fingerprint": fingerprint,
    }


def _gpu_oc_replay_is_blocked(entry: dict[str, Any]) -> bool:
    block = entry.get("replay_block")
    marker = _gpu_oc_intent_marker(entry)
    return bool(
        isinstance(block, dict)
        and block.get("intent_id") == marker["intent_id"]
        and block.get("desired_fingerprint") == marker["desired_fingerprint"]
    )


def _gpu_oc_command_matches_intent(payload: Any, entry: dict[str, Any]) -> bool:
    if (
        not isinstance(payload, dict) or payload.get("vendor") != "nvidia"
        or not isinstance(payload.get("mode"), str)
        or payload["mode"] not in {"replace_one", "reset_one"}
        or payload.get("expected_gpu_uuid") != entry["gpu_uuid"]
        or not isinstance(payload.get("settings"), list) or len(payload["settings"]) != 1
    ):
        return False
    setting = _normalized_legacy_gpu_oc_setting(payload["settings"][0])
    if setting is None or _gpu_oc_desired_fingerprint({
        "gpu_uuid": entry["gpu_uuid"], "setting": setting,
    }) != _gpu_oc_desired_fingerprint(entry):
        return False
    marker = payload.get(CONTROLLER_GPU_OC_INTENT_MARKER)
    # A pre-upgrade command may acknowledge only a pre-upgrade desired entry.
    # Explicit saves always mint a generation, including identical values.
    if marker is None:
        return entry.get("intent_id") in (None, f"legacy-{_gpu_oc_desired_fingerprint(entry)}")
    return marker == _gpu_oc_intent_marker(entry)


def _legacy_gpu_oc_reset_required(error: Any, index: int) -> bool:
    """Recognize only the old backend's exact same-card NVML16 receipts."""
    if not isinstance(error, str) or not error:
        return False
    text = error.removeprefix("GPU OC transition failed: ")
    # Old fan compensation wraps another exact backend error. Verify every
    # wrapper's GPU before unwrapping; a different card must never be held.
    fan_wrapper = r"(?:set|reset) GPU ([0-9]+) fans failed: "
    if any(int(match.group(1)) != index for match in re.finditer(fan_wrapper, text)):
        return False
    text = re.sub(fan_wrapper, "", text)
    text = text.replace("; rollback also failed: ", "; ")
    labels = {
        "power_limit_w": ("power limit", "power limits", "default power limit"),
        "core_clock_offset_mhz": ("core clock offset", "core clock offset limits"),
        "memory_clock_offset_mhz": ("memory clock offset", "memory clock offset limits"),
        "core_clock_lock_mhz": ("core clock lock", "maximum core clock lock"),
        "memory_clock_lock_mhz": ("memory clock lock", "maximum memory clock lock"),
        "fan_percent": ("fan policy", "fan percent", "fan speed", "fans", "fan limits"),
    }
    known_labels = {label for values in labels.values() for label in values} | set(GPU_OC_CONTROL_RANGES)
    observed_reset = False
    range_query = False
    for part in text.split("; "):
        continuation = re.fullmatch(r"(replacement|legacy) query: (GPU requires reset|Not Supported)", part)
        if continuation:
            if not range_query:
                return False
            observed_reset |= continuation.group(2) == "GPU requires reset"
            continue
        match = re.fullmatch(
            r"(query|set|reset|restore) GPU ([0-9]+) ([a-z_0-9 ]+) failed: (.+)", part
        )
        if match is None or int(match.group(2)) != index:
            return False
        operation, label, message = match.group(1), match.group(3), match.group(4)
        fan = re.fullmatch(r"fan ([0-9]+)( speed)?", label)
        if fan is not None:
            if int(fan.group(1)) > 31 or (operation == "query") != bool(fan.group(2)):
                return False
            label = "fan speed"
        elif operation == "restore":
            return False
        if label not in known_labels:
            return False
        range_query = operation == "query" and label in {
            "core clock offset limits", "memory clock offset limits",
        }
        if range_query:
            first = re.fullmatch(r"(replacement|legacy) query: (GPU requires reset|Not Supported)", message)
            if first is None:
                return False
            observed_reset |= first.group(2) == "GPU requires reset"
        elif message in {"GPU requires reset", "Not Supported"}:
            observed_reset |= message == "GPU requires reset"
        else:
            return False
        # replace_one first resets prior UUID-owned fields, which may have
        # been cleared from the new request. The outer binder verifies the
        # complete request and generation; every error here still binds GPU.
    return observed_reset


def _gpu_oc_failure_replay_block(
    command: sqlite3.Row | dict[str, Any],
    payload: Any,
    result: Any,
    error: Any,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    """Bind positive unsafe-failure evidence to one current desired generation."""
    if (
        command["action"] != "apply_gpu_profile"
        or not _gpu_oc_command_matches_intent(payload, entry)
    ):
        return None
    setting = _normalized_legacy_gpu_oc_setting(payload["settings"][0])
    assert setting is not None
    index, gpu_uuid = setting["index"], entry["gpu_uuid"]
    recovery = result.get("gpu_oc_recovery") if isinstance(result, dict) else None
    marker = _gpu_oc_intent_marker(entry)
    if isinstance(result, dict) and "gpu_oc_recovery" not in result:
        if not _legacy_gpu_oc_reset_required(error, index):
            return None
        return {
            **{key: marker[key] for key in ("intent_id", "desired_fingerprint")},
            "command_id": command["id"], "reason": "legacy_reset_required",
            "error": error, "recovery_status": "unknown",
            "recovered": False, "state_reconciled": False,
            "hardware_reset_required": True, "confirmed_reset": [], "nvml_codes": [],
            "hardware_identity_confirmed": False,
        }
    if not isinstance(recovery, dict):
        return None
    statuses = {"rolled_back", "defaults_restored", "partial", "failed"}
    booleans = (
        "hardware_reset_required", "reset_required", "hardware_fault", "write_attempted",
        "state_reconciled", "recovered",
    )
    if (
        not isinstance(recovery.get("status"), str) or recovery["status"] not in statuses
        or any(type(recovery.get(key)) is not bool for key in booleans)
        or recovery.get("command_id") != command["id"]
        or not isinstance(recovery.get("errors"), list) or len(recovery["errors"]) > 8
        or any(not isinstance(item, str) or len(item) > 256 for item in recovery["errors"])
        or not isinstance(recovery.get("nvml_codes"), list) or len(recovery["nvml_codes"]) > 16
        or any(type(code) is not int or not 0 <= code <= 999 for code in recovery["nvml_codes"])
        or ("persistence_failed" in recovery and type(recovery["persistence_failed"]) is not bool)
        or recovery["hardware_fault"] != bool({15, 16}.intersection(recovery["nvml_codes"]))
        or recovery["reset_required"] != (16 in recovery["nvml_codes"])
        or recovery["hardware_reset_required"] != (16 in recovery["nvml_codes"])
    ):
        return None
    hardware_fault = recovery.get("hardware_fault") is True
    request_intent = {
        "vendor": "nvidia", "mode": payload["mode"], "settings": [setting],
        "expected_gpu_uuid": gpu_uuid,
    }
    setting_intent = {
        "vendor": "nvidia", "mode": payload["mode"], "index": index,
        "setting": setting, "expected_gpu_uuid": gpu_uuid,
    }
    targets = recovery.get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        return None
    row = targets[0]
    expected_target = {
        "index": index, "setting_fingerprint": _gpu_oc_digest(setting_intent),
        "requested_gpu_uuid": gpu_uuid, "identity_verified": False,
    }
    if (
        recovery.get("request_fingerprint") != _gpu_oc_digest(request_intent)
        or row != expected_target or type(row.get("index")) is not int
        or row.get("identity_verified") is not False
    ):
        return None
    target = recovery.get("failure_target")
    if target is not None and (
        not isinstance(target, dict)
        or set(target) - {"index", "operation", "field", "requested_gpu_uuid", "identity_verified", "phase"}
        or type(target.get("index")) is not int or target["index"] != index
        or target.get("requested_gpu_uuid") != gpu_uuid or target.get("identity_verified") is not False
        or not isinstance(target.get("operation"), str)
        or target["operation"] not in {"query", "apply", "reset"}
        or ("field" in target and (not isinstance(target["field"], str) or target["field"] not in GPU_OC_CONTROL_RANGES))
        or ("phase" in target and target["phase"] != "identity_verification")
    ):
        return None
    confirmed = recovery.get("confirmed_reset")
    if not isinstance(confirmed, list) or len(confirmed) > 32:
        return None
    early_identity = "phase" in recovery or "identity_verified" in recovery
    if early_identity and (
        recovery.get("phase") != "identity_verification"
        or recovery.get("identity_verified") is not False
        or recovery.get("state_unchanged") is not True
        or recovery["state_reconciled"] is not True
        or not isinstance(target, dict) or target.get("phase") != "identity_verification"
        or target["operation"] != "query" or recovery["write_attempted"] or confirmed
        or recovery.get("persistence_failed") is True
    ):
        return None
    for row in confirmed:
        if (
            not isinstance(row, dict) or set(row) != {"index", "gpu_uuid", "reset"}
            or type(row.get("index")) is not int or row["index"] != index
            or row.get("gpu_uuid") != gpu_uuid
            or not isinstance(row.get("reset"), list) or not row["reset"]
            or any(not isinstance(field, str) or field not in GPU_OC_CONTROL_RANGES for field in row["reset"])
            or len(row["reset"]) != len(set(row["reset"]))
        ):
            return None
    # The contract's recovered bit is derived, never a standalone health claim.
    recovered = (
        recovery["status"] in {"rolled_back", "defaults_restored"}
        and not recovery["hardware_reset_required"]
        and not recovery["reset_required"] and not hardware_fault
        and not recovery["errors"] and recovery["state_reconciled"]
        and recovery.get("persistence_failed") is not True
    )
    if recovery["recovered"] != recovered:
        return None
    if recovery["hardware_reset_required"] or recovery["reset_required"]:
        reason = "hardware_reset_required"
    elif hardware_fault:
        reason = "hardware_fault"
    elif recovery.get("persistence_failed") is True:
        reason = "state_persistence_failed"
    elif (
        not recovery["state_reconciled"] and (recovery["write_attempted"] or confirmed)
    ) or (
        recovery["write_attempted"] and not recovery["recovered"]
        and (recovery["status"] in {"partial", "failed"} or recovery["errors"])
    ):
        reason = "recovery_failed"
    else:
        return None
    return {
        **{key: marker[key] for key in ("intent_id", "desired_fingerprint")},
        "command_id": command["id"], "reason": reason,
        "error": error if isinstance(error, str) else "",
        "recovery_status": recovery["status"], "recovered": recovery["recovered"],
        "state_reconciled": recovery["state_reconciled"],
        "hardware_reset_required": recovery["hardware_reset_required"] or recovery["reset_required"],
        "confirmed_reset": confirmed, "nvml_codes": recovery["nvml_codes"],
        "hardware_identity_confirmed": False,
    }


def _reported_legacy_gpu_oc_settings(inventory: Any) -> list[dict[str, Any]] | None:
    if not isinstance(inventory, dict):
        return None
    mining_status = inventory.get("mining_status")
    profiles = mining_status.get("gpu_oc_profiles") if isinstance(mining_status, dict) else None
    if not isinstance(profiles, list):
        return None
    for profile in profiles[:64]:
        if (
            not isinstance(profile, dict)
            or profile.get("source") != "nvml"
            or profile.get("state") != "applied_record"
            or profile.get("state_version") != 1
            or profile.get("identity_complete") is not False
        ):
            continue
        raw_settings = profile.get("settings")
        if not isinstance(raw_settings, list) or not 1 <= len(raw_settings) <= 32:
            return None
        normalized = [_normalized_legacy_gpu_oc_setting(item) for item in raw_settings]
        if any(item is None for item in normalized):
            return None
        settings = [dict(item) for item in normalized if item is not None]
        indices = [int(item["index"]) for item in settings]
        return settings if len(indices) == len(set(indices)) else None
    return None


def _gpu_oc_result_rows_match(
    raw_rows: Any,
    expected_fields_by_index: dict[int, set[str]],
    *,
    field_name: Literal["applied", "reset"],
) -> bool:
    """Bind one Agent GPU-OC operation list to the queued settings exactly."""

    if (
        not isinstance(raw_rows, list)
        or len(raw_rows) != len(expected_fields_by_index)
    ):
        return False
    seen_indices: set[int] = set()
    for row in raw_rows:
        if not isinstance(row, dict) or set(row) != {"index", field_name}:
            return False
        index = row.get("index")
        fields = row.get(field_name)
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index not in expected_fields_by_index
            or index in seen_indices
            or not isinstance(fields, list)
            or any(not isinstance(value, str) for value in fields)
            or len(fields) != len(set(fields))
            or set(fields) != expected_fields_by_index[index]
        ):
            return False
        seen_indices.add(index)
    return seen_indices == set(expected_fields_by_index)


def _gpu_oc_success_result_is_bound(
    payload: Any,
    result: Any,
) -> bool:
    """Verify that a successful Agent receipt covers the queued GPU OC work."""

    if (
        not isinstance(payload, dict)
        or payload.get("vendor") != "nvidia"
        or not isinstance(payload.get("gpu_profile_id"), str)
        or not isinstance(result, dict)
        or result.get("vendor") != payload["vendor"]
        or result.get("gpu_profile_id") != payload["gpu_profile_id"]
    ):
        return False
    settings = payload.get("settings")
    if not isinstance(settings, list) or not settings:
        return False
    expected_fields_by_index: dict[int, set[str]] = {}
    for setting in settings:
        if not isinstance(setting, dict):
            return False
        index = setting.get("index")
        fields = set(setting) - {"index"}
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index in expected_fields_by_index
            or not fields
            or not fields <= set(GPU_OC_CONTROL_RANGES)
        ):
            return False
        expected_fields_by_index[index] = fields

    if payload.get("mode") == "reset_one":
        return result.get("operations") == [] and _gpu_oc_result_rows_match(
            result.get("reset"),
            expected_fields_by_index,
            field_name="reset",
        )
    return _gpu_oc_result_rows_match(
        result.get("operations"),
        expected_fields_by_index,
        field_name="applied",
    )


def _legacy_gpu_oc_state_fingerprint(settings: list[dict[str, Any]]) -> str:
    """Return a representation-stable fingerprint for one normalized v1 state."""

    canonical: list[dict[str, Any]] = []
    for setting in sorted(settings, key=lambda item: int(item["index"])):
        canonical.append(
            {
                key: (
                    int(value)
                    if isinstance(value, float) and value.is_integer()
                    else value
                )
                for key, value in setting.items()
            }
        )
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _migration_payload_state_fingerprint(payload: Any) -> str | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        return None
    normalized: list[dict[str, Any]] = []
    for device in payload["devices"]:
        setting = (
            _normalized_legacy_gpu_oc_setting(device.get("setting"))
            if isinstance(device, dict)
            else None
        )
        if setting is None:
            return None
        normalized.append(setting)
    indices = [int(setting["index"]) for setting in normalized]
    if not normalized or len(indices) != len(set(indices)):
        return None
    return _legacy_gpu_oc_state_fingerprint(normalized)


def _reconstruct_legacy_gpu_oc_migration(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    reported_settings: list[dict[str, Any]],
) -> tuple[dict[str, Any], int] | None:
    """Replay successful GPU profile commands and retain only proven UUID bindings."""

    rows = db.execute(
        """SELECT rowid AS command_rowid, id, payload_json, created_by
           FROM commands
           WHERE agent_id=? AND action='apply_gpu_profile' AND status='succeeded'
           ORDER BY COALESCE(finished_at, created_at), command_rowid""",
        (agent_id,),
    ).fetchall()
    state_by_index: dict[int, dict[str, Any]] = {}
    binding_by_index: dict[int, tuple[str, str, int, int]] = {}
    state_proven = True
    for position, row in enumerate(rows):
        payload = _json(row["payload_json"], {})
        raw_settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(raw_settings, list) or not 1 <= len(raw_settings) <= 32:
            state_by_index = {}
            binding_by_index = {}
            state_proven = False
            continue
        normalized = [_normalized_legacy_gpu_oc_setting(item) for item in raw_settings]
        if any(item is None for item in normalized):
            state_by_index = {}
            binding_by_index = {}
            state_proven = False
            continue
        command_settings = [dict(item) for item in normalized if item is not None]
        indices = [int(item["index"]) for item in command_settings]
        if len(indices) != len(set(indices)):
            state_by_index = {}
            binding_by_index = {}
            state_proven = False
            continue
        mode = payload.get("mode")
        expected_uuid = payload.get("expected_gpu_uuid")
        profile_id = payload.get("gpu_profile_id")
        is_device_command = isinstance(profile_id, str) and profile_id.startswith(
            "device-oc-"
        )
        if not is_device_command:
            if (
                set(payload) != {"gpu_profile_id", "name", "vendor", "settings"}
                or not isinstance(profile_id, str)
                or not profile_id
                or not isinstance(payload.get("name"), str)
                or str(payload.get("vendor") or "").lower() != "nvidia"
            ):
                state_by_index = {}
                binding_by_index = {}
                state_proven = False
                continue
            state_by_index = {
                int(setting["index"]): setting for setting in command_settings
            }
            binding_by_index = {}
            state_proven = True
            continue
        # A legacy v1 Agent merged a UUID-verified device patch by current
        # numeric index.  Newer authoritative replace/reset semantics may
        # move or remove UUID-owned v2 entries and cannot be replayed as v1.
        # Treat them (and malformed device commands) as an unknown-state
        # barrier; a later full profile may establish a new proven baseline.
        if (
            mode != "merge_one"
            or set(payload)
            != {
                "gpu_profile_id",
                "name",
                "vendor",
                "mode",
                "expected_gpu_uuid",
                "settings",
            }
            or not isinstance(expected_uuid, str)
            or not isinstance(payload.get("name"), str)
            or str(payload.get("vendor") or "").lower() != "nvidia"
            or not 4 <= len(expected_uuid) <= 128
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in expected_uuid
            )
            or len(command_settings) != 1
            or not state_proven
        ):
            state_by_index = {}
            binding_by_index = {}
            state_proven = False
            continue
        setting = command_settings[0]
        index = int(setting["index"])
        if index in state_by_index:
            setting = {**state_by_index[index], **setting, "index": index}
        state_by_index[index] = setting
        binding_by_index[index] = (
            expected_uuid,
            str(row["id"]),
            int(row["created_by"]),
            position,
        )
    reported_by_index = {
        int(setting["index"]): dict(setting) for setting in reported_settings
    }
    if (
        not state_proven
        or state_by_index != reported_by_index
        or set(binding_by_index) != set(state_by_index)
    ):
        return None
    uuids = [binding_by_index[index][0] for index in sorted(binding_by_index)]
    if len(uuids) != len(set(uuids)):
        return None
    evidence_ids = list(
        dict.fromkeys(binding_by_index[index][1] for index in sorted(binding_by_index))
    )
    if not evidence_ids or any(
        re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            command_id,
        )
        is None
        for command_id in evidence_ids
    ):
        return None
    latest_index = max(binding_by_index, key=lambda index: binding_by_index[index][3])
    creator_id = binding_by_index[latest_index][2]
    return (
        {
            "schema_version": 1,
            "history_command_ids": evidence_ids,
            "devices": [
                {
                    "gpu_uuid": binding_by_index[index][0],
                    "setting": state_by_index[index],
                }
                for index in sorted(state_by_index)
            ],
        },
        creator_id,
    )


def _legacy_gpu_oc_migration_hardware_ready(metrics: Any, payload: Any) -> bool:
    """Require every candidate UUID to be uniquely present before queueing."""

    if not isinstance(metrics, dict) or not isinstance(payload, dict):
        return False
    reported_gpus = metrics.get("gpus")
    devices = payload.get("devices")
    if not isinstance(reported_gpus, list) or not isinstance(devices, list):
        return False
    by_uuid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gpu in reported_gpus[:32]:
        if isinstance(gpu, dict) and isinstance(gpu.get("uuid"), str):
            by_uuid[gpu["uuid"]].append(gpu)
    used_indices: set[int] = set()
    for device in devices:
        gpu_uuid = device.get("gpu_uuid") if isinstance(device, dict) else None
        matches = by_uuid.get(gpu_uuid, []) if isinstance(gpu_uuid, str) else []
        if len(matches) != 1:
            return False
        match = matches[0]
        index = match.get("index")
        if (
            str(match.get("vendor") or "").lower() != "nvidia"
            or isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= 31
            or index in used_indices
        ):
            return False
        used_indices.add(index)
    return len(used_indices) == len(devices)


def _migration_desired_gpu_oc(
    payload: Any,
    result: Any,
    metrics: Any,
) -> list[dict[str, Any]] | None:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "history_command_ids", "devices"}
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or not isinstance(result, dict)
    ):
        return None
    history_ids = payload.get("history_command_ids")
    if (
        not isinstance(history_ids, list)
        or not 1 <= len(history_ids) <= 256
        or any(
            not isinstance(command_id, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                command_id,
            )
            is None
            for command_id in history_ids
        )
        or len(set(history_ids)) != len(history_ids)
    ):
        return None
    expected_devices = payload.get("devices")
    if not isinstance(expected_devices, list) or not 1 <= len(expected_devices) <= 32:
        return None
    expected: dict[str, dict[str, Any]] = {}
    expected_indices: set[int] = set()
    for item in expected_devices:
        if not isinstance(item, dict) or set(item) != {"gpu_uuid", "setting"}:
            return None
        gpu_uuid = item.get("gpu_uuid")
        normalized = _normalized_legacy_gpu_oc_setting(item.get("setting"))
        if (
            not isinstance(gpu_uuid, str)
            or not 4 <= len(gpu_uuid) <= 128
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in gpu_uuid
            )
            or gpu_uuid in expected
            or normalized is None
            or int(normalized["index"]) in expected_indices
        ):
            return None
        expected[gpu_uuid] = normalized
        expected_indices.add(int(normalized["index"]))
    if set(result) != {
        "migrated",
        "source_state_version",
        "target_state_version",
        "nvml_writes",
        "history_command_ids",
        "devices",
    }:
        return None
    if (
        result.get("migrated") is not True
        or type(result.get("source_state_version")) is not int
        or result.get("source_state_version") != 1
        or type(result.get("target_state_version")) is not int
        or result.get("target_state_version") != 2
        or type(result.get("nvml_writes")) is not int
        or result.get("nvml_writes") != 0
        or result.get("history_command_ids") != history_ids
        or not isinstance(result.get("devices"), list)
        or len(result["devices"]) != len(expected)
    ):
        return None
    reported_gpus = metrics.get("gpus") if isinstance(metrics, dict) else None
    gpu_by_uuid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gpu in reported_gpus if isinstance(reported_gpus, list) else []:
        if isinstance(gpu, dict) and isinstance(gpu.get("uuid"), str):
            gpu_by_uuid[gpu["uuid"]].append(gpu)
    desired: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    seen_uuids: set[str] = set()
    for item in result["devices"]:
        if not isinstance(item, dict) or set(item) != {"gpu_uuid", "index", "setting"}:
            return None
        gpu_uuid = item.get("gpu_uuid")
        index = item.get("index")
        setting_fields = item.get("setting")
        matches = gpu_by_uuid.get(gpu_uuid, []) if isinstance(gpu_uuid, str) else []
        expected_setting = expected.get(gpu_uuid) if isinstance(gpu_uuid, str) else None
        normalized_result_setting = (
            _normalized_legacy_gpu_oc_setting(
                {"index": index, **setting_fields}
            )
            if isinstance(setting_fields, dict) and "index" not in setting_fields
            else None
        )
        if (
            not isinstance(gpu_uuid, str)
            or gpu_uuid in seen_uuids
            or isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= 31
            or index in seen_indices
            or not isinstance(setting_fields, dict)
            or not isinstance(expected_setting, dict)
            or setting_fields != {key: value for key, value in expected_setting.items() if key != "index"}
            or normalized_result_setting != {**expected_setting, "index": index}
            or len(matches) != 1
            or str(matches[0].get("vendor") or "").lower() != "nvidia"
            or matches[0].get("index") != index
        ):
            return None
        raw_name = re.sub(
            r"\s+",
            " ",
            re.sub(r"[\x00-\x1f\x7f]+", " ", str(matches[0].get("name") or gpu_uuid)),
        ).strip()
        desired.append(
            {
                "gpu_uuid": gpu_uuid,
                "name": f"{raw_name[:55] or gpu_uuid[:55]} · GPU {index}"[:80],
                "setting": {"index": index, **setting_fields},
            }
        )
        seen_uuids.add(gpu_uuid)
        seen_indices.add(index)
    return sorted(desired, key=lambda item: int(item["setting"]["index"])) if set(expected) == seen_uuids else None


def _flight_payload_has_unsupported_miner(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for role in ("primary", "secondary"):
        leg = payload.get(role)
        if not isinstance(leg, dict):
            continue
        miner_type = leg.get("miner_type", "generic")
        if not isinstance(miner_type, str) or miner_type not in {*MINER_TYPES, "generic"}:
            return True
    return False


def _dispatchable_miner_commands(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    # A historical command already executing may still report a result. Keep
    # that record, but never resend an unsupported apply payload to an Agent.
    return [row for row in rows if row["action"] != "apply_flight_sheet"
            or not _flight_payload_has_unsupported_miner(_json(row["payload_json"], {}))]


def _redact_command_secrets(value: Any, *, key: str | None = None) -> Any:
    """Return a UI-safe copy of a command payload.

    Command payloads are also the Agent transport envelope, so the database
    must retain the original values.  The operator/audit API has no reason to
    echo credentials back to a browser, however.  Redact recursively here
    instead of weakening the Agent payload or relying on every caller to
    remember individual secret fields.
    """

    normalized_key = str(key or "").casefold()
    if any(marker in normalized_key for marker in ("password", "secret", "token")):
        if value in (None, ""):
            return value
        return "[configured]"
    if isinstance(value, dict):
        return {
            str(nested_key): _redact_command_secrets(
                nested_value, key=str(nested_key)
            )
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_command_secrets(item) for item in value]
    return value


def _without_controller_markers(payload: dict[str, Any]) -> dict[str, Any]:
    visible = dict(payload)
    visible.pop("_controller_auto_reapply", None)
    visible.pop(CONTROLLER_MINING_AUTOSTART_MARKER, None)
    visible.pop(CONTROLLER_MINING_RESULT_BINDING_MARKER, None)
    visible.pop(CONTROLLER_FLIGHT_DISPATCH_MARKER, None)
    visible.pop(CONTROLLER_FLIGHT_RUNTIME_MARKER, None)
    visible.pop(CONTROLLER_GPU_OC_INTENT_MARKER, None)
    # Keep empty editable state in SQLite, but omit the new wire key for
    # older Agents whose option sets do not yet include extra_args.
    for role in ("primary", "secondary"):
        leg = visible.get(role)
        if not isinstance(leg, dict) or not isinstance(leg.get("miner_type"), str) or leg["miner_type"] not in MINER_TYPES:
            continue
        options = leg.get("extra_config")
        if not isinstance(options, dict) or "extra_args" not in options:
            continue
        value = options["extra_args"]
        if value is None or isinstance(value, str) and not value.strip():
            visible[role] = {**leg, "extra_config": {
                key: item for key, item in options.items() if key != "extra_args"
            }}
    return visible


def _command_payload_for_agent(value: str | None) -> dict[str, Any]:
    payload = _json(value, {})
    if not isinstance(payload, dict):
        return {}
    return _without_controller_markers(payload)


def _command_payload_for_view(value: str | None) -> dict[str, Any]:
    visible = _command_payload_for_agent(value)
    visible.pop(FLIGHT_RUNTIME_FINGERPRINT_FIELD, None)
    redacted = _redact_command_secrets(visible)
    return redacted if isinstance(redacted, dict) else {}


def _flight_runtime_payload_fingerprint(payload: dict[str, Any]) -> str:
    """Hash the exact flight-sheet document delivered to a capable Agent."""

    runtime_payload = _without_controller_markers(payload)
    runtime_payload.pop(FLIGHT_RUNTIME_FINGERPRINT_FIELD, None)
    encoded = json.dumps(
        runtime_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _successful_mining_result_is_bound(
    action: str,
    command_payload: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    """Bind a terminal mining result to the exact controller-owned target.

    This validation is capability-gated at the result endpoint so older
    Agents retain their historical result shape.  New Agents must prove that
    the process/configuration they changed belongs to the flight sheet stored
    with this command; a bare ``{}`` can therefore never advance refresh,
    autostart, or desired-OC state.
    """

    if action == "apply_flight_sheet":
        expected = command_payload.get("flight_sheet_id")
        if (
            not isinstance(expected, str)
            or not expected
            or result.get("flight_sheet_id") != expected
        ):
            return False
        primary = command_payload.get("primary")
        managed_type = (
            primary.get("miner_type")
            if isinstance(primary, dict)
            else None
        )
        managed = result.get("managed_miner")
        runtime_marker = command_payload.get(CONTROLLER_FLIGHT_RUNTIME_MARKER)
        if (
            isinstance(runtime_marker, dict)
            and runtime_marker.get("result_binding_required") is True
        ):
            expected_fingerprint = runtime_marker.get("runtime_fingerprint")
            if (
                not isinstance(expected_fingerprint, str)
                or not expected_fingerprint
                or result.get(FLIGHT_RUNTIME_FINGERPRINT_FIELD)
                != expected_fingerprint
            ):
                return False
            if isinstance(managed, dict) and (
                managed.get(FLIGHT_RUNTIME_FINGERPRINT_FIELD)
                != expected_fingerprint
            ):
                return False
            expected_roles = runtime_marker.get("roles")
            if expected_roles == ["primary", "secondary"]:
                roles = managed.get("roles") if isinstance(managed, dict) else None
                if not isinstance(roles, dict) or set(roles) != {
                    "primary",
                    "secondary",
                }:
                    return False
                for role in ("primary", "secondary"):
                    role_result = roles.get(role)
                    if (
                        not isinstance(role_result, dict)
                        or role_result.get("managed") is not True
                        or role_result.get("flight_sheet_id") != expected
                        or role_result.get(FLIGHT_RUNTIME_FINGERPRINT_FIELD)
                        != expected_fingerprint
                    ):
                        return False
        if managed_type in MANAGED_MINER_DISPLAY_NAMES or is_retired_miner(managed_type):
            return bool(
                isinstance(managed, dict)
                and managed.get("managed") is True
                and managed.get("flight_sheet_id") == expected
            )
        return managed is None or (
            isinstance(managed, dict)
            and managed.get("flight_sheet_id") == expected
        )

    if action not in MINING_CONTROL_ACTIONS:
        return True
    if CONTROLLER_MINING_RESULT_BINDING_MARKER not in command_payload:
        return False
    expected = command_payload[CONTROLLER_MINING_RESULT_BINDING_MARKER]
    if expected is not None and (
        not isinstance(expected, str) or not expected
    ):
        return False
    if (
        result.get("managed") is not True
        or result.get("flight_sheet_id") != expected
    ):
        return False

    if action == "restart_mining":
        return result.get("restarted") is True

    if not isinstance(result.get("stopped"), bool):
        return False
    gpu_reset = result.get("gpu_oc_reset")
    if (
        not isinstance(gpu_reset, dict)
        or not isinstance(gpu_reset.get("state_cleared"), bool)
        or gpu_reset.get("status") == "failed"
    ):
        return False
    srb_reset = result.get("srb_gpu_oc_reset")
    if srb_reset is not None and (
        not isinstance(srb_reset, dict)
        or srb_reset.get("cleared") is not True
        or srb_reset.get("status") == "failed"
    ):
        return False
    if action == "remove_flight_sheet":
        return (
            result.get("configuration_cleared") is True
            and "configuration_removal" not in result
        )
    return True


def _mining_residue_finding_semantics_are_valid(finding: dict[str, Any]) -> bool:
    """Require each residue status to agree with its identity evidence.

    ``reason`` remains an opaque diagnostic string for compatibility with
    older Agents.  Authorization decisions are bound only to typed identity
    fields; accepting contradictory booleans here would otherwise let an
    ``ambiguous`` finding masquerade as a selectable, verified process.
    """

    status = finding["status"]
    record_identity = (
        finding["miner_type"],
        finding["flight_sheet_id"],
        finding["record_pid"],
    )
    has_record_identity = all(value is not None for value in record_identity)
    has_no_record_identity = all(value is None for value in record_identity)
    verified_pid = finding["verified_pid"]
    process_verified = finding["process_verified"]
    clearable = finding["clearable"]

    if status == "no_record":
        return bool(
            has_no_record_identity
            and verified_pid is None
            and process_verified is False
            and clearable is False
        )
    if status == "ambiguous":
        # Record parsing/runtime-path failures legitimately have no record
        # identity; process-inspection failures retain the complete identity.
        return bool(
            (has_record_identity or has_no_record_identity)
            and verified_pid is None
            and process_verified is False
            and clearable is False
        )
    if status in {"preboot_record", "orphan_record"}:
        return bool(
            has_record_identity
            and verified_pid is None
            and process_verified is False
            and clearable is True
        )
    if status == "verified_process":
        return bool(
            has_record_identity
            and verified_pid == finding["record_pid"]
            and process_verified is True
            and clearable is True
        )
    if status == "exact_replacement":
        return bool(
            has_record_identity
            and verified_pid is not None
            and process_verified is True
            and clearable is True
        )
    return False


def _mining_residue_scan_result_is_valid(result: Any) -> bool:
    """Validate the secret-free, fingerprint-bound Agent residue report."""

    if not isinstance(result, dict) or set(result) != {
        "schema_version",
        "scan_fingerprint",
        "cleanup_available",
        "clearable_count",
        "ambiguous_count",
        "findings",
    }:
        return False
    if (
        type(result.get("schema_version")) is not int
        or result["schema_version"] not in {1, 2, 3}
        or not _cache_fingerprint(result.get("scan_fingerprint"))
        or not isinstance(result.get("cleanup_available"), bool)
    ):
        return False
    for field in ("clearable_count", "ambiguous_count"):
        value = result.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    findings = result.get("findings")
    if not isinstance(findings, list) or len(findings) > 3:
        return False
    finding_fields = {
        "role",
        "status",
        "miner_type",
        "flight_sheet_id",
        "record_pid",
        "verified_pid",
        "process_verified",
        "clearable",
        "reason",
    }
    schema_version = result.get("schema_version")
    if schema_version in {2, 3}:
        finding_fields.add("target_fingerprint")
    if schema_version == 3:
        finding_fields.add("manual_candidates")
    valid_statuses = {
        "no_record",
        "preboot_record",
        "orphan_record",
        "verified_process",
        "exact_replacement",
        "ambiguous",
    }
    seen_roles: set[str] = set()
    seen_target_fingerprints: set[str] = set()
    seen_candidate_fingerprints: set[str] = set()
    seen_candidate_pids: set[int] = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != finding_fields:
            return False
        role = finding.get("role")
        if (
            not isinstance(role, str)
            or role not in {"legacy", "primary", "secondary"}
            or role in seen_roles
        ):
            return False
        seen_roles.add(role)
        if not isinstance(finding.get("status"), str) or finding["status"] not in valid_statuses:
            return False
        if finding.get("miner_type") not in (None, *MINER_TYPES) and not is_retired_miner(finding.get("miner_type")):
            return False
        flight_sheet_id = finding.get("flight_sheet_id")
        if flight_sheet_id is not None and (
            not isinstance(flight_sheet_id, str)
            or not flight_sheet_id
            or len(flight_sheet_id) > 64
        ):
            return False
        for field in ("record_pid", "verified_pid"):
            pid = finding.get(field)
            if pid is not None and (
                not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            ):
                return False
        if not isinstance(finding.get("process_verified"), bool):
            return False
        if not isinstance(finding.get("clearable"), bool):
            return False
        reason = finding.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 1000:
            return False
        if schema_version in {2, 3} and not _cache_fingerprint(finding.get("target_fingerprint")):
            return False
        if schema_version in {2, 3}:
            target_fingerprint = finding["target_fingerprint"]
            if target_fingerprint in seen_target_fingerprints:
                return False
            seen_target_fingerprints.add(target_fingerprint)
        if schema_version == 3:
            manual_candidates = finding.get("manual_candidates")
            if (
                not isinstance(manual_candidates, list)
                or len(manual_candidates) > 1
            ):
                return False
            if finding.get("status") != "ambiguous" and manual_candidates:
                return False
            for candidate in manual_candidates:
                if not isinstance(candidate, dict) or set(candidate) != {
                    "pid",
                    "candidate_fingerprint",
                }:
                    return False
                candidate_pid = candidate.get("pid")
                candidate_fingerprint = candidate.get("candidate_fingerprint")
                if (
                    not isinstance(candidate_pid, int)
                    or isinstance(candidate_pid, bool)
                    or candidate_pid <= 0
                    or candidate_pid in seen_candidate_pids
                    or not _cache_fingerprint(candidate_fingerprint)
                    or candidate_fingerprint in seen_candidate_fingerprints
                ):
                    return False
                seen_candidate_pids.add(candidate_pid)
                seen_candidate_fingerprints.add(candidate_fingerprint)
        if not _mining_residue_finding_semantics_are_valid(finding):
            return False
    clearable_count = sum(bool(item["clearable"]) for item in findings)
    ambiguous_count = sum(item["status"] == "ambiguous" for item in findings)
    if (
        result["clearable_count"] != clearable_count
        or result["ambiguous_count"] != ambiguous_count
        # A clean scan is intentionally actionable: cleanup also clears
        # persisted SRBMiner/NVML OC state even when there is no process
        # record left to remove.
        or (
            schema_version == 1
            and result["cleanup_available"] is not bool(not ambiguous_count)
        )
        or (
            schema_version in {2, 3}
            and result["cleanup_available"] is not True
        )
    ):
        return False
    return True


def _cache_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _cache_bounded_text(value: Any, *, limit: int = 160) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(ord(character) < 32 for character in value)
    )


def _cache_reason(value: Any) -> bool:
    return value is None or bool(
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
    )


def _cache_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _miner_cache_scan_result_is_valid(result: Any) -> bool:
    """Validate the bounded, path-free Agent cache inventory before display."""

    if not isinstance(result, dict) or set(result) != {
        "schema_version", "scan_fingerprint", "can_cleanup", "reason", "disk",
        "candidate_logical_bytes", "candidates", "protected", "unclassified",
    } or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        return False
    fingerprint = result.get("scan_fingerprint")
    if (fingerprint is not None and not _cache_fingerprint(fingerprint)) or (
        not isinstance(result.get("can_cleanup"), bool)
        or result["can_cleanup"] and fingerprint is None
        or not _cache_reason(result.get("reason"))
    ):
        return False
    disk = result.get("disk")
    base_disk_fields = {"total_bytes", "free_bytes", "scope"}
    if not isinstance(disk, dict) or set(disk) not in (base_disk_fields, base_disk_fields | {"filesystems"}):
        return False
    if disk["scope"] != "package_cache_filesystem":
        return False
    if any(disk[key] is not None and not _cache_nonnegative_int(disk[key]) for key in ("total_bytes", "free_bytes")):
        return False
    if (disk["total_bytes"] is not None and disk["free_bytes"] is not None
            and disk["free_bytes"] > disk["total_bytes"]):
        return False
    filesystems = disk.get("filesystems", [])
    if not isinstance(filesystems, list) or len(filesystems) > 16:
        return False
    filesystem_keys: set[str] = set()
    for filesystem in filesystems:
        if not isinstance(filesystem, dict) or set(filesystem) != {"scope", "filesystem_key", "total_bytes", "free_bytes"}:
            return False
        key = filesystem.get("filesystem_key")
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{16}", key) is None or key in filesystem_keys:
            return False
        filesystem_keys.add(key)
        if filesystem["scope"] not in ("package_cache", "hiveos_session_payload"):
            return False
        if any(filesystem[key] is not None and not _cache_nonnegative_int(filesystem[key]) for key in ("total_bytes", "free_bytes")):
            return False
        if (filesystem["total_bytes"] is not None and filesystem["free_bytes"] is not None
                and filesystem["free_bytes"] > filesystem["total_bytes"]):
            return False
        if filesystem["scope"] == "package_cache" and any(filesystem[key] != disk[key] for key in ("total_bytes", "free_bytes")):
            return False
    for key in ("candidates", "protected", "unclassified"):
        if not isinstance(result.get(key), list) or len(result[key]) > 256:
            return False
    if not _cache_nonnegative_int(result.get("candidate_logical_bytes")):
        return False
    candidate_ids: set[str] = set()
    logical_total = 0
    for item in result["candidates"]:
        if not isinstance(item, dict) or set(item) != {
            "candidate_id", "package_id", "name", "version",
            "logical_bytes", "file_count", "kind",
        } or not _cache_fingerprint(item.get("candidate_id")):
            return False
        if item["candidate_id"] in candidate_ids:
            return False
        candidate_ids.add(item["candidate_id"])
        kind = item.get("kind")
        if kind not in ("package_cache", "hiveos_session_payload"):
            return False
        if not _cache_bounded_text(item.get("name"), limit=120):
            return False
        for key in ("package_id", "version"):
            value = item.get(key)
            if value is None and kind == "package_cache":
                return False
            if value is not None and not _cache_bounded_text(value, limit=80):
                return False
        if not _cache_nonnegative_int(item.get("logical_bytes")) or not _cache_nonnegative_int(item.get("file_count")):
            return False
        logical_total += item["logical_bytes"]
    if logical_total != result["candidate_logical_bytes"]:
        return False
    for item in result["protected"]:
        if not isinstance(item, dict) or set(item) != {"entry_id", "package_id", "version", "reason", "kind"}:
            return False
        if not _cache_bounded_text(item.get("entry_id"), limit=96) or any(char in item["entry_id"] for char in "/\\"):
            return False
        if item.get("kind") not in ("package_cache", "hiveos_session_payload"):
            return False
        if item.get("reason") is None or not _cache_reason(item.get("reason")):
            return False
        if any(item.get(key) is not None and not _cache_bounded_text(item[key], limit=80) for key in ("package_id", "version")):
            return False
    for item in result["unclassified"]:
        if not isinstance(item, dict) or set(item) != {"entry_id", "reason", "kind"}:
            return False
        entry_id = item.get("entry_id")
        if not isinstance(entry_id, str) or len(entry_id) > 96 or any(char in entry_id for char in "/\\") or any(ord(char) < 32 for char in entry_id):
            return False
        if not entry_id and not (item.get("kind") == "hiveos_session_payload" and item.get("reason") == "invalid_session_entry"):
            return False
        if item.get("kind") not in ("package_cache", "hiveos_session_payload"):
            return False
        if item.get("reason") is None or not _cache_reason(item.get("reason")):
            return False
    return True


def _miner_cache_cleanup_result_is_bound(payload: Any, result: Any, *, succeeded: bool, scan: Any) -> bool:
    """Keep reported removals and measured disk changes tied to one scan."""

    if not isinstance(payload, dict) or set(payload) != {"scan_fingerprint"} or not _cache_fingerprint(payload["scan_fingerprint"]):
        return False
    if not _miner_cache_scan_result_is_valid(scan) or scan["scan_fingerprint"] != payload["scan_fingerprint"] or scan["can_cleanup"] is not True:
        return False
    if not isinstance(result, dict) or set(result) != {
        "schema_version", "state", "requested_scan_fingerprint", "before_scan_fingerprint",
        "deleted", "skipped", "failed", "logical_bytes_removed", "disk", "reason",
    } or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        return False
    if result.get("state") not in ("succeeded", "stale_scan", "partial", "failed"):
        return False
    if succeeded != (result["state"] == "succeeded"):
        return False
    if result.get("requested_scan_fingerprint") != payload["scan_fingerprint"] or not _cache_reason(result.get("reason")):
        return False
    before = result.get("before_scan_fingerprint")
    if before is not None and not _cache_fingerprint(before):
        return False
    if not all(isinstance(result.get(key), list) and len(result[key]) <= 256 for key in ("deleted", "skipped", "failed")):
        return False
    candidates = {item["candidate_id"]: item for item in scan["candidates"]}
    seen: set[str] = set()
    logical_total = 0
    for item in result["deleted"]:
        if not isinstance(item, dict) or set(item) != {"candidate_id", "package_id", "version", "logical_bytes", "kind"}:
            return False
        if not _cache_fingerprint(item.get("candidate_id")):
            return False
        if not _cache_nonnegative_int(item.get("logical_bytes")):
            return False
        candidate = candidates.get(item.get("candidate_id"))
        if candidate is None or item["candidate_id"] in seen or item.get("kind") != candidate["kind"] or item.get("package_id") != candidate["package_id"] or item.get("version") != candidate["version"] or item.get("logical_bytes") != candidate["logical_bytes"]:
            return False
        seen.add(item["candidate_id"])
        logical_total += item["logical_bytes"]
    for key in ("skipped", "failed"):
        for item in result[key]:
            if not isinstance(item, dict) or set(item) != {"candidate_id", "reason", "kind"}:
                return False
            candidate_id = item.get("candidate_id")
            if not _cache_fingerprint(candidate_id) or candidate_id not in candidates or candidate_id in seen or item.get("kind") != candidates[candidate_id]["kind"] or not _cache_reason(item.get("reason")) or item["reason"] is None:
                return False
            seen.add(candidate_id)
    if not _cache_nonnegative_int(result.get("logical_bytes_removed")) or result["logical_bytes_removed"] != logical_total:
        return False
    if result["state"] == "stale_scan" and (seen or result["logical_bytes_removed"]):
        return False
    if succeeded and (
        result["skipped"] or result["failed"]
        or before != payload["scan_fingerprint"]
        or seen != set(candidates)
    ):
        return False
    disk = result.get("disk")
    base_disk_fields = {"free_bytes_before", "free_bytes_after", "free_bytes_delta", "scope"}
    if not isinstance(disk, dict) or set(disk) not in (base_disk_fields, base_disk_fields | {"filesystems"}):
        return False
    if disk["scope"] != "package_cache_filesystem":
        return False
    first, last, delta = (disk[key] for key in ("free_bytes_before", "free_bytes_after", "free_bytes_delta"))
    if any(value is not None and not _cache_nonnegative_int(value) for value in (first, last)):
        return False
    if delta is not None and (not isinstance(delta, int) or isinstance(delta, bool)):
        return False
    if (first is None or last is None) and delta is not None:
        return False
    if first is not None and last is not None and delta is not None and delta != last - first:
        return False
    filesystems = disk.get("filesystems", [])
    if not isinstance(filesystems, list) or len(filesystems) > 16:
        return False
    expected_session_filesystems = sum(
        item["scope"] == "hiveos_session_payload"
        for item in scan["disk"].get("filesystems", [])
    )
    if (succeeded or result["state"] == "partial") and expected_session_filesystems and sum(
        isinstance(item, dict) and item.get("scope") == "hiveos_session_payload"
        for item in filesystems
    ) != expected_session_filesystems:
        return False
    scan_filesystems = {item["filesystem_key"]: item for item in scan["disk"].get("filesystems", [])}
    result_filesystem_keys: set[str] = set()
    for filesystem in filesystems:
        if not isinstance(filesystem, dict) or set(filesystem) != {"scope", "filesystem_key", "free_bytes_before", "free_bytes_after", "free_bytes_delta"}:
            return False
        key = filesystem.get("filesystem_key")
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{16}", key) is None or key in result_filesystem_keys:
            return False
        result_filesystem_keys.add(key)
        if filesystem["scope"] not in ("package_cache", "hiveos_session_payload"):
            return False
        if succeeded and key in scan_filesystems and filesystem["scope"] != scan_filesystems[key]["scope"]:
            return False
        fs_first, fs_last, fs_delta = (filesystem[key] for key in ("free_bytes_before", "free_bytes_after", "free_bytes_delta"))
        if any(value is not None and not _cache_nonnegative_int(value) for value in (fs_first, fs_last)):
            return False
        if fs_delta is not None and (not isinstance(fs_delta, int) or isinstance(fs_delta, bool)):
            return False
        if (fs_first is None or fs_last is None) and fs_delta is not None:
            return False
        if fs_first is not None and fs_last is not None and fs_delta is not None and fs_delta != fs_last - fs_first:
            return False
        if filesystem["scope"] == "package_cache" and any(filesystem[field] != disk[field] for field in ("free_bytes_before", "free_bytes_after", "free_bytes_delta")):
            return False
    if succeeded and scan_filesystems and result_filesystem_keys != set(scan_filesystems):
        return False
    return True


def _cache_scan_for_cleanup(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    created_by: str,
    fingerprint: str,
    before_created_at: str | None = None,
    require_fresh: bool = False,
) -> sqlite3.Row | None:
    """Find this operator's latest scan for this Agent, never a caller path."""

    before_clause = "AND created_at < ?" if before_created_at is not None else ""
    params: tuple[Any, ...] = (
        (agent_id, MINER_CACHE_SCAN_ACTION, created_by, before_created_at)
        if before_created_at is not None
        else (agent_id, MINER_CACHE_SCAN_ACTION, created_by)
    )
    scan = db.execute(
        f"""SELECT id, status, result_json, finished_at FROM commands
            WHERE agent_id=? AND action=? AND created_by=? {before_clause}
            ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        params,
    ).fetchone()
    if scan is None or scan["status"] != "succeeded":
        return None
    result = _json(scan["result_json"], {})
    if (
        not _miner_cache_scan_result_is_valid(result)
        or result["can_cleanup"] is not True
        or result["scan_fingerprint"] != fingerprint
    ):
        return None
    if require_fresh:
        finished = _parse_time(scan["finished_at"])
        now = datetime.now(timezone.utc)
        if finished is None or finished > now + timedelta(seconds=30) or now - finished > timedelta(minutes=5):
            return None
    return scan


def _mining_residue_cleanup_uses_explicit_candidate(payload: Any) -> bool:
    """Return whether cleanup selects a scan-bound manual process candidate."""

    return bool(
        isinstance(payload, dict)
        and isinstance(payload.get("targets"), list)
        and any(
            isinstance(target, dict) and "candidate_fingerprint" in target
            for target in payload["targets"]
        )
    )


def _explicit_residue_cleanup_matches_latest_scan(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    payload: Any,
) -> bool:
    """Authorize a schema-v3 candidate when heartbeat capabilities lag.

    A successful scan command is stronger, fresher evidence than the Agent's
    asynchronously refreshed capability cache: the same authenticated Agent
    has just returned a schema-v3, snapshot-bound candidate.  The fallback is
    deliberately limited to the latest successful scan for this Agent, and
    every target remains bound to that scan's role and fingerprints.  The
    Agent still repeats the process-identity checks before termination.
    """

    if not _mining_residue_cleanup_uses_explicit_candidate(payload):
        return False
    row = db.execute(
        """SELECT result_json FROM commands
           WHERE agent_id=? AND action=? AND status='succeeded'
             AND result_json IS NOT NULL
           ORDER BY finished_at DESC, created_at DESC, id DESC
           LIMIT 1""",
        (agent_id, MINING_RESIDUE_SCAN_ACTION),
    ).fetchone()
    if row is None:
        return False
    scan = _json(row["result_json"], None)
    if (
        not _mining_residue_scan_result_is_valid(scan)
        or scan.get("schema_version") != 3
        or scan.get("scan_fingerprint") != payload.get("scan_fingerprint")
    ):
        return False
    targets = payload.get("targets")
    if not isinstance(targets, list):
        return False
    findings_by_role = {
        finding["role"]: finding
        for finding in scan["findings"]
    }
    explicit_candidate_seen = False
    for target in targets:
        if not isinstance(target, dict):
            return False
        role = target.get("role")
        finding = findings_by_role.get(role)
        if (
            finding is None
            or finding.get("target_fingerprint")
            != target.get("target_fingerprint")
        ):
            return False
        candidate_fingerprint = target.get("candidate_fingerprint")
        if candidate_fingerprint is None:
            if finding.get("clearable") is not True:
                return False
            continue
        explicit_candidate_seen = True
        if (
            finding.get("status") != "ambiguous"
            or finding.get("clearable") is not False
            or not any(
                isinstance(candidate, dict)
                and candidate.get("candidate_fingerprint")
                == candidate_fingerprint
                for candidate in finding.get("manual_candidates", [])
            )
        ):
            return False
    return explicit_candidate_seen


def _mining_residue_cleanup_result_is_bound(
    payload: Any,
    result: Any,
) -> bool:
    if not isinstance(payload, dict) or not isinstance(result, dict):
        return False
    payload_fields = set(payload)
    old_payload = payload_fields == {"scan_fingerprint"}
    new_payload = payload_fields == {
        "scan_fingerprint",
        "targets",
        "reset_srbminer_oc",
        "reset_nvml_oc",
    }
    if not old_payload and not new_payload:
        return False
    scan_fields = {
        key: result.get(key)
        for key in (
            "schema_version",
            "scan_fingerprint",
            "cleanup_available",
            "clearable_count",
            "ambiguous_count",
            "findings",
        )
    }
    if (
        not _mining_residue_scan_result_is_valid(scan_fields)
        or result.get("scan_fingerprint") != payload.get("scan_fingerprint")
        or not set(result).issuperset({
            *scan_fields,
            "blocked",
            "changed",
            "processes_stopped",
            "records_removed",
            "results",
            "gpu_oc_reset",
        })
    ):
        return False
    allowed_result_fields = {
        *scan_fields,
        "blocked",
        "changed",
        "processes_stopped",
        "records_removed",
        "results",
        "gpu_oc_reset",
    }
    # A fingerprint-only request may also be executed by a newer Agent.
    allowed_result_fields.update({"selected_targets", "reset_options"})
    if (
        set(result) - allowed_result_fields
        or result.get("blocked") is not False
        or not isinstance(result.get("changed"), bool)
        or not isinstance(result.get("gpu_oc_reset"), dict)
        or set(result["gpu_oc_reset"]) != {"srbminer", "nvml"}
        or not isinstance(result["gpu_oc_reset"].get("srbminer"), dict)
        or not isinstance(result["gpu_oc_reset"].get("nvml"), dict)
    ):
        return False
    if new_payload:
        targets = payload.get("targets")
        if (
            result.get("schema_version") not in {2, 3}
            or not isinstance(targets, list)
            or result.get("selected_targets") != targets
            or result.get("reset_options")
            != {
                "srbminer": payload.get("reset_srbminer_oc"),
                "nvml": payload.get("reset_nvml_oc"),
            }
        ):
            return False
        if (
            _mining_residue_cleanup_uses_explicit_candidate(payload)
            and result.get("schema_version") != 3
        ):
            return False
    elif "selected_targets" in result and not isinstance(
        result.get("selected_targets"), list
    ):
        return False
    if "reset_options" in result and (
        not isinstance(result.get("reset_options"), dict)
        or set(result["reset_options"]) != {"srbminer", "nvml"}
        or not all(
            isinstance(result["reset_options"].get(key), bool)
            for key in ("srbminer", "nvml")
        )
    ):
        return False
    srb_reset = result["gpu_oc_reset"]["srbminer"]
    nvml_reset = result["gpu_oc_reset"]["nvml"]
    if not isinstance(srb_reset.get("required"), bool) or not isinstance(
        srb_reset.get("cleared"), bool
    ):
        return False
    if srb_reset["required"] and not srb_reset["cleared"]:
        return False
    if not isinstance(nvml_reset.get("state_cleared"), bool):
        return False
    for field in ("processes_stopped", "records_removed"):
        value = result.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    cleanup_results = result.get("results")
    if not isinstance(cleanup_results, list) or len(cleanup_results) > 3:
        return False
    expected_fields = {
        "role",
        "status",
        "process_stopped",
        "record_removed",
        "reason",
    }
    findings_by_role = {
        finding["role"]: finding
        for finding in scan_fields["findings"]
    }
    selected_roles: list[str] = []
    selected_targets_by_role: dict[str, dict[str, Any]] = {}
    if new_payload:
        for target in payload["targets"]:
            if not isinstance(target, dict) or set(target) not in (
                {"role", "target_fingerprint"},
                {"role", "target_fingerprint", "candidate_fingerprint"},
            ):
                return False
            role = target.get("role")
            target_fingerprint = target.get("target_fingerprint")
            finding = findings_by_role.get(role)
            if (
                role in selected_targets_by_role
                or finding is None
                or finding.get("target_fingerprint") != target_fingerprint
            ):
                return False
            candidate_fingerprint = target.get("candidate_fingerprint")
            if candidate_fingerprint is None:
                if finding["clearable"] is not True:
                    return False
            else:
                if (
                    result.get("schema_version") != 3
                    or finding.get("status") != "ambiguous"
                    or finding.get("clearable") is not False
                    or not any(
                        candidate.get("candidate_fingerprint")
                        == candidate_fingerprint
                        for candidate in finding.get("manual_candidates", [])
                    )
                ):
                    return False
            selected_targets_by_role[str(role)] = target
            selected_roles.append(str(role))
    seen_result_roles: set[str] = set()
    for item in cleanup_results:
        if not isinstance(item, dict) or set(item) != expected_fields:
            return False
        role = item.get("role")
        if (
            not isinstance(role, str)
            or role not in {"legacy", "primary", "secondary"}
            or role in seen_result_roles
        ):
            return False
        seen_result_roles.add(role)
        if item.get("status") not in (
            "orphan_record_removed",
            "verified_process_stopped",
            "explicit_candidate_stopped",
        ):
            return False
        if not isinstance(item.get("process_stopped"), bool) or not isinstance(
            item.get("record_removed"), bool
        ):
            return False
        reason = item.get("reason")
        if reason is not None and (
            not isinstance(reason, str) or not reason or len(reason) > 1000
        ):
            return False
        finding = findings_by_role.get(role)
        selected_target = selected_targets_by_role.get(str(role))
        explicit_candidate = bool(
            selected_target is not None
            and "candidate_fingerprint" in selected_target
        )
        if finding is None or (
            finding["clearable"] is not True and not explicit_candidate
        ):
            return False
        if item["status"] == "orphan_record_removed":
            if (
                finding["status"] not in {"preboot_record", "orphan_record"}
                or item["process_stopped"] is not False
                or item["record_removed"] is not True
                or reason is not None
            ):
                return False
        elif item["status"] == "verified_process_stopped" and (
            finding["status"] not in {"verified_process", "exact_replacement"}
            or explicit_candidate
            or item["process_stopped"] is not True
            or item["record_removed"] is not True
            or reason is not None
        ):
            return False
        elif item["status"] == "explicit_candidate_stopped" and (
            not explicit_candidate
            or finding["status"] != "ambiguous"
            or item["process_stopped"] is not True
            or item["record_removed"] is not True
            or reason is not None
        ):
            return False
    stopped = sum(bool(item["process_stopped"]) for item in cleanup_results)
    removed = sum(bool(item["record_removed"]) for item in cleanup_results)
    if new_payload:
        result_roles = [item.get("role") for item in cleanup_results]
        if result_roles != selected_roles:
            return False
    else:
        expected_roles = [
            finding["role"]
            for finding in scan_fields["findings"]
            if finding["clearable"] is True
        ]
        if [item.get("role") for item in cleanup_results] != expected_roles:
            return False
        if "selected_targets" in result:
            selected_targets = result["selected_targets"]
            if not isinstance(selected_targets, list):
                return False
            if result.get("schema_version") in {2, 3}:
                expected_targets = [
                    {
                        "role": finding["role"],
                        "target_fingerprint": finding["target_fingerprint"],
                    }
                    for finding in scan_fields["findings"]
                    if finding["clearable"] is True
                ]
                if selected_targets != expected_targets:
                    return False
    return bool(
        result["processes_stopped"] == stopped
        and result["records_removed"] == removed
        and result["changed"] is bool(stopped or removed)
    )


def _parse_time(value: str | None) -> datetime | None:
    # Older telemetry rows used timezone-less UTC. Workload submissions
    # still require an explicit offset, including the ISO 8601 "Z" suffix.
    return _utc_time(value, assume_naive_utc=True)


def _command_hard_expiry(
    created_at: datetime,
    *,
    action: str,
    ttl_seconds: int,
) -> str:
    """Return a finite execution deadline beyond the initial dispatch lease."""

    if action in {
        "collect_diagnostics",
        "collect_miner_logs",
        AGENT_UPDATE_FORENSICS_ACTION,
    }:
        total_seconds = ttl_seconds
    elif action == "apply_flight_sheet":
        total_seconds = min(
            7_200,
            max(ttl_seconds + 1_200, ttl_seconds * 3),
        )
    elif action in {
        "stop_mining",
        "restart_mining",
        "remove_flight_sheet",
        MINING_RESIDUE_CLEANUP_ACTION,
        MINER_CACHE_CLEANUP_ACTION,
        "apply_gpu_profile",
    }:
        total_seconds = min(
            1_800,
            max(ttl_seconds + 600, ttl_seconds * 3),
        )
    else:
        total_seconds = min(
            3_600,
            max(ttl_seconds + 300, ttl_seconds * 2),
        )
    return (created_at + timedelta(seconds=total_seconds)).isoformat(
        timespec="seconds"
    )


def _renew_running_command_lease(
    db: sqlite3.Connection,
    command: sqlite3.Row | dict[str, Any],
) -> None:
    """Extend a confirmed-running command once, never past its hard deadline."""

    hard_expiry = _parse_time(
        _optional_row_value(command, "hard_expires_at")
    )
    current_expiry = _parse_time(
        _optional_row_value(command, "expires_at")
    )
    now = datetime.now(timezone.utc)
    if (
        hard_expiry is None
        or current_expiry is None
        or hard_expiry <= current_expiry
        or hard_expiry <= now
    ):
        return
    db.execute(
        """UPDATE commands SET expires_at=hard_expires_at
           WHERE id=? AND status IN ('queued','dispatched','running')
             AND hard_expires_at IS NOT NULL
             AND expires_at < hard_expires_at""",
        (command["id"],),
    )


def _boot_time_identity(metrics: Any) -> str | None:
    """Return a stable UTC boot identity from a bounded Agent metric."""

    if not isinstance(metrics, dict):
        return None
    raw = metrics.get("boot_time")
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    parsed = _parse_time(raw)
    if parsed is None:
        return None
    normalized = _utc_time(parsed)
    return normalized.isoformat(timespec="seconds") if normalized is not None else None


def _safe_metric(value: Any) -> float | None:
    try:
        number = float(value)
        return round(number, 1) if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_json_payload(
    value: Any,
    label: str,
    max_bytes: int = 262_144,
    *,
    max_string_lengths: dict[str, int] | None = None,
) -> None:
    """Bound untrusted agent JSON before persisting or rendering it."""
    nodes = 0

    def walk(item: Any, depth: int = 0, field_name: str | None = None) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 5_000 or depth > 8:
            raise HTTPException(422, f"{label} is too complex")
        if isinstance(item, dict):
            if len(item) > 512:
                raise HTTPException(422, f"{label} contains too many fields")
            for key, nested in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise HTTPException(422, f"{label} contains an invalid key")
                walk(nested, depth + 1, key)
        elif isinstance(item, list):
            if len(item) > 512:
                raise HTTPException(422, f"{label} contains too many items")
            for nested in item:
                walk(nested, depth + 1)
        elif isinstance(item, str):
            maximum = (
                max_string_lengths.get(field_name, 8_192)
                if max_string_lengths is not None and field_name is not None
                else 8_192
            )
            if len(item) > maximum:
                raise HTTPException(422, f"{label} contains an oversized string")
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            try:
                finite = math.isfinite(item)
            except OverflowError:
                finite = False
            if not finite:
                raise HTTPException(422, f"{label} contains a non-finite number")
        elif item is not None and not isinstance(item, (bool, int)):
            raise HTTPException(422, f"{label} contains an unsupported value")

    walk(value)
    try:
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"{label} is not valid JSON") from exc
    if len(encoded) > max_bytes:
        raise HTTPException(413, f"{label} exceeds {max_bytes} bytes")

def _normalize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Expose a stable flat summary while retaining the detailed Agent payload."""
    normalized = dict(metrics)
    cpu = metrics.get("cpu")
    if "cpu_percent" not in normalized and isinstance(cpu, dict):
        normalized["cpu_percent"] = cpu.get("percent")
    memory = metrics.get("memory")
    if "memory_percent" not in normalized and isinstance(memory, dict):
        normalized["memory_percent"] = memory.get("percent")
    disks = metrics.get("disks")
    if "disk_percent" not in normalized and isinstance(disks, list):
        percentages = [
            percent
            for disk in disks
            if isinstance(disk, dict) and (percent := _safe_metric(disk.get("percent"))) is not None
        ]
        normalized["disk_percent"] = max(percentages) if percentages else None
    gpus = metrics.get("gpus")
    if isinstance(gpus, list):
        normalized_gpus: list[Any] = []
        for gpu in gpus:
            if not isinstance(gpu, dict):
                normalized_gpus.append(gpu)
                continue
            normalized_gpu = dict(gpu)
            total = _safe_metric(gpu.get("memory_total_mib"))
            used = _safe_metric(gpu.get("memory_used_mib"))
            if "memory_percent" not in normalized_gpu and total and used is not None:
                normalized_gpu["memory_percent"] = round(used / total * 100, 1)
            normalized_gpus.append(normalized_gpu)
        normalized["gpus"] = normalized_gpus
    return normalized


def _mining_text(value: Any, *, maximum: int, fallback: str) -> str:
    """Return a bounded label suitable for a public hashrate series."""

    if not isinstance(value, str):
        return fallback
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        return fallback
    return normalized


def _hashrate_hps(value: Any, unit: Any) -> float | None:
    """Normalize a finite, non-negative hashrate to H/s."""

    if isinstance(value, dict):
        for key in ("now", "current", "total", "total_hashrate"):
            if key in value:
                value = value[key]
                break
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    normalized_unit = str(unit or "H/s").strip().replace(" ", "").lower()
    factor = _HASHRATE_UNIT_FACTORS.get(normalized_unit)
    if factor is None:
        return None
    normalized = numeric * factor
    return normalized if math.isfinite(normalized) else None


def _hashrate_series_identity(
    role: Any,
    coin: Any,
    algorithm: Any,
) -> tuple[str, str, str, str]:
    """Return a stable coin/algorithm identity for history aggregation.

    ``role`` remains in the returned tuple because heartbeat telemetry keeps
    the per-leg diagnostic label, but it deliberately does not participate in
    the series id.  A primary and secondary leg mining the same coin with the
    same algorithm therefore belong to one chart series.

    Older XMRig Agents sometimes omitted the algorithm even though the coin
    was XMR.  XMR's proof-of-work algorithm is RandomX, so normalize that
    legacy ``unknown`` value while reading both new and historical telemetry.
    """

    normalized_role = _mining_text(role, maximum=32, fallback="primary").casefold()
    normalized_coin = _mining_text(coin, maximum=24, fallback="unknown").upper()
    normalized_algorithm = _mining_text(
        algorithm, maximum=64, fallback="unknown"
    ).casefold()
    compact_algorithm = re.sub(r"[^a-z0-9]", "", normalized_algorithm)
    if normalized_coin == "XMR" and compact_algorithm in {
        "",
        "unknown",
        "none",
        "na",
        "rx",
        "rx0",
    }:
        normalized_algorithm = "randomx"
    encoded = json.dumps(
        [normalized_coin, normalized_algorithm],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    series_id = hashlib.sha256(encoded).hexdigest()[:24]
    return series_id, normalized_role, normalized_coin, normalized_algorithm


def _mining_observation_is_current(
    mining: dict[str, Any],
    *,
    observed_at: datetime | None,
    boot_time: datetime | None,
    require_statistics: bool,
) -> bool:
    """Check the new split process/statistics observation, never cached numbers."""

    checked = _parse_time(mining.get("checked_at") if isinstance(mining.get("checked_at"), str) else None)
    if checked is None or observed_at is None or abs((observed_at - checked).total_seconds()) > 60:
        return False
    if boot_time is not None and checked < boot_time:
        return False
    if not require_statistics:
        return True
    updated = _parse_time(mining.get("stats_updated_at") if isinstance(mining.get("stats_updated_at"), str) else None)
    return bool(
        updated is not None
        and updated <= checked + timedelta(seconds=30)
        and checked - updated <= timedelta(seconds=30)
        and (boot_time is None or updated >= boot_time)
    )


def _mining_hashrate_samples(
    inventory: dict[str, Any],
    *,
    observed_at: datetime | None = None,
    boot_time: datetime | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Extract non-secret per-leg samples from one heartbeat.

    A configured, explicitly stopped/failed miner contributes zero.  Missing,
    starting, stopping and unknown readings contribute no sample, so history
    never carries a stale value through a reboot or telemetry outage.
    """

    status_value = inventory.get("mining_status")
    if not isinstance(status_value, dict):
        return None, []
    state = _mining_text(
        status_value.get("state"), maximum=24, fallback="unknown"
    ).casefold()
    raw_running_roles = status_value.get("running_roles")
    running_roles = (
        {
            str(role).casefold()
            for role in raw_running_roles
            if str(role).casefold() in {"primary", "secondary"}
        }
        if status_value.get("degraded") is True
        and isinstance(raw_running_roles, list)
        else set()
    )
    degraded_running = bool(running_roles)
    sample_state = "degraded" if degraded_running else state
    running = state in {"running", "active"} or degraded_running
    explicitly_inactive = state in {"stopped", "failed"}
    reported_errors = (status_value.get("error"), status_value.get("process_error"))
    reported_error_present = any(
        value is not None
        and not (isinstance(value, str) and not value.strip())
        for value in reported_errors
    )
    if "process_verified" in status_value:
        verification_failed = status_value.get("process_verified") is not True
    else:
        # 0.5.8 and earlier Agents cannot attest the managed PID.  Preserve
        # compatibility only when their miner API produced a fresh statistic
        # during this exact inventory collection.  A reboot leaves checked_at
        # current but stats_updated_at old (or absent), so it fails closed.
        checked_at = _parse_time(
            status_value.get("checked_at")
            if isinstance(status_value.get("checked_at"), str)
            else None
        )
        stats_updated_at = _parse_time(
            status_value.get("stats_updated_at")
            if isinstance(status_value.get("stats_updated_at"), str)
            else None
        )
        verification_failed = not (
            checked_at is not None
            and stats_updated_at is not None
            and (
                observed_at is None
                or abs((observed_at - checked_at).total_seconds()) <= 60
            )
            and stats_updated_at <= checked_at + timedelta(seconds=30)
            and checked_at - stats_updated_at <= timedelta(seconds=30)
            and (boot_time is None or stats_updated_at >= boot_time)
        )
    if running and not degraded_running and (
        verification_failed or reported_error_present
    ):
        return sample_state, []
    if not running and not explicitly_inactive:
        return sample_state, []
    if running and "stats_state" in status_value:
        if (
            status_value.get("stats_state") != "fresh"
            or status_value.get("stats_error")
            or not _mining_observation_is_current(
                status_value, observed_at=observed_at, boot_time=boot_time,
                require_statistics=True,
            )
        ):
            return sample_state, []

    # Preserve per-leg heartbeat diagnostics.  The history reader below
    # combines equal coin/algorithm samples from the same device before the
    # fleet total is calculated, so an Agent is counted only once per series.
    samples: dict[tuple[str, str], dict[str, Any]] = {}
    detail_roles: set[str] = set()

    def add_sample(
        role_value: Any,
        detail: dict[str, Any] | None,
        value: Any,
        unit: Any,
        *,
        include_inactive_role: bool = False,
    ) -> None:
        role_hint = _mining_text(role_value, maximum=32, fallback="primary").casefold()
        if (
            degraded_running
            and role_hint in {"primary", "secondary"}
            and role_hint not in running_roles
            and not include_inactive_role
        ):
            return
        leg = status_value.get(role_hint)
        if not isinstance(leg, dict):
            leg = {}
        detail = detail if isinstance(detail, dict) else {}
        if (
            running
            and not include_inactive_role
            and ("stats_updated_at" in detail or "stats_state" in status_value)
        ):
            # Independent miners have their own sampling clocks.  A current
            # CPU observation must not refresh an old or absent GPU sample.
            # An explicit null is unknown. Agents without per-detail clocks
            # or split statistics state retain their existing parent policy.
            observation = status_value
            if "stats_updated_at" in detail:
                observation = {
                    **status_value,
                    "stats_updated_at": detail["stats_updated_at"],
                }
            if not _mining_observation_is_current(
                observation,
                observed_at=observed_at,
                boot_time=boot_time,
                require_statistics=True,
            ):
                return
        coin = detail.get("coin", leg.get("coin"))
        algorithm = detail.get("algorithm", leg.get("algorithm"))
        hashrate = (
            0.0
            if explicitly_inactive or include_inactive_role
            else _hashrate_hps(value, unit)
        )
        if hashrate is None:
            return
        series_id, role, normalized_coin, normalized_algorithm = (
            _hashrate_series_identity(role_hint, coin, algorithm)
        )
        samples[(role, series_id)] = {
            "series": series_id,
            "role": role,
            "coin": normalized_coin,
            "algorithm": normalized_algorithm,
            "hashrate": hashrate,
            "unit": "H/s",
        }

    details = status_value.get("hashrate_details")
    if isinstance(details, list):
        for index, detail in enumerate(details[:8]):
            if not isinstance(detail, dict):
                continue
            role = detail.get("role") or (
                ("primary", "secondary")[index]
                if index < 2
                else f"algorithm_{index + 1}"
            )
            detail_roles.add(
                _mining_text(role, maximum=32, fallback="primary").casefold()
            )
            add_sample(
                role,
                detail,
                detail.get("hashrate"),
                detail.get("hashrate_unit", status_value.get("hashrate_unit")),
            )

    hashrate = status_value.get("hashrate")
    if isinstance(hashrate, dict):
        for role in ("primary", "secondary"):
            # Once details are supplied, an aggregate must not fill missing
            # roles or revive rejected readings with an older value.
            if role in hashrate and not detail_roles:
                add_sample(role, None, hashrate[role], status_value.get("hashrate_unit"))
        if not samples and not detail_roles and any(
            key in hashrate for key in ("now", "current", "total", "total_hashrate")
        ):
            add_sample("primary", None, hashrate, status_value.get("hashrate_unit"))
    elif not samples and not detail_roles and hashrate is not None:
        add_sample("primary", None, hashrate, status_value.get("hashrate_unit"))

    # Stopped heartbeats usually omit hashrate_details.  Emit one explicit
    # zero for each configured leg so the graph drops immediately instead of
    # appearing to retain the previous heartbeat's rate.
    if explicitly_inactive:
        for role in ("primary", "secondary"):
            if isinstance(status_value.get(role), dict):
                add_sample(role, None, 0, "H/s")

    # A dual-miner heartbeat can prove one role live while the other role is
    # unavailable. Preserve the healthy leg and emit an explicit zero for the
    # failed configured leg so its independent chart does not retain a stale
    # pre-failure value.
    if degraded_running:
        for role in ("primary", "secondary"):
            if role not in running_roles and isinstance(status_value.get(role), dict):
                add_sample(
                    role,
                    None,
                    0,
                    "H/s",
                    include_inactive_role=True,
                )

    return sample_state, list(samples.values())


def _telemetry_hashrate_samples(metrics: Any) -> list[dict[str, Any]]:
    """Read and combine one device heartbeat's hashrate samples defensively.

    Historical rows can contain separate primary/secondary samples and old
    XMRig rows can contain `XMR / unknown`. Recomputing the identity here
    keeps the 24-hour chart migration immediate without rewriting telemetry.
    """

    if not isinstance(metrics, dict) or not isinstance(
        metrics.get("hashrate_samples"), list
    ):
        return []
    samples: dict[str, dict[str, Any]] = {}
    for sample in metrics["hashrate_samples"][:16]:
        if not isinstance(sample, dict):
            continue
        value = _hashrate_hps(sample.get("hashrate"), sample.get("unit"))
        if value is None:
            continue
        series_id, role, coin, algorithm = _hashrate_series_identity(
            sample.get("role"), sample.get("coin"), sample.get("algorithm")
        )
        existing = samples.get(series_id)
        if existing is None:
            samples[series_id] = {
                "series": series_id,
                "role": role,
                "coin": coin,
                "algorithm": algorithm,
                "hashrate": value,
                "unit": "H/s",
            }
        else:
            existing["hashrate"] = float(existing["hashrate"]) + value
            if existing["role"] != role:
                existing["role"] = "combined"
    return list(samples.values())


_TELEMETRY_PRUNE_BATCH_ROWS = 10_000


# Reject extreme totals and short-lived jumps against the recent baseline
# for the same device and series before adding them to hashrate history.
_HASHRATE_SPIKE_ABSOLUTE_LIMIT_HPS = 4e15
_HASHRATE_SPIKE_BASELINE_BUCKETS = 12
_HASHRATE_SPIKE_BASELINE_MIN_SAMPLES = 3
_HASHRATE_SPIKE_BASELINE_TOLERANCE = 10.0


def _hashrate_series_baseline_values(
    db: sqlite3.Connection, agent_id: str, series_id: str
) -> list[float]:
    """Collect recent sane rates for one device series, newest bucket first.

    Values above the absolute limit are excluded so already-poisoned history
    (written before this gate existed) cannot raise the baseline and let new
    spikes pass.  ``samples_json`` stores at most one entry per series per
    bucket; duplicates from very old rows are harmless for a median.
    """

    rows = db.execute(
        """SELECT samples_json FROM hashrate_history_buckets
           WHERE agent_id=? ORDER BY bucket_start DESC LIMIT ?""",
        (agent_id, _HASHRATE_SPIKE_BASELINE_BUCKETS),
    ).fetchall()
    values: list[float] = []
    for row in rows:
        samples = _json(row["samples_json"], [])
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, dict) or sample.get("series") != series_id:
                continue
            rate = _hashrate_hps(sample.get("hashrate"), sample.get("unit"))
            if (
                rate is not None
                and rate <= _HASHRATE_SPIKE_ABSOLUTE_LIMIT_HPS
            ):
                values.append(rate)
    return values


def _gate_hashrate_samples(
    db: sqlite3.Connection,
    agent_id: str,
    samples: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reject per-series samples whose amplitude is physically implausible.

    A sample is rejected when it exceeds the conservative absolute cap, or
    when the same device's recent baseline for the same series exists and
    the sample exceeds that baseline's tolerance band.  Zeros, unknown
    states and legitimate rates are never rejected: a zero baseline (stopped
    device) disables the relative rule, and the first samples of a new
    series only face the absolute cap.  Returns the kept samples plus one
    rejection record per refused sample for the audit trail.
    """

    kept: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    baselines: dict[str, float | None] = {}
    for sample in samples:
        series_id = str(sample.get("series"))
        value = _hashrate_hps(sample.get("hashrate"), sample.get("unit"))
        if value is None:
            kept.append(sample)
            continue
        if value <= _HASHRATE_SPIKE_ABSOLUTE_LIMIT_HPS:
            if series_id not in baselines:
                baseline_values = _hashrate_series_baseline_values(
                    db, agent_id, series_id
                )
                median = (
                    statistics.median(baseline_values)
                    if len(baseline_values) >= _HASHRATE_SPIKE_BASELINE_MIN_SAMPLES
                    else None
                )
                baselines[series_id] = median
            median = baselines[series_id]
            if (
                median is not None
                and median > 0
                and value > median * _HASHRATE_SPIKE_BASELINE_TOLERANCE
            ):
                rejections.append(
                    {
                        "series": series_id,
                        "coin": sample.get("coin"),
                        "algorithm": sample.get("algorithm"),
                        "hashrate": value,
                        "unit": "H/s",
                        "rule": "baseline_band",
                        "baseline_hps": float(median),
                        "threshold_hps": float(median)
                        * _HASHRATE_SPIKE_BASELINE_TOLERANCE,
                    }
                )
                continue
            kept.append(sample)
            continue
        rejections.append(
            {
                "series": series_id,
                "coin": sample.get("coin"),
                "algorithm": sample.get("algorithm"),
                "hashrate": value,
                "unit": "H/s",
                "rule": "absolute_cap",
                "threshold_hps": _HASHRATE_SPIKE_ABSOLUTE_LIMIT_HPS,
            }
        )
    return kept, rejections


def _hashrate_bucket_start(moment: datetime) -> datetime:
    return workload_bucket_start(moment)


def _record_hashrate_history_bucket(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    recorded_at: str,
    metrics: dict[str, Any],
) -> None:
    """Keep only the final compact hashrate snapshot in each five-minute bucket."""

    moment = _parse_time(recorded_at)
    if moment is None:
        return
    bucket_start = _hashrate_bucket_start(moment).isoformat(timespec="seconds")
    samples_json = json.dumps(
        _telemetry_hashrate_samples(metrics),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    db.execute(
        """INSERT INTO hashrate_history_buckets
           (agent_id,bucket_start,recorded_at,samples_json)
           VALUES(?,?,?,?)
           ON CONFLICT(agent_id,bucket_start) DO UPDATE SET
               recorded_at=excluded.recorded_at,
               samples_json=excluded.samples_json
           WHERE excluded.recorded_at > hashrate_history_buckets.recorded_at""",
        (agent_id, bucket_start, recorded_at, samples_json),
    )


def _agent_alerts(agent: dict[str, Any], settings: Settings) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    if agent["status"] == "offline":
        alerts.append({"level": "critical", "message": "设备离线"})
        return alerts
    metrics = agent["metrics"]
    thresholds = (
        ("cpu_percent", 90, "CPU 使用率过高"),
        ("memory_percent", 90, "内存使用率过高"),
        ("disk_percent", 90, "磁盘使用率过高"),
    )
    for key, threshold, message in thresholds:
        value = _safe_metric(metrics.get(key))
        if value is not None and value >= threshold:
            alerts.append({"level": "warning", "message": f"{message} ({value}%)"})
    for gpu in metrics.get("gpus", []) if isinstance(metrics.get("gpus"), list) else []:
        if not isinstance(gpu, dict):
            continue
        temperature = _safe_metric(gpu.get("temperature_c"))
        if temperature is not None and temperature >= 85:
            name = gpu.get("name") or f"GPU {gpu.get('index', '?')}"
            alerts.append(
                {"level": "critical", "message": f"{name} 温度过高 ({temperature}°C)"}
            )
    return alerts


def _canonical_ip(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_unspecified or address.is_loopback or address.is_multicast:
        return None
    return str(address)


def _network_ip_list(values: Any, *, public: bool) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for raw in values[:32]:
        canonical = _canonical_ip(raw)
        if canonical is None:
            continue
        address = ipaddress.ip_address(canonical)
        matches = (
            address.is_global
            if public
            else any(
                address.version == network.version and address in network
                for network in LAN_NETWORKS
            )
        )
        if matches and canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _reported_network_addresses(body: HeartbeatRequest) -> tuple[list[str], list[str]]:
    """Read new network fields, then fall back to legacy heartbeat inventory."""

    network = body.inventory.get("network")
    if not isinstance(network, dict):
        network = {}
    legacy = body.inventory.get("ip_addresses")
    local = _network_ip_list(body.local_ip_addresses, public=False)
    if not local:
        local = _network_ip_list(network.get("local_ip_addresses"), public=False)
    if not local:
        local = _network_ip_list(legacy, public=False)
    if not local:
        local = _network_ip_list(body.ip_addresses, public=False)

    public = _network_ip_list(body.public_ip_addresses, public=True)
    if not public:
        public = _network_ip_list(network.get("public_ip_addresses"), public=True)
    if not public:
        public = _network_ip_list(legacy, public=True)
    if not public:
        public = _network_ip_list(body.ip_addresses, public=True)
    return local, public


def _serialize_agent(row: sqlite3.Row, settings: Settings) -> dict[str, Any]:
    last_seen = _parse_time(row["last_seen"])
    online = bool(
        last_seen
        and row["disabled_at"] is None
        and datetime.now(timezone.utc) - last_seen < timedelta(seconds=settings.agent_online_seconds)
    )
    inventory = _json(row["inventory_json"], {})
    stored_local = _network_ip_list(
        _json(_optional_row_value(row, "local_ip_addresses_json", "[]"), []),
        public=False,
    )
    if not stored_local and isinstance(inventory, dict):
        network = inventory.get("network")
        stored_local = _network_ip_list(
            network.get("local_ip_addresses") if isinstance(network, dict) else None,
            public=False,
        ) or _network_ip_list(inventory.get("ip_addresses"), public=False)
    public_candidates = _network_ip_list(
        [
            _optional_row_value(row, "public_ip"),
            _optional_row_value(row, "last_ip"),
        ],
        public=True,
    )
    agent = {
        "id": row["id"],
        "name": row["name"],
        "hostname": row["hostname"],
        "os_family": row["os_family"],
        "platform": row["platform"],
        "agent_version": row["agent_version"],
        "note": _optional_row_value(row, "note", ""),
        # Deprecated API compatibility: tags are no longer used by the web UI,
        # but retain the wire field while older clients are still deployed.
        "tags": _json(row["tags_json"], []),
        "inventory": inventory,
        "metrics": _json(row["metrics_json"], {}),
        "capabilities": _json(_optional_row_value(row, "capabilities_json", "[]"), []),
        # Desired NVML settings live on the controller and intentionally remain
        # present while the hardware is reset after a stop or host reboot.
        "desired_gpu_oc": _desired_gpu_oc_entries(
            _optional_row_value(row, "desired_gpu_oc_json", "[]")
        ),
        "mining_autostart_enabled": bool(
            _optional_row_value(row, "mining_autostart_enabled", 0)
        ),
        "mining_autostart_last_boot_time": _optional_row_value(
            row, "mining_autostart_last_boot_time"
        ),
        "last_ip": row["last_ip"],
        "public_ip": public_candidates[0] if public_candidates else None,
        "local_ip": stored_local[0] if stored_local else None,
        "local_ip_addresses": stored_local,
        "last_seen": row["last_seen"],
        "enrolled_at": row["enrolled_at"],
        "disabled": row["disabled_at"] is not None,
        "status": "online" if online else ("disabled" if row["disabled_at"] else "offline"),
    }
    agent["alerts"] = _agent_alerts(agent, settings)
    return agent


def _reported_agent_flight_sheet_id(
    row: sqlite3.Row | dict[str, Any],
) -> str | None:
    """Return the configured sheet independent of online/running state."""

    inventory = _json(row["inventory_json"], {})
    metrics = _json(row["metrics_json"], {})
    mining = inventory.get("mining_status") if isinstance(inventory, dict) else None
    if not isinstance(mining, dict) and isinstance(metrics, dict):
        mining = metrics.get("mining_status")
    if not isinstance(mining, dict) or mining.get("configured") is False:
        return None
    sheet_id = mining.get("flight_sheet_id")
    if not isinstance(sheet_id, str):
        return None
    sheet_id = sheet_id.strip()
    return sheet_id if sheet_id and len(sheet_id) <= 64 else None


def _agent_explicitly_reports_no_flight_sheet(
    row: sqlite3.Row | dict[str, Any],
) -> bool:
    inventory = _json(row["inventory_json"], {})
    metrics = _json(row["metrics_json"], {})
    mining = inventory.get("mining_status") if isinstance(inventory, dict) else None
    if not isinstance(mining, dict) and isinstance(metrics, dict):
        mining = metrics.get("mining_status")
    return isinstance(mining, dict) and mining.get("configured") is False


def _active_serialized_agent_flight_sheet_id(
    agent: dict[str, Any],
) -> str | None:
    """Return the sheet only when a serialized online Agent proves its miner is live."""

    if agent["status"] != "online":
        return None
    inventory = agent["inventory"] if isinstance(agent["inventory"], dict) else {}
    metrics = agent["metrics"] if isinstance(agent["metrics"], dict) else {}
    mining = inventory.get("mining_status")
    if not isinstance(mining, dict):
        mining = metrics.get("mining_status")
    if not isinstance(mining, dict):
        return None
    if str(mining.get("state") or "").casefold() not in {"running", "active"}:
        return None
    if mining.get("configured") is False:
        return None
    sheet_id = mining.get("flight_sheet_id")
    if not isinstance(sheet_id, str) or not sheet_id.strip() or len(sheet_id) > 64:
        return None
    if "process_verified" in mining:
        if (
            mining.get("process_verified") is not True
            or mining.get("process_error")
            or mining.get("error")
        ):
            return None
        if "stats_state" in mining and not _mining_observation_is_current(
            mining,
            observed_at=_parse_time(agent.get("last_seen") if isinstance(agent.get("last_seen"), str) else None),
            boot_time=_parse_time(metrics.get("boot_time") if isinstance(metrics.get("boot_time"), str) else None),
            require_statistics=False,
        ):
            return None
        return sheet_id
    checked_at = _parse_time(
        mining.get("checked_at") if isinstance(mining.get("checked_at"), str) else None
    )
    stats_at = _parse_time(
        mining.get("stats_updated_at")
        if isinstance(mining.get("stats_updated_at"), str)
        else None
    )
    observed_at = _parse_time(
        agent.get("last_seen") if isinstance(agent.get("last_seen"), str) else None
    )
    boot_at = _parse_time(
        metrics.get("boot_time") if isinstance(metrics.get("boot_time"), str) else None
    )
    if mining.get("error") or checked_at is None or stats_at is None:
        return None
    if observed_at is not None and abs((observed_at - checked_at).total_seconds()) > 60:
        return None
    if stats_at > checked_at + timedelta(seconds=30):
        return None
    if checked_at - stats_at > timedelta(seconds=30):
        return None
    if boot_at is not None and stats_at < boot_at:
        return None
    return sheet_id


def _active_agent_flight_sheet_id(
    row: sqlite3.Row | dict[str, Any], settings: Settings
) -> str | None:
    """Serialize one Agent row and return its verified active flight sheet."""

    return _active_serialized_agent_flight_sheet_id(_serialize_agent(row, settings))


def _serialized_agent_has_current_hashrate(agent: dict[str, Any]) -> bool:
    """Count an Agent only when every configured miner reports current work.

    Process truth remains separate: a miner warming up, reconnecting or
    reporting zero may still be running and must remain controllable.
    """

    if _active_serialized_agent_flight_sheet_id(agent) is None:
        return False
    inventory = agent["inventory"] if isinstance(agent["inventory"], dict) else {}
    metrics = agent["metrics"] if isinstance(agent["metrics"], dict) else {}
    mining = inventory.get("mining_status")
    if not isinstance(mining, dict):
        mining = metrics.get("mining_status")
    if not isinstance(mining, dict):
        return False
    if (
        ("stats_state" in mining and mining["stats_state"] != "fresh")
        or mining.get("stats_error")
    ):
        return False
    observed_at = _parse_time(
        agent.get("last_seen") if isinstance(agent.get("last_seen"), str) else None
    )
    boot_time = _parse_time(
        metrics.get("boot_time") if isinstance(metrics.get("boot_time"), str) else None
    )
    if not _mining_observation_is_current(
        mining,
        observed_at=observed_at,
        boot_time=boot_time,
        require_statistics=True,
    ):
        return False
    configured_roles = {
        role for role in ("primary", "secondary") if isinstance(mining.get(role), dict)
    } or {"primary"}
    details = mining.get("hashrate_details")
    reported_details = (
        [detail for detail in details[:8] if isinstance(detail, dict)]
        if isinstance(details, list)
        else []
    )
    if reported_details:
        detailed_roles: set[str] = set()
        for index, detail in enumerate(reported_details):
            role = detail.get("role") or (
                ("primary", "secondary")[index]
                if index < 2
                else f"algorithm_{index + 1}"
            )
            role = _mining_text(role, maximum=32, fallback="unknown").casefold()
            if role in {"primary", "secondary"}:
                configured_roles.add(role)
                detailed_roles.add(role)
            rate = _hashrate_hps(
                detail.get("hashrate"),
                detail.get("hashrate_unit", mining.get("hashrate_unit")),
            )
            observation = mining
            if "stats_updated_at" in detail:
                observation = {**mining, "stats_updated_at": detail["stats_updated_at"]}
            if rate is None or rate <= 0 or not _mining_observation_is_current(
                observation,
                observed_at=observed_at,
                boot_time=boot_time,
                require_statistics=True,
            ):
                return False
        # Explicit rows describe the reported miners. A parent's aggregate
        # cannot fill an absent configured role or hide a zero/old row with
        # another positive row bearing the same role.
        return configured_roles <= detailed_roles
    hashrate = mining.get("hashrate")
    if isinstance(hashrate, dict):
        configured_roles.update(
            role for role in ("primary", "secondary") if role in hashrate
        )
    _, samples = _mining_hashrate_samples(
        {"mining_status": mining}, observed_at=observed_at, boot_time=boot_time
    )
    positive_roles = {
        sample["role"] for sample in samples if sample["hashrate"] > 0
    }
    return configured_roles <= positive_roles


def _flight_sheet_configuration_fingerprint(
    row: sqlite3.Row | dict[str, Any],
) -> str:
    columns = (
        "miner_type",
        "miner_version",
        "coin",
        "wallet_id",
        "pool_url",
        "miner_profile",
        "miner_package_id",
        "execution_mode",
        "algorithm",
        "pool_password",
        "device_indices_json",
        "worker_name_template",
        "extra_config_json",
        "gpu_oc_json",
        "secondary_mode",
        "secondary_miner_type",
        "secondary_miner_version",
        "secondary_coin",
        "secondary_wallet_id",
        "secondary_pool_url",
        "secondary_miner_profile",
        "secondary_miner_package_id",
        "secondary_execution_mode",
        "secondary_algorithm",
        "secondary_pool_password",
        "secondary_device_indices_json",
        "secondary_extra_config_json",
    )
    keys = set(row.keys())
    configuration = {column: row[column] for column in columns}
    configuration["execution_mode"] = row["execution_mode"] or "native"
    configuration["secondary_execution_mode"] = (
        (row["secondary_execution_mode"] or "native")
        if row["secondary_coin"]
        else None
    )
    for column in ("wallet_address", "secondary_wallet_address"):
        configuration[column] = row[column] if column in keys else None
    encoded = json.dumps(
        configuration,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _agent_version_at_least(value: str | None, minimum: tuple[int, int, int]) -> bool:
    """Compare the numeric prefix used by released RigPilot Agents."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\D|$)", value or "")
    return bool(match and tuple(int(item) for item in match.groups()) >= minimum)


def _strict_agent_version(value: str | None) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value or "")
    return tuple(int(item) for item in match.groups()) if match else None


def _require_extra_args_target(agent: sqlite3.Row | dict[str, Any]) -> None:
    capabilities = _json(agent["capabilities_json"], [])
    if not _agent_version_at_least(agent["agent_version"], (0, 8, 31)) or EXTRA_ARGS_CAPABILITY not in capabilities:
        raise HTTPException(409, EXTRA_ARGS_TARGET_MESSAGE)


def _flight_has_extra_args(payload: dict[str, Any]) -> bool:
    found = False
    for role in ("primary", "secondary"):
        leg = payload.get(role)
        if not isinstance(leg, dict) or not isinstance(leg.get("miner_type"), str) or leg["miner_type"] not in MINER_TYPES:
            continue
        options = leg.get("extra_config", {})
        if not isinstance(options, dict):
            raise HTTPException(409, "Stored miner options are invalid")
        try:
            arguments = parse_extra_args(options.get("extra_args", ""), miner_type=leg["miner_type"])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        found = found or bool(arguments)
    return found


def _validate_command(command: CommandCreate) -> dict[str, Any]:
    if CONTROLLER_GPU_OC_INTENT_MARKER in command.payload:
        raise HTTPException(422, "Controller GPU OC intent is not a public payload field")
    payload = dict(command.payload)
    if command.action == "restart_service":
        service = str(payload.get("service", "")).strip()
        if not service or len(service) > 128:
            raise HTTPException(422, "restart_service requires a valid service name")
        if any(character in service for character in ";&|`$<>\n\r"):
            raise HTTPException(422, "invalid service name")
        payload = {"service": service}
    elif command.action in DESTRUCTIVE_ACTIONS:
        if payload:
            raise HTTPException(422, "reboot and shutdown payloads must be empty")
        payload = {}
    elif command.action == "collect_miner_logs":
        if payload:
            raise HTTPException(422, "collect_miner_logs payload must be empty")
        payload = {}
    elif command.action == MINER_CACHE_SCAN_ACTION:
        if payload:
            raise HTTPException(422, "miner cache scan payload must be empty")
        payload = {}
    elif command.action == MINER_CACHE_CLEANUP_ACTION:
        fingerprint = payload.get("scan_fingerprint")
        if set(payload) != {"scan_fingerprint"} or not isinstance(
            fingerprint, str
        ) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise HTTPException(422, "miner cache cleanup payload is invalid")
        payload = {"scan_fingerprint": fingerprint}
    elif command.action == MINING_RESIDUE_SCAN_ACTION:
        if payload:
            raise HTTPException(422, "mining residue scan payload must be empty")
        payload = {}
    elif command.action == MINING_RESIDUE_CLEANUP_ACTION:
        old_fields = {"scan_fingerprint"}
        new_fields = {
            "scan_fingerprint",
            "targets",
            "reset_srbminer_oc",
            "reset_nvml_oc",
        }
        if set(payload) not in (old_fields, new_fields) or re.fullmatch(
            r"[0-9a-f]{64}", str(payload.get("scan_fingerprint") or "")
        ) is None:
            raise HTTPException(
                422,
                "mining residue cleanup payload is invalid",
            )
        if set(payload) == old_fields:
            payload = {"scan_fingerprint": str(payload["scan_fingerprint"])}
        else:
            targets = payload.get("targets")
            if not isinstance(targets, list) or len(targets) > 3:
                raise HTTPException(422, "mining residue cleanup targets are invalid")
            normalized_targets: list[dict[str, str]] = []
            seen_roles: set[str] = set()
            for target in targets:
                if not isinstance(target, dict) or set(target) not in (
                    {"role", "target_fingerprint"},
                    {
                        "role",
                        "target_fingerprint",
                        "candidate_fingerprint",
                    },
                ):
                    raise HTTPException(
                        422, "mining residue cleanup target is invalid"
                    )
                role = target.get("role")
                target_fingerprint = target.get("target_fingerprint")
                candidate_fingerprint = target.get("candidate_fingerprint")
                if (
                    not isinstance(role, str)
                    or role not in {"legacy", "primary", "secondary"}
                    or role in seen_roles
                    or re.fullmatch(
                        r"[0-9a-f]{64}", str(target_fingerprint or "")
                    )
                    is None
                    or (
                        candidate_fingerprint is not None
                        and re.fullmatch(
                            r"[0-9a-f]{64}",
                            str(candidate_fingerprint or ""),
                        )
                        is None
                    )
                ):
                    raise HTTPException(
                        422, "mining residue cleanup target is invalid"
                    )
                seen_roles.add(str(role))
                normalized_target = {
                    "role": str(role),
                    "target_fingerprint": str(target_fingerprint),
                }
                if candidate_fingerprint is not None:
                    normalized_target["candidate_fingerprint"] = str(
                        candidate_fingerprint
                    )
                normalized_targets.append(normalized_target)
            reset_srb = payload.get("reset_srbminer_oc")
            reset_nvml = payload.get("reset_nvml_oc")
            if not isinstance(reset_srb, bool) or not isinstance(
                reset_nvml, bool
            ):
                raise HTTPException(
                    422, "mining residue cleanup OC flags are invalid"
                )
            if not normalized_targets and not reset_srb and not reset_nvml:
                raise HTTPException(
                    422, "select at least one residue target or OC reset option"
                )
            payload = {
                "scan_fingerprint": str(payload["scan_fingerprint"]),
                "targets": normalized_targets,
                "reset_srbminer_oc": reset_srb,
                "reset_nvml_oc": reset_nvml,
            }
    elif command.action in MINING_CONTROL_ACTIONS:
        if payload:
            raise HTTPException(422, "mining control payload must be empty")
        payload = {}
    else:
        # The closed action Literal leaves only collect_diagnostics here;
        # it has no options and historically ignores redundant payload keys.
        payload = {}
    return payload


def _serialize_command(
    row: sqlite3.Row, *, role: str, detail: bool = False
) -> dict[str, Any]:
    operator = ROLE_LEVEL[role] >= ROLE_LEVEL["operator"]
    hide_retired = row["action"] in _RETIRED_WORKSPACE_ACTIONS
    result = {} if hide_retired else _json(row["result_json"], {})
    if row["action"] == "collect_miner_logs" and not (detail and operator):
        logs = result.get("logs") if isinstance(result, dict) else None
        result = {
            "captured_at": result.get("captured_at") if isinstance(result, dict) else None,
            "window_seconds": result.get("window_seconds") if isinstance(result, dict) else None,
            "miner_type": result.get("miner_type") if isinstance(result, dict) else None,
            "running": result.get("running") if isinstance(result, dict) else None,
            "source_count": len(logs) if isinstance(logs, list) else 0,
            "log_output_omitted": bool(logs),
        }
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"],
        "action": row["action"],
        "payload": {} if hide_retired else _command_payload_for_view(row["payload_json"]),
        "status": row["status"],
        "created_by": row["created_by_name"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "dispatched_at": row["dispatched_at"],
        "finished_at": row["finished_at"],
        "result": result,
        "error": None if hide_retired else row["error"],
    }


def _abandon_agent_commands(
    db: sqlite3.Connection,
    *,
    agent_id: str,
    command_ids: list[str] | None,
    actor: str,
    reason: str,
    now: str,
) -> list[dict[str, str]]:
    """Stop tracking active commands that an offline Agent cannot acknowledge.

    A dispatched/running command cannot be truthfully described as remotely
    interrupted: it may already have changed the device.  This operation only
    closes the controller ledger, and the audit record preserves that fact.
    Terminal result reports received later remain ignored by
    ``agent_command_result``.
    """

    params: list[Any] = [agent_id]
    query = """SELECT id, action, status FROM commands
               WHERE agent_id=?
                 AND status IN ('queued','dispatched','running')"""
    if command_ids is not None:
        if not command_ids:
            return []
        placeholders = ",".join("?" for _ in command_ids)
        query += f" AND id IN ({placeholders})"
        params.extend(command_ids)
    query += " ORDER BY created_at, id"
    rows = db.execute(query, params).fetchall()
    if not rows:
        return []

    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    # Abandoning only the chained post-update policy must also close its
    # parent activating update. Otherwise the job becomes failed while the
    # parent command remains "running" and continues to block this device.
    orphaned_update_jobs = db.execute(
        f"""SELECT command_id, agent_id, post_update_command_id
            FROM agent_update_jobs
            WHERE post_update_command_id IN ({placeholders})
              AND command_id NOT IN ({placeholders})
              AND status='activating'""",
        (*ids, *ids),
    ).fetchall()
    db.execute(
        f"""UPDATE commands
            SET status='cancelled', finished_at=?, error=COALESCE(error, ?)
            WHERE id IN ({placeholders})
              AND status IN ('queued','dispatched','running')""",
        (now, reason, *ids),
    )
    uncertain_apply_ids = [
        row["id"]
        for row in rows
        if row["action"] == "apply_flight_sheet"
        and row["status"] in {"dispatched", "running"}
    ]
    uncertain_apply_id_set = set(uncertain_apply_ids)
    cancelled_refresh_ids = [
        row["id"] for row in rows if row["id"] not in uncertain_apply_id_set
    ]
    if cancelled_refresh_ids:
        cancelled_placeholders = ",".join("?" for _ in cancelled_refresh_ids)
        db.execute(
            f"""UPDATE configuration_refreshes
                SET status='cancelled', updated_at=?, completed_at=?,
                    last_error=?
                WHERE command_id IN ({cancelled_placeholders})
                  AND status IN (
                      'pending','preparing','queued','dispatched','running'
                  )""",
            (now, now, reason, *cancelled_refresh_ids),
        )
    if uncertain_apply_ids:
        uncertain_placeholders = ",".join("?" for _ in uncertain_apply_ids)
        db.execute(
            f"""UPDATE configuration_refreshes
                SET status='pending', command_id=NULL, updated_at=?,
                    queued_at=NULL, completed_at=NULL,
                    last_error=?
                WHERE command_id IN ({uncertain_placeholders})
                  AND status IN ('queued','dispatched','running')""",
            (
                now,
                (
                    "controller stopped tracking an in-flight flight-sheet apply; "
                    "waiting to verify and reapply after the device reconnects"
                ),
                *uncertain_apply_ids,
            ),
        )
    db.execute(
        f"""UPDATE agent_update_jobs
            SET status='cancelled', updated_at=?, completed_at=?,
                last_error=?
            WHERE command_id IN ({placeholders})
              AND status IN ('queued','dispatched','activating')""",
        (now, now, reason, *ids),
    )
    db.execute(
        f"""UPDATE agent_update_jobs
            SET status='failed', updated_at=?, completed_at=?, last_error=?
            WHERE post_update_command_id IN ({placeholders})
              AND status='activating'""",
        (now, now, reason, *ids),
    )
    for update_job in orphaned_update_jobs:
        db.execute(
            """UPDATE commands SET status='failed', finished_at=?,
                       error=COALESCE(error, ?)
               WHERE id=? AND status IN ('queued','dispatched','running')""",
            (now, reason, update_job["command_id"]),
        )
        Database.audit(
            db,
            actor=actor,
            action="agent_update_activation_failed",
            target_type="agent",
            target_id=update_job["agent_id"],
            details={
                "command_id": update_job["command_id"],
                "post_update_command_id": update_job["post_update_command_id"],
                "reason": reason,
            },
        )
    abandoned = [
        {"id": row["id"], "action": row["action"], "previous_status": row["status"]}
        for row in rows
    ]
    for item in abandoned:
        Database.audit(
            db,
            actor=actor,
            action="command_abandoned",
            target_type="command",
            target_id=item["id"],
            details={
                "agent_id": agent_id,
                "action": item["action"],
                "previous_status": item["previous_status"],
                "reason": reason,
            },
        )
    return abandoned


def _validate_saved_pool_url(value: str) -> str:
    """Store only explicit standard URLs in the saved pool catalog."""
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise HTTPException(422, "pool_url contains whitespace or control characters")
    try:
        scheme = urlparse(value).scheme.lower()
    except ValueError as exc:
        raise HTTPException(422, "pool_url is malformed") from exc
    if scheme == "quic":
        try:
            return parse_pool_endpoint(value, {"quic"}).url
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if scheme == "pearl":
        return _validate_pool_url(value, miner_type="pearlminer")
    return _validate_pool_url(value)


def _validate_pool_url(value: str, *, miner_type: str = "generic") -> str:
    normalized_type = str(miner_type or "generic").strip().lower()
    adapter = MINER_ADAPTERS.get(normalized_type)
    allowed = adapter.pool_schemes if adapter else {"stratum+tcp", "stratum+ssl", "stratum+tls"}
    try:
        return parse_pool_endpoint(value, allowed).url
    except ValueError as exc:
        detail = str(exc)
        if detail == "pool_url uses an unsupported protocol":
            detail = ("Pearl Miner pool URL must use pearl://host:port, stratum+tcp://host:port, or stratum+ssl://host:port"
                      if normalized_type == "pearlminer" else "pool_url must use stratum+tcp, stratum+ssl, or stratum+tls")
        raise HTTPException(422, detail) from exc


def _normalize_managed_options(
    *,
    miner_type: str,
    coin: str,
    algorithm: str | None,
    pool_url: str,
    extra_config: dict[str, Any],
    miner_version: str | None = None,
    pool_password: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Validate the small typed option set exposed by the simple form."""

    if str(miner_type).strip().lower() in {"seine", "org.zpalmtree.seine"} and pool_password not in (None, ""):
        raise HTTPException(422, "Seine does not support pool_password; leave it empty")
    if miner_type == "generic":
        return algorithm, dict(extra_config)
    try:
        normalized_algorithm = normalize_algorithm(miner_type, coin, algorithm)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    adapter = MINER_ADAPTERS.require(miner_type)
    unknown = set(extra_config) - adapter.option_keys
    if unknown:
        prefix = (
            "Unsupported Pearl Miner options: "
            if miner_type == "pearlminer"
            else "Unsupported managed miner options: "
        )
        raise HTTPException(
            422,
            prefix + ", ".join(sorted(unknown)),
        )
    normalized = dict(extra_config)

    for option in adapter.options:
        key = option.name
        if key not in normalized:
            if option.default is not None:
                normalized[key] = option.default
            continue
        value = normalized[key]
        if value is None and option.nullable:
            normalized.pop(key, None)
            continue
        try:
            normalized[key] = validate_option_value(option, value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    try:
        normalized_algorithm = adapter.validate_execution_context(
            normalized_algorithm, miner_version, normalized, coin=coin,
        )
        adapter.algorithm_resource(normalized_algorithm, coin=coin, resource_kind=normalized.get("resource_kind"))
        adapter.validate_pool_transport(pool_url, normalized_algorithm, miner_version, normalized, pool_password=pool_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # TLS is derived from the validated endpoint and cannot be spoofed by a
    # form or an adapter manifest.
    if "tls" in adapter.option_keys:
        normalized["tls"] = urlparse(pool_url).scheme.lower() in {
            "stratum+ssl",
            "stratum+tls",
        }
    if miner_type == "pearlminer":
        gpu_backend = normalized.get("gpu_backend", "auto")
        cuda_variant = normalized.get("cuda_variant", "auto")
        if gpu_backend == "amd" and cuda_variant != "auto":
            raise HTTPException(
                422, "cuda_variant is available only with the NVIDIA backend"
            )
    return normalized_algorithm, normalized


def _validate_managed_flight_shape(body: FlightSheetCreate) -> None:
    """Reject fields that a selected managed adapter cannot address safely."""

    legs = [body]
    if body.secondary is not None:
        legs.append(body.secondary)
    for leg in legs:
        adapter = MINER_ADAPTERS.get(leg.miner_type)
        if adapter is None:
            continue
        if leg.device_indices and "device_selection" not in adapter.capabilities:
            label = "Pearl Miner" if leg.miner_type == "pearlminer" else adapter.display_name
            raise HTTPException(
                422,
                f"{label} does not support device_indices in the managed flight sheet",
            )


def _pearl_inventory_gpu_vendors(
    agent: sqlite3.Row | dict[str, Any],
) -> tuple[set[str], bool]:
    """Return supported discrete vendors plus whether any card was unresolved."""

    inventory = _json(agent["inventory_json"], {})
    metrics = _json(agent["metrics_json"], {})
    candidate_lists: list[Any] = []
    for document in (inventory, metrics):
        if not isinstance(document, dict):
            continue
        for key in ("gpus", "discrete_gpus", "gpu_devices"):
            candidate_lists.append(document.get(key))
        hardware = document.get("hardware")
        if isinstance(hardware, dict):
            candidate_lists.append(hardware.get("gpus"))
    if isinstance(inventory, dict):
        mining = inventory.get("mining_status")
        if isinstance(mining, dict):
            candidate_lists.append(mining.get("gpu_mapping"))

    vendors: set[str] = set()
    unresolved = False
    found = False
    for candidates in candidate_lists:
        if not isinstance(candidates, list):
            continue
        for item in candidates[:64]:
            if not isinstance(item, dict):
                continue
            device_type = str(item.get("device_type") or "").strip().casefold()
            if item.get("discrete") is False or device_type in {
                "integrated",
                "igpu",
            }:
                continue
            found = True
            identity = " ".join(
                str(item.get(key) or "")
                for key in ("vendor", "manufacturer", "name", "model")
            ).casefold()
            if "nvidia" in identity or "geforce" in identity:
                vendors.add("nvidia")
            elif (
                "amd" in identity
                or "advanced micro devices" in identity
                or "radeon" in identity
            ):
                vendors.add("amd")
            else:
                unresolved = True
    return vendors, unresolved or not found


def _pearl_package_variant(
    agent: sqlite3.Row | dict[str, Any],
    extra_config: dict[str, Any],
    *,
    execution_os_family: str | None = None,
) -> str | None:
    """Resolve the one official Pearl Miner package that can run on a rig."""

    os_family = (
        normalize_os_family(execution_os_family)
        if execution_os_family is not None
        else normalize_os_family(str(agent["os_family"] or ""))
    )
    requested = str(extra_config.get("gpu_backend") or "auto").strip().lower()
    if os_family == "windows":
        if requested == "amd":
            raise HTTPException(
                409,
                "Pearl Miner for Windows is available only as the official NVIDIA miner.exe package",
            )
        return None
    if requested in {"nvidia", "amd"}:
        return requested
    vendors, unresolved = _pearl_inventory_gpu_vendors(agent)
    if unresolved:
        raise HTTPException(
            409,
            "Inventory not ready: Cannot determine every discrete GPU vendor "
            "for Pearl Miner; select NVIDIA or AMD explicitly to bypass "
            "automatic detection",
        )
    if len(vendors) != 1:
        if vendors:
            raise HTTPException(
                409,
                "Mixed NVIDIA/AMD rigs require an explicit Pearl Miner GPU backend",
            )
        raise HTTPException(
            409,
            "Inventory not ready: no supported NVIDIA or AMD discrete GPU "
            "was reported for Pearl Miner",
        )
    return next(iter(vendors))


def _is_gpu_mining_leg(miner_type: str, algorithm: str | None, extra_config: dict[str, Any] | None = None) -> bool:
    """Return whether one managed leg owns discrete GPU work."""
    adapter = MINER_ADAPTERS.get(miner_type)
    if adapter is None:
        return False
    try:
        return adapter.algorithm_resource(algorithm, resource_kind=(extra_config or {}).get("resource_kind")) == "gpu"
    except ValueError:
        return False


def _validate_wsl_flight_modes(
    *,
    primary_type: str,
    primary_algorithm: str | None,
    primary_execution_mode: str,
    secondary_mode: str,
    secondary_type: str | None,
    secondary_algorithm: str | None,
    secondary_execution_mode: str | None,
    primary_extra_config: dict[str, Any] | None = None,
    secondary_extra_config: dict[str, Any] | None = None,
    status_code: int = 422,
) -> None:
    """Keep the first WSL release limited to independent NVIDIA GPU legs."""

    legs = (
        (
            "主矿工",
            primary_type,
            primary_algorithm,
            primary_execution_mode,
            primary_extra_config or {},
        ),
        (
            "第二矿工",
            secondary_type,
            secondary_algorithm,
            secondary_execution_mode or "native",
            secondary_extra_config or {},
        ),
    )
    for label, miner_type, algorithm, execution_mode, extra_config in legs:
        if miner_type is None:
            if execution_mode != "native":
                raise HTTPException(
                    status_code, f"{label}启用 WSL 时必须选择受管 GPU 矿工"
                )
            continue
        if execution_mode not in {"native", "wsl"}:
            raise HTTPException(
                status_code, f"{label}执行模式只能是 native 或 wsl"
            )
        adapter = MINER_ADAPTERS.get(miner_type)
        if execution_mode == "wsl" and (
            adapter is None
            or "wsl_gpu" not in adapter.capabilities
            or not _is_gpu_mining_leg(miner_type, algorithm, extra_config)
        ):
            raise HTTPException(
                status_code,
                "WSL 暂时只支持 Windows 上的 GPU 矿工；XMRig 和 SRBMiner RandomX 必须使用原生模式",
            )
        if (
            execution_mode == "wsl"
            and miner_type == "pearlminer"
            and str(extra_config.get("gpu_backend") or "auto").strip().lower()
            == "amd"
        ):
            raise HTTPException(
                status_code,
                "WSL 暂时只支持 Windows 主机上的 NVIDIA GPU；Pearl Miner 不能选择 AMD 后端",
            )
    if secondary_mode == "integrated" and secondary_type is not None and (
        primary_execution_mode == "wsl"
        or (secondary_execution_mode or "native") == "wsl"
    ):
        raise HTTPException(
            status_code,
            "WSL 暂不支持 SRBMiner 单进程内置双挖；请改用第二独立矿工模式",
        )


def _validate_wsl_target_agent(
    agent: sqlite3.Row | dict[str, Any],
    *,
    status_code: int = 409,
) -> None:
    """Require the first WSL backend's supported Windows/NVIDIA target."""

    try:
        os_family = normalize_os_family(str(agent["os_family"] or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code,
            "设备未上报可识别的操作系统，无法使用 WSL 矿工",
        ) from exc
    if os_family != "windows":
        raise HTTPException(
            status_code,
            "WSL 矿工只能下发到 Windows 设备；Linux 设备请使用原生模式",
        )
    vendors, unresolved = _pearl_inventory_gpu_vendors(agent)
    if "nvidia" not in vendors:
        detail = (
            "设备显卡清单尚未完整上报，无法确认 NVIDIA GPU；请等待新心跳后重试"
            if unresolved
            else "设备没有可用于 WSL 矿工的 NVIDIA 独立显卡"
        )
        raise HTTPException(status_code, detail)


def _validate_flight_miner_pair(
    primary_type: str,
    secondary_type: str | None,
    secondary_mode: str = "integrated",
) -> None:
    """Validate the miner adapters used by either dual-mining mode."""

    if secondary_mode not in {"integrated", "independent"}:
        raise HTTPException(422, "Unsupported secondary mining mode")
    if secondary_type is None:
        if secondary_mode == "independent":
            raise HTTPException(
                422, "Independent dual mining requires a secondary miner"
            )
        return
    if secondary_mode == "independent":
        if primary_type not in MINER_TYPES or secondary_type not in MINER_TYPES:
            raise HTTPException(
                422,
                "Independent dual mining requires two managed miners",
            )
        return
    primary_managed = primary_type in MINER_TYPES
    secondary_managed = secondary_type in MINER_TYPES
    if primary_managed != secondary_managed:
        raise HTTPException(
            422,
            "Primary and secondary must both use managed miners or both use legacy profiles",
        )
    primary_adapter = MINER_ADAPTERS.get(primary_type)
    secondary_adapter = MINER_ADAPTERS.get(secondary_type)
    if primary_managed and (
        primary_adapter is None
        or primary_adapter is not secondary_adapter
        or "integrated_dual" not in primary_adapter.capabilities
    ):
        raise HTTPException(
            422,
            "Managed dual mining is supported only by SRBMiner-MULTI",
        )


def _validate_prepared_flight_hardware(
    agent: sqlite3.Row | dict[str, Any], payload: dict[str, Any],
) -> None:
    """Recheck target and options against the package selected for delivery."""
    if _flight_has_extra_args(payload):
        _require_extra_args_target(agent)
    for role in ("primary", "secondary"):
        leg = payload.get(role)
        if not isinstance(leg, dict):
            continue
        kind = leg.get("miner_type")
        options = leg.get("extra_config", {})
        package = leg.get("miner_package")
        if isinstance(kind, str) and kind in MINER_TYPES and isinstance(options, dict) and parse_extra_args(options.get("extra_args", ""), miner_type=kind):
            if not isinstance(package, dict) or not isinstance(package.get("version"), str):
                raise HTTPException(409, "Queued miner package version is missing")
            try:
                MINER_ADAPTERS.require(kind).validate_execution_context(
                    leg.get("algorithm"), package["version"], options, coin=leg.get("coin"),
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        wsl = leg.get("execution_mode") == "wsl"
        if wsl:
            _validate_wsl_target_agent(agent)
        if leg.get("miner_type") != "pearlminer":
            continue
        if not isinstance(options, dict) or not isinstance(package, dict) or not isinstance(package.get("version"), str):
            raise HTTPException(409, "Pearl Miner queued package identity is missing")
        try:
            variant = _pearl_package_variant(
                agent, options, execution_os_family="linux" if wsl else None,
            )
            if variant is not None and package.get("id") not in (
                managed_package_id("pearlminer", package.get("version"), "linux", variant=variant),
                managed_test_package_id("pearlminer", package.get("version"), "linux", variant=variant),
            ):
                raise HTTPException(409, "Pearl Miner GPU backend changed while command was prepared; retry")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


def _validate_independent_miner_resources(
    *,
    secondary_mode: str,
    primary_type: str,
    secondary_type: str | None,
    primary_algorithm: str | None,
    secondary_algorithm: str | None,
    primary_device_indices: list[int],
    secondary_device_indices: list[int],
    primary_extra_config: dict[str, Any] | None = None,
    secondary_extra_config: dict[str, Any] | None = None,
    status_code: int = 422,
) -> None:
    """Validate declared resources and explicit GPU ownership for independent miners."""

    if secondary_mode != "independent":
        return
    if secondary_type is None:
        raise HTTPException(
            status_code, "Independent dual mining requires a secondary miner"
        )

    def resource(miner_type: str, algorithm: str | None, extra: dict[str, Any] | None) -> str:
        adapter = MINER_ADAPTERS.get(miner_type)
        try:
            if adapter is not None:
                return adapter.algorithm_resource(algorithm, resource_kind=(extra or {}).get("resource_kind"))
        except ValueError:
            pass
        raise HTTPException(
            status_code,
            "Cannot determine resources required by independent miners",
        )

    primary_resource = resource(primary_type, primary_algorithm, primary_extra_config)
    secondary_resource = resource(secondary_type, secondary_algorithm, secondary_extra_config)
    if {primary_resource, secondary_resource} == {"cpu", "gpu"}:
        return
    if primary_resource == secondary_resource == "cpu":
        return
    primary_adapter = MINER_ADAPTERS.get(primary_type)
    secondary_adapter = MINER_ADAPTERS.get(secondary_type)
    if (
        primary_adapter is not None
        and primary_adapter is secondary_adapter
        and "device_selection" in primary_adapter.capabilities
        and primary_device_indices
        and secondary_device_indices
        and set(primary_device_indices).isdisjoint(secondary_device_indices)
    ):
        return
    raise HTTPException(
        status_code,
        "Independent GPU miners require explicit, non-overlapping managed GPU indices",
    )


def _validate_srb_dual_configuration(
    *,
    secondary_mode: str = "integrated",
    primary_type: str,
    secondary_type: str | None,
    primary_version: str | None,
    secondary_version: str | None,
    primary_algorithm: str | None,
    secondary_algorithm: str | None,
    primary_device_indices: list[int],
    secondary_device_indices: list[int],
    primary_extra_config: dict[str, Any] | None = None,
    secondary_extra_config: dict[str, Any] | None = None,
    status_code: int = 422,
) -> None:
    """Reject SRBMiner combinations that the Agent cannot start faithfully."""

    if secondary_mode != "integrated" or secondary_type is None:
        return
    if primary_type != "srbminer" or secondary_type != "srbminer":
        return
    if primary_version != secondary_version:
        raise HTTPException(
            status_code,
            "SRBMiner dual mining must use the same miner version for both legs",
        )

    primary = str(primary_algorithm or "").lower()
    secondary = str(secondary_algorithm or "").lower()
    if primary == "randomx" and secondary == "randomx":
        raise HTTPException(
            status_code,
            "SRBMiner dual mining cannot use RandomX for both legs",
        )

    adapter = MINER_ADAPTERS.require("srbminer")
    try:
        primary_resource = adapter.algorithm_resource(primary, resource_kind=(primary_extra_config or {}).get("resource_kind"))
        secondary_resource = adapter.algorithm_resource(secondary, resource_kind=(secondary_extra_config or {}).get("resource_kind"))
    except ValueError as exc:
        raise HTTPException(status_code, str(exc)) from exc
    supported = srb_dual_pair_supported(primary, secondary, primary_resource, secondary_resource)
    if not supported:
        raise HTTPException(
            status_code,
            "Unsupported SRBMiner dual-mining algorithm combination",
        )

    if primary_resource == secondary_resource == "cpu" and primary in adapter.dual_any_secondary:
        try:
            validate_periodic_cpu_threads(primary_extra_config or {}, secondary_extra_config or {})
        except ValueError as exc:
            raise HTTPException(status_code, str(exc)) from exc

    if primary_resource == secondary_resource == "gpu":
        if list(primary_device_indices) != list(secondary_device_indices):
            raise HTTPException(
                status_code,
                "GPU-to-GPU SRBMiner dual mining must use the same ordered GPU indices for both legs",
            )


def _independent_srb_gpu_oc_shape_supported(
    *, primary_type: str, primary_algorithm: str | None,
    secondary_type: str | None, secondary_algorithm: str | None,
    primary_extra_config: dict[str, Any] | None = None,
    secondary_extra_config: dict[str, Any] | None = None,
) -> bool:
    if primary_type != "srbminer" or not _is_gpu_mining_leg(primary_type, primary_algorithm, primary_extra_config):
        return False
    adapter = MINER_ADAPTERS.get(secondary_type or "")
    try:
        return bool(adapter and adapter.algorithm_resource(secondary_algorithm, resource_kind=(secondary_extra_config or {}).get("resource_kind")) == "cpu")
    except ValueError:
        return False


def _normalize_flight_gpu_oc(
    body: FlightSheetCreate,
    primary_algorithm: str | None,
    secondary_algorithm: str | None,
) -> dict[str, Any] | None:
    """Validate flight-scoped OC against the managed SRBMiner GPU legs."""

    if body.gpu_oc is None:
        return None
    if body.miner_type != "srbminer":
        raise HTTPException(
            422,
            "Flight-sheet GPU OC is supported only for managed SRBMiner",
        )

    if body.secondary_mode == "independent":
        secondary = body.secondary
        if not _independent_srb_gpu_oc_shape_supported(
            primary_type=body.miner_type,
            primary_algorithm=primary_algorithm,
            secondary_type=(secondary.miner_type if secondary is not None else None),
            secondary_algorithm=secondary_algorithm,
            primary_extra_config=body.extra_config,
            secondary_extra_config=secondary.extra_config if secondary else None,
        ):
            raise HTTPException(
                422,
                "Independent flight-sheet GPU OC requires a primary SRBMiner "
                "GPU leg and a CPU-only second miner",
            )
        # Top-level flight OC belongs only to the primary SRBMiner process.
        # A future per-role schema can safely add independent GPU+GPU OC.
        legs: list[tuple[Any, str | None]] = [(body, primary_algorithm)]
    else:
        legs = [(body, primary_algorithm)]
        if body.secondary is not None:
            legs.append((body.secondary, secondary_algorithm))
    gpu_legs = [
        leg
        for leg, algorithm in legs
        if leg.miner_type == "srbminer"
        and _is_gpu_mining_leg(leg.miner_type, algorithm, leg.extra_config)
    ]
    if not gpu_legs:
        raise HTTPException(
            422,
            "Flight-sheet GPU OC requires an SRBMiner GPU algorithm",
        )

    settings_payload = [
        setting.model_dump(exclude_none=True) for setting in body.gpu_oc.settings
    ]
    model_mode = "model_match" in settings_payload[0]
    if not model_mode:
        # Preserve the original numeric-index contract for existing sheets.
        # New model rules are resolved against the target Agent's actual cards
        # and therefore also work when GPU indices are left on automatic.
        if any(not leg.device_indices for leg in gpu_legs):
            raise HTTPException(
                422,
                "Legacy flight-sheet GPU OC requires explicit GPU indices on every GPU mining leg",
            )
        selected_gpu_indices = set(gpu_legs[0].device_indices)
        setting_indices = {setting["index"] for setting in settings_payload}
        if setting_indices != selected_gpu_indices:
            raise HTTPException(
                422,
                "Legacy flight-sheet GPU OC must contain exactly one row for every selected GPU",
            )
    control_sets = {
        frozenset(setting) - {"index", "model_match"} for setting in settings_payload
    }
    if len(control_sets) != 1:
        raise HTTPException(
            422,
            "Every SRBMiner GPU OC row must fill the same control columns",
        )
    payload = {"vendor": body.gpu_oc.vendor, "settings": settings_payload}
    _validate_json_payload(payload, "gpu_oc", max_bytes=16_384)
    return payload


def _render_worker_name(template: str, agent: sqlite3.Row) -> str:
    try:
        return render_worker_name_template(template, agent_name=str(agent["name"]),
                                           system_hostname=str(agent["hostname"]), agent_id=str(agent["id"]))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _optional_row_value(
    row: sqlite3.Row | dict[str, Any], key: str, default: Any = None
) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _serialize_miner_package(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    package = {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "download_url": row["download_url"],
        # The database column remains for old installations and upload
        # metadata, but external packages no longer have to provide a digest.
        "sha256": row["sha256"] or None,
        "archive_type": row["archive_type"],
        "note": row["note"],
        "source_type": _optional_row_value(row, "source_type", "external"),
        "original_filename": _optional_row_value(row, "original_filename"),
        "file_size": _optional_row_value(row, "file_size"),
        "managed_miner_type": _optional_row_value(row, "managed_miner_type"),
        "managed_os_family": _optional_row_value(row, "managed_os_family"),
        "managed_variant": _optional_row_value(row, "managed_variant"),
        "managed_channel": _optional_row_value(row, "managed_channel", "stable"),
    }
    return package


def _miner_package_command_payload(
    row: sqlite3.Row | dict[str, Any] | None,
) -> dict[str, Any] | None:
    package = _serialize_miner_package(row)
    if package is not None:
        package.pop("note", None)
        package.pop("source_type", None)
        package.pop("original_filename", None)
        package.pop("file_size", None)
        # Classification is controller-side catalog metadata. Keep the Agent
        # command wire shape stable for old and current portable clients.
        package.pop("managed_miner_type", None)
        package.pop("managed_os_family", None)
        package.pop("managed_variant", None)
        package.pop("managed_channel", None)
    return package


def _package_storage_path(settings: Settings, storage_key: str) -> Path:
    root = (settings.data_dir / "miner-packages").resolve()
    candidate = (root / storage_key).resolve()
    if candidate == root or root not in candidate.parents:
        raise HTTPException(500, "invalid miner package storage key")
    return candidate


def _agent_release_storage_path(settings: Settings, storage_key: str) -> Path:
    """Resolve a release path while preventing DB-controlled path traversal."""

    root = (settings.data_dir / "agent-releases").resolve()
    candidate = (root / storage_key).resolve()
    if candidate == root or root not in candidate.parents:
        raise HTTPException(500, "invalid Agent release storage key")
    return candidate


def _normalize_agent_release_platform(value: str) -> str:
    normalized = value.strip().casefold()
    aliases = {
        "windows": "windows",
        "win": "windows",
        "win32": "windows",
        "linux": "linux",
        "ubuntu": "linux",
    }
    platform = aliases.get(normalized)
    if platform not in AGENT_RELEASE_PLATFORMS:
        raise HTTPException(422, "Agent release platform must be Windows or Linux")
    return platform


def _normalize_agent_release_architecture(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_")
    if normalized not in {"x86_64", "x64", "amd64"}:
        raise HTTPException(422, "only x86_64 Agent releases are supported")
    return AGENT_RELEASE_ARCHITECTURE


def _safe_agent_release_filename(value: str, *, platform: str) -> str:
    """Accept a conservative platform archive filename without rewriting it."""

    filename = value.strip()
    suffix = ".zip" if platform == "windows" else ".tar.gz"
    if (
        not filename
        or filename != value
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(suffix)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+-]*" + re.escape(suffix),
            filename,
            flags=re.IGNORECASE,
        )
    ):
        raise HTTPException(
            422,
            f"file_name must be a path-free {suffix} Agent release filename",
        )
    stem = filename[: -len(suffix)].rstrip(". ")
    if not stem or stem.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT1",
    }:
        raise HTTPException(422, "file_name is not valid")
    return filename


def _validate_windows_portable_zip(
    path: Path, *, max_upload_bytes: int, expected_version: str
) -> None:
    """Validate the same signed-by-digest bundle contract enforced by the Agent."""

    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", expected_version):
        raise HTTPException(422, "Agent release version must use strict X.Y.Z format")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > 16_384:
                raise HTTPException(422, "Agent release ZIP has an invalid entry count")
            expanded_limit = max_upload_bytes * 4
            expanded_size = 0
            normalized_paths: set[str] = set()
            regular_entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
            for entry in entries:
                normalized = entry.filename.replace("\\", "/")
                path_without_suffix = normalized[:-1] if normalized.endswith("/") else normalized
                parts = tuple(path_without_suffix.split("/"))
                if (
                    "\\" in entry.orig_filename
                    or normalized.startswith("/")
                    or not parts
                    or any(not part or part in {".", ".."} for part in parts)
                    or any(":" in part for part in parts)
                    or any(part.endswith((" ", ".")) for part in parts)
                    or any(len(part) > 255 for part in parts)
                    or any(
                        part.split(".", 1)[0].upper() in WINDOWS_RESERVED_PATH_NAMES
                        for part in parts
                    )
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in normalized
                    )
                ):
                    raise HTTPException(422, "Agent release ZIP contains an unsafe path")
                if entry.flag_bits & 0x1:
                    raise HTTPException(422, "Agent release ZIP must not be encrypted")
                if entry.compress_type not in AGENT_RELEASE_ALLOWED_COMPRESSION:
                    raise HTTPException(
                        422, "Agent release ZIP uses an unsupported compression method"
                    )
                unix_file_type = (entry.external_attr >> 16) & 0o170000
                if entry.create_system == 3 and unix_file_type not in {
                    0,
                    0o040000,
                    0o100000,
                }:
                    raise HTTPException(
                        422,
                        "Agent release ZIP must not contain links or special files",
                    )
                folded_path = "/".join(parts).casefold()
                if folded_path in normalized_paths:
                    raise HTTPException(
                        422, "Agent release ZIP contains duplicate Windows paths"
                    )
                normalized_paths.add(folded_path)
                expanded_size += max(0, entry.file_size)
                if expanded_size > expanded_limit:
                    raise HTTPException(413, "Agent release ZIP expands beyond the limit")
                if not entry.is_dir():
                    regular_entries.append((entry, parts))

            manifests = [
                (entry, parts)
                for entry, parts in regular_entries
                if parts[-1] == AGENT_RELEASE_MANIFEST
            ]
            if len(manifests) != 1:
                raise HTTPException(
                    422, "Agent release ZIP must contain exactly one agent-release.json"
                )
            manifest_entry, manifest_parts = manifests[0]
            if len(manifest_parts) not in {1, 2}:
                raise HTTPException(422, "Agent release ZIP has an unsupported root layout")
            prefix = manifest_parts[:-1]
            relative_entries: dict[str, zipfile.ZipInfo] = {}
            for entry, parts in regular_entries:
                if parts[: len(prefix)] != prefix or len(parts) <= len(prefix):
                    raise HTTPException(
                        422, "Agent release files must share one bundle root"
                    )
                relative_entries["/".join(parts[len(prefix) :])] = entry
            if manifest_entry.file_size > AGENT_RELEASE_MANIFEST_MAX_BYTES:
                raise HTTPException(422, "Agent release manifest is too large")
            try:
                manifest = json.loads(archive.read(manifest_entry).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, OSError, RuntimeError) as exc:
                raise HTTPException(422, "Agent release manifest is invalid") from exc
            if not isinstance(manifest, dict) or set(manifest) != {
                "schema_version",
                "version",
                "platform",
                "architecture",
                "files",
            }:
                raise HTTPException(422, "Agent release manifest has invalid fields")
            if (
                isinstance(manifest.get("schema_version"), bool)
                or manifest.get("schema_version") != 1
                or manifest.get("version") != expected_version
                or manifest.get("platform") != "windows"
                or manifest.get("architecture") != AGENT_RELEASE_ARCHITECTURE
                or not isinstance(manifest.get("files"), dict)
            ):
                raise HTTPException(422, "Agent release manifest does not match upload metadata")
            declared_files = manifest["files"]
            normalized_declared: set[str] = set()
            for name, digest in declared_files.items():
                if not isinstance(name, str) or not name or "\\" in name:
                    raise HTTPException(422, "Agent release manifest contains an invalid file entry")
                parts = tuple(name.split("/"))
                if parts[0].casefold() in {"data", "versions"}:
                    raise HTTPException(
                        422,
                        "Agent release manifest must not contain device data or version slots",
                    )
                if (
                    name.startswith("/")
                    or any(not part or part in {".", ".."} or ":" in part for part in parts)
                    or "/".join(parts) != name
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise HTTPException(422, "Agent release manifest contains an invalid file entry")
                folded = name.casefold()
                if folded in normalized_declared:
                    raise HTTPException(422, "Agent release manifest contains duplicate Windows paths")
                normalized_declared.add(folded)
            actual_files = set(relative_entries) - {AGENT_RELEASE_MANIFEST}
            if actual_files != set(declared_files):
                raise HTTPException(422, "Agent release manifest file list does not match ZIP")
            if not WINDOWS_AGENT_RELEASE_REQUIRED_FILES <= actual_files:
                missing = sorted(WINDOWS_AGENT_RELEASE_REQUIRED_FILES - actual_files)
                raise HTTPException(
                    422,
                    "Agent release ZIP is missing required update files: "
                    + ", ".join(missing),
                )

            # Hash every regular file without extracting.  This simultaneously
            # validates local headers, decompression and CRC.
            for name, entry in relative_entries.items():
                digest = hashlib.sha256()
                with archive.open(entry, "r") as source:
                    while chunk := source.read(PACKAGE_CHUNK_BYTES):
                        digest.update(chunk)
                if name != AGENT_RELEASE_MANIFEST and digest.hexdigest() != declared_files[name]:
                    raise HTTPException(422, "Agent release manifest SHA-256 does not match")
    except HTTPException:
        raise
    except (OSError, zlib.error, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise HTTPException(422, "Agent release file is not a valid ZIP") from exc


def _validate_linux_agent_tar_gz(
    path: Path, *, max_upload_bytes: int, expected_version: str
) -> None:
    """Validate a Linux online-update payload without extracting any member."""

    if not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",
        expected_version,
    ):
        raise HTTPException(422, "Agent release version must use strict X.Y.Z format")
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            entries = archive.getmembers()
            if not entries or len(entries) > 16_384:
                raise HTTPException(
                    422, "Agent release TAR.GZ has an invalid entry count"
                )

            expanded_limit = max_upload_bytes * 4
            expanded_size = 0
            normalized_paths: set[str] = set()
            regular_entries: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
            bundle_roots: set[str] = set()
            for entry in entries:
                raw_name = entry.name
                is_directory = entry.isdir()
                normalized = (
                    raw_name[:-1]
                    if is_directory and raw_name.endswith("/")
                    else raw_name
                )
                parts = tuple(normalized.split("/"))
                sparse = bool(
                    entry.type == tarfile.GNUTYPE_SPARSE
                    or getattr(entry, "sparse", None)
                    or any(
                        str(key).casefold().startswith("gnu.sparse")
                        for key in entry.pax_headers
                    )
                )
                if sparse:
                    raise HTTPException(
                        422, "Agent release TAR.GZ must not contain sparse files"
                    )
                if not (entry.isfile() or is_directory):
                    raise HTTPException(
                        422,
                        "Agent release TAR.GZ must contain only regular files and directories",
                    )
                if (
                    "\\" in raw_name
                    or raw_name.startswith("/")
                    or not normalized
                    or not parts
                    or any(not part or part in {".", ".."} for part in parts)
                    or any(":" in part for part in parts)
                    or any(len(part.encode("utf-8")) > 255 for part in parts)
                    or len(normalized.encode("utf-8")) > 4096
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in normalized
                    )
                    or (not is_directory and raw_name.endswith("/"))
                ):
                    raise HTTPException(
                        422, "Agent release TAR.GZ contains an unsafe path"
                    )
                if normalized in normalized_paths:
                    raise HTTPException(
                        422, "Agent release TAR.GZ contains duplicate paths"
                    )
                normalized_paths.add(normalized)
                bundle_roots.add(parts[0])
                if entry.isfile():
                    if len(parts) < 2:
                        raise HTTPException(
                            422,
                            "Agent release files must share one bundle root",
                        )
                    expanded_size += max(0, entry.size)
                    if expanded_size > expanded_limit:
                        raise HTTPException(
                            413, "Agent release TAR.GZ expands beyond the limit"
                        )
                    regular_entries.append((entry, parts))

            if len(bundle_roots) != 1:
                raise HTTPException(
                    422, "Agent release files must share one bundle root"
                )
            manifests = [
                (entry, parts)
                for entry, parts in regular_entries
                if len(parts) == 2 and parts[1] == AGENT_RELEASE_MANIFEST
            ]
            if len(manifests) != 1:
                raise HTTPException(
                    422,
                    "Agent release TAR.GZ must contain exactly one root agent-release.json",
                )
            manifest_entry, _manifest_parts = manifests[0]
            if manifest_entry.size > AGENT_RELEASE_MANIFEST_MAX_BYTES:
                raise HTTPException(422, "Agent release manifest is too large")
            manifest_stream = archive.extractfile(manifest_entry)
            if manifest_stream is None:
                raise HTTPException(422, "Agent release manifest is invalid")
            try:
                manifest_bytes = manifest_stream.read(
                    AGENT_RELEASE_MANIFEST_MAX_BYTES + 1
                )
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError, OSError) as exc:
                raise HTTPException(422, "Agent release manifest is invalid") from exc
            if len(manifest_bytes) != manifest_entry.size:
                raise HTTPException(422, "Agent release manifest is truncated")
            if not isinstance(manifest, dict) or set(manifest) != {
                "schema_version",
                "version",
                "platform",
                "architecture",
                "files",
            }:
                raise HTTPException(422, "Agent release manifest has invalid fields")
            if (
                isinstance(manifest.get("schema_version"), bool)
                or manifest.get("schema_version") != 1
                or manifest.get("version") != expected_version
                or manifest.get("platform") != "linux"
                or manifest.get("architecture") != AGENT_RELEASE_ARCHITECTURE
                or not isinstance(manifest.get("files"), dict)
            ):
                raise HTTPException(
                    422, "Agent release manifest does not match upload metadata"
                )

            relative_entries = {
                "/".join(parts[1:]): entry
                for entry, parts in regular_entries
            }
            declared_files = manifest["files"]
            normalized_declared: set[str] = set()
            for name, digest in declared_files.items():
                if not isinstance(name, str) or not name or "\\" in name:
                    raise HTTPException(
                        422, "Agent release manifest contains an invalid file entry"
                    )
                parts = tuple(name.split("/"))
                if (
                    name.startswith("/")
                    or any(not part or part in {".", ".."} or ":" in part for part in parts)
                    or "/".join(parts) != name
                    or any(len(part.encode("utf-8")) > 255 for part in parts)
                    or len(name.encode("utf-8")) > 4096
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise HTTPException(
                        422, "Agent release manifest contains an invalid file entry"
                    )
                if name in normalized_declared:
                    raise HTTPException(
                        422, "Agent release manifest contains duplicate paths"
                    )
                normalized_declared.add(name)
            actual_files = set(relative_entries) - {AGENT_RELEASE_MANIFEST}
            if actual_files != set(declared_files):
                raise HTTPException(
                    422, "Agent release manifest file list does not match TAR.GZ"
                )
            if actual_files != LINUX_AGENT_RELEASE_REQUIRED_FILES:
                missing = sorted(LINUX_AGENT_RELEASE_REQUIRED_FILES - actual_files)
                extra = sorted(actual_files - LINUX_AGENT_RELEASE_REQUIRED_FILES)
                details = []
                if missing:
                    details.append("missing " + ", ".join(missing))
                if extra:
                    details.append("unexpected " + ", ".join(extra))
                raise HTTPException(
                    422,
                    "Linux online-update file set must be exactly RigPilotAgent"
                    + (": " + "; ".join(details) if details else ""),
                )
            runtime_entry = relative_entries["RigPilotAgent"]
            if runtime_entry.mode & 0o111 == 0:
                raise HTTPException(
                    422, "Linux RigPilotAgent must have an executable mode"
                )

            # Stream and hash every regular member.  No member is extracted.
            for name, entry in relative_entries.items():
                source = archive.extractfile(entry)
                if source is None:
                    raise HTTPException(422, "Agent release TAR.GZ member is invalid")
                digest = hashlib.sha256()
                bytes_read = 0
                while chunk := source.read(PACKAGE_CHUNK_BYTES):
                    bytes_read += len(chunk)
                    if bytes_read > entry.size:
                        raise HTTPException(
                            422, "Agent release TAR.GZ member size is invalid"
                        )
                    digest.update(chunk)
                if bytes_read != entry.size:
                    raise HTTPException(
                        422, "Agent release TAR.GZ member is truncated"
                    )
                if (
                    name != AGENT_RELEASE_MANIFEST
                    and digest.hexdigest() != declared_files[name]
                ):
                    raise HTTPException(
                        422, "Agent release manifest SHA-256 does not match"
                    )
    except HTTPException:
        raise
    except (EOFError, OSError, tarfile.TarError, zlib.error) as exc:
        raise HTTPException(422, "Agent release file is not a valid TAR.GZ") from exc


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return authorization[7:].strip()


def _normalize_flight_input(body: FlightSheetCreate, miner_version: str | None,
                            secondary_miner_version: str | None) -> tuple[Any, ...]:
    """Normalize a flight request without DB, version resolution or dispatch side effects."""
    pool_url = _validate_pool_url(body.pool_url, miner_type=body.miner_type)
    _validate_json_payload(body.extra_config, "extra_config", max_bytes=16_384)
    algorithm, extra_config = _normalize_managed_options(
        miner_type=body.miner_type,
        coin=body.coin,
        algorithm=body.algorithm,
        pool_url=pool_url,
        extra_config=body.extra_config,
        miner_version=miner_version, pool_password=(body.pool_password if "pool_password" in body.model_fields_set else None),
    )
    secondary_pool_url = None
    secondary_algorithm = None
    secondary_extra_config: dict[str, Any] | None = None
    if body.secondary:
        secondary_pool_url = _validate_pool_url(
            body.secondary.pool_url,
            miner_type=body.secondary.miner_type,
        )
        _validate_json_payload(
            body.secondary.extra_config,
            "secondary.extra_config",
            max_bytes=16_384,
        )
        secondary_algorithm, secondary_extra_config = _normalize_managed_options(
            miner_type=body.secondary.miner_type,
            coin=body.secondary.coin,
            algorithm=body.secondary.algorithm,
            pool_url=secondary_pool_url,
            extra_config=body.secondary.extra_config,
            miner_version=secondary_miner_version, pool_password=(body.secondary.pool_password if "pool_password" in body.secondary.model_fields_set else None),
        )
    _validate_wsl_flight_modes(
        primary_type=body.miner_type,
        primary_algorithm=algorithm,
        primary_execution_mode=body.execution_mode,
        primary_extra_config=extra_config,
        secondary_mode=body.secondary_mode,
        secondary_type=body.secondary.miner_type if body.secondary else None,
        secondary_algorithm=secondary_algorithm,
        secondary_execution_mode=(
            body.secondary.execution_mode if body.secondary else None
        ),
        secondary_extra_config=secondary_extra_config,
    )
    if (
        body.gpu_oc is not None
        and (
            body.execution_mode == "wsl"
            or (
                body.secondary is not None
                and body.secondary.execution_mode == "wsl"
            )
        )
    ):
        raise HTTPException(
            422,
            "WSL 矿工暂不接收飞行表 SRBMiner OC；请使用设备详情中的 Windows NVML OC",
        )
    _validate_independent_miner_resources(
        secondary_mode=body.secondary_mode,
        primary_type=body.miner_type,
        secondary_type=body.secondary.miner_type if body.secondary else None,
        primary_algorithm=algorithm,
        secondary_algorithm=secondary_algorithm,
        primary_extra_config=extra_config, secondary_extra_config=secondary_extra_config,
        primary_device_indices=body.device_indices,
        secondary_device_indices=(
            body.secondary.device_indices if body.secondary else []
        ),
    )
    _validate_srb_dual_configuration(
        secondary_mode=body.secondary_mode,
        primary_type=body.miner_type,
        secondary_type=body.secondary.miner_type if body.secondary else None,
        primary_version=miner_version,
        secondary_version=secondary_miner_version,
        primary_algorithm=algorithm,
        secondary_algorithm=secondary_algorithm,
        primary_extra_config=extra_config, secondary_extra_config=secondary_extra_config,
        primary_device_indices=body.device_indices,
        secondary_device_indices=(
            body.secondary.device_indices if body.secondary else []
        ),
    )
    gpu_oc = _normalize_flight_gpu_oc(
        body, algorithm, secondary_algorithm
    )
    return (pool_url, algorithm, extra_config, secondary_pool_url,
            secondary_algorithm, secondary_extra_config, gpu_oc)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_path)
    rate_limiter = LoginRateLimiter()
    password_change_rate_limiter = LoginRateLimiter()
    replacement_rate_limiter = LoginRateLimiter(
        attempts=12,
        window_seconds=60,
        ip_attempts=60,
        max_keys=2_048,
        max_ips=1_024,
    )
    dummy_password_hash = hash_password(secrets.token_urlsafe(48))
    managed_package_lock = threading.Lock()
    agent_release_lock = threading.Lock()
    managed_miner_catalog = ManagedMinerCatalog()
    controller_metrics = ControllerMetricsSampler(settings.data_dir)
    instance_lock = ControllerInstanceLock(settings.data_dir)
    heartbeat_clock_lock = threading.Lock()
    last_heartbeat_receive_micros = 0

    def heartbeat_received_at() -> str:
        """Return a strictly increasing microsecond receive token for CAS writes."""

        nonlocal last_heartbeat_receive_micros
        with heartbeat_clock_lock:
            current = max(
                time.time_ns() // 1000,
                last_heartbeat_receive_micros + 1,
            )
            last_heartbeat_receive_micros = current
        return datetime.fromtimestamp(
            current / 1_000_000, timezone.utc
        ).isoformat(timespec="microseconds")

    def _queue_agent_update_forensics(
        db: sqlite3.Connection,
        job: sqlite3.Row,
        *,
        now: str,
        reason: str,
    ) -> str | None:
        """Queue the fixed read-only follow-up once per timed-out activation.

        The command is attributed to the operator who initiated the update,
        carries a controller-fixed payload (no operator input reaches the
        device through this path), and is deduplicated per device so repeated
        sweeps never stack forensic commands.
        """

        pending = db.execute(
            """SELECT id FROM commands
               WHERE agent_id=? AND action=?
                 AND status IN ('queued','dispatched','running')
                 AND expires_at >= ?
               LIMIT 1""",
            (job["agent_id"], AGENT_UPDATE_FORENSICS_ACTION, now),
        ).fetchone()
        if pending is not None:
            return None
        creator = db.execute(
            "SELECT id, username FROM users WHERE id=?", (job["created_by"],)
        ).fetchone()
        if creator is None:
            LOGGER.warning(
                "Cannot queue Agent update forensics for job %s: creator user "
                "%s no longer exists",
                job["command_id"],
                job["created_by"],
            )
            return None
        command_id = str(uuid.uuid4())
        created_moment = datetime.now(timezone.utc)
        created_at = created_moment.isoformat(timespec="microseconds")
        expires_at = (created_moment + timedelta(
            seconds=AGENT_UPDATE_FORENSICS_TTL_SECONDS
        )).isoformat(timespec="seconds")
        payload = {
            "_controller_auto_forensics": {
                "trigger": "agent_update_activation_timeout",
                "command_id": job["command_id"],
                "source_version": job["source_version"],
                "target_version": job["target_version"],
                "commands": list(AGENT_UPDATE_FORENSICS_COMMANDS),
            },
        }
        db.execute(
            """INSERT INTO commands
               (id, agent_id, action, payload_json, status, created_by,
                created_at, expires_at, hard_expires_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                command_id,
                job["agent_id"],
                AGENT_UPDATE_FORENSICS_ACTION,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                "queued",
                creator["id"],
                created_at,
                expires_at,
                _command_hard_expiry(
                    created_moment,
                    action=AGENT_UPDATE_FORENSICS_ACTION,
                    ttl_seconds=AGENT_UPDATE_FORENSICS_TTL_SECONDS,
                ),
            ),
        )
        Database.audit(
            db,
            actor=creator["username"],
            action="agent_update_forensics_queued",
            target_type="agent",
            target_id=job["agent_id"],
            details={
                "command_id": command_id,
                "update_command_id": job["command_id"],
                "target_version": job["target_version"],
                "commands": list(AGENT_UPDATE_FORENSICS_COMMANDS),
                "reason": reason,
                "queued_for_offline_device": True,
            },
        )
        return command_id

    def expire_stale_agent_updates(db: sqlite3.Connection, now: str) -> int:
        """Make every activation path terminal within a fixed upper bound.

        Also flags activations whose replacement reached two target heartbeats
        and then went silent: those are rolled back or dead, not waiting.
        """

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=AGENT_UPDATE_ACTIVATION_MAX_MINUTES)
        ).isoformat(timespec="microseconds")
        stall_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=AGENT_UPDATE_REPLACEMENT_STALL_MINUTES)
        ).isoformat(timespec="microseconds")
        stalled_rows = db.execute(
            """SELECT command_id, agent_id, release_id, source_version,
                      target_version, target_heartbeat_count, target_heartbeat_at
               FROM agent_update_jobs
               WHERE status='activating'
                 AND stalled_at IS NULL
                 AND post_update_command_id IS NULL
                 AND target_heartbeat_count >= 2
                 AND target_heartbeat_at IS NOT NULL
                 AND target_heartbeat_at < ?""",
            (stall_cutoff,),
        ).fetchall()
        for row in stalled_rows:
            db.execute(
                """UPDATE agent_update_jobs
                   SET stalled_at=? WHERE command_id=? AND status='activating'""",
                (now, row["command_id"]),
            )
            Database.audit(
                db,
                actor="controller",
                action="agent_update_replacement_stalled",
                target_type="agent",
                target_id=row["agent_id"],
                details={
                    "command_id": row["command_id"],
                    "release_id": row["release_id"],
                    "source_version": row["source_version"],
                    "target_version": row["target_version"],
                    "target_heartbeat_count": row["target_heartbeat_count"],
                    "target_heartbeat_at": row["target_heartbeat_at"],
                    "reason": (
                        "replacement stalled (rolled back or dead); waiting "
                        "for rollback receipt or the activation timeout sweep"
                    ),
                },
            )
        rows = db.execute(
            """SELECT j.command_id, j.agent_id, j.created_by,
                      j.source_version, j.target_version, j.release_id,
                      j.post_update_command_id,
                      pc.status AS post_status, pc.error AS post_error
               FROM agent_update_jobs j
               LEFT JOIN commands pc ON pc.id=j.post_update_command_id
               WHERE j.status='activating'
                 AND (j.updated_at < ?
                      OR pc.status IN ('failed','cancelled','expired'))""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            if row["post_status"] in {"failed", "cancelled", "expired"}:
                reason = row["post_error"] or (
                    "post-update configuration command ended as "
                    f"{row['post_status']}"
                )
                timed_out = False
            else:
                reason = (
                    "Agent update activation did not complete within "
                    f"{AGENT_UPDATE_ACTIVATION_MAX_MINUTES} minutes"
                )
                timed_out = True
            transitioned = db.execute(
                """UPDATE agent_update_jobs
                   SET status='failed', updated_at=?, completed_at=?, last_error=?
                   WHERE command_id=? AND status='activating'""",
                (now, now, reason[:1000], row["command_id"]),
            ).rowcount
            db.execute(
                """UPDATE commands SET status='failed', finished_at=?, error=?
                   WHERE id=? AND status IN ('queued','dispatched','running')""",
                (now, reason[:1000], row["command_id"]),
            )
            Database.audit(
                db,
                actor="controller",
                action="agent_update_activation_failed",
                target_type="agent",
                target_id=row["agent_id"],
                details={
                    "command_id": row["command_id"],
                    "post_update_command_id": row["post_update_command_id"],
                    "reason": reason,
                },
            )
            if timed_out and transitioned:
                # A-mode forensics: the device is typically offline or rolled
                # back here.  Queue the fixed read-only evidence script so it
                # runs on the next authenticated heartbeat and old Agents that
                # do not know this action simply report a rejected command.
                try:
                    _queue_agent_update_forensics(
                        db, row, now=now, reason=reason
                    )
                except Exception:
                    LOGGER.exception(
                        "Could not queue Agent update forensics for job %s",
                        row["command_id"],
                    )
        return len(rows)

    def expire_stale_agent_commands(
        db: sqlite3.Connection,
        *,
        agent_id: str | None = None,
        now: str,
    ) -> int:
        """Expire one device or the whole stale ledger in a writer transaction.

        Command-creation endpoints cannot rely on the minute maintenance loop:
        an expired row must not block a new, otherwise valid operation.  Keep
        the durable refresh/update ledgers synchronized in the same writer
        transaction so an expired command cannot remain an invisible blocker.
        """

        if agent_id is None:
            expire_stale_agent_updates(db, now)
        scope = "agent_id=? AND " if agent_id is not None else ""
        params = (agent_id,) if agent_id is not None else ()
        expired = db.execute(
            f"""UPDATE commands SET status='expired', finished_at=?
               WHERE {scope}status IN ('queued','dispatched','running')
                 AND expires_at < ?
                 AND NOT EXISTS (
                   SELECT 1 FROM agent_update_jobs j
                   WHERE j.command_id=commands.id AND j.status='activating'
                 )""",
            (now, *params, now),
        ).rowcount
        db.execute(
            f"""UPDATE configuration_refreshes
               SET status='pending', command_id=NULL, updated_at=?,
                   queued_at=NULL, completed_at=NULL,
                   last_error='previous refresh command expired; waiting to retry'
               WHERE {scope}status IN ('queued','dispatched','running')
                 AND command_id IN (
                   SELECT id FROM commands
                   WHERE {scope}status='expired'
                 )""",
            (now, *params, *params),
        )
        db.execute(
            f"""UPDATE agent_update_jobs
               SET status='expired', updated_at=?, completed_at=?,
                   last_error=COALESCE(last_error, 'Agent update command expired')
               WHERE {scope}status IN ('queued','dispatched')
                 AND command_id IN (
                   SELECT id FROM commands
                   WHERE {scope}status='expired'
                 )""",
            (now, now, *params, *params),
        )
        expired_post_updates = db.execute(
            f"""SELECT j.command_id, j.agent_id, j.post_update_command_id
               FROM agent_update_jobs j
               JOIN commands pc ON pc.id=j.post_update_command_id
               WHERE {'j.agent_id=? AND ' if agent_id is not None else ''}j.status='activating'
                 AND pc.status='expired'""",
            params,
        ).fetchall()
        for update_job in expired_post_updates:
            reason = "post-update configuration command expired before completion"
            db.execute(
                """UPDATE agent_update_jobs
                   SET status='failed', updated_at=?, completed_at=?, last_error=?
                   WHERE command_id=? AND status='activating'""",
                (now, now, reason, update_job["command_id"]),
            )
            # New releases mark the update command succeeded before this
            # optional policy stage. Repair older ledgers that left it active.
            db.execute(
                """UPDATE commands SET status='failed', finished_at=?,
                           error=COALESCE(error, ?)
                   WHERE id=? AND status IN ('queued','dispatched','running')""",
                (now, reason, update_job["command_id"]),
            )
            Database.audit(
                db,
                actor="controller",
                action="agent_update_activation_failed",
                target_type="agent",
                target_id=update_job["agent_id"],
                details={
                    "command_id": update_job["command_id"],
                    "post_update_command_id": update_job[
                        "post_update_command_id"
                    ],
                    "reason": reason,
                },
            )
        return max(0, expired)

    def expire_preboot_power_commands(
        db: sqlite3.Connection,
        *,
        agent_id: str,
        boot_time: str | None,
        now: str,
    ) -> int:
        """Never replay a one-shot restart/reboot/shutdown on a later boot."""

        if boot_time is None:
            return 0
        rows = db.execute(
            """SELECT id, action, status FROM commands
               WHERE agent_id=? AND action IN ('restart_service','reboot','shutdown')
                 AND status IN ('queued','dispatched','running')
                 AND created_at < ?
               ORDER BY created_at, id""",
            (agent_id, boot_time),
        ).fetchall()
        if not rows:
            return 0
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        reason = (
            "Host boot changed before this one-shot power command completed; "
            "the command will not be replayed"
        )
        db.execute(
            f"""UPDATE commands SET status='expired', finished_at=?,
                       error=COALESCE(error, ?)
                WHERE id IN ({placeholders})
                  AND status IN ('queued','dispatched','running')""",
            (now, reason, *ids),
        )
        for row in rows:
            Database.audit(
                db,
                actor="controller",
                action="preboot_power_command_expired",
                target_type="command",
                target_id=row["id"],
                details={
                    "agent_id": agent_id,
                    "action": row["action"],
                    "previous_status": row["status"],
                    "boot_time": boot_time,
                },
            )
        return len(rows)

    def reject_incompatible_queued_agent_updates(
        db: sqlite3.Connection,
        *,
        agent_id: str,
        heartbeat: HeartbeatRequest,
        now: str,
    ) -> int:
        """Revalidate update bindings against the heartbeat that will receive it."""

        rows = db.execute(
            """SELECT j.command_id, j.source_version,
                      r.platform, r.architecture
               FROM agent_update_jobs j
               JOIN commands c ON c.id=j.command_id
               JOIN agent_releases r ON r.id=j.release_id
               WHERE j.agent_id=? AND j.status='queued' AND c.status='queued'""",
            (agent_id,),
        ).fetchall()
        os_family = str(heartbeat.os_family or "").strip().casefold()
        current_platform = {
            "windows": "windows",
            "win": "windows",
            "win32": "windows",
            "linux": "linux",
            "ubuntu": "linux",
            "debian": "linux",
        }.get(os_family)
        architecture = str(
            heartbeat.inventory.get("architecture", "")
            if isinstance(heartbeat.inventory, dict)
            else ""
        ).strip().casefold().replace("-", "_")
        capabilities = set(heartbeat.capabilities)
        rejected = 0
        for row in rows:
            reason: str | None = None
            release_platform = str(row["platform"] or "").strip().casefold()
            release_arch = str(row["architecture"] or "").strip().casefold().replace(
                "-", "_"
            )
            if current_platform != release_platform:
                reason = "device operating system changed before update dispatch"
            elif architecture not in {"amd64", "x86_64", "x64"}:
                reason = "device no longer reports a supported x86_64 architecture"
            elif release_arch != AGENT_RELEASE_ARCHITECTURE:
                reason = "release architecture is no longer supported"
            elif AGENT_UPDATE_CAPABILITY not in capabilities:
                reason = "device no longer advertises agent_update.v1"
            elif heartbeat.agent_version != row["source_version"]:
                reason = "device version changed before update dispatch"
            if reason is None:
                continue
            rejected += 1
            db.execute(
                """UPDATE commands SET status='failed', error=?, finished_at=?
                   WHERE id=? AND status='queued'""",
                (reason, now, row["command_id"]),
            )
            db.execute(
                """UPDATE agent_update_jobs
                   SET status='failed', updated_at=?, completed_at=?, last_error=?
                   WHERE command_id=? AND status='queued'""",
                (now, now, reason, row["command_id"]),
            )
            Database.audit(
                db,
                actor="controller",
                action="agent_update_dispatch_rejected",
                target_type="agent",
                target_id=agent_id,
                details={"command_id": row["command_id"], "reason": reason},
            )
        return rejected

    def reject_miner_cache_commands_without_capability(
        db: sqlite3.Connection,
        *,
        agent_id: str,
        heartbeat: HeartbeatRequest,
        now: str,
    ) -> int:
        """Never dispatch or resend cache work to an Agent that lost the feature."""

        if MINER_CACHE_CAPABILITY in set(heartbeat.capabilities):
            return 0
        rows = db.execute(
            """SELECT id, action FROM commands WHERE agent_id=? AND action IN (?, ?)
               AND status IN ('queued','dispatched','running') AND expires_at >= ?""",
            (agent_id, MINER_CACHE_SCAN_ACTION, MINER_CACHE_CLEANUP_ACTION, now),
        ).fetchall()
        for row in rows:
            reason = f"Agent no longer reports {MINER_CACHE_CAPABILITY}; update Agent before retrying"
            db.execute(
                """UPDATE commands SET status='failed', error=?, finished_at=?
                   WHERE id=? AND status IN ('queued','dispatched','running')""",
                (reason, now, row["id"]),
            )
            Database.audit(
                db,
                actor="controller",
                action="miner_cache_dispatch_rejected",
                target_type="agent",
                target_id=agent_id,
                details={"command_id": row["id"], "action": row["action"], "reason": reason},
            )
        return len(rows)

    def reject_incompatible_queued_flight_sheet_commands(
        db: sqlite3.Connection,
        *,
        agent_id: str,
        heartbeat: HeartbeatRequest,
        now: str,
    ) -> int:
        """Revalidate the queued payload against the heartbeat receiving it."""

        rows = db.execute(
            """SELECT id, payload_json FROM commands
               WHERE agent_id=? AND action='apply_flight_sheet'
                 AND status='queued' AND expires_at >= ?""",
            (agent_id, now),
        ).fetchall()
        rejected = 0
        for row in rows:
            payload = _json(row["payload_json"], {})
            if not isinstance(payload, dict):
                continue
            marker = payload.get(CONTROLLER_FLIGHT_DISPATCH_MARKER)
            reason: str | None = None
            if isinstance(marker, dict):
                expected_os = marker.get("os_family")
                try:
                    current_os = normalize_os_family(heartbeat.os_family)
                except ValueError:
                    current_os = None
                if (
                    isinstance(expected_os, str)
                    and expected_os
                    and current_os != expected_os
                ):
                    reason = (
                        "device operating system changed before flight-sheet dispatch"
                    )
                minimum = marker.get("minimum_agent_version")
                minimum_match = (
                    re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", minimum)
                    if isinstance(minimum, str)
                    else None
                )
                if reason is None and minimum_match is not None:
                    minimum_tuple = tuple(
                        int(value) for value in minimum_match.groups()
                    )
                    # A 0.0.0 marker means that this legacy/generic flight has
                    # no Agent-version dependency.  Old Agents did not report
                    # a version on every heartbeat, so treating a missing
                    # value as "older than 0.0.0" would incorrectly discard a
                    # command that has no version requirement at all.
                    if minimum_tuple != (0, 0, 0) and not _agent_version_at_least(
                        heartbeat.agent_version, minimum_tuple
                    ):
                        reason = (
                            "device Agent version changed before flight-sheet "
                            f"dispatch; version {minimum} or newer is required"
                        )
                required_capabilities = marker.get("required_capabilities")
                if reason is None and isinstance(required_capabilities, list):
                    missing = sorted(
                        capability
                        for capability in required_capabilities
                        if isinstance(capability, str)
                        and capability not in set(heartbeat.capabilities)
                    )
                    if missing:
                        reason = (
                            "device capabilities changed before flight-sheet "
                            "dispatch; missing " + ", ".join(missing)
                        )
            elif (
                payload.get("secondary_mode") == "independent"
                and not _agent_version_at_least(
                    heartbeat.agent_version, (0, 6, 48)
                )
            ):
                # Compatibility for commands queued before the dispatch marker
                # was introduced.
                reason = (
                    "Independent dual mining requires RigPilot Agent 0.6.48 or "
                    "newer; device version changed before command dispatch"
                )
            if _flight_payload_has_unsupported_miner(payload):
                reason = "Unsupported or retired miner type; reconfigure the flight sheet"
            if reason is None:
                try:
                    _validate_prepared_flight_hardware({
                        "os_family": heartbeat.os_family,
                        "agent_version": heartbeat.agent_version,
                        "capabilities_json": json.dumps(heartbeat.capabilities),
                        "metrics_json": json.dumps(heartbeat.metrics),
                        "inventory_json": json.dumps(heartbeat.inventory),
                    }, payload)
                except HTTPException as exc:
                    reason = str(exc.detail)
            if reason is None:
                continue
            updated = db.execute(
                """UPDATE commands SET status='failed', error=?, finished_at=?
                   WHERE id=? AND status='queued'""",
                (reason, now, row["id"]),
            )
            if updated.rowcount != 1:
                continue
            rejected += 1
            db.execute(
                """UPDATE configuration_refreshes
                   SET status='pending', command_id=NULL, claim_token=NULL,
                       updated_at=?, queued_at=NULL, completed_at=NULL,
                       last_error=?
                   WHERE command_id=? AND status='queued'""",
                (now, reason, row["id"]),
            )
            Database.audit(
                db,
                actor="controller",
                action="flight_sheet_dispatch_rejected",
                target_type="agent",
                target_id=agent_id,
                details={"command_id": row["id"], "reason": reason},
            )
        return rejected

    def require_supported_mining_restart(
        db: sqlite3.Connection, agent: sqlite3.Row | dict[str, Any],
    ) -> None:
        """Validate saved miner identities without treating an old draft as runtime truth."""
        inventory = _json(_optional_row_value(agent, "inventory_json", "{}"), {})
        mining = inventory.get("mining_status") if isinstance(inventory, dict) else None
        if not isinstance(mining, dict):
            metrics = _json(_optional_row_value(agent, "metrics_json", "{}"), {})
            mining = metrics.get("mining_status") if isinstance(metrics, dict) else None
        if not isinstance(mining, dict) or mining.get("configured") is False:
            return
        supported = {*MINER_TYPES, "generic"}

        def checked_type(value: Any) -> str | None:
            if value is None or value == "":
                return None
            normalized = value.strip().lower() if isinstance(value, str) else value
            if not isinstance(normalized, str) or normalized not in supported:
                raise HTTPException(
                    409,
                    "该飞行表使用已移除或不受支持的矿工类型，现已停用且不会下发；"
                    "请编辑后改用当前支持的托管矿工。",
                )
            return normalized

        # Every explicit local identity must be supported, including a second
        # leg. A supported primary must not hide an unknown secondary or PID.
        top_type = checked_type(mining.get("miner_type"))
        reported_types: dict[str, str | None] = {}
        for role in ("primary", "secondary"):
            leg = mining.get(role)
            reported_types[role] = checked_type(leg.get("miner_type")) if isinstance(leg, dict) else None
        reported_types["primary"] = reported_types["primary"] or top_type
        missing_roles = [role for role, kind in reported_types.items() if kind is None
                         and not (role == "secondary" and "secondary" in mining and mining[role] is None)]
        if not missing_roles:
            return
        sheet_id = _reported_agent_flight_sheet_id(agent)
        if not sheet_id:
            return
        # A successful, bound apply proves what was saved before the editable
        # Controller sheet changed or was deleted. Legacy wire omits generic.
        saved: dict[str, Any] | None = None
        for command in db.execute(
            """SELECT payload_json FROM commands
               WHERE agent_id=? AND action='apply_flight_sheet' AND status='succeeded'
               ORDER BY rowid DESC""", (agent["id"],),
        ):
            payload = _json(command["payload_json"], {})
            if isinstance(payload, dict) and payload.get("flight_sheet_id") == sheet_id:
                saved = payload
                break
        sheet = None if saved is not None else db.execute(
            "SELECT * FROM flight_sheets WHERE id=?", (sheet_id,),
        ).fetchone()
        for role in missing_roles:
            if saved is not None:
                leg = saved.get(role)
                if isinstance(leg, dict):
                    checked_type(leg.get("miner_type", "generic"))
            elif sheet is not None and (role == "primary" or sheet["secondary_coin"]):
                checked_type(sheet["miner_type" if role == "primary" else "secondary_miner_type"] or "generic")

    def reject_unsupported_queued_restarts(
        db: sqlite3.Connection, *, agent: dict[str, Any], now: str,
    ) -> None:
        """Fail queued mining restarts whose saved miner became unsupported.

        A restart queued before the local configuration or the editable sheet
        changed must never be resent to the Agent.  Keep the command row as a
        terminal receipt with the rejection reason (0.8.48: this replaces the
        former HiveOS-specific variant; the supported-miner check is the same).
        """
        rows = db.execute(
            """SELECT id FROM commands WHERE agent_id=? AND action='restart_mining' AND status='queued' AND expires_at>=?""",
            (agent["id"], now),
        ).fetchall()
        if not rows:
            return
        try:
            require_supported_mining_restart(db, agent)
        except HTTPException as exc:
            for row in rows:
                db.execute(
                    """UPDATE commands SET status='failed',error=?,finished_at=? WHERE id=? AND status='queued'""",
                    (str(exc.detail), now, row["id"]),
                )
                Database.audit(
                    db, actor="controller", action="mining_restart_dispatch_rejected",
                    target_type="agent", target_id=agent["id"],
                    details={"command_id": row["id"], "reason": str(exc.detail)},
                )

    async def maintenance_loop() -> None:
        next_telemetry_prune = time.monotonic() + 60
        next_history_prune = time.monotonic() + 3600
        while True:
            await asyncio.sleep(60)
            try:
                if time.monotonic() >= next_telemetry_prune:
                    deleted = database.prune_telemetry(
                        settings.telemetry_retention_days,
                        max_rows=_TELEMETRY_PRUNE_BATCH_ROWS,
                    )
                    # Drain a legacy backlog gradually instead of one
                    # deployment-time transaction against a multi-GB table.
                    next_telemetry_prune = time.monotonic() + (
                        60
                        if deleted >= _TELEMETRY_PRUNE_BATCH_ROWS
                        else 3600
                    )
                if time.monotonic() >= next_history_prune:
                    database.prune_hashrate_history(
                        settings.hashrate_history_retention_days
                    )
                    database.prune_device_work(WORKLOAD_RETENTION_DAYS)
                    database.prune_enrollment_tokens(
                        ENROLLMENT_TOKEN_METADATA_RETENTION_DAYS
                    )
                    database.prune_agent_replacement_tokens(
                        ENROLLMENT_TOKEN_METADATA_RETENTION_DAYS
                    )
                    next_history_prune = time.monotonic() + 3600
                now = utcnow()
                with database.connection() as db:
                    expire_stale_agent_commands(db, now=now)
            except Exception:
                LOGGER.exception("Controller maintenance iteration failed; will retry")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        instance_lock.acquire()
        try:
            generated = database.initialize(settings.admin_username, settings.admin_password)
            database.prune_telemetry(
                settings.telemetry_retention_days,
                max_rows=_TELEMETRY_PRUNE_BATCH_ROWS,
            )
            database.prune_hashrate_history(
                settings.hashrate_history_retention_days
            )
            database.prune_device_work(WORKLOAD_RETENTION_DAYS)
            database.prune_enrollment_tokens(
                ENROLLMENT_TOKEN_METADATA_RETENTION_DAYS
            )
            database.prune_agent_replacement_tokens(
                ENROLLMENT_TOKEN_METADATA_RETENTION_DAYS
            )
            if generated:
                LOGGER.warning("=" * 72)
                LOGGER.warning("First-run admin password for %s: %s", settings.admin_username, generated)
                LOGGER.warning("Set RIGPILOT_ADMIN_PASSWORD before production deployment.")
                LOGGER.warning("=" * 72)
            maintenance_task = asyncio.create_task(maintenance_loop())
            try:
                yield
            finally:
                maintenance_task.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance_task
        finally:
            instance_lock.release()

    app = FastAPI(
        title="RigPilot",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    @app.exception_handler(RequestValidationError)
    async def enrollment_validation_error(request: Request, exc: RequestValidationError):
        # Rejected input/ctx may contain credentials, private wallet material,
        # or non-finite values. Keep useful diagnostics without echoing them.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
            for error in exc.errors()
        ]})

    app.add_middleware(RequestBodyLimitMiddleware, settings=settings)
    app.state.db = database
    app.state.settings = settings
    app.state.managed_miner_catalog = managed_miner_catalog
    app.state.controller_metrics = controller_metrics

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable):
        content_length = request.headers.get("content-length")
        path = request.url.path
        if content_length and path.startswith("/api/agent/"):
            try:
                parsed_content_length = int(content_length)
                if parsed_content_length < 0:
                    raise ValueError
                if parsed_content_length > AGENT_REQUEST_BODY_LIMIT_BYTES:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large"})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        sensitive_secret_response = bool(
            path in {
                "/api/enrollment-tokens",
                "/api/agent/enroll",
                "/api/agent/replace",
                "/api/workload-settlement-note",
                "/api/auth/password",
            }
            or (
                path.startswith("/api/agents/")
                and path.endswith("/replacement-token")
            )
        )
        if sensitive_secret_response:
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def current_user(
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        token = _bearer(authorization)
        payload = verify_session_token(token, settings.secret_key)
        if not payload:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
        session_version = payload.get("sv", 0)
        if type(session_version) is not int or session_version < 0:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
        with database.connection() as db:
            row = db.execute(
                "SELECT id, username, role, is_active, session_version FROM users WHERE id = ?",
                (payload.get("uid"),),
            ).fetchone()
        if not row or not row["is_active"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User is disabled")
        if session_version != row["session_version"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
        return dict(row)

    def require_role(minimum: str):
        def dependency(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
            if ROLE_LEVEL[user["role"]] < ROLE_LEVEL[minimum]:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
            return user

        return dependency

    def current_agent(
        request: Request,
        x_agent_id: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if settings.require_https_for_agents and request.url.scheme != "https":
            raise HTTPException(400, "HTTPS is required for agent connections")
        if not x_agent_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing agent id")
        secret = _bearer(authorization)
        with database.connection() as db:
            row = db.execute("SELECT * FROM agents WHERE id = ?", (x_agent_id,)).fetchone()
        if not row or row["disabled_at"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or disabled agent")
        if not secrets.compare_digest(row["secret_hash"], hash_api_secret(secret)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent credential")
        return dict(row)

    def _require_current_agent_identity(
        db: sqlite3.Connection,
        authenticated_agent: dict[str, Any],
    ) -> None:
        """Recheck identity inside the transaction admitting an Agent action."""

        current = db.execute(
            "SELECT secret_hash, disabled_at FROM agents WHERE id=?",
            (authenticated_agent["id"],),
        ).fetchone()
        if (
            current is None
            or current["disabled_at"] is not None
            or not secrets.compare_digest(
                str(current["secret_hash"] or ""),
                str(authenticated_agent["secret_hash"] or ""),
            )
        ):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Agent identity is disabled, removed, or replaced; reconnect with the current identity",
            )

    def unsupported_restart_filter(
        db: sqlite3.Connection, agent: sqlite3.Row | dict[str, Any], rows: list[sqlite3.Row]
    ) -> list[sqlite3.Row]:
        if not any(row["action"] == "restart_mining" for row in rows):
            return rows
        try:
            require_supported_mining_restart(db, agent)
        except HTTPException:
            # Keep historical inflight receipts, but never resend a start for
            # an unsupported saved miner. Queued restarts fail above.
            return [row for row in rows if row["action"] != "restart_mining"]
        return rows

    def queue_agent_command(
        db: sqlite3.Connection,
        *,
        agent_id: str,
        action: str,
        payload: dict[str, Any],
        user: dict[str, Any],
        ttl_seconds: int = 600,
        mining_autostart: dict[str, str] | None = None,
        gpu_oc_intent: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if CONTROLLER_GPU_OC_INTENT_MARKER in payload:
            raise HTTPException(422, "Controller GPU OC intent is not a public payload field")
        if gpu_oc_intent is not None:
            if action != "apply_gpu_profile":
                raise ValueError("GPU OC intent requires an OC command")
            payload = {**payload, CONTROLLER_GPU_OC_INTENT_MARKER: dict(gpu_oc_intent)}
        allowed_actions = {
            "collect_diagnostics",
            "collect_miner_logs",
            "stop_mining",
            "restart_mining",
            "remove_flight_sheet",
            MINING_RESIDUE_SCAN_ACTION,
            MINING_RESIDUE_CLEANUP_ACTION,
            "restart_service",
            "reboot",
            "shutdown",
            "apply_flight_sheet",
            "apply_gpu_profile",
            GPU_OC_STATE_MIGRATION_ACTION,
            LINUX_SSH_CONFIGURE_ACTION,
        }
        if action not in allowed_actions:
            raise HTTPException(422, "Unsupported command action")
        if action == "collect_miner_logs" and payload:
            raise HTTPException(422, "collect_miner_logs payload must be empty")
        if action in MINING_CONTROL_ACTIONS and payload:
            raise HTTPException(422, "mining control payload must be empty")
        if mining_autostart is not None:
            payload = {**payload, CONTROLLER_MINING_AUTOSTART_MARKER: mining_autostart}
        automatic_reapply = user.get("_automatic_reapply")
        if action == "apply_flight_sheet" and isinstance(automatic_reapply, dict):
            payload = {
                **payload,
                "_controller_auto_reapply": {
                    "flight_sheet_id": str(
                        automatic_reapply.get("flight_sheet_id") or ""
                    ),
                    "trigger_type": str(
                        automatic_reapply.get("trigger_type") or "configuration"
                    )[:32],
                    "trigger_id": str(automatic_reapply.get("trigger_id") or "")[:64],
                    "configuration_fingerprint": str(
                        automatic_reapply.get("configuration_fingerprint") or ""
                    ),
                    "desired_apply": automatic_reapply.get("desired_apply") is True,
                    "created_at": utcnow(),
                },
            }
            ttl_seconds = max(ttl_seconds, 3_600)
        _validate_json_payload(payload, "command payload", max_bytes=65_536)
        agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not agent:
            raise HTTPException(404, "Agent not found")
        if agent["disabled_at"]:
            raise HTTPException(409, "Agent is disabled")
        if action in DEVICE_STATE_TRANSITION_ACTIONS:
            active_cache = db.execute(
                """SELECT id FROM commands WHERE agent_id=? AND action IN (?, ?)
                   AND status IN ('queued','dispatched','running')
                   AND expires_at >= ? LIMIT 1""",
                (agent_id, MINER_CACHE_SCAN_ACTION, MINER_CACHE_CLEANUP_ACTION, utcnow()),
            ).fetchone()
            if active_cache:
                raise HTTPException(409, "Device command is blocked while miner cache scan or cleanup is in progress")
        if action == "restart_mining":
            require_supported_mining_restart(db, agent)
        if action in MINING_CONTROL_ACTIONS:
            payload = {
                **payload,
                CONTROLLER_MINING_RESULT_BINDING_MARKER:
                    _reported_agent_flight_sheet_id(agent)
            }
        if action in {
            "apply_flight_sheet",
            "apply_gpu_profile",
            *MINING_CONTROL_ACTIONS,
            *MINING_RESIDUE_ACTIONS,
        }:
            migration = db.execute(
                """SELECT id FROM commands
                   WHERE agent_id=? AND action=?
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   LIMIT 1""",
                (agent_id, GPU_OC_STATE_MIGRATION_ACTION, utcnow()),
            ).fetchone()
            if migration:
                raise HTTPException(
                    409,
                    "GPU OC state migration is in progress; retry after it finishes",
                )
        if action in MINING_RESIDUE_ACTIONS:
            if _serialize_agent(agent, settings)["status"] != "online":
                raise HTTPException(409, "Mining residue repair requires an online device")
            capabilities = set(_json(agent["capabilities_json"], []))
            if MINING_RESIDUE_REPAIR_CAPABILITY not in capabilities:
                raise HTTPException(
                    409,
                    "Mining residue repair is unavailable until the Agent reports "
                    f"{MINING_RESIDUE_REPAIR_CAPABILITY}",
                )
            if (
                action == MINING_RESIDUE_CLEANUP_ACTION
                and "targets" in payload
                and MINING_RESIDUE_SELECTIVE_CAPABILITY not in capabilities
            ):
                raise HTTPException(
                    409,
                    "Selective mining residue cleanup is unavailable until "
                    "the Agent reports "
                    f"{MINING_RESIDUE_SELECTIVE_CAPABILITY}",
                )
            if (
                action == MINING_RESIDUE_CLEANUP_ACTION
                and _mining_residue_cleanup_uses_explicit_candidate(payload)
                and MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY
                not in capabilities
                and not _explicit_residue_cleanup_matches_latest_scan(
                    db,
                    agent_id=agent_id,
                    payload=payload,
                )
            ):
                raise HTTPException(
                    409,
                    "Explicit mining residue candidate cleanup is unavailable "
                    "until the Agent reports "
                    f"{MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY}",
                )
            residue_transition = db.execute(
                """SELECT action FROM commands
                   WHERE agent_id=? AND action IN (?, ?)
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   LIMIT 1""",
                (
                    agent_id,
                    MINING_RESIDUE_SCAN_ACTION,
                    MINING_RESIDUE_CLEANUP_ACTION,
                    utcnow(),
                ),
            ).fetchone()
            if residue_transition:
                raise HTTPException(
                    409,
                    "A mining residue scan or cleanup is already in progress",
                )
            mining_transition = db.execute(
                """SELECT action FROM commands
                   WHERE agent_id=?
                     AND action IN (
                       'apply_flight_sheet','stop_mining','restart_mining',
                       'remove_flight_sheet','apply_gpu_profile','restart_service',
                       'reboot','shutdown','agent_update.v1',
                       'migrate_gpu_oc_state.v1',
                       'configure_linux_ssh.v1'
                     )
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   ORDER BY created_at DESC LIMIT 1""",
                (agent_id, utcnow()),
            ).fetchone()
            if mining_transition:
                raise HTTPException(
                    409,
                    "Mining residue repair cannot run while command "
                    f"{mining_transition['action']} is in progress",
                )
        if action == "collect_miner_logs" and not _agent_version_at_least(
            agent["agent_version"], (0, 5, 1)
        ):
            raise HTTPException(
                409,
                "Miner log collection requires RigPilot Agent 0.5.1 or newer; "
                "upgrade this device first",
            )
        if action in MINING_CONTROL_ACTIONS:
            if _serialize_agent(agent, settings)["status"] != "online":
                raise HTTPException(409, "Mining control requires an online device")
            if not _agent_version_at_least(agent["agent_version"], (0, 5, 2)):
                raise HTTPException(
                    409,
                    "Mining control requires RigPilot Agent 0.5.2 or newer; "
                    "upgrade this device first",
                )
            if action == "remove_flight_sheet" and not _agent_version_at_least(
                agent["agent_version"], (0, 6, 1)
            ):
                raise HTTPException(
                    409,
                    "Removing a flight sheet requires RigPilot Agent 0.6.1 or newer",
                )
            residue_transition = db.execute(
                """SELECT id FROM commands
                   WHERE agent_id=? AND action IN (?, ?)
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   LIMIT 1""",
                (
                    agent_id,
                    MINING_RESIDUE_SCAN_ACTION,
                    MINING_RESIDUE_CLEANUP_ACTION,
                    utcnow(),
                ),
            ).fetchone()
            if residue_transition:
                raise HTTPException(
                    409,
                    "Mining control is blocked while mining residue repair is in progress",
                )
            unfinished = db.execute(
                """SELECT id FROM commands
                   WHERE agent_id=?
                     AND action IN ('stop_mining','restart_mining','remove_flight_sheet')
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   LIMIT 1""",
                (agent_id, utcnow()),
            ).fetchone()
            if unfinished:
                raise HTTPException(
                    409, "A mining control command is already in progress"
                )
        command_id = str(uuid.uuid4())
        created_moment = datetime.now(timezone.utc)
        created_at = created_moment.isoformat(timespec="microseconds")
        expires_at = (created_moment + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="seconds"
        )
        hard_expires_at = _command_hard_expiry(
            created_moment,
            action=action,
            ttl_seconds=ttl_seconds,
        )
        db.execute(
            """INSERT INTO commands
               (id, agent_id, action, payload_json, status, created_by,
                created_at, expires_at, hard_expires_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                command_id,
                agent_id,
                action,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                "queued",
                user["id"],
                created_at,
                expires_at,
                hard_expires_at,
            ),
        )
        audit_details: dict[str, Any] = {
            "command_id": command_id,
            "action": action,
        }
        if action == "apply_flight_sheet":
            gpu_oc = payload.get("gpu_oc")
            audit_details.update(
                {
                    "flight_sheet_id": payload.get("flight_sheet_id"),
                    "secondary_enabled": payload.get("secondary") is not None,
                    "gpu_oc_enabled": isinstance(gpu_oc, dict),
                    "gpu_oc_indices": (
                        [
                            setting.get("index")
                            for setting in gpu_oc.get("settings", [])
                            if isinstance(setting, dict)
                            and isinstance(setting.get("index"), int)
                        ]
                        if isinstance(gpu_oc, dict)
                        else []
                    ),
                    "gpu_oc_models": (
                        [
                            setting.get("model_match")
                            for setting in gpu_oc.get("settings", [])
                            if isinstance(setting, dict)
                            and isinstance(setting.get("model_match"), str)
                        ]
                        if isinstance(gpu_oc, dict)
                        else []
                    ),
                }
            )
            selected_seine_packages = []
            for role in ("primary", "secondary"):
                leg = payload.get(role)
                package = leg.get("miner_package") if isinstance(leg, dict) else None
                if (
                    isinstance(leg, dict) and leg.get("miner_type") == "seine"
                    and isinstance(package, dict) and str(package.get("id", "")).startswith("test-")
                ):
                    selected_seine_packages.append({
                        "role": role, "id": package["id"], "version": package.get("version"),
                        "sha256": package.get("sha256"), "managed_channel": "test",
                    })
            if selected_seine_packages:
                audit_details["selected_miner_packages"] = selected_seine_packages
        else:
            audit_details["payload"] = _command_payload_for_view(
                json.dumps(payload, separators=(",", ":"))
            )
        Database.audit(
            db,
            actor=user["username"],
            action="command_queued",
            target_type="agent",
            target_id=agent_id,
            details=audit_details,
        )
        return {
            "id": command_id,
            "agent_id": agent_id,
            "action": action,
            "payload": _command_payload_for_view(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            ),
            "status": "queued",
            "created_at": created_at,
            "expires_at": expires_at,
        }

    def reconcile_mining_autostart(
        db: sqlite3.Connection,
        *,
        agent: sqlite3.Row,
        now: str,
    ) -> dict[str, Any] | None:
        """Queue exactly one normal mining restart for each observed boot."""

        if not bool(_optional_row_value(agent, "mining_autostart_enabled", 0)):
            return None
        metrics = _json(agent["metrics_json"], {})
        current_boot = _boot_time_identity(metrics)
        if current_boot is None:
            return None
        previous_boot = _optional_row_value(
            agent, "mining_autostart_last_boot_time"
        )
        if previous_boot is None:
            # Older rows and first reports establish a baseline; enabling the
            # switch must never start mining on the current boot.
            db.execute(
                """UPDATE agents SET mining_autostart_last_boot_time=?
                   WHERE id=? AND mining_autostart_last_boot_time IS NULL""",
                (current_boot, agent["id"]),
            )
            return None
        if previous_boot == current_boot:
            return None

        inventory = _json(agent["inventory_json"], {})
        mining = (
            inventory.get("mining_status")
            if isinstance(inventory, dict)
            else None
        )
        if not isinstance(mining, dict) and isinstance(metrics, dict):
            mining = metrics.get("mining_status")
        if not isinstance(mining, dict) or mining.get("configured") is not True:
            return None

        if _active_agent_flight_sheet_id(agent, settings) is not None:
            db.execute(
                """UPDATE agents SET mining_autostart_last_boot_time=?
                   WHERE id=? AND mining_autostart_last_boot_time=?""",
                (current_boot, agent["id"], previous_boot),
            )
            return None

        # Only the Agent's verified stopped state is safe to restart.  Startup,
        # stale-process, and error states are retried on a later heartbeat.
        process_error = mining.get("process_error")
        if (
            str(mining.get("state") or "").casefold() != "stopped"
            or mining.get("process_verified") is not True
            or (
                process_error
                and not _is_verified_rebooted_miner_stop_error(process_error)
            )
        ):
            return None
        conflict = db.execute(
            """SELECT id, action FROM commands
               WHERE agent_id=?
                 AND action IN (
                   'apply_flight_sheet','stop_mining','restart_mining',
                   'remove_flight_sheet','restart_service','reboot','shutdown',
                   'agent_update.v1','apply_gpu_profile',
                   'migrate_gpu_oc_state.v1','scan_mining_residue.v1',
                   'cleanup_mining_residue.v1','scan_miner_cache.v1',
                   'cleanup_miner_cache.v1',
                   'configure_linux_ssh.v1'
                 )
                 AND status IN ('queued','dispatched','running')
                 AND expires_at >= ?
               LIMIT 1""",
            (agent["id"], now),
        ).fetchone()
        if conflict:
            return None
        creator = db.execute(
            """SELECT id, role FROM users
               WHERE is_active=1 AND role IN ('admin','operator')
               ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, id
               LIMIT 1"""
        ).fetchone()
        if creator is None:
            return None
        try:
            command = queue_agent_command(
                db,
                agent_id=agent["id"],
                action="restart_mining",
                payload={},
                mining_autostart={"boot_time": current_boot, "previous_boot_time": previous_boot},
                user={
                    "id": creator["id"],
                    "username": "controller",
                    "role": creator["role"],
                },
                ttl_seconds=600,
            )
        except HTTPException:
            # A version/online/conflict guard may change between heartbeats.
            # Preserve the old marker so the next accepted heartbeat retries.
            return None
        Database.audit(
            db,
            actor="controller",
            action="mining_autostart_queued",
            target_type="agent",
            target_id=agent["id"],
            details={
                "command_id": command["id"],
                "boot_time": current_boot,
                "previous_boot_time": previous_boot,
            },
        )
        return command

    def queue_legacy_gpu_oc_migration(
        db: sqlite3.Connection,
        *,
        agent: sqlite3.Row,
        reported_settings: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Queue one history-bound, zero-NVML-write v1 ownership adoption."""

        if (
            not reported_settings
            or not _agent_version_at_least(agent["agent_version"], (0, 6, 17))
            or _desired_gpu_oc_entries(
                _optional_row_value(agent, "desired_gpu_oc_json", "[]")
            )
        ):
            return None
        state_fingerprint = _legacy_gpu_oc_state_fingerprint(reported_settings)
        marker = _json(
            _optional_row_value(agent, "gpu_oc_migration_marker_json", "{}"),
            {},
        )
        # Success is a permanent opt-out from automatic import.  For other
        # outcomes, suppress only the same v1 state so a genuinely changed
        # local state may be reconsidered once, without heartbeat audit spam.
        if isinstance(marker, dict) and (
            marker.get("status") == "succeeded"
            or marker.get("state_fingerprint") == state_fingerprint
        ):
            return None

        def persist_marker(status_value: str, **extra: Any) -> None:
            value = {
                "state_fingerprint": state_fingerprint,
                "status": status_value,
                "updated_at": utcnow(),
                **extra,
            }
            db.execute(
                "UPDATE agents SET gpu_oc_migration_marker_json=? WHERE id=?",
                (
                    json.dumps(value, separators=(",", ":"), ensure_ascii=False),
                    agent["id"],
                ),
            )

        existing = db.execute(
            "SELECT id FROM commands WHERE agent_id=? AND action=? LIMIT 1",
            (agent["id"], GPU_OC_STATE_MIGRATION_ACTION),
        ).fetchone()
        if existing:
            return None
        active = db.execute(
            """SELECT id FROM commands
               WHERE agent_id=? AND status IN ('queued','dispatched','running')
               LIMIT 1""",
            (agent["id"],),
        ).fetchone()
        if active:
            return None
        reconstructed = _reconstruct_legacy_gpu_oc_migration(
            db,
            agent_id=agent["id"],
            reported_settings=reported_settings,
        )
        if reconstructed is None:
            persist_marker("skipped")
            Database.audit(
                db,
                actor="controller",
                action="legacy_gpu_oc_migration_skipped",
                target_type="agent",
                target_id=agent["id"],
                details={
                    "reason": "successful command history does not fully bind reported v1 state",
                    "reported_indices": [item["index"] for item in reported_settings],
                },
            )
            return None
        payload, creator_id = reconstructed
        if not _legacy_gpu_oc_migration_hardware_ready(
            _json(_optional_row_value(agent, "metrics_json", "{}"), {}),
            payload,
        ):
            # Hardware can disappear temporarily during boot.  This is not a
            # terminal reconstruction failure and is intentionally unaudited;
            # a later heartbeat with a complete unique UUID topology retries.
            return None
        creator = db.execute(
            "SELECT id, username FROM users WHERE id=?",
            (creator_id,),
        ).fetchone()
        if creator is None:
            persist_marker("skipped", reason="history_creator_missing")
            Database.audit(
                db,
                actor="controller",
                action="legacy_gpu_oc_migration_skipped",
                target_type="agent",
                target_id=agent["id"],
                details={"reason": "history command creator no longer exists"},
            )
            return None
        command = queue_agent_command(
            db,
            agent_id=agent["id"],
            action=GPU_OC_STATE_MIGRATION_ACTION,
            payload=payload,
            user={
                "id": creator["id"],
                "username": creator["username"],
                "role": "operator",
            },
        )
        persist_marker("queued", command_id=command["id"])
        Database.audit(
            db,
            actor="controller",
            action="legacy_gpu_oc_migration_queued",
            target_type="agent",
            target_id=agent["id"],
            details={
                "command_id": command["id"],
                "history_command_ids": payload["history_command_ids"],
                "gpu_uuids": [item["gpu_uuid"] for item in payload["devices"]],
                "zero_nvml_write_protocol": True,
            },
        )
        return command

    def reconcile_gpu_oc_replay_blocks(db: sqlite3.Connection, *, agent_id: str) -> sqlite3.Row | None:
        """Adopt old bound fault receipts before any autonomous replay.

        Safety state belongs to desired intent, so clearing operation history
        cannot re-enable it. Legacy adoption has no arbitrary history window.
        """
        agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None:
            return None
        desired = _desired_gpu_oc_entries(agent["desired_gpu_oc_json"])
        by_uuid = {entry["gpu_uuid"]: entry for entry in desired}
        legacy_by_uuid = {entry["gpu_uuid"]: entry for entry in desired if "intent_id" not in entry}
        changed = False
        if legacy_by_uuid:
            rows = db.execute(
                """SELECT * FROM commands WHERE agent_id=?
                   AND action='apply_gpu_profile' AND status IN ('failed','succeeded')
                   ORDER BY rowid DESC""", (agent_id,),
            )
            succeeded_uuids: set[str] = set()
            for command in rows:
                payload = _json(command["payload_json"], {})
                entry = legacy_by_uuid.get(payload.get("expected_gpu_uuid")) if isinstance(payload, dict) and isinstance(payload.get("expected_gpu_uuid"), str) else None
                if entry is None or entry["gpu_uuid"] in succeeded_uuids or _gpu_oc_replay_is_blocked(entry):
                    continue
                if command["status"] == "succeeded":
                    if payload.get("mode") == "replace_one" and _gpu_oc_command_matches_intent(payload, entry):
                        succeeded_uuids.add(entry["gpu_uuid"])
                    continue
                block = _gpu_oc_failure_replay_block(
                    command, payload, _json(command["result_json"], {}), command["error"], entry,
                )
                if block is not None:
                    entry["intent_id"] = block["intent_id"]
                    entry["replay_block"] = block
                    changed = True
            for entry in legacy_by_uuid.values():
                entry["intent_id"] = _gpu_oc_intent_marker(entry)["intent_id"]
                changed = True
        blocked = {entry["gpu_uuid"] for entry in desired if _gpu_oc_replay_is_blocked(entry)}
        pending_before = set(_desired_gpu_oc_pending_uuids(agent["desired_gpu_oc_pending_json"]))
        pending = pending_before - blocked
        if changed or pending != pending_before:
            db.execute(
                "UPDATE agents SET desired_gpu_oc_json=?, desired_gpu_oc_pending_json=? WHERE id=?",
                (json.dumps(desired, separators=(",", ":"), ensure_ascii=False), json.dumps(sorted(pending)), agent_id),
            )
        if blocked:
            for command in db.execute(
                """SELECT id,payload_json FROM commands WHERE agent_id=?
                   AND action='apply_gpu_profile' AND status='queued'""", (agent_id,),
            ).fetchall():
                payload = _json(command["payload_json"], {})
                entry = by_uuid.get(payload.get("expected_gpu_uuid")) if isinstance(payload, dict) and isinstance(payload.get("expected_gpu_uuid"), str) else None
                if (
                    entry is not None and entry["gpu_uuid"] in blocked
                    and str(payload.get("gpu_profile_id", "")).startswith("device-oc-reapply-")
                    and _gpu_oc_command_matches_intent(payload, entry)
                ):
                    db.execute("UPDATE commands SET status='cancelled',finished_at=? WHERE id=? AND status='queued'", (utcnow(), command["id"]))
        return db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()

    def queue_desired_gpu_oc_reapply(
        db: sqlite3.Connection,
        *,
        agent_id: str,
        source_command: sqlite3.Row | dict[str, Any],
        only_gpu_uuids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Queue UUID-verified desired NVML state after mining is live again.

        A flight-sheet apply and a managed-miner restart both start the miner
        themselves.  Waiting for their successful result ensures a failed
        launch never leaves an unrelated OC command behind, while making the
        controller-owned per-card values the final settings when a flight also
        declares SRBMiner CLI OC.
        """

        agent = reconcile_gpu_oc_replay_blocks(db, agent_id=agent_id)
        if agent is None or agent["disabled_at"]:
            return []
        desired = _desired_gpu_oc_entries(
            _optional_row_value(agent, "desired_gpu_oc_json", "[]")
        )
        if not desired or not _agent_version_at_least(agent["agent_version"], (0, 6, 16)):
            return []
        desired_uuids = {entry["gpu_uuid"] for entry in desired if not _gpu_oc_replay_is_blocked(entry)}
        target_uuids = (
            desired_uuids
            if only_gpu_uuids is None
            else desired_uuids.intersection(only_gpu_uuids)
        )
        if not target_uuids:
            return []
        pending_before = set(
            _desired_gpu_oc_pending_uuids(
                _optional_row_value(agent, "desired_gpu_oc_pending_json", "[]")
            )
        ).intersection(desired_uuids)
        # A stop/remove requested while the launch command was running wins.
        # Never manufacture a post-stop OC task from a late success receipt.
        stopping = db.execute(
            """SELECT id FROM commands
               WHERE agent_id=? AND action IN ('stop_mining','remove_flight_sheet')
                 AND status IN ('queued','dispatched','running') LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if stopping:
            return []
        # A direct operator edit or an earlier follow-up already represents the
        # latest persisted desired state.  Do not race duplicate transitions.
        existing = db.execute(
            """SELECT id FROM commands
               WHERE agent_id=?
                 AND action IN ('apply_gpu_profile','migrate_gpu_oc_state.v1')
                 AND status IN ('queued','dispatched','running') LIMIT 1""",
            (agent_id,),
        ).fetchone()
        if existing:
            return []
        metrics = _json(agent["metrics_json"], {})
        reported_gpus = metrics.get("gpus") if isinstance(metrics, dict) else None
        reported_by_uuid: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if isinstance(reported_gpus, list):
            for candidate in reported_gpus[:32]:
                if not isinstance(candidate, dict):
                    continue
                candidate_uuid = str(candidate.get("uuid") or "").strip()
                candidate_index = candidate.get("index")
                if isinstance(candidate_index, str) and candidate_index.isdigit():
                    candidate_index = int(candidate_index)
                if (
                    not candidate_uuid
                    or isinstance(candidate_index, bool)
                    or not isinstance(candidate_index, int)
                    or not 0 <= candidate_index <= 31
                    or str(candidate.get("vendor") or "nvidia").lower() != "nvidia"
                ):
                    continue
                reported_by_uuid[candidate_uuid].append(
                    {**candidate, "index": candidate_index}
                )
        remapped: list[tuple[dict[str, Any], int]] = []
        skipped: list[dict[str, Any]] = []
        used_current_indices: set[int] = set()
        synchronized_desired: list[dict[str, Any]] = []
        for entry in desired:
            targeted = entry["gpu_uuid"] in target_uuids
            matches = reported_by_uuid.get(str(entry["gpu_uuid"]), [])
            if len(matches) != 1:
                if targeted:
                    skipped.append(
                        {
                            "gpu_uuid": entry["gpu_uuid"],
                            "old_index": entry["setting"]["index"],
                            "reason": (
                                "uuid_not_reported" if not matches else "uuid_ambiguous"
                            ),
                        }
                    )
                synchronized_desired.append(entry)
                continue
            current_gpu = matches[0]
            current_index = int(current_gpu["index"])
            if current_index in used_current_indices:
                if targeted:
                    skipped.append(
                        {
                            "gpu_uuid": entry["gpu_uuid"],
                            "old_index": entry["setting"]["index"],
                            "reason": "reported_index_collision",
                        }
                    )
                synchronized_desired.append(entry)
                continue
            used_current_indices.add(current_index)
            if targeted:
                remapped.append((entry, current_index))
            synchronized_setting = {**entry["setting"], "index": current_index}
            synchronized_desired.append({**entry, "setting": synchronized_setting})
        synchronized_indices = [
            int(entry["setting"]["index"]) for entry in synchronized_desired
        ]
        indices_synchronized = len(synchronized_indices) == len(set(synchronized_indices))
        if indices_synchronized and synchronized_desired != desired:
            synchronized_desired.sort(
                key=lambda entry: int(entry["setting"]["index"])
            )
            db.execute(
                "UPDATE agents SET desired_gpu_oc_json=? WHERE id=?",
                (
                    json.dumps(
                        synchronized_desired,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    agent_id,
                ),
            )
        if skipped or not indices_synchronized:
            Database.audit(
                db,
                actor="controller",
                action="desired_gpu_oc_reapply_skipped",
                target_type="agent",
                target_id=agent_id,
                details={
                    "source_command_id": source_command["id"],
                    "items": skipped,
                    "index_sync_skipped": not indices_synchronized,
                },
            )
        # Desired OC remains pending until the Agent confirms each card.  This
        # also makes a lost/expired follow-up recoverable: once its command is
        # no longer active, a later heartbeat can safely queue the same UUID
        # again instead of silently treating "queued" as "applied".
        pending_after = (
            target_uuids
            if only_gpu_uuids is None
            else pending_before | target_uuids
        )
        db.execute(
            "UPDATE agents SET desired_gpu_oc_pending_json=? WHERE id=?",
            (
                json.dumps(sorted(pending_after), separators=(",", ":")),
                agent_id,
            ),
        )
        if not remapped:
            return []
        creator = db.execute(
            "SELECT id, username FROM users WHERE id=?",
            (source_command["created_by"],),
        ).fetchone()
        if creator is None:
            return []
        user = {"id": creator["id"], "username": creator["username"], "role": "operator"}
        queued: list[dict[str, Any]] = []
        for entry, gpu_index in remapped:
            remapped_setting = {**entry["setting"], "index": gpu_index}
            payload = {
                "gpu_profile_id": f"device-oc-reapply-{uuid.uuid4().hex}",
                "name": str(entry["name"])[:80],
                "vendor": "nvidia",
                "mode": "replace_one",
                "expected_gpu_uuid": entry["gpu_uuid"],
                "previous_index": int(entry["setting"]["index"]),
                "settings": [remapped_setting],
            }
            queued.append(
                queue_agent_command(
                    db,
                    agent_id=agent_id,
                    action="apply_gpu_profile",
                    payload=payload,
                    user=user,
                    gpu_oc_intent=_gpu_oc_intent_marker(entry),
                )
            )
        Database.audit(
            db,
            actor="controller",
            action="desired_gpu_oc_reapply_queued",
            target_type="agent",
            target_id=agent_id,
            details={
                "source_command_id": source_command["id"],
                "source_action": source_command["action"],
                "command_ids": [item["id"] for item in queued],
                "gpu_indices": [item["payload"]["settings"][0]["index"] for item in queued],
            },
        )
        return queued

    def update_oc_mining_observation(agent: sqlite3.Row) -> dict[str, Any]:
        inventory = _json(agent["inventory_json"], {})
        mining = inventory.get("mining_status") if isinstance(inventory, dict) else None
        if not isinstance(mining, dict):
            metrics = _json(agent["metrics_json"], {})
            mining = metrics.get("mining_status") if isinstance(metrics, dict) else None
        if not isinstance(mining, dict):
            return {}
        return {**mining, "checked_at": mining.get("checked_at") if isinstance(mining.get("checked_at"), str) else None}

    def update_oc_desired_fingerprint(agent: sqlite3.Row) -> str:
        # PCI enumeration can change during activation; intent is UUID-bound.
        desired = [
            {"gpu_uuid": entry["gpu_uuid"], "name": entry["name"], "setting": {k: v for k, v in entry["setting"].items() if k != "index"}}
            for entry in _desired_gpu_oc_entries(agent["desired_gpu_oc_json"])
        ]
        desired.sort(key=lambda entry: entry["gpu_uuid"])
        return hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()

    def capture_update_oc_recovery(db: sqlite3.Connection, agent: sqlite3.Row) -> str | None:
        """Bind only a new update of an observed live, configured workload."""
        mining = update_oc_mining_observation(agent)
        sheet_id = _active_agent_flight_sheet_id(agent, settings)
        sheet = db.execute("SELECT f.*, w.address AS wallet_address, sw.address AS secondary_wallet_address FROM flight_sheets f JOIN wallets w ON w.id=f.wallet_id LEFT JOIN wallets sw ON sw.id=f.secondary_wallet_id WHERE f.id=?", (sheet_id,)).fetchone()
        if (
            sheet is None or mining.get("configured") is not True
            or str(mining.get("state") or "").casefold() not in {"running", "active"}
            or mining.get("process_verified") is not True
            or mining.get("miner_type") != sheet["miner_type"]
            or not _mining_observation_is_current(mining, observed_at=_parse_time(agent["last_seen"]), boot_time=None, require_statistics=False)
            or not _desired_gpu_oc_entries(agent["desired_gpu_oc_json"])
        ):
            return None
        return json.dumps({
            "state": "armed", "flight_sheet_id": sheet_id,
            "miner_type": mining["miner_type"],
            "flight_fingerprint": _flight_sheet_configuration_fingerprint(sheet),
            "desired_fingerprint": update_oc_desired_fingerprint(agent),
            "audit_after": db.execute("SELECT COALESCE(MAX(id),0) FROM audit_logs").fetchone()[0],
            "command_ids": [],
        }, separators=(",", ":"))

    def reconcile_update_oc_recovery(db: sqlite3.Connection, *, agent: sqlite3.Row) -> bool:
        """Consume a bounded post-activation event, never infer one from old jobs.

        A final activation result is not a miner-resume receipt. Require a newer
        Agent sample than the one seen at final acceptance, not just a new HTTP
        request (and never order Agent sampling time against Controller time).
        Keep ownership through UUID acknowledgements so busy queues, restarts
        and missing inventory cannot lose the event or bypass its intent guard.
        """
        jobs = db.execute(
            """SELECT j.*, c.rowid AS source_rowid, c.status AS command_status
               FROM agent_update_jobs j JOIN commands c ON c.id=j.command_id
               WHERE j.agent_id=? AND j.oc_recovery_json IS NOT NULL
                 AND json_extract(j.oc_recovery_json,'$.state') IN ('armed','waiting','pending')
               ORDER BY c.rowid""", (agent["id"],),
        ).fetchall()
        handled = False
        for job in jobs:
            handled = True
            context = _json(job["oc_recovery_json"], {})
            owned_ids = set(context["command_ids"])
            desired = _desired_gpu_oc_entries(agent["desired_gpu_oc_json"])
            blocked_uuids = {entry["gpu_uuid"] for entry in desired if _gpu_oc_replay_is_blocked(entry)}
            pending = set(_desired_gpu_oc_pending_uuids(agent["desired_gpu_oc_pending_json"])) - blocked_uuids
            owned_uuids = set(context.get("gpu_uuids", []))
            sheet = db.execute("SELECT f.*, w.address AS wallet_address, sw.address AS secondary_wallet_address FROM flight_sheets f JOIN wallets w ON w.id=f.wallet_id LEFT JOIN wallets sw ON sw.id=f.secondary_wallet_id WHERE f.id=?", (context["flight_sheet_id"],)).fetchone()
            later_commands = db.execute(
                """SELECT id, action FROM commands WHERE agent_id=? AND rowid>?
                   AND action IN ('stop_mining','remove_flight_sheet','apply_flight_sheet',
                     'restart_mining','reboot','shutdown','restart_service','agent_update.v1',
                     'apply_gpu_profile','migrate_gpu_oc_state.v1',
                     'configure_linux_ssh.v1')""",
                (agent["id"], job["source_rowid"]),
            ).fetchall()
            # Preserve operator intent even if completed command history was cleared.
            later_commands.extend(db.execute(
                """SELECT json_extract(details_json,'$.command_id') AS id,
                          json_extract(details_json,'$.action') AS action
                   FROM audit_logs WHERE id>? AND target_type='agent' AND target_id=?
                     AND action='command_queued' AND json_extract(details_json,'$.action') IN
                     ('stop_mining','remove_flight_sheet','apply_flight_sheet','restart_mining',
                      'reboot','shutdown','restart_service','agent_update.v1','apply_gpu_profile',
                      'migrate_gpu_oc_state.v1',
                      'configure_linux_ssh.v1')""",
                (context["audit_after"], agent["id"]),
            ).fetchall())
            later_intent = any(
                row["id"] not in owned_ids and row["id"] != job["post_update_command_id"]
                for row in later_commands
            )
            edited = db.execute(
                """SELECT 1 FROM audit_logs WHERE id>? AND (
                   (target_type='flight_sheet' AND target_id=? AND action IN ('flight_sheet_updated','flight_sheet_deleted'))
                   OR (target_type='agent' AND target_id=? AND action IN (
                     'desired_gpu_oc_saved','desired_gpu_oc_batch_saved','desired_gpu_oc_reset_queued',
                     'desired_gpu_oc_batch_reset_queued','desired_gpu_oc_forgotten','mining_autostart_updated'))
                   ) LIMIT 1""", (context["audit_after"], context["flight_sheet_id"], agent["id"]),
            ).fetchone()
            refresh = db.execute(
                "SELECT 1 FROM configuration_refreshes WHERE agent_id=? AND requested_at>? LIMIT 1",
                (agent["id"], job["created_at"]),
            ).fetchone()
            expired = context.get("expires_at") and utcnow() >= context["expires_at"]
            cancel = (
                agent["disabled_at"] or job["command_status"] in {'failed','cancelled','expired'}
                or later_intent or edited or refresh or expired or sheet is None
                or _flight_sheet_configuration_fingerprint(sheet) != context["flight_fingerprint"]
                or update_oc_desired_fingerprint(agent) != context["desired_fingerprint"]
            )
            if cancel:
                context["state"] = "cancelled"
                # Only retire commands and pending UUIDs owned by this event.
                for command_id in owned_ids:
                    db.execute("UPDATE commands SET status='cancelled',finished_at=? WHERE id=? AND status='queued'",
                               (utcnow(), command_id))
                newer_oc_intent = (
                    update_oc_desired_fingerprint(agent) != context["desired_fingerprint"]
                    or any(row["action"] == "apply_gpu_profile" and row["id"] not in owned_ids for row in later_commands)
                    or db.execute(
                        """SELECT 1 FROM audit_logs WHERE id>? AND target_type='agent' AND target_id=?
                           AND action IN ('desired_gpu_oc_saved','desired_gpu_oc_batch_saved',
                             'desired_gpu_oc_reset_queued','desired_gpu_oc_batch_reset_queued',
                             'desired_gpu_oc_forgotten') LIMIT 1""",
                        (context["audit_after"], agent["id"]),
                    ).fetchone()
                )
                if owned_uuids and not newer_oc_intent:
                    db.execute("UPDATE agents SET desired_gpu_oc_pending_json=? WHERE id=?",
                               (json.dumps(sorted(pending - owned_uuids)), agent["id"]))
            elif context["state"] == "armed":
                continue
            elif context["state"] == "pending" and not pending.intersection(owned_uuids):
                context["state"] = "blocked" if blocked_uuids.intersection(owned_uuids) else "complete"
                if context["state"] == "blocked":
                    context["blocked_gpu_uuids"] = sorted(blocked_uuids.intersection(owned_uuids))
            else:
                mining = update_oc_mining_observation(agent)
                checked = _parse_time(mining.get("checked_at"))
                baseline = _parse_time(context.get("checked_at"))
                if checked is None:
                    continue
                if baseline is None:
                    # Missing final sample: establish a baseline, then wait for
                    # a strictly newer observation rather than guessing.
                    context["checked_at"] = mining["checked_at"]
                elif (
                    checked > baseline and agent["agent_version"] == job["target_version"]
                    and _mining_observation_is_current(mining, observed_at=_parse_time(agent["last_seen"]), boot_time=None, require_statistics=False)
                    and mining.get("configured") is True
                    and str(mining.get("state") or "").casefold() in {"running", "active"}
                    and mining.get("process_verified") is True
                    and mining.get("miner_type") == context["miner_type"]
                    and _active_agent_flight_sheet_id(agent, settings) == context["flight_sheet_id"]
                ):
                    targets = owned_uuids if context["state"] == "pending" else {entry["gpu_uuid"] for entry in desired}
                    if context["state"] == "waiting":
                        context["state"] = "pending"
                        context["gpu_uuids"] = sorted(targets)
                        context["blocked_gpu_uuids"] = sorted(targets.intersection(blocked_uuids))
                        pending |= targets - blocked_uuids
                        if not pending.intersection(targets):
                            context["state"] = "blocked" if targets.intersection(blocked_uuids) else "complete"
                        db.execute("UPDATE agents SET desired_gpu_oc_pending_json=? WHERE id=?",
                                   (json.dumps(sorted(pending)), agent["id"]))
                    busy = db.execute(
                        """SELECT 1 FROM commands WHERE agent_id=? AND action NOT IN
                           ('apply_gpu_profile','collect_diagnostics','collect_miner_logs')
                           AND status IN ('queued','dispatched','running') LIMIT 1""", (agent["id"],),
                    ).fetchone()
                    if context["state"] == "pending" and not busy:
                        commands = queue_desired_gpu_oc_reapply(
                            db, agent_id=agent["id"],
                            source_command={"id": job["command_id"], "action": AGENT_UPDATE_CAPABILITY, "created_by": job["created_by"]},
                            only_gpu_uuids=pending.intersection(targets),
                        )
                        context["command_ids"].extend(item["id"] for item in commands)
            db.execute("UPDATE agent_update_jobs SET oc_recovery_json=? WHERE command_id=?",
                       (json.dumps(context, separators=(",", ":")), job["command_id"]))
        return handled

    def reconcile_pending_desired_gpu_oc(
        db: sqlite3.Connection,
        *,
        agent: sqlite3.Row,
    ) -> list[dict[str, Any]]:
        """Retry only UUIDs whose desired OC could not be mapped/applied yet."""

        pending = set(
            _desired_gpu_oc_pending_uuids(
                _optional_row_value(agent, "desired_gpu_oc_pending_json", "[]")
            )
        )
        desired_uuids = {
            entry["gpu_uuid"]
            for entry in _desired_gpu_oc_entries(
                _optional_row_value(agent, "desired_gpu_oc_json", "[]")
            )
            if not _gpu_oc_replay_is_blocked(entry)
        }
        valid_pending = pending.intersection(desired_uuids)
        if valid_pending != pending:
            db.execute(
                "UPDATE agents SET desired_gpu_oc_pending_json=? WHERE id=?",
                (
                    json.dumps(sorted(valid_pending), separators=(",", ":")),
                    agent["id"],
                ),
            )
        pending = valid_pending
        if not pending or _active_agent_flight_sheet_id(agent, settings) is None:
            return []
        transition = db.execute(
            """SELECT 1 FROM commands
               WHERE agent_id=?
                 AND action IN (
                   'apply_flight_sheet','restart_mining','stop_mining',
                   'remove_flight_sheet','reboot','shutdown','agent_update.v1'
                 )
                 AND status IN ('queued','dispatched','running')
                 AND expires_at >= ?
               LIMIT 1""",
            (agent["id"], utcnow()),
        ).fetchone()
        if transition is not None:
            return []
        source = db.execute(
            """SELECT * FROM commands
               WHERE agent_id=?
                 AND action IN (
                   'apply_flight_sheet','restart_mining','apply_gpu_profile'
                 )
                 AND status='succeeded'
               ORDER BY COALESCE(finished_at, created_at) DESC, rowid DESC
               LIMIT 1""",
            (agent["id"],),
        ).fetchone()
        if source is None:
            return []
        if db.execute(
            """SELECT 1 FROM commands WHERE agent_id=?
               AND action IN ('stop_mining','remove_flight_sheet','reboot','shutdown')
               AND rowid>(SELECT rowid FROM commands WHERE id=?) LIMIT 1""",
            (agent["id"], source["id"]),
        ).fetchone():
            return []
        return queue_desired_gpu_oc_reapply(
            db,
            agent_id=agent["id"],
            source_command=source,
            only_gpu_uuids=pending,
        )

    def cancel_queued_gpu_oc_commands(
        db: sqlite3.Connection,
        *,
        agent_id: str,
        user: dict[str, Any],
        reason: str,
        expected_gpu_uuid: str | None = None,
    ) -> list[str]:
        """Cancel not-yet-dispatched OC so an explicit stop remains final."""

        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        rows = db.execute(
            """SELECT id, payload_json FROM commands
               WHERE agent_id=? AND action='apply_gpu_profile' AND status='queued'""",
            (agent_id,),
        ).fetchall()
        cancelled: list[str] = []
        for row in rows:
            if expected_gpu_uuid is not None:
                payload = _json(row["payload_json"], {})
                if (
                    not isinstance(payload, dict)
                    or payload.get("expected_gpu_uuid") != expected_gpu_uuid
                ):
                    continue
            changed = db.execute(
                "UPDATE commands SET status='cancelled', finished_at=? "
                "WHERE id=? AND status='queued'",
                (now, row["id"]),
            )
            if changed.rowcount != 1:
                continue
            cancelled.append(row["id"])
            Database.audit(
                db,
                actor=user["username"],
                action="command_cancelled",
                target_type="command",
                target_id=row["id"],
                details={"reason": reason, "desired_gpu_oc_preserved": True},
            )
        return cancelled

    def serialize_configuration_refresh(
        row: sqlite3.Row | dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "status": row["status"],
            "flight_sheet_id": row["flight_sheet_id"],
            "trigger_type": row["trigger_type"],
            "trigger_id": row["trigger_id"],
            "command_id": row["command_id"],
            "requested_at": row["requested_at"],
            "updated_at": row["updated_at"],
            "queued_at": row["queued_at"],
            "completed_at": row["completed_at"],
            "attempt_count": row["attempt_count"],
            "last_error": row["last_error"],
        }

    def configuration_reapply_result(*, triggered: bool) -> dict[str, Any]:
        return {
            "triggered": triggered,
            "matched": 0,
            "marked": 0,
            "pending": 0,
            "queued": 0,
            "skipped": 0,
            "failed": 0,
            "commands": [],
            "issues": [],
        }

    def _configuration_refresh_error_is_retryable(
        status_code: int,
        detail: str,
    ) -> bool:
        """Keep durable desired state pending for temporary device incompatibility."""

        if status_code in {502, 503, 504}:
            return True
        if status_code != 409:
            return False
        normalized_detail = detail.casefold()
        return detail.startswith("Automatic configuration refresh skipped:") or detail in {
            "Flight sheet changed while command was prepared; retry",
            "Device changed while command was prepared; retry",
        } or any(
            marker in detail
            for marker in (
                "requires RigPilot Agent ",
                "upgrade this device first",
                "does not currently advertise managed mining",
            )
        ) or any(
            marker in normalized_detail
            for marker in (
                "inventory not ready:",
                "inventory_not_ready",
                "did not report any gpu devices",
                "gpu inventory is not ready",
                "gpu inventory is unavailable",
            )
        )

    def _claim_configuration_refresh(
        *,
        agent_id: str,
        fingerprint: str,
        generation_id: str,
        heartbeat_token: str,
    ) -> str | None:
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(minutes=15)).isoformat(timespec="seconds")
        now_text = now.isoformat(timespec="seconds")
        claim_token = uuid.uuid4().hex
        with database.connection() as db:
            cursor = db.execute(
                """UPDATE configuration_refreshes
                   SET status='preparing', claim_token=?, updated_at=?,
                       last_error=NULL
                   WHERE agent_id=? AND desired_fingerprint=? AND generation_id=?
                      AND EXISTS (
                        SELECT 1 FROM agents a
                        WHERE a.id=configuration_refreshes.agent_id
                         AND a.last_seen=?
                     )
                     AND (
                       status='pending'
                       OR (status='preparing' AND updated_at < ?)
                     )""",
                (
                    claim_token,
                    now_text,
                    agent_id,
                    fingerprint,
                    generation_id,
                    heartbeat_token,
                    stale_before,
                ),
            )
        return claim_token if cursor.rowcount else None

    def _mark_configuration_refresh_failure(
        *,
        agent_id: str,
        fingerprint: str,
        generation_id: str,
        claim_token: str,
        reason: str,
        failed: bool,
    ) -> None:
        now = utcnow()
        with database.connection() as db:
            db.execute(
                """UPDATE configuration_refreshes
                   SET status=?, claim_token=NULL, updated_at=?,
                       completed_at=?, last_error=?
                   WHERE agent_id=? AND desired_fingerprint=?
                      AND generation_id=? AND claim_token=?
                      AND status='preparing'""",
                (
                    "failed" if failed else "pending",
                    now,
                    now if failed else None,
                    reason[:1000],
                    agent_id,
                    fingerprint,
                    generation_id,
                    claim_token,
                ),
            )

    def _bind_configuration_refresh_command(
        *,
        agent_id: str,
        sheet_id: str,
        fingerprint: str,
        generation_id: str,
        claim_token: str,
        command_id: str,
        desired_apply: bool = False,
    ) -> bool:
        now = utcnow()
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A concurrent heartbeat legitimately changes ``last_seen`` while
            # the same miner remains active.  Bind against current mining
            # truth, not that volatile timestamp; generation + claim tokens
            # still fence competing configuration edits.
            latest_agent = db.execute(
                "SELECT * FROM agents WHERE id=?",
                (agent_id,),
            ).fetchone()
            still_active = latest_agent is not None and (
                desired_apply
                or _active_agent_flight_sheet_id(latest_agent, settings)
                == sheet_id
            )
            refresh_binding = db.execute(
                """SELECT trigger_type, trigger_id
                   FROM configuration_refreshes
                   WHERE agent_id=? AND flight_sheet_id=?
                     AND desired_fingerprint=? AND generation_id=?
                     AND claim_token=? AND status='preparing'""",
                (
                    agent_id,
                    sheet_id,
                    fingerprint,
                    generation_id,
                    claim_token,
                ),
            ).fetchone()
            command = db.execute(
                """SELECT status, created_at, finished_at, error FROM commands
                   WHERE id=? AND agent_id=? AND action='apply_flight_sheet'""",
                (command_id, agent_id),
            ).fetchone()
            # The Agent or an operator may advance the command after queueing
            # but before this binding. Preserve that outcome instead of
            # reviving a cancelled command or regressing progress to queued.
            command_status = command["status"] if command is not None else None
            retryable_failure = command_status == "failed" and (
                _configuration_refresh_error_is_retryable(409, command["error"] or "")
            )
            binding_status = "pending" if retryable_failure else command_status
            cursor = (
                db.execute(
                    """UPDATE configuration_refreshes
                       SET status=?, command_id=?, queued_at=?, updated_at=?,
                           completed_at=?, attempt_count=attempt_count+1,
                           claim_token=NULL, last_error=?
                       WHERE agent_id=? AND flight_sheet_id=?
                          AND desired_fingerprint=? AND generation_id=?
                          AND claim_token=? AND status='preparing'""",
                    (
                        binding_status,
                        None if retryable_failure else command_id,
                        None if retryable_failure else command["created_at"],
                        now,
                        command["finished_at"] if binding_status in TERMINAL_COMMAND_STATUSES else None,
                        command["error"],
                        agent_id,
                        sheet_id,
                        fingerprint,
                        generation_id,
                        claim_token,
                    ),
                )
                if command is not None and (still_active or command_status != "queued")
                else None
            )
            if cursor is not None and cursor.rowcount:
                trigger_id = (
                    str(refresh_binding["trigger_id"])
                    if refresh_binding is not None
                    else ""
                )
                replacement_prefix = "desired:replacement:"
                if (
                    refresh_binding is not None
                    and refresh_binding["trigger_type"] == "flight_sheet"
                    and trigger_id.startswith(replacement_prefix)
                ):
                    replacement_id = trigger_id[len(replacement_prefix) :]
                    db.execute(
                        """UPDATE agent_replacement_tokens
                           SET resumed_command_id=?
                           WHERE id=? AND target_agent_id=?
                             AND resume_flight_sheet_id=?
                             AND activated_at IS NOT NULL
                             AND resumed_command_id IS NULL""",
                        (
                            command_id,
                            replacement_id,
                            agent_id,
                            sheet_id,
                        ),
                    )
                return command_status != "cancelled"
            cancelled = db.execute(
                """UPDATE commands SET status='cancelled', finished_at=?
                   WHERE id=? AND status='queued'""",
                (now, command_id),
            )
            db.execute(
                """UPDATE configuration_refreshes
                   SET status='pending', command_id=NULL, updated_at=?,
                       queued_at=NULL, completed_at=NULL,
                       claim_token=NULL,
                       last_error='device mining state changed while preparing configuration'
                   WHERE agent_id=? AND flight_sheet_id=?
                      AND desired_fingerprint=? AND generation_id=?
                      AND claim_token=? AND status='preparing'""",
                (
                    now,
                    agent_id,
                    sheet_id,
                    fingerprint,
                    generation_id,
                    claim_token,
                ),
            )
            if cancelled.rowcount:
                Database.audit(
                    db,
                    actor="controller",
                    action="command_cancelled",
                    target_type="command",
                    target_id=command_id,
                    details={"reason": "configuration_refresh_superseded_before_binding"},
                )
        return False

    def _persist_configuration_refreshes(
        db: sqlite3.Connection,
        selected: set[str],
        user: dict[str, Any],
        *,
        trigger_type: str,
        trigger_id: str,
        target_agent_ids: set[str] | None = None,
    ) -> int:
        """Write the durable outbox in the caller's entity-edit transaction."""

        if not selected:
            return 0
        placeholders = ",".join("?" for _ in selected)
        requested_at = utcnow()
        generation_id = uuid.uuid4().hex
        sheets = db.execute(
            f"""SELECT fs.*, w.address AS wallet_address,
                       sw.address AS secondary_wallet_address
                FROM flight_sheets fs
                JOIN wallets w ON fs.wallet_id=w.id
                LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                WHERE fs.id IN ({placeholders})""",
            tuple(sorted(selected)),
        ).fetchall()
        fingerprints = {
            row["id"]: _flight_sheet_configuration_fingerprint(row) for row in sheets
        }
        # A queued manual apply is durable desired state even though an offline
        # Agent has not reported the sheet yet.  Preserve that intent while
        # replacing its stale embedded wallet/pool/config snapshot.
        desired_apply_by_agent: dict[str, str] = {}
        apply_rows = db.execute(
            """SELECT agent_id, status, payload_json FROM commands
               WHERE action='apply_flight_sheet'
                 AND status IN ('queued','dispatched','running')
               ORDER BY created_at DESC"""
        ).fetchall()
        for apply_row in apply_rows:
            payload = _json(apply_row["payload_json"], {})
            intended_sheet_id = (
                payload.get("flight_sheet_id") if isinstance(payload, dict) else None
            )
            if (
                isinstance(intended_sheet_id, str)
                and intended_sheet_id in selected
                and apply_row["agent_id"] not in desired_apply_by_agent
            ):
                desired_apply_by_agent[apply_row["agent_id"]] = intended_sheet_id
        marked = 0
        agents = db.execute(
            "SELECT * FROM agents WHERE disabled_at IS NULL ORDER BY name COLLATE NOCASE"
        ).fetchall()
        for agent_row in agents:
            if (
                target_agent_ids is not None
                and agent_row["id"] not in target_agent_ids
            ):
                continue
            reported_sheet_id = _reported_agent_flight_sheet_id(agent_row)
            queued_desired_sheet_id = desired_apply_by_agent.get(agent_row["id"])
            desired_apply = (
                queued_desired_sheet_id
                if queued_desired_sheet_id != reported_sheet_id
                else None
            )
            sheet_id = desired_apply or reported_sheet_id
            fingerprint = fingerprints.get(sheet_id or "")
            if not sheet_id or not fingerprint:
                continue
            existing = db.execute(
                "SELECT command_id FROM configuration_refreshes WHERE agent_id=?",
                (agent_row["id"],),
            ).fetchone()
            if existing and existing["command_id"]:
                db.execute(
                    """UPDATE commands SET status='cancelled', finished_at=?
                       WHERE id=? AND status='queued'""",
                    (requested_at, existing["command_id"]),
                )
            if desired_apply:
                queued_applies = db.execute(
                    """SELECT id, payload_json FROM commands
                       WHERE agent_id=? AND action='apply_flight_sheet'
                         AND status='queued'""",
                    (agent_row["id"],),
                ).fetchall()
                for queued_apply in queued_applies:
                    queued_payload = _json(queued_apply["payload_json"], {})
                    if (
                        isinstance(queued_payload, dict)
                        and queued_payload.get("flight_sheet_id") == sheet_id
                    ):
                        db.execute(
                            """UPDATE commands SET status='cancelled', finished_at=?
                               WHERE id=? AND status='queued'""",
                            (requested_at, queued_apply["id"]),
                        )
            db.execute(
                """INSERT INTO configuration_refreshes
                   (agent_id,flight_sheet_id,desired_fingerprint,generation_id,
                    claim_token,trigger_type,
                    trigger_id,status,command_id,requested_by,requested_at,
                    updated_at,queued_at,completed_at,attempt_count,last_error)
                   VALUES(?,?,?,?,NULL,?,?,'pending',NULL,?,?,?,NULL,NULL,0,NULL)
                   ON CONFLICT(agent_id) DO UPDATE SET
                      flight_sheet_id=excluded.flight_sheet_id,
                      desired_fingerprint=excluded.desired_fingerprint,
                      generation_id=excluded.generation_id,
                      claim_token=NULL,
                      trigger_type=excluded.trigger_type,
                     trigger_id=excluded.trigger_id,
                     status='pending', command_id=NULL,
                     requested_by=excluded.requested_by,
                     requested_at=excluded.requested_at,
                     updated_at=excluded.updated_at,
                     queued_at=NULL, completed_at=NULL,
                     attempt_count=0, last_error=NULL""",
                (
                    agent_row["id"],
                    sheet_id,
                    fingerprint,
                    generation_id,
                    trigger_type,
                    (
                        f"desired:{trigger_id}" if desired_apply else trigger_id
                    ),
                    user["id"], requested_at, requested_at,
                ),
            )
            marked += 1
        return marked

    def reapply_active_flight_sheets(
        sheet_ids: list[str] | set[str],
        user: dict[str, Any],
        *,
        trigger_type: str,
        trigger_id: str,
        persist: bool = True,
        target_agent_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Persist the newest desired config, then queue confirmed live miners."""

        selected = {sheet_id for sheet_id in sheet_ids if sheet_id}
        result = configuration_reapply_result(triggered=True)
        if not selected or target_agent_ids == set():
            return result
        placeholders = ",".join("?" for _ in selected)
        if persist:
            with database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                _persist_configuration_refreshes(
                    db,
                    selected,
                    user,
                    trigger_type=trigger_type,
                    trigger_id=trigger_id,
                    target_agent_ids=target_agent_ids,
                )
        with database.connection() as db:
            sheets = db.execute(
                f"""SELECT fs.*, w.address AS wallet_address,
                           sw.address AS secondary_wallet_address
                    FROM flight_sheets fs
                    JOIN wallets w ON fs.wallet_id=w.id
                    LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                    WHERE fs.id IN ({placeholders})""",
                tuple(sorted(selected)),
            ).fetchall()
            fingerprints = {
                row["id"]: _flight_sheet_configuration_fingerprint(row) for row in sheets
            }
            agents = db.execute(
                "SELECT * FROM agents WHERE disabled_at IS NULL ORDER BY name COLLATE NOCASE"
            ).fetchall()
            refreshes = {
                row["agent_id"]: row
                for row in db.execute(
                    f"""SELECT * FROM configuration_refreshes
                        WHERE flight_sheet_id IN ({placeholders})
                          AND trigger_type=? AND trigger_id=?""",
                    (*tuple(sorted(selected)), trigger_type, trigger_id),
                ).fetchall()
            }
        marked = []
        for agent_row in agents:
            if (
                target_agent_ids is not None
                and agent_row["id"] not in target_agent_ids
            ):
                continue
            sheet_id = _reported_agent_flight_sheet_id(agent_row)
            fingerprint = fingerprints.get(sheet_id or "")
            refresh = refreshes.get(agent_row["id"])
            if (
                not sheet_id
                or not fingerprint
                or refresh is None
                or refresh["desired_fingerprint"] != fingerprint
            ):
                continue
            marked.append(
                {
                    "agent": agent_row,
                    "agent_id": agent_row["id"],
                    "agent_name": agent_row["name"],
                    "sheet_id": sheet_id,
                    "fingerprint": fingerprint,
                    "generation_id": refresh["generation_id"],
                }
            )
        result["marked"] = len(marked)
        targets = [
            item
            for item in marked
            if _active_agent_flight_sheet_id(item["agent"], settings)
            == item["sheet_id"]
        ]
        result["matched"] = len(targets)
        for target in targets:
            heartbeat_token = target["agent"]["last_seen"]
            if not isinstance(heartbeat_token, str) or not heartbeat_token:
                continue
            claim_token = _claim_configuration_refresh(
                agent_id=target["agent_id"],
                fingerprint=target["fingerprint"],
                generation_id=target["generation_id"],
                heartbeat_token=heartbeat_token,
            )
            if claim_token is None:
                continue
            automatic_user = {
                **user,
                "_automatic_reapply": {
                    "flight_sheet_id": target["sheet_id"],
                    "trigger_type": trigger_type,
                    "trigger_id": trigger_id,
                    "generation_id": target["generation_id"],
                    "claim_token": claim_token,
                },
            }
            command = None
            for attempt in range(2):
                try:
                    command = apply_flight_sheet(
                        target["agent_id"], target["sheet_id"], automatic_user
                    )
                    break
                except HTTPException as exc:
                    detail = str(exc.detail)[:1000]
                    if attempt == 0 and detail in {
                        "Flight sheet changed while command was prepared; retry",
                        "Device changed while command was prepared; retry",
                    }:
                        continue
                    retryable_transport = _configuration_refresh_error_is_retryable(
                        exc.status_code, detail
                    )
                    outcome = (
                        "skipped"
                        if exc.status_code == 409
                        and detail.startswith("Automatic configuration refresh skipped:")
                        else ("pending" if retryable_transport else "failed")
                    )
                    if outcome in {"skipped", "failed"}:
                        result[outcome] += 1
                    result["issues"].append(
                        {
                            "agent_id": target["agent_id"],
                            "sheet_id": target["sheet_id"],
                            "reason": detail,
                        }
                    )
                    _mark_configuration_refresh_failure(
                        agent_id=target["agent_id"],
                        fingerprint=target["fingerprint"],
                        generation_id=target["generation_id"],
                        claim_token=claim_token,
                        reason=detail,
                        failed=outcome == "failed",
                    )
                    break
                except Exception:
                    LOGGER.exception(
                        "Could not queue automatic flight-sheet refresh for %s",
                        target["agent_id"],
                    )
                    result["failed"] += 1
                    result["issues"].append(
                        {
                            "agent_id": target["agent_id"],
                            "sheet_id": target["sheet_id"],
                            "reason": "internal queue error",
                        }
                    )
                    _mark_configuration_refresh_failure(
                        agent_id=target["agent_id"],
                        fingerprint=target["fingerprint"],
                        generation_id=target["generation_id"],
                        claim_token=claim_token,
                        reason="internal queue error",
                        failed=True,
                    )
                    break
            if command is not None and _bind_configuration_refresh_command(
                agent_id=target["agent_id"],
                sheet_id=target["sheet_id"],
                fingerprint=target["fingerprint"],
                generation_id=target["generation_id"],
                claim_token=claim_token,
                command_id=command["id"],
            ):
                result["queued"] += 1
                result["commands"].append(
                    {
                        "agent_id": target["agent_id"],
                        "sheet_id": target["sheet_id"],
                        "command_id": command["id"],
                    }
                )
        with database.connection() as db:
            pending_sql = (
                f"""SELECT COUNT(*) AS count FROM configuration_refreshes
                    WHERE flight_sheet_id IN ({placeholders})
                      AND status IN ('pending','preparing')"""
            )
            pending_params: list[Any] = list(sorted(selected))
            if target_agent_ids is not None:
                target_placeholders = ",".join("?" for _ in target_agent_ids)
                pending_sql += f" AND agent_id IN ({target_placeholders})"
                pending_params.extend(sorted(target_agent_ids))
            result["pending"] = db.execute(
                pending_sql,
                tuple(pending_params),
            ).fetchone()["count"]
            Database.audit(
                db,
                actor=user["username"],
                action="configuration_reapply",
                target_type=trigger_type,
                target_id=trigger_id,
                details={
                    "flight_sheet_ids": sorted(selected),
                    "marked": result["marked"],
                    "matched": result["matched"],
                    "pending": result["pending"],
                    "queued": result["queued"],
                    "skipped": result["skipped"],
                    "failed": result["failed"],
                },
            )
        return result

    def reconcile_pending_configuration_refresh(
        agent_id: str,
        *,
        heartbeat_token: str,
        previous_agent_online: bool,
        previous_active_sheet_id: str | None,
        observed_active_sheet_id: str | None,
        observed_reported_sheet_id: str | None,
        observed_explicitly_unconfigured: bool,
    ) -> None:
        """Queue one persisted refresh once the device proves this miner is live."""

        with database.connection() as db:
            row = db.execute(
                """SELECT r.*, u.username AS requested_by_name,
                          a.id AS id, a.secret_hash, a.name, a.hostname, a.os_family,
                          a.platform, a.agent_version, a.note, a.tags_json,
                          a.inventory_json, a.metrics_json, a.capabilities_json,
                          a.last_ip, a.last_seen, a.enrolled_at, a.disabled_at
                   FROM configuration_refreshes r
                   JOIN users u ON r.requested_by=u.id
                   JOIN agents a ON r.agent_id=a.id
                   WHERE r.agent_id=? AND r.status IN ('pending','preparing')""",
                (agent_id,),
            ).fetchone()
        if (
            row is None
            or row["disabled_at"]
            or row["last_seen"] != heartbeat_token
        ):
            return
        if _serialize_agent(row, settings)["status"] != "online":
            return
        sheet_id = row["flight_sheet_id"]
        desired_apply = str(row["trigger_id"] or "").startswith("desired:")
        # The accepted heartbeat has already committed.  Keep the explicit
        # observations as a second binding boundary so a slower request can
        # neither restart a stopped miner nor queue an obsolete sheet.
        if desired_apply and not previous_agent_online:
            return
        if not desired_apply:
            # Require two accepted heartbeats that both prove this miner is
            # active.  The first one may race with a stop/restart transition;
            # queueing only after the previous committed state agrees avoids a
            # create-then-cancel command that the Agent never had a chance to
            # observe.
            if previous_active_sheet_id != sheet_id:
                return
            if observed_explicitly_unconfigured:
                now = utcnow()
                with database.connection() as db:
                    db.execute(
                        """UPDATE configuration_refreshes
                           SET status='cancelled', updated_at=?, completed_at=?,
                               last_error='device no longer uses this flight sheet'
                           WHERE agent_id=? AND desired_fingerprint=? AND generation_id=?
                             AND EXISTS (SELECT 1 FROM agents a WHERE a.id=configuration_refreshes.agent_id AND a.last_seen=?)
                             AND status IN ('pending','preparing')""",
                        (
                            now,
                            now,
                            agent_id,
                            row["desired_fingerprint"], row["generation_id"], heartbeat_token,
                        ),
                    )
                return
            if observed_reported_sheet_id != sheet_id:
                if observed_reported_sheet_id is not None:
                    now = utcnow()
                    with database.connection() as db:
                        db.execute(
                            """UPDATE configuration_refreshes
                               SET status='cancelled', updated_at=?, completed_at=?,
                                   last_error='device no longer uses this flight sheet'
                               WHERE agent_id=? AND desired_fingerprint=? AND generation_id=?
                             AND EXISTS (SELECT 1 FROM agents a WHERE a.id=configuration_refreshes.agent_id AND a.last_seen=?)
                                 AND status IN ('pending','preparing')""",
                            (
                                now,
                                now,
                                agent_id,
                                row["desired_fingerprint"], row["generation_id"], heartbeat_token,
                            ),
                        )
                return
            if observed_active_sheet_id != sheet_id:
                return
        reported_sheet_id = _reported_agent_flight_sheet_id(row)
        if not desired_apply and reported_sheet_id != sheet_id:
            if reported_sheet_id is not None or _agent_explicitly_reports_no_flight_sheet(row):
                now = utcnow()
                with database.connection() as db:
                    db.execute(
                        """UPDATE configuration_refreshes
                           SET status='cancelled', updated_at=?, completed_at=?,
                               last_error='device no longer uses this flight sheet'
                           WHERE agent_id=? AND desired_fingerprint=? AND generation_id=?
                             AND EXISTS (SELECT 1 FROM agents a WHERE a.id=configuration_refreshes.agent_id AND a.last_seen=?)
                             AND status IN ('pending','preparing')""",
                        (now, now, agent_id, row["desired_fingerprint"], row["generation_id"], heartbeat_token),
                    )
            return
        if not desired_apply and _active_agent_flight_sheet_id(row, settings) != sheet_id:
            return
        claim_token = _claim_configuration_refresh(
            agent_id=agent_id,
            fingerprint=row["desired_fingerprint"],
            generation_id=row["generation_id"],
            heartbeat_token=heartbeat_token,
        )
        if claim_token is None:
            return
        automatic_user = {
            "id": row["requested_by"],
            "username": row["requested_by_name"],
            "role": "operator",
            "_automatic_reapply": {
                "flight_sheet_id": sheet_id,
                "trigger_type": row["trigger_type"],
                "trigger_id": row["trigger_id"],
                "desired_apply": desired_apply,
                "generation_id": row["generation_id"],
                "claim_token": claim_token,
            },
        }
        try:
            command = apply_flight_sheet(agent_id, sheet_id, automatic_user)
        except HTTPException as exc:
            detail = str(exc.detail)[:1000]
            retryable = _configuration_refresh_error_is_retryable(
                exc.status_code, detail
            )
            _mark_configuration_refresh_failure(
                agent_id=agent_id,
                fingerprint=row["desired_fingerprint"],
                generation_id=row["generation_id"],
                claim_token=claim_token,
                reason=detail,
                failed=not retryable,
            )
            return
        except Exception:
            LOGGER.exception("Could not reconcile configuration refresh for %s", agent_id)
            _mark_configuration_refresh_failure(
                agent_id=agent_id,
                fingerprint=row["desired_fingerprint"],
                generation_id=row["generation_id"],
                claim_token=claim_token,
                reason="internal queue error",
                failed=True,
            )
            return
        _bind_configuration_refresh_command(
            agent_id=agent_id,
            sheet_id=sheet_id,
            fingerprint=row["desired_fingerprint"],
            generation_id=row["generation_id"],
            claim_token=claim_token,
            command_id=command["id"],
            desired_apply=desired_apply,
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "time": utcnow(),
                "enrollment_protocol": "rigpilot.enrollment.v2"}

    @app.post("/api/auth/login")
    def login(body: LoginRequest, request: Request) -> dict[str, Any]:
        ip = request.client.host if request.client else "unknown"
        username = body.username.strip()
        rate_limiter.check(ip, username)
        invalid = False
        token = ""
        try:
            with database.connection() as db:
                row = db.execute(
                    "SELECT * FROM users WHERE username = ?", (username,)
                ).fetchone()
                candidate_password_hash = (
                    row["password_hash"] if row else dummy_password_hash
                )
                password_valid = verify_password(body.password, candidate_password_hash)
                invalid = bool(
                    not row
                    or not row["is_active"]
                    or not password_valid
                )
                if invalid:
                    Database.audit(
                        db,
                        actor=body.username[:64],
                        action="login_failed",
                        target_type="session",
                        details={"ip": ip},
                    )
                else:
                    Database.audit(
                        db,
                        actor=row["username"],
                        action="login_succeeded",
                        target_type="session",
                        details={"ip": ip},
                    )
                    token = create_session_token(
                        {
                            "uid": row["id"],
                            "sub": row["username"],
                            "role": row["role"],
                            "sv": row["session_version"],
                        },
                        settings.secret_key,
                        settings.session_hours * 3600,
                    )
        except Exception:
            rate_limiter.abandon(ip, username)
            raise
        if invalid:
            rate_limiter.record_failure(ip, username)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
        rate_limiter.record_success(ip, username)
        return {
            "token": token,
            "expires_in": settings.session_hours * 3600,
            "user": {"username": row["username"], "role": row["role"]},
        }

    @app.get("/api/auth/me")
    def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"username": user["username"], "role": user["role"]}

    def installation_setup_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            # Existing installations never had this key. Do not turn an
            # upgrade or a read into a first-install prompt.
            return {"status": "complete", "step": "done", "password_changed": False,
                    "revision": 0, "updated_at": None}
        value = _json(row["value"], None)
        if (not isinstance(value, dict)
                or set(value) != {"status", "step", "password_changed"}
                or value["status"] not in {"pending", "deferred", "complete"}
                or value["step"] not in {"password", "device", "configuration", "done"}
                or type(value["password_changed"]) is not bool):
            raise HTTPException(500, "保存的安装向导进度格式异常")
        return {**value, "revision": int(row["revision"]), "updated_at": row["updated_at"]}

    def save_installation_setup(
        db: sqlite3.Connection, row: sqlite3.Row | None,
        setup: dict[str, Any], user_id: int,
    ) -> dict[str, Any]:
        current_revision = int(row["revision"]) if row is not None else 0
        if current_revision >= 2**63 - 1:
            raise HTTPException(409, "安装向导进度已改变，请重新载入")
        revision = current_revision + 1
        updated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        value = json.dumps({key: setup[key] for key in ("status", "step", "password_changed")},
                           separators=(",", ":"))
        if row is None:
            db.execute(
                """INSERT INTO controller_preferences
                   (key,value,updated_by,updated_at,revision) VALUES(?,?,?,?,?)""",
                (INSTALLATION_SETUP_KEY, value, user_id, updated_at, revision),
            )
        else:
            changed = db.execute(
                """UPDATE controller_preferences SET value=?,updated_by=?,updated_at=?,revision=?
                   WHERE key=? AND revision=?""",
                (value, user_id, updated_at, revision, INSTALLATION_SETUP_KEY, current_revision),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "安装向导进度已改变，请重新载入")
        return {**setup, "revision": revision, "updated_at": updated_at}

    @app.get("/api/setup")
    def get_installation_setup(
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        del user
        with database.connection() as db:
            row = db.execute("SELECT * FROM controller_preferences WHERE key=?",
                             (INSTALLATION_SETUP_KEY,)).fetchone()
        return installation_setup_record(row)

    @app.patch("/api/setup")
    def update_installation_setup(
        body: InstallationSetupUpdate,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM controller_preferences WHERE key=?",
                             (INSTALLATION_SETUP_KEY,)).fetchone()
            setup = installation_setup_record(row)
            if setup["revision"] != body.expected_revision:
                raise HTTPException(409, "安装向导进度已改变，请重新载入")
            if body.action == "step":
                setup.update(status="pending", step=body.step)
            elif body.action == "later":
                setup["status"] = "deferred"
            elif body.action == "skip":
                steps = ("password", "device", "configuration", "done")
                setup["step"] = steps[min(steps.index(setup["step"]) + 1, len(steps) - 1)]
                setup["status"] = "complete" if setup["step"] == "done" else "pending"
            elif body.action == "complete":
                setup.update(status="complete", step="done")
            elif body.action == "reopen":
                setup["status"] = "pending"
                if setup["step"] == "done":
                    setup["step"] = "device" if setup["password_changed"] else "password"
            result = save_installation_setup(db, row, setup, user["id"])
            Database.audit(
                db, actor=user["username"], action="installation_setup_updated",
                target_type="controller", target_id=INSTALLATION_SETUP_KEY,
                details={"action": body.action, "step": result["step"], "status": result["status"]},
            )
        return result

    @app.post("/api/auth/password")
    def change_password(
        body: PasswordChangeRequest,
        request: Request,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, bool]:
        ip = request.client.host if request.client else "unknown"
        password_change_rate_limiter.check(ip, user["username"])
        invalid_password = False
        try:
            with database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT * FROM users WHERE id=?", (user["id"],),
                ).fetchone()
                if (current is None or not current["is_active"]
                        or current["session_version"] != user["session_version"]):
                    raise HTTPException(401, "Invalid or expired session")
                if current["role"] != "admin":
                    raise HTTPException(403, "Insufficient role")
                invalid_password = not verify_password(
                    body.current_password.get_secret_value(), current["password_hash"],
                )
                if invalid_password:
                    Database.audit(
                        db, actor=current["username"], action="user_password_change_failed",
                        target_type="user", target_id=str(current["id"]), details={"ip": ip},
                    )
                else:
                    db.execute(
                        """UPDATE users SET password_hash=?, session_version=session_version+1
                           WHERE id=?""",
                        (hash_password(body.new_password.get_secret_value()), current["id"]),
                    )
                    setup_row = db.execute(
                        "SELECT * FROM controller_preferences WHERE key=?", (INSTALLATION_SETUP_KEY,),
                    ).fetchone()
                    if setup_row is not None:
                        setup = installation_setup_record(setup_row)
                        setup["password_changed"] = True
                        if setup["step"] == "password":
                            setup["step"] = "device"
                        save_installation_setup(db, setup_row, setup, current["id"])
                    Database.audit(
                        db, actor=current["username"], action="user_password_changed",
                        target_type="user", target_id=str(current["id"]), details={"ip": ip},
                    )
        except Exception:
            password_change_rate_limiter.abandon(ip, user["username"])
            raise
        if invalid_password:
            password_change_rate_limiter.record_failure(ip, user["username"])
            raise HTTPException(400, "Current password is incorrect")
        password_change_rate_limiter.record_success(ip, user["username"])
        return {"changed": True, "reauthenticate": True}

    @app.get("/api/summary")
    def summary(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        del user
        now = utcnow()
        with database.connection() as db:
            rows = db.execute("SELECT * FROM agents ORDER BY name").fetchall()
            queued = db.execute(
                """SELECT COUNT(*) AS count FROM commands
                   WHERE status IN ('queued','dispatched','running')
                     AND is_transient=0
                     AND (
                         expires_at >= ?
                         OR EXISTS (
                             SELECT 1 FROM agent_update_jobs j
                             WHERE j.command_id=commands.id
                               AND j.status='activating'
                         )
                     )""",
                (now,),
            ).fetchone()["count"]
        agents = [_serialize_agent(row, settings) for row in rows]
        return {
            "total": len(agents),
            "online": sum(agent["status"] == "online" for agent in agents),
            "offline": sum(agent["status"] == "offline" for agent in agents),
            "normal_mining": sum(
                _serialized_agent_has_current_hashrate(agent) for agent in agents
            ),
            "alerts": sum(len(agent["alerts"]) for agent in agents),
            "active_commands": queued,
            "controller": {"version": __version__, **controller_metrics.sample()},
        }

    @app.get("/api/agents")
    def list_agents(
        # Deprecated API compatibility; the dashboard does not use this query.
        tag: str | None = Query(default=None, max_length=32),
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, Any]]:
        del user
        with database.connection() as db:
            rows = db.execute("SELECT * FROM agents ORDER BY name COLLATE NOCASE").fetchall()
            refresh_rows = db.execute("SELECT * FROM configuration_refreshes").fetchall()
        refreshes = {row["agent_id"]: row for row in refresh_rows}
        agents = []
        for row in rows:
            item = _serialize_agent(row, settings)
            item["configuration_refresh"] = serialize_configuration_refresh(
                refreshes.get(row["id"])
            )
            agents.append(item)
        if tag:
            agents = [agent for agent in agents if tag.lower() in agent["tags"]]
        return agents

    @app.get("/api/agents/{agent_id}")
    def get_agent(
        agent_id: str, user: dict[str, Any] = Depends(current_user)
    ) -> dict[str, Any]:
        del user
        with database.connection() as db:
            row = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            refresh = db.execute(
                "SELECT * FROM configuration_refreshes WHERE agent_id=?", (agent_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Agent not found")
        item = _serialize_agent(row, settings)
        item["configuration_refresh"] = serialize_configuration_refresh(refresh)
        return item

    @app.patch("/api/agents/{agent_id}")
    def update_agent(
        agent_id: str,
        body: AgentUpdate,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        requested_name = body.name.strip() if body.name is not None else None
        fields: list[str] = []
        values: list[Any] = []
        if requested_name is not None:
            fields.append("name = ?")
            values.append(requested_name)
        if body.note is not None:
            fields.append("note = ?")
            values.append(body.note.strip())
        if body.tags is not None:
            fields.append("tags_json = ?")
            values.append(json.dumps(body.tags))
        if body.disabled is not None:
            fields.append("disabled_at = ?")
            values.append(utcnow() if body.disabled else None)
        if not fields:
            return get_agent(agent_id, user)
        values.append(agent_id)
        worker_refresh_sheet_ids: list[str] = []
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if not existing:
                raise HTTPException(404, "Agent not found")
            name_changed = (
                requested_name is not None and requested_name != existing["name"]
            )
            disabling_now = body.disabled is True and not existing["disabled_at"]
            abandoned_commands: list[dict[str, str]] = []
            if disabling_now:
                abandoned_commands = _abandon_agent_commands(
                    db,
                    agent_id=agent_id,
                    command_ids=None,
                    actor=user["username"],
                    reason=(
                        "device disabled by administrator; controller stopped "
                        "tracking unfinished commands; remote execution may "
                        "already have occurred"
                    ),
                    now=utcnow(),
                )
            cursor = db.execute(
                f"UPDATE agents SET {', '.join(fields)} WHERE id = ?", values
            )
            if cursor.rowcount == 0:
                raise HTTPException(404, "Agent not found")
            audit_details = body.model_dump(exclude_none=True)
            if disabling_now:
                audit_details["abandoned_unfinished_commands"] = abandoned_commands
            Database.audit(
                db,
                actor=user["username"],
                action="agent_updated",
                target_type="agent",
                target_id=agent_id,
                details=audit_details,
            )
            row = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if name_changed and row["disabled_at"] is None:
                sheet_id = _reported_agent_flight_sheet_id(row)
                sheet = (
                    db.execute(
                        """SELECT worker_name_template FROM flight_sheets
                           WHERE id=?""",
                        (sheet_id,),
                    ).fetchone()
                    if sheet_id
                    else None
                )
                template = (
                    str(sheet["worker_name_template"]) if sheet is not None else ""
                )
                if worker_template_uses_agent_name(template):
                    worker_refresh_sheet_ids = [sheet_id]
                    _persist_configuration_refreshes(
                        db,
                        {sheet_id},
                        user,
                        trigger_type="flight_sheet",
                        trigger_id=f"agent-name:{agent_id}",
                        target_agent_ids={agent_id},
                    )
            refresh = db.execute(
                "SELECT * FROM configuration_refreshes WHERE agent_id=?", (agent_id,)
            ).fetchone()
        if worker_refresh_sheet_ids and not disabling_now:
            reapply_active_flight_sheets(
                worker_refresh_sheet_ids,
                user,
                trigger_type="flight_sheet",
                trigger_id=f"agent-name:{agent_id}",
                persist=False,
                target_agent_ids={agent_id},
            )
        if worker_refresh_sheet_ids:
            with database.connection() as db:
                refresh = db.execute(
                    "SELECT * FROM configuration_refreshes WHERE agent_id=?",
                    (agent_id,),
                ).fetchone()
        item = _serialize_agent(row, settings)
        item["configuration_refresh"] = serialize_configuration_refresh(refresh)
        return item

    @app.patch("/api/agents/{agent_id}/mining-autostart")
    def update_agent_mining_autostart(
        agent_id: str,
        body: MiningAutostartUpdate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Agent not found")
            if body.enabled and not _agent_version_at_least(
                row["agent_version"], (0, 5, 2)
            ):
                raise HTTPException(
                    409,
                    "Mining autostart requires RigPilot Agent 0.5.2 or newer; "
                    "upgrade this device first",
                )
            was_enabled = bool(
                _optional_row_value(row, "mining_autostart_enabled", 0)
            )
            baseline = _optional_row_value(
                row, "mining_autostart_last_boot_time"
            )
            if body.enabled and not was_enabled:
                baseline = _boot_time_identity(_json(row["metrics_json"], {}))
            db.execute(
                """UPDATE agents
                   SET mining_autostart_enabled=?,
                       mining_autostart_last_boot_time=?
                   WHERE id=?""",
                (int(body.enabled), baseline, agent_id),
            )
            Database.audit(
                db,
                actor=user["username"],
                action="mining_autostart_updated",
                target_type="agent",
                target_id=agent_id,
                details={
                    "enabled": body.enabled,
                    "boot_time_baseline": baseline,
                },
            )
            updated = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
        return _serialize_agent(updated, settings)

    @app.delete("/api/agents/{agent_id}")
    def delete_agent(
        agent_id: str,
        abandon_unfinished: bool = Query(default=False),
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        """Permanently remove an offline or disabled agent and its history.

        The status and unfinished-command checks happen in the same database
        transaction as the delete.  SQLite foreign keys intentionally cascade
        terminal command and telemetry rows; the audit entry remains.
        """

        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if not agent:
                raise HTTPException(404, "Agent not found")
            agent_status = _serialize_agent(agent, settings)["status"]
            if agent_status not in {"offline", "disabled"}:
                raise HTTPException(409, "Only offline or disabled agents can be deleted")
            now = utcnow()
            unfinished = db.execute(
                """SELECT id, action, status FROM commands
                   WHERE agent_id=?
                     AND status IN ('queued','dispatched','running')
                     AND (
                         expires_at > ?
                         OR EXISTS (
                             SELECT 1 FROM agent_update_jobs j
                             WHERE j.command_id=commands.id
                               AND j.status='activating'
                         )
                     )
                   ORDER BY created_at, id""",
                (agent_id, now),
            ).fetchall()
            if unfinished and not abandon_unfinished:
                raise HTTPException(
                    409,
                    "设备有未完成任务 (unfinished commands)；请先取消等待任务，"
                    "或确认删除时一并结束离线遗留记录",
                )
            abandoned_commands: list[dict[str, str]] = []
            if abandon_unfinished:
                abandoned_commands = _abandon_agent_commands(
                    db,
                    agent_id=agent_id,
                    command_ids=None,
                    actor=user["username"],
                    reason=(
                        "offline device deleted by administrator; controller abandoned "
                        "unfinished command; remote execution may already have occurred"
                    ),
                    now=now,
                )
            else:
                db.execute(
                    """UPDATE commands SET status='expired', finished_at=?
                       WHERE agent_id=?
                         AND status IN ('queued','dispatched','running')
                         AND expires_at <= ?
                         AND NOT EXISTS (
                             SELECT 1 FROM agent_update_jobs j
                             WHERE j.command_id=commands.id
                               AND j.status='activating'
                         )""",
                    (now, agent_id, now),
                )
            terminal_commands = db.execute(
                "SELECT COUNT(*) AS count FROM commands WHERE agent_id=?",
                (agent_id,),
            ).fetchone()["count"]
            telemetry_points = db.execute(
                "SELECT COUNT(*) AS count FROM telemetry WHERE agent_id=?",
                (agent_id,),
            ).fetchone()["count"]
            hashrate_buckets = db.execute(
                """SELECT COUNT(*) AS count FROM hashrate_history_buckets
                   WHERE agent_id=?""",
                (agent_id,),
            ).fetchone()["count"]
            db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            Database.audit(
                db,
                actor=user["username"],
                action="agent_deleted",
                target_type="agent",
                target_id=agent_id,
                details={
                    "name": agent["name"],
                    "hostname": agent["hostname"],
                    "terminal_commands_deleted": terminal_commands,
                    "telemetry_points_deleted": telemetry_points,
                    "hashrate_history_buckets_deleted": hashrate_buckets,
                    "abandoned_unfinished_commands": abandoned_commands,
                },
            )
        return {"status": "deleted"}

    @app.get("/api/agents/{agent_id}/telemetry")
    def telemetry(
        agent_id: str,
        limit: int = Query(default=60, ge=1, le=500),
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, Any]]:
        del user
        with database.connection() as db:
            exists = db.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if not exists:
                raise HTTPException(404, "Agent not found")
            rows = db.execute(
                """SELECT recorded_at, metrics_json FROM telemetry
                   WHERE agent_id = ? ORDER BY recorded_at DESC, id DESC LIMIT ?""",
                (agent_id, limit),
            ).fetchall()
        return [
            {"recorded_at": row["recorded_at"], "metrics": _json(row["metrics_json"], {})}
            for row in reversed(rows)
        ]

    @app.get("/api/hashrate-history")
    def hashrate_history(
        hours: int = Query(default=24, ge=1, le=168),
        bucket_minutes: int = Query(default=5, ge=5, le=60),
        agent_id: str | None = Query(default=None, min_length=1, max_length=128),
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        """Aggregate each device's last complete snapshot by coin.

        The last complete telemetry heartbeat for each device/time bucket is
        authoritative.  This avoids both over-counting fast heartbeats and
        carrying an earlier rate through a later stopped/unknown heartbeat.
        """

        del user
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        bucket_seconds = bucket_minutes * 60
        with database.connection() as db:
            state_row = db.execute(
                """SELECT started_at FROM hashrate_history_rollup_state
                   WHERE singleton=1"""
            ).fetchone()
            rollup_started_at = _parse_time(
                state_row["started_at"] if state_row is not None else None
            )
            compact_agent_clause = " AND agent_id=?" if agent_id is not None else ""
            compact_rows = db.execute(
                f"""SELECT agent_id,recorded_at,samples_json
                    FROM hashrate_history_buckets
                    WHERE bucket_start >= ? AND bucket_start <= ?
                    {compact_agent_clause}
                    ORDER BY recorded_at,agent_id""",
                (
                    _hashrate_bucket_start(start).isoformat(timespec="seconds"),
                    end.isoformat(timespec="seconds"),
                    *((agent_id,) if agent_id is not None else ()),
                ),
            ).fetchall()
            # Existing telemetry before the O(1) rollout boundary remains
            # immediately visible without a startup backfill.  This indexed
            # compatibility read naturally disappears after raw retention
            # expires; new history reads only the compact table.
            legacy_end = min(end, rollup_started_at or end)
            raw_rows = []
            if legacy_end >= start:
                raw_agent_clause = " AND agent_id=?" if agent_id is not None else ""
                raw_rows = db.execute(
                    f"""SELECT t.id,t.agent_id,t.recorded_at,t.metrics_json
                        FROM telemetry AS t
                        JOIN (
                            SELECT MAX(id) AS id
                            FROM telemetry
                            WHERE recorded_at >= ? AND recorded_at <= ?
                            {raw_agent_clause}
                            GROUP BY agent_id,
                                CAST(strftime('%s', recorded_at) AS INTEGER) / ?
                        ) AS latest ON latest.id=t.id
                        ORDER BY t.recorded_at,t.id""",
                    (
                        start.isoformat(timespec="seconds"),
                        legacy_end.isoformat(timespec="seconds"),
                        *((agent_id,) if agent_id is not None else ()),
                        bucket_seconds,
                    ),
                ).fetchall()

        last_rows: dict[
            tuple[str, int],
            tuple[datetime, int, list[dict[str, Any]]],
        ] = {}

        def keep_latest(
            *,
            agent_id: str,
            recorded_at_value: Any,
            samples: list[dict[str, Any]],
            source_priority: int,
        ) -> None:
            recorded_at = _parse_time(recorded_at_value)
            if recorded_at is None or recorded_at < start or recorded_at > end:
                return
            epoch = int(recorded_at.timestamp())
            bucket_epoch = epoch - (epoch % bucket_seconds)
            key = (agent_id, bucket_epoch)
            previous = last_rows.get(key)
            if previous is None or (recorded_at, source_priority) > (
                previous[0],
                previous[1],
            ):
                last_rows[key] = (recorded_at, source_priority, samples)

        for row in raw_rows:
            keep_latest(
                agent_id=row["agent_id"],
                recorded_at_value=row["recorded_at"],
                samples=_telemetry_hashrate_samples(
                    _json(row["metrics_json"], {})
                ),
                source_priority=0,
            )
        for row in compact_rows:
            samples_value = _json(row["samples_json"], [])
            keep_latest(
                agent_id=row["agent_id"],
                recorded_at_value=row["recorded_at"],
                samples=_telemetry_hashrate_samples(
                    {
                        "hashrate_samples": (
                            samples_value if isinstance(samples_value, list) else []
                        )
                    }
                ),
                source_priority=1,
            )

        metadata: dict[str, dict[str, Any]] = {}
        totals: dict[tuple[str, int], list[float | int]] = {}
        for (source_agent_id, bucket_epoch), (_recorded_at, _priority, samples) in last_rows.items():
            device_totals: dict[str, list[float]] = defaultdict(list)
            for sample in samples:
                coin = sample["coin"]
                # Missing coin identity cannot safely join a known coin or
                # another unidentified device/algorithm. Keep its source visible.
                known_coin = coin != "UNKNOWN"
                series_id = f"coin:{coin}" if known_coin else f"unknown:{source_agent_id}:{sample['series']}"
                item = metadata.setdefault(series_id, {
                    "key": series_id, "coin": coin, "unit": "H/s",
                    "aggregation": "coin" if known_coin else "unknown_source",
                    "algorithms": set(), "source_series_keys": set(),
                    **({"agent_id": source_agent_id} if not known_coin else {}),
                })
                item["algorithms"].add(sample["algorithm"])
                item["source_series_keys"].add(sample["series"])
                device_totals[series_id].append(float(sample["hashrate"]))
            # Multiple legs/algorithm aliases in this one authoritative device
            # snapshot contribute their rates, but count as only one device.
            for series_id, values in device_totals.items():
                aggregate = totals.setdefault((series_id, bucket_epoch), [0.0, 0])
                aggregate[0] = float(aggregate[0]) + math.fsum(values)
                aggregate[1] = int(aggregate[1]) + 1
        for item in metadata.values():
            item["algorithms"] = sorted(item["algorithms"])
            item["source_series_keys"] = sorted(item["source_series_keys"])
            item["algorithm"] = item["algorithms"][0] if len(item["algorithms"]) == 1 else None

        series_totals: defaultdict[str, list[tuple[int, float | int, float | int]]] = defaultdict(list)
        for (series_id, bucket_epoch), (total, agent_count) in sorted(
            totals.items(), key=lambda pair: (pair[0][1], pair[0][0])
        ):
            series_totals[series_id].append((bucket_epoch, total, agent_count))
        series: list[dict[str, Any]] = []
        for series_id, item in sorted(
            metadata.items(),
            key=lambda pair: (
                pair[1]["coin"],
                pair[0],
            ),
        ):
            points = []
            for bucket_epoch, total, agent_count in series_totals[series_id]:
                numeric_total: float | int = float(total)
                if numeric_total.is_integer():
                    numeric_total = int(numeric_total)
                points.append(
                    {
                        "timestamp": datetime.fromtimestamp(
                            bucket_epoch, timezone.utc
                        ).isoformat(timespec="seconds"),
                        "value": numeric_total,
                        "agent_count": int(agent_count),
                    }
                )
            series.append({**item, "points": points})

        return {
            "group_by": "coin",
            **({"agent_id": agent_id} if agent_id is not None else {}),
            "hours": hours,
            "bucket_minutes": bucket_minutes,
            "from": start.isoformat(timespec="seconds"),
            "to": end.isoformat(timespec="seconds"),
            "series": series,
        }

    @app.get("/api/workload")
    def workload(
        from_: datetime = Query(alias="from"),
        to_: datetime = Query(alias="to"),
        agent_id: str | None = Query(default=None, min_length=1, max_length=128),
        algorithm: str | None = Query(default=None, min_length=1, max_length=64),
        coin: str | None = Query(default=None, min_length=1, max_length=WORKLOAD_COIN_MAX_LENGTH, pattern=r"^[A-Za-z0-9._+-]+$"),
        coins: list[str] | None = Query(default=None, max_length=64),
        include_points: bool = Query(default=False),
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        """Return supported-coin Agent-integrated work from five-minute buckets."""

        del user
        if from_.utcoffset() is None or to_.utcoffset() is None:
            raise HTTPException(422, "Workload timestamps must include a timezone")
        start = _utc_time(from_)
        end = _utc_time(to_)
        if start is None or end is None:
            raise HTTPException(422, "Workload timestamps cannot be represented in UTC")
        if end <= start:
            raise HTTPException(422, "Workload end time must be after start time")
        if end - start > timedelta(days=WORKLOAD_RETENTION_DAYS):
            raise HTTPException(
                422,
                f"Workload range cannot exceed {WORKLOAD_RETENTION_DAYS} days",
            )
        normalized_algorithm = None
        if algorithm is not None:
            normalized_algorithm = algorithm.strip().casefold()
            if not normalized_algorithm or len(normalized_algorithm) > 64 or any(
                ord(character) < 32 or ord(character) == 127
                for character in algorithm
            ):
                raise HTTPException(422, "Invalid workload algorithm")
        normalized_coin = canonical_workload_coin(coin) if coin is not None else None
        if coin is not None and normalized_coin is None:
            raise HTTPException(422, "Invalid workload coin")
        selected_coins = None if coins is None else []
        for value in coins or []:
            candidate = canonical_workload_coin(value) if 1 <= len(value) <= WORKLOAD_COIN_MAX_LENGTH else None
            if candidate is None:
                raise HTTPException(422, "Invalid workload coin")
            if candidate not in selected_coins:
                selected_coins.append(candidate)
        try:
            with database.connection() as db:
                summary = workload_summary(
                    db,
                    start=start,
                    end=end,
                    agent_id=agent_id,
                    algorithm=normalized_algorithm,
                    include_points=include_points,
                    coin=normalized_coin,
                    coins=selected_coins,
                )
        except WorkloadTimeRangeError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**summary, "supported_coins": sorted(set(SUPPORTED_WORKLOAD_COINS) | set(summary["available_coins"]))}

    def workload_settlement_record(row: sqlite3.Row | None) -> dict[str, Any]:
        """Expose the new timestamp while preserving the legacy note contract."""

        stored_value = row["value"] if row is not None else ""
        settlement_at: str | None = None
        legacy_note: str | None = None
        if stored_value:
            candidate = (
                stored_value[:-1] + "+00:00"
                if stored_value.endswith(("Z", "z"))
                else stored_value
            )
            normalized = _utc_time(candidate)
            if normalized is not None:
                if (
                    normalized.minute % 5 == 0
                    and normalized.second == 0
                    and normalized.microsecond == 0
                ):
                    settlement_at = normalized.isoformat(timespec="seconds")
                else:
                    legacy_note = stored_value
            else:
                legacy_note = stored_value
        return {
            "settlement_at": settlement_at,
            "legacy_note": legacy_note,
            # Deprecated compatibility field for pre-time-picker clients.
            "note": stored_value,
            "updated_at": _optional_row_value(row, "updated_at") if row is not None else None,
            "updated_by": _optional_row_value(row, "updated_by") if row is not None else None,
            "revision": int(_optional_row_value(row, "revision", 0)) if row is not None else 0,
        }

    def workload_owner_groups_record(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {"initialized": False, "revision": 0, "groups": [], "updated_at": None}
        try:
            data = json.loads(row["value"])
            validated = WorkloadOwnerGroupsUpdate.model_validate({
                "expected_revision": int(row["revision"]), "groups": data["groups"],
            })
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(500, "保存的归属记录格式异常，请联系管理员") from exc
        return {"initialized": True, "revision": int(row["revision"]),
                "groups": [group.model_dump() for group in validated.groups], "updated_at": row["updated_at"]}

    @app.get("/api/workload-owner-groups")
    def get_workload_owner_groups(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        key = f"workload_owner_groups:{user['id']}"
        with database.connection() as db:
            row = db.execute("SELECT value,updated_at,revision FROM controller_preferences WHERE key=?", (key,)).fetchone()
        return workload_owner_groups_record(row)

    @app.put("/api/workload-owner-groups")
    def update_workload_owner_groups(body: WorkloadOwnerGroupsUpdate,
                                     user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        key = f"workload_owner_groups:{user['id']}"
        value = json.dumps({"groups": [group.model_dump() for group in body.groups]}, separators=(",", ":"), ensure_ascii=False)
        if len(value.encode("utf-8")) > 1024 * 1024:
            raise HTTPException(422, "归属记录过大")
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value,updated_at,revision FROM controller_preferences WHERE key=?", (key,)).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != body.expected_revision or current_revision >= 2**63 - 1:
                raise HTTPException(409, "归属已被其他页面修改，请载入服务器归属后再保存")
            revision = current_revision + 1
            updated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            if row is None:
                db.execute("INSERT INTO controller_preferences(key,value,updated_by,updated_at,revision) VALUES(?,?,?,?,?)",
                           (key, value, user["id"], updated_at, revision))
            else:
                changed = db.execute("UPDATE controller_preferences SET value=?,updated_by=?,updated_at=?,revision=? WHERE key=? AND revision=?",
                                     (value, user["id"], updated_at, revision, key, current_revision))
                if changed.rowcount != 1:
                    raise HTTPException(409, "归属已被其他页面修改，请载入服务器归属后再保存")
        return {"initialized": True, "revision": revision, "groups": [group.model_dump() for group in body.groups], "updated_at": updated_at}

    @app.get("/api/workload-settlement-note")
    def get_workload_settlement_note(
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        del user
        with database.connection() as db:
            row = db.execute(
                """SELECT p.value, p.updated_at, p.revision,
                          u.username AS updated_by
                   FROM controller_preferences p
                   LEFT JOIN users u ON u.id=p.updated_by
                   WHERE p.key='workload_settlement_note'"""
            ).fetchone()
        return workload_settlement_record(row)

    @app.put("/api/workload-settlement-note")
    def update_workload_settlement_note(
        body: WorkloadSettlementNoteUpdate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        settlement_moment = body.settlement_at
        if settlement_moment > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise HTTPException(422, "结算时间不能明显晚于控制端当前时间")
        settlement_at = settlement_moment.isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT value, updated_at, revision
                   FROM controller_preferences
                   WHERE key='workload_settlement_note'"""
            ).fetchone()
            current_revision = int(existing["revision"]) if existing else 0
            if body.expected_revision != current_revision:
                raise HTTPException(
                    409,
                    "结算时间已被其他用户修改，请刷新后再保存",
                )
            previous_record = workload_settlement_record(existing)
            updated_moment = datetime.now(timezone.utc)
            previous_updated_at = _parse_time(
                existing["updated_at"] if existing is not None else None
            )
            if previous_updated_at is not None and updated_moment <= previous_updated_at:
                updated_moment = previous_updated_at + timedelta(microseconds=1)
            updated_at = updated_moment.isoformat(timespec="microseconds")
            next_revision = current_revision + 1
            if existing is None:
                db.execute(
                    """INSERT INTO controller_preferences
                       (key, value, updated_by, updated_at, revision)
                       VALUES('workload_settlement_note', ?, ?, ?, ?)""",
                    (settlement_at, user["id"], updated_at, next_revision),
                )
            else:
                updated = db.execute(
                    """UPDATE controller_preferences
                       SET value=?, updated_by=?, updated_at=?, revision=?
                       WHERE key='workload_settlement_note' AND revision=?""",
                    (
                        settlement_at,
                        user["id"],
                        updated_at,
                        next_revision,
                        current_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise HTTPException(
                        409,
                        "结算时间已被其他用户修改，请刷新后再保存",
                    )
            Database.audit(
                db,
                actor=user["username"],
                action="workload_settlement_note_updated",
                target_type="controller_preference",
                target_id="workload_settlement_note",
                details={
                    "settlement_at": settlement_at,
                    "replaced_legacy_note": previous_record["legacy_note"] is not None,
                },
            )
        return {
            "settlement_at": settlement_at,
            "legacy_note": None,
            "note": settlement_at,
            "updated_at": updated_at,
            "updated_by": user["username"],
            "revision": next_revision,
        }

    @app.post("/api/enrollment-tokens")
    def create_enrollment_token(
        body: EnrollmentTokenCreate,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        token_id = str(uuid.uuid4())
        token = "rpe_" + secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)
        ).isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute(
                """INSERT INTO enrollment_tokens
                   (id, token_hash, label, expires_at, created_by, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    token_id,
                    hash_api_secret(token),
                    body.label.strip(),
                    expires_at,
                    user["id"],
                    utcnow(),
                ),
            )
            Database.audit(
                db,
                actor=user["username"],
                action="enrollment_token_created",
                target_type="enrollment_token",
                target_id=token_id,
                details={"label": body.label, "expires_at": expires_at},
            )
        return {
            "id": token_id,
            "token": token,
            "label": body.label,
            "expires_at": expires_at,
        }

    @app.get("/api/enrollment-tokens")
    def list_enrollment_tokens(
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> list[dict[str, Any]]:
        del user
        with database.connection() as db:
            rows = db.execute(
                """SELECT id, label, expires_at, used_at, created_at
                   FROM enrollment_tokens ORDER BY created_at DESC LIMIT 100"""
            ).fetchall()
        return [dict(row) for row in rows]

    @app.delete("/api/enrollment-tokens/{token_id}")
    def revoke_enrollment_token(
        token_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        """Revoke one not-yet-used enrollment token before its expiry.

        A token that already enrolled a device anchors that device's
        enrollment-recovery receipt (``agent_enrollment_attempts``) through a
        cascading foreign key; deleting it here would silently strip an
        in-flight recoverable enrollment of its identity, so used tokens stay
        until the metadata retention prune removes them with their receipts.
        """
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            token = db.execute(
                "SELECT id, label, used_at FROM enrollment_tokens WHERE id=?",
                (token_id,),
            ).fetchone()
            if token is None:
                raise HTTPException(404, "Enrollment token not found")
            if token["used_at"] is not None:
                raise HTTPException(
                    409,
                    "Used enrollment tokens anchor enrollment recovery; they "
                    "retire through metadata retention",
                )
            db.execute("DELETE FROM enrollment_tokens WHERE id=?", (token_id,))
            Database.audit(
                db,
                actor=user["username"],
                action="enrollment_token_revoked",
                target_type="enrollment_token",
                target_id=token_id,
                details={"label": token["label"]},
            )
        return {"status": "revoked"}

    @app.post("/api/agents/{agent_id}/replacement-token", status_code=201)
    def create_agent_replacement_token(
        agent_id: str,
        body: AgentReplacementTokenCreate,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        token_id = str(uuid.uuid4())
        token = "rpr_" + secrets.token_urlsafe(32)
        created_at = utcnow()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)
        ).isoformat(timespec="seconds")
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if agent is None:
                raise HTTPException(404, "Agent not found")
            if agent["disabled_at"]:
                raise HTTPException(409, "Disabled devices cannot be inherited")
            if not secrets.compare_digest(
                body.confirm_name.encode("utf-8"),
                str(agent["name"]).encode("utf-8"),
            ):
                raise HTTPException(422, "Device name confirmation does not match")
            pending_replacement = db.execute(
                """SELECT id FROM agent_replacement_tokens
                   WHERE target_agent_id=? AND used_at IS NOT NULL
                     AND activated_at IS NULL
                   ORDER BY used_at DESC LIMIT 1""",
                (agent_id,),
            ).fetchone()
            if pending_replacement is not None:
                raise HTTPException(
                    409,
                    "This device already has an inherited installation waiting "
                    "for its first authenticated heartbeat",
                )
            # Issuing a new code is an explicit administrative replacement of
            # every earlier unused code for this same device.
            db.execute(
                """DELETE FROM agent_replacement_tokens
                   WHERE target_agent_id=? AND used_at IS NULL""",
                (agent_id,),
            )
            # A code may be issued while the old Agent is online; it can be
            # claimed only once that Agent is offline. Preserve the controller-
            # observed configured sheet only when the administrator had also
            # enabled mining autostart.  The clean installation will compile a
            # fresh payload from current wallet/package/flight data after its
            # new credential proves ownership with a heartbeat.
            active_sheet_id = (
                _reported_agent_flight_sheet_id(agent)
                if bool(
                    _optional_row_value(agent, "mining_autostart_enabled", 0)
                )
                else None
            )
            if active_sheet_id is not None and db.execute(
                "SELECT 1 FROM flight_sheets WHERE id=?", (active_sheet_id,)
            ).fetchone() is None:
                active_sheet_id = None
            db.execute(
                """INSERT INTO agent_replacement_tokens
                   (id, token_hash, target_agent_id, expires_at,
                    resume_flight_sheet_id, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    token_id,
                    hash_api_secret(token),
                    agent_id,
                    expires_at,
                    active_sheet_id,
                    user["id"],
                    created_at,
                ),
            )
            Database.audit(
                db,
                actor=user["username"],
                action="agent_replacement_token_created",
                target_type="agent",
                target_id=agent_id,
                details={
                    "token_id": token_id,
                    "expires_at": expires_at,
                    "resume_flight_sheet_id": active_sheet_id,
                },
            )
        return {
            "id": token_id,
            "token": token,
            "agent_id": agent_id,
            "agent_name": agent["name"],
            "expires_at": expires_at,
            "schema": "rigpilot.replacement.v1",
        }

    @app.post("/api/agent/replace", status_code=201)
    def replace_agent_identity(
        body: AgentReplaceRequest,
        request: Request,
    ) -> dict[str, Any]:
        if settings.require_https_for_agents and request.url.scheme != "https":
            raise HTTPException(400, "HTTPS is required for device inheritance")
        token_hash = hash_api_secret(body.replacement_token)
        proposed_secret_hash = hash_api_secret(body.proposed_agent_secret)
        remote_ip = request.client.host if request.client else "unknown"
        limiter_key = token_hash[:16]
        replacement_rate_limiter.check(remote_ip, limiter_key)
        inherited_agent_id: str
        try:
            # Invalid random tokens must not acquire SQLite's single writer
            # lock.  Re-read under BEGIN IMMEDIATE below before trusting any
            # lifecycle field, so this remains race-safe.
            with database.connection() as db:
                known_token = db.execute(
                    "SELECT 1 FROM agent_replacement_tokens WHERE token_hash=?",
                    (token_hash,),
                ).fetchone()
            if known_token is None:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "Invalid or expired device inheritance token",
                )
            with database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                used_at = utcnow()
                replacement = db.execute(
                    """SELECT t.*, a.name AS agent_name, a.disabled_at,
                              a.last_seen, a.inventory_json, a.metrics_json,
                              a.agent_version AS previous_agent_version,
                              a.secret_hash AS current_secret_hash
                       FROM agent_replacement_tokens t
                       JOIN agents a ON a.id=t.target_agent_id
                       WHERE t.token_hash=?""",
                    (token_hash,),
                ).fetchone()
                if replacement is None or (
                    replacement["used_at"] is None
                    and replacement["expires_at"] <= used_at
                ):
                    raise HTTPException(
                        status.HTTP_401_UNAUTHORIZED,
                        "Invalid or expired device inheritance token",
                    )
                inherited_agent_id = replacement["target_agent_id"]
                if replacement["used_at"] is not None:
                    if not (
                        secrets.compare_digest(
                            str(replacement["request_id"] or ""), body.request_id
                        )
                        and secrets.compare_digest(
                            str(replacement["replacement_secret_hash"] or ""),
                            proposed_secret_hash,
                        )
                    ):
                        raise HTTPException(
                            status.HTTP_401_UNAUTHORIZED,
                            "Device inheritance token was already used",
                        )
                    if replacement["disabled_at"]:
                        raise HTTPException(409, "Device selected for inheritance is disabled")
                    if not secrets.compare_digest(
                        str(replacement["current_secret_hash"] or ""),
                        proposed_secret_hash,
                    ):
                        raise HTTPException(
                            409,
                            "This device inheritance attempt was superseded by a newer identity",
                        )
                else:
                    if replacement["disabled_at"]:
                        raise HTTPException(
                            409, "Device selected for inheritance is disabled"
                        )
                    agent_row = db.execute(
                        "SELECT * FROM agents WHERE id=?", (inherited_agent_id,)
                    ).fetchone()
                    if agent_row is None:
                        raise HTTPException(
                            409, "Device selected for inheritance is unavailable"
                        )
                    if _serialize_agent(agent_row, settings)["status"] != "offline":
                        raise HTTPException(
                            409,
                            "Device selected for inheritance is still online; shut down "
                            "the previous Agent and wait until it is offline",
                        )
                    activating = db.execute(
                        """SELECT 1 FROM agent_update_jobs
                           WHERE agent_id=? AND status='activating' LIMIT 1""",
                        (inherited_agent_id,),
                    ).fetchone()
                    if activating is not None:
                        raise HTTPException(
                            409,
                            "Device has an activating Agent update; finish or clear it before inheritance",
                        )
                    _abandon_agent_commands(
                        db,
                        agent_id=inherited_agent_id,
                        command_ids=None,
                        actor=f"replacement:{replacement['id']}",
                        reason=(
                            "device identity moved to a clean installation; unfinished "
                            "work from the previous Agent was closed"
                        ),
                        now=used_at,
                    )
                    # A refresh still waiting for the previous identity to
                    # prove its miner live can never resolve on the clean
                    # installation, which compiles its first payload from
                    # current wallet/package data.  Close it instead of leaving
                    # a permanent "configuration pending" badge; an autostart
                    # resume re-creates the row on the first authenticated
                    # heartbeat.
                    db.execute(
                        """UPDATE configuration_refreshes
                           SET status='cancelled', updated_at=?, completed_at=?,
                               claim_token=NULL, last_error=?
                           WHERE agent_id=? AND status IN ('pending','preparing')""",
                        (
                            used_at,
                            used_at,
                            (
                                "device identity was replaced; the clean "
                                "installation compiles fresh configuration"
                            ),
                            inherited_agent_id,
                        ),
                    )
                    updated = db.execute(
                        """UPDATE agents
                           SET secret_hash=?, hostname=?, os_family=?, platform=?,
                               agent_version=?, inventory_json='{}', metrics_json='{}',
                               capabilities_json='[]', last_ip=NULL, public_ip=NULL,
                               local_ip_addresses_json='[]', last_seen=NULL,
                               mining_autostart_last_boot_time=NULL
                           WHERE id=? AND disabled_at IS NULL""",
                        (
                            proposed_secret_hash,
                            body.hostname,
                            body.os_family.casefold(),
                            body.platform,
                            body.agent_version,
                            inherited_agent_id,
                        ),
                    )
                    claimed = db.execute(
                        """UPDATE agent_replacement_tokens
                           SET used_at=?, request_id=?, replacement_secret_hash=?
                           WHERE id=? AND used_at IS NULL""",
                        (
                            used_at,
                            body.request_id,
                            proposed_secret_hash,
                            replacement["id"],
                        ),
                    )
                    if updated.rowcount != 1 or claimed.rowcount != 1:
                        raise HTTPException(
                            409, "Device inheritance could not be committed"
                        )
                    Database.audit(
                        db,
                        actor=f"replacement:{replacement['id']}",
                        action="agent_identity_replaced",
                        target_type="agent",
                        target_id=inherited_agent_id,
                        details={
                            "previous_agent_version": replacement[
                                "previous_agent_version"
                            ],
                            "new_agent_version": body.agent_version,
                            "hostname": body.hostname,
                            "preserved_name": replacement["agent_name"],
                        },
                    )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_401_UNAUTHORIZED:
                replacement_rate_limiter.record_failure(remote_ip, limiter_key)
            else:
                replacement_rate_limiter.abandon(remote_ip, limiter_key)
            raise
        except Exception:
            replacement_rate_limiter.abandon(remote_ip, limiter_key)
            raise
        else:
            replacement_rate_limiter.record_success(remote_ip, limiter_key)
        return {
            "agent_id": inherited_agent_id,
            "heartbeat_interval": 10,
            "inherited_existing_device": True,
        }

    @app.post("/api/agent/enroll", status_code=201)
    def enroll_agent(body: AgentEnrollRequest, request: Request) -> Any:
        if settings.require_https_for_agents and request.url.scheme != "https":
            raise HTTPException(400, "HTTPS is required for agent enrollment")
        recovery = body.enrollment_request_id is not None
        token_hash = hash_api_secret(body.enrollment_token) if body.enrollment_token else None
        request_hash = hash_api_secret(body.enrollment_request_id) if recovery else None
        proposed_hash = hash_api_secret(body.proposed_agent_secret) if recovery else None
        metadata = {"name": (body.name or body.hostname).strip(), "hostname": body.hostname,
                    "os_family": body.os_family.lower(), "platform": body.platform, "agent_version": body.agent_version}
        metadata_hash = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False,
                                                  separators=(",", ":")).encode()).hexdigest()

        def replay(db: sqlite3.Connection) -> dict[str, Any] | None:
            if not recovery:
                return None
            receipt = db.execute(
                """SELECT r.*,t.token_hash,a.secret_hash AS current_secret_hash,a.disabled_at
                   FROM agent_enrollment_attempts r
                   JOIN enrollment_tokens t ON t.id=r.token_id
                   LEFT JOIN agents a ON a.id=r.agent_id WHERE r.request_id_hash=?""",
                (request_hash,),
            ).fetchone()
            if receipt is None:
                return None
            if (not secrets.compare_digest(receipt["secret_hash"], proposed_hash)
                    or (token_hash is not None and not secrets.compare_digest(receipt["token_hash"], token_hash))):
                raise HTTPException(401, "Enrollment recovery proof does not match")
            if not secrets.compare_digest(receipt["metadata_hash"], metadata_hash):
                raise HTTPException(409, "Enrollment recovery metadata differs from the original request")
            if (receipt["agent_id"] is None or receipt["disabled_at"] is not None
                    or not secrets.compare_digest(str(receipt["current_secret_hash"] or ""), proposed_hash)):
                raise HTTPException(409, "The enrolled identity is no longer available to this request")
            return {"agent_id": receipt["agent_id"], "agent_secret": body.proposed_agent_secret,
                    "heartbeat_interval": 10, "enrollment_request_id": body.enrollment_request_id}

        def reject_token(db: sqlite3.Connection) -> JSONResponse:
            # A caller can retire only an unclaimed attempt after a definite
            # token rejection. Never issue this signal for lost-token recovery,
            # existing receipts, reused credentials or ambiguous transport errors.
            if recovery and token_hash is not None:
                bound = db.execute(
                    """SELECT 1 FROM agent_enrollment_attempts WHERE request_id_hash=? OR secret_hash=?
                       UNION ALL SELECT 1 FROM agents WHERE secret_hash=? LIMIT 1""",
                    (request_hash, proposed_hash, proposed_hash),
                ).fetchone()
                if bound is None:
                    return JSONResponse(status_code=401, content={
                        "detail": "Invalid or expired enrollment token",
                        "code": "enrollment_not_created",
                        "enrollment_request_id": body.enrollment_request_id,
                    })
            raise HTTPException(401, "Invalid or expired enrollment token")

        # Recover only a known receipt; neither a hostname nor a secret alone
        # can discover/reclaim an identity. Unknown tokens avoid the writer lock.
        with database.connection() as db:
            recovered = replay(db)
            if recovered is not None:
                return recovered
            known_token = db.execute("SELECT id FROM enrollment_tokens WHERE token_hash=?", (token_hash,)).fetchone() if token_hash else None
            if known_token is None:
                return reject_token(db)

        agent_id = str(uuid.uuid4())
        agent_secret = body.proposed_agent_secret if recovery else "rpa_" + secrets.token_urlsafe(36)
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            used_at = utcnow()
            recovered = replay(db)
            if recovered is not None:
                return recovered
            token = db.execute("SELECT * FROM enrollment_tokens WHERE token_hash=?", (token_hash,)).fetchone()
            if token is None or token["used_at"] is not None or token["expires_at"] <= used_at:
                return reject_token(db)
            if recovery:
                conflict = db.execute("SELECT 1 FROM agent_enrollment_attempts WHERE secret_hash=?", (proposed_hash,)).fetchone()
                current = db.execute("SELECT 1 FROM agents WHERE secret_hash=? LIMIT 1", (proposed_hash,)).fetchone()
                if conflict is not None or current is not None:
                    raise HTTPException(409, "Proposed enrollment credential is already bound")
            db.execute("UPDATE enrollment_tokens SET used_at=? WHERE id=?", (used_at, token["id"]))
            db.execute(
                """INSERT INTO agents
                   (id,secret_hash,name,hostname,os_family,platform,agent_version,enrolled_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (agent_id, hash_api_secret(agent_secret), metadata["name"], metadata["hostname"],
                 metadata["os_family"], metadata["platform"], metadata["agent_version"], used_at),
            )
            if recovery:
                db.execute("""INSERT INTO agent_enrollment_attempts
                    (request_id_hash,token_id,agent_id,secret_hash,metadata_hash,created_at)
                    VALUES(?,?,?,?,?,?)""", (request_hash,token["id"],agent_id,proposed_hash,metadata_hash,used_at))
            Database.audit(db, actor=f"enrollment:{token['label']}", action="agent_enrolled",
                           target_type="agent", target_id=agent_id,
                           details={"hostname": body.hostname, "os_family": body.os_family})
        response = {"agent_id": agent_id, "agent_secret": agent_secret, "heartbeat_interval": 10}
        if recovery:
            response["enrollment_request_id"] = body.enrollment_request_id
        return response

    @app.post("/api/agent/heartbeat")
    def agent_heartbeat(
        body: HeartbeatRequest,
        request: Request,
        agent: dict[str, Any] = Depends(current_agent),
    ) -> dict[str, Any]:
        heartbeat_counters = [counter.model_dump() for counter in body.workload_counters]
        received_at = heartbeat_received_at()
        now = received_at
        # Validate raw values before numeric conversions or mining-state
        # derivation. JSON integers can exceed the finite float range even
        # when the request itself is small and otherwise well formed.
        _validate_json_payload(body.metrics, "metrics")
        _validate_json_payload(body.inventory, "inventory")
        normalized_metrics = _normalize_metrics(body.metrics)
        # These fields are controller-derived.  Never let an Agent-provided
        # metrics key bypass the state/shape checks below.
        normalized_metrics.pop("hashrate_samples", None)
        normalized_metrics.pop("mining_state", None)
        mining_inventory = body.inventory
        if not isinstance(mining_inventory.get("mining_status"), dict) and isinstance(
            body.metrics.get("mining_status"), dict
        ):
            # Compatibility with early portable Agents that reported this
            # otherwise identical object under metrics instead of inventory.
            mining_inventory = {"mining_status": body.metrics["mining_status"]}
        mining_state, hashrate_samples = _mining_hashrate_samples(
            mining_inventory,
            observed_at=_parse_time(now),
            boot_time=_parse_time(
                normalized_metrics.get("boot_time")
                if isinstance(normalized_metrics.get("boot_time"), str)
                else None
            ),
        )
        if mining_state is not None:
            normalized_metrics["mining_state"] = mining_state
            normalized_metrics["hashrate_samples"] = hashrate_samples
        hashrate_spike_rejections: list[dict[str, Any]] = []
        if hashrate_samples:
            # Second-line amplitude gate before anything is serialized: the
            # kept samples flow unchanged into agents.metrics_json, telemetry
            # rows and hashrate history buckets, while refusals become one
            # audit event inside the heartbeat transaction below.
            with database.connection() as review_db:
                hashrate_samples, hashrate_spike_rejections = _gate_hashrate_samples(
                    review_db, agent["id"], hashrate_samples
                )
            normalized_metrics["hashrate_samples"] = hashrate_samples
        heartbeat_inventory = (
            body.inventory
            if mining_inventory is body.inventory
            else {**body.inventory, **mining_inventory}
        )
        heartbeat_mining = heartbeat_inventory.get("mining_status")
        observed_reported_sheet_id: str | None = None
        observed_explicitly_unconfigured = False
        if isinstance(heartbeat_mining, dict):
            observed_explicitly_unconfigured = (
                heartbeat_mining.get("configured") is False
            )
            reported_sheet = heartbeat_mining.get("flight_sheet_id")
            if (
                isinstance(reported_sheet, str)
                and reported_sheet.strip()
                and len(reported_sheet.strip()) <= 64
            ):
                observed_reported_sheet_id = reported_sheet.strip()
        observed_active_sheet_id = _active_serialized_agent_flight_sheet_id(
            {
                "status": "online",
                "inventory": heartbeat_inventory,
                "metrics": normalized_metrics,
                "last_seen": now,
            }
        )
        _validate_json_payload(normalized_metrics, "metrics")
        _validate_json_payload(body.capabilities, "capabilities", max_bytes=8_192)
        metrics_json = json.dumps(normalized_metrics, separators=(",", ":"), ensure_ascii=False)
        inventory_json = json.dumps(body.inventory, separators=(",", ":"), ensure_ascii=False)
        capabilities_json = json.dumps(body.capabilities, separators=(",", ":"))
        local_ip_addresses, reported_public_ips = _reported_network_addresses(body)
        remote_ip = _canonical_ip(request.client.host) if request.client else None
        observed_public_ips = _network_ip_list(
            [remote_ip] if remote_ip is not None else [], public=True
        )
        public_ip = (observed_public_ips or reported_public_ips or [None])[0]
        local_ip_addresses_json = json.dumps(local_ip_addresses, separators=(",", ":"))
        with database.connection() as db:
            accepted = db.execute(
                """UPDATE agents SET hostname=?, os_family=?, platform=?, agent_version=?,
                   inventory_json=?, metrics_json=?, capabilities_json=?, last_ip=?,
                   public_ip=COALESCE(?, public_ip), local_ip_addresses_json=?,
                   last_seen=? WHERE id=? AND secret_hash=?
                     AND disabled_at IS NULL
                     AND (last_seen IS NULL OR last_seen < ?)""",
                (
                    body.hostname,
                    body.os_family.lower(),
                    body.platform,
                    body.agent_version,
                    inventory_json,
                    metrics_json,
                    capabilities_json,
                    remote_ip,
                    public_ip,
                    local_ip_addresses_json,
                    now,
                    agent["id"],
                    agent["secret_hash"],
                    now,
                ),
            )
            if accepted.rowcount != 1:
                _require_current_agent_identity(db, agent)
                return {
                    "server_time": now,
                    "heartbeat_interval": 10,
                    "commands": [],
                    "stale_heartbeat": True,
                    "workload_counter_acks": [],
                }
            replacement = db.execute(
                """SELECT id, resume_flight_sheet_id, created_by
                   FROM agent_replacement_tokens
                   WHERE target_agent_id=? AND used_at IS NOT NULL
                     AND activated_at IS NULL
                   ORDER BY used_at DESC, created_at DESC LIMIT 1""",
                (agent["id"],),
            ).fetchone()
            if replacement is not None:
                activated = db.execute(
                    """UPDATE agent_replacement_tokens SET activated_at=?
                       WHERE id=? AND activated_at IS NULL""",
                    (now, replacement["id"]),
                )
                if activated.rowcount == 1:
                    resume_sheet_id = replacement["resume_flight_sheet_id"]
                    replacement_agent = db.execute(
                        "SELECT * FROM agents WHERE id=?", (agent["id"],)
                    ).fetchone()
                    resume_sheet = (
                        db.execute(
                            """SELECT fs.*, w.address AS wallet_address,
                                      sw.address AS secondary_wallet_address
                               FROM flight_sheets fs
                               JOIN wallets w ON fs.wallet_id=w.id
                               LEFT JOIN wallets sw
                                 ON fs.secondary_wallet_id=sw.id
                               WHERE fs.id=?""",
                            (resume_sheet_id,),
                        ).fetchone()
                        if resume_sheet_id
                        and replacement_agent is not None
                        and bool(
                            _optional_row_value(
                                replacement_agent,
                                "mining_autostart_enabled",
                                0,
                            )
                        )
                        else None
                    )
                    if resume_sheet is not None:
                        requested_at = now
                        db.execute(
                            """INSERT INTO configuration_refreshes
                               (agent_id,flight_sheet_id,desired_fingerprint,
                                generation_id,claim_token,trigger_type,
                                trigger_id,status,command_id,requested_by,
                                requested_at,updated_at,queued_at,completed_at,
                                attempt_count,last_error)
                               VALUES(?,?,?,?,NULL,'flight_sheet',?,'pending',
                                      NULL,?,?,?,NULL,NULL,0,NULL)
                               ON CONFLICT(agent_id) DO UPDATE SET
                                  flight_sheet_id=excluded.flight_sheet_id,
                                  desired_fingerprint=excluded.desired_fingerprint,
                                  generation_id=excluded.generation_id,
                                  claim_token=NULL,
                                  trigger_type=excluded.trigger_type,
                                  trigger_id=excluded.trigger_id,
                                  status='pending',command_id=NULL,
                                  requested_by=excluded.requested_by,
                                  requested_at=excluded.requested_at,
                                  updated_at=excluded.updated_at,
                                  queued_at=NULL,completed_at=NULL,
                                  attempt_count=0,last_error=NULL""",
                            (
                                agent["id"],
                                resume_sheet_id,
                                _flight_sheet_configuration_fingerprint(
                                    resume_sheet
                                ),
                                uuid.uuid4().hex,
                                f"desired:replacement:{replacement['id']}",
                                replacement["created_by"],
                                requested_at,
                                requested_at,
                            ),
                        )
                    Database.audit(
                        db,
                        actor=f"agent:{agent['id']}",
                        action="agent_identity_replacement_activated",
                        target_type="agent",
                        target_id=agent["id"],
                        details={
                            "replacement_token_id": replacement["id"],
                            "preserved_flight_sheet_id": resume_sheet_id,
                            "automatic_resume_pending": resume_sheet is not None,
                        },
                    )
            expire_stale_agent_updates(db, now)
            expire_stale_agent_commands(
                db,
                agent_id=agent["id"],
                now=now,
            )
            expire_preboot_power_commands(
                db,
                agent_id=agent["id"],
                boot_time=_boot_time_identity(normalized_metrics),
                now=now,
            )
            # Count authenticated target-version health checks, but do not
            # declare success yet.  The new Agent must survive at least two
            # heartbeats and explicitly confirm its activation ID.
            target_updates = db.execute(
                """SELECT command_id, release_id, target_heartbeat_count
                   FROM agent_update_jobs
                   WHERE agent_id=? AND target_version=?
                     AND status='activating'""",
                (agent["id"], body.agent_version),
            ).fetchall()
            for update in target_updates:
                db.execute(
                    """UPDATE agent_update_jobs
                       SET target_heartbeat_count=target_heartbeat_count+1,
                           target_heartbeat_at=?
                       WHERE command_id=?""",
                    (now, update["command_id"]),
                )
                if update["target_heartbeat_count"] == 0:
                    Database.audit(
                        db,
                        actor=f"agent:{agent['id']}",
                        action="agent_update_target_connected",
                        target_type="agent",
                        target_id=agent["id"],
                        details={
                            "command_id": update["command_id"],
                            "release_id": update["release_id"],
                        },
                    )
            record_heartbeat_counters(
                db,
                agent_id=agent["id"],
                received_at=now,
                counters=heartbeat_counters,
            )
            if hashrate_spike_rejections and not normalized_metrics.get(
                "hashrate_samples"
            ):
                # Every sample of this heartbeat was refused by the amplitude
                # gate.  Keep the bucket's last good snapshot instead of
                # overwriting it with an empty list; a bucket whose heartbeats
                # were all refused simply keeps its earlier reading.
                pass
            else:
                _record_hashrate_history_bucket(
                    db,
                    agent_id=agent["id"],
                    recorded_at=now,
                    metrics=normalized_metrics,
                )
            if hashrate_spike_rejections:
                Database.audit(
                    db,
                    actor=f"agent:{agent['id']}",
                    action="hashrate_spike_rejected",
                    target_type="agent",
                    target_id=agent["id"],
                    details={
                        "reason": "hashrate sample amplitude beyond device series band",
                        "rejected_samples": hashrate_spike_rejections,
                        "accepted_samples": len(
                            normalized_metrics.get("hashrate_samples") or []
                        ),
                    },
                )
            db.execute(
                "INSERT INTO telemetry(agent_id, recorded_at, metrics_json) VALUES(?,?,?)",
                (agent["id"], now, metrics_json),
            )
            fresh_agent = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent["id"],)
            ).fetchone()
            if fresh_agent is not None:
                queue_legacy_gpu_oc_migration(
                    db,
                    agent=fresh_agent,
                    reported_settings=_reported_legacy_gpu_oc_settings(
                        body.inventory
                    ),
                )
                # Re-read after migration reconciliation because it can update
                # controller-owned Agent state before autostart evaluates
                # command conflicts and the persisted boot marker.
                fresh_agent = db.execute(
                    "SELECT * FROM agents WHERE id=?", (agent["id"],)
                ).fetchone()
                if fresh_agent is not None:
                    reconcile_mining_autostart(
                        db,
                        agent=fresh_agent,
                        now=now,
                    )
                    fresh_agent = db.execute(
                        "SELECT * FROM agents WHERE id=?", (agent["id"],)
                    ).fetchone()
                    if fresh_agent is not None:
                        fresh_agent = reconcile_gpu_oc_replay_blocks(db, agent_id=agent["id"])
                        if not reconcile_update_oc_recovery(db, agent=fresh_agent):
                            reconcile_pending_desired_gpu_oc(
                                db,
                                agent=fresh_agent,
                            )
                        fresh_agent = db.execute(
                            "SELECT * FROM agents WHERE id=?", (agent["id"],)
                        ).fetchone()
            active_sheet_id = (
                _active_agent_flight_sheet_id(fresh_agent, settings)
                if fresh_agent is not None
                else None
            )
            automatic_commands = db.execute(
                """SELECT id, status, payload_json FROM commands
                   WHERE agent_id=? AND action='apply_flight_sheet'
                     AND status IN ('queued','dispatched','running')
                   ORDER BY created_at, id""",
                (agent["id"],),
            ).fetchall()
            for automatic_command in automatic_commands:
                command_payload = _json(automatic_command["payload_json"], {})
                marker = (
                    command_payload.get("_controller_auto_reapply")
                    if isinstance(command_payload, dict)
                    else None
                )
                if not isinstance(marker, dict):
                    continue
                # Once a command was dispatched, the Agent may already be
                # executing it. Never pretend that a controller-side cancel
                # can revoke it; only queued automatic work is conditional.
                if automatic_command["status"] != "queued":
                    continue
                expected_sheet_id = marker.get("flight_sheet_id")
                desired_apply = marker.get("desired_apply") is True
                fingerprint_matches = False
                if expected_sheet_id:
                    current_sheet = db.execute(
                        """SELECT fs.*, w.address AS wallet_address,
                                  sw.address AS secondary_wallet_address
                           FROM flight_sheets fs
                           JOIN wallets w ON fs.wallet_id=w.id
                           LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                           WHERE fs.id=?""",
                        (expected_sheet_id,),
                    ).fetchone()
                    fingerprint_matches = bool(
                        current_sheet
                        and marker.get("configuration_fingerprint")
                        == _flight_sheet_configuration_fingerprint(current_sheet)
                    )
                safe_to_keep = fingerprint_matches and (
                    desired_apply or active_sheet_id == expected_sheet_id
                )
                conflict = db.execute(
                    """SELECT action FROM commands
                       WHERE agent_id=? AND id<>?
                         AND action IN (
                           'stop_mining','restart_mining','remove_flight_sheet',
                           'restart_service','reboot','shutdown','agent_update.v1',
                           'apply_gpu_profile','migrate_gpu_oc_state.v1',
                           'scan_mining_residue.v1','cleanup_mining_residue.v1',
                           'scan_miner_cache.v1','cleanup_miner_cache.v1',
                           'configure_linux_ssh.v1'
                         )
                         AND status IN ('queued','dispatched','running')
                         AND expires_at >= ?
                       LIMIT 1""",
                    (agent["id"], automatic_command["id"], now),
                ).fetchone()
                if conflict:
                    safe_to_keep = False
                other_applies = db.execute(
                    """SELECT status, payload_json FROM commands
                       WHERE agent_id=? AND id<>? AND action='apply_flight_sheet'
                         AND status IN ('queued','dispatched','running')
                         AND expires_at >= ?""",
                    (agent["id"], automatic_command["id"], now),
                ).fetchall()
                for other_apply in other_applies:
                    other_payload = _json(other_apply["payload_json"], {})
                    other_sheet_id = (
                        other_payload.get("flight_sheet_id")
                        if isinstance(other_payload, dict)
                        else None
                    )
                    if (
                        other_apply["status"] in {"dispatched", "running"}
                        and other_sheet_id == expected_sheet_id
                    ):
                        # Keep the latest command queued behind the in-flight
                        # same-sheet apply; the dispatch query below gates it.
                        continue
                    safe_to_keep = False
                    break
                if safe_to_keep:
                    continue
                db.execute(
                    """UPDATE commands SET status='cancelled', finished_at=?
                       WHERE id=? AND status='queued'""",
                    (now, automatic_command["id"]),
                )
                db.execute(
                    """UPDATE configuration_refreshes
                       SET status='pending', command_id=NULL, updated_at=?,
                           queued_at=NULL, completed_at=NULL,
                           last_error='waiting until the configured miner is active'
                       WHERE command_id=?""",
                    (now, automatic_command["id"]),
                )
                Database.audit(
                    db,
                    actor="controller",
                    action="command_cancelled",
                    target_type="command",
                    target_id=automatic_command["id"],
                    details={
                        "reason": "automatic_reapply_precondition_changed",
                        "expected_flight_sheet_id": expected_sheet_id,
                        "active_flight_sheet_id": active_sheet_id,
                        "configuration_current": fingerprint_matches,
                    },
                )
            reject_incompatible_queued_agent_updates(
                db, agent_id=agent["id"], heartbeat=body, now=now
            )
            reject_miner_cache_commands_without_capability(
                db, agent_id=agent["id"], heartbeat=body, now=now,
            )
            reject_incompatible_queued_flight_sheet_commands(
                db, agent_id=agent["id"], heartbeat=body, now=now,
            )
            reject_unsupported_queued_restarts(
                db, now=now, agent={
                    "id": agent["id"], "os_family": body.os_family,
                    "capabilities_json": capabilities_json,
                    "inventory_json": inventory_json, "metrics_json": metrics_json,
                },
            )
            commands = db.execute(
                f"""SELECT id, action, payload_json, created_at, expires_at FROM commands
                   WHERE agent_id=? AND status IN ('queued','dispatched','running')
                     AND action NOT IN (?, ?) AND expires_at >= ?
                     AND NOT EXISTS (
                         SELECT 1 FROM agent_update_jobs j
                         WHERE j.command_id=commands.id AND j.status='activating'
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM agent_update_jobs j,
                              json_each(j.oc_recovery_json,'$.command_ids') owned
                         WHERE j.agent_id=commands.agent_id AND owned.value=commands.id
                           AND json_extract(j.oc_recovery_json,'$.state')='cancelled'
                     )
                     AND NOT (
                         commands.action='apply_flight_sheet'
                         AND commands.status='queued'
                         AND EXISTS (
                             SELECT 1 FROM commands in_flight
                             WHERE in_flight.agent_id=commands.agent_id
                               AND in_flight.action='apply_flight_sheet'
                               AND in_flight.status IN ('dispatched','running')
                               AND in_flight.id<>commands.id
                               AND in_flight.expires_at >= ?
                         )
                     )
                   ORDER BY
                     CASE
                       WHEN action IN (
                         'stop_mining','restart_mining','remove_flight_sheet',
                         'restart_service','reboot','shutdown'
                       ) THEN 0
                       WHEN status='queued' THEN 1
                       ELSE 2
                     END,
                     created_at, rowid
                   LIMIT ?""",
                (agent["id"], *_RETIRED_WORKSPACE_ACTIONS, now, now, settings.command_batch_size),
            ).fetchall()
            current_dispatch_agent = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent["id"],)
            ).fetchone()
            commands = unsupported_restart_filter(db, current_dispatch_agent, _dispatchable_miner_commands(commands))
            if commands:
                ids = [row["id"] for row in commands]
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"UPDATE commands SET status='dispatched', dispatched_at=? WHERE id IN ({placeholders}) AND status='queued'",
                    (now, *ids),
                )
                db.execute(
                    f"UPDATE agent_update_jobs SET status='dispatched', updated_at=? "
                    f"WHERE command_id IN ({placeholders}) AND status='queued'",
                    (now, *ids),
                )
                db.execute(
                    f"UPDATE configuration_refreshes SET status='dispatched', updated_at=? "
                    f"WHERE command_id IN ({placeholders}) AND status='queued'",
                    (now, *ids),
                )
            workload_counter_acks = acknowledged_heartbeat_counters(
                db, agent_id=agent["id"], counters=heartbeat_counters,
            )

        # Only an accepted, committed heartbeat may change the durable
        # configuration-refresh outbox.  Binding the coordinator to this
        # exact last_seen token also prevents a slower old request from
        # cancelling or queuing work after a newer heartbeat won the CAS.
        reconcile_pending_configuration_refresh(
            agent["id"],
            heartbeat_token=now,
            previous_agent_online=(
                _serialize_agent(agent, settings)["status"] == "online"
            ),
            previous_active_sheet_id=_active_agent_flight_sheet_id(
                agent, settings
            ),
            observed_active_sheet_id=observed_active_sheet_id,
            observed_reported_sheet_id=observed_reported_sheet_id,
            observed_explicitly_unconfigured=observed_explicitly_unconfigured,
        )
        # Reconciliation intentionally runs only after the accepted heartbeat
        # commits.  If it queued a command bound to this exact heartbeat, make
        # that one command visible in the same response instead of imposing an
        # unnecessary third heartbeat.  The last_seen predicate prevents an
        # older request from dispatching after a newer heartbeat won the CAS.
        remaining = max(0, settings.command_batch_size - len(commands))
        if remaining:
            with database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                reconciled_commands = db.execute(
                    """SELECT c.id, c.action, c.payload_json, c.created_at,
                              c.expires_at
                       FROM commands c
                       JOIN configuration_refreshes r ON r.command_id=c.id
                       JOIN agents a ON a.id=c.agent_id
                       WHERE c.agent_id=? AND c.action='apply_flight_sheet'
                         AND c.status='queued' AND c.expires_at >= ?
                         AND r.status='queued' AND a.last_seen=?
                         AND NOT EXISTS (
                           SELECT 1 FROM commands in_flight
                           WHERE in_flight.agent_id=c.agent_id
                             AND in_flight.action='apply_flight_sheet'
                             AND in_flight.status IN ('dispatched','running')
                             AND in_flight.id<>c.id
                             AND in_flight.expires_at >= ?
                         )
                       ORDER BY c.created_at, c.rowid LIMIT ?""",
                    (agent["id"], now, now, now, remaining),
                ).fetchall()
                current_dispatch_agent = db.execute(
                    "SELECT * FROM agents WHERE id=?", (agent["id"],)
                ).fetchone()
                reconciled_commands = unsupported_restart_filter(db, current_dispatch_agent, _dispatchable_miner_commands(reconciled_commands))
                if reconciled_commands:
                    reconciled_ids = [row["id"] for row in reconciled_commands]
                    reconciled_placeholders = ",".join(
                        "?" for _ in reconciled_ids
                    )
                    db.execute(
                        f"""UPDATE commands
                            SET status='dispatched', dispatched_at=?
                            WHERE id IN ({reconciled_placeholders})
                              AND status='queued'""",
                        (now, *reconciled_ids),
                    )
                    db.execute(
                        f"""UPDATE configuration_refreshes
                            SET status='dispatched', updated_at=?
                            WHERE command_id IN ({reconciled_placeholders})
                              AND status='queued'""",
                        (now, *reconciled_ids),
                    )
                    commands = [*commands, *reconciled_commands]
        return {
            "workload_counter_acks": workload_counter_acks,
            "server_time": now,
            "heartbeat_interval": 10,
            "commands": [
                {
                    "id": row["id"],
                    "action": row["action"],
                    "payload": _command_payload_for_agent(row["payload_json"]),
                }
                for row in commands
            ],
        }

    @app.post("/api/agent/commands/{command_id}/result")
    def agent_command_result(
        command_id: str,
        body: CommandResult,
        agent: dict[str, Any] = Depends(current_agent),
    ) -> dict[str, str]:
        _validate_json_payload(body.result, "command result", max_bytes=131_072)
        with database.connection() as db:
            # Result validation may bind current Agent state to a controller
            # desired-state write.  Reserve the writer before reading either
            # record so a concurrent operator edit cannot be overwritten
            # between validation and commit.
            db.execute("BEGIN IMMEDIATE")
            _require_current_agent_identity(db, agent)
            command = db.execute(
                "SELECT * FROM commands WHERE id=? AND agent_id=?",
                (command_id, agent["id"]),
            ).fetchone()
            if not command:
                raise HTTPException(404, "Command not found")
            is_agent_update = command["action"] == AGENT_UPDATE_CAPABILITY
            if is_agent_update:
                job = db.execute(
                    "SELECT * FROM agent_update_jobs WHERE command_id=?",
                    (command_id,),
                ).fetchone()
                if not job:
                    raise HTTPException(409, "Agent update ledger is missing")
                now = utcnow()
                if body.status == "failed":
                    if command["status"] in {"succeeded", "failed", "cancelled"}:
                        return {"status": command["status"]}
                    if command["status"] == "expired" and (
                        job["activation_id"] is None
                        or body.result.get("rolled_back") is not True
                    ):
                        return {"status": "expired"}
                    activation_id = body.result.get("activation_id")
                    if job["activation_id"] is not None and (
                        set(body.result) != {"activation_id", "version", "rolled_back"}
                        or activation_id != job["activation_id"]
                        or body.result.get("version") != job["target_version"]
                        or body.result.get("rolled_back") is not True
                    ):
                        raise HTTPException(409, "Agent update failure binding is invalid")
                    db.execute(
                        """UPDATE commands SET status='failed', result_json=?,
                           error=?, finished_at=? WHERE id=?""",
                        (
                            json.dumps(
                                body.result, separators=(",", ":"), ensure_ascii=False
                            ),
                            body.error,
                            now,
                            command_id,
                        ),
                    )
                    db.execute(
                        """UPDATE agent_update_jobs
                           SET status='failed', updated_at=?, completed_at=?,
                               last_error=? WHERE command_id=?""",
                        (now, now, body.error, command_id),
                    )
                    Database.audit(
                        db,
                        actor=f"agent:{agent['id']}",
                        action=(
                            "agent_update_rollback_reported"
                            if body.result.get("rolled_back") is True
                            else "agent_update_failed"
                        ),
                        target_type="agent",
                        target_id=agent["id"],
                        details={
                            "command_id": command_id,
                            "activation_id": job["activation_id"],
                            "rolled_back": body.result.get("rolled_back") is True,
                            "error": body.error,
                        },
                    )
                    return {"status": "failed"}
                if command["status"] in {"succeeded", "failed", "cancelled", "expired"}:
                    return {"status": command["status"]}
                if body.status == "running":
                    db.execute(
                        """UPDATE commands SET status='running', result_json=?,
                           error=NULL, finished_at=NULL WHERE id=?""",
                        (
                            json.dumps(
                                body.result, separators=(",", ":"), ensure_ascii=False
                            ),
                            command_id,
                        ),
                    )
                    return {"status": "running"}

                if body.result.get("activation_confirmed") is True:
                    expected_fields = {
                        "activation_confirmed",
                        "activation_id",
                        "version",
                        "health_heartbeats",
                    }
                    if (
                        set(body.result) != expected_fields
                        or body.result.get("activation_id") != job["activation_id"]
                        or body.result.get("version") != job["target_version"]
                        or isinstance(body.result.get("health_heartbeats"), bool)
                        or not isinstance(body.result.get("health_heartbeats"), int)
                        or body.result["health_heartbeats"] < 2
                        or job["target_heartbeat_count"] < 2
                        or job["status"] != "activating"
                    ):
                        raise HTTPException(
                            409, "Agent update activation confirmation is not ready"
                        )
                    current = db.execute(
                        "SELECT * FROM agents WHERE id=?", (agent["id"],)
                    ).fetchone()
                    if not current:
                        raise HTTPException(
                            409, "Target Agent version has not reconnected"
                        )
                    # R2 (0.8.44): the two authenticated target-version
                    # heartbeats counted above are the authoritative proof
                    # that the replacement reconnected.  A late in-flight
                    # heartbeat from the pre-update process can transiently
                    # overwrite agents.agent_version back to the source
                    # version; rejecting the confirmation for that one-beat
                    # row lag produced retryable 409 noise, so the version
                    # row is no longer treated as a hard gate here.
                    recovery = _json(job["oc_recovery_json"], None)
                    if isinstance(recovery, dict) and recovery.get("state") == "armed":
                        recovery.update(
                            state="waiting",
                            checked_at=update_oc_mining_observation(current).get("checked_at"),
                            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                        )
                        db.execute("UPDATE agent_update_jobs SET oc_recovery_json=? WHERE command_id=?",
                                   (json.dumps(recovery, separators=(",", ":")), command_id))
                    db.execute(
                        """UPDATE commands SET status='succeeded', result_json=?,
                           error=NULL, finished_at=? WHERE id=?""",
                        (
                            json.dumps(
                                body.result, separators=(",", ":"), ensure_ascii=False
                            ),
                            now,
                            command_id,
                        ),
                    )
                    db.execute(
                        """UPDATE agent_update_jobs
                           SET status='succeeded', updated_at=?, completed_at=?,
                               last_error=NULL WHERE command_id=?""",
                        (now, now, command_id),
                    )
                    Database.audit(
                        db,
                        actor=f"agent:{agent['id']}",
                        action="agent_update_succeeded",
                        target_type="agent",
                        target_id=agent["id"],
                        details={
                            "command_id": command_id,
                            "release_id": job["release_id"],
                            "source_version": job["source_version"],
                            "target_version": job["target_version"],
                            "activation_id": job["activation_id"],
                            "health_heartbeats": job["target_heartbeat_count"],
                        },
                    )
                    return {"status": "succeeded"}

                staged_fields = {
                    "state",
                    "release_id",
                    "version",
                    "sha256",
                    "activation_id",
                    "activation_scheduled",
                    "restart_required",
                    "miner_continuity_checked",
                }
                activation_id = body.result.get("activation_id")
                release = db.execute(
                    "SELECT sha256 FROM agent_releases WHERE id=?", (job["release_id"],)
                ).fetchone()
                if (
                    set(body.result) != staged_fields
                    or body.result.get("state") != "staged"
                    or body.result.get("release_id") != job["release_id"]
                    or body.result.get("version") != job["target_version"]
                    or not release
                    or body.result.get("sha256") != release["sha256"]
                    or not isinstance(activation_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", activation_id) is None
                    or body.result.get("activation_scheduled") is not True
                    or body.result.get("restart_required") is not True
                    or body.result.get("miner_continuity_checked") is not True
                ):
                    raise HTTPException(409, "Agent update staged result is invalid")
                if job["status"] == "activating":
                    if job["activation_id"] == activation_id:
                        return {"status": "running"}
                    raise HTTPException(409, "Agent update activation ID changed")
                db.execute(
                    """UPDATE commands SET status='running', result_json=?,
                       error=NULL, finished_at=NULL WHERE id=?""",
                    (
                        json.dumps(
                            body.result, separators=(",", ":"), ensure_ascii=False
                        ),
                        command_id,
                    ),
                )
                db.execute(
                    """UPDATE agent_update_jobs
                       SET status='activating', activation_id=?, updated_at=?,
                           target_heartbeat_count=0, target_heartbeat_at=NULL,
                           stalled_at=NULL, completed_at=NULL,
                           last_error=NULL WHERE command_id=?""",
                    (activation_id, now, command_id),
                )
                Database.audit(
                    db,
                    actor=f"agent:{agent['id']}",
                    action="agent_update_activating",
                    target_type="agent",
                    target_id=agent["id"],
                    details={
                        "command_id": command_id,
                        "activation_id": activation_id,
                    },
                )
                return {"status": "running"}
            if command["status"] in {"succeeded", "failed", "cancelled", "expired"}:
                return {"status": command["status"]}
            stored_status = body.status
            migrated_desired_gpu_oc: list[dict[str, Any]] | None = None
            migration_marker_update: dict[str, Any] | None = None
            reset_gpu_oc_uuid: str | None = None
            command_payload = _json(command["payload_json"], {})
            capabilities = _json(
                _optional_row_value(agent, "capabilities_json", "[]"),
                [],
            )
            if (
                stored_status == "succeeded"
                and command["action"]
                in {"apply_flight_sheet", *MINING_CONTROL_ACTIONS}
                and isinstance(capabilities, list)
                and MINING_RESULT_BINDING_CAPABILITY in capabilities
                and (
                    command["action"] == "apply_flight_sheet"
                    or CONTROLLER_MINING_RESULT_BINDING_MARKER
                    in command_payload
                )
                and not _successful_mining_result_is_bound(
                    command["action"],
                    command_payload,
                    body.result,
                )
            ):
                raise HTTPException(
                    409,
                    "Mining command success result binding is invalid",
                )
            if command["action"] in MINER_CACHE_ACTIONS and stored_status == "running" and body.result:
                raise HTTPException(409, "Miner cache progress result must be empty")
            if (
                command["action"] == MINER_CACHE_SCAN_ACTION
                and stored_status in {"succeeded", "failed"}
                and (stored_status == "succeeded" or body.result)
                and not _miner_cache_scan_result_is_valid(body.result)
            ):
                raise HTTPException(409, "Miner cache scan result is invalid")
            if (
                command["action"] == MINER_CACHE_CLEANUP_ACTION
                and stored_status in {"succeeded", "failed"}
                and not (stored_status == "failed" and not body.result)
            ):
                scan = _cache_scan_for_cleanup(
                    db,
                    agent_id=agent["id"],
                    created_by=command["created_by"],
                    fingerprint=command_payload.get("scan_fingerprint", ""),
                    before_created_at=command["created_at"],
                )
                if scan is None or not _miner_cache_cleanup_result_is_bound(
                    command_payload,
                    body.result,
                    succeeded=stored_status == "succeeded",
                    scan=_json(scan["result_json"], {}),
                ):
                    raise HTTPException(409, "Miner cache cleanup result binding is invalid")
            if (
                command["action"] == MINING_RESIDUE_SCAN_ACTION
                and stored_status == "succeeded"
                and not _mining_residue_scan_result_is_valid(body.result)
            ):
                raise HTTPException(
                    409,
                    "Mining residue scan success result binding is invalid",
                )
            if (
                command["action"] == MINING_RESIDUE_CLEANUP_ACTION
                and stored_status == "succeeded"
                and not _mining_residue_cleanup_result_is_bound(
                    command_payload,
                    body.result,
                )
            ):
                raise HTTPException(
                    409,
                    "Mining residue cleanup success result binding is invalid",
                )
            if (
                command["action"] == "apply_gpu_profile"
                and stored_status == "succeeded"
                and not _gpu_oc_success_result_is_bound(
                    command_payload,
                    body.result,
                )
            ):
                raise HTTPException(
                    409,
                    "GPU OC success result binding is invalid",
                )
            if (
                command["action"] == "apply_gpu_profile"
                and isinstance(command_payload, dict)
                and command_payload.get("mode") == "reset_one"
                and isinstance(command_payload.get("expected_gpu_uuid"), str)
            ):
                reset_gpu_oc_uuid = command_payload["expected_gpu_uuid"]
            if command["action"] == GPU_OC_STATE_MIGRATION_ACTION:
                latest_agent = db.execute(
                    """SELECT metrics_json, desired_gpu_oc_json,
                              gpu_oc_migration_marker_json
                       FROM agents WHERE id=?""",
                    (agent["id"],),
                ).fetchone()
                command_payload = _json(command["payload_json"], {})
                state_fingerprint = _migration_payload_state_fingerprint(
                    command_payload
                )
                current_marker = _json(
                    latest_agent["gpu_oc_migration_marker_json"]
                    if latest_agent
                    else "{}",
                    {},
                )
                marker_matches = bool(
                    isinstance(current_marker, dict)
                    and current_marker.get("status") == "queued"
                    and current_marker.get("command_id") == command_id
                    and state_fingerprint is not None
                    and current_marker.get("state_fingerprint")
                    == state_fingerprint
                )
                if stored_status in {"succeeded", "failed"} and not marker_matches:
                    raise HTTPException(409, "GPU OC migration marker binding is invalid")
                if stored_status == "succeeded":
                    migrated_desired_gpu_oc = _migration_desired_gpu_oc(
                        command_payload,
                        body.result,
                        _json(latest_agent["metrics_json"], {})
                        if latest_agent
                        else {},
                    )
                    if migrated_desired_gpu_oc is None:
                        raise HTTPException(
                            409, "GPU OC migration result binding is invalid"
                        )
                    if _desired_gpu_oc_entries(
                        latest_agent["desired_gpu_oc_json"] if latest_agent else "[]"
                    ):
                        raise HTTPException(
                            409, "GPU OC desired state changed during migration"
                        )
                if stored_status in {"succeeded", "failed"}:
                    migration_marker_update = {
                        "state_fingerprint": state_fingerprint,
                        "status": stored_status,
                        "command_id": command_id,
                        "updated_at": utcnow(),
                    }
            stored_error = body.error
            if command["action"] in MINER_CACHE_ACTIONS and stored_status == "failed":
                reason = body.result.get("reason") if isinstance(body.result, dict) else None
                stored_error = reason if reason is not None and _cache_reason(reason) else "miner_cache_operation_failed"
            finished_at = (
                utcnow() if stored_status in {"succeeded", "failed"} else None
            )
            if stored_status == "running":
                _renew_running_command_lease(db, command)
            db.execute(
                """UPDATE commands SET status=?, result_json=?, error=?, finished_at=?
                   WHERE id=?""",
                (
                    stored_status,
                    json.dumps(body.result, separators=(",", ":"), ensure_ascii=False),
                    stored_error,
                    finished_at,
                    command_id,
                ),
            )
            autostart_marker = (
                command_payload.get(CONTROLLER_MINING_AUTOSTART_MARKER)
                if (
                    command["action"] == "restart_mining"
                    and isinstance(command_payload, dict)
                )
                else None
            )
            if (
                stored_status == "succeeded"
                and isinstance(autostart_marker, dict)
                and isinstance(autostart_marker.get("boot_time"), str)
                and isinstance(autostart_marker.get("previous_boot_time"), str)
            ):
                latest_agent = db.execute(
                    """SELECT metrics_json, mining_autostart_last_boot_time
                       FROM agents WHERE id=?""",
                    (agent["id"],),
                ).fetchone()
                reported_boot = (
                    _boot_time_identity(_json(latest_agent["metrics_json"], {}))
                    if latest_agent is not None
                    else None
                )
                target_boot = autostart_marker["boot_time"]
                previous_boot = autostart_marker["previous_boot_time"]
                if reported_boot != target_boot:
                    raise HTTPException(
                        409,
                        "Mining autostart result no longer matches the current host boot",
                    )
                current_marker = latest_agent["mining_autostart_last_boot_time"]
                if current_marker == previous_boot:
                    advanced = db.execute(
                        """UPDATE agents SET mining_autostart_last_boot_time=?
                           WHERE id=? AND mining_autostart_last_boot_time=?""",
                        (target_boot, agent["id"], previous_boot),
                    )
                    marker_confirmed = advanced.rowcount == 1
                else:
                    # A heartbeat may prove the restarted miner live before
                    # its command result POST arrives.  In that order the
                    # normal active-miner reconciliation already advances the
                    # same marker, so accepting target==current is idempotent.
                    marker_confirmed = current_marker == target_boot
                if not marker_confirmed:
                    raise HTTPException(
                        409,
                        "Mining autostart boot marker changed before restart completed",
                    )
                Database.audit(
                    db,
                    actor=f"agent:{agent['id']}",
                    action="mining_autostart_confirmed",
                    target_type="agent",
                    target_id=agent["id"],
                    details={
                        "command_id": command_id,
                        "boot_time": target_boot,
                    },
                )
            if stored_status == "succeeded" and reset_gpu_oc_uuid is not None:
                latest_agent = db.execute(
                    """SELECT desired_gpu_oc_json, desired_gpu_oc_pending_json
                       FROM agents WHERE id=?""",
                    (agent["id"],),
                ).fetchone()
                desired = _desired_gpu_oc_entries(
                    latest_agent["desired_gpu_oc_json"] if latest_agent else "[]"
                )
                pending = _desired_gpu_oc_pending_uuids(
                    latest_agent["desired_gpu_oc_pending_json"]
                    if latest_agent
                    else "[]"
                )
                remaining = [
                    entry
                    for entry in desired
                    if entry["gpu_uuid"] != reset_gpu_oc_uuid
                    or not _gpu_oc_command_matches_intent(command_payload, entry)
                ]
                if len(remaining) != len(desired):
                    db.execute(
                        """UPDATE agents
                           SET desired_gpu_oc_json=?, desired_gpu_oc_pending_json=?
                           WHERE id=?""",
                        (
                            json.dumps(
                                remaining,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            json.dumps(
                                sorted(
                                    gpu_uuid
                                    for gpu_uuid in pending
                                    if gpu_uuid != reset_gpu_oc_uuid
                                ),
                                separators=(",", ":"),
                            ),
                            agent["id"],
                        ),
                    )
                    Database.audit(
                        db,
                        actor=f"agent:{agent['id']}",
                        action="desired_gpu_oc_deleted",
                        target_type="agent",
                        target_id=agent["id"],
                        details={
                            "command_id": command_id,
                            "gpu_uuid": reset_gpu_oc_uuid,
                            "reset_succeeded": True,
                        },
                    )
            if (
                command["action"] == "apply_gpu_profile"
                and isinstance(command_payload, dict)
                and command_payload.get("mode") in {"replace_one", "reset_one"}
                and isinstance(command_payload.get("expected_gpu_uuid"), str)
                and stored_status in {"succeeded", "failed"}
            ):
                cancelled_update_event = db.execute(
                    """SELECT 1 FROM agent_update_jobs j,
                         json_each(j.oc_recovery_json,'$.command_ids') owned
                       WHERE j.agent_id=? AND owned.value=?
                         AND json_extract(j.oc_recovery_json,'$.state')='cancelled'
                       LIMIT 1""", (agent["id"], command_id),
                ).fetchone() is not None
                applied_gpu_uuid = command_payload["expected_gpu_uuid"]
                latest_agent = db.execute(
                    "SELECT desired_gpu_oc_json, desired_gpu_oc_pending_json FROM agents WHERE id=?",
                    (agent["id"],),
                ).fetchone()
                desired = _desired_gpu_oc_entries(latest_agent["desired_gpu_oc_json"] if latest_agent else "[]")
                desired_by_uuid = {entry["gpu_uuid"]: entry for entry in desired}
                current_entry = desired_by_uuid.get(applied_gpu_uuid)
                pending_uuids = set(_desired_gpu_oc_pending_uuids(
                    latest_agent["desired_gpu_oc_pending_json"] if latest_agent else "[]"
                )).intersection(desired_by_uuid)
                if current_entry is None:
                    pending_uuids.discard(applied_gpu_uuid)
                elif _gpu_oc_command_matches_intent(command_payload, current_entry):
                    block = _gpu_oc_failure_replay_block(
                        command, command_payload, body.result, body.error, current_entry,
                    ) if stored_status == "failed" else None
                    if block is not None:
                        current_entry["intent_id"] = block["intent_id"]
                        current_entry["replay_block"] = block
                        Database.audit(
                            db, actor=f"agent:{agent['id']}", action="desired_gpu_oc_replay_blocked",
                            target_type="agent", target_id=agent["id"],
                            details={"gpu_uuid": applied_gpu_uuid, "command_id": command_id, "reason": block["reason"]},
                        )
                    # Do not revive a cancelled event from a late ordinary
                    # receipt. A matching unsafe receipt may still hold intent.
                    if not cancelled_update_event or block is not None:
                        if stored_status == "failed" and not _gpu_oc_replay_is_blocked(current_entry):
                            if command_payload["mode"] == "replace_one":
                                pending_uuids.add(applied_gpu_uuid)
                        else:
                            pending_uuids.discard(applied_gpu_uuid)
                pending_uuids -= {entry["gpu_uuid"] for entry in desired if _gpu_oc_replay_is_blocked(entry)}
                db.execute(
                    "UPDATE agents SET desired_gpu_oc_json=?, desired_gpu_oc_pending_json=? WHERE id=?",
                    (json.dumps(desired, separators=(",", ":"), ensure_ascii=False), json.dumps(sorted(pending_uuids)), agent["id"]),
                )
            if command["action"] == "apply_flight_sheet":
                refresh_error = (
                    body.error
                    if isinstance(body.error, str)
                    else ""
                )
                retryable_failure = (
                    stored_status == "failed"
                    and _configuration_refresh_error_is_retryable(
                        409,
                        refresh_error,
                    )
                )
                if retryable_failure:
                    db.execute(
                        """UPDATE configuration_refreshes
                           SET status='pending', command_id=NULL, updated_at=?,
                               queued_at=NULL, completed_at=NULL, last_error=?
                           WHERE command_id=?""",
                        (utcnow(), refresh_error, command_id),
                    )
                else:
                    completed_at = (
                        utcnow()
                        if stored_status in {"succeeded", "failed"}
                        else None
                    )
                    db.execute(
                        """UPDATE configuration_refreshes
                           SET status=?, updated_at=?, completed_at=?,
                               last_error=?
                           WHERE command_id=?""",
                        (
                            stored_status,
                            utcnow(),
                            completed_at,
                            (
                                refresh_error
                                if stored_status == "failed"
                                else None
                            ),
                            command_id,
                        ),
                    )
            if migrated_desired_gpu_oc is not None:
                db.execute(
                    """UPDATE agents
                       SET desired_gpu_oc_json=?, gpu_oc_migration_marker_json=?
                       WHERE id=?""",
                    (
                        json.dumps(
                            migrated_desired_gpu_oc,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            migration_marker_update,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        agent["id"],
                    ),
                )
                Database.audit(
                    db,
                    actor=f"agent:{agent['id']}",
                    action="legacy_gpu_oc_migration_succeeded",
                    target_type="agent",
                    target_id=agent["id"],
                    details={
                        "command_id": command_id,
                        "history_command_ids": _json(
                            command["payload_json"], {}
                        ).get("history_command_ids", []),
                        "gpu_uuids": [
                            entry["gpu_uuid"]
                            for entry in migrated_desired_gpu_oc
                        ],
                        "desired_state_restored": True,
                        "nvml_writes": 0,
                    },
                )
            elif migration_marker_update is not None:
                db.execute(
                    "UPDATE agents SET gpu_oc_migration_marker_json=? WHERE id=?",
                    (
                        json.dumps(
                            migration_marker_update,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        agent["id"],
                    ),
                )
                Database.audit(
                    db,
                    actor=f"agent:{agent['id']}",
                    action="legacy_gpu_oc_migration_failed",
                    target_type="agent",
                    target_id=agent["id"],
                    details={
                        "command_id": command_id,
                        "error": body.error,
                        "desired_state_restored": False,
                    },
                )
            desired_gpu_oc_commands: list[dict[str, Any]] = []
            if stored_status == "succeeded" and command["action"] in {
                "stop_mining",
                "remove_flight_sheet",
                "reboot",
                "shutdown",
            }:
                latest_agent = db.execute(
                    "SELECT desired_gpu_oc_json FROM agents WHERE id=?",
                    (agent["id"],),
                ).fetchone()
                desired_uuids = [
                    entry["gpu_uuid"]
                    for entry in _desired_gpu_oc_entries(
                        latest_agent["desired_gpu_oc_json"]
                        if latest_agent
                        else "[]"
                    )
                    if not _gpu_oc_replay_is_blocked(entry)
                ]
                db.execute(
                    "UPDATE agents SET desired_gpu_oc_pending_json=? WHERE id=?",
                    (
                        json.dumps(sorted(desired_uuids), separators=(",", ":")),
                        agent["id"],
                    ),
                )
            if stored_status == "succeeded" and command["action"] in {
                "apply_flight_sheet",
                "restart_mining",
            }:
                desired_gpu_oc_commands = queue_desired_gpu_oc_reapply(
                    db,
                    agent_id=agent["id"],
                    source_command=command,
                )
            if not command["is_transient"]:
                Database.audit(
                    db,
                    actor=f"agent:{agent['id']}",
                    action=f"command_{stored_status}",
                    target_type="command",
                    target_id=command_id,
                    details={
                        "action": command["action"],
                        "error": stored_error,
                        "desired_gpu_oc_command_ids": [
                            item["id"] for item in desired_gpu_oc_commands
                        ],
                    },
                )
        return {"status": stored_status}

    @app.get("/api/wallets")
    def list_wallets(
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, Any]]:
        del user
        with database.connection() as db:
            rows = db.execute(
                """SELECT w.*, u.username AS created_by_name
                   FROM wallets w JOIN users u ON w.created_by=u.id
                   ORDER BY w.coin, w.name, w.created_at"""
            ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "coin": row["coin"],
                "address": row["address"],
                "note": row["note"],
                "created_by": row["created_by_name"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    @app.post("/api/wallets", status_code=201)
    def create_wallet(
        body: WalletCreate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        wallet_id = str(uuid.uuid4())
        now = utcnow()
        with database.connection() as db:
            db.execute(
                """INSERT INTO wallets
                   (id, name, coin, address, note, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    wallet_id,
                    body.name,
                    body.coin,
                    body.address,
                    body.note,
                    user["id"],
                    now,
                    now,
                ),
            )
            Database.audit(
                db,
                actor=user["username"],
                action="wallet_created",
                target_type="wallet",
                target_id=wallet_id,
                details={"name": body.name, "coin": body.coin},
            )
        return {
            "id": wallet_id,
            "name": body.name,
            "coin": body.coin,
            "address": body.address,
            "note": body.note,
            "created_by": user["username"],
            "created_at": now,
            "updated_at": now,
        }

    @app.put("/api/wallets/{wallet_id}")
    def update_wallet(
        wallet_id: str,
        body: WalletUpdate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        now = utcnow()
        changed_fields: list[str] = []
        sheet_ids: list[str] = []
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            wallet = db.execute(
                """SELECT w.*, u.username AS created_by_name
                   FROM wallets w JOIN users u ON w.created_by=u.id
                   WHERE w.id=?""",
                (wallet_id,),
            ).fetchone()
            if not wallet:
                raise HTTPException(404, "Wallet not found")
            sheet_rows = db.execute(
                """SELECT id FROM flight_sheets
                   WHERE wallet_id=? OR secondary_wallet_id=?""",
                (wallet_id, wallet_id),
            ).fetchall()
            sheet_ids = [row["id"] for row in sheet_rows]
            if sheet_ids and body.coin != wallet["coin"].upper():
                raise HTTPException(
                    409,
                    "Wallet coin cannot be changed while it is used by a flight sheet",
                )
            changed_fields = [
                field
                for field, value in (
                    ("name", body.name),
                    ("coin", body.coin),
                    ("address", body.address),
                    ("note", body.note),
                )
                if value != wallet[field]
            ]
            if changed_fields:
                db.execute(
                    """UPDATE wallets SET name=?, coin=?, address=?, note=?, updated_at=?
                       WHERE id=?""",
                    (body.name, body.coin, body.address, body.note, now, wallet_id),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="wallet_updated",
                    target_type="wallet",
                    target_id=wallet_id,
                    details={
                        "name": body.name,
                        "coin": body.coin,
                        "changed_fields": changed_fields,
                        "flight_sheet_references": len(sheet_ids),
                    },
                )
                if {"coin", "address"}.intersection(changed_fields):
                    _persist_configuration_refreshes(
                        db,
                        set(sheet_ids),
                        user,
                        trigger_type="wallet",
                        trigger_id=wallet_id,
                    )
        effective_change = bool({"coin", "address"}.intersection(changed_fields))
        reapply = (
            reapply_active_flight_sheets(
                sheet_ids,
                user,
                trigger_type="wallet",
                trigger_id=wallet_id,
                persist=False,
            )
            if effective_change
            else configuration_reapply_result(triggered=False)
        )
        return {
            "id": wallet_id,
            "name": body.name,
            "coin": body.coin,
            "address": body.address,
            "note": body.note,
            "created_by": wallet["created_by_name"],
            "created_at": wallet["created_at"],
            "updated_at": now if changed_fields else wallet["updated_at"],
            "changed_fields": changed_fields,
            "reapply": reapply,
        }

    @app.delete("/api/wallets/{wallet_id}")
    def delete_wallet(
        wallet_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        with database.connection() as db:
            wallet = db.execute("SELECT * FROM wallets WHERE id=?", (wallet_id,)).fetchone()
            if not wallet:
                raise HTTPException(404, "Wallet not found")
            referenced = db.execute(
                """SELECT COUNT(*) AS count FROM flight_sheets
                   WHERE wallet_id=? OR secondary_wallet_id=?""",
                (wallet_id, wallet_id),
            ).fetchone()["count"]
            if referenced:
                raise HTTPException(409, "Wallet is used by a flight sheet")
            try:
                db.execute("DELETE FROM wallets WHERE id=?", (wallet_id,))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Wallet is used by a flight sheet") from exc
            Database.audit(
                db,
                actor=user["username"],
                action="wallet_deleted",
                target_type="wallet",
                target_id=wallet_id,
                details={"name": wallet["name"], "coin": wallet["coin"]},
            )
        return {"status": "deleted"}

    def serialize_pool(row: sqlite3.Row | dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        protocol = urlparse(row["pool_url"]).scheme.lower()
        return {
            "id": row["id"], "name": row["name"], "coin": row["coin"],
            "pool_url": row["pool_url"], "protocol": protocol,
            "tls": protocol in {"stratum+ssl", "stratum+tls"},
            "pool_password": row["pool_password"] if ROLE_LEVEL[user["role"]] >= ROLE_LEVEL["operator"] else None,
            "pool_password_configured": bool(row["pool_password"]),
            "note": row["note"], "created_by": row["created_by_name"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @app.get("/api/pools")
    def list_pools(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
        with database.connection() as db:
            rows = db.execute(
                """SELECT p.*, u.username AS created_by_name
                   FROM pools p JOIN users u ON p.created_by=u.id
                   ORDER BY p.coin, p.name, p.created_at"""
            ).fetchall()
        return [serialize_pool(row, user) for row in rows]

    @app.post("/api/pools", status_code=201)
    def create_pool(
        body: PoolCreate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        pool_url = _validate_saved_pool_url(body.pool_url)
        pool_id = str(uuid.uuid4())
        now = utcnow()
        with database.connection() as db:
            db.execute(
                """INSERT INTO pools
                   (id,name,coin,pool_url,pool_password,note,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (pool_id, body.name, body.coin, pool_url, body.pool_password,
                 body.note, user["id"], now, now),
            )
            Database.audit(
                db, actor=user["username"], action="pool_created",
                target_type="pool", target_id=pool_id,
                details={"name": body.name, "coin": body.coin},
            )
        row = {"id": pool_id, "name": body.name, "coin": body.coin,
               "pool_url": pool_url, "pool_password": body.pool_password,
               "note": body.note, "created_by_name": user["username"],
               "created_at": now, "updated_at": now}
        return serialize_pool(row, user)

    @app.put("/api/pools/{pool_id}")
    def update_pool(
        pool_id: str,
        body: PoolUpdate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        pool_url = _validate_saved_pool_url(body.pool_url)
        now = utcnow()
        with database.connection() as db:
            row = db.execute(
                """SELECT p.*, u.username AS created_by_name
                   FROM pools p JOIN users u ON p.created_by=u.id WHERE p.id=?""",
                (pool_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Pool not found")
            changed = [field for field, value in (
                ("name", body.name), ("coin", body.coin),
                ("pool_url", pool_url), ("pool_password", body.pool_password),
                ("note", body.note),
            ) if value != row[field]]
            if changed:
                db.execute(
                    """UPDATE pools SET name=?,coin=?,pool_url=?,pool_password=?,note=?,updated_at=?
                       WHERE id=?""",
                    (body.name, body.coin, pool_url, body.pool_password,
                     body.note, now, pool_id),
                )
                Database.audit(
                    db, actor=user["username"], action="pool_updated",
                    target_type="pool", target_id=pool_id,
                    details={"name": body.name, "coin": body.coin,
                             "changed_fields": changed},
                )
        result = {"id": pool_id, "name": body.name, "coin": body.coin,
                  "pool_url": pool_url, "pool_password": body.pool_password,
                  "note": body.note, "created_by_name": row["created_by_name"],
                  "created_at": row["created_at"],
                  "updated_at": now if changed else row["updated_at"]}
        return {**serialize_pool(result, user), "changed_fields": changed}

    @app.delete("/api/pools/{pool_id}")
    def delete_pool(
        pool_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        with database.connection() as db:
            row = db.execute("SELECT name,coin FROM pools WHERE id=?", (pool_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Pool not found")
            db.execute("DELETE FROM pools WHERE id=?", (pool_id,))
            Database.audit(
                db, actor=user["username"], action="pool_deleted",
                target_type="pool", target_id=pool_id,
                details={"name": row["name"], "coin": row["coin"]},
            )
        return {"status": "deleted"}

    def serialize_agent_release(
        row: sqlite3.Row | dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "version": row["version"],
            "platform": row["platform"],
            "architecture": row["architecture"],
            "file_name": row["file_name"],
            "size": row["file_size"],
            "sha256": row["sha256"],
            "update_protocol": _optional_row_value(row, "update_protocol"),
            "deployable": _optional_row_value(row, "update_protocol") == AGENT_UPDATE_CAPABILITY,
            "notes": row["notes"],
            "created_by": _optional_row_value(row, "created_by_name"),
            "created_at": row["created_at"],
            "download_url": f"/api/agent-releases/{row['id']}/download",
        }

    def serialize_agent_update(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        activate_workspace = bool(
            _optional_row_value(row, "activate_remote_workspace", 0)
        )
        workspace_status = "not_requested"
        if activate_workspace:
            post_status = _optional_row_value(row, "post_update_command_status")
            if row["status"] == "failed":
                workspace_status = "failed"
            elif row["status"] == "succeeded":
                workspace_status = "succeeded"
            elif not _optional_row_value(row, "post_update_command_id"):
                workspace_status = "waiting_for_agent_update"
            elif post_status in {"queued", "dispatched", "running"}:
                workspace_status = "activating"
            elif post_status == "succeeded":
                workspace_status = "waiting_for_capabilities"
            elif post_status in {"failed", "cancelled", "expired"}:
                workspace_status = "failed"
        return {
            "command_id": row["command_id"],
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "release_id": row["release_id"],
            "source_version": row["source_version"],
            "target_version": row["target_version"],
            "status": row["status"],
            "command_status": row["command_status"],
            "file_name": row["file_name"],
            "sha256": row["sha256"],
            "size": row["file_size"],
            "created_by": row["created_by_name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "downloaded_at": row["downloaded_at"],
            "activation_id": row["activation_id"],
            "target_heartbeat_count": row["target_heartbeat_count"],
            "target_heartbeat_at": _optional_row_value(
                row, "target_heartbeat_at"
            ),
            "stalled_at": _optional_row_value(row, "stalled_at"),
            "activation_stalled": bool(
                row["status"] == "activating"
                and _optional_row_value(row, "stalled_at")
            ),
            "completed_at": row["completed_at"],
            "error": row["last_error"],
            "activate_remote_workspace": activate_workspace,
            "post_update_command_id": _optional_row_value(
                row, "post_update_command_id"
            ),
            "post_update_command_status": _optional_row_value(
                row, "post_update_command_status"
            ),
            "workspace_activation_status": workspace_status,
        }

    @app.get("/api/agent-releases")
    def list_agent_releases(
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> list[dict[str, Any]]:
        del user
        with database.connection() as db:
            rows = db.execute(
                """SELECT ar.*, u.username AS created_by_name
                   FROM agent_releases ar JOIN users u ON ar.created_by=u.id
                   ORDER BY ar.created_at DESC, ar.version DESC"""
            ).fetchall()
        return [serialize_agent_release(row) for row in rows]

    @app.get("/api/agent-updates")
    def list_agent_updates(
        limit: int = Query(default=100, ge=1, le=500),
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> list[dict[str, Any]]:
        del user
        now = utcnow()
        activation_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=AGENT_UPDATE_ACTIVATION_MAX_MINUTES)
        ).isoformat(timespec="microseconds")
        stall_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=AGENT_UPDATE_REPLACEMENT_STALL_MINUTES)
        ).isoformat(timespec="microseconds")
        with database.connection() as db:
            # A read snapshot does not reserve SQLite's single writer.  Detect
            # expiry/mirroring work inside the deferred snapshot first, then
            # take the writer lock only when there is work, mirroring
            # list_commands.
            db.execute("BEGIN")
            needs_maintenance = db.execute(
                """SELECT 1
                   FROM agent_update_jobs j
                   LEFT JOIN commands pc ON pc.id=j.post_update_command_id
                   LEFT JOIN commands c ON c.id=j.command_id
                   WHERE (j.status='activating'
                          AND (j.updated_at < ?
                               OR pc.status IN ('failed','cancelled','expired')
                               OR (j.stalled_at IS NULL
                                   AND j.post_update_command_id IS NULL
                                   AND j.target_heartbeat_count >= 2
                                   AND j.target_heartbeat_at IS NOT NULL
                                   AND j.target_heartbeat_at < ?)))
                     OR (j.status IN ('queued','dispatched')
                         AND c.status IN ('failed','cancelled','expired'))
                   LIMIT 1""",
                (activation_cutoff, stall_cutoff),
            ).fetchone()
            if needs_maintenance:
                # Drop the read snapshot before taking the writer; upgrading
                # a stale WAL snapshot can fail under a concurrent heartbeat.
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
                expire_stale_agent_updates(db, now)
                # Expiry and cancellation can be performed by generic command
                # maintenance.  Mirror those terminal states into the update
                # ledger before presenting it to an administrator.
                for terminal_status in ("failed", "cancelled", "expired"):
                    db.execute(
                        """UPDATE agent_update_jobs
                           SET status=?, updated_at=?, completed_at=?,
                               last_error=COALESCE(
                                   (SELECT error FROM commands
                                    WHERE id=agent_update_jobs.command_id),
                                   last_error
                               )
                           WHERE status IN ('queued','dispatched')
                             AND command_id IN (
                                 SELECT id FROM commands WHERE status=?
                             )""",
                        (terminal_status, now, now, terminal_status),
                    )
            rows = db.execute(
                """SELECT j.*, c.status AS command_status,
                          pc.status AS post_update_command_status,
                          a.name AS agent_name, ar.file_name, ar.file_size,
                          ar.sha256, u.username AS created_by_name
                   FROM agent_update_jobs j
                   JOIN commands c ON c.id=j.command_id
                   JOIN agents a ON a.id=j.agent_id
                   JOIN agent_releases ar ON ar.id=j.release_id
                   JOIN users u ON u.id=j.created_by
                   LEFT JOIN commands pc ON pc.id=j.post_update_command_id
                   ORDER BY j.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [serialize_agent_update(row) for row in rows]

    @app.delete("/api/agent-updates/history")
    def clear_agent_update_history(
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        """Delete only completed update jobs; active rollouts are immutable."""

        terminal = tuple(sorted(TERMINAL_COMMAND_STATUSES))
        placeholders = ",".join("?" for _ in terminal)
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            deletable = db.execute(
                f"""SELECT COUNT(*) AS count
                    FROM agent_update_jobs j
                    JOIN commands c ON c.id=j.command_id
                    WHERE c.action=?
                      AND c.status IN ({placeholders})
                      AND j.status IN ({placeholders})
                      AND COALESCE(json_extract(j.oc_recovery_json,'$.state'),'') NOT IN ('armed','waiting','pending')
                      AND NOT EXISTS (
                          SELECT 1 FROM json_each(j.oc_recovery_json,'$.command_ids') owned
                          JOIN commands child ON child.id=owned.value
                          WHERE child.status IN ('queued','dispatched','running')
                      )""",
                (
                    AGENT_UPDATE_CAPABILITY,
                    *terminal,
                    *terminal,
                ),
            ).fetchone()["count"]
            command_cursor = db.execute(
                f"""DELETE FROM commands
                    WHERE action=? AND status IN ({placeholders})
                      AND id IN (
                          SELECT command_id FROM agent_update_jobs
                          WHERE status IN ({placeholders})
                            AND COALESCE(json_extract(oc_recovery_json,'$.state'),'') NOT IN ('armed','waiting','pending')
                            AND NOT EXISTS (
                                SELECT 1 FROM json_each(agent_update_jobs.oc_recovery_json,'$.command_ids') owned
                                JOIN commands child ON child.id=owned.value
                                WHERE child.status IN ('queued','dispatched','running')
                            )
                      )""",
                (
                    AGENT_UPDATE_CAPABILITY,
                    *terminal,
                    *terminal,
                ),
            )
            commands_deleted = max(0, command_cursor.rowcount)
            Database.audit(
                db,
                actor=user["username"],
                action="agent_update_history_cleared",
                target_type="agent_update_history",
                details={
                    "update_jobs_deleted": int(deletable),
                    "commands_deleted": commands_deleted,
                    "active_updates_preserved": True,
                },
            )
        return {
            "status": "cleared",
            "update_jobs_deleted": int(deletable),
            "commands_deleted": commands_deleted,
        }

    def _load_deployable_agent_release(
        db: sqlite3.Connection,
        release_id: str,
    ) -> sqlite3.Row:
        release = db.execute(
            "SELECT * FROM agent_releases WHERE id=?", (release_id,)
        ).fetchone()
        if not release:
            raise HTTPException(404, "Agent release not found")
        if _optional_row_value(release, "update_protocol") != AGENT_UPDATE_CAPABILITY:
            raise HTTPException(
                409,
                "This stored package predates agent_update.v1 and is download-only",
            )
        if str(release["architecture"] or "").strip().casefold().replace(
            "-", "_"
        ) != AGENT_RELEASE_ARCHITECTURE:
            raise HTTPException(409, "Agent release architecture is unsupported")
        release_path = _agent_release_storage_path(settings, release["storage_key"])
        if (
            not release_path.is_file()
            or release_path.stat().st_size != release["file_size"]
        ):
            raise HTTPException(410, "Agent release file is unavailable or incomplete")
        return release

    def _queue_agent_release_deployment(
        db: sqlite3.Connection,
        *,
        release: sqlite3.Row,
        agent_id: str,
        user: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate one explicit target and atomically create its update job."""

        agent = db.execute(
            "SELECT * FROM agents WHERE id=?", (agent_id,)
        ).fetchone()
        if not agent:
            raise HTTPException(404, "Agent not found")
        if agent["disabled_at"]:
            raise HTTPException(409, "Agent is disabled")
        serialized_agent = _serialize_agent(agent, settings)
        if serialized_agent["status"] != "online":
            raise HTTPException(409, "Agent update requires an online device")
        agent_platform = {
            "windows": "windows",
            "win": "windows",
            "win32": "windows",
            "linux": "linux",
            "ubuntu": "linux",
            "debian": "linux",
        }.get(str(agent["os_family"] or "").strip().casefold())
        release_platform = str(release["platform"] or "").strip().casefold()
        if agent_platform != release_platform:
            target_label = "Windows" if release_platform == "windows" else "Linux"
            raise HTTPException(
                409, f"This release supports {target_label} devices only"
            )
        inventory = _json(agent["inventory_json"], {})
        agent_architecture = str(
            inventory.get("architecture", "")
            if isinstance(inventory, dict)
            else ""
        ).strip().casefold().replace("-", "_")
        if agent_architecture not in {"amd64", "x86_64", "x64"}:
            raise HTTPException(
                409,
                "This release requires a reported "
                f"{release_platform} x86_64 device",
            )
        capabilities = _json(
            _optional_row_value(agent, "capabilities_json", "[]"), []
        )
        if (
            not isinstance(capabilities, list)
            or AGENT_UPDATE_CAPABILITY not in capabilities
        ):
            raise HTTPException(
                409,
                "This device does not advertise agent_update.v1; bootstrap an "
                "updater-capable Agent once before remote updates",
            )
        source_version = _strict_agent_version(agent["agent_version"])
        target_version = _strict_agent_version(release["version"])
        if source_version is None or target_version is None:
            raise HTTPException(409, "Agent update requires strict X.Y.Z versions")
        if (
            release_platform == "linux"
            and source_version < LINUX_AGENT_ONLINE_UPDATE_MIN_SOURCE
        ):
            raise HTTPException(
                409,
                "Linux Agent 0.6.6 or older may contain the original updater bridge; "
                "manually install the 0.6.8 full package once before online updates",
            )
        if target_version == source_version:
            raise HTTPException(409, "Device already runs this Agent version")
        if target_version < source_version:
            raise HTTPException(409, "Remote Agent downgrade is not allowed")
        expire_stale_agent_commands(
            db,
            agent_id=agent["id"],
            now=utcnow(),
        )
        unfinished_update = db.execute(
            """SELECT command_id FROM agent_update_jobs
               WHERE agent_id=?
                 AND status IN ('queued','dispatched','activating') LIMIT 1""",
            (agent["id"],),
        ).fetchone()
        if unfinished_update:
            raise HTTPException(
                409, "An Agent update is already in progress for this device"
            )
        unfinished_command = db.execute(
            """SELECT id FROM commands
               WHERE agent_id=?
                 AND status IN ('queued','dispatched','running') LIMIT 1""",
            (agent["id"],),
        ).fetchone()
        if unfinished_command:
            raise HTTPException(
                409, "Device has another unfinished command; retry after it finishes"
            )

        now_moment = datetime.now(timezone.utc)
        created_at = now_moment.isoformat(timespec="microseconds")
        expires_at = (now_moment + timedelta(minutes=30)).isoformat(
            timespec="seconds"
        )
        command_id = str(uuid.uuid4())
        download_url = f"/api/agent/updates/{command_id}/download"
        payload = {
            "release_id": release["id"],
            "version": release["version"],
            "sha256": release["sha256"],
            "size": release["file_size"],
            "download_url": download_url,
        }
        db.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at,
                expires_at,is_transient)
               VALUES(?,?,?,?,?,?,?,?,0)""",
            (
                command_id,
                agent["id"],
                AGENT_UPDATE_CAPABILITY,
                json.dumps(payload, separators=(",", ":")),
                "queued",
                user["id"],
                created_at,
                expires_at,
            ),
        )
        db.execute(
            """INSERT INTO agent_update_jobs
               (command_id,agent_id,release_id,source_version,target_version,
                status,created_by,created_at,updated_at,activate_remote_workspace)
               VALUES(?,?,?,?,?,'queued',?,?,?,0)""",
            (
                command_id,
                agent["id"],
                release["id"],
                agent["agent_version"],
                release["version"],
                user["id"],
                created_at,
                created_at,
            ),
        )
        db.execute("UPDATE agent_update_jobs SET oc_recovery_json=? WHERE command_id=?",
                   (capture_update_oc_recovery(db, agent), command_id))
        Database.audit(
            db,
            actor=user["username"],
            action="agent_update_queued",
            target_type="agent",
            target_id=agent["id"],
            details={
                "command_id": command_id,
                "release_id": release["id"],
                "source_version": agent["agent_version"],
                "target_version": release["version"],
                "sha256": release["sha256"],
                "size": release["file_size"],
            },
        )
        return {
            "command_id": command_id,
            "agent_id": agent_id,
            "release_id": release["id"],
            "source_version": agent["agent_version"],
            "target_version": release["version"],
            "status": "queued",
            "created_at": created_at,
            "expires_at": expires_at,
        }

    @app.post("/api/agent-releases/{release_id}/deploy", status_code=201)
    def deploy_agent_release(
        release_id: str,
        body: AgentReleaseDeployRequest,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            expire_stale_agent_updates(db, utcnow())
            release = _load_deployable_agent_release(db, release_id)
            return _queue_agent_release_deployment(
                db,
                release=release,
                agent_id=body.agent_id,
                user=user,
            )

    @app.post("/api/agent-releases/{release_id}/deploy-batch", status_code=201)
    def deploy_agent_release_batch(
        release_id: str,
        body: AgentReleaseBatchDeployRequest,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        """Queue updates only for the explicitly supplied device IDs."""

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            expire_stale_agent_updates(db, utcnow())
            release = _load_deployable_agent_release(db, release_id)
            for position, agent_id in enumerate(body.agent_ids):
                savepoint = f"batch_agent_update_{position}"
                db.execute(f"SAVEPOINT {savepoint}")
                try:
                    queued = _queue_agent_release_deployment(
                        db,
                        release=release,
                        agent_id=agent_id,
                        user=user,
                    )
                except HTTPException as exc:
                    db.execute(f"ROLLBACK TO {savepoint}")
                    db.execute(f"RELEASE {savepoint}")
                    rejected.append(
                        {
                            "agent_id": agent_id,
                            "accepted": False,
                            "status_code": exc.status_code,
                            "detail": str(exc.detail),
                        }
                    )
                else:
                    db.execute(f"RELEASE {savepoint}")
                    accepted.append({"accepted": True, **queued})
            Database.audit(
                db,
                actor=user["username"],
                action="agent_update_batch_requested",
                target_type="agent_release",
                target_id=release_id,
                details={
                    "selected_agent_ids": body.agent_ids,
                    "accepted_agent_ids": [
                        item["agent_id"] for item in accepted
                    ],
                    "rejected_agent_ids": [
                        item["agent_id"] for item in rejected
                    ],
                },
            )
        return {
            "release_id": release_id,
            "target_version": release["version"],
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "accepted": accepted,
            "rejected": rejected,
        }
    @app.post("/api/agent-releases/upload", status_code=201)
    async def upload_agent_release(
        request: Request,
        version: str = Query(
            min_length=1,
            max_length=40,
            pattern=r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$",
        ),
        platform: str = Query(default="windows", min_length=1, max_length=24),
        architecture: str = Query(
            default="x86_64", min_length=1, max_length=24
        ),
        file_name: str = Query(min_length=1, max_length=255),
        notes: str = Query(default="", max_length=500),
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        normalized_platform = _normalize_agent_release_platform(platform)
        normalized_architecture = _normalize_agent_release_architecture(architecture)
        safe_file_name = _safe_agent_release_filename(
            file_name, platform=normalized_platform
        )

        media_type = (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        allowed_media_types = {"application/octet-stream"}
        if normalized_platform == "windows":
            allowed_media_types.update(
                {"application/zip", "application/x-zip-compressed"}
            )
        else:
            allowed_media_types.update({"application/gzip", "application/x-gzip"})
        if media_type not in allowed_media_types:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "Agent release upload content type does not match its platform archive",
            )
        maximum = max(1, settings.max_agent_release_bytes)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "Invalid Content-Length") from exc
            if declared_size < 0:
                raise HTTPException(400, "Invalid Content-Length")
            if declared_size > maximum:
                raise HTTPException(413, "Agent release exceeds the upload limit")

        storage_root = (settings.data_dir / "agent-releases").resolve()
        storage_root.mkdir(parents=True, exist_ok=True)
        release_id = str(uuid.uuid4())
        staging_path = storage_root / f".{release_id}.uploading"
        digest = hashlib.sha256()
        file_size = 0
        stream = staging_path.open("xb")
        try:
            with stream:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    file_size += len(chunk)
                    if file_size > maximum:
                        raise HTTPException(
                            413, "Agent release exceeds the upload limit"
                        )
                    digest.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if file_size == 0:
                raise HTTPException(422, "Agent release is empty")
            validator = (
                _validate_windows_portable_zip
                if normalized_platform == "windows"
                else _validate_linux_agent_tar_gz
            )
            await asyncio.to_thread(
                validator,
                staging_path,
                max_upload_bytes=maximum,
                expected_version=version,
            )

            storage_key = f"{release_id}/{safe_file_name}"
            destination = _agent_release_storage_path(settings, storage_key)
            now = utcnow()
            with agent_release_lock:
                destination.parent.mkdir(parents=True, exist_ok=False)
                try:
                    # Source and destination are deliberately within the same
                    # filesystem so replace is an atomic publication step.
                    staging_path.replace(destination)
                    with database.connection() as db:
                        db.execute(
                            """INSERT INTO agent_releases
                               (id,version,platform,architecture,file_name,
                                storage_key,file_size,sha256,update_protocol,notes,
                                created_by,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                release_id,
                                version,
                                normalized_platform,
                                normalized_architecture,
                                safe_file_name,
                                storage_key,
                                file_size,
                                digest.hexdigest(),
                                AGENT_UPDATE_CAPABILITY,
                                notes.strip(),
                                user["id"],
                                now,
                            ),
                        )
                        Database.audit(
                            db,
                            actor=user["username"],
                            action="agent_release_created",
                            target_type="agent_release",
                            target_id=release_id,
                            details={
                                "version": version,
                                "platform": normalized_platform,
                                "architecture": normalized_architecture,
                                "file_name": safe_file_name,
                                "size": file_size,
                            },
                        )
                except sqlite3.IntegrityError as exc:
                    destination.unlink(missing_ok=True)
                    with suppress(OSError):
                        destination.parent.rmdir()
                    raise HTTPException(
                        409,
                        "this Agent version already exists for "
                        f"{normalized_platform} x86_64",
                    ) from exc
                except Exception:
                    destination.unlink(missing_ok=True)
                    with suppress(OSError):
                        destination.parent.rmdir()
                    raise
            return {
                "id": release_id,
                "version": version,
                "platform": normalized_platform,
                "architecture": normalized_architecture,
                "file_name": safe_file_name,
                "size": file_size,
                "sha256": digest.hexdigest(),
                "update_protocol": AGENT_UPDATE_CAPABILITY,
                "deployable": True,
                "notes": notes.strip(),
                "created_by": user["username"],
                "created_at": now,
                "download_url": f"/api/agent-releases/{release_id}/download",
            }
        finally:
            staging_path.unlink(missing_ok=True)

    @app.get("/api/agent-releases/{release_id}/download")
    def download_agent_release(
        release_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> FileResponse:
        del user
        with database.connection() as db:
            release = db.execute(
                "SELECT * FROM agent_releases WHERE id=?", (release_id,)
            ).fetchone()
        if not release:
            raise HTTPException(404, "Agent release not found")
        path = _agent_release_storage_path(settings, release["storage_key"])
        if not path.is_file():
            raise HTTPException(410, "Agent release file is unavailable")
        return FileResponse(
            path,
            filename=release["file_name"],
            media_type=(
                "application/zip"
                if release["platform"] == "windows"
                else "application/gzip"
            ),
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/api/agent/updates/{command_id}/download")
    def download_agent_update(
        command_id: str,
        agent: dict[str, Any] = Depends(current_agent),
    ) -> FileResponse:
        """Serve one immutable release only to the Agent assigned its command."""

        now = utcnow()
        with database.connection() as db:
            row = db.execute(
                """SELECT c.id AS command_id, c.agent_id, c.action,
                          c.payload_json, c.status AS command_status, c.expires_at,
                          j.release_id, j.target_version, j.status AS update_status,
                          j.downloaded_at, ar.storage_key, ar.file_name,
                          ar.file_size, ar.sha256, ar.platform
                   FROM commands c
                   JOIN agent_update_jobs j ON j.command_id=c.id
                   JOIN agent_releases ar ON ar.id=j.release_id
                   WHERE c.id=? AND c.agent_id=?""",
                (command_id, agent["id"]),
            ).fetchone()
            if not row:
                # Deliberately do not reveal whether another device owns the
                # command identifier.
                raise HTTPException(404, "Agent update download not found")
            if (
                row["action"] != AGENT_UPDATE_CAPABILITY
                or row["command_status"] not in {"dispatched", "running"}
                or row["update_status"] not in {"dispatched", "activating"}
                or row["expires_at"] < now
            ):
                raise HTTPException(409, "Agent update download is no longer active")
            expected_payload = {
                "release_id": row["release_id"],
                "version": row["target_version"],
                "sha256": row["sha256"],
                "size": row["file_size"],
                "download_url": f"/api/agent/updates/{command_id}/download",
            }
            if _json(row["payload_json"], {}) != expected_payload:
                raise HTTPException(409, "Agent update binding is invalid")
            path = _agent_release_storage_path(settings, row["storage_key"])
            if not path.is_file() or path.stat().st_size != row["file_size"]:
                raise HTTPException(410, "Agent update file is unavailable or incomplete")
            if row["downloaded_at"] is None:
                db.execute(
                    """UPDATE agent_update_jobs
                       SET downloaded_at=?, updated_at=? WHERE command_id=?""",
                    (now, now, command_id),
                )
                Database.audit(
                    db,
                    actor=f"agent:{agent['id']}",
                    action="agent_update_download_started",
                    target_type="agent",
                    target_id=agent["id"],
                    details={
                        "command_id": command_id,
                        "release_id": row["release_id"],
                        "target_version": row["target_version"],
                    },
                )
        return FileResponse(
            path,
            filename=row["file_name"],
            media_type=(
                "application/zip"
                if row["platform"] == "windows"
                else "application/gzip"
            ),
            headers={
                "Cache-Control": "private, no-store",
                "X-Checksum-Sha256": row["sha256"],
            },
        )

    @app.delete("/api/agent-releases/{release_id}")
    def delete_agent_release(
        release_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        tombstone: Path | None = None
        stored_path: Path | None = None
        moved = False
        committed = False
        with agent_release_lock:
            try:
                with database.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    release = db.execute(
                        "SELECT * FROM agent_releases WHERE id=?", (release_id,)
                    ).fetchone()
                    if not release:
                        raise HTTPException(404, "Agent release not found")
                    used_by_update = db.execute(
                        """SELECT command_id FROM agent_update_jobs
                           WHERE release_id=? LIMIT 1""",
                        (release_id,),
                    ).fetchone()
                    if used_by_update:
                        raise HTTPException(
                            409,
                            "Agent release has update history and cannot be deleted",
                        )
                    stored_path = _agent_release_storage_path(
                        settings, release["storage_key"]
                    )
                    if stored_path.is_file():
                        tombstone = stored_path.with_name(
                            f".{stored_path.name}.deleting-{uuid.uuid4().hex}"
                        )
                        try:
                            stored_path.replace(tombstone)
                            moved = True
                        except OSError as exc:
                            raise HTTPException(
                                500, "could not stage Agent release file for deletion"
                            ) from exc
                    db.execute(
                        "DELETE FROM agent_releases WHERE id=?", (release_id,)
                    )
                    Database.audit(
                        db,
                        actor=user["username"],
                        action="agent_release_deleted",
                        target_type="agent_release",
                        target_id=release_id,
                        details={
                            "version": release["version"],
                            "platform": release["platform"],
                            "architecture": release["architecture"],
                            "file_name": release["file_name"],
                        },
                    )
                committed = True
            except Exception:
                if moved and tombstone is not None and stored_path is not None:
                    with suppress(OSError):
                        tombstone.replace(stored_path)
                raise

            # The public path has already disappeared atomically.  A rare
            # cleanup failure leaves only an unreachable tombstone, never a DB
            # row pointing at a partially deleted payload.
            if moved and tombstone is not None:
                try:
                    tombstone.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning(
                        "Could not remove deleted Agent release tombstone %s",
                        release_id,
                        exc_info=True,
                    )
            if committed and stored_path is not None:
                with suppress(OSError):
                    stored_path.parent.rmdir()
        return {"status": "deleted"}

    def _load_seine_test_package(
        db: sqlite3.Connection,
        package_id: str,
        version: str,
        *,
        target_os: str | None = None,
        error_status: int = 422,
        verify_bytes: bool = True,
    ) -> sqlite3.Row:
        """Validate an explicitly selected build without changing official releases."""

        package = db.execute(
            "SELECT * FROM miner_packages WHERE id=?", (package_id,),
        ).fetchone()
        if package is None:
            raise HTTPException(error_status, "Selected Seine test package no longer exists")
        if (
            package["managed_miner_type"] != "seine"
            or package["managed_channel"] != "test"
            or package["source_type"] != "uploaded"
            or package["version"] != version
        ):
            raise HTTPException(error_status, "Selected Seine package must be an uploaded test build of the selected version")
        try:
            package_os = normalize_os_family(str(package["managed_os_family"] or ""))
            variant = package["managed_variant"]
            spec = MINER_ADAPTERS.require("seine").asset(package_os, variant)
            canonical_id = managed_test_package_id("seine", version, package_os, variant=variant)
            target_matches = target_os is None or normalize_os_family(target_os) == package_os
        except ValueError as exc:
            raise HTTPException(error_status, f"Invalid Seine test package platform: {exc}") from exc
        if package_id != canonical_id or package["archive_type"] != spec.archive_type:
            raise HTTPException(error_status, "Selected Seine test package identity or archive type is invalid")
        if not target_matches:
            raise HTTPException(error_status, "Selected Seine test package does not match the target operating system")
        filename = package["original_filename"]
        storage_key = package["storage_key"]
        digest_value = package["sha256"]
        if (
            not isinstance(filename, str) or not filename or Path(filename).name != filename
            or storage_key != f"{package_id}/{filename}"
            or package["download_url"] != f"/api/agent/miner-packages/{package_id}/download"
            or not isinstance(digest_value, str) or re.fullmatch(r"[0-9a-f]{64}", digest_value) is None
            or not isinstance(package["file_size"], int) or package["file_size"] <= 0
        ):
            raise HTTPException(error_status, "Selected Seine test package hosted metadata is invalid")
        root = (settings.data_dir / "miner-packages").resolve()
        raw = root / storage_key
        if any(part.is_symlink() for part in (raw, *raw.parents) if part != root and root in part.parents):
            raise HTTPException(error_status, "Selected Seine test package storage must not use links")
        storage = _package_storage_path(settings, storage_key)
        try:
            if not storage.is_file() or storage.stat().st_size != package["file_size"]:
                raise HTTPException(error_status, "Selected Seine test package file is unavailable or its size changed")
            if verify_bytes:
                digest = hashlib.sha256()
                byte_count = 0
                with storage.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(PACKAGE_CHUNK_BYTES), b""):
                        byte_count += len(chunk)
                        digest.update(chunk)
                if byte_count != package["file_size"] or not secrets.compare_digest(digest.hexdigest(), digest_value):
                    raise HTTPException(error_status, "Selected Seine test package file size or SHA256 changed")
        except OSError as exc:
            raise HTTPException(error_status, "Selected Seine test package file is unavailable") from exc
        return package

    @app.get("/api/miner-packages/limits")
    def miner_package_limits(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        del user
        storage_root = (settings.data_dir / "miner-packages").resolve()
        try:
            free = shutil.disk_usage(storage_root if storage_root.exists() else settings.data_dir).free
        except OSError:
            free = None
        return {
            "native": {"max_archive_bytes": settings.max_miner_package_bytes},
            "storage": {"free_bytes": free, "checked_at": utcnow()},
        }

    @app.get("/api/miner-packages")
    def list_miner_packages(
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, Any]]:
        del user
        with database.connection() as db:
            rows = db.execute(
                """SELECT mp.*, u.username AS created_by_name
                   FROM miner_packages mp JOIN users u ON mp.created_by=u.id
                   ORDER BY mp.name, mp.version, mp.created_at"""
            ).fetchall()
            seine_availability = {}
            for row in rows:
                if row["managed_miner_type"] == "seine" and row["managed_channel"] == "test":
                    try:
                        _load_seine_test_package(db, row["id"], row["version"], verify_bytes=False)
                        seine_availability[row["id"]] = True
                    except HTTPException:
                        seine_availability[row["id"]] = False
        return [
            {
                **(_serialize_miner_package(row) or {}),
                **({"available": seine_availability[row["id"]]} if row["id"] in seine_availability else {}),
                "created_by": row["created_by_name"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    @app.get("/api/managed-miners")
    def list_managed_miners(
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, object]]:
        del user
        payload = managed_miner_catalog.payload()
        with database.connection() as db:
            cache_rows = db.execute(
                """SELECT * FROM miner_packages
                   WHERE managed_miner_type IS NOT NULL"""
            ).fetchall()

        cached: set[str] = set()
        catalog_rows: list[sqlite3.Row] = []
        for row in cache_rows:
            storage_key = row["storage_key"]
            if not isinstance(storage_key, str) or not storage_key:
                continue
            try:
                if not _package_storage_path(settings, storage_key).is_file():
                    continue
            except HTTPException:
                continue
            cached.add(row["id"])
            catalog_rows.append(row)

        # Official cache rows take precedence over colliding local tests,
        # independently of the order in which those rows were registered.
        catalog_rows.sort(key=lambda row: row["managed_channel"] == "test")
        for miner in payload:
            miner_type = str(miner.get("id") or "")
            versions = miner.get("versions")
            if not isinstance(versions, list):
                continue
            by_version = {
                str(release.get("version")): release
                for release in versions
                if isinstance(release, dict) and release.get("version")
            }
            test_versions: set[str] = set()
            for row in catalog_rows:
                if row["managed_miner_type"] != miner_type:
                    continue
                version = str(row["version"])
                try:
                    normalize_version(version, miner_type)
                    os_family = normalize_os_family(row["managed_os_family"])
                    channel = str(
                        _optional_row_value(row, "managed_channel", "stable")
                    )
                    variant = (
                        (_optional_row_value(row, "managed_variant") or "").strip()
                        or None
                    )
                    expected_id = (
                        managed_test_package_id(
                            miner_type,
                            version,
                            os_family,
                            variant=variant,
                        )
                        if channel == "test"
                        else managed_package_id(
                            miner_type,
                            version,
                            os_family,
                            variant=variant,
                        )
                    )
                except (TypeError, ValueError):
                    continue
                if channel not in {"stable", "test"} or row["id"] != expected_id:
                    continue
                if miner_type == "pearlminer" and os_family == "linux":
                    if variant not in {"nvidia", "amd"}:
                        continue
                    asset_key = f"linux-{variant}"
                else:
                    if variant is not None:
                        continue
                    asset_key = os_family
                # A local test must not decorate official discovery or an
                # official cache. Only test groups created in this pass can
                # collect more platform/variant assets from the same version.
                if channel == "test" and version in by_version and version not in test_versions:
                    continue
                release = by_version.get(version)
                if release is None:
                    release = {
                        "version": version,
                        "prerelease": channel == "test",
                        "source_type": "controller_cache",
                        "assets": {},
                    }
                    versions.append(release)
                    by_version[version] = release
                    if channel == "test":
                        test_versions.add(version)
                assets = release.get("assets")
                if not isinstance(assets, dict):
                    assets = {}
                    release["assets"] = assets
                existing_asset = assets.get(asset_key)
                if not isinstance(existing_asset, dict):
                    assets[asset_key] = {
                        "os_family": os_family,
                        "filename": row["original_filename"],
                        "archive_type": row["archive_type"],
                        "package_id": row["id"],
                        "source_type": "controller_cache",
                        "variant": variant,
                    }
            versions.sort(
                key=lambda release: managed_version_sort_key(
                    miner_type, str(release["version"])
                ),
                reverse=True,
            )
            supported_os: set[str] = set()
            for release in versions:
                assets = release.get("assets")
                if not isinstance(assets, dict):
                    continue
                for asset in assets.values():
                    if not isinstance(asset, dict):
                        continue
                    is_cached = asset.get("package_id") in cached
                    asset["cached"] = is_cached
                    asset["available"] = bool(is_cached or asset.get("download_url"))
                    os_family = asset.get("os_family")
                    if isinstance(os_family, str):
                        supported_os.add(os_family)
            miner["supported_os"] = sorted(supported_os)
        return payload

    @app.get("/api/miner-adapters")
    def list_miner_adapters(
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, object]]:
        """Expose immutable adapter capabilities without runtime code/hooks."""

        del user
        return public_adapter_catalog()

    def reject_managed_test_catalog_collision(
        miner_type: str,
        version: str,
    ) -> None:
        # Seine compatibility builds retain their own test ID and SHA even
        # when their upstream version also has an official GitHub release.
        if miner_type == "seine":
            return
        try:
            if managed_miner_catalog.has_version(miner_type, version):
                raise HTTPException(
                    409,
                    f"{MANAGED_MINER_DISPLAY_NAMES[miner_type]} {version} already "
                    "exists in the managed GitHub catalog; select that release directly",
                )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    def selected_managed_version(
        miner_type: str,
        requested_version: str | None,
    ) -> str | None:
        if miner_type == "generic":
            if requested_version is not None:
                raise HTTPException(422, "Legacy flight sheets do not use miner_version")
            return None
        try:
            releases = managed_miner_catalog.releases(miner_type)
            selected = requested_version or next(
                (release.version for release in releases if not release.prerelease), None
            )
            if selected is None:
                raise ValueError(f"No stable {miner_type} release is available")
            normalize_version(selected, miner_type)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if any(release.version == selected for release in releases):
            return selected
        with database.connection() as db:
            rows = db.execute(
                """SELECT id, storage_key, managed_os_family, managed_variant,
                          managed_channel
                   FROM miner_packages
                   WHERE managed_miner_type=? AND version=?
                     AND managed_channel IN ('stable','test')""",
                (miner_type, selected),
            ).fetchall()
        for row in rows:
            try:
                os_family = normalize_os_family(row["managed_os_family"])
                variant = (row["managed_variant"] or "").strip() or None
                expected_id = (
                    managed_test_package_id(
                        miner_type,
                        selected,
                        os_family,
                        variant=variant,
                    )
                    if row["managed_channel"] == "test"
                    else managed_package_id(
                        miner_type,
                        selected,
                        os_family,
                        variant=variant,
                    )
                )
            except (TypeError, ValueError):
                continue
            if row["id"] != expected_id:
                continue
            storage_key = row["storage_key"]
            if not storage_key:
                continue
            try:
                if _package_storage_path(settings, storage_key).is_file():
                    return selected
            except HTTPException:
                continue
        raise HTTPException(
            422,
            f"{miner_type} version {selected} is not in the current managed "
            "catalog or controller cache",
        )
    def selected_update_managed_version(
        miner_type: str,
        requested_version: str | None,
        existing_miner_type: str | None,
        existing_version: str | None,
    ) -> str | None:
        """Allow PUT to preserve a formerly valid pinned managed release.

        Catalog and valid controller-cache versions may be selected freely.
        An unavailable version already pinned to this exact sheet remains
        preservable so an unrelated edit never invalidates historical data.
        """

        normalized_existing_type = (existing_miner_type or "generic").lower()
        if (
            miner_type in MINER_TYPES
            and miner_type == normalized_existing_type
            and requested_version is not None
            and requested_version == existing_version
        ):
            try:
                return normalize_version(requested_version, miner_type)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        return selected_managed_version(miner_type, requested_version)

    def finalize_hosted_miner_package(
        *,
        package_id: str,
        staging_path: Path,
        filename: str,
        name: str,
        version: str,
        archive_type: Literal["zip", "tar.gz"],
        note: str,
        file_size: int,
        sha256: str,
        user: dict[str, Any],
        imported_from_host: str | None = None,
        registration_complete: Callable[[sqlite3.Connection, str], None] | None = None,
        managed_miner_type: str | None = None,
        managed_os_family: str | None = None,
        managed_variant: str | None = None,
        managed_channel: str = "stable",
    ) -> dict[str, Any]:
        storage_key = f"{package_id}/{filename}"
        destination = _package_storage_path(settings, storage_key)
        destination.parent.mkdir(parents=True, exist_ok=False)
        try:
            staging_path.replace(destination)
            now = utcnow()
            download_url = f"/api/agent/miner-packages/{package_id}/download"
            with database.connection() as db:
                db.execute(
                    """INSERT INTO miner_packages
                       (id, name, version, download_url, sha256, archive_type, note,
                        source_type, storage_key, original_filename, file_size,
                        managed_miner_type, managed_os_family,
                        managed_variant, managed_channel,
                        created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        package_id,
                        name.strip(),
                        version,
                        download_url,
                        sha256,
                        archive_type,
                        note.strip(),
                        "uploaded",
                        storage_key,
                        filename,
                        file_size,
                        managed_miner_type,
                        managed_os_family,
                        managed_variant,
                        managed_channel,
                        user["id"],
                        now,
                        now,
                    ),
                )
                if registration_complete is not None:
                    registration_complete(db, package_id)
                details: dict[str, Any] = {
                    "name": name.strip(),
                    "version": version,
                    "sha256": sha256,
                    "archive_type": archive_type,
                    "file_size": file_size,
                    "source_type": "uploaded",
                }
                if imported_from_host:
                    details["imported_from_host"] = imported_from_host
                if managed_miner_type:
                    details["managed_miner_type"] = managed_miner_type
                    details["managed_os_family"] = managed_os_family
                    details["managed_variant"] = managed_variant
                    details["managed_channel"] = managed_channel
                Database.audit(
                    db,
                    actor=user["username"],
                    action="miner_package_created",
                    target_type="miner_package",
                    target_id=package_id,
                    details=details,
                )
        except Exception:
            staging_path.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            with suppress(OSError):
                destination.parent.rmdir()
            raise
        return {
            "id": package_id,
            "name": name.strip(),
            "version": version,
            "download_url": download_url,
            "sha256": sha256,
            "archive_type": archive_type,
            "note": note.strip(),
            "source_type": "uploaded",
            "original_filename": filename,
            "file_size": file_size,
            "managed_miner_type": managed_miner_type,
            "managed_os_family": managed_os_family,
            "managed_variant": managed_variant,
            "managed_channel": managed_channel,
            "created_by": user["username"],
            "created_at": now,
            "updated_at": now,
        }
    def payload_references_package(value: Any, package_id: str) -> bool:
        if isinstance(value, dict):
            return any(
                item == package_id or payload_references_package(item, package_id)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(payload_references_package(item, package_id) for item in value)
        return False

    def cleanup_managed_miner_cache(
        miner_type: str,
        os_family: str,
        user: dict[str, Any],
        *,
        variant: str | None = None,
    ) -> None:
        """Best-effort retention of the newest unprotected archive cache.

        Version discovery is independent and may expose many more releases.
        Flight-sheet and active-command references win over the cache limit.
        A referenced older archive remains cached until that reference goes
        away, at which point a later managed download may collect it.
        """

        with database.connection() as db:
            rows = db.execute(
                """SELECT * FROM miner_packages
                   WHERE managed_miner_type=? AND managed_os_family=?""",
                (miner_type, os_family),
            ).fetchall()
        valid_rows: list[sqlite3.Row] = []
        for row in rows:
            try:
                normalize_version(row["version"], miner_type)
                expected_id = managed_package_id(
                    miner_type,
                    row["version"],
                    os_family,
                    variant=variant,
                )
            except ValueError:
                continue
            if row["id"] != expected_id:
                continue
            valid_rows.append(row)
        valid_rows.sort(
            key=lambda row: managed_version_sort_key(miner_type, row["version"]),
            reverse=True,
        )
        for candidate in valid_rows[MANAGED_MINER_CACHE_VERSIONS:]:
            package_id = candidate["id"]
            version = candidate["version"]
            storage_key = _optional_row_value(candidate, "storage_key")
            stored_path = (
                _package_storage_path(settings, storage_key) if storage_key else None
            )
            tombstone = (
                stored_path.with_name(f".{stored_path.name}.deleting-{uuid.uuid4().hex}")
                if stored_path is not None
                else None
            )
            moved = False
            deleted_committed = False
            try:
                with database.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        """SELECT id FROM miner_packages
                           WHERE id=? AND managed_miner_type=? AND managed_os_family=?""",
                        (package_id, miner_type, os_family),
                    ).fetchone()
                    if not current:
                        continue
                    sheet_reference = db.execute(
                        """SELECT 1 FROM flight_sheets
                           WHERE (miner_type=? AND miner_version=?)
                              OR (secondary_miner_type=? AND secondary_miner_version=?)
                           LIMIT 1""",
                        (miner_type, version, miner_type, version),
                    ).fetchone()
                    if sheet_reference:
                        continue
                    commands = db.execute(
                        """SELECT payload_json FROM commands
                           WHERE status IN ('queued','dispatched','running')"""
                    ).fetchall()
                    if any(
                        payload_references_package(
                            _json(command["payload_json"], {}), package_id
                        )
                        for command in commands
                    ):
                        continue
                    if stored_path is not None and stored_path.is_file():
                        stored_path.replace(tombstone)
                        moved = True
                    db.execute(
                        """DELETE FROM miner_packages
                           WHERE id=? AND managed_miner_type=? AND managed_os_family=?""",
                        (package_id, miner_type, os_family),
                    )
                    Database.audit(
                        db,
                        actor=user["username"],
                        action="managed_miner_cache_pruned",
                        target_type="miner_package",
                        target_id=package_id,
                        details={
                            "miner_type": miner_type,
                            "version": version,
                            "os_family": os_family,
                            "variant": variant,
                        },
                    )
                deleted_committed = True
                if moved and tombstone is not None:
                    tombstone.unlink(missing_ok=True)
                if stored_path is not None:
                    with suppress(OSError):
                        stored_path.parent.rmdir()
            except Exception:
                if (
                    moved
                    and not deleted_committed
                    and tombstone is not None
                    and tombstone.exists()
                ):
                    with suppress(OSError):
                        tombstone.replace(stored_path)
                LOGGER.warning(
                    "Could not prune managed miner cache entry %s",
                    package_id,
                    exc_info=True,
                )

    def ensure_managed_miner_package(
        miner_type: str,
        os_family: str,
        version: str,
        user: dict[str, Any],
        *,
        variant: str | None = None,
    ) -> sqlite3.Row | dict[str, Any]:
        """Reuse an official cache, fetch GitHub, or use an isolated local test."""

        try:
            normalized_type = miner_type.strip().lower()
            normalized_os = normalize_os_family(os_family)
            normalized_version = normalize_version(version, normalized_type)
            managed_asset_spec = MINER_ADAPTERS.require(normalized_type).asset(
                normalized_os, variant
            )
            pinned_asset_digest = managed_asset_spec.expected_sha256(
                normalized_version
            )
            package_id = managed_package_id(
                normalized_type,
                normalized_version,
                normalized_os,
                variant=variant,
            )
            test_package_id = managed_test_package_id(
                normalized_type,
                normalized_version,
                normalized_os,
                variant=variant,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with managed_package_lock:
            # A Seine compatibility upload is an explicit operator choice for
            # that exact version/platform.  It wins over discovery even when
            # GitHub later exposes an official asset (for example, official
            # Seine Linux binaries require newer glibc than Ubuntu 22.04).
            if normalized_type == "seine":
                with database.connection() as db:
                    try:
                        local_seine = _load_seine_test_package(
                            db,
                            test_package_id,
                            normalized_version,
                            target_os=normalized_os,
                            error_status=409,
                        )
                    except HTTPException as error:
                        if error.status_code != 409 or "no longer exists" not in error.detail:
                            raise
                        local_seine = None
                if local_seine is not None:
                    return local_seine
            # Only the official namespace is reusable before catalog lookup.
            # This preserves pinned official sheets offline without allowing a
            # controller-uploaded test build to impersonate a GitHub asset.
            with database.connection() as db:
                existing = db.execute(
                    """SELECT * FROM miner_packages
                       WHERE id=? AND managed_channel!='test'""",
                    (package_id,),
                ).fetchone()
            if existing:
                if managed_asset_spec.sha256_by_version and pinned_asset_digest is None:
                    raise HTTPException(
                        409,
                        "This official miner release has no approved SHA-256 pin; "
                        "register an operator-reviewed test package instead",
                    )
                if pinned_asset_digest is not None and not secrets.compare_digest(
                    str(existing["sha256"] or ""), pinned_asset_digest
                ):
                    raise HTTPException(
                        409,
                        "Cached official miner package does not match its approved "
                        "SHA-256; delete the cache entry and apply again",
                    )
                storage_key = _optional_row_value(existing, "storage_key")
                if storage_key and _package_storage_path(settings, storage_key).is_file():
                    cleanup_managed_miner_cache(
                        normalized_type,
                        normalized_os,
                        user,
                        variant=variant,
                    )
                    return existing
                raise HTTPException(
                    409,
                    "Managed miner metadata exists but its cached archive is missing; "
                    "delete that package record and apply again",
                )

            try:
                asset = managed_miner_catalog.asset(
                    normalized_type,
                    normalized_os,
                    normalized_version,
                    variant=variant,
                )
            except ValueError as catalog_error:
                # Local tests intentionally live outside the official package
                # namespace and are used only while this platform is absent
                # from the current GitHub catalog. Official assets above always
                # take precedence, while an explicitly pinned package bypasses
                # this resolver entirely.
                with database.connection() as db:
                    local_test = db.execute(
                        """SELECT * FROM miner_packages
                           WHERE id=? AND managed_miner_type=?
                             AND managed_os_family=? AND managed_channel='test'""",
                        (
                            test_package_id,
                            normalized_type,
                            normalized_os,
                        ),
                    ).fetchone()
                    if normalized_type == "seine" and local_test is not None:
                        return _load_seine_test_package(
                            db,
                            test_package_id,
                            normalized_version,
                            target_os=normalized_os,
                            error_status=409,
                        )
                if local_test is None:
                    platform = f"{normalized_os} x64" + (f" ({variant})" if variant else "")
                    raise HTTPException(
                        422,
                        f"{catalog_error}; no uploaded test package for {platform} "
                        f"at version {normalized_version}",
                    ) from catalog_error
                storage_key = _optional_row_value(local_test, "storage_key")
                if storage_key and _package_storage_path(settings, storage_key).is_file():
                    return local_test
                raise HTTPException(
                    409,
                    "Categorized test miner metadata exists but its cached archive is "
                    "missing; delete that package record and register it again",
                )
            if asset.download_url is None:
                raise HTTPException(
                    409,
                    "This managed miner version has no downloadable official package",
                )
            storage_root = (settings.data_dir / "miner-packages").resolve()
            storage_root.mkdir(parents=True, exist_ok=True)
            staging_path = storage_root / f".{asset.package_id}.importing"
            staging_path.unlink(missing_ok=True)
            try:
                file_size, digest = _download_https_package(
                    asset.download_url,
                    staging_path,
                    max_bytes=max(1, settings.max_miner_package_bytes),
                    timeout_seconds=max(
                        1, settings.miner_package_import_timeout_seconds
                    ),
                    redirect_hosts=frozenset(
                        {
                            "github.com",
                            "release-assets.githubusercontent.com",
                            "objects.githubusercontent.com",
                        }
                    ),
                    max_redirects=3,
                )
                if asset.expected_sha256 is not None and not secrets.compare_digest(
                    digest, asset.expected_sha256
                ):
                    raise HTTPException(
                        502,
                        "Official miner package SHA-256 does not match the approved "
                        "release digest",
                    )
                package = finalize_hosted_miner_package(
                    package_id=asset.package_id,
                    staging_path=staging_path,
                    filename=asset.filename,
                    name=asset.display_name,
                    version=asset.version,
                    archive_type=asset.archive_type,  # type: ignore[arg-type]
                    note="RigPilot managed miner; cached from the official GitHub release",
                    file_size=file_size,
                    sha256=digest,
                    user=user,
                    imported_from_host="github.com",
                    managed_miner_type=normalized_type,
                    managed_os_family=normalized_os,
                    managed_variant=variant,
                    managed_channel="stable",
                )
                cleanup_managed_miner_cache(
                    normalized_type,
                    normalized_os,
                    user,
                    variant=variant,
                )
                return package
            except Exception:
                staging_path.unlink(missing_ok=True)
                raise
    @app.post("/api/miner-packages/upload", status_code=201)
    async def upload_miner_package(
        request: Request,
        name: str = Query(min_length=1, max_length=80),
        version: str = Query(
            min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._+-]+$"
        ),
        archive_type: Literal["zip", "tar.gz"] = Query(),
        filename: str = Query(min_length=1, max_length=255),
        note: str = Query(default="", max_length=500),
        managed_miner_type: str | None = Query(
            default=None, min_length=1, max_length=80
        ),
        managed_os_family: Literal["windows", "linux"] | None = Query(default=None),
        managed_variant: Literal["nvidia", "amd"] | None = Query(default=None),
        managed_channel: Literal["test", "custom"] | None = Query(default=None),
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/octet-stream":
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "miner package uploads require application/octet-stream",
            )
        safe_filename = _safe_package_filename(
            filename, archive_type, require_matching_extension=True
        )
        classification = _manual_managed_classification(
            managed_miner_type,
            managed_os_family,
            managed_variant,
            managed_channel,
            version,
            archive_type,
        )
        if classification is not None:
            (
                managed_miner_type,
                managed_os_family,
                managed_variant,
                managed_channel,
            ) = classification
            reject_managed_test_catalog_collision(managed_miner_type, version)
            name = MANAGED_MINER_DISPLAY_NAMES[managed_miner_type]

        maximum = settings.max_miner_package_bytes
        declared_size = None
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, "Invalid Content-Length") from exc
            if declared_size < 0:
                raise HTTPException(400, "Invalid Content-Length")
            if declared_size > maximum:
                raise HTTPException(413, "miner package exceeds the upload limit")

        storage_root = (settings.data_dir / "miner-packages").resolve()
        storage_root.mkdir(parents=True, exist_ok=True)
        package_id = (
            managed_test_package_id(
                managed_miner_type,
                version,
                managed_os_family,
                variant=managed_variant,
            )
            if classification is not None
            else str(uuid.uuid4())
        )
        if classification is not None:
            with database.connection() as db:
                if db.execute(
                    "SELECT 1 FROM miner_packages WHERE id=?", (package_id,)
                ).fetchone():
                    raise HTTPException(
                        409,
                        f"{MANAGED_MINER_DISPLAY_NAMES[managed_miner_type]} {version} "
                        f"{managed_os_family} test package is already registered",
                    )
        # Managed test builds have a deterministic package id.  The staging
        # name must not be deterministic: a controller crash could otherwise
        # leave ``.<package_id>.uploading`` behind and permanently block every
        # retry, while concurrent requests would race for the same file.
        staging_path = storage_root / f".{package_id}.{uuid.uuid4().hex}.uploading"
        digest = hashlib.sha256()
        file_size = 0
        try:
            with staging_path.open("xb") as stream:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    file_size += len(chunk)
                    if file_size > maximum:
                        raise HTTPException(413, "miner package exceeds the upload limit")
                    digest.update(chunk)
                    stream.write(chunk)
            if file_size == 0:
                raise HTTPException(422, "miner package is empty")
            uploaded_sha256 = digest.hexdigest()
            return await asyncio.to_thread(
                finalize_hosted_miner_package,
                package_id=package_id,
                staging_path=staging_path,
                filename=safe_filename,
                name=name,
                version=version,
                archive_type=archive_type,
                note=(
                    note
                    or (
                        f"{MANAGED_MINER_DISPLAY_NAMES[managed_miner_type]} {version} "
                        f"本地测试包（{managed_os_family}"
                        f"{' / ' + managed_variant if managed_variant else ''}）"
                        if classification is not None
                        else ""
                    )
                ),
                file_size=file_size,
                sha256=uploaded_sha256,
                user=user,
                managed_miner_type=(
                    managed_miner_type if classification is not None else None
                ),
                managed_os_family=(
                    managed_os_family if classification is not None else None
                ),
                managed_variant=(
                    managed_variant if classification is not None else None
                ),
                managed_channel=(
                    managed_channel if classification is not None else "stable"
                ),
            )
        finally:
            staging_path.unlink(missing_ok=True)

    @app.post("/api/miner-packages/import", status_code=201)
    def import_miner_package(
        body: MinerPackageImport,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        # One worker owns download, registration and cleanup even if the
        # request disconnects while the blocking download is still running.
        classification = _manual_managed_classification(
            body.managed_miner_type,
            body.managed_os_family,
            body.managed_variant,
            body.managed_channel,
            body.version,
            body.archive_type,
        )
        managed_miner_type = body.managed_miner_type
        managed_os_family = body.managed_os_family
        managed_variant = body.managed_variant
        managed_channel = body.managed_channel
        name = body.name
        if classification is not None:
            (
                managed_miner_type,
                managed_os_family,
                managed_variant,
                managed_channel,
            ) = classification
            reject_managed_test_catalog_collision(managed_miner_type, body.version)
            name = MANAGED_MINER_DISPLAY_NAMES[managed_miner_type]

        storage_root = (settings.data_dir / "miner-packages").resolve()
        storage_root.mkdir(parents=True, exist_ok=True)
        package_id = (
            managed_test_package_id(
                managed_miner_type,
                body.version,
                managed_os_family,
                variant=managed_variant,
            )
            if classification is not None
            else str(uuid.uuid4())
        )
        if classification is not None:
            with database.connection() as db:
                if db.execute(
                    "SELECT 1 FROM miner_packages WHERE id=?", (package_id,)
                ).fetchone():
                    raise HTTPException(
                        409,
                        f"{MANAGED_MINER_DISPLAY_NAMES[managed_miner_type]} "
                        f"{body.version} {managed_os_family} test package is already registered",
                    )
        # Managed test packages have deterministic IDs, so every request needs
        # its own staging file.  A fixed name lets concurrent imports truncate
        # or rename a file while another downloader is still writing it.
        staging_path = storage_root / f".{package_id}.{uuid.uuid4().hex}.importing"
        parsed = urlparse(body.download_url)
        candidate = unquote(parsed.path).replace("\\", "/").rsplit("/", 1)[-1]
        safe_filename = _safe_package_filename(
            candidate, body.archive_type, require_matching_extension=False
        )
        try:
            file_size, sha256 = _download_https_package(
                body.download_url,
                staging_path,
                max_bytes=settings.max_miner_package_bytes,
                timeout_seconds=max(1, settings.miner_package_import_timeout_seconds),
            )
            return finalize_hosted_miner_package(
                package_id=package_id,
                staging_path=staging_path,
                filename=safe_filename,
                name=name,
                version=body.version,
                archive_type=body.archive_type,
                note=body.note,
                file_size=file_size,
                sha256=sha256,
                user=user,
                imported_from_host=parsed.hostname,
                managed_miner_type=(
                    managed_miner_type if classification is not None else None
                ),
                managed_os_family=(
                    managed_os_family if classification is not None else None
                ),
                managed_variant=(
                    managed_variant if classification is not None else None
                ),
                managed_channel=(
                    managed_channel if classification is not None else "stable"
                ),
            )
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise
    @app.get("/api/agent/miner-packages/{package_id}/download")
    def download_uploaded_miner_package(
        package_id: str,
        agent: dict[str, Any] = Depends(current_agent),
    ) -> FileResponse:
        del agent
        with database.connection() as db:
            package = db.execute(
                "SELECT * FROM miner_packages WHERE id=?", (package_id,)
            ).fetchone()
        if not package or package["source_type"] != "uploaded" or not package["storage_key"]:
            raise HTTPException(404, "Uploaded miner package not found")
        path = _package_storage_path(settings, package["storage_key"])
        if not path.is_file():
            raise HTTPException(410, "Uploaded miner package file is unavailable")
        return FileResponse(
            path,
            filename=package["original_filename"] or path.name,
            media_type="application/octet-stream",
            headers={"Cache-Control": "private, no-store"},
        )

    @app.post("/api/miner-packages", status_code=201)
    def create_miner_package(
        body: MinerPackageCreate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        package_id = str(uuid.uuid4())
        now = utcnow()
        # Keep the legacy NOT NULL database column without making SHA-256 a
        # deployment requirement. Older clients may still submit it as
        # informational metadata.
        package_sha256 = body.sha256 or ""
        with database.connection() as db:
            db.execute(
                """INSERT INTO miner_packages
                   (id, name, version, download_url, sha256, archive_type, note,
                    created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    package_id,
                    body.name,
                    body.version,
                    body.download_url,
                    package_sha256,
                    body.archive_type,
                    body.note,
                    user["id"],
                    now,
                    now,
                ),
            )
            Database.audit(
                db,
                actor=user["username"],
                action="miner_package_created",
                target_type="miner_package",
                target_id=package_id,
                details={
                    "name": body.name,
                    "version": body.version,
                    "archive_type": body.archive_type,
                },
            )
        return {
            "id": package_id,
            "name": body.name,
            "version": body.version,
            "download_url": body.download_url,
            "sha256": body.sha256,
            "archive_type": body.archive_type,
            "note": body.note,
            "source_type": "external",
            "original_filename": None,
            "file_size": None,
            "created_by": user["username"],
            "created_at": now,
            "updated_at": now,
        }

    @app.delete("/api/miner-packages/{package_id}")
    def delete_miner_package(
        package_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        storage_key: str | None = None
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            package = db.execute(
                "SELECT * FROM miner_packages WHERE id=?", (package_id,)
            ).fetchone()
            if not package:
                raise HTTPException(404, "Miner package not found")
            referenced = db.execute(
                """SELECT COUNT(*) AS count FROM flight_sheets
                   WHERE miner_package_id=? OR secondary_miner_package_id=?""",
                (package_id, package_id),
            ).fetchone()["count"]
            if referenced:
                raise HTTPException(409, "Miner package is used by a flight sheet")
            managed_miner_type = package["managed_miner_type"]
            if managed_miner_type:
                version_reference = db.execute(
                    """SELECT 1 FROM flight_sheets
                       WHERE (miner_type=? AND miner_version=?)
                          OR (secondary_miner_type=? AND secondary_miner_version=?)
                       LIMIT 1""",
                    (
                        managed_miner_type,
                        package["version"],
                        managed_miner_type,
                        package["version"],
                    ),
                ).fetchone()
                if version_reference:
                    raise HTTPException(
                        409,
                        "Managed miner package version is used by a flight sheet",
                    )
            active_commands = db.execute(
                """SELECT payload_json FROM commands
                   WHERE status IN ('queued','dispatched','running')"""
            ).fetchall()
            if any(
                payload_references_package(
                    _json(command["payload_json"], {}), package_id
                )
                for command in active_commands
            ):
                raise HTTPException(
                    409,
                    "Miner package is used by an unfinished command",
                )
            storage_key = package["storage_key"]
            try:
                db.execute("DELETE FROM miner_packages WHERE id=?", (package_id,))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "Miner package is used by a flight sheet") from exc
            Database.audit(
                db,
                actor=user["username"],
                action="miner_package_deleted",
                target_type="miner_package",
                target_id=package_id,
                details={"name": package["name"], "version": package["version"]},
            )
        if storage_key:
            stored_path = _package_storage_path(settings, storage_key)
            try:
                stored_path.unlink(missing_ok=True)
                stored_path.parent.rmdir()
            except OSError:
                LOGGER.warning(
                    "Could not remove stored miner package %s", package_id, exc_info=True
                )
        return {"status": "deleted"}
    @app.get("/api/flight-sheets")
    def list_flight_sheets(
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, Any]]:
        can_view_pool_password = ROLE_LEVEL[user["role"]] >= ROLE_LEVEL["operator"]
        with database.connection() as db:
            rows = db.execute(
                """SELECT fs.*, w.name AS wallet_name, w.address AS wallet_address,
                          sw.name AS secondary_wallet_name,
                          sw.address AS secondary_wallet_address,
                          u.username AS created_by_name
                   FROM flight_sheets fs
                   JOIN wallets w ON fs.wallet_id=w.id
                   LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                   JOIN users u ON fs.created_by=u.id
                   ORDER BY fs.name, fs.created_at"""
            ).fetchall()
            package_rows = db.execute("SELECT * FROM miner_packages").fetchall()
            agent_rows = db.execute("SELECT * FROM agents").fetchall()
        packages = {row["id"]: row for row in package_rows}
        device_counts: defaultdict[str, int] = defaultdict(int)
        configured_device_counts: defaultdict[str, int] = defaultdict(int)
        for agent_row in agent_rows:
            active_sheet_id = _active_agent_flight_sheet_id(agent_row, settings)
            if active_sheet_id:
                device_counts[active_sheet_id] += 1
            configured_sheet_id = _reported_agent_flight_sheet_id(agent_row)
            if configured_sheet_id:
                configured_device_counts[configured_sheet_id] += 1
        result: list[dict[str, Any]] = []
        for row in rows:
            secondary = None
            if row["secondary_coin"]:
                secondary = {
                    "miner_type": row["secondary_miner_type"] or "generic",
                    "miner_version": row["secondary_miner_version"],
                    "coin": row["secondary_coin"],
                    "wallet_id": row["secondary_wallet_id"],
                    "wallet_name": row["secondary_wallet_name"],
                    "wallet_address": row["secondary_wallet_address"],
                    "pool_url": row["secondary_pool_url"],
                    "miner_profile": row["secondary_miner_profile"],
                    "miner_package_id": row["secondary_miner_package_id"],
                    "execution_mode": row["secondary_execution_mode"] or "native",
                    "miner_package": _serialize_miner_package(
                        packages.get(row["secondary_miner_package_id"])
                    ),
                    "algorithm": row["secondary_algorithm"],
                    "pool_password": (
                        _effective_pool_password(row["secondary_pool_password"])
                        if can_view_pool_password
                        else None
                    ),
                    "pool_password_configured": row["secondary_pool_password"]
                    is not None,
                    "device_indices": _json(
                        row["secondary_device_indices_json"], []
                    ),
                    "extra_config": _json(row["secondary_extra_config_json"], {}),
                }
            result.append({
                "id": row["id"],
                "name": row["name"],
                "secondary_mode": row["secondary_mode"] or "integrated",
                "miner_type": row["miner_type"] or "generic",
                "miner_version": row["miner_version"],
                "coin": row["coin"],
                "wallet_id": row["wallet_id"],
                "wallet_name": row["wallet_name"],
                "wallet_address": row["wallet_address"],
                "pool_url": row["pool_url"],
                "miner_profile": row["miner_profile"],
                "miner_package_id": row["miner_package_id"],
                "execution_mode": row["execution_mode"] or "native",
                "miner_package": _serialize_miner_package(
                    packages.get(row["miner_package_id"])
                ),
                "algorithm": row["algorithm"],
                "pool_password": (
                    _effective_pool_password(row["pool_password"])
                    if can_view_pool_password
                    else None
                ),
                "pool_password_configured": row["pool_password"] is not None,
                "device_indices": _json(row["device_indices_json"], []),
                "worker_name_template": row["worker_name_template"],
                "extra_config": _json(row["extra_config_json"], {}),
                "gpu_oc": _json(row["gpu_oc_json"], None),
                # Keep device_count's historical "actively mining" meaning for
                # older dashboards and expose the actual configured count.
                "device_count": device_counts[row["id"]],
                "configured_device_count": configured_device_counts[row["id"]],
                "secondary": secondary,
                "created_by": row["created_by_name"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result

    @app.post("/api/flight-sheets", status_code=201)
    def create_flight_sheet(
        body: FlightSheetCreate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        _validate_managed_flight_shape(body)
        _validate_flight_miner_pair(
            body.miner_type,
            body.secondary.miner_type if body.secondary else None,
            body.secondary_mode,
        )
        # Release discovery may perform a bounded GitHub request. Keep it
        # outside the subsequent SQLite transaction.
        miner_version = selected_managed_version(
            body.miner_type, body.miner_version
        )
        secondary_miner_version = (
            selected_managed_version(
                body.secondary.miner_type, body.secondary.miner_version
            )
            if body.secondary
            else None
        )
        (pool_url, algorithm, extra_config, secondary_pool_url,
         secondary_algorithm, secondary_extra_config, gpu_oc) = _normalize_flight_input(
            body, miner_version, secondary_miner_version
        )
        sheet_id = str(uuid.uuid4())
        now = utcnow()
        with database.connection() as db:
            if (body.miner_type == "seine" and body.miner_package_id) or (
                body.secondary and body.secondary.miner_type == "seine" and body.secondary.miner_package_id
            ):
                db.execute("BEGIN IMMEDIATE")
            wallet = db.execute(
                "SELECT id, name, coin, address FROM wallets WHERE id=?",
                (body.wallet_id,),
            ).fetchone()
            if not wallet:
                raise HTTPException(404, "Wallet not found")
            if wallet["coin"].upper() != body.coin.upper():
                raise HTTPException(422, "Flight sheet coin must match the wallet coin")
            package = None
            if body.miner_package_id:
                package = db.execute(
                    "SELECT * FROM miner_packages WHERE id=?",
                    (body.miner_package_id,),
                ).fetchone()
                if not package:
                    raise HTTPException(404, "Primary miner package not found")
                if body.miner_type == "seine":
                    package = _load_seine_test_package(db, body.miner_package_id, miner_version)

            secondary_wallet = None
            secondary_package = None
            if body.secondary:
                secondary_wallet = db.execute(
                    "SELECT id, name, coin, address FROM wallets WHERE id=?",
                    (body.secondary.wallet_id,),
                ).fetchone()
                if not secondary_wallet:
                    raise HTTPException(404, "Secondary wallet not found")
                if secondary_wallet["coin"].upper() != body.secondary.coin.upper():
                    raise HTTPException(
                        422, "Secondary miner coin must match the secondary wallet coin"
                    )
                if body.secondary.miner_package_id:
                    secondary_package = db.execute(
                        "SELECT * FROM miner_packages WHERE id=?",
                        (body.secondary.miner_package_id,),
                    ).fetchone()
                    if not secondary_package:
                        raise HTTPException(404, "Secondary miner package not found")
                    if body.secondary.miner_type == "seine":
                        secondary_package = _load_seine_test_package(
                            db, body.secondary.miner_package_id, secondary_miner_version,
                        )
            db.execute(
                """INSERT INTO flight_sheets
                   (id, name, miner_type, miner_version, coin, wallet_id, pool_url, miner_profile,
                    miner_package_id, execution_mode, algorithm, pool_password, device_indices_json,
                    worker_name_template, extra_config_json, gpu_oc_json,
                    secondary_mode,
                    secondary_miner_type, secondary_miner_version,
                    secondary_coin, secondary_wallet_id, secondary_pool_url,
                    secondary_miner_profile, secondary_miner_package_id,
                    secondary_execution_mode, secondary_algorithm, secondary_pool_password,
                    secondary_device_indices_json, secondary_extra_config_json,
                    created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sheet_id,
                    body.name,
                    body.miner_type,
                    miner_version,
                    body.coin,
                    body.wallet_id,
                    pool_url,
                    body.miner_profile,
                    body.miner_package_id,
                    body.execution_mode,
                    algorithm,
                    body.pool_password,
                    json.dumps(body.device_indices, separators=(",", ":")),
                    body.worker_name_template,
                    json.dumps(extra_config, separators=(",", ":"), ensure_ascii=False),
                    (
                        json.dumps(gpu_oc, separators=(",", ":"))
                        if gpu_oc is not None
                        else None
                    ),
                    body.secondary_mode,
                    body.secondary.miner_type if body.secondary else None,
                    secondary_miner_version,
                    body.secondary.coin if body.secondary else None,
                    body.secondary.wallet_id if body.secondary else None,
                    secondary_pool_url,
                    body.secondary.miner_profile if body.secondary else None,
                    body.secondary.miner_package_id if body.secondary else None,
                    body.secondary.execution_mode if body.secondary else None,
                    secondary_algorithm,
                    body.secondary.pool_password if body.secondary else None,
                    (
                        json.dumps(body.secondary.device_indices, separators=(",", ":"))
                        if body.secondary
                        else None
                    ),
                    (
                        json.dumps(
                            secondary_extra_config,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                        if body.secondary
                        else None
                    ),
                    user["id"],
                    now,
                    now,
                ),
            )
            Database.audit(
                db,
                actor=user["username"],
                action="flight_sheet_created",
                target_type="flight_sheet",
                target_id=sheet_id,
                details={
                    "name": body.name,
                    "coin": body.coin,
                    "miner_type": body.miner_type,
                    "miner_version": miner_version,
                    "secondary_mode": body.secondary_mode,
                    "wallet_id": body.wallet_id,
                    "miner_profile": body.miner_profile,
                    "miner_package_id": body.miner_package_id,
                    "execution_mode": body.execution_mode,
                    "secondary_execution_mode": (
                        body.secondary.execution_mode if body.secondary else None
                    ),
                    "secondary_enabled": body.secondary is not None,
                    "gpu_oc_enabled": gpu_oc is not None,
                    "gpu_oc_indices": (
                        [
                            setting["index"]
                            for setting in gpu_oc["settings"]
                            if "index" in setting
                        ]
                        if gpu_oc is not None
                        else []
                    ),
                    "gpu_oc_models": (
                        [
                            setting["model_match"]
                            for setting in gpu_oc["settings"]
                            if "model_match" in setting
                        ]
                        if gpu_oc is not None
                        else []
                    ),
                },
            )
        return {
            "id": sheet_id,
            "name": body.name,
            "secondary_mode": body.secondary_mode,
            "miner_type": body.miner_type,
            "miner_version": miner_version,
            "coin": body.coin,
            "wallet_id": body.wallet_id,
            "wallet_name": wallet["name"],
            "wallet_address": wallet["address"],
            "pool_url": pool_url,
            "miner_profile": body.miner_profile,
            "miner_package_id": body.miner_package_id,
            "execution_mode": body.execution_mode,
            "miner_package": _serialize_miner_package(package),
            "algorithm": algorithm,
            "pool_password": body.pool_password,
            "device_indices": body.device_indices,
            "worker_name_template": body.worker_name_template,
            "extra_config": extra_config,
            "gpu_oc": gpu_oc,
            "secondary": (
                {
                    "miner_type": body.secondary.miner_type,
                    "miner_version": secondary_miner_version,
                    "coin": body.secondary.coin,
                    "wallet_id": body.secondary.wallet_id,
                    "wallet_name": secondary_wallet["name"],
                    "wallet_address": secondary_wallet["address"],
                    "pool_url": secondary_pool_url,
                    "miner_profile": body.secondary.miner_profile,
                    "miner_package_id": body.secondary.miner_package_id,
                    "execution_mode": body.secondary.execution_mode,
                    "miner_package": _serialize_miner_package(secondary_package),
                    "algorithm": secondary_algorithm,
                    "pool_password": body.secondary.pool_password,
                    "device_indices": body.secondary.device_indices,
                    "extra_config": secondary_extra_config,
                }
                if body.secondary
                else None
            ),
            "created_by": user["username"],
            "created_at": now,
            "updated_at": now,
        }

    @app.put("/api/flight-sheets/{sheet_id}")
    def update_flight_sheet(
        sheet_id: str,
        body: FlightSheetCreate,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        # Read the pinned versions first, then close SQLite before a possible
        # catalog refresh. This also lets an exact, formerly-current version be
        # preserved without consulting GitHub.
        with database.connection() as db:
            existing_versions = db.execute(
                """SELECT fs.*, w.address AS wallet_address,
                          sw.address AS secondary_wallet_address
                   FROM flight_sheets fs
                   JOIN wallets w ON fs.wallet_id=w.id
                   LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                   WHERE fs.id=?""",
                (sheet_id,),
            ).fetchone()
        if not existing_versions:
            raise HTTPException(404, "Flight sheet not found")
        _validate_managed_flight_shape(body)
        _validate_flight_miner_pair(
            body.miner_type,
            body.secondary.miner_type if body.secondary else None,
            body.secondary_mode,
        )
        miner_version = selected_update_managed_version(
            body.miner_type,
            body.miner_version,
            existing_versions["miner_type"],
            existing_versions["miner_version"],
        )
        secondary_miner_version = (
            selected_update_managed_version(
                body.secondary.miner_type,
                body.secondary.miner_version,
                existing_versions["secondary_miner_type"],
                existing_versions["secondary_miner_version"],
            )
            if body.secondary
            else None
        )
        (pool_url, algorithm, extra_config, secondary_pool_url,
         secondary_algorithm, secondary_extra_config, gpu_oc) = _normalize_flight_input(
            body, miner_version, secondary_miner_version
        )

        now = utcnow()
        encoded_device_indices = json.dumps(body.device_indices, separators=(",", ":"))
        encoded_extra_config = json.dumps(
            extra_config, separators=(",", ":"), ensure_ascii=False
        )
        encoded_gpu_oc = (
            json.dumps(gpu_oc, separators=(",", ":")) if gpu_oc is not None else None
        )
        encoded_secondary_indices = (
            json.dumps(body.secondary.device_indices, separators=(",", ":"))
            if body.secondary
            else None
        )
        encoded_secondary_extra = (
            json.dumps(
                secondary_extra_config,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if body.secondary
            else None
        )
        changed_fields: list[str] = []
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT fs.*, w.address AS wallet_address,
                          sw.address AS secondary_wallet_address
                   FROM flight_sheets fs
                   JOIN wallets w ON fs.wallet_id=w.id
                   LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                   WHERE fs.id=?""",
                (sheet_id,),
            ).fetchone()
            if not existing:
                raise HTTPException(404, "Flight sheet not found")
            if _flight_sheet_configuration_fingerprint(
                existing
            ) != _flight_sheet_configuration_fingerprint(existing_versions):
                raise HTTPException(409, "Flight sheet changed while it was being edited")
            wallet = db.execute(
                "SELECT id, coin FROM wallets WHERE id=?", (body.wallet_id,)
            ).fetchone()
            if not wallet:
                raise HTTPException(404, "Wallet not found")
            if wallet["coin"].upper() != body.coin.upper():
                raise HTTPException(422, "Flight sheet coin must match the wallet coin")
            if body.miner_package_id:
                package = db.execute(
                    "SELECT * FROM miner_packages WHERE id=?", (body.miner_package_id,),
                ).fetchone()
                if package is None:
                    raise HTTPException(404, "Primary miner package not found")
                if body.miner_type == "seine":
                    package = _load_seine_test_package(db, body.miner_package_id, miner_version)
            if body.secondary:
                secondary_wallet = db.execute(
                    "SELECT id, coin FROM wallets WHERE id=?",
                    (body.secondary.wallet_id,),
                ).fetchone()
                if not secondary_wallet:
                    raise HTTPException(404, "Secondary wallet not found")
                if secondary_wallet["coin"].upper() != body.secondary.coin.upper():
                    raise HTTPException(
                        422, "Secondary miner coin must match the secondary wallet coin"
                    )
                if body.secondary.miner_package_id:
                    secondary_package = db.execute(
                        "SELECT * FROM miner_packages WHERE id=?", (body.secondary.miner_package_id,),
                    ).fetchone()
                    if secondary_package is None:
                        raise HTTPException(404, "Secondary miner package not found")
                    if body.secondary.miner_type == "seine":
                        secondary_package = _load_seine_test_package(
                            db, body.secondary.miner_package_id, secondary_miner_version,
                        )

            current_config = {
                "name": existing["name"],
                "secondary_mode": existing["secondary_mode"] or "integrated",
                "miner_type": existing["miner_type"] or "generic",
                "miner_version": existing["miner_version"],
                "coin": existing["coin"],
                "wallet_id": existing["wallet_id"],
                "pool_url": existing["pool_url"],
                "miner_profile": existing["miner_profile"],
                "miner_package_id": existing["miner_package_id"],
                "execution_mode": existing["execution_mode"] or "native",
                "algorithm": existing["algorithm"],
                "pool_password": _effective_pool_password(
                    existing["pool_password"]
                ),
                "device_indices": _json(existing["device_indices_json"], []),
                "worker_name_template": existing["worker_name_template"],
                "extra_config": _json(existing["extra_config_json"], {}),
                "gpu_oc": _json(existing["gpu_oc_json"], None),
                "secondary_miner_type": existing["secondary_miner_type"],
                "secondary_miner_version": existing["secondary_miner_version"],
                "secondary_coin": existing["secondary_coin"],
                "secondary_wallet_id": existing["secondary_wallet_id"],
                "secondary_pool_url": existing["secondary_pool_url"],
                "secondary_miner_profile": existing["secondary_miner_profile"],
                "secondary_miner_package_id": existing["secondary_miner_package_id"],
                "secondary_execution_mode": (
                    existing["secondary_execution_mode"] or "native"
                    if existing["secondary_coin"]
                    else None
                ),
                "secondary_algorithm": existing["secondary_algorithm"],
                "secondary_pool_password": (
                    _effective_pool_password(existing["secondary_pool_password"])
                    if existing["secondary_coin"]
                    else None
                ),
                "secondary_device_indices": (
                    _json(existing["secondary_device_indices_json"], [])
                    if existing["secondary_coin"]
                    else None
                ),
                "secondary_extra_config": (
                    _json(existing["secondary_extra_config_json"], {})
                    if existing["secondary_coin"]
                    else None
                ),
            }
            new_config = {
                "name": body.name,
                "secondary_mode": body.secondary_mode,
                "miner_type": body.miner_type,
                "miner_version": miner_version,
                "coin": body.coin,
                "wallet_id": body.wallet_id,
                "pool_url": pool_url,
                "miner_profile": body.miner_profile,
                "miner_package_id": body.miner_package_id,
                "execution_mode": body.execution_mode,
                "algorithm": algorithm,
                "pool_password": body.pool_password,
                "device_indices": body.device_indices,
                "worker_name_template": body.worker_name_template,
                "extra_config": extra_config,
                "gpu_oc": gpu_oc,
                "secondary_miner_type": body.secondary.miner_type if body.secondary else None,
                "secondary_miner_version": secondary_miner_version,
                "secondary_coin": body.secondary.coin if body.secondary else None,
                "secondary_wallet_id": body.secondary.wallet_id if body.secondary else None,
                "secondary_pool_url": secondary_pool_url,
                "secondary_miner_profile": body.secondary.miner_profile if body.secondary else None,
                "secondary_miner_package_id": body.secondary.miner_package_id if body.secondary else None,
                "secondary_execution_mode": (
                    body.secondary.execution_mode if body.secondary else None
                ),
                "secondary_algorithm": secondary_algorithm,
                "secondary_pool_password": (
                    body.secondary.pool_password if body.secondary else None
                ),
                "secondary_device_indices": body.secondary.device_indices if body.secondary else None,
                "secondary_extra_config": secondary_extra_config if body.secondary else None,
            }
            changed_fields = [
                field for field, value in new_config.items() if current_config[field] != value
            ]
            runtime_changed_fields = [
                field for field in changed_fields if field != "name"
            ]
            if changed_fields:
                db.execute(
                    """UPDATE flight_sheets SET
                           name=?, secondary_mode=?, miner_type=?, miner_version=?,
                           coin=?, wallet_id=?, pool_url=?,
                           miner_profile=?, miner_package_id=?, execution_mode=?, algorithm=?,
                           pool_password=?, device_indices_json=?, worker_name_template=?,
                           extra_config_json=?, gpu_oc_json=?,
                           secondary_miner_type=?, secondary_miner_version=?,
                           secondary_coin=?, secondary_wallet_id=?, secondary_pool_url=?,
                           secondary_miner_profile=?, secondary_miner_package_id=?,
                           secondary_execution_mode=?, secondary_algorithm=?, secondary_pool_password=?,
                           secondary_device_indices_json=?, secondary_extra_config_json=?,
                           updated_at=?
                       WHERE id=?""",
                    (
                        body.name,
                        body.secondary_mode,
                        body.miner_type,
                        miner_version,
                        body.coin,
                        body.wallet_id,
                        pool_url,
                        body.miner_profile,
                        body.miner_package_id,
                        body.execution_mode,
                        algorithm,
                        body.pool_password,
                        encoded_device_indices,
                        body.worker_name_template,
                        encoded_extra_config,
                        encoded_gpu_oc,
                        body.secondary.miner_type if body.secondary else None,
                        secondary_miner_version,
                        body.secondary.coin if body.secondary else None,
                        body.secondary.wallet_id if body.secondary else None,
                        secondary_pool_url,
                        body.secondary.miner_profile if body.secondary else None,
                        body.secondary.miner_package_id if body.secondary else None,
                        body.secondary.execution_mode if body.secondary else None,
                        secondary_algorithm,
                        body.secondary.pool_password if body.secondary else None,
                        encoded_secondary_indices,
                        encoded_secondary_extra,
                        now,
                        sheet_id,
                    ),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="flight_sheet_updated",
                    target_type="flight_sheet",
                    target_id=sheet_id,
                    details={
                        "old_name": existing["name"],
                        "name": body.name,
                        "coin": body.coin,
                        "miner_type": body.miner_type,
                        "miner_version": miner_version,
                        "secondary_mode": body.secondary_mode,
                        "execution_mode": body.execution_mode,
                        "secondary_execution_mode": (
                            body.secondary.execution_mode if body.secondary else None
                        ),
                        "changed_fields": changed_fields,
                        "secondary_enabled": body.secondary is not None,
                        "gpu_oc_enabled": gpu_oc is not None,
                        "gpu_oc_indices": (
                            [setting["index"] for setting in gpu_oc["settings"] if "index" in setting]
                            if gpu_oc is not None
                            else []
                        ),
                        "gpu_oc_models": (
                            [setting["model_match"] for setting in gpu_oc["settings"] if "model_match" in setting]
                            if gpu_oc is not None
                            else []
                        ),
                    },
                )
                if runtime_changed_fields:
                    _persist_configuration_refreshes(
                        db,
                        {sheet_id},
                        user,
                        trigger_type="flight_sheet",
                        trigger_id=sheet_id,
                    )
        reapply = (
            reapply_active_flight_sheets(
                {sheet_id},
                user,
                trigger_type="flight_sheet",
                trigger_id=sheet_id,
                persist=False,
            )
            if runtime_changed_fields
            else configuration_reapply_result(triggered=False)
        )
        for item in list_flight_sheets(user):
            if item["id"] == sheet_id:
                response = dict(item)
                response["changed_fields"] = changed_fields
                response["reapply"] = reapply
                return response
        raise HTTPException(500, "Updated flight sheet could not be reloaded")

    @app.delete("/api/flight-sheets/{sheet_id}")
    def delete_flight_sheet(
        sheet_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sheet = db.execute(
                "SELECT id, name, coin FROM flight_sheets WHERE id=?", (sheet_id,)
            ).fetchone()
            if not sheet:
                raise HTTPException(404, "Flight sheet not found")
            agent_rows = db.execute(
                "SELECT * FROM agents ORDER BY name COLLATE NOCASE"
            ).fetchall()
            configured_agents = [
                row
                for row in agent_rows
                if _reported_agent_flight_sheet_id(row) == sheet_id
            ]
            apply_rows = db.execute(
                """SELECT id, agent_id, status, payload_json FROM commands
                   WHERE action='apply_flight_sheet'
                     AND status IN ('queued','dispatched','running')"""
            ).fetchall()
            matching_applies = [
                row
                for row in apply_rows
                if (
                    isinstance((payload := _json(row["payload_json"], {})), dict)
                    and payload.get("flight_sheet_id") == sheet_id
                )
            ]
            # A command can reach its expiry between maintenance passes.  Do
            # not let that stale ledger entry make a deletable sheet appear to
            # be permanently in flight.  Reuse the normal per-Agent expiry
            # path so the durable refresh/update ledgers stay synchronized,
            # then evaluate the live blockers again under this writer lock.
            deleted_at = utcnow()
            for agent_id in sorted({row["agent_id"] for row in matching_applies}):
                expire_stale_agent_commands(
                    db,
                    agent_id=agent_id,
                    now=deleted_at,
                )
            if matching_applies:
                apply_rows = db.execute(
                    """SELECT id, agent_id, status, payload_json FROM commands
                       WHERE action='apply_flight_sheet'
                         AND status IN ('queued','dispatched','running')"""
                ).fetchall()
                matching_applies = [
                    row
                    for row in apply_rows
                    if (
                        isinstance(
                            (payload := _json(row["payload_json"], {})), dict
                        )
                        and payload.get("flight_sheet_id") == sheet_id
                    )
                ]
            in_flight = [
                row for row in matching_applies if row["status"] != "queued"
            ]
            if configured_agents or in_flight:
                names = ", ".join(row["name"] for row in configured_agents[:5])
                reason = (
                    f"flight sheet is still configured on {len(configured_agents)} device(s)"
                    + (f": {names}" if names else "")
                )
                if in_flight:
                    reason += f"; {len(in_flight)} apply command(s) are already in flight"
                raise HTTPException(
                    409,
                    reason + "; remove the flight sheet from those devices first",
                )
            # Flight V2 used a controller-owned assignment ledger with a
            # RESTRICT foreign key.  V2 was retired, but upgraded databases can
            # still contain that legacy table and stale rows which no current
            # code reads.  Clear a row only when the corresponding Agent has
            # explicitly reported no sheet or a different sheet after the
            # assignment was written, or no longer exists.  An Agent with
            # unknown or older state remains a blocker;
            # this preserves the same fail-closed deletion rule as V1 while
            # preventing known-stale legacy rows from turning deletion into 500.
            legacy_assignment_rows_deleted = 0
            legacy_assignments_table = db.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='agent_flight_assignments'"""
            ).fetchone()
            if legacy_assignments_table:
                legacy_columns = {
                    row["name"]
                    for row in db.execute(
                        "PRAGMA table_info(agent_flight_assignments)"
                    ).fetchall()
                }
                if {"agent_id", "flight_sheet_id", "updated_at"}.issubset(
                    legacy_columns
                ):
                    agents_by_id = {row["id"]: row for row in agent_rows}
                    stale_agent_ids: list[str] = []
                    unknown_agents: list[sqlite3.Row] = []
                    legacy_rows = db.execute(
                        """SELECT agent_id, updated_at FROM agent_flight_assignments
                           WHERE flight_sheet_id=? ORDER BY agent_id""",
                        (sheet_id,),
                    ).fetchall()
                    for legacy_row in legacy_rows:
                        agent = agents_by_id.get(legacy_row["agent_id"])
                        if agent is None:
                            stale_agent_ids.append(legacy_row["agent_id"])
                            continue
                        reported_sheet_id = _reported_agent_flight_sheet_id(agent)
                        report_at = _parse_time(agent["last_seen"])
                        assignment_at = _parse_time(legacy_row["updated_at"])
                        report_supersedes_assignment = (
                            report_at is not None
                            and assignment_at is not None
                            and report_at >= assignment_at
                        )
                        if report_supersedes_assignment and (
                            (
                                reported_sheet_id is not None
                                and reported_sheet_id != sheet_id
                            )
                            or _agent_explicitly_reports_no_flight_sheet(agent)
                        ):
                            stale_agent_ids.append(agent["id"])
                        else:
                            unknown_agents.append(agent)
                    if unknown_agents:
                        names = ", ".join(row["name"] for row in unknown_agents[:5])
                        raise HTTPException(
                            409,
                            "flight sheet still has legacy assignment(s) on "
                            f"{len(unknown_agents)} device(s) whose current flight "
                            f"sheet is unknown: {names}; wait for current device "
                            "status before deleting",
                        )
                    for agent_id in stale_agent_ids:
                        legacy_assignment_rows_deleted += db.execute(
                            """DELETE FROM agent_flight_assignments
                               WHERE agent_id=? AND flight_sheet_id=?""",
                            (agent_id, sheet_id),
                        ).rowcount
            for pending in matching_applies:
                db.execute(
                    """UPDATE commands SET status='cancelled', finished_at=?
                       WHERE id=? AND status='queued'""",
                    (deleted_at, pending["id"]),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="command_cancelled",
                    target_type="command",
                    target_id=pending["id"],
                    details={"reason": "flight_sheet_deleted", "flight_sheet_id": sheet_id},
                )
            db.execute(
                """UPDATE configuration_refreshes
                   SET status='cancelled', updated_at=?, completed_at=?,
                       last_error='flight sheet deleted before refresh completed'
                   WHERE flight_sheet_id=?
                     AND status IN ('pending','preparing','queued','dispatched','running')""",
                (deleted_at, deleted_at, sheet_id),
            )
            db.execute("DELETE FROM flight_sheets WHERE id=?", (sheet_id,))
            Database.audit(
                db,
                actor=user["username"],
                action="flight_sheet_deleted",
                target_type="flight_sheet",
                target_id=sheet_id,
                details={
                    "name": sheet["name"],
                    "coin": sheet["coin"],
                    "legacy_assignment_rows_deleted": legacy_assignment_rows_deleted,
                },
            )
        return {"status": "deleted"}

    @app.post(
        "/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply", status_code=201
    )
    def apply_flight_sheet(
        agent_id: str,
        sheet_id: str,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        # Read controller state first and close the SQLite connection before a
        # first-use GitHub download.  The download may cache package metadata
        # in its own transaction and must not be blocked by this read handle.
        with database.connection() as db:
            sheet = db.execute(
                """SELECT fs.*, w.address AS wallet_address,
                          sw.address AS secondary_wallet_address
                   FROM flight_sheets fs
                   JOIN wallets w ON fs.wallet_id=w.id
                   LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                   WHERE fs.id=?""",
                (sheet_id,),
            ).fetchone()
            if not sheet:
                raise HTTPException(404, "Flight sheet not found")
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise HTTPException(404, "Agent not found")
            primary_type = (sheet["miner_type"] or "generic").lower()
            secondary_type = (
                (sheet["secondary_miner_type"] or "generic").lower()
                if sheet["secondary_coin"]
                else None
            )
            secondary_mode = str(sheet["secondary_mode"] or "integrated").lower()
            primary_execution_mode = str(
                sheet["execution_mode"] or "native"
            ).lower()
            secondary_execution_mode = (
                str(sheet["secondary_execution_mode"] or "native").lower()
                if sheet["secondary_coin"]
                else None
            )
            unsupported_types = {
                miner_type
                for miner_type in (primary_type, secondary_type)
                if miner_type is not None
                and miner_type != "generic"
                and miner_type not in MINER_TYPES
            }
            if unsupported_types:
                raise HTTPException(
                    409,
                    "该飞行表使用已移除或不受支持的矿工类型，现已停用且不会下发；"
                    "请编辑后改用当前支持的托管矿工。",
                )
            has_wsl_leg = primary_execution_mode == "wsl" or (
                secondary_execution_mode == "wsl"
            )
            _validate_flight_miner_pair(
                primary_type, secondary_type, secondary_mode
            )
            _validate_wsl_flight_modes(
                primary_type=primary_type,
                primary_algorithm=sheet["algorithm"],
                primary_execution_mode=primary_execution_mode,
                primary_extra_config=_json(sheet["extra_config_json"], {}),
                secondary_mode=secondary_mode,
                secondary_type=secondary_type,
                secondary_algorithm=sheet["secondary_algorithm"],
                secondary_execution_mode=secondary_execution_mode,
                secondary_extra_config=_json(
                    sheet["secondary_extra_config_json"], {}
                ),
                status_code=409,
            )
            gpu_oc = _json(sheet["gpu_oc_json"], None)
            if has_wsl_leg:
                _validate_wsl_target_agent(agent)
                if gpu_oc is not None:
                    raise HTTPException(
                        409,
                        "WSL 矿工暂不接收飞行表 SRBMiner OC；请改用设备详情中的 Windows NVML OC",
                    )
            for role in ("primary", "secondary"):
                if role == "secondary" and not sheet["secondary_coin"]: continue
                prefix = "" if role == "primary" else "secondary_"
                kind = sheet[prefix + "miner_type"] or "generic"
                if kind not in MINER_TYPES: continue
                opts = _json(sheet[prefix + "extra_config_json"], {})
                algo = sheet[prefix + "algorithm"]
                ver = sheet[prefix + "miner_version"]
                try:
                    _normalize_managed_options(miner_type=kind, coin=sheet[prefix + "coin"], algorithm=algo,
                        pool_url=sheet[prefix + "pool_url"], extra_config=opts,
                        miner_version=ver, pool_password=sheet[prefix + "pool_password"])
                    if parse_extra_args(opts.get("extra_args", ""), miner_type=kind):
                        _require_extra_args_target(agent)
                except (ValueError, HTTPException) as exc:
                    raise HTTPException(409, getattr(exc, "detail", str(exc))) from exc
            _validate_independent_miner_resources(
                secondary_mode=secondary_mode,
                primary_type=primary_type,
                secondary_type=secondary_type,
                primary_algorithm=sheet["algorithm"],
                secondary_algorithm=sheet["secondary_algorithm"],
                primary_extra_config=_json(sheet["extra_config_json"], {}),
                secondary_extra_config=_json(sheet["secondary_extra_config_json"], {}),
                primary_device_indices=_json(sheet["device_indices_json"], []),
                secondary_device_indices=_json(
                    sheet["secondary_device_indices_json"], []
                ),
                status_code=409,
            )
            _validate_srb_dual_configuration(
                secondary_mode=secondary_mode,
                primary_type=primary_type,
                secondary_type=secondary_type,
                primary_version=sheet["miner_version"],
                secondary_version=sheet["secondary_miner_version"],
                primary_algorithm=sheet["algorithm"],
                secondary_algorithm=sheet["secondary_algorithm"],
                primary_extra_config=_json(sheet["extra_config_json"], {}),
                secondary_extra_config=_json(sheet["secondary_extra_config_json"], {}),
                primary_device_indices=_json(sheet["device_indices_json"], []),
                secondary_device_indices=_json(
                    sheet["secondary_device_indices_json"], []
                ),
                status_code=409,
            )
            managed_types = {
                value for value in (primary_type, secondary_type) if value in MINER_TYPES
            }
            primary_package_os = (
                "linux" if primary_execution_mode == "wsl" else agent["os_family"]
            )
            secondary_package_os = (
                "linux"
                if secondary_execution_mode == "wsl"
                else agent["os_family"]
            )
            primary_variant: str | None = None
            secondary_variant: str | None = None
            if managed_types:
                match = re.match(r"^(\d+)\.(\d+)\.(\d+)", agent["agent_version"] or "")
                version = tuple(int(value) for value in match.groups()) if match else (0, 0, 0)
                advertised_capabilities = set(
                    _json(agent["capabilities_json"], [])
                )
                if has_wsl_leg:
                    if version < (0, 6, 59):
                        raise HTTPException(
                            409,
                            "WSL GPU 矿工需要 RigPilot Agent 0.6.59 或更高版本",
                        )
                    if WSL_GPU_MINING_CAPABILITY not in advertised_capabilities:
                        raise HTTPException(
                            409,
                            "该设备尚未上报 WSL GPU 挖矿能力；请先更新并启用 Agent，然后等待一次新心跳",
                        )
                if version < (0, 5, 0):
                    raise HTTPException(
                        409,
                        "This managed flight sheet requires RigPilot Agent 0.5.0 or newer",
                    )
                if (
                    version >= (0, 6, 48)
                    and MANAGED_MINING_CAPABILITY not in advertised_capabilities
                ):
                    raise HTTPException(
                        409,
                        "This device does not currently advertise managed mining; "
                        "enable managed mining locally and wait for a new heartbeat",
                    )
                if secondary_mode == "independent" and version < (0, 6, 48):
                    raise HTTPException(
                        409,
                        "Independent dual mining requires RigPilot Agent 0.6.48 or newer",
                    )
                if secondary_mode == "independent" and isinstance(gpu_oc, dict):
                    if not _independent_srb_gpu_oc_shape_supported(
                        primary_type=primary_type,
                        primary_algorithm=sheet["algorithm"],
                        secondary_type=secondary_type,
                        secondary_algorithm=sheet["secondary_algorithm"],
                        primary_extra_config=_json(sheet["extra_config_json"], {}),
                        secondary_extra_config=_json(sheet["secondary_extra_config_json"], {}),
                    ):
                        raise HTTPException(
                            409,
                            "Stored independent SRBMiner flight GPU OC does not "
                            "have one primary GPU owner and one CPU-only second miner",
                        )
                    if (
                        version < (0, 6, 57)
                        or INDEPENDENT_SRB_GPU_OC_CAPABILITY
                        not in advertised_capabilities
                    ):
                        raise HTTPException(
                            409,
                            "Independent SRBMiner flight GPU OC requires "
                            "RigPilot Agent 0.6.57 or newer",
                        )
                if isinstance(gpu_oc, dict) and version < (0, 5, 3):
                    raise HTTPException(
                        409,
                        "SRBMiner GPU OC requires RigPilot Agent 0.5.3 or newer",
                    )
                srb_algorithms = {
                    str(value or "").lower()
                    for value in (sheet["algorithm"], sheet["secondary_algorithm"])
                }
                if (
                    primary_type == "srbminer"
                    and srb_algorithms.intersection(SRB_GPU_ALGORITHMS)
                    and version < (0, 5, 3)
                ):
                    raise HTTPException(
                        409,
                        "SRBMiner discrete-GPU mapping requires RigPilot Agent 0.5.3 or newer",
                    )
                pearl_legs = (
                    (
                        "primary",
                        primary_type,
                        primary_execution_mode,
                        sheet["coin"],
                        sheet["algorithm"],
                        sheet["miner_version"],
                        sheet["pool_url"],
                        sheet["device_indices_json"],
                        sheet["extra_config_json"],
                    ),
                    (
                        "secondary",
                        secondary_type,
                        secondary_execution_mode,
                        sheet["secondary_coin"],
                        sheet["secondary_algorithm"],
                        sheet["secondary_miner_version"],
                        sheet["secondary_pool_url"],
                        sheet["secondary_device_indices_json"],
                        sheet["secondary_extra_config_json"],
                    ),
                )
                for (
                    leg_name,
                    leg_type,
                    leg_execution_mode,
                    leg_coin,
                    leg_algorithm,
                    leg_miner_version,
                    leg_pool_url,
                    leg_indices_json,
                    leg_extra_json,
                ) in pearl_legs:
                    if leg_type != "pearlminer":
                        continue
                    if version < (0, 6, 39):
                        raise HTTPException(
                            409,
                            "Pearl Miner requires RigPilot Agent 0.6.39 or newer",
                        )
                    if (
                        str(leg_coin or "").upper() != "PEARL"
                        or str(leg_algorithm or "").lower() != "pearlhash"
                        or _json(leg_indices_json, [])
                        or (leg_name == "primary" and sheet["gpu_oc_json"] is not None)
                    ):
                        raise HTTPException(
                            409,
                            f"Stored {leg_name} Pearl Miner flight sheet contains unsupported options",
                        )
                    variant = _pearl_package_variant(
                        agent,
                        _json(leg_extra_json, {}),
                        execution_os_family=(
                            "linux" if leg_execution_mode == "wsl" else None
                        ),
                    )
                    if leg_execution_mode == "wsl" and variant != "nvidia":
                        raise HTTPException(
                            409,
                            "WSL 暂时只支持 Windows 主机上的 NVIDIA GPU；无法选择该 Pearl Miner 资产",
                        )
                    if leg_name == "primary":
                        primary_variant = variant
                    else:
                        secondary_variant = variant
            primary_seine_pin = sheet["miner_package_id"] if primary_type == "seine" else None
            secondary_seine_pin = sheet["secondary_miner_package_id"] if secondary_type == "seine" else None
            primary_package: sqlite3.Row | dict[str, Any] | None = None
            if primary_seine_pin:
                primary_package = _load_seine_test_package(
                    db, primary_seine_pin, sheet["miner_version"],
                    target_os=primary_package_os, error_status=409,
                )
            if primary_type not in MINER_TYPES and sheet["miner_package_id"]:
                primary_package = db.execute(
                    "SELECT * FROM miner_packages WHERE id=?",
                    (sheet["miner_package_id"],),
                ).fetchone()
                if not primary_package:
                    raise HTTPException(409, "Primary miner package no longer exists")
            secondary_package: sqlite3.Row | dict[str, Any] | None = None
            if secondary_seine_pin:
                secondary_package = _load_seine_test_package(
                    db, secondary_seine_pin, sheet["secondary_miner_version"],
                    target_os=secondary_package_os, error_status=409,
                )
            if (
                secondary_type not in MINER_TYPES
                and sheet["secondary_miner_package_id"]
            ):
                secondary_package = db.execute(
                    "SELECT * FROM miner_packages WHERE id=?",
                    (sheet["secondary_miner_package_id"],),
                ).fetchone()
                if not secondary_package:
                    raise HTTPException(409, "Secondary miner package no longer exists")

        if primary_type in MINER_TYPES and not primary_seine_pin:
            primary_package = ensure_managed_miner_package(
                primary_type,
                primary_package_os,
                sheet["miner_version"],
                user,
                variant=primary_variant,
            )
        if secondary_type in MINER_TYPES and not secondary_seine_pin:
            if (
                secondary_type == primary_type
                and sheet["secondary_miner_version"] == sheet["miner_version"]
                and secondary_variant == primary_variant
                and secondary_seine_pin == primary_seine_pin
                and normalize_os_family(str(secondary_package_os))
                == normalize_os_family(str(primary_package_os))
            ):
                secondary_package = primary_package
            else:
                secondary_package = ensure_managed_miner_package(
                    str(secondary_type),
                    secondary_package_os,
                    sheet["secondary_miner_version"],
                    user,
                    variant=secondary_variant,
                )
        if not _agent_version_at_least(agent["agent_version"], (0, 6, 48)):
            for package in (primary_package, secondary_package):
                package_id = _optional_row_value(package, "id") if package else None
                if isinstance(package_id, str) and package_id.startswith("test-"):
                    raise HTTPException(
                        409,
                        "Controller-classified test miner packages require "
                        "RigPilot Agent 0.6.48 or newer; upgrade this device first",
                    )

        primary_package_payload = _miner_package_command_payload(primary_package)
        if primary_execution_mode == "wsl" and primary_package_payload is not None:
            primary_package_payload["managed_os_family"] = "linux"
        secondary_package_payload = _miner_package_command_payload(secondary_package)
        if secondary_execution_mode == "wsl" and secondary_package_payload is not None:
            secondary_package_payload["managed_os_family"] = "linux"

        secondary = None
        if sheet["secondary_coin"]:
            secondary = {
                "miner_type": secondary_type,
                "coin": sheet["secondary_coin"],
                "wallet_address": sheet["secondary_wallet_address"],
                "pool_url": sheet["secondary_pool_url"],
                "miner_profile": sheet["secondary_miner_profile"],
                "miner_package": secondary_package_payload,
                "algorithm": sheet["secondary_algorithm"],
                "pool_password": _effective_pool_password(
                    sheet["secondary_pool_password"]
                ),
                "device_indices": _json(sheet["secondary_device_indices_json"], []),
                "extra_config": _json(sheet["secondary_extra_config_json"], {}),
            }
            if secondary_execution_mode == "wsl":
                secondary["execution_mode"] = "wsl"
        payload = {
            "flight_sheet_id": sheet["id"],
            "name": sheet["name"],
            "worker_name": _render_worker_name(sheet["worker_name_template"], agent),
            "primary": {
                "miner_type": primary_type,
                "coin": sheet["coin"],
                "wallet_address": sheet["wallet_address"],
                "pool_url": sheet["pool_url"],
                "miner_profile": sheet["miner_profile"],
                "miner_package": primary_package_payload,
                "algorithm": sheet["algorithm"],
                "pool_password": _effective_pool_password(sheet["pool_password"]),
                "device_indices": _json(sheet["device_indices_json"], []),
                "extra_config": _json(sheet["extra_config_json"], {}),
            },
            "secondary": secondary,
        }
        if primary_execution_mode == "wsl":
            payload["primary"]["execution_mode"] = "wsl"
        if secondary_mode == "independent":
            # Older Agents reject unknown wire fields, so integrated sheets
            # intentionally retain their historical payload shape.
            payload["secondary_mode"] = "independent"
        gpu_oc = _json(sheet["gpu_oc_json"], None)
        if gpu_oc is not None:
            payload["gpu_oc"] = gpu_oc
        has_extra_args = _flight_has_extra_args(payload)
        _validate_prepared_flight_hardware(agent, payload)
        minimum_agent_version = (0, 8, 31) if has_extra_args else (0, 0, 0)
        if managed_types:
            minimum_agent_version = max(minimum_agent_version, (0, 5, 0))
        if gpu_oc is not None:
            minimum_agent_version = max(minimum_agent_version, (0, 5, 3))
        if "pearlminer" in managed_types:
            minimum_agent_version = max(minimum_agent_version, (0, 6, 39))
        if has_wsl_leg:
            minimum_agent_version = max(minimum_agent_version, (0, 6, 59))
        if secondary_mode == "independent":
            minimum_agent_version = max(minimum_agent_version, (0, 6, 48))
            if gpu_oc is not None:
                minimum_agent_version = max(minimum_agent_version, (0, 6, 57))
        package_ids = {
            str(_optional_row_value(package, "id") or "")
            for package in (primary_package, secondary_package)
            if package is not None
        }
        if any(package_id.startswith("test-") for package_id in package_ids):
            minimum_agent_version = max(minimum_agent_version, (0, 6, 48))
        required_capabilities: list[str] = []
        if has_extra_args:
            required_capabilities.append(EXTRA_ARGS_CAPABILITY)
        advertised_capabilities = set(_json(agent["capabilities_json"], []))
        if secondary_mode == "independent" or (
            managed_types and MANAGED_MINING_CAPABILITY in advertised_capabilities
        ):
            required_capabilities.append(MANAGED_MINING_CAPABILITY)
        if secondary_mode == "independent" and gpu_oc is not None:
            required_capabilities.append(INDEPENDENT_SRB_GPU_OC_CAPABILITY)
        if has_wsl_leg:
            required_capabilities.append(WSL_GPU_MINING_CAPABILITY)
        try:
            expected_os_family: str | None = normalize_os_family(agent["os_family"])
        except ValueError:
            expected_os_family = None
        payload[CONTROLLER_FLIGHT_DISPATCH_MARKER] = {
            "os_family": expected_os_family,
            "minimum_agent_version": ".".join(
                str(value) for value in minimum_agent_version
            ),
            "required_capabilities": required_capabilities,
        }
        # Legacy sheets keep the pre-0.5 wire shape so older Agents can still
        # apply them. Managed sheets include the explicit adapter.
        if primary_type == "generic":
            payload["primary"].pop("miner_type", None)
        if secondary is not None and secondary_type == "generic":
            secondary.pop("miner_type", None)
        prepared_fingerprint = _flight_sheet_configuration_fingerprint(sheet)
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current_sheet = db.execute(
                """SELECT fs.*, w.address AS wallet_address,
                          sw.address AS secondary_wallet_address
                   FROM flight_sheets fs
                   JOIN wallets w ON fs.wallet_id=w.id
                   LEFT JOIN wallets sw ON fs.secondary_wallet_id=sw.id
                   WHERE fs.id=?""",
                (sheet_id,),
            ).fetchone()
            if not current_sheet:
                raise HTTPException(404, "Flight sheet not found")
            current_fingerprint = _flight_sheet_configuration_fingerprint(current_sheet)
            if current_fingerprint != prepared_fingerprint:
                raise HTTPException(
                    409, "Flight sheet changed while command was prepared; retry"
                )

            latest_agent = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if latest_agent is None:
                raise HTTPException(404, "Agent not found")
            if (
                latest_agent["os_family"] != agent["os_family"]
                or latest_agent["agent_version"] != agent["agent_version"]
            ):
                raise HTTPException(
                    409, "Device changed while command was prepared; retry"
                )
            latest_capabilities = set(
                _json(latest_agent["capabilities_json"], [])
            )
            _validate_prepared_flight_hardware(latest_agent, payload)
            for role, pin_id, current_version, package_os, prepared_package in (
                ("primary", primary_seine_pin, current_sheet["miner_version"], primary_package_os, primary_package_payload),
                ("secondary", secondary_seine_pin, current_sheet["secondary_miner_version"], secondary_package_os, secondary_package_payload),
            ):
                if pin_id:
                    current_package = _load_seine_test_package(
                        db, pin_id, current_version, target_os=package_os, error_status=409,
                    )
                    if _miner_package_command_payload(current_package) != prepared_package:
                        raise HTTPException(409, f"Selected Seine {role} test package changed while command was prepared")
            if has_wsl_leg:
                if WSL_GPU_MINING_CAPABILITY not in latest_capabilities:
                    raise HTTPException(
                        409,
                        "设备在命令准备期间停止上报 WSL GPU 挖矿能力；请等待新心跳后重试",
                    )
            payload["worker_name"] = _render_worker_name(
                current_sheet["worker_name_template"], latest_agent
            )
            runtime_fingerprint = _flight_runtime_payload_fingerprint(payload)
            if MINING_CONFIG_FINGERPRINT_CAPABILITY in latest_capabilities:
                payload[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = runtime_fingerprint
            payload[CONTROLLER_FLIGHT_RUNTIME_MARKER] = {
                "version": 1,
                "runtime_fingerprint": runtime_fingerprint,
                "configuration_fingerprint": current_fingerprint,
                "result_binding_required": (
                    MINING_CONFIG_FINGERPRINT_CAPABILITY
                    in latest_capabilities
                ),
                "roles": (
                    ["primary", "secondary"]
                    if secondary_mode == "independent"
                    else ["primary"]
                ),
            }

            gpu_oc_transition = db.execute(
                """SELECT action FROM commands
                   WHERE agent_id=? AND action IN (?, ?)
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   ORDER BY created_at DESC LIMIT 1""",
                (
                    agent_id,
                    "apply_gpu_profile",
                    GPU_OC_STATE_MIGRATION_ACTION,
                    utcnow(),
                ),
            ).fetchone()
            if gpu_oc_transition:
                raise HTTPException(
                    409,
                    "Flight sheet cannot be applied while GPU OC command "
                    f"{gpu_oc_transition['action']} is in progress",
                )

            automatic_reapply = user.get("_automatic_reapply")
            now = utcnow()
            if isinstance(automatic_reapply, dict):
                desired_apply = automatic_reapply.get("desired_apply") is True
                active_claim = db.execute(
                    """SELECT 1 FROM configuration_refreshes
                       WHERE agent_id=? AND generation_id=? AND claim_token=?
                         AND desired_fingerprint=? AND status='preparing'""",
                    (
                        agent_id,
                        automatic_reapply.get("generation_id"),
                        automatic_reapply.get("claim_token"),
                        current_fingerprint,
                    ),
                ).fetchone()
                if active_claim is None:
                    raise HTTPException(
                        409,
                        "Automatic configuration refresh skipped: request was "
                        "superseded while command was prepared",
                    )
                if (
                    not desired_apply
                    and _active_agent_flight_sheet_id(latest_agent, settings) != sheet_id
                ):
                    raise HTTPException(
                        409,
                        "Automatic configuration refresh skipped: device is no longer "
                        "actively mining this flight sheet",
                    )
                automatic_reapply["configuration_fingerprint"] = current_fingerprint

            conflict = db.execute(
                """SELECT action FROM commands
                   WHERE agent_id=?
                     AND action IN (
                       'stop_mining','restart_mining','remove_flight_sheet',
                       'restart_service','reboot','shutdown','agent_update.v1',
                       'apply_gpu_profile','migrate_gpu_oc_state.v1',
                       'scan_mining_residue.v1','cleanup_mining_residue.v1',
                       'scan_miner_cache.v1','cleanup_miner_cache.v1',
                       'configure_linux_ssh.v1'
                     )
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   ORDER BY created_at DESC LIMIT 1""",
                (agent_id, now),
            ).fetchone()
            if conflict:
                if isinstance(automatic_reapply, dict):
                    detail = (
                        "Automatic configuration refresh skipped: conflicting command "
                        f"{conflict['action']}"
                    )
                else:
                    detail = (
                        "Flight sheet cannot be applied while command "
                        f"{conflict['action']} is in progress"
                    )
                raise HTTPException(409, detail)

            existing_applies = db.execute(
                """SELECT id, status FROM commands
                   WHERE agent_id=? AND action='apply_flight_sheet'
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   ORDER BY created_at, rowid""",
                (agent_id, now),
            ).fetchall()
            in_flight = next(
                (
                    candidate
                    for candidate in existing_applies
                    if candidate["status"] in {"dispatched", "running"}
                ),
                None,
            )
            if in_flight is not None:
                if isinstance(automatic_reapply, dict):
                    detail = (
                        "Automatic configuration refresh skipped: another flight-sheet "
                        "command is already in progress"
                    )
                else:
                    detail = "A flight-sheet command is already in progress"
                raise HTTPException(409, detail)

            cancel_reason = (
                "superseded_by_configuration_update"
                if isinstance(automatic_reapply, dict)
                else "superseded_by_manual_apply"
            )
            for candidate in existing_applies:
                db.execute(
                    """UPDATE commands SET status='cancelled', finished_at=?
                       WHERE id=? AND status='queued'""",
                    (now, candidate["id"]),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="command_cancelled",
                    target_type="command",
                    target_id=candidate["id"],
                    details={"reason": cancel_reason},
                )

            command = queue_agent_command(
                db,
                agent_id=agent_id,
                action="apply_flight_sheet",
                payload=payload,
                user=user,
            )
            if not isinstance(automatic_reapply, dict):
                requested_at = utcnow()
                db.execute(
                    """INSERT INTO configuration_refreshes
                       (agent_id,flight_sheet_id,desired_fingerprint,generation_id,
                        claim_token,trigger_type,
                        trigger_id,status,command_id,requested_by,requested_at,
                        updated_at,queued_at,completed_at,attempt_count,last_error)
                       VALUES(?,?,?,?,NULL,'flight_sheet',?,'queued',?,?,?,?,?,NULL,1,NULL)
                       ON CONFLICT(agent_id) DO UPDATE SET
                          flight_sheet_id=excluded.flight_sheet_id,
                          desired_fingerprint=excluded.desired_fingerprint,
                          generation_id=excluded.generation_id,
                          claim_token=NULL,
                          trigger_type=excluded.trigger_type,
                         trigger_id=excluded.trigger_id,
                         status='queued', command_id=excluded.command_id,
                         requested_by=excluded.requested_by,
                         requested_at=excluded.requested_at,
                         updated_at=excluded.updated_at,
                         queued_at=excluded.queued_at,
                         completed_at=NULL, attempt_count=1, last_error=NULL""",
                    (
                        agent_id,
                        sheet_id,
                        current_fingerprint,
                        uuid.uuid4().hex,
                        f"desired:manual:{sheet_id}",
                        command["id"],
                        user["id"],
                        requested_at,
                        requested_at,
                        requested_at,
                    ),
                )
            return command

    @app.post(
        "/api/flight-sheets/{sheet_id}/apply-batch", status_code=201
    )
    def apply_flight_sheet_batch(
        sheet_id: str,
        body: FlightSheetBatchApplyRequest,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        """Apply one flight sheet to only the explicitly selected devices."""

        with database.connection() as db:
            sheet = db.execute(
                "SELECT id, name FROM flight_sheets WHERE id=?", (sheet_id,)
            ).fetchone()
        if sheet is None:
            raise HTTPException(404, "Flight sheet not found")

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for agent_id in body.agent_ids:
            try:
                # This deliberately invokes the authoritative single-device
                # path. Platform, Agent capability, package, WSL, dual-miner,
                # OC and command-conflict checks must never diverge here.
                queued = apply_flight_sheet(agent_id, sheet_id, user)
            except HTTPException as exc:
                rejected.append(
                    {
                        "agent_id": agent_id,
                        "accepted": False,
                        "status_code": exc.status_code,
                        "detail": str(exc.detail),
                    }
                )
            else:
                accepted.append({"accepted": True, **queued})

        with database.connection() as db:
            Database.audit(
                db,
                actor=user["username"],
                action="flight_sheet_batch_apply_requested",
                target_type="flight_sheet",
                target_id=sheet_id,
                details={
                    "selected_agent_ids": body.agent_ids,
                    "accepted_agent_ids": [
                        item["agent_id"] for item in accepted
                    ],
                    "rejected_agent_ids": [
                        item["agent_id"] for item in rejected
                    ],
                },
            )
        return {
            "sheet_id": sheet_id,
            "sheet_name": sheet["name"],
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "accepted": accepted,
            "rejected": rejected,
        }

    def _load_agent_gpu_oc_context(
        db: sqlite3.Connection,
        agent_id: str,
        *,
        operation: str,
    ) -> tuple[sqlite3.Row, list[dict[str, Any]], dict[str, dict[str, Any]]]:
        agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not agent:
            raise HTTPException(404, "Agent not found")
        if _serialize_agent(agent, settings)["status"] != "online":
            raise HTTPException(409, f"GPU OC {operation} requires an online device")
        if not _agent_version_at_least(agent["agent_version"], (0, 6, 16)):
            raise HTTPException(
                409,
                "Per-GPU OC requires RigPilot Agent 0.6.16 or newer; "
                "upgrade this device before changing OC",
            )
        metrics = _json(agent["metrics_json"], {})
        raw_gpus = metrics.get("gpus") if isinstance(metrics, dict) else None
        reported_gpus: list[dict[str, Any]] = []
        by_uuid: dict[str, dict[str, Any]] = {}
        duplicate_uuids: set[str] = set()
        if isinstance(raw_gpus, list):
            for raw_gpu in raw_gpus[:32]:
                if not isinstance(raw_gpu, dict):
                    continue
                candidate_index = raw_gpu.get("index")
                if (
                    isinstance(candidate_index, str)
                    and candidate_index.isdigit()
                ):
                    candidate_index = int(candidate_index)
                if (
                    not isinstance(candidate_index, int)
                    or isinstance(candidate_index, bool)
                    or not 0 <= candidate_index <= 31
                ):
                    continue
                candidate = {**raw_gpu, "index": candidate_index}
                reported_gpus.append(candidate)
                candidate_uuid = str(candidate.get("uuid") or "").strip()
                candidate_vendor = str(
                    candidate.get("vendor") or "nvidia"
                ).strip().casefold()
                if not candidate_uuid or candidate_vendor != "nvidia":
                    continue
                if candidate_uuid in by_uuid:
                    duplicate_uuids.add(candidate_uuid)
                else:
                    by_uuid[candidate_uuid] = candidate
        for duplicate_uuid in duplicate_uuids:
            by_uuid.pop(duplicate_uuid, None)
        return agent, reported_gpus, by_uuid

    def _require_current_nvidia_gpu(
        by_uuid: dict[str, dict[str, Any]],
        gpu_uuid: str,
        *,
        expected_index: int | None = None,
    ) -> dict[str, Any]:
        matched_gpu = by_uuid.get(gpu_uuid)
        if matched_gpu is None or (
            expected_index is not None
            and matched_gpu["index"] != expected_index
        ):
            raise HTTPException(
                409,
                "The selected NVIDIA GPU identity is no longer reported by this device",
            )
        return matched_gpu

    def _validate_agent_gpu_oc_setting(
        matched_gpu: dict[str, Any],
        setting: dict[str, Any],
    ) -> None:
        reported_capabilities = matched_gpu.get("oc_capabilities")
        if isinstance(reported_capabilities, list):
            supported_fields = {
                value
                for value in reported_capabilities
                if isinstance(value, str)
            }
        else:
            # Older telemetry can safely prove only a bounded power limit.
            minimum = matched_gpu.get("power_min_limit_w")
            maximum = matched_gpu.get("power_max_limit_w")
            supported_fields = set()
            if (
                isinstance(minimum, (int, float))
                and not isinstance(minimum, bool)
                and isinstance(maximum, (int, float))
                and not isinstance(maximum, bool)
                and maximum >= minimum
                and maximum > 0
            ):
                supported_fields.add("power_limit_w")
        requested_fields = set(setting) - {"index"}
        unsupported_fields = sorted(requested_fields - supported_fields)
        if unsupported_fields:
            raise HTTPException(
                409,
                "The selected GPU did not confirm NVML support for: "
                + ", ".join(unsupported_fields),
            )
        if "power_limit_w" in requested_fields and setting["power_limit_w"] is not None:
            minimum = matched_gpu.get("power_min_limit_w")
            maximum = matched_gpu.get("power_max_limit_w")
            if (
                isinstance(minimum, (int, float))
                and not isinstance(minimum, bool)
                and isinstance(maximum, (int, float))
                and not isinstance(maximum, bool)
                and maximum >= minimum
                and not minimum <= setting["power_limit_w"] <= maximum
            ):
                raise HTTPException(
                    409,
                    "The requested power limit is outside this GPU's hardware range "
                    f"{minimum:g}-{maximum:g} W",
                )
        if "fan_percent" in requested_fields and setting["fan_percent"] is not None:
            fan_minimum = matched_gpu.get("fan_min_percent")
            fan_maximum = matched_gpu.get("fan_max_percent")
            if not (
                isinstance(fan_minimum, int)
                and not isinstance(fan_minimum, bool)
                and isinstance(fan_maximum, int)
                and not isinstance(fan_maximum, bool)
                and 0 <= fan_minimum <= fan_maximum <= 100
            ):
                raise HTTPException(
                    409,
                    "The selected GPU did not report a trustworthy NVML fan range",
                )
            if not fan_minimum <= setting["fan_percent"] <= fan_maximum:
                raise HTTPException(
                    409,
                    "The requested fan speed is outside this GPU's hardware range "
                    f"{fan_minimum}-{fan_maximum}%",
                )

    def _ensure_agent_gpu_oc_idle(
        db: sqlite3.Connection,
        agent_id: str,
    ) -> None:
        now = utcnow()
        expire_stale_agent_commands(db, agent_id=agent_id, now=now)
        mining_transition = db.execute(
            """SELECT action FROM commands
               WHERE agent_id=?
                 AND action IN (
                   'apply_flight_sheet','stop_mining','restart_mining',
                   'remove_flight_sheet','scan_mining_residue.v1',
                   'cleanup_mining_residue.v1','scan_miner_cache.v1',
                   'cleanup_miner_cache.v1'
                 )
                 AND status IN ('queued','dispatched','running')
                 AND expires_at >= ?
               ORDER BY created_at DESC LIMIT 1""",
            (agent_id, now),
        ).fetchone()
        if mining_transition:
            raise HTTPException(
                409,
                "GPU OC cannot be edited while mining command "
                f"{mining_transition['action']} is in progress",
            )
        unfinished = db.execute(
            """SELECT action FROM commands
               WHERE agent_id=?
                 AND action IN ('apply_gpu_profile','migrate_gpu_oc_state.v1')
                  AND status IN ('queued','dispatched','running')
                  AND expires_at >= ?
               LIMIT 1""",
            (agent_id, now),
        ).fetchone()
        if unfinished:
            if unfinished["action"] == GPU_OC_STATE_MIGRATION_ACTION:
                raise HTTPException(
                    409,
                    "GPU OC state migration is in progress; retry after it finishes",
                )
            raise HTTPException(409, "A GPU OC command is already in progress")
        device_transition = db.execute(
            """SELECT action FROM commands
               WHERE agent_id=?
                 AND action IN (
                   'restart_service','reboot','shutdown','agent_update.v1',
                   'configure_linux_ssh.v1'
                 )
                 AND status IN ('queued','dispatched','running')
                 AND expires_at >= ?
               ORDER BY created_at DESC LIMIT 1""",
            (agent_id, now),
        ).fetchone()
        if device_transition:
            raise HTTPException(
                409,
                "GPU OC cannot be edited while device command "
                f"{device_transition['action']} is in progress",
            )

    def _gpu_oc_profile_name(
        matched_gpu: dict[str, Any],
        gpu_index: int,
    ) -> str:
        gpu_name = re.sub(
            r"[\x00-\x1f\x7f]+",
            " ",
            str(matched_gpu.get("name") or f"GPU {gpu_index}"),
        ).strip()
        gpu_name = re.sub(r"\s+", " ", gpu_name) or f"GPU {gpu_index}"
        return f"{gpu_name[:55]} · GPU {gpu_index}"[:80]

    def _queue_agent_gpu_oc_apply_targets(
        db: sqlite3.Connection,
        *,
        agent: sqlite3.Row,
        reported_gpus: list[dict[str, Any]],
        targets: list[tuple[str, dict[str, Any], dict[str, Any]]],
        user: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """Apply explicit clears immediately; ordinary sets retain deferred behavior."""

        desired = _desired_gpu_oc_entries(
            _optional_row_value(agent, "desired_gpu_oc_json", "[]")
        )
        pending_before = set(
            _desired_gpu_oc_pending_uuids(
                _optional_row_value(agent, "desired_gpu_oc_pending_json", "[]")
            )
        )
        previous_by_uuid = {
            entry["gpu_uuid"]: entry
            for entry in desired
        }
        current_indices = {
            str(candidate.get("uuid") or "").strip(): int(candidate["index"])
            for candidate in reported_gpus
            if str(candidate.get("uuid") or "").strip()
        }
        selected_uuids = {gpu_uuid for gpu_uuid, _, _ in targets}
        apply_now = (_active_agent_flight_sheet_id(agent, settings) is not None
                     or any(value is None for _, _, requested in targets for key, value in requested.items() if key != "index"))
        reset_uuids: set[str] = set()
        updated_desired = [
            {
                **entry,
                "setting": {
                    **entry["setting"],
                    "index": current_indices.get(
                        entry["gpu_uuid"], int(entry["setting"]["index"])
                    ),
                },
            }
            for entry in desired
            if entry["gpu_uuid"] not in selected_uuids
        ]
        commands: list[dict[str, Any]] = []
        for gpu_uuid, matched_gpu, requested_setting in targets:
            gpu_index = int(matched_gpu["index"])
            previous = previous_by_uuid.get(gpu_uuid)
            cleared_fields = {field for field, value in requested_setting.items() if field != "index" and value is None}
            merged_setting = {
                **(previous["setting"] if previous is not None else {}),
                **{key: value for key, value in requested_setting.items() if value is not None},
                "index": gpu_index,
            }
            for field in cleared_fields:
                merged_setting.pop(field, None)
            reset_all = len(merged_setting) == 1
            if reset_all and previous is None:
                raise HTTPException(409, "该卡没有控制端已保存的 OC 参数可清除，请先核实原有记录")
            if reset_all:
                reset_uuids.add(gpu_uuid)
            profile_name = _gpu_oc_profile_name(matched_gpu, gpu_index)
            updated_entry = (
                {**previous, "name": profile_name, "setting": {**previous["setting"], "index": gpu_index}}
                if reset_all else {
                    "gpu_uuid": gpu_uuid, "name": profile_name,
                    "setting": merged_setting, "intent_id": uuid.uuid4().hex,
                }
            )
            updated_desired.append(updated_entry)
            payload = {
                "gpu_profile_id": f"device-oc-{uuid.uuid4().hex}",
                "name": profile_name,
                "vendor": "nvidia",
                "mode": "reset_one" if reset_all else "replace_one",
                "expected_gpu_uuid": gpu_uuid,
                "previous_index": (
                    int(previous["setting"]["index"])
                    if previous is not None
                    else gpu_index
                ),
                "settings": [{**previous["setting"], "index": gpu_index} if reset_all else merged_setting],
            }
            command = None
            if apply_now:
                command = queue_agent_command(
                    db,
                    agent_id=agent["id"],
                    action="apply_gpu_profile",
                    payload=payload,
                    user=user,
                    gpu_oc_intent=_gpu_oc_intent_marker(updated_entry),
                )
                commands.append(command)
            Database.audit(
                db,
                actor=user["username"],
                action="desired_gpu_oc_saved",
                target_type="agent",
                target_id=agent["id"],
                details={
                    "command_id": command["id"] if command is not None else None,
                    "gpu_index": gpu_index,
                    "gpu_uuid": gpu_uuid,
                    "fields": sorted(set(merged_setting) - {"index"}),
                    "cleared_fields": sorted(cleared_fields),
                    "reset_all": reset_all,
                    "deferred_until_mining": not apply_now,
                },
            )
        updated_desired.sort(key=lambda entry: int(entry["setting"]["index"]))
        updated_uuids = {entry["gpu_uuid"] for entry in updated_desired if not _gpu_oc_replay_is_blocked(entry)}
        pending_after = (
            pending_before.intersection(updated_uuids) | (selected_uuids - reset_uuids)
        )
        db.execute(
            """UPDATE agents
               SET desired_gpu_oc_json=?, desired_gpu_oc_pending_json=?
               WHERE id=?""",
            (
                json.dumps(
                    updated_desired, separators=(",", ":"), ensure_ascii=False
                ),
                json.dumps(sorted(pending_after), separators=(",", ":")),
                agent["id"],
            ),
        )
        return commands, updated_desired, apply_now

    @app.post("/api/agents/{agent_id}/gpu-oc/apply", status_code=201)
    def apply_agent_gpu_oc(
        agent_id: str,
        body: AgentGpuOcApply,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        setting = body.setting.model_dump(exclude_unset=True)
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent, reported_gpus, by_uuid = _load_agent_gpu_oc_context(
                db, agent_id, operation="apply"
            )
            matched_gpu = _require_current_nvidia_gpu(
                by_uuid,
                body.gpu_uuid,
                expected_index=setting["index"],
            )
            _validate_agent_gpu_oc_setting(matched_gpu, setting)
            _ensure_agent_gpu_oc_idle(db, agent_id)
            commands, desired, apply_now = _queue_agent_gpu_oc_apply_targets(
                db,
                agent=agent,
                reported_gpus=reported_gpus,
                targets=[(body.gpu_uuid, matched_gpu, setting)],
                user=user,
            )
            if apply_now:
                return {**commands[0], "deferred": False}
            return {
                "agent_id": agent_id,
                "action": "apply_gpu_profile",
                "status": "saved",
                "deferred": True,
                "desired_gpu_oc": desired,
            }

    @app.post("/api/agents/{agent_id}/gpu-oc/batch-apply", status_code=201)
    def apply_agent_gpu_oc_batch(
        agent_id: str,
        body: AgentGpuOcBatchApply,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        """Apply one shared setting only to an explicit UUID selection."""

        shared_setting = body.setting.model_dump(exclude_unset=True)
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent, reported_gpus, by_uuid = _load_agent_gpu_oc_context(
                db, agent_id, operation="batch apply"
            )
            targets: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
            for gpu_uuid in body.gpu_uuids:
                matched_gpu = _require_current_nvidia_gpu(by_uuid, gpu_uuid)
                setting = {**shared_setting, "index": int(matched_gpu["index"])}
                _validate_agent_gpu_oc_setting(matched_gpu, setting)
                targets.append((gpu_uuid, matched_gpu, setting))
            # Do not create the first command until every selected card passes.
            _ensure_agent_gpu_oc_idle(db, agent_id)
            commands, _desired, apply_now = _queue_agent_gpu_oc_apply_targets(
                db,
                agent=agent,
                reported_gpus=reported_gpus,
                targets=targets,
                user=user,
            )
            Database.audit(
                db,
                actor=user["username"],
                action="desired_gpu_oc_batch_saved",
                target_type="agent",
                target_id=agent_id,
                details={
                    "gpu_uuids": body.gpu_uuids,
                    "command_ids": [command["id"] for command in commands],
                    "fields": sorted(shared_setting),
                },
            )
        return {
            "agent_id": agent_id,
            "status": "queued" if apply_now else "saved",
            "deferred": not apply_now,
            "results": [
                {
                    "gpu_uuid": gpu_uuid,
                    "gpu_index": int(matched_gpu["index"]),
                    "command_id": (
                        commands[index]["id"] if apply_now else None
                    ),
                    "status": (
                        commands[index]["status"] if apply_now else "saved"
                    ),
                }
                for index, (gpu_uuid, matched_gpu, _) in enumerate(
                    targets
                )
            ],
        }

    @app.post("/api/agents/{agent_id}/gpu-oc/batch-clear", status_code=201)
    def clear_agent_gpu_oc_batch(
        agent_id: str,
        body: AgentGpuOcBatchClear,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        """Queue UUID-bound reset_one commands for an explicit saved selection."""

        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent, _reported_gpus, by_uuid = _load_agent_gpu_oc_context(
                db, agent_id, operation="batch clear"
            )
            desired = _desired_gpu_oc_entries(
                _optional_row_value(agent, "desired_gpu_oc_json", "[]")
            )
            desired_by_uuid = {
                entry["gpu_uuid"]: entry
                for entry in desired
            }
            targets: list[
                tuple[str, dict[str, Any], dict[str, Any]]
            ] = []
            for gpu_uuid in body.gpu_uuids:
                matched_gpu = _require_current_nvidia_gpu(by_uuid, gpu_uuid)
                selected = desired_by_uuid.get(gpu_uuid)
                if selected is None:
                    raise HTTPException(404, "Saved GPU OC setting not found")
                targets.append((gpu_uuid, matched_gpu, selected))
            # Keep desired state until each Agent-confirmed reset succeeds.
            _ensure_agent_gpu_oc_idle(db, agent_id)
            commands: list[dict[str, Any]] = []
            for gpu_uuid, matched_gpu, selected in targets:
                gpu_index = int(matched_gpu["index"])
                reset_command = queue_agent_command(
                    db,
                    agent_id=agent_id,
                    action="apply_gpu_profile",
                    payload={
                        "gpu_profile_id": f"device-oc-reset-{uuid.uuid4().hex}",
                        "name": str(selected["name"])[:80],
                        "vendor": "nvidia",
                        "mode": "reset_one",
                        "expected_gpu_uuid": gpu_uuid,
                        "previous_index": int(selected["setting"]["index"]),
                        "settings": [
                            {**selected["setting"], "index": gpu_index}
                        ],
                    },
                    user=user,
                    gpu_oc_intent=_gpu_oc_intent_marker(selected),
                )
                commands.append(reset_command)
                Database.audit(
                    db,
                    actor=user["username"],
                    action="desired_gpu_oc_reset_queued",
                    target_type="agent",
                    target_id=agent_id,
                    details={
                        "gpu_index": gpu_index,
                        "gpu_uuid": gpu_uuid,
                        "reset_command_id": reset_command["id"],
                    },
                )
            Database.audit(
                db,
                actor=user["username"],
                action="desired_gpu_oc_batch_reset_queued",
                target_type="agent",
                target_id=agent_id,
                details={
                    "gpu_uuids": body.gpu_uuids,
                    "command_ids": [command["id"] for command in commands],
                },
            )
        return {
            "agent_id": agent_id,
            "status": "reset_pending",
            "results": [
                {
                    "gpu_uuid": gpu_uuid,
                    "gpu_index": int(matched_gpu["index"]),
                    "command_id": command["id"],
                    "status": command["status"],
                }
                for (gpu_uuid, matched_gpu, _), command in zip(
                    targets, commands, strict=True
                )
            ],
        }
    @app.delete("/api/agents/{agent_id}/gpu-oc/{gpu_index}")
    def delete_agent_gpu_oc(
        agent_id: str,
        gpu_index: int,
        gpu_uuid: str = Query(min_length=4, max_length=128),
        forget_stale: bool = Query(False),
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        """Reset a current card, or explicitly forget a card absent from telemetry."""

        if not 0 <= gpu_index <= 31 or any(
            ord(character) < 32 or ord(character) == 127 for character in gpu_uuid
        ):
            raise HTTPException(422, "Invalid GPU identity")
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None:
                raise HTTPException(404, "Agent not found")
            if _serialize_agent(agent, settings)["status"] != "online":
                raise HTTPException(409, "Deleting saved GPU OC requires an online device")
            if not _agent_version_at_least(agent["agent_version"], (0, 6, 16)):
                raise HTTPException(
                    409,
                    "Deleting saved per-GPU OC requires RigPilot Agent 0.6.16 or newer",
                )
            desired = _desired_gpu_oc_entries(
                _optional_row_value(agent, "desired_gpu_oc_json", "[]")
            )
            selected = next(
                (entry for entry in desired if entry["gpu_uuid"] == gpu_uuid),
                None,
            )
            if selected is None:
                raise HTTPException(404, "Saved GPU OC setting not found")
            metrics = _json(agent["metrics_json"], {})
            reported_gpus = metrics.get("gpus") if isinstance(metrics, dict) else None
            current_match = [
                candidate for candidate in (reported_gpus[:32] if isinstance(reported_gpus, list) else [])
                if isinstance(candidate, dict)
                and candidate.get("index") == gpu_index
                and str(candidate.get("uuid") or "").strip() == gpu_uuid
            ]
            forget_without_reset = False
            if len(current_match) != 1:
                uuid_is_reported = bool(
                    isinstance(reported_gpus, list)
                    and any(
                        isinstance(candidate, dict)
                        and str(candidate.get("uuid") or "").strip() == gpu_uuid
                        for candidate in reported_gpus[:32]
                    )
                )
                if (
                    not forget_stale
                    or not isinstance(reported_gpus, list)
                    or uuid_is_reported
                ):
                    raise HTTPException(
                        409, "The selected GPU identity is no longer reported"
                    )
                forget_without_reset = True
            mining_transition = db.execute(
                """SELECT action FROM commands
                   WHERE agent_id=?
                     AND action IN (
                       'apply_flight_sheet','stop_mining','restart_mining',
                       'remove_flight_sheet','scan_mining_residue.v1',
                       'cleanup_mining_residue.v1','scan_miner_cache.v1',
                       'cleanup_miner_cache.v1'
                     )
                     AND status IN ('queued','dispatched','running')
                     AND expires_at >= ?
                   ORDER BY created_at DESC LIMIT 1""",
                (agent_id, utcnow()),
            ).fetchone()
            if mining_transition:
                raise HTTPException(
                    409,
                    "Saved GPU OC cannot be deleted while mining command "
                    f"{mining_transition['action']} is in progress",
                )
            in_flight = db.execute(
                """SELECT id FROM commands
                   WHERE agent_id=? AND action='apply_gpu_profile'
                     AND status IN ('dispatched','running')
                     AND expires_at >= ? LIMIT 1""",
                (agent_id, utcnow()),
            ).fetchone()
            if in_flight:
                raise HTTPException(409, "A GPU OC command is already in progress")
            cancel_queued_gpu_oc_commands(
                db,
                agent_id=agent_id,
                user=user,
                reason="saved_gpu_oc_deleted",
                expected_gpu_uuid=gpu_uuid,
            )
            if forget_without_reset:
                remaining = [
                    entry for entry in desired if entry["gpu_uuid"] != gpu_uuid
                ]
                remaining_pending = [
                    pending_uuid
                    for pending_uuid in _desired_gpu_oc_pending_uuids(
                        _optional_row_value(
                            agent,
                            "desired_gpu_oc_pending_json",
                            "[]",
                        )
                    )
                    if pending_uuid != gpu_uuid
                ]
                db.execute(
                    """UPDATE agents
                       SET desired_gpu_oc_json=?, desired_gpu_oc_pending_json=?
                       WHERE id=?""",
                    (
                        json.dumps(
                            remaining, separators=(",", ":"), ensure_ascii=False
                        ),
                        json.dumps(
                            sorted(remaining_pending),
                            separators=(",", ":"),
                        ),
                        agent_id,
                    ),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="desired_gpu_oc_forgotten",
                    target_type="agent",
                    target_id=agent_id,
                    details={
                        "gpu_index": gpu_index,
                        "gpu_uuid": gpu_uuid,
                        "controller_only": True,
                    },
                )
                return {
                    "status": "deleted",
                    "gpu_index": gpu_index,
                    "gpu_uuid": gpu_uuid,
                    "desired_gpu_oc": remaining,
                    "reset_command": None,
                }
            reset_command = queue_agent_command(
                db,
                agent_id=agent_id,
                action="apply_gpu_profile",
                payload={
                    "gpu_profile_id": f"device-oc-reset-{uuid.uuid4().hex}",
                    "name": str(selected["name"])[:80],
                    "vendor": "nvidia",
                    "mode": "reset_one",
                    "expected_gpu_uuid": gpu_uuid,
                    "previous_index": int(selected["setting"]["index"]),
                    "settings": [{**selected["setting"], "index": gpu_index}],
                },
                user=user,
                gpu_oc_intent=_gpu_oc_intent_marker(selected),
            )
            Database.audit(
                db,
                actor=user["username"],
                action="desired_gpu_oc_reset_queued",
                target_type="agent",
                target_id=agent_id,
                details={
                    "gpu_index": gpu_index,
                    "gpu_uuid": gpu_uuid,
                    "reset_command_id": reset_command["id"],
                },
            )
        return {
            "status": "reset_pending",
            "gpu_index": gpu_index,
            "gpu_uuid": gpu_uuid,
            "desired_gpu_oc": desired,
            "reset_command": reset_command,
        }

    @app.post("/api/agents/{agent_id}/linux-ssh", status_code=201)
    def configure_agent_linux_ssh(
        agent_id: str,
        body: LinuxSshConfigureRequest,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        payload = body.model_dump()
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute(
                "SELECT * FROM agents WHERE id=?", (agent_id,)
            ).fetchone()
            if not agent:
                raise HTTPException(404, "Agent not found")
            if agent["disabled_at"]:
                raise HTTPException(409, "Agent is disabled")
            if str(agent["os_family"] or "").strip().casefold() not in {
                "linux",
                "ubuntu",
                "debian",
            }:
                raise HTTPException(409, "Linux SSH configuration is Linux-only")
            if _serialize_agent(agent, settings)["status"] != "online":
                raise HTTPException(409, "Linux SSH configuration requires an online device")
            capabilities = _json(agent["capabilities_json"], [])
            if (
                not isinstance(capabilities, list)
                or LINUX_SSH_MANAGE_CAPABILITY not in capabilities
            ):
                raise HTTPException(
                    409,
                    "This Linux Agent cannot safely manage the fixed SSH listener",
                )
            expire_stale_agent_commands(
                db,
                agent_id=agent_id,
                now=utcnow(),
            )
            unfinished = db.execute(
                """SELECT id FROM commands WHERE agent_id=?
                   AND status IN ('queued','dispatched','running') LIMIT 1""",
                (agent_id,),
            ).fetchone()
            if unfinished:
                raise HTTPException(409, "Device has another unfinished command")
            command = queue_agent_command(
                db,
                agent_id=agent_id,
                action=LINUX_SSH_CONFIGURE_ACTION,
                payload=payload,
                user=user,
                ttl_seconds=300,
            )
            Database.audit(
                db,
                actor=user["username"],
                action=(
                    "linux_ssh_enable_queued"
                    if payload["enabled"]
                    else "linux_ssh_disable_queued"
                ),
                target_type="agent",
                target_id=agent_id,
                details={"command_id": command["id"], "port": 22},
            )
        return command

    @app.post("/api/agents/{agent_id}/commands", status_code=201)
    def create_command(
        agent_id: str,
        body: CommandCreate,
        live: bool = Query(default=False),
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        payload = _validate_command(body)
        if live and body.action != "collect_miner_logs":
            raise HTTPException(422, "live mode is only valid for miner logs")
        command_id = str(uuid.uuid4())
        cache_scan_command_id: str | None = None
        created_moment = datetime.now(timezone.utc)
        created_at = created_moment.isoformat(timespec="microseconds")
        ttl_seconds = (
            90
            if body.action == "collect_miner_logs"
            else 300
            if body.action in {MINING_RESIDUE_CLEANUP_ACTION, MINER_CACHE_CLEANUP_ACTION}
            else 120
            if body.action
            in DESTRUCTIVE_ACTIONS | MINING_CONTROL_ACTIONS | MINING_RESIDUE_ACTIONS | MINER_CACHE_ACTIONS
            else 600
        )
        expires_at = (created_moment + timedelta(seconds=ttl_seconds)).isoformat(
            timespec="seconds"
        )
        hard_expires_at = _command_hard_expiry(
            created_moment,
            action=body.action,
            ttl_seconds=ttl_seconds,
        )
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise HTTPException(404, "Agent not found")
            if agent["disabled_at"]:
                raise HTTPException(409, "Agent is disabled")
            if body.action == "restart_mining":
                require_supported_mining_restart(db, agent)
            if body.action in MINING_CONTROL_ACTIONS:
                payload = {
                    CONTROLLER_MINING_RESULT_BINDING_MARKER:
                        _reported_agent_flight_sheet_id(agent)
                }
            expire_stale_agent_commands(
                db,
                agent_id=agent_id,
                now=created_at,
            )
            if body.action in DEVICE_STATE_TRANSITION_ACTIONS and body.action not in MINER_CACHE_ACTIONS:
                active_cache = db.execute(
                    """SELECT action FROM commands WHERE agent_id=? AND action IN (?, ?)
                       AND status IN ('queued','dispatched','running')
                       AND expires_at >= ? LIMIT 1""",
                    (agent_id, MINER_CACHE_SCAN_ACTION, MINER_CACHE_CLEANUP_ACTION, created_at),
                ).fetchone()
                if active_cache:
                    raise HTTPException(409, "Device command is blocked while miner cache scan or cleanup is in progress")
            if body.action == "collect_miner_logs" and not _agent_version_at_least(
                agent["agent_version"], (0, 5, 1)
            ):
                raise HTTPException(
                    409,
                    "Miner log collection requires RigPilot Agent 0.5.1 or newer; "
                    "upgrade this device first",
                )
            if body.action in (
                MINING_CONTROL_ACTIONS
                | MINING_RESIDUE_ACTIONS
                | MINER_CACHE_ACTIONS
                | DESTRUCTIVE_ACTIONS
                | {"restart_service"}
            ):
                migration = db.execute(
                    """SELECT id FROM commands
                       WHERE agent_id=? AND action=?
                         AND status IN ('queued','dispatched','running')
                       LIMIT 1""",
                    (agent_id, GPU_OC_STATE_MIGRATION_ACTION),
                ).fetchone()
                if migration:
                    raise HTTPException(
                        409,
                        "GPU OC state migration is in progress; retry after it finishes",
                    )
            if body.action in DESTRUCTIVE_ACTIONS | {"restart_service"}:
                if _serialize_agent(agent, settings)["status"] != "online":
                    raise HTTPException(
                        409, "Power and service control requires an online device"
                    )
                transition_actions = sorted(DEVICE_STATE_TRANSITION_ACTIONS)
                placeholders = ",".join("?" for _ in transition_actions)
                conflicting = db.execute(
                    f"""SELECT action FROM commands
                        WHERE agent_id=? AND action IN ({placeholders})
                          AND status IN ('queued','dispatched','running')
                          AND expires_at >= ?
                        ORDER BY created_at DESC LIMIT 1""",
                    (agent_id, *transition_actions, created_at),
                ).fetchone()
                if conflicting:
                    raise HTTPException(
                        409,
                        "Power or service control is blocked while command "
                        f"{conflicting['action']} is in progress",
                    )
            if body.action in MINER_CACHE_ACTIONS:
                if _serialize_agent(agent, settings)["status"] != "online":
                    raise HTTPException(409, "Miner cache cleanup requires an online device")
                capabilities = set(_json(agent["capabilities_json"], []))
                if MINER_CACHE_CAPABILITY not in capabilities:
                    raise HTTPException(
                        409,
                        "Miner cache cleanup requires a newer Agent reporting "
                        f"{MINER_CACHE_CAPABILITY}; update the Agent first",
                    )
                transition_actions = sorted(DEVICE_STATE_TRANSITION_ACTIONS)
                placeholders = ",".join("?" for _ in transition_actions)
                conflict = db.execute(
                    f"""SELECT action FROM commands WHERE agent_id=?
                        AND action IN ({placeholders})
                        AND status IN ('queued','dispatched','running')
                        AND expires_at >= ? LIMIT 1""",
                    (agent_id, *transition_actions, created_at),
                ).fetchone()
                if conflict:
                    raise HTTPException(
                        409,
                        "Miner cache scan or cleanup cannot run while command "
                        f"{conflict['action']} is in progress",
                    )
                if body.action == MINER_CACHE_CLEANUP_ACTION:
                    scan = _cache_scan_for_cleanup(
                        db,
                        agent_id=agent_id,
                        created_by=user["id"],
                        fingerprint=payload["scan_fingerprint"],
                        require_fresh=True,
                    )
                    if scan is None:
                        raise HTTPException(409, "Miner cache scan is stale or invalid; scan this device again")
                    if not _json(scan["result_json"], {}).get("candidates"):
                        raise HTTPException(409, "This scan has no safely deletable old miner packages")
                    cache_scan_command_id = scan["id"]
            if body.action in MINING_RESIDUE_ACTIONS:
                if _serialize_agent(agent, settings)["status"] != "online":
                    raise HTTPException(
                        409, "Mining residue repair requires an online device"
                    )
                capabilities = set(_json(agent["capabilities_json"], []))
                if MINING_RESIDUE_REPAIR_CAPABILITY not in capabilities:
                    raise HTTPException(
                        409,
                        "Mining residue repair is unavailable until the Agent reports "
                        f"{MINING_RESIDUE_REPAIR_CAPABILITY}",
                    )
                if (
                    body.action == MINING_RESIDUE_CLEANUP_ACTION
                    and "targets" in payload
                    and MINING_RESIDUE_SELECTIVE_CAPABILITY not in capabilities
                ):
                    raise HTTPException(
                        409,
                        "Selective mining residue cleanup is unavailable until "
                        "the Agent reports "
                        f"{MINING_RESIDUE_SELECTIVE_CAPABILITY}",
                    )
                if (
                    body.action == MINING_RESIDUE_CLEANUP_ACTION
                    and _mining_residue_cleanup_uses_explicit_candidate(payload)
                    and MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY
                    not in capabilities
                    and not _explicit_residue_cleanup_matches_latest_scan(
                        db,
                        agent_id=agent_id,
                        payload=payload,
                    )
                ):
                    raise HTTPException(
                        409,
                        "Explicit mining residue candidate cleanup is unavailable "
                        "until the Agent reports "
                        f"{MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY}",
                    )
                residue_transition = db.execute(
                    """SELECT action FROM commands
                       WHERE agent_id=? AND action IN (?, ?)
                         AND status IN ('queued','dispatched','running')
                         AND expires_at >= ?
                       LIMIT 1""",
                    (
                        agent_id,
                        MINING_RESIDUE_SCAN_ACTION,
                        MINING_RESIDUE_CLEANUP_ACTION,
                        created_at,
                    ),
                ).fetchone()
                if residue_transition:
                    raise HTTPException(
                        409,
                        "A mining residue scan or cleanup is already in progress",
                    )
                conflicting = db.execute(
                    """SELECT action FROM commands
                       WHERE agent_id=?
                         AND action IN (
                           'apply_flight_sheet','stop_mining','restart_mining',
                           'remove_flight_sheet','apply_gpu_profile',
                           'restart_service','reboot','shutdown','agent_update.v1',
                           'migrate_gpu_oc_state.v1',
                           'configure_linux_ssh.v1'
                         )
                         AND status IN ('queued','dispatched','running')
                         AND expires_at >= ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (agent_id, created_at),
                ).fetchone()
                if conflicting:
                    raise HTTPException(
                        409,
                        "Mining residue repair cannot run while command "
                        f"{conflicting['action']} is in progress",
                    )
            if body.action in MINING_CONTROL_ACTIONS:
                if _serialize_agent(agent, settings)["status"] != "online":
                    raise HTTPException(409, "Mining control requires an online device")
                if not _agent_version_at_least(agent["agent_version"], (0, 5, 2)):
                    raise HTTPException(
                        409,
                        "Mining control requires RigPilot Agent 0.5.2 or newer; "
                        "upgrade this device first",
                    )
                residue_transition = db.execute(
                    """SELECT action FROM commands
                       WHERE agent_id=? AND action IN (?, ?)
                         AND status IN ('queued','dispatched','running')
                         AND expires_at >= ?
                       LIMIT 1""",
                    (
                        agent_id,
                        MINING_RESIDUE_SCAN_ACTION,
                        MINING_RESIDUE_CLEANUP_ACTION,
                        created_at,
                    ),
                ).fetchone()
                if residue_transition:
                    raise HTTPException(
                        409,
                        "Mining control is blocked while mining residue repair is in progress",
                    )
                device_actions = [
                    "restart_service",
                    "reboot",
                    "shutdown",
                    AGENT_UPDATE_CAPABILITY,
                    GPU_OC_STATE_MIGRATION_ACTION,
                    LINUX_SSH_CONFIGURE_ACTION,
                ]
                if body.action != "stop_mining":
                    device_actions.append("apply_gpu_profile")
                device_placeholders = ",".join("?" for _ in device_actions)
                device_transition = db.execute(
                    f"""SELECT action FROM commands
                        WHERE agent_id=? AND action IN ({device_placeholders})
                          AND status IN ('queued','dispatched','running')
                          AND expires_at >= ?
                        ORDER BY created_at DESC LIMIT 1""",
                    (
                        agent_id, *device_actions, created_at,
                    ),
                ).fetchone()
                if device_transition:
                    raise HTTPException(
                        409,
                        "Mining control is blocked while device command "
                        f"{device_transition['action']} is in progress",
                    )
                if body.action == "stop_mining":
                    in_flight_gpu_oc = db.execute(
                        """SELECT id FROM commands
                           WHERE agent_id=? AND action='apply_gpu_profile'
                             AND status IN ('dispatched','running')
                             AND expires_at >= ? LIMIT 1""",
                        (agent_id, created_at),
                    ).fetchone()
                    if in_flight_gpu_oc:
                        raise HTTPException(
                            409,
                            "Mining stop is blocked while a GPU OC command is executing",
                        )
                apply_transition = db.execute(
                    """SELECT id, status FROM commands
                       WHERE agent_id=? AND action='apply_flight_sheet'
                         AND status IN ('queued','dispatched','running')
                         AND expires_at >= ?
                       ORDER BY created_at, rowid""",
                    (agent_id, created_at),
                ).fetchall()
                in_flight_apply = next(
                    (
                        item
                        for item in apply_transition
                        if item["status"] in {"dispatched", "running"}
                    ),
                    None,
                )
                if in_flight_apply is not None:
                    raise HTTPException(
                        409,
                        "A flight-sheet apply is already running; retry after it finishes",
                    )
                if body.action == "restart_mining" and apply_transition:
                    raise HTTPException(
                        409,
                        "Mining restart is blocked by a queued flight-sheet apply",
                    )
                if body.action == "remove_flight_sheet":
                    if not _agent_version_at_least(agent["agent_version"], (0, 6, 1)):
                        raise HTTPException(
                            409,
                            "Removing a flight sheet requires RigPilot Agent 0.6.1 or newer",
                        )
                    conflicting = db.execute(
                        """SELECT action FROM commands
                           WHERE agent_id=?
                             AND action IN (
                               'restart_service','reboot','shutdown',
                               'agent_update.v1','apply_gpu_profile',
                               'migrate_gpu_oc_state.v1',
                               'configure_linux_ssh.v1'
                             )
                             AND status IN ('queued','dispatched','running')
                           LIMIT 1""",
                        (agent_id,),
                    ).fetchone()
                    if conflicting:
                        raise HTTPException(
                            409,
                            "Flight-sheet removal is blocked by conflicting command "
                            f"{conflicting['action']}",
                        )
                    for queued_apply in apply_transition:
                        db.execute(
                            "UPDATE commands SET status='cancelled', finished_at=? WHERE id=?",
                            (created_at, queued_apply["id"]),
                        )
                        Database.audit(
                            db,
                            actor=user["username"],
                            action="command_cancelled",
                            target_type="command",
                            target_id=queued_apply["id"],
                            details={"reason": "flight_sheet_removed_from_device"},
                        )
                    db.execute(
                        "DELETE FROM configuration_refreshes WHERE agent_id=?",
                        (agent_id,),
                    )
                unfinished = db.execute(
                    """SELECT id FROM commands
                       WHERE agent_id=?
                         AND action IN ('stop_mining','restart_mining','remove_flight_sheet')
                         AND status IN ('queued','dispatched','running')
                         AND expires_at >= ?
                       LIMIT 1""",
                    (agent_id, created_at),
                ).fetchone()
                if unfinished:
                    raise HTTPException(
                        409, "A mining control command is already in progress"
                    )
                if body.action == "stop_mining":
                    for queued_apply in apply_transition:
                        changed = db.execute(
                            """UPDATE commands SET status='cancelled', finished_at=?
                               WHERE id=? AND status='queued'""",
                            (created_at, queued_apply["id"]),
                        )
                        if changed.rowcount != 1:
                            continue
                        db.execute(
                            """UPDATE configuration_refreshes
                               SET status='cancelled', command_id=NULL, updated_at=?,
                                   completed_at=?, last_error=
                                     'cancelled because mining stop was requested'
                               WHERE command_id=?""",
                            (created_at, created_at, queued_apply["id"]),
                        )
                        Database.audit(
                            db,
                            actor=user["username"],
                            action="command_cancelled",
                            target_type="command",
                            target_id=queued_apply["id"],
                            details={"reason": "mining_stop_requested"},
                        )
                    db.execute(
                        """UPDATE configuration_refreshes
                           SET status='cancelled', command_id=NULL, updated_at=?,
                               completed_at=?,
                               last_error='cancelled because mining stop was requested'
                           WHERE agent_id=? AND trigger_id LIKE 'desired:%'
                             AND status IN (
                               'pending','preparing','queued','dispatched','running'
                             )""",
                        (created_at, created_at, agent_id),
                    )
                    cancel_queued_gpu_oc_commands(
                        db,
                        agent_id=agent_id,
                        user=user,
                        reason="mining_stop_requested",
                    )
            if live:
                cleanup_before = (
                    datetime.now(timezone.utc)
                    - timedelta(minutes=LIVE_LOG_TRANSIENT_RETENTION_MINUTES)
                ).isoformat(timespec="seconds")
                db.execute(
                    """DELETE FROM commands
                       WHERE is_transient=1
                         AND status IN ('succeeded','failed','cancelled','expired')
                         AND finished_at < ?""",
                    (cleanup_before,),
                )
                db.execute(
                    """UPDATE commands SET status='expired', finished_at=?
                       WHERE is_transient=1
                         AND status IN ('queued','dispatched','running')
                         AND expires_at <= ?""",
                    (created_at, created_at),
                )
                unfinished_live = db.execute(
                    """SELECT id FROM commands
                       WHERE is_transient=1 AND agent_id=? AND created_by=?
                         AND status IN ('queued','dispatched','running')
                       LIMIT 1""",
                    (agent_id, user["id"]),
                ).fetchone()
                if unfinished_live:
                    raise HTTPException(
                        409,
                        "A live miner log snapshot is already in progress for this device",
                    )
                rate_cutoff = (
                    datetime.fromisoformat(created_at)
                    - timedelta(seconds=LIVE_LOG_MIN_CREATE_INTERVAL_SECONDS)
                ).isoformat(timespec="seconds")
                recently_created = db.execute(
                    """SELECT id FROM commands
                       WHERE is_transient=1 AND agent_id=? AND created_by=?
                         AND created_at > ?
                       LIMIT 1""",
                    (agent_id, user["id"], rate_cutoff),
                ).fetchone()
                if recently_created:
                    raise HTTPException(
                        status.HTTP_429_TOO_MANY_REQUESTS,
                        "Live miner log snapshots are limited to one every 2 seconds",
                        headers={
                            "Retry-After": str(LIVE_LOG_MIN_CREATE_INTERVAL_SECONDS)
                        },
                    )
            db.execute(
                """INSERT INTO commands
                   (id, agent_id, action, payload_json, status, created_by, created_at,
                    expires_at, hard_expires_at, is_transient)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    command_id,
                    agent_id,
                    body.action,
                    json.dumps(payload, separators=(",", ":")),
                    "queued",
                    user["id"],
                    created_at,
                    expires_at,
                    hard_expires_at,
                    int(live),
                ),
            )
            if not live:
                Database.audit(
                    db,
                    actor=user["username"],
                    action="command_queued",
                    target_type="agent",
                    target_id=agent_id,
                    details={
                        "command_id": command_id,
                        "action": body.action,
                        "payload": _command_payload_for_view(
                            json.dumps(payload, separators=(",", ":"))
                        ),
                        **({"scan_command_id": cache_scan_command_id} if cache_scan_command_id else {}),
                    },
                )
        return {
            "id": command_id,
            "agent_id": agent_id,
            "action": body.action,
            "payload": _command_payload_for_view(
                json.dumps(payload, separators=(",", ":"))
            ),
            "status": "queued",
            "created_at": created_at,
            "expires_at": expires_at,
        }

    @app.post("/api/agents/commands/batch", status_code=201)
    def batch_mining_control(
        body: MiningControlBatchRequest,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, Any]:
        """Queue one mining-control command for only the explicitly selected devices."""

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for agent_id in body.agent_ids:
            try:
                # This deliberately invokes the authoritative single-device
                # path. Online state, the Agent 0.5.2 minimum, command
                # conflicts and duplicate-queue checks must never diverge
                # here.  Every device commits or rolls
                # back inside the single-device transaction, so rejecting
                # one device can never discard another device's queued
                # command; the per-device outcome equals a SAVEPOINT-style
                # partial-success batch.
                queued = create_command(
                    agent_id,
                    CommandCreate(action=body.action, payload={}),
                    live=False,
                    user=user,
                )
            except HTTPException as exc:
                rejected.append(
                    {
                        "agent_id": agent_id,
                        "accepted": False,
                        "status_code": exc.status_code,
                        "detail": str(exc.detail),
                    }
                )
            else:
                accepted.append({"accepted": True, **queued})

        with database.connection() as db:
            Database.audit(
                db,
                actor=user["username"],
                action="mining_control_batch_requested",
                target_type="agent",
                details={
                    "action": body.action,
                    "selected_agent_ids": body.agent_ids,
                    "accepted_agent_ids": [
                        item["agent_id"] for item in accepted
                    ],
                    "rejected_agent_ids": [
                        item["agent_id"] for item in rejected
                    ],
                },
            )
        return {
            "action": body.action,
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "accepted": accepted,
            "rejected": rejected,
        }

    @app.get("/api/commands")
    def list_commands(
        agent_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        include_all_active: bool = Query(default=False),
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, Any]]:
        query = """SELECT c.*, u.username AS created_by_name, a.name AS agent_name
                   FROM commands c JOIN users u ON c.created_by=u.id
                   JOIN agents a ON c.agent_id=a.id
                   WHERE c.is_transient=0"""
        params: list[Any] = []
        if agent_id:
            query += " AND c.agent_id=?"
            params.append(agent_id)
        with database.connection() as db:
            # A read snapshot does not reserve SQLite's single writer.
            db.execute("BEGIN")
            now = utcnow()
            stale_agents = db.execute(
                """SELECT DISTINCT agent_id FROM commands
                   WHERE status IN ('queued','dispatched','running')
                     AND expires_at < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM agent_update_jobs j
                         WHERE j.command_id=commands.id
                           AND j.status='activating'
                     )""",
                (now,),
            ).fetchall()
            if stale_agents:
                # Drop the read snapshot before taking the writer; upgrading
                # a stale WAL snapshot can fail under a concurrent heartbeat.
                db.rollback()
                db.execute("BEGIN IMMEDIATE")
                expire_stale_agent_commands(db, now=now)
            if include_all_active:
                active = db.execute(
                    query
                    + " AND c.status IN ('queued','dispatched','running')"
                    + " ORDER BY c.created_at DESC, c.id DESC",
                    params,
                ).fetchall()
                recent_terminal = db.execute(
                    query
                    + " AND c.status NOT IN ('queued','dispatched','running')"
                    + " ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
                rows = [*active, *recent_terminal]
                rows.sort(
                    key=lambda row: (row["created_at"], row["id"]), reverse=True
                )
            else:
                rows = db.execute(
                    query + " ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
        return [_serialize_command(row, role=user["role"]) for row in rows]

    @app.get("/api/commands/{command_id}")
    def get_command(
        command_id: str,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        with database.connection() as db:
            row = db.execute(
                """SELECT c.*, u.username AS created_by_name, a.name AS agent_name
                   FROM commands c JOIN users u ON c.created_by=u.id
                   JOIN agents a ON c.agent_id=a.id WHERE c.id=?""",
                (command_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Command not found")
        return _serialize_command(row, role=user["role"], detail=True)

    @app.post("/api/commands/{command_id}/cancel")
    def cancel_command(
        command_id: str,
        user: dict[str, Any] = Depends(require_role("operator")),
    ) -> dict[str, str]:
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Command not found")
            if row["action"] == AGENT_UPDATE_CAPABILITY and user["role"] != "admin":
                raise HTTPException(403, "Only admin can cancel an Agent update")
            cancelled_at = utcnow()
            cancelled = db.execute(
                """UPDATE commands SET status='cancelled', finished_at=?
                   WHERE id=? AND status='queued'""",
                (cancelled_at, command_id),
            )
            if cancelled.rowcount != 1:
                runtime_actions = tuple(sorted(CANCELLABLE_RUNNING_COMMAND_ACTIONS))
                runtime_placeholders = ",".join("?" for _ in runtime_actions)
                runtime_cancelled = db.execute(
                    f"""UPDATE commands SET status='cancelled', finished_at=?
                        WHERE id=? AND status IN ('dispatched','running')
                          AND action IN ({runtime_placeholders})""",
                    (cancelled_at, command_id, *runtime_actions),
                ).rowcount
                if runtime_cancelled != 1:
                    raise HTTPException(
                        409,
                        "Only queued commands, or running cancellable commands "
                        "(GPU OC applications), can be cancelled",
                    )
                # Server-side ledger close for a command whose device-side
                # execution cannot be interrupted (0.8.56 Agents have no
                # in-flight cancel channel).  cancelled is terminal, so the
                # conflict/expiry sweeps stop holding the operation channel
                # and new commands may be created immediately; any late
                # Agent result for this row is safely ignored by the
                # result handler's terminal-status path.  This mirrors the
                # audited manual precedent (command_cancelled_manual).
                if not row["is_transient"]:
                    Database.audit(
                        db,
                        actor=user["username"],
                        action="command_cancelled",
                        target_type="command",
                        target_id=command_id,
                        details={
                            "action": row["action"],
                            "agent_id": row["agent_id"],
                            "prior_status": row["status"],
                            "runtime_cancel": True,
                            "device_execution_interrupted": False,
                        },
                    )
                return {"status": "cancelled"}
            db.execute(
                """UPDATE configuration_refreshes
                   SET status='cancelled', updated_at=?, completed_at=?,
                       last_error='refresh command cancelled by operator'
                   WHERE command_id=?""",
                (cancelled_at, cancelled_at, command_id),
            )
            if row["action"] == AGENT_UPDATE_CAPABILITY:
                cancelled_at = utcnow()
                db.execute(
                    """UPDATE agent_update_jobs
                       SET status='cancelled', updated_at=?, completed_at=?
                       WHERE command_id=?""",
                    (cancelled_at, cancelled_at, command_id),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="agent_update_cancelled",
                    target_type="agent",
                    target_id=row["agent_id"],
                    details={"command_id": command_id},
                )
            post_update_job = db.execute(
                """SELECT command_id, agent_id FROM agent_update_jobs
                   WHERE post_update_command_id=? AND status='activating'""",
                (command_id,),
            ).fetchone()
            if post_update_job:
                reason = "post-update configuration command cancelled by operator"
                db.execute(
                    """UPDATE agent_update_jobs
                       SET status='failed', updated_at=?, completed_at=?, last_error=?
                       WHERE command_id=? AND status='activating'""",
                    (
                        cancelled_at,
                        cancelled_at,
                        reason,
                        post_update_job["command_id"],
                    ),
                )
                db.execute(
                    """UPDATE commands SET status='failed', finished_at=?, error=?
                       WHERE id=? AND status IN ('queued','dispatched','running')""",
                    (
                        cancelled_at,
                        reason,
                        post_update_job["command_id"],
                    ),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="agent_update_activation_failed",
                    target_type="agent",
                    target_id=post_update_job["agent_id"],
                    details={
                        "command_id": post_update_job["command_id"],
                        "post_update_command_id": command_id,
                        "reason": reason,
                    },
                )
            if not row["is_transient"]:
                Database.audit(
                    db,
                    actor=user["username"],
                    action="command_cancelled",
                    target_type="command",
                    target_id=command_id,
                )
        return {"status": "cancelled"}

    @app.post("/api/commands/{command_id}/abandon")
    def abandon_command(
        command_id: str,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        """Close an in-flight command ledger for an offline device.

        This does not claim to interrupt work that the Agent may already have
        started.  Online devices and Agent self-update commands deliberately
        remain protected from this administrative recovery path.
        """

        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM commands WHERE id=?", (command_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Command not found")
            if row["status"] in TERMINAL_COMMAND_STATUSES:
                return {
                    "status": row["status"],
                    "previous_status": row["status"],
                    "effect": "already_terminal",
                }
            if row["status"] == "queued":
                raise HTTPException(
                    409,
                    "等待设备领取的任务请使用普通取消；此入口仅结束已送达或执行中的遗留记录",
                )
            if row["status"] not in {"dispatched", "running"}:
                raise HTTPException(409, "任务状态不支持结束遗留记录")
            if row["action"] == AGENT_UPDATE_CAPABILITY:
                raise HTTPException(
                    409,
                    "被控端更新不能作为普通遗留任务结束；若设备永久弃用，请在删除设备时明确确认",
                )
            agent = db.execute(
                "SELECT * FROM agents WHERE id=?", (row["agent_id"],)
            ).fetchone()
            if not agent:
                raise HTTPException(404, "Agent not found")
            if _serialize_agent(agent, settings)["status"] != "offline":
                raise HTTPException(
                    409,
                    "设备仍在线，无法保证远端动作已停止；仅离线设备可结束遗留记录",
                )
            previous_status = row["status"]
            abandoned = _abandon_agent_commands(
                db,
                agent_id=row["agent_id"],
                command_ids=[command_id],
                actor=user["username"],
                reason=(
                    "offline device command abandoned by administrator; "
                    "remote execution may already have occurred"
                ),
                now=utcnow(),
            )
            if len(abandoned) != 1:
                raise HTTPException(409, "任务状态已变化，请刷新后重试")
        return {
            "status": "cancelled",
            "previous_status": previous_status,
            "effect": "controller_record_abandoned",
        }

    @app.get("/api/audit")
    def audit_logs(
        limit: int = Query(default=100, ge=1, le=500),
        agent_id: str | None = Query(default=None, min_length=1, max_length=128),
        user: dict[str, Any] = Depends(current_user),
    ) -> list[dict[str, Any]]:
        with database.connection() as db:
            if agent_id is None:
                rows = db.execute(
                    "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                # Match exact device identity. A command/session row is
                # authoritative while it exists; its recorded agent_id is a
                # fallback only after the linked row has been pruned.
                rows = db.execute(
                    """SELECT a.* FROM audit_logs a
                       WHERE CASE
                         WHEN a.target_type='agent' THEN a.target_id=?
                         WHEN a.target_type='command' THEN
                           COALESCE(
                             (SELECT c.agent_id FROM commands c WHERE c.id=a.target_id),
                             CASE WHEN json_valid(a.details_json)
                               THEN json_extract(a.details_json,'$.agent_id') END
                           )=?
                         WHEN a.target_type='remote_session' THEN
                           CASE WHEN json_valid(a.details_json)
                             THEN json_extract(a.details_json,'$.agent_id') END=?
                         WHEN a.target_type IN ('flight_sheet','gpu_profile') THEN
                           CASE WHEN json_valid(a.details_json)
                             THEN json_extract(a.details_json,'$.agent_id') END=?
                         ELSE 0
                       END
                       ORDER BY a.created_at DESC, a.id DESC LIMIT ?""",
                    (agent_id, agent_id, agent_id, agent_id, limit),
                ).fetchall()
        return [
            {
                "id": row["id"],
                "actor": row["actor"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                **({"related_agent_id": agent_id} if agent_id is not None else {}),
                "details": {} if user["role"] == "viewer" else _json(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    user_reference_columns = (
        ("enrollment_tokens", "created_by"),
        ("agent_replacement_tokens", "created_by"),
        ("controller_preferences", "updated_by"),
        ("commands", "created_by"),
        ("wallets", "created_by"),
        ("pools", "created_by"),
        ("miner_packages", "created_by"),
        ("agent_releases", "created_by"),
        ("flight_sheets", "created_by"),
        ("configuration_refreshes", "requested_by"),
        ("agent_update_jobs", "created_by"),
    )

    def _delete_user_and_reassign_history(
        db: sqlite3.Connection, *, user_id: int, replacement_user_id: int
    ) -> None:
        for table_name, column_name in user_reference_columns:
            db.execute(
                f"UPDATE {table_name} SET {column_name}=? WHERE {column_name}=?",
                (replacement_user_id, user_id),
            )
        db.execute("DELETE FROM users WHERE id=?", (user_id,))

    @app.get("/api/users")
    def list_users(
        include_disabled: bool = Query(default=False),
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> list[dict[str, Any]]:
        del user
        query = "SELECT id, username, role, is_active, created_at FROM users"
        if not include_disabled:
            query += " WHERE is_active=1"
        query += " ORDER BY username"
        with database.connection() as db:
            rows = db.execute(query).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/users", status_code=201)
    def create_user(
        body: UserCreate,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        deleted_user_id: int | None = None
        try:
            password_hash = hash_password(body.password)
            with database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT id, is_active FROM users WHERE username=?", (body.username,)
                ).fetchone()
                if existing and existing["is_active"]:
                    raise HTTPException(409, "Username already exists")
                if existing:
                    deleted_user_id = int(existing["id"])
                    _delete_user_and_reassign_history(
                        db,
                        user_id=deleted_user_id,
                        replacement_user_id=user["id"],
                    )
                cursor = db.execute(
                    "INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                    (body.username, password_hash, body.role, utcnow()),
                )
                Database.audit(
                    db,
                    actor=user["username"],
                    action="user_created",
                    target_type="user",
                    target_id=str(cursor.lastrowid),
                    details={"username": body.username, "role": body.role},
                )
        except sqlite3.IntegrityError as exc:
            if str(exc) == "UNIQUE constraint failed: users.username":
                raise HTTPException(409, "Username already exists") from None
            raise
        del deleted_user_id
        return {"username": body.username, "role": body.role, "is_active": True}

    @app.delete("/api/users/{user_id}")
    def delete_user(
        user_id: int,
        user: dict[str, Any] = Depends(require_role("admin")),
    ) -> dict[str, str]:
        if user_id == user["id"]:
            raise HTTPException(409, "You cannot delete your own account")
        with database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            target = db.execute(
                "SELECT id, username, role, is_active FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            if not target:
                raise HTTPException(404, "User not found")
            if target["role"] == "admin" and target["is_active"]:
                active_admins = db.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE role='admin' AND is_active=1"
                ).fetchone()["count"]
                if active_admins <= 1:
                    raise HTTPException(409, "The last active admin cannot be deleted")
            _delete_user_and_reassign_history(
                db, user_id=user_id, replacement_user_id=user["id"]
            )
            Database.audit(
                db,
                actor=user["username"],
                action="user_deleted",
                target_type="user",
                target_id=str(user_id),
                details={"username": target["username"], "role": target["role"]},
            )
        return {"status": "deleted"}

    static_dir = Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            static_dir / "index.html",
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


app = create_app()
