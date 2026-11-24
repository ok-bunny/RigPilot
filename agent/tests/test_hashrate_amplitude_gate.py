"""Failure-injection tests for the hashrate amplitude gate.

Synthetic telemetry profile:
TH/s level baselines (~105-220 TH/s) with episodic frozen multi-PH/s
plateaus (5-46 PH/s, up to ~3 minutes) reported by the miners themselves.
The gate must reject those aggregates without ever dropping a legitimate
TH/s sample, a measured zero, or a gradual/sustained genuine change.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import agent.inventory as inventory_module
from agent.inventory import _HashrateAmplitudeGate
from agent.pearl_workload import PearlWorkloadCounters
from agent.tests.test_managed_miner_status import (
    FakeResponse,
    collect_mining_status,
    managed_config,
    write_process_record,
    write_status,
)

TH = 1e12
PH = 1e15


def _pearl_flight(tmp_path: Path) -> None:
    (tmp_path / "flight.json").write_text(
        json.dumps(
            {
                "flight_sheet_id": "sheet",
                "name": "PEARL",
                "worker_name": "rig-01",
                "applied_at": "2026-07-14T01:00:00+00:00",
                "primary": {
                    "miner_type": "pearlminer",
                    "coin": "PEARL",
                    "algorithm": "pearlhash",
                    "pool_url": "stratum+tcp://pearl.example.test:4444",
                    "miner_profile": "default",
                    "miner_package": None,
                },
                "secondary": None,
            }
        ),
        encoding="utf-8",
    )


def _write_pearl_log(config: Any, total: str, device: str) -> None:
    assert config.managed_miner_runtime_dir is not None
    (config.managed_miner_runtime_dir / "pearlminer.log").write_text(
        f'event=large.progress height=123 proof_per_sec="{total}" '
        f'devices="[RTX 5080: {device}]"\n',
        encoding="utf-8",
    )


def _srb_payload(now: float, gpu0: float | None, name: str = "pearlhash") -> dict[str, Any]:
    gpu: dict[str, float] = {} if gpu0 is None else {"gpu0": gpu0}
    return {
        "algorithms": [
            {
                "name": name,
                "hashrate": {"now": now, "gpu": gpu},
                "shares": {"accepted": 10, "rejected": 1},
            }
        ]
    }


# ---------------------------------------------------------------------------
# Unit level: the gate itself
# ---------------------------------------------------------------------------


def test_normal_th_level_samples_are_never_rejected() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("managed-session", "PEARL", "pearlhash")
    now = 1_000.0
    for index, rate in enumerate(
        [186.4, 191.2, 195.0, 198.7, 202.3, 205.9, 210.1, 213.8,
         217.5, 221.0, 224.6, 228.9, 204.2, 199.9, 195.5, 190.8]
    ):
        assert gate.observe(
            key, rate * TH, gpu_total=rate * TH, now=now + index * 10,
        ) is None


def test_single_spike_is_dropped_and_recovery_is_immediate() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("s", "PEARL", "pearlhash")
    now = 0.0
    for index in range(4):
        assert gate.observe(key, 210 * TH, gpu_total=None, now=now + index * 10) is None
    assert gate.observe(key, 7.96 * PH, gpu_total=None, now=now + 40) == "above_baseline"
    assert gate.observe(key, 208 * TH, gpu_total=None, now=now + 50) is None


def test_production_frozen_plateau_is_fully_rejected() -> None:
    """Simulate 16 consecutive frozen ~8.1 PH/s rows."""
    gate = _HashrateAmplitudeGate()
    key = ("s", "PEARL", "pearlhash")
    tick = 0

    def step(rate: float) -> Any:
        nonlocal tick
        result = gate.observe(key, rate, gpu_total=None, now=tick * 10)
        tick += 1
        return result

    for _ in range(4):
        assert step(210 * TH) is None
    for _ in range(16):
        assert step(8_124.5 * TH) == "above_baseline"
    assert step(213.6 * TH) is None
    for _ in range(10):
        assert step(8_120 * TH) == "above_baseline"
    assert step(205.1 * TH) is None


def test_interleaved_spikes_never_rebaseline_or_poison() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("s", "PEARL", "pearlhash")
    tick = 0

    def step(rate: float) -> Any:
        nonlocal tick
        result = gate.observe(key, rate, gpu_total=None, now=tick * 10)
        tick += 1
        return result

    for _ in range(4):
        assert step(208 * TH) is None
    for index in range(30):
        if index % 2:
            assert step(10_304.8 * TH) == "above_baseline"
        else:
            assert step(208 * TH) is None


def test_gradual_baseline_ramp_is_accepted() -> None:
    """Legitimate growth: +30% steps, every sample accepted."""
    gate = _HashrateAmplitudeGate()
    key = ("s", "PEARL", "pearlhash")
    tick = 0
    rate = 200.0 * TH
    for _ in range(10):
        for _ in range(4):
            assert gate.observe(key, rate, gpu_total=None, now=tick * 10) is None
            tick += 1
        rate *= 1.3


def test_sustained_large_elevation_rebaselines_after_confirm_window() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("s", "PEARL", "pearlhash")
    for index in range(4):
        assert gate.observe(key, 200 * TH, gpu_total=None, now=index * 10) is None
    rejected = 0
    accepted_at: int | None = None
    for index in range(60):
        if gate.observe(key, 5_000 * TH, gpu_total=None, now=40 + index * 10) is None:
            accepted_at = index
            break
        rejected += 1
    assert rejected == inventory_module._HASHRATE_GATE_RAMP_CONFIRM - 1
    assert accepted_at == inventory_module._HASHRATE_GATE_RAMP_CONFIRM - 1
    # The confirmed elevation is the new baseline: it and its neighbours pass.
    assert gate.observe(key, 5_100 * TH, gpu_total=None, now=700) is None
    assert gate.observe(key, 4_900 * TH, gpu_total=None, now=710) is None


def test_low_rate_burst_within_absolute_slack_is_accepted() -> None:
    """Seine blocknet profile: 5 H/s baseline with a legitimate 75 H/s window."""
    gate = _HashrateAmplitudeGate()
    key = ("s", "BNT", "blocknet-argon2id")
    for index in range(4):
        assert gate.observe(key, 5.0, gpu_total=None, now=index * 10) is None
    assert gate.observe(key, 75.0, gpu_total=None, now=40) is None
    assert gate.observe(key, 5.0, gpu_total=None, now=50) is None
    # A PH-level value is still rejected at this scale.
    assert gate.observe(key, 5.2 * PH, gpu_total=None, now=60) == "above_baseline"


def test_gpu_cross_check_rejects_cold_start_total_mismatch() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("fresh-session", "PEARL", "pearlhash")
    assert gate.observe(
        key, 15.7 * PH, gpu_total=107.8 * TH, now=0.0,
    ) == "gpu_total_mismatch"


def test_gpu_cross_check_tolerates_small_reported_drift() -> None:
    """Synthetic aggregate: total 105.67 TH/s vs gpu0 107.81 TH/s (-2%)."""
    gate = _HashrateAmplitudeGate()
    assert gate.observe(("s", "PEARL", "pearlhash"), 105.67 * TH, gpu_total=107.81 * TH, now=0.0) is None


def test_measured_zero_is_always_accepted_and_never_seeds_baseline() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("s", "XMR", "randomx")
    for index in range(4):
        assert gate.observe(key, 85_000.0, gpu_total=None, now=index * 10) is None
    for index in range(4):
        assert gate.observe(key, 0.0, gpu_total=None, now=40 + index * 10) is None
    # Baseline is still the 85 kH/s history: a spike stays a spike, a normal
    # rate stays normal.
    assert gate.observe(key, 2.0 * PH, gpu_total=None, now=90) == "above_baseline"
    assert gate.observe(key, 84_000.0, gpu_total=None, now=100) is None


def test_low_reading_is_accepted_and_does_not_poison_baseline() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("s", "PEARL", "pearlhash")
    for index in range(4):
        assert gate.observe(key, 200 * TH, gpu_total=None, now=index * 10) is None
    # A real collapse during a driver episode is signal, not noise.
    assert gate.observe(key, 0.4 * TH, gpu_total=None, now=40) is None
    assert gate.observe(key, 0.3 * TH, gpu_total=None, now=50) is None
    assert gate.observe(key, 199 * TH, gpu_total=None, now=60) is None


def test_session_change_resets_baseline() -> None:
    gate = _HashrateAmplitudeGate()
    for index in range(4):
        assert gate.observe(
            ("old", "PEARL", "pearlhash"), 200 * TH, gpu_total=None, now=index * 10,
        ) is None
    # A restarted miner session has no baseline: its first sample is taken
    # as-is (documented cold-start risk, bounded by the gpu cross-check).
    assert gate.observe(
        ("new", "PEARL", "pearlhash"), 8.1 * PH, gpu_total=None, now=40,
    ) is None


def test_stale_series_resets_baseline() -> None:
    gate = _HashrateAmplitudeGate()
    key = ("s", "PEARL", "pearlhash")
    for index in range(4):
        assert gate.observe(key, 200 * TH, gpu_total=None, now=index * 10) is None
    gap = inventory_module._HASHRATE_GATE_STALE_SECONDS + 60
    assert gate.observe(key, 6.1 * PH, gpu_total=None, now=gap) is None


def test_series_capacity_is_bounded() -> None:
    gate = _HashrateAmplitudeGate()
    for index in range(80):
        gate.observe((f"s{index}", "PEARL", "pearlhash"), 200 * TH, gpu_total=None, now=0.0)
    assert len(gate._series) <= inventory_module._HASHRATE_GATE_MAX_SERIES


def test_non_finite_rate_is_rejected() -> None:
    gate = _HashrateAmplitudeGate()
    assert gate.observe(("s", "PEARL", "pearlhash"), math.inf, gpu_total=None, now=0.0) == "invalid_rate"


# ---------------------------------------------------------------------------
# Seeding-window upper bound (0.8.60): samples 2-3 of a fresh series are
# bounded against the already-seeded median, so a stale frozen log read
# during session startup cannot poison a new baseline.
# ---------------------------------------------------------------------------


def test_seeding_second_sample_spike_is_rejected() -> None:
    """A frozen fake rate appearing at sample two never enters the baseline."""
    gate = _HashrateAmplitudeGate()
    key = ("fresh", "PEARL", "pearlhash")
    assert gate.observe(key, 205.0 * TH, gpu_total=None, now=0.0) is None
    assert gate.observe(key, 8.1 * PH, gpu_total=None, now=10.0) == "above_baseline"
    # The rejection did not seed anything: the next real sample is sample two
    # of the baseline again, and normal traffic flows.
    assert gate.observe(key, 208.0 * TH, gpu_total=None, now=20.0) is None
    assert gate.observe(key, 210.0 * TH, gpu_total=None, now=30.0) is None
    assert gate.observe(key, 8.1 * PH, gpu_total=None, now=40.0) == "above_baseline"


def test_seeding_first_fake_sample_is_outvoted_by_median() -> None:
    """One fake first sample cannot move the median of three real seeds."""
    gate = _HashrateAmplitudeGate()
    key = ("fresh2", "PEARL", "pearlhash")
    assert gate.observe(key, 8.1 * PH, gpu_total=None, now=0.0) is None
    assert gate.observe(key, 200.0 * TH, gpu_total=None, now=10.0) is None
    assert gate.observe(key, 202.0 * TH, gpu_total=None, now=20.0) is None
    # Median of [8.1 PH/s, 200 TH/s, 202 TH/s] is ~201 TH/s: a fake aggregate
    # relative to the real baseline is still rejected after seeding.
    assert gate.observe(key, 8.1 * PH, gpu_total=None, now=30.0) == "above_baseline"
    assert gate.observe(key, 205.0 * TH, gpu_total=None, now=40.0) is None


def test_seeding_respects_absolute_slack_at_low_rates() -> None:
    """Low-rate series keep the slack headroom during seeding (Seine shape)."""
    gate = _HashrateAmplitudeGate()
    key = ("fresh3", "BNT", "blocknet-argon2id")
    assert gate.observe(key, 5.0, gpu_total=None, now=0.0) is None
    assert gate.observe(key, 75.0, gpu_total=None, now=10.0) is None
    assert gate.observe(key, 4.8, gpu_total=None, now=20.0) is None


def test_seeding_sustained_elevation_still_rebaselines() -> None:
    """A legitimately higher startup level confirms after the ramp window."""
    gate = _HashrateAmplitudeGate()
    key = ("fresh4", "PEARL", "pearlhash")
    assert gate.observe(key, 200.0 * TH, gpu_total=None, now=0.0) is None
    rejected = 0
    accepted_at: int | None = None
    for index in range(60):
        if gate.observe(key, 5_000.0 * TH, gpu_total=None, now=10.0 + index * 10) is None:
            accepted_at = index
            break
        rejected += 1
    assert accepted_at == inventory_module._HASHRATE_GATE_RAMP_CONFIRM - 1
    assert gate.observe(key, 5_050.0 * TH, gpu_total=None, now=700.0) is None


def test_seeding_gpu_banded_series_unchanged() -> None:
    """Per-GPU-banded series were already bounded in seeding; still are."""
    gate = _HashrateAmplitudeGate()
    key = ("fresh5", "PEARL", "pearlhash")
    assert gate.observe(key, 100.0 * TH, gpu_total=105.0 * TH, now=0.0) is None
    # 4x upper band applies from the first sample on.
    assert gate.observe(key, 500.0 * TH, gpu_total=105.0 * TH, now=10.0) == "gpu_total_mismatch"
    assert gate.observe(key, 104.0 * TH, gpu_total=105.0 * TH, now=20.0) is None


# ---------------------------------------------------------------------------
# Integration: Pearl Miner log path end to end
# ---------------------------------------------------------------------------


def _pearl_collect(tmp_path: Path) -> tuple[Any, Any]:
    config = managed_config(tmp_path)
    _pearl_flight(tmp_path)
    write_status(config, "pearlminer")
    factory = write_process_record(config, "pearlminer")
    return config, factory


def test_pearl_log_spike_yields_null_sample_but_keeps_gpu_rows(tmp_path: Path) -> None:
    config, factory = _pearl_collect(tmp_path)
    for _ in range(4):
        _write_pearl_log(config, "212.1 TH/s", "212.1T/s")
        status = collect_mining_status(config, process_factory=factory)
        assert status["hashrate"] == pytest.approx(212.1 * TH)

    _write_pearl_log(config, "8.12 PH/s", "212.1T/s")
    spiked = collect_mining_status(config, process_factory=factory)
    assert spiked["state"] == "running"
    assert spiked["hashrate"] is None
    assert spiked["hashrate_unit"] is None
    detail = spiked["hashrate_details"][0]
    assert detail["hashrate"] is None
    # The payload's own per-GPU row survives for display.
    assert detail["gpu_hashrates"] == [
        {"index": 0, "hashrate": pytest.approx(212.1 * TH), "hashrate_unit": "H/s"}
    ]
    assert spiked["stats_updated_at"] is not None

    _write_pearl_log(config, "208.4 TH/s", "208.4T/s")
    recovered = collect_mining_status(config, process_factory=factory)
    assert recovered["hashrate"] == pytest.approx(208.4 * TH)


def test_pearl_cold_start_accepts_first_sample(tmp_path: Path) -> None:
    config, factory = _pearl_collect(tmp_path)
    _write_pearl_log(config, "195.0 TH/s", "195.0T/s")
    status = collect_mining_status(config, process_factory=factory)
    assert status["hashrate"] == pytest.approx(195.0 * TH)


# ---------------------------------------------------------------------------
# Integration: SRBMiner HTTP path end to end
# ---------------------------------------------------------------------------


def _srb_collect(tmp_path: Path) -> tuple[Any, Any, list[dict[str, Any]]]:
    config = managed_config(tmp_path)
    (tmp_path / "flight.json").write_text(
        json.dumps(
            {
                "flight_sheet_id": "sheet",
                "name": "PEARL",
                "worker_name": "rig-01",
                "applied_at": "2026-07-14T01:00:00+00:00",
                "primary": {
                    "miner_type": "srbminer",
                    "coin": "PEARL",
                    "algorithm": "pearlhash",
                    "pool_url": "stratum+tcp://pearl.example.test:4444",
                    "miner_profile": "default",
                    "miner_package": None,
                },
                "secondary": None,
            }
        ),
        encoding="utf-8",
    )
    write_status(config, "srbminer")
    factory = write_process_record(config, "srbminer")
    payloads: list[dict[str, Any]] = []
    return config, factory, payloads


def _collect_srb(
    config: Any, factory: Any, payloads: list[dict[str, Any]], payload: dict[str, Any]
) -> Any:
    payloads.append(payload)

    def requester(url: str, **_kwargs: Any) -> FakeResponse:
        return FakeResponse(payloads[-1])

    return collect_mining_status(config, requester=requester, process_factory=factory)


def test_srb_cold_start_spike_rejected_by_gpu_cross_check(tmp_path: Path) -> None:
    config, factory, payloads = _srb_collect(tmp_path)
    status = _collect_srb(
        config, factory, payloads, _srb_payload(now=15.72 * PH, gpu0=107.8 * TH)
    )
    assert status["state"] == "running"
    assert status["hashrate"] is None
    assert status["hashrate_details"][0]["hashrate"] is None
    gpu_row = status["hashrate_details"][0]["gpu_hashrates"][0]
    assert gpu_row["hashrate"] == pytest.approx(107.8 * TH)


def test_srb_partial_rejection_keeps_healthy_algorithm(tmp_path: Path) -> None:
    config, factory, payloads = _srb_collect(tmp_path)

    def quantus_payload(now: float) -> dict[str, Any]:
        return {
            "algorithms": [
                {
                    "name": "pearlhash",
                    "hashrate": {"now": 105.6 * TH, "gpu": {"gpu0": 107.8 * TH}},
                    "shares": {"accepted": 10, "rejected": 1},
                },
                {
                    "name": "quantus",
                    "hashrate": {"now": now, "gpu": {}},
                    "shares": {"accepted": 3, "rejected": 0},
                },
            ]
        }

    # Seed both series' baselines with ordinary values first.
    for _ in range(4):
        healthy = _collect_srb(config, factory, payloads, quantus_payload(1.4 * TH))
        assert {d["algorithm"] for d in healthy["hashrate_details"]} == {
            "pearlhash", "quantus",
        }
    spiked = _collect_srb(config, factory, payloads, quantus_payload(20.3 * PH))
    details = {detail["algorithm"]: detail for detail in spiked["hashrate_details"]}
    assert details["pearlhash"]["hashrate"] == pytest.approx(105.6 * TH)
    assert details["quantus"]["hashrate"] is None
    # The aggregate keeps the healthy leg: a single-series scalar.
    assert spiked["hashrate"] == pytest.approx(105.6 * TH)


# ---------------------------------------------------------------------------
# Integration: the workload counters consume the gated payload
# ---------------------------------------------------------------------------


def test_gated_details_do_not_inflate_workload_counters() -> None:
    counters = PearlWorkloadCounters()
    base = datetime(2026, 11, 3, 5, 40, tzinfo=timezone.utc)
    session = "managed-4242-3600"

    def host(moment: datetime, rate: float | None) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "role": "primary",
            "coin": "PEARL",
            "algorithm": "pearlhash",
            "hashrate_unit": "H/s",
        }
        if rate is not None:
            detail["hashrate"] = rate
        return {
            "inventory": {
                "mining_status": {
                    "miner_type": "pearlminer",
                    "state": "running",
                    "process_verified": True,
                    "session_id": session,
                    "stats_updated_at": moment.isoformat(),
                    "hashrate_details": [detail],
                }
            }
        }

    class _State:
        workload_counter_state = None

    state = _State()
    moments = [
        (timedelta(seconds=0), 200.0 * TH),
        (timedelta(seconds=10), 200.0 * TH),
        (timedelta(seconds=20), 200.0 * TH),
        (timedelta(seconds=30), 200.0 * TH),
        # The gate turned the 8 PH/s aggregate into a missing reading.
        (timedelta(seconds=40), None),
        (timedelta(seconds=50), 200.0 * TH),
    ]
    for delta, rate in moments:
        moment = base + delta
        counters.observe(host(moment, rate), state, now=moment)
    entry = {
        (item["coin"], item["algorithm"]): item
        for item in counters.reports()
    }[("PEARL", "pearlhash")]
    # Three adjacent 10 s trapezoids at 200 TH/s only: 3 * 200e12 * 10 = 6e15.
    # An ungated spike heartbeat would have added ~4.1e16 in a single step.
    assert Decimal(entry["estimated_work_total"]) == Decimal("6E+15")
