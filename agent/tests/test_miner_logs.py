from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psutil
import pytest

from agent import miner_logs as miner_logs_module
from agent.commands import Command, CommandExecutor
from agent.config import AgentConfig
from agent.miner_logs import (
    MAX_LINES,
    MAX_RESULT_BYTES,
    MAX_TEXT_CHARS,
    WINDOW_SECONDS,
    collect_managed_miner_logs,
)
from agent.process_identity import managed_argv_fingerprint


NOW = datetime(2026, 7, 15, 12, 0, 30, tzinfo=timezone.utc)


def test_retired_miners_are_not_managed_log_sources():
    for miner_type in ("lolminer", "forgeminer"):
        assert miner_type not in miner_logs_module._MINER_TYPES
        assert miner_type not in miner_logs_module._EXPECTED_EXECUTABLES


def config(tmp_path: Path, **overrides) -> AgentConfig:
    values = {
        "server_url": "https://controller.example.test",
        "state_file": tmp_path / "agent-state.json",
        "managed_mining_enabled": True,
        "managed_miner_runtime_dir": tmp_path / "runtime",
        "miner_status_file": tmp_path / "runtime" / "miner-status.json",
        "miner_packages_dir": tmp_path / "packages",
    }
    values.update(overrides)
    return AgentConfig(**values)


def write_status(cfg: AgentConfig, miner_type: str = "xmrig") -> None:
    assert cfg.managed_miner_runtime_dir is not None
    cfg.managed_miner_runtime_dir.mkdir(parents=True, exist_ok=True)
    assert cfg.miner_status_file is not None
    cfg.miner_status_file.write_text(
        json.dumps({"miner_type": miner_type, "state": "stopped"}),
        encoding="utf-8",
    )


def make_recent(path: Path) -> None:
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))


def write_process_record(
    cfg: AgentConfig, executable: Path, *, miner_type: str = "xmrig"
) -> None:
    assert cfg.managed_miner_runtime_dir is not None
    cfg.managed_miner_runtime_dir.mkdir(parents=True, exist_ok=True)
    (cfg.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 123,
                "executable": str(executable.resolve()),
                "create_time": 1000.25,
                "miner_type": miner_type,
                "flight_sheet_id": "sheet-1",
                "started_at": "2026-07-15T11:59:00+00:00",
            }
        ),
        encoding="utf-8",
    )


class FakeProcess:
    def __init__(
        self,
        executable: Path,
        *,
        running: bool = True,
        created: float = 1000.25,
        argv: list[str] | None = None,
    ):
        self.executable = executable
        self.running = running
        self.created = created
        self.argv = list(argv) if argv is not None else [str(executable)]

    def is_running(self):
        return self.running

    def status(self):
        return "running" if self.running else "stopped"

    def create_time(self):
        return self.created

    def exe(self):
        return str(self.executable)

    def cmdline(self):
        return list(self.argv)


class RestartedProcess(FakeProcess):
    def __init__(
        self,
        executable: Path,
        argv: list[str],
        *,
        pid: int = 456,
        created: float = 1002.25,
    ):
        super().__init__(executable, created=created)
        self.pid = pid
        self.argv = argv

    def name(self):
        return self.executable.name

    def cmdline(self):
        return list(self.argv)


def test_recent_snapshot_uses_only_fixed_runtime_logs_and_reports_identity(tmp_path: Path):
    cfg = config(tmp_path)
    executable = cfg.miner_packages_dir / "xmrig-6.26.0" / "xmrig.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    write_status(cfg)
    write_process_record(cfg, executable)
    runtime = cfg.managed_miner_runtime_dir
    assert runtime is not None
    miner_log = runtime / "xmrig.log"
    miner_log.write_text(
        "2026-07-15T11:58:00Z old line\n"
        "2026-07-15T12:00:05Z accepted 1000 H/s\n"
        "continuation: share accepted\n",
        encoding="utf-8",
    )
    launcher_log = runtime / "managed-miner-launcher.log"
    launcher_log.write_text("2026-07-15T12:00:20Z miner ready\n", encoding="utf-8")
    make_recent(miner_log)
    make_recent(launcher_log)

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda pid: FakeProcess(executable),
    )

    assert result["captured_at"] == "2026-07-15T12:00:30+00:00"
    assert result["window_seconds"] == WINDOW_SECONDS == 60
    assert result["miner_type"] == "xmrig"
    assert result["running"] is True
    assert [entry["source"] for entry in result["logs"]] == ["miner", "launcher"]
    text = "\n".join(
        line for entry in result["logs"] for line in entry["lines"]
    )
    assert "old line" not in text
    assert "accepted 1000 H/s" in text
    assert "continuation: share accepted" in text
    assert "miner ready" in text
    assert str(runtime) not in json.dumps(result)


def test_current_and_v2_process_records_are_accepted_for_running_logs(tmp_path: Path):
    cfg = config(tmp_path)
    executable = cfg.miner_packages_dir / "srbminer" / "SRBMiner-MULTI.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    write_status(cfg, "srbminer")
    write_process_record(cfg, executable, miner_type="srbminer")
    assert cfg.managed_miner_runtime_dir is not None
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    value = json.loads(state_path.read_text(encoding="utf-8"))
    # 0.5.6+ version-one records added the restart-survival proof.
    value["survives_agent_restart"] = True
    state_path.write_text(json.dumps(value), encoding="utf-8")
    current = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda pid: FakeProcess(executable),
    )
    assert current["running"] is True

    argv = [str(executable), "--algorithm", "randomx", "--api-port", "23456"]
    value.update(
        {
            "version": 2,
            "argv_sha256": managed_argv_fingerprint(argv, executable=executable),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(value), encoding="utf-8")
    v2 = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda pid: FakeProcess(executable, argv=argv),
    )
    assert v2["running"] is True


def test_wsl_v3_logs_verify_wrapper_and_keep_fixed_host_log_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = config(tmp_path)
    assert cfg.miner_packages_dir is not None
    assert cfg.managed_miner_runtime_dir is not None
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"trusted-wrapper")
    target = cfg.miner_packages_dir / "pearlminer" / "miner-cuda13"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"linux-miner")
    write_status(cfg, "pearlminer")
    argv = [
        str(wrapper.resolve()),
        "--exec",
        "/mnt/c/rigpilot/miner-cuda13",
        "--pool",
        "pool.example.test:3360",
    ]
    record = {
        "version": 3,
        "pid": 123,
        "executable": str(wrapper.resolve()),
        "create_time": 1000.25,
        "miner_type": "pearlminer",
        "flight_sheet_id": "sheet-1",
        "started_at": "2026-07-15T11:59:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": managed_argv_fingerprint(argv, executable=wrapper),
        "api_port": 23456,
        "execution_mode": "wsl",
        "target_executable": str(target.resolve()),
    }
    (cfg.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    log = cfg.managed_miner_runtime_dir / "pearlminer.log"
    log.write_text(
        "2026-07-15T12:00:20Z WSL miner accepted proof\n",
        encoding="utf-8",
    )
    make_recent(log)
    monkeypatch.setattr(
        "agent.miner_logs.wsl_executable_path",
        lambda: wrapper.resolve(),
    )

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda _pid: FakeProcess(wrapper.resolve(), argv=argv),
        process_iter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("WSL logs must not scan replacement wrappers")
        ),
    )

    assert result["running"] is True
    assert result["miner_type"] == "pearlminer"
    assert result["logs"] == [
        {
            "source": "miner",
            "lines": ["2026-07-15T12:00:20Z WSL miner accepted proof"],
            "truncated": False,
        }
    ]

    changed = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda _pid: FakeProcess(
            wrapper.resolve(), argv=[*argv, "--different-invocation"]
        ),
        process_iter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("WSL argv mismatch must not scan replacement wrappers")
        ),
    )
    assert changed["running"] is False


def test_wsl_v3_missing_wrapper_never_rebinds_but_still_reads_fixed_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = config(tmp_path)
    assert cfg.miner_packages_dir is not None
    assert cfg.managed_miner_runtime_dir is not None
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"trusted-wrapper")
    target = cfg.miner_packages_dir / "pearlminer" / "miner-cuda12"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"linux-miner")
    write_status(cfg, "pearlminer")
    argv = [str(wrapper.resolve()), "--exec", "/mnt/c/rigpilot/miner-cuda12"]
    (cfg.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        json.dumps(
            {
                "version": 3,
                "pid": 123,
                "executable": str(wrapper.resolve()),
                "create_time": 1000.25,
                "miner_type": "pearlminer",
                "flight_sheet_id": "sheet-1",
                "started_at": "2026-07-15T11:59:00+00:00",
                "survives_agent_restart": True,
                "argv_sha256": managed_argv_fingerprint(argv, executable=wrapper),
                "api_port": 23456,
                "execution_mode": "wsl",
                "target_executable": str(target.resolve()),
            }
        ),
        encoding="utf-8",
    )
    log = cfg.managed_miner_runtime_dir / "pearlminer.log"
    log.write_text(
        "2026-07-15T12:00:20Z final WSL line\n",
        encoding="utf-8",
    )
    make_recent(log)
    monkeypatch.setattr(
        "agent.miner_logs.wsl_executable_path",
        lambda: wrapper.resolve(),
    )

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid)),
        process_iter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("WSL logs must not guess a replacement wrapper")
        ),
    )

    assert result["running"] is False
    assert result["logs"][0]["lines"] == [
        "2026-07-15T12:00:20Z final WSL line"
    ]


def test_wsl_v5_logs_use_cached_guest_when_windows_source_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cfg = config(tmp_path)
    assert cfg.miner_packages_dir is not None
    assert cfg.managed_miner_runtime_dir is not None
    wrapper = tmp_path / "System32" / "wsl.exe"
    wrapper.parent.mkdir()
    wrapper.write_bytes(b"trusted-wrapper")
    source = cfg.miner_packages_dir / "pearlminer" / "miner-cuda13"
    source.parent.mkdir(parents=True)
    key = "f" * 64
    guest = f"/home/user/.rigpilot/miner-cache-v1/{key}/miner-cuda13"
    write_status(cfg, "pearlminer")
    record = {
        "version": 5,
        "pid": 123,
        "executable": str(wrapper.resolve()),
        "create_time": 1000.25,
        "miner_type": "pearlminer",
        "flight_sheet_id": "sheet-cache",
        "started_at": "2026-07-15T11:59:00+00:00",
        "survives_agent_restart": True,
        "argv_sha256": "a" * 64,
        "api_port": 23456,
        "execution_mode": "wsl",
        "target_executable": str(source),
        "wsl_token": "b" * 64,
        "wsl_guest_executable": guest,
        "wsl_identity": {
            "windows_session_id": 7,
            "windows_user_sid": "S-1-5-21-1000",
            "distribution": "Ubuntu-22.04",
            "linux_uid": 1000,
        },
        "wsl_cache_key": key,
    }
    (cfg.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    log = cfg.managed_miner_runtime_dir / "pearlminer.log"
    log.write_text(
        "2026-07-15T12:00:20Z cached WSL proof accepted\n",
        encoding="utf-8",
    )
    make_recent(log)
    monkeypatch.setattr(
        "agent.miner_logs.wsl_executable_path", lambda: wrapper.resolve()
    )
    monkeypatch.setattr(
        "agent.miner_logs.query_wsl_guest", lambda *_args, **_kwargs: True
    )

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda _pid: (_ for _ in ()).throw(
            AssertionError("live v5 guest must not require a host wrapper PID")
        ),
        process_iter_factory=lambda: (_ for _ in ()).throw(
            AssertionError("WSL logs must not scan replacement wrappers")
        ),
    )

    assert result["running"] is True
    assert result["logs"][0]["lines"] == [
        "2026-07-15T12:00:20Z cached WSL proof accepted"
    ]


def test_v2_live_recorded_pid_with_changed_argv_is_not_reported_running(
    tmp_path: Path,
):
    cfg = config(tmp_path)
    executable = cfg.miner_packages_dir / "xmrig" / "xmrig.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    write_status(cfg, "xmrig")
    write_process_record(cfg, executable, miner_type="xmrig")
    assert cfg.managed_miner_runtime_dir is not None
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable), "--url", "pool-a", "--api-port", "23456"]
    record = json.loads(state_path.read_text(encoding="utf-8"))
    record.update(
        {
            "version": 2,
            "survives_agent_restart": True,
            "argv_sha256": managed_argv_fingerprint(
                expected_argv, executable=executable
            ),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    changed = RestartedProcess(
        executable,
        [str(executable), "--url", "pool-b", "--api-port", "23456"],
        pid=123,
        created=1000.25,
    )

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda _pid: changed,
        process_iter_factory=lambda: [changed],
    )

    assert result["running"] is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == record


def test_linux_pearlminer_scrubbed_argv_still_reports_running_logs(
    tmp_path: Path,
):
    cfg = config(tmp_path)
    executable = cfg.miner_packages_dir / "pearlminer" / "miner-cuda13"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    write_status(cfg, "pearlminer")
    write_process_record(cfg, executable, miner_type="pearlminer")
    assert cfg.managed_miner_runtime_dir is not None
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [
        str(executable),
        "--pool",
        "pool.example.test:3360",
        "--wallet",
        "public-wallet",
    ]
    record = json.loads(state_path.read_text(encoding="utf-8"))
    record.update(
        {
            "version": 2,
            "survives_agent_restart": True,
            "argv_sha256": managed_argv_fingerprint(
                expected_argv,
                executable=executable,
            ),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(record), encoding="utf-8")
    scrubbed = FakeProcess(
        executable,
        argv=["miner-cuda13"],
    )

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda _pid: scrubbed,
        process_iter_factory=lambda: [scrubbed],
    )

    assert result["running"] is True
    assert json.loads(state_path.read_text(encoding="utf-8")) == record


def test_logs_rebind_exact_srbminer_watchdog_before_next_heartbeat(tmp_path: Path):
    cfg = config(tmp_path)
    executable = cfg.miner_packages_dir / "srbminer" / "SRBMiner-MULTI.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    write_status(cfg, "srbminer")
    write_process_record(cfg, executable, miner_type="srbminer")
    assert cfg.managed_miner_runtime_dir is not None
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    argv = [str(executable), "--algorithm", "randomx", "--api-port", "23456"]
    value = json.loads(state_path.read_text(encoding="utf-8"))
    value.update(
        {
            "version": 2,
            "survives_agent_restart": True,
            "argv_sha256": managed_argv_fingerprint(
                argv, executable=executable
            ),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(value), encoding="utf-8")
    log = cfg.managed_miner_runtime_dir / "srbminer.log"
    recent_label = datetime.fromtimestamp(NOW.timestamp() - 10).strftime("%H:%M:%S")
    log.write_text(f"[{recent_label}] accepted share\n", encoding="utf-8")
    make_recent(log)
    replacement = RestartedProcess(executable, argv)

    def missing_original(_pid: int):
        raise psutil.NoSuchProcess(_pid)

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=missing_original,
        process_iter_factory=lambda: [replacement],
    )

    rebound = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["running"] is True
    assert result["logs"][0]["lines"] == [f"[{recent_label}] accepted share"]
    assert rebound["pid"] == replacement.pid
    assert rebound["create_time"] == replacement.created


def test_logs_do_not_rebind_srbminer_with_different_arguments(tmp_path: Path):
    cfg = config(tmp_path)
    executable = cfg.miner_packages_dir / "srbminer" / "SRBMiner-MULTI.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    write_status(cfg, "srbminer")
    write_process_record(cfg, executable, miner_type="srbminer")
    assert cfg.managed_miner_runtime_dir is not None
    state_path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    expected_argv = [str(executable), "--algorithm", "randomx", "--api-port", "23456"]
    value = json.loads(state_path.read_text(encoding="utf-8"))
    value.update(
        {
            "version": 2,
            "survives_agent_restart": True,
            "argv_sha256": managed_argv_fingerprint(
                expected_argv, executable=executable
            ),
            "api_port": 23456,
        }
    )
    state_path.write_text(json.dumps(value), encoding="utf-8")
    original = state_path.read_text(encoding="utf-8")
    unrelated = RestartedProcess(
        executable,
        [str(executable), "--algorithm", "pearlhash", "--api-port", "23456"],
    )

    def missing_original(_pid: int):
        raise psutil.NoSuchProcess(_pid)

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=missing_original,
        process_iter_factory=lambda: [unrelated],
    )

    assert result["running"] is False
    assert state_path.read_text(encoding="utf-8") == original


def test_independent_logs_fall_back_to_v4_active_sheet_without_manifest(
    tmp_path: Path,
):
    flight_path = tmp_path / "flight.json"
    cfg = config(tmp_path, flight_sheet_file=flight_path)
    runtime = cfg.managed_miner_runtime_dir
    assert runtime is not None
    runtime.mkdir()
    flight_path.write_text(
        json.dumps(
            {
                "version": 4,
                "secondary_mode": "independent",
                "flight_sheet_id": "sheet-1",
                "name": "dual",
                "worker_name": "rig-1",
                "applied_at": "2026-07-15T11:59:00+00:00",
                "primary": {"miner_type": "xmrig"},
                "secondary": {"miner_type": "pearlminer"},
                "gpu_oc": None,
            }
        ),
        encoding="utf-8",
    )
    processes: dict[int, FakeProcess] = {}
    for role, miner_type, executable_name, pid in (
        ("primary", "xmrig", "xmrig.exe", 501),
        ("secondary", "pearlminer", "miner.exe", 502),
    ):
        role_runtime = runtime / role
        role_runtime.mkdir()
        executable = cfg.miner_packages_dir / role / executable_name
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"exe")
        (role_runtime / "miner-status.json").write_text(
            json.dumps({"miner_type": miner_type, "state": "running"}),
            encoding="utf-8",
        )
        (role_runtime / "managed-miner-process.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": pid,
                    "executable": str(executable.resolve()),
                    "create_time": 3000.0 + pid,
                    "miner_type": miner_type,
                    "flight_sheet_id": "sheet-1",
                    "started_at": "2026-07-15T11:59:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        log_path = role_runtime / f"{miner_type}.log"
        log_path.write_text(
            "2026-07-15T12:00:20Z role is mining\n",
            encoding="utf-8",
        )
        make_recent(log_path)
        processes[pid] = FakeProcess(
            executable.resolve(),
            created=3000.0 + pid,
        )

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=processes.__getitem__,
    )

    assert not (runtime / "independent-miners.json").exists()
    assert result["running"] is True
    assert result["running_roles"] == ["primary", "secondary"]
    assert {entry["role"] for entry in result["logs"]} == {
        "primary",
        "secondary",
    }


def test_independent_logs_keep_healthy_role_when_other_runtime_is_missing(
    tmp_path: Path,
):
    flight_path = tmp_path / "flight.json"
    cfg = config(tmp_path, flight_sheet_file=flight_path)
    runtime = cfg.managed_miner_runtime_dir
    assert runtime is not None
    runtime.mkdir()
    flight_path.write_text(
        json.dumps(
            {
                "version": 4,
                "secondary_mode": "independent",
                "flight_sheet_id": "sheet-1",
                "name": "dual",
                "worker_name": "rig-1",
                "applied_at": "2026-07-15T11:59:00+00:00",
                "primary": {"miner_type": "xmrig"},
                "secondary": {"miner_type": "pearlminer"},
                "gpu_oc": None,
            }
        ),
        encoding="utf-8",
    )
    primary_runtime = runtime / "primary"
    primary_runtime.mkdir()
    executable = cfg.miner_packages_dir / "primary" / "xmrig.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    (primary_runtime / "miner-status.json").write_text(
        json.dumps({"miner_type": "xmrig", "state": "running"}),
        encoding="utf-8",
    )
    (primary_runtime / "managed-miner-process.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 501,
                "executable": str(executable.resolve()),
                "create_time": 3501.0,
                "miner_type": "xmrig",
                "flight_sheet_id": "sheet-1",
                "started_at": "2026-07-15T11:59:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    log_path = primary_runtime / "xmrig.log"
    log_path.write_text(
        "2026-07-15T12:00:20Z primary remains healthy\n",
        encoding="utf-8",
    )
    make_recent(log_path)

    result = collect_managed_miner_logs(
        cfg,
        now=NOW,
        process_factory=lambda _pid: FakeProcess(
            executable.resolve(), created=3501.0
        ),
    )

    assert result["running"] is False
    assert result["running_roles"] == ["primary"]
    assert result["degraded"] is True
    assert "secondary" in result["role_errors"]
    assert {
        (entry["role"], line)
        for entry in result["logs"]
        for line in entry["lines"]
    } == {("primary", "2026-07-15T12:00:20Z primary remains healthy")}


def test_stopped_missing_and_malformed_state_never_crash(tmp_path: Path):
    cfg = config(tmp_path)
    empty = collect_managed_miner_logs(cfg, now=NOW)
    assert empty == {
        "captured_at": "2026-07-15T12:00:30+00:00",
        "window_seconds": 60,
        "miner_type": None,
        "running": False,
        "logs": [],
    }

    write_status(cfg, "srbminer")
    assert cfg.managed_miner_runtime_dir is not None
    (cfg.managed_miner_runtime_dir / "managed-miner-process.json").write_text(
        "not-json", encoding="utf-8"
    )
    log = cfg.managed_miner_runtime_dir / "srbminer.log"
    log.write_text("SRBMiner stopped normally\n", encoding="utf-8")
    make_recent(log)

    stopped = collect_managed_miner_logs(cfg, now=NOW)
    assert stopped["miner_type"] == "srbminer"
    assert stopped["running"] is False
    assert stopped["logs"][0]["lines"] == ["SRBMiner stopped normally"]


@pytest.mark.parametrize("secret_line, secret", [(line, "authentication secret") for line in (
    '--wallet "node authentication secret"',
    'Wallet: node authentication secret',
    'wallet_address=node authentication secret',
    'password="node authentication secret" accepted',
    "api_key='node authentication secret' accepted",
    '{"password": "node authentication secret"}',
    '{"wallet_address": "node authentication secret"}',
    r'password="node \" authentication secret" accepted',
    r'--password "node \" authentication secret" accepted',
    'password="node authentication secret',
    'password="node authentication secret' + "\\",
    "--password 'node authentication secret" + "\\",
)] + [
    ("Authorization: Basic c3ludGhldGljOnNlY3JldA==", "c3ludGhldGljOnNlY3JldA=="),
    ("Authorization: Basic dTpz", "dTpz"),
    ("socks5://:synthetic-secret@proxy.invalid:1080", "synthetic-secret"),
    ("https://user@alias:first@synthetic-secret@proxy.invalid/path", "synthetic-secret"),
])
def test_secret_markers_are_redacted_from_log_snapshot(tmp_path: Path, secret_line, secret):
    cfg = config(tmp_path)
    write_status(cfg, "srbminer")
    log = cfg.managed_miner_runtime_dir / "srbminer.log"
    log.write_text("2026-07-15T12:00:20Z " + secret_line + "\n", encoding="utf-8")
    make_recent(log)
    result = collect_managed_miner_logs(cfg, now=NOW)
    assert result["logs"]
    encoded = json.dumps(result)
    assert secret not in encoded
    assert "[REDACTED]" in encoded


@pytest.mark.parametrize("independent", [False, True])
def test_json_byte_trim_preserves_snapshot_metadata_and_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    independent: bool,
):
    monkeypatch.setattr(miner_logs_module, "MAX_RESULT_BYTES", 1200)
    cfg = config(tmp_path)
    write_status(cfg)
    if independent:
        (cfg.managed_miner_runtime_dir / "independent-miners.json").write_text(
            json.dumps({"version": 1, "mode": "independent", "roles": ["primary", "secondary"]})
        )
        targets = []
        for role, miner_type in (("primary", "xmrig"), ("secondary", "srbminer")):
            runtime = cfg.managed_miner_runtime_dir / role
            role_cfg = config(
                tmp_path,
                managed_miner_runtime_dir=runtime,
                miner_status_file=runtime / "miner-status.json",
            )
            write_status(role_cfg, miner_type)
            targets.append((runtime, miner_type))
    else:
        targets = [(cfg.managed_miner_runtime_dir, "xmrig")]
    for runtime, miner_type in targets:
        log = runtime / f"{miner_type}.log"
        log.write_text(
            "".join(
                f"2026-07-15T12:00:20Z entry-{index} " + "矿" * 30
                + " password=must-not-leak\n"
                for index in range(12)
            ),
            encoding="utf-8",
        )
        make_recent(log)

    result = collect_managed_miner_logs(cfg, now=NOW)

    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= 1200
    assert b"must-not-leak" not in encoded
    assert result["captured_at"] == NOW.isoformat(timespec="seconds")
    assert result["window_seconds"] == WINDOW_SECONDS
    assert result["running"] is False
    assert result["logs"] and any(item["truncated"] for item in result["logs"])
    assert any("矿" in line for item in result["logs"] for line in item["lines"])
    if independent:
        assert result["miner_type"] == "xmrig+srbminer"
        assert result["running_roles"] == []
        assert result["role_errors"] == {}
        assert result["degraded"] is False
        assert all(item["role"] in {"primary", "secondary"} for item in result["logs"])
    else:
        assert result["miner_type"] == "xmrig"
        assert set(result) == {"captured_at", "window_seconds", "miner_type", "running", "logs"}


def test_output_is_bounded_utf8_safe_and_redacts_credentials(tmp_path: Path):
    cfg = config(tmp_path)
    write_status(cfg)
    assert cfg.managed_miner_runtime_dir is not None
    log = cfg.managed_miner_runtime_dir / "xmrig.log"
    credential_line = (
        b"2026-07-15T12:00:20Z Bearer abcdefghijklmnopqrstuvwxyz "
        b"api_key=very-private https://user:pass@example.test/path?access_token=hidden "
        b"bad=\xff\n"
    )
    many_lines = b"".join(
        f"2026-07-15T12:00:20Z line-{index} ".encode() + b"x" * 220 + b"\n"
        for index in range(700)
    )
    log.write_bytes(credential_line + many_lines)
    make_recent(log)

    result = collect_managed_miner_logs(cfg, now=NOW)
    lines = [line for entry in result["logs"] for line in entry["lines"]]
    text = "\n".join(lines)

    assert len(lines) <= MAX_LINES
    assert sum(len(line) + 1 for line in lines) <= MAX_TEXT_CHARS
    assert len(json.dumps(result, ensure_ascii=True).encode("utf-8")) <= MAX_RESULT_BYTES
    assert result["logs"][0]["truncated"] is True
    # The invalid byte is decoded safely if that line remains in the bounded tail.
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "very-private" not in text
    assert "user:pass" not in text
    assert "access_token=hidden" not in text


def test_invalid_utf8_and_secret_redaction_are_preserved_in_small_snapshot(tmp_path: Path):
    cfg = config(tmp_path)
    write_status(cfg)
    assert cfg.managed_miner_runtime_dir is not None
    log = cfg.managed_miner_runtime_dir / "xmrig.log"
    log.write_bytes(
        b"2026-07-15T12:00:20Z bad=\xff Bearer abcdefghijk token-ok "
        b"password=hunter2 https://user:pass@example.test/?api_key=secret\n"
    )
    make_recent(log)

    text = collect_managed_miner_logs(cfg, now=NOW)["logs"][0]["lines"][0]

    assert "\ufffd" in text
    assert "Bearer [REDACTED]" in text
    assert "password=[REDACTED]" in text
    assert "user:pass" not in text
    assert "api_key=secret" not in text
    assert text.count("[REDACTED]") >= 4


def test_cli_secret_arguments_are_redacted(tmp_path: Path):
    cfg = config(tmp_path)
    write_status(cfg)
    assert cfg.managed_miner_runtime_dir is not None
    log = cfg.managed_miner_runtime_dir / "xmrig.log"
    log.write_text(
        "2026-07-15T12:00:20Z --password hunter2 --api-key supersecret "
        "--pool-password='quoted secret' -p shortsecret "
        "--password comma,secret --api-key amp&secret accepted\n",
        encoding="utf-8",
    )
    make_recent(log)

    text = collect_managed_miner_logs(cfg, now=NOW)["logs"][0]["lines"][0]

    for secret in (
        "hunter2",
        "supersecret",
        "quoted secret",
        "shortsecret",
        "comma,secret",
        "amp&secret",
    ):
        assert secret not in text
    assert text.count("[REDACTED]") == 6


def test_old_untimestamped_file_is_not_returned(tmp_path: Path):
    cfg = config(tmp_path)
    write_status(cfg)
    assert cfg.managed_miner_runtime_dir is not None
    log = cfg.managed_miner_runtime_dir / "xmrig.log"
    log.write_text("wallet and old output\n", encoding="utf-8")
    old = NOW.timestamp() - 300
    os.utime(log, (old, old))

    assert collect_managed_miner_logs(cfg, now=NOW)["logs"] == []


def test_srbminer_time_only_lines_are_filtered_to_the_same_window(tmp_path: Path):
    cfg = config(tmp_path)
    write_status(cfg, "srbminer")
    assert cfg.managed_miner_runtime_dir is not None
    log = cfg.managed_miner_runtime_dir / "srbminer.log"
    local_now = NOW.astimezone()
    old_time = (local_now.timestamp() - 120)
    recent_time = (local_now.timestamp() - 20)
    old_label = datetime.fromtimestamp(old_time).strftime("%H:%M:%S")
    recent_label = datetime.fromtimestamp(recent_time).strftime("%H:%M:%S")
    log.write_text(
        f"[{old_label}] old hashrate\n[{recent_label}] recent hashrate\n",
        encoding="utf-8",
    )
    make_recent(log)

    result = collect_managed_miner_logs(cfg, now=NOW)
    text = "\n".join(result["logs"][0]["lines"])

    assert "old hashrate" not in text
    assert "recent hashrate" in text


def test_symlink_or_reparse_log_is_never_followed(tmp_path: Path):
    cfg = config(tmp_path)
    write_status(cfg)
    assert cfg.managed_miner_runtime_dir is not None
    outside = tmp_path / "outside-secret.log"
    outside.write_text("TOP SECRET\n", encoding="utf-8")
    link = cfg.managed_miner_runtime_dir / "xmrig.log"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = collect_managed_miner_logs(cfg, now=NOW)

    assert result["logs"] == []
    assert "TOP SECRET" not in json.dumps(result)


def test_command_has_no_remote_path_or_window_and_works_in_dry_run(tmp_path: Path):
    cfg = config(tmp_path, dry_run=True)
    write_status(cfg)
    assert cfg.managed_miner_runtime_dir is not None
    log = cfg.managed_miner_runtime_dir / "xmrig.log"
    log.write_text("2026-07-15T12:00:20Z local log\n", encoding="utf-8")
    # Use the real current clock for the command integration branch.
    os.utime(log, None)
    executor = CommandExecutor(cfg)

    accepted = executor.execute(Command("logs", "collect_miner_logs", {}))
    remote_path = executor.execute(
        Command("path", "collect_miner_logs", {"path": str(tmp_path / "secret")})
    )
    remote_window = executor.execute(
        Command("window", "collect_miner_logs", {"seconds": 3600})
    )

    assert accepted.status == "succeeded"
    assert accepted.result["window_seconds"] == 60
    assert "dry_run" not in accepted.result
    assert remote_path.status == "failed" and remote_path.result["rejected"] is True
    assert remote_window.status == "failed" and remote_window.result["rejected"] is True
    assert "no fields" in (remote_path.error or "")


@pytest.mark.parametrize("timestamp", [
    "0001-01-01T00:00:00+14:00",
    "9999-12-31T23:59:59-14:00",
])
def test_unrepresentable_timestamp_does_not_abort_recent_log_snapshot(tmp_path, timestamp):
    cfg = config(tmp_path)
    write_status(cfg)
    log = cfg.managed_miner_runtime_dir / "xmrig.log"
    log.write_text(f"{timestamp} invalid clock\n2026-07-15T12:00:20Z valid sample\n")
    make_recent(log)
    result = collect_managed_miner_logs(cfg, now=NOW)
    assert result["logs"][0]["lines"] == ["2026-07-15T12:00:20Z valid sample"]


@pytest.mark.parametrize("field, invalid", [
    ("create_time", 10 ** 1000), ("create_time", True),
    ("create_time", float("nan")), ("create_time", float("inf")),
    ("miner_type", []), ("miner_type", {}),
], ids=["overflow", "boolean", "nan", "infinity", "miner-list", "miner-object"])
def test_process_record_invalid_values_are_rejected_without_mutation(tmp_path, field, invalid):
    cfg = config(tmp_path)
    write_process_record(cfg, tmp_path / "xmrig.exe")
    path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    value = json.loads(path.read_text())
    value[field] = invalid
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    assert miner_logs_module._valid_process_record(cfg.managed_miner_runtime_dir) is None
    if field == "miner_type":
        write_status(cfg, invalid)
        assert collect_managed_miner_logs(cfg, now=NOW)["miner_type"] is None
    assert path.read_bytes() == before


def test_process_record_json_integer_parser_limit_is_rejected_without_mutation(tmp_path):
    cfg = config(tmp_path)
    write_process_record(cfg, tmp_path / "xmrig.exe")
    path = cfg.managed_miner_runtime_dir / "managed-miner-process.json"
    path.write_text(path.read_text().replace('"create_time": 1000.25', '"create_time": ' + "1" * 5000))
    before = path.read_bytes()
    assert miner_logs_module._valid_process_record(cfg.managed_miner_runtime_dir) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
@pytest.mark.parametrize("source", ["status", "process", "flight"])
def test_retired_log_history_does_not_read_logs_or_inspect_process(tmp_path, miner_type, source):
    cfg = config(tmp_path)
    runtime = cfg.managed_miner_runtime_dir
    runtime.mkdir()
    if source == "status":
        write_status(cfg, miner_type)
        saved = cfg.miner_status_file
    elif source == "process":
        write_process_record(cfg, tmp_path / "retired-miner", miner_type=miner_type)
        saved = runtime / "managed-miner-process.json"
    else:
        saved = tmp_path / "flight.json"
        cfg = config(tmp_path, flight_sheet_file=saved)
        saved.write_text(json.dumps({"primary": {"miner_type": miner_type,
                                                 "miner_profile": "default"}}))
    before = saved.read_bytes()
    log = runtime / f"{miner_type}.log"
    log.write_text("2026-07-15T12:00:05Z archived private line\n")
    def forbidden(*args, **kwargs):
        pytest.fail("retired adapter must not inspect/adopt a live process")
    snapshot = collect_managed_miner_logs(cfg, now=NOW, process_factory=forbidden,
                                         process_iter_factory=forbidden)
    assert snapshot["miner_type"] == miner_type and snapshot["retired"] is True
    assert snapshot["running"] is False and snapshot["logs"] == []
    assert snapshot["error"] == "managed miner adapter is retired"
    assert saved.read_bytes() == before
    assert log.read_text() == "2026-07-15T12:00:05Z archived private line\n"


@pytest.mark.parametrize("display_name", ["lolminer", "forgeminer"])
def test_generic_same_name_flight_does_not_block_log_semantics(tmp_path, monkeypatch, display_name):
    from agent.miner_packages import validate_package_metadata
    from agent.tests.test_miner_packages import metadata, zip_bytes
    cfg = config(tmp_path, flight_sheet_file=tmp_path / "flight.json", allow_miner_downloads=True,
                 miner_download_host_allowlist=frozenset({"downloads.example.test"}))
    cfg.managed_miner_runtime_dir.mkdir()
    package = metadata(zip_bytes({"generic-program": b"generic"}), id=display_name, name=display_name)
    validated = validate_package_metadata(package, cfg)
    cfg.flight_sheet_file.write_text(json.dumps({"primary": {
        "miner_type": "generic", "miner_profile": "default",
        "miner_package": validated}, "secondary": None}))
    cfg.miner_status_file.write_text(json.dumps({"miner_type": "generic", "state": "stopped", "flight_sheet_id": "same-name-history"}))
    before = cfg.flight_sheet_file.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("stopped same-name history must not inspect/adopt a process")
    snapshot = collect_managed_miner_logs(cfg, now=NOW, process_factory=forbidden, process_iter_factory=forbidden)
    assert "retired" not in snapshot and "error" not in snapshot
    assert snapshot["running"] is False and cfg.flight_sheet_file.read_bytes() == before
    assert snapshot["miner_type"] is None and snapshot["logs"] == []


@pytest.mark.parametrize("adapter_id", ["org.lolliedieb.lolminer", "org.hashraptor.forgeminer"])
def test_structured_retired_adapter_id_remains_a_retired_log_identity(adapter_id):
    assert miner_logs_module._retired_log_identity({"adapter_id": adapter_id}) in {"lolminer", "forgeminer"}
