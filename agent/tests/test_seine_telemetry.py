"""Synthetic decoded STATS only; no Seine process, API, or native execution."""
from copy import deepcopy
import hashlib

import pytest

from agent import seine_telemetry as telemetry


IDENTITY = (1234, 1700000000.5, "a" * 64)


def state(elapsed=10, hashes=100, *, run_id=1, log_elapsed=None, **changes):
    log_elapsed = elapsed if log_elapsed is None else log_elapsed
    fingerprint = hashlib.sha256(repr((elapsed, hashes, log_elapsed)).encode()).hexdigest()
    value = {"running": True, "lifecycle": "running", "run_id": run_id,
             "started_at_unix_ms": 1800000000000, "hashrate_hs": 999999,
             "block_diagnostics": {"accepted_blocks": 5, "submitted_blocks": 7, "stale_submits": 2},
             "sample": {"fingerprint": fingerprint, "stats_elapsed_secs": elapsed,
                        "log_elapsed_secs": log_elapsed, "hashrate_hs": 999999,
                        "upstream_stats_counters": {"hashes": hashes, "templates": 8,
                            "submitted": 7, "stale": 2, "accepted": 5, "deferred": 0, "dropped": 0}}}
    value.update(changes)
    return value


def window(observer, identity=IDENTITY):
    assert observer.observe(state(), identity, 0) is None  # Untrusted event floor.
    assert observer.observe(state(20, 200), identity, 1) is None  # First genuinely new counter baseline.
    return observer.observe(state(30, 250), identity, 2)


def test_first_old_average_is_never_published_then_two_fresh_stats_define_window():
    observer = telemetry.SeineTelemetryObserver()
    result = window(observer)
    assert result["hashrate_hs"] == 5
    assert result["delta_hashes"] == 50 and result["delta_elapsed_secs"] == 10
    assert result["sample_new"] is True
    assert result["accepted_shares"] is result["rejected_shares"] is result["invalid_shares"] is None
    assert result["block_diagnostics"]["accepted_blocks"] == 5
    assert result["upstream_stats_counters"]["stale"] == 2
    assert not any("gpu" in key for key in result)


def test_duplicate_keeps_recent_display_without_delta_or_refreshing_sample_age():
    observer = telemetry.SeineTelemetryObserver()
    first = window(observer)
    first["upstream_stats_counters"]["hashes"] = -1
    duplicate = observer.observe(state(30, 250), IDENTITY, 50)
    assert duplicate["hashrate_hs"] == 5 and duplicate["sample_new"] is False
    assert duplicate["delta_hashes"] == duplicate["delta_elapsed_secs"] == 0
    assert duplicate["sample_age_secs"] == 48
    assert duplicate["upstream_stats_counters"]["hashes"] == 250
    assert observer.observe(state(30, 250), IDENTITY, 153) is None


def test_optional_actual_wall_sample_time_is_fixed_across_duplicate_clock_jitter():
    observer = telemetry.SeineTelemetryObserver()
    assert observer.observe(state(), IDENTITY, 0, wall_time=1800000000) is None
    assert observer.observe(state(20, 200), IDENTITY, 1, wall_time=1800000001) is None
    sample = observer.observe(state(30, 250), IDENTITY, 2, wall_time=1800000002.125)
    assert sample["sampled_at_unix"] == 1800000002.125
    duplicate = observer.observe(state(30, 250), IDENTITY, 3.01, wall_time=1800000003.137)
    assert duplicate["sampled_at_unix"] == sample["sampled_at_unix"]
    assert duplicate["sample_new"] is False and duplicate["delta_hashes"] == 0
    fresh = observer.observe(state(40, 300), IDENTITY, 4, wall_time=float("nan"))
    assert fresh["sample_new"] is True and "sampled_at_unix" not in fresh


def test_known_old_stats_retained_by_new_run_never_reuses_prior_window():
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    assert observer.observe(state(30, 250, run_id=2), IDENTITY, 3) is None
    assert observer.observe(state(30, 250, run_id=2), IDENTITY, 4) is None
    assert observer.observe(state(10, 20, run_id=2, log_elapsed=40), IDENTITY, 5) is None
    result = observer.observe(state(20, 60, run_id=2, log_elapsed=50), IDENTITY, 6)
    assert result["delta_hashes"] == 40 and result["hashrate_hs"] == 4


def test_unseen_old_stats_monotonic_to_new_run_counters_is_still_only_a_floor():
    observer = telemetry.SeineTelemetryObserver()
    assert observer.observe(state(10, 1, run_id=2, log_elapsed=100), IDENTITY, 0) is None
    # Both counters increase, so rollback detection alone cannot protect this.
    assert observer.observe(state(15, 10, run_id=2, log_elapsed=105), IDENTITY, 1) is None
    result = observer.observe(state(20, 30, run_id=2, log_elapsed=110), IDENTITY, 2)
    assert result["delta_hashes"] == 20 and result["delta_elapsed_secs"] == 5
    assert result["hashrate_hs"] == 4  # Never (10-1)/(15-10).


@pytest.mark.parametrize("changed", [(1234, 1700000001.0, "a" * 64),
                                     (1234, 1700000000.5, "b" * 64),
                                     (1235, 1700000000.5, "a" * 64)])
def test_pid_reuse_creation_time_and_argv_changes_start_new_observation_history(changed):
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    assert observer.observe(state(40, 300), changed, 3) is None
    assert observer.observe(state(50, 350), changed, 4) is None
    assert observer.observe(state(60, 450), changed, 5)["delta_hashes"] == 100


def test_counter_rollback_and_elapsed_rollback_require_new_baseline():
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    assert observer.observe(state(5, 1, log_elapsed=40), IDENTITY, 3) is None
    assert observer.observe(state(10, 11, log_elapsed=50), IDENTITY, 4) is None
    assert observer.observe(state(15, 31, log_elapsed=60), IDENTITY, 5)["hashrate_hs"] == 4
    # A decrease in hashes alone also cannot produce a negative window.
    assert observer.observe(state(20, 2, log_elapsed=70), IDENTITY, 6) is None


@pytest.mark.parametrize("bad", [state(sample=None), state(running=False),
                                 state(lifecycle="starting"), state(float("nan")),
                                 state(hashes=-1), state(hashes=True)])
def test_missing_health_sample_or_invalid_counter_clears_display_and_history(bad):
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    assert observer.observe(bad, IDENTITY, 3) is None
    assert observer.observe(state(40, 300), IDENTITY, 4) is None
    assert observer.observe(state(50, 350), IDENTITY, 5) is None
    assert observer.observe(state(60, 450), IDENTITY, 6)["delta_hashes"] == 100


def test_long_gap_lost_history_does_not_fill_unobserved_work():
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    assert observer.observe(state(200, 10000), IDENTITY, 200) is None
    assert observer.observe(state(210, 10100), IDENTITY, 201) is None
    assert observer.observe(state(220, 10250), IDENTITY, 202)["delta_hashes"] == 150


def test_unix_start_time_is_not_added_to_log_elapsed_and_source_is_unmodified():
    observer = telemetry.SeineTelemetryObserver()
    first = state(log_elapsed=5000000)
    before = deepcopy(first)
    assert observer.observe(first, IDENTITY, 100) is None
    assert observer.observe(state(20, 200, log_elapsed=5000001), IDENTITY, 101) is None
    result = observer.observe(state(30, 200, log_elapsed=5000002), IDENTITY, 102)
    assert result["hashrate_hs"] == 0 and result["sample_new"] is True
    assert result["delta_hashes"] == 0 and first == before


def test_process_history_is_bounded_and_evicted_identity_does_not_reuse_counters():
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    for pid in range(2000, 2000 + telemetry._MAX_ENTRIES):
        observer.observe(state(), (pid, 1700000000.5, "a" * 64), 3)
    assert len(observer._entries) == telemetry._MAX_ENTRIES
    assert observer.observe(state(40, 300), IDENTITY, 4) is None


def test_replayed_older_fingerprint_and_backwards_observation_clock_never_repeat_delta():
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    assert observer.observe(state(20, 200), IDENTITY, 3) is None
    assert observer.observe(state(40, 300), IDENTITY, 2) is None


def test_invalid_observation_clock_discards_previous_display_window():
    observer = telemetry.SeineTelemetryObserver()
    window(observer)
    assert observer.observe(state(30, 250), IDENTITY, float("nan")) is None
    assert observer.observe(state(30, 250), IDENTITY, 3) is None


@pytest.mark.parametrize("identity,now", [((1, float("nan"), "a" * 64), 1),
                                        ((1, 1.0, "not-a-sha"), 1),
                                        ((True, 1.0, "a" * 64), 1),
                                        (IDENTITY, float("nan"))])
def test_invalid_identity_or_clock_has_no_observation(identity, now):
    assert telemetry.SeineTelemetryObserver().observe(state(), identity, now) is None
