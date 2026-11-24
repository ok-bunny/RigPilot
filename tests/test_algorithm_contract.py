from __future__ import annotations

import pytest

from agent.managed_miners import (
    ManagedMinerError,
    SRB_DUAL_ALGORITHM_PAIRS as AGENT_DUAL_PAIRS,
    SRB_GPU_ALGORITHMS as AGENT_GPU_ALGORITHMS,
    leg_effective_resource,
)
from server.miner_adapters import MINER_ADAPTERS
from server.managed_miners import (
    SRB_CPU_ALGORITHMS,
    SRB_DUAL_ALGORITHM_PAIRS as SERVER_DUAL_PAIRS,
    SRB_GPU_ALGORITHMS as SERVER_GPU_ALGORITHMS,
    SRB_SIMPLE_ALGORITHMS,
)


def test_controller_and_agent_share_the_same_srbminer_algorithm_contract():
    specs = MINER_ADAPTERS.require("srbminer").algorithm_specs
    # Compatibility exports describe different real contracts: support vs default.
    assert SERVER_GPU_ALGORITHMS == {spec.name for spec in specs if "gpu" in spec.resources}
    assert AGENT_GPU_ALGORITHMS == {spec.name for spec in specs if spec.resource_kind == "gpu"}
    assert SERVER_DUAL_PAIRS == AGENT_DUAL_PAIRS
    assert SRB_SIMPLE_ALGORITHMS == SERVER_GPU_ALGORITHMS | SRB_CPU_ALGORITHMS
    assert "pearlhash" in SERVER_GPU_ALGORITHMS


@pytest.mark.parametrize("spec", MINER_ADAPTERS.require("srbminer").algorithm_specs, ids=lambda spec: spec.name)
def test_each_srb_algorithm_uses_its_default_or_explicit_supported_resource(spec):
    leg = {"miner_type":"srbminer", "algorithm":spec.name,
        "coin":spec.coins[0] if spec.coins else "LOCAL", "extra_config":{}}
    assert leg_effective_resource(leg) == spec.resource_kind
    for kind in ("cpu", "gpu"):
        selected = {**leg,"extra_config":{"resource_kind":kind}}
        if kind in spec.resources:
            assert leg_effective_resource(selected) == kind
        else:
            with pytest.raises(ManagedMinerError, match="task resource"):
                leg_effective_resource(selected)



@pytest.mark.parametrize("kind,algorithm,resource", [("xmrig", "randomx", "cpu"), ("srbminer", "randomx", "cpu"), ("srbminer", "pearlhash", "gpu")])
def test_extra_software_args_do_not_infer_algorithm_or_GPU_resource(kind, algorithm, resource):
    spec = MINER_ADAPTERS.require(kind).algorithm_spec(algorithm)
    leg = {"miner_type": kind, "algorithm": algorithm,
           "coin": spec.coins[0] if spec.coins else "LOCAL",
           "extra_config": {"extra_args": "--pearl-k1 --future-gpu-kernel"}}
    assert leg_effective_resource(leg) == resource
