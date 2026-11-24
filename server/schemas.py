from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from urllib.parse import ParseResult, urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from .miner_adapters.validation import (
    SUPPORTED_VERSION, normalize_extra_args, normalize_pool_text, parse_extra_args,
)
from .miner_adapters.workload import WORKLOAD_COIN_MAX_LENGTH, canonical_workload_coin
from .workload import _utc_time
from .miner_adapters import MINER_ADAPTERS
from .miner_adapters.templates import (
    DEFAULT_WORKER_TEMPLATE, DEFAULT_WALLET_WORKER_TEMPLATE, MAX_WORKER_TEMPLATE_CHARS,
    validate_worker_name_template,
)


LAN_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=256, repr=False)
    new_password: SecretStr = Field(min_length=12, max_length=256, repr=False)
    confirm_password: SecretStr = Field(min_length=12, max_length=256, repr=False)

    @model_validator(mode="after")
    def matching_passwords(self) -> "PasswordChangeRequest":
        if self.new_password.get_secret_value() != self.confirm_password.get_secret_value():
            raise ValueError("New passwords do not match")
        return self


class InstallationSetupUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0, le=2**63 - 1)
    action: Literal["step", "later", "skip", "complete", "reopen"]
    step: Literal["password", "device", "configuration"] | None = None

    @model_validator(mode="after")
    def step_action(self) -> "InstallationSetupUpdate":
        if self.action == "step" and self.step is None:
            raise ValueError("step action requires a step")
        if self.action != "step" and "step" in self.model_fields_set:
            raise ValueError("step is only valid for the step action")
        return self


class EnrollmentTokenCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(default="new-device", min_length=1, max_length=80)
    ttl_minutes: int = Field(default=30, ge=5, le=1440)


class AgentEnrollRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    enrollment_token: str | None = Field(default=None, min_length=20, max_length=256, repr=False)
    enrollment_request_id: Annotated[str | None, StringConstraints(strip_whitespace=False)] = Field(
        default=None, min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$", repr=False)
    proposed_agent_secret: Annotated[str | None, StringConstraints(strip_whitespace=False)] = Field(
        default=None, min_length=52, max_length=52, pattern=r"^rpa_[A-Za-z0-9_-]{48}$", repr=False)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    hostname: str = Field(min_length=1, max_length=255)
    os_family: str = Field(min_length=1, max_length=32)
    platform: str = Field(default="unknown", min_length=1, max_length=512)
    agent_version: str = Field(default="unknown", min_length=1, max_length=32)

    @field_validator("enrollment_token", mode="before")
    @classmethod
    def empty_token(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def complete_recovery_material(self):
        recovery_fields = {"enrollment_request_id", "proposed_agent_secret"}
        if self.model_fields_set & recovery_fields:
            if not self.enrollment_request_id or not self.proposed_agent_secret:
                raise ValueError("enrollment_request_id and proposed_agent_secret must be provided together")
        elif self.enrollment_token is None:
            raise ValueError("enrollment_token is required for legacy enrollment")
        return self


class AgentReplacementTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    confirm_name: str = Field(min_length=1, max_length=80)
    ttl_minutes: int = Field(default=15, ge=5, le=60)


class AgentReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    replacement_token: str = Field(
        min_length=47,
        max_length=47,
        pattern=r"^rpr_[A-Za-z0-9_-]{43}$",
    )
    request_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
    )
    proposed_agent_secret: str = Field(
        min_length=52,
        max_length=52,
        pattern=r"^rpa_[A-Za-z0-9_-]{48}$",
    )
    hostname: str = Field(min_length=1, max_length=255)
    os_family: str = Field(min_length=1, max_length=32)
    platform: str = Field(default="unknown", min_length=1, max_length=512)
    agent_version: str = Field(default="unknown", min_length=1, max_length=32)


class WorkloadOwnerGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    name: str = Field(min_length=1, max_length=80)
    agent_ids: list[str] = Field(min_length=1, max_length=4096)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value) or not value.strip():
            raise ValueError("invalid owner name")
        value.encode("utf-8")
        return value.strip()

    @field_validator("agent_ids")
    @classmethod
    def validate_agents(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) for value in values):
            raise ValueError("invalid Agent ID")
        if len(set(values)) != len(values):
            raise ValueError("duplicate Agent ID")
        return values


class WorkloadOwnerGroupsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=0, le=2**63 - 1)
    groups: list[WorkloadOwnerGroup] = Field(max_length=512)

    @model_validator(mode="after")
    def unique_groups_and_members(self) -> "WorkloadOwnerGroupsUpdate":
        ids, names, agents = set(), set(), set()
        for group in self.groups:
            if group.id in ids or group.name in names or agents.intersection(group.agent_ids):
                raise ValueError("owner groups and Agent membership must be unique")
            ids.add(group.id)
            names.add(group.name)
            agents.update(group.agent_ids)
        return self


class WorkloadSettlementNoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settlement_at: datetime
    expected_revision: int = Field(ge=0, le=2**63 - 1)

    @field_validator("settlement_at")
    @classmethod
    def validate_settlement_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("settlement_at must include a timezone")
        normalized = _utc_time(value)
        if normalized is None:
            raise ValueError("settlement_at cannot be represented in UTC")
        if (
            normalized.minute % 5 != 0
            or normalized.second != 0
            or normalized.microsecond != 0
        ):
            raise ValueError("settlement_at must be on a five-minute boundary")
        return normalized


class WorkloadCounter(BaseModel):
    """One absolute Agent-side supported-coin hashrate integral."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    coin: str = Field(min_length=1, max_length=WORKLOAD_COIN_MAX_LENGTH)
    algorithm: str = Field(min_length=1, max_length=64)
    counter_epoch: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    snapshot_seq: int = Field(ge=0, le=2**63 - 1)
    estimated_work_total: str | int | float
    observed_at: str = Field(min_length=20, max_length=64)

    @field_validator("coin", mode="before")
    @classmethod
    def validate_coin(cls, value: Any) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= WORKLOAD_COIN_MAX_LENGTH or canonical_workload_coin(value) != value:
            raise ValueError("invalid canonical workload coin")
        return value

    @field_validator("algorithm", mode="before")
    @classmethod
    def normalize_algorithm(cls, value: Any) -> str:
        if not isinstance(value, str) or len(value) > 64 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("invalid workload algorithm")
        normalized = value.strip().casefold()
        if not normalized or len(normalized) > 64:
            raise ValueError("invalid workload algorithm")
        return normalized

    @field_validator("estimated_work_total")
    @classmethod
    def validate_total(cls, value: str | int | float) -> str:
        if isinstance(value, bool):
            raise ValueError("estimated_work_total must be a non-negative number")
        text = str(value).strip()
        if not text or len(text) > 128:
            raise ValueError("estimated_work_total is out of range")
        try:
            total = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("estimated_work_total must be a number") from exc
        if not total.is_finite() or total < 0 or total > Decimal("1e100"):
            raise ValueError("estimated_work_total is out of range")
        return text

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: str) -> str:
        normalized = _utc_time(value)
        if normalized is None:
            raise ValueError("observed_at must be an ISO timestamp with a timezone representable in UTC")
        return normalized.isoformat(timespec="microseconds")


class HeartbeatRequest(BaseModel):
    # Heartbeats intentionally keep Pydantic's forward-compatible handling of
    # unknown fields, but identity strings must be normalized before their
    # length constraints run.  Otherwise a string containing only spaces can
    # pass validation and overwrite a device's visible identity with "".
    model_config = ConfigDict(str_strip_whitespace=True)

    hostname: str = Field(min_length=1, max_length=255)
    os_family: str = Field(min_length=1, max_length=32)
    platform: str = Field(default="unknown", min_length=1, max_length=512)
    agent_version: str = Field(default="unknown", min_length=1, max_length=32)
    ip_addresses: list[str] = Field(default_factory=list, max_length=32)
    local_ip_addresses: list[str] = Field(default_factory=list, max_length=32)
    public_ip_addresses: list[str] = Field(default_factory=list, max_length=32)
    metrics: dict[str, Any] = Field(default_factory=dict)
    inventory: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    workload_counters: list[WorkloadCounter] = Field(
        default_factory=list, max_length=16
    )

    @field_validator("local_ip_addresses")
    @classmethod
    def normalize_local_ip_addresses(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            try:
                address = ipaddress.ip_address(raw.split("%", 1)[0])
            except ValueError as exc:
                raise ValueError("invalid local IP address") from exc
            if not any(address in network for network in LAN_NETWORKS):
                raise ValueError("local IP address is outside LAN address ranges")
            canonical = str(address)
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @field_validator("public_ip_addresses")
    @classmethod
    def normalize_public_ip_addresses(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            try:
                address = ipaddress.ip_address(raw.split("%", 1)[0])
            except ValueError as exc:
                raise ValueError("invalid public IP address") from exc
            if not address.is_global:
                raise ValueError("public IP address is not globally routable")
            canonical = str(address)
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for capability in value:
            item = capability.strip().lower()
            if not item or len(item) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", item):
                raise ValueError("invalid Agent capability")
            if item not in normalized:
                normalized.append(item)
        return normalized

    @field_validator("workload_counters")
    @classmethod
    def unique_workload_series(
        cls, value: list[WorkloadCounter]
    ) -> list[WorkloadCounter]:
        identities = [(counter.coin, counter.algorithm) for counter in value]
        if len(identities) != len(set(identities)):
            raise ValueError("workload_counters must contain unique series")
        return value


class AgentReleaseDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent_id: str = Field(min_length=36, max_length=36)


class _AgentBatchRequest(BaseModel):
    """Shared identity constraints for an explicit set of target devices."""

    model_config = ConfigDict(extra="forbid", strict=True)

    agent_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("agent_ids")
    @classmethod
    def unique_agent_ids(cls, value: list[str]) -> list[str]:
        if any(
            not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                agent_id,
            )
            for agent_id in value
        ):
            raise ValueError("each agent_id must be a UUID")
        if len(value) != len(set(value)):
            raise ValueError("agent_ids must be unique")
        return value


class AgentReleaseBatchDeployRequest(_AgentBatchRequest):
    """An explicit, bounded set of devices selected for one Agent release."""

    activate_remote_workspace: bool = False


class FlightSheetBatchApplyRequest(_AgentBatchRequest):
    """An explicit, bounded set of devices selected for one flight sheet."""


class MiningControlBatchRequest(_AgentBatchRequest):
    """An explicit, bounded set of devices selected for one mining-control action."""

    action: Literal["stop_mining", "restart_mining"]


class CommandCreate(BaseModel):
    action: Literal[
        "collect_diagnostics",
        "collect_miner_logs",
        "stop_mining",
        "restart_mining",
        "remove_flight_sheet",
        "scan_mining_residue.v1",
        "cleanup_mining_residue.v1",
        "scan_miner_cache.v1",
        "cleanup_miner_cache.v1",
        "restart_service",
        "reboot",
        "shutdown",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def validate_payload_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value)) > 8_000:
            raise ValueError("payload is too large")
        return value


class CommandResult(BaseModel):
    status: Literal["running", "succeeded", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=4000)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    # Deprecated compatibility field.  The dashboard no longer exposes device
    # tags/grouping, but older API clients may still send it.
    tags: list[str] | None = Field(default=None, max_length=20)
    disabled: bool | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        cleaned = []
        for tag in value:
            tag = tag.strip().lower()
            if not tag or len(tag) > 32:
                raise ValueError("each tag must contain 1-32 characters")
            if tag not in cleaned:
                cleaned.append(tag)
        return cleaned


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: Literal["viewer", "operator", "admin"] = "viewer"


class StrictRequest(BaseModel):
    """Request model that rejects accidental secret or unsupported fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class MiningAutostartUpdate(StrictRequest):
    enabled: bool


class LinuxSshConfigureRequest(StrictRequest):
    enabled: bool


class WalletCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    coin: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9._+-]+$")
    address: str = Field(min_length=3, max_length=256)
    note: str = Field(default="", max_length=500)

    @field_validator("coin")
    @classmethod
    def normalize_coin(cls, value: str) -> str:
        return value.upper()

    @field_validator("address")
    @classmethod
    def public_address_only(cls, value: str) -> str:
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("address must be a single public wallet address")
        lowered = value.lower()
        extended_private_prefixes = (
            "xprv", "yprv", "zprv", "tprv", "uprv", "vprv",
        )
        looks_like_raw_private_key = bool(
            re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", value)
        )
        looks_like_wif = bool(
            re.fullmatch(r"[5KL][1-9A-HJ-NP-Za-km-z]{50,51}", value)
            or re.fullmatch(r"6P[1-9A-HJ-NP-Za-km-z]{56}", value)
        )
        looks_like_stellar_seed = bool(re.fullmatch(r"S[A-Z2-7]{55}", value))
        if (
            lowered.startswith(extended_private_prefixes)
            or looks_like_raw_private_key
            or looks_like_wif
            or looks_like_stellar_seed
            or value.startswith(("{", "["))
        ):
            raise ValueError("private keys and seed material are not accepted")
        return value


class WalletUpdate(WalletCreate):
    """A complete wallet replacement using the same public-data validation."""

    pass


class PoolCreate(StrictRequest):
    """A saved pool is copied into a flight; changing it never edits a flight."""

    name: str = Field(min_length=1, max_length=80)
    coin: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9._+-]+$")
    pool_url: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(min_length=8, max_length=512)
    pool_password: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(default="", max_length=128)
    note: str = Field(default="", max_length=500)

    @field_validator("pool_url", mode="before")
    @classmethod
    def check_raw_pool_url(cls, value: Any) -> str:
        return normalize_pool_text(value)

    @field_validator("coin")
    @classmethod
    def normalize_coin(cls, value: str) -> str:
        return value.upper()

    @field_validator("pool_password")
    @classmethod
    def reject_password_controls(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("pool_password contains control characters")
        return value


class PoolUpdate(PoolCreate):
    """Replace one saved pool snapshot; existing flight sheets are untouched."""

    pass


GpuPowerLimit = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class GpuSetting(StrictRequest):
    """One bounded GPU tuning entry for reusable standalone OC profiles."""

    index: int = Field(ge=0, le=31)
    power_limit_w: GpuPowerLimit | None = None
    core_clock_offset_mhz: int | None = Field(default=None, ge=-2000, le=2000)
    core_clock_lock_mhz: int | None = Field(default=None, ge=100, le=5000)
    memory_clock_offset_mhz: int | None = Field(default=None, ge=-5000, le=5000)
    memory_clock_lock_mhz: int | None = Field(default=None, ge=100, le=20000)
    fan_percent: int | None = Field(default=None, ge=30, le=100)

    @model_validator(mode="after")
    def require_setting(self):
        if all(
            value is None
            for value in (
                self.power_limit_w,
                self.core_clock_offset_mhz,
                self.core_clock_lock_mhz,
                self.memory_clock_offset_mhz,
                self.memory_clock_lock_mhz,
                self.fan_percent,
            )
        ):
            raise ValueError("each GPU entry must contain at least one setting")
        return self


class FlightGpuSetting(StrictRequest):
    """One legacy-index or model-matched SRBMiner flight-sheet OC rule."""

    # ``index`` remains accepted with SRBMiner's --list-devices GPU-ID semantics
    # so already-created flight sheets can still be edited and applied. New
    # sheets use ``model_match``: the Agent
    # resolves it against SRBMiner's local --list-devices output immediately
    # before launch, after integrated GPUs have been removed.
    index: int | None = Field(default=None, ge=0, le=31)
    model_match: str | None = Field(
        default=None,
        min_length=1,
        max_length=96,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    core_clock_offset_mhz: int | None = Field(default=None, ge=-2000, le=2000)
    core_clock_lock_mhz: int | None = Field(default=None, ge=100, le=5000)
    memory_clock_offset_mhz: int | None = Field(default=None, ge=-5000, le=5000)
    memory_clock_lock_mhz: int | None = Field(default=None, ge=100, le=20000)
    power_limit_w: GpuPowerLimit | None = None

    @field_validator("model_match")
    @classmethod
    def normalize_model_match(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(re.sub(r"[^a-z0-9]+", "", value.casefold())) < 2:
            raise ValueError("GPU model matcher must contain at least two letters or digits")
        return value

    @model_validator(mode="after")
    def require_setting(self):
        if (self.index is None) == (self.model_match is None):
            raise ValueError("each GPU entry must contain exactly one target: index or model_match")
        if all(
            value is None
            for value in (
                self.core_clock_offset_mhz,
                self.core_clock_lock_mhz,
                self.memory_clock_offset_mhz,
                self.memory_clock_lock_mhz,
                self.power_limit_w,
            )
        ):
            raise ValueError("each GPU entry must contain at least one setting")
        return self


def _require_unique_gpu_indices(value: list[Any]) -> list[Any]:
    indices = [setting.index for setting in value]
    if len(indices) != len(set(indices)):
        raise ValueError("GPU indices must be unique")
    return value


class FlightGpuOc(StrictRequest):
    """NVIDIA OC settings applied immediately before managed SRBMiner starts."""

    vendor: Literal["nvidia"]
    settings: list[FlightGpuSetting] = Field(min_length=1, max_length=32)

    @field_validator("settings")
    @classmethod
    def unique_gpu_targets(
        cls, value: list[FlightGpuSetting]
    ) -> list[FlightGpuSetting]:
        modes = {"model" if setting.model_match is not None else "index" for setting in value}
        if len(modes) != 1:
            raise ValueError("GPU OC rows cannot mix legacy indices and model matchers")
        if "index" in modes:
            return _require_unique_gpu_indices(value)
        matchers = [
            re.sub(r"[^a-z0-9]+", "", str(setting.model_match).casefold())
            for setting in value
        ]
        if len(matchers) != len(set(matchers)):
            raise ValueError("GPU model matchers must be unique")
        return value


class FlightMinerCreate(StrictRequest):
    # ``generic`` keeps existing API clients and custom local supervisors
    # compatible. The web console exposes the supported managed choices.
    miner_type: str = Field(default="generic", min_length=1, max_length=80)
    # Built-in adapters retain their exact version grammar in the model
    # validator.
    miner_version: str | None = Field(
        default=None, min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._+-]+$",
    )
    coin: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9._+-]+$")
    wallet_id: str = Field(min_length=1, max_length=64)
    pool_url: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=0, max_length=4096,
    )
    # The bundled Windows portable agent allows the ``default`` profile out of
    # the box.  Keeping the API default here lets the simple flight-sheet form
    # omit this implementation detail while custom installations can still
    # supply one of their locally configured profile names.
    miner_profile: str = Field(
        default="default", min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.+-]+$"
    )
    miner_package_id: str | None = Field(default=None, min_length=1, max_length=64)
    # Per-leg execution keeps native CPU mining independent from an optional
    # WSL GPU miner in the same flight sheet.  WSL availability is deliberately
    # checked on the target Windows Agent at dispatch/start time; saving a
    # flight sheet never installs or enables WSL remotely.
    execution_mode: Literal["native", "wsl"] = "native"
    algorithm: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.+/-]+$"
    )
    pool_password: Annotated[
        str,
        StringConstraints(strip_whitespace=False),
    ] = Field(default="x", max_length=128)
    device_indices: list[int] = Field(default_factory=list, max_length=32)
    extra_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("miner_type")
    @classmethod
    def validate_miner_adapter(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "generic":
            return normalized
        try:
            return MINER_ADAPTERS.require(normalized).legacy_miner_type
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @model_validator(mode="before")
    @classmethod
    def normalize_miner_defaults(cls, value: Any) -> Any:
        if isinstance(value, dict) and str(value.get("miner_type", "")).strip().lower() in {"seine", "org.zpalmtree.seine"}:
            # Seine has no pool-password CLI mapping; omission means empty.
            value = dict(value)
            value.setdefault("pool_password", "")
        return value

    @field_validator("pool_url", mode="before")
    @classmethod
    def retain_legacy_pool_whitespace_rules(cls, value: Any) -> Any:
        if isinstance(value, str):
            return normalize_pool_text(value)
        return value

    @field_validator("coin")
    @classmethod
    def normalize_coin(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_miner_version_style(self) -> FlightMinerCreate:
        if "extra_args" in self.extra_config:
            extra_args = normalize_extra_args(self.extra_config["extra_args"])
            parse_extra_args(extra_args, miner_type=self.miner_type)
            self.extra_config["extra_args"] = extra_args
        self.pool_url = self.pool_url.strip()
        if not 8 <= len(self.pool_url) <= 512:
            raise ValueError("pool_url must contain 8 to 512 characters")
        if self.miner_version is not None:
            if not 4 <= len(self.miner_version) <= 14 or not SUPPORTED_VERSION.fullmatch(self.miner_version):
                raise ValueError("invalid built-in miner version")
            if self.miner_type != "generic":
                try:
                    MINER_ADAPTERS.require(self.miner_type).release.normalize_version(self.miner_version)
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
        return self

    @field_validator("pool_password")
    @classmethod
    def validate_pool_password(cls, value: str, info: Any) -> str:
        if info.data.get("miner_type") == "seine" and value != "":
            raise ValueError("Seine does not support pool_password; leave it empty")
        if any(ord(character) < 32 for character in value):
            raise ValueError("pool_password contains control characters")
        return value

    @field_validator("device_indices")
    @classmethod
    def validate_device_indices(cls, value: list[int]) -> list[int]:
        if any(index < 0 or index > 31 for index in value):
            raise ValueError("device indices must be from 0 to 31")
        if len(value) != len(set(value)):
            raise ValueError("device indices must be unique")
        return value


class FlightSheetCreate(FlightMinerCreate):
    name: str = Field(min_length=1, max_length=80)
    worker_name_template: str = Field(default=DEFAULT_WORKER_TEMPLATE, min_length=1, max_length=MAX_WORKER_TEMPLATE_CHARS)
    secondary_mode: Literal["integrated", "independent"] = "integrated"
    secondary: FlightMinerCreate | None = None
    gpu_oc: FlightGpuOc | None = None

    @model_validator(mode="after")
    def require_secondary_for_independent_mode(self) -> FlightSheetCreate:
        if self.secondary_mode == "independent" and self.secondary is None:
            raise ValueError("independent secondary_mode requires a secondary miner")
        return self

    @field_validator("worker_name_template")
    @classmethod
    def validate_worker_template(cls, value: str) -> str:
        return validate_worker_name_template(value)



def _parse_package_url(value: str) -> ParseResult:
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("download_url must not contain whitespace or controls")
    try:
        parsed = urlparse(value)
        port = parsed.port  # urlparse defers numeric and range validation
    except ValueError as exc:
        raise ValueError("download_url is malformed") from exc
    if port == 0:
        raise ValueError("download_url has an invalid port")
    return parsed


class MinerPackageCreate(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(
        min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._+-]+$"
    )
    download_url: str = Field(min_length=10, max_length=1024)
    # Retained as optional metadata for older API clients. Package deployment
    # must not depend on callers supplying a digest.
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    archive_type: Literal["zip", "tar.gz"]
    note: str = Field(default="", max_length=500)

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("download_url")
    @classmethod
    def validate_download_url(cls, value: str) -> str:
        parsed = _parse_package_url(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("download_url must contain a host and no credentials or fragment")
        loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            try:

                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("download_url must use HTTPS (HTTP is loopback-only)")
        if not parsed.path or parsed.path.endswith("/"):
            raise ValueError("download_url must point to a package file")
        return value


class MinerPackageImport(StrictRequest):
    """Metadata for importing and then serving a remote package locally."""

    name: str = Field(min_length=1, max_length=80)
    version: str = Field(
        min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._+-]+$"
    )
    download_url: str = Field(min_length=10, max_length=1024)
    archive_type: Literal["zip", "tar.gz"]
    note: str = Field(default="", max_length=500)
    managed_miner_type: str | None = Field(default=None, min_length=1, max_length=80)
    managed_os_family: Literal["windows", "linux"] | None = None
    managed_variant: Literal["nvidia", "amd"] | None = None
    managed_channel: Literal["test"] | None = None

    @field_validator("managed_miner_type")
    @classmethod
    def validate_managed_miner_adapter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return MINER_ADAPTERS.require(value).legacy_miner_type
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("download_url")
    @classmethod
    def validate_import_url(cls, value: str) -> str:
        parsed = _parse_package_url(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError(
                "download_url must use HTTPS and contain no credentials or fragment"
            )
        return value



class DeviceGpuOcSetting(GpuSetting):
    """Device edits distinguish omitted values from explicit null resets."""

    @model_validator(mode="after")
    def require_setting(self):
        if not self.model_fields_set - {"index"}:
            raise ValueError("device GPU OC must set or clear at least one field")
        return self


class AgentGpuOcApply(StrictRequest):
    """One immediate NVIDIA NVML adjustment for one reported device GPU."""

    vendor: Literal["nvidia"]
    gpu_uuid: str = Field(
        min_length=4,
        max_length=128,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    setting: DeviceGpuOcSetting


class AgentGpuOcBatchSetting(StrictRequest):
    """One shared OC adjustment applied to every explicitly selected GPU."""

    power_limit_w: GpuPowerLimit | None = None
    core_clock_offset_mhz: int | None = Field(default=None, ge=-2000, le=2000)
    core_clock_lock_mhz: int | None = Field(default=None, ge=100, le=5000)
    memory_clock_offset_mhz: int | None = Field(default=None, ge=-5000, le=5000)
    memory_clock_lock_mhz: int | None = Field(default=None, ge=100, le=20000)
    fan_percent: int | None = Field(default=None, ge=30, le=100)

    @model_validator(mode="after")
    def require_setting(self):
        if not self.model_fields_set:
            raise ValueError("batch GPU OC must set or explicitly clear at least one field")
        return self


class _GpuOcBatchTargets(StrictRequest):
    """Shared constraints for explicitly selected current card UUIDs."""

    vendor: Literal["nvidia"]
    gpu_uuids: list[str] = Field(min_length=1, max_length=32)

    @field_validator("gpu_uuids")
    @classmethod
    def validate_gpu_uuids(cls, value: list[str]) -> list[str]:
        if any(
            len(gpu_uuid) < 4
            or len(gpu_uuid) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in gpu_uuid)
            for gpu_uuid in value
        ):
            raise ValueError("invalid GPU UUID")
        if len(value) != len(set(value)):
            raise ValueError("gpu_uuids must be unique")
        return value


class AgentGpuOcBatchApply(_GpuOcBatchTargets):
    """Apply one shared adjustment only to the explicitly selected card UUIDs."""

    setting: AgentGpuOcBatchSetting


class AgentGpuOcBatchClear(_GpuOcBatchTargets):
    """Reset and forget only the explicitly selected current card UUIDs."""
