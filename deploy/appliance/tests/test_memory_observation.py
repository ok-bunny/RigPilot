"""Memory observation regressions with synthetic samples/serial data; no host or VM sampling."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

MEMORY = Path(__file__).resolve().parents[1] / "memory"
TARGETS = [180.0, 240.0, 300.0]
MEMTOTAL = 1_000_000


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, MEMORY / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = load("memory_collector_under_test", "collect-memory.py")
runner = load("memory_runner_under_test", "measure-image.py")


def collect(monkeypatch, capsys, *, failed_inventory=None, invalid_field=None):
    events = []
    monkeypatch.setattr(sys, "argv", ["collect-memory.py", "--label", "mock-image"])
    monkeypatch.setattr(collector.os, "geteuid", lambda: 0)
    monkeypatch.setattr(collector.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(collector.platform, "release", lambda: "mock-kernel")
    monkeypatch.setattr(collector, "uptime", lambda: 400.0)

    def snapshot(target, lateness):
        events.append(("sample", target))
        memory = {"MemTotal": MEMTOTAL, "MemAvailable": 700_000}
        sample = {"target_uptime_seconds": target, "schedule_valid": True,
                  "actual_start_uptime_seconds": target + 0.5,
                  "capture_end_uptime_seconds": target + 1.0,
                  "services_end_uptime_seconds": target + 1.5,
                  "process_observations_valid": True, "services_observation_valid": True,
                  "meminfo_before_kib": dict(memory), "meminfo_after_kib": dict(memory)}
        if invalid_field and target == 240.0:
            sample[invalid_field] = False
        return sample

    def command(argv):
        key = ("package_inventory" if argv[0] == "dpkg-query" else
               "failed_units" if argv[1] == "list-units" else "timers")
        events.append(("inventory", key))
        if key == failed_inventory:
            if key == "package_inventory":
                raise OSError("mock package inventory unavailable")
            if key == "failed_units":
                raise subprocess.CalledProcessError(1, argv)
            raise subprocess.TimeoutExpired(argv, 30)
        return "mock " + key + "\n"

    monkeypatch.setattr(collector, "snapshot", snapshot)
    monkeypatch.setattr(collector, "command", command)
    if invalid_field:
        with pytest.raises(SystemExit) as exited:
            collector.main()
        assert exited.value.code == 2
    else:
        collector.main()
    return json.loads(capsys.readouterr().out), events


@pytest.mark.parametrize("failed_inventory", [None, "package_inventory", "failed_units", "timers"])
def test_inventory_completeness_does_not_invalidate_timed_samples(monkeypatch, capsys, failed_inventory):
    report, events = collect(monkeypatch, capsys, failed_inventory=failed_inventory)
    assert report["measurement_valid"] is True
    assert report["all_sample_schedules_valid"] is True
    assert report["inventory_complete"] is (failed_inventory is None)
    assert set(report["inventory_errors"]) == ({failed_inventory} if failed_inventory else set())
    for key in ("package_inventory", "failed_units", "timers"):
        if key == failed_inventory:
            assert key not in report and report["inventory_errors"][key]
        else:
            assert report[key] == "mock " + key + "\n"
    assert events == ([("sample", target) for target in TARGETS]
                      + [("inventory", key) for key in ("package_inventory", "failed_units", "timers")])
    ready = runner.parse_ready(b"architecture=x86_64\nuptime_seconds=100\nmemtotal_kib=1000000\n")
    assert runner.validate_report(report, ready, TARGETS, "mock-image") == []


@pytest.mark.parametrize("invalid_field", [
    "schedule_valid", "process_observations_valid", "services_observation_valid",
])
@pytest.mark.parametrize("failed_inventory", [None, "package_inventory"])
def test_core_observation_failure_still_exits_two_and_runner_rejects(
        monkeypatch, capsys, invalid_field, failed_inventory):
    report, _ = collect(monkeypatch, capsys, failed_inventory=failed_inventory,
                        invalid_field=invalid_field)
    assert report["measurement_valid"] is False
    assert report["inventory_complete"] is (failed_inventory is None)
    ready = {"architecture": "x86_64", "uptime_seconds": 100.0, "memtotal_kib": MEMTOTAL}
    assert "collector measurement_valid is not true" in runner.validate_report(
        report, ready, TARGETS, "mock-image")


@pytest.mark.parametrize("section, field, value, error", [
    pytest.param(None, "services_observation_valid", False, "invalid service observations", id="service-failure"),
    pytest.param("meminfo_before_kib", "MemAvailable", True, "invalid MemAvailable", id="boolean-available"),
    pytest.param("meminfo_after_kib", "MemAvailable", -1, "invalid MemAvailable", id="negative-available"),
    pytest.param("meminfo_after_kib", "MemAvailable", MEMTOTAL + 1, "invalid MemAvailable", id="available-exceeds-total"),
    pytest.param("meminfo_before_kib", "MemTotal", float(MEMTOTAL), "MemTotal differs", id="noninteger-total"),
    pytest.param(None, "actual_start_uptime_seconds", 239.0, "invalid capture timing", id="early-sample"),
    pytest.param(None, "actual_start_uptime_seconds", 243.0, "invalid capture timing", id="late-sample"),
    pytest.param(None, "actual_start_uptime_seconds", True, "invalid capture timing", id="boolean-uptime"),
    pytest.param(None, "capture_end_uptime_seconds", None, "invalid capture timing", id="missing-uptime"),
    pytest.param(None, "capture_end_uptime_seconds", float('nan'), "invalid capture timing", id="nonfinite-uptime"),
    pytest.param(None, "capture_end_uptime_seconds", 240.0, "invalid capture timing", id="capture-before-start"),
    pytest.param(None, "services_end_uptime_seconds", 240.5, "invalid capture timing", id="services-before-capture"),
])
def test_runner_rejects_invalid_sample_even_with_valid_summary(
    monkeypatch, capsys, section, field, value, error,
):
    report, _ = collect(monkeypatch, capsys)
    sample = report["snapshots"][1]
    target = sample if section is None else sample[section]
    target[field] = value
    assert report["measurement_valid"] is True
    ready = {"architecture": "x86_64", "uptime_seconds": 100.0, "memtotal_kib": MEMTOTAL}
    assert any(error in message for message in runner.validate_report(report, ready, TARGETS, "mock-image"))


def test_ready_frame_rejects_negative_guest_uptime():
    with pytest.raises(ValueError, match="preparation memory/uptime is invalid"):
        runner.parse_ready(b"architecture=x86_64\nuptime_seconds=-1\nmemtotal_kib=1000000\n")


def test_runner_preserves_inventory_errors_and_separate_metadata(monkeypatch, capsys, tmp_path):
    report, _ = collect(monkeypatch, capsys, failed_inventory="timers")
    image = tmp_path / "mock.raw"
    image.write_bytes(b"synthetic RAW fixture")
    qemu = tmp_path / "qemu-system-x86_64"
    qemu.write_text("never executed")
    qemu.chmod(0o755)
    frames = iter([
        b"architecture=x86_64\nuptime_seconds=100\nmemtotal_kib=1000000\n",
        base64.b64encode(json.dumps(report).encode()), b"", b"0",
    ])
    process = SimpleNamespace(pid=123, returncode=0, poll=lambda: 0)
    popen_calls = []

    def popen(argv, **kwargs):
        popen_calls.append(argv)
        return process

    class Serial:
        def __init__(self, *args):
            pass

        def send(self, value):
            pass

        def frame(self, begin, end):
            return next(frames)

    monkeypatch.setenv("RIGPILOTOS_TEST_PASSWORD", "mock-password")
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner, "connect_serial",
                        lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(runner, "Serial", Serial)
    monkeypatch.setattr(runner, "login", lambda *args: None)
    # macOS pytest roots can exceed the real Unix-socket path limit. Keep the
    # run directory short without weakening the runner's path validation.
    with TemporaryDirectory(prefix="rpm-", dir="/tmp") as parent:
        run_dir = Path(parent) / "r"
        args = SimpleNamespace(image=image, collector=MEMORY / "collect-memory.py",
                               qemu=qemu, run_dir=run_dir, label="mock-image",
                               sample_uptimes="180,240,300", timeout=30)
        assert runner.run(args) == 0
        output = json.loads(capsys.readouterr().out)
        metadata = json.loads((run_dir / "metadata.json").read_text())
        raw = json.loads((run_dir / "report.raw.json").read_text())
    assert len(popen_calls) == 1
    assert output["measurement_valid"] is True
    assert output["inventory_complete"] is False
    assert output["inventory_errors"] == report["inventory_errors"]
    assert metadata["measurement_passed"] is True
    assert metadata["inventory_complete"] is False
    assert output["runner_validation"]["passed"] is True
    assert output["runner_validation"]["full_boot_gate_evaluated"] is False
    assert raw == report


@pytest.mark.parametrize("timeout", [0, -1])
def test_nonpositive_timeout_fails_before_input_or_process_access(monkeypatch, timeout):
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("started QEMU"))
    args = SimpleNamespace(sample_uptimes="180,240,300", timeout=timeout)
    with pytest.raises(ValueError, match="timeout must be positive"):
        runner.run(args)
