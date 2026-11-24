"""Non-blocking host-data snapshots for enrollment and heartbeats."""

from __future__ import annotations

import copy
import json
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import Any


LOGGER = logging.getLogger(__name__)
MAX_HOST_DATA_AGE_SECONDS = 45.0
HOST_DATA_REFRESH_SECONDS = 15.0
# ``--once`` is used by enrollment/install and frozen-binary smoke checks.
# On Windows the optional CPU-temperature path can consume three consecutive
# bounded probes (the bundled helper plus two allowlisted WMI providers), each
# with a 4-second subprocess timeout.  Keep a small scheduler/process-teardown
# margin while retaining a hard upper bound when any unrelated probe wedges.
# This is deliberately independent of the controller HTTP request timeout.
ONCE_HOST_DATA_PREWARM_TIMEOUT_SECONDS = 15.0


def minimum_host_data() -> dict[str, Any]:
    """Return the required controller payload without running telemetry probes.

    These values intentionally come only from process constants, the environment,
    and ``gethostname(2)``.  In particular, this fallback must never call the full
    inventory collector: it is the payload used when that collector is wedged.
    """

    try:
        hostname = socket.gethostname().strip()
    except OSError:
        hostname = ""
    if not hostname:
        hostname = (
            os.environ.get("COMPUTERNAME")
            or os.environ.get("HOSTNAME")
            or "unknown-host"
        ).strip()
    if sys.platform.startswith("linux"):
        os_family = "Linux"
    elif sys.platform.startswith("win"):
        os_family = "Windows"
    elif sys.platform == "darwin":
        os_family = "Darwin"
    else:
        os_family = os.name or "unknown"
    return {
        "hostname": hostname or "unknown-host",
        "os_family": os_family,
        "platform": sys.platform or os_family,
        "ip_addresses": [],
        "local_ip_addresses": [],
        "public_ip_addresses": [],
        "metrics": {},
        "inventory": {},
    }


def _normalized_snapshot(value: Any) -> dict[str, Any]:
    """Detach and validate one collector result entirely on the worker thread."""

    # A JSON round trip prevents a collector-owned mutable object (or a custom
    # mapping with surprising copy behavior) from reaching the heartbeat thread.
    # ``requests`` rejects NaN and infinities while preparing its JSON body.
    # Reject them recursively here so one bad metric cannot replace the last
    # heartbeat-safe snapshot and make every subsequent heartbeat fail.
    normalized = json.loads(
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    )
    if not isinstance(normalized, dict):
        raise ValueError("host data root must be an object")
    for key in ("hostname", "os_family", "platform"):
        if not isinstance(normalized.get(key), str) or not normalized[key]:
            raise ValueError(f"host data {key} must be a non-empty string")
    for key in ("ip_addresses", "local_ip_addresses", "public_ip_addresses"):
        if not isinstance(normalized.get(key), list):
            raise ValueError(f"host data {key} must be an array")
    for key in ("metrics", "inventory"):
        if not isinstance(normalized.get(key), dict):
            raise ValueError(f"host data {key} must be an object")
    return normalized


def _identity_only_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Keep stable addressing while removing every potentially stale metric."""

    result = minimum_host_data()
    for key in ("hostname", "os_family", "platform"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result[key] = item
    for key in ("ip_addresses", "local_ip_addresses", "public_ip_addresses"):
        item = value.get(key)
        if isinstance(item, list) and all(
            isinstance(address, str) for address in item
        ):
            result[key] = copy.deepcopy(item)
    # In particular, do not retain inventory.mining_status after the collector
    # stops making progress.  The client adds its cheap, current readiness data
    # to this empty inventory immediately before sending the heartbeat.
    result["metrics"] = {}
    result["inventory"] = {}
    return result


class HostDataSampler:
    """Maintain one asynchronously refreshed, JSON-safe host-data snapshot.

    A permanently blocked collector consumes at most this one daemon thread.
    Refresh requests are coalesced, so heartbeats never accumulate workers while
    a probe is stuck.  Cooperative/normal collectors are joined during shutdown;
    an uninterruptible third-party probe cannot prevent the Agent process exiting.
    """

    def __init__(
        self,
        collector: Callable[[], dict[str, Any]],
        *,
        fallback_factory: Callable[[], dict[str, Any]] = minimum_host_data,
        thread_name: str = "rigpilot-host-data",
        max_age_seconds: float = MAX_HOST_DATA_AGE_SECONDS,
        refresh_interval_seconds: float = HOST_DATA_REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be positive")
        self._collector = collector
        self._fallback_factory = fallback_factory
        self._thread_name = thread_name
        self._max_age_seconds = max_age_seconds
        self._refresh_interval_seconds = refresh_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._available = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: dict[str, Any] | None = None
        self._latest_at: float | None = None
        self._closed = False

    @property
    def worker_thread(self) -> threading.Thread | None:
        """Expose the single worker for lifecycle diagnostics and tests."""

        with self._lock:
            return self._thread

    def _start_if_needed(self) -> None:
        start_error: Exception | None = None
        with self._lock:
            if self._closed:
                return
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._run,
                name=self._thread_name,
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:
                # Thread creation can fail under process/resource pressure.
                # Keep telemetry best-effort: the heartbeat uses its fallback
                # now and a later refresh can retry with a fresh Thread object.
                if self._thread is thread:
                    self._thread = None
                start_error = exc
        if start_error is not None:
            LOGGER.warning(
                "host telemetry worker could not start; using the previous or minimal snapshot",
                extra={
                    "event": "host_data_worker_start_failed",
                    "error": str(start_error),
                },
            )

    def request_refresh(self) -> None:
        """Coalesce a refresh request without waiting for collection."""

        self._start_if_needed()
        if not self._stopping.is_set():
            self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        """Return the last good snapshot, or the guaranteed-minimal fallback."""

        self.request_refresh()
        with self._lock:
            latest = self._latest
            latest_at = self._latest_at
        if latest is None:
            return self._fallback_factory()
        if latest_at is None or self._clock() - latest_at > self._max_age_seconds:
            return _identity_only_snapshot(latest)
        # ``latest`` contains only JSON primitives, so this copy cannot invoke
        # collector-controlled code and the client may safely modify its payload.
        return copy.deepcopy(latest)

    def wait_for_first(self, timeout: float) -> bool:
        """Boundedly prewarm one snapshot for finite ``--once`` invocations."""

        self.request_refresh()
        if timeout <= 0:
            return self._available.is_set()
        self._available.wait(timeout)
        return self._available.is_set()

    def request_stop(self) -> None:
        """Signal shutdown without joining the worker."""

        # A plain assignment is sufficient under the GIL.  A concurrent start
        # that already passed this flag still observes ``_stopping`` in _run.
        self._closed = True
        self._stopping.set()
        self._wake.set()

    def close(self, timeout: float = 1.0) -> bool:
        """Stop and join a responsive worker; return false if its probe is stuck."""

        self.request_stop()
        with self._lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return thread is None
        if thread.ident is None:
            # Be defensive about a Thread whose start failed before this
            # process version cleared it from ``_thread``.  ``join`` on an
            # unstarted Thread raises RuntimeError and could mask shutdown.
            with self._lock:
                if self._thread is thread:
                    self._thread = None
            return True
        thread.join(max(0.0, timeout))
        if thread.is_alive():
            LOGGER.warning(
                "host telemetry collector did not stop; leaving its single daemon worker bounded",
                extra={"event": "host_data_collector_stop_timeout"},
            )
            return False
        return True

    def _run(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                self._wake.wait(self._refresh_interval_seconds)
                self._wake.clear()
                if self._stopping.is_set():
                    return
                try:
                    snapshot = _normalized_snapshot(self._collector())
                    collected_at = self._clock()
                except Exception as exc:
                    # A failed refresh never evicts the last good snapshot.
                    LOGGER.warning(
                        "host telemetry collection failed; using the previous or minimal snapshot",
                        extra={
                            "event": "host_data_collection_failed",
                            "error": str(exc),
                        },
                    )
                    continue
                with self._lock:
                    if not self._stopping.is_set():
                        self._latest = snapshot
                        self._latest_at = collected_at
                        self._available.set()
        finally:
            with self._lock:
                if self._thread is current:
                    self._thread = None
