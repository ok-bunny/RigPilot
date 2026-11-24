"""Trusted miner adapter registry shared by Controller and Agent."""

from .builtin import (
    BUILTIN_ADAPTERS,
    MINER_ADAPTERS,
    PEARLMINER_ADAPTER,
    SRBMINER_ADAPTER,
    SEINE_ADAPTER,
    XMRIG_ADAPTER,
)
from .retired import (
    RETIRED_MINER_TYPES, RETIRED_ADAPTER_IDENTITIES,
    is_retired_miner, retired_miner_type,
)
from .contract import (
    AlgorithmSpec,
    AssetSpec,
    MINER_ADAPTER_ABI,
    MinerAdapter,
    MinerAdapterRegistry,
    OptionSpec,
    PlatformSpec,
    PrereleasePolicy,
    ReleaseSource,
    ReleaseTagStyle,
    ReleaseVersionStyle,
)

__all__ = [
    "AlgorithmSpec",
    "AssetSpec",
    "BUILTIN_ADAPTERS",
    "RETIRED_MINER_TYPES",
    "RETIRED_ADAPTER_IDENTITIES",
    "is_retired_miner",
    "retired_miner_type",
    "MINER_ADAPTERS",
    "MINER_ADAPTER_ABI",
    "MinerAdapter",
    "MinerAdapterRegistry",
    "OptionSpec",
    "PEARLMINER_ADAPTER",
    "PlatformSpec",
    "PrereleasePolicy",
    "ReleaseSource",
    "ReleaseTagStyle",
    "ReleaseVersionStyle",
    "SRBMINER_ADAPTER",
    "SEINE_ADAPTER",
    "XMRIG_ADAPTER",
]


def list_adapters() -> tuple[MinerAdapter, ...]:
    return MINER_ADAPTERS.list()


def get_adapter(value: object) -> MinerAdapter | None:
    return MINER_ADAPTERS.get(value)


def require_adapter(value: object) -> MinerAdapter:
    return MINER_ADAPTERS.require(value)


def public_adapter_catalog() -> list[dict[str, object]]:
    return MINER_ADAPTERS.public_catalog()
