from __future__ import annotations

import hashlib

import pytest

from server.miner_adapters.runtime_v1 import (
    AdapterRuntimeDeclarationError,
    canonical_runtime_bytes,
    canonical_runtime_sha256,
    runtime_descriptor_for,
)


def test_runtime_canonical_hash_is_stable_and_excludes_envelope_digest():
    first = {"schema": "adapter-runtime.v1", "z": [2, 1], "a": {"x": True}}
    second = {
        "a": {"x": True},
        "runtime_sha256": "f" * 64,
        "z": [2, 1],
        "schema": "adapter-runtime.v1",
    }
    assert canonical_runtime_bytes(first) == canonical_runtime_bytes(second)
    assert canonical_runtime_sha256(first) == hashlib.sha256(
        canonical_runtime_bytes(second)
    ).hexdigest()


def test_runtime_canonical_hash_rejects_non_finite_json():
    with pytest.raises(AdapterRuntimeDeclarationError, match="finite JSON"):
        canonical_runtime_sha256(
            {"schema": "adapter-runtime.v1", "not_finite": float("nan")}
        )


def test_incomplete_runtime_fails_closed_instead_of_using_legacy_handler():
    with pytest.raises(AdapterRuntimeDeclarationError, match="incomplete"):
        runtime_descriptor_for("not-installed")
