from __future__ import annotations

import json
import subprocess

import pytest
from pathlib import Path

from agent.commands import Command, CommandExecutor
from agent.config import AgentConfig
from agent.state import AgentState


def mining_leg(**overrides):
    leg = {
        "coin": "BTC",
        "wallet_address": "bc1qexamplepublicaddress000000",
        "pool_url": "stratum+ssl://pool.example.test:443",
        "miner_profile": "xmrig-safe",
        "miner_package": None,
        "extra_config": {},
        "algorithm": "sha256",
        "pool_password": "x",
        "device_indices": [0],
    }
    leg.update(overrides)
    return leg


def flight_payload(*, primary_overrides=None, **overrides):
    payload = {
        "flight_sheet_id": "sheet-1",
        "name": "BTC safe",
        "worker_name": "rig-01",
        "primary": mining_leg(**(primary_overrides or {})),
        "secondary": None,
    }
    payload.update(overrides)
    return payload


def extended_flight_payload(**overrides):
    payload = flight_payload()
    payload["secondary"] = mining_leg(
        coin="ZIL",
        wallet_address="zil1publicaddress000000000000",
        pool_url="stratum+tcp://zil.example.test:3333",
        miner_profile="gpu-safe",
        extra_config={"dual_intensity": 4},
        algorithm="zil",
        device_indices=[1],
    )
    payload.update(overrides)
    return payload


def gpu_payload(**setting_overrides):
    setting = {
        "index": 0,
        "power_limit_w": 120,
        "core_clock_offset_mhz": 100,
        "memory_clock_offset_mhz": 800,
        "fan_percent": 65,
    }
    setting.update(setting_overrides)
    return {
        "gpu_profile_id": "gpu-profile-1",
        "name": "safe",
        "vendor": "nvidia",
        "settings": [setting],
    }


def device_gpu_payload(*, gpu_uuid: str, **setting_overrides):
    payload = gpu_payload(**setting_overrides)
    payload.update(
        {
            "gpu_profile_id": f"device-oc-{gpu_uuid}",
            "mode": "merge_one",
            "expected_gpu_uuid": gpu_uuid,
        }
    )
    return payload


def config(tmp_path: Path, **overrides) -> AgentConfig:
    values = {
        "server_url": "https://example.test",
        "state_file": tmp_path / "state.json",
        "flight_sheet_file": tmp_path / "miner" / "flight-sheet.json",
        "miner_profile_allowlist": frozenset({"xmrig-safe"}),
        "allow_gpu_oc": True,
    }
    values.update(overrides)
    return AgentConfig(**values)


def completed(argv, **kwargs):
    assert kwargs["shell"] is False
    return subprocess.CompletedProcess(argv, 0, "", "")


def test_flight_sheet_is_atomic_and_can_restart_only_local_service(tmp_path: Path):
    cfg = config(
        tmp_path,
        service_allowlist=frozenset({"miner-service"}),
        miner_service="miner-service",
    )
    executor = CommandExecutor(cfg, runner=completed)
    outcome = executor.execute(Command("1", "apply_flight_sheet", flight_payload()))
    assert outcome.status == "succeeded", outcome.error
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["primary"]["wallet_address"].startswith("bc1q")
    assert stored["primary"]["miner_profile"] == "xmrig-safe"
    assert stored["primary"]["algorithm"] == "sha256"
    assert stored["primary"]["pool_password"] == "x"
    assert stored["primary"]["device_indices"] == [0]
    assert outcome.result["service"] == "miner-service"


def test_flight_sheet_rejects_local_policy_violations(tmp_path: Path):
    disabled = config(tmp_path, flight_sheet_file=None)
    outcome = CommandExecutor(disabled).execute(
        Command("1", "apply_flight_sheet", flight_payload())
    )
    assert outcome.status == "failed"
    assert outcome.result["rejected"] is True

    cfg = config(tmp_path)
    bad_profile = CommandExecutor(cfg).execute(
        Command(
            "2",
            "apply_flight_sheet",
            flight_payload(primary_overrides={"miner_profile": "not-local"}),
        )
    )
    assert bad_profile.status == "failed"
    assert bad_profile.error == (
        "primary.miner_profile is not in the local allowlist; "
        "allowed values: xmrig-safe"
    )
    extra = CommandExecutor(cfg).execute(
        Command(
            "3",
            "apply_flight_sheet",
            flight_payload(primary_overrides={"extra_config": {"command": "do-not-run"}}),
        )
    )
    assert extra.status == "failed"
    assert not cfg.flight_sheet_file.exists()


def test_flight_sheet_malformed_ipv6_fails_closed_and_algorithm_may_use_slash(
    tmp_path: Path,
):
    cfg = config(tmp_path)
    malformed = flight_payload(
        primary_overrides={"pool_url": "stratum+tcp://[::1:3333"}
    )
    outcome = CommandExecutor(cfg).execute(
        Command("bad-ipv6", "apply_flight_sheet", malformed)
    )
    assert outcome.status == "failed"
    assert outcome.result["rejected"] is True
    assert "malformed" in (outcome.error or "")

    slash_algorithm = flight_payload(
        primary_overrides={"algorithm": "ethash/zil"}
    )
    accepted = CommandExecutor(cfg).execute(
        Command("slash", "apply_flight_sheet", slash_algorithm)
    )
    assert accepted.status == "succeeded", accepted.error


def test_dry_run_validates_but_does_not_change_files_or_gpu(tmp_path: Path):
    calls = []

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-test": 0}

        def apply(self, settings):
            calls.append(settings)
            return {"operations": []}

    cfg = config(tmp_path, dry_run=True)
    flight = CommandExecutor(cfg).execute(
        Command("1", "apply_flight_sheet", flight_payload())
    )
    gpu = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command("2", "apply_gpu_profile", gpu_payload())
    )
    assert flight.result["dry_run"] is True
    assert gpu.result["dry_run"] is True
    assert not cfg.flight_sheet_file.exists()
    assert calls == []


def test_gpu_profile_uses_mock_backend_after_local_range_checks(tmp_path: Path):
    calls = []

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-test": 0}

        def apply(self, settings):
            calls.append(settings)
            return {"operations": [{"index": 0, "applied": ["power_limit_w"]}]}

    cfg = config(tmp_path, gpu_power_limit_min_w=50, gpu_power_limit_max_w=300)
    outcome = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command("1", "apply_gpu_profile", gpu_payload())
    )
    assert outcome.status == "succeeded", outcome.error
    assert calls[0][0]["power_limit_w"] == 120.0

    too_high = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command("2", "apply_gpu_profile", gpu_payload(power_limit_w=999))
    )
    assert too_high.status == "failed"
    assert too_high.result["rejected"] is True
    assert len(calls) == 1


def test_gpu_profile_validates_and_combines_offsets_with_absolute_locks(tmp_path: Path):
    calls = []

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-test": 0}

        def apply(self, settings):
            calls.append(settings)
            return {"operations": [{"index": 0, "applied": []}]}

    cfg = config(
        tmp_path,
        gpu_core_clock_lock_min_mhz=500,
        gpu_core_clock_lock_max_mhz=2500,
        gpu_memory_clock_lock_min_mhz=500,
        gpu_memory_clock_lock_max_mhz=12000,
    )
    payload = gpu_payload()
    payload["settings"][0].update(
        core_clock_lock_mhz=1450,
        memory_clock_lock_mhz=8100,
    )
    accepted = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command("lock-ok", "apply_gpu_profile", payload)
    )
    assert accepted.status == "succeeded", accepted.error
    assert calls[0][0]["core_clock_lock_mhz"] == 1450
    assert calls[0][0]["memory_clock_lock_mhz"] == 8100
    assert calls[0][0]["core_clock_offset_mhz"] == 100
    assert calls[0][0]["memory_clock_offset_mhz"] == 800

    rejected_payload = gpu_payload()
    rejected_payload["settings"][0].pop("core_clock_offset_mhz")
    rejected_payload["settings"][0]["core_clock_lock_mhz"] = 2600
    rejected = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command(
            "lock-high",
            "apply_gpu_profile",
            rejected_payload,
        )
    )
    assert rejected.status == "failed"
    assert "local range" in (rejected.error or "")
    assert len(calls) == 1


def test_gpu_oc_is_disabled_by_default(tmp_path: Path):
    cfg = AgentConfig(server_url="https://example.test", state_file=tmp_path / "state.json")
    outcome = CommandExecutor(cfg).execute(
        Command("1", "apply_gpu_profile", gpu_payload())
    )
    assert outcome.status == "failed"
    assert "disabled" in (outcome.error or "")


def test_gpu_profile_state_works_without_flight_sheet_configuration(tmp_path: Path):
    calls = []

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-test": 0}

        def apply(self, settings):
            calls.append(settings)
            return {"operations": []}

    cfg = AgentConfig(
        server_url="https://example.test",
        state_file=tmp_path / "agent-state.json",
        allow_gpu_oc=True,
    )
    outcome = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command("standalone-oc", "apply_gpu_profile", gpu_payload())
    )
    assert outcome.status == "succeeded", outcome.error
    assert calls
    assert (tmp_path / "rigpilot-gpu-oc-state.json").is_file()


def test_per_device_gpu_oc_preserves_other_cards_and_unchanged_fields(
    tmp_path: Path,
):
    transitions = []
    verified = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            verified.append((index, expected_uuid))
            return expected_uuid

        def transition(self, previous, current):
            transitions.append(
                json.loads(json.dumps((previous, current)))
            )
            return {"vendor": "nvidia", "operations": [], "reset": []}

    cfg = config(tmp_path)
    executor = CommandExecutor(cfg, gpu_backend_factory=Backend)

    first = device_gpu_payload(gpu_uuid="GPU-first")
    second = device_gpu_payload(
        gpu_uuid="GPU-second", index=1, power_limit_w=135
    )
    patch = {
        "gpu_profile_id": "device-oc-update-second",
        "name": "GPU 1 power update",
        "vendor": "nvidia",
        "settings": [{"index": 1, "power_limit_w": 140}],
        "mode": "merge_one",
        "expected_gpu_uuid": "GPU-second",
    }

    assert executor.execute(
        Command("device-0", "apply_gpu_profile", first)
    ).status == "succeeded"
    assert executor.execute(
        Command("device-1", "apply_gpu_profile", second)
    ).status == "succeeded"
    assert executor.execute(
        Command("device-1-update", "apply_gpu_profile", patch)
    ).status == "succeeded"

    gpu_0 = {
        "index": 0,
        "power_limit_w": 120.0,
        "core_clock_offset_mhz": 100,
        "memory_clock_offset_mhz": 800,
        "fan_percent": 65,
    }
    gpu_1 = {
        "index": 1,
        "power_limit_w": 135.0,
        "core_clock_offset_mhz": 100,
        "memory_clock_offset_mhz": 800,
        "fan_percent": 65,
    }
    gpu_1_updated = {**gpu_1, "power_limit_w": 140.0}
    assert transitions == [
        [[], [gpu_0]],
        [[], [gpu_1]],
        [[gpu_1], [gpu_1_updated]],
    ]
    assert verified == [
        (0, "GPU-first"),
        (1, "GPU-second"),
        (1, "GPU-second"),
    ]
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-first",
                "index_hint": 0,
                "setting": {key: value for key, value in gpu_0.items() if key != "index"},
            },
            {
                "gpu_uuid": "GPU-second",
                "index_hint": 1,
                "setting": {key: value for key, value in gpu_1_updated.items() if key != "index"},
            },
        ],
    }


def test_uuid_verified_replace_one_does_not_inherit_stale_card_fields(
    tmp_path: Path,
):
    transitions = []
    verified = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            verified.append((index, expected_uuid))

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    cfg = config(tmp_path)
    executor = CommandExecutor(cfg, gpu_backend_factory=Backend)
    first = device_gpu_payload(gpu_uuid="GPU-stable")
    replacement = {
        "gpu_profile_id": "device-oc-authoritative",
        "name": "controller desired GPU 0",
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 145}],
        "mode": "replace_one",
        "expected_gpu_uuid": "GPU-stable",
        "previous_index": 0,
    }

    assert executor.execute(Command("first", "apply_gpu_profile", first)).status == "succeeded"
    outcome = executor.execute(Command("replace", "apply_gpu_profile", replacement))

    assert outcome.status == "succeeded", outcome.error
    previous = transitions[0][1][0]
    assert transitions[1] == [
        [previous],
        [{"index": 0, "power_limit_w": 145.0}],
    ]
    assert verified == [(0, "GPU-stable"), (0, "GPU-stable")]
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-stable",
                "index_hint": 0,
                "setting": {"power_limit_w": 145.0},
            }
        ],
    }

    reset = {
        "gpu_profile_id": "device-oc-reset-one",
        "name": "controller desired GPU 0",
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 145}],
        "mode": "reset_one",
        "expected_gpu_uuid": "GPU-stable",
        "previous_index": 0,
    }
    reset_outcome = executor.execute(Command("reset", "apply_gpu_profile", reset))
    assert reset_outcome.status == "succeeded", reset_outcome.error
    assert transitions[2] == [[{"index": 0, "power_limit_w": 145.0}], []]
    assert verified[-1] == (0, "GPU-stable")
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {"version": 2, "devices": []}


def test_uuid_verified_replace_one_migrates_swapped_indices_without_ghost_state(
    tmp_path: Path,
):
    transitions = []
    current_uuids = {0: "GPU-A", 1: "GPU-B"}

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            if current_uuids.get(index) != expected_uuid:
                raise RuntimeError("GPU identity mismatch")

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    def payload(gpu_uuid, index, previous_index, power):
        return {
            "gpu_profile_id": f"device-oc-{gpu_uuid}-{index}",
            "name": gpu_uuid,
            "vendor": "nvidia",
            "settings": [{"index": index, "power_limit_w": power}],
            "mode": "replace_one",
            "expected_gpu_uuid": gpu_uuid,
            "previous_index": previous_index,
        }

    executor = CommandExecutor(config(tmp_path), gpu_backend_factory=Backend)
    assert executor.execute(Command("a0", "apply_gpu_profile", payload("GPU-A", 0, 0, 100))).status == "succeeded"
    assert executor.execute(Command("b1", "apply_gpu_profile", payload("GPU-B", 1, 1, 110))).status == "succeeded"

    current_uuids.clear()
    current_uuids.update({0: "GPU-B", 1: "GPU-A"})
    assert executor.execute(Command("a1", "apply_gpu_profile", payload("GPU-A", 1, 0, 100))).status == "succeeded"
    intermediate = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert [(item["gpu_uuid"], item["index_hint"]) for item in intermediate["devices"]] == [
        ("GPU-A", 1),
        ("GPU-B", 1),
    ]
    assert executor.execute(Command("b0", "apply_gpu_profile", payload("GPU-B", 0, 1, 110))).status == "succeeded"

    # Both transition sides use only the newly UUID-verified index.  In
    # particular A's migration never resets index 0 after B occupies it.
    assert all(
        setting["index"] == 1
        for side in transitions[2]
        for setting in side
    )
    assert all(
        setting["index"] == 0
        for side in transitions[3]
        for setting in side
    )
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-A",
                "index_hint": 1,
                "setting": {"power_limit_w": 100.0},
            },
            {
                "gpu_uuid": "GPU-B",
                "index_hint": 0,
                "setting": {"power_limit_w": 110.0},
            },
        ],
    }


def test_uuid_verified_replace_one_accepts_multiple_index_generations(
    tmp_path: Path,
):
    transitions = []
    current_uuids = {0: "GPU-stable"}

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            if current_uuids.get(index) != expected_uuid:
                raise RuntimeError("GPU identity mismatch")

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    def payload(index, previous_index, power):
        return {
            "gpu_profile_id": f"device-oc-GPU-stable-{index}",
            "name": "GPU-stable",
            "vendor": "nvidia",
            "settings": [{"index": index, "power_limit_w": power}],
            "mode": "replace_one",
            "expected_gpu_uuid": "GPU-stable",
            "previous_index": previous_index,
        }

    executor = CommandExecutor(config(tmp_path), gpu_backend_factory=Backend)
    initial = executor.execute(
        Command("initial", "apply_gpu_profile", payload(0, 0, 100))
    )
    assert initial.status == "succeeded", initial.error

    # The controller observed an intermediate index 1, while this Agent last
    # persisted index 0. The same UUID is now verified at index 2. Numeric
    # indices are position hints, not device identity, so the transition must
    # touch only the currently verified index 2.
    current_uuids.clear()
    current_uuids.update({2: "GPU-stable"})
    moved = executor.execute(
        Command("moved", "apply_gpu_profile", payload(2, 1, 125))
    )
    assert moved.status == "succeeded", moved.error
    assert transitions[-1] == [
        [{"index": 2, "power_limit_w": 100.0}],
        [{"index": 2, "power_limit_w": 125.0}],
    ]
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["devices"][0]["index_hint"] == 2

    # A real UUID mismatch is still rejected before NVML transition or state
    # mutation, even though stale numeric hints are accepted for the same UUID.
    before = state_path.read_bytes()
    transition_count = len(transitions)
    current_uuids.clear()
    current_uuids.update({3: "GPU-other"})
    rejected = executor.execute(
        Command("wrong-device", "apply_gpu_profile", payload(3, 2, 130))
    )
    assert rejected.status == "failed"
    assert "GPU identity mismatch" in (rejected.error or "")
    assert len(transitions) == transition_count
    assert state_path.read_bytes() == before


def test_stop_after_first_swap_reapply_resolves_and_resets_both_uuid_devices(
    tmp_path: Path,
):
    current_uuids = {0: "GPU-A", 1: "GPU-B"}
    transitions = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            if current_uuids.get(index) != expected_uuid:
                raise RuntimeError("GPU identity mismatch")

        def resolve_device_indices(self, expected_uuids):
            resolved = {
                gpu_uuid: index
                for index, gpu_uuid in current_uuids.items()
                if gpu_uuid in expected_uuids
            }
            return resolved

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    class Manager:
        def stop(self):
            return {"stopped": True}

    def payload(gpu_uuid, index, previous_index, power):
        return {
            "gpu_profile_id": f"device-oc-{gpu_uuid}-{index}",
            "name": gpu_uuid,
            "vendor": "nvidia",
            "settings": [{"index": index, "power_limit_w": power}],
            "mode": "replace_one",
            "expected_gpu_uuid": gpu_uuid,
            "previous_index": previous_index,
        }

    executor = CommandExecutor(
        config(tmp_path),
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda _config: Manager(),
    )
    assert executor.execute(Command("a0", "apply_gpu_profile", payload("GPU-A", 0, 0, 100))).status == "succeeded"
    assert executor.execute(Command("b1", "apply_gpu_profile", payload("GPU-B", 1, 1, 110))).status == "succeeded"
    current_uuids.clear()
    current_uuids.update({0: "GPU-B", 1: "GPU-A"})
    assert executor.execute(Command("a1", "apply_gpu_profile", payload("GPU-A", 1, 0, 100))).status == "succeeded"
    transitions.clear()

    stopped = executor.execute(Command("stop", "stop_mining", {}))

    assert stopped.status == "succeeded", stopped.error
    assert transitions == [[
        [
            {"index": 1, "power_limit_w": 100.0},
            {"index": 0, "power_limit_w": 110.0},
        ],
        [],
    ]]


def test_stop_defers_missing_uuid_without_resetting_reused_index(tmp_path: Path):
    cfg = config(tmp_path)
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "devices": [
                    {
                        "gpu_uuid": "GPU-present",
                        "index_hint": 0,
                        "setting": {"power_limit_w": 140},
                    },
                    {
                        "gpu_uuid": "GPU-missing",
                        "index_hint": 3,
                        "setting": {"power_limit_w": 150},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    transitions = []

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-present": 1, "GPU-unrelated": 3}

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    class Manager:
        def stop(self):
            return {"stopped": True}

    stopped = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("stop-partial", "stop_mining", {}))

    assert stopped.status == "succeeded", stopped.error
    assert transitions == [
        [[{"index": 1, "power_limit_w": 140.0}], []]
    ]
    assert stopped.result["gpu_oc_reset"]["settings_count"] == 2
    assert stopped.result["gpu_oc_reset"]["reset_settings_count"] == 1
    assert stopped.result["gpu_oc_reset"]["deferred_gpu_uuids"] == [
        "GPU-missing"
    ]
    assert stopped.result["gpu_oc_reset"]["state_cleared"] is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-missing",
                "index_hint": 3,
                "setting": {"power_limit_w": 150.0},
            }
        ],
    }


def test_legacy_v1_gpu_oc_state_migration_adopts_four_history_bound_devices(
    tmp_path: Path,
):
    transitions = []
    resolved = {
        "GPU-A": 3,
        "GPU-B": 2,
        "GPU-C": 1,
        "GPU-D": 0,
    }

    class Backend:
        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {"vendor": "nvidia", "operations": [], "reset": []}

        def resolve_device_indices(self, expected_uuids):
            assert expected_uuids == ["GPU-A", "GPU-B", "GPU-C", "GPU-D"]
            return dict(resolved)

    cfg = config(tmp_path)
    executor = CommandExecutor(cfg, gpu_backend_factory=Backend)
    legacy_settings = [
        {"index": index, "power_limit_w": 100 + index * 10}
        for index in range(4)
    ]
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": 1, "settings": legacy_settings}),
        encoding="utf-8",
    )
    history_ids = [
        f"0000000{index + 1}-0000-4000-8000-00000000000{index + 1}"
        for index in range(4)
    ]
    devices = [
        {"gpu_uuid": f"GPU-{letter}", "setting": legacy_settings[index]}
        for index, letter in enumerate("ABCD")
    ]

    outcome = executor.execute(
        Command(
            "migrate",
            "migrate_gpu_oc_state.v1",
            {
                "schema_version": 1,
                "history_command_ids": history_ids,
                "devices": devices,
            },
        )
    )

    assert outcome.status == "succeeded", outcome.error
    assert outcome.result["nvml_writes"] == 0
    assert transitions == []
    assert [(item["gpu_uuid"], item["index"]) for item in outcome.result["devices"]] == [
        ("GPU-A", 3),
        ("GPU-B", 2),
        ("GPU-C", 1),
        ("GPU-D", 0),
    ]
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["version"] == 2
    assert [(item["gpu_uuid"], item["index_hint"]) for item in state["devices"]] == [
        ("GPU-A", 3),
        ("GPU-B", 2),
        ("GPU-C", 1),
        ("GPU-D", 0),
    ]


def test_legacy_v1_gpu_oc_migration_mismatch_or_missing_uuid_is_zero_write(
    tmp_path: Path,
):
    transitions = []
    resolve_error = False

    class Backend:
        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

        def resolve_device_indices(self, expected_uuids):
            if resolve_error:
                raise RuntimeError("migration GPU UUID is missing")
            return {"GPU-A": 0, "GPU-B": 1}

    cfg = config(tmp_path)
    executor = CommandExecutor(cfg, gpu_backend_factory=Backend)
    legacy_settings = [
        {"index": 0, "power_limit_w": 100},
        {"index": 1, "power_limit_w": 110},
    ]
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": 1, "settings": legacy_settings}),
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    base_payload = {
        "schema_version": 1,
        "history_command_ids": ["10000000-0000-4000-8000-000000000001"],
        "devices": [
            {"gpu_uuid": "GPU-A", "setting": legacy_settings[0]},
            {"gpu_uuid": "GPU-B", "setting": legacy_settings[1]},
        ],
    }
    mismatched = json.loads(json.dumps(base_payload))
    mismatched["devices"][1]["setting"]["power_limit_w"] = 120

    mismatch_outcome = executor.execute(
        Command("mismatch", "migrate_gpu_oc_state.v1", mismatched)
    )

    assert mismatch_outcome.status == "failed"
    assert transitions == []
    assert state_path.read_bytes() == before

    resolve_error = True
    missing_outcome = executor.execute(
        Command("missing", "migrate_gpu_oc_state.v1", base_payload)
    )
    assert missing_outcome.status == "failed"
    assert "missing" in (missing_outcome.error or "")
    assert transitions == []
    assert state_path.read_bytes() == before


def test_stop_fails_closed_before_reset_when_persisted_uuid_indices_are_stale(
    tmp_path: Path,
):
    current_uuids = {0: "GPU-A", 1: "GPU-B"}
    transitions = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            if current_uuids.get(index) != expected_uuid:
                raise RuntimeError("GPU identity mismatch")

        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    class Manager:
        def stop(self):
            return {"stopped": True}

    def payload(gpu_uuid, index, power):
        return {
            "gpu_profile_id": f"device-oc-{gpu_uuid}",
            "name": gpu_uuid,
            "vendor": "nvidia",
            "settings": [{"index": index, "power_limit_w": power}],
            "mode": "replace_one",
            "expected_gpu_uuid": gpu_uuid,
            "previous_index": index,
        }

    cfg = config(tmp_path)
    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda _config: Manager(),
    )
    assert executor.execute(Command("a", "apply_gpu_profile", payload("GPU-A", 0, 100))).status == "succeeded"
    assert executor.execute(Command("b", "apply_gpu_profile", payload("GPU-B", 1, 110))).status == "succeeded"
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    before = state_path.read_bytes()
    transitions.clear()
    current_uuids.clear()
    current_uuids.update({0: "GPU-B", 1: "GPU-A"})

    stopped = executor.execute(Command("stop", "stop_mining", {}))

    assert stopped.status == "failed"
    assert stopped.result["stopped"] is True
    assert stopped.result["gpu_oc_reset"]["state_cleared"] is False
    assert transitions == []
    assert state_path.read_bytes() == before


def test_stop_mining_stops_miner_then_resets_nvml_without_controller_state(
    tmp_path: Path,
):
    events = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            assert (index, expected_uuid) == (0, "GPU-stop")

        def resolve_device_indices(self, expected_uuids):
            assert expected_uuids == ["GPU-stop"]
            return {"GPU-stop": 0}

        def transition(self, previous, current):
            events.append(("nvml", json.loads(json.dumps(previous)), current))
            return {
                "vendor": "nvidia",
                "operations": [],
                "reset": [{"index": 0, "reset": ["power_limit_w"]}],
            }

    class Manager:
        def stop(self):
            events.append(("miner", "stop"))
            return {"stopped": True}

    cfg = config(tmp_path)
    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda _config: Manager(),
    )
    applied = executor.execute(
        Command(
            "apply",
            "apply_gpu_profile",
            {
                "gpu_profile_id": "device-oc-stop",
                "name": "stop",
                "vendor": "nvidia",
                "settings": [{"index": 0, "power_limit_w": 120}],
                "mode": "replace_one",
                "expected_gpu_uuid": "GPU-stop",
                "previous_index": 0,
            },
        )
    )
    assert applied.status == "succeeded", applied.error
    events.clear()

    stopped = executor.execute(Command("stop", "stop_mining", {}))

    assert stopped.status == "succeeded", stopped.error
    assert events[0] == ("miner", "stop")
    assert events[1][0] == "nvml"
    assert events[1][1][0]["power_limit_w"] == 120.0
    assert events[1][2] == []
    assert stopped.result["gpu_oc_reset"]["state_cleared"] is True
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {"version": 2, "devices": []}


def test_stop_reports_nvml_reset_failure_after_miner_is_already_stopped(
    tmp_path: Path,
):
    events = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            assert (index, expected_uuid) == (0, "GPU-stop-failure")

        def resolve_device_indices(self, expected_uuids):
            return {"GPU-stop-failure": 0}

        def transition(self, previous, current):
            if current:
                return {"vendor": "nvidia", "operations": [], "reset": []}
            events.append("reset")
            raise RuntimeError("simulated NVML reset failure")

    class Manager:
        def stop(self):
            events.append("stop")
            return {"stopped": True}

    cfg = config(tmp_path)
    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda _config: Manager(),
    )
    assert executor.execute(Command("apply", "apply_gpu_profile", {
        "gpu_profile_id": "device-oc-stop-failure",
        "name": "stop failure",
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 120}],
        "mode": "replace_one",
        "expected_gpu_uuid": "GPU-stop-failure",
        "previous_index": 0,
    })).status == "succeeded"

    stopped = executor.execute(Command("stop", "stop_mining", {}))

    assert stopped.status == "failed"
    assert stopped.error == "simulated NVML reset failure"
    assert stopped.result["stopped"] is True
    assert stopped.result["gpu_oc_reset"] == {
        "status": "failed",
        "state_cleared": False,
        "error": "simulated NVML reset failure",
    }
    assert events == ["stop", "reset"]
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["devices"][0]["setting"]["power_limit_w"] == 120.0


def test_stop_rejects_v1_state_without_any_nvml_write_and_preserves_state(
    tmp_path: Path,
):
    transitions = []

    class Backend:
        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    class Manager:
        def stop(self):
            return {"stopped": True}

    cfg = config(tmp_path)
    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda _config: Manager(),
    )
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": 1, "settings": gpu_payload()["settings"]}),
        encoding="utf-8",
    )
    before = state_path.read_bytes()

    stopped = executor.execute(Command("stop", "stop_mining", {}))

    assert stopped.status == "failed"
    assert "complete UUID-owned state" in (stopped.error or "")
    assert transitions == []
    assert state_path.read_bytes() == before


def test_merge_one_migrates_only_a_single_verified_v1_setting(tmp_path: Path):
    transitions = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            assert (index, expected_uuid) == (0, "GPU-verified")

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    cfg = config(tmp_path)
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": [{"index": 0, "power_limit_w": 120}],
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "gpu_profile_id": "device-oc-safe-v1",
        "name": "safe migration",
        "vendor": "nvidia",
        "settings": [{"index": 0, "fan_percent": 65}],
        "mode": "merge_one",
        "expected_gpu_uuid": "GPU-verified",
    }

    outcome = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command("safe-v1", "apply_gpu_profile", payload)
    )

    assert outcome.status == "succeeded", outcome.error
    assert transitions == [[
        [{"index": 0, "power_limit_w": 120.0}],
        [{"index": 0, "power_limit_w": 120.0, "fan_percent": 65}],
    ]]
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-verified",
                "index_hint": 0,
                "setting": {"power_limit_w": 120.0, "fan_percent": 65},
            }
        ],
    }


def test_merge_one_rejects_ambiguous_v1_without_nvml_write(tmp_path: Path):
    transitions = []

    class Backend:
        def verify_device_uuid(self, _index, _expected_uuid):
            return None

        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    cfg = config(tmp_path)
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": [
                    {"index": 0, "power_limit_w": 120},
                    {"index": 1, "power_limit_w": 130},
                ],
            }
        ),
        encoding="utf-8",
    )
    before = state_path.read_bytes()

    outcome = CommandExecutor(cfg, gpu_backend_factory=Backend).execute(
        Command(
            "ambiguous-v1",
            "apply_gpu_profile",
            device_gpu_payload(gpu_uuid="GPU-unknown"),
        )
    )

    assert outcome.status == "failed"
    assert "cannot safely bind" in (outcome.error or "")
    assert transitions == []
    assert state_path.read_bytes() == before


def test_stop_migrates_empty_v1_state_to_empty_v2(tmp_path: Path):
    class Manager:
        def stop(self):
            return {"stopped": True}

    cfg = config(tmp_path)
    state_path = tmp_path / "miner" / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": 1, "settings": []}),
        encoding="utf-8",
    )
    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=lambda: (_ for _ in ()).throw(
            AssertionError("empty state must not access NVML")
        ),
        managed_miner_factory=lambda _config: Manager(),
    )

    outcome = executor.execute(Command("stop-empty-v1", "stop_mining", {}))

    assert outcome.status == "succeeded", outcome.error
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "devices": [],
    }


def test_saved_gpu_profile_remains_a_complete_replacement_after_device_oc(
    tmp_path: Path,
):
    transitions = []

    class Backend:
        def verify_device_uuid(self, _index, expected_uuid):
            return expected_uuid

        def enumerate_device_indices(self):
            return {"GPU-first": 0, "GPU-saved": 2}

        def transition(self, previous, current):
            transitions.append(
                json.loads(json.dumps((previous, current)))
            )
            return {"vendor": "nvidia", "operations": [], "reset": []}

    cfg = config(tmp_path)
    executor = CommandExecutor(cfg, gpu_backend_factory=Backend)
    per_device = device_gpu_payload(gpu_uuid="GPU-first")
    replacement = gpu_payload(index=2, power_limit_w=150)
    replacement["gpu_profile_id"] = "saved-profile"

    assert executor.execute(
        Command("device", "apply_gpu_profile", per_device)
    ).status == "succeeded"
    assert executor.execute(
        Command("saved", "apply_gpu_profile", replacement)
    ).status == "succeeded"

    current = transitions[1][1]
    assert transitions[1][0] == transitions[0][1]
    assert current[0]["index"] == 2
    state = json.loads(
        (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-saved",
                "index_hint": 2,
                "setting": {
                    key: value for key, value in current[0].items()
                    if key != "index"
                },
            }
        ],
    }


def test_gpu_oc_state_write_failure_does_not_replay_hardware(
    tmp_path: Path,
    monkeypatch,
):
    transitions = []

    class Backend:
        def verify_device_uuid(self, _index, expected_uuid):
            return expected_uuid

        def transition(self, previous, current):
            transitions.append(
                json.loads(json.dumps((previous, current)))
            )
            return {"vendor": "nvidia", "operations": [], "reset": []}

    monkeypatch.setattr(
        "agent.commands._write_gpu_oc_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated state write failure")
        ),
    )
    payload = device_gpu_payload(gpu_uuid="GPU-state-write")
    outcome = CommandExecutor(
        config(tmp_path),
        gpu_backend_factory=Backend,
    ).execute(Command("state-write-failure", "apply_gpu_profile", payload))

    assert outcome.status == "failed"
    assert "simulated state write failure" in (outcome.error or "")
    expected = payload["settings"]
    assert transitions == [[[], expected]]
    recovery = outcome.result["gpu_oc_recovery"]
    assert recovery["persistence_failed"] is True
    assert recovery["state_reconciled"] is False
    assert recovery["recovered"] is False
    # This fake only reports acceptance, not actual NVML writes.
    assert recovery["hardware_state"] == "unknown"
    assert recovery["write_attempted"] is False


def test_per_device_gpu_oc_requires_and_verifies_uuid_before_transition(
    tmp_path: Path,
):
    transitions = []

    class Backend:
        def verify_device_uuid(self, _index, _expected_uuid):
            raise RuntimeError("GPU identity mismatch")

        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    cfg = config(tmp_path)
    executor = CommandExecutor(cfg, gpu_backend_factory=Backend)
    missing_identity = gpu_payload()
    missing_identity["gpu_profile_id"] = "device-oc-legacy"
    rejected = executor.execute(
        Command("missing-identity", "apply_gpu_profile", missing_identity)
    )
    assert rejected.status == "failed"
    assert "payload fields" in (rejected.error or "")

    mismatched = executor.execute(
        Command(
            "wrong-card",
            "apply_gpu_profile",
            device_gpu_payload(gpu_uuid="GPU-expected"),
        )
    )
    assert mismatched.status == "failed"
    assert "identity mismatch" in (mismatched.error or "")
    assert transitions == []
    assert not (tmp_path / "miner" / "rigpilot-gpu-oc-state.json").exists()


def test_dual_mining_checks_secondary_local_allowlists(tmp_path: Path):
    cfg = config(
        tmp_path,
        miner_profile_allowlist=frozenset({"xmrig-safe", "gpu-safe"}),
        flight_sheet_extra_key_allowlist=frozenset({"dual_intensity"}),
    )
    outcome = CommandExecutor(cfg).execute(
        Command("1", "apply_flight_sheet", extended_flight_payload())
    )
    assert outcome.status == "succeeded", outcome.error
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["version"] == 3
    assert stored["secondary"]["miner_profile"] == "gpu-safe"
    assert stored["secondary"]["extra_config"] == {"dual_intensity": 4}

    rejected_profile = extended_flight_payload()
    rejected_profile["secondary"]["miner_profile"] = "remote-not-allowed"
    assert (
        CommandExecutor(cfg)
        .execute(Command("2", "apply_flight_sheet", rejected_profile))
        .status
        == "failed"
    )
    rejected_outcome = CommandExecutor(cfg).execute(
        Command("2-detail", "apply_flight_sheet", rejected_profile)
    )
    assert rejected_outcome.error == (
        "secondary.miner_profile is not in the local allowlist; "
        "allowed values: gpu-safe, xmrig-safe"
    )
    rejected_extra = extended_flight_payload()
    rejected_extra["secondary"]["extra_config"] = {"shell": "never"}
    assert (
        CommandExecutor(cfg)
        .execute(Command("3", "apply_flight_sheet", rejected_extra))
        .status
        == "failed"
    )


def test_flight_sheet_installs_packages_but_never_executes_them(tmp_path: Path):
    calls = []
    package = {
        "id": "generic-gpu",
        "name": "Generic GPU",
        "version": "1.99",
        "download_url": "https://downloads.example.test/generic-gpu.zip",
        "sha256": "a" * 64,
        "archive_type": "zip",
    }

    def installer(metadata, cfg):
        calls.append((metadata, cfg))
        target = tmp_path / "packages" / f"{metadata['id']}-{metadata['version']}"
        target.mkdir(parents=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(target),
            "sha256": metadata["sha256"],
            "downloaded": True,
        }

    def no_processes(*args, **kwargs):
        raise AssertionError("a package application without miner_service must run no process")

    cfg = config(
        tmp_path,
        miner_profile_allowlist=frozenset({"xmrig-safe", "gpu-safe"}),
        flight_sheet_extra_key_allowlist=frozenset({"dual_intensity"}),
        allow_miner_downloads=True,
        miner_packages_dir=tmp_path / "packages",
        miner_download_host_allowlist=frozenset({"downloads.example.test"}),
    )
    payload = extended_flight_payload()
    payload["primary"]["miner_package"] = package
    payload["secondary"]["miner_package"] = {**package, "id": "generic-gpu-secondary"}
    outcome = CommandExecutor(
        cfg, runner=no_processes, package_installer=installer
    ).execute(Command("1", "apply_flight_sheet", payload))
    assert outcome.status == "succeeded", outcome.error
    assert [item["role"] for item in outcome.result["miner_packages"]] == [
        "primary",
        "secondary",
    ]
    assert len(calls) == 2
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["primary"]["miner_package"]["installed_path"].startswith(
        str(tmp_path)
    )
    assert stored["secondary"]["miner_package"]["installed_path"].startswith(
        str(tmp_path)
    )


def test_package_metadata_requires_download_opt_in_but_not_a_hash_pin(tmp_path: Path):
    package = {
        "id": "miner",
        "name": "Miner",
        "version": "1.0",
        "download_url": "https://downloads.example.test/miner.zip",
        "sha256": "a" * 64,
        "archive_type": "zip",
    }
    payload = flight_payload(primary_overrides={"miner_package": package})
    outcome = CommandExecutor(config(tmp_path)).execute(
        Command("1", "apply_flight_sheet", payload)
    )
    assert outcome.status == "failed"
    assert "disabled locally" in (outcome.error or "")

    enabled_but_unpinned = config(
        tmp_path,
        allow_miner_downloads=True,
        miner_packages_dir=tmp_path / "packages",
        miner_download_host_allowlist=frozenset({"downloads.example.test"}),
    )
    def installer(metadata, cfg):
        target = tmp_path / "packages" / "miner"
        target.mkdir(parents=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(target),
            "sha256": metadata.get("sha256"),
            "downloaded": True,
        }

    unpinned = CommandExecutor(
        enabled_but_unpinned, package_installer=installer
    ).execute(
        Command("2", "apply_flight_sheet", payload)
    )
    assert unpinned.status == "succeeded", unpinned.error


def test_controller_hosted_package_does_not_need_manual_hash_pin(tmp_path: Path):
    package = {
        "id": "miner",
        "name": "Miner",
        "version": "1.0",
        "download_url": "/api/agent/miner-packages/miner/download",
        "archive_type": "zip",
    }
    payload = flight_payload(primary_overrides={"miner_package": package})
    calls = []

    def installer(metadata, cfg, **kwargs):
        calls.append((metadata, kwargs))
        target = tmp_path / "packages/miner"
        target.mkdir(parents=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(target),
            "sha256": metadata["sha256"],
            "downloaded": True,
        }

    cfg = config(
        tmp_path,
        allow_miner_downloads=True,
        miner_packages_dir=tmp_path / "packages",
        miner_download_host_allowlist=frozenset(),
    )
    state = AgentState.new("agent-id", "agent-secret")
    outcome = CommandExecutor(cfg, package_installer=installer).execute(
        Command("controller", "apply_flight_sheet", payload), agent_state=state
    )

    assert outcome.status == "succeeded", outcome.error
    assert calls[0][0]["download_url"] == (
        "https://example.test/api/agent/miner-packages/miner/download"
    )
    assert calls[0][1] == {"state": state}


def _contract_mining_config(tmp_path: Path):
    return config(
        tmp_path,
        managed_mining_enabled=True,
        managed_miner_runtime_dir=tmp_path / "runtime",
        allow_miner_downloads=True,
        miner_packages_dir=tmp_path / "packages",
    )


def _contract_srb_leg(*, version="3.7.1", algorithm="quantus", **overrides):
    package_id = f"builtin-srbminer-{version}-linux-x64"
    leg = mining_leg(
        miner_type="srbminer",
        miner_profile="default",
        coin="QUANTUS" if algorithm == "quantus" else "XMR" if algorithm == "randomx" else "TEST",
        algorithm=algorithm,
        miner_package={
            "id": package_id,
            "name": "SRBMiner",
            "version": version,
            "download_url": f"/api/agent/miner-packages/{package_id}/download",
            "archive_type": "tar.gz",
        },
        extra_config={},
    )
    leg.update(overrides)
    return leg


@pytest.mark.parametrize("url", [
    "\nstratum+tcp://pool.example.test:3333",
    "stratum+tcp://pool.example.test:3333\n",
    "stratum+tcp://po\x7fol.example.test:3333",
    "stratum+tcp://@pool.example.test:3333",
])
def test_pool_shared_parser_rejects_raw_controls_and_empty_userinfo(tmp_path: Path, url):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    with pytest.raises(MiningCommandError):
        validate_flight_sheet(flight_payload(primary_overrides={"pool_url": url}), config(tmp_path))


def test_quantus_quic_uses_typed_opaque_pin_and_existing_secret_field(tmp_path: Path):
    from agent.mining_commands import validate_flight_sheet

    pin = "provider-encoded/pin+value=:opaque"
    leg = _contract_srb_leg(
        version="3.6.4", pool_url="quic://node.example.test:18181",
        pool_password="private-node-auth-token", wallet_address="public-payment-label",
        extra_config={"tls_cert_sha256": pin, "quic_wallet_mode": "token"},
    )
    result = validate_flight_sheet(flight_payload(primary=leg), _contract_mining_config(tmp_path))
    assert result["primary"]["pool_password"] == "private-node-auth-token"
    assert result["primary"]["wallet_address"] == "public-payment-label"
    assert result["primary"]["extra_config"]["tls_cert_sha256"] == pin
    assert "execution_mode" not in result["primary"]


@pytest.mark.parametrize("changes", [
    {"extra_config": {}},
    {"pool_password": ""},
    {"algorithm": "randomx", "coin": "XMR"},
])
def test_quantus_quic_rejects_missing_pin_secret_or_wrong_algorithm(tmp_path: Path, changes):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    leg = _contract_srb_leg(
        pool_url="quic://node.example.test:18181", pool_password="node-token",
        extra_config={"tls_cert_sha256": "opaque-pin", "quic_wallet_mode": "token"},
    )
    leg.update(changes)
    with pytest.raises(MiningCommandError):
        validate_flight_sheet(flight_payload(primary=leg), _contract_mining_config(tmp_path))


@pytest.mark.parametrize("algorithm,version", [("quantus", "3.6.3"), ("walahash", "3.5.8")])
def test_srb_actual_algorithm_version_bounds_are_consumed(tmp_path: Path, algorithm, version):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    with pytest.raises(MiningCommandError):
        validate_flight_sheet(
            flight_payload(primary=_contract_srb_leg(algorithm=algorithm, version=version)),
            _contract_mining_config(tmp_path),
        )


def test_srb_compatible_legacy_version_and_nullable_native_shape_remain_valid(tmp_path: Path):
    from agent.mining_commands import validate_flight_sheet

    old = validate_flight_sheet(
        flight_payload(primary=_contract_srb_leg(algorithm="walahash", version="3.5.7")),
        _contract_mining_config(tmp_path),
    )
    assert old["primary"]["algorithm"] == "walahash"
    leg = _contract_srb_leg(
        algorithm="randomx", device_indices=[],
        extra_config={"cpu_threads_intensity": None, "resource_kind": None, "tls_cert_sha256": None},
    )
    result = validate_flight_sheet(flight_payload(primary=leg), _contract_mining_config(tmp_path))
    assert result["primary"]["extra_config"] == leg["extra_config"]
    assert "execution_mode" not in result["primary"]


def test_srb_effective_gpu_resource_is_used_for_wsl_and_flight_oc(tmp_path: Path):
    from agent.mining_commands import validate_flight_sheet

    leg = _contract_srb_leg(algorithm="yespower", extra_config={"resource_kind": "gpu"})
    wsl = validate_flight_sheet(
        flight_payload(primary={**leg, "execution_mode": "wsl"}), _contract_mining_config(tmp_path)
    )
    assert wsl["primary"]["execution_mode"] == "wsl"
    oc = {"vendor": "nvidia", "settings": [{"index": 0, "core_clock_offset_mhz": 100}]}
    native = validate_flight_sheet(flight_payload(primary=leg, gpu_oc=oc), _contract_mining_config(tmp_path))
    assert native["gpu_oc"]["settings"][0]["core_clock_offset_mhz"] == 100


def test_srb_gpu_resource_override_cannot_bypass_independent_device_overlap(tmp_path: Path):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    primary = _contract_srb_leg(algorithm="yespower", extra_config={"resource_kind": "gpu"})
    secondary = _contract_srb_leg(device_indices=[0])
    with pytest.raises(MiningCommandError, match="disjoint"):
        validate_flight_sheet(
            flight_payload(primary=primary, secondary=secondary, secondary_mode="independent"),
            _contract_mining_config(tmp_path),
        )


def test_srb_gpu_resource_override_supports_independent_gpu_plus_cpu_oc(tmp_path: Path):
    from agent.mining_commands import validate_flight_sheet

    primary = _contract_srb_leg(algorithm="yespower", extra_config={"resource_kind": "gpu"})
    secondary = _contract_srb_leg(algorithm="randomx", device_indices=[])
    oc = {"vendor": "nvidia", "settings": [{"index": 0, "core_clock_offset_mhz": 100}]}
    result = validate_flight_sheet(
        flight_payload(primary=primary, secondary=secondary, secondary_mode="independent", gpu_oc=oc),
        _contract_mining_config(tmp_path),
    )
    assert result["version"] == 4 and result["secondary_mode"] == "independent"
    assert result["primary"]["extra_config"] == {"resource_kind": "gpu"}


def test_srb_cpu_only_algorithm_rejects_gpu_resource_override(tmp_path: Path):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    with pytest.raises(MiningCommandError, match="resource"):
        validate_flight_sheet(
            flight_payload(primary=_contract_srb_leg(algorithm="randomx", extra_config={"resource_kind": "gpu"})),
            _contract_mining_config(tmp_path),
        )


def test_quantus_gpu_dual_pair_is_not_invented(tmp_path: Path):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    with pytest.raises(MiningCommandError, match="pair"):
        validate_flight_sheet(
            flight_payload(primary=_contract_srb_leg(), secondary=_contract_srb_leg(algorithm="ethash")),
            _contract_mining_config(tmp_path),
        )


@pytest.mark.parametrize("mode", ["omitted", None, "wallet"])
def test_quantus_public_quic_does_not_require_or_infer_node_token(tmp_path: Path, mode):
    from agent.mining_commands import validate_flight_sheet

    options = {"tls_cert_sha256": "opaque-public-pool-pin"}
    if mode != "omitted":
        options["quic_wallet_mode"] = mode
    leg = _contract_srb_leg(
        pool_url="quic://public-pool.example.test:443",
        pool_password="", wallet_address="public-payment-address", extra_config=options,
    )
    result = validate_flight_sheet(flight_payload(primary=leg), _contract_mining_config(tmp_path))
    assert result["primary"]["wallet_address"] == "public-payment-address"
    assert result["primary"]["pool_password"] == ""
    assert result["primary"]["extra_config"] == options


def test_shared_known_first_version_is_not_limited_to_quantus(tmp_path: Path):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    with pytest.raises(MiningCommandError):
        validate_flight_sheet(
            flight_payload(primary=_contract_srb_leg(algorithm="yespowermwc", version="3.1.0")),
            _contract_mining_config(tmp_path),
        )
    result = validate_flight_sheet(
        flight_payload(primary=_contract_srb_leg(algorithm="yespowermwc", version="3.1.1")),
        _contract_mining_config(tmp_path),
    )
    assert result["primary"]["miner_package"]["version"] == "3.1.1"


def test_srb_effective_resources_govern_integrated_dual_and_cpu_choices(tmp_path: Path):
    from agent.mining_commands import MiningCommandError, validate_flight_sheet

    secondary = _contract_srb_leg(algorithm="randomx", device_indices=[])
    selected_gpu = _contract_srb_leg(algorithm="yespower", extra_config={"resource_kind": "gpu"})
    accepted = validate_flight_sheet(
        flight_payload(primary=selected_gpu, secondary=secondary), _contract_mining_config(tmp_path)
    )
    assert accepted["version"] == 3 and "secondary_mode" not in accepted
    with pytest.raises(MiningCommandError):
        validate_flight_sheet(
            flight_payload(primary={**selected_gpu, "extra_config": {"resource_kind": "cpu"}}, secondary=secondary),
            _contract_mining_config(tmp_path),
        )
    with pytest.raises(MiningCommandError):
        validate_flight_sheet(
            flight_payload(primary=_contract_srb_leg(algorithm="randomx", extra_config={"cpu_threads_intensity": True})),
            _contract_mining_config(tmp_path),
        )


def test_nullable_algorithm_and_explicit_opaque_x_node_token_are_not_rewritten(tmp_path: Path):
    from agent.mining_commands import validate_flight_sheet

    leg = _contract_srb_leg(algorithm=None, coin="XMR", device_indices=[])
    normalized = validate_flight_sheet(flight_payload(primary=leg), _contract_mining_config(tmp_path))
    assert normalized["primary"]["algorithm"] is None
    token = _contract_srb_leg(
        pool_url="quic://node.example.test:18181", pool_password="x",
        extra_config={"tls_cert_sha256": "opaque-pin", "quic_wallet_mode": "token"},
    )
    normalized = validate_flight_sheet(flight_payload(primary=token), _contract_mining_config(tmp_path))
    assert normalized["primary"]["pool_password"] == "x"


def test_srb_disjoint_gpu_gpu_independent_contract_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from agent.managed_miners import ManagedMinerManager
    from agent.mining_commands import validate_flight_sheet

    cfg = _contract_mining_config(tmp_path)
    primary = _contract_srb_leg(version="3.7.2", algorithm="pearlhash",
                                coin="PRL", device_indices=[0])
    secondary = _contract_srb_leg(version="3.7.2", algorithm="pearlhash",
                                  coin="PRL", device_indices=[1])
    document = validate_flight_sheet(
        flight_payload(primary=primary, secondary=secondary,
                       secondary_mode="independent", gpu_oc=None), cfg,
    )
    manager = ManagedMinerManager(cfg, system_name="Linux")
    accepted = []
    monkeypatch.setattr(manager, "_apply_independent",
                        lambda value: accepted.append(value) or {"accepted": True})
    monkeypatch.setattr(manager, "_prepare_single",
                        lambda *_args, **_kwargs: pytest.fail("unexpected single-miner preparation"))
    assert manager.apply(document) == {"accepted": True}
    assert accepted == [document]
    assert [document[role]["device_indices"] for role in ("primary", "secondary")] == [[0], [1]]
