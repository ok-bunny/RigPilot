from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import psutil
import pytest

import agent.inventory as inventory_module
from agent.config import AgentConfig
from agent.inventory import (
    annotate_nvidia_oc_capabilities,
    collect_host_data,
    collect_mining_status,
    cpu_model,
    cpu_temperature_metrics,
    linux_nvidia_display_devices,
    network_ip_addresses,
    nvidia_gpu_metrics,
    remote_access_metadata,
)


@pytest.mark.parametrize("value", [10**1000, -(10**1000)], ids=["huge-positive", "huge-negative"])
def test_oc_telemetry_rejects_unrepresentable_numbers_without_aborting(value):
    assert inventory_module._gpu_oc_settings_summary(
        [{"index": 0, "power_limit_w": value}], allow_model_match=False
    ) is None


def test_managed_process_rejects_unrepresentable_timestamp_before_process_probe(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    record = {
        "version": 1,
        "pid": 123,
        "executable": str(tmp_path / "xmrig"),
        "create_time": 10**1000,
        "miner_type": "xmrig",
        "flight_sheet_id": "flight-id",
        "started_at": "2026-10-03T00:00:00+00:00",
        "survives_agent_restart": True,
    }
    (runtime / "managed-miner-process.json").write_text(json.dumps(record), encoding="utf-8")
    config = AgentConfig(
        server_url="https://controller.test", state_file=tmp_path / "state.json",
        managed_miner_runtime_dir=runtime,
    )
    status = inventory_module._managed_process_truth(
        config, lambda _pid: pytest.fail("invalid time must not reach process lookup"),
        boot_time_factory=lambda: 0,
    )
    assert status == ("error", False, "managed miner process record is invalid", None)


@pytest.mark.parametrize("parser,payload", [
    (inventory_module._miner_stats, {"hashrate": 10**1000}),
    (inventory_module._parse_xmrig_summary, {"hashrate": {"total": [10**1000, 12.5]}}),
    (inventory_module._parse_srbminer_summary, {"hashrate_total_now": 10**1000}),
], ids=["local-status", "xmrig", "srbminer"])
def test_miner_telemetry_ignores_unrepresentable_rate_without_aborting(parser, payload):
    parsed = parser(payload)
    if parser is inventory_module._parse_xmrig_summary:
        assert parsed["hashrate"] == 12.5
    else:
        assert parsed is None or parsed["hashrate"] is None


def test_linux_cpu_telemetry_keeps_valid_sensor_after_unrepresentable_reading():
    sensors = {"coretemp": [
        SimpleNamespace(label="Package id 0", current=10**1000),
        SimpleNamespace(label="Package id 1", current=42.5),
    ]}
    result = cpu_temperature_metrics(system_name="Linux", sensors_factory=lambda: sensors)
    assert result["temperature_c"] == 42.5
    assert "Package id 1" in result["temperature_source"]


def test_device_rate_sum_overflow_does_not_emit_nonfinite_heartbeat():
    rate = "1" + "0" * 308
    lines = [f'event=large.progress.device device={index} proof_per_sec="{rate}/s"' for index in range(2)]
    parsed = inventory_module._parse_pearlminer_log("\n".join(lines))
    if parsed is not None:
        assert parsed["hashrate"] is None
    json.dumps(parsed, allow_nan=False)


def _write_pci_attribute(device: Path, name: str, value: str) -> None:
    device.mkdir(parents=True, exist_ok=True)
    (device / name).write_text(f"{value}\n", encoding="ascii")


def test_linux_pci_fallback_reports_only_nvidia_display_devices(
    tmp_path: Path, monkeypatch
):
    # Windows cannot create ':' in a temporary directory name.  Preserve the
    # production BDF parser while translating this test-only portable spelling.
    original_bus_key = inventory_module._nvidia_bus_key
    monkeypatch.setattr(
        inventory_module,
        "_nvidia_bus_key",
        lambda value: original_bus_key(value.replace("_", ":", 2)),
    )
    display = tmp_path / "0000_01_00.0"
    _write_pci_attribute(display, "vendor", "0x10de")
    _write_pci_attribute(display, "class", "0x030000")
    _write_pci_attribute(display, "device", "0xab01")
    _write_pci_attribute(display, "subsystem_vendor", "0x1b4c")
    _write_pci_attribute(display, "subsystem_device", "0xaa01")

    audio = tmp_path / "0000_01_00.1"
    _write_pci_attribute(audio, "vendor", "0x10de")
    _write_pci_attribute(audio, "class", "0x040300")

    other = tmp_path / "0000_02_00.0"
    _write_pci_attribute(other, "vendor", "0x1002")
    _write_pci_attribute(other, "class", "0x030000")

    devices = linux_nvidia_display_devices(tmp_path)
    assert len(devices) == 1
    gpu = devices[0]
    assert gpu["pci_bus_id"] == "00000000:01:00.0"
    assert gpu["pci_device_id"] == "ab01"
    assert gpu["pci_subsystem_id"] == "aa011b4c"
    assert gpu["board_vendor"] == "GALAX"
    assert gpu["driver_available"] is False
    assert gpu["error"] == "nvidia_driver_unavailable"
    assert gpu["index"] is None
    assert gpu["uuid"] is None
    assert gpu["temperature_c"] is None
    assert gpu["utilization_percent"] is None


def test_nvidia_metrics_use_pci_identity_when_linux_driver_is_unavailable():
    def runner(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "driver unavailable")

    placeholder = {
        "vendor": "nvidia",
        "driver_available": False,
        "pci_bus_id": "00000000:01:00.0",
        "temperature_c": None,
    }
    assert nvidia_gpu_metrics(
        "nvidia-smi",
        runner=runner,
        system_name="Linux",
        linux_pci_probe=lambda: [placeholder],
    ) == [placeholder]
    assert nvidia_gpu_metrics(
        "nvidia-smi",
        runner=runner,
        system_name="Linux",
        linux_pci_probe=lambda: [],
    ) == []


def test_nvidia_oc_capabilities_are_attached_per_card_without_guessing():
    class Backend:
        def supported_fields(self, indices):
            assert indices == [0, 1]
            return {
                0: ["power_limit_w", "core_clock_offset_mhz", "unknown"],
                1: [],
            }

        def supported_fan_limits(self):
            return {}

    gpus = [
        {"index": 0, "vendor": "nvidia"},
        {"index": 1, "vendor": "nvidia"},
        {"index": 2, "vendor": "amd"},
    ]
    annotate_nvidia_oc_capabilities(gpus, backend_factory=Backend)

    assert gpus[0]["oc_capabilities"] == [
        "power_limit_w",
        "core_clock_offset_mhz",
    ]
    assert gpus[1]["oc_capabilities"] == []
    assert "oc_capabilities" not in gpus[2]


def test_nvidia_inventory_attaches_confirmed_fan_hardware_range():
    class Backend:
        def supported_fields(self, indices):
            assert indices == [0]
            return {0: ["power_limit_w", "fan_percent"]}

        def supported_fan_limits(self):
            return {0: (42, 95)}

    gpus = [{"index": 0, "vendor": "nvidia"}]
    annotate_nvidia_oc_capabilities(gpus, backend_factory=Backend)

    assert gpus[0]["fan_min_percent"] == 42
    assert gpus[0]["fan_max_percent"] == 95


def test_nvidia_oc_capability_probe_failure_is_explicitly_empty():
    class Backend:
        def supported_fields(self, _indices):
            raise OSError("driver unavailable")

    gpus = [{"index": 0, "vendor": "nvidia"}]
    annotate_nvidia_oc_capabilities(gpus, backend_factory=Backend)
    assert gpus[0]["oc_capabilities"] == []


def test_network_ip_addresses_separates_lan_and_direct_public_interfaces():
    network = network_ip_addresses(
        ["127.0.0.1", "169.254.1.2", "192.168.50.12", "100.64.3.4", "fd00::2", "8.8.8.8", "2001:4860:4860::8888"]
    )

    assert network == {
        "schema_version": 1,
        "local_ip_addresses": ["100.64.3.4", "192.168.50.12", "fd00::2"],
        "public_ip_addresses": ["2001:4860:4860::8888", "8.8.8.8"],
    }


def config(tmp_path: Path, **overrides):
    values = {
        "server_url": "https://controller.example.test",
        "state_file": tmp_path / "state.json",
        "flight_sheet_file": tmp_path / "flight.json",
        "miner_status_file": tmp_path / "miner-status.json",
        "miner_service": "rig-miner",
        "service_allowlist": frozenset({"rig-miner"}),
    }
    values.update(overrides)
    return AgentConfig(**values)


def test_managed_inventory_metadata_stays_aligned_with_adapter_registry():
    adapters = inventory_module.MINER_ADAPTERS.list()
    expected_executables = {
        (system_name, adapter.legacy_miner_type): adapter.executable_candidates(
            system_name
        )
        for adapter in adapters
        for system_name in ("Windows", "Linux")
    }
    telemetry_ids = {adapter.telemetry_id for adapter in adapters}
    log_telemetry_ids = {
        telemetry_id
        for telemetry_id in telemetry_ids
        if telemetry_id.endswith(".log.v1")
    }

    assert inventory_module._MANAGED_MINER_TYPES == {
        adapter.legacy_miner_type for adapter in adapters
    }
    assert inventory_module._MANAGED_MINER_EXECUTABLES == expected_executables
    assert set(inventory_module._MANAGED_TELEMETRY_API_PATHS) == (
        telemetry_ids - log_telemetry_ids
    )
    assert log_telemetry_ids == {"builtin.pearlminer.log.v1"}


@pytest.mark.parametrize("adapter_available", [False, True])
def test_managed_inventory_rejects_unknown_telemetry_without_requesting_it(
    monkeypatch,
    adapter_available,
):
    class UnknownTelemetryAdapter:
        telemetry_id = "builtin.untrusted.http.v1"

    class UnknownTelemetryRegistry:
        @staticmethod
        def get(_miner_type):
            return UnknownTelemetryAdapter() if adapter_available else None

    calls = []
    monkeypatch.setattr(
        inventory_module,
        "MINER_ADAPTERS",
        UnknownTelemetryRegistry(),
    )

    stats, error = inventory_module._managed_miner_api_stats(
        {
            "version": 1,
            "miner_type": "untrusted",
            "api_host": "127.0.0.1",
            "api_port": 23456,
        },
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert stats is None
    assert error == "managed miner has invalid local API metadata"
    assert calls == []


@pytest.mark.parametrize("oversized", [False, True])
def test_local_miner_json_is_bounded_while_streaming(oversized):
    reads = []

    class Response:
        status_code = 200
        headers = {}

        @property
        def content(self):
            pytest.fail("must not eagerly buffer an untrusted response")

        def iter_content(self, chunk_size):
            if not oversized:
                yield b'{"hashrate":'
                yield b'12.5}'
                return
            for index in range(100):
                reads.append(index)
                yield b" " * chunk_size

    if oversized:
        with pytest.raises(ValueError, match="too large"):
            inventory_module._bounded_response_json(Response())
        assert len(reads) <= inventory_module._MAX_MINER_API_RESPONSE_BYTES // 16384 + 1
    else:
        assert inventory_module._bounded_response_json(Response()) == {"hashrate": 12.5}


def test_wsl_v5_inventory_uses_live_cached_guest_without_windows_source(
    tmp_path: Path, monkeypatch
):
    packages = tmp_path / "packages"
    runtime = tmp_path / "runtime"
    source = packages / "pearl" / "miner-cuda13"
    source.parent.mkdir(parents=True)
    runtime.mkdir()
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"MZ")
    key = "f" * 64
    guest = f"/home/user/.rigpilot/miner-cache-v1/{key}/miner-cuda13"
    record = {
        "version": 5,
        "pid": 123,
        "executable": str(wrapper.resolve()),
        "create_time": 1000.25,
        "miner_type": "pearlminer",
        "flight_sheet_id": "sheet-cache",
        "started_at": "2026-08-03T00:00:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": "a" * 64,
        "api_port": 23456,
        "execution_mode": "wsl",
        "target_executable": str(source),
        "wsl_token": "b" * 64,
        "wsl_guest_executable": guest,
        "wsl_identity": {
            "windows_session_id": 7,
            "windows_user_sid": "S-1-5-21-1000",
            "distribution": "Ubuntu-22.04",
            "linux_uid": 1000,
        },
        "wsl_cache_key": key,
    }
    (runtime / "managed-miner-process.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    cfg = config(
        tmp_path,
        managed_mining_enabled=True,
        miner_packages_dir=packages,
        managed_miner_runtime_dir=runtime,
    )
    monkeypatch.setattr(
        inventory_module, "wsl_executable_path", lambda: wrapper.resolve()
    )
    monkeypatch.setattr(
        inventory_module, "query_wsl_guest", lambda *_args, **_kwargs: True
    )

    state, verified, error, returned = inventory_module._managed_process_truth(
        cfg,
        lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        "Windows",
        lambda: 0.0,
        lambda: (),
    )

    assert (state, verified, error) == ("running", True, None)
    assert returned == record


def test_nvidia_inventory_exposes_pci_identity_for_miner_index_mapping():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] != (
            "--query-gpu=index,name,uuid,pci.bus_id,utilization.gpu,memory.total,"
            "memory.used,temperature.gpu,power.draw"
        ):
            return subprocess.CompletedProcess(argv, 2, "", "Field not supported")
        output = (
            "0, NVIDIA GeForce RTX 5080, GPU-safe-id, 00000000:01:00.0, "
            "75, 16384, 8000, 62, 240.5\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    metrics = nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux", memory_probe=lambda: {}
    )

    assert calls[0][0][1].startswith("--query-gpu=index,name,uuid,pci.bus_id,")
    assert any(call[0][1].endswith("power.draw") for call in calls)
    assert metrics == [
        {
            "index": 0,
            "name": "NVIDIA GeForce RTX 5080",
            "uuid": "GPU-safe-id",
            "vendor": "nvidia",
            "device_type": "discrete",
            "pci_bus_id": "00000000:01:00.0",
            "utilization_percent": 75.0,
            "memory_total_mib": 16384.0,
            "memory_used_mib": 8000.0,
            "temperature_c": 62.0,
            "power_w": 240.5,
            "fan_speed_percent": None,
            "graphics_clock_mhz": None,
            "memory_clock_mhz": None,
            "pci_subsystem_id": None,
            "board_vendor_id": None,
            "board_vendor": None,
            "serial_number": None,
            "vbios_version": None,
            "driver_version": None,
            "power_limit_w": None,
            "power_default_limit_w": None,
            "power_min_limit_w": None,
            "power_max_limit_w": None,
            "memory_vendor": None,
            "memory_type": None,
            "memory_identity_source": "nvml_private",
            "memory_identity_status": "unavailable",
            "memory_identity_unavailable_reason": "native_probe_unavailable",
        }
    ]


def test_nvidia_inventory_reports_supported_board_firmware_and_power_details():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "--query-gpu=pci.bus_id,fan.speed":
            return subprocess.CompletedProcess(
                argv, 0, "00000000:02:00.0, 43\n", ""
            )
        output = (
            "1, NVIDIA Fixture GPU, GPU-board-fixture, 00000000:02:00.0, "
            "75, 8192, 512, 48, 112.5, 0xAA021458, fixture-board-serial, "
            "FE.ED.00.00.01, 999.01, 160, 180, 100, 220\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    metrics = nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux", memory_probe=lambda: {}
    )

    assert len(calls) == 3
    assert "pci.sub_device_id" in calls[0][0][1]
    assert metrics == [
        {
            "index": 1,
            "name": "NVIDIA Fixture GPU",
            "uuid": "GPU-board-fixture",
            "vendor": "nvidia",
            "device_type": "discrete",
            "pci_bus_id": "00000000:02:00.0",
            "utilization_percent": 75.0,
            "memory_total_mib": 8192.0,
            "memory_used_mib": 512.0,
            "temperature_c": 48.0,
            "power_w": 112.5,
            "fan_speed_percent": 43.0,
            "graphics_clock_mhz": None,
            "memory_clock_mhz": None,
            "pci_subsystem_id": "0xAA021458",
            "board_vendor_id": "1458",
            "board_vendor": "Gigabyte",
            "serial_number": "fixture-board-serial",
            "vbios_version": "FE.ED.00.00.01",
            "driver_version": "999.01",
            "power_limit_w": 160.0,
            "power_default_limit_w": 180.0,
            "power_min_limit_w": 100.0,
            "power_max_limit_w": 220.0,
            "memory_vendor": None,
            "memory_type": None,
            "memory_identity_source": "nvml_private",
            "memory_identity_status": "unavailable",
            "memory_identity_unavailable_reason": "native_probe_unavailable",
        }
    ]


def test_nvidia_inventory_retries_base_fields_when_driver_rejects_details():
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        query = argv[1].removeprefix("--query-gpu=")
        if query == ",".join(inventory_module._NVIDIA_BASE_FIELDS):
            return subprocess.CompletedProcess(
                argv,
                0,
                "0, RTX 3060, GPU-id, 0000:01:00.0, 80, 12288, 100, 60, N/A\n",
                "",
            )
        if query == "pci.bus_id,pci.sub_device_id":
            return subprocess.CompletedProcess(
                argv, 0, "0000:01:00.0, 0x12347377\n", ""
            )
        return subprocess.CompletedProcess(argv, 2, "", "Field not supported")

    metrics = nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux"
    )

    assert len(calls) >= 3
    assert "pci.sub_device_id" in calls[0][1]
    assert metrics[0]["name"] == "RTX 3060"
    assert metrics[0]["power_w"] is None
    assert metrics[0]["vbios_version"] is None
    assert metrics[0]["board_vendor_id"] == "7377"
    assert metrics[0]["board_vendor"] == "Colorful"


def test_nvidia_inventory_merges_optional_fan_speed_by_pci_bus_not_row_order():
    def runner(argv, **kwargs):
        query = argv[1].removeprefix("--query-gpu=")
        if query == "pci.bus_id,fan.speed":
            return subprocess.CompletedProcess(
                argv,
                0,
                "00000000:02:00.0, 68\n00000000:01:00.0, 41\n",
                "",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                "0, RTX 5080, GPU-a, 00000000:01:00.0, 90, 16384, 100, 60, 200, "
                "0x12341458, N/A, vbios-a, 580.1, 250, 300, 200, 400\n"
                "1, RTX 5080, GPU-b, 00000000:02:00.0, 91, 16384, 110, 61, 201, "
                "0x12341458, N/A, vbios-b, 580.1, 250, 300, 200, 400\n"
            ),
            "",
        )

    metrics = nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux"
    )

    assert [gpu["fan_speed_percent"] for gpu in metrics] == [41.0, 68.0]


def test_nvidia_inventory_merges_current_clocks_by_pci_bus():
    def runner(argv, **kwargs):
        query = argv[1].removeprefix("--query-gpu=")
        if query == "pci.bus_id,fan.speed":
            return subprocess.CompletedProcess(argv, 2, "", "Field not supported")
        if query == (
            "pci.bus_id,clocks.current.graphics,clocks.current.memory"
        ):
            return subprocess.CompletedProcess(
                argv,
                0,
                (
                    "00000000:02:00.0, 2400, 14900\n"
                    "00000000:01:00.0, 2325, 14801\n"
                ),
                "",
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                "0, RTX 5080, GPU-a, 00000000:01:00.0, 90, 16384, 100, 60, 200, "
                "0x12341458, N/A, vbios-a, 580.1, 250, 300, 200, 400\n"
                "1, RTX 5080, GPU-b, 00000000:02:00.0, 91, 16384, 110, 61, 201, "
                "0x12341458, N/A, vbios-b, 580.1, 250, 300, 200, 400\n"
            ),
            "",
        )

    metrics = nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux"
    )

    assert [gpu["graphics_clock_mhz"] for gpu in metrics] == [2325.0, 2400.0]
    assert [gpu["memory_clock_mhz"] for gpu in metrics] == [14801.0, 14900.0]


def test_nvidia_inventory_keeps_all_gpus_when_optional_fan_query_is_unsupported():
    def runner(argv, **kwargs):
        query = argv[1].removeprefix("--query-gpu=")
        if query == "pci.bus_id,fan.speed":
            return subprocess.CompletedProcess(argv, 2, "", "Field not supported")
        return subprocess.CompletedProcess(
            argv,
            0,
            (
                "0, RTX 3060, GPU-a, 00000000:01:00.0, 80, 12288, 100, 60, 170, "
                "0x12347377, N/A, vbios-a, 470.1, 170, 170, 100, 200\n"
                "1, RTX 3060, GPU-b, 00000000:02:00.0, 81, 12288, 110, 61, 171, "
                "0x12347377, N/A, vbios-b, 470.1, 170, 170, 100, 200\n"
            ),
            "",
        )

    metrics = nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux"
    )

    assert [gpu["uuid"] for gpu in metrics] == ["GPU-a", "GPU-b"]
    assert [gpu["fan_speed_percent"] for gpu in metrics] == [None, None]


def test_nvidia_numeric_parser_rejects_non_finite_values():
    for value in ("nan", "NaN", "inf", "+Infinity", "-inf", "1e9999"):
        assert inventory_module._float_or_none(value) is None
    assert inventory_module._float_or_none("240.5") == 240.5


def test_nvidia_inventory_maps_observed_board_partners_and_windows_vram():
    def runner(argv, **kwargs):
        output = (
            "0, RTX 5080, GPU-a, 00000000:01:00.0, 99, 16384, 1000, 60, 200, "
            "0x12347377, N/A, vbios-a, 580.1, 250, 300, 200, 400\n"
            "1, RTX 5060 Ti, GPU-b, 00000000:02:00.0, 98, 16384, 900, 61, 190, "
            "0x12341771, N/A, vbios-b, 580.1, 200, 250, 150, 350\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    metrics = nvidia_gpu_metrics(
        "nvidia-smi",
        runner=runner,
        system_name="Windows",
        memory_probe=lambda: {
            "00000000:01:00.0": {
                "memory_vendor": "Samsung",
                "memory_type": "GDDR7",
            }
        },
    )

    assert metrics[0]["board_vendor"] == "Colorful"
    assert metrics[0]["memory_vendor"] == "Samsung"
    assert metrics[0]["memory_type"] == "GDDR7"
    assert metrics[0]["memory_identity_source"] == "nvapi"
    assert metrics[0]["memory_identity_status"] == "complete"
    assert metrics[0]["memory_identity_unavailable_reason"] is None
    assert metrics[1]["board_vendor"] == "Inno3D"
    assert metrics[1]["memory_vendor"] is None
    assert metrics[1]["memory_type"] is None
    assert metrics[1]["memory_identity_source"] == "nvapi"
    assert metrics[1]["memory_identity_status"] == "unavailable"
    assert metrics[1]["memory_identity_unavailable_reason"] == (
        "native_probe_unavailable"
    )


def test_linux_inventory_does_not_replace_failed_native_query_with_verified_table():
    # The low word is a public PCI vendor ID. All subsystem device numbers,
    # firmware revisions, device slots and telemetry are generated for this test.
    vendors = ("1458", "1043", "1043", "7377", "1771")
    rows = [
        (index, f"GPU-fixture-{index}", f"00000000:{index + 16:02x}:00.0",
         f"0x{0xAA10 + index:04X}{vendor}", f"FE.ED.00.01.{index:02X}")
        for index, vendor in enumerate(vendors)
    ]

    def runner(argv, **kwargs):
        query = argv[1].removeprefix("--query-gpu=")
        if query == "pci.bus_id,fan.speed":
            return subprocess.CompletedProcess(argv, 2, "", "Field not supported")
        output = "".join(
            f"{index}, NVIDIA Fixture GPU, {uuid}, {bus}, "
            f"75, 8192, 512, 48, 125, {subsystem}, N/A, {vbios}, "
            "999.01, 160, 180, 100, 220\n"
            for index, uuid, bus, subsystem, vbios in rows
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    metrics = nvidia_gpu_metrics("nvidia-smi", runner=runner, system_name="Linux", memory_probe=lambda: {})

    assert [gpu["board_vendor"] for gpu in metrics] == [
        "Gigabyte", "ASUS", "ASUS", "Colorful", "Inno3D"
    ]
    assert [gpu["memory_vendor"] for gpu in metrics] == [None] * 5
    assert [gpu["memory_type"] for gpu in metrics] == [None] * 5
    assert [gpu["memory_identity_source"] for gpu in metrics] == [
        "nvml_private"
    ] * 5


def test_linux_native_failure_does_not_use_model_or_board_memory_identity():
    def runner(argv, **kwargs):
        output = (
            "0, NVIDIA Fixture GPU B, GPU-a, 00000000:01:00.0, "
            "50, 8192, 256, 45, 125, 0x12341458, N/A, "
            "FE.ED.00.00.02, 999.01, 160, 180, 100, 220\n"
            "1, NVIDIA Fixture GPU A, GPU-b, 00000000:02:00.0, "
            "50, 8192, 256, 45, 125, 0x12341462, N/A, "
            "FE.ED.00.00.02, 999.01, 160, 180, 100, 220\n"
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    metrics = nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux"
    )

    assert len(metrics) == 2
    assert metrics[0]["memory_vendor"] is None
    assert metrics[0]["memory_type"] is None
    assert metrics[0]["memory_identity_source"] == "nvml_private"
    assert metrics[0]["memory_identity_status"] == "unavailable"
    assert metrics[0]["memory_identity_unavailable_reason"] == (
        "native_probe_identity_conflict"
    )
    assert metrics[1]["memory_vendor"] is None
    assert metrics[1]["memory_type"] is None
    assert metrics[1]["memory_identity_source"] == "nvml_private"
    assert metrics[1]["memory_identity_status"] == "unavailable"
    assert metrics[1]["memory_identity_unavailable_reason"] == (
        "native_probe_identity_conflict"
    )


def test_nvidia_inventory_never_fails_when_both_queries_are_unavailable():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 8)

    assert nvidia_gpu_metrics(
        "nvidia-smi", runner=runner, system_name="Linux"
    ) == []


def test_mining_status_reports_service_sheet_and_bounded_stats(tmp_path: Path):
    cfg = config(tmp_path)
    cfg.flight_sheet_file.write_text(
        json.dumps(
            {
                "flight_sheet_id": "sheet-1",
                "name": "ETC + ZIL",
                "worker_name": "worker-01",
                "applied_at": "2026-07-13T12:00:00+00:00",
                "primary": {
                    "miner_profile": "srbminer-etc",
                    "wallet_address": "must-not-be-reported",
                    "pool_password": "must-not-be-reported",
                    "miner_package": {
                        "id": "srbminer",
                        "name": "SRBMiner-MULTI",
                        "version": "3.2.0",
                        "installed_path": "must-not-be-reported",
                    },
                },
                "secondary": {
                    "miner_profile": "srbminer-zil",
                    "miner_package": None,
                },
            }
        ),
        encoding="utf-8",
    )
    cfg.miner_status_file.write_text(
        json.dumps(
            {
                "hashrate": {"total": 123.5, "primary": 120, "secondary": 3.5},
                "hashrate_unit": "MH/s",
                "shares": {"accepted": 42, "rejected": 1, "stale": 0},
                "updated_at": "2026-07-13T12:01:00+00:00",
                "ignored_secret": "not reported",
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "active\n", "")

    status = collect_mining_status(cfg, runner=runner, system_name="Linux")

    assert calls[0][0] == ["systemctl", "is-active", "--", "rig-miner"]
    assert calls[0][1]["shell"] is False
    assert status["configured"] is True
    assert status["state"] == "running"
    assert status["primary"] == {
        "miner_profile": "srbminer-etc",
        "miner_package": {"id": "srbminer", "name": "SRBMiner-MULTI", "version": "3.2.0"},
    }
    assert status["secondary"] == {
        "miner_profile": "srbminer-zil",
        "miner_package": None,
    }
    assert status["hashrate"] == {"total": 123.5, "primary": 120, "secondary": 3.5}
    assert status["shares"]["accepted"] == 42
    assert "wallet" not in json.dumps(status)
    assert "ignored_secret" not in status


def test_mining_status_safely_degrades_on_windows_and_missing_files(tmp_path: Path):
    cfg = config(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        output = "STATE              : 1  STOPPED\n"
        return subprocess.CompletedProcess(argv, 0, output, "")

    status = collect_mining_status(cfg, runner=runner, system_name="Windows")

    assert calls == [["sc.exe", "query", "rig-miner"]]
    assert status["configured"] is False
    assert status["state"] == "stopped"
    assert status["error"] is None


def test_valid_sheet_without_local_service_has_unknown_state(tmp_path: Path):
    cfg = config(tmp_path, miner_service=None, miner_status_file=None)
    cfg.flight_sheet_file.write_text(
        json.dumps(
            {
                "flight_sheet_id": "sheet",
                "name": "Primary",
                "worker_name": "worker",
                "primary": {"miner_profile": "safe", "miner_package": None},
                "secondary": None,
            }
        ),
        encoding="utf-8",
    )

    status = collect_mining_status(cfg, system_name="Linux")

    assert status["configured"] is True
    assert status["state"] == "unknown"
    assert status["service_state"] == "not_configured"


def test_linux_cpu_model_prefers_bounded_proc_cpuinfo(tmp_path: Path):
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        "processor : 0\nmodel name : AMD Ryzen 9 9950X 16-Core Processor\n",
        encoding="utf-8",
    )

    assert cpu_model(system_name="Linux", cpuinfo_path=cpuinfo) == (
        "AMD Ryzen 9 9950X 16-Core Processor"
    )


def test_windows_cpu_model_prefers_registry_brand_string(monkeypatch):
    class FakeRegistryKey:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    calls = []

    def open_key(root, path, reserved, access):
        calls.append((root, path, reserved, access))
        return FakeRegistryKey()

    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        KEY_READ=0x20019,
        OpenKey=open_key,
        QueryValueEx=lambda _key, name: (
            "AMD Ryzen 9 9950X 16-Core Processor            ",
            name,
        ),
    )
    monkeypatch.setattr(inventory_module, "winreg", fake_winreg)
    monkeypatch.setattr(
        inventory_module.platform,
        "processor",
        lambda: "AMD64 Family 26 Model 68 Stepping 0, AuthenticAMD",
    )
    monkeypatch.setenv(
        "PROCESSOR_IDENTIFIER", "AMD64 Family 26 Model 68 Stepping 0, AuthenticAMD"
    )

    assert cpu_model(system_name="Windows") == (
        "AMD Ryzen 9 9950X 16-Core Processor"
    )
    assert calls == [
        (
            "HKLM",
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            0,
            0x20019,
        )
    ]


def test_windows_cpu_model_falls_back_when_registry_read_fails(monkeypatch):
    def fail_open_key(*_args, **_kwargs):
        raise OSError("registry unavailable")

    fake_winreg = SimpleNamespace(
        HKEY_LOCAL_MACHINE="HKLM",
        KEY_READ=0x20019,
        OpenKey=fail_open_key,
    )
    monkeypatch.setattr(inventory_module, "winreg", fake_winreg)
    monkeypatch.setattr(
        inventory_module.platform, "processor", lambda: "Fallback Processor Name"
    )

    assert cpu_model(system_name="Windows") == "Fallback Processor Name"


def test_windows_cpu_model_falls_back_to_identifier_without_winreg(monkeypatch):
    monkeypatch.setattr(inventory_module, "winreg", None)
    monkeypatch.setattr(inventory_module.platform, "processor", lambda: "")
    monkeypatch.setenv("PROCESSOR_IDENTIFIER", "Windows Processor Identifier")

    assert cpu_model(system_name="Windows") == "Windows Processor Identifier"


def _temperature(label, current):
    return SimpleNamespace(label=label, current=current, high=None, critical=None)


def test_linux_cpu_temperature_prefers_amd_tdie_over_tctl_and_gpu_sensors():
    metrics = cpu_temperature_metrics(
        system_name="Linux",
        sensors_factory=lambda: {
            "amdgpu": [_temperature("edge", 81.0)],
            "k10temp": [
                _temperature("Tctl", 73.5),
                _temperature("Tdie", 68.25),
                _temperature("Tccd1", 66.0),
            ],
        },
    )

    assert metrics == {
        "temperature_c": 68.2,
        "temperature_source": "psutil:k10temp:Tdie",
    }


def test_linux_cpu_temperature_prefers_intel_package_over_individual_cores():
    metrics = cpu_temperature_metrics(
        system_name="Linux",
        sensors_factory=lambda: {
            "coretemp": [
                _temperature("Core 0", 70.0),
                _temperature("Package id 0", 76.0),
                _temperature("Core 1", 72.0),
            ]
        },
    )

    assert metrics["temperature_c"] == 76.0
    assert metrics["temperature_source"] == "psutil:coretemp:Package id 0"


def test_linux_cpu_temperature_uses_highest_equally_ranked_cpu_package():
    metrics = cpu_temperature_metrics(
        system_name="Linux",
        sensors_factory=lambda: {
            "coretemp": [
                _temperature("Package id 0", 64.0),
                _temperature("Package id 1", 71.0),
            ],
            "cpu_thermal": [_temperature("", 90.0)],
        },
    )

    assert metrics["temperature_c"] == 71.0
    assert metrics["temperature_source"].endswith("Package id 1")


def test_cpu_temperature_rejects_invalid_or_implausible_values_and_never_raises():
    assert cpu_temperature_metrics(
        system_name="Linux",
        sensors_factory=lambda: {
            "k10temp": [
                _temperature("Tdie", float("nan")),
                _temperature("Tctl", 999.0),
                _temperature("CPU", True),
            ]
        },
    ) == {}

    def unavailable():
        raise OSError("hwmon unavailable")

    assert cpu_temperature_metrics(
        system_name="Linux", sensors_factory=unavailable
    ) == {}


def test_windows_cpu_temperature_prefers_fixed_bundled_helper(tmp_path: Path):
    helper = tmp_path / "tools" / "cpu-temperature" / "RigPilot.CpuTemperature.exe"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"safe test helper")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "temperature_c": 72.25,
                    "sensor_name": "CPU Package",
                    "source": "LibreHardwareMonitor",
                }
            ),
            "",
        )

    metrics = cpu_temperature_metrics(
        system_name="Windows",
        runner=runner,
        windows_helper_path=helper,
    )

    assert metrics == {
        "temperature_c": 72.2,
        "temperature_source": "windows_helper:CPU Package",
    }
    assert calls[0][0] == [str(helper), "--json"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 4


def test_windows_cpu_temperature_uses_explicit_cpu_wmi_identifier(tmp_path: Path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        output = (
            '"Identifier","Name","Value"\n'
            '"/intelcpu/0/temperature/1","CPU Core #1","67"\n'
            '"/intelcpu/0/temperature/0","CPU Package","74.5"\n'
            '"/gpu-nvidia/0/temperature/0","GPU Core","89"\n'
        )
        return subprocess.CompletedProcess(argv, 0, output, "")

    metrics = cpu_temperature_metrics(
        system_name="Windows",
        runner=runner,
        windows_helper_path=tmp_path / "missing-helper.exe",
    )

    assert metrics == {
        "temperature_c": 74.5,
        "temperature_source": "windows_wmi:LibreHardwareMonitor:CPU Package",
    }
    assert calls[0][0][0] == "powershell.exe"
    assert calls[0][1]["shell"] is False
    assert "root\\LibreHardwareMonitor" in calls[0][0][-1]


def test_windows_cpu_temperature_without_helper_or_monitor_safely_degrades(
    tmp_path: Path,
):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        raise FileNotFoundError("PowerShell or monitor provider unavailable")

    assert cpu_temperature_metrics(
        system_name="Windows",
        runner=runner,
        windows_helper_path=tmp_path / "missing-helper.exe",
    ) == {}
    assert len(calls) == 2
    assert all(call[0][0] == "powershell.exe" for call in calls)
    assert all(call[1]["shell"] is False for call in calls)


def test_windows_cpu_temperature_rejects_monitor_zero_sentinel(tmp_path: Path):
    helper = tmp_path / "RigPilot.CpuTemperature.exe"
    helper.write_bytes(b"safe test helper")

    def runner(argv, **kwargs):
        if argv[0] == str(helper):
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {"temperature_c": 0, "sensor_name": "Core (Tctl/Tdie)"}
                ),
                "",
            )
        return subprocess.CompletedProcess(argv, 1, "", "provider unavailable")

    assert cpu_temperature_metrics(
        system_name="Windows",
        runner=runner,
        windows_helper_path=helper,
    ) == {}


def test_default_windows_cpu_temperature_is_cached_between_heartbeats(monkeypatch):
    calls = []
    monkeypatch.setattr(inventory_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(inventory_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(inventory_module, "_windows_cpu_temperature_cache", None)
    monkeypatch.setattr(
        inventory_module,
        "_windows_cpu_temperature",
        lambda runner, helper_path: calls.append((runner, helper_path))
        or {"temperature_c": 64.5},
    )

    assert cpu_temperature_metrics() == {"temperature_c": 64.5}
    assert cpu_temperature_metrics() == {"temperature_c": 64.5}
    assert len(calls) == 1


def test_missing_windows_cpu_sensor_uses_long_negative_cache(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(inventory_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(inventory_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(inventory_module, "_windows_cpu_temperature_cache", None)
    monkeypatch.setattr(
        inventory_module,
        "_windows_cpu_temperature",
        lambda runner, helper_path: calls.append((runner, helper_path)) or {},
    )

    assert cpu_temperature_metrics() == {}
    now[0] += 299.0
    assert cpu_temperature_metrics() == {}
    assert len(calls) == 1
    now[0] += 2.0
    assert cpu_temperature_metrics() == {}
    assert len(calls) == 2


def test_remote_access_metadata_requires_opt_in_linux_and_non_loopback_listener():
    cfg = SimpleNamespace(
        advertise_ssh=True,
        advertise_sftp=True,
        ssh_port=2222,
        ssh_username="operator",
    )
    listening = SimpleNamespace(status="LISTEN", laddr=("0.0.0.0", 2222))

    metadata = remote_access_metadata(
        cfg,
        system_name="Linux",
        connections_factory=lambda **_kwargs: [listening],
    )

    assert metadata == {
        "schema_version": 1,
        "ssh": {"available": True, "port": 2222, "username": "operator"},
        "sftp": {"available": True, "port": 2222, "username": "operator"},
    }
    assert remote_access_metadata(cfg, system_name="Windows") is None


def test_remote_access_metadata_does_not_advertise_loopback_only_listener():
    cfg = SimpleNamespace(
        advertise_ssh=True,
        advertise_sftp=False,
        ssh_port=22,
        ssh_username=None,
    )
    listening = SimpleNamespace(status="LISTEN", laddr=("127.0.0.1", 22))

    metadata = remote_access_metadata(
        cfg,
        system_name="Linux",
        connections_factory=lambda **_kwargs: [listening],
    )

    assert metadata == {
        "schema_version": 1,
        "ssh": {"available": False, "port": 22},
        "sftp": {"available": False, "port": 22},
    }


def test_collect_host_data_has_cpu_model_and_memory_total(monkeypatch):
    _mock_host_resources(monkeypatch)
    monkeypatch.setattr("agent.inventory.cpu_model", lambda: "Test CPU")
    monkeypatch.setattr(
        "agent.inventory.cpu_temperature_metrics",
        lambda: {
            "temperature_c": 65.5,
            "temperature_source": "psutil:k10temp:Tdie",
        },
    )
    host = collect_host_data(collect_gpu=False)

    assert host["metrics"]["cpu"]["model"] == "Test CPU"
    assert host["metrics"]["cpu"]["temperature_c"] == 65.5
    assert host["metrics"]["cpu"]["temperature_source"] == "psutil:k10temp:Tdie"
    assert host["inventory"]["processor"] == "Test CPU"
    assert host["metrics"]["memory"]["total_bytes"] > 0
    assert host["inventory"]["ip_addresses"] == host["ip_addresses"]
    assert host["inventory"]["network"]["local_ip_addresses"] == host["local_ip_addresses"]
    assert host["inventory"]["network"]["public_ip_addresses"] == host["public_ip_addresses"]


def _mock_host_resources(monkeypatch):
    # Inventory is synthetic here; host psutil/sysctl calls are unavailable in
    # restricted macOS test runs and unrelated to capability selection.
    monkeypatch.setattr(
        inventory_module.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=1024, available=512, used=512, percent=50),
    )
    monkeypatch.setattr(
        inventory_module.psutil,
        "swap_memory",
        lambda: SimpleNamespace(total=0, used=0, percent=0),
    )
    monkeypatch.setattr(inventory_module.psutil, "cpu_freq", lambda: None)
    monkeypatch.setattr(inventory_module.psutil, "cpu_percent", lambda **_kwargs: 0)
    monkeypatch.setattr(inventory_module.psutil, "cpu_count", lambda **_kwargs: 4)
    monkeypatch.setattr(inventory_module.psutil, "boot_time", lambda: 1)
    monkeypatch.setattr(inventory_module.psutil, "net_if_addrs", lambda: {})
    monkeypatch.setattr(inventory_module, "cpu_temperature_metrics", lambda: {})
    monkeypatch.setattr(inventory_module, "ip_addresses", lambda: [])
    monkeypatch.setattr(
        inventory_module,
        "network_ip_addresses",
        lambda _addrs: {"local_ip_addresses": [], "public_ip_addresses": []},
    )
    monkeypatch.setattr(inventory_module, "_disk_metrics", lambda: [])


@pytest.mark.parametrize("managed_mining_enabled", [False, True])
def test_windows_heartbeat_inventory_preserves_mining_capabilities(monkeypatch, managed_mining_enabled):
    _mock_host_resources(monkeypatch)
    monkeypatch.setattr(inventory_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(inventory_module, "cpu_model", lambda: "Test CPU")
    monkeypatch.setattr(
        inventory_module,
        "collect_mining_status",
        lambda _config: {"configured": False, "state": "not_configured"},
    )
    config = AgentConfig(
        server_url="https://controller.example",
        state_file=Path("C:/ProgramData/RigPilotAgentPortable/data/state.json"),
        managed_mining_enabled=managed_mining_enabled,
    )

    host = collect_host_data(collect_gpu=False, mining_config=config)

    assert "wifi_management" not in host["inventory"]
    capabilities = host["inventory"]["_heartbeat_capabilities"]
    assert ("managed_mining.extra_args.v1" in capabilities) is managed_mining_enabled
    if not managed_mining_enabled:
        assert not any(capability.startswith("managed_mining.") for capability in capabilities)
        return
    assert (
        "managed_mining.residue_repair.v1"
        in host["inventory"]["_heartbeat_capabilities"]
    )
    assert (
        "managed_mining.residue_repair.selective.v1"
        in host["inventory"]["_heartbeat_capabilities"]
    )
    assert (
        "managed_mining.residue_repair.explicit_candidate.v1"
        in host["inventory"]["_heartbeat_capabilities"]
    )
    assert (
        "managed_mining.stop_resets_gpu_oc.v1"
        in host["inventory"]["_heartbeat_capabilities"]
    )
    assert (
        "managed_mining.result_binding.v1"
        in host["inventory"]["_heartbeat_capabilities"]
    )
    assert (
        "managed_mining.independent_srb_gpu_oc.v1"
        in host["inventory"]["_heartbeat_capabilities"]
    )
    assert (
        "managed_mining.wsl_gpu.v1"
        in host["inventory"]["_heartbeat_capabilities"]
    )


def test_linux_maintenance_hint_ignores_success_receipt_and_finds_rollback(
    tmp_path: Path, monkeypatch
):
    import agent.agent_updates as updates

    program = tmp_path / "program"
    data = tmp_path / "data"
    staged = data / "agent-updates" / "staged"
    program.mkdir()
    staged.mkdir(parents=True)
    monkeypatch.setattr(updates, "LINUX_PROGRAM_ROOT", program)
    monkeypatch.setattr(updates, "LINUX_DATA_ROOT", data)

    activation_id = "c" * 32
    (program / "pending-update.json").write_text(
        json.dumps({"activation_id": activation_id}), encoding="utf-8"
    )

    succeeded = staged / ("a" * 32)
    succeeded.mkdir()
    (succeeded / "activation-result.json").write_text(
        json.dumps({"status": "succeeded"}), encoding="utf-8"
    )
    hints = inventory_module._linux_heartbeat_maintenance_hints(
        supervision={"configured": True, "ready": True},
    )
    assert hints["pending_agent_update"] is True
    assert hints["rollback_receipt"] is False

    reported = data / "agent-updates" / "reported"
    reported.mkdir()
    (reported / f"{activation_id}.json").write_text("{}", encoding="utf-8")

    rolled_back = staged / ("b" * 32)
    rolled_back.mkdir()
    (rolled_back / "activation-result.json").write_text(
        json.dumps({"status": "rolled_back"}), encoding="utf-8"
    )
    hints = inventory_module._linux_heartbeat_maintenance_hints(
        supervision={"configured": True, "ready": False},
    )
    assert hints == {
        "pending_agent_update": False,
        "rollback_receipt": True,
        "supervision_repair": True,
    }


def test_linux_maintenance_hint_finds_rollback_after_more_than_128_jobs(
    tmp_path: Path, monkeypatch
):
    import agent.agent_updates as updates

    program = tmp_path / "program"
    data = tmp_path / "data"
    staged = data / "agent-updates" / "staged"
    program.mkdir()
    staged.mkdir(parents=True)
    monkeypatch.setattr(updates, "LINUX_PROGRAM_ROOT", program)
    monkeypatch.setattr(updates, "LINUX_DATA_ROOT", data)

    for index in range(140):
        job = staged / f"{index:032x}"
        job.mkdir()
        (job / "activation-result.json").write_text(
            json.dumps({"status": "succeeded"}), encoding="utf-8"
        )
    rollback = staged / ("f" * 32)
    rollback.mkdir()
    (rollback / "activation-result.json").write_text(
        json.dumps({"status": "rolled_back"}), encoding="utf-8"
    )

    hints = inventory_module._linux_heartbeat_maintenance_hints(
        supervision={"configured": True, "ready": True},
    )

    assert hints["rollback_receipt"] is True


def test_linux_maintenance_hint_does_not_report_false_on_pending_io_error(
    tmp_path: Path, monkeypatch
):
    import agent.agent_updates as updates

    program = tmp_path / "program"
    data = tmp_path / "data"
    program.mkdir()
    monkeypatch.setattr(updates, "LINUX_PROGRAM_ROOT", program)
    monkeypatch.setattr(updates, "LINUX_DATA_ROOT", data)
    pending = program / "pending-update.json"
    original_lstat = Path.lstat

    def fail_pending_lstat(path: Path):
        if path == pending:
            raise OSError("simulated USB read failure")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_pending_lstat)
    hints = inventory_module._linux_heartbeat_maintenance_hints(
        supervision={"configured": True, "ready": True},
    )

    assert hints["pending_agent_update"] == "unknown"
    assert hints["pending_agent_update"] is not False


def test_linux_maintenance_hint_does_not_report_false_on_rollback_scan_error(
    tmp_path: Path, monkeypatch
):
    import agent.agent_updates as updates

    program = tmp_path / "program"
    data = tmp_path / "data"
    staged = data / "agent-updates" / "staged"
    program.mkdir()
    staged.mkdir(parents=True)
    monkeypatch.setattr(updates, "LINUX_PROGRAM_ROOT", program)
    monkeypatch.setattr(updates, "LINUX_DATA_ROOT", data)
    original_scandir = inventory_module.os.scandir

    def fail_staged_scan(path):
        if Path(path) == staged:
            raise OSError("simulated USB directory failure")
        return original_scandir(path)

    monkeypatch.setattr(inventory_module.os, "scandir", fail_staged_scan)
    hints = inventory_module._linux_heartbeat_maintenance_hints(
        supervision={"configured": True, "ready": True},
    )

    assert hints["rollback_receipt"] == "unknown"
    assert hints["rollback_receipt"] is not False


def test_linux_maintenance_hint_does_not_trust_malformed_reported_marker(
    tmp_path: Path, monkeypatch
):
    import agent.agent_updates as updates

    program = tmp_path / "program"
    data = tmp_path / "data"
    job = data / "agent-updates" / "staged" / ("d" * 32)
    program.mkdir()
    job.mkdir(parents=True)
    monkeypatch.setattr(updates, "LINUX_PROGRAM_ROOT", program)
    monkeypatch.setattr(updates, "LINUX_DATA_ROOT", data)
    (job / "activation-result.json").write_text(
        json.dumps(
            {
                "status": "rolled_back",
                "command_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            }
        ),
        encoding="utf-8",
    )
    (job / "rollback-reported.json").write_text("{}", encoding="utf-8")

    hints = inventory_module._linux_heartbeat_maintenance_hints(
        supervision={"configured": True, "ready": True},
    )

    assert hints["rollback_receipt"] == "unknown"
    assert hints["rollback_receipt"] is not False


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
@pytest.mark.parametrize("source", ["flight-primary", "flight-secondary", "status", "process"])
def test_retired_history_is_visible_without_process_probe_or_api_request(tmp_path, miner_type, source):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cfg = config(tmp_path, miner_service=None, managed_mining_enabled=True,
                 managed_miner_runtime_dir=runtime, miner_packages_dir=tmp_path / "packages")
    saved = {}
    leg = {
        "miner_type": miner_type, "miner_profile": "default", "coin": "ETC",
        "algorithm": "etchash", "wallet_address": "synthetic-secret-wallet",
        "miner_package": {"id": f"builtin-{miner_type}-1.0-linux-x64",
                          "name": miner_type, "version": "1.0",
                          "installed_path": "must-not-be-reported"},
    }
    if source.startswith("flight-"):
        sheet = {"flight_sheet_id": "saved-history", "name": "Historical flight",
                 "primary": leg if source == "flight-primary" else
                            {"miner_type": "xmrig", "miner_profile": "default"},
                 "secondary": leg if source == "flight-secondary" else None}
        cfg.flight_sheet_file.write_text(json.dumps(sheet))
        saved[cfg.flight_sheet_file] = cfg.flight_sheet_file.read_bytes()
    elif source == "status":
        cfg.miner_status_file.write_text(json.dumps({
            "miner_type": miner_type, "state": "running", "hashrate": 123,
            "hashrate_unit": "H/s", "shares": {"accepted": 12},
        }))
        saved[cfg.miner_status_file] = cfg.miner_status_file.read_bytes()
    else:
        path = runtime / "managed-miner-process.json"
        path.write_text(json.dumps({
            "version": 1, "pid": 123, "executable": str(tmp_path / "retired-miner"),
            "create_time": 1000.25, "miner_type": miner_type,
            "flight_sheet_id": "saved-history", "started_at": "2026-10-07T00:00:00Z",
        }))
        saved[path] = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("retired history must not request API telemetry or inspect/adopt a process")
    status = collect_mining_status(cfg, requester=forbidden, process_factory=forbidden,
                                  process_iter_factory=forbidden, system_name="Linux")
    assert status["retired"] is True
    assert status["miner_type"] == miner_type
    assert status["state"] == "error"
    assert status["process_verified"] is False
    assert status["error"] == "managed miner adapter is retired"
    assert all(status[field] is None for field in (
        "hashrate", "hashrate_unit", "hashrate_details", "shares", "proofs",
        "stats_updated_at", "session_id", "gpu_hashrate_status",
    ))
    if source.startswith("flight-"):
        summary = status[source.removeprefix("flight-")]
        assert summary["retired"] is True and summary["miner_type"] == miner_type
        assert summary["miner_package"]["id"] == leg["miner_package"]["id"]
        assert "synthetic-secret-wallet" not in json.dumps(status)
        assert "installed_path" not in json.dumps(status)
    assert all(path.read_bytes() == before for path, before in saved.items())


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
def test_retired_http_telemetry_is_not_a_request_source(miner_type):
    def forbidden(*args, **kwargs):
        pytest.fail("retired adapter must not reach an HTTP request")
    parsed, error = inventory_module._managed_miner_api_stats(
        {"miner_type": miner_type, "api_host": "127.0.0.1", "api_port": 23456}, forbidden,
    )
    assert parsed is None and error == "managed miner adapter is retired"


@pytest.mark.parametrize("identity_field", ["miner_profile", "miner_package"])
def test_retired_legacy_profile_and_package_do_not_become_supported_miner(identity_field):
    leg = {"miner_profile": "default", "miner_package": None}
    if identity_field == "miner_profile":
        leg["miner_profile"] = "lolminer-etc"
    else:
        leg["miner_package"] = {"id": "builtin-forgeminer-1.5.16-linux-x64",
                                "name": "Historical bundle", "version": "1.5.16"}
    summary = inventory_module._mining_leg_summary(leg)
    assert summary["retired"] is True
    assert summary["miner_type"] == ("lolminer" if identity_field == "miner_profile" else "forgeminer")
    assert summary["miner_package"] == leg["miner_package"]


@pytest.mark.parametrize("display_name", ["lolminer", "forgeminer"])
def test_generic_same_name_history_is_not_retired(tmp_path, monkeypatch, display_name):
    from agent.miner_packages import validate_package_metadata
    from agent.tests.test_miner_packages import metadata, zip_bytes
    cfg = config(tmp_path, miner_service=None,
                 managed_miner_runtime_dir=tmp_path / "runtime", miner_packages_dir=tmp_path / "packages",
                 allow_miner_downloads=True, miner_download_host_allowlist=frozenset({"downloads.example.test"}))
    package = metadata(zip_bytes({"generic-program": b"generic"}), id=display_name, name=display_name)
    validated = validate_package_metadata(package, cfg)
    leg = {"miner_type": "generic", "miner_profile": "default",
           "coin": "PEARL", "algorithm": "pearlhash", "miner_package": validated,
           "wallet_address": "synthetic-private-wallet", "pool_url": "pool.example:3333", "pool_password": "",
           "extra_config": {}}
    cfg.flight_sheet_file.write_text(json.dumps({"flight_sheet_id": "same-name-history", "primary": leg, "secondary": None}))
    cfg.miner_status_file.write_text(json.dumps({"miner_type": "generic", "state": "stopped", "hashrate": 123}))
    before = cfg.flight_sheet_file.read_bytes()
    monkeypatch.setattr(inventory_module, "_managed_process_truth", lambda *_a, **_k: ("stopped", True, None, None))
    def forbidden(*args, **kwargs):
        pytest.fail("same-name historical display must not require HTTP or process inspection")
    status = collect_mining_status(cfg, system_name="Linux", requester=forbidden, process_factory=forbidden)
    assert "retired" not in status and "retired" not in status["primary"]
    assert status["error"] is None
    assert status["primary"]["miner_package"] == {key: validated[key] for key in ("id", "name", "version")}
    assert cfg.flight_sheet_file.read_bytes() == before
    assert status["state"] == "unknown" and status["hashrate"] == 123
    assert "synthetic-private-wallet" not in json.dumps(status)


@pytest.mark.parametrize("adapter_id", ["org.lolliedieb.lolminer", "org.hashraptor.forgeminer"])
def test_structured_retired_adapter_id_is_still_retired(adapter_id):
    summary = inventory_module._mining_leg_summary({"miner_profile": "default", "adapter_id": adapter_id})
    assert summary["retired"] is True
