"""Validation and local application of structured mining configuration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from server.miner_adapters.validation import normalize_extra_args, parse_pool_endpoint
from server.miner_adapters import is_retired_miner

from .config import AgentConfig
from .managed_miners import (
    FLIGHT_RUNTIME_FINGERPRINT_FIELD,
    MINER_ADAPTERS,
    ManagedMinerError,
    managed_package_id_matches_miner,
    leg_effective_resource,
    validate_managed_algorithm_context,
    validate_managed_extra_config,
    validate_srb_dual_configuration,
)
from .miner_packages import (
    MinerPackageError,
    validate_package_metadata,
)


class MiningCommandError(ValueError):
    """A flight-sheet or GPU profile violates local policy."""


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.+-]+$")
_SAFE_ALGORITHM = re.compile(r"^[A-Za-z0-9_.+/-]+$")
_POOL_SCHEMES = {"stratum+tcp", "stratum+ssl", "stratum+tls"}


def _exact(payload: Mapping[str, Any], fields: set[str]) -> None:
    if set(payload) != fields:
        raise MiningCommandError(
            "payload fields must be exactly: " + ", ".join(sorted(fields))
        )


def _text(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: int,
    no_whitespace: bool = False,
) -> str:
    if not isinstance(value, str):
        raise MiningCommandError(f"{label} must be a string")
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise MiningCommandError(f"{label} must contain {minimum}-{maximum} characters")
    if any(ord(character) < 32 for character in value):
        raise MiningCommandError(f"{label} contains control characters")
    if no_whitespace and any(character.isspace() for character in value):
        raise MiningCommandError(f"{label} must not contain whitespace")
    return value


def _validate_json(value: Any, label: str, *, max_bytes: int = 16_384, allow_extra_args: bool = False) -> None:
    nodes = 0

    def walk(item: Any, depth: int = 0, key_name: str | None = None) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 512 or depth > 5:
            raise MiningCommandError(f"{label} is too complex")
        if isinstance(item, dict):
            if len(item) > 64:
                raise MiningCommandError(f"{label} has too many fields")
            for key, nested in item.items():
                if not isinstance(key, str) or not 1 <= len(key) <= 64:
                    raise MiningCommandError(f"{label} has an invalid key")
                walk(nested, depth + 1, key)
        elif isinstance(item, list):
            if len(item) > 64:
                raise MiningCommandError(f"{label} has too many items")
            for nested in item:
                walk(nested, depth + 1)
        elif isinstance(item, str):
            if allow_extra_args and depth == 1 and key_name == "extra_args":
                try:
                    normalize_extra_args(item)
                except ValueError as exc:
                    raise MiningCommandError(f"{label}.{exc}") from exc
                return
            if len(item) > 1024 or any(ord(character) < 32 for character in item):
                raise MiningCommandError(f"{label} contains an invalid string")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise MiningCommandError(f"{label} contains a non-finite number")
        elif item is not None and not isinstance(item, (bool, int)):
            raise MiningCommandError(f"{label} contains an unsupported value")

    walk(value)
    try:
        # The separately bounded native text field allows 4096 Unicode
        # characters. Do not charge JSON's ASCII escaping against the legacy
        # option envelope; all other values keep the existing byte limit.
        envelope = dict(value) if allow_extra_args and isinstance(value, dict) else value
        if allow_extra_args and isinstance(envelope, dict) and "extra_args" in envelope:
            envelope["extra_args"] = ""
        encoded = json.dumps(envelope, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise MiningCommandError(f"{label} is not valid JSON") from exc
    if len(encoded) > max_bytes:
        raise MiningCommandError(f"{label} is too large")


def _pool_url(
    value: Any,
    label: str = "pool_url",
    *,
    allowed_schemes: frozenset[str] = _POOL_SCHEMES,
) -> str:
    try:
        endpoint = parse_pool_endpoint(value, allowed_schemes)
    except ValueError as exc:
        reason = str(exc).replace("unsupported protocol", "unsupported scheme", 1)
        if reason.startswith("pool_url"):
            reason = label + reason[len("pool_url"):]
        raise MiningCommandError(reason) from exc
    # Raw controls must be rejected before the legacy text length/trim step.
    return _text(endpoint.url, label, maximum=512, no_whitespace=True)


_MINING_LEG_FIELDS = {
    "coin",
    "wallet_address",
    "pool_url",
    "miner_profile",
    "miner_package",
    "extra_config",
    "algorithm",
    "pool_password",
    "device_indices",
}
_MANAGED_MINING_LEG_FIELDS = _MINING_LEG_FIELDS | {"miner_type"}
_WSL_MANAGED_MINING_LEG_FIELDS = _MANAGED_MINING_LEG_FIELDS | {
    "execution_mode"
}


def _device_indices(value: Any, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) > 32:
        raise MiningCommandError(f"{label} must be an array with at most 32 entries")
    normalized: list[int] = []
    seen: set[int] = set()
    for position, index in enumerate(value):
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 31:
            raise MiningCommandError(
                f"{label}[{position}] must be an integer from 0 to 31"
            )
        if index in seen:
            raise MiningCommandError(f"{label} must not contain duplicate indices")
        seen.add(index)
        normalized.append(index)
    return normalized


def _mining_leg(
    raw: Mapping[str, Any],
    config: AgentConfig,
    *,
    label: str,
) -> dict[str, Any]:
    fields = set(raw)
    if fields not in (
        frozenset(_MINING_LEG_FIELDS),
        frozenset(_MANAGED_MINING_LEG_FIELDS),
        frozenset(_WSL_MANAGED_MINING_LEG_FIELDS),
    ):
        raise MiningCommandError(
            f"{label} fields must be the legacy fields, optionally plus "
            "miner_type and execution_mode"
        )
    raw_miner_type = raw.get("miner_type")
    if raw_miner_type is None:
        miner_type = None
    elif isinstance(raw_miner_type, str) and raw_miner_type.strip():
        miner_type = raw_miner_type.strip().lower()
        if _SAFE_NAME.fullmatch(miner_type) is None or len(miner_type) > 64:
            raise MiningCommandError(f"{label}.miner_type is not canonical")
        if not config.managed_mining_enabled:
            raise MiningCommandError(
                "managed mining is disabled by local configuration"
            )
        if config.managed_miner_runtime_dir is None:
            raise MiningCommandError(
                "managed miner runtime directory is not configured"
            )
    else:
        raise MiningCommandError(
            f"{label}.miner_type must be a canonical string or null"
        )
    adapter = MINER_ADAPTERS.get(miner_type) if miner_type is not None else None
    if miner_type is not None and adapter is None:
        if is_retired_miner(miner_type):
            raise MiningCommandError(f"{label}.{miner_type} is retired; select an active miner")
        raise MiningCommandError(f"{label}.miner_type is unsupported")
    raw_execution_mode = raw.get("execution_mode", "native")
    if raw_execution_mode not in {"native", "wsl"}:
        raise MiningCommandError(
            f"{label}.execution_mode must be native or wsl"
        )
    execution_mode = str(raw_execution_mode)
    if execution_mode == "wsl" and miner_type is None:
        raise MiningCommandError(
            f"{label}.execution_mode wsl requires a managed miner"
        )
    coin = _text(
        raw["coin"], f"{label}.coin", maximum=24, no_whitespace=True
    ).upper()
    if not _SAFE_NAME.fullmatch(coin):
        raise MiningCommandError(f"{label}.coin contains unsupported characters")
    wallet = _text(
        raw["wallet_address"],
        f"{label}.wallet_address",
        minimum=3,
        maximum=256,
        no_whitespace=True,
    )
    pool = _pool_url(
        raw["pool_url"], f"{label}.pool_url",
        allowed_schemes=(
            frozenset(adapter.pool_schemes)
            if adapter is not None
            else frozenset(_POOL_SCHEMES)
        ),
    )
    profile = _text(
        raw["miner_profile"],
        f"{label}.miner_profile",
        maximum=80,
        no_whitespace=True,
    )
    if not _SAFE_NAME.fullmatch(profile):
        raise MiningCommandError(
            f"{label}.miner_profile contains unsupported characters"
        )
    if is_retired_miner(profile):
        raise MiningCommandError(f"{label}.{profile} is retired; select an active miner")
    if miner_type is None and profile not in config.miner_profile_allowlist:
        allowed = ", ".join(sorted(config.miner_profile_allowlist)) or "(none configured)"
        raise MiningCommandError(
            f"{label}.miner_profile is not in the local allowlist; "
            f"allowed values: {allowed}"
        )
    algorithm_value = raw["algorithm"]
    if algorithm_value is None:
        algorithm = None
    else:
        algorithm = _text(
            algorithm_value,
            f"{label}.algorithm",
            maximum=64,
            no_whitespace=True,
        )
        if not _SAFE_ALGORITHM.fullmatch(algorithm):
            raise MiningCommandError(
                f"{label}.algorithm contains unsupported characters"
            )
    password = raw["pool_password"]
    if not isinstance(password, str) or len(password) > 128:
        raise MiningCommandError(
            f"{label}.pool_password must be a string with at most 128 characters"
        )
    if any(ord(character) < 32 for character in password):
        raise MiningCommandError(f"{label}.pool_password contains control characters")
    devices = _device_indices(raw["device_indices"], f"{label}.device_indices")
    extra = raw["extra_config"]
    if not isinstance(extra, dict):
        raise MiningCommandError(f"{label}.extra_config must be an object")
    _validate_json(extra, f"{label}.extra_config", allow_extra_args=miner_type is not None)
    if miner_type is None:
        unknown_keys = set(extra) - config.flight_sheet_extra_key_allowlist
        if unknown_keys:
            raise MiningCommandError(
                f"{label}.extra_config keys are not locally allowed: "
                + ", ".join(sorted(unknown_keys))
            )
    else:
        try:
            extra = validate_managed_extra_config(
                miner_type, extra, f"{label}.extra_config"
            )
        except (ManagedMinerError, ValueError) as exc:
            raise MiningCommandError(str(exc)) from exc
    try:
        package = validate_package_metadata(
            raw["miner_package"], config, f"{label}.miner_package"
        )
    except MinerPackageError as exc:
        raise MiningCommandError(str(exc)) from exc
    if miner_type is not None:
        if package is None:
            raise MiningCommandError(
                f"{label}.miner_package is required for managed mining"
            )
        if not managed_package_id_matches_miner(package["id"], miner_type):
            raise MiningCommandError(
                f"{label}.miner_package.id must identify a controller-managed "
                f"{miner_type} official or test package"
            )
        required_capability = "wsl_gpu" if execution_mode == "wsl" else "native"
        assert adapter is not None
        if required_capability not in adapter.capabilities:
            raise MiningCommandError(
                (
                    f"{label}.execution_mode wsl is available only to Windows GPU miners"
                    if execution_mode == "wsl"
                    else f"{label}.native execution is unsupported for {miner_type}"
                )
            )
        if devices and "device_selection" not in adapter.capabilities:
            raise MiningCommandError(f"{label} adapter does not provide a GPU index selector")
    normalized_leg = {
        "coin": coin,
        "wallet_address": wallet,
        "pool_url": pool,
        "miner_profile": profile,
        "miner_package": package,
        "extra_config": extra,
        "algorithm": algorithm,
        "pool_password": password,
        "device_indices": devices,
        "miner_type": miner_type,
    }
    # Preserve the legacy/native wire and on-disk shape.  Only the new WSL
    # backend needs an explicit marker, so older native status and rollback
    # logic remains byte-for-byte compatible.
    if execution_mode == "wsl":
        normalized_leg["execution_mode"] = "wsl"
    if adapter is not None:
        try:
            # Validate the shared algorithm/version/transport context only
            # after package and secret fields are available. Keep the original
            # nullable algorithm field and native wire/disk shape.
            validate_managed_algorithm_context(normalized_leg)
            resource = leg_effective_resource(normalized_leg)
        except (ManagedMinerError, ValueError) as exc:
            raise MiningCommandError(str(exc)) from exc
        if execution_mode == "wsl" and resource != "gpu":
            raise MiningCommandError(
                f"{label}.execution_mode wsl is available only to Windows "
                "GPU miners; XMRig and RandomX must use native mode"
            )
    return normalized_leg


def validate_flight_sheet(
    payload: Mapping[str, Any], config: AgentConfig
) -> dict[str, Any]:
    base_fields = {"flight_sheet_id", "name", "worker_name", "primary", "secondary"}
    allowed_fields = base_fields | {
        "gpu_oc",
        "secondary_mode",
        FLIGHT_RUNTIME_FINGERPRINT_FIELD,
    }
    if set(payload) - allowed_fields or not base_fields <= set(payload):
        raise MiningCommandError(
            "payload fields must be flight_sheet_id, name, worker_name, primary, "
            "secondary, and optional gpu_oc, secondary_mode, and "
            "runtime_fingerprint"
        )
    runtime_fingerprint = payload.get(FLIGHT_RUNTIME_FINGERPRINT_FIELD)
    if runtime_fingerprint is not None:
        if (
            not isinstance(runtime_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", runtime_fingerprint) is None
        ):
            raise MiningCommandError(
                "runtime_fingerprint must be a lowercase SHA-256 digest"
            )
        fingerprint_payload = dict(payload)
        fingerprint_payload.pop(FLIGHT_RUNTIME_FINGERPRINT_FIELD, None)
        calculated_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if not secrets.compare_digest(
            runtime_fingerprint, calculated_fingerprint
        ):
            raise MiningCommandError(
                "runtime_fingerprint does not match the flight-sheet payload"
            )
    raw_secondary_mode = payload.get("secondary_mode", "integrated")
    if raw_secondary_mode not in {"integrated", "independent"}:
        raise MiningCommandError(
            "secondary_mode must be integrated or independent"
        )
    secondary_mode = str(raw_secondary_mode)
    if config.flight_sheet_file is None:
        raise MiningCommandError("flight-sheet application is disabled locally")
    sheet_id = _text(payload["flight_sheet_id"], "flight_sheet_id", maximum=64)
    name = _text(payload["name"], "name", maximum=80)
    worker_name = _text(payload["worker_name"], "worker_name", maximum=128)
    raw_primary = payload["primary"]
    if not isinstance(raw_primary, dict):
        raise MiningCommandError("primary must be an object")
    primary = _mining_leg(raw_primary, config, label="primary")
    raw_secondary = payload["secondary"]
    if raw_secondary is None:
        secondary = None
    elif isinstance(raw_secondary, dict):
        secondary = _mining_leg(raw_secondary, config, label="secondary")
    else:
        raise MiningCommandError("secondary must be an object or null")
    if secondary is None and secondary_mode != "integrated":
        raise MiningCommandError(
            "independent secondary_mode requires a secondary mining leg"
        )
    if secondary is not None:
        primary_type = primary["miner_type"]
        secondary_type = secondary["miner_type"]
        if (primary_type is None) != (secondary_type is None):
            raise MiningCommandError(
                "primary and secondary must both be legacy or both be managed"
            )
        if secondary_mode == "integrated":
            if (
                primary.get("execution_mode", "native") == "wsl"
                or secondary.get("execution_mode", "native") == "wsl"
            ):
                raise MiningCommandError(
                    "integrated managed dual mining does not support WSL; "
                    "use independent mode"
                )
            if primary_type is not None and primary_type != secondary_type:
                raise MiningCommandError(
                    "integrated managed dual mining must use one miner_type"
                )
            primary_adapter = (
                MINER_ADAPTERS.get(primary_type)
                if primary_type is not None else None
            )
            if primary_adapter is not None and (
                "integrated_dual" not in primary_adapter.capabilities
            ):
                raise MiningCommandError(
                    f"managed {primary_type} does not support a secondary "
                    "mining leg in integrated mode"
                )
            if primary_type is not None and primary["miner_package"] != secondary["miner_package"]:
                raise MiningCommandError(
                    "integrated managed dual mining must use one package version"
                )
            if primary_adapter is not None:
                try:
                    validate_srb_dual_configuration(primary, secondary)
                except ManagedMinerError as exc:
                    raise MiningCommandError(str(exc)) from exc
        else:
            if primary_type is None or secondary_type is None:
                raise MiningCommandError(
                    "independent dual mining requires two managed miners"
                )
            legs = (primary, secondary)
            try:
                resources = [leg_effective_resource(leg) for leg in legs]
            except ManagedMinerError as exc:
                raise MiningCommandError(str(exc)) from exc
            cpu_legs = [leg for leg, resource in zip(legs, resources) if resource == "cpu"]
            gpu_legs = [leg for leg, resource in zip(legs, resources) if resource == "gpu"]
            if len(cpu_legs) == 2 or (len(cpu_legs) == 1 and len(gpu_legs) == 1):
                pass
            elif (
                not cpu_legs and len(gpu_legs) == 2
                and all(leg.get("device_indices") for leg in gpu_legs)
                and set(gpu_legs[0]["device_indices"]).isdisjoint(gpu_legs[1]["device_indices"])
            ):
                pass
            else:
                raise MiningCommandError(
                    "independent dual mining requires two CPU miners, one CPU and one GPU miner, "
                    "or two GPU legs with disjoint device indices"
                )
    raw_gpu_oc = payload.get("gpu_oc")
    if raw_gpu_oc is None:
        gpu_oc = None
    else:
        if primary.get("execution_mode", "native") == "wsl":
            raise MiningCommandError(
                "WSL managed miners do not support flight-sheet CLI OC; use the "
                "Windows host NVML OC controls"
            )
        if primary.get("miner_type") != "srbminer":
            raise MiningCommandError(
                "flight-sheet GPU OC is supported only for managed SRBMiner"
            )
        primary_adapter = MINER_ADAPTERS.get("srbminer")
        assert primary_adapter is not None
        if "miner_cli_oc" not in primary_adapter.capabilities:
            raise MiningCommandError(
                "flight-sheet GPU OC is unsupported by the selected adapter"
            )
        if secondary_mode == "independent":
            try:
                primary_resource = leg_effective_resource(primary)
                secondary_resource = leg_effective_resource(secondary) if secondary is not None else None
            except ManagedMinerError as exc:
                raise MiningCommandError(str(exc)) from exc
            if primary_resource != "gpu" or secondary_resource != "cpu":
                raise MiningCommandError(
                    "independent flight-sheet GPU OC requires a primary "
                    "GPU leg and a CPU-only second miner"
                )
            gpu_legs = [primary]
        else:
            gpu_legs = []
            for leg in (primary, secondary):
                if leg is None:
                    continue
                try:
                    resource = leg_effective_resource(leg)
                except ManagedMinerError as exc:
                    raise MiningCommandError(str(exc)) from exc
                if resource == "gpu":
                    gpu_legs.append(leg)
        if not gpu_legs:
            raise MiningCommandError(
                "flight-sheet GPU OC requires an SRBMiner GPU algorithm"
            )
        if not isinstance(raw_gpu_oc, dict) or set(raw_gpu_oc) != {
            "vendor",
            "settings",
        }:
            raise MiningCommandError(
                "gpu_oc must contain exactly vendor and settings"
            )
        vendor, settings = validate_gpu_profile(
            {
                "gpu_profile_id": f"flight-{sheet_id}",
                "name": name,
                "vendor": raw_gpu_oc["vendor"],
                "settings": raw_gpu_oc["settings"],
            },
            config,
            allowed_fields=_FLIGHT_GPU_FIELDS,
            allow_model_match=True,
        )
        model_mode = "model_match" in settings[0]
        if not model_mode:
            if any(not leg.get("device_indices") for leg in gpu_legs):
                raise MiningCommandError(
                    "legacy flight-sheet GPU OC requires explicit GPU indices on every GPU mining leg"
                )
            selected_gpu_indices = set(gpu_legs[0]["device_indices"])
            setting_indices = {setting["index"] for setting in settings}
            if setting_indices != selected_gpu_indices:
                raise MiningCommandError(
                    "legacy flight-sheet GPU OC must contain exactly one row for every selected GPU"
                )
        control_sets = {
            frozenset(setting) - {"index", "model_match"} for setting in settings
        }
        if len(control_sets) != 1:
            raise MiningCommandError(
                "every GPU OC row must fill the same control columns"
            )
        gpu_oc = {"vendor": vendor, "settings": settings}
    result = {
        "version": 4 if secondary_mode == "independent" else 3,
        "flight_sheet_id": sheet_id,
        "name": name,
        "worker_name": worker_name,
        "primary": primary,
        "secondary": secondary,
        "gpu_oc": gpu_oc,
    }
    if secondary_mode == "independent":
        result["secondary_mode"] = secondary_mode
    if isinstance(runtime_fingerprint, str):
        result[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = runtime_fingerprint
    return result


def write_flight_sheet(document: dict[str, Any], target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    stored = dict(document)
    stored["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(stored, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
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
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    result = {
        "path": str(target),
        "flight_sheet_id": stored["flight_sheet_id"],
        "worker_name": stored["worker_name"],
        "primary_miner_profile": stored["primary"]["miner_profile"],
        "primary_miner_type": stored["primary"].get("miner_type"),
        "secondary_miner_profile": (
            stored["secondary"]["miner_profile"] if stored["secondary"] else None
        ),
        "secondary_miner_type": (
            stored["secondary"].get("miner_type") if stored["secondary"] else None
        ),
    }
    runtime_fingerprint = stored.get(FLIGHT_RUNTIME_FINGERPRINT_FIELD)
    if isinstance(runtime_fingerprint, str):
        result[FLIGHT_RUNTIME_FINGERPRINT_FIELD] = runtime_fingerprint
    return result


_GPU_FIELDS = frozenset({
    "index",
    "power_limit_w",
    "core_clock_offset_mhz",
    "core_clock_lock_mhz",
    "memory_clock_offset_mhz",
    "memory_clock_lock_mhz",
    "fan_percent",
})
_FLIGHT_GPU_FIELDS = (_GPU_FIELDS - {"fan_percent"}) | {"model_match"}


def _number(
    value: Any, label: str, minimum: float | None, maximum: float | None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MiningCommandError(f"{label} must be numeric")
    try:
        value = float(value)
    except OverflowError as exc:
        raise MiningCommandError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise MiningCommandError(f"{label} must be a positive finite number")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        bounds = (
            f"{minimum:g}-{maximum:g}" if minimum is not None and maximum is not None
            else f"minimum {minimum:g}" if minimum is not None
            else f"maximum {maximum:g}"
        )
        raise MiningCommandError(f"{label} is outside the local range {bounds}")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MiningCommandError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise MiningCommandError(f"{label} is outside the local range {minimum}-{maximum}")
    return value


def validate_gpu_profile(
    payload: Mapping[str, Any],
    config: AgentConfig,
    *,
    allowed_fields: frozenset[str] = _GPU_FIELDS,
    allow_model_match: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    base_fields = {"gpu_profile_id", "name", "vendor", "settings"}
    profile_id = payload.get("gpu_profile_id")
    is_device_patch = (
        isinstance(profile_id, str) and profile_id.startswith("device-oc-")
    )
    if is_device_patch:
        mode = payload.get("mode")
        protocol_fields = {"mode", "expected_gpu_uuid"}
        if mode in {"replace_one", "reset_one"}:
            protocol_fields.add("previous_index")
        _exact(payload, base_fields | protocol_fields)
        if payload["mode"] not in {"merge_one", "replace_one", "reset_one"}:
            raise MiningCommandError(
                "device GPU OC mode must be merge_one, replace_one, or reset_one"
            )
        expected_gpu_uuid = _text(
            payload["expected_gpu_uuid"],
            "expected_gpu_uuid",
            minimum=4,
            maximum=128,
            no_whitespace=True,
        )
        if "\x7f" in expected_gpu_uuid:
            raise MiningCommandError("expected_gpu_uuid contains control characters")
        if mode in {"replace_one", "reset_one"}:
            _integer(payload["previous_index"], "previous_index", 0, 31)
    else:
        _exact(payload, base_fields)
    if not config.allow_gpu_oc:
        raise MiningCommandError("GPU OC is disabled by local configuration")
    _text(payload["gpu_profile_id"], "gpu_profile_id", maximum=64)
    _text(payload["name"], "name", maximum=80)
    vendor = _text(payload["vendor"], "vendor", maximum=16, no_whitespace=True).lower()
    if vendor not in config.gpu_oc_allowed_vendors:
        raise MiningCommandError("GPU vendor is not locally allowed")
    if vendor != "nvidia":
        raise MiningCommandError("this Agent build currently supports NVIDIA OC only")
    raw_settings = payload["settings"]
    if not isinstance(raw_settings, list) or not 1 <= len(raw_settings) <= 32:
        raise MiningCommandError("settings must contain 1-32 GPU entries")
    if is_device_patch and len(raw_settings) != 1:
        raise MiningCommandError("device GPU OC must contain exactly one GPU entry")
    settings: list[dict[str, Any]] = []
    seen: set[tuple[str, int | str]] = set()
    locator_modes: set[str] = set()
    for position, raw in enumerate(raw_settings):
        if not isinstance(raw, dict) or set(raw) - allowed_fields:
            raise MiningCommandError(f"settings[{position}] contains unsupported fields")
        locator_fields = {field for field in ("index", "model_match") if field in raw}
        if len(locator_fields) != 1 or set(raw) <= {"index", "model_match"}:
            raise MiningCommandError(
                f"settings[{position}] must contain a change and exactly one GPU target"
            )
        locator_mode = next(iter(locator_fields))
        if locator_mode == "model_match":
            if not allow_model_match:
                raise MiningCommandError(f"settings[{position}] contains unsupported fields")
            model_match = _text(
                raw["model_match"],
                f"settings[{position}].model_match",
                maximum=96,
            )
            matcher_key = re.sub(r"[^a-z0-9]+", "", model_match.casefold())
            if len(matcher_key) < 2:
                raise MiningCommandError(
                    f"settings[{position}].model_match must contain at least two letters or digits"
                )
            locator_key: tuple[str, int | str] = ("model_match", matcher_key)
            normalized = {"model_match": model_match}
            target_label = f"GPU model {model_match!r}"
        else:
            index = _integer(raw["index"], f"settings[{position}].index", 0, 31)
            locator_key = ("index", index)
            normalized = {"index": index}
            target_label = f"GPU {index}"
        if locator_key in seen:
            raise MiningCommandError("GPU targets must be unique")
        seen.add(locator_key)
        locator_modes.add(locator_mode)
        for field, validator, minimum, maximum in (
            ("power_limit_w", _number, config.gpu_power_limit_min_w, config.gpu_power_limit_max_w),
            ("core_clock_offset_mhz", _integer, config.gpu_core_clock_offset_min_mhz, config.gpu_core_clock_offset_max_mhz),
            ("core_clock_lock_mhz", _integer, config.gpu_core_clock_lock_min_mhz, config.gpu_core_clock_lock_max_mhz),
            ("memory_clock_offset_mhz", _integer, config.gpu_memory_clock_offset_min_mhz, config.gpu_memory_clock_offset_max_mhz),
            ("memory_clock_lock_mhz", _integer, config.gpu_memory_clock_lock_min_mhz, config.gpu_memory_clock_lock_max_mhz),
            ("fan_percent", _integer, config.gpu_fan_min_percent, config.gpu_fan_max_percent),
        ):
            if field in raw:
                normalized[field] = validator(
                    raw[field], f"{target_label} {field}", minimum, maximum,
                )
        settings.append(normalized)
    if len(locator_modes) != 1:
        raise MiningCommandError(
            "GPU OC rows cannot mix legacy indices and model matchers"
        )
    return vendor, settings
