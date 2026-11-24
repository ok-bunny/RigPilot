"""Inventory precedence and full-selection handoff to the bounded collector."""
import subprocess

import pytest

from agent import nvidia_memory
from agent.inventory import nvidia_gpu_metrics


UUIDS = ["GPU-11111111-2222-3333-4444-555555555555",
         "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]


def runner(argv, **kwargs):
    if not argv[1].startswith("--query-gpu=index,"):
        return subprocess.CompletedProcess(argv, 2, "", "optional field unavailable")
    return subprocess.CompletedProcess(argv, 0, "".join(
        f"{1-index}, NVIDIA Fixture GPU, {uuid}, 00000000:0{index+1}:00.0, "
        "70, 8192, 512, 48, 125, 0xAA201458, N/A, FE.ED.00.00.03, "
        "999.01, 160, 180, 100, 220\n" for index, uuid in enumerate(UUIDS)), "")


@pytest.mark.parametrize("system,source", [("Linux", "nvml_private"), ("Windows", "nvapi")])
@pytest.mark.parametrize("kind", ["complete", "partial", "timeout", "unknown"])
def test_inventory_driver_data_precedes_old_material_and_failure_is_explicit(monkeypatch, system, source, kind):
    captured = []
    def collect(selected, actual_system):
        captured.append((selected, actual_system))
        details = {}
        for row in selected:
            details[row["pci_bus_id"]] = {
                "uuid": row["uuid"], "pci_bus_id": row["pci_bus_id"],
                "memory_vendor": "Micron" if kind in {"complete", "partial"} else None,
                "memory_type": "GDDR7" if kind == "complete" else None,
                "memory_identity_unavailable_reason": "native_probe_timeout" if kind == "timeout" else "driver_memory_identity_unknown",
            }
        return details
    monkeypatch.setattr(nvidia_memory, "collect_nvidia_memory", collect)
    result = nvidia_gpu_metrics("nvidia-smi", runner=runner, system_name=system)
    selection, actual_system = captured[0]
    assert actual_system == system and [r["uuid"] for r in selection] == UUIDS
    assert all(r["driver_version"] == "999.01" and r["vbios_version"] == "FE.ED.00.00.03" for r in selection)
    assert len(result) == 2
    for gpu in result:
        assert gpu["memory_identity_source"] == source
        assert gpu["memory_identity_status"] == (kind if kind in {"complete", "partial"} else "unavailable")
        assert gpu["memory_vendor"] == ("Micron" if kind in {"complete", "partial"} else None)
        assert gpu["memory_type"] == ("GDDR7" if kind == "complete" else None)
        assert gpu["board_vendor"] == "Gigabyte" and gpu["power_w"] == 125
        if kind == "timeout":
            assert gpu["memory_identity_unavailable_reason"] == "native_probe_timeout"


def test_memory_collector_failure_preserves_gpu_telemetry_and_does_not_fill_legacy(monkeypatch):
    def fail(*_args):
        raise RuntimeError("optional child boundary failed")
    monkeypatch.setattr(nvidia_memory, "collect_nvidia_memory", fail)
    result = nvidia_gpu_metrics("nvidia-smi", runner=runner, system_name="Linux")
    assert [gpu["uuid"] for gpu in result] == UUIDS
    assert all(gpu["memory_vendor"] is None and gpu["memory_type"] is None for gpu in result)
    assert all(gpu["utilization_percent"] == 70 and gpu["memory_identity_source"] == "nvml_private" for gpu in result)
