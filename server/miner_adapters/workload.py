"""Shared coin and basis contract for observed Agent hashrate integrals.

These counters estimate hashes from fresh H/s samples. They do not represent
accepted-share difficulty, pool-confirmed work, or retrospectively filled data.
"""

from __future__ import annotations

import re


WORKLOAD_COIN_MAX_LENGTH = 24
WORKLOAD_COIN_MAX_INPUT_LENGTH = WORKLOAD_COIN_MAX_LENGTH
WORKLOAD_COIN_PATTERN = rf"[A-Z0-9._+-]{{1,{WORKLOAD_COIN_MAX_LENGTH}}}"
_COIN = re.compile(WORKLOAD_COIN_PATTERN)
# Suggestions for empty UI state, never an ingestion or query allowlist.
KNOWN_WORKLOAD_COINS = ("PEARL", "QUANTUS", "XMR")
SUPPORTED_WORKLOAD_COINS = KNOWN_WORKLOAD_COINS
WORKLOAD_BASIS = "agent_hashrate_integral"
WORKLOAD_COIN_SQL_CHECK = (
    f"length({{column}}) BETWEEN 1 AND {WORKLOAD_COIN_MAX_LENGTH} "
    "AND {column}=upper({column}) "
    "AND instr({column}, char(0))=0 "
    "AND {column} NOT GLOB '*[^A-Z0-9._+-]*' "
    "AND {column}<>'PRL'"
)


def canonical_workload_coin(
    value: object, *, allow_pearl_aliases: bool = False,
) -> str | None:
    """Return a bounded coin label from trusted observed/configured mining data.

    Agent sample collection already accepts PRL and PEARL spellings with
    separators. PRL always canonicalizes to PEARL; QTC stays distinct from
    QUANTUS. A syntactically valid label is not proof of a coin or its mining
    work: sample trust, freshness and H/s checks remain with the Agent.
    """
    if (not isinstance(value, str) or len(value) > WORKLOAD_COIN_MAX_INPUT_LENGTH
            or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return None
    if allow_pearl_aliases:
        legacy = "".join(character for character in value.casefold() if character.isalnum())
        if legacy in {"pearl", "prl"}:
            return "PEARL"
    normalized = value.strip().upper()
    if _COIN.fullmatch(normalized) is None:
        return None
    return "PEARL" if normalized == "PRL" else normalized
