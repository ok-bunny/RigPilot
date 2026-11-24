"""Process/cache/protocol boundaries; no driver library or live GPU access."""
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import threading

import pytest

from agent import nvidia_memory as memory

UUID = "GPU-11111111-2222-3333-4444-555555555555"
BDF = "00000000:01:00.0"


def selected(**changes):
    return [{"uuid": UUID, "pci_bus_id": BDF, "driver_version": "999.01",
             "vbios_version": "FE.ED.00.00.06", **changes}]


def payload(**changes):
    return {"schema_version": 1, "results": {BDF: {"uuid": UUID, "pci_bus_id": BDF,
            "memory_vendor": "Samsung", "memory_type": "GDDR7", **changes}}}


def complete(selection, system):
    return memory._normalize_result(payload(), selection, system)


@pytest.mark.parametrize("system,source", [("Linux", "nvml_private"), ("Windows", "nvapi")])
def test_driver_values_project_only_current_identity_and_source(system, source):
    result = memory._normalize_result(payload(), selected(), system)[BDF]
    assert result["memory_vendor"] == "Samsung" and result["memory_type"] == "GDDR7"
    assert result["memory_identity_source"] == source
    assert result["memory_identity_status"] == "complete"
    assert "raw_status" not in result


@pytest.mark.parametrize("change", [
    {"raw_status": 3}, {"memory_probe_status": "query_failed"},
    {"uuid": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
    {"pci_bus_id": "00000000:02:00.0"}, {"driver_version": "999.02"},
    {"output_layout_intact": False},
])
def test_failed_or_mismatched_payload_never_keeps_positive_labels(change):
    row = memory._normalize_result(payload(**change), selected(), "Linux")[BDF]
    assert row["memory_vendor"] is None and row["memory_type"] is None
    assert row["memory_identity_status"] == "unavailable"


def test_partial_unknown_values_are_not_made_up_or_completed_from_specs():
    partial = memory._normalize_result(payload(memory_type=None), selected(), "Windows")[BDF]
    assert partial["memory_identity_status"] == "partial"
    assert partial["memory_identity_unavailable_reason"] == "driver_reported_partial_identity"
    unknown = memory._normalize_result(payload(memory_vendor="123", memory_type="Unknown"), selected(), "Linux")[BDF]
    assert unknown["memory_identity_status"] == "unavailable"
    assert unknown["memory_identity_unavailable_reason"] == "driver_memory_identity_unknown"


def test_four_and_eight_digit_domains_do_not_create_distinct_devices():
    data = payload()
    data["results"]["0000:01:00.0"] = data["results"].pop(BDF)
    assert memory._normalize_result(data, selected(), "Linux")[BDF]["memory_vendor"] == "Samsung"
    data["results"][BDF] = dict(data["results"]["0000:01:00.0"])
    with pytest.raises(ValueError):
        memory._normalize_result(data, selected(), "Linux")


def test_cache_success_failure_retry_and_mutation_detachment():
    now, calls = [0.0], []
    def query(rows, system):
        calls.append(rows)
        if len(calls) == 2:
            return memory._unavailable(rows, system, "native_probe_timeout")
        return complete(rows, system)
    cache = memory.MemoryIdentityCache(query, lambda: now[0])
    first = cache.collect(selected(), "Linux"); first[BDF]["memory_vendor"] = "mutated"
    now[0] = 599
    assert cache.collect(selected(), "Linux")[BDF]["memory_vendor"] == "Samsung"
    assert len(calls) == 1
    now[0] = 600
    assert cache.collect(selected(), "Linux")[BDF]["memory_vendor"] is None
    now[0] = 629
    assert cache.collect(selected(), "Linux")[BDF]["memory_vendor"] is None
    now[0] = 630
    assert cache.collect(selected(), "Linux")[BDF]["memory_vendor"] == "Samsung"
    assert len(calls) == 3


@pytest.mark.parametrize("field,value", [("uuid", "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    ("pci_bus_id", "00000001:01:00.0"), ("driver_version", "999.02"),
    ("vbios_version", "FE.ED.00.00.07")])
def test_cache_identity_changes_trigger_new_query(field, value):
    calls = []
    def query(rows, system):
        calls.append(deepcopy(rows)); return memory._unavailable(rows, system, "native_probe_unavailable")
    cache = memory.MemoryIdentityCache(query, lambda: 0)
    cache.collect(selected(), "Linux"); cache.collect(selected(**{field: value}), "Linux")
    assert len(calls) == 2 and calls[1][0][field] == value


def test_cache_concurrency_coalesces_without_returning_other_identity():
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def query(rows, system):
        calls.append(rows)
        if len(calls) == 1:
            entered.set(); assert finish.wait(2)
        return memory._unavailable(rows, system, "native_probe_unavailable")
    cache = memory.MemoryIdentityCache(query, lambda: 0)
    thread = threading.Thread(target=lambda: cache.collect(selected(), "Linux"))
    thread.start(); assert entered.wait(2)
    changed = selected(pci_bus_id="00000000:02:00.0")
    pending = cache.collect(changed, "Linux")
    assert BDF not in pending and len(calls) == 1
    finish.set(); thread.join(2); assert not thread.is_alive()
    cache.collect(changed, "Linux"); assert len(calls) == 2


def test_source_and_frozen_worker_commands_are_fixed(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    source, cwd = memory._worker_command()
    assert source[1:] == ["-m", "agent.nvidia_memory", "--worker"]
    assert (Path(cwd) / "agent/nvidia_memory.py").is_file()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen, _ = memory._worker_command()
    assert frozen == [str(Path(sys.executable).absolute()), "--nvidia-memory-worker"]


class Process:
    pid = 987654
    def __init__(self, output, code=0, timeout=False):
        self.output, self.returncode, self.timeout = output, code, timeout
        self.killed, self.calls = False, []
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
    def communicate(self, *args, **kwargs):
        self.calls.append(kwargs["timeout"])
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("own-worker", kwargs["timeout"])
        return self.output, b""
    def poll(self):
        return None if self.timeout and not self.killed else self.returncode
    def kill(self):
        self.killed = True; self.returncode = -9


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_process_timeout_only_terminates_its_own_worker_and_keeps_deadline(monkeypatch, system):
    process = Process(b"", timeout=True); captured = {}
    monkeypatch.setattr(memory.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(memory.subprocess, "Popen", lambda argv, **kw: captured.update(argv=argv, **kw) or process)
    def killpg(pid, sig):
        assert pid == process.pid
        process.kill()
    monkeypatch.setattr(memory.os, "killpg", killpg, raising=False)
    result = memory._query(selected(), system)[BDF]
    assert result["memory_identity_unavailable_reason"] == "native_probe_timeout"
    assert process.killed and process.calls == [2.0, 0.25]
    assert captured["shell"] is False
    assert captured.get("start_new_session") is (True if system == "Linux" else None)


@pytest.mark.parametrize("output,code,reason", [
    (b"", -11, "native_probe_crashed"), (b"bad JSON", 0, "native_probe_unavailable"),
    (b"x" * (64 * 1024 + 1), 0, "native_probe_unavailable"),
], ids=["native_crash", "invalid_json", "oversized_stdout"])
def test_native_crash_bad_json_or_excess_output_is_unavailable(monkeypatch, output, code, reason):
    process = Process(output, code)
    monkeypatch.setattr(memory.subprocess, "Popen", lambda *a, **k: process)
    assert memory._query(selected(), "Linux")[BDF]["memory_identity_unavailable_reason"] == reason


def test_frozen_self_worker_keeps_its_loader_path_but_not_credentials_or_notify(monkeypatch):
    process = Process(json.dumps(payload()).encode()); captured = {}
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for key, value in {"LD_LIBRARY_PATH": "/tmp/_MEI-test", "LD_LIBRARY_PATH_ORIG": "/usr/lib/original",
                       "_PYI_PARENT_PROCESS_LEVEL": "1", "NOTIFY_SOCKET": "/run/notify",
                       "WATCHDOG_PID": "123", "RIGPILOT_AGENT_SECRET": "test-only-secret"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(memory.subprocess, "Popen", lambda argv, **kw: captured.update(argv=argv, **kw) or process)
    assert memory._query(selected(), "Linux")[BDF]["memory_vendor"] == "Samsung"
    assert captured["argv"][-1] == "--nvidia-memory-worker"
    assert captured["env"]["LD_LIBRARY_PATH"] == "/tmp/_MEI-test"
    assert captured["env"]["LD_LIBRARY_PATH_ORIG"] == "/usr/lib/original"
    assert captured["env"]["_PYI_PARENT_PROCESS_LEVEL"] == "1"
    assert not {"NOTIFY_SOCKET", "WATCHDOG_PID", "RIGPILOT_AGENT_SECRET"}.intersection(captured["env"])


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_worker_protocol_projects_backend_without_recursing_parent(monkeypatch, capsys, system):
    from agent import nvidia_memory_linux, nvidia_nvapi
    request = {"schema_version": 1, "system": system, "selected": selected()}
    monkeypatch.setattr(memory.platform, "system", lambda: system)
    monkeypatch.setattr(memory.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(nvidia_memory_linux, "collect_linux_memory", lambda rows: payload()["results"])
    monkeypatch.setattr(nvidia_nvapi, "collect_windows_memory", lambda rows: payload()["results"])
    monkeypatch.setattr(memory, "_query", lambda *a: pytest.fail("recursive parent query"))
    assert memory.worker_main() == 0
    assert json.loads(capsys.readouterr().out)["results"][BDF]["memory_vendor"] == "Samsung"


def test_invalid_worker_request_does_not_start_runtime_or_load_driver(tmp_path):
    command = [sys.executable, "-B", "-m", "agent.nvidia_memory", "--worker"]
    result = subprocess.run(command, input=b"invalid-json", capture_output=True, timeout=3,
                            cwd=Path(__file__).resolve().parents[2])
    assert result.returncode == 0 and json.loads(result.stdout) == {"schema_version": 1, "results": {}}
    assert list(tmp_path.iterdir()) == []
