from __future__ import annotations

import copy

import pytest

from agent.seine_runtime import build_seine_cpu_arguments, parse_seine_runtime_state


def args(**overrides):
    # Opaque fixture, never a live mining address or node/wallet creation.
    return build_seine_cpu_arguments(**{
        "address": "fixture-address-not-for-live-use", "pool_url": "pool.example.test:3333",
        "worker": "test-worker", "data_dir": "/tmp/owned-seine-test/data", **overrides,
    })


def log(*, elapsed=15.0, rate="1.250", unit="KH", accepted=8, stale=4):
    return {
        "elapsed_secs": elapsed, "level": "info", "tag": "STATS",
        "message": f"elapsed=10.0s hashes=12500 rate={rate} {unit}/s templates=2 submitted=12 stale={stale} accepted={accepted} deferred=0 dropped=0",
    }


def state(**overrides):
    return {
        "lifecycle": "running", "running": True, "run_id": 1,
        "started_at_unix_ms": 1000,
        "effective_config": {"mode": "pool", "backend_specs": [{"kind": "cpu", "cpu_threads": 2}]},
        "metrics": {"accepted_blocks": 999, "submitted_blocks": 1000, "stale_submits": 1},
        "recent_logs": [log()], **overrides,
    }


def test_fixed_cpu_pool_plain_private_api_arguments_preserve_auto_sizing():
    actual = args()
    assert actual == [
        "--mode", "pool", "--backend", "cpu", "--address", "fixture-address-not-for-live-use",
        "--pool-url", "pool.example.test:3333", "--pool-worker", "test-worker", "--ui", "plain",
        "--api-server", "--api-bind", "127.0.0.1:9977", "--data-dir", "/tmp/owned-seine-test/data",
    ]
    assert not {"--threads", "--allow-oversubscribe", "--token", "--cookie", "--wallet-password", "--api-allow-unsafe-bind", "--nvidia-devices"}.intersection(actual)


def test_0244_external_network_defaults_are_explicit_and_off():
    actual = args(disable_update_check=True, disable_telemetry=True)
    position = actual.index("--data-dir")
    assert actual[position - 3:position] == [
        "--disable-update-check", "--telemetry", "off",
    ]
    assert "--telemetry-url" not in actual


def test_explicit_threads_have_no_invented_1024_cap_and_windows_path_is_one_argument():
    actual = args(threads=2048, data_dir=r"C:\ProgramData\RigPilot\Private Seine", api_port=19377)
    assert actual[-2:] == ["--threads", "2048"]
    assert actual[actual.index("--data-dir") + 1] == r"C:\ProgramData\RigPilot\Private Seine"
    assert actual[actual.index("--api-bind") + 1] == "127.0.0.1:19377"


@pytest.mark.parametrize("profile", ["balanced", "throughput", "efficiency"])
def test_documented_cpu_profile_choices_are_explicit_and_do_not_force_threads(profile):
    actual = args(cpu_profile=profile)
    assert actual[-2:] == ["--cpu-profile", profile]
    assert "--threads" not in actual


@pytest.mark.parametrize("profile", ["", "BALANCED", "performance", False])
def test_unknown_cpu_profile_rejected_without_coercion(profile):
    with pytest.raises(ValueError, match="CPU profile"):
        args(cpu_profile=profile)


@pytest.mark.parametrize("url", ["stratum+ssl://pool.test:443", "stratum+ws://pool.test:3333", "https://pool.test:3333", "pool.test:3333/path", "pool.test:3333?x=1", "user:secret@pool.test:3333", "pool.test", "pool.test:0", "pool.test:65536"])
def test_pool_contract_does_not_add_unimplemented_protocol_or_hidden_auth(url):
    with pytest.raises(ValueError):
        args(pool_url=url)


@pytest.mark.parametrize("change", [{"threads": 0}, {"threads": True}, {"threads": -1}, {"api_port": True}, {"api_port": 65536}, {"address": "--backend"}, {"address": "a b"}, {"worker": "name\n--mode daemon"}, {"data_dir": "relative"}, {"data_dir": "/"}, {"data_dir": "/tmp/../root"}])
def test_invalid_inputs_reject_before_any_external_action(change):
    with pytest.raises(ValueError):
        args(**change)


def test_ipv6_tcp_pool_is_supported_without_rewriting_explicit_endpoint():
    actual = args(pool_url="stratum+tcp://[2001:db8::1]:3333")
    assert actual[actual.index("--pool-url") + 1] == "stratum+tcp://[2001:db8::1]:3333"


def test_current_cpu_state_rate_converts_to_hs_but_never_invents_share_totals():
    result = parse_seine_runtime_state(state())
    assert result["hashrate_hs"] == 1250
    assert result["sample"]["upstream_stats_counters"]["stale"] == 4
    assert result["sample"]["upstream_stats_counters"]["accepted"] == 8
    assert result["block_diagnostics"]["accepted_blocks"] == 999
    assert result["accepted_shares"] is result["rejected_shares"] is result["invalid_shares"] is None


@pytest.mark.parametrize("unit,multiplier", [("H", 1), ("KH", 1000), ("MH", 1000000), ("GH", 1000000000)])
def test_official_decimal_rate_units(unit, multiplier):
    result = parse_seine_runtime_state(state(recent_logs=[log(rate="2.5", unit=unit)]))
    assert result["hashrate_hs"] == 2.5 * multiplier


def test_latest_valid_stats_selected_by_upstream_event_time_and_warning_is_not_rate():
    warning = {"tag": "STATS", "elapsed_secs": 40, "message": "running degraded: 0/1 backends active"}
    result = parse_seine_runtime_state(state(recent_logs=[log(elapsed=20, rate="3"), warning, log(elapsed=10, rate="1")]))
    assert result["hashrate_hs"] == 3000
    assert result["sample"]["log_elapsed_secs"] == 20


def test_retained_sample_fingerprint_does_not_change_merely_on_api_run_restart():
    first = parse_seine_runtime_state(state(run_id=1))
    restarted = parse_seine_runtime_state(state(run_id=2))
    assert first["run_id"] != restarted["run_id"]
    assert first["sample"]["fingerprint"] == restarted["sample"]["fingerprint"]
    assert len(first["sample"]["fingerprint"]) == 64


@pytest.mark.parametrize("lifecycle,running", [("idle", False), ("starting", True), ("stopping", True), ("error", False), ("running", False)])
def test_nonrunning_lifecycle_keeps_identity_but_never_reuses_rate(lifecycle, running):
    result = parse_seine_runtime_state(state(lifecycle=lifecycle, running=running))
    assert result["lifecycle"] == lifecycle
    assert result["hashrate_hs"] is None and result["sample"] is None


@pytest.mark.parametrize("change", [{"lifecycle": []}, {"running": 1}, {"run_id": True}, {"run_id": -1}, {"effective_config": {"mode": "daemon", "backend_specs": [{"kind": "cpu"}]}}, {"effective_config": {"mode": "pool", "backend_specs": [{"kind": "cpu"}, {"kind": "nvidia"}]}}, {"effective_config": None}])
def test_malformed_or_non_cpu_pool_contract_rejected(change):
    assert parse_seine_runtime_state(state(**change)) is None


@pytest.mark.parametrize("bad_log", [
    log(rate="NaN"), log(rate="1e309"), log(rate="9" * 400), log(unit="TH"),
    log(elapsed=float("nan")), log(elapsed=True), log(accepted=-1), log(stale=2**64),
])
def test_unknown_nonfinite_overflow_or_negative_telemetry_is_unavailable(bad_log):
    result = parse_seine_runtime_state(state(recent_logs=[bad_log]))
    assert result["hashrate_hs"] is None and result["sample"] is None


def test_only_last_upstream_200_logs_are_examined_and_no_input_mutation():
    data = state(recent_logs=[log()] + [{"tag": "SHARE", "elapsed_secs": i, "message": "accepted"} for i in range(200)])
    before = copy.deepcopy(data)
    result = parse_seine_runtime_state(data)
    assert result["hashrate_hs"] is None
    assert result["accepted_shares"] is None
    assert data == before
