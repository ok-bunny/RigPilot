#!/usr/bin/env python3
"""Measure an x86_64 RAW appliance in a disposable, fixed 1024 MiB QEMU VM.

This runner never changes guest cache/swap/service policy. It uses a fresh run
directory, BIOS, TCG, two vCPUs and an isolated user-mode NIC. Only the QEMU
process created here can be terminated. Guest credentials are never recorded.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import time
import uuid

RAM_MIB = 1024
VCPUS = 2
MAX_BUFFER = 64 * 1024 * 1024
MAX_LATENESS_SECONDS = 2.0


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def file_signature(path: Path) -> dict:
    item = path.stat()
    return {'device': item.st_dev, 'inode': item.st_ino, 'size': item.st_size,
            'mtime_ns': item.st_mtime_ns, 'ctime_ns': item.st_ctime_ns}


def parse_targets(value: str) -> list[float]:
    try:
        targets = [float(part) for part in value.split(',')]
    except ValueError as error:
        raise ValueError('sample uptimes must be comma-separated numbers') from error
    if (not targets or any(not math.isfinite(t) or not 1 <= t <= 3600 for t in targets)
            or targets != sorted(set(targets))):
        raise ValueError('sample uptimes must be finite, increasing, within 1..3600')
    return targets


class Serial:
    """A receive buffer with exact line frames and persistent unread suffixes."""
    def __init__(self, stream, transcript, process, deadline: float, abort: bytes):
        self.stream = stream
        self.transcript = transcript
        self.process = process
        self.deadline = deadline
        self.abort = abort
        self.buffer = bytearray()
        self.record = True

    def send(self, value: bytes) -> None:
        self.stream.sendall(value)

    def _receive(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError('serial operation exceeded its deadline')
        try:
            chunk = self.stream.recv(65536)
        except socket.timeout:
            if self.process.poll() is not None:
                raise RuntimeError('QEMU exited before serial operation completed')
            return
        if not chunk:
            raise RuntimeError('QEMU serial connection closed before completion')
        if self.record:
            self.transcript.write(chunk)
            self.transcript.flush()
        # Preserve raw serial.log; normalized CRLF is used only for parsing.
        self.buffer.extend(chunk.replace(b'\r', b''))
        if len(self.buffer) > MAX_BUFFER:
            raise RuntimeError('serial receive buffer exceeded 64 MiB')

    def until_any(self, tokens: list[bytes]) -> tuple[bytes, bytes]:
        while True:
            found = [(self.buffer.find(token), token) for token in tokens]
            found = [(index, token) for index, token in found if index >= 0]
            if found:
                index, token = min(found, key=lambda item: item[0])
                end = index + len(token)
                data = bytes(self.buffer[:end])
                del self.buffer[:end]
                return token, data
            self._receive()

    def marker(self, marker: bytes) -> bytes:
        pattern = re.compile(rb'(?:^|\n)' + re.escape(marker) + rb'\n')
        abort_pattern = re.compile(rb'(?:^|\n)' + re.escape(self.abort) + rb'\n')
        while True:
            found = pattern.search(self.buffer)
            failed = abort_pattern.search(self.buffer)
            if failed and (not found or failed.start() < found.start()):
                raise RuntimeError('guest launcher aborted before completing its output')
            if found:
                before = bytes(self.buffer[:found.start()])
                del self.buffer[:found.end()]
                return before
            self._receive()

    def frame(self, begin: str, end: str) -> bytes:
        self.marker(begin.encode('ascii'))
        return self.marker(end.encode('ascii'))


def connect_serial(path: Path, process, deadline: float):
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('QEMU exited before opening its serial socket')
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stream.settimeout(1.0)
        try:
            stream.connect(str(path))
            return stream
        except OSError:
            stream.close()
            time.sleep(0.1)
    raise TimeoutError('QEMU serial socket did not become available')


def login(serial: Serial, password: str, nonce: str) -> None:
    serial.until_any([b'rigpilot login:'])
    serial.send(b'user\n')
    serial.until_any([b'Password:'])
    serial.record = False
    try:
        serial.send(password.encode('utf-8') + b'\n')
        result, _ = serial.until_any([b'user@rigpilot:', b'Login incorrect', b'rigpilot login:'])
        if result != b'user@rigpilot:':
            raise RuntimeError('serial login rejected')
        confirmation = nonce + '_LOGIN_CONFIRMED'
        serial.send(("printf '\\n" + confirmation + "\\n'\n").encode('ascii'))
        serial.marker(confirmation.encode('ascii'))
        serial.until_any([b'user@rigpilot:'])
    finally:
        serial.record = True
    authenticate_sudo(serial, password, nonce)
    serial.send(b'stty -echo\n')
    serial.until_any([b'user@rigpilot:'])


def authenticate_sudo(serial: Serial, password: str, nonce: str) -> None:
    prompt = (nonce + '_SUDO_PASSWORD:').encode('ascii')
    prompt_argument = "'" + nonce + "_SUDO_''PASSWORD:'"
    serial.send(('sudo -S -p ' + prompt_argument + ' -v\n').encode('ascii'))
    # Suppress this complete exchange. Even an unexpectedly echoed short
    # password must not leak, and global replacement would corrupt numeric data.
    serial.record = False
    try:
        result, _ = serial.until_any([prompt, b'user@rigpilot:', b'Sorry, try again.'])
        if result == prompt:
            serial.send(password.encode('utf-8') + b'\n')
            result, _ = serial.until_any([b'user@rigpilot:', b'Sorry, try again.', prompt])
        if result != b'user@rigpilot:':
            raise RuntimeError('sudo authentication rejected')
        confirmation = nonce + '_SUDO_CONFIRMED'
        serial.send(("sudo -n true && printf '\\n" + confirmation + "\\n'\n").encode('ascii'))
        serial.marker(confirmation.encode('ascii'))
        serial.until_any([b'user@rigpilot:'])
    finally:
        serial.record = True


def guest_launcher(source: bytes, nonce: str, label: str, targets: str) -> tuple[str, str]:
    work = '/run/rigpilot-memory-' + nonce
    encoded = base64.b64encode(source).decode('ascii')
    wrapped = '\n'.join(encoded[index:index + 76] for index in range(0, len(encoded), 76))
    script = [
        "set -eu",
        "umask 077",
        "finished=no",
        "trap 'if [ \"$finished\" != yes ]; then printf \"\\n" + nonce + "_ABORT\\n\"; fi' EXIT",
        'work=' + shlex.quote(work),
        'mkdir -m 0700 "$work"',
        'base64 -d >"$work/collector.py" <<\'' + nonce + "_SOURCE'",
        wrapped,
        nonce + '_SOURCE',
        'guest_arch=$(uname -m)',
        'read -r guest_uptime guest_idle </proc/uptime',
        'guest_memtotal=',
        'while read -r key value unit; do case "$key" in MemTotal:) guest_memtotal=$value;; esac; done </proc/meminfo',
        'test -n "$guest_memtotal"',
        'printf "\\n' + nonce + '_READY_BEGIN\\narchitecture=%s\\nuptime_seconds=%s\\nmemtotal_kib=%s\\n'
        + nonce + '_READY_END\\n" "$guest_arch" "$guest_uptime" "$guest_memtotal"',
        'if python3 "$work/collector.py" --label ' + shlex.quote(label)
        + ' --sample-uptimes ' + shlex.quote(targets)
        + ' --max-lateness-seconds ' + str(MAX_LATENESS_SECONDS)
        + ' >"$work/report.json" 2>"$work/collector.stderr"; then collector_status=0; else collector_status=$?; fi',
        'printf "\\n' + nonce + '_REPORT_BEGIN\\n"',
        'base64 "$work/report.json"',
        'printf "\\n' + nonce + '_REPORT_END\\n"',
        'printf "\\n' + nonce + '_ERROR_BEGIN\\n"',
        'base64 "$work/collector.stderr"',
        'printf "\\n' + nonce + '_ERROR_END\\n"',
        'printf "\\n' + nonce + '_STATUS_BEGIN\\n%s\\n' + nonce + '_STATUS_END\\n" "$collector_status"',
        'finished=yes',
    ]
    return ("sudo -n /bin/bash <<'" + nonce + "_LAUNCHER'\n"
            + '\n'.join(script) + '\n' + nonce + '_LAUNCHER\n'), work


def decode_frame(payload: bytes) -> bytes:
    return base64.b64decode(b''.join(payload.split()), validate=True)


def parse_ready(payload: bytes) -> dict:
    values = dict(line.split('=', 1) for line in payload.decode('ascii').strip().splitlines())
    ready = {'architecture': values['architecture'],
             'uptime_seconds': float(values['uptime_seconds']),
             'memtotal_kib': int(values['memtotal_kib'])}
    if (ready['architecture'] != 'x86_64'
            or not math.isfinite(ready['uptime_seconds'])
            or ready['uptime_seconds'] < 0
            or not 0 < ready['memtotal_kib'] <= RAM_MIB * 1024):
        raise ValueError('guest architecture or preparation memory/uptime is invalid')
    return ready


def validate_report(report: dict, ready: dict, targets: list[float], label: str) -> list[str]:
    """Validate timed observations; post-sample inventory has its own completeness."""
    errors = []
    if report.get('architecture') != 'x86_64':
        errors.append('collector architecture is not x86_64')
    if report.get('label') != label:
        errors.append('collector label does not match this run')
    if report.get('mode') != 'scheduled-target-image':
        errors.append('collector did not run in scheduled target-image mode')
    if report.get('measurement_valid') is not True:
        errors.append('collector measurement_valid is not true')
    if report.get('all_sample_schedules_valid') is not True:
        errors.append('one or more sample schedules are invalid')
    snapshots = report.get('snapshots')
    if not isinstance(snapshots, list) or len(snapshots) != len(targets):
        errors.append('snapshot count differs from requested schedule')
        return errors
    for index, (sample, target) in enumerate(zip(snapshots, targets)):
        if not isinstance(sample, dict):
            errors.append('snapshot %d is not an object' % index)
            continue
        if sample.get('target_uptime_seconds') != target or sample.get('schedule_valid') is not True:
            errors.append('snapshot %d did not satisfy its requested uptime' % index)
        timings = [sample.get(field) for field in (
            'actual_start_uptime_seconds', 'capture_end_uptime_seconds', 'services_end_uptime_seconds')]
        if (any(type(value) not in (int, float) or not math.isfinite(value) for value in timings)
                or not target <= timings[0] <= target + MAX_LATENESS_SECONDS
                or not timings[0] <= timings[1] <= timings[2]):
            errors.append('snapshot %d has invalid capture timing' % index)
        if sample.get('process_observations_valid') is not True:
            errors.append('snapshot %d has invalid process observations' % index)
        if sample.get('services_observation_valid') is not True:
            errors.append('snapshot %d has invalid service observations' % index)
        for field in ('meminfo_before_kib', 'meminfo_after_kib'):
            memory = sample.get(field, {})
            if not isinstance(memory, dict):
                errors.append('snapshot %d %s is not an object' % (index, field))
                continue
            total = memory.get('MemTotal')
            available = memory.get('MemAvailable')
            if type(total) is not int or total != ready['memtotal_kib']:
                errors.append('snapshot %d %s MemTotal differs from preparation' % (index, field))
            if (type(available) is not int or available < 0
                    or available > ready['memtotal_kib']):
                errors.append('snapshot %d %s has invalid MemAvailable' % (index, field))
    return errors


def drain_shutdown(serial: Serial, process, metadata: dict, timeout: float = 90.0) -> None:
    """Keep draining the serial socket while waiting only for our QEMU child."""
    started = time.monotonic()
    deadline = started + timeout
    closed = False
    metadata['shutdown_serial_eof'] = False
    metadata['shutdown_output_bytes'] = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if process.poll() is None:
                    raise TimeoutError('QEMU did not exit within %g seconds after poweroff' % timeout)
                return
            if closed:
                # EOF can precede process exit; it is not proof of clean shutdown.
                try:
                    process.wait(timeout=min(0.25, remaining))
                    return
                except subprocess.TimeoutExpired:
                    continue
            serial.stream.settimeout(min(1.0, remaining))
            try:
                chunk = serial.stream.recv(65536)
            except socket.timeout:
                if process.poll() is not None:
                    return
                continue
            except OSError as error:
                metadata['shutdown_serial_read_error'] = type(error).__name__ + ': ' + str(error)
                closed = True
                continue
            if not chunk:
                metadata['shutdown_serial_eof'] = True
                closed = True
                continue
            # Earlier buffered bytes were already logged by Serial._receive.
            # These are fresh raw bytes; no marker buffer is needed after poweroff.
            serial.transcript.write(chunk)
            serial.transcript.flush()
            metadata['shutdown_output_bytes'] += len(chunk)
    finally:
        metadata['shutdown_wait_seconds'] = round(time.monotonic() - started, 3)


def stop_owned_process(process, metadata: dict) -> None:
    if process.poll() is None:
        metadata['forced_qemu_termination'] = True
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
    metadata['qemu_returncode'] = process.returncode


def run(args) -> int:
    targets = parse_targets(args.sample_uptimes)
    if args.timeout <= 0:
        raise ValueError('--timeout must be positive')
    password = os.environ.get('RIGPILOTOS_TEST_PASSWORD', '')
    if not password or any(character in password for character in '\r\n\0'):
        raise ValueError('RIGPILOTOS_TEST_PASSWORD must be a nonempty single-line value')
    image = args.image.expanduser().resolve(strict=True)
    collector = args.collector.expanduser().resolve(strict=True)
    qemu = args.qemu.expanduser().resolve(strict=True)
    if not stat.S_ISREG(image.stat().st_mode):
        raise ValueError('image must be a regular RAW file, never a block device')
    if not collector.is_file() or not qemu.is_file() or not os.access(qemu, os.X_OK):
        raise ValueError('collector must be a file and QEMU must be an executable file')
    if qemu.name not in ('qemu-system-x86_64', 'qemu-system-x86_64.exe'):
        raise ValueError('--qemu must identify qemu-system-x86_64')
    with image.open('rb') as handle:
        if handle.read(8).startswith((b'QFI\xfb', b'vhdxfile', b'KDMV')):
            raise ValueError('image header identifies a container format; provide RAW')
    run_dir = args.run_dir.expanduser().resolve()
    socket_path = run_dir / 'serial.sock'
    if len(os.fsencode(socket_path)) > 100:
        raise ValueError('Unix socket path is too long; use a shorter new run directory')
    if ',' in str(image) or ',' in str(socket_path):
        raise ValueError('image and run-directory paths must not contain commas')
    source = collector.read_bytes()
    # Fail before starting QEMU on a syntax error in the supplied collector.
    compile(source, str(collector), 'exec')
    signature = file_signature(image)
    run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    nonce = 'RPM_' + uuid.uuid4().hex
    qemu_args = [
        str(qemu), '-machine', 'pc,accel=tcg', '-cpu', 'max',
        '-smp', str(VCPUS), '-m', str(RAM_MIB), '-display', 'none',
        '-monitor', 'none', '-no-reboot', '-snapshot',
        '-netdev', 'user,id=memory0,restrict=on,ipv6=off',
        '-device', 'virtio-net-pci,netdev=memory0',
        '-serial', 'unix:' + str(socket_path) + ',server=on,wait=off',
        '-drive', 'file=' + str(image) + ',if=virtio,format=raw',
    ]
    metadata = {
        'schema': 'rigpilotos.memory-run.v1', 'started_utc': utc_now(),
        'label': args.label, 'run_dir': str(run_dir), 'image': str(image),
        'input_stat_before': signature, 'collector': str(collector),
        'collector_sha256': hashlib.sha256(source).hexdigest(),
        'qemu_arguments': qemu_args, 'requested_uptimes_seconds': targets,
        'configuration': {'architecture': 'x86_64', 'machine': 'pc', 'accelerator': 'tcg',
                          'cpu': 'max', 'vcpus': VCPUS, 'ram_mib': RAM_MIB,
                          'firmware': 'BIOS', 'snapshot': True,
                          'network': 'user,restrict=on,ipv6=off'},
        'forced_qemu_termination': False, 'validation_errors': [],
    }
    report = {'schema': 'rigpilotos.memory-run-error.v1', 'measurement_valid': False}
    process = None
    serial = None
    stream = None
    collected = False
    validation_completed = False
    deadline = time.monotonic() + args.timeout
    (run_dir / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    try:
        with (run_dir / 'qemu-stderr.log').open('wb') as qemu_stderr, (run_dir / 'serial.log').open('wb') as transcript:
            try:
                environment = dict(os.environ)
                environment.pop('RIGPILOTOS_TEST_PASSWORD', None)
                environment['TMPDIR'] = str(run_dir)
                process = subprocess.Popen(qemu_args, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=qemu_stderr,
                                           cwd=run_dir, env=environment)
                metadata['qemu_pid'] = process.pid
                stream = connect_serial(socket_path, process, deadline)
                serial = Serial(stream, transcript, process, deadline, (nonce + '_ABORT').encode('ascii'))
                login(serial, password, nonce)
                launcher, guest_work = guest_launcher(source, nonce, args.label, args.sample_uptimes)
                metadata['guest_work_directory'] = guest_work
                serial.send(launcher.encode('utf-8'))
                ready = parse_ready(serial.frame(nonce + '_READY_BEGIN', nonce + '_READY_END'))
                metadata['guest_preparation'] = ready
                report_bytes = decode_frame(serial.frame(nonce + '_REPORT_BEGIN', nonce + '_REPORT_END'))
                (run_dir / 'report.raw.json').write_bytes(report_bytes)
                guest_errors = decode_frame(serial.frame(nonce + '_ERROR_BEGIN', nonce + '_ERROR_END'))
                (run_dir / 'collector-stderr.log').write_bytes(guest_errors)
                collector_status = int(serial.frame(nonce + '_STATUS_BEGIN', nonce + '_STATUS_END').strip())
                metadata['collector_exit_status'] = collector_status
                collected = True
                report = json.loads(report_bytes, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON value')))
                if not isinstance(report, dict):
                    raise ValueError('collector report is not a JSON object')
                metadata['inventory_complete'] = report.get('inventory_complete')
                metadata['validation_errors'].extend(validate_report(report, ready, targets, args.label))
                if collector_status != 0:
                    metadata['validation_errors'].append('collector exited with status %d' % collector_status)
                validation_completed = True
            except (Exception, KeyboardInterrupt) as error:
                metadata['validation_errors'].append(type(error).__name__ + ': ' + str(error))
            finally:
                if serial is not None and process is not None and process.poll() is None:
                    # Completed collection leaves us at the login shell. A
                    # timeout may leave a command foregrounded: interrupt only
                    # this VM's serial session before requesting clean poweroff.
                    try:
                        serial.deadline = time.monotonic() + 30
                        if not collected:
                            serial.send(b'\x03\n')
                        serial.until_any([b'user@rigpilot:'])
                        authenticate_sudo(serial, password, nonce)
                        serial.send(b'sudo -n systemctl poweroff --no-block\n')
                        metadata['graceful_poweroff_requested'] = True
                        drain_shutdown(serial, process, metadata)
                    except (Exception, KeyboardInterrupt) as error:
                        metadata['shutdown_error'] = type(error).__name__ + ': ' + str(error)
                if stream is not None:
                    stream.close()
                if process is not None:
                    stop_owned_process(process, metadata)
    except (Exception, KeyboardInterrupt) as error:
        metadata['validation_errors'].append(type(error).__name__ + ': ' + str(error))
    finally:
        try:
            after = file_signature(image)
            metadata['input_stat_after'] = after
            if after != signature:
                metadata['validation_errors'].append('source image stat changed during measurement')
        except OSError as error:
            metadata['validation_errors'].append('cannot recheck source image: ' + type(error).__name__)
        metadata['finished_utc'] = utc_now()
        if not validation_completed and not metadata['validation_errors']:
            metadata['validation_errors'].append('measurement did not complete report validation')
        metadata['measurement_passed'] = validation_completed and not metadata['validation_errors']
        metadata['shutdown_clean'] = bool(metadata.get('graceful_poweroff_requested')
                                          and not metadata['forced_qemu_termination']
                                          and metadata.get('qemu_returncode') == 0)
        metadata['full_boot_gate_evaluated'] = False
        if not isinstance(report, dict):
            report = {'schema': 'rigpilotos.memory-run-error.v1', 'measurement_valid': False}
        report['runner_validation'] = {
            'passed': metadata['measurement_passed'],
            'errors': metadata['validation_errors'],
            'shutdown_clean': metadata['shutdown_clean'],
            'full_boot_gate_evaluated': False,
            'metadata_file': str(run_dir / 'metadata.json'),
        }
        if not metadata['measurement_passed']:
            report['measurement_valid'] = False
        (run_dir / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        output = json.dumps(report, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        (run_dir / 'report.json').write_text(output + '\n', encoding='utf-8')
        print(output, flush=True)
    return 0 if metadata['measurement_passed'] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--collector', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--sample-uptimes', default='180,240,300')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as error:
        print(json.dumps({'schema': 'rigpilotos.memory-run-error.v1', 'measurement_valid': False,
                          'runner_validation': {'passed': False, 'errors': [type(error).__name__ + ': ' + str(error)]}},
                         ensure_ascii=False), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

