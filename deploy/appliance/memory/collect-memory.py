#!/usr/bin/env python3
"""Read-only Linux memory/service sampler. No cache, swap, service or sysctl changes."""
from __future__ import annotations
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import time


def values(path: Path) -> dict[str, int]:
    result = {}
    for line in path.read_text().splitlines():
        if ':' not in line:
            continue
        key, rest = line.split(':', 1)
        words = rest.strip().split()
        if words and words[0].isdigit():
            result[key] = int(words[0])
    return result


def uptime() -> float:
    return float(Path('/proc/uptime').read_text().split()[0])


def memory_info() -> tuple[dict[str, int], dict[str, int]]:
    kib, counts = {}, {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, rest = line.split(':', 1)
        fields = rest.split()
        (kib if fields[1:] == ['kB'] else counts)[key] = int(fields[0])
    return kib, counts


def command(args: list[str]) -> str:
    return subprocess.run(args, text=True, capture_output=True, check=True,
                          env=dict(os.environ, LC_ALL='C'), timeout=30).stdout


def services() -> list[dict]:
    names = []
    for line in command(['systemctl','list-units','--all','--type=service','--no-legend','--no-pager','--plain']).splitlines():
        parts = line.split()
        if parts and parts[0].endswith('.service'):
            names.append(parts[0])
    if not names:
        return []
    properties = ('Id','LoadState','ActiveState','SubState','MainPID','MemoryCurrent','TasksCurrent','ControlGroup','UnitFileState','Result')
    argv = ['systemctl','show','--no-pager',*names]
    for prop in properties:
        argv.extend(['-p',prop])
    data = command(argv)
    result = []
    for block in data.strip().split('\n\n'):
        row = dict(line.split('=',1) for line in block.splitlines() if '=' in line)
        if row.get('Id'):
            result.append(row)
    return result


def snapshot(target: float | None, lateness: float) -> dict:
    started = uptime()
    before, counts_before = memory_info()
    processes = []
    missing = []
    no_mm = 0
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            stat_before = (proc/'stat').read_text()
            start_ticks = stat_before[stat_before.rfind(')')+2:].split()[19]
            status = values(proc/'status')
            if 'VmRSS' not in status:
                no_mm += 1
                continue
            rollup = values(proc/'smaps_rollup')
            if 'Pss' not in rollup or 'Rss' not in rollup:
                raise RuntimeError('missing smaps RSS/PSS')
            cgroup = (proc/'cgroup').read_text().strip()
            comm = (proc/'comm').read_text().strip()
            stat_after = (proc/'stat').read_text()
            if stat_after[stat_after.rfind(')')+2:].split()[19] != start_ticks:
                raise RuntimeError('PID reused during sample')
            processes.append({'pid':int(proc.name),'start_ticks':int(start_ticks),
                              'comm':comm,'cgroup':cgroup,
                              'rss_kib':rollup['Rss'],'pss_kib':rollup['Pss'],
                              'swap_pss_kib':rollup.get('SwapPss'),
                              'observer':int(proc.name)==os.getpid()})
        except (OSError,ValueError,IndexError,RuntimeError) as error:
            transient = isinstance(error, (FileNotFoundError, ProcessLookupError)) or str(error) == 'PID reused during sample'
            missing.append({'pid':int(proc.name),'reason':type(error).__name__,
                            'detail':str(error),'transient':transient})
    after, counts_after = memory_info()
    captured = uptime()
    service_error = None
    try:
        unit_rows = services()
    except (OSError, subprocess.SubprocessError) as error:
        unit_rows = []
        service_error = str(error)
    for unit in unit_rows:
        cg = unit.get('ControlGroup')
        members = [p for p in processes if cg and p['cgroup'] is not None and
                   any(line == '0::'+cg or line.startswith('0::'+cg+'/') for line in p['cgroup'].splitlines())]
        unit['observed_pids'] = [p['pid'] for p in members]
        unit['observed_pss_kib'] = sum(p['pss_kib'] for p in members) if members else None
        unit['observed_pss_excluding_observer_kib'] = sum(p['pss_kib'] for p in members if not p['observer']) if members else None
    return {'target_uptime_seconds':target,'actual_start_uptime_seconds':started,
            'capture_end_uptime_seconds':captured,'services_end_uptime_seconds':uptime(),
            'schedule_valid':target is None or 0 <= started-target <= lateness,
            'meminfo_before_kib':before,'meminfo_after_kib':after,
            'meminfo_before_page_counts':counts_before,'meminfo_after_page_counts':counts_after,
            'processes':sorted(processes,key=lambda r:r['pss_kib'],reverse=True),
            'process_pss_sum_excluding_observer_kib':sum(p['pss_kib'] for p in processes if not p['observer']),
            'process_rss_sum_not_total_ram_kib':sum(p['rss_kib'] for p in processes if not p['observer']),
            'unreadable_or_changed_processes':missing,'tasks_without_userspace_mm':no_mm,
            'process_observations_valid':not any(not p['transient'] for p in missing),
            'services_observation_valid':service_error is None,
            'services_error':service_error,'services':unit_rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label',required=True)
    parser.add_argument('--sample-uptimes',default='180,240,300')
    parser.add_argument('--max-lateness-seconds',type=float,default=2.0)
    parser.add_argument('--capture-now',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise RuntimeError('Root is required for complete smaps observations')
    targets=[float(v) for v in args.sample_uptimes.split(',')]
    if not targets or targets!=sorted(set(targets)) or any(not math.isfinite(t) or t<1 or t>3600 for t in targets):
        raise RuntimeError('Use increasing target uptimes within 1..3600 seconds')
    if not math.isfinite(args.max_lateness_seconds) or args.max_lateness_seconds < 0:
        raise RuntimeError('Maximum lateness must be finite and nonnegative')
    report={'schema':'rigpilotos.memory-observation.v1','label':args.label,
            'mode':'host-context-now' if args.capture_now else 'scheduled-target-image',
            'architecture':platform.machine(),'kernel':platform.release(),
            'captured_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'observer_pid':os.getpid(),'observer_effect_removed_from_memavailable':False,
            'cache_dropped':False,'swap_changed':False,'services_changed':False,
            'snapshots':[]}
    if args.capture_now:
        report['snapshots'].append(snapshot(None,args.max_lateness_seconds))
    else:
        for target in targets:
            while uptime()<target:
                time.sleep(min(0.5,max(0.001,target-uptime())))
            report['snapshots'].append(snapshot(target,args.max_lateness_seconds))
    # Collect potentially cache-affecting inventory only after the timed series.
    report['inventory_errors'] = {}
    for key, argv in {
        'package_inventory':['dpkg-query','-W','-f=${Package}\t${Version}\t${Status}\n'],
        'failed_units':['systemctl','list-units','--state=failed','--no-pager','--plain'],
        'timers':['systemctl','list-timers','--all','--no-pager','--plain'],
    }.items():
        try:
            report[key] = command(argv)
        except (OSError, subprocess.SubprocessError) as error:
            report['inventory_errors'][key] = str(error)
    report['all_sample_schedules_valid']=all(s['schedule_valid'] for s in report['snapshots'])
    report['inventory_complete'] = not report['inventory_errors']
    report['measurement_valid'] = report['all_sample_schedules_valid'] and all(
        s['process_observations_valid'] and s['services_observation_valid'] for s in report['snapshots'])
    print(json.dumps(report,ensure_ascii=False,separators=(',',':'),allow_nan=False),flush=True)
    if not report['measurement_valid']:
        raise SystemExit(2)


if __name__=='__main__':
    main()
