from __future__ import annotations

import pytest

from server.miner_adapters.workload import (
    SUPPORTED_WORKLOAD_COINS, WORKLOAD_BASIS, canonical_workload_coin,
)


@pytest.mark.parametrize("value,expected", [
    ("PEARL", "PEARL"), (" pearl ", "PEARL"),
    ("QUANTUS", "QUANTUS"), (" quantus ", "QUANTUS"),
    ("XMR", "XMR"), ("xmr", "XMR"), ("RVN", "RVN"),
    ("C00", "C00"), ("custom_1", "CUSTOM_1"),
])
def test_canonical_supported_workload_names(value, expected):
    assert canonical_workload_coin(value) == expected
    assert canonical_workload_coin(expected) == expected


@pytest.mark.parametrize("value", [None, True, 1, [], {}, "", "X MR", "XMR\n", "XMR\x00", "XMR\x7f", "X" * 25, "ＸＭＲ", "ß", "BTC/USD"])
def test_invalid_coin_data_is_not_supported(value):
    assert canonical_workload_coin(value) is None
    assert canonical_workload_coin(value, allow_pearl_aliases=True) is None


@pytest.mark.parametrize("value", ["PRL", "prl"])
def test_prl_is_one_canonical_pearl_identity(value):
    assert canonical_workload_coin(value) == "PEARL"
    assert canonical_workload_coin(value, allow_pearl_aliases=True) == "PEARL"


def test_old_pearl_separator_aliases_remain_sample_collection_only():
    assert canonical_workload_coin("P-E_A R.L") is None
    assert canonical_workload_coin("P-E_A R.L", allow_pearl_aliases=True) == "PEARL"


def test_known_defaults_do_not_exclude_other_valid_observed_coins_or_alias_qtc():
    assert "XMR" in SUPPORTED_WORKLOAD_COINS
    assert "RVN" not in SUPPORTED_WORKLOAD_COINS
    assert canonical_workload_coin("RVN") == "RVN"
    assert canonical_workload_coin("QTC") == "QTC"
    assert canonical_workload_coin("QTC") != canonical_workload_coin("QUANTUS")


def test_basis_describes_an_estimate_not_difficulty_weighted_proofs():
    assert WORKLOAD_BASIS == "agent_hashrate_integral"


UTC_OVERFLOW_TIMESTAMPS = [
    "0001-01-01T00:00:00+14:00",
    "9999-12-31T23:59:59-14:00",
]


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS)
def test_counter_schema_rejects_unrepresentable_utc_timestamp(value):
    from pydantic import ValidationError
    from server.schemas import WorkloadCounter
    with pytest.raises(ValidationError, match="observed_at"):
        WorkloadCounter(coin="PEARL", algorithm="pearlhash", counter_epoch="epoch",
                        snapshot_seq=0, estimated_work_total="0", observed_at=value)


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS)
def test_settlement_schema_rejects_unrepresentable_utc_timestamp(value):
    from pydantic import ValidationError
    from server.schemas import WorkloadSettlementNoteUpdate
    with pytest.raises(ValidationError, match="settlement_at"):
        WorkloadSettlementNoteUpdate(settlement_at=value, expected_revision=0)


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS)
def test_ingest_utc_parser_ignores_unrepresentable_timestamp(value):
    from server.workload import _utc_time
    assert _utc_time(value) is None


@pytest.mark.parametrize("value,expected", [
    ("1969-12-31T23:59:59.900000+00:00", "1969-12-31T23:55:00+00:00"),
    ("1970-01-01T07:59:59.900000+08:00", "1969-12-31T23:55:00+00:00"),
    ("2026-10-05T12:34:56.123456+08:00", "2026-10-05T04:30:00+00:00"),
])
def test_bucket_floor_preserves_fractional_and_offset_calendar_time(value, expected):
    from datetime import datetime
    from server.workload import _bucket_start
    assert _bucket_start(datetime.fromisoformat(value)).isoformat() == expected


@pytest.mark.parametrize("value", UTC_OVERFLOW_TIMESTAMPS)
def test_boot_identity_ignores_unrepresentable_utc_timestamp(value):
    from server.main import _boot_time_identity
    assert _boot_time_identity({"boot_time": value}) is None
