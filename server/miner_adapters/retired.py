"""Historical identities only; retired miners have no executable adapter."""
from __future__ import annotations

RETIRED_MINER_TYPES = frozenset({"quantusminer", "lolminer", "forgeminer", "hiveos"})
RETIRED_ADAPTER_IDENTITIES = (
    ("lolminer", "org.lolliedieb.lolminer"),
    ("forgeminer", "org.hashraptor.forgeminer"),
)


def retired_miner_type(value: object) -> str | None:
    """Recognize stored identities/package namespaces without reviving ABI."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in RETIRED_MINER_TYPES:
        return normalized
    for kind in RETIRED_MINER_TYPES:
        if normalized in {
            f"builtin.{kind}.v1", f"builtin.{kind}.http.v1",
            f"builtin.{kind}.log.v1", f"managed_mining.{kind}.v1",
        } or normalized.startswith((f"builtin-{kind}-", f"test-{kind}-", f"{kind}-")):
            return kind
    return next((kind for kind, identity in RETIRED_ADAPTER_IDENTITIES
                 if normalized == identity), None)


def is_retired_miner(value: object) -> bool:
    return retired_miner_type(value) is not None
