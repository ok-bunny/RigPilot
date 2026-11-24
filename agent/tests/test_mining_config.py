from __future__ import annotations

import json

import pytest

from agent.config import ConfigError, load_config


def write_config(tmp_path, **overrides):
    raw = {
        "server_url": "https://example.test",
        "state_file": "state.json",
    }
    raw.update(overrides)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_old_config_remains_compatible_and_new_actions_default_off(tmp_path):
    config = load_config(write_config(tmp_path))
    assert config.flight_sheet_file is None
    assert config.allow_gpu_oc is False
    assert config.gpu_oc_allowed_vendors == frozenset({"nvidia"})
    assert config.gpu_core_clock_lock_min_mhz == 100
    assert config.gpu_core_clock_lock_max_mhz == 5000
    assert config.gpu_memory_clock_lock_max_mhz == 20000


def test_new_local_policy_is_resolved_and_validated(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            service_allowlist=["miner"],
            miner_service="miner",
            flight_sheet_file="miner/flight.json",
            miner_profile_allowlist=["xmrig-safe"],
            flight_sheet_extra_key_allowlist=["password", "tls"],
            allow_gpu_oc=True,
            gpu_power_limit_min_w=80,
            gpu_power_limit_max_w=200,
            gpu_core_clock_lock_min_mhz=900,
            gpu_core_clock_lock_max_mhz=2400,
            gpu_memory_clock_lock_min_mhz=1000,
            gpu_memory_clock_lock_max_mhz=12000,
        )
    )
    assert config.flight_sheet_file == (tmp_path / "miner" / "flight.json").resolve()
    assert config.miner_service == "miner"
    assert config.allow_gpu_oc is True
    assert config.gpu_power_limit_max_w == 200
    assert config.gpu_core_clock_lock_min_mhz == 900
    assert config.gpu_memory_clock_lock_max_mhz == 12000


def test_miner_service_and_gpu_ranges_fail_closed(tmp_path):
    with pytest.raises(ConfigError, match="service_allowlist"):
        load_config(write_config(tmp_path, miner_service="not-allowed"))
    with pytest.raises(ConfigError, match="minimum"):
        load_config(
            write_config(
                tmp_path,
                gpu_power_limit_min_w=300,
                gpu_power_limit_max_w=100,
            )
        )
    with pytest.raises(ConfigError, match="only nvidia"):
        load_config(write_config(tmp_path, gpu_oc_allowed_vendors=["amd"]))
    with pytest.raises(ConfigError, match="gpu_fan_min_percent"):
        load_config(write_config(tmp_path, gpu_fan_min_percent=20))
    with pytest.raises(ConfigError, match="GPU core lock minimum"):
        load_config(
            write_config(
                tmp_path,
                gpu_core_clock_lock_min_mhz=2500,
                gpu_core_clock_lock_max_mhz=1500,
            )
        )
