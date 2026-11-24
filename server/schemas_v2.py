"""Flight V2 request models.

Kept in a separate module while the legacy flight-sheet model remains the
rollback/API compatibility surface.  No endpoint imports this module yet.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .miner_adapters import MINER_ADAPTERS
from .miner_adapters.validation import (
    parse_extra_args, parse_pool_endpoint, validate_option_value,
)
from .schemas import FlightSheetCreate, StrictRequest


_ADAPTER_SEMVER = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def adapter_public_version(adapter: Any) -> str:
    version = getattr(adapter, "adapter_version", None)
    if isinstance(version, str) and _ADAPTER_SEMVER.fullmatch(version):
        return version
    revision = getattr(adapter, "adapter_revision", None)
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
        return f"{revision}.0.0"
    raise ValueError("miner adapter has no valid public revision")


def _normalize_options(adapter: Any, raw: dict[str, Any]) -> dict[str, Any]:
    specs = {item.name: item for item in adapter.options}
    if set(raw) - set(specs):
        raise ValueError("options contain unsupported adapter keys")
    result: dict[str, Any] = {}
    for name, spec in specs.items():
        value = raw.get(name, spec.default)
        if name == "extra_args":
            value = validate_option_value(spec, value)
            parse_extra_args(value, miner_type=adapter.legacy_miner_type)
            result[name] = value
            continue
        if value is None:
            if not spec.nullable:
                raise ValueError(f"adapter option {name} is required")
        else:
            value = validate_option_value(spec, value)
        result[name] = value
    return result


class FlightJobV2Create(StrictRequest):
    job_id: str = Field(default_factory=_new_job_id, min_length=1, max_length=64)
    adapter_id: str = Field(
        min_length=3, max_length=80, pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$"
    )
    adapter_revision: str = Field(min_length=5, max_length=64)
    miner_version: str = Field(min_length=2, max_length=40)
    resource: Literal["cpu", "gpu"]
    coin: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9._+-]+$")
    wallet_id: str = Field(min_length=1, max_length=64)
    pool_url: str = Field(min_length=8, max_length=512)
    pool_password: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        default="x", max_length=128
    )
    miner_package_id: str = Field(min_length=1, max_length=64)
    execution_mode: Literal["native", "wsl"] = "native"
    algorithm: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.+/-]+$"
    )
    options: dict[str, Any] = Field(default_factory=dict)
    devices: list[int] = Field(default_factory=list, max_length=32)
    process_group: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def normalize_seine_defaults(cls, value: Any) -> Any:
        if isinstance(value, dict) and str(value.get("adapter_id", "")).strip().lower() in {"seine", "org.zpalmtree.seine"}:
            value = dict(value)
            value.setdefault("pool_password", "")
        return value

    @field_validator("job_id")
    @classmethod
    def job_id_is_canonical(cls, value: str) -> str:
        if _IDENTIFIER.fullmatch(value) is None:
            raise ValueError("job_id is not canonical")
        return value

    @field_validator("adapter_revision")
    @classmethod
    def revision_is_semver(cls, value: str) -> str:
        if _ADAPTER_SEMVER.fullmatch(value) is None:
            raise ValueError("adapter_revision must be canonical SemVer")
        return value

    @field_validator("coin")
    @classmethod
    def normalize_coin(cls, value: str) -> str:
        return value.upper()

    @field_validator("pool_password")
    @classmethod
    def password_has_no_control_characters(cls, value: str, info: Any) -> str:
        if info.data.get("adapter_id") in {"seine", "org.zpalmtree.seine"} and value != "":
            raise ValueError("Seine does not support pool_password; leave it empty")
        if any(ord(character) < 32 for character in value):
            raise ValueError("pool_password contains control characters")
        return value

    @field_validator("devices")
    @classmethod
    def devices_are_unique_indices(cls, value: list[int]) -> list[int]:
        if any(
            isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 31
            for index in value
        ):
            raise ValueError("devices must contain GPU indices from 0 to 31")
        if len(value) != len(set(value)):
            raise ValueError("devices must be unique")
        return value

    @model_validator(mode="after")
    def adapter_contract(self) -> "FlightJobV2Create":
        try:
            adapter = MINER_ADAPTERS.require(self.adapter_id)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        self.adapter_id = adapter.adapter_id
        expected_revision = adapter_public_version(adapter)
        if self.adapter_revision != expected_revision:
            raise ValueError(
                f"adapter_revision must be {expected_revision} for {adapter.adapter_id}"
            )
        try:
            self.miner_version = adapter.release.normalize_version(self.miner_version)
            self.algorithm = adapter.normalize_algorithm(self.algorithm, coin=self.coin)
            expected_resource = adapter.algorithm_resource(self.algorithm, coin=self.coin)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if self.resource != expected_resource:
            raise ValueError("resource disagrees with the selected algorithm")
        parse_pool_endpoint(self.pool_url, adapter.pool_schemes)
        if not adapter.matches_package_id(self.miner_package_id):
            raise ValueError("miner_package_id disagrees with the selected adapter")
        capability = "wsl_gpu" if self.execution_mode == "wsl" else "native"
        if capability not in adapter.capabilities:
            raise ValueError("execution_mode is unsupported by the selected adapter")
        if self.execution_mode == "wsl" and self.resource != "gpu":
            raise ValueError("WSL execution is available only to GPU jobs")
        if self.resource == "cpu" and self.devices:
            raise ValueError("CPU jobs cannot select GPU devices")
        if self.devices and "device_selection" not in adapter.capabilities:
            raise ValueError("the selected adapter does not support device selection")
        self.options = _normalize_options(adapter, self.options)
        if self.process_group is None:
            self.process_group = f"pg_{self.job_id}"
        elif _IDENTIFIER.fullmatch(self.process_group) is None:
            raise ValueError("process_group is not canonical")
        return self


class FlightSheetV2Create(StrictRequest):
    schema_version: Literal[2] = 2
    name: str = Field(min_length=1, max_length=80)
    worker_name_template: str = Field(default="{hostname}", min_length=1, max_length=128)
    jobs: list[FlightJobV2Create] = Field(min_length=1, max_length=4)

    @field_validator("worker_name_template")
    @classmethod
    def worker_template_is_safe(cls, value: str) -> str:
        return FlightSheetCreate.validate_worker_template(value)

    @model_validator(mode="after")
    def job_topology(self) -> "FlightSheetV2Create":
        if len({job.job_id for job in self.jobs}) != len(self.jobs):
            raise ValueError("job_id values must be unique")
        groups: dict[str, list[FlightJobV2Create]] = {}
        for job in self.jobs:
            assert job.process_group is not None
            groups.setdefault(job.process_group, []).append(job)
        if any(len(group) > 2 for group in groups.values()):
            raise ValueError("a process_group can contain at most two jobs")
        for group in groups.values():
            if len(group) != 2:
                continue
            first, second = group
            adapter = MINER_ADAPTERS.require(first.adapter_id)
            if "integrated_dual" not in adapter.capabilities:
                raise ValueError("the adapter does not support an integrated process group")
            def identity(item: FlightJobV2Create) -> tuple[str, ...]:
                return (
                    item.adapter_id,
                    item.adapter_revision,
                    item.miner_version,
                    item.miner_package_id,
                    item.execution_mode,
                )
            if identity(first) != identity(second):
                raise ValueError(
                    "integrated jobs must share adapter, revision, version, package, and execution mode"
                )
        gpu_jobs = [job for job in self.jobs if job.resource == "gpu"]
        for position, first in enumerate(gpu_jobs):
            for second in gpu_jobs[position + 1 :]:
                if first.process_group == second.process_group:
                    continue
                if (
                    not first.devices
                    or not second.devices
                    or set(first.devices).intersection(second.devices)
                ):
                    raise ValueError("independent GPU jobs have conflicting device ownership")
        return self


__all__ = ["FlightJobV2Create", "FlightSheetV2Create", "adapter_public_version"]
