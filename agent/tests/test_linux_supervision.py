from __future__ import annotations

import contextlib
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import AgentConfig
from agent.linux_supervision import (
    SUPERVISION_CONTENT,
    SUPERVISION_DROPIN,
    SUPERVISION_SERVICE,
    SystemdWatchdog,
    notify_watchdog,
    repair_supervision_policy,
    schedule_supervision_policy_repair,
    supervision_diagnostics,
    supervision_policy_ready,
)


def _config() -> AgentConfig:
    return AgentConfig(
        server_url="https://controller.example",
        state_file=Path("/var/lib/rigpilot-agent/state.json"),
    )


def _effective(dropin: Path = SUPERVISION_DROPIN):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "NotifyAccess=all\n"
                "Type=notify\n"
                "WatchdogUSec=2min\n"
                "Restart=on-failure\n"
                "KillMode=process\n"
                "TimeoutStartUSec=45s\n"
                f"DropInPaths={dropin}\n"
                "ActiveState=active\n"
            ),
            stderr="",
        )

    return run


def test_readiness_requires_exact_dropin_and_effective_policy(monkeypatch, tmp_path):
    import agent.linux_supervision as module

    dropin = tmp_path / "supervision.conf"
    dropin.write_bytes(SUPERVISION_CONTENT)
    monkeypatch.setattr(module, "SUPERVISION_DROPIN", dropin)
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "_trusted_paths", lambda **_kwargs: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)

    assert supervision_policy_ready(_config(), runner=_effective(dropin))
    dropin.write_bytes(b"[Service]\nWatchdogSec=60s\n")
    assert not supervision_policy_ready(_config(), runner=_effective(dropin))
    dropin.write_bytes(SUPERVISION_CONTENT)

    def wrong_watchdog(*_args, **_kwargs):
        result = _effective(dropin)()
        result.stdout = result.stdout.replace("2min", "1min")
        return result

    assert not supervision_policy_ready(_config(), runner=wrong_watchdog)

    def wrong_process_scope(*_args, **_kwargs):
        result = _effective(dropin)()
        result.stdout = result.stdout.replace("KillMode=process", "KillMode=control-group")
        return result

    assert not supervision_policy_ready(_config(), runner=wrong_process_scope)

    def wrong_start_timeout(*_args, **_kwargs):
        result = _effective(dropin)()
        result.stdout = result.stdout.replace(
            "TimeoutStartUSec=45s", "TimeoutStartUSec=90s"
        )
        return result

    assert not supervision_policy_ready(_config(), runner=wrong_start_timeout)


@pytest.mark.skipif(os.name == "nt", reason="dir_fd creation is Linux-only")
def test_online_repair_safely_bootstraps_missing_dropin_directory(monkeypatch, tmp_path):
    import agent.linux_supervision as module

    systemd = tmp_path / "etc" / "systemd" / "system"
    systemd.mkdir(parents=True)
    service = systemd / "rigpilot-agent.service"
    service.write_text("[Service]\n", encoding="ascii")
    config_dir = tmp_path / "etc" / "rigpilot-agent"
    config_dir.mkdir(parents=True)
    config = config_dir / "config.json"
    config.write_text("{}\n", encoding="ascii")
    dropin_dir = systemd / "rigpilot-agent.service.d"
    # The repair runs as root on Linux. Model ownership for the created
    # directory/file while preserving real existence, type and permission bits.
    real_lstat = Path.lstat

    def dropin_lstat(path):
        info = real_lstat(path)
        if path in (dropin_dir, dropin_dir / "supervision.conf"):
            fields = list(info)
            fields[stat.ST_UID] = fields[stat.ST_GID] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", dropin_lstat)
    monkeypatch.setattr(module, "SUPERVISION_CONFIG", config)
    monkeypatch.setattr(module, "SUPERVISION_SERVICE_FILE", service)
    monkeypatch.setattr(module, "SUPERVISION_DROPIN_DIRECTORY", dropin_dir)
    monkeypatch.setattr(module, "SUPERVISION_DROPIN", dropin_dir / "supervision.conf")
    monkeypatch.setattr(module, "_linux_plain_root_owned", lambda *_a, **_k: True)
    monkeypatch.setattr(module, "_linux_protected_directory_chain", lambda _p: True)
    ownership_changes: list[tuple[int, int]] = []
    monkeypatch.setattr(
        module.os,
        "fchown",
        lambda _fd, uid, gid: ownership_changes.append((uid, gid)),
        raising=False,
    )

    assert module._trusted_paths(require_dropin=False)
    assert not module._trusted_paths(require_dropin=True)
    module._write_dropin()
    assert module.SUPERVISION_DROPIN.read_bytes() == SUPERVISION_CONTENT
    assert module._trusted_paths(require_dropin=True)
    assert ownership_changes == [(0, 0)]
    assert module.SUPERVISION_DROPIN.stat().st_mode & 0o777 == 0o644
    module._replace_dropin(None)
    assert not module.SUPERVISION_DROPIN.exists()


def test_schedule_uses_only_fixed_transient_unit_and_active_executable(
    monkeypatch, tmp_path
):
    import agent.linux_supervision as module

    root = tmp_path / "opt" / "rigpilot-agent"
    data = tmp_path / "var" / "lib" / "rigpilot-agent"
    executable = root / "versions" / "0.8.1" / "RigPilotAgent"
    captured = {}
    statuses = []
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "supervision_policy_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(module, "_linux_update_layout", lambda _c: (root, data, executable))
    monkeypatch.setattr(module, "_trusted_paths", lambda **_k: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _p: True)
    monkeypatch.setattr(module, "_load_status", lambda: None)
    monkeypatch.setattr(module, "_write_status", lambda *args: statuses.append(args))

    @contextlib.contextmanager
    def guard(_root):
        yield True

    monkeypatch.setattr(module, "_update_guard", guard)

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    assert schedule_supervision_policy_repair(_config(), runner=run)
    argv = captured["argv"]
    assert Path(argv[0]).as_posix() == "/usr/bin/systemd-run"
    assert str(executable) in argv
    assert argv[-3:] == [
        "--config",
        str(module.SUPERVISION_CONFIG),
        "--repair-supervision-policy",
    ]
    assert any(str(data) in value for value in argv)
    assert any(str(module.SUPERVISION_SERVICE_FILE.parent) in value for value in argv)
    assert "--property=TimeoutStartSec=240s" in argv
    assert not any("/root" in value for value in argv)
    assert captured["kwargs"]["shell"] is False
    assert statuses[0][0:2] == ("submitted", "none")


def test_schedule_skips_an_active_update_transaction(monkeypatch, tmp_path):
    import agent.linux_supervision as module

    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "supervision_policy_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(
        module,
        "_linux_update_layout",
        lambda _c: (tmp_path, tmp_path, tmp_path / "RigPilotAgent"),
    )
    monkeypatch.setattr(module, "_trusted_paths", lambda **_k: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _p: True)
    monkeypatch.setattr(module, "_load_status", lambda: None)

    @contextlib.contextmanager
    def busy(_root):
        yield False

    monkeypatch.setattr(module, "_update_guard", busy)
    assert not schedule_supervision_policy_repair(
        _config(), runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )


def test_schedule_retries_beyond_three_with_bounded_backoff(monkeypatch, tmp_path):
    import agent.linux_supervision as module

    status = {
        "state": "failed",
        "reason": "service_restart_failed",
        "unit": "rigpilot-agent-supervision-123-9",
        "attempt": 9,
        "version": module.__version__,
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    submitted = []
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "supervision_policy_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(
        module,
        "_linux_update_layout",
        lambda _c: (tmp_path, tmp_path, tmp_path / "RigPilotAgent"),
    )
    monkeypatch.setattr(module, "_trusted_paths", lambda **_k: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _p: True)
    monkeypatch.setattr(module, "_load_status", lambda: status)
    monkeypatch.setattr(module, "_write_status", lambda *args: submitted.append(args))

    @contextlib.contextmanager
    def available(_root):
        yield True

    monkeypatch.setattr(module, "_update_guard", available)
    assert schedule_supervision_policy_repair(
        _config(), runner=lambda argv, **_k: subprocess.CompletedProcess(argv, 0)
    )
    assert submitted[0][3] == 10
    assert submitted[0][2].endswith("-10")


def test_schedule_caps_retry_delay_at_one_hour(monkeypatch, tmp_path):
    import agent.linux_supervision as module

    status = {
        "state": "failed",
        "reason": "service_restart_failed",
        "unit": "rigpilot-agent-supervision-123-20",
        "attempt": 20,
        "version": module.__version__,
        "updated_at": "2020-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "supervision_policy_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(
        module,
        "_linux_update_layout",
        lambda _c: (tmp_path, tmp_path, tmp_path / "RigPilotAgent"),
    )
    monkeypatch.setattr(module, "_load_status", lambda: status)
    monkeypatch.setattr(module, "_status_age", lambda _s: 3599.0)
    assert not schedule_supervision_policy_repair(_config())


def test_future_status_timestamp_is_invalid():
    import agent.linux_supervision as module

    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert module._status_age({"updated_at": future.isoformat()}) == float("inf")


def test_update_lock_parent_accepts_only_protected_or_root_sticky_directory(monkeypatch):
    import agent.linux_supervision as module

    monkeypatch.setattr(module, "_linux_protected_directory_chain", lambda _p: True)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _p: SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=0),
    )
    assert module._trusted_update_lock_parent()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _p: SimpleNamespace(st_mode=stat.S_IFDIR | 0o0777, st_uid=0),
    )
    assert not module._trusted_update_lock_parent()
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda _p: SimpleNamespace(st_mode=stat.S_IFLNK | 0o0777, st_uid=0),
    )
    assert not module._trusted_update_lock_parent()


def test_ready_restarted_agent_promotes_pending_receipt(monkeypatch):
    import agent.linux_supervision as module

    status = {
        "state": "running",
        "reason": "none",
        "unit": "rigpilot-agent-supervision-456-2",
        "attempt": 2,
        "version": module.__version__,
    }
    writes = []
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "supervision_policy_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(module, "_load_status", lambda: status)
    monkeypatch.setattr(module, "_write_status", lambda *args: writes.append(args))
    assert not schedule_supervision_policy_repair(_config())
    assert writes == [
        ("succeeded", "none", "rigpilot-agent-supervision-456-2", 2)
    ]


def test_hidden_repair_writes_dropin_reloads_and_restarts_only_agent(
    monkeypatch, tmp_path
):
    import agent.linux_supervision as module

    status = {
        "state": "submitted",
        "reason": "none",
        "unit": "rigpilot-agent-supervision-123-1",
        "attempt": 1,
        "version": module.__version__,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    calls = []
    writes = []
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    fixed_config = Path(os.path.abspath(tmp_path / "config.json"))
    monkeypatch.setattr(module, "SUPERVISION_CONFIG", fixed_config)
    monkeypatch.setattr(module, "_load_status", lambda: status)
    monkeypatch.setattr(
        module, "_linux_update_layout", lambda _c: (tmp_path, tmp_path, tmp_path / "agent")
    )
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _p: True)
    monkeypatch.setattr(module, "_write_status", lambda *args: writes.append(args))
    monkeypatch.setattr(module, "_read_dropin_backup", lambda: b"old")
    monkeypatch.setattr(module, "_write_dropin", lambda: None)
    monkeypatch.setattr(module, "supervision_policy_ready", lambda *_a, **_k: True)

    @contextlib.contextmanager
    def available(_root):
        yield True

    monkeypatch.setattr(module, "_update_guard", available)

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    repair_supervision_policy(_config(), fixed_config, runner=run)
    assert [Path(call[0]).as_posix() for call in calls] == [
        "/usr/bin/systemctl",
        "/usr/bin/systemctl",
    ]
    assert calls[0][1:] == ["daemon-reload"]
    assert calls[1][1:] == ["restart", SUPERVISION_SERVICE]
    assert writes == [("running", "none", status["unit"], 1)]
    assert all("miner" not in " ".join(call).lower() for call in calls)


def test_failed_notify_activation_atomically_rolls_back_old_policy(
    monkeypatch, tmp_path
):
    import agent.linux_supervision as module

    status = {
        "state": "submitted",
        "reason": "none",
        "unit": "rigpilot-agent-supervision-123-4",
        "attempt": 4,
        "version": module.__version__,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    fixed_config = Path(os.path.abspath(tmp_path / "config.json"))
    writes = []
    restored = []
    calls = []
    monkeypatch.setattr(module, "SUPERVISION_CONFIG", fixed_config)
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "_load_status", lambda: status)
    monkeypatch.setattr(
        module, "_linux_update_layout", lambda _c: (tmp_path, tmp_path, tmp_path / "agent")
    )
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _p: True)
    monkeypatch.setattr(module, "_write_status", lambda *args: writes.append(args))
    monkeypatch.setattr(module, "_read_dropin_backup", lambda: b"old-policy\n")
    monkeypatch.setattr(module, "_write_dropin", lambda: None)
    monkeypatch.setattr(
        module, "_replace_dropin", lambda payload: restored.append(payload)
    )

    @contextlib.contextmanager
    def available(_root):
        yield True

    monkeypatch.setattr(module, "_update_guard", available)

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs.get("timeout")))
        # First reload succeeds, new Type=notify start fails.  Rollback reload
        # and restart then both succeed.
        return subprocess.CompletedProcess(argv, 1 if len(calls) == 2 else 0)

    with pytest.raises(module.LinuxSupervisionError) as error:
        repair_supervision_policy(_config(), fixed_config, runner=run)
    assert error.value.reason == "service_restart_failed"
    assert restored == [b"old-policy\n"]
    assert [call[0][1] for call in calls] == [
        "daemon-reload", "restart", "daemon-reload", "restart"
    ]
    assert calls[1][1] == module.SYSTEMCTL_RESTART_TIMEOUT_SECONDS
    assert writes[-1][0:2] == ("failed", "service_restart_failed")


def test_diagnostics_has_stable_controller_fields(monkeypatch):
    import agent.linux_supervision as module

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module, "_production_linux_root", lambda: True)
    monkeypatch.setattr(module, "supervision_policy_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(module, "_load_status", lambda: None)
    value = supervision_diagnostics(_config())
    assert value == {
        "schema_version": 1,
        "configured": True,
        "ready": True,
        "state": "ready",
        "service_name": "systemd watchdog",
        "watchdog_seconds": 120,
        "repair_state": None,
        "repair_reason": "none",
    }


def test_sd_notify_supports_filesystem_and_abstract_sockets(monkeypatch):
    import agent.linux_supervision as module

    sent = []

    class Channel:
        def settimeout(self, timeout):
            self.timeout = timeout

        def sendto(self, payload, address):
            assert self.timeout == module.NOTIFY_SOCKET_TIMEOUT_SECONDS
            sent.append((payload, address))

        def close(self):
            sent.append((b"closed", ""))

    factory = lambda *_args: Channel()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.socket, "AF_UNIX", 1, raising=False)
    assert notify_watchdog(
        environment={"NOTIFY_SOCKET": "/run/systemd/notify"}, socket_factory=factory
    )
    assert notify_watchdog(
        environment={"NOTIFY_SOCKET": "@systemd/notify"}, socket_factory=factory
    )
    assert sent[0] == (b"WATCHDOG=1", "/run/systemd/notify")
    assert sent[2] == (b"WATCHDOG=1", "\0systemd/notify")
    assert not notify_watchdog(environment={"NOTIFY_SOCKET": "relative"}, socket_factory=factory)


def test_sd_notify_handles_socket_creation_failure(monkeypatch):
    import agent.linux_supervision as module

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.socket, "AF_UNIX", 1, raising=False)

    def fail(*_args):
        raise OSError("socket unavailable")

    assert not notify_watchdog(
        environment={"NOTIFY_SOCKET": "/run/systemd/notify"}, socket_factory=fail
    )


def test_watchdog_feeds_only_during_progress_lease_and_close_is_idempotent(monkeypatch):
    import agent.linux_supervision as module

    now = [100.0]
    sent = []
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    watchdog = SystemdWatchdog(
        environment={"NOTIFY_SOCKET": "/run/systemd/notify", "WATCHDOG_USEC": "120000000"},
        monotonic=lambda: now[0],
        notifier=lambda **kwargs: sent.append(kwargs["payload"]) or True,
        autostart=False,
    )
    assert watchdog.enabled
    assert not watchdog.feed_if_progressing()
    assert watchdog.progress(90)
    assert watchdog.feed_if_progressing()
    now[0] = 191.0
    assert not watchdog.feed_if_progressing()
    with watchdog.lease(600):
        assert watchdog.feed_if_progressing()
    now[0] = 281.0
    assert watchdog.feed_if_progressing()
    now[0] = 282.0
    assert not watchdog.feed_if_progressing()
    watchdog.close()
    watchdog.close()
    assert not watchdog.progress(90)
    assert sent == [
        f"MAINPID={os.getpid()}\nREADY=1".encode("ascii"),
        b"WATCHDOG=1",
        b"WATCHDOG=1",
        b"WATCHDOG=1",
    ]


def test_onefile_child_accepts_bootloader_parent_and_claims_main_pid(monkeypatch):
    import agent.linux_supervision as module

    sent = []
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.os, "getpid", lambda: 4321)
    monkeypatch.setattr(module.os, "getppid", lambda: 1234)
    watchdog = SystemdWatchdog(
        environment={
            "NOTIFY_SOCKET": "/run/systemd/notify",
            "WATCHDOG_USEC": "120000000",
            "WATCHDOG_PID": "1234",
        },
        notifier=lambda **kwargs: sent.append(kwargs["payload"]) or True,
        autostart=False,
    )
    assert watchdog.enabled
    assert watchdog.progress()
    assert watchdog.feed_if_progressing()
    assert sent == [b"MAINPID=4321\nREADY=1", b"WATCHDOG=1"]
    watchdog.close()


def test_watchdog_rejects_unrelated_watchdog_pid(monkeypatch):
    import agent.linux_supervision as module

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.os, "getpid", lambda: 4321)
    monkeypatch.setattr(module.os, "getppid", lambda: 1234)
    watchdog = SystemdWatchdog(
        environment={
            "NOTIFY_SOCKET": "/run/systemd/notify",
            "WATCHDOG_USEC": "120000000",
            "WATCHDOG_PID": "9999",
        },
        autostart=False,
    )
    assert not watchdog.enabled


def test_watchdog_is_quiet_without_systemd_or_on_windows(monkeypatch):
    import agent.linux_supervision as module

    monkeypatch.setattr(module.platform, "system", lambda: "Windows")
    watchdog = SystemdWatchdog(
        environment={"NOTIFY_SOCKET": "/run/systemd/notify", "WATCHDOG_USEC": "120000000"},
        autostart=False,
    )
    assert not watchdog.enabled
    assert not watchdog.progress()
    watchdog.close()


@pytest.fixture
def update_guard_io(monkeypatch):
    import fcntl
    import agent.linux_supervision as module

    events = []
    fake_fd = 123
    monkeypatch.setattr(module, "_pending_update_exists", lambda _root: False)
    monkeypatch.setattr(module, "_trusted_update_lock_parent", lambda: True)
    fake_os = SimpleNamespace(
        O_RDWR=os.O_RDWR,
        O_CREAT=os.O_CREAT,
        O_NOFOLLOW=getattr(os, "O_NOFOLLOW", 0),
        O_CLOEXEC=getattr(os, "O_CLOEXEC", 0),
        open=lambda *_args: fake_fd,
        fstat=lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1),
        fchmod=lambda fd, mode: events.append(("chmod", fd, mode)),
        close=lambda fd: events.append(("close", fd)),
    )
    monkeypatch.setattr(module, "os", fake_os)
    monkeypatch.setattr(fcntl, "flock", lambda fd, flags: events.append(("flock", fd, flags)))
    return module, fake_os, fcntl, events, fake_fd


def test_update_guard_propagates_body_os_error_and_releases_lock(update_guard_io):
    module, _fake_os, fcntl, events, fd = update_guard_io
    failure = OSError("status disk unavailable")
    with pytest.raises(OSError) as captured:
        with module._update_guard(Path("/unused")) as available:
            assert available is True
            raise failure
    assert captured.value is failure
    assert events[-2:] == [("flock", fd, fcntl.LOCK_UN), ("close", fd)]


def test_update_guard_setup_error_yields_unavailable_once(update_guard_io):
    module, fake_os, _fcntl, events, _fd = update_guard_io

    def failed_open(*_args):
        raise OSError("lock open unavailable")

    fake_os.open = failed_open
    body_calls = 0
    with module._update_guard(Path("/unused")) as available:
        assert available is False
        body_calls += 1
    assert body_calls == 1
    assert events == []


def test_update_guard_busy_lock_is_unavailable_and_closes_fd(update_guard_io, monkeypatch):
    module, _fake_os, fcntl, events, fd = update_guard_io

    def busy(_fd, _flags):
        raise BlockingIOError("updater owns the lock")

    monkeypatch.setattr(fcntl, "flock", busy)
    with module._update_guard(Path("/unused")) as available:
        assert available is False
    assert events[-1] == ("close", fd)


@pytest.mark.parametrize("send_error", [BlockingIOError("busy"), TimeoutError("timeout"), OSError("send failed")])
def test_sd_notify_bounds_busy_or_timed_out_send_and_always_closes(monkeypatch, send_error):
    import agent.linux_supervision as module

    events = []

    class Channel:
        def settimeout(self, timeout):
            events.append(("timeout", timeout))

        def sendto(self, payload, address):
            events.append(("send", payload, address))
            raise send_error

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    assert notify_watchdog(
        environment={"NOTIFY_SOCKET": "/run/systemd/notify"},
        socket_factory=lambda *_args: Channel(),
    ) is False
    assert events == [
        ("timeout", module.NOTIFY_SOCKET_TIMEOUT_SECONDS),
        ("send", b"WATCHDOG=1", "/run/systemd/notify"),
        ("close",),
    ]




@pytest.mark.parametrize("damage", ["integer", "state", "reason"])
def test_invalid_supervision_status_is_rejected(tmp_path, monkeypatch, damage):
    import json
    import agent.linux_supervision as module
    path = tmp_path / "repair-status.json"
    if damage == "integer":
        content = '{"invalid":' + '9' * 5000 + '}'
    else:
        document = module._status_document("running", "none", "rigpilot-agent-supervision-123-1", 1)
        document[damage] = [] if damage == "state" else {}
        content = json.dumps(document)
    path.write_text(content)
    monkeypatch.setattr(module, "SUPERVISION_STATUS", path)
    monkeypatch.setattr(module, "_status_boundary_trusted", lambda **_kwargs: True)
    assert module._load_status() is None
