"""Monotonic verified coin workload counters from trusted H/s samples."""

from __future__ import annotations

import math
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any

from server.miner_adapters.workload import canonical_workload_coin
from server.miner_adapters.retired import is_retired_miner

from .state import AgentState


MAX_SAMPLE_GAP_SECONDS = Decimal("30")
MAX_SAMPLE_AGE_SECONDS = Decimal("30")
# Wire budget and retained backlog are separate limits. Inactive snapshots
# leave the backlog only after an explicit ACK of their final sequence.
MAX_SERIES = 16
MAX_SAMPLE_DETAILS = 64
MAX_RETAINED_SERIES = 2 * MAX_SAMPLE_DETAILS
_HISTORY_REPORT_SLOTS = 4
_STATE_VERSION = 1
_MAX_WORK_TOTAL = Decimal("1e100")
_MAX_WORK_TEXT_LENGTH = 128
LOGGER = logging.getLogger(__name__)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _safe_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        return None
    return normalized


def _safe_algorithm(value: Any) -> str | None:
    if not isinstance(value, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    normalized = _safe_text(value, 64)
    if normalized is None:
        return None
    normalized = normalized.casefold()
    return normalized if len(normalized) <= 64 else None


def _decimal_rate(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(float(value)) or value < 0:
            return None
        return Decimal(str(value))
    except (InvalidOperation, OverflowError):
        return None


def _decimal_seconds(value: float) -> Decimal:
    return Decimal(str(value))


def _work_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if len(rendered) > _MAX_WORK_TEXT_LENGTH:
        return format(value, "E")
    return rendered or "0"


@dataclass
class _Sample:
    observed_at: datetime
    rate: Decimal
    session_id: str
    sample_id: str | None = None


@dataclass
class _Counter:
    counter_epoch: str
    snapshot_seq: int
    total_work: Decimal
    coin: str
    algorithm: str
    observed_at: datetime

    def report(self) -> dict[str, Any]:
        return {
            "coin": self.coin,
            "algorithm": self.algorithm,
            "counter_epoch": self.counter_epoch,
            "snapshot_seq": self.snapshot_seq,
            "estimated_work_total": _work_text(self.total_work),
            "observed_at": self.observed_at.isoformat(),
        }


class PearlWorkloadCounters:
    """Maintain one process-local counter per verified coin and algorithm.

    Existing AgentState writes persist snapshots without adding a
    heartbeat-cadence disk write, but a new Agent process deliberately does not
    resume their epoch or total. Its first fresh sample establishes a new epoch
    and controller baseline; integration begins with the adjacent sample.
    Active samples get most wire slots, with a reserved rotating historical
    share. ACKed inactive series retire; an exhausted unconfirmed backlog is
    reported in state/inventory and logs rather than evicting pending work.
    """

    def __init__(self) -> None:
        self._bound_state: AgentState | None = None
        self._counters: dict[tuple[str, str], _Counter] = {}
        self._previous: dict[tuple[str, str], _Sample] = {}
        self._native_previous: dict[tuple[str, str], dict[tuple[str, str], _Sample]] = {}
        self._active: set[tuple[str, str]] = set()
        self._baselines: dict[tuple[str, str], dict[str, Any]] = {}
        self._acknowledged: dict[tuple[str, str], int] = {}
        self._inflight: dict[tuple[str, str], dict[str, Any]] = {}
        self._retired_samples: dict[tuple[str, str], datetime] = {}
        self._retired_sample_floor: datetime | None = None
        self._ack_supported: bool | None = None
        self._active_cursor = 0
        self._history_cursor = 0
        self._blocked_series_count = 0

    def bind_state(self, state: AgentState) -> None:
        if self._bound_state is state:
            return
        self._bound_state = state
        # A persisted snapshot can lag a heartbeat the controller already
        # accepted. Reusing it would make the new process spend time catching
        # up while valid work is ignored, so every process starts fresh epochs.
        self._counters = {}
        self._previous = {}
        self._native_previous = {}
        self._active.clear()
        self._baselines.clear()
        self._acknowledged.clear()
        self._inflight.clear()
        self._retired_samples.clear()
        self._retired_sample_floor = None
        self._active_cursor = self._history_cursor = 0
        self._blocked_series_count = 0
        self._publish_state()

    def _clear_samples(self) -> None:
        self._previous.clear()
        self._native_previous.clear()
        self._set_blocked_series_count(0)
        self._set_active(set())

    def _retire_confirmed(self) -> None:
        for series, counter in list(self._counters.items()):
            if (series not in self._active
                    and self._acknowledged.get(series) == counter.snapshot_seq):
                self._remember_retired_sample(series, counter.observed_at)
                self._counters.pop(series)
                self._baselines.pop(series, None)
                self._acknowledged.pop(series, None)
                self._inflight.pop(series, None)
                self._previous.pop(series, None)
                self._native_previous.pop(series, None)

    def _remember_retired_sample(self, series: tuple[str, str], observed_at: datetime) -> None:
        """Reject cached anchors after retirement without retaining all history."""
        if series not in self._retired_samples and len(self._retired_samples) >= MAX_RETAINED_SERIES:
            oldest = min(self._retired_samples, key=self._retired_samples.get)
            removed = self._retired_samples.pop(oldest)
            # An evicted anchor can still be fresh for at most 30 seconds. A
            # single conservative floor protects it until that TTL expires.
            if self._retired_sample_floor is None or removed > self._retired_sample_floor:
                self._retired_sample_floor = removed
        self._retired_samples[series] = observed_at

    def _expire_retired_samples(self, now: datetime) -> None:
        for series, observed_at in list(self._retired_samples.items()):
            if (now - observed_at).total_seconds() > MAX_SAMPLE_AGE_SECONDS:
                self._retired_samples.pop(series)
        if (self._retired_sample_floor is not None
                and (now - self._retired_sample_floor).total_seconds() > MAX_SAMPLE_AGE_SECONDS):
            self._retired_sample_floor = None

    def _set_active(self, series: set[tuple[str, str]]) -> None:
        self._active = series
        self._retire_confirmed()
        self._publish_state()

    def _set_blocked_series_count(self, count: int) -> None:
        if count and count != self._blocked_series_count:
            LOGGER.warning(
                "workload counter capacity exhausted; retaining unconfirmed work",
                extra={"event": "workload_counter_capacity_exhausted",
                       "blocked_series_count": count,
                       "retained_series": len(self._counters),
                       "max_retained_series": MAX_RETAINED_SERIES},
            )
        self._blocked_series_count = count

    def note_ack_support(self, supported: bool) -> None:
        """Keep legacy cumulative reporting without treating HTTP success as ACK.

        Once an epoch has been sent in legacy mode, never rewind it to its
        initial baseline when the controller gains ACK support. New epochs on
        an ACK-capable controller retain their zero baseline until confirmed.
        """
        self._ack_supported = supported
        if not supported:
            self._baselines.clear()

    def acknowledge(self, snapshots: Any) -> None:
        """Retire only exact snapshots explicitly confirmed by the controller."""
        if not isinstance(snapshots, list):
            return
        for snapshot in snapshots[:MAX_SERIES]:
            if (not isinstance(snapshot, dict)
                    or type(snapshot.get("snapshot_seq")) is not int):
                continue
            series = (snapshot.get("coin"), snapshot.get("algorithm"))
            if not all(isinstance(value, str) for value in series):
                continue
            sent = self._inflight.get(series)
            counter = self._counters.get(series)
            if sent is None or snapshot != sent or counter is None:
                continue
            if sent["counter_epoch"] != counter.counter_epoch:
                continue
            sequence = sent["snapshot_seq"]
            self._acknowledged[series] = max(
                sequence, self._acknowledged.get(series, -1),
            )
            self._baselines.pop(series, None)
            self._inflight.pop(series, None)
        self._retire_confirmed()
        self._publish_state()

    def _observe_native_samples(
        self,
        series: tuple[str, str],
        samples: list[tuple[tuple[str, str], _Sample, Decimal]],
        counter: _Counter,
        *,
        new_counter: bool,
    ) -> bool:
        """Credit each owned role/session's source window once, never its display rate."""
        before = self._native_previous.get(series, {})
        current = {}
        conflicting = set()
        for source, sample, delta in samples:
            existing = current.get(source)
            if existing is not None and existing != (sample, delta):
                conflicting.add(source)
            current[source] = sample, delta
        after = {source: previous for source, previous in before.items()
                 if source in current and source not in conflicting}
        increment, advanced = Decimal(0), False
        observed_at = counter.observed_at
        for source, (sample, delta) in current.items():
            if source in conflicting:
                continue
            previous = before.get(source)
            if previous is not None:
                if (sample.sample_id == previous.sample_id
                        or sample.observed_at <= previous.observed_at):
                    continue
                gap = _decimal_seconds((sample.observed_at-previous.observed_at).total_seconds())
                if gap <= MAX_SAMPLE_GAP_SECONDS:
                    increment += delta
            elif not new_counter and sample.observed_at <= counter.observed_at:
                # A lost source anchor cannot be restored by a cached row,
                # even if another role has kept the aggregate heartbeat fresh.
                continue
            after[source] = sample
            observed_at = max(observed_at, sample.observed_at)
            advanced = True
        try:
            candidate_total = counter.total_work + increment
            valid_total = candidate_total.is_finite() and 0 <= candidate_total <= _MAX_WORK_TOTAL
        except (DecimalException, OverflowError):
            valid_total = False
        if not valid_total:
            self._native_previous.pop(series, None)
            return False
        self._native_previous[series] = after
        if not advanced:
            return False
        counter.total_work = candidate_total
        counter.observed_at = observed_at
        if not new_counter:
            counter.snapshot_seq += 1
        return True
    def _publish_state(self) -> None:
        if self._bound_state is None:
            return
        self._bound_state.workload_counter_state = {
            "version": _STATE_VERSION,
            "counters": [self._counters[series].report() for series in sorted(self._counters)],
            "status": {
                "capacity_exhausted": bool(self._blocked_series_count),
                "blocked_series_count": self._blocked_series_count,
                "retained_series": len(self._counters),
                "max_retained_series": MAX_RETAINED_SERIES,
            },
        }

    def observe(
        self,
        host: dict[str, Any],
        state: AgentState,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        self.bind_state(state)
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._expire_retired_samples(current_time)
        inventory = host.get("inventory")
        mining = inventory.get("mining_status") if isinstance(inventory, dict) else None
        if (
            not isinstance(mining, dict)
            or mining.get("retired") is True
            or is_retired_miner(mining.get("miner_type"))
            or mining.get("process_verified") is not True
            or mining.get("state") not in {"running", "active"}
        ):
            self._clear_samples()
            return self.reports()

        session_id = _safe_text(mining.get("session_id"), 128)
        observed_at = _parse_timestamp(mining.get("stats_updated_at"))
        if session_id is None or observed_at is None:
            self._clear_samples()
            return self.reports()
        age = _decimal_seconds((current_time - observed_at).total_seconds())
        if age < 0 or age > MAX_SAMPLE_AGE_SECONDS:
            self._clear_samples()
            return self.reports()

        details = mining.get("hashrate_details")
        if not isinstance(details, list):
            self._clear_samples()
            return self.reports()

        readings: dict[tuple[str, str], Decimal] = {}
        observations: dict[tuple[str, str], dict[str, Any]] = {}
        for detail in details[:MAX_SAMPLE_DETAILS]:
            if not isinstance(detail, dict) or detail.get("hashrate_unit") != "H/s":
                continue
            coin = canonical_workload_coin(
                detail.get("coin"), allow_pearl_aliases=True,
            )
            algorithm = _safe_algorithm(detail.get("algorithm"))
            rate = _decimal_rate(detail.get("hashrate"))
            if coin is None or algorithm is None or rate is None:
                continue
            sample_time = (_parse_timestamp(detail.get("stats_updated_at"))
                           if "stats_updated_at" in detail else observed_at)
            sample_session = (_safe_text(detail.get("session_id"), 128)
                              if "session_id" in detail else session_id)
            if sample_time is None or sample_session is None:
                continue
            sample_age = _decimal_seconds((current_time - sample_time).total_seconds())
            if sample_age < 0 or sample_age > MAX_SAMPLE_AGE_SECONDS:
                continue
            series = (coin, algorithm)
            readings[series] = readings.get(series, Decimal(0)) + rate
            context = observations.setdefault(series, {
                "observed_at": sample_time, "sessions": set(), "native_samples": [],
                "native_only": True, "native_declared": False,
            })
            context["observed_at"] = min(context["observed_at"], sample_time)
            context["sessions"].add(sample_session)
            native_sample = None
            if detail.get("workload_source") == "seine_stats_window":
                context["native_declared"] = True
                sample_id = detail.get("workload_sample_id")
                delta = _decimal_rate(detail.get("workload_delta_work"))
                if (isinstance(sample_id, str) and re.fullmatch(r"[0-9a-f]{64}", sample_id)
                        and delta is not None and delta <= _MAX_WORK_TOTAL):
                    source = (_safe_text(detail.get("role"), 32) or "", sample_session)
                    native_sample = source, _Sample(sample_time, rate, sample_session, sample_id), delta
            else:
                context["native_only"] = False
            context["native_samples"].append(native_sample)

        for series in set(self._previous) - set(readings):
            self._previous.pop(series, None)
        for series in set(self._native_previous) - set(readings):
            self._native_previous.pop(series, None)

        self._set_active(set(readings))
        blocked = 0
        for series, rate in readings.items():
            coin, algorithm = series
            context = observations[series]
            observed_at = context["observed_at"]
            sessions = sorted(context["sessions"])
            session_id = (sessions[0] if len(sessions) == 1 else
                          hashlib.sha256("\n".join(sessions).encode()).hexdigest())
            samples = context["native_samples"]
            counter = self._counters.get(series)
            new_counter = counter is None
            if counter is None:
                retired_at = self._retired_samples.get(series)
                if ((retired_at is not None and observed_at <= retired_at)
                        or (self._retired_sample_floor is not None
                            and observed_at <= self._retired_sample_floor)):
                    continue
                if len(self._counters) >= MAX_RETAINED_SERIES:
                    blocked += 1
                    continue
                counter = _Counter(
                    counter_epoch=str(uuid.uuid4()),
                    snapshot_seq=0,
                    total_work=Decimal(0),
                    coin=coin,
                    algorithm=algorithm,
                    observed_at=observed_at,
                )
                self._counters[series] = counter
                if self._ack_supported is not False:
                    self._baselines[series] = counter.report()
            if context["native_declared"]:
                self._previous.pop(series, None)
                if context["native_only"]:
                    self._observe_native_samples(
                        series, [sample for sample in samples if sample is not None],
                        counter, new_counter=new_counter,
                    )
                else:
                    self._native_previous.pop(series, None)
                continue
            self._native_previous.pop(series, None)
            if not new_counter and observed_at <= counter.observed_at:
                # A stopped or invalid series has no adjacent sample. Cached
                # timestamps must not re-establish its integration anchor.
                continue

            previous = self._previous.get(series)
            if previous is not None:
                gap = _decimal_seconds(
                    (observed_at - previous.observed_at).total_seconds()
                )
                if gap <= 0:
                    continue
                if (
                    previous.session_id == session_id
                    and gap <= MAX_SAMPLE_GAP_SECONDS
                ):
                    try:
                        increment = (previous.rate + rate) * gap / Decimal(2)
                        candidate_total = counter.total_work + increment
                        valid_total = (
                            candidate_total.is_finite()
                            and 0 <= candidate_total <= _MAX_WORK_TOTAL
                        )
                    except (DecimalException, OverflowError):
                        valid_total = False
                    if not valid_total:
                        # Keep the last wire-safe snapshot and require a new
                        # adjacent pair instead of bridging this bad interval.
                        self._previous.pop(series, None)
                        continue
                    counter.total_work = candidate_total
            if observed_at > counter.observed_at:
                # Recovery advances the controller baseline even when no work
                # was integrated, so its next delta excludes the interruption.
                counter.snapshot_seq += 1
                counter.observed_at = observed_at
            self._previous[series] = _Sample(
                observed_at=observed_at,
                rate=rate,
                session_id=session_id,
            )

        self._set_blocked_series_count(blocked)
        self._publish_state()
        return self.reports()

    def _report_series(self, *, advance: bool) -> list[tuple[str, str]]:
        active = sorted(self._active.intersection(self._counters))
        history = sorted(set(self._counters) - self._active)
        history_slots = min(len(history), _HISTORY_REPORT_SLOTS if active else MAX_SERIES)
        active_slots = min(len(active), MAX_SERIES - history_slots)
        history_slots = min(len(history), MAX_SERIES - active_slots)

        def select(values, cursor, count):
            return [values[(cursor + index) % len(values)] for index in range(count)]

        selected = select(active, self._active_cursor, active_slots)
        selected += select(history, self._history_cursor, history_slots)
        if advance:
            self._active_cursor = (self._active_cursor + active_slots) % max(1, len(active))
            self._history_cursor = (self._history_cursor + history_slots) % max(1, len(history))
        return selected

    def reports(self) -> list[dict[str, Any]]:
        """Return a bounded view of live totals, preferring current mining series."""
        return [self._counters[series].report() for series in self._report_series(advance=False)]

    def heartbeat_reports(self) -> list[dict[str, Any]]:
        """Rotate active/history snapshots, fencing unconfirmed initial baselines."""
        reports = [dict(self._baselines.get(series) or self._counters[series].report())
                   for series in self._report_series(advance=True)]
        self._inflight = {(report["coin"], report["algorithm"]): report for report in reports}
        return reports
