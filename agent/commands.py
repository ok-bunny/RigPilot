"""Strictly allowlisted remote command execution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import AgentConfig
from .fsutil import is_link_or_reparse as _is_link_or_reparse
from .agent_updates import (
    AGENT_UPDATE_ACTION,
    AgentUpdateError,
    LINUX_SYSTEMCTL,
    LINUX_SYSTEMD_RUN,
    _linux_trusted_system_executable,
    validate_agent_update_payload,
)
from .gpu_oc import GpuOcError, NvmlGpuOcBackend, gpu_oc_state_path as _gpu_oc_state_path
from .external_process import external_process_environment
from .inventory import collect_host_data
from .linux_ssh import (
    LINUX_SSH_CONFIGURE_ACTION,
    LinuxSshError,
    configure_linux_ssh,
    validate_linux_ssh_payload,
)
from .managed_miners import (
    MINING_RESIDUE_CLEANUP_ACTION,
    MINING_RESIDUE_SCAN_ACTION,
    leg_effective_resource,
    _document_uses_srb_gpu_oc,
    _srb_gpu_oc_reset_satisfied,
    ManagedMinerError,
    ManagedMinerManager,
    ManagedMinerTransitionError,
    cancel_deferred_secondary_start,
)
from .miner_logs import collect_managed_miner_logs
from .miner_cache_cleanup import (
    SCAN_ACTION as MINER_CACHE_SCAN_ACTION,
    CLEANUP_ACTION as MINER_CACHE_CLEANUP_ACTION,
    MinerCacheCleanupError,
    scan as scan_miner_cache,
    cleanup as cleanup_miner_cache,
)
from .miner_packages import MinerPackageError, install_miner_package
from .mining_commands import (
    MiningCommandError,
    validate_flight_sheet,
    validate_gpu_profile,
    write_flight_sheet,
)
from .state import AgentState


LOGGER = logging.getLogger(__name__)
WINDOWS_POWER_DELAY_SECONDS = 10
LINUX_POWER_DELAY_SECONDS = 10
GPU_OC_STATE_MIGRATION_ACTION = "migrate_gpu_oc_state.v1"
# Controller-owned, read-only activation-timeout forensics.  The action is
# never dispatchable by operators; the payload must repeat the controller's
# fixed command list exactly, and execution always uses these module-level
# literals so no payload text can ever reach a shell.
UPDATE_FORENSICS_ACTION = "collect_update_forensics.v1"
UPDATE_FORENSICS_TRIGGER = "agent_update_activation_timeout"
UPDATE_FORENSICS_COMMANDS = (
    "df -h / /var /tmp",
    "systemctl list-timers | grep rigpilot",
    "journalctl -u rigpilot-agent --since -2h | tail -200",
)
UPDATE_FORENSICS_TIMEOUT_SECONDS = 30
# The controller caps every string inside a command result body at 8,192
# characters and the whole body at 131,072 bytes; three bounded outputs plus
# JSON escaping stay safely below both.
UPDATE_FORENSICS_OUTPUT_LIMIT_CHARS = 8_192
_FORENSICS_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}$")
_FORENSICS_COMMAND_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
SUPPORTED_ACTIONS = frozenset(
    {
        "collect_diagnostics",
        "collect_miner_logs",
        "stop_mining",
        "restart_mining",
        "remove_flight_sheet",
        MINING_RESIDUE_SCAN_ACTION,
        MINING_RESIDUE_CLEANUP_ACTION,
        MINER_CACHE_SCAN_ACTION,
        MINER_CACHE_CLEANUP_ACTION,
        "restart_service",
        "reboot",
        "shutdown",
        "apply_flight_sheet",
        "apply_gpu_profile",
        GPU_OC_STATE_MIGRATION_ACTION,
        AGENT_UPDATE_ACTION,
        LINUX_SSH_CONFIGURE_ACTION,
        UPDATE_FORENSICS_ACTION,
    }
)


class CommandRejected(ValueError):
    """A command failed authorization or schema validation."""


@dataclass(frozen=True)
class Command:
    command_id: str
    action: str
    payload: Mapping[str, Any]

    @classmethod
    def parse(cls, raw: Any) -> "Command":
        if not isinstance(raw, dict):
            raise CommandRejected("command must be an object")
        if set(raw) - {"id", "action", "payload"}:
            raise CommandRejected("command contains unknown fields")
        command_id = raw.get("id")
        action = raw.get("action")
        payload = raw.get("payload", {})
        if not isinstance(command_id, (str, int)) or isinstance(command_id, bool):
            raise CommandRejected("command id must be a string or integer")
        if not str(command_id).strip():
            raise CommandRejected("command id must not be empty")
        if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
            raise CommandRejected("unsupported command action")
        if not isinstance(payload, dict):
            raise CommandRejected("command payload must be an object")
        return cls(str(command_id), action, payload)

    def fingerprint(self) -> str:
        canonical = json.dumps(
            {"action": self.action, "payload": self.payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class CommandOutcome:
    status: str
    result: dict[str, Any]
    error: str | None = None

    @classmethod
    def unexpected_failure(cls) -> "CommandOutcome":
        """Return a terminal failure without exposing exception contents.

        Unexpected exceptions can contain command payloads, bearer credentials,
        filesystem paths, or third-party output.  The detailed exception type is
        kept in the local structured log; the durable/controller-facing result is
        deliberately fixed and short.
        """

        return cls(
            status="failed",
            result={"unexpected": True},
            error="unexpected local command execution failure",
        )

    def to_api(self) -> dict[str, Any]:
        return {"status": self.status, "result": self.result, "error": self.error}


def _ensure_exact_payload(payload: Mapping[str, Any], keys: set[str]) -> None:
    if set(payload) != keys:
        expected = ", ".join(sorted(keys)) or "no fields"
        raise CommandRejected(f"payload must contain exactly: {expected}")


def _validate_update_forensics_payload(payload: Mapping[str, Any]) -> None:
    """Accept only the controller's fixed read-only forensics block.

    The command list must equal the module-level whitelist byte for byte, so
    an operator-crafted or drifted payload can never smuggle an arbitrary
    command past this action.  Execution ignores the payload entirely and
    always runs ``UPDATE_FORENSICS_COMMANDS``.
    """

    if not isinstance(payload, dict) or set(payload) != {"_controller_auto_forensics"}:
        raise CommandRejected(
            "update forensics payload must carry only the controller forensics block"
        )
    block = payload["_controller_auto_forensics"]
    if not isinstance(block, dict) or set(block) != {
        "trigger",
        "command_id",
        "source_version",
        "target_version",
        "commands",
    }:
        raise CommandRejected("update forensics block fields are invalid")
    if block["trigger"] != UPDATE_FORENSICS_TRIGGER:
        raise CommandRejected("update forensics trigger is invalid")
    command_id = block["command_id"]
    if (
        not isinstance(command_id, str)
        or _FORENSICS_COMMAND_ID_RE.fullmatch(command_id) is None
    ):
        raise CommandRejected("update forensics command id is invalid")
    for key in ("source_version", "target_version"):
        value = block[key]
        if value is not None and (
            not isinstance(value, str)
            or _FORENSICS_VERSION_RE.fullmatch(value) is None
        ):
            raise CommandRejected("update forensics version is invalid")
    commands = block["commands"]
    if not isinstance(commands, list) or tuple(commands) != UPDATE_FORENSICS_COMMANDS:
        raise CommandRejected(
            "update forensics commands do not match the fixed read-only whitelist"
        )


def _validate_residue_cleanup_payload(
    payload: Mapping[str, Any],
) -> tuple[str, list[dict[str, str]] | None, bool, bool]:
    old_fields = {"scan_fingerprint"}
    new_fields = {
        "scan_fingerprint",
        "targets",
        "reset_srbminer_oc",
        "reset_nvml_oc",
    }
    payload_fields = frozenset(payload)
    if payload_fields not in {frozenset(old_fields), frozenset(new_fields)}:
        raise CommandRejected(
            "residue cleanup payload must use the legacy fingerprint-only "
            "contract or the selective target contract"
        )
    scan_fingerprint = payload.get("scan_fingerprint")
    if (
        not isinstance(scan_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", scan_fingerprint) is None
    ):
        raise CommandRejected(
            "scan_fingerprint must be a lowercase SHA-256 digest"
        )
    if payload_fields == old_fields:
        return scan_fingerprint, None, True, True
    raw_targets = payload.get("targets")
    if (
        not isinstance(raw_targets, list)
        or len(raw_targets) > 3
    ):
        raise CommandRejected("targets must contain at most three entries")
    targets: list[dict[str, str]] = []
    seen_roles: set[str] = set()
    for target in raw_targets:
        if not isinstance(target, dict) or set(target) not in (
            {"role", "target_fingerprint"},
            {
                "role",
                "target_fingerprint",
                "candidate_fingerprint",
            },
        ):
            raise CommandRejected("residue cleanup target is invalid")
        role = target.get("role")
        fingerprint = target.get("target_fingerprint")
        candidate_fingerprint = target.get("candidate_fingerprint")
        if (
            role not in {"legacy", "primary", "secondary"}
            or role in seen_roles
            or not isinstance(fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            or (
                candidate_fingerprint is not None
                and (
                    not isinstance(candidate_fingerprint, str)
                    or re.fullmatch(
                        r"[0-9a-f]{64}", candidate_fingerprint
                    )
                    is None
                )
            )
        ):
            raise CommandRejected("residue cleanup target is invalid")
        seen_roles.add(str(role))
        targets.append(
            {
                "role": str(role),
                "target_fingerprint": fingerprint,
                **(
                    {"candidate_fingerprint": candidate_fingerprint}
                    if candidate_fingerprint is not None
                    else {}
                ),
            }
        )
    reset_srb = payload.get("reset_srbminer_oc")
    reset_nvml = payload.get("reset_nvml_oc")
    if not isinstance(reset_srb, bool) or not isinstance(reset_nvml, bool):
        raise CommandRejected("residue cleanup OC reset flags must be boolean")
    if not targets and not reset_srb and not reset_nvml:
        raise CommandRejected(
            "select at least one residue target or OC reset option"
        )
    return scan_fingerprint, targets, reset_srb, reset_nvml


def _validate_gpu_oc_state_migration_payload(
    payload: Mapping[str, Any], config: AgentConfig
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    _ensure_exact_payload(payload, {"schema_version", "history_command_ids", "devices"})
    if payload.get("schema_version") != 1:
        raise CommandRejected("GPU OC state migration schema_version must be 1")
    raw_ids = payload.get("history_command_ids")
    if (
        not isinstance(raw_ids, list)
        or not 1 <= len(raw_ids) <= 256
        or len(set(raw_ids)) != len(raw_ids)
        or any(
            not isinstance(command_id, str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                command_id,
            )
            is None
            for command_id in raw_ids
        )
    ):
        raise CommandRejected("GPU OC migration history command IDs are invalid")
    raw_devices = payload.get("devices")
    if not isinstance(raw_devices, list) or not 1 <= len(raw_devices) <= 32:
        raise CommandRejected("GPU OC migration devices must contain 1-32 entries")
    devices: dict[str, dict[str, Any]] = {}
    raw_settings: list[dict[str, Any]] = []
    for position, device in enumerate(raw_devices):
        if not isinstance(device, dict) or set(device) != {"gpu_uuid", "setting"}:
            raise CommandRejected(f"GPU OC migration devices[{position}] is invalid")
        gpu_uuid = device.get("gpu_uuid")
        setting = device.get("setting")
        if (
            not isinstance(gpu_uuid, str)
            or not 4 <= len(gpu_uuid) <= 128
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in gpu_uuid
            )
            or gpu_uuid in devices
            or not isinstance(setting, dict)
        ):
            raise CommandRejected(f"GPU OC migration devices[{position}] is invalid")
        devices[gpu_uuid] = dict(setting)
        raw_settings.append(dict(setting))
    try:
        _vendor, normalized = validate_gpu_profile(
            {
                "gpu_profile_id": "gpu-oc-state-migration",
                "name": "GPU OC state migration",
                "vendor": "nvidia",
                "settings": raw_settings,
            },
            config,
        )
    except MiningCommandError as exc:
        raise CommandRejected(str(exc)) from exc
    return {
        gpu_uuid: normalized[position]
        for position, gpu_uuid in enumerate(devices)
    }, list(raw_ids)


def _windows_system32_binary(filename: str) -> str:
    """Resolve one fixed Windows system binary without consulting PATH."""

    if filename not in {"shutdown.exe"}:
        raise RuntimeError("unsupported Windows system binary")
    system_root_value = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root_value:
        raise RuntimeError("Windows SystemRoot is unavailable")
    system_root = Path(system_root_value)
    if not system_root.is_absolute():
        raise RuntimeError("Windows SystemRoot is invalid")
    binary = system_root / "System32" / filename
    if not binary.is_file():
        raise RuntimeError(
            f"Windows system binary is unavailable: {binary}"
        )
    return str(binary)




def _load_gpu_oc_state_record(
    config: AgentConfig,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], int]:
    path = _gpu_oc_state_path(config)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [], {}, 2
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("GPU OC state file is not a safe regular file")
    if info.st_size > 65_536:
        raise RuntimeError("GPU OC state file is too large")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("GPU OC state file is invalid") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("GPU OC state file has an unsupported shape")
    version = raw.get("version")
    expected_fields = {"version", "settings"} if version == 1 else {"version", "devices"} if version == 2 else set()
    if set(raw) != expected_fields:
        raise RuntimeError("GPU OC state file has an unsupported version")
    devices: dict[str, dict[str, Any]] = {}
    try:
        state_validation_config = replace(
            config,
            allow_gpu_oc=True,
            gpu_oc_allowed_vendors=frozenset({"nvidia"}),
            # Previously applied power remains valid history after local
            # policy changes. New requests still enforce explicit policy and
            # NVML bounds; the state codec only requires positive finite power.
            gpu_power_limit_min_w=None,
            gpu_power_limit_max_w=None,
            gpu_core_clock_offset_min_mhz=-2000,
            gpu_core_clock_offset_max_mhz=2000,
            gpu_core_clock_lock_min_mhz=100,
            gpu_core_clock_lock_max_mhz=5000,
            gpu_memory_clock_offset_min_mhz=-5000,
            gpu_memory_clock_offset_max_mhz=5000,
            gpu_memory_clock_lock_min_mhz=100,
            gpu_memory_clock_lock_max_mhz=20000,
            gpu_fan_min_percent=20,
            gpu_fan_max_percent=100,
        )
        if version == 1 and isinstance(raw["settings"], list) and raw["settings"]:
            _, settings = validate_gpu_profile(
                {
                    "gpu_profile_id": "local-state",
                    "name": "RigPilot GPU OC state",
                    "vendor": "nvidia",
                    "settings": raw["settings"],
                },
                state_validation_config,
            )
        elif version == 1 and isinstance(raw["settings"], list):
            settings = []
        elif version == 2 and isinstance(raw["devices"], list) and len(raw["devices"]) <= 32:
            settings = []
            for position, device in enumerate(raw["devices"]):
                if not isinstance(device, dict) or set(device) != {"gpu_uuid", "index_hint", "setting"}:
                    raise MiningCommandError(f"devices[{position}] has an invalid shape")
                gpu_uuid = device["gpu_uuid"]
                index_hint = device["index_hint"]
                setting_fields = device["setting"]
                if (
                    not isinstance(gpu_uuid, str)
                    or not 4 <= len(gpu_uuid) <= 128
                    or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in gpu_uuid)
                    or gpu_uuid in devices
                    or isinstance(index_hint, bool)
                    or not isinstance(index_hint, int)
                    or not 0 <= index_hint <= 31
                    or not isinstance(setting_fields, dict)
                    or "index" in setting_fields
                    or not setting_fields
                ):
                    raise MiningCommandError(f"devices[{position}] is invalid")
                _, normalized = validate_gpu_profile(
                    {
                        "gpu_profile_id": "local-state",
                        "name": "RigPilot GPU OC state",
                        "vendor": "nvidia",
                        "settings": [{"index": index_hint, **setting_fields}],
                    },
                    state_validation_config,
                )
                devices[gpu_uuid] = normalized[0]
                settings.append(normalized[0])
        else:
            raise MiningCommandError("GPU OC state has invalid entries")
    except MiningCommandError as exc:
        raise RuntimeError("GPU OC state file contains invalid settings") from exc
    return settings, devices, int(version)


def _write_gpu_oc_state(
    config: AgentConfig,
    settings: list[dict[str, Any]],
    devices: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    path = _gpu_oc_state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    if devices is None:
        if settings:
            raise RuntimeError(
                "GPU OC 状态缺少完整显卡 UUID 绑定 "
                "(GPU OC state requires complete UUID-owned devices)"
            )
        devices = {}
    if len(devices) != len(settings) or sorted(
        json.dumps(setting, sort_keys=True) for setting in settings
    ) != sorted(
        json.dumps(dict(setting), sort_keys=True) for setting in devices.values()
    ):
        raise RuntimeError("GPU OC device identities do not fully cover settings")
    state: dict[str, Any] = {
        "version": 2,
        "devices": [
            {
                "gpu_uuid": gpu_uuid,
                "index_hint": int(devices[gpu_uuid]["index"]),
                "setting": {
                    key: value for key, value in devices[gpu_uuid].items()
                    if key != "index"
                },
            }
            for gpu_uuid in sorted(devices)
        ],
    }
    payload = json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            temporary_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _active_flight_gpu_oc_document(config: AgentConfig) -> dict[str, Any] | None:
    """Read only the bounded active sheet metadata needed to separate OC paths."""

    path = config.flight_sheet_file
    if path is None:
        return None
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("active flight sheet cannot be inspected") from exc
    if (
        _is_link_or_reparse(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > 256 * 1024
    ):
        raise RuntimeError("active flight sheet is unsafe")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("active flight sheet is invalid") from exc
    if not isinstance(document, dict):
        raise RuntimeError("active flight sheet is invalid")
    return document


def _active_flight_uses_srb_gpu_oc(config: AgentConfig) -> bool:
    document = _active_flight_gpu_oc_document(config)
    return bool(document and _document_uses_srb_gpu_oc(
        document,
        local_oc_enabled=config.allow_gpu_oc,
        system_name=platform.system(),
    ))


def _flight_uses_srb_gpu(document: Mapping[str, Any]) -> bool:
    """Return whether native SRBMiner owns at least one GPU/CLI-OC leg.

    WSL GPU mining deliberately leaves OC ownership with the Windows host's
    saved NVML profile.  Treating it as an SRBMiner CLI-OC flight would reset
    that profile and inject unsupported guest clock flags.
    """

    for role in ("primary", "secondary"):
        leg = document.get(role)
        if (
            not isinstance(leg, Mapping)
            or leg.get("miner_type") != "srbminer"
            or leg.get("execution_mode", "native") != "native"
        ):
            continue
        if leg_effective_resource(leg) == "gpu":
            return True
    return False



def _covered_srb_cli_nvml_devices(
    document: Mapping[str, Any] | None,
    owned_devices: Mapping[str, Mapping[str, Any]],
    identities: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Prove model-based CLI fields are covered by UUID-owned NVML profiles.

    SRBMiner numeric IDs are not NVML indices. Only a complete, unambiguous
    model match can transfer ownership without inventing that translation.
    """
    if not isinstance(document, Mapping) or not _flight_uses_srb_gpu(document):
        return {}
    raw = document.get("gpu_oc")
    if not isinstance(raw, Mapping) or raw.get("vendor") != "nvidia":
        return {}
    rows = raw.get("settings")
    if (not isinstance(rows, list) or not rows or not isinstance(identities, Mapping)
            or not identities or not isinstance(owned_devices, Mapping)):
        return {}
    # Miner IDs are not NVML indices. Without a proven PCI translation, only
    # automatic selection can be covered, including every NVIDIA card that
    # could receive a model rule's zero-filled CLI parameter.
    for role in ("primary", "secondary"):
        leg = document.get(role)
        if (isinstance(leg, Mapping) and leg.get("miner_type") == "srbminer"
                and leg.get("execution_mode", "native") == "native"
                and leg_effective_resource(leg) == "gpu" and leg.get("device_indices")):
            return {}
    if set(owned_devices) - set(identities):
        return {}
    indices: set[int] = set()
    for gpu_uuid, identity in identities.items():
        if not isinstance(gpu_uuid, str) or not isinstance(identity, Mapping):
            return {}
        index, name = identity.get("index"), identity.get("name")
        if (isinstance(index, bool) or not isinstance(index, int)
                or not 0 <= index <= 31 or index in indices
                or not isinstance(name, str) or not name.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            return {}
        indices.add(index)
    # A homogeneous automatic NVIDIA selection has no unmatched device that
    # could receive an unproven zero-filled CLI control or algorithm-floor skip.
    if len({re.sub(r"[^a-z0-9]+", "", i["name"].casefold()) for i in identities.values()}) != 1:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or "index" in row:
            return {}
        model = row.get("model_match")
        if not isinstance(model, str):
            return {}
        key = re.sub(r"[^a-z0-9]+", "", model.casefold())
        fields = set(row) - {"model_match"}
        if len(key) < 2 or not fields:
            return {}
        matches = {
            uuid: identity for uuid, identity in identities.items()
            if key in re.sub(r"[^a-z0-9]+", "", identity["name"].casefold())
        }
        models = {re.sub(r"[^a-z0-9]+", "", i["name"].casefold()) for i in matches.values()}
        if not matches or len(models) != 1:
            return {}
        for gpu_uuid, identity in matches.items():
            owned = owned_devices.get(gpu_uuid)
            if not isinstance(owned, Mapping) or not fields <= (set(owned) - {"index"}):
                return {}
            result[gpu_uuid] = {**owned, "index": identity["index"]}
    if set(result) != set(identities):
        return {}
    return result


def _transition_gpu_oc(
    backend: Any,
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    expected_gpu_uuids: Mapping[int, str] | None = None,
    *,
    config: AgentConfig | None = None,
) -> dict[str, Any]:
    if expected_gpu_uuids and getattr(
        backend, "supports_uuid_bound_transitions", False
    ):
        def bind(settings: list[dict[str, Any]]) -> list[dict[str, Any]]:
            bound: list[dict[str, Any]] = []
            for setting in settings:
                index = int(setting["index"])
                expected_uuid = expected_gpu_uuids.get(index)
                if expected_uuid is None:
                    raise RuntimeError(
                        f"GPU OC transition lacks UUID binding for index {index}"
                    )
                bound.append(
                    {**setting, "_expected_gpu_uuid": expected_uuid}
                )
            return bound

        previous = bind(previous)
        current = bind(current)
    try:
        transition = getattr(backend, "transition", None)
        if callable(transition):
            return dict(transition(previous, current))
        if previous and previous != current:
            raise RuntimeError("configured GPU backend cannot safely replace previous settings")
        if current:
            return dict(backend.apply(current))
        return {"vendor": "nvidia", "operations": []}
    except GpuOcError as exc:
        if config is not None:
            _record_gpu_oc_recovery(
                config, exc, previous + current, expected_gpu_uuids or {},
            )
        raise


_GPU_OC_RECOVERY_FIELDS = frozenset({
    "power_limit_w", "core_clock_offset_mhz", "memory_clock_offset_mhz",
    "core_clock_lock_mhz", "memory_clock_lock_mhz", "fan_percent",
})


def _gpu_oc_fault_details(exc: GpuOcError) -> dict[str, Any]:
    """Bound displayed codes while preserving every observed hardware fault."""

    observed = [
        code for code in getattr(exc, "nvml_codes", ())
        if type(code) is int and 0 <= code <= 999
    ]
    displayed = observed[:16]
    critical = {15, 16}
    missing = list(dict.fromkeys(
        code for code in observed if code in critical and code not in displayed
    ))
    if missing:
        slots = [index for index in range(len(displayed) - 1, -1, -1) if displayed[index] not in critical]
        # Normally aggregation has distinct codes. Preserve both fault kinds
        # even if a custom backend supplied repeated critical codes first.
        slots.extend(
            index for index in range(len(displayed) - 1, -1, -1)
            if displayed[index] in critical and displayed.index(displayed[index]) != index
        )
        for index, code in zip(sorted(slots[:len(missing)]), missing):
            displayed[index] = code
    return {
        "nvml_codes": displayed,
        "reset_required": bool(getattr(exc, "reset_required", False) or 16 in observed),
        "hardware_fault": bool(getattr(exc, "hardware_fault", False) or critical.intersection(observed)),
    }


def _persist_gpu_oc_transition(
    config: AgentConfig,
    settings: list[dict[str, Any]],
    devices: Mapping[str, Mapping[str, Any]] | None,
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    gpu_uuids: Mapping[int, str],
    completed: Mapping[str, Any],
) -> None:
    """Persist a completed hardware transition without replaying it on I/O failure."""

    try:
        _write_gpu_oc_state(config, settings, devices)
    except Exception as exc:
        def fields(rows: Any, key: str) -> dict[int, set[str]]:
            result: dict[int, set[str]] = {}
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, Mapping) or type(row.get("index")) is not int:
                        continue
                    values = row.get(key)
                    if isinstance(values, list) and all(isinstance(field, str) for field in values):
                        result.setdefault(row["index"], set()).update(set(values) & _GPU_OC_RECOVERY_FIELDS)
            return result

        reset = fields(completed.get("reset"), "reset")
        applied = fields(completed.get("operations"), "applied")
        current_fields = {int(row["index"]): set(row) & _GPU_OC_RECOVERY_FIELDS for row in current}
        known = all(
            set(row) & _GPU_OC_RECOVERY_FIELDS <= reset.get(int(row["index"]), set())
            for row in previous
        ) and all(
            set(row) & _GPU_OC_RECOVERY_FIELDS <= applied.get(int(row["index"]), set())
            for row in current
        )
        confirmed = [
            {"index": index, "gpu_uuid": gpu_uuids[index], "reset": sorted(reset_fields - current_fields.get(index, set()))}
            for index, reset_fields in reset.items()
            if index in gpu_uuids and reset_fields - current_fields.get(index, set())
        ]
        exc.command_recovery = {
            "status": "failed", "confirmed_reset": confirmed,
            "hardware_reset_required": False, "reset_required": False,
            "hardware_fault": False,
            "nvml_codes": [], "write_attempted": completed.get("write_attempted") is True,
            "recovered": False, "state_reconciled": False, "persistence_failed": True,
            "hardware_state": (
                "requested_settings_applied" if current else "defaults_restored"
            ) if known and (previous or current) else "unknown",
            "errors": ["GPU OC state could not be persisted after the hardware transition"],
        }
        exc.command_gpu_uuids = dict(gpu_uuids)
        raise


def _record_gpu_oc_recovery(
    config: AgentConfig,
    exc: GpuOcError,
    attempted: list[dict[str, Any]],
    expected_gpu_uuids: Mapping[int, str],
) -> None:
    """Retire only fields whose UUID-bound compensation restored defaults.

    The failed desired request is never saved or replayed.  An incomplete
    compensation keeps the remaining owned state available for an explicit
    later reset, and a state-write failure never triggers another GPU write.
    """

    raw = getattr(exc, "recovery", None)
    if not isinstance(raw, Mapping):
        raw = {"status": "failed"}
    statuses = {"rolled_back", "defaults_restored", "partial", "failed"}
    status = raw.get("status")
    fault = _gpu_oc_fault_details(exc)
    recovery: dict[str, Any] = {
        "status": status if isinstance(status, str) and status in statuses else "failed",
        "confirmed_reset": [],
        "hardware_reset_required": bool(
            raw.get("hardware_reset_required") is True
            or fault["reset_required"]
        ),
        **fault,
        "errors": [
            error[:256] for error in raw.get("errors", [])[:8]
            if isinstance(error, str)
        ] if isinstance(raw.get("errors", []), (list, tuple)) else [],
        "state_reconciled": True,
        "write_attempted": getattr(exc, "write_attempted", False) is True,
    }
    allowed: dict[int, set[str]] = {}
    for setting in attempted:
        allowed.setdefault(int(setting["index"]), set()).update(
            set(setting) & _GPU_OC_RECOVERY_FIELDS
        )
    confirmed = raw.get("confirmed_reset", [])
    invalid = not isinstance(confirmed, (list, tuple)) or len(confirmed) > 32
    if not invalid:
        for item in confirmed:
            if not isinstance(item, Mapping):
                invalid = True
                continue
            index, fields = item.get("index"), item.get("reset")
            uuid = expected_gpu_uuids.get(index) if type(index) is int else None
            claimed_uuid = item.get("_expected_gpu_uuid", uuid)
            if (
                uuid is None or claimed_uuid != uuid
                or not isinstance(fields, (list, tuple)) or not fields
                or len(fields) > len(_GPU_OC_RECOVERY_FIELDS)
                or any(not isinstance(field, str) for field in fields)
                or not set(fields) <= allowed.get(index, set())
            ):
                invalid = True
                continue
            recovery["confirmed_reset"].append({
                "index": index, "gpu_uuid": uuid, "reset": sorted(set(fields)),
            })
    if invalid:
        recovery["state_reconciled"] = False
        recovery["errors"].append("GPU OC recovery identity or fields could not be verified")
    try:
        if recovery["confirmed_reset"]:
            _previous, devices, version = _load_gpu_oc_state_record(config)
            if version != 2:
                raise RuntimeError("legacy GPU OC state lacks complete UUID ownership")
            updated = {uuid: dict(setting) for uuid, setting in devices.items()}
            for item in recovery["confirmed_reset"]:
                owned = updated.get(item["gpu_uuid"])
                if owned is None:
                    continue
                for field in item["reset"]:
                    owned.pop(field, None)
                if set(owned) == {"index"}:
                    updated.pop(item["gpu_uuid"])
            if updated != devices:
                _write_gpu_oc_state(
                    config, [updated[uuid] for uuid in sorted(updated)], updated,
                )
    except (OSError, RuntimeError, ValueError):
        recovery["state_reconciled"] = False
        recovery["errors"].append("GPU OC recovery state could not be persisted")
    recovery["errors"] = recovery["errors"][:8]
    recovery["recovered"] = bool(
        recovery["status"] in {"rolled_back", "defaults_restored"}
        and not recovery["hardware_reset_required"]
        and not recovery["hardware_fault"]
        and not recovery["errors"] and recovery["state_reconciled"]
    )
    target = getattr(exc, "failure_target", None)
    if isinstance(target, Mapping):
        index = target.get("index")
        uuid = expected_gpu_uuids.get(index) if type(index) is int else None
        operation = target.get("operation")
        if (
            type(index) is int and index in allowed
            and operation in {"query", "apply", "reset"}
            and ("_expected_gpu_uuid" not in target or target["_expected_gpu_uuid"] == uuid)
        ):
            recovery["failure_target"] = {
                "index": index, "operation": operation, "identity_verified": False,
            }
            if uuid is not None:
                recovery["failure_target"]["requested_gpu_uuid"] = uuid
            field = target.get("field")
            if isinstance(field, str) and field in allowed[index]:
                recovery["failure_target"]["field"] = field
    exc.command_recovery = recovery
    exc.command_gpu_uuids = dict(expected_gpu_uuids)


def _record_gpu_oc_identity_failure(
    exc: GpuOcError, index: int | None = None, requested_uuid: str | None = None,
    *, phase: str = "identity_verification",
) -> None:
    """Keep a typed read failure without claiming the requested UUID was read."""

    fault = _gpu_oc_fault_details(exc)
    exc.command_recovery = {
        "status": "failed", "confirmed_reset": [], "errors": [],
        **fault, "hardware_reset_required": fault["reset_required"],
        "write_attempted": False, "recovered": False,
        "phase": phase, "identity_verified": False,
        # This read-only failure leaves the prior journal untouched. It does
        # not prove the saved settings or physical GPU are healthy.
        "state_reconciled": True, "state_unchanged": True,
    }
    if index is not None:
        exc.command_recovery["failure_target"] = {
            "index": index, "operation": "query", "phase": phase,
            "identity_verified": False,
        }
        if requested_uuid is not None:
            exc.command_recovery["failure_target"]["requested_gpu_uuid"] = requested_uuid
    exc.command_gpu_uuids = {}


def _gpu_oc_request_metadata(command: Command, gpu_uuids: Mapping[int, str]) -> dict[str, Any]:
    """Fingerprint hardware intent so the Controller can hold the same fault."""

    def digest(value: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()

    settings = [
        {
            key: int(value) if isinstance(value, float) and value.is_integer() else value
            for key, value in setting.items()
            if key == "index" or key in _GPU_OC_RECOVERY_FIELDS
        }
        for setting in command.payload["settings"]
    ]
    settings.sort(key=lambda setting: setting["index"])
    mode = command.payload.get("mode", "replace_all")
    intent: dict[str, Any] = {"vendor": "nvidia", "mode": mode, "settings": settings}
    if "expected_gpu_uuid" in command.payload:
        intent["expected_gpu_uuid"] = command.payload["expected_gpu_uuid"]
    targets = []
    for setting in settings:
        target = {"vendor": "nvidia", "mode": mode, "index": setting["index"], "setting": setting}
        uuid = command.payload.get("expected_gpu_uuid", gpu_uuids.get(setting["index"]))
        if uuid is not None:
            target["expected_gpu_uuid"] = uuid
        targets.append({
            "index": setting["index"],
            **({"requested_gpu_uuid": uuid} if uuid is not None else {}),
            # A target and its fingerprint describe intent, never physical
            # identity proof. Successful bound default writes have their own
            # confirmed_reset receipt.
            "identity_verified": False,
            "setting_fingerprint": digest(target),
        })
    return {
        "command_id": command.command_id,
        "request_fingerprint": digest(intent),
        "targets": targets,
    }


def _gpu_oc_failure_result(exc: BaseException, command: Command | None = None) -> dict[str, Any]:
    """Keep a bounded recovery receipt when a flight rollback wraps the error."""

    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    for _ in range(8):
        if not pending:
            break
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        recovery = getattr(current, "command_recovery", None)
        if isinstance(recovery, dict):
            if command is not None:
                recovery = dict(recovery)
                recovery["command_id"] = command.command_id
                if command.action == "apply_gpu_profile":
                    recovery.update(_gpu_oc_request_metadata(
                        command, getattr(current, "command_gpu_uuids", {}),
                    ))
            return {"gpu_oc_recovery": recovery}
        pending.extend(
            cause for cause in (current.__cause__, current.__context__)
            if cause is not None and id(cause) not in seen
        )
    return {}


def _enumerate_gpu_oc_device_indices(backend: Any) -> dict[str, int]:
    enumerator = getattr(backend, "enumerate_device_indices", None)
    if not callable(enumerator):
        raise RuntimeError(
            "configured GPU backend cannot enumerate device identity"
        )
    try:
        current = enumerator()
    except GpuOcError as exc:
        _record_gpu_oc_identity_failure(exc, phase="identity_enumeration")
        raise
    if (
        not isinstance(current, Mapping)
        or len(current) > 32
        or any(
            not isinstance(gpu_uuid, str)
            or not 4 <= len(gpu_uuid) <= 128
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in gpu_uuid
            )
            or isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= 31
            for gpu_uuid, index in current.items()
        )
        or len(set(current.values())) != len(current)
    ):
        raise RuntimeError("GPU OC device inventory is invalid")
    return {str(gpu_uuid): int(index) for gpu_uuid, index in current.items()}


def _bind_gpu_oc_settings_to_current_devices(
    backend: Any,
    settings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Bind an index-addressed saved profile to one current UUID snapshot."""

    current = _enumerate_gpu_oc_device_indices(backend)
    uuid_by_index = {index: gpu_uuid for gpu_uuid, index in current.items()}
    devices: dict[str, dict[str, Any]] = {}
    for setting in settings:
        index = int(setting["index"])
        gpu_uuid = uuid_by_index.get(index)
        if gpu_uuid is None:
            raise RuntimeError(
                f"GPU index {index} is not present in the current device inventory"
            )
        devices[gpu_uuid] = dict(setting)
    return devices, current


@dataclass(frozen=True)
class _GpuOcResetPlan:
    resolved: list[dict[str, Any]]
    resolved_devices: dict[str, dict[str, Any]]
    deferred: list[dict[str, Any]]
    deferred_devices: dict[str, dict[str, Any]]


def _plan_gpu_oc_state_reset(
    backend: Any,
    previous: list[dict[str, Any]],
    devices: Mapping[str, Mapping[str, Any]],
) -> _GpuOcResetPlan:
    if not previous:
        return _GpuOcResetPlan([], {}, [], {})
    if not devices or len(devices) != len(previous):
        raise RuntimeError(
            "旧版 GPU OC 状态缺少完整 UUID 绑定，无法安全重置 "
            "(GPU OC reset requires complete UUID-owned state)"
        )
    if sorted(json.dumps(setting, sort_keys=True) for setting in previous) != sorted(
        json.dumps(dict(setting), sort_keys=True) for setting in devices.values()
    ):
        raise RuntimeError("GPU OC reset identity coverage is incomplete")

    expected_uuids = sorted(devices)
    enumerator = getattr(backend, "enumerate_device_indices", None)
    if callable(enumerator):
        current = _enumerate_gpu_oc_device_indices(backend)
        resolved = {
            gpu_uuid: int(current[gpu_uuid])
            for gpu_uuid in expected_uuids
            if gpu_uuid in current
        }
    else:
        resolver = getattr(backend, "resolve_device_indices", None)
        if callable(resolver):
            try:
                resolved = dict(resolver(expected_uuids))
            except GpuOcError as exc:
                _record_gpu_oc_identity_failure(exc, phase="identity_resolution")
                raise
        else:
            verify_device_uuid = getattr(backend, "verify_device_uuid", None)
            if not callable(verify_device_uuid):
                raise RuntimeError(
                    "configured GPU backend cannot resolve persisted device identity"
                )
            resolved = {}
            for gpu_uuid, setting in devices.items():
                index = int(setting["index"])
                try:
                    verify_device_uuid(index, gpu_uuid)
                except GpuOcError as exc:
                    _record_gpu_oc_identity_failure(exc, index, gpu_uuid)
                    raise
                resolved[gpu_uuid] = index
    if (
        not set(resolved) <= set(devices)
        or any(isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 31 for index in resolved.values())
        or len(set(resolved.values())) != len(resolved)
    ):
        raise RuntimeError("GPU OC device identities did not resolve uniquely")
    resolved_devices = {
        gpu_uuid: {**devices[gpu_uuid], "index": resolved[gpu_uuid]}
        for gpu_uuid in expected_uuids
        if gpu_uuid in resolved
    }
    deferred_devices = {
        gpu_uuid: dict(devices[gpu_uuid])
        for gpu_uuid in expected_uuids
        if gpu_uuid not in resolved
    }
    return _GpuOcResetPlan(
        [resolved_devices[gpu_uuid] for gpu_uuid in sorted(resolved_devices)],
        resolved_devices,
        [deferred_devices[gpu_uuid] for gpu_uuid in sorted(deferred_devices)],
        deferred_devices,
    )


def _resolve_gpu_oc_state_for_reset(
    backend: Any,
    previous: list[dict[str, Any]],
    devices: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Strict wrapper retained for migrations that require every UUID."""

    plan = _plan_gpu_oc_state_reset(backend, previous, devices)
    if plan.deferred_devices:
        raise RuntimeError(
            "GPU UUID is no longer reported: "
            + ", ".join(sorted(plan.deferred_devices))
        )
    return plan.resolved


def _authoritative_device_gpu_oc_settings(
    devices: Mapping[str, Mapping[str, Any]],
    update: dict[str, Any],
    *,
    expected_gpu_uuid: str,
    previous_index: int,
    reset: bool,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Move UUID-owned state without ever resetting the old numeric index."""

    current_index = int(update["index"])
    owned_setting = devices.get(expected_gpu_uuid)
    # Only reset/apply on current_index, whose UUID was verified immediately
    # before this helper.  An old index may now belong to another card, so it
    # is removed from the record only and is never passed to NVML.
    transition_previous = []
    if owned_setting is not None:
        transition_previous = [{**owned_setting, "index": current_index}]
    elif reset:
        # A delete command carries the controller's full saved setting.  Its
        # field set is a safe reset template after current UUID verification.
        transition_previous = [dict(update)]

    persisted_devices = {
        gpu_uuid: dict(setting) for gpu_uuid, setting in devices.items()
        if gpu_uuid != expected_gpu_uuid
    }
    transition_current: list[dict[str, Any]] = []
    if not reset:
        replacement = dict(update)
        persisted_devices[expected_gpu_uuid] = replacement
        transition_current = [replacement]
    persisted = [persisted_devices[gpu_uuid] for gpu_uuid in sorted(persisted_devices)]
    return persisted, persisted_devices, transition_previous, transition_current


class CommandExecutor:
    """Authorize and execute the strictly supported operations.

    ``subprocess`` always receives an argument vector and ``shell=False``. There
    is deliberately no generic command or script operation.
    """

    def __init__(
        self,
        config: AgentConfig,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        gpu_backend_factory: Callable[[], Any] = NvmlGpuOcBackend,
        package_installer: Callable[..., dict[str, Any]] = install_miner_package,
        managed_miner_factory: Callable[[AgentConfig], Any] = ManagedMinerManager,
        agent_update_manager: Any | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.gpu_backend_factory = gpu_backend_factory
        self.package_installer = package_installer
        self.managed_miner_factory = managed_miner_factory
        self.agent_update_manager = agent_update_manager

    def authorize(self, command: Command) -> None:
        if command.action == LINUX_SSH_CONFIGURE_ACTION:
            try:
                validate_linux_ssh_payload(command.payload)
            except LinuxSshError as exc:
                raise CommandRejected(str(exc)) from exc
            return
        if command.action == AGENT_UPDATE_ACTION:
            if not self.config.allow_agent_updates:
                raise CommandRejected(
                    "agent updates are disabled by local configuration"
                )
            try:
                validate_agent_update_payload(command.payload)
            except AgentUpdateError as exc:
                raise CommandRejected(str(exc)) from exc
            return
        if command.action == GPU_OC_STATE_MIGRATION_ACTION:
            _validate_gpu_oc_state_migration_payload(command.payload, self.config)
            return
        if command.action == UPDATE_FORENSICS_ACTION:
            _validate_update_forensics_payload(command.payload)
            # The three whitelisted commands are Linux system tooling (df,
            # systemctl, journalctl); Windows gets the same fast rejected
            # result as any other unsupported platform instead of a new
            # capability surface.
            if platform.system() != "Linux":
                raise CommandRejected(
                    "update forensics collection is Linux-only"
                )
            return
        try:
            if command.action == "apply_flight_sheet":
                validate_flight_sheet(command.payload, self.config)
                return
            if command.action == "apply_gpu_profile":
                validate_gpu_profile(command.payload, self.config)
                return
        except MiningCommandError as exc:
            raise CommandRejected(str(exc)) from exc
        if command.action in {
            "collect_diagnostics",
            "collect_miner_logs",
            "stop_mining",
            "restart_mining",
            "remove_flight_sheet",
            MINING_RESIDUE_SCAN_ACTION,
            MINER_CACHE_SCAN_ACTION,
        }:
            _ensure_exact_payload(command.payload, set())
            return
        if command.action == MINER_CACHE_CLEANUP_ACTION:
            if (not isinstance(command.payload, dict)
                    or set(command.payload) != {"scan_fingerprint"}
                    or not isinstance(command.payload["scan_fingerprint"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", command.payload["scan_fingerprint"]) is None):
                raise CommandRejected("cache cleanup requires one lowercase scan_fingerprint")
            return
        if command.action == MINING_RESIDUE_CLEANUP_ACTION:
            _validate_residue_cleanup_payload(command.payload)
            return
        if command.action == "restart_service":
            _ensure_exact_payload(command.payload, {"service"})
            service = command.payload.get("service")
            if not isinstance(service, str) or not service:
                raise CommandRejected("service must be a non-empty string")
            if service not in self.config.service_allowlist:
                raise CommandRejected("service is not in the local allowlist")
            return
        _ensure_exact_payload(command.payload, set())
        if command.action == "reboot" and not self.config.allow_reboot:
            raise CommandRejected("reboot is disabled by local configuration")
        if command.action == "shutdown" and not self.config.allow_shutdown:
            raise CommandRejected("shutdown is disabled by local configuration")

    def _service_commands(self, service: str) -> Sequence[list[str]]:
        if platform.system() == "Windows":
            return (["sc.exe", "stop", service], ["sc.exe", "start", service])
        return (["systemctl", "restart", service],)

    def _power_command(self, action: str) -> list[str]:
        if platform.system() == "Windows":
            return [
                _windows_system32_binary("shutdown.exe"),
                "/r" if action == "reboot" else "/s",
                "/f",
                "/t",
                str(WINDOWS_POWER_DELAY_SECONDS),
                "/c",
                "RigPilot remote power action",
            ]
        if not (
            _linux_trusted_system_executable(LINUX_SYSTEMD_RUN)
            and _linux_trusted_system_executable(LINUX_SYSTEMCTL)
        ):
            raise RuntimeError("trusted Linux systemd power tools are unavailable")
        return [
            str(LINUX_SYSTEMD_RUN),
            "--unit",
            f"rigpilot-host-{action}-{os.getpid()}",
            "--collect",
            "--quiet",
            "--no-block",
            f"--on-active={LINUX_POWER_DELAY_SECONDS}s",
            "--property=Type=oneshot",
            "--property=User=root",
            "--property=Group=root",
            "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes",
            "--property=ProtectHome=yes",
            "--property=ProtectSystem=strict",
            str(LINUX_SYSTEMCTL),
            "reboot" if action == "reboot" else "poweroff",
        ]

    def _run(self, argv: list[str]) -> dict[str, Any]:
        completed = self.runner(
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=external_process_environment(platform.system()),
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise RuntimeError(
                f"operation returned exit code {completed.returncode}"
                + (f": {stderr[:1000]}" if stderr else "")
            )
        return {"return_code": completed.returncode}

    def _collect_update_forensics(self, block: Mapping[str, Any]) -> dict[str, Any]:
        """Run the fixed read-only update-forensics whitelist (Linux only).

        The validated payload block is echoed as provenance only; execution
        always uses the module-level ``UPDATE_FORENSICS_COMMANDS`` literals,
        so no payload text can reach a shell.  A missing recovery timer
        legitimately makes the grep pipeline exit 1, so per-command exit
        codes are reported instead of failing the whole collection; only an
        inability to run a command at all is recorded as a per-command
        error.  Output is bounded so the result stays under the
        controller's command-result body limit.
        """

        entries: list[dict[str, Any]] = []
        for literal in UPDATE_FORENSICS_COMMANDS:
            entry: dict[str, Any] = {"command": literal}
            output = ""
            try:
                completed = self.runner(
                    ["/bin/bash", "-c", literal],
                    shell=False,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=UPDATE_FORENSICS_TIMEOUT_SECONDS,
                    env=external_process_environment(platform.system()),
                )
                entry["return_code"] = completed.returncode
                output = (completed.stdout or "") + (completed.stderr or "")
            except subprocess.TimeoutExpired:
                entry["return_code"] = None
                entry["error"] = "command timed out"
            except (OSError, ValueError) as exc:
                entry["return_code"] = None
                entry["error"] = f"command could not run: {exc}"[:300]
            if len(output) > UPDATE_FORENSICS_OUTPUT_LIMIT_CHARS:
                entry["output"] = output[:UPDATE_FORENSICS_OUTPUT_LIMIT_CHARS]
                entry["output_truncated"] = True
            else:
                entry["output"] = output
            entries.append(entry)
        return {
            "platform": "linux",
            "trigger": block["trigger"],
            "update_command_id": block["command_id"],
            "source_version": block["source_version"],
            "target_version": block["target_version"],
            "commands": entries,
        }

    def _reset_standalone_gpu_oc(self) -> dict[str, Any]:
        """Restore defaults for RigPilot-owned NVML state after mining stops."""

        previous, devices, state_version = _load_gpu_oc_state_record(self.config)
        if not previous:
            if state_version == 1:
                _write_gpu_oc_state(self.config, [], {})
            return {
                "vendor": "nvidia",
                "settings_count": 0,
                "reset_settings_count": 0,
                "reset": [],
                "deferred_gpu_uuids": [],
                "state_cleared": True,
            }
        backend = self.gpu_backend_factory()
        plan = _plan_gpu_oc_state_reset(backend, previous, devices)
        uuid_by_index = {
            int(setting["index"]): gpu_uuid
            for gpu_uuid, setting in plan.resolved_devices.items()
        }
        result = (
            _transition_gpu_oc(backend, plan.resolved, [], uuid_by_index, config=self.config)
            if plan.resolved
            else {"vendor": "nvidia", "operations": [], "reset": []}
        )
        if plan.resolved:
            _persist_gpu_oc_transition(
                self.config, plan.deferred, plan.deferred_devices,
                plan.resolved, [], uuid_by_index, result,
            )
        result["settings_count"] = len(previous)
        result["reset_settings_count"] = len(plan.resolved)
        result["deferred_gpu_uuids"] = sorted(plan.deferred_devices)
        result["state_cleared"] = not plan.deferred_devices
        return result

    def _stop_miner_and_clear_srb_oc(
        self, manager: Any
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None, str | None]:
        """Stop the owned miner before independently clearing its SRB OC."""

        metadata_error: str | None = None
        try:
            srb_required = _active_flight_uses_srb_gpu_oc(self.config)
        except (OSError, RuntimeError) as exc:
            # Broken sheet metadata does not prevent stopping the exact
            # process whose identity is independently bound by its record.
            srb_required = False
            metadata_error = str(exc)
        miner_result = dict(manager.stop())
        srb_reset: dict[str, Any] | None = None
        srb_error = metadata_error
        if srb_required:
            clearer = getattr(manager, "clear_active_srb_gpu_oc", None)
            if not callable(clearer):
                srb_error = "managed miner cannot clear active SRBMiner GPU OC"
            else:
                try:
                    # Explicit stop/removal must never restore the miner merely
                    # because the independent OC reset later fails.
                    srb_reset = dict(clearer(restart_on_failure=False))
                    if not srb_reset.get("cleared"):
                        srb_error = "active SRBMiner GPU OC reset was not confirmed"
                except (OSError, RuntimeError) as exc:
                    srb_error = str(exc)
        return miner_result, srb_reset, srb_error, metadata_error

    def _finish_mining_stop_with_gpu_reset(
        self,
        command: Command,
        miner_result: Mapping[str, Any],
        *,
        srb_reset: Mapping[str, Any] | None = None,
        srb_error: str | None = None,
        configuration_error: str | None = None,
    ) -> CommandOutcome:
        """Reset every RigPilot-owned OC path after mining has already stopped.

        The local applied-state journal is cleared only after hardware reset
        succeeds.  The controller's desired OC profile is separate and remains
        saved so a later start/restart can apply it again.
        """

        result = dict(miner_result)
        errors: list[str] = []
        if srb_reset is not None:
            result["srb_gpu_oc_reset"] = dict(srb_reset)
        if srb_error is not None:
            result["srb_gpu_oc_reset"] = {
                **dict(srb_reset or {}),
                "required": True,
                "cleared": False,
                "status": "failed",
                "error": srb_error,
            }
            errors.append(srb_error)
        if configuration_error is not None:
            result["configuration_removal"] = {
                "status": "failed",
                "configuration_cleared": False,
                "error": configuration_error,
            }
            errors.append(configuration_error)
        try:
            result["gpu_oc_reset"] = self._reset_standalone_gpu_oc()
        except (OSError, RuntimeError) as exc:
            LOGGER.warning(
                "mining stopped but GPU OC reset failed",
                extra={
                    "event": "gpu_oc_reset_after_mining_stop_failed",
                    "command_id": command.command_id,
                },
            )
            result["gpu_oc_reset"] = {
                "status": "failed",
                "state_cleared": False,
                "error": str(exc),
            }
            result.update(_gpu_oc_failure_result(exc, command))
            errors.append(str(exc))
        if errors:
            return CommandOutcome(
                status="failed",
                result=result,
                error="; ".join(dict.fromkeys(errors)),
            )
        return CommandOutcome(status="succeeded", result=result)

    def execute(
        self,
        command: Command,
        *,
        agent_state: AgentState | None = None,
    ) -> CommandOutcome:
        if command.action == "apply_flight_sheet" and self.config.miner_packages_dir is not None:
            from .miner_cache_cleanup import cache_operation_lock, MinerCacheCleanupError
            try:
                # Hold one boundary from package download through the pending
                # and active flight switch and owned process launch. A cleanup
                # in another Agent process cannot delete the new target in the
                # interval between install and committing its reference.
                with cache_operation_lock(self.config.miner_packages_dir, wait_seconds=30.0):
                    return self._execute_authorized(
                        command,
                        agent_state=agent_state,
                    )
            except MinerCacheCleanupError as exc:
                return CommandOutcome(status="failed", result={}, error=exc.reason)
        return self._execute_authorized(
            command,
            agent_state=agent_state,
        )

    def _execute_authorized(
        self,
        command: Command,
        *,
        agent_state: AgentState | None = None,
    ) -> CommandOutcome:
        try:
            self.authorize(command)
        except CommandRejected as exc:
            return CommandOutcome(status="failed", result={"rejected": True}, error=str(exc))

        if command.action == "collect_diagnostics":
            return CommandOutcome(
                status="succeeded",
                result=collect_host_data(
                    self.config.collect_gpu_metrics,
                    self.config.nvidia_smi_path,
                    self.config,
                ),
            )

        if command.action == "collect_miner_logs":
            return CommandOutcome(
                status="succeeded",
                result=collect_managed_miner_logs(self.config),
            )

        if self.config.dry_run:
            return CommandOutcome(
                status="succeeded",
                result={"dry_run": True, "action": command.action},
            )

        try:
            if command.action == UPDATE_FORENSICS_ACTION:
                return CommandOutcome(
                    status="succeeded",
                    result=self._collect_update_forensics(
                        command.payload["_controller_auto_forensics"]
                    ),
                )
            if command.action == MINER_CACHE_SCAN_ACTION:
                report = scan_miner_cache(self.config)
                return CommandOutcome(
                    status="succeeded" if report["scan_fingerprint"] is not None else "failed",
                    result=report,
                    error=None if report["scan_fingerprint"] is not None else report["reason"],
                )
            if command.action == MINER_CACHE_CLEANUP_ACTION:
                report = cleanup_miner_cache(self.config, command.payload["scan_fingerprint"])
                return CommandOutcome(
                    status="succeeded" if report["state"] == "succeeded" else "failed",
                    result=report,
                    error=report["reason"] if report["state"] != "succeeded" else None,
                )
            if command.action == MINING_RESIDUE_SCAN_ACTION:
                manager = self.managed_miner_factory(self.config)
                return CommandOutcome(
                    status="succeeded",
                    result=dict(manager.scan_residue()),
                )
            if command.action == LINUX_SSH_CONFIGURE_ACTION:
                return CommandOutcome(
                    status="succeeded",
                    result=configure_linux_ssh(command.payload),
                )
            if command.action == GPU_OC_STATE_MIGRATION_ACTION:
                candidate_devices, history_command_ids = (
                    _validate_gpu_oc_state_migration_payload(
                        command.payload, self.config
                    )
                )
                previous, existing_devices, state_version = (
                    _load_gpu_oc_state_record(self.config)
                )
                if state_version != 1 or existing_devices or not previous:
                    raise RuntimeError(
                        "GPU OC migration requires a non-empty legacy v1 state"
                    )
                backend = self.gpu_backend_factory()
                if not callable(getattr(backend, "resolve_device_indices", None)):
                    raise RuntimeError(
                        "configured GPU backend cannot resolve migration UUIDs"
                    )
                resolved_settings = _resolve_gpu_oc_state_for_reset(
                    backend,
                    previous,
                    candidate_devices,
                )
                migrated_devices = {
                    gpu_uuid: resolved_settings[position]
                    for position, gpu_uuid in enumerate(sorted(candidate_devices))
                }
                _write_gpu_oc_state(
                    self.config,
                    resolved_settings,
                    migrated_devices,
                )
                return CommandOutcome(
                    status="succeeded",
                    result={
                        "migrated": True,
                        "source_state_version": 1,
                        "target_state_version": 2,
                        "nvml_writes": 0,
                        "history_command_ids": history_command_ids,
                        "devices": [
                            {
                                "gpu_uuid": gpu_uuid,
                                "index": int(migrated_devices[gpu_uuid]["index"]),
                                "setting": {
                                    key: value
                                    for key, value in migrated_devices[gpu_uuid].items()
                                    if key != "index"
                                },
                            }
                            for gpu_uuid in sorted(migrated_devices)
                        ],
                    },
                )
            if command.action == AGENT_UPDATE_ACTION:
                if agent_state is None:
                    raise AgentUpdateError("agent update requires enrolled device state")
                if self.agent_update_manager is None:
                    raise AgentUpdateError("agent update manager is unavailable")
                result = self.agent_update_manager.stage(
                    command.payload, agent_state, command.command_id
                )
                return CommandOutcome(status="succeeded", result=dict(result))
            if command.action == MINING_RESIDUE_CLEANUP_ACTION:
                manager = self.managed_miner_factory(self.config)
                (
                    scan_fingerprint,
                    selected_targets,
                    reset_srbminer_oc,
                    reset_nvml_oc,
                ) = _validate_residue_cleanup_payload(command.payload)
                cleanup = dict(
                    manager.cleanup_residue(scan_fingerprint)
                    if selected_targets is None
                    else manager.cleanup_residue(
                        scan_fingerprint,
                        selected_targets,
                    )
                )
                if cleanup.get("blocked") is True:
                    return CommandOutcome(
                        status="failed",
                        result=cleanup,
                        error=(
                            "managed miner residue cleanup was blocked by "
                            "ambiguous or changed process identity"
                        ),
                    )
                srb_reset: dict[str, Any] = {
                    "required": False,
                    "cleared": False,
                }
                if (
                    reset_srbminer_oc
                    and _active_flight_uses_srb_gpu_oc(self.config)
                ):
                    clearer = getattr(manager, "clear_active_srb_gpu_oc", None)
                    if not callable(clearer):
                        raise ManagedMinerError(
                            "managed miner cannot clear active SRBMiner GPU OC"
                        )
                    srb_reset = dict(clearer())
                    if not srb_reset.get("cleared"):
                        raise ManagedMinerError(
                            "active SRBMiner GPU OC reset was not confirmed"
                        )
                elif not reset_srbminer_oc:
                    srb_reset["status"] = "not_selected"
                cleanup["gpu_oc_reset"] = {
                    "srbminer": srb_reset,
                    "nvml": (
                        self._reset_standalone_gpu_oc()
                        if reset_nvml_oc
                        else {
                            "status": "not_selected",
                            "state_cleared": False,
                        }
                    ),
                }
                cleanup["reset_options"] = {
                    "srbminer": reset_srbminer_oc,
                    "nvml": reset_nvml_oc,
                }
                return CommandOutcome(status="succeeded", result=cleanup)
            if command.action == "apply_flight_sheet":
                document = validate_flight_sheet(command.payload, self.config)
                assert self.config.flight_sheet_file is not None
                previous_srb_gpu_oc = _active_flight_uses_srb_gpu_oc(self.config)
                installed_packages: list[dict[str, Any]] = []
                install_cache: dict[str, dict[str, Any]] = {}
                for role in ("primary", "secondary"):
                    leg = document[role]
                    if leg is None or leg["miner_package"] is None:
                        continue
                    metadata = leg["miner_package"]
                    package_key = json.dumps(
                        metadata,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    cached = package_key in install_cache
                    if not cached:
                        install_options = {"state": agent_state} if agent_state else {}
                        install_cache[package_key] = dict(
                            self.package_installer(
                                metadata, self.config, **install_options
                            )
                        )
                    installed = install_cache[package_key]
                    installed_packages.append(
                        {**installed, "role": role, "reused": cached}
                    )
                    leg["miner_package"] = {
                        **metadata,
                        "installed_path": installed["path"],
                    }
                active_path = self.config.flight_sheet_file
                pending_path = active_path.with_name(f".{active_path.name}.pending")
                try:
                    pending_path.unlink()
                except FileNotFoundError:
                    pass
                result = write_flight_sheet(document, pending_path)
                operations: list[dict[str, Any]] = []
                managed_result: dict[str, Any] | None = None
                managed_manager: Any | None = None
                managed_metadata_snapshot: (
                    dict[Path, dict[str, Any] | None] | None
                ) = None
                managed_transition_begun = False
                managed_transition_target_id = str(
                    document["flight_sheet_id"]
                )
                previous_managed_was_running = False
                srb_cli_reset_result: dict[str, Any] | None = None
                gpu_oc_result: dict[str, Any] | None = None
                try:
                    (
                        previous_gpu_settings,
                        previous_gpu_devices,
                        _previous_gpu_state_version,
                    ) = _load_gpu_oc_state_record(self.config)
                except Exception:
                    try:
                        pending_path.unlink()
                    except FileNotFoundError:
                        pass
                    raise
                flight_gpu_settings = (
                    document["gpu_oc"]["settings"]
                    if document["gpu_oc"] is not None
                    else []
                )
                gpu_backend: Any | None = None
                srb_gpu_flight = _flight_uses_srb_gpu(document)
                # A managed SRBMiner GPU flight owns the card's OC state.  It
                # must first clear a standalone NVML profile even when the new
                # flight deliberately declares no OC; otherwise an earlier
                # direct power/clock limit remains active while the UI says
                # that flight OC is off.  CPU-only SRBMiner and XMRig flights
                # do not take GPU ownership and preserve the standalone state.
                gpu_transitioned = bool(srb_gpu_flight and previous_gpu_settings)
                # ``--gpu-reset-oc`` is deliberately idempotent.  Do not rely
                # only on the previous active-flight metadata here: a prior
                # SRBMiner may have applied OC and then exited before the
                # pending sheet was committed, or an earlier reset attempt may
                # have failed while the already-saved sheet says "no OC".
                # Linux and Windows no-OC flights leave ownership with device
                # NVML tuning; only recorded CLI OC requests the SRBMiner reset. A
                # reset restores NVIDIA defaults and then applies any selected
                # flight settings.
                reset_srb_gpu_oc = bool(
                    srb_gpu_flight
                    and (
                        previous_srb_gpu_oc
                        or _document_uses_srb_gpu_oc(
                            document,
                            local_oc_enabled=self.config.allow_gpu_oc,
                            system_name=platform.system(),
                        )
                    )
                )
                reset_result: dict[str, Any] | None = None
                reset_plan: _GpuOcResetPlan | None = None
                reset_uuid_by_index: dict[int, str] = {}
                standalone_gpu_reset_performed = False
                standalone_gpu_reapply_result: dict[str, Any] | None = None
                standalone_gpu_reapply_deferred: list[str] = []
                nvml_handoff_devices: dict[str, dict[str, Any]] = {}
                nvml_handoff_result: dict[str, Any] | None = None
                runtime_document = dict(document)
                if reset_srb_gpu_oc:
                    runtime_document["_reset_srb_gpu_oc"] = True
                try:
                    if (platform.system() in {"Linux", "Windows"} and srb_gpu_flight
                            and previous_srb_gpu_oc and not flight_gpu_settings
                            and previous_gpu_devices):
                        gpu_backend = self.gpu_backend_factory()
                        snapshotter = getattr(gpu_backend, "enumerate_device_identities", None)
                        if callable(snapshotter):
                            try:
                                nvml_handoff_devices = _covered_srb_cli_nvml_devices(
                                    _active_flight_gpu_oc_document(self.config),
                                    previous_gpu_devices,
                                    snapshotter(),
                                )
                            except (OSError, RuntimeError, TypeError, ValueError):
                                nvml_handoff_devices = {}
                        if nvml_handoff_devices:
                            # The old CLI actor must first exit. Reapply its fully
                            # covered UUID-owned NVML profiles after apply() stops
                            # it; the new worker never receives a reset flag.
                            reset_srb_gpu_oc = False
                            gpu_transitioned = False
                            runtime_document.pop("_reset_srb_gpu_oc", None)
                    # Flight-scoped OC is applied by SRBMiner's documented
                    # --gpu-*0/1 arguments. NVML is used here only to clear a
                    # previously RigPilot-owned profile before the miner starts.
                    if gpu_transitioned:
                        gpu_backend = self.gpu_backend_factory()
                        reset_plan = _plan_gpu_oc_state_reset(
                            gpu_backend,
                            previous_gpu_settings,
                            previous_gpu_devices,
                        )
                        reset_uuid_by_index = {
                            int(setting["index"]): gpu_uuid
                            for gpu_uuid, setting in reset_plan.resolved_devices.items()
                        }
                        if reset_plan.resolved:
                            reset_result = _transition_gpu_oc(
                                gpu_backend,
                                reset_plan.resolved,
                                [],
                                reset_uuid_by_index,
                                config=self.config,
                            )
                            standalone_gpu_reset_performed = True
                            _persist_gpu_oc_transition(
                                self.config, reset_plan.deferred, reset_plan.deferred_devices,
                                reset_plan.resolved, [], reset_uuid_by_index, reset_result,
                            )
                    needs_srb_transition_reset = bool(
                        previous_srb_gpu_oc and not srb_gpu_flight
                    )
                    if (
                        document["primary"].get("miner_type") is not None
                        or needs_srb_transition_reset
                    ):
                        managed_manager = self.managed_miner_factory(self.config)
                        snapshotter = getattr(
                            managed_manager,
                            "snapshot_independent_metadata",
                            None,
                        )
                        if callable(snapshotter):
                            managed_metadata_snapshot = dict(snapshotter())
                        running_reader = getattr(
                            managed_manager,
                            "transition_has_live_processes",
                            None,
                        )
                        if not callable(running_reader):
                            running_reader = getattr(
                                managed_manager,
                                "running",
                                None,
                            )
                        if callable(running_reader):
                            previous_managed_was_running = bool(running_reader())
                        transition_beginner = getattr(
                            managed_manager,
                            "begin_flight_transition",
                            None,
                        )
                        if callable(transition_beginner):
                            transition_beginner(
                                runtime_document,
                                previous_was_running=(
                                    previous_managed_was_running
                                ),
                            )
                            managed_transition_begun = True
                            transition_marker = getattr(
                                managed_manager,
                                "mark_flight_transition_phase",
                                None,
                            )
                            if not callable(transition_marker):
                                raise ManagedMinerError(
                                    "managed miner transition journal cannot "
                                    "record mutation"
                                )
                            transition_marker(
                                managed_transition_target_id,
                                "mutating",
                            )
                        if needs_srb_transition_reset:
                            clearer = getattr(
                                managed_manager,
                                "clear_active_srb_gpu_oc",
                                None,
                            )
                            if not callable(clearer):
                                raise ManagedMinerError(
                                    "managed miner cannot clear previous SRBMiner GPU OC"
                                )
                            srb_cli_reset_result = dict(clearer())
                            if not srb_cli_reset_result.get("cleared"):
                                raise ManagedMinerError(
                                    "previous SRBMiner GPU OC reset was not confirmed"
                                )
                            previous_managed_was_running = bool(
                                srb_cli_reset_result.get("previous_was_running")
                            )
                    if document["primary"].get("miner_type") is not None:
                        assert managed_manager is not None
                        managed_result = dict(
                            managed_manager.apply(runtime_document)
                        )
                        previous_managed_was_running = bool(
                            previous_managed_was_running
                            or managed_result.get("replaced_previous")
                        )
                    elif self.config.miner_service:
                        operations = [
                            self._run(argv)
                            for argv in self._service_commands(self.config.miner_service)
                        ]
                    if nvml_handoff_devices:
                        assert gpu_backend is not None
                        affected = list(nvml_handoff_devices.values())
                        bindings = {
                            int(setting["index"]): uuid
                            for uuid, setting in nvml_handoff_devices.items()
                        }
                        nvml_handoff_result = _transition_gpu_oc(
                            gpu_backend, affected, affected, bindings,
                            config=self.config,
                        )
                        updated_devices = {**previous_gpu_devices, **nvml_handoff_devices}
                        _persist_gpu_oc_transition(
                            self.config,
                            [updated_devices[uuid] for uuid in sorted(updated_devices)],
                            updated_devices,
                            affected, affected, bindings, nvml_handoff_result,
                        )
                    if managed_transition_begun:
                        transition_marker = getattr(
                            managed_manager,
                            "mark_flight_transition_phase",
                            None,
                        )
                        if not callable(transition_marker):
                            raise ManagedMinerError(
                                "managed miner transition journal cannot "
                                "record readiness"
                            )
                        transition_marker(
                            managed_transition_target_id,
                            "target_ready",
                        )
                    # The transient SRBMiner ``--gpu-reset-oc`` intentionally
                    # restores vendor defaults. If an independently managed
                    # NVML profile was saved while SRBMiner was active, put its
                    # currently present UUIDs back after the new non-SRB miner
                    # is confirmed running. The desired state file is never
                    # deleted or rewritten by this transition.
                    if srb_cli_reset_result is not None and previous_gpu_settings:
                        if gpu_backend is None:
                            gpu_backend = self.gpu_backend_factory()
                        reapply_plan = _plan_gpu_oc_state_reset(
                            gpu_backend,
                            previous_gpu_settings,
                            previous_gpu_devices,
                        )
                        reapply_uuid_by_index = {
                            int(setting["index"]): gpu_uuid
                            for gpu_uuid, setting in reapply_plan.resolved_devices.items()
                        }
                        if reapply_plan.resolved:
                            standalone_gpu_reapply_result = _transition_gpu_oc(
                                gpu_backend,
                                [],
                                reapply_plan.resolved,
                                reapply_uuid_by_index,
                                config=self.config,
                            )
                        standalone_gpu_reapply_deferred = sorted(
                            reapply_plan.deferred_devices
                        )
                    if flight_gpu_settings:
                        gpu_oc_result = {
                            "vendor": "nvidia",
                            "source": "srbminer_cli",
                            "settings_count": len(flight_gpu_settings),
                            "indices": [
                                setting["index"]
                                for setting in flight_gpu_settings
                                if "index" in setting
                            ],
                            "models": [
                                setting["model_match"]
                                for setting in flight_gpu_settings
                                if "model_match" in setting
                            ],
                            "reset": (reset_result or {}).get("reset", []),
                        }
                    elif nvml_handoff_result is not None:
                        gpu_oc_result = {
                            "vendor": "nvidia",
                            "source": "nvml_handoff",
                            "settings_count": len(nvml_handoff_devices),
                            "standalone_nvml_reapplied": True,
                            "reset": nvml_handoff_result.get("reset", []),
                            "operations": nvml_handoff_result.get("operations", []),
                        }
                    elif reset_srb_gpu_oc:
                        gpu_oc_result = {
                            "vendor": "nvidia",
                            "source": "srbminer_cli_reset",
                            "settings_count": 0,
                            "reset": (reset_result or {}).get("reset", []),
                        }
                    elif srb_cli_reset_result is not None:
                        gpu_oc_result = {
                            "vendor": "nvidia",
                            "source": "srbminer_cli_transition_reset",
                            "settings_count": 0,
                            "reset": [],
                            "previous_srb_gpu_oc": srb_cli_reset_result,
                            "standalone_nvml_reapplied": bool(
                                standalone_gpu_reapply_result is not None
                            ),
                        }
                        if standalone_gpu_reapply_deferred:
                            gpu_oc_result["deferred_gpu_uuids"] = (
                                standalone_gpu_reapply_deferred
                            )
                    elif standalone_gpu_reset_performed:
                        gpu_oc_result = {
                            "vendor": "nvidia",
                            "source": "nvml_reset",
                            "settings_count": 0,
                            "reset": (reset_result or {}).get("reset", []),
                        }
                    if reset_plan is not None and reset_plan.deferred_devices:
                        if gpu_oc_result is None:
                            gpu_oc_result = {
                                "vendor": "nvidia",
                                "source": "nvml_reset_deferred",
                                "settings_count": 0,
                                "reset": (reset_result or {}).get("reset", []),
                            }
                        gpu_oc_result["deferred_gpu_uuids"] = sorted(
                            reset_plan.deferred_devices
                        )
                        gpu_oc_result["standalone_state_cleared"] = False
                    os.replace(pending_path, active_path)
                    if managed_transition_begun:
                        transition_finisher = getattr(
                            managed_manager,
                            "finish_flight_transition",
                            None,
                        )
                        try:
                            if not callable(transition_finisher):
                                raise ManagedMinerError(
                                    "managed miner transition journal cannot "
                                    "be completed"
                                )
                            transition_finisher(
                                managed_transition_target_id
                            )
                        except Exception:
                            # The root sheet is already atomically committed.
                            # Startup recovery will see that target binding and
                            # consume the journal without stopping the miner.
                            LOGGER.exception(
                                "could not consume committed flight transition journal",
                                extra={
                                    "event": (
                                        "managed_miner_transition_journal_"
                                        "finish_failed"
                                    ),
                                    "command_id": command.command_id,
                                },
                            )
                except Exception as apply_exc:
                    managed_rollback_errors: list[str] = []
                    transition_error = (
                        apply_exc
                        if isinstance(
                            apply_exc, ManagedMinerTransitionError
                        )
                        else None
                    )
                    if transition_error is not None:
                        managed_rollback_errors.extend(
                            transition_error.rollback_errors
                        )
                    managed_state_changed = bool(
                        managed_result is not None
                        or srb_cli_reset_result is not None
                        or (
                            transition_error is not None
                            and transition_error.state_changed
                        )
                    )
                    target_srb_oc_may_have_applied = bool(
                        srb_gpu_flight
                        and (
                            managed_result is not None
                            or (
                                transition_error is not None
                                and transition_error.target_oc_may_have_applied
                            )
                        )
                    )
                    if (
                        managed_manager is not None
                        and managed_state_changed
                    ):
                        # An unconfirmed owned-child exit cannot be overlapped
                        # by a transient SRB reset process. Preserve its rollback
                        # error and phase; unrelated metadata errors do not imply
                        # that this child is still alive.
                        if (
                            target_srb_oc_may_have_applied
                            and not (
                                transition_error is not None
                                and transition_error.target_cleanup_unconfirmed
                            )
                        ):
                            target_clearer = getattr(
                                managed_manager,
                                "clear_srb_gpu_oc_for_document",
                                None,
                            )
                            try:
                                if not callable(target_clearer):
                                    raise ManagedMinerError(
                                        "managed miner cannot roll back target "
                                        "SRBMiner GPU OC"
                                    )
                                target_reset = target_clearer(
                                    runtime_document,
                                    restart_on_failure=False,
                                )
                                if not _srb_gpu_oc_reset_satisfied(target_reset):
                                    raise ManagedMinerError(
                                        "target SRBMiner GPU OC rollback was "
                                        "not confirmed"
                                    )
                            except Exception as exc:
                                managed_rollback_errors.append(str(exc))
                        stopper = getattr(managed_manager, "stop", None)
                        if callable(stopper):
                            try:
                                stopper()
                            except Exception as exc:
                                managed_rollback_errors.append(str(exc))
                        if managed_metadata_snapshot is not None:
                            restorer = getattr(
                                managed_manager,
                                "restore_independent_metadata",
                                None,
                            )
                            try:
                                if not callable(restorer):
                                    raise ManagedMinerError(
                                        "managed miner metadata rollback is unavailable"
                                    )
                                restorer(managed_metadata_snapshot)
                            except Exception as exc:
                                managed_rollback_errors.append(str(exc))
                        if (
                            previous_managed_was_running
                            and not managed_rollback_errors
                        ):
                            restarter = getattr(managed_manager, "restart", None)
                            try:
                                if not callable(restarter):
                                    raise ManagedMinerError(
                                        "managed miner restart rollback is unavailable"
                                    )
                                restarter()
                            except Exception as exc:
                                managed_rollback_errors.append(str(exc))
                        if managed_rollback_errors:
                            LOGGER.error(
                                "managed miner rollback after flight commit failure was incomplete",
                                extra={
                                    "event": "managed_miner_commit_rollback_failed",
                                    "command_id": command.command_id,
                                    "errors": managed_rollback_errors,
                                },
                            )
                    # Roll back only after this command actually changed NVML.
                    # Identity resolution and legacy-state validation fail
                    # before the first write; applying the stale index hints in
                    # that case would touch the wrong physical GPU.
                    gpu_rollback_error: str | None = None
                    recovery = _gpu_oc_failure_result(apply_exc).get("gpu_oc_recovery", {})
                    if (
                        gpu_transitioned and standalone_gpu_reset_performed
                        and not recovery.get("hardware_reset_required")
                        and not recovery.get("hardware_fault")
                        and not recovery.get("persistence_failed")
                    ):
                        try:
                            if gpu_backend is None:
                                gpu_backend = self.gpu_backend_factory()
                            if reset_plan is None:
                                raise RuntimeError("GPU OC rollback plan is unavailable")
                            restored_result = _transition_gpu_oc(
                                gpu_backend,
                                [],
                                reset_plan.resolved,
                                reset_uuid_by_index,
                                config=self.config,
                            )
                            restored_devices = {
                                **reset_plan.deferred_devices,
                                **reset_plan.resolved_devices,
                            }
                            restored_settings = [
                                restored_devices[gpu_uuid]
                                for gpu_uuid in sorted(restored_devices)
                            ]
                            _persist_gpu_oc_transition(
                                self.config,
                                restored_settings,
                                restored_devices,
                                [], reset_plan.resolved, reset_uuid_by_index, restored_result,
                            )
                        except Exception as exc:
                            gpu_rollback_error = str(exc)
                            rollback_recovery = _gpu_oc_failure_result(exc).get("gpu_oc_recovery")
                            if rollback_recovery is not None:
                                apply_exc.command_recovery = rollback_recovery
                            LOGGER.exception(
                                "GPU OC rollback failed",
                                extra={
                                    "event": "gpu_oc_rollback_failed",
                                    "command_id": command.command_id,
                                },
                            )
                    try:
                        pending_path.unlink()
                    except FileNotFoundError:
                        pass
                    if gpu_rollback_error is not None:
                        managed_rollback_errors.append(
                            f"NVML OC rollback: {gpu_rollback_error}"
                        )
                    if (
                        managed_transition_begun
                        and not managed_rollback_errors
                    ):
                        transition_finisher = getattr(
                            managed_manager,
                            "finish_flight_transition",
                            None,
                        )
                        try:
                            if not callable(transition_finisher):
                                raise ManagedMinerError(
                                    "managed miner transition journal cannot "
                                    "be completed after rollback"
                                )
                            transition_finisher(
                                managed_transition_target_id
                            )
                        except Exception as exc:
                            managed_rollback_errors.append(
                                f"transition journal cleanup: {exc}"
                            )
                    if managed_rollback_errors:
                        rollback_error = ManagedMinerError(
                            f"{apply_exc}; rollback incomplete: "
                            + "; ".join(managed_rollback_errors)
                        )
                        raise rollback_error from apply_exc
                    raise
                result["path"] = str(active_path)
                result["service"] = self.config.miner_service
                result["operations"] = operations
                result["miner_packages"] = installed_packages
                result["managed_miner"] = managed_result
                result["gpu_oc"] = gpu_oc_result
                return CommandOutcome(status="succeeded", result=result)
            if command.action == "apply_gpu_profile":
                vendor, settings = validate_gpu_profile(command.payload, self.config)
                (
                    previous_gpu_settings,
                    previous_devices,
                    local_state_version,
                ) = _load_gpu_oc_state_record(self.config)
                persisted_settings = settings
                persisted_devices: dict[str, dict[str, Any]] | None = None
                transition_previous = previous_gpu_settings
                transition_current = settings
                transition_uuid_by_index: dict[int, str] = {}
                backend = self.gpu_backend_factory()
                is_device_patch = command.payload.get("mode") in {
                    "merge_one",
                    "replace_one",
                    "reset_one",
                }
                if is_device_patch:
                    verify_device_uuid = getattr(backend, "verify_device_uuid", None)
                    if not callable(verify_device_uuid):
                        raise RuntimeError(
                            "configured GPU backend cannot verify device identity"
                        )
                    try:
                        verify_device_uuid(
                            int(settings[0]["index"]),
                            str(command.payload["expected_gpu_uuid"]),
                        )
                    except GpuOcError as exc:
                        _record_gpu_oc_identity_failure(
                            exc, int(settings[0]["index"]),
                            str(command.payload["expected_gpu_uuid"]),
                        )
                        raise
                    mode = str(command.payload.get("mode"))
                    expected_gpu_uuid = str(command.payload["expected_gpu_uuid"])
                    transition_uuid_by_index[int(settings[0]["index"])] = (
                        expected_gpu_uuid
                    )
                    if mode in {"replace_one", "reset_one"}:
                        previous_index = int(command.payload["previous_index"])
                        current_index = int(settings[0]["index"])
                        working_devices = previous_devices
                        if local_state_version == 1 and previous_gpu_settings:
                            if (
                                len(previous_gpu_settings) != 1
                                or previous_index != current_index
                                or int(previous_gpu_settings[0]["index"]) != current_index
                            ):
                                raise RuntimeError(
                                    "旧版 GPU OC 状态无法安全确认显卡身份 "
                                    "(legacy GPU OC state cannot safely bind complete device identity)"
                                )
                            working_devices = {
                                expected_gpu_uuid: dict(previous_gpu_settings[0])
                            }
                        (
                            persisted_settings,
                            persisted_devices,
                            transition_previous,
                            transition_current,
                        ) = _authoritative_device_gpu_oc_settings(
                            working_devices,
                            settings[0],
                            expected_gpu_uuid=expected_gpu_uuid,
                            previous_index=previous_index,
                            reset=mode == "reset_one",
                        )
                    else:
                        working_devices = previous_devices
                        if local_state_version == 1:
                            working_devices = {}
                            if previous_gpu_settings:
                                current_index = int(settings[0]["index"])
                                if (
                                    len(previous_gpu_settings) != 1
                                    or int(previous_gpu_settings[0]["index"])
                                    != current_index
                                ):
                                    raise RuntimeError(
                                        "旧版 GPU OC 状态无法安全确认显卡身份 "
                                        "(legacy GPU OC state cannot safely bind complete device identity)"
                                    )
                                working_devices = {
                                    expected_gpu_uuid: dict(previous_gpu_settings[0])
                                }
                        old = working_devices.get(expected_gpu_uuid)
                        merged = {
                            **(old or {}),
                            **settings[0],
                            "index": int(settings[0]["index"]),
                        }
                        (
                            persisted_settings,
                            persisted_devices,
                            transition_previous,
                            transition_current,
                        ) = _authoritative_device_gpu_oc_settings(
                            working_devices,
                            merged,
                            expected_gpu_uuid=expected_gpu_uuid,
                            previous_index=(
                                int(old["index"])
                                if old
                                else int(settings[0]["index"])
                            ),
                            reset=False,
                        )
                else:
                    if local_state_version == 1 and previous_gpu_settings:
                        raise RuntimeError(
                            "旧版 GPU OC 状态必须先完成显卡 UUID 迁移，才能覆盖 "
                            "(legacy GPU OC state must be explicitly migrated before replacing it)"
                        )
                    persisted_devices, current_inventory = (
                        _bind_gpu_oc_settings_to_current_devices(backend, settings)
                    )
                    transition_uuid_by_index = {
                        int(index): gpu_uuid
                        for gpu_uuid, index in current_inventory.items()
                    }
                    if local_state_version == 2 and previous_gpu_settings:
                        missing_uuids = sorted(
                            set(previous_devices) - set(current_inventory)
                        )
                        if missing_uuids:
                            raise RuntimeError(
                                "GPU UUID is no longer reported: "
                                + ", ".join(missing_uuids)
                            )
                        transition_previous = [
                            {
                                **previous_devices[gpu_uuid],
                                "index": current_inventory[gpu_uuid],
                            }
                            for gpu_uuid in sorted(previous_devices)
                        ]
                    else:
                        transition_previous = []
                result = _transition_gpu_oc(
                    backend,
                    transition_previous,
                    transition_current,
                    transition_uuid_by_index,
                    config=self.config,
                )
                _persist_gpu_oc_transition(
                    self.config, persisted_settings, persisted_devices,
                    transition_previous, transition_current, transition_uuid_by_index, result,
                )
                result["gpu_profile_id"] = command.payload["gpu_profile_id"]
                result["vendor"] = vendor
                return CommandOutcome(status="succeeded", result=result)
            if command.action in {
                "stop_mining",
                "restart_mining",
                "remove_flight_sheet",
            }:
                manager = self.managed_miner_factory(self.config)
                if command.action == "stop_mining":
                    miner_result, srb_reset, srb_error, _metadata_error = (
                        self._stop_miner_and_clear_srb_oc(manager)
                    )
                    return self._finish_mining_stop_with_gpu_reset(
                        command, miner_result, srb_reset=srb_reset, srb_error=srb_error
                    )
                elif command.action == "restart_mining":
                    method = manager.restart
                else:
                    miner_result, srb_reset, srb_error, srb_metadata_error = (
                        self._stop_miner_and_clear_srb_oc(manager)
                    )
                    configuration_error = None
                    if srb_error is None or srb_metadata_error is not None:
                        try:
                            miner_result.update(manager.remove_flight_sheet())
                        except (OSError, RuntimeError) as exc:
                            # Removing the saved flight is a separate step from
                            # restoring hardware defaults.  Even if the file
                            # cannot be removed, finish the NVML reset and
                            # report a retryable partial failure.
                            configuration_error = str(exc)
                    return self._finish_mining_stop_with_gpu_reset(
                        command,
                        miner_result,
                        srb_reset=srb_reset,
                        srb_error=srb_error,
                        configuration_error=configuration_error,
                    )
                return CommandOutcome(status="succeeded", result=dict(method()))
            if command.action == "restart_service":
                results = [
                    self._run(argv)
                    for argv in self._service_commands(str(command.payload["service"]))
                ]
                return CommandOutcome(
                    status="succeeded",
                    result={
                        "service": command.payload["service"],
                        "operations": results,
                    },
                )
            if command.action in {"reboot", "shutdown"}:
                # The host is going down: a pending delayed secondary start
                # must never fire into a machine that is already powering off.
                cancel_deferred_secondary_start(command.action)
            result = self._run(self._power_command(command.action))
            result.update(
                {
                    "scheduled": True,
                    "action": command.action,
                    "delay_seconds": (
                        WINDOWS_POWER_DELAY_SECONDS
                        if platform.system() == "Windows"
                        else LINUX_POWER_DELAY_SECONDS
                    ),
                }
            )
            return CommandOutcome(status="succeeded", result=result)
        except MinerPackageError as exc:
            LOGGER.warning(
                "miner package installation failed",
                extra={"event": "miner_package_failed", "command_id": command.command_id},
            )
            return CommandOutcome(status="failed", result={}, error=str(exc))
        except ManagedMinerError as exc:
            LOGGER.warning(
                "managed miner operation failed",
                extra={"event": "managed_miner_failed", "command_id": command.command_id},
            )
            result = _gpu_oc_failure_result(exc, command)
            return CommandOutcome(status="failed", result=result, error=str(exc))
        except (
            AgentUpdateError,
            OSError,
            RuntimeError,
            subprocess.TimeoutExpired,
        ) as exc:
            LOGGER.warning(
                "command execution failed",
                extra={"event": "command_failed", "command_id": command.command_id},
            )
            return CommandOutcome(status="failed", result=_gpu_oc_failure_result(exc, command), error=str(exc))
