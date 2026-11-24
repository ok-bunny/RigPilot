"""Shared fixtures for the Agent test suite.

The delayed secondary-leg start for independent dual mining ships with a 30
second product default.  Runtime tests that are not about the delay itself
assert session completion inside ``apply()``, so the suite pins the module
constant to its synchronous value (0) by default.  Dedicated delay-semantics
tests override the constant explicitly.
"""

from __future__ import annotations

import pytest

import agent.inventory as inventory_module
import agent.managed_miners as managed_miners_module


@pytest.fixture(autouse=True)
def _synchronous_secondary_start_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        managed_miners_module, "SECONDARY_LEG_START_DELAY_SECONDS", 0.0
    )
    # The hashrate amplitude gate keeps per-series baselines across calls;
    # never leak one test's baselines into the next.
    inventory_module._HASHRATE_AMPLITUDE_GATE.reset()
    yield
    # Never leak an armed deferred start between tests.
    managed_miners_module.cancel_deferred_secondary_start("test_teardown")
    inventory_module._HASHRATE_AMPLITUDE_GATE.reset()
