"""Seine's declared adapter wired into actual Agent runtime/API entry points.

All endpoints, process identities and executable paths are synthetic. No
miner process, external HTTP request or production configuration is used.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import inventory
from agent.managed_miners import ManagedMinerError, TRUSTED_MINER_RUNTIME_HANDLERS
from agent.miner_runtime_handlers import MinerRuntimePrepareContext
from agent.seine_telemetry import SeineTelemetryObserver
from agent.tests.test_managed_miner_status import FakeResponse
from agent.tests.test_seine_workload_bridge import _seine_state
from server.miner_adapters import MINER_ADAPTERS
from server.miner_adapters.validation import EXTRA_ARGS_RESERVED_FLAGS


def _context(tmp_path, *, options=None, password="", version="0.2.45"):
    primary = {
        "miner_type": "seine", "coin": "BNT", "algorithm": "blocknet-argon2id",
        "wallet_address": "synthetic-address-never-used-for-mining",
        "pool_url": "stratum+tcp://pool.example.test:3333", "pool_password": password,
        "miner_package": ({"version": version} if version is not None else {}), "extra_config": options or {},
        "device_indices": [],
    }

    def unexpected(*args, **kwargs):
        pytest.fail("CPU argument preparation attempted an external/GPU operation")

    return MinerRuntimePrepareContext(
        document={"primary": primary, "secondary": None, "worker_name": "synthetic-rig"},
        primary=primary, runtime=tmp_path / "runtime", executable=tmp_path / "package" / "seine",
        execution_mode="native", api_port=23456, miner_log=tmp_path / "launcher.log",
        wsl_runtime=None, system_name="Linux", device_runner=unexpected,
        privilege_checker=unexpected,
    )


def _prepare(context):
    adapter = MINER_ADAPTERS.require("seine")
    handler = TRUSTED_MINER_RUNTIME_HANDLERS.get(adapter.runtime_id)
    assert handler is not None
    return handler(context)


def test_registered_runtime_builds_fixed_cpu_pool_argv_with_auto_threads_and_private_api(tmp_path):
    context = _context(tmp_path)
    prepared = _prepare(context)
    argv = prepared.argv
    assert argv[:-1] == [
        str(context.executable), "--mode", "pool", "--backend", "cpu", "--address",
        context.primary["wallet_address"], "--pool-url", context.primary["pool_url"],
        "--pool-worker", "synthetic-rig", "--ui", "plain", "--api-server", "--api-bind",
        "127.0.0.1:23456", "--disable-update-check", "--telemetry", "off", "--data-dir",
    ]
    private = Path(argv[-1])
    assert private.parent == context.runtime / "seine-data"
    assert len(private.name) == 64 and all(char in "0123456789abcdef" for char in private.name)
    assert _prepare(context).argv == argv
    assert prepared.gpu_mapping == []
    assert prepared.config_path == private / "control-api.cookie"
    assert not context.runtime.exists()


def test_legacy_0215_arguments_do_not_receive_flags_introduced_later(tmp_path):
    prepared = _prepare(_context(tmp_path, version="0.2.15"))
    assert "--disable-update-check" not in prepared.argv
    assert "--telemetry" not in prepared.argv




def test_registered_runtime_preserves_large_positive_threads_profile_and_literal_extra_args(tmp_path):
    context = _context(tmp_path, options={
        "cpu_threads": 2048, "cpu_profile": "efficiency",
        "extra_args": '--future-label "two words" --future-value "$(literal)"',
    })
    argv = _prepare(context).argv
    assert argv[-8:] == [
        "--threads", "2048", "--cpu-profile", "efficiency", "--future-label", "two words",
        "--future-value", "$(literal)",
    ]
    cleared = _prepare(_context(tmp_path)).argv
    assert "--threads" not in cleared and "--cpu-profile" not in cleared
    assert argv[argv.index("--data-dir") + 1] != cleared[cleared.index("--data-dir") + 1]


@pytest.mark.parametrize("threads", [0, -1, True])
def test_registered_runtime_rejects_nonpositive_or_boolean_threads(tmp_path, threads):
    with pytest.raises(ManagedMinerError, match="cpu_threads"):
        _prepare(_context(tmp_path, options={"cpu_threads": threads}))


@pytest.mark.parametrize("password", ["x", " ", "arbitrary-nonempty"])
def test_registered_runtime_rejects_every_nonempty_pool_password(tmp_path, password):
    with pytest.raises(ManagedMinerError, match="password"):
        _prepare(_context(tmp_path, password=password))


@pytest.mark.parametrize("flag", sorted(EXTRA_ARGS_RESERVED_FLAGS["seine"]))
def test_registered_runtime_rejects_managed_field_overrides_in_literal_extra_args(tmp_path, flag):
    for extra_args in (flag + "=override", flag.upper() + " override"):
        with pytest.raises(ManagedMinerError, match="conflicts with managed parameter"):
            _prepare(_context(tmp_path, options={"extra_args": extra_args}))


@pytest.fixture
def local_api(monkeypatch, tmp_path, *, authenticated=True):
    clock = SimpleNamespace(seconds=0.0)
    epoch = 1_800_000_000.0
    monkeypatch.setattr(inventory, "time", SimpleNamespace(
        monotonic=lambda: clock.seconds, time=lambda: epoch + clock.seconds,
    ))
    monkeypatch.setattr(inventory, "_SEINE_TELEMETRY_OBSERVER", SeineTelemetryObserver())
    body = {"state": _seine_state(), "error": False}
    responses = []
    data_dir = tmp_path / "seine-data" / "run"
    record = {
        "pid": 4242, "create_time": epoch - 100, "argv_sha256": "a" * 64,
    }
    expected_headers = {"Accept": "application/json"}
    if authenticated:
        data_dir.mkdir(parents=True)
        (data_dir / "control-api.cookie").write_text("b" * 64)
        record["seine_data_dir"] = str(data_dir)
        expected_headers["Authorization"] = "Bearer " + "b" * 64

    def requester(url, **kwargs):
        assert url == "http://127.0.0.1:23456/v1/runtime/state"
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert kwargs["headers"] == expected_headers
        if body["error"]:
            raise TimeoutError("synthetic local API failure")
        response = FakeResponse(deepcopy(body["state"]))
        responses.append(response)
        return response

    status = {"version": 1, "miner_type": "seine", "api_host": "127.0.0.1", "api_port": 23456}
    leg = {"miner_type": "seine", "coin": "BNT", "algorithm": "blocknet-argon2id"}
    def poll(seconds, payload, *, unavailable=False):
        clock.seconds, body["state"], body["error"] = seconds, payload, unavailable
        result, error = inventory._managed_miner_api_stats(
            status, requester, (leg, None), process_record=record,
        )
        if not unavailable:
            assert responses[-1].closed is True
        return result, error

    return poll


def test_v6_cookie_reader_rejects_bad_files_and_legacy_records_have_no_auth(tmp_path):
    legacy = {"pid": 1}
    assert inventory._seine_api_headers(legacy) == {}
    good = tmp_path / "seine-data"
    good.mkdir()
    cookie = good / "control-api.cookie"
    cookie.write_text("c" * 64)
    assert inventory._seine_api_headers({"seine_data_dir": str(good)}) == {
        "Authorization": "Bearer " + "c" * 64
    }
    cookie.write_text("z" * 64)
    with pytest.raises(ValueError, match="invalid Seine API token"):
        inventory._seine_api_headers({"seine_data_dir": str(good)})


def test_actual_local_api_warms_then_publishes_only_native_counter_windows(local_api):
    epoch = 1_800_000_000.0

    def poll(seconds, elapsed, hashes):
        result, error = local_api(seconds, _seine_state(elapsed, hashes))
        assert error is None and result is not None
        assert result["shares"] is None and result["gpu_hashrate_status"] == "not_applicable"
        return result, result["hashrate_details"][0]

    for sample in ((0, 10, 1), (1, 15, 10)):
        result, detail = poll(*sample)
        assert result["hashrate"] is None and result["stats_updated_at"] is None
        assert detail["hashrate"] is None and "workload_source" not in detail

    result, detail = poll(2, 25, 60)
    assert result["hashrate"] == detail["hashrate"] == 5  # Upstream session rate is deliberately 999999.
    assert detail["workload_source"] == "seine_stats_window"
    assert detail["workload_delta_work"] == 50 and detail["shares"] is None
    assert detail["stats_updated_at"] == datetime.fromtimestamp(epoch + 2, timezone.utc).isoformat()
    fixed_time, fixed_id = detail["stats_updated_at"], detail["workload_sample_id"]

    duplicate, detail = poll(3, 25, 60)
    assert duplicate["hashrate"] == 5 and detail["workload_delta_work"] == 0
    assert detail["stats_updated_at"] == fixed_time and detail["workload_sample_id"] == fixed_id

    result, detail = poll(4, 26, 135)
    assert result["hashrate"] == 75 and detail["workload_delta_work"] == 75
    assert detail["workload_sample_id"] != fixed_id and detail["stats_updated_at"] != fixed_time


@pytest.mark.parametrize("interruption", ["invalid-payload", "idle", "http-error"])
def test_actual_local_api_invalidated_history_requires_two_new_stats_after_recovery(local_api, interruption):
    for seconds, elapsed, hashes in ((0, 10, 1), (10, 15, 10), (20, 25, 60)):
        result, error = local_api(seconds, _seine_state(elapsed, hashes))
        assert error is None
    assert result["hashrate"] == 5

    payload = {"invalid": True} if interruption == "invalid-payload" else {
        **_seine_state(25, 60), "running": False, "lifecycle": "idle",
    }
    result, error = local_api(21, payload, unavailable=interruption == "http-error")
    assert result is None and error is not None
    for seconds, elapsed, hashes in ((22, 25, 60), (23, 26, 135)):
        result, error = local_api(seconds, _seine_state(elapsed, hashes))
        assert error is None and result["hashrate"] is None
        assert result["stats_updated_at"] is None
        assert "workload_source" not in result["hashrate_details"][0]
    result, error = local_api(24, _seine_state(28, 143))
    assert error is None and result["hashrate"] == 4
    assert result["hashrate_details"][0]["workload_delta_work"] == 8
