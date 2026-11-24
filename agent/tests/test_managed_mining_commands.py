from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os

import pytest

from agent.commands import Command, CommandExecutor
from agent.config import AgentConfig
from agent.managed_miners import (
    FLIGHT_RUNTIME_FINGERPRINT_FIELD,
    ManagedMinerError,
    ManagedMinerTransitionError,
)
from agent.mining_commands import MiningCommandError, validate_flight_sheet


def config(tmp_path: Path, **overrides) -> AgentConfig:
    values = {
        "server_url": "https://example.test",
        "state_file": tmp_path / "state.json",
        "flight_sheet_file": tmp_path / "flight.json",
        "miner_status_file": tmp_path / "runtime" / "miner-status.json",
        "managed_mining_enabled": True,
        "managed_miner_runtime_dir": tmp_path / "runtime",
        "allow_miner_downloads": True,
        "miner_packages_dir": tmp_path / "packages",
        "miner_download_host_allowlist": frozenset({"downloads.example.test"}),
    }
    values.update(overrides)
    return AgentConfig(**values)


def _package_installer(tmp_path: Path, package_name: str | None = None):
    """Model reusable installed metadata without downloading or executing bytes."""
    def install(metadata, _config, **_kwargs):
        installed = tmp_path / "packages" / (package_name or str(metadata["id"]))
        installed.mkdir(parents=True, exist_ok=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(installed),
            "sha256": None,
            "downloaded": True,
        }
    return install


def package(package_id="builtin-xmrig-6.22.3"):
    return {
        "id": package_id,
        "name": "XMRig",
        "version": "6.22.3",
        "download_url": "https://downloads.example.test/xmrig.zip",
        "archive_type": "zip",
    }


def payload(**primary_overrides):
    primary = {
        "coin": "XMR",
        "wallet_address": "48A-public-wallet",
        "pool_url": "stratum+ssl://pool.example.test:443",
        "miner_profile": "default",
        "miner_type": "xmrig",
        "miner_package": package(),
        "extra_config": {
            "cpu_threads": None,
            "cpu_max_threads_hint": 75,
            "huge_pages": True,
            "randomx_1gb_pages": False,
            "tls": True,
            "keepalive": True,
            "pause_on_battery": False,
        },
        "algorithm": "randomx",
        "pool_password": "x",
        "device_indices": [],
    }
    primary.update(primary_overrides)
    return {
        "flight_sheet_id": "managed-sheet",
        "name": "XMR",
        "worker_name": "rig-01",
        "primary": primary,
        "secondary": None,
    }


def gpu_oc(power_limit_w=120):
    return {
        "vendor": "nvidia",
        "settings": [
            {
                "index": 0,
                "power_limit_w": power_limit_w,
                "core_clock_offset_mhz": 100,
                "memory_clock_offset_mhz": 800,
            }
        ],
    }


def srb_payload(*, oc=None):
    document = payload(
        miner_type="srbminer",
        coin="ETC",
        miner_package=package("builtin-srbminer-3.4.6"),
        algorithm="etchash",
        device_indices=[0],
        extra_config={
            "cpu_threads": None,
            "huge_pages": True,
            "randomx_1gb_pages": False,
            "tls": True,
            "keepalive": True,
            "extended_log": False,
        },
    )
    document["gpu_oc"] = gpu_oc() if oc is None else oc
    return document


def pearl_payload():
    return payload(
        miner_type="pearlminer",
        coin="PEARL",
        miner_package={
            **package("builtin-pearlminer-2.0.3-linux-x64-nvidia"),
            "name": "Pearl Miner",
            "version": "2.0.3",
        },
        algorithm="pearlhash",
        pool_url="pearl://pool.example.test:4444",
        extra_config={"gpu_backend": "nvidia", "cuda_variant": "13"},
    )


def srb_gpu_payload():
    return payload(
        miner_type="srbminer",
        coin="ETC",
        miner_package={
            **package("builtin-srbminer-3.7.2-windows-x64"),
            "name": "SRBMiner-MULTI",
            "version": "3.7.2",
        },
        algorithm="etchash",
        extra_config={"tls": True},
    )


_ADAPTER_METADATA_KEYS = {
    "abi_version",
    "adapter_id",
    "adapter_revision",
    "capabilities",
    "options",
    "package_namespaces",
    "resource_kinds",
    "runtime_id",
    "telemetry_id",
}
_LEGACY_MANAGED_LEG_KEYS = {
    "algorithm",
    "coin",
    "device_indices",
    "extra_config",
    "miner_package",
    "miner_profile",
    "miner_type",
    "pool_password",
    "pool_url",
    "wallet_address",
}


def _assert_no_adapter_metadata(value):
    if isinstance(value, dict):
        assert not (_ADAPTER_METADATA_KEYS & set(value))
        for item in value.values():
            _assert_no_adapter_metadata(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_adapter_metadata(item)


@pytest.mark.parametrize(
    ("factory", "allow_gpu_oc"),
    ((payload, False), (srb_payload, True), (pearl_payload, False)),
    ids=("xmrig-native", "srbminer-native-oc", "pearlminer-native"),
)
def test_builtin_adapter_native_wire_remains_legacy_golden(
    tmp_path: Path,
    factory,
    allow_gpu_oc: bool,
):
    original = factory()
    validated = validate_flight_sheet(
        original,
        config(tmp_path, allow_gpu_oc=allow_gpu_oc),
    )

    assert set(validated) == {
        "flight_sheet_id",
        "gpu_oc",
        "name",
        "primary",
        "secondary",
        "version",
        "worker_name",
    }
    assert set(validated["primary"]) == _LEGACY_MANAGED_LEG_KEYS
    assert validated["primary"]["extra_config"] == original["primary"]["extra_config"]
    assert "execution_mode" not in validated["primary"]
    _assert_no_adapter_metadata(validated)


def test_independent_xmrig_and_srb_gpu_are_classified_from_adapter_resources(
    tmp_path: Path,
):
    value = payload()
    value["secondary_mode"] = "independent"
    value["secondary"] = srb_gpu_payload()["primary"]

    validated = validate_flight_sheet(value, config(tmp_path))

    assert validated["primary"]["miner_type"] == "xmrig"
    assert validated["secondary"]["miner_type"] == "srbminer"
    assert validated["secondary"]["algorithm"] == "etchash"


def test_srbminer_integrated_dual_oc_wire_remains_legacy_golden(tmp_path: Path):
    original = srb_payload()
    secondary = json.loads(json.dumps(original["primary"]))
    secondary.update(
        {
            "coin": "XMR",
            "algorithm": "randomx",
            "device_indices": [],
        }
    )
    original["secondary"] = secondary

    validated = validate_flight_sheet(
        original,
        config(tmp_path, allow_gpu_oc=True),
    )

    assert validated["version"] == 3
    assert "secondary_mode" not in validated
    assert set(validated["primary"]) == _LEGACY_MANAGED_LEG_KEYS
    assert set(validated["secondary"]) == _LEGACY_MANAGED_LEG_KEYS
    assert validated["gpu_oc"] == {
        "vendor": "nvidia",
        "settings": [
            {
                "index": 0,
                "power_limit_w": 120.0,
                "core_clock_offset_mhz": 100,
                "memory_clock_offset_mhz": 800,
            }
        ],
    }
    _assert_no_adapter_metadata(validated)


def test_pearl_flight_validates_backend_and_disallows_secondary_or_gpu_ids(
    tmp_path: Path,
):
    cfg = config(tmp_path)
    valid = validate_flight_sheet(pearl_payload(), cfg)
    assert valid["primary"]["miner_package"]["version"] == "2.0.3"
    assert valid["primary"]["extra_config"] == {
        "gpu_backend": "nvidia",
        "cuda_variant": "13",
    }

    with_gpu_id = pearl_payload()
    with_gpu_id["primary"]["device_indices"] = [0]
    with pytest.raises(MiningCommandError, match="GPU index selector"):
        validate_flight_sheet(with_gpu_id, cfg)

    invalid_backend = pearl_payload()
    invalid_backend["primary"]["extra_config"]["gpu_backend"] = "intel"
    with pytest.raises(MiningCommandError, match="auto, nvidia, or amd"):
        validate_flight_sheet(invalid_backend, cfg)

    dual = pearl_payload()
    dual["secondary"] = dict(dual["primary"])
    with pytest.raises(MiningCommandError, match="does not support a secondary"):
        validate_flight_sheet(dual, cfg)


@pytest.mark.parametrize("pool_url", [
    "stratum+ssl://pool.example.test:443",
    "stratum+tcp://pool.example.test:3333",
])
def test_pearl_flight_accepts_stratum_pool_without_version_gate(tmp_path: Path, pool_url: str):
    document = pearl_payload()
    document["primary"]["pool_url"] = pool_url
    document["primary"]["miner_package"]["version"] = "2.0.3"

    validated = validate_flight_sheet(document, config(tmp_path))

    assert validated["primary"]["pool_url"] == pool_url
    assert validated["primary"]["miner_package"]["version"] == "2.0.3"


def test_wsl_mode_is_explicit_gpu_only_and_native_remains_legacy_compatible(
    tmp_path: Path,
):
    cfg = config(tmp_path)
    native = validate_flight_sheet(pearl_payload(), cfg)
    assert "execution_mode" not in native["primary"]
    _assert_no_adapter_metadata(native)

    wsl = pearl_payload()
    wsl["primary"]["execution_mode"] = "wsl"
    wsl["primary"]["miner_package"]["managed_os_family"] = "linux"
    validated = validate_flight_sheet(wsl, cfg)
    assert validated["primary"]["execution_mode"] == "wsl"
    assert validated["primary"]["miner_package"]["managed_os_family"] == "linux"
    assert set(validated["primary"]) == _LEGACY_MANAGED_LEG_KEYS | {
        "execution_mode"
    }
    _assert_no_adapter_metadata(validated)

    cpu = payload(execution_mode="wsl")
    cpu["primary"]["miner_package"]["managed_os_family"] = "linux"
    with pytest.raises(MiningCommandError, match="GPU miners"):
        validate_flight_sheet(cpu, cfg)

    srb = srb_payload()
    srb["primary"]["execution_mode"] = "wsl"
    srb["primary"]["miner_package"]["managed_os_family"] = "linux"
    with pytest.raises(MiningCommandError, match="host NVML"):
        validate_flight_sheet(srb, config(tmp_path, allow_gpu_oc=True))


def test_independent_native_cpu_and_wsl_gpu_is_allowed(tmp_path: Path):
    cfg = config(tmp_path)
    document = payload()
    document["secondary_mode"] = "independent"
    document["secondary"] = pearl_payload()["primary"]
    document["secondary"]["execution_mode"] = "wsl"
    document["secondary"]["miner_package"]["managed_os_family"] = "linux"

    validated = validate_flight_sheet(document, cfg)

    assert validated["secondary_mode"] == "independent"
    assert validated["primary"].get("execution_mode", "native") == "native"
    assert validated["secondary"]["execution_mode"] == "wsl"
    _assert_no_adapter_metadata(validated)


def test_independent_pearl_and_xmrig_are_two_managed_legs(tmp_path: Path):
    cfg = config(tmp_path)
    document = pearl_payload()
    document["secondary_mode"] = "independent"
    document["secondary"] = payload()["primary"]

    validated = validate_flight_sheet(document, cfg)

    assert validated["version"] == 4
    assert validated["secondary_mode"] == "independent"
    assert validated["primary"]["miner_type"] == "pearlminer"
    assert validated["secondary"]["miner_type"] == "xmrig"
    _assert_no_adapter_metadata(validated)

    reversed_document = payload()
    reversed_document["secondary_mode"] = "independent"
    reversed_document["secondary"] = pearl_payload()["primary"]
    reversed_validated = validate_flight_sheet(reversed_document, cfg)
    assert reversed_validated["primary"]["miner_type"] == "xmrig"
    assert reversed_validated["secondary"]["miner_type"] == "pearlminer"


def test_independent_primary_srb_gpu_oc_is_owned_only_by_primary_role(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    document = srb_payload(
        oc={
            "vendor": "nvidia",
            "settings": [
                {"model_match": "RTX 5080", "power_limit_w": 250}
            ],
        }
    )
    document["primary"]["device_indices"] = []
    document["secondary_mode"] = "independent"
    document["secondary"] = payload()["primary"]

    validated = validate_flight_sheet(document, cfg)

    assert validated["version"] == 4
    assert validated["gpu_oc"] == document["gpu_oc"]
    assert validated["primary"]["miner_type"] == "srbminer"
    assert validated["secondary"]["miner_type"] == "xmrig"
    _assert_no_adapter_metadata(validated)

    srb_cpu_document = json.loads(json.dumps(document))
    srb_cpu_document["secondary"] = srb_payload(oc=None)["primary"]
    srb_cpu_document["secondary"].update(
        {
            "coin": "XMR",
            "algorithm": "randomx",
            "device_indices": [],
        }
    )
    srb_cpu_validated = validate_flight_sheet(srb_cpu_document, cfg)
    assert srb_cpu_validated["gpu_oc"] == document["gpu_oc"]
    assert srb_cpu_validated["secondary"]["miner_type"] == "srbminer"
    assert srb_cpu_validated["secondary"]["algorithm"] == "randomx"

    ambiguous = json.loads(json.dumps(document))
    ambiguous["primary"]["device_indices"] = [0]
    ambiguous["secondary"] = {
        **srb_payload(oc=None)["primary"],
        "device_indices": [1],
    }
    with pytest.raises(MiningCommandError, match="CPU-only second miner"):
        validate_flight_sheet(ambiguous, cfg)


def test_runtime_fingerprint_is_validated_and_preserved(tmp_path: Path):
    cfg = config(tmp_path)
    document = payload()
    fingerprint = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    document[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = fingerprint

    validated = validate_flight_sheet(document, cfg)
    assert validated[FLIGHT_RUNTIME_FINGERPRINT_FIELD] == fingerprint

    tampered = json.loads(json.dumps(document))
    tampered["worker_name"] = "other-worker"
    with pytest.raises(MiningCommandError, match="does not match"):
        validate_flight_sheet(tampered, cfg)


@pytest.mark.parametrize("pair", [
    ("xmrig", "seine"), ("seine", "xmrig"),
    ("xmrig", "xmrig"), ("seine", "seine"), ("xmrig", "srbminer"),
])
def test_independent_dual_accepts_cpu_miners_preserving_role_threads(tmp_path: Path, pair):
    cfg = config(tmp_path)
    document = payload()
    document["secondary_mode"] = "independent"
    for role, miner_type, threads in zip(("primary", "secondary"), pair, (None, 7)):
        version = {"xmrig": "6.22.3", "seine": "0.2.15", "srbminer": "3.4.6"}[miner_type]
        document[role] = payload(
            miner_type=miner_type,
            miner_package={**package(f"builtin-{miner_type}-{version}"), "version": version},
            coin="BNT" if miner_type == "seine" else "XMR",
            algorithm="blocknet-argon2id" if miner_type == "seine" else "randomx",
            pool_url="stratum+tcp://pool.example.test:3333",
            extra_config={"cpu_threads": threads},
        )["primary"]

    validated = validate_flight_sheet(document, cfg)

    assert validated["secondary_mode"] == "independent"
    for role in ("primary", "secondary"):
        for key, expected in document[role].items():
            if key == "miner_package":
                assert all(validated[role][key][field] == value for field, value in expected.items())
            else:
                assert validated[role][key] == expected
    assert validated["primary"]["extra_config"]["cpu_threads"] is None
    assert validated["secondary"]["extra_config"]["cpu_threads"] == 7


def test_managed_pool_schemes_remain_adapter_specific(tmp_path: Path):
    cfg = config(tmp_path)
    wrong_scheme = pearl_payload()
    wrong_scheme["primary"]["pool_url"] = "stratum+tls://pool.example.test:4444"
    with pytest.raises(MiningCommandError, match="unsupported scheme"):
        validate_flight_sheet(wrong_scheme, cfg)

    non_pearl_scheme = payload()
    non_pearl_scheme["primary"]["pool_url"] = "pearl://pool.example.test:4444"
    with pytest.raises(MiningCommandError, match="unsupported scheme"):
        validate_flight_sheet(non_pearl_scheme, cfg)


def test_managed_type_bypasses_profile_allowlist_and_runs_manager(tmp_path: Path):
    cfg = config(tmp_path, miner_profile_allowlist=frozenset())
    installed = tmp_path / "packages" / "builtin-xmrig-6.22.3-6.22.3"
    manager_documents = []

    def installer(metadata, agent_config, **kwargs):
        installed.mkdir(parents=True)
        (installed / "xmrig.exe").write_bytes(b"not executed")
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(installed),
            "sha256": None,
            "downloaded": True,
        }

    class Manager:
        def apply(self, document):
            manager_documents.append(document)
            return {"managed": True, "miner_type": "xmrig", "pid": 123}

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda agent_config: Manager(),
    ).execute(Command("1", "apply_flight_sheet", payload()))

    assert outcome.status == "succeeded", outcome.error
    assert outcome.result["managed_miner"]["pid"] == 123
    assert outcome.result["service"] is None
    assert outcome.result["operations"] == []
    assert manager_documents[0]["primary"]["miner_type"] == "xmrig"
    assert manager_documents[0]["primary"]["miner_package"]["installed_path"] == str(
        installed
    )


def test_root_flight_commit_failure_stops_new_miners_and_restores_previous_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = config(tmp_path)
    installed = tmp_path / "packages" / "builtin-xmrig-6.22.3-6.22.3"
    calls: list[str] = []

    def installer(metadata, agent_config, **kwargs):
        installed.mkdir(parents=True)
        (installed / "xmrig.exe").write_bytes(b"not executed")
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(installed),
            "sha256": None,
            "downloaded": True,
        }

    class Manager:
        def snapshot_independent_metadata(self):
            calls.append("snapshot")
            return {tmp_path / "runtime" / "independent-miners.json": None}

        def apply(self, document):
            calls.append("apply")
            return {
                "managed": True,
                "miner_type": "xmrig",
                "pid": 123,
                "replaced_previous": True,
            }

        def stop(self):
            calls.append("stop")
            return {"stopped": True}

        def restore_independent_metadata(self, snapshot):
            assert snapshot
            calls.append("restore")

        def restart(self):
            calls.append("restart")
            return {"restarted": True}

    original_replace = os.replace

    def fail_final_commit(source, target):
        if Path(target) == cfg.flight_sheet_file:
            raise OSError("simulated root flight commit failure")
        return original_replace(source, target)

    monkeypatch.setattr("agent.commands.os.replace", fail_final_commit)
    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("commit-fails", "apply_flight_sheet", payload()))

    assert outcome.status == "failed"
    assert "simulated root flight commit failure" in (outcome.error or "")
    assert calls == [
        "snapshot",
        "apply",
        "stop",
        "restore",
        "restart",
    ]
    assert not cfg.flight_sheet_file.exists()
    assert not cfg.flight_sheet_file.with_name(".flight.json.pending").exists()


def test_managed_policy_rejects_disabled_wrong_package_and_remote_runtime_options(
    tmp_path: Path,
):
    disabled = config(tmp_path, managed_mining_enabled=False)
    outcome = CommandExecutor(disabled).execute(
        Command("disabled", "apply_flight_sheet", payload())
    )
    assert outcome.status == "failed"
    assert "disabled by local configuration" in (outcome.error or "")

    wrong_package = payload(miner_package=package("uploaded-xmrig"))
    outcome = CommandExecutor(config(tmp_path)).execute(
        Command("prefix", "apply_flight_sheet", wrong_package)
    )
    assert outcome.status == "failed"
    assert "controller-managed xmrig official or test package" in (
        outcome.error or ""
    )

    remote_port = payload(extra_config={"api_port": 21550})
    outcome = CommandExecutor(config(tmp_path)).execute(
        Command("api-port", "apply_flight_sheet", remote_port)
    )
    assert outcome.status == "failed"
    assert "unsupported xmrig keys: api_port" in (outcome.error or "")


@pytest.mark.parametrize("role", ["primary", "secondary"])
@pytest.mark.parametrize("miner_type", [
    "synthetic-unsupported-miner", " ORG.SYNTHETIC.UNSUPPORTED-MINER ",
])
def test_unknown_queued_miner_is_rejected_before_install_or_runtime_use(tmp_path, role, miner_type):
    cfg = config(tmp_path)
    saved_flight = json.dumps(payload()).encode()
    cfg.flight_sheet_file.write_bytes(saved_flight)
    queued = payload()
    unknown_leg = payload(miner_type=miner_type)["primary"]
    queued[role] = unknown_leg
    if role == "secondary":
        queued["secondary_mode"] = "independent"

    def forbidden(*args, **kwargs):
        pytest.fail("an unknown miner must not install packages, invoke a runtime or run a command")

    with pytest.raises(MiningCommandError, match=f"{role}.miner_type is unsupported"):
        validate_flight_sheet(queued, cfg)
    outcome = CommandExecutor(
        cfg, package_installer=forbidden, managed_miner_factory=forbidden, runner=forbidden,
    ).execute(Command("old-unknown-command", "apply_flight_sheet", queued))

    assert outcome.status == "failed"
    assert f"{role}.miner_type is unsupported" in outcome.error
    assert cfg.flight_sheet_file.read_bytes() == saved_flight
    assert not list(cfg.miner_packages_dir.rglob("*"))
    assert not cfg.managed_miner_runtime_dir.exists()


def test_managed_policy_accepts_controller_classified_test_package(tmp_path: Path):
    cfg = config(tmp_path)
    document = pearl_payload()
    document["primary"]["miner_package"] = {
        **document["primary"]["miner_package"],
        "id": "test-pearlminer-2.0.5-linux-x64-nvidia",
        "version": "2.0.5",
    }

    validated = validate_flight_sheet(document, cfg)

    assert (
        validated["primary"]["miner_package"]["id"]
        == "test-pearlminer-2.0.5-linux-x64-nvidia"
    )


def test_legacy_profile_allowlist_behavior_is_preserved(tmp_path: Path):
    legacy = payload()
    legacy["primary"].pop("miner_type")
    legacy["primary"]["miner_package"] = None
    legacy["primary"]["extra_config"] = {}
    outcome = CommandExecutor(config(tmp_path)).execute(
        Command("legacy", "apply_flight_sheet", legacy)
    )
    assert outcome.status == "failed"
    assert "miner_profile is not in the local allowlist" in (outcome.error or "")


def test_srb_flight_gpu_oc_still_requires_the_local_switch(tmp_path: Path):
    cfg = config(tmp_path, allow_gpu_oc=False)
    outcome = CommandExecutor(cfg).execute(
        Command("oc-disabled", "apply_flight_sheet", srb_payload())
    )

    assert outcome.status == "failed"
    assert outcome.result["rejected"] is True
    assert "GPU OC is disabled by local configuration" in (outcome.error or "")


def test_srb_flight_gpu_oc_still_uses_local_numeric_bounds(tmp_path: Path):
    cfg = config(tmp_path, allow_gpu_oc=True, gpu_power_limit_min_w=50, gpu_power_limit_max_w=200)
    outcome = CommandExecutor(cfg).execute(
        Command("oc-range", "apply_flight_sheet", srb_payload(oc=gpu_oc(250)))
    )

    assert outcome.status == "failed"
    assert outcome.result["rejected"] is True
    assert "outside the local range 50-200" in (outcome.error or "")


def test_srb_model_gpu_oc_is_validated_locally_without_explicit_indices(tmp_path: Path):
    cfg = config(tmp_path, allow_gpu_oc=True)
    document = srb_payload(
        oc={
            "vendor": "nvidia",
            "settings": [
                {"model_match": "5080", "power_limit_w": 300},
                {"model_match": "RTX 5070", "power_limit_w": 250},
                {"model_match": "5060 Ti", "power_limit_w": 200},
            ],
        }
    )
    document["primary"]["device_indices"] = []
    seen = []

    def installer(metadata, agent_config, **kwargs):
        installed = tmp_path / "packages" / "srb-model"
        installed.mkdir(parents=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(installed),
            "sha256": None,
            "downloaded": True,
        }

    class Manager:
        def apply(self, managed_document):
            seen.append(managed_document["gpu_oc"])
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda agent_config: Manager(),
    ).execute(Command("model-oc", "apply_flight_sheet", document))

    assert outcome.status == "succeeded", outcome.error
    assert document["primary"]["device_indices"] == []
    assert [item["model_match"] for item in seen[0]["settings"]] == [
        "5080",
        "RTX 5070",
        "5060 Ti",
    ]


def test_srb_model_gpu_oc_rejects_duplicate_or_mixed_targets_before_install(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    invalid_settings = (
        [
            {"model_match": "RTX 5080", "power_limit_w": 300},
            {"model_match": "rtx-5080", "power_limit_w": 290},
        ],
        [
            {"index": 0, "power_limit_w": 300},
            {"model_match": "5070", "power_limit_w": 250},
        ],
    )

    for position, settings in enumerate(invalid_settings):
        document = srb_payload(oc={"vendor": "nvidia", "settings": settings})
        outcome = CommandExecutor(cfg).execute(
            Command(f"invalid-model-{position}", "apply_flight_sheet", document)
        )
        assert outcome.status == "failed"
        assert outcome.result["rejected"] is True


def test_srb_flight_gpu_oc_rejects_profile_only_fan_control(tmp_path: Path):
    cfg = config(tmp_path, allow_gpu_oc=True)
    document = srb_payload()
    document["gpu_oc"]["settings"][0]["fan_percent"] = 65

    outcome = CommandExecutor(cfg).execute(
        Command("oc-fan", "apply_flight_sheet", document)
    )

    assert outcome.status == "failed"
    assert "unsupported fields" in (outcome.error or "")


def test_flight_gpu_oc_rejects_xmrig_and_legacy_sheets(tmp_path: Path):
    cfg = config(tmp_path, allow_gpu_oc=True)
    xmrig = payload()
    xmrig["gpu_oc"] = gpu_oc()
    xmrig_outcome = CommandExecutor(cfg).execute(
        Command("xmrig-oc", "apply_flight_sheet", xmrig)
    )

    legacy = payload()
    legacy["primary"].pop("miner_type")
    legacy["primary"]["miner_package"] = None
    legacy["primary"]["extra_config"] = {}
    legacy["gpu_oc"] = gpu_oc()
    legacy_outcome = CommandExecutor(
        config(tmp_path, allow_gpu_oc=True, miner_profile_allowlist=frozenset({"default"}))
    ).execute(Command("legacy-oc", "apply_flight_sheet", legacy))

    assert xmrig_outcome.status == "failed"
    assert legacy_outcome.status == "failed"
    assert "only for managed SRBMiner" in (xmrig_outcome.error or "")
    assert "only for managed SRBMiner" in (legacy_outcome.error or "")


def test_srb_flight_gpu_oc_requires_a_gpu_leg_and_matching_selected_index(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    cpu_only = srb_payload()
    cpu_only["primary"]["coin"] = "XMR"
    cpu_only["primary"]["algorithm"] = "randomx"
    cpu_only["primary"]["device_indices"] = []
    cpu_outcome = CommandExecutor(cfg).execute(
        Command("oc-cpu-only", "apply_flight_sheet", cpu_only)
    )

    wrong_index = srb_payload()
    wrong_index["gpu_oc"] = {
        **gpu_oc(),
        "settings": [{**gpu_oc()["settings"][0], "index": 1}],
    }
    index_outcome = CommandExecutor(cfg).execute(
        Command("oc-wrong-index", "apply_flight_sheet", wrong_index)
    )

    assert cpu_outcome.status == "failed"
    assert "requires an SRBMiner GPU algorithm" in (cpu_outcome.error or "")
    assert index_outcome.status == "failed"
    assert "exactly one row for every selected GPU" in (index_outcome.error or "")


def test_srb_flight_gpu_oc_does_not_use_standalone_nvml_backend(tmp_path: Path):
    events = []
    cfg = config(tmp_path, allow_gpu_oc=True)

    def installer(metadata, agent_config, **kwargs):
        events.append("install")
        installed = tmp_path / "packages" / "srb"
        installed.mkdir(parents=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(installed),
            "sha256": None,
            "downloaded": True,
        }

    class FailingBackend:
        def apply(self, settings):
            raise AssertionError("flight OC must be applied by SRBMiner CLI")

    class Manager:
        def apply(self, document):
            assert document["gpu_oc"]["settings"][0]["power_limit_w"] == 120.0
            events.append("manager")
            return {"managed": True}

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        gpu_backend_factory=FailingBackend,
        managed_miner_factory=lambda agent_config: Manager(),
    ).execute(Command("oc-fails", "apply_flight_sheet", srb_payload()))

    assert outcome.status == "succeeded", outcome.error
    assert events == ["install", "manager"]
    assert outcome.result["gpu_oc"]["source"] == "srbminer_cli"
    assert cfg.flight_sheet_file.exists()


def test_srb_flight_gpu_oc_is_passed_to_managed_miner(tmp_path: Path):
    events = []
    cfg = config(tmp_path, allow_gpu_oc=True)

    def installer(metadata, agent_config, **kwargs):
        events.append("install")
        installed = tmp_path / "packages" / "srb"
        installed.mkdir(parents=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(installed),
            "sha256": None,
            "downloaded": True,
        }

    class Backend:
        def apply(self, settings):
            raise AssertionError("flight OC must not be applied through NVML")

    class Manager:
        def apply(self, document):
            pending = cfg.flight_sheet_file.with_name(
                f".{cfg.flight_sheet_file.name}.pending"
            )
            assert pending.is_file()
            assert not cfg.flight_sheet_file.exists()
            assert document["gpu_oc"]["settings"][0]["power_limit_w"] == 120.0
            events.append("manager")
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda agent_config: Manager(),
    ).execute(Command("oc-success", "apply_flight_sheet", srb_payload()))

    assert outcome.status == "succeeded", outcome.error
    assert events == ["install", "manager"]
    assert outcome.result["gpu_oc"]["source"] == "srbminer_cli"
    assert outcome.result["managed_miner"]["pid"] == 123
    assert cfg.flight_sheet_file.is_file()


def test_srb_no_oc_flight_resets_cli_oc_once_without_persisting_private_flag(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    manager_documents = []

    installer = _package_installer(tmp_path, 'srb')

    class Backend:
        def transition(self, previous, current):
            raise AssertionError("SRBMiner CLI OC must stay separate from NVML state")

    class Manager:
        def apply(self, document):
            manager_documents.append(json.loads(json.dumps(document)))
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    executor = CommandExecutor(
        cfg,
        package_installer=installer,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda agent_config: Manager(),
    )
    first = executor.execute(
        Command("first-oc", "apply_flight_sheet", srb_payload())
    )
    no_oc = srb_payload(oc=None)
    no_oc["gpu_oc"] = None
    no_oc["flight_sheet_id"] = "no-oc-sheet"
    second = executor.execute(
        Command("remove-cli-oc", "apply_flight_sheet", no_oc)
    )

    assert first.status == "succeeded", first.error
    assert second.status == "succeeded", second.error
    assert manager_documents[0]["_reset_srb_gpu_oc"] is True
    assert manager_documents[1]["_reset_srb_gpu_oc"] is True
    assert second.result["gpu_oc"]["source"] == "srbminer_cli_reset"
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["flight_sheet_id"] == "no-oc-sheet"
    assert stored["gpu_oc"] is None
    assert "_reset_srb_gpu_oc" not in stored
    assert not (
        cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    ).exists()


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_fresh_srb_no_oc_restart_respects_platform_oc_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_name: str,
):
    """Linux and Windows leave no-OC mining ownership to device NVML."""

    monkeypatch.setattr("agent.commands.platform.system", lambda: system_name)
    cfg = config(tmp_path, allow_gpu_oc=True)
    manager_documents = []

    installer = _package_installer(tmp_path, 'srb')

    class Manager:
        def apply(self, document):
            manager_documents.append(json.loads(json.dumps(document)))
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    no_oc = srb_payload()
    no_oc["gpu_oc"] = None
    executor = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda agent_config: Manager(),
    )

    first = executor.execute(Command("no-oc-1", "apply_flight_sheet", no_oc))
    second = executor.execute(Command("no-oc-2", "apply_flight_sheet", no_oc))

    assert first.status == "succeeded", first.error
    assert second.status == "succeeded", second.error
    assert all("_reset_srb_gpu_oc" not in item for item in manager_documents)
    assert first.result["gpu_oc"] is None
    assert second.result["gpu_oc"] is None
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["gpu_oc"] is None
    assert "_reset_srb_gpu_oc" not in stored


def test_fresh_srb_no_oc_does_not_gain_privileged_reset_when_local_oc_is_disabled(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=False)
    manager_documents = []

    installer = _package_installer(tmp_path, 'srb')

    class Manager:
        def apply(self, document):
            manager_documents.append(json.loads(json.dumps(document)))
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    no_oc = srb_payload()
    no_oc["gpu_oc"] = None
    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda agent_config: Manager(),
    ).execute(Command("no-policy", "apply_flight_sheet", no_oc))

    assert outcome.status == "succeeded", outcome.error
    assert "_reset_srb_gpu_oc" not in manager_documents[0]
    assert outcome.result["gpu_oc"] is None


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_srb_gpu_no_oc_clears_previous_standalone_nvml_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_name: str,
):
    monkeypatch.setattr("agent.commands.platform.system", lambda: system_name)
    cfg = config(tmp_path, allow_gpu_oc=True)
    transitions = []
    manager_documents = []

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            assert (index, expected_uuid) == (0, "GPU-flight")

        def resolve_device_indices(self, expected_uuids):
            assert expected_uuids == ["GPU-flight"]
            return {"GPU-flight": 0}

        def transition(self, previous, current):
            transitions.append((previous, current))
            return {
                "vendor": "nvidia",
                "operations": [],
                "reset": [
                    {"index": item["index"], "reset": ["power_limit_w"]}
                    for item in previous
                ],
            }

    installer = _package_installer(tmp_path, 'srb')

    class Manager:
        def apply(self, document):
            manager_documents.append(json.loads(json.dumps(document)))
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        package_installer=installer,
        managed_miner_factory=lambda agent_config: Manager(),
    )
    profile = {
        "gpu_profile_id": "device-oc-flight",
        "name": "NVML power",
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 250}],
        "mode": "replace_one",
        "expected_gpu_uuid": "GPU-flight",
        "previous_index": 0,
    }
    applied = executor.execute(
        Command("standalone", "apply_gpu_profile", profile)
    )
    no_oc = srb_payload()
    no_oc["gpu_oc"] = None
    flight = executor.execute(
        Command("srb-no-oc", "apply_flight_sheet", no_oc)
    )

    assert applied.status == "succeeded", applied.error
    assert flight.status == "succeeded", flight.error
    settings = [{"index": 0, "power_limit_w": 250.0}]
    assert transitions == [([], settings), (settings, [])]
    assert "_reset_srb_gpu_oc" not in manager_documents[0]
    assert flight.result["gpu_oc"] == {
        "vendor": "nvidia",
        "source": "nvml_reset",
        "settings_count": 0,
        "reset": [{"index": 0, "reset": ["power_limit_w"]}],
    }
    assert json.loads(
        (cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    ) == {"version": 2, "devices": []}


def test_srb_apply_rejects_v1_nvml_state_without_writes_or_state_loss(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    transitions = []
    manager_calls = []

    class Backend:
        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    installer = _package_installer(tmp_path, 'srb-v1')

    class Manager:
        def apply(self, document):
            manager_calls.append(document)
            return {"managed": True}

    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    )
    state_path = cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": [{"index": 0, "power_limit_w": 250}],
            }
        ),
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    no_oc = srb_payload()
    no_oc["gpu_oc"] = None

    outcome = executor.execute(Command("flight", "apply_flight_sheet", no_oc))

    assert outcome.status == "failed"
    assert "complete UUID-owned state" in (outcome.error or "")
    assert transitions == []
    assert manager_calls == []
    assert state_path.read_bytes() == before


def test_srb_apply_resolve_failure_makes_no_nvml_write_and_preserves_v2_state(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    transitions = []
    gpu_present = True

    class Backend:
        def verify_device_uuid(self, index, expected_uuid):
            if not gpu_present or (index, expected_uuid) != (0, "GPU-flight-stale"):
                raise RuntimeError("GPU identity unavailable")

        def resolve_device_indices(self, expected_uuids):
            if not gpu_present:
                raise RuntimeError("GPU UUID is no longer reported")
            return {"GPU-flight-stale": 0}

        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    installer = _package_installer(tmp_path, 'srb-stale')

    class Manager:
        def apply(self, document):
            raise AssertionError("miner must not start after UUID resolution failure")

    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    )
    profile = {
        "gpu_profile_id": "device-oc-flight-stale",
        "name": "stale",
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 250}],
        "mode": "replace_one",
        "expected_gpu_uuid": "GPU-flight-stale",
        "previous_index": 0,
    }
    assert executor.execute(Command("apply", "apply_gpu_profile", profile)).status == "succeeded"
    state_path = cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    before = state_path.read_bytes()
    transitions.clear()
    gpu_present = False
    no_oc = srb_payload()
    no_oc["gpu_oc"] = None

    outcome = executor.execute(Command("flight", "apply_flight_sheet", no_oc))

    assert outcome.status == "failed"
    assert "no longer reported" in (outcome.error or "")
    assert transitions == []
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("system_name", ["Linux", "Windows"])
def test_srb_apply_defers_missing_uuid_without_resetting_reused_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system_name: str,
):
    monkeypatch.setattr("agent.commands.platform.system", lambda: system_name)
    cfg = config(tmp_path, allow_gpu_oc=True)
    state_path = cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "devices": [
                    {
                        "gpu_uuid": "GPU-present",
                        "index_hint": 0,
                        "setting": {"power_limit_w": 250},
                    },
                    {
                        "gpu_uuid": "GPU-missing",
                        "index_hint": 3,
                        "setting": {"power_limit_w": 210},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    transitions = []
    manager_documents = []

    class Backend:
        def enumerate_device_indices(self):
            # GPU-present moved to index 1.  A different physical card now
            # occupies the missing card's old index 3.
            return {"GPU-present": 1, "GPU-unrelated": 3}

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps((previous, current))))
            return {
                "vendor": "nvidia",
                "operations": [],
                "reset": [
                    {"index": item["index"], "reset": ["power_limit_w"]}
                    for item in previous
                ],
            }

    installer = _package_installer(tmp_path, 'srb-partial')

    class Manager:
        def apply(self, document):
            manager_documents.append(json.loads(json.dumps(document)))
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    no_oc = srb_payload()
    no_oc["gpu_oc"] = None
    outcome = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("flight-partial", "apply_flight_sheet", no_oc))

    assert outcome.status == "succeeded", outcome.error
    assert transitions == [
        [[{"index": 1, "power_limit_w": 250.0}], []]
    ]
    assert "_reset_srb_gpu_oc" not in manager_documents[0]
    assert outcome.result["gpu_oc"]["source"] == "nvml_reset"
    assert outcome.result["gpu_oc"]["deferred_gpu_uuids"] == ["GPU-missing"]
    assert outcome.result["gpu_oc"]["standalone_state_cleared"] is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-missing",
                "index_hint": 3,
                "setting": {"power_limit_w": 210.0},
            }
        ],
    }


def test_srb_launch_failure_rolls_back_only_current_uuid_bound_index(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    state_path = cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "devices": [
                    {
                        "gpu_uuid": "GPU-present",
                        "index_hint": 0,
                        "setting": {"power_limit_w": 250},
                    },
                    {
                        "gpu_uuid": "GPU-missing",
                        "index_hint": 3,
                        "setting": {"power_limit_w": 210},
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

    installer = _package_installer(tmp_path, 'srb-rollback')

    class Manager:
        def apply(self, document):
            raise ManagedMinerError("launch failed")

    no_oc = srb_payload()
    no_oc["gpu_oc"] = None
    outcome = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("flight-rollback", "apply_flight_sheet", no_oc))

    assert outcome.status == "failed"
    assert outcome.error == "launch failed"
    assert transitions == [
        [[{"index": 1, "power_limit_w": 250.0}], []],
        [[], [{"index": 1, "power_limit_w": 250.0}]],
    ]
    # The absent UUID remains metadata only; neither transition touches index 3.
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-missing",
                "index_hint": 3,
                "setting": {"power_limit_w": 210.0},
            },
            {
                "gpu_uuid": "GPU-present",
                "index_hint": 1,
                "setting": {"power_limit_w": 250.0},
            },
        ],
    }


def test_cpu_only_srb_flight_preserves_unrelated_standalone_nvml_profile(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    transitions = []
    manager_documents = []

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-standalone": 0}

        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    installer = _package_installer(tmp_path, 'srb')

    class Manager:
        def apply(self, document):
            manager_documents.append(json.loads(json.dumps(document)))
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    executor = CommandExecutor(
        cfg,
        gpu_backend_factory=Backend,
        package_installer=installer,
        managed_miner_factory=lambda agent_config: Manager(),
    )
    profile = {
        "gpu_profile_id": "standalone-profile",
        "name": "NVML power",
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 250}],
    }
    assert executor.execute(
        Command("standalone", "apply_gpu_profile", profile)
    ).status == "succeeded"
    cpu_only = srb_payload()
    cpu_only["gpu_oc"] = None
    cpu_only["primary"].update(
        {"coin": "XMR", "algorithm": "randomx", "device_indices": []}
    )
    outcome = executor.execute(
        Command("srb-cpu", "apply_flight_sheet", cpu_only)
    )

    assert outcome.status == "succeeded", outcome.error
    assert transitions == [([], [{"index": 0, "power_limit_w": 250.0}])]
    assert "_reset_srb_gpu_oc" not in manager_documents[0]
    state = json.loads(
        (cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-standalone",
                "index_hint": 0,
                "setting": {"power_limit_w": 250.0},
            }
        ],
    }


def test_controller_desired_nvml_profile_can_override_active_srb_cli_oc(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    applied_settings = []

    installer = _package_installer(tmp_path, 'srb')

    class Manager:
        def apply(self, document):
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-overlay": 0}

        def apply(self, settings):
            applied_settings.append(json.loads(json.dumps(settings)))
            return {"vendor": "nvidia", "operations": [], "reset": []}

    executor = CommandExecutor(
        cfg,
        package_installer=installer,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda agent_config: Manager(),
    )
    accepted = executor.execute(
        Command("srb-oc", "apply_flight_sheet", srb_payload())
    )
    profile = {
        "gpu_profile_id": "standalone-profile",
        "name": "NVML profile",
        "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 120}],
    }
    overlaid = executor.execute(
        Command("standalone-oc", "apply_gpu_profile", profile)
    )

    assert accepted.status == "succeeded", accepted.error
    assert overlaid.status == "succeeded", overlaid.error
    assert applied_settings == [[{"index": 0, "power_limit_w": 120.0}]]


def test_failed_flight_keeps_cli_oc_separate_and_preserves_previous_active_file(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    transitions = []
    manager_calls = 0

    installer = _package_installer(tmp_path, 'srb')

    class Backend:
        def transition(self, previous, current):
            transitions.append((previous, current))
            return {"operations": [], "reset": []}

    class Manager:
        def apply(self, document):
            nonlocal manager_calls
            manager_calls += 1
            if manager_calls == 2:
                raise ManagedMinerError("mock miner startup failed")
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    executor = CommandExecutor(
        cfg,
        package_installer=installer,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda agent_config: Manager(),
    )
    first = srb_payload()
    first["flight_sheet_id"] = "first-sheet"
    accepted = executor.execute(Command("first", "apply_flight_sheet", first))
    assert accepted.status == "succeeded", accepted.error

    second = srb_payload()
    second["flight_sheet_id"] = "second-sheet"
    second["gpu_oc"] = None
    rejected = executor.execute(Command("second", "apply_flight_sheet", second))
    assert rejected.status == "failed"
    assert "mock miner startup failed" in (rejected.error or "")

    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["flight_sheet_id"] == "first-sheet"
    assert transitions == []
    assert not (
        cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    ).exists()


def test_dual_flight_installs_one_shared_package_only_once(tmp_path: Path):
    cfg = config(tmp_path)
    install_calls = []

    def installer(metadata, agent_config, **kwargs):
        install_calls.append(metadata["id"])
        installed = tmp_path / "packages" / "shared-srb"
        installed.mkdir(parents=True, exist_ok=True)
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(installed),
            "sha256": None,
            "downloaded": True,
        }

    document = srb_payload()
    document["gpu_oc"] = None
    document["secondary"] = {
        **document["primary"],
        "coin": "HEAVYHASH",
        "wallet_address": "heavyhash-public-wallet",
        "pool_url": "stratum+tcp://heavyhash.example.test:3333",
        "algorithm": "heavyhash",
    }

    class Manager:
        def apply(self, managed_document):
            assert (
                managed_document["primary"]["miner_package"]["installed_path"]
                == managed_document["secondary"]["miner_package"]["installed_path"]
            )
            return {"managed": True, "miner_type": "srbminer", "pid": 123}

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda agent_config: Manager(),
    ).execute(Command("dual", "apply_flight_sheet", document))
    assert outcome.status == "succeeded", outcome.error
    assert install_calls == ["builtin-srbminer-3.4.6"]
    assert outcome.result["miner_packages"][1]["reused"] is True


def test_invalid_srb_dual_configuration_is_rejected_before_install(tmp_path: Path):
    cfg = config(tmp_path)
    install_calls = []

    def installer(metadata, agent_config, **kwargs):
        install_calls.append(metadata["id"])
        raise AssertionError("invalid dual flight must not install a package")

    base = srb_payload()
    base["gpu_oc"] = None
    base["primary"]["coin"] = "XMR"
    base["primary"]["algorithm"] = "randomx"
    base["primary"]["device_indices"] = []
    base["secondary"] = {
        **base["primary"],
        "wallet_address": "another-public-wallet",
        "pool_url": "stratum+tcp://second.example.test:3333",
    }
    randomx_twice = CommandExecutor(
        cfg, package_installer=installer
    ).execute(Command("two-randomx", "apply_flight_sheet", base))
    assert randomx_twice.status == "failed"
    assert randomx_twice.result["rejected"] is True
    assert "cannot use RandomX for both" in (randomx_twice.error or "")

    unequal = srb_payload()
    unequal["gpu_oc"] = None
    unequal["secondary"] = {
        **unequal["primary"],
        "coin": "HEAVYHASH",
        "wallet_address": "heavyhash-public-wallet",
        "pool_url": "stratum+tcp://heavyhash.example.test:3333",
        "algorithm": "heavyhash",
        "device_indices": [1],
    }
    unequal_result = CommandExecutor(
        cfg, package_installer=installer
    ).execute(Command("unequal-gpus", "apply_flight_sheet", unequal))
    assert unequal_result.status == "failed"
    assert unequal_result.result["rejected"] is True
    assert "same ordered GPU indices" in (unequal_result.error or "")
    assert install_calls == []


def _store_active_srb_oc_sheet(cfg: AgentConfig) -> None:
    assert cfg.flight_sheet_file is not None
    cfg.flight_sheet_file.parent.mkdir(parents=True, exist_ok=True)
    active = validate_flight_sheet(srb_payload(), cfg)
    active["applied_at"] = "2026-07-30T00:00:00+00:00"
    cfg.flight_sheet_file.write_text(json.dumps(active), encoding="utf-8")


@pytest.mark.parametrize("target_factory", [payload, pearl_payload])
def test_switch_from_srb_oc_automatically_clears_cli_oc_without_empty_srb_flight(
    tmp_path: Path,
    target_factory,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    _store_active_srb_oc_sheet(cfg)
    events: list[object] = []
    manager_documents: list[dict] = []
    standalone_state = cfg.managed_miner_runtime_dir / "rigpilot-gpu-oc-state.json"
    standalone_state.parent.mkdir(parents=True, exist_ok=True)
    desired_nvml = {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": "GPU-independent",
                "index_hint": 0,
                "setting": {"power_limit_w": 245.0},
            }
        ],
    }
    standalone_state.write_text(json.dumps(desired_nvml), encoding="utf-8")
    transitions: list[list] = []

    installer = _package_installer(tmp_path)

    class Backend:
        def enumerate_device_indices(self):
            return {"GPU-independent": 0}

        def transition(self, previous, current):
            transitions.append(json.loads(json.dumps([previous, current])))
            return {"vendor": "nvidia", "operations": current, "reset": []}

    class Manager:
        def snapshot_independent_metadata(self):
            return {}

        def running(self):
            return {"managed": True, "miner_type": "srbminer", "pid": 42}

        def clear_active_srb_gpu_oc(self):
            events.append("clear-srb-oc")
            return {
                "required": True,
                "cleared": True,
                "method": "srbminer_cli_reset",
                "previous_was_running": True,
            }

        def apply(self, document):
            events.append("apply-target")
            manager_documents.append(json.loads(json.dumps(document)))
            return {
                "managed": True,
                "miner_type": document["primary"]["miner_type"],
                "pid": 123,
            }

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        gpu_backend_factory=Backend,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("switch-away", "apply_flight_sheet", target_factory()))

    assert outcome.status == "succeeded", outcome.error
    assert events == ["clear-srb-oc", "apply-target"]
    assert manager_documents[0]["primary"]["miner_type"] in {
        "xmrig",
        "pearlminer",
    }
    assert outcome.result["gpu_oc"]["source"] == "srbminer_cli_transition_reset"
    assert outcome.result["gpu_oc"]["standalone_nvml_reapplied"] is True
    assert transitions == [[[], [{"index": 0, "power_limit_w": 245.0}]]]
    assert json.loads(standalone_state.read_text(encoding="utf-8")) == desired_nvml


def test_failed_switch_after_srb_oc_reset_restores_previous_running_flight(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    _store_active_srb_oc_sheet(cfg)
    events: list[str] = []

    installer = _package_installer(tmp_path)

    class Manager:
        def snapshot_independent_metadata(self):
            return {}

        def restore_independent_metadata(self, _snapshot):
            events.append("restore-metadata")

        def running(self):
            return {"managed": True, "miner_type": "srbminer", "pid": 42}

        def clear_active_srb_gpu_oc(self):
            events.append("clear-srb-oc")
            return {
                "required": True,
                "cleared": True,
                "previous_was_running": True,
            }

        def apply(self, _document):
            events.append("apply-target")
            raise ManagedMinerTransitionError(
                "target launch failed",
                state_changed=True,
                old_stopped=True,
            )

        def stop(self):
            events.append("stop-failed-target")
            return {"managed": True, "stopped": False}

        def restart(self):
            events.append("restart-previous")
            return {"managed": True, "miner_type": "srbminer", "pid": 99}

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("switch-fails", "apply_flight_sheet", payload()))

    assert outcome.status == "failed"
    assert outcome.error == "target launch failed"
    assert events == [
        "clear-srb-oc",
        "apply-target",
        "stop-failed-target",
        "restore-metadata",
        "restart-previous",
    ]
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["primary"]["miner_type"] == "srbminer"


def test_failed_regular_flight_switch_restores_previous_running_flight(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    assert cfg.flight_sheet_file is not None
    previous = validate_flight_sheet(payload(), cfg)
    previous["applied_at"] = "2026-07-30T00:00:00+00:00"
    cfg.flight_sheet_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.flight_sheet_file.write_text(json.dumps(previous), encoding="utf-8")
    events: list[str] = []

    installer = _package_installer(tmp_path)

    class Manager:
        def snapshot_independent_metadata(self):
            events.append("snapshot")
            return {}

        def running(self):
            events.append("running")
            return {"managed": True, "miner_type": "xmrig", "pid": 42}

        def apply(self, _document):
            events.append("apply-target")
            raise ManagedMinerTransitionError(
                "target launch failed",
                state_changed=True,
                old_stopped=True,
            )

        def stop(self):
            events.append("stop-failed-target")
            return {"managed": True, "stopped": False}

        def restore_independent_metadata(self, _snapshot):
            events.append("restore-metadata")

        def restart(self):
            events.append("restart-previous")
            return {"managed": True, "miner_type": "xmrig", "pid": 99}

    target = pearl_payload()
    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("regular-switch-fails", "apply_flight_sheet", target))

    assert outcome.status == "failed"
    assert outcome.error == "target launch failed"
    assert events == [
        "snapshot",
        "running",
        "apply-target",
        "stop-failed-target",
        "restore-metadata",
        "restart-previous",
    ]
    stored = json.loads(cfg.flight_sheet_file.read_text(encoding="utf-8"))
    assert stored["primary"]["miner_type"] == "xmrig"


def test_preflight_failure_does_not_interrupt_previous_running_flight(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    previous = validate_flight_sheet(payload(), cfg)
    previous["applied_at"] = "2026-07-30T00:00:00+00:00"
    assert cfg.flight_sheet_file is not None
    cfg.flight_sheet_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.flight_sheet_file.write_text(json.dumps(previous), encoding="utf-8")
    events: list[str] = []

    installer = _package_installer(tmp_path)

    class Manager:
        def snapshot_independent_metadata(self):
            events.append("snapshot")
            return {}

        def running(self):
            events.append("running")
            return {"managed": True, "miner_type": "xmrig", "pid": 42}

        def apply(self, _document):
            events.append("preflight-target")
            raise ManagedMinerError("target package preflight failed")

        def stop(self):
            events.append("unexpected-stop")

        def restart(self):
            events.append("unexpected-restart")

    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(
        Command("preflight-fails", "apply_flight_sheet", pearl_payload())
    )

    assert outcome.status == "failed"
    assert outcome.error == "target package preflight failed"
    assert events == ["snapshot", "running", "preflight-target"]


def test_partial_srb_launch_failure_clears_target_oc_before_rollback(
    tmp_path: Path,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    events: list[object] = []

    installer = _package_installer(tmp_path)

    class Manager:
        def snapshot_independent_metadata(self):
            return {}

        def running(self):
            return None

        def apply(self, _document):
            events.append("partial-target")
            raise ManagedMinerTransitionError(
                "target status write failed",
                state_changed=True,
                target_started=True,
                target_oc_may_have_applied=True,
            )

        def clear_srb_gpu_oc_for_document(
            self, document, *, restart_on_failure=True
        ):
            events.append(
                (
                    "clear-target-oc",
                    document["flight_sheet_id"],
                    restart_on_failure,
                )
            )
            return {"required": True, "cleared": True}

        def stop(self):
            events.append("stop-target")
            return {"managed": True, "stopped": False}

        def restore_independent_metadata(self, _snapshot):
            events.append("restore-metadata")

    target = srb_payload(oc=gpu_oc(250))
    outcome = CommandExecutor(
        cfg,
        package_installer=installer,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command("partial-srb", "apply_flight_sheet", target))

    assert outcome.status == "failed"
    assert outcome.error == "target status write failed"
    assert events == [
        "partial-target",
        ("clear-target-oc", target["flight_sheet_id"], False),
        "stop-target",
        "restore-metadata",
    ]


@pytest.mark.parametrize("action", ["stop_mining", "remove_flight_sheet"])
def test_stop_or_remove_clears_active_srb_cli_oc_before_finishing(
    tmp_path: Path,
    action: str,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    _store_active_srb_oc_sheet(cfg)
    events: list[object] = []

    class Manager:
        def clear_active_srb_gpu_oc(self, *, restart_on_failure=True):
            events.append(("clear-srb-oc", restart_on_failure))
            return {
                "required": True,
                "cleared": True,
                "method": "srbminer_cli_reset",
                "previous_was_running": True,
            }

        def stop(self):
            events.append("stop")
            return {"managed": True, "stopped": False}

        def remove_flight_sheet(self):
            events.append("remove")
            return {"managed": True, "configuration_cleared": True}

    outcome = CommandExecutor(
        cfg,
        managed_miner_factory=lambda _config: Manager(),
    ).execute(Command(action, action, {}))

    assert outcome.status == "succeeded", outcome.error
    assert events == (
        ["stop", ("clear-srb-oc", False)]
        if action == "stop_mining"
        else ["stop", ("clear-srb-oc", False), "remove"]
    )
    assert outcome.result["srb_gpu_oc_reset"]["cleared"] is True


@pytest.mark.parametrize("action", ["stop_mining", "remove_flight_sheet"])
def test_explicit_stop_remains_stopped_when_srb_oc_reset_fails(
    tmp_path: Path,
    action: str,
):
    cfg = config(tmp_path, allow_gpu_oc=True)
    _store_active_srb_oc_sheet(cfg)
    events: list[object] = []

    class Manager:
        def stop(self):
            events.append("stop")
            return {"managed": True, "stopped": True}

        def clear_active_srb_gpu_oc(self, *, restart_on_failure=True):
            events.append(("clear-srb-oc", restart_on_failure))
            raise ManagedMinerError(
                "temporary SRBMiner GPU OC reset process did not become ready: timed out"
            )

        def remove_flight_sheet(self):
            events.append("remove")
            return {"configuration_cleared": True}

    executor = CommandExecutor(
        cfg,
        managed_miner_factory=lambda _config: Manager(),
    )
    original_reset = executor._reset_standalone_gpu_oc

    def reset_nvml():
        events.append("reset-nvml")
        return {"state_cleared": True, "settings_count": 0}

    executor._reset_standalone_gpu_oc = reset_nvml
    try:
        outcome = executor.execute(Command(action, action, {}))
    finally:
        executor._reset_standalone_gpu_oc = original_reset

    assert outcome.status == "failed"
    assert outcome.result["stopped"] is True
    assert outcome.result["srb_gpu_oc_reset"]["status"] == "failed"
    assert outcome.result["gpu_oc_reset"]["state_cleared"] is True
    assert "timed out" in (outcome.error or "")
    assert events == [
        "stop",
        ("clear-srb-oc", False),
        "reset-nvml",
    ]


def test_unconfirmed_srb_reset_fails_stop_and_still_resets_nvml(tmp_path: Path):
    cfg = config(tmp_path, allow_gpu_oc=True)
    _store_active_srb_oc_sheet(cfg)
    events: list[str] = []

    class Manager:
        def stop(self):
            events.append("stop")
            return {"managed": True, "stopped": True}

        def clear_active_srb_gpu_oc(self, *, restart_on_failure=True):
            events.append("clear-srb-oc")
            return {
                "required": True,
                "cleared": False,
                "method": "srbminer_cli_reset",
            }

    executor = CommandExecutor(
        cfg,
        managed_miner_factory=lambda _config: Manager(),
    )
    original_reset = executor._reset_standalone_gpu_oc

    def reset_nvml():
        events.append("reset-nvml")
        return {"state_cleared": True, "settings_count": 0}

    executor._reset_standalone_gpu_oc = reset_nvml
    try:
        outcome = executor.execute(Command("stop", "stop_mining", {}))
    finally:
        executor._reset_standalone_gpu_oc = original_reset

    assert outcome.status == "failed"
    assert outcome.result["stopped"] is True
    assert outcome.result["srb_gpu_oc_reset"]["cleared"] is False
    assert outcome.result["srb_gpu_oc_reset"]["status"] == "failed"
    assert "was not confirmed" in (outcome.error or "")
    assert events == ["stop", "clear-srb-oc", "reset-nvml"]


def test_remove_configuration_failure_still_resets_nvml(tmp_path: Path):
    cfg = config(tmp_path, allow_gpu_oc=True)
    events: list[str] = []

    class Manager:
        def stop(self):
            events.append("stop")
            return {"managed": True, "stopped": True}

        def remove_flight_sheet(self):
            events.append("remove")
            raise ManagedMinerError("active flight sheet could not be removed")

    executor = CommandExecutor(
        cfg,
        managed_miner_factory=lambda _config: Manager(),
    )
    original_reset = executor._reset_standalone_gpu_oc

    def reset_nvml():
        events.append("reset-nvml")
        return {"state_cleared": True, "settings_count": 1}

    executor._reset_standalone_gpu_oc = reset_nvml
    try:
        outcome = executor.execute(
            Command("remove", "remove_flight_sheet", {})
        )
    finally:
        executor._reset_standalone_gpu_oc = original_reset

    assert outcome.status == "failed"
    assert outcome.result["stopped"] is True
    assert outcome.result["configuration_removal"]["status"] == "failed"
    assert outcome.result["gpu_oc_reset"]["state_cleared"] is True
    assert "could not be removed" in (outcome.error or "")
    assert events == ["stop", "remove", "reset-nvml"]
