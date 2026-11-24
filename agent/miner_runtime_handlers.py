"""Static, audited runtime preparation handlers for managed miners.

The controller adapter catalog contains data only.  It may select one of the
runtime identifiers registered here, but it can never supply Python modules,
callable names, shell fragments, or package callbacks.  This small registry is
therefore the Agent's trust boundary between adapter metadata and executable
launch planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class MinerRuntimePrepareContext:
    """Fully validated inputs available to one built-in runtime handler."""

    document: Mapping[str, Any]
    primary: Mapping[str, Any]
    runtime: Path
    executable: Path
    execution_mode: str
    api_port: int
    miner_log: Path
    wsl_runtime: Any | None
    system_name: str
    device_runner: Callable[..., Any]
    privilege_checker: Callable[[str], bool]


@dataclass(frozen=True)
class MinerRuntimePreparation:
    """The bounded launch-specific portion of a managed miner plan."""

    argv: list[str]
    gpu_mapping: list[dict[str, Any]]
    config_path: Path | None = None


MinerRuntimePrepareHandler = Callable[
    [MinerRuntimePrepareContext], MinerRuntimePreparation
]


class TrustedMinerRuntimeHandlerRegistry:
    """Immutable runtime-id to audited callable mapping.

    Registration happens only in Agent source code.  Runtime identifiers from
    controller data are lookups into this mapping and cannot trigger dynamic
    imports or arbitrary package code.
    """

    def __init__(
        self,
        handlers: Mapping[str, MinerRuntimePrepareHandler],
    ) -> None:
        copied: dict[str, MinerRuntimePrepareHandler] = {}
        for runtime_id, handler in handlers.items():
            if (
                not isinstance(runtime_id, str)
                or not runtime_id.startswith("builtin.")
                or not runtime_id.endswith(".v1")
                or runtime_id in copied
                or not callable(handler)
            ):
                raise ValueError("trusted miner runtime registry is invalid")
            copied[runtime_id] = handler
        if not copied:
            raise ValueError("trusted miner runtime registry must not be empty")
        self._handlers = MappingProxyType(copied)

    @property
    def runtime_ids(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def get(self, runtime_id: object) -> MinerRuntimePrepareHandler | None:
        if not isinstance(runtime_id, str):
            return None
        return self._handlers.get(runtime_id)
