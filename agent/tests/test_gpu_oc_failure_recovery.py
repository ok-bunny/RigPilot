"""GPU recovery receipts cross the real command/state boundary without devices."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from agent import commands
from agent.commands import Command, CommandExecutor
from agent.config import AgentConfig
from agent.gpu_oc import GpuOcError, NvmlGpuOcBackend, gpu_oc_state_path
from agent.runtime import AgentRuntime
from agent.state import AgentState, load_state


TARGET = "GPU-recovery-target"
OTHER = "GPU-recovery-other"
MISSING = "GPU-recovery-missing"
CORE = "core_clock_offset_mhz"


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):

    def forbidden(*_args, **_kwargs):
        pytest.fail("GPU recovery tests cannot launch processes or use the network")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr(NvmlGpuOcBackend, "_load_library", staticmethod(forbidden))


def config(tmp_path):
    return AgentConfig(
        server_url="https://controller.invalid", state_file=tmp_path / "identity.json",
        flight_sheet_file=tmp_path / "miner" / "flight.json", allow_gpu_oc=True,
    )


def save_owned(cfg, devices):
    commands._write_gpu_oc_state(cfg, list(devices.values()), devices)


def owned(cfg):
    return commands._load_gpu_oc_state_record(cfg)[1]


def request(*, index=1, mode="replace_one", fields=None):
    return {
        "gpu_profile_id": "device-oc-recovery", "name": "Recovery target",
        "vendor": "nvidia", "mode": mode, "expected_gpu_uuid": TARGET,
        "previous_index": 0,
        "settings": [{"index": index, **(fields or {CORE: 500, "power_limit_w": 180})}],
    }


def failure(*, reset=(CORE,), status="partial", hardware=True, uuid=TARGET):
    return GpuOcError(
        "set GPU 1 core clock offset failed: GPU requires reset",
        nvml_codes=(16,) if hardware else (4,),
        write_attempted=True,
        failure_target={"index": 1, "_expected_gpu_uuid": TARGET, "field": CORE, "operation": "apply"},
        recovery={
            "status": status,
            "confirmed_reset": ([{"index": 1, "_expected_gpu_uuid": uuid, "reset": list(reset)}] if reset else []),
            "hardware_reset_required": hardware, "errors": [],
        },
    )


class Backend:
    supports_uuid_bound_transitions = True

    def __init__(self, error):
        self.error = error
        self.calls = []
        self.identities = {TARGET: 1, OTHER: 2}

    def verify_device_uuid(self, index, uuid):
        assert self.identities.get(uuid) == index
        return uuid

    def enumerate_device_indices(self):
        return self.identities

    def transition(self, previous, current):
        self.calls.append(copy.deepcopy((previous, current)))
        raise self.error


def executor(cfg, backend, *, manager=None):
    def forbidden(*_args, **_kwargs):
        pytest.fail("GPU profile recovery must not invoke a miner or package")

    return CommandExecutor(
        cfg, runner=forbidden, package_installer=forbidden,
        gpu_backend_factory=lambda: backend,
        managed_miner_factory=(lambda _cfg: manager) if manager is not None else forbidden,
    )


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_confirmed_default_clears_only_owned_field_for_bound_uuid(
    tmp_path, monkeypatch, system_name,
):
    monkeypatch.setattr("agent.commands.platform.system", lambda: system_name)
    cfg = config(tmp_path)
    previous = {
        TARGET: {"index": 0, CORE: 300, "power_limit_w": 160, "fan_percent": 65},
        OTHER: {"index": 2, CORE: 100}, MISSING: {"index": 3, CORE: 250},
    }
    save_owned(cfg, previous)
    backend = Backend(failure())

    outcome = executor(cfg, backend).execute(Command("partial", "apply_gpu_profile", request()))

    assert outcome.status == "failed" and "GPU requires reset" in outcome.error
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["hardware_reset_required"] is True
    assert recovery["state_reconciled"] is True
    assert recovery["confirmed_reset"] == [{"index": 1, "gpu_uuid": TARGET, "reset": [CORE]}]
    assert owned(cfg) == {
        **previous, TARGET: {"index": 0, "power_limit_w": 160.0, "fan_percent": 65},
    }
    assert len(backend.calls) == 1
    assert backend.calls[0][0][0]["_expected_gpu_uuid"] == TARGET
    assert backend.calls[0][0][0]["index"] == 1

    # A newly constructed executor must read the reconciled record, not
    # resurrect the cleared value through the old index hint.
    second = Backend(failure(reset=(), status="rolled_back", hardware=False))
    executor(cfg, second).execute(Command("rebuilt", "apply_gpu_profile", request(fields={"power_limit_w": 170})))
    assert CORE not in second.calls[0][0][0]


def test_rolled_back_failure_preserves_old_state_and_never_saves_request(tmp_path):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}})
    before = gpu_oc_state_path(cfg).read_bytes()
    backend = Backend(failure(reset=(), status="rolled_back", hardware=False))

    outcome = executor(cfg, backend).execute(Command("restored", "apply_gpu_profile", request()))

    assert outcome.status == "failed"
    assert outcome.result["gpu_oc_recovery"]["status"] == "rolled_back"
    assert gpu_oc_state_path(cfg).read_bytes() == before
    assert len(backend.calls) == 1


def test_confirmed_last_owned_field_removes_only_that_device(tmp_path):
    cfg = config(tmp_path)
    remaining = {OTHER: {"index": 2, CORE: 100}, MISSING: {"index": 3, CORE: 200}}
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}, **remaining})

    outcome = executor(cfg, Backend(failure())).execute(Command("default", "apply_gpu_profile", request()))

    assert outcome.status == "failed"
    assert owned(cfg) == remaining


@pytest.mark.parametrize("bad_reset,bad_uuid", [
    ((CORE,), OTHER), (("memory_clock_offset_mhz",), TARGET), (("not_a_gpu_field",), TARGET),
])
def test_unbound_or_unattempted_reset_claim_never_removes_state(tmp_path, bad_reset, bad_uuid):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}})
    before = gpu_oc_state_path(cfg).read_bytes()
    backend = Backend(failure(reset=bad_reset, uuid=bad_uuid))

    outcome = executor(cfg, backend).execute(Command("invalid-proof", "apply_gpu_profile", request()))

    assert outcome.status == "failed"
    assert outcome.result["gpu_oc_recovery"]["state_reconciled"] is False
    assert gpu_oc_state_path(cfg).read_bytes() == before
    assert len(backend.calls) == 1


def test_recovery_state_write_failure_never_replays_hardware(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}, OTHER: {"index": 2, CORE: 100}})
    before = gpu_oc_state_path(cfg).read_bytes()
    backend = Backend(failure())

    def cannot_save(*_args, **_kwargs):
        raise OSError("simulated recovery state write failure")

    monkeypatch.setattr(commands, "_write_gpu_oc_state", cannot_save)
    outcome = executor(cfg, backend).execute(Command("state-failure", "apply_gpu_profile", request()))

    assert outcome.status == "failed"
    assert outcome.result["gpu_oc_recovery"]["state_reconciled"] is False
    assert "GPU OC recovery state could not be persisted" in outcome.result["gpu_oc_recovery"]["errors"]
    assert gpu_oc_state_path(cfg).read_bytes() == before
    assert len(backend.calls) == 1


@pytest.mark.parametrize("stop", [False, True])
def test_successful_hardware_then_persistence_failure_has_no_inverse_write(tmp_path, monkeypatch, stop):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}})
    before = gpu_oc_state_path(cfg).read_bytes()

    class SuccessfulBackend(Backend):
        def transition(self, previous, current):
            self.calls.append(copy.deepcopy((previous, current)))
            return {
                "vendor": "nvidia", "write_attempted": True,
                "reset": [{"index": row["index"], "reset": list(set(row) & commands._GPU_OC_RECOVERY_FIELDS)} for row in previous],
                "operations": [{"index": row["index"], "applied": list(set(row) & commands._GPU_OC_RECOVERY_FIELDS)} for row in current],
            }

    backend = SuccessfulBackend(None)
    manager = SimpleNamespace(stop=lambda: {"stopped": True})
    monkeypatch.setattr(commands, "_active_flight_uses_srb_gpu_oc", lambda _cfg: False)

    def cannot_save(*_args, **_kwargs):
        raise OSError("simulated completed-transition state write failure")

    monkeypatch.setattr(commands, "_write_gpu_oc_state", cannot_save)
    command = Command("completed", "stop_mining", {}) if stop else Command("completed", "apply_gpu_profile", request())
    outcome = executor(cfg, backend, manager=manager).execute(command)

    assert outcome.status == "failed"
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["persistence_failed"] is True and recovery["recovered"] is False
    assert recovery["state_reconciled"] is False and recovery["write_attempted"] is True
    assert recovery["hardware_state"] == ("defaults_restored" if stop else "requested_settings_applied")
    assert recovery["hardware_reset_required"] is False and recovery["reset_required"] is False
    assert gpu_oc_state_path(cfg).read_bytes() == before
    assert len(backend.calls) == 1


def test_receipt_fingerprint_is_hardware_intent_and_uses_actual_failed_target(tmp_path):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}})
    first = request(fields={CORE: 500, "power_limit_w": 180})
    second = {**first, "name": "Changed UI name", "gpu_profile_id": "device-oc-different-id", "previous_index": 31}
    second["settings"] = [{"power_limit_w": 180.0, CORE: 500, "index": 1}]

    a = executor(cfg, Backend(failure())).execute(Command("fingerprint-a", "apply_gpu_profile", first))
    b = executor(cfg, Backend(failure())).execute(Command("fingerprint-b", "apply_gpu_profile", second))

    first_receipt = a.result["gpu_oc_recovery"]
    second_receipt = b.result["gpu_oc_recovery"]
    intent = {
        "vendor": "nvidia", "mode": "replace_one", "expected_gpu_uuid": TARGET,
        "settings": [{"index": 1, CORE: 500, "power_limit_w": 180}],
    }
    expected = hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert first_receipt["request_fingerprint"] == second_receipt["request_fingerprint"] == expected
    assert first_receipt["command_id"] == "fingerprint-a"
    assert first_receipt["write_attempted"] is True
    assert first_receipt["failure_target"] == {"index": 1, "operation": "apply", "requested_gpu_uuid": TARGET, "identity_verified": False, "field": CORE}
    assert first_receipt["targets"][0]["requested_gpu_uuid"] == TARGET
    assert first_receipt["targets"][0]["identity_verified"] is False
    assert len(first_receipt["targets"][0]["setting_fingerprint"]) == 64


def test_initial_failed_request_never_creates_desired_state(tmp_path):
    cfg = config(tmp_path)
    backend = Backend(failure(status="defaults_restored"))

    outcome = executor(cfg, backend).execute(Command("initial", "apply_gpu_profile", request()))

    assert outcome.status == "failed"
    assert not gpu_oc_state_path(cfg).exists()
    assert outcome.result["gpu_oc_recovery"]["state_reconciled"] is True
    assert len(backend.calls) == 1


def test_reset_required_query_without_mutation_keeps_pending_owned_state(tmp_path):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}})
    before = gpu_oc_state_path(cfg).read_bytes()
    backend = Backend(GpuOcError("query limits: GPU requires reset", nvml_codes=(16,)))

    outcome = executor(cfg, backend).execute(Command("query-failed", "apply_gpu_profile", request()))

    assert outcome.status == "failed"
    assert outcome.result["gpu_oc_recovery"]["hardware_reset_required"] is True
    assert outcome.result["gpu_oc_recovery"]["confirmed_reset"] == []
    assert gpu_oc_state_path(cfg).read_bytes() == before


@pytest.mark.parametrize("code", [16, 15, 4])
def test_early_uuid_failure_is_unverified_read_only_receipt(tmp_path, monkeypatch, code):
    from agent.tests.test_gpu_oc_backend import FakeFunction, FakeNvmlWithUuid

    cfg = config(tmp_path)
    save_owned(cfg, {"GPU-stable-0": {"index": 0, CORE: 300}, OTHER: {"index": 2, CORE: 100}})
    before = gpu_oc_state_path(cfg).read_bytes()
    library = FakeNvmlWithUuid()
    library.nvmlDeviceGetUUID = FakeFunction(lambda *_args: code)
    document = request(index=0, fields={CORE: 100})
    document["expected_gpu_uuid"] = "GPU-stable-0"

    def no_state_write(*_args, **_kwargs):
        pytest.fail("identity failure must never update GPU ownership state")

    monkeypatch.setattr(commands, "_write_gpu_oc_state", no_state_write)
    outcome = executor(cfg, NvmlGpuOcBackend(library)).execute(Command("early-query", "apply_gpu_profile", document))

    assert outcome.status == "failed" and library.calls == []
    assert gpu_oc_state_path(cfg).read_bytes() == before
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["nvml_codes"] == [code]
    assert recovery["hardware_fault"] is (code in {15, 16})
    assert recovery["reset_required"] is (code == 16)
    assert recovery["hardware_reset_required"] is (code == 16)
    assert recovery["write_attempted"] is False and recovery["recovered"] is False
    assert recovery["confirmed_reset"] == []
    assert recovery["phase"] == "identity_verification" and recovery["identity_verified"] is False
    assert recovery["state_unchanged"] is True and recovery["state_reconciled"] is True
    assert recovery["failure_target"] == {
        "index": 0, "operation": "query", "phase": "identity_verification",
        "requested_gpu_uuid": "GPU-stable-0", "identity_verified": False,
    }
    assert recovery["targets"][0]["requested_gpu_uuid"] == "GPU-stable-0"
    assert recovery["targets"][0]["identity_verified"] is False
    assert "expected_gpu_uuid" not in recovery["targets"][0]


def test_identity_mismatch_claim_never_becomes_hardware_fault_or_proof(tmp_path):
    from agent.tests.test_gpu_oc_backend import FakeNvmlWithUuid

    cfg = config(tmp_path)
    document = request(index=0, fields={CORE: 100})
    document["expected_gpu_uuid"] = "GPU-another-claim"
    library = FakeNvmlWithUuid()

    outcome = executor(cfg, NvmlGpuOcBackend(library)).execute(Command("mismatch", "apply_gpu_profile", document))

    assert outcome.status == "failed" and library.calls == []
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["nvml_codes"] == [] and recovery["hardware_fault"] is False
    assert recovery["hardware_reset_required"] is False and recovery["reset_required"] is False
    assert recovery["targets"][0]["requested_gpu_uuid"] == "GPU-another-claim"
    assert recovery["targets"][0]["identity_verified"] is False
    assert recovery["confirmed_reset"] == [] and not gpu_oc_state_path(cfg).exists()


@pytest.mark.parametrize("code", [15, 16])
def test_early_and_late_query_faults_have_same_intent_fingerprints(tmp_path, code):
    from agent.tests.test_gpu_oc_backend import FakeFunction, FakeNvmlWithUuid

    cfg = config(tmp_path)
    document = request(index=0, fields={CORE: 100})
    document["expected_gpu_uuid"] = "GPU-stable-0"
    early = FakeNvmlWithUuid()
    early.nvmlDeviceGetUUID = FakeFunction(lambda *_args: code)
    late = FakeNvmlWithUuid()
    late.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(lambda *_args: code)

    a = executor(cfg, NvmlGpuOcBackend(early)).execute(Command("early", "apply_gpu_profile", document))
    b = executor(cfg, NvmlGpuOcBackend(late)).execute(Command("late", "apply_gpu_profile", document))

    first, second = a.result["gpu_oc_recovery"], b.result["gpu_oc_recovery"]
    assert first["request_fingerprint"] == second["request_fingerprint"]
    assert first["targets"][0]["setting_fingerprint"] == second["targets"][0]["setting_fingerprint"]
    assert first["targets"][0]["identity_verified"] is False
    assert second["targets"][0]["identity_verified"] is False
    assert first["hardware_fault"] is True and second["hardware_fault"] is True
    assert early.calls == [] and late.calls == []


@pytest.mark.parametrize("code", [15, 16])
def test_partial_identity_enumeration_never_exposes_verified_partial_mapping(tmp_path, monkeypatch, code):
    from agent.tests.test_gpu_oc_backend import FakeFunction, FakeNvmlWithUuid

    cfg = config(tmp_path)
    save_owned(cfg, {"GPU-stable-0": {"index": 0, CORE: 300}, "GPU-stable-1": {"index": 1, CORE: 200}})
    before = gpu_oc_state_path(cfg).read_bytes()
    library = FakeNvmlWithUuid()
    read_uuid = library.nvmlDeviceGetUUID
    library.nvmlDeviceGetUUID = FakeFunction(
        lambda handle, output, length: code if handle.value == 1001 else read_uuid(handle, output, length)
    )
    document = {
        "gpu_profile_id": "whole-profile", "name": "All requested GPUs", "vendor": "nvidia",
        "settings": [{"index": 0, CORE: 100}, {"index": 1, CORE: 150}],
    }

    def no_state_write(*_args, **_kwargs):
        pytest.fail("partial identity enumeration must never update state")

    monkeypatch.setattr(commands, "_write_gpu_oc_state", no_state_write)
    outcome = executor(cfg, NvmlGpuOcBackend(library)).execute(Command("partial-enumeration", "apply_gpu_profile", document))

    recovery = outcome.result["gpu_oc_recovery"]
    assert outcome.status == "failed" and recovery["nvml_codes"] == [code]
    assert recovery["phase"] == "identity_enumeration" and recovery["hardware_fault"] is True
    assert recovery["write_attempted"] is False and recovery["confirmed_reset"] == []
    assert "failure_target" not in recovery
    assert all(target["identity_verified"] is False and "requested_gpu_uuid" not in target for target in recovery["targets"])
    assert library.calls == [] and gpu_oc_state_path(cfg).read_bytes() == before


@pytest.mark.parametrize("fault_codes", [(15,), (16,), (15, 16)])
@pytest.mark.parametrize("phase", ["identity", "transition"])
def test_bounded_code_display_preserves_tail_fault_codes_and_classification(tmp_path, fault_codes, phase):
    cfg = config(tmp_path)
    codes = (*range(1, 15), 17, 18, *fault_codes)
    error = GpuOcError(
        "aggregated NVML errors", nvml_codes=codes,
        recovery={"status": "rolled_back", "confirmed_reset": [], "errors": []},
    )
    backend = Backend(error)
    if phase == "identity":
        def identity_failure(_index, _uuid):
            raise error
        backend.verify_device_uuid = identity_failure

    outcome = executor(cfg, backend).execute(Command("tail-fault", "apply_gpu_profile", request()))

    recovery = outcome.result["gpu_oc_recovery"]
    assert outcome.status == "failed" and recovery["hardware_fault"] is True
    assert recovery["recovered"] is False
    assert recovery["reset_required"] is (16 in fault_codes)
    assert recovery["hardware_reset_required"] is (16 in fault_codes)
    assert len(recovery["nvml_codes"]) == 16
    assert recovery["nvml_codes"][:14] == list(range(1, 15))
    assert set(fault_codes) <= set(recovery["nvml_codes"])
    assert recovery["confirmed_reset"] == [] and not gpu_oc_state_path(cfg).exists()
    assert len(backend.calls) == (0 if phase == "identity" else 1)


@pytest.mark.parametrize("failure_phase", ["query", "setter"])
def test_real_backend_reset_required_receipt_crosses_command_state_boundary(tmp_path, failure_phase):
    from agent.tests.test_gpu_oc_backend import FakeFunction
    from agent.tests.test_gpu_oc_clear_integration import RecordingNvml, payload

    cfg = config(tmp_path)
    uuid = "GPU-stable-0"
    save_owned(cfg, {uuid: {"index": 0, CORE: 300}, OTHER: {"index": 2, CORE: 100}})
    before = gpu_oc_state_path(cfg).read_bytes()
    library = RecordingNvml()
    library.hardware[0][CORE] = 300
    setter = library.nvmlDeviceSetGpcClkVfOffset
    if failure_phase == "query":
        library.nvmlDeviceGetGpcClkMinMaxVfOffset = FakeFunction(lambda *_args: 16)
    else:
        def set_and_fail(handle, value):
            result = setter(handle, value)
            return 16 if value.value == 500 else result
        library.nvmlDeviceSetGpcClkVfOffset = FakeFunction(set_and_fail)
    backend = NvmlGpuOcBackend(library)

    outcome = executor(cfg, backend).execute(Command("native-contract", "apply_gpu_profile", payload(0, {CORE: 500})))

    assert outcome.status == "failed"
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["nvml_codes"] == [16]
    assert recovery["reset_required"] is True and recovery["hardware_reset_required"] is True
    assert recovery["failure_target"] == {"index": 0, "operation": "query" if failure_phase == "query" else "apply", "requested_gpu_uuid": uuid, "identity_verified": False, "field": CORE}
    assert recovery["recovered"] is False
    if failure_phase == "query":
        assert recovery["write_attempted"] is False
        assert library.writes == [] and gpu_oc_state_path(cfg).read_bytes() == before
    else:
        assert recovery["write_attempted"] is True
        assert library.writes == [(0, CORE, 0), (0, CORE, 500), (0, CORE, 0)]
        assert library.hardware[0][CORE] == 0
        assert owned(cfg) == {OTHER: {"index": 2, CORE: 100}}


def test_stop_failure_keeps_recovery_receipt_and_other_card_state(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}, OTHER: {"index": 2, CORE: 100}})
    backend = Backend(failure())
    manager = SimpleNamespace(stop=lambda: {"stopped": True})
    monkeypatch.setattr(commands, "_active_flight_uses_srb_gpu_oc", lambda _cfg: False)

    outcome = executor(cfg, backend, manager=manager).execute(Command("stop-failed", "stop_mining", {}))

    assert outcome.status == "failed" and outcome.result["stopped"] is True
    assert outcome.result["gpu_oc_recovery"]["hardware_reset_required"] is True
    assert outcome.result["gpu_oc_reset"]["state_cleared"] is False
    assert owned(cfg) == {OTHER: {"index": 2, CORE: 100}}


def test_flight_state_failure_after_default_restore_never_replays_old_oc(tmp_path, monkeypatch):
    from agent.tests.test_managed_mining_commands import (
        _package_installer, config as managed_config, srb_payload,
    )

    cfg = managed_config(tmp_path, allow_gpu_oc=True)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}, MISSING: {"index": 3, CORE: 200}})
    before = gpu_oc_state_path(cfg).read_bytes()

    class SuccessfulReset(Backend):
        def transition(self, previous, current):
            self.calls.append(copy.deepcopy((previous, current)))
            assert current == []
            return {
                "vendor": "nvidia", "write_attempted": True, "operations": [],
                "reset": [{"index": row["index"], "reset": [CORE]} for row in previous],
            }

    backend = SuccessfulReset(None)

    def cannot_save(*_args, **_kwargs):
        raise OSError("simulated flight state write failure")

    def no_miner(*_args, **_kwargs):
        pytest.fail("a failed GPU state handoff must not start a miner")

    monkeypatch.setattr(commands, "_write_gpu_oc_state", cannot_save)
    document = srb_payload()
    document["gpu_oc"] = None
    document["primary"]["device_indices"] = [1]
    outcome = CommandExecutor(
        cfg, gpu_backend_factory=lambda: backend,
        package_installer=_package_installer(tmp_path, "local-fixture"),
        managed_miner_factory=no_miner,
    ).execute(Command("flight-state-failed", "apply_flight_sheet", document))

    assert outcome.status == "failed"
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["persistence_failed"] is True and recovery["hardware_state"] == "defaults_restored"
    assert recovery["recovered"] is False and recovery["state_reconciled"] is False
    assert gpu_oc_state_path(cfg).read_bytes() == before
    assert len(backend.calls) == 1
    assert not cfg.flight_sheet_file.with_name(f".{cfg.flight_sheet_file.name}.pending").exists()


def test_failed_gpu_command_receipt_replays_after_runtime_reload_without_execution(tmp_path):
    cfg = config(tmp_path)
    save_owned(cfg, {TARGET: {"index": 0, CORE: 300}})
    backend = Backend(failure())
    posted = []
    client = SimpleNamespace(post_result=lambda _state, command_id, response: (posted.append((command_id, copy.deepcopy(response))), response["status"])[1])
    runtime = object.__new__(AgentRuntime)
    runtime.config, runtime.client, runtime.executor = cfg, client, executor(cfg, backend)
    state = AgentState.new("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "local-test-secret")
    raw = {"id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff", "action": "apply_gpu_profile", "payload": request()}

    runtime._handle_command(state, raw)
    receipt = copy.deepcopy(posted[-1][1])
    runtime.executor = SimpleNamespace(execute=lambda *_a, **_k: pytest.fail("cached failure was re-executed"))
    runtime._handle_command(load_state(cfg.state_file), raw)

    assert receipt["status"] == "failed"
    assert receipt["result"]["gpu_oc_recovery"]["hardware_reset_required"] is True
    assert posted[-1][1] == receipt
    assert len(backend.calls) == 1
