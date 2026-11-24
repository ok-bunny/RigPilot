from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
import requests

from agent.client import RigPilotClient
from agent.config import AgentConfig
from agent.pearl_workload import MAX_SERIES, PearlWorkloadCounters
from agent.runtime import AgentRuntime
from agent.state import AgentState, load_state, save_state
from server.miner_adapters.workload import (
    WORKLOAD_COIN_MAX_INPUT_LENGTH,
    WORKLOAD_COIN_MAX_LENGTH,
)
from server.schemas import WorkloadCounter


BASE = datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc)
WIRE_FIELDS = {
    "coin",
    "algorithm",
    "counter_epoch",
    "snapshot_seq",
    "estimated_work_total",
    "observed_at",
}


def _host(
    seconds: int,
    rate: float = 100.0,
    *,
    session_id: str = "managed-session-a",
    state: str = "running",
    process_verified: bool = True,
    coin: str = "PEARL",
    algorithm: str = "pearlhash",
) -> dict[str, object]:
    observed = BASE + timedelta(seconds=seconds)
    return {
        "hostname": "pearl-rig",
        "os_family": "Linux",
        "platform": "Linux-test",
        "ip_addresses": [],
        "local_ip_addresses": [],
        "public_ip_addresses": [],
        "metrics": {},
        "inventory": {
            "mining_status": {
                "state": state,
                "process_verified": process_verified,
                "session_id": session_id,
                "stats_updated_at": observed.isoformat(),
                "hashrate_details": [
                    {
                        "role": "primary",
                        "coin": coin,
                        "algorithm": algorithm,
                        "hashrate": rate,
                        "hashrate_unit": "H/s",
                    }
                ],
            }
        },
    }


def _observe(
    tracker: PearlWorkloadCounters,
    agent_state: AgentState,
    seconds: int,
    rate: float = 100.0,
    **overrides,
) -> list[dict[str, object]]:
    return tracker.observe(
        _host(seconds, rate, **overrides),
        agent_state,
        now=BASE + timedelta(seconds=seconds),
    )


def test_trapezoid_is_monotonic_idempotent_and_breaks_large_gaps() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    first = _observe(tracker, state, 0, 100)
    assert first[0]["snapshot_seq"] == 0
    assert first[0]["estimated_work_total"] == "0"

    second = _observe(tracker, state, 10, 300)
    assert second[0]["snapshot_seq"] == 1
    assert second[0]["estimated_work_total"] == "2000"

    duplicate = _observe(tracker, state, 10, 300)
    assert duplicate == second

    large_gap = _observe(tracker, state, 41, 100)
    assert large_gap[0]["snapshot_seq"] == 2
    assert large_gap[0]["estimated_work_total"] == "2000"

    resumed = _observe(tracker, state, 51, 100)
    assert resumed[0]["snapshot_seq"] == 3
    assert resumed[0]["estimated_work_total"] == "3000"
    assert set(resumed[0]) == WIRE_FIELDS
    assert {"role", "session", "basis", "estimated"}.isdisjoint(resumed[0])


def test_invalid_stopped_stale_and_missing_samples_never_bridge() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    _observe(tracker, state, 0)
    _observe(tracker, state, 5, process_verified=False)
    assert _observe(tracker, state, 10)[0]["estimated_work_total"] == "0"
    assert _observe(tracker, state, 20)[0]["estimated_work_total"] == "1000"

    _observe(tracker, state, 25, state="stopped")
    assert _observe(tracker, state, 30)[0]["estimated_work_total"] == "1000"
    assert _observe(tracker, state, 40)[0]["estimated_work_total"] == "2000"

    stale = _host(50)
    reports = tracker.observe(stale, state, now=BASE + timedelta(seconds=81))
    assert reports[0]["estimated_work_total"] == "2000"
    assert _observe(tracker, state, 60)[0]["estimated_work_total"] == "2000"
    assert _observe(tracker, state, 70)[0]["estimated_work_total"] == "3000"

    missing = _host(75)
    missing["inventory"]["mining_status"]["hashrate_details"] = []
    tracker.observe(missing, state, now=BASE + timedelta(seconds=75))
    assert _observe(tracker, state, 80)[0]["estimated_work_total"] == "3000"


def test_session_change_keeps_one_epoch_but_does_not_bridge() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0)
    before = _observe(tracker, state, 10)

    changed = _observe(tracker, state, 20, session_id="managed-session-b")
    assert len(changed) == 1
    assert changed[0]["counter_epoch"] == before[0]["counter_epoch"]
    assert changed[0]["snapshot_seq"] == 2
    assert changed[0]["estimated_work_total"] == "1000"

    next_sample = _observe(tracker, state, 30, session_id="managed-session-b")
    assert len(next_sample) == 1
    assert next_sample[0]["counter_epoch"] == before[0]["counter_epoch"]
    assert next_sample[0]["snapshot_seq"] == 3
    assert next_sample[0]["estimated_work_total"] == "2000"


@pytest.mark.parametrize("interruption", [
    {"state": "stopped"},
    {"process_verified": False},
    {"session_id": ""},
    {"stats_updated_at": "invalid"},
    {"stats_updated_at": (BASE - timedelta(seconds=31)).isoformat()},
    {"hashrate_details": []},
], ids=["stopped", "unverified", "missing-session", "bad-time", "stale", "missing-series"])
def test_recovered_sample_advances_the_anchor_without_adding_work(interruption) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0)
    baseline = _observe(tracker, state, 10)
    interrupted = _host(20)
    interrupted["inventory"]["mining_status"].update(interruption)
    assert tracker.observe(interrupted, state, now=BASE + timedelta(seconds=20)) == baseline

    recovered = _observe(tracker, state, 3600)
    assert recovered[0]["counter_epoch"] == baseline[0]["counter_epoch"]
    assert recovered[0]["estimated_work_total"] == "1000"
    assert recovered[0]["snapshot_seq"] == 2
    assert recovered[0]["observed_at"] == (BASE + timedelta(seconds=3600)).isoformat()
    assert state.workload_counter_state["counters"] == recovered
    WorkloadCounter.model_validate(recovered[0])

    adjacent = _observe(tracker, state, 3610)
    assert adjacent[0]["estimated_work_total"] == "2000"
    assert adjacent[0]["snapshot_seq"] == 3


@pytest.mark.parametrize("seconds", [9, 10], ids=["backward", "duplicate"])
def test_recovery_rejects_nonadvancing_samples_without_setting_an_anchor(seconds) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0)
    baseline = _observe(tracker, state, 10)
    _observe(tracker, state, 20, state="stopped")

    assert tracker.observe(
        _host(seconds, 3000), state, now=BASE + timedelta(seconds=20),
    ) == baseline
    assert state.workload_counter_state["counters"] == baseline
    recovered = _observe(tracker, state, 20)
    assert recovered[0]["estimated_work_total"] == "1000"
    assert recovered[0]["snapshot_seq"] == 2
    assert _observe(tracker, state, 30)[0]["estimated_work_total"] == "2000"


def test_dual_roles_with_same_algorithm_are_summed_into_one_series() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    first = _host(0, 100, algorithm="PearlHash")
    first["inventory"]["mining_status"]["hashrate_details"].append(
        {
            "role": "secondary",
            "coin": "PRL",
            "algorithm": "pearlhash",
            "hashrate": 50.0,
            "hashrate_unit": "H/s",
        }
    )
    second = deepcopy(first)
    second["inventory"]["mining_status"]["stats_updated_at"] = (
        BASE + timedelta(seconds=10)
    ).isoformat()

    tracker.observe(first, state, now=BASE)
    reports = tracker.observe(
        second,
        state,
        now=BASE + timedelta(seconds=10),
    )

    assert len(reports) == 1
    assert reports[0]["coin"] == "PEARL"
    assert reports[0]["algorithm"] == "pearlhash"
    assert reports[0]["estimated_work_total"] == "1500"


@pytest.mark.parametrize(
    "coin", ["PRL", "prl", "P.R.L", "P R L", "Pearl", "P-E_A.R L"],
)
def test_existing_pearl_aliases_share_the_canonical_series(coin: str) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    first = _observe(tracker, state, 0, coin=coin, algorithm="PearlHash")
    second = _observe(tracker, state, 10, coin="PEARL", algorithm="pearlhash")

    assert len(second) == 1
    assert second[0]["coin"] == "PEARL"
    assert second[0]["counter_epoch"] == first[0]["counter_epoch"]
    assert second[0]["estimated_work_total"] == "1000"


def test_quantus_integrates_fresh_hashrate_and_never_bridges_invalid_samples() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    def observe(seconds: int, rate: float = 100, **overrides):
        return _observe(
            tracker, state, seconds, rate,
            coin="QUANTUS", algorithm="qhash", **overrides,
        )

    first = _observe(tracker, state, 0, 100, coin="quantus", algorithm="QHash")
    assert first[0]["coin"] == "QUANTUS"
    assert first[0]["algorithm"] == "qhash"
    assert first[0]["estimated_work_total"] == "0"
    assert set(first[0]) == WIRE_FIELDS

    second = observe(10, 300)
    assert second[0]["estimated_work_total"] == "2000"
    assert observe(10, 300) == second

    observe(15, process_verified=False)
    resumed = observe(20)
    assert resumed[0]["estimated_work_total"] == "2000"
    assert observe(30)[0]["estimated_work_total"] == "3000"

    stale = _host(40, coin="QUANTUS", algorithm="qhash")
    reports = tracker.observe(stale, state, now=BASE + timedelta(seconds=71))
    assert reports[0]["estimated_work_total"] == "3000"
    assert observe(50)[0]["estimated_work_total"] == "3000"
    assert observe(60)[0]["estimated_work_total"] == "4000"
    assert observe(91)[0]["estimated_work_total"] == "4000"
    assert observe(101)[0]["estimated_work_total"] == "5000"


@pytest.mark.parametrize("sample_coin, canonical_coin", [
    (" xmr ", "XMR"), ("rvn", "RVN"), ("C00", "C00"),
    ("r_v.n+alt-1", "R_V.N+ALT-1"),
])
def test_other_safe_coin_samples_have_independent_fresh_integrals(
    sample_coin: str, canonical_coin: str,
) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    first = _observe(tracker, state, 0, coin=sample_coin, algorithm="SharedHash")
    reports = _observe(
        tracker, state, 10, 200, coin=canonical_coin, algorithm="sharedhash",
    )

    assert reports[0]["coin"] == canonical_coin
    assert reports[0]["counter_epoch"] == first[0]["counter_epoch"]
    assert reports[0]["estimated_work_total"] == "1500"
    assert set(reports[0]) == WIRE_FIELDS
    WorkloadCounter.model_validate(reports[0])


@pytest.mark.parametrize("coin", ["QTC", "QUAN-TUS"])
def test_other_coin_labels_keep_their_identity_instead_of_becoming_quantus(
    coin: str,
) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0, coin=coin, algorithm="qhash")
    reports = _observe(tracker, state, 10, coin=coin.lower(), algorithm="qhash")

    assert reports[0]["coin"] == coin
    assert reports[0]["estimated_work_total"] == "1000"
    WorkloadCounter.model_validate(reports[0])


@pytest.mark.parametrize("second_coin", ["QUANTUS", "XMR", "RVN"])
def test_same_algorithm_under_two_coins_keeps_totals_and_continuity_separate(
    second_coin: str,
) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    def sample(seconds: int, *, include_pearl: bool = True):
        host = _host(seconds, 100, algorithm="shared-hash")
        details = host["inventory"]["mining_status"]["hashrate_details"]
        if not include_pearl:
            details.clear()
        details.append({
            "coin": second_coin,
            "algorithm": "SHARED-HASH",
            "hashrate": 300.0,
            "hashrate_unit": "H/s",
        })
        return tracker.observe(host, state, now=BASE + timedelta(seconds=seconds))

    first = sample(0)
    assert {report["coin"] for report in first} == {"PEARL", second_coin}
    assert len({report["counter_epoch"] for report in first}) == 2
    second = {report["coin"]: report for report in sample(10)}
    assert second["PEARL"]["estimated_work_total"] == "1000"
    assert second[second_coin]["estimated_work_total"] == "3000"

    sample(20, include_pearl=False)
    restored = {report["coin"]: report for report in sample(30)}
    assert restored["PEARL"]["estimated_work_total"] == "1000"
    assert restored[second_coin]["estimated_work_total"] == "9000"
    assert restored["PEARL"]["snapshot_seq"] == 2
    assert restored[second_coin]["snapshot_seq"] == 3
    assert state.workload_counter_state["counters"] == list(restored.values())

    next_sample = {report["coin"]: report for report in sample(40)}
    assert next_sample["PEARL"]["estimated_work_total"] == "2000"
    assert next_sample[second_coin]["estimated_work_total"] == "12000"


def test_current_algorithm_is_reported_when_history_fills_one_wire_batch() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    first = _host(0)
    first["inventory"]["mining_status"]["hashrate_details"] = [
        {
            "role": "primary",
            "coin": "PEARL",
            "algorithm": f"pearlhash-{index}",
            "hashrate": 100.0,
            "hashrate_unit": "H/s",
        }
        for index in range(MAX_SERIES)
    ]

    reports = tracker.observe(first, state, now=BASE)
    assert len(reports) == MAX_SERIES

    rotated = _host(10, algorithm=f"pearlhash-{MAX_SERIES}")
    reports = tracker.observe(
        rotated,
        state,
        now=BASE + timedelta(seconds=10),
    )

    assert len(reports) == MAX_SERIES
    assert len({report["algorithm"] for report in reports}) == MAX_SERIES
    assert f"pearlhash-{MAX_SERIES}" in {
        report["algorithm"] for report in reports
    }
    assert len(tracker._counters) == MAX_SERIES + 1


def test_current_coin_is_reported_without_discarding_unconfirmed_history() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    def sample(seconds: int):
        host = _host(seconds)
        host["inventory"]["mining_status"]["hashrate_details"] = [
            {
                "coin": f"C{index:02d}",
                "algorithm": "sharedhash",
                "hashrate": 100.0,
                "hashrate_unit": "H/s",
            }
            for index in range(MAX_SERIES)
        ]
        return tracker.observe(host, state, now=BASE + timedelta(seconds=seconds))

    assert len(sample(0)) == MAX_SERIES
    baseline = sample(10)
    assert len(baseline) == MAX_SERIES
    assert all(report["estimated_work_total"] == "1000" for report in baseline)
    assert len({report["coin"] for report in baseline}) == MAX_SERIES
    for report in baseline:
        WorkloadCounter.model_validate(report)

    rotated = _observe(tracker, state, 20, coin=f"C{MAX_SERIES:02d}", algorithm="sharedhash")
    assert len(rotated) <= MAX_SERIES
    assert any(report["coin"] == f"C{MAX_SERIES:02d}" for report in rotated)
    retained = state.workload_counter_state["counters"]
    assert len(retained) == MAX_SERIES + 1
    assert all(report["estimated_work_total"] == "1000"
               for report in retained if report["coin"] != f"C{MAX_SERIES:02d}")


def test_only_first_64_hashrate_details_contribute_to_counters() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    def sample(seconds: int):
        host = _host(seconds)
        host["inventory"]["mining_status"]["hashrate_details"] = [
            {
                "coin": "PEARL",
                "algorithm": "pearlhash",
                "hashrate": 1.0,
                "hashrate_unit": "H/s",
            }
            for _ in range(64)
        ] + [{
            "coin": "XMR",
            "algorithm": "randomx",
            "hashrate": 1e101,
            "hashrate_unit": "H/s",
        }]
        return tracker.observe(host, state, now=BASE + timedelta(seconds=seconds))

    assert len(sample(0)) == 1
    reports = sample(10)
    assert len(reports) == 1
    assert reports[0]["coin"] == "PEARL"
    assert reports[0]["estimated_work_total"] == "640"
    WorkloadCounter.model_validate(reports[0])


def test_only_safe_coin_series_with_hps_are_counted() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")

    assert _observe(
        tracker, state, 0, coin="", algorithm="randomx"
    ) == []
    assert _observe(
        tracker, state, 5, coin=None, algorithm="pearlhash"
    ) == []
    assert _observe(
        tracker, state, 10, coin="PRL", algorithm="pearlhash"
    )[0]["coin"] == "PEARL"

    invalid_unit = _host(20)
    invalid_unit["inventory"]["mining_status"]["hashrate_details"][0][
        "hashrate_unit"
    ] = "T/s"
    reports = tracker.observe(
        invalid_unit, state, now=BASE + timedelta(seconds=20)
    )
    assert reports[0]["estimated_work_total"] == "0"


@pytest.mark.parametrize("coin", ["PEARL", "QUANTUS", "XMR"])
def test_restart_uses_new_epoch_and_counts_from_second_sample(
    tmp_path: Path, coin: str,
) -> None:
    path = tmp_path / "state.json"
    state = AgentState.new("agent-id", "secret")
    first_tracker = PearlWorkloadCounters()
    _observe(first_tracker, state, 0, coin=coin)
    old = _observe(first_tracker, state, 10, coin=coin)
    save_state(path, state)

    restored = load_state(path)
    assert restored is not None
    second_tracker = PearlWorkloadCounters()
    reports = _observe(second_tracker, restored, 20, coin=coin)

    assert len(reports) == 1
    assert reports[0]["coin"] == coin
    assert reports[0]["counter_epoch"] != old[0]["counter_epoch"]
    assert reports[0]["snapshot_seq"] == 0
    assert reports[0]["estimated_work_total"] == "0"

    resumed = _observe(second_tracker, restored, 30, coin=coin)
    assert len(resumed) == 1
    assert resumed[0]["counter_epoch"] == reports[0]["counter_epoch"]
    assert resumed[0]["snapshot_seq"] == 1
    assert resumed[0]["estimated_work_total"] == "1000"

def test_duplicate_persisted_algorithms_are_not_restored_as_two_series() -> None:
    state = AgentState.new("agent-id", "secret")
    counter = {
        "coin": "PEARL",
        "algorithm": "PearlHash",
        "counter_epoch": "11111111-1111-4111-8111-111111111111",
        "snapshot_seq": 7,
        "estimated_work_total": "7000",
        "observed_at": BASE.isoformat(),
    }
    duplicate = dict(counter)
    duplicate["algorithm"] = "pearlhash"
    duplicate["counter_epoch"] = "22222222-2222-4222-8222-222222222222"
    state.workload_counter_state = {
        "version": 1,
        "counters": [counter, duplicate],
    }

    reports = _observe(PearlWorkloadCounters(), state, 0)

    assert len(reports) == 1
    assert reports[0]["algorithm"] == "pearlhash"
    assert reports[0]["snapshot_seq"] == 0
    assert reports[0]["estimated_work_total"] == "0"
    assert reports[0]["counter_epoch"] not in {
        counter["counter_epoch"],
        duplicate["counter_epoch"],
    }


@pytest.mark.parametrize("coin", ["PEARL", "QUANTUS", "XMR"])
def test_runtime_integrates_before_failed_heartbeat_and_reports_after_recovery(
    tmp_path: Path, coin: str,
) -> None:
    runtime = AgentRuntime(
        AgentConfig(
            server_url="https://controller.test",
            state_file=tmp_path / "state.json",
        )
    )
    state = AgentState.new("agent-id", "secret")
    samples = iter((_host(0, coin=coin), _host(10, coin=coin), _host(20, coin=coin)))
    runtime._host_data = lambda: deepcopy(next(samples))
    captured: list[dict[str, object]] = []

    with (
        mock.patch.object(runtime, "_reconcile_pending_agent_update"),
        mock.patch("agent.pearl_workload.datetime", wraps=datetime) as clock,
    ):
        clock.now.side_effect = (
            BASE,
            BASE + timedelta(seconds=10),
            BASE + timedelta(seconds=20),
        )
        runtime.client.heartbeat = mock.Mock(side_effect=RuntimeError("offline"))
        with pytest.raises(RuntimeError, match="offline"):
            runtime.heartbeat_once(state)
        with pytest.raises(RuntimeError, match="offline"):
            runtime.heartbeat_once(state)

        def recovered(_state, host):
            captured.append(host)
            runtime.client.workload_ack_supported = False
            return []

        runtime.client.heartbeat = recovered
        assert runtime.heartbeat_once(state) == 0

    counters = captured[0]["workload_counters"]
    assert counters[0]["coin"] == coin
    # With no successful response yet, retry the zero baseline while retaining
    # the complete local integral. A legacy response releases this fence only.
    assert counters[0]["snapshot_seq"] == 0
    assert counters[0]["estimated_work_total"] == "0"
    assert state.workload_counter_state["counters"][0]["estimated_work_total"] == "2000"
    assert runtime._workload_counters.heartbeat_reports()[0]["estimated_work_total"] == "2000"


def test_client_payload_validates_as_strict_heartbeat_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RigPilotClient(
        AgentConfig(
            server_url="https://controller.test",
            state_file=Path("state.json"),
        )
    )
    captured = {}
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"commands":[]}'

    def request(*_args, **kwargs):
        captured.update(kwargs)
        return response

    monkeypatch.setattr(client.session, "request", request)
    host = _host(0)
    host["workload_counters"] = [
        {
            "coin": "PEARL",
            "algorithm": "pearlhash",
            "counter_epoch": "11111111-1111-4111-8111-111111111111",
            "snapshot_seq": 0,
            "estimated_work_total": "0",
            "observed_at": BASE.isoformat(),
        }
    ]

    client.heartbeat(AgentState.new("agent-id", "secret"), host)

    assert captured["json"]["workload_counters"] == host["workload_counters"]
    assert "workload_counters" not in captured["json"]["inventory"]


@pytest.mark.parametrize("invalid_sample", [
    10 ** 1000,
    "0001-01-01T00:00:00+14:00",
    "9999-12-31T23:59:59-14:00",
], ids=["oversized-rate", "utc-underflow", "utc-overflow"])
def test_unrepresentable_samples_do_not_crash_or_bridge(invalid_sample) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    baseline = _observe(tracker, state, 0)
    invalid = _host(5)
    mining = invalid["inventory"]["mining_status"]
    if isinstance(invalid_sample, int):
        mining["hashrate_details"][0]["hashrate"] = invalid_sample
    else:
        mining["stats_updated_at"] = invalid_sample
    assert tracker.observe(invalid, state, now=BASE + timedelta(seconds=5)) == baseline
    assert _observe(tracker, state, 10)[0]["estimated_work_total"] == "0"
    assert _observe(tracker, state, 20)[0]["estimated_work_total"] == "1000"


@pytest.mark.parametrize("coin, canonical_coin", [
    ("A" * WORKLOAD_COIN_MAX_LENGTH, "A" * WORKLOAD_COIN_MAX_LENGTH),
    (" " * (WORKLOAD_COIN_MAX_INPUT_LENGTH - len("XMR")) + "XMR", "XMR"),
], ids=["canonical-length-limit", "raw-input-length-limit"])
def test_coin_length_boundaries_produce_valid_canonical_reports(
    coin: str, canonical_coin: str,
) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0, coin=coin)
    reports = _observe(tracker, state, 10, coin=coin)

    assert reports[0]["coin"] == canonical_coin
    assert reports[0]["estimated_work_total"] == "1000"
    WorkloadCounter.model_validate(reports[0])


@pytest.mark.parametrize("coin", [
    "\tPEARL", "P\nEARL", "PEARL\x00", "PEARL\x7f",
    "A" * (WORKLOAD_COIN_MAX_LENGTH + 1),
    " " * (WORKLOAD_COIN_MAX_INPUT_LENGTH + 1 - len("XMR")) + "XMR",
    "XΜR", "PEARL\u00a0", "X/MR",
], ids=["leading-tab", "newline-alias", "nul", "del", "canonical-too-long", "raw-input-too-long", "non-ascii-letter", "non-ascii-space", "unsafe-ascii-separator"])
def test_invalid_coin_labels_do_not_poison_reports_or_bridge(coin: str) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0)
    baseline = _observe(tracker, state, 10)

    rejected = _observe(tracker, state, 20, coin=coin)
    assert rejected == baseline
    assert _observe(tracker, state, 30)[0]["estimated_work_total"] == "1000"
    recovered = _observe(tracker, state, 40)
    assert recovered[0]["estimated_work_total"] == "2000"
    for report in rejected + recovered:
        WorkloadCounter.model_validate(report)


@pytest.mark.parametrize("algorithm", [
    "pearl\nhash", "\tpearlhash", "pearlhash\x00", "pearlhash\x7f", "ß" * 64,
], ids=["newline", "leading-tab", "nul", "del", "casefold-expansion"])
def test_invalid_algorithms_do_not_poison_reports_or_bridge(algorithm: str) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0)
    baseline = _observe(tracker, state, 10)

    reports = _observe(tracker, state, 20, algorithm=algorithm)
    assert reports == baseline
    assert _observe(tracker, state, 30)[0]["estimated_work_total"] == "1000"
    recovered = _observe(tracker, state, 40)
    assert recovered[0]["estimated_work_total"] == "2000"
    for report in reports + recovered:
        WorkloadCounter.model_validate(report)


@pytest.mark.parametrize("algorithm, normalized", [
    ("ß" * 32, "ss" * 32),
    ("  Algo / Weird_Name-v1 . Test  ", "algo / weird_name-v1 . test"),
])
def test_safe_algorithms_keep_separators_and_casefold(
    algorithm: str, normalized: str,
) -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0, algorithm=algorithm)
    reports = _observe(tracker, state, 10, algorithm=algorithm)

    assert reports[0]["algorithm"] == normalized
    assert reports[0]["estimated_work_total"] == "1000"
    WorkloadCounter.model_validate(reports[0])


def test_out_of_range_integral_keeps_valid_snapshot_and_breaks_continuity() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0)
    baseline = _observe(tracker, state, 10)

    rejected = _observe(tracker, state, 20, 1e101)
    assert rejected == baseline
    assert state.workload_counter_state["counters"] == baseline
    first_good = _observe(tracker, state, 30)
    assert first_good[0]["estimated_work_total"] == "1000"
    assert first_good[0]["snapshot_seq"] == 2
    assert first_good[0]["observed_at"] == (BASE + timedelta(seconds=30)).isoformat()
    recovered = _observe(tracker, state, 40)
    assert recovered[0]["estimated_work_total"] == "2000"
    assert recovered[0]["snapshot_seq"] == 3
    for report in rejected + recovered:
        WorkloadCounter.model_validate(report)


def test_tiny_integral_is_exact_and_fits_the_wire_text_limit() -> None:
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0, 1e-200)
    reports = _observe(tracker, state, 10, 1e-200)

    total = reports[0]["estimated_work_total"]
    assert len(total) <= 128
    assert Decimal(total) == Decimal("1e-199")
    assert reports[0]["snapshot_seq"] == 1
    WorkloadCounter.model_validate(reports[0])


@pytest.mark.parametrize("retirement", [
    {"miner_type": "lolminer"}, {"miner_type": "forgeminer"},
    {"retired": True},
])
def test_retired_samples_preserve_counters_without_integrating_or_bridging(retirement):
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    _observe(tracker, state, 0)
    baseline = _observe(tracker, state, 10)
    retired = _host(20, 10000)
    retired["inventory"]["mining_status"].update(retirement)
    assert tracker.observe(retired, state, now=BASE + timedelta(seconds=20)) == baseline
    assert state.workload_counter_state["counters"] == baseline
    assert _observe(tracker, state, 30)[0]["estimated_work_total"] == "1000"
    assert _observe(tracker, state, 40)[0]["estimated_work_total"] == "2000"


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
def test_retired_samples_cannot_create_a_new_workload_series(miner_type):
    tracker = PearlWorkloadCounters()
    state = AgentState.new("agent-id", "secret")
    for seconds in (0, 10):
        retired = _host(seconds)
        retired["inventory"]["mining_status"]["miner_type"] = miner_type
        assert tracker.observe(retired, state, now=BASE + timedelta(seconds=seconds)) == []
    assert state.workload_counter_state["counters"] == []
