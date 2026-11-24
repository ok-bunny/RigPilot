"""Pure CPU/pool argv and read-only telemetry helpers for Seine v0.2.45.

Contract source: zpalmtree/seine commits 7a73ed904cfd6701062f0c8ef3efcb02ce13ad0d
and 752f6679d0656eb45404bf867a72a3c475c59cdf. No process, network, native
library, wallet, daemon or filesystem operation is performed by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
from typing import Any
from urllib.parse import urlsplit


_LIFECYCLES = frozenset({"idle", "starting", "running", "stopping", "error"})
_LOG_CAPACITY = 200  # Upstream control_api::RECENT_LOG_CAPACITY.
_STATS = re.compile(
    r"elapsed=(?P<elapsed>[0-9]+(?:\.[0-9]+)?)s hashes=(?P<hashes>[0-9]+) "
    r"rate=(?P<rate>[0-9]+(?:\.[0-9]+)?) (?P<unit>H|KH|MH|GH)/s "
    r"templates=(?P<templates>[0-9]+) submitted=(?P<submitted>[0-9]+) "
    r"stale=(?P<stale>[0-9]+) accepted=(?P<accepted>[0-9]+) "
    r"deferred=(?P<deferred>[0-9]+) dropped=(?P<dropped>[0-9]+)"
)
_RATE_MULTIPLIERS = {"H": 1, "KH": 1_000, "MH": 1_000_000, "GH": 1_000_000_000}


def _argument(value: Any, name: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str) or not value or value != value.strip()
        or len(value) > maximum or value.startswith("-")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"invalid {name}")
    return value


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _pool(value: Any) -> str:
    pool = _argument(value, "pool URL")
    if any(char.isspace() for char in pool):
        raise ValueError("pool URL cannot contain whitespace")
    parsed = urlsplit(pool if "://" in pool else "stratum+tcp://" + pool)
    try:
        valid_port = parsed.port is not None and 1 <= parsed.port <= 65535
    except ValueError:
        valid_port = False
    if (
        parsed.scheme != "stratum+tcp" or not parsed.hostname or not valid_port
        or parsed.username is not None or parsed.password is not None
        or parsed.path or parsed.query or parsed.fragment
    ):
        raise ValueError("Seine pool must be host:port or stratum+tcp://host:port")
    return pool


def build_seine_cpu_arguments(
    *, address: str, pool_url: str, worker: str, data_dir: str,
    api_port: int = 9977, threads: int | None = None, cpu_profile: str | None = None,
    disable_update_check: bool = False, disable_telemetry: bool = False,
) -> list[str]:
    """Build fixed noninteractive pool/CPU args, without spawning anything.

    The caller owns/creates a private data directory and validates the user's
    actual BNT address. Seine itself checks Base58/checksum/Ristretto keys;
    this helper does not claim a textual address check proves wallet validity.
    Omitted threads retain upstream's CPU/RAM-aware sizing and page fallback.
    Upstream pool mode may discover local daemon cookies; no argv option is
    claimed to disable that discovery. Daemon/wallet setup is never requested.
    """
    address = _argument(address, "BNT address", maximum=256)
    if any(char.isspace() for char in address):
        raise ValueError("BNT address cannot contain whitespace")
    pool_url = _pool(pool_url)
    worker = _argument(worker, "worker", maximum=256)
    data_dir = _argument(data_dir, "private data directory", maximum=4096)
    posix, windows = PurePosixPath(data_dir), PureWindowsPath(data_dir)
    parts = windows.parts if windows.is_absolute() else posix.parts
    if not (posix.is_absolute() or windows.is_absolute()) or len(parts) < 2 or ".." in parts:
        raise ValueError("private data directory must be an absolute non-traversing path")
    api_port = _positive_integer(api_port, "API port")
    if api_port > 65535:
        raise ValueError("API port is out of range")
    if not isinstance(disable_update_check, bool) or not isinstance(disable_telemetry, bool):
        raise ValueError("Seine external-service defaults must be boolean")
    result = [
        "--mode", "pool", "--backend", "cpu", "--address", address,
        "--pool-url", pool_url, "--pool-worker", worker, "--ui", "plain",
        "--api-server", "--api-bind", f"127.0.0.1:{api_port}",
    ]
    if disable_update_check:
        result.append("--disable-update-check")
    if disable_telemetry:
        result.extend(["--telemetry", "off"])
    result.extend(["--data-dir", data_dir])
    if threads is not None:
        result.extend(["--threads", str(_positive_integer(threads, "threads"))])
    if cpu_profile is not None:
        if not isinstance(cpu_profile, str) or cpu_profile not in {"balanced", "throughput", "efficiency"}:
            raise ValueError("unknown Seine CPU profile")
        result.extend(["--cpu-profile", cpu_profile])
    return result


def _uint(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**64 - 1 else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _stats_sample(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict) or row.get("tag") != "STATS":
        return None
    message, log_elapsed = row.get("message"), _number(row.get("elapsed_secs"))
    if not isinstance(message, str) or len(message) > 2048 or log_elapsed is None:
        return None
    matched = _STATS.fullmatch(message)
    if matched is None:
        return None
    try:
        elapsed, rate = float(matched["elapsed"]), float(matched["rate"])
        rate *= _RATE_MULTIPLIERS[matched["unit"]]
        counters = {key: int(matched[key]) for key in ("hashes", "templates", "submitted", "stale", "accepted", "deferred", "dropped")}
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(elapsed) or elapsed <= 0 or not math.isfinite(rate) or any(_uint(value) is None for value in counters.values()):
        return None
    fingerprint = hashlib.sha256(json.dumps(
        [log_elapsed, message], ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "hashrate_hs": rate, "stats_elapsed_secs": elapsed,
        "log_elapsed_secs": log_elapsed, "fingerprint": fingerprint,
        "upstream_stats_counters": counters,
    }


def parse_seine_runtime_state(payload: Any) -> dict[str, Any] | None:
    """Decode CPU/pool state and a bounded latest STATS sample.

    Hashrate is the upstream session average. Share totals remain unknown:
    STATS counters are not relabelled as rejected shares, and block metrics
    are not shares. The parent must bind samples to its owned PID/run and
    track fingerprint observation freshness; API restart retains old logs.
    Fingerprints deliberately exclude run_id so an old retained log does not
    become a new sample merely because the run ID changes.
    """
    if not isinstance(payload, dict):
        return None
    lifecycle, running, run_id = payload.get("lifecycle"), payload.get("running"), _uint(payload.get("run_id"))
    if not isinstance(lifecycle, str) or lifecycle not in _LIFECYCLES or not isinstance(running, bool) or run_id is None:
        return None
    config = payload.get("effective_config")
    if not isinstance(config, dict) or config.get("mode") != "pool":
        return None
    backends = config.get("backend_specs")
    if not isinstance(backends, list) or not backends or len(backends) > 64:
        return None
    if any(not isinstance(row, dict) or row.get("kind") != "cpu" for row in backends):
        return None
    result: dict[str, Any] = {
        "running": running, "lifecycle": lifecycle, "run_id": run_id,
        "started_at_unix_ms": _uint(payload.get("started_at_unix_ms")),
        "hashrate_hs": None, "accepted_shares": None, "rejected_shares": None,
        "invalid_shares": None, "sample": None,
        "source": "seine_runtime_state_recent_logs",
    }
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        result["block_diagnostics"] = {key: _uint(metrics.get(key)) for key in ("accepted_blocks", "submitted_blocks", "stale_submits")}
    logs = payload.get("recent_logs")
    if running and lifecycle == "running" and isinstance(logs, list):
        samples = [sample for row in logs[-_LOG_CAPACITY:] if (sample := _stats_sample(row)) is not None]
        if samples:
            sample = max(samples, key=lambda row: row["log_elapsed_secs"])
            result["sample"] = sample
            result["hashrate_hs"] = sample["hashrate_hs"]
    return result
