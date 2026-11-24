"""Linux/Windows no-flight-OC has one owner, including delayed GPU startup.

Vendor and process boundaries are synthetic. The actual command, persistence,
launch, role and restart paths run without any miner, GPU or network operation.
"""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import psutil
import pytest

import agent.commands as commands
import agent.managed_miners as miners
from agent.commands import Command, CommandExecutor
from agent.managed_miners import ManagedMinerManager
from agent.tests.test_managed_mining_commands import config, package, srb_payload
from agent.tests.test_managed_miners import (
    FakePopen, FakeProcess, completed_device_list, sealed_srb_fixture_metadata,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SyntheticGpu:
    supports_uuid_bound_transitions = True

    def __init__(self, events):
        self.memory_mhz, self.power_w = 15001, 360.0
        self.core_offset = 0
        self.core_lock_mhz = None
        self.identities = {"GPU-synthetic-order": {
            "index": 0, "name": "NVIDIA GeForce RTX 5080",
        }}
        self.other_values = {}
        self.events, self.transitions = events, []

    def verify_device_uuid(self, index, expected):
        assert self.identities[expected]["index"] == index

    def enumerate_device_indices(self):
        return {uuid: identity["index"] for uuid, identity in self.identities.items()}

    def enumerate_device_identities(self):
        return copy.deepcopy(self.identities)

    def transition(self, previous, current):
        self.transitions.append((copy.deepcopy(previous), copy.deepcopy(current)))
        reset, applied = [], []
        for setting in previous:
            fields = [field for field in ("power_limit_w", "core_clock_offset_mhz", "core_clock_lock_mhz", "memory_clock_lock_mhz")
                      if field in setting]
            if setting["index"] == 0:
                if "memory_clock_lock_mhz" in setting:
                    self.memory_mhz = 15001
                if "power_limit_w" in setting:
                    self.power_w = 360.0
                if "core_clock_offset_mhz" in setting:
                    self.core_offset = 0
                if "core_clock_lock_mhz" in setting:
                    self.core_lock_mhz = None
            else:
                values = self.other_values.setdefault(setting["index"], {})
                values.update({field: {"power_limit_w": 360.0,
                    "memory_clock_lock_mhz": 15001, "core_clock_offset_mhz": 0,
                    "core_clock_lock_mhz": None}[field]
                    for field in fields})
            reset.append({"index": setting["index"], "reset": fields})
        for setting in current:
            if setting["index"] == 0:
                self.memory_mhz = setting.get("memory_clock_lock_mhz", self.memory_mhz)
                self.power_w = setting.get("power_limit_w", self.power_w)
                self.core_offset = setting.get("core_clock_offset_mhz", self.core_offset)
                self.core_lock_mhz = setting.get("core_clock_lock_mhz", self.core_lock_mhz)
            else:
                self.other_values.setdefault(setting["index"], {}).update({
                    field: setting[field] for field in ("power_limit_w",
                    "core_clock_offset_mhz", "core_clock_lock_mhz", "memory_clock_lock_mhz") if field in setting})
            applied.append({"index": setting["index"], "applied": [
                field for field in ("power_limit_w", "core_clock_offset_mhz", "core_clock_lock_mhz", "memory_clock_lock_mhz")
                if field in setting]})
        self.events.append("nvml_reset_reapply" if previous and current else
                           "nvml_reset" if previous else "nvml_apply")
        return {"vendor": "nvidia", "reset": reset, "operations": applied}

    def deferred_gpu_start(self, process):
        if process.running and "--gpu-reset-oc" in process.argv:
            self.memory_mhz, self.power_w = 15001, 360.0
            self.core_offset = 0
            self.core_lock_mhz = None
            self.events.append("late_live_srb_reset")
        else:
            self.events.append("gpu_start_without_oc_write")


def profile_command(identifier, *, uuid="GPU-synthetic-order", index=0,
                    memory=810, power=300.0, core_offset=None, core_lock=None):
    settings = {"index": index, "memory_clock_lock_mhz": memory,
                "power_limit_w": power}
    if core_offset is not None:
        settings["core_clock_offset_mhz"] = core_offset
    if core_lock is not None:
        settings["core_clock_lock_mhz"] = core_lock
    return Command(identifier, "apply_gpu_profile", {
        "gpu_profile_id": "device-oc-" + identifier, "name": "Synthetic startup OC",
        "vendor": "nvidia", "mode": "merge_one",
        "expected_gpu_uuid": uuid, "settings": [settings],
    })


def harness(tmp_path, monkeypatch, *, system_name="Windows", independent=False,
            late_old_write_at_stop=False, gpu_role="primary"):
    cfg = config(tmp_path, allow_gpu_oc=True)
    cfg.miner_packages_dir.mkdir()
    srb_root = cfg.miner_packages_dir / "builtin-srbminer-3.7.4-windows-x64"
    srb_root.mkdir()
    executable = "SRBMiner-MULTI.exe" if system_name == "Windows" else "SRBMiner-MULTI"
    (srb_root / executable).write_bytes(b"synthetic SRB only")
    metadata = sealed_srb_fixture_metadata(srb_root, {
        "id": srb_root.name, "name": "SRBMiner-MULTI", "version": "3.7.4",
        "installed_path": str(srb_root),
    })
    public_metadata = {key: value for key, value in metadata.items()
                       if key != "installed_path"}
    from dataclasses import replace
    cfg = replace(cfg, miner_download_host_allowlist=frozenset({
        "downloads.example.test", str(urlparse(public_metadata["download_url"]).hostname),
    }))
    flight = srb_payload()
    flight["gpu_oc"] = None
    flight["primary"].update(coin="PRL", algorithm="pearlhash", device_indices=[0],
                             miner_package=public_metadata, extra_config={})
    paths = {metadata["id"]: srb_root}
    if independent:
        xmrig_root = cfg.miner_packages_dir / "builtin-xmrig-synthetic"
        xmrig_root.mkdir()
        (xmrig_root / ("xmrig.exe" if system_name == "Windows" else "xmrig")).write_bytes(b"synthetic XMRig")
        secondary = copy.deepcopy(srb_payload()["primary"])
        secondary.update(miner_type="xmrig", miner_package=package(xmrig_root.name),
                         algorithm="randomx", coin="XMR", device_indices=[], extra_config={})
        flight.update(secondary_mode="independent", secondary=secondary)
        paths[xmrig_root.name] = xmrig_root
        if gpu_role == "secondary":
            flight["primary"], flight["secondary"] = flight["secondary"], flight["primary"]
    clock, events, processes, launches = Clock(), [], {}, []
    monkeypatch.setattr(miners, "time", clock)
    # Command platform policy must match the simulated manager platform.
    monkeypatch.setattr(commands.platform, "system", lambda: system_name)
    monkeypatch.setattr(ManagedMinerManager, "_loopback_api_is_listening", staticmethod(lambda _p: False))
    monkeypatch.setattr(ManagedMinerManager, "_startup_log_progressed", staticmethod(lambda *_a: True))

    def forbidden_reset(*_args, **_kwargs):
        pytest.fail("no-CLI startup must never launch or acknowledge a short SRB reset")

    monkeypatch.setattr(ManagedMinerManager, "_clear_srb_gpu_oc_document", forbidden_reset)
    monkeypatch.setattr(ManagedMinerManager, "_wait_for_srb_oc_reset_ready", forbidden_reset)

    class Process(FakeProcess):
        def terminate(self):
            if late_old_write_at_stop and "--gpu-reset-oc" in self.argv:
                gpu.memory_mhz, gpu.power_w, gpu.core_offset = 14801, 360.0, 0
                gpu.core_lock_mhz = None
                events.append("old_srb_write_before_stop")
            events.append("stop_owned_process")
            super().terminate()

    def popen(argv, **_kwargs):
        pid = 800 + len(launches)
        process = Process(pid, Path(argv[0]), 2000.0 + pid, argv)
        processes[pid] = process
        launches.append(process)
        events.append("launch_srb" if "srbminer" in process.executable.name.casefold() else "launch_cpu")
        return FakePopen(pid)

    def process_factory(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]

    def installer(selected, _config, **_kwargs):
        return {"package_id": selected["id"], "version": selected["version"],
                "path": str(paths[selected["id"]]), "sha256": selected.get("sha256"),
                "downloaded": False}

    ports = itertools.count(24560)
    manager = ManagedMinerManager(
        cfg, system_name=system_name, popen_factory=popen,
        process_factory=process_factory,
        process_iter_factory=lambda: list(processes.values()),
        boot_time_factory=lambda: 0.0, port_allocator=lambda: next(ports),
        device_runner=completed_device_list(
            "GPU0 [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [16384 MB]"),
        privilege_checker=lambda _system: True, enforce_startup_readiness=True,
    )
    gpu = SyntheticGpu(events)
    executor = CommandExecutor(cfg, gpu_backend_factory=lambda: gpu,
        package_installer=installer, managed_miner_factory=lambda _cfg: manager,)
    return SimpleNamespace(cfg=cfg, flight=flight, manager=manager, executor=executor,
                           clock=clock, gpu=gpu, events=events,
                           launches=launches, processes=processes)


def succeed(h, command):
    outcome = h.executor.execute(command)
    assert outcome.status == "succeeded", outcome.error
    return outcome


def srb_processes(h):
    return [p for p in h.launches if "srbminer" in p.executable.name.casefold()]


def assert_late_gpu_start_preserves_profile(h, *, expected_core=None):
    assert (h.gpu.memory_mhz, h.gpu.power_w) == (810, 300.0)
    assert h.gpu.core_lock_mhz == expected_core
    # The supplied real log reached NVML initialization after47seconds. This is
    # an event boundary only; no elapsed-time reset acknowledgement is invented.
    h.clock.sleep(47.0)
    for process in srb_processes(h):
        h.gpu.deferred_gpu_start(process)
    assert (h.gpu.memory_mhz, h.gpu.power_w) == (810, 300.0)
    assert h.gpu.core_lock_mhz == expected_core
    assert "late_live_srb_reset" not in h.events


@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
@pytest.mark.parametrize("independent,gpu_role", [(False, "primary"), (True, "primary"), (True, "secondary")])
def test_windows_no_cli_flight_syncs_known_nvml_reset_before_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, independent: bool, gpu_role: str, system_name: str,
):
    h = harness(tmp_path, monkeypatch, independent=independent, gpu_role=gpu_role, system_name=system_name)
    succeed(h, Command("old-no-cli", "apply_flight_sheet", copy.deepcopy(h.flight)))
    succeed(h, profile_command("old-device-profile", core_lock=1800))
    assert (h.gpu.memory_mhz, h.gpu.power_w) == (810, 300.0)
    h.events.clear()
    new = copy.deepcopy(h.flight)
    new["flight_sheet_id"] = "new-no-cli"
    outcome = succeed(h, Command("new-no-cli", "apply_flight_sheet", new))
    assert h.gpu.transitions[-1][0] and h.gpu.transitions[-1][1] == []
    assert h.events.index("nvml_reset") < h.events.index("launch_srb")
    assert len(srb_processes(h)) == 2
    succeed(h, profile_command("final-device-profile", core_lock=1800))
    assert_late_gpu_start_preserves_profile(h, expected_core=1800)
    assert all("--gpu-reset-oc" not in p.argv for p in srb_processes(h))
    assert outcome.result["gpu_oc"]["source"] == "nvml_reset"
    assert outcome.result["gpu_oc"]["reset"] == [{"index": 0,
        "reset": ["power_limit_w", "core_clock_lock_mhz", "memory_clock_lock_mhz"]}]


@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
@pytest.mark.parametrize("independent,gpu_role", [(False, "primary"), (True, "primary"), (True, "secondary")])
def test_windows_no_cli_restart_does_not_acquire_srb_oc_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, independent: bool, gpu_role: str, system_name: str,
):
    h = harness(tmp_path, monkeypatch, independent=independent, gpu_role=gpu_role, system_name=system_name)
    succeed(h, Command("old-no-cli", "apply_flight_sheet", copy.deepcopy(h.flight)))
    succeed(h, profile_command("old-device-profile", core_lock=1800))
    outcome = succeed(h, Command("restart-no-cli", "restart_mining", {}))
    assert outcome.result["restarted"] is True
    assert len(srb_processes(h)) == 2
    assert_late_gpu_start_preserves_profile(h, expected_core=1800)
    assert all("--gpu-reset-oc" not in p.argv for p in srb_processes(h))


@pytest.mark.parametrize("system_name,old_cli,new_cli", [
    ("Linux", True, False),
    ("Linux", False, True),
    ("Linux", True, True),
    ("Windows", True, False),
    ("Windows", False, True),
    ("Windows", True, True),
])
def test_linux_and_recorded_cli_oc_preserve_existing_reset_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    system_name: str, old_cli: bool, new_cli: bool,
):
    h = harness(tmp_path, monkeypatch, system_name=system_name)
    cli = {"vendor": "nvidia", "settings": [{"index": 0,
           "memory_clock_lock_mhz": 810, "power_limit_w": 300}]}
    old = copy.deepcopy(h.flight)
    old["gpu_oc"] = cli if old_cli else None
    succeed(h, Command("old-flight", "apply_flight_sheet", old))
    new = copy.deepcopy(h.flight)
    new["flight_sheet_id"] = "new-flight"
    new["gpu_oc"] = cli if new_cli else None
    succeed(h, Command("new-flight", "apply_flight_sheet", new))
    assert "--gpu-reset-oc" in srb_processes(h)[-1].argv
    if new_cli:
        assert "--gpu-mclock0" in srb_processes(h)[-1].argv
    else:
        assert "--gpu-mclock0" not in srb_processes(h)[-1].argv


@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
@pytest.mark.parametrize("independent,gpu_role", [(False, "primary"), (True, "primary"), (True, "secondary")])
def test_windows_no_cli_clear_does_not_start_an_uptime_confirmed_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_name: str, independent: bool, gpu_role: str,
):
    h = harness(tmp_path, monkeypatch, independent=independent, gpu_role=gpu_role, system_name=system_name)
    succeed(h, Command("no-cli", "apply_flight_sheet", copy.deepcopy(h.flight)))
    launches = len(h.launches)
    result = h.manager.clear_active_srb_gpu_oc()
    assert result["required"] is False
    assert result["cleared"] is False
    assert len(h.launches) == launches


@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
@pytest.mark.parametrize("independent,gpu_role", [(False, "primary"), (True, "primary"), (True, "secondary")])
@pytest.mark.parametrize("action", ["stop_mining", "remove_flight_sheet"])
def test_no_cli_stop_or_remove_closes_owned_roles_then_resets_nvml_without_srb_helper(
    tmp_path, monkeypatch, system_name, independent, gpu_role, action,
):
    h = harness(tmp_path, monkeypatch, independent=independent,
                gpu_role=gpu_role, system_name=system_name)
    succeed(h, Command("no-cli", "apply_flight_sheet", copy.deepcopy(h.flight)))
    succeed(h, profile_command("owned-profile", core_lock=1800))
    launches = len(h.launches)
    h.events.clear()

    outcome = succeed(h, Command(action, action, {}))

    assert len(h.launches) == launches
    assert all(not process.running for process in h.launches)
    assert h.events.index("stop_owned_process") < h.events.index("nvml_reset")
    assert (h.gpu.memory_mhz, h.gpu.power_w, h.gpu.core_lock_mhz) == (15001, 360.0, None)
    assert outcome.result["gpu_oc_reset"]["state_cleared"] is True
    state = json.loads((h.cfg.managed_miner_runtime_dir /
                        "rigpilot-gpu-oc-state.json").read_text())
    assert state == {"version": 2, "devices": []}
    if action == "remove_flight_sheet":
        assert not h.cfg.flight_sheet_file.exists()


@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
@pytest.mark.parametrize("independent,gpu_role", [(False, "primary"), (True, "primary"), (True, "secondary")])
def test_no_cli_device_profile_clear_is_not_reversed_by_late_gpu_start(
    tmp_path, monkeypatch, system_name, independent, gpu_role,
):
    h = harness(tmp_path, monkeypatch, independent=independent,
                gpu_role=gpu_role, system_name=system_name)
    succeed(h, Command("no-cli", "apply_flight_sheet", copy.deepcopy(h.flight)))
    succeed(h, profile_command("owned-profile", core_lock=1800))
    clear = profile_command("clear-profile", core_lock=1800)
    clear.payload.update(mode="reset_one", previous_index=0)
    succeed(h, clear)
    h.gpu.transitions.clear()

    h.clock.sleep(47.0)
    for process in srb_processes(h):
        h.gpu.deferred_gpu_start(process)

    assert (h.gpu.memory_mhz, h.gpu.power_w, h.gpu.core_lock_mhz) == (15001, 360.0, None)
    assert h.gpu.transitions == []
    assert "late_live_srb_reset" not in h.events
    state = json.loads((h.cfg.managed_miner_runtime_dir /
                        "rigpilot-gpu-oc-state.json").read_text())
    assert state == {"version": 2, "devices": []}


def model_cli_flight(h):
    old = copy.deepcopy(h.flight)
    # Only automatic miner selection can be compared to a complete NVML model
    # snapshot; numeric SRB device IDs never establish NVML index identity.
    old["primary"]["device_indices"] = []
    old["gpu_oc"] = {"vendor": "nvidia", "settings": [{
        "model_match": "5080", "memory_clock_lock_mhz": 810,
        "power_limit_w": 300.0, "core_clock_offset_mhz": 300,
    }]}
    return old


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
def test_covered_single_model_cli_handoff_closes_old_writer_then_reapplies_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, independent: bool, system_name: str,
):
    h = harness(tmp_path, monkeypatch, independent=independent,
                late_old_write_at_stop=True, system_name=system_name)
    succeed(h, Command("old-model-cli", "apply_flight_sheet", model_cli_flight(h)))
    succeed(h, profile_command("known-5080", core_offset=300))
    h.events.clear()
    before_transitions = len(h.gpu.transitions)

    new = copy.deepcopy(h.flight)
    new["flight_sheet_id"] = "new-no-cli"
    outcome = succeed(h, Command("covered-no-cli", "apply_flight_sheet", new))
    assert outcome.result["gpu_oc"]["source"] == "nvml_handoff"
    assert outcome.result["gpu_oc"]["standalone_nvml_reapplied"] is True
    assert "--gpu-reset-oc" not in srb_processes(h)[-1].argv
    assert len(h.gpu.transitions) == before_transitions + 1
    previous, current = h.gpu.transitions[-1]
    assert previous == current
    assert len(previous) == 1
    assert previous[0]["_expected_gpu_uuid"] == "GPU-synthetic-order"
    assert previous[0]["index"] == 0
    assert h.events.index("old_srb_write_before_stop") < h.events.index("launch_srb")
    assert h.events.index("launch_srb") < h.events.index("nvml_reset_reapply")
    assert (h.gpu.memory_mhz, h.gpu.power_w, h.gpu.core_offset) == (810, 300.0, 300)
    assert h.gpu.other_values == {}

    state = json.loads((h.cfg.managed_miner_runtime_dir /
                        "rigpilot-gpu-oc-state.json").read_text())
    assert {device["gpu_uuid"] for device in state["devices"]} == {"GPU-synthetic-order"}
    succeed(h, profile_command("controller-final-replay", core_offset=300))
    assert_late_gpu_start_preserves_profile(h)
    assert h.gpu.core_offset == 300
    assert h.gpu.other_values == {}


@pytest.mark.parametrize("uncovered", ["extra_old_field", "matching_other_gpu", "numeric_locator",
                                       "unmatched_owned_gpu", "explicit_miner_selection"])
@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
def test_uncovered_cli_handoff_preserves_existing_srb_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uncovered: str, system_name: str,
):
    h = harness(tmp_path, monkeypatch, system_name=system_name)
    old = model_cli_flight(h)
    if uncovered == "extra_old_field":
        old["gpu_oc"]["settings"][0]["memory_clock_offset_mhz"] = 800
    elif uncovered == "numeric_locator":
        setting = old["gpu_oc"]["settings"][0]
        setting.pop("model_match")
        setting["index"] = 0
        old["primary"]["device_indices"] = [0]
    elif uncovered == "explicit_miner_selection":
        old["primary"]["device_indices"] = [0]
    elif uncovered == "unmatched_owned_gpu":
        h.gpu.identities["GPU-unmatched-5070"] = {
            "index": 1, "name": "NVIDIA GeForce RTX 5070",
        }
    else:
        h.gpu.identities["GPU-unowned-5080"] = {
            "index": 1, "name": "NVIDIA GeForce RTX 5080",
        }
    succeed(h, Command("old-uncovered-cli", "apply_flight_sheet", old))
    succeed(h, profile_command("known-three-fields", core_offset=300))
    if uncovered == "unmatched_owned_gpu":
        # Old CLI model rows can emit zero values for other selected devices.
        # Their meaning is not proven, so even an owned unmatched GPU prevents
        # this conservative handoff; do not assert that it was uncontrolled.
        succeed(h, profile_command("known-unmatched-5070", uuid="GPU-unmatched-5070",
                                   index=1, memory=7001, power=250.0, core_offset=100))
    new = copy.deepcopy(h.flight)
    new["flight_sheet_id"] = "new-uncovered-no-cli"
    outcome = succeed(h, Command("uncovered-no-cli", "apply_flight_sheet", new))
    assert "--gpu-reset-oc" in srb_processes(h)[-1].argv
    assert outcome.result["gpu_oc"]["source"] == "srbminer_cli_reset"


@pytest.mark.parametrize("unknown", ["missing_name", "ambiguous_model", "unmatched_model",
                                     "deferred_owned_uuid"])
def test_cli_coverage_predicate_does_not_infer_unknown_model_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown: str,
):
    h = harness(tmp_path, monkeypatch)
    old = model_cli_flight(h)
    owned = {"GPU-synthetic-order": {
        "index": 0, "memory_clock_lock_mhz": 810,
        "power_limit_w": 300.0, "core_clock_offset_mhz": 300,
    }}
    identities = copy.deepcopy(h.gpu.identities)
    if unknown == "missing_name":
        identities["GPU-synthetic-order"]["name"] = ""
    elif unknown == "unmatched_model":
        old["gpu_oc"]["settings"][0]["model_match"] = "5090"
    elif unknown == "deferred_owned_uuid":
        owned["GPU-no-longer-reported"] = {**owned["GPU-synthetic-order"], "index": 1}
    else:
        old["gpu_oc"]["settings"][0]["model_match"] = "RTX"
        identities["GPU-second-model"] = {"index": 1, "name": "NVIDIA GeForce RTX 5070"}
        owned["GPU-second-model"] = {**owned["GPU-synthetic-order"], "index": 1}
    assert commands._covered_srb_cli_nvml_devices(old, owned, identities) == {}


def test_cli_coverage_predicate_binds_uuid_and_current_index_using_model_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    h = harness(tmp_path, monkeypatch)
    old = model_cli_flight(h)
    old["gpu_oc"]["settings"][0]["model_match"] = "RTX-5080"
    owned = {"GPU-synthetic-order": {
        "index": 7, "memory_clock_lock_mhz": 810,
        "power_limit_w": 300.0, "core_clock_offset_mhz": 300,
    }}
    assert commands._covered_srb_cli_nvml_devices(
        old, owned, h.gpu.identities,
    ) == {"GPU-synthetic-order": {**owned["GPU-synthetic-order"], "index": 0}}


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
def test_failed_cli_handoff_stops_no_cli_target_and_restores_previous_cli_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, independent: bool, system_name: str,
):
    h = harness(tmp_path, monkeypatch, independent=independent, system_name=system_name)
    succeed(h, Command("old-cli", "apply_flight_sheet", model_cli_flight(h)))
    succeed(h, profile_command("known-before-failed-handoff", core_offset=300))
    state_path = h.cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    old_state, old_active = state_path.read_bytes(), h.cfg.flight_sheet_file.read_bytes()
    original_transition = h.gpu.transition
    attempts = []

    def failed_transition(previous, current):
        if previous and previous == current:
            attempts.append((copy.deepcopy(previous), copy.deepcopy(current)))
            raise RuntimeError("synthetic synchronous handoff failed")
        return original_transition(previous, current)

    monkeypatch.setattr(h.gpu, "transition", failed_transition)
    new = copy.deepcopy(h.flight)
    new["flight_sheet_id"] = "failed-handoff-target"
    outcome = h.executor.execute(Command("failed-handoff", "apply_flight_sheet", new))
    assert outcome.status == "failed"
    assert "synthetic synchronous handoff failed" in outcome.error
    assert len(attempts) == 1
    srb = srb_processes(h)
    assert len(srb) == 3
    assert not srb[0].running
    assert "--gpu-reset-oc" not in srb[1].argv
    assert not srb[1].running and srb[1].terminated
    assert srb[2].running
    assert "--gpu-reset-oc" in srb[2].argv
    assert srb[2].argv[srb[2].argv.index("--gpu-mclock0") + 1] == "810"
    assert srb[2].argv[srb[2].argv.index("--gpu-plimit0") + 1] == "300"
    assert state_path.read_bytes() == old_state
    assert h.cfg.flight_sheet_file.read_bytes() == old_active
    assert not h.cfg.flight_sheet_file.with_name(
        "." + h.cfg.flight_sheet_file.name + ".pending").exists()
    if independent:
        current = h.manager.running()
        assert current["complete"] is True
        assert current["flight_sheet_id"] == h.flight["flight_sheet_id"]


@pytest.mark.parametrize("system_name", ["Windows", "Linux"])
def test_cli_handoff_backend_construction_failure_cleans_pending_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_name: str,
):
    h = harness(tmp_path, monkeypatch, system_name=system_name)
    succeed(h, Command("old-cli", "apply_flight_sheet", model_cli_flight(h)))
    succeed(h, profile_command("known-before-factory-failure", core_offset=300))
    state_path = h.cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    old_state, old_active = state_path.read_bytes(), h.cfg.flight_sheet_file.read_bytes()
    old_launches = list(h.launches)
    old_transitions = copy.deepcopy(h.gpu.transitions)
    calls = []

    def failed_factory():
        calls.append(True)
        raise RuntimeError("synthetic backend construction failed")

    monkeypatch.setattr(h.executor, "gpu_backend_factory", failed_factory)
    new = copy.deepcopy(h.flight)
    new["flight_sheet_id"] = "failed-factory-target"
    outcome = h.executor.execute(Command("failed-factory", "apply_flight_sheet", new))
    assert outcome.status == "failed"
    assert "synthetic backend construction failed" in outcome.error
    assert calls == [True]
    assert h.launches == old_launches
    assert all(process.running for process in old_launches)
    assert h.gpu.transitions == old_transitions
    assert state_path.read_bytes() == old_state
    assert h.cfg.flight_sheet_file.read_bytes() == old_active
    assert not h.cfg.flight_sheet_file.with_name(
        "." + h.cfg.flight_sheet_file.name + ".pending").exists()


def test_cli_coverage_keeps_nvidia_oc_scope_for_amd_capable_algorithm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    h = harness(tmp_path, monkeypatch)
    old = model_cli_flight(h)
    old["primary"].update(algorithm="etchash", coin="ETC")
    owned = {"GPU-synthetic-order": {
        "index": 0, "memory_clock_lock_mhz": 810,
        "power_limit_w": 300.0, "core_clock_offset_mhz": 300,
    }}
    # Etchash can use AMD, but the modeled --gpu-* OC fields are NVIDIA-only.
    # Algorithm capability must not invent an AMD OC writer or reject an
    # otherwise fully covered automatic NVIDIA snapshot.
    assert commands._covered_srb_cli_nvml_devices(old, owned, h.gpu.identities) == owned
