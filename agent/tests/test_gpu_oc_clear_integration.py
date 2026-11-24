"""Existing OC wire contract -> actual executor -> NVML setters, no devices.

Controller resolves explicit nulls to a complete replace_one target; an empty
resolved target uses reset_one with the previously owned field template.
"""
from __future__ import annotations

import copy
import json
from itertools import count
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.commands import Command, CommandExecutor
from agent.gpu_oc import NvmlGpuOcBackend
from agent.tests.test_gpu_oc_backend import FakeFunction, FakeNvmlWithUuid
from agent.tests.test_mining_commands import config


FIELDS = {
    "power_limit_w": 125,
    "core_clock_offset_mhz": 100,
    "memory_clock_offset_mhz": 800,
    "core_clock_lock_mhz": 2700,
    "memory_clock_lock_mhz": 8100,
    "fan_percent": 65,
}
DEFAULTS = {
    "power_limit_w": 150,
    "core_clock_offset_mhz": 0,
    "memory_clock_offset_mhz": 0,
    "core_clock_lock_mhz": None,
    "memory_clock_lock_mhz": None,
    "fan_percent": None,
}


class RecordingNvml(FakeNvmlWithUuid):
    """Vendor fake models board state; production reset/apply remain real."""

    def __init__(self):
        self.hardware = {index: dict(DEFAULTS) for index in range(3)}
        self.writes = []
        super().__init__()
        self.nvmlDeviceGetFanSpeed_v2 = FakeFunction(self._fan_speed)
        self.nvmlDeviceGetMinMaxFanSpeed = FakeFunction(self._fan_limits)

    def _record(self, handle, field, value):
        index = handle.value - 1000
        self.writes.append((index, field, value))
        self.hardware[index][field] = value

    def _set_power(self, handle, value):
        self._record(handle, "power_limit_w", value.value / 1000)
        return super()._set_power(handle, value)

    def _set_core_offset(self, handle, value):
        self._record(handle, "core_clock_offset_mhz", value.value)
        return super()._set_core_offset(handle, value)

    def _set_memory_offset(self, handle, value):
        self._record(handle, "memory_clock_offset_mhz", value.value)
        return super()._set_memory_offset(handle, value)

    def _set_core_lock(self, handle, minimum, maximum):
        assert minimum.value == maximum.value
        self._record(handle, "core_clock_lock_mhz", minimum.value)
        return super()._set_core_lock(handle, minimum, maximum)

    def _set_memory_lock(self, handle, minimum, maximum):
        assert minimum.value == maximum.value
        self._record(handle, "memory_clock_lock_mhz", minimum.value)
        return super()._set_memory_lock(handle, minimum, maximum)

    def _reset_core_lock(self, handle):
        self._record(handle, "core_clock_lock_mhz", None)
        return super()._reset_core_lock(handle)

    def _reset_memory_lock(self, handle):
        self._record(handle, "memory_clock_lock_mhz", None)
        return super()._reset_memory_lock(handle)

    def _fan_speed(self, handle, _fan, speed):
        speed._obj.value = self.hardware[handle.value - 1000]["fan_percent"] or 47
        return 0

    def _set_fan(self, handle, fan, value):
        self._record(handle, "fan_percent", value.value)
        return super()._set_fan(handle, fan, value)

    def _reset_fan(self, handle, fan):
        self._record(handle, "fan_percent", None)
        return super()._reset_fan(handle, fan)


@pytest.fixture
def rig(tmp_path):
    cfg = config(tmp_path)
    library = RecordingNvml()

    def external_operation_forbidden(*_args, **_kwargs):
        pytest.fail("this test must not invoke a process, package, or miner")

    def executor():
        return CommandExecutor(
            cfg,
            runner=external_operation_forbidden,
            gpu_backend_factory=lambda: NvmlGpuOcBackend(library),
            package_installer=external_operation_forbidden,
            managed_miner_factory=external_operation_forbidden,
        )

    def state():
        path = cfg.flight_sheet_file.parent / "rigpilot-gpu-oc-state.json"
        raw = json.loads(path.read_text())
        assert raw["version"] == 2
        return {row["gpu_uuid"]: row["setting"] for row in raw["devices"]}

    return SimpleNamespace(library=library, executor=executor, state=state, cfg=cfg, command_ids=count())


def payload(index, fields, mode="replace_one"):
    result = {
        "gpu_profile_id": f"device-oc-GPU-stable-{index}",
        "name": f"GPU {index}",
        "vendor": "nvidia",
        "settings": [{"index": index, **fields}],
        "mode": mode,
        "expected_gpu_uuid": f"GPU-stable-{index}",
    }
    if mode in {"replace_one", "reset_one"}:
        result["previous_index"] = index
    return result


def execute(rig, index, fields, mode="replace_one"):
    # A fresh executor each time ensures the real persisted state is reloaded.
    outcome = rig.executor().execute(Command(f"oc-test-{next(rig.command_ids)}", "apply_gpu_profile", payload(index, fields, mode)))
    assert outcome.status == "succeeded", outcome.error
    return outcome


@pytest.mark.parametrize("removed", list(FIELDS))
@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_replace_restores_one_field_default_and_preserves_other_controls_and_gpu(
    rig, removed, monkeypatch, system_name
):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: system_name)
    execute(rig, 0, FIELDS)
    other = {"power_limit_w": 175, "core_clock_offset_mhz": -50}
    execute(rig, 1, other)
    other_hardware = dict(rig.library.hardware[1])
    rig.library.writes.clear()

    target = {field: value for field, value in FIELDS.items() if field != removed}
    execute(rig, 0, target)
    assert (0, removed, DEFAULTS[removed]) in rig.library.writes
    assert [write for write in rig.library.writes if write[1] == removed] == [(0, removed, DEFAULTS[removed])]
    assert all(index == 0 for index, _, _ in rig.library.writes)
    assert rig.library.hardware[0] == {**FIELDS, removed: DEFAULTS[removed]}
    assert rig.library.hardware[1] == other_hardware
    assert rig.state() == {"GPU-stable-0": target, "GPU-stable-1": other}
    assert rig.library.uuid_queries and set(rig.library.uuid_queries) == {0, 1}

    # Reapplying the persisted desired target after executor reconstruction must
    # neither call the removed setter nor resurrect its previous nondefault.
    rig.library.writes.clear()
    execute(rig, 0, rig.state()["GPU-stable-0"])
    assert all(field != removed for _, field, _ in rig.library.writes)
    assert rig.library.hardware[0] == {**FIELDS, removed: DEFAULTS[removed]}
    assert rig.state() == {"GPU-stable-0": target, "GPU-stable-1": other}


@pytest.mark.parametrize("field", ["core_clock_offset_mhz", "memory_clock_offset_mhz"])
def test_numeric_zero_remains_owned_value_and_is_reapplied(rig, field):
    execute(rig, 0, FIELDS)
    target = {**FIELDS, field: 0}
    execute(rig, 0, target)
    assert field in rig.state()["GPU-stable-0"]
    assert rig.state()["GPU-stable-0"][field] == 0
    rig.library.writes.clear()
    execute(rig, 0, rig.state()["GPU-stable-0"])
    assert rig.library.writes.count((0, field, 0)) == 2  # owned reset + explicit apply
    assert rig.library.hardware[0] == target


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_reset_one_restores_all_owned_defaults_and_does_not_resurrect_after_rebuild(
    rig, monkeypatch, system_name
):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: system_name)
    execute(rig, 0, FIELDS)
    other = {"power_limit_w": 175, "core_clock_offset_mhz": -50}
    execute(rig, 1, other)
    other_hardware = dict(rig.library.hardware[1])
    rig.library.writes.clear()
    execute(rig, 0, FIELDS, "reset_one")
    assert rig.library.hardware[0] == DEFAULTS
    assert rig.library.hardware[1] == other_hardware
    assert all(index == 0 for index, _, _ in rig.library.writes)
    assert {field for _, field, _ in rig.library.writes} == set(FIELDS)
    assert rig.state() == {"GPU-stable-1": other}
    # Reconstruct and apply remaining desired records only; cleared GPU stays default.
    rig.library.writes.clear()
    execute(rig, 1, rig.state()["GPU-stable-1"])
    assert all(index == 1 for index, _, _ in rig.library.writes)
    assert rig.library.hardware[0] == DEFAULTS
    assert rig.state() == {"GPU-stable-1": other}


def test_omitted_merge_fields_remain_unchanged(rig):
    execute(rig, 0, FIELDS)
    execute(rig, 0, {"power_limit_w": 175}, "merge_one")
    expected = {**FIELDS, "power_limit_w": 175}
    assert rig.library.hardware[0] == expected
    assert rig.state() == {"GPU-stable-0": expected}


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_uuid_mismatch_rejects_clear_before_any_vendor_write(rig, monkeypatch, system_name):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: system_name)
    execute(rig, 0, FIELDS)
    before = rig.state()
    rig.library.writes.clear()
    wrong = payload(0, FIELDS, "reset_one")
    wrong["expected_gpu_uuid"] = "GPU-stable-1"
    outcome = rig.executor().execute(Command("wrong-card", "apply_gpu_profile", wrong))
    assert outcome.status == "failed"
    assert "identity changed" in outcome.error
    assert rig.library.writes == []
    assert rig.library.hardware[0] == FIELDS
    assert rig.state() == before


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_persistence_failure_reports_hardware_without_replaying_old_values(rig, monkeypatch, system_name):
    monkeypatch.setattr("agent.gpu_oc.platform.system", lambda: system_name)
    execute(rig, 0, FIELDS)
    before = copy.deepcopy(rig.state())
    rig.library.writes.clear()

    def cannot_save(*_args, **_kwargs):
        raise OSError("fake state write failure")

    monkeypatch.setattr("agent.commands._write_gpu_oc_state", cannot_save)
    target = {key: value for key, value in FIELDS.items() if key != "core_clock_lock_mhz"}
    outcome = rig.executor().execute(Command("state-failure", "apply_gpu_profile", payload(0, target)))
    assert outcome.status == "failed"
    assert "fake state write failure" in outcome.error
    assert rig.library.hardware[0] == {**FIELDS, "core_clock_lock_mhz": None}
    assert [row for row in rig.library.writes if row[1] == "core_clock_lock_mhz"] == [(0, "core_clock_lock_mhz", None)]
    assert rig.state() == before
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["persistence_failed"] is True
    assert recovery["hardware_state"] == "requested_settings_applied"
    assert recovery["write_attempted"] is True
    assert recovery["state_reconciled"] is False and recovery["recovered"] is False



def test_vendor_failure_while_clearing_rolls_back_hardware_and_preserves_state(rig):
    execute(rig, 0, FIELDS)
    before = copy.deepcopy(rig.state())
    setter = rig.library.nvmlDeviceSetGpcClkVfOffset
    rig.library.nvmlDeviceSetGpcClkVfOffset = FakeFunction(
        lambda handle, value: 4 if value.value == 110 else setter(handle, value)
    )
    target = {key: value for key, value in FIELDS.items() if key != "core_clock_lock_mhz"}
    target["core_clock_offset_mhz"] = 110
    outcome = rig.executor().execute(Command("vendor-failure", "apply_gpu_profile", payload(0, target)))
    assert outcome.status == "failed"
    assert rig.library.hardware[0] == FIELDS
    assert rig.state() == before


def test_explicit_api_null_must_be_resolved_before_legacy_agent_wire(rig):
    execute(rig, 0, FIELDS)
    before = copy.deepcopy(rig.state())
    rig.library.writes.clear()
    outcome = rig.executor().execute(Command("raw-null", "apply_gpu_profile", payload(0, {**FIELDS, "core_clock_offset_mhz": None})))
    assert outcome.status == "failed" and outcome.result.get("rejected") is True
    assert rig.library.writes == [] and rig.state() == before


def test_amd_clear_is_rejected_before_constructing_an_nvml_backend(rig):
    cfg = replace(rig.cfg, gpu_oc_allowed_vendors=frozenset({"nvidia", "amd"}))
    def forbidden():pytest.fail("unsupported AMD OC must not reach NVIDIA hardware")
    executor = CommandExecutor(cfg, gpu_backend_factory=forbidden,)
    request = payload(0, FIELDS, "reset_one");request["vendor"] = "amd"
    outcome = executor.execute(Command("amd-clear", "apply_gpu_profile", request))
    assert outcome.status == "failed" and outcome.result.get("rejected") is True
    assert "NVIDIA OC only" in outcome.error
    assert rig.library.writes == []


@pytest.mark.parametrize("power,hardware_min,hardware_max", [(360, 50, 450), (15, 5, 25)])
def test_default_power_policy_defers_to_mock_vendor_range(rig, power, hardware_min, hardware_max):
    assert rig.cfg.gpu_power_limit_min_w is None and rig.cfg.gpu_power_limit_max_w is None

    def constraints(_handle, minimum, maximum):
        minimum._obj.value = hardware_min * 1000
        maximum._obj.value = hardware_max * 1000
        return 0

    rig.library.nvmlDeviceGetPowerManagementLimitConstraints = FakeFunction(constraints)
    outcome = rig.executor().execute(Command("power-hardware", "apply_gpu_profile", payload(0, {"power_limit_w": power})))
    assert outcome.status == "succeeded", outcome.error
    assert rig.library.hardware[0]["power_limit_w"] == power
    assert rig.state() == {"GPU-stable-0": {"power_limit_w": float(power)}}
    assert all(index == 0 for index, *_rest in rig.library.writes)


def test_explicit_power_policy_still_rejects_360_before_vendor_write(rig):
    executor = rig.executor()
    executor.config = replace(rig.cfg, gpu_power_limit_min_w=50, gpu_power_limit_max_w=300)
    outcome = executor.execute(Command("power-local-policy", "apply_gpu_profile", payload(0, {"power_limit_w": 360})))
    assert outcome.status == "failed" and outcome.result.get("rejected") is True
    assert "outside the local range 50-300" in outcome.error
    assert rig.library.writes == []


def test_power_outside_actual_mock_hardware_range_still_fails(rig):
    outcome = rig.executor().execute(Command("power-hardware-reject", "apply_gpu_profile", payload(0, {"power_limit_w": 360})))
    assert outcome.status == "failed"
    assert "outside hardware range" in outcome.error
    assert rig.library.writes == []


@pytest.mark.parametrize("power", [True, False, 0, -1, float("nan"), float("inf"), float("-inf"), None])
def test_power_request_still_rejects_nonpositive_nonfinite_or_boolean(rig, power):
    outcome = rig.executor().execute(Command("power-invalid", "apply_gpu_profile", payload(0, {"power_limit_w": power})))
    assert outcome.status == "failed" and outcome.result.get("rejected") is True
    assert rig.library.writes == []


def test_power_default_change_does_not_relax_gpu_uuid_binding(rig):
    wrong = payload(0, {"power_limit_w": 360})
    wrong["expected_gpu_uuid"] = "GPU-stable-1"
    outcome = rig.executor().execute(Command("power-wrong-uuid", "apply_gpu_profile", wrong))
    assert outcome.status == "failed" and "identity changed" in outcome.error
    assert rig.library.writes == []


def test_power_default_change_does_not_relax_other_oc_fields(rig):
    outcome = rig.executor().execute(Command("core-policy-unchanged", "apply_gpu_profile", payload(0, {"core_clock_offset_mhz": 501})))
    assert outcome.status == "failed" and outcome.result.get("rejected") is True
    assert "outside the local range -500-500" in outcome.error
    assert rig.library.writes == []


@pytest.mark.parametrize("state_version", [1, 2])
@pytest.mark.parametrize("power,hardware_min,hardware_max", [(15, 5, 25), (1200, 50, 1500)])
def test_hardware_valid_power_survives_owned_state_reload_and_next_command(
    rig, state_version, power, hardware_min, hardware_max,
):
    def constraints(_handle, minimum, maximum):
        minimum._obj.value = hardware_min * 1000
        maximum._obj.value = hardware_max * 1000
        return 0
    rig.library.nvmlDeviceGetPowerManagementLimitConstraints = FakeFunction(constraints)
    execute(rig, 0, {"power_limit_w": power})
    if state_version == 1:
        path = rig.cfg.flight_sheet_file.parent / "rigpilot-gpu-oc-state.json"
        path.write_text(json.dumps({"version": 1, "settings": [{"index": 0, "power_limit_w": power}]}))
    rig.library.writes.clear()
    execute(rig, 0, {"power_limit_w": power, "core_clock_offset_mhz": 20})
    assert rig.state() == {"GPU-stable-0": {"power_limit_w": float(power), "core_clock_offset_mhz": 20}}
    assert rig.library.hardware[0]["power_limit_w"] == power
    assert all(index == 0 for index, *_rest in rig.library.writes)


@pytest.mark.parametrize("state_version", [1, 2])
@pytest.mark.parametrize("power,hardware_min,hardware_max,factory_power", [(15, 5, 25, 20), (1200, 50, 1500, 150)])
def test_hardware_valid_power_state_can_be_cleared_by_fresh_executor(
    rig, state_version, power, hardware_min, hardware_max, factory_power,
):
    def constraints(_handle, minimum, maximum):
        minimum._obj.value = hardware_min * 1000
        maximum._obj.value = hardware_max * 1000
        return 0
    def default_power(_handle, value):
        value._obj.value = factory_power * 1000
        return 0
    rig.library.nvmlDeviceGetPowerManagementLimitConstraints = FakeFunction(constraints)
    rig.library.nvmlDeviceGetPowerManagementDefaultLimit = FakeFunction(default_power)
    execute(rig, 0, {"power_limit_w": power})
    if state_version == 1:
        path = rig.cfg.flight_sheet_file.parent / "rigpilot-gpu-oc-state.json"
        path.write_text(json.dumps({"version": 1, "settings": [{"index": 0, "power_limit_w": power}]}))
    rig.library.writes.clear()
    execute(rig, 0, {"power_limit_w": power}, mode="reset_one")
    assert rig.state() == {}
    assert rig.library.hardware[0]["power_limit_w"] == factory_power
    assert all(index == 0 for index, *_rest in rig.library.writes)
