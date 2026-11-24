"""Namespace parser used by legacy SRBMiner runtime tests."""
import os
from pathlib import Path
from agent.native_miner_runtime import NativeMinerRuntimeError

_ROLES = frozenset({"single", "primary", "secondary"})

_SLOTS = ("slot-0", "slot-1")

_RECLAIM = "reclaiming"

def _fail(message: str) -> None:
    raise NativeMinerRuntimeError(message)

def _children(path: Path, allowed: set[str]) -> None:
    with os.scandir(path) as stream:
        if any(entry.name not in allowed for entry in stream):
            _fail("native SRBMiner runtime namespace has unknown entries")
