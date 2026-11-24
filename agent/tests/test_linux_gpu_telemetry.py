from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent.linux_gpu_telemetry import canonical_pci_id, collect_gpu_snapshot


def nvidia(index=0, bus='0000:01:00.0', uuid='GPU-one', **fields):
    return {'vendor':'nvidia','index':index,'pci_bus_id':bus,'uuid':uuid,'name':'Fixture NVIDIA',
            'temperature_c':51,'fan_speed_percent':0,'power_w':100,'driver_available':True,**fields}


def pci_device(root, bdf, vendor='0x1002', cls='0x030000', driver=True, hwmon=True):
    path=root/bdf;path.mkdir(parents=True)
    for name,value in {'vendor':vendor,'class':cls,'device':'0x744c','product_name':'Fixture AMD'}.items():
        (path/name).write_text(value)
    if driver:
        target=root.parent/'drivers'/'amdgpu';target.mkdir(parents=True,exist_ok=True)
        (path/'driver').symlink_to(target,target_is_directory=True)
    (path/'drm'/'card7').mkdir(parents=True)
    if hwmon:
        monitor=path/'hwmon'/'hwmon4';monitor.mkdir(parents=True)
        for name,value in {'name':'amdgpu','temp1_input':'42500','temp2_input':'58000','temp3_input':'64000',
                           'power1_average':'123456789','pwm1':'128','fan1_input':'1700','freq1_input':'1500000000',
                           'freq2_input':'875000000'}.items():
            (monitor/name).write_text(value)
    return path


def snapshot(tmp_path, rows=(), **kwargs):
    pci=tmp_path/'pci';pci.mkdir(exist_ok=True)
    return collect_gpu_snapshot(nvidia_metrics=list(rows),pci_root=pci,captured_at=1000,**kwargs)


@pytest.mark.parametrize('raw,expected', [('01:00.0','00000000:01:00.0'),('0000:0A:1f.7','00000000:0a:1f.7'),
 ('12345678:ff:00.0','12345678:ff:00.0'),('0000:01:20.0',None),('01:00.8',None),('001:00.0',None),(None,None)])
def test_pci_canonicalization_preserves_domain_device_function(raw,expected):
    assert canonical_pci_id(raw)==expected


def test_reuses_rows_without_any_command_and_keeps_nvml_control_index(tmp_path):
    runner=Mock(side_effect=AssertionError('must reuse the heartbeat sample'))
    snap=snapshot(tmp_path,[nvidia(9,'0000:0b:00.0'),nvidia(3,'0000:02:00.0','GPU-two')],runner=runner)
    runner.assert_not_called()
    assert [d['native_index'] for d in snap['devices']]==[3,9]
    assert [d['display_index'] for d in snap['devices']]==[0,1]
    assert [d['index'] for d in snap['devices']]==[3,9]
    assert [d['control_index'] for d in snap['devices']]==[3,9]
    assert snap['devices'][0]['gpu_key']=='nvidia:GPU-two'
    assert snap['scan_complete'] and snap['sources']['nvidia']['status']=='ok'


def test_empty_success_differs_from_failed_scan(tmp_path):
    empty=snapshot(tmp_path)
    assert empty['devices']==[] and empty['scan_complete']
    assert empty['sources']['amd']['status']=='ok'
    failure=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path/'missing',captured_at=1000)
    assert failure['devices']==[] and not failure['scan_complete']
    assert failure['sources']['amd']=={'status':'unavailable','reason':'missing'}


def test_one_bounded_nvidia_query_no_optional_retry_groups(tmp_path):
    runner=Mock(return_value=subprocess.CompletedProcess([],0,'4, GPU-real, 0000:0a:00.0, Sample, 0, N/A, 80.5, 20, 1500, 5000\n',''))
    snap=collect_gpu_snapshot(pci_root=tmp_path,nvidia_smi='/trusted/nvidia-smi',runner=runner,captured_at=1000,timeout=1)
    runner.assert_called_once()
    args,opts=runner.call_args
    assert args[0][0]=='/trusted/nvidia-smi' and args[0][1].startswith('--query-gpu=index,uuid,pci.bus_id')
    assert opts['timeout']==1 and opts['shell'] is False
    row=snap['devices'][0]
    assert row['temperature_c']==0 and row['fan_speed_percent'] is None and row['power_w']==80.5


@pytest.mark.parametrize('failure,reason',[(FileNotFoundError(),'command_missing'),(subprocess.TimeoutExpired('nvidia-smi',1),'command_timeout'),(OSError(),'command_failed')])
def test_nvidia_query_failure_preserves_detected_hardware(tmp_path,failure,reason):
    pci_device(tmp_path,'0000:01:00.0',vendor='0x10de',driver=False,hwmon=False)
    runner=Mock(side_effect=failure)
    snap=collect_gpu_snapshot(pci_root=tmp_path,runner=runner,captured_at=1000)
    runner.assert_called_once()
    assert len(snap['devices'])==1
    row=snap['devices'][0]
    assert row['uuid'] is None and row['native_index'] is None and row['driver_available'] is False
    assert row['temperature_c'] is None and row['gpu_key']=='nvidia:00000000:01:00.0'
    assert snap['sources']['nvidia']['status']=='partial'


def test_no_nvidia_hardware_does_not_require_driver_tool(tmp_path):
    snap=collect_gpu_snapshot(pci_root=tmp_path,runner=Mock(side_effect=FileNotFoundError()),captured_at=1000)
    assert snap['sources']['nvidia']['status']=='not_present'
    assert snap['devices']==[] and snap['scan_complete']


def test_amd_reads_real_sysfs_units_and_distinguishes_pwm_rpm(tmp_path):
    device=pci_device(tmp_path,'0001:05:00.0')
    (device/'gpu_busy_percent').write_text('75')
    (device/'mem_info_vram_total').write_text(str(16*1024**3))
    before={str(p):p.read_bytes() for p in device.rglob('*') if p.is_file()}
    snap=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path,captured_at=1000)
    row=snap['devices'][0]
    assert row['gpu_key']=='amd:00000001:05:00.0' and row['uuid'] is None
    assert row['native_index']==7 and row['native_index_kind']=='drm'
    assert row['control_index'] is None and row['index'] is None
    assert row['temperature_c']==42.5 and row['power_w']==pytest.approx(123.456789)
    assert row['graphics_clock_mhz']==1500 and row['memory_clock_mhz']==875
    assert row['memory_total_mib']==16384 and row['utilization_percent']==75
    assert row['fan_rpm']==1700 and row['fan_speed_percent'] is None
    assert row['fan_pwm_percent']==pytest.approx(128*100/255)
    assert row['field_sources']['fan_pwm_percent']=='amdgpu_pwm' and row['power_scope']=='device_or_soc'
    assert before=={str(p):p.read_bytes() for p in device.rglob('*') if p.is_file()}


def test_amd_missing_driver_does_not_report_stale_sysfs_values(tmp_path):
    pci_device(tmp_path,'0000:05:00.0',driver=False)
    row=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path,captured_at=1000)['devices'][0]
    assert row['driver_available'] is False
    assert row['temperature_c'] is None and row['power_w'] is None and row['fan_rpm'] is None
    assert row['field_status']['temperature_c']=='driver_unavailable'


def test_amd_permission_failure_and_missing_fields_are_distinct(tmp_path):
    device=pci_device(tmp_path,'0000:05:00.0')
    (device/'hwmon/hwmon4/power1_average').unlink()
    def reader(path):
        if path.name=='temp1_input':raise PermissionError()
        return path.read_text()
    row=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path,captured_at=1000,read_text=reader)['devices'][0]
    assert row['temperature_c'] is None and row['field_status']['temperature_c']=='permission_denied'
    assert row['power_w'] is None and row['field_status']['power_w']=='missing'
    assert row['fan_rpm']==1700


def test_pci_attribute_read_failure_does_not_claim_successful_zero_scan(tmp_path):
    pci_device(tmp_path,'0000:05:00.0')
    def reader(path):
        if path.name=='vendor':raise PermissionError()
        return path.read_text()
    snap=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path,captured_at=1000,read_text=reader)
    assert snap['devices']==[] and not snap['scan_complete']
    assert snap['sources']['pci']['reason']=='permission_denied'


def test_excludes_audio_functions_and_does_not_guess_device_name(tmp_path):
    pci_device(tmp_path,'0000:05:00.1',cls='0x040300')
    gpu=pci_device(tmp_path,'0000:05:00.0')
    (gpu/'product_name').unlink()
    snap=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path,captured_at=1000)
    assert len(snap['devices'])==1 and snap['devices'][0]['name'] is None


def test_multiple_amd_hwmon_or_drm_indices_are_not_guessed(tmp_path):
    device=pci_device(tmp_path,'0000:05:00.0')
    (device/'drm/card8').mkdir()
    other=device/'hwmon/hwmon5';other.mkdir();(other/'name').write_text('amdgpu')
    row=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path,captured_at=1000)['devices'][0]
    assert row['native_index'] is None and row['temperature_c'] is None


@pytest.mark.parametrize('value',[True,float('nan'),float('inf'),-1,'N/A'])
def test_invalid_values_are_null_without_losing_identity(tmp_path,value):
    row=snapshot(tmp_path,[nvidia(power_w=value)])['devices'][0]
    assert row['power_w'] is None and row['gpu_key']=='nvidia:GPU-one'


def test_duplicate_native_indices_do_not_become_control_targets(tmp_path):
    snap=snapshot(tmp_path,[nvidia(0,'0000:01:00.0','GPU-a'),nvidia(0,'0000:02:00.0','GPU-b')])
    assert all(row['native_index']==0 for row in snap['devices'])
    assert all(row['control_index'] is None and row['index'] is None for row in snap['devices'])
    assert len({row['gpu_key'] for row in snap['devices']})==2


def test_power_fallback_does_not_hide_original_permission_error(tmp_path):
    pci_device(tmp_path,'0000:05:00.0')
    def reader(path):
        if path.name=='power1_average':raise PermissionError()
        return path.read_text()
    row=collect_gpu_snapshot(nvidia_metrics=[],pci_root=tmp_path,captured_at=1000,read_text=reader)['devices'][0]
    assert row['power_w'] is None and row['field_status']['power_w']=='permission_denied'


def test_compact_snapshot_preserves_views_order_binding_and_fits_64_gpu_channel(tmp_path):
    from agent.linux_gpu_telemetry import compact_snapshot, MAX_COMPACT_SNAPSHOT_BYTES
    rows=[nvidia(i,f'0000:{i:02x}:00.0',f'GPU-{i:08x}-0000-0000-0000-000000000000',
                 name='Fixture GPU',board_vendor='board',vbios_version='version',memory_vendor='maker') for i in range(64)]
    full=snapshot(tmp_path,rows);small=compact_snapshot(full)
    assert small['snapshot_id']==full['snapshot_id'] and len(small['devices'])==64
    assert 'sources' not in small and all('field_status' not in row for row in small['devices'])
    assert len(json.dumps(small,separators=(',',':'),ensure_ascii=False).encode())<=MAX_COMPACT_SNAPSHOT_BYTES<65536


def test_compact_bounds_unicode_display_fields_without_truncating_identity(tmp_path):
    from agent.linux_gpu_telemetry import compact_snapshot, MAX_COMPACT_SNAPSHOT_BYTES
    rows=[nvidia(i,f'0000:{i:02x}:00.0','GPU-'+str(i)+'x'*118,name='显卡'*70,
                 board_vendor='品牌'*70,vbios_version='版本'*70,memory_vendor='显存'*70) for i in range(64)]
    full=snapshot(tmp_path,rows);small=compact_snapshot(full)
    assert [row['gpu_key'] for row in small['devices']]==[row['gpu_key'] for row in full['devices']]
    assert all(len(row['name'].encode())<=48 for row in small['devices'])
    assert len(json.dumps(small,separators=(',',':'),ensure_ascii=False).encode())<=MAX_COMPACT_SNAPSHOT_BYTES


@pytest.mark.parametrize('change',[{'snapshot_id':'bad'},{'captured_at':float('nan')},{'scan_complete':None},{'devices':[{}]*65}])
def test_compact_rejects_unbound_or_overlimit_context(tmp_path,change):
    from agent.linux_gpu_telemetry import compact_snapshot
    full=snapshot(tmp_path,[nvidia()]);full.update(change)
    with pytest.raises(ValueError):compact_snapshot(full)
