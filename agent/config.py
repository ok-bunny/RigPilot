"""Configuration loading and validation for the RigPilot agent."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Mapping, overload
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when the agent configuration is invalid."""


@dataclass(frozen=True)
class AgentConfig:
    """Validated agent configuration.

    JSON is intentionally used for the built-in configuration format so the
    runtime only needs ``requests`` and ``psutil`` as third-party packages.
    """

    server_url: str
    state_file: Path
    name: str | None = None
    enrollment_token: str | None = None
    heartbeat_interval: float = 30.0
    request_timeout: float = 20.0
    ca_bundle: str | None = None
    allow_insecure_http: bool = False
    dry_run: bool = False
    log_level: str = "INFO"
    log_file: Path | None = None
    service_allowlist: frozenset[str] = field(default_factory=frozenset)
    allow_reboot: bool = False
    allow_shutdown: bool = False
    allow_agent_updates: bool = False
    agent_update_max_mb: int = 512
    agent_update_total_timeout: float = 900.0
    collect_gpu_metrics: bool = True
    nvidia_smi_path: str = "nvidia-smi"
    advertise_ssh: bool = False
    advertise_sftp: bool = False
    ssh_port: int = 22
    ssh_username: str | None = None
    flight_sheet_file: Path | None = None
    miner_status_file: Path | None = None
    managed_mining_enabled: bool = False
    managed_miner_runtime_dir: Path | None = None
    miner_profile_allowlist: frozenset[str] = field(default_factory=frozenset)
    flight_sheet_extra_key_allowlist: frozenset[str] = field(default_factory=frozenset)
    miner_service: str | None = None
    allow_miner_downloads: bool = False
    miner_packages_dir: Path | None = None
    miner_download_host_allowlist: frozenset[str] = field(default_factory=frozenset)
    miner_download_max_mb: int = 512
    miner_download_total_timeout: float = 300.0
    allow_gpu_oc: bool = False
    gpu_oc_allowed_vendors: frozenset[str] = field(
        default_factory=lambda: frozenset({"nvidia"})
    )
    # Missing bounds defer to the GPU's real hardware range. Explicit local
    # limits remain authoritative and may further narrow that range.
    gpu_power_limit_min_w: float | None = None
    gpu_power_limit_max_w: float | None = None
    gpu_core_clock_offset_min_mhz: int = -500
    gpu_core_clock_offset_max_mhz: int = 500
    gpu_core_clock_lock_min_mhz: int = 100
    gpu_core_clock_lock_max_mhz: int = 5000
    gpu_memory_clock_offset_min_mhz: int = -1500
    gpu_memory_clock_offset_max_mhz: int = 2000
    gpu_memory_clock_lock_min_mhz: int = 100
    gpu_memory_clock_lock_max_mhz: int = 20000
    gpu_fan_min_percent: int = 30
    gpu_fan_max_percent: int = 100
    command_history_limit: int = 200

    @property
    def tls_verify(self) -> bool | str:
        """The strict verification value passed to ``requests``."""

        return self.ca_bundle or True

    def with_dry_run(self, enabled: bool) -> "AgentConfig":
        return replace(self, dry_run=enabled)


def _require_bool(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _positive_number(data: Mapping[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value <= sys.float_info.max
    ):
        raise ConfigError(f"{key} must be a positive finite number")
    return float(value)


def _optional_positive_number(data: Mapping[str, Any], key: str) -> float | None:
    if data.get(key) is None:
        return None
    return _positive_number(data, key, 0)


@overload
def _bounded_number(
    data: Mapping[str, Any], key: str, default: int | float,
    hard_minimum: int | float, hard_maximum: int | float, *, integer: Literal[True],
) -> int: ...


@overload
def _bounded_number(
    data: Mapping[str, Any], key: str, default: int | float,
    hard_minimum: int | float, hard_maximum: int | float, *, integer: Literal[False] = False,
) -> float: ...


def _bounded_number(
    data: Mapping[str, Any],
    key: str,
    default: int | float,
    hard_minimum: int | float,
    hard_maximum: int | float,
    *,
    integer: bool = False,
) -> int | float:
    value = data.get(key, default)
    valid_type = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not valid_type:
        kind = "an integer" if integer else "a number"
        raise ConfigError(f"{key} must be {kind}")
    if not hard_minimum <= value <= hard_maximum:
        raise ConfigError(f"{key} must be from {hard_minimum} to {hard_maximum}")
    return int(value) if integer else float(value)


def _string_set(
    data: Mapping[str, Any], key: str, default: list[str] | None = None
) -> frozenset[str]:
    value = data.get(key, default or [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError(f"{key} must be an array of non-empty strings")
    return frozenset(item.strip() for item in value)


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_server_url(url: str, allow_insecure_http: bool) -> str:
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        username = parsed.username
        password = parsed.password
        parsed.port
    except ValueError as exc:
        raise ConfigError("server_url is malformed") from exc
    if username or password:
        raise ConfigError("server_url must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("server_url must not contain a query string or fragment")
    if not parsed.netloc or not hostname:
        raise ConfigError("server_url must be an absolute URL")
    if scheme == "https":
        pass
    elif scheme == "http" and (
        allow_insecure_http or _is_loopback_host(hostname)
    ):
        pass
    else:
        raise ConfigError(
            "server_url must use HTTPS (HTTP is only allowed for loopback or "
            "when allow_insecure_http is explicitly enabled)"
        )
    return url.rstrip("/")


def load_config(
    path: str | os.PathLike[str], *, packaged_defaults: bool = False
) -> AgentConfig:
    """Load a JSON config file and resolve its relative paths.

    ``packaged_defaults`` is retained for installer compatibility; every
    packaged default it used to influence belonged to a removed feature.
    """

    config_path = Path(path).expanduser().resolve()
    try:
        # Windows editors and installer APIs commonly emit a UTF-8 BOM.  The
        # ``utf-8-sig`` codec accepts both BOM and non-BOM UTF-8 files.
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except ValueError as exc:
        raise ConfigError(f"invalid JSON configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a JSON object")

    allowed_keys = {
        "server_url",
        "state_file",
        "name",
        "enrollment_token",
        "heartbeat_interval",
        "request_timeout",
        "ca_bundle",
        "allow_insecure_http",
        "dry_run",
        "log_level",
        "log_file",
        "service_allowlist",
        "allow_reboot",
        "allow_shutdown",
        "allow_agent_updates",
        # Retired keys are accepted so legacy configurations keep starting
        # after a binary-only update.  Their values are inert: none of them
        # can re-enable a removed capability.
        "allow_nvidia_driver_updates",
        "agent_update_max_mb",
        "agent_update_total_timeout",
        "collect_gpu_metrics",
        "nvidia_smi_path",
        "advertise_ssh",
        "advertise_sftp",
        "ssh_port",
        "ssh_username",
        # Accept retired keys for binary-only updates of legacy configs.
        # Their values are inert; they cannot enable removed capabilities.
        "allow_wifi_management",
        "allow_remote_sessions",
        "allow_remote_root_sessions",
        "allow_local_web",
        "local_web_port",
        "flight_sheet_file",
        "miner_status_file",
        "managed_mining_enabled",
        "managed_miner_runtime_dir",
        "miner_profile_allowlist",
        "flight_sheet_extra_key_allowlist",
        "miner_service",
        "allow_miner_downloads",
        "miner_packages_dir",
        "miner_download_host_allowlist",
        "miner_download_max_mb",
        "hiveos_max_archive_bytes",
        "hiveos_max_expanded_bytes",
        "hiveos_min_free_bytes",
        "allow_hiveos_runtime_preparation",
        "miner_download_total_timeout",
        "miner_package_sha256_allowlist",
        "allow_gpu_oc",
        "gpu_oc_allowed_vendors",
        "gpu_power_limit_min_w",
        "gpu_power_limit_max_w",
        "gpu_core_clock_offset_min_mhz",
        "gpu_core_clock_offset_max_mhz",
        "gpu_core_clock_lock_min_mhz",
        "gpu_core_clock_lock_max_mhz",
        "gpu_memory_clock_offset_min_mhz",
        "gpu_memory_clock_offset_max_mhz",
        "gpu_memory_clock_lock_min_mhz",
        "gpu_memory_clock_lock_max_mhz",
        "gpu_fan_min_percent",
        "gpu_fan_max_percent",
        "command_history_limit",
        # Inert keys retained for upgrades of already-configured 0.8.61 Agents.
        "miner_self_heal_enabled",
        "miner_self_heal_l1_enabled",
        "miner_self_heal_l2_enabled",
        "miner_self_heal_l3_enabled",
        "miner_self_heal_max_attempts",
        "miner_self_heal_backoff_base_seconds",
        "miner_self_heal_backoff_max_seconds",
        "miner_self_heal_gpu_settle_seconds",
        "miner_self_heal_driver_reload_limit",
    }
    unknown = sorted(set(raw) - allowed_keys)
    if unknown:
        raise ConfigError(f"unknown configuration keys: {', '.join(unknown)}")

    server_url = raw.get("server_url")
    if not isinstance(server_url, str) or not server_url.strip():
        raise ConfigError("server_url is required and must be a string")
    allow_insecure = _require_bool(raw, "allow_insecure_http", False)
    server_url = _validate_server_url(server_url.strip(), allow_insecure)

    state_value = raw.get("state_file", "rigpilot-agent-state.json")
    if not isinstance(state_value, str) or not state_value.strip():
        raise ConfigError("state_file must be a non-empty path string")
    state_file = Path(state_value).expanduser()
    if not state_file.is_absolute():
        state_file = config_path.parent / state_file
    state_file = state_file.resolve()

    normalized_services = _string_set(raw, "service_allowlist")

    history_limit = raw.get("command_history_limit", 200)
    if (
        isinstance(history_limit, bool)
        or not isinstance(history_limit, int)
        or not 10 <= history_limit <= 10_000
    ):
        raise ConfigError("command_history_limit must be an integer from 10 to 10000")

    def optional_string(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} must be a non-empty string when set")
        return value.strip()

    log_level = raw.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level.upper() not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        raise ConfigError("log_level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    def optional_path(key: str) -> Path | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{key} must be a non-empty path when set")
        resolved = Path(value).expanduser()
        if not resolved.is_absolute():
            resolved = config_path.parent / resolved
        return resolved.resolve()

    log_file = optional_path("log_file")

    nvidia_smi_path = raw.get("nvidia_smi_path", "nvidia-smi")
    if not isinstance(nvidia_smi_path, str) or not nvidia_smi_path.strip():
        raise ConfigError("nvidia_smi_path must be a non-empty string")
    ssh_port = _bounded_number(raw, "ssh_port", 22, 1, 65535, integer=True)
    ssh_username = optional_string("ssh_username")
    if ssh_username is not None and not re.fullmatch(
        r"[a-z_][a-z0-9_-]{0,31}", ssh_username
    ):
        raise ConfigError("ssh_username must be a valid Linux account name")
    flight_sheet_file = optional_path("flight_sheet_file")
    miner_status_file = optional_path("miner_status_file")

    managed_mining_enabled = _require_bool(raw, "managed_mining_enabled", False)
    managed_runtime_dir = optional_path("managed_miner_runtime_dir")
    if managed_mining_enabled:
        if managed_runtime_dir is None:
            raise ConfigError(
                "managed_miner_runtime_dir is required when managed_mining_enabled is true"
            )
        if miner_status_file is None:
            miner_status_file = (managed_runtime_dir / "miner-status.json").resolve()

    miner_profiles = _string_set(raw, "miner_profile_allowlist")
    for profile in miner_profiles:
        if len(profile) > 80 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.+-"
            for character in profile
        ):
            raise ConfigError("miner_profile_allowlist contains an invalid profile name")
    extra_keys = _string_set(raw, "flight_sheet_extra_key_allowlist")
    for extra_key in extra_keys:
        if len(extra_key) > 64 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for character in extra_key
        ):
            raise ConfigError("flight_sheet_extra_key_allowlist contains an invalid key")
    miner_service = optional_string("miner_service")
    if miner_service and miner_service not in normalized_services:
        raise ConfigError("miner_service must also appear in service_allowlist")

    allow_miner_downloads = _require_bool(raw, "allow_miner_downloads", False)
    miner_packages_dir = optional_path("miner_packages_dir")
    download_hosts = _string_set(raw, "miner_download_host_allowlist")
    normalized_download_hosts: set[str] = set()
    for host in download_hosts:
        candidate = host.rstrip(".").lower()
        if not candidate or len(candidate) > 253 or any(
            character.isspace() or character in "/@?#" for character in candidate
        ):
            raise ConfigError("miner_download_host_allowlist contains an invalid hostname")
        try:
            normalized_download_hosts.add(ipaddress.ip_address(candidate).compressed.lower())
            continue
        except ValueError:
            pass
        try:
            candidate = candidate.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConfigError(
                "miner_download_host_allowlist contains an invalid hostname"
            ) from exc
        labels = candidate.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
            for label in labels
        ):
            raise ConfigError("miner_download_host_allowlist contains an invalid hostname")
        normalized_download_hosts.add(candidate)
    download_max_mb = _bounded_number(
        raw, "miner_download_max_mb", 512, 1, 4096, integer=True
    )
    download_total_timeout = _bounded_number(
        raw, "miner_download_total_timeout", 300, 10, 3600
    )
    request_timeout = _positive_number(raw, "request_timeout", 20.0)
    if download_total_timeout < request_timeout:
        raise ConfigError(
            "miner_download_total_timeout must not be shorter than request_timeout"
        )
    if allow_miner_downloads and miner_packages_dir is None:
        raise ConfigError(
            "miner_packages_dir is required when allow_miner_downloads is true"
        )
    # ``miner_package_sha256_allowlist`` remains an accepted JSON key so old
    # installations continue to start. Its value is deliberately ignored
    # regardless of shape: package downloads no longer depend on this legacy
    # caller-provided digest field.

    gpu_vendors = _string_set(raw, "gpu_oc_allowed_vendors", ["nvidia"])
    if not gpu_vendors or not gpu_vendors <= {"nvidia"}:
        raise ConfigError("gpu_oc_allowed_vendors currently supports only nvidia")
    power_min = _optional_positive_number(raw, "gpu_power_limit_min_w")
    power_max = _optional_positive_number(raw, "gpu_power_limit_max_w")
    core_min = _bounded_number(
        raw, "gpu_core_clock_offset_min_mhz", -500, -2000, 2000, integer=True
    )
    core_max = _bounded_number(
        raw, "gpu_core_clock_offset_max_mhz", 500, -2000, 2000, integer=True
    )
    core_lock_min = _bounded_number(
        raw, "gpu_core_clock_lock_min_mhz", 100, 100, 5000, integer=True
    )
    core_lock_max = _bounded_number(
        raw, "gpu_core_clock_lock_max_mhz", 5000, 100, 5000, integer=True
    )
    memory_min = _bounded_number(
        raw, "gpu_memory_clock_offset_min_mhz", -1500, -5000, 5000, integer=True
    )
    memory_max = _bounded_number(
        raw, "gpu_memory_clock_offset_max_mhz", 2000, -5000, 5000, integer=True
    )
    memory_lock_min = _bounded_number(
        raw, "gpu_memory_clock_lock_min_mhz", 100, 100, 20000, integer=True
    )
    memory_lock_max = _bounded_number(
        raw, "gpu_memory_clock_lock_max_mhz", 20000, 100, 20000, integer=True
    )
    fan_min = _bounded_number(raw, "gpu_fan_min_percent", 30, 30, 100, integer=True)
    fan_max = _bounded_number(raw, "gpu_fan_max_percent", 100, 30, 100, integer=True)
    for label, minimum, maximum in (
        ("GPU power limit", power_min, power_max),
        ("GPU core offset", core_min, core_max),
        ("GPU core lock", core_lock_min, core_lock_max),
        ("GPU memory offset", memory_min, memory_max),
        ("GPU memory lock", memory_lock_min, memory_lock_max),
        ("GPU fan", fan_min, fan_max),
    ):
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ConfigError(f"{label} minimum must not exceed maximum")

    ca_bundle = optional_string("ca_bundle")
    if ca_bundle:
        ca_path = Path(ca_bundle).expanduser()
        if not ca_path.is_absolute():
            ca_path = config_path.parent / ca_path
        ca_path = ca_path.resolve()
        if not ca_path.is_file():
            raise ConfigError(f"ca_bundle file not found: {ca_path}")
        ca_bundle = str(ca_path)
    return AgentConfig(
        server_url=server_url,
        state_file=state_file,
        name=optional_string("name"),
        enrollment_token=optional_string("enrollment_token"),
        heartbeat_interval=_positive_number(raw, "heartbeat_interval", 30.0),
        request_timeout=request_timeout,
        ca_bundle=ca_bundle,
        allow_insecure_http=allow_insecure,
        dry_run=_require_bool(raw, "dry_run", False),
        log_level=log_level.upper(),
        log_file=log_file,
        service_allowlist=normalized_services,
        allow_reboot=_require_bool(raw, "allow_reboot", False),
        allow_shutdown=_require_bool(raw, "allow_shutdown", False),
        allow_agent_updates=_require_bool(raw, "allow_agent_updates", False),
        agent_update_max_mb=_bounded_number(
            raw, "agent_update_max_mb", 512, 1, 2048, integer=True
        ),
        agent_update_total_timeout=_bounded_number(
            raw, "agent_update_total_timeout", 900, 60, 3600
        ),
        collect_gpu_metrics=_require_bool(raw, "collect_gpu_metrics", True),
        nvidia_smi_path=nvidia_smi_path.strip(),
        advertise_ssh=_require_bool(raw, "advertise_ssh", False),
        advertise_sftp=_require_bool(raw, "advertise_sftp", False),
        ssh_port=ssh_port,
        ssh_username=ssh_username,
        flight_sheet_file=flight_sheet_file,
        miner_status_file=miner_status_file,
        managed_mining_enabled=managed_mining_enabled,
        managed_miner_runtime_dir=managed_runtime_dir,
        miner_profile_allowlist=miner_profiles,
        flight_sheet_extra_key_allowlist=extra_keys,
        miner_service=miner_service,
        allow_miner_downloads=allow_miner_downloads,
        miner_packages_dir=miner_packages_dir,
        miner_download_host_allowlist=frozenset(normalized_download_hosts),
        miner_download_max_mb=download_max_mb,
        miner_download_total_timeout=download_total_timeout,
        allow_gpu_oc=_require_bool(raw, "allow_gpu_oc", False),
        gpu_oc_allowed_vendors=gpu_vendors,
        gpu_power_limit_min_w=power_min,
        gpu_power_limit_max_w=power_max,
        gpu_core_clock_offset_min_mhz=core_min,
        gpu_core_clock_offset_max_mhz=core_max,
        gpu_core_clock_lock_min_mhz=core_lock_min,
        gpu_core_clock_lock_max_mhz=core_lock_max,
        gpu_memory_clock_offset_min_mhz=memory_min,
        gpu_memory_clock_offset_max_mhz=memory_max,
        gpu_memory_clock_lock_min_mhz=memory_lock_min,
        gpu_memory_clock_lock_max_mhz=memory_lock_max,
        gpu_fan_min_percent=fan_min,
        gpu_fan_max_percent=fan_max,
        command_history_limit=history_limit,
    )
