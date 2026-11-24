"""Exercise Windows device discovery with synthetic SRBMiner output."""

import subprocess

import pytest

from agent.managed_miners import (
    ManagedMinerError,
    TRUSTED_MINER_RUNTIME_HANDLERS,
    parse_srbminer_devices,
)
from agent.miner_runtime_handlers import MinerRuntimePrepareContext


def synthetic_sample(cuda_index, dual=False):
    first = "GPU0 [0][0] [09:00.0] : amd_radeon_graphics [iGPU: disabled by default]"
    second = first.replace("[0][0]", "[0][1]")
    rows = [first, second]
    if cuda_index == 2:
        rows.append(first.replace("GPU0", "GPU1").replace("09:00.0", "08:00.0"))
    rows.append(f"GPU{cuda_index} [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [blackwell]")
    return {"cuda_index": cuda_index, "dual": dual, "package": "3.8.6", "stdout": rows, "stderr": []}


SAMPLES = [synthetic_sample(1), synthetic_sample(2), synthetic_sample(1, dual=True)]


def prepare(sample, tmp_path, indices):
    # Reproduce the algorithm, package version and device selection. All pool
    # and wallet fields are synthetic. The only injected I/O is --list-devices.
    primary = {
        "miner_type": "srbminer", "miner_profile": "default",
        "miner_package": {"version": sample["package"]}, "device_indices": indices,
        "algorithm": "pearlhash", "coin": "PRL", "pool_url": "stratum+ssl://pool.example.test:443",
        "wallet_address": "fixture-wallet", "pool_password": "x", "extra_config": {},
    }
    secondary = (dict(primary, algorithm="randomx", coin="XMR", device_indices=[])
                 if sample["dual"] else None)
    document = {"version": 3, "flight_sheet_id": "fixture-sheet", "name": "fixture",
                "worker_name": "fixture-worker", "primary": primary, "secondary": secondary}
    exe = tmp_path / "SRBMiner-MULTI.exe"

    def replay(argv, *, cwd, shell, **kwargs):
        assert argv == [str(exe), "--list-devices"] and not shell and cwd == str(tmp_path)
        return subprocess.CompletedProcess(argv, 0, "\r\n".join(sample["stdout"]), "\r\n".join(sample["stderr"]))

    handler = TRUSTED_MINER_RUNTIME_HANDLERS.get("builtin.srbminer.v1")
    assert handler is not None
    return handler(MinerRuntimePrepareContext(
        document=document, primary=primary, executable=exe, runtime=tmp_path,
        miner_log=tmp_path / "fixture.log", api_port=24567, execution_mode="native",
        wsl_runtime=None, system_name="Windows", device_runner=replay,
        privilege_checker=lambda _: False,
    ))


def sample_name(sample):
    return f"cuda-{sample['cuda_index']}-dual-{sample['dual']}"


@pytest.mark.parametrize("sample", SAMPLES, ids=sample_name)
def test_synthetic_auto_selection_keeps_real_cuda_id_and_pci(sample, tmp_path):
    cuda_id = sample["cuda_index"]
    parsed = parse_srbminer_devices("\n".join(sample["stdout"]))
    assert [d["index"] for d in parsed] == list(range(cuda_id + 1))
    assert all(not d["discrete"] for d in parsed[:-1])
    assert parsed[-1]["discrete"] and parsed[-1]["index"] == cuda_id
    plan = prepare(sample, tmp_path, [])
    assert plan.argv.count("--gpu-id") == 1
    assert plan.argv[plan.argv.index("--gpu-id") + 1] == str(cuda_id)
    assert plan.gpu_mapping == [{
        "logical_index": 0, "miner_id": cuda_id, "vendor": "nvidia",
        "name": "nvidia_geforce_rtx_5080", "pci_bus_id": "0000:01:00.0",
    }]
    if sample["dual"]:
        assert plan.argv[plan.argv.index("--algorithm-gpu") + 1] == "pearlhash"
        assert plan.argv[plan.argv.index("--algorithm-cpu") + 1] == "randomx"


@pytest.mark.parametrize("sample", SAMPLES, ids=sample_name)
def test_synthetic_explicit_igpu_is_not_eligible(sample, tmp_path):
    with pytest.raises(ManagedMinerError, match="not an eligible discrete device"):
        prepare(sample, tmp_path, [0])


IGPU = "GPU0  [0][0] [09:00.0] : amd_radeon_tm__graphics [gfx90c] [iGPU: disabled by default]"
DUP = IGPU.replace("[0][0]", "[0][1]")
MARKER = " [iGPU: disabled by default]"
RTX0 = "GPU0  [CUDA][0] [0000:01:00.0] : nvidia_geforce_rtx_5080 [blackwell]"


@pytest.mark.parametrize("qualified", ["0000:09:00.0", "00000000:09:00.0"])
def test_equivalent_short_and_fully_qualified_igpu_pci_merge(qualified):
    devices = parse_srbminer_devices(IGPU + "\n" + DUP.replace("09:00.0", qualified))
    assert devices == [{"index": 0, "name": "amd_radeon_tm__graphics", "vendor": "amd",
                        "pci_bus_id": "0000:09:00.0", "discrete": False}]


def test_explicitly_marked_intel_integrated_alias_keeps_one_device():
    output = "\n".join((IGPU, DUP)).replace("amd_radeon_tm__graphics [gfx90c]", "intel_uhd_graphics_770")
    devices = parse_srbminer_devices(output)
    assert len(devices) == 1 and devices[0]["vendor"] == "intel" and not devices[0]["discrete"]


@pytest.mark.parametrize("rows", [
    [IGPU, DUP.replace("09:00.0", "0a:00.0")],
    [IGPU, DUP.replace("09:00.0", "0001:09:00.0")],
    [IGPU, DUP.replace("amd_radeon_tm__graphics", "amd_radeon_780m")],
    [IGPU, DUP.replace("amd_radeon_tm__graphics", "intel_uhd_graphics")],
    [IGPU, DUP.replace(MARKER, "")],
    [IGPU.replace(MARKER, ""), DUP],
    [IGPU.replace(MARKER, ""), DUP.replace(MARKER, "")],
    [s.replace("[09:00.0]", "") for s in (IGPU, DUP)],
    [s.replace("09:00.0", "09:20.0") for s in (IGPU, DUP)],
    [IGPU, DUP.replace("09:00.0", "00000:09:00.0")],
    [IGPU, DUP.replace("09:00.0", "000000:09:00.0")],
    [IGPU, DUP.replace("09:00.0", "0000000:09:00.0")],
    [IGPU, DUP.replace("09:00.0", "09:00.0] [0a:00.0")],
    [s.replace("amd_radeon_tm__graphics", "unidentified_graphics") for s in (IGPU, DUP)],
    [s.replace("amd_radeon_tm__graphics", "amd_radeon_rx_7900_xtx") for s in (IGPU, DUP)],
    [s.replace("amd_radeon_tm__graphics", "nvidia_geforce_rtx_5080").replace(MARKER, "") for s in (IGPU, DUP)],
    [IGPU, RTX0],
    [RTX0, RTX0.replace("[CUDA][0] [0000:01:00.0]", "[CUDA][1] [0000:02:00.0]")],
    [IGPU, DUP, DUP.replace("09:00.0", "0a:00.0")],
], ids=["different-bus", "different-domain", "different-model", "different-vendor",
        "unmarked-second", "unmarked-first", "both-unmarked", "missing-pci", "invalid-slot",
        "five-digit-domain", "six-digit-domain", "seven-digit-domain", "ambiguous-address",
        "unknown-vendor", "marked-discrete", "same-discrete-card", "different-gpu-class",
        "two-discrete-cards-same-model", "conflicting-third"])
def test_conflicting_repeat_still_rejects_entire_list(rows):
    with pytest.raises(ManagedMinerError, match="invalid GPU indices"):
        parse_srbminer_devices("\n".join(rows))
