from __future__ import annotations

import json
import platform
from functools import partial
from pathlib import Path
from typing import Any

import requests
import psutil
import pytest

import agent.inventory as inventory_module
from agent.config import AgentConfig
from agent.process_identity import managed_argv_fingerprint


LIVE_CREATED_AT = 3600.0
# These unit tests model a supported miner runtime even on a macOS development
# host. Explicit Windows/WSL and Linux cases can still override system_name.
TEST_SYSTEM = "Windows" if platform.system() == "Windows" else "Linux"
collect_mining_status = partial(
    inventory_module.collect_mining_status, system_name=TEST_SYSTEM,
    boot_time_factory=lambda: 0.0,
    process_iter_factory=lambda: (),
)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.content))}
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


def managed_config(tmp_path: Path) -> AgentConfig:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    packages = tmp_path / "packages"
    packages.mkdir()
    return AgentConfig(
        server_url="https://controller.example.test",
        state_file=tmp_path / "state.json",
        managed_mining_enabled=True,
        managed_miner_runtime_dir=runtime,
        miner_packages_dir=packages,
        flight_sheet_file=tmp_path / "flight.json",
        miner_status_file=runtime / "miner-status.json",
    )


class FakeProcess:
    def __init__(self, executable: Path, created: float = LIVE_CREATED_AT) -> None:
        self.executable = executable
        self.created = created

    def is_running(self) -> bool:
        return True

    def status(self) -> str:
        return "running"

    def exe(self) -> str:
        return str(self.executable)

    def create_time(self) -> float:
        return self.created


class RestartedProcess(FakeProcess):
    def __init__(
        self, pid: int, executable: Path, created: float, argv: list[str]
    ) -> None:
        super().__init__(executable, created)
        self.pid = pid
        self.argv = argv

    def cmdline(self) -> list[str]:
        return list(self.argv)

    def name(self) -> str:
        return self.executable.name


def write_process_record(
    config: AgentConfig,
    miner_type: str,
    *,
    actual_executable: Path | None = None,
    actual_created: float | None = None,
    record_created: float = LIVE_CREATED_AT,
    flight_sheet_id: str = "sheet",
) -> Any:
    assert config.managed_miner_runtime_dir is not None
    assert config.miner_packages_dir is not None
    package = config.miner_packages_dir / f"builtin-{miner_type}-test"
    package.mkdir(exist_ok=True)
    names = {
        ("Windows", "xmrig"): "xmrig.exe",
        ("Windows", "srbminer"): "SRBMiner-MULTI.exe",
        ("Windows", "pearlminer"): "miner.exe",
        ("Linux", "xmrig"): "xmrig",
        ("Linux", "srbminer"): "SRBMiner-MULTI",
        ("Linux", "pearlminer"): "miner-cuda12",
    }
    executable = package / names[(TEST_SYSTEM, miner_type)]
    executable.write_bytes(b"test")
    record = {
        "version": 1,
        "pid": 4242,
        "executable": str(executable.resolve()),
        "create_time": record_created,
        "miner_type": miner_type,
        "flight_sheet_id": flight_sheet_id,
        "started_at": "2026-07-14T01:00:00+00:00",
        "survives_agent_restart": True,
    }
    (config.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    process = FakeProcess(
        actual_executable or executable.resolve(),
        record_created if actual_created is None else actual_created,
    )

    def factory(pid: int) -> FakeProcess:
        assert pid == 4242
        return process

    return factory


def write_wsl_process_record(
    config: AgentConfig,
    wrapper: Path,
    target: Path,
    *,
    miner_type: str = "pearlminer",
) -> tuple[dict[str, Any], list[str]]:
    assert config.managed_miner_runtime_dir is not None
    wrapper_argv = [
        str(wrapper.resolve()),
        "--exec",
        f"/mnt/c/rigpilot/{target.name}",
        "--pool",
        "pool.example.test:3360",
    ]
    record = {
        "version": 3,
        "pid": 4242,
        "executable": str(wrapper.resolve()),
        "create_time": LIVE_CREATED_AT,
        "miner_type": miner_type,
        "flight_sheet_id": "sheet",
        "started_at": "2026-07-14T01:00:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": managed_argv_fingerprint(
            wrapper_argv,
            executable=wrapper,
        ),
        "api_port": 23456,
        "execution_mode": "wsl",
        "target_executable": str(target.resolve()),
    }
    (config.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    return record, wrapper_argv


def write_status(config: AgentConfig, miner_type: str, **overrides: Any) -> None:
    assert config.miner_status_file is not None
    value = {
        "version": 1,
        "state": "running",
        "miner_type": miner_type,
        "api_host": "127.0.0.1",
        "api_port": 23456,
        "updated_at": "2026-07-14T01:00:00+00:00",
        "hashrate": None,
        "shares": None,
    }
    value.update(overrides)
    config.miner_status_file.write_text(json.dumps(value), encoding="utf-8")


def write_dual_flight(config: AgentConfig) -> None:
    assert config.flight_sheet_file is not None
    config.flight_sheet_file.write_text(
        json.dumps(
            {
                "flight_sheet_id": "dual-sheet",
                "name": "PRL + XMR",
                "worker_name": "rig-01",
                "applied_at": "2026-07-14T01:00:00+00:00",
                "primary": {
                    "coin": "PRL",
                    "algorithm": "pearlhash",
                    "pool_url": "stratum+ssl://pearl.example.test:3360",
                    "miner_profile": "default",
                    "miner_package": None,
                },
                "secondary": {
                    "coin": "XMR",
                    "algorithm": "randomx",
                    "pool_url": "stratum+tcp://xmr.example.test:3333",
                    "miner_profile": "default",
                    "miner_package": None,
                },
            }
        ),
        encoding="utf-8",
    )




def test_pearlminer_log_reports_total_per_gpu_and_stable_session_id(tmp_path: Path):
    config = managed_config(tmp_path)
    assert config.flight_sheet_file is not None
    assert config.managed_miner_runtime_dir is not None
    config.flight_sheet_file.write_text(
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
    write_status(config, "pearlminer")
    (config.managed_miner_runtime_dir / "pearlminer.log").write_text(
        'event=large.progress height=123 proof_per_sec="830.93 T/s" '
        'devices="[RTX 4090: 96.01T/s, RTX 5080: 734.92T/s]"\n',
        encoding="utf-8",
    )
    process_factory = write_process_record(config, "pearlminer")

    first = collect_mining_status(config, process_factory=process_factory)
    second = collect_mining_status(config, process_factory=process_factory)

    assert first["state"] == "running"
    assert first["hashrate"] == pytest.approx(830.93e12)
    assert first["hashrate_details"][0]["gpu_hashrates"] == [
        {"index": 0, "hashrate": pytest.approx(96.01e12), "hashrate_unit": "H/s"},
        {"index": 1, "hashrate": pytest.approx(734.92e12), "hashrate_unit": "H/s"},
    ]
    assert first["shares"] is None
    assert "accepted_work_total" not in first
    assert first["session_id"] == second["session_id"]
    assert first["session_id"].startswith("managed-")

    restarted_factory = write_process_record(
        config,
        "pearlminer",
        record_created=LIVE_CREATED_AT + 60,
    )
    restarted = collect_mining_status(config, process_factory=restarted_factory)
    assert restarted["session_id"] != first["session_id"]


def test_independent_dual_reports_both_processes_and_separate_hashrates(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    assert config.flight_sheet_file is not None
    assert config.managed_miner_runtime_dir is not None
    assert config.miner_packages_dir is not None
    primary_leg = {
        "miner_type": "xmrig",
        "coin": "XMR",
        "algorithm": "randomx",
        "pool_url": "stratum+ssl://xmr.example.test:443",
        "miner_profile": "default",
        "miner_package": None,
    }
    secondary_leg = {
        "miner_type": "pearlminer",
        "coin": "PEARL",
        "algorithm": "pearlhash",
        "pool_url": "pearl://pearl.example.test:443",
        "miner_profile": "default",
        "miner_package": None,
    }
    root_document = {
        "version": 4,
        "secondary_mode": "independent",
        "flight_sheet_id": "sheet",
        "name": "XMR + PEARL",
        "worker_name": "rig-01",
        "applied_at": "2026-07-14T01:00:00+00:00",
        "primary": primary_leg,
        "secondary": secondary_leg,
        "gpu_oc": None,
    }
    config.flight_sheet_file.write_text(
        json.dumps(root_document), encoding="utf-8"
    )
    names = {
        "xmrig": "xmrig.exe" if TEST_SYSTEM == "Windows" else "xmrig",
        "pearlminer": (
            "miner.exe" if TEST_SYSTEM == "Windows" else "miner-cuda12"
        ),
    }
    processes: dict[int, FakeProcess] = {}
    for role, miner_type, leg, pid, port in (
        ("primary", "xmrig", primary_leg, 4242, 23456),
        ("secondary", "pearlminer", secondary_leg, 4243, 23457),
    ):
        role_runtime = config.managed_miner_runtime_dir / role
        role_runtime.mkdir()
        package = config.miner_packages_dir / f"builtin-{miner_type}-dual"
        package.mkdir()
        executable = package / names[miner_type]
        executable.write_bytes(b"test")
        (role_runtime / "managed-miner-process.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": pid,
                    "executable": str(executable.resolve()),
                    "create_time": LIVE_CREATED_AT + pid,
                    "miner_type": miner_type,
                    "flight_sheet_id": "sheet",
                    "started_at": "2026-07-14T01:00:00+00:00",
                    "survives_agent_restart": True,
                }
            ),
            encoding="utf-8",
        )
        (role_runtime / "miner-status.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "running",
                    "miner_type": miner_type,
                    "api_host": "127.0.0.1",
                    "api_port": port,
                    "updated_at": "2026-07-14T01:00:00+00:00",
                    "hashrate": None,
                    "shares": None,
                }
            ),
            encoding="utf-8",
        )
        (role_runtime / "active-flight-sheet.json").write_text(
            json.dumps(
                {
                    **root_document,
                    "version": 3,
                    "primary": leg,
                    "secondary": None,
                    "secondary_mode": None,
                }
            ),
            encoding="utf-8",
        )
        processes[pid] = FakeProcess(
            executable.resolve(), LIVE_CREATED_AT + pid
        )
    (config.managed_miner_runtime_dir / "secondary" / "pearlminer.log").write_text(
        'event=large.progress height=123 proof_per_sec="830.93 T/s" '
        'devices="[RTX 5080: 830.93T/s]"\n',
        encoding="utf-8",
    )

    def requester(url: str, **_kwargs: Any) -> FakeResponse:
        assert url == "http://127.0.0.1:23456/2/summary"
        return FakeResponse(
            {
                "hashrate": {"total": [12345.0]},
                "results": {"shares_good": 3, "shares_total": 3},
            }
        )

    status = collect_mining_status(
        config,
        requester=requester,
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
    )

    assert status["state"] == "running"
    assert status["process_verified"] is True
    assert status["hashrate"] == {
        "primary": 12345.0,
        "secondary": pytest.approx(830.93e12),
    }
    assert [item["role"] for item in status["hashrate_details"]] == [
        "primary",
        "secondary",
    ]
    assert status["shares"] is None
    assert status["proofs"] is None
    assert status["hashrate_details"][0]["shares"] == {
        "accepted": 3,
        "rejected": 0,
    }
    assert status["gpu_hashrate_status"] == "reported"

    mismatched_path = (
        config.managed_miner_runtime_dir
        / "secondary"
        / "active-flight-sheet.json"
    )
    mismatched = json.loads(mismatched_path.read_text(encoding="utf-8"))
    mismatched["flight_sheet_id"] = "different-sheet"
    mismatched_path.write_text(json.dumps(mismatched), encoding="utf-8")

    mismatched_status = collect_mining_status(
        config,
        requester=requester,
        process_factory=processes.__getitem__,
        boot_time_factory=lambda: 0.0,
    )
    assert mismatched_status["state"] == "stale"
    assert mismatched_status["process_verified"] is False
    assert "does not match" in mismatched_status["error"]


def test_independent_status_keeps_healthy_role_when_other_runtime_is_missing(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    assert config.flight_sheet_file is not None
    assert config.managed_miner_runtime_dir is not None
    assert config.miner_packages_dir is not None
    primary_leg = {
        "miner_type": "xmrig",
        "coin": "XMR",
        "algorithm": "randomx",
        "pool_url": "stratum+ssl://xmr.example.test:443",
        "miner_profile": "default",
        "miner_package": None,
    }
    root_document = {
        "version": 4,
        "secondary_mode": "independent",
        "flight_sheet_id": "sheet",
        "name": "XMR + PEARL",
        "worker_name": "rig-01",
        "applied_at": "2026-07-14T01:00:00+00:00",
        "primary": primary_leg,
        "secondary": {
            "miner_type": "pearlminer",
            "coin": "PEARL",
            "algorithm": "pearlhash",
            "pool_url": "pearl://pearl.example.test:443",
            "miner_profile": "default",
            "miner_package": None,
        },
        "gpu_oc": None,
    }
    config.flight_sheet_file.write_text(
        json.dumps(root_document), encoding="utf-8"
    )
    primary_runtime = config.managed_miner_runtime_dir / "primary"
    primary_runtime.mkdir()
    executable_name = "xmrig.exe" if TEST_SYSTEM == "Windows" else "xmrig"
    package = config.miner_packages_dir / "builtin-xmrig-dual"
    package.mkdir()
    executable = package / executable_name
    executable.write_bytes(b"test")
    (primary_runtime / "managed-miner-process.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 4242,
                "executable": str(executable.resolve()),
                "create_time": LIVE_CREATED_AT + 4242,
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet",
                "started_at": "2026-07-14T01:00:00+00:00",
                "survives_agent_restart": True,
            }
        ),
        encoding="utf-8",
    )
    (primary_runtime / "miner-status.json").write_text(
        json.dumps(
            {
                "version": 1,
                "state": "running",
                "miner_type": "xmrig",
                "api_host": "127.0.0.1",
                "api_port": 23456,
                "updated_at": "2026-07-14T01:00:00+00:00",
                "hashrate": None,
                "shares": None,
            }
        ),
        encoding="utf-8",
    )
    (primary_runtime / "active-flight-sheet.json").write_text(
        json.dumps(
            {
                **root_document,
                "version": 3,
                "primary": primary_leg,
                "secondary": None,
                "secondary_mode": None,
            }
        ),
        encoding="utf-8",
    )

    status = collect_mining_status(
        config,
        requester=lambda _url, **_kwargs: FakeResponse(
            {
                "hashrate": {"total": [12345.0]},
                "results": {"shares_good": 3, "shares_total": 3},
            }
        ),
        process_factory=lambda _pid: FakeProcess(
            executable.resolve(), LIVE_CREATED_AT + 4242
        ),
        boot_time_factory=lambda: 0.0,
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert status["running_roles"] == ["primary"]
    assert status["degraded"] is True
    assert "secondary" in status["role_errors"]
    assert status["hashrate"] == {"primary": 12345.0}
    assert [item["role"] for item in status["hashrate_details"]] == ["primary"]
    assert status["hashrate_details"][0]["shares"] == {
        "accepted": 3,
        "rejected": 0,
    }


def test_pearlminer_latest_complete_tui_table_reports_pool_and_proof_counters(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    assert config.flight_sheet_file is not None
    assert config.managed_miner_runtime_dir is not None
    config.flight_sheet_file.write_text(
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
                    "pool_url": "pearl://pool.example.test:443",
                    "miner_profile": "default",
                    "miner_package": None,
                },
                "secondary": None,
            }
        ),
        encoding="utf-8",
    )
    write_status(config, "pearlminer")
    (config.managed_miner_runtime_dir / "pearlminer.log").write_text(
        'event=large.progress height=123 proof_per_sec="972 T/s" '
        'devices="[RTX 5080: 242T/s, RTX 5080: 250T/s, '
        'RTX 5080: 233T/s, RTX 5080: 247T/s]"\n'
        '| # | Name | State | Hashrate | Proof S/R/D | Pool A/R(St) | Temp | Power |\n'
        '|   | TOTAL | -- | 900T | 10/1/2 | 8/1(1) | -- | 998W |\n'
        '| # | Name | State | Hashrate | Proof S/R/D | Pool A/R(St) | Temp | Power |\n'
        '\x1b[32m|     | TOTAL                        | --          |      975T |      18/0/1 |      18/0(0) |    -- |      1000W |    -- |             -- |\x1b[0m\n',
        encoding="utf-8",
    )
    process_factory = write_process_record(config, "pearlminer")

    status = collect_mining_status(config, process_factory=process_factory)

    assert status["shares"] == {"accepted": 18, "rejected": 0, "stale": 0}
    assert status["proofs"] == {"s": 18, "r": 0, "d": 1}
    assert status["hashrate_details"][0]["shares"] == status["shares"]
    assert status["hashrate_details"][0]["proofs"] == status["proofs"]


def test_pearlminer_incomplete_new_tui_table_does_not_reuse_old_counters(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    assert config.managed_miner_runtime_dir is not None
    write_status(config, "pearlminer")
    (config.managed_miner_runtime_dir / "pearlminer.log").write_text(
        'event=large.progress height=123 proof_per_sec="830.93 T/s"\n'
        '|   | TOTAL | -- | 800T | 10/0/0 | 10/0(0) | -- | 900W |\n'
        '| # | Name | State | Hashrate | Proof S/R/D | Pool A/R(St) | Temp | Power |\n'
        '| 0 | NVIDIA GeForce RTX 5080 | healthy | 242T | 3/0/0 | 3/0(0) | 66C | 250/250W |\n',
        encoding="utf-8",
    )
    process_factory = write_process_record(config, "pearlminer")

    status = collect_mining_status(config, process_factory=process_factory)

    assert status["hashrate"] == pytest.approx(830.93e12)
    assert status["shares"] is None
    assert status["hashrate_details"][0]["shares"] is None


def test_oc_profiles_report_srb_rules_and_persisted_nvml_state(tmp_path: Path):
    config = managed_config(tmp_path)
    assert config.flight_sheet_file is not None
    assert config.managed_miner_runtime_dir is not None
    config.flight_sheet_file.write_text(
        json.dumps(
            {
                "flight_sheet_id": "oc-sheet",
                "name": "PRL",
                "worker_name": "rig-01",
                "applied_at": "2026-07-14T01:00:00+00:00",
                "primary": {
                    "miner_type": "srbminer",
                    "coin": "PRL",
                    "algorithm": "pearlhash",
                    "pool_url": "stratum+ssl://pearl.example.test:3360",
                    "miner_profile": "default",
                    "miner_package": None,
                },
                "secondary": None,
                "gpu_oc": {
                    "vendor": "nvidia",
                    "settings": [
                        {
                            "model_match": "5080",
                            "power_limit_w": 250,
                            "core_clock_lock_mhz": 1800,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (config.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "settings": [
                    {
                        "index": 1,
                        "power_limit_w": 220,
                        "fan_percent": 70,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    status = collect_mining_status(config)

    assert status["gpu_oc_profiles"] == [
        {
            "source": "srbminer_cli",
            "state": "declared",
            "settings": [
                {
                    "model_match": "5080",
                    "power_limit_w": 250,
                    "core_clock_lock_mhz": 1800,
                }
            ],
        },
        {
            "source": "nvml",
            "state": "applied_record",
            "state_version": 1,
            "identity_complete": False,
            "settings": [
                {"index": 1, "power_limit_w": 220, "fan_percent": 70}
            ],
        },
    ]


def test_v2_oc_profile_retains_uuid_for_identity_safe_display(tmp_path: Path):
    config = managed_config(tmp_path)
    assert config.managed_miner_runtime_dir is not None
    (config.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json").write_text(
        json.dumps(
            {
                "version": 2,
                "devices": [
                    {
                        "gpu_uuid": "GPU-stale-device",
                        "index_hint": 3,
                        "setting": {"power_limit_w": 220, "fan_percent": 70},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    status = collect_mining_status(config)

    assert status["gpu_oc_profiles"] == [
        {
            "source": "nvml",
            "state": "applied_record",
            "state_version": 2,
            "identity_complete": True,
            "settings": [
                {
                    "index": 3,
                    "uuid": "GPU-stale-device",
                    "power_limit_w": 220,
                    "fan_percent": 70,
                }
            ],
        }
    ]


def test_oc_profiles_are_empty_for_no_oc_and_ignore_malformed_state(tmp_path: Path):
    config = managed_config(tmp_path)
    assert config.managed_miner_runtime_dir is not None
    (config.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "settings": [{"index": 0, "power_limit_w": "not-a-number"}],
            }
        ),
        encoding="utf-8",
    )

    status = collect_mining_status(config)

    assert status["gpu_oc_profiles"] == []


def test_xmrig_loopback_summary_is_minimized_and_does_not_leak(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(
        config,
        "xmrig",
        api_url="https://attacker.example/stats",
        api_token="must-not-be-used",
    )
    process_factory = write_process_record(config, "xmrig")
    response = FakeResponse(
        {
            "hashrate": {"total": [123.5, 120.0, 110.0], "highest": 999.0},
            "results": {"shares_good": 7, "shares_total": 9},
            "connection": {
                "pool": "pool-with-secret.example",
                "wallet": "super-secret-wallet",
            },
            "argv": ["--user", "super-secret-wallet"],
        }
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    def requester(url: str, **kwargs: Any) -> FakeResponse:
        calls.append((url, kwargs))
        return response

    status = collect_mining_status(
        config, requester=requester, process_factory=process_factory
    )

    assert calls == [
        (
            "http://127.0.0.1:23456/2/summary",
            {
                "headers": {"Accept": "application/json"},
                "timeout": (0.25, 0.75),
                "allow_redirects": False,
                "stream": True,
            },
        )
    ]
    assert response.closed is True
    assert status["state"] == "running"
    assert status["process_verified"] is True
    assert status["process_error"] is None

    assert status["hashrate"] == 123.5
    assert status["hashrate_unit"] == "H/s"
    assert status["shares"] == {"accepted": 7, "rejected": 2}
    assert status["stats_updated_at"] != "2026-07-14T01:00:00+00:00"
    encoded = json.dumps(status)
    for secret in (
        "attacker.example",
        "must-not-be-used",
        "pool-with-secret.example",
        "super-secret-wallet",
        "argv",
    ):
        assert secret not in encoded


def test_srbminer_current_api_maps_dual_stats_by_algorithm_without_summing(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "srbminer", api_port=24567)
    process_factory = write_process_record(
        config, "srbminer", flight_sheet_id="dual-sheet"
    )
    write_dual_flight(config)
    response = FakeResponse(
        {
            "miner_version": "3.4.6",
            "algorithms": [
                {
                    "name": "randomx",
                    "hashrate": {
                        "now": 0,
                        "1min": 12_500,
                        "cpu": {"total": 12_500, "thread0": 500},
                    },
                    "shares": {"accepted": 4, "rejected": 1},
                },
                {
                    "name": "pearlhash",
                    "hashrate": {
                        "now": 1_250_000_000,
                        "1min": 1_200_000_000,
                        "gpu": {
                            "total": 1_250_000_000,
                            "gpu0": 750_000_000,
                            "GPU_1": 500_000_000,
                            "gpu64": 999,
                            "gpu2": "not-a-number",
                        },
                    },
                    "shares": {"accepted": 2, "rejected": 0},
                },
            ],
            "pool": "must-not-be-reported",
        }
    )
    seen: list[str] = []

    def requester(url: str, **kwargs: Any) -> FakeResponse:
        seen.append(url)
        assert kwargs["allow_redirects"] is False
        return response

    status = collect_mining_status(
        config, requester=requester, process_factory=process_factory
    )

    assert seen == ["http://127.0.0.1:24567/"]
    assert status["state"] == "running"
    assert status["hashrate"] == {
        "primary": 1_250_000_000,
        "secondary": 12_500,
    }
    assert "total" not in status["hashrate"]
    assert status["hashrate_unit"] == "H/s"
    assert status["hashrate_details"] == [
        {
            "role": "primary",
            "coin": "PRL",
            "algorithm": "pearlhash",
            "pool_url": "stratum+ssl://pearl.example.test:3360",
            "hashrate": 1_250_000_000,
            "hashrate_unit": "H/s",
            "shares": {"accepted": 2, "rejected": 0},
            "gpu_hashrates": [
                {"index": 0, "hashrate": 750_000_000, "hashrate_unit": "H/s"},
                {"index": 1, "hashrate": 500_000_000, "hashrate_unit": "H/s"},
            ],
        },
        {
            "role": "secondary",
            "coin": "XMR",
            "algorithm": "randomx",
            "pool_url": "stratum+tcp://xmr.example.test:3333",
            "hashrate": 12_500,
            "hashrate_unit": "H/s",
            "shares": {"accepted": 4, "rejected": 1},
        },
    ]
    assert status["shares"] == {"accepted": 6, "rejected": 1}
    assert "must-not-be-reported" not in json.dumps(status)


@pytest.mark.parametrize("persist_mapping", [False, True])
def test_srbminer_mixed_device_ids_map_to_nvml_indices_by_pci_identity(
    tmp_path: Path, persist_mapping: bool
):
    """An integrated GPU0 must not shift SRBMiner gpu1 onto NVML GPU1."""

    config = managed_config(tmp_path)
    mapping = [
        {
            "logical_index": 0,
            "miner_id": 1,
            "vendor": "nvidia",
            "name": "nvidia_geforce_rtx_5080",
            "pci_bus_id": "0000:01:00.0",
        },
        {
            "logical_index": 1,
            "miner_id": 2,
            "vendor": "nvidia",
            "name": "nvidia_geforce_rtx_5070",
            "pci_bus_id": "0000:02:00.0",
        },
    ]
    write_status(
        config,
        "srbminer",
        api_port=24567,
        **({"gpu_mapping": mapping} if persist_mapping else {}),
    )
    process_factory = write_process_record(
        config, "srbminer", flight_sheet_id="dual-sheet"
    )
    write_dual_flight(config)
    response = FakeResponse(
        {
            "miner_version": "3.4.6",
            "gpu_devices": [
                {
                    "id": 0,
                    "topology_id": "0000:10:00.0",
                    "bus_id": 16,
                    "device": "gpu0",
                    "model": "amd_radeon_graphics",
                },
                {
                    "id": 1,
                    "topology_id": "0000:01:00.0",
                    "bus_id": 1,
                    "device": "gpu1",
                    "model": "nvidia_geforce_rtx_5080",
                },
                {
                    "id": 2,
                    "topology_id": "0000:02:00.0",
                    "bus_id": 2,
                    "device": "gpu2",
                    "model": "nvidia_geforce_rtx_5070",
                },
            ],
            "algorithms": [
                {
                    "name": "randomx",
                    "hashrate": {"now": 12_500, "cpu": {"total": 12_500}},
                    "shares": {"accepted": 4, "rejected": 1},
                },
                {
                    "name": "pearlhash",
                    "hashrate": {
                        "now": 1_250_000_000,
                        "gpu": {
                            "gpu1": 750_000_000,
                            "gpu2": 500_000_000,
                            "total": 1_250_000_000,
                        },
                    },
                    "shares": {"accepted": 2, "rejected": 0},
                },
            ],
        }
    )
    gpu_metrics = [
        {"index": 0, "pci_bus_id": "00000000:01:00.0"},
        {"index": 1, "pci_bus_id": "00000000:02:00.0"},
    ]

    status = collect_mining_status(
        config,
        requester=lambda *_args, **_kwargs: response,
        process_factory=process_factory,
        gpu_metrics=gpu_metrics,
    )

    primary = next(
        detail for detail in status["hashrate_details"] if detail["role"] == "primary"
    )
    assert primary["gpu_hashrates"] == [
        {"index": 0, "hashrate": 750_000_000, "hashrate_unit": "H/s"},
        {"index": 1, "hashrate": 500_000_000, "hashrate_unit": "H/s"},
    ]
    assert status["gpu_hashrate_status"] == "reported"


def test_srbminer_does_not_attach_rate_when_pci_identity_cannot_be_matched(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(
        config,
        "srbminer",
        gpu_mapping=[
            {
                "logical_index": 0,
                "miner_id": 1,
                "vendor": "nvidia",
                "name": "nvidia_geforce_rtx_5080",
                "pci_bus_id": "0000:09:00.0",
            }
        ],
    )
    process_factory = write_process_record(config, "srbminer")
    response = FakeResponse(
        {
            "algorithms": [
                {
                    "name": "pearlhash",
                    "hashrate": {
                        "now": 750_000_000,
                        "gpu": {"gpu1": 750_000_000, "total": 750_000_000},
                    },
                    "shares": {"accepted": 1, "rejected": 0},
                }
            ]
        }
    )

    status = collect_mining_status(
        config,
        requester=lambda *_args, **_kwargs: response,
        process_factory=process_factory,
        gpu_metrics=[{"index": 0, "pci_bus_id": "00000000:01:00.0"}],
    )

    assert status["hashrate_details"][0].get("gpu_hashrates") is None
    assert status["gpu_hashrate_status"] == "identity_unmatched"


@pytest.mark.parametrize(
    "api_gpu_devices",
    [
        None,
        [{"id": 1, "bus_id": 1}],
        [{"id": 1, "topology_id": "0000:01:00.0", "bus_id": 1}],
    ],
)
def test_srbminer_does_not_attach_rate_when_nvml_pci_identity_is_ambiguous(
    tmp_path: Path, api_gpu_devices: list[dict[str, Any]] | None,
):
    config = managed_config(tmp_path)
    write_status(
        config,
        "srbminer",
        gpu_mapping=[
            {
                "logical_index": 0,
                "miner_id": 1,
                "vendor": "nvidia",
                "name": "nvidia_geforce_rtx_5080",
                "pci_bus_id": "0000:01:00.0",
            }
        ],
    )
    process_factory = write_process_record(config, "srbminer")
    response = FakeResponse(
        {
            **({"gpu_devices": api_gpu_devices} if api_gpu_devices is not None else {}),
            "algorithms": [
                {
                    "name": "pearlhash",
                    "hashrate": {
                        "now": 750_000_000,
                        "gpu": {"gpu1": 750_000_000, "total": 750_000_000},
                    },
                    "shares": {"accepted": 1, "rejected": 0},
                }
            ]
        }
    )

    status = collect_mining_status(
        config,
        requester=lambda *_args, **_kwargs: response,
        process_factory=process_factory,
        gpu_metrics=[
            {"index": 0, "pci_bus_id": "00000000:01:00.0"},
            {"index": 1, "pci_bus_id": "00000000:01:00.0"},
        ],
    )

    assert status["hashrate_details"][0].get("gpu_hashrates") is None
    assert status["gpu_hashrate_status"] == "identity_unmatched"
    assert status["hashrate"] == 750_000_000


@pytest.mark.parametrize("pci_bus_id", ["00000000:01:00.0", "00000001:01:00.0"])
@pytest.mark.parametrize("raw_bus_id", [1, "0x1"])
def test_srbminer_bus_only_identity_keeps_unique_inventory_card(
    pci_bus_id: str, raw_bus_id: int | str,
):
    result = inventory_module._srbminer_gpu_index_map(
        {"gpu_devices": [{"id": 1, "bus_id": raw_bus_id}]},
        None,
        [{"index": 3, "pci_bus_id": pci_bus_id}],
    )
    assert result == {1: 3}


def test_srbminer_bus_only_identity_does_not_guess_between_pci_domains():
    result = inventory_module._srbminer_gpu_index_map(
        {"gpu_devices": [{"id": 1, "bus_id": 1}]},
        None,
        [
            {"index": 0, "pci_bus_id": "00000000:01:00.0"},
            {"index": 1, "pci_bus_id": "00000001:01:00.0"},
        ],
    )
    assert result == {}


def test_srbminer_official_legacy_fields_remain_compatible(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "srbminer")
    process_factory = write_process_record(config, "srbminer")

    def requester(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {
                "algorithm": "randomx",
                "hashrate_total_now": 321,
                "shares": {"accepted": 8, "rejected": 3},
            }
        )

    status = collect_mining_status(
        config, requester=requester, process_factory=process_factory
    )

    assert status["hashrate"] == 321
    assert status["hashrate_unit"] == "H/s"
    assert status["shares"] == {"accepted": 8, "rejected": 3}


def test_local_api_failure_never_breaks_running_heartbeat(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "xmrig")
    process_factory = write_process_record(config, "xmrig")

    def requester(url: str, **kwargs: Any) -> FakeResponse:
        raise requests.Timeout("wallet=must-not-leak")

    status = collect_mining_status(
        config, requester=requester, process_factory=process_factory
    )

    assert status["state"] == "running"
    assert status["hashrate"] is None
    assert status["shares"] is None
    assert status["error"] == "local xmrig API is unavailable"
    assert "must-not-leak" not in json.dumps(status)


def test_remote_api_metadata_is_rejected_without_a_request(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(
        config,
        "srbminer",
        api_host="miner.example.test",
        api_url="https://miner.example.test/stats",
        api_token="remote-secret",
    )
    process_factory = write_process_record(config, "srbminer")
    called = False

    def requester(url: str, **kwargs: Any) -> FakeResponse:
        nonlocal called
        called = True
        raise AssertionError("remote metadata must never reach the requester")

    status = collect_mining_status(
        config, requester=requester, process_factory=process_factory
    )

    assert called is False
    assert status["state"] == "running"
    assert status["error"] == "managed miner has invalid local API metadata"
    assert "miner.example.test" not in json.dumps(status)
    assert "remote-secret" not in json.dumps(status)


def test_stopped_managed_miner_does_not_poll_an_api(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "xmrig", state="stopped")

    def requester(url: str, **kwargs: Any) -> FakeResponse:
        raise AssertionError("stopped miners must not be polled")

    status = collect_mining_status(config, requester=requester)

    assert status["state"] == "stopped"
    assert status["error"] is None
    assert status["process_verified"] is True
    assert status["process_error"] is None


def test_cached_running_status_without_process_record_is_stale_after_reboot(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(config, "xmrig", hashrate=999, shares={"accepted": 5, "rejected": 0})

    status = collect_mining_status(
        config,
        requester=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a stale miner must not be polled")
        ),
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert status["process_error"] == "managed miner has no live process record"
    assert status["hashrate"] is None
    assert status["shares"] is None


def test_process_record_from_previous_host_boot_is_intentionally_stopped(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(
        config,
        "xmrig",
        hashrate=999,
        hashrate_unit="H/s",
        shares={"accepted": 5, "rejected": 0},
    )
    current_boot = LIVE_CREATED_AT
    write_process_record(
        config,
        "xmrig",
        record_created=current_boot - 1.0,
    )

    def forbidden_process_lookup(_pid: int) -> FakeProcess:
        raise AssertionError("a pre-reboot PID must not be inspected")

    status = collect_mining_status(
        config,
        process_factory=forbidden_process_lookup,
        boot_time_factory=lambda: current_boot,
        requester=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a pre-reboot miner must not be polled")
        ),
    )

    assert status["state"] == "stopped"
    assert status["process_verified"] is True
    assert status["process_error"] == (
        "host rebooted; managed miner was not restarted"
    )
    assert status["error"] is None
    assert status["hashrate"] is None
    assert status["hashrate_unit"] is None
    assert status["shares"] is None
    assert status["stats_updated_at"] is None


def test_missing_recorded_pid_is_stale(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "xmrig")
    write_process_record(config, "xmrig")

    def missing(pid: int) -> FakeProcess:
        raise psutil.NoSuchProcess(pid)

    status = collect_mining_status(config, process_factory=missing)

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert status["process_error"] == "managed miner process no longer exists"


def test_wsl_v3_record_verifies_exact_wrapper_and_linux_package_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = managed_config(tmp_path)
    write_status(config, "pearlminer")
    assert config.miner_packages_dir is not None
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"trusted-wrapper")
    target = config.miner_packages_dir / "builtin-pearlminer-test" / "miner-cuda13"
    target.parent.mkdir()
    target.write_bytes(b"linux-miner")
    record, wrapper_argv = write_wsl_process_record(config, wrapper, target)
    monkeypatch.setattr(
        inventory_module,
        "wsl_executable_path",
        lambda: wrapper.resolve(),
    )
    process = RestartedProcess(
        int(record["pid"]),
        wrapper.resolve(),
        float(record["create_time"]),
        wrapper_argv,
    )

    status = collect_mining_status(
        config,
        system_name="Windows",
        process_factory=lambda _pid: process,
        process_iter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("WSL wrapper verification must not scan replacements")
        ),
    )

    assert status["state"] == "running"
    assert status["process_verified"] is True
    assert status["process_error"] is None

    changed = RestartedProcess(
        int(record["pid"]),
        wrapper.resolve(),
        float(record["create_time"]),
        [*wrapper_argv, "--different-invocation"],
    )
    changed_status = collect_mining_status(
        config,
        system_name="Windows",
        process_factory=lambda _pid: changed,
        process_iter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("WSL argv mismatch must not scan replacements")
        ),
    )
    assert changed_status["state"] == "stale"
    assert changed_status["process_verified"] is False
    assert changed_status["process_error"] == (
        "managed miner process command line does not match"
    )


def test_wsl_v3_missing_wrapper_is_stale_without_replacement_or_distro_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = managed_config(tmp_path)
    write_status(config, "pearlminer")
    assert config.miner_packages_dir is not None
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"trusted-wrapper")
    target = config.miner_packages_dir / "builtin-pearlminer-test" / "miner-cuda12"
    target.parent.mkdir()
    target.write_bytes(b"linux-miner")
    write_wsl_process_record(config, wrapper, target)
    monkeypatch.setattr(
        inventory_module,
        "wsl_executable_path",
        lambda: wrapper.resolve(),
    )

    status = collect_mining_status(
        config,
        system_name="Windows",
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("WSL records must never guess a replacement wrapper")
        ),
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert status["process_error"] == "managed miner process no longer exists"


def test_wsl_v3_rejects_target_outside_packages_and_wrong_linux_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = managed_config(tmp_path)
    write_status(config, "pearlminer")
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"trusted-wrapper")
    outside = tmp_path / "outside" / "miner-cuda13"
    outside.parent.mkdir()
    outside.write_bytes(b"linux-miner")
    monkeypatch.setattr(
        inventory_module,
        "wsl_executable_path",
        lambda: wrapper.resolve(),
    )
    write_wsl_process_record(config, wrapper, outside)

    outside_status = collect_mining_status(config, system_name="Windows")
    assert outside_status["state"] == "error"
    assert outside_status["process_error"] == (
        "managed WSL miner target is outside the package directory"
    )

    assert config.miner_packages_dir is not None
    wrong = config.miner_packages_dir / "builtin-pearlminer-test" / "xmrig"
    wrong.parent.mkdir(exist_ok=True)
    wrong.write_bytes(b"wrong-linux-miner")
    write_wsl_process_record(config, wrapper, wrong)
    wrong_status = collect_mining_status(config, system_name="Windows")
    assert wrong_status["state"] == "error"
    assert wrong_status["process_error"] == "managed WSL miner target name is invalid"

    target = config.miner_packages_dir / "builtin-pearlminer-test" / "miner-cuda13"
    target.write_bytes(b"linux-miner")
    untrusted_wrapper = tmp_path / "untrusted" / "wsl.exe"
    untrusted_wrapper.parent.mkdir()
    untrusted_wrapper.write_bytes(b"lookalike-wrapper")
    write_wsl_process_record(config, untrusted_wrapper, target)
    wrapper_status = collect_mining_status(config, system_name="Windows")
    assert wrapper_status["state"] == "error"
    assert wrapper_status["process_error"] == "managed WSL wrapper path is invalid"


def test_unique_exact_watchdog_restart_is_securely_rebound(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "srbminer")
    write_process_record(config, "srbminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    argv = [
        record["executable"],
        "--algorithm",
        "pearlhash",
        "--api-port",
        "23456",
        "--wallet",
        "public-wallet",
    ]
    record.update(
        {
            "version": 2,
            "argv_sha256": managed_argv_fingerprint(argv),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    replacement = RestartedProcess(
        5252,
        Path(record["executable"]),
        float(record["create_time"]) + 20.0,
        argv,
    )

    def missing(pid: int) -> FakeProcess:
        raise psutil.NoSuchProcess(pid)

    status = collect_mining_status(
        config,
        process_factory=missing,
        process_iter_factory=lambda: [replacement],
    )

    assert status["state"] == "running"
    assert status["process_verified"] is True
    assert status["process_error"] is None
    rebound = json.loads(state_path.read_text(encoding="utf-8"))
    assert rebound["pid"] == 5252
    assert rebound["create_time"] == replacement.created
    assert rebound["argv_sha256"] == record["argv_sha256"]
    assert "public-wallet" not in state_path.read_text(encoding="utf-8")


def test_same_pid_creation_time_shift_is_rejected_as_pid_reuse(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(config, "srbminer")
    write_process_record(config, "srbminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    argv = [
        record["executable"],
        "--algorithm",
        "pearlhash",
        "--api-port",
        "23456",
        "--wallet",
        "public-wallet",
    ]
    record.update(
        {
            "version": 2,
            "argv_sha256": managed_argv_fingerprint(argv),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    shifted = RestartedProcess(
        int(record["pid"]),
        Path(record["executable"]),
        float(record["create_time"]) + 20.0,
        argv,
    )

    status = collect_mining_status(
        config,
        process_factory=lambda pid: shifted,
        process_iter_factory=lambda: [shifted],
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert status["process_error"] == (
        "managed miner PID creation time does not match"
    )
    unchanged = json.loads(state_path.read_text(encoding="utf-8"))
    assert unchanged == record
    assert "public-wallet" not in state_path.read_text(encoding="utf-8")


def test_same_pid_and_executable_with_changed_v2_argv_is_stale(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(config, "srbminer")
    write_process_record(config, "srbminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    expected_argv = [record["executable"], "--api-port", "23456", "--gpu-id", "0"]
    record.update(
        {
            "version": 2,
            "argv_sha256": managed_argv_fingerprint(expected_argv),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    changed = RestartedProcess(
        int(record["pid"]),
        Path(record["executable"]),
        float(record["create_time"]),
        [*expected_argv[:-1], "1"],
    )

    status = collect_mining_status(
        config,
        process_factory=lambda _pid: changed,
        process_iter_factory=lambda: [changed],
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert status["process_error"] == (
        "managed miner process command line does not match"
    )
    assert json.loads(state_path.read_text(encoding="utf-8")) == record


def test_linux_pearlminer_scrubbed_argv_keeps_recorded_process_verified(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(config, "pearlminer")
    write_process_record(config, "pearlminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    recorded_path = Path(record["executable"])
    linux_executable = recorded_path.with_name("miner-cuda13")
    linux_executable.write_bytes(b"test")
    expected_argv = [
        str(linux_executable),
        "--pool",
        "pool.example.test:3360",
        "--wallet",
        "public-wallet",
    ]
    record.update(
        {
            "version": 2,
            "executable": str(linux_executable.resolve()),
            "argv_sha256": managed_argv_fingerprint(
                expected_argv,
                executable=linux_executable,
            ),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    scrubbed = RestartedProcess(
        int(record["pid"]),
        linux_executable.resolve(),
        float(record["create_time"]),
        # The real miner overwrites its argv memory with NUL bytes.  psutil
        # preserves the original argc shape and exposes the padding as empty
        # strings rather than collapsing it to a single entry.
        ["miner-cuda13", "", "", "", ""],
    )

    status = collect_mining_status(
        config,
        system_name="Linux",
        process_factory=lambda _pid: scrubbed,
        process_iter_factory=lambda: [scrubbed],
    )

    assert status["state"] == "running"
    assert status["process_verified"] is True
    assert status["process_error"] is None
    # Existing v2 records remain secret-free and need no disruptive rewrite.
    assert json.loads(state_path.read_text(encoding="utf-8")) == record


def test_linux_pearlminer_scrubbed_argv_never_rebinds_another_pid(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(config, "pearlminer")
    write_process_record(config, "pearlminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    linux_executable = Path(record["executable"]).with_name("miner-cuda12")
    linux_executable.write_bytes(b"test")
    expected_argv = [
        str(linux_executable),
        "--pool",
        "pool.example.test:3360",
        "--wallet",
        "public-wallet",
    ]
    record.update(
        {
            "version": 2,
            "executable": str(linux_executable.resolve()),
            "argv_sha256": managed_argv_fingerprint(
                expected_argv,
                executable=linux_executable,
            ),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    unrelated = RestartedProcess(
        int(record["pid"]) + 100,
        linux_executable.resolve(),
        float(record["create_time"]) + 10.0,
        ["miner-cuda12"],
    )

    status = collect_mining_status(
        config,
        system_name="Linux",
        process_factory=lambda pid: (_ for _ in ()).throw(
            psutil.NoSuchProcess(pid)
        ),
        process_iter_factory=lambda: [unrelated],
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == record


def test_linux_pearlminer_non_scrubbed_changed_argv_remains_stale(
    tmp_path: Path,
):
    config = managed_config(tmp_path)
    write_status(config, "pearlminer")
    write_process_record(config, "pearlminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    linux_executable = Path(record["executable"]).with_name("miner")
    linux_executable.write_bytes(b"test")
    expected_argv = [str(linux_executable), "--wallet", "wallet-a"]
    record.update(
        {
            "version": 2,
            "executable": str(linux_executable.resolve()),
            "argv_sha256": managed_argv_fingerprint(
                expected_argv,
                executable=linux_executable,
            ),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    changed = RestartedProcess(
        int(record["pid"]),
        linux_executable.resolve(),
        float(record["create_time"]),
        [str(linux_executable), "--wallet", "wallet-b"],
    )

    status = collect_mining_status(
        config,
        system_name="Linux",
        process_factory=lambda _pid: changed,
        process_iter_factory=lambda: [changed],
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert status["process_error"] == (
        "managed miner process command line does not match"
    )
    assert json.loads(state_path.read_text(encoding="utf-8")) == record


@pytest.mark.parametrize("multiple_exact", [False, True])
def test_same_pid_creation_time_shift_fails_closed_without_one_exact_v2_identity(
    tmp_path: Path, multiple_exact: bool
):
    config = managed_config(tmp_path)
    write_status(config, "srbminer")
    write_process_record(config, "srbminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    expected_argv = [
        record["executable"],
        "--api-port",
        "23456",
        "--gpu-id",
        "0",
    ]
    record.update(
        {
            "version": 2,
            "argv_sha256": managed_argv_fingerprint(expected_argv),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    shifted = RestartedProcess(
        int(record["pid"]),
        Path(record["executable"]),
        float(record["create_time"]) + 20.0,
        (
            expected_argv
            if multiple_exact
            else [*expected_argv[:-1], "1"]
        ),
    )
    candidates = [shifted]
    if multiple_exact:
        candidates.append(
            RestartedProcess(
                int(record["pid"]) + 1,
                Path(record["executable"]),
                float(record["create_time"]) + 30.0,
                expected_argv,
            )
        )

    status = collect_mining_status(
        config,
        process_factory=lambda pid: shifted,
        process_iter_factory=lambda: candidates,
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    assert (
        status["process_error"]
        == "managed miner PID creation time does not match"
    )
    unchanged = json.loads(state_path.read_text(encoding="utf-8"))
    assert unchanged == record


@pytest.mark.parametrize("ambiguous", [False, True])
def test_watchdog_rebind_rejects_different_argv_and_ambiguous_matches(
    tmp_path: Path, ambiguous: bool
):
    config = managed_config(tmp_path)
    write_status(config, "srbminer")
    write_process_record(config, "srbminer")
    assert config.managed_miner_runtime_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    record = json.loads(state_path.read_text(encoding="utf-8"))
    expected_argv = [record["executable"], "--api-port", "23456", "--gpu-id", "0"]
    record.update(
        {
            "version": 2,
            "argv_sha256": managed_argv_fingerprint(expected_argv),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    candidates = [
        RestartedProcess(
            6001,
            Path(record["executable"]),
            float(record["create_time"]) + 10.0,
            expected_argv if ambiguous else [*expected_argv[:-1], "1"],
        )
    ]
    if ambiguous:
        candidates.append(
            RestartedProcess(
                6002,
                Path(record["executable"]),
                float(record["create_time"]) + 11.0,
                expected_argv,
            )
        )

    status = collect_mining_status(
        config,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: candidates,
    )

    assert status["state"] == "stale"
    assert status["process_verified"] is False
    unchanged = json.loads(state_path.read_text(encoding="utf-8"))
    assert unchanged["pid"] == record["pid"]


def test_reused_pid_path_or_creation_time_is_stale(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "xmrig")
    other = tmp_path / "unrelated.exe"
    other.write_bytes(b"other")
    wrong_path = write_process_record(config, "xmrig", actual_executable=other)

    status = collect_mining_status(config, process_factory=wrong_path)

    assert status["state"] == "stale"
    assert status["process_error"] == "managed miner PID belongs to another executable"

    wrong_time = write_process_record(
        config, "xmrig", actual_created=LIVE_CREATED_AT + 1.0
    )
    status = collect_mining_status(config, process_factory=wrong_time)
    assert status["state"] == "stale"
    assert status["process_error"] == "managed miner PID creation time does not match"


def test_uninspectable_or_invalid_process_record_is_error(tmp_path: Path):
    config = managed_config(tmp_path)
    write_status(config, "xmrig")
    write_process_record(config, "xmrig")

    def denied(pid: int) -> FakeProcess:
        raise psutil.AccessDenied(pid)

    status = collect_mining_status(config, process_factory=denied)
    assert status["state"] == "error"
    assert status["process_verified"] is False
    assert status["process_error"] == "managed miner process cannot be inspected"

    assert config.managed_miner_runtime_dir is not None
    (config.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        "{not-json", encoding="utf-8"
    )
    status = collect_mining_status(config)
    assert status["state"] == "error"
    assert status["process_error"] == "managed miner process record is invalid"


def test_process_record_rejects_untrusted_executable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = managed_config(tmp_path)
    write_status(config, "xmrig")
    process_factory = write_process_record(config, "xmrig")
    assert config.managed_miner_runtime_dir is not None
    assert config.miner_packages_dir is not None
    state_path = config.managed_miner_runtime_dir / "managed-miner-process.json"
    original = json.loads(state_path.read_text(encoding="utf-8"))
    expected_name = Path(original["executable"]).name

    outside = tmp_path / "outside" / expected_name
    outside.parent.mkdir()
    outside.write_bytes(b"not-managed")
    record = {**original, "executable": str(outside.resolve())}
    state_path.write_text(json.dumps(record), encoding="utf-8")
    status = collect_mining_status(config, process_factory=process_factory)
    assert status["state"] == "error"
    assert status["process_error"] == (
        "managed miner executable is outside the package directory"
    )

    wrong_name = config.miner_packages_dir / "builtin-xmrig-test" / "other.exe"
    wrong_name.write_bytes(b"not-xmrig")
    record["executable"] = str(wrong_name.resolve())
    state_path.write_text(json.dumps(record), encoding="utf-8")
    status = collect_mining_status(config, process_factory=process_factory)
    assert status["state"] == "error"
    assert status["process_error"] == "managed miner executable name is invalid"

    record = original
    state_path.write_text(json.dumps(record), encoding="utf-8")
    executable = Path(original["executable"])
    real_lstat = Path.lstat

    def reparse_lstat(path: Path):
        value = real_lstat(path)
        if path == executable:
            return type(
                "ReparseStat",
                (),
                {
                    "st_mode": value.st_mode,
                    "st_file_attributes": 0x400,
                },
            )()
        return value

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    status = collect_mining_status(config, process_factory=process_factory)
    assert status["state"] == "error"
    assert status["process_error"] == (
        "managed miner executable is not a regular local file"
    )


@pytest.mark.parametrize("include_matching_primary", [False, True])
def test_srbminer_named_mismatch_is_not_assigned_to_a_configured_coin(
    include_matching_primary: bool,
):
    primary = {"algorithm": "pearlhash", "coin": "PEARL", "pool_url": "stratum+tcp://pearl.example:1"}
    secondary = {"algorithm": "randomx", "coin": "XMR", "pool_url": "stratum+tcp://xmr.example:2"}
    algorithms = [{"name": "QUANTUS", "hashrate": {"now": 1234}}]
    if include_matching_primary:
        algorithms.append({"name": "pearlhash", "hashrate": {"now": 5678}})
    result = inventory_module._parse_srbminer_summary(
        {"algorithms": algorithms}, mining_legs=(primary, secondary if include_matching_primary else None)
    )
    assert result is not None
    unmatched = next(row for row in result["hashrate_details"] if row["algorithm"] == "quantus")
    assert unmatched["role"].startswith("algorithm_")
    assert "coin" not in unmatched and "pool_url" not in unmatched
    assert not any(row["role"] == "secondary" for row in result["hashrate_details"])
    if include_matching_primary:
        known = next(row for row in result["hashrate_details"] if row["role"] == "primary")
        assert known["coin"] == "PEARL" and known["hashrate"] == 5678


def test_srbminer_unnamed_legacy_rate_keeps_configured_leg_fallback():
    result = inventory_module._parse_srbminer_summary(
        {"algorithms": [{"hashrate": {"now": 1234}}]},
        mining_legs=({"algorithm": "pearlhash", "coin": "PEARL"}, None),
    )
    assert result is not None
    assert result["hashrate"] == 1234
    assert result["hashrate_details"][0]["role"] == "primary"
    assert result["hashrate_details"][0]["coin"] == "PEARL"


@pytest.mark.parametrize("miner_type", ["xmrig", "srbminer"])
@pytest.mark.parametrize("failure", ["timeout", "invalid_response"])
def test_builtin_api_failure_does_not_report_old_status_file_rate(
    tmp_path: Path, miner_type: str, failure: str,
):
    config = managed_config(tmp_path)
    write_status(config, miner_type, hashrate=999, hashrate_unit="H/s", shares={"accepted": 7, "rejected": 1})
    process_factory = write_process_record(config, miner_type)
    def requester(*_args, **_kwargs):
        if failure == "timeout":
            raise requests.Timeout("password=must-not-leak")
        return FakeResponse([])
    result = collect_mining_status(config, requester=requester, process_factory=process_factory)
    assert result["state"] == "running" and result["process_verified"] is True
    assert result["hashrate"] is None and result["hashrate_unit"] is None
    assert not result["hashrate_details"] and result["shares"] is None
    assert result["stats_updated_at"] is None
    if miner_type != "xmrig":
        assert result["gpu_hashrate_status"] == "api_unavailable"
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.parametrize("now, legacy_interval, new_interval, expected", [
    (None, None, 42, 42), (0, None, 42, 42),
    (12, None, 42, 12), (0, 9, 42, 9),
    (None, True, 42, 42), (None, None, True, None),
    (None, None, float("inf"), None),
    (None, None, float("nan"), None), (None, None, -1, None),
])
def test_srbminer_one_minute_interval_alias_preserves_current_and_finite_values(
    now, legacy_interval, new_interval, expected,
):
    from agent.inventory import _parse_srbminer_summary
    result = _parse_srbminer_summary({"algorithms": [{
        "name": "quantus", "hashrate": {
            "now": now, "1min": legacy_interval, "1m": new_interval,
        }, "shares": {"accepted": 2, "rejected": 0},
    }]})
    assert result is not None
    assert result["hashrate"] == expected
    assert result["shares"] == {"accepted": 2, "rejected": 0}
    assert result["hashrate_details"][0].get("gpu_hashrates", []) == []


@pytest.mark.parametrize("topology, expected", [
    (None, {}), ("0000:01:00.0", {}),
    ("0001:01:00.0", {7: 2}), ("0002:01:00.0", {}),
])
def test_srbminer_bus_alias_retains_ambiguity_of_removed_duplicate_pci(topology, expected):
    device = {"id": 7, "bus_id": 1}
    if topology is not None:
        device["topology_id"] = topology
    result = inventory_module._srbminer_gpu_index_map(
        {"gpu_devices": [device]}, None,
        [{"index": 0, "pci_bus_id": "0000:01:00.0"},
         {"index": 1, "pci_bus_id": "0000:01:00.0"},
         {"index": 2, "pci_bus_id": "0001:01:00.0"}],
    )
    assert result == expected


@pytest.mark.parametrize("matches", [False, True])
def test_independent_srb_named_stats_keep_unknown_algorithm_unbound(tmp_path, matches):
    from dataclasses import replace
    config = managed_config(tmp_path)
    legs = [
        {"miner_type": "srbminer", "coin": "PEARL", "algorithm": "pearlhash", "pool_url": "stratum+tcp://pearl.test:1"},
        {"miner_type": "srbminer", "coin": "XMR", "algorithm": "randomx", "pool_url": "stratum+tcp://xmr.test:2"},
    ]
    for leg in legs:
        leg.update(miner_profile="default", miner_package=None)
    document = {"version": 4, "flight_sheet_id": "sheet", "name": "dual", "worker_name": "fixture",
                "secondary_mode": "independent", "primary": legs[0], "secondary": legs[1]}
    config.flight_sheet_file.write_text(json.dumps(document))
    processes = {}
    for n, role in enumerate(("primary", "secondary")):
        runtime = config.managed_miner_runtime_dir / role
        runtime.mkdir()
        role_config = replace(config, managed_miner_runtime_dir=runtime,
                              flight_sheet_file=runtime / "active-flight-sheet.json",
                              miner_status_file=runtime / "miner-status.json")
        write_process_record(role_config, "srbminer", record_created=LIVE_CREATED_AT+n)
        path = runtime / "managed-miner-process.json"
        record = json.loads(path.read_text())
        record["pid"] += n
        path.write_text(json.dumps(record))
        processes[record["pid"]] = FakeProcess(Path(record["executable"]), record["create_time"])
        write_status(role_config, "srbminer", api_port=24560+n)
        role_config.flight_sheet_file.write_text(json.dumps({**document, "secondary_mode": None,
                                                             "primary": legs[n], "secondary": None}))
    def requester(url, **kwargs):
        name = ("pearlhash" if matches else "quantus") if url.endswith(":24560/") else "randomx"
        rate = 1234 if url.endswith(":24560/") else 5678
        return FakeResponse({"algorithms": [{"name": name, "hashrate": {"now": rate}}]})
    result = collect_mining_status(config, requester=requester,
                                   process_factory=processes.__getitem__, boot_time_factory=lambda: 0.0)
    assert result["state"] == "running" and result["process_verified"] is True
    if matches:
        assert result["hashrate"] == {"primary": 1234, "secondary": 5678}
        assert [d["role"] for d in result["hashrate_details"]] == ["primary", "secondary"]
    else:
        assert result["hashrate"] == {"secondary": 5678}
        unknown = next(d for d in result["hashrate_details"] if d["algorithm"] == "quantus")
        assert unknown["role"].startswith("algorithm_")
        assert "coin" not in unknown and "pool_url" not in unknown
        assert unknown["hashrate"] == 1234


@pytest.mark.parametrize("url, expected", [
    ("quic://node.test:1234", "quic://node.test:1234"),
    ("quic://[2001:db8::1]:1234", "quic://[2001:db8::1]:1234"),
    ("stratum+ssl://pool.test:443", "stratum+ssl://pool.test:443"),
    ("quic://secret@node.test:1234", None),
    ("quic://node.test:1234\t", None),
    ("quic://node.test:1234/?token=secret", None),
])
def test_mining_status_projects_shared_pool_transport_without_credentials(url, expected):
    value = inventory_module._mining_leg_summary({
        "miner_profile": "default", "miner_type": "srbminer", "algorithm": "quantus",
        "coin": "QUANTUS", "pool_url": url,
        "wallet_address": "payment-wallet-private", "pool_password": "node-auth-private",
    })
    assert value is not None
    assert value.get("pool_url") == expected
    encoded = json.dumps(value)
    assert "payment-wallet-private" not in encoded and "node-auth-private" not in encoded
    assert "secret" not in encoded


@pytest.mark.parametrize("coin, declared, actual", [
    ("XMR", None, "randomx"), ("XMR", "rx/0", "randomx"),
    ("QUANTUS", None, "quantus"), ("PEARL", None, "pearlhash"),
])
def test_srb_stats_bind_nullable_default_and_alias_without_changing_saved_wire(tmp_path, coin, declared, actual):
    config = managed_config(tmp_path)
    document = {"flight_sheet_id": "sheet", "name": "fixture", "worker_name": "fixture", "secondary": None,
                "primary": {"miner_type": "srbminer", "miner_profile": "default", "coin": coin,
                            "algorithm": declared, "pool_url": "stratum+tcp://pool.test:1"}}
    saved = json.dumps(document)
    config.flight_sheet_file.write_text(saved)
    write_status(config, "srbminer")
    process = write_process_record(config, "srbminer")
    result = collect_mining_status(config, process_factory=process,
        requester=lambda *args, **kwargs: FakeResponse({"algorithms": [{"name": actual, "hashrate": {"now": 1234}}]}))
    assert result["hashrate"] == 1234
    detail = result["hashrate_details"][0]
    assert detail["role"] == "primary" and detail["coin"] == coin
    assert detail["algorithm"] == actual
    assert config.flight_sheet_file.read_text() == saved
