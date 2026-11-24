"""PCI labels consume the shared registry without inventing memory identity."""
from pathlib import Path
import subprocess

import pytest

from agent.inventory import linux_nvidia_display_devices, nvidia_gpu_metrics
from server.pci_identity import VENDORS, BRAND_ALIASES


def _runner(vendor):
    def run(argv, **kwargs):
        assert kwargs['shell'] is False
        if argv[1].startswith('--query-gpu=index,'):
            return subprocess.CompletedProcess(argv, 0,
                f'0, NVIDIA GeForce Fixture Model A, GPU-pci-fixture, 00000000:01:00.0, '
                f'50, 16384, 1000, 50, 100, 0xaa30{vendor}, N/A, '
                'FE.ED.00.00.04, 999.01, 200, 200, 100, 300\n', '')
        return subprocess.CompletedProcess(argv, 2, '', 'unsupported optional query')
    return run


@pytest.mark.parametrize('vendor,expected', [
    ('10b0', 'Gainward / CardExpert'), ('1458', 'Gigabyte'),
    ('1022', 'Advanced Micro Devices, Inc. [AMD]'), ('fefe', None),
])
def test_nvidia_brand_uses_registry_independently_of_model_and_memory(vendor, expected):
    gpu = nvidia_gpu_metrics('nvidia-smi', runner=_runner(vendor), system_name='Linux')[0]
    assert gpu['board_vendor_id'] == vendor
    assert gpu['board_vendor'] == expected
    assert gpu['pci_subsystem_id'] == f'0xaa30{vendor}'
    assert gpu['memory_type'] is None
    assert gpu['memory_vendor'] is None
    assert gpu['memory_identity_source'] == 'nvml_private'


def test_new_model_with_registered_vendor_still_gets_board_label():
    original = _runner('10b0')
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        return subprocess.CompletedProcess(argv, result.returncode,
            result.stdout.replace('Fixture Model A', 'Unregistered Future GPU'), result.stderr)
    gpu = nvidia_gpu_metrics('nvidia-smi', runner=run, system_name='Linux')[0]
    assert gpu['board_vendor'] == 'Gainward / CardExpert'
    assert gpu['memory_vendor'] is None and gpu['memory_type'] is None


def test_driver_unavailable_path_uses_same_registry_and_keeps_raw_identity(tmp_path, monkeypatch):
    from agent import inventory
    actual = inventory._nvidia_bus_key
    monkeypatch.setattr(inventory, '_nvidia_bus_key', lambda v: actual(v.replace('_', ':', 2)))
    path = tmp_path/'0000_01_00.0';path.mkdir()
    for name, value in {'vendor':'0x10de','class':'0x030000','device':'0xab30',
                        'subsystem_vendor':'0x10b0','subsystem_device':'0xaa30'}.items():
        (path/name).write_text(value+'\n')
    gpu = linux_nvidia_display_devices(tmp_path)[0]
    assert gpu['board_vendor'] == 'Gainward / CardExpert'
    assert gpu['board_vendor_id'] == '10b0'
    assert gpu['pci_device_id'] == 'ab30'
    assert gpu['pci_subsystem_id'] == 'aa3010b0'
    assert gpu['memory_vendor'] is None and gpu['driver_available'] is False


def test_registered_vendor_outside_display_aliases_is_not_lost():
    vendor = next(k for k in VENDORS if k not in BRAND_ALIASES)
    gpu = nvidia_gpu_metrics('nvidia-smi', runner=_runner(vendor), system_name='Linux')[0]
    assert gpu['board_vendor'] == VENDORS[vendor]
