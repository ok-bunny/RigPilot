"""Deterministic Controller declarations for Agent adapter-runtime.v1.

The Controller is the authority for audited adapter declarations.  This
module deliberately contains only JSON data builders and canonical hashing;
it never emits shell text, executable paths, environment variables, or code.

Concrete built-in descriptors are installed below only after the
Agent-side closed-schema validator has accepted them.  Keeping construction
and hashing here gives the Controller and the Agent migration fixtures one
stable source of canonical bytes.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover - imported only for static checking
    from .contract import MinerAdapter


ADAPTER_RUNTIME_SCHEMA = "adapter-runtime.v1"


class AdapterRuntimeDeclarationError(ValueError):
    """An adapter has no complete audited runtime-v1 declaration."""


def canonical_runtime_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the one canonical byte encoding used for runtime identities.

    ``runtime_sha256`` is an envelope field and is excluded from its own
    digest.  Non-finite values and non-JSON objects fail closed.
    """

    material = dict(value)
    material.pop("runtime_sha256", None)
    try:
        return json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AdapterRuntimeDeclarationError(
            "adapter runtime declaration is not finite JSON"
        ) from exc


def canonical_runtime_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_runtime_bytes(value)).hexdigest()


# Populated only with complete declarations accepted by the current Agent
# validator.  A missing entry is a hard error; callers must never silently
# fall back to a miner-name-specific runtime on the Flight V2 path.
_RUNTIMES: dict[str, dict[str, Any]] = {}


def _adapter_key(adapter_or_type: "MinerAdapter | str") -> str:
    if isinstance(adapter_or_type, str):
        key = adapter_or_type.strip().lower()
    else:
        key = str(adapter_or_type.legacy_miner_type).strip().lower()
    if not key:
        raise AdapterRuntimeDeclarationError("adapter runtime identity is missing")
    return key


def runtime_descriptor_for(adapter_or_type: "MinerAdapter | str") -> dict[str, Any]:
    """Return a defensive copy of one sealed built-in runtime declaration."""

    key = _adapter_key(adapter_or_type)
    descriptor = _RUNTIMES.get(key)
    if descriptor is None:
        raise AdapterRuntimeDeclarationError(
            f"adapter runtime declaration is incomplete for {key}"
        )
    return deepcopy(descriptor)


def runtime_sha256_for(adapter_or_type: "MinerAdapter | str") -> str:
    return str(runtime_descriptor_for(adapter_or_type)["runtime_sha256"])


def public_runtime_payload_for(
    adapter_or_type: "MinerAdapter | str",
) -> dict[str, Any]:
    """Return the exact declaration embedded in authenticated Flight V2 jobs."""

    return runtime_descriptor_for(adapter_or_type)


__all__ = [
    "ADAPTER_RUNTIME_SCHEMA",
    "AdapterRuntimeDeclarationError",
    "canonical_runtime_bytes",
    "canonical_runtime_sha256",
    "public_runtime_payload_for",
    "runtime_descriptor_for",
    "runtime_sha256_for",
]
