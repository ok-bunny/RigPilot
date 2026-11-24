"""Observation windows for decoded Seine CPU STATS, without IO or shares.

An API can retain logs from a prior run. A new identity/run/history therefore
records only an event floor, then waits for two subsequent fresh STATS events
before publishing a counter-difference rate. Session-average rate is not used.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass, field
import math
import re
import threading
from typing import Any

_MAX_ENTRIES = 64
_MAX_FINGERPRINTS = 200  # Same bounded recent-log history as Seine's API.
_FRESH_SECONDS = 150.0  # Existing Agent log-observation freshness concept.
_SHA256 = re.compile(r"[0-9a-f]{64}", re.I)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _uint(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**64 - 1 else None


def _identity(value: Any) -> tuple[int, float, str] | None:
    if not isinstance(value, tuple) or len(value) != 3:
        return None
    pid, created, argv = value
    created = _number(created)
    if (type(pid) is not int or pid <= 0 or created is None or created <= 0
            or not isinstance(argv, str) or _SHA256.fullmatch(argv) is None):
        return None
    return pid, created, argv.lower()


@dataclass
class _Entry:
    run_id: int | None = None
    floor: float | None = None
    baseline: tuple[float, int] | None = None
    fresh_at: float | None = None
    last_observed_at: float | None = None
    window: dict[str, Any] | None = None
    window_at: float | None = None
    fingerprints: deque[str] = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)

    def remember(self, fingerprint: str) -> None:
        if fingerprint not in self.seen:
            if len(self.fingerprints) == _MAX_FINGERPRINTS:
                self.seen.remove(self.fingerprints.popleft())
            self.fingerprints.append(fingerprint)
            self.seen.add(fingerprint)

    def reset(self, run_id: int | None) -> None:
        self.run_id = run_id
        self.floor = self.baseline = self.fresh_at = None
        self.window = self.window_at = None

    def set_floor(self, run_id: int, log_elapsed: float, fingerprint: str, now: float) -> None:
        self.reset(run_id)
        self.floor, self.fresh_at = log_elapsed, now
        self.remember(fingerprint)


class SeineTelemetryObserver:
    """Bounded per-owned-process CPU windows; ``now`` is local observation time.

    ``identity`` must be the verified (PID, create_time, argv_sha256) tuple.
    The same clock must supply every ``now`` value. Neither the API's Unix
    start time nor log elapsed time is converted into a wall-clock timestamp.
    A duplicate may return a recent display window with sample_new=False and
    zero delta; callers must not advance a workload sample timestamp for it.
    Optional wall_time records actual Unix observation time only for a new
    window; duplicate display retains that exact sampled_at_unix value.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[int, float, str], _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, decoded: Any, identity: tuple[int, float, str], now: float,
                *, wall_time: float | None = None) -> dict[str, Any] | None:
        key, observed = _identity(identity), _number(now)
        if key is None:
            return None
        if observed is None:
            with self._lock:
                if key in self._entries:
                    self._entries[key].reset(None)
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._entries[key] = _Entry()
                if len(self._entries) > _MAX_ENTRIES:
                    self._entries.popitem(last=False)
            self._entries.move_to_end(key)
            previous_time, entry.last_observed_at = entry.last_observed_at, observed
            run_id = _uint(decoded.get("run_id")) if isinstance(decoded, dict) else None
            if (not isinstance(decoded, dict) or run_id is None
                    or decoded.get("running") is not True or decoded.get("lifecycle") != "running"):
                entry.reset(run_id)
                return None
            sample = decoded.get("sample")
            counters = sample.get("upstream_stats_counters") if isinstance(sample, dict) else None
            fingerprint = sample.get("fingerprint") if isinstance(sample, dict) else None
            elapsed = _number(sample.get("stats_elapsed_secs")) if isinstance(sample, dict) else None
            log_elapsed = _number(sample.get("log_elapsed_secs")) if isinstance(sample, dict) else None
            hashes = _uint(counters.get("hashes")) if isinstance(counters, dict) else None
            if (not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None
                    or elapsed is None or elapsed <= 0 or log_elapsed is None or hashes is None):
                entry.reset(run_id)
                return None
            fingerprint = fingerprint.lower()
            lost_history = (entry.run_id != run_id or entry.floor is None
                            or (previous_time is not None and observed < previous_time)
                            or (entry.fresh_at is not None and observed-entry.fresh_at > _FRESH_SECONDS))
            if lost_history:
                entry.set_floor(run_id, log_elapsed, fingerprint, observed)
                return None
            if fingerprint in entry.seen:
                if log_elapsed < entry.floor:
                    entry.set_floor(run_id, log_elapsed, fingerprint, observed)
                    return None
                if entry.window is None or entry.window_at is None or observed-entry.window_at > _FRESH_SECONDS:
                    return None
                result = deepcopy(entry.window)
                result.update(sample_new=False, delta_hashes=0, delta_elapsed_secs=0.0,
                              sample_age_secs=observed-entry.window_at)
                return result
            if log_elapsed <= entry.floor:
                entry.set_floor(run_id, log_elapsed, fingerprint, observed)
                return None
            entry.remember(fingerprint)
            entry.floor, entry.fresh_at = log_elapsed, observed
            if entry.baseline is None:
                entry.baseline = elapsed, hashes
                return None
            before_elapsed, before_hashes = entry.baseline
            delta_elapsed, delta_hashes = elapsed-before_elapsed, hashes-before_hashes
            if delta_elapsed <= 0 or delta_hashes < 0:
                entry.set_floor(run_id, log_elapsed, fingerprint, observed)
                return None
            rate = delta_hashes/delta_elapsed
            if not math.isfinite(rate):
                entry.set_floor(run_id, log_elapsed, fingerprint, observed)
                return None
            entry.baseline = elapsed, hashes
            result = {"hashrate_hs": rate, "sample_new": True,
                      "delta_hashes": delta_hashes, "delta_elapsed_secs": delta_elapsed,
                      "sample_age_secs": 0.0, "run_id": run_id, "fingerprint": fingerprint,
                      "accepted_shares": None, "rejected_shares": None, "invalid_shares": None,
                      "source": "seine_runtime_state_stats_window"}
            sampled_at = _number(wall_time)
            if sampled_at is not None:
                result["sampled_at_unix"] = sampled_at
            diagnostics = decoded.get("block_diagnostics")
            if isinstance(diagnostics, dict):
                result["block_diagnostics"] = {key: _uint(diagnostics.get(key)) for key in
                                                ("accepted_blocks", "submitted_blocks", "stale_submits")}
            result["upstream_stats_counters"] = {key: _uint(counters.get(key)) for key in
                                                  ("hashes", "templates", "submitted", "stale", "accepted", "deferred", "dropped")}
            entry.window, entry.window_at = deepcopy(result), observed
            return result
