from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import agent.linux_ssh as module
from agent.commands import Command, CommandExecutor
from agent.config import AgentConfig
from agent.linux_ssh import (
    LINUX_SSH_CONFIGURE_ACTION,
    LINUX_SSH_MANAGE_CAPABILITY,
    LinuxSshError,
    configure_linux_ssh,
    linux_ssh_status,
    validate_linux_ssh_payload,
)


def _config() -> AgentConfig:
    return AgentConfig(
        server_url="https://controller.example",
        state_file=Path("state.json"),
    )


def _completed(argv: list[str], returncode: int = 0, stdout: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


def test_payload_is_one_exact_boolean():
    assert validate_linux_ssh_payload({"enabled": True}) == {"enabled": True}
    for invalid in ({}, {"enabled": 1}, {"enabled": True, "port": 22}):
        with pytest.raises(LinuxSshError):
            validate_linux_ssh_payload(invalid)


def test_status_reports_missing_server_without_hiding_management(monkeypatch):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda **_kw: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: False)
    monkeypatch.setattr(module, "_account_available", lambda: False)

    status = linux_ssh_status(system_name="Linux", connections_factory=lambda **_kw: [])

    assert status == {
        "schema_version": 1,
        "manageable": True,
        "installed": False,
        "account_available": False,
        "listening": False,
        "service_active": False,
        "socket_active": False,
        "managed_policy": False,
        "effective_policy": False,
        "port": 22,
        "account": "user",
        "access_scope": "private_sources_only",
        "reason": "openssh_server_missing",
    }


def test_enable_missing_server_returns_chinese_install_guidance(monkeypatch):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: False)

    with pytest.raises(LinuxSshError, match="未安装 openssh-server") as exc_info:
        configure_linux_ssh({"enabled": True})

    assert "apt-get install" in str(exc_info.value)


def test_effective_policy_requires_private_allowusers_for_every_context():
    private = " ".join(sorted(module._PRIVATE_ALLOW_USERS))

    def runner(argv, **_kwargs):
        if "-t" in argv:
            return _completed(argv)
        context = argv[argv.index("-C") + 1]
        password = "no" if "addr=203.0.113.2" in context else "yes"
        root = "no"
        return _completed(
            argv,
            stdout=(
                f"passwordauthentication {password}\n"
                f"permitrootlogin {root}\n"
                "port 22\n"
                f"allowusers {private}\n"
            ),
        )

    module._validate_effective_policy(runner=runner)

    def unsafe_runner(argv, **_kwargs):
        if "-t" in argv:
            return _completed(argv)
        context = argv[argv.index("-C") + 1]
        password = "no" if "addr=203.0.113.2" in context else "yes"
        return _completed(
            argv,
            stdout=(
                f"passwordauthentication {password}\n"
                "permitrootlogin no\n"
                "port 22\n"
                "allowusers user\n"
            ),
        )

    with pytest.raises(LinuxSshError, match="来源地址限制"):
        module._validate_effective_policy(runner=unsafe_runner)

    def extra_port_runner(argv, **_kwargs):
        if "-t" in argv:
            return _completed(argv)
        context = argv[argv.index("-C") + 1]
        password = "no" if "addr=203.0.113.2" in context else "yes"
        return _completed(
            argv,
            stdout=(
                f"passwordauthentication {password}\n"
                "permitrootlogin no\n"
                "port 22\nport 2222\n"
                f"allowusers {private}\n"
            ),
        )

    with pytest.raises(LinuxSshError, match="安全策略"):
        module._validate_effective_policy(runner=extra_port_runner)


def test_managed_policy_requires_exact_content(monkeypatch):
    monkeypatch.setattr(module, "_read_managed_policy", lambda: module._POLICY)
    assert module._managed_policy_content_matches() is True
    monkeypatch.setattr(module, "_read_managed_policy", lambda: module._POLICY + b"# changed\n")
    assert module._managed_policy_content_matches() is False


def test_status_calls_listener_managed_only_when_exact_policy_matches(monkeypatch):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda **_kw: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_account_available", lambda: True)
    effective = {"value": True}
    monkeypatch.setattr(
        module,
        "_effective_policy_matches",
        lambda **_kwargs: effective["value"],
    )
    policy = {"value": module._POLICY}
    monkeypatch.setattr(module, "_read_managed_policy", lambda: policy["value"])

    def runner(argv, **_kwargs):
        return _completed(argv, 3 if argv[-1] == "ssh.socket" else 0)

    connections = lambda **_kwargs: [
        SimpleNamespace(status=module.psutil.CONN_LISTEN, laddr=("0.0.0.0", 22))
    ]
    ready = linux_ssh_status(
        system_name="Linux", connections_factory=connections, runner=runner
    )
    assert ready["managed_policy"] is True
    assert ready["effective_policy"] is True
    assert ready["service_active"] is True
    assert ready["socket_active"] is False
    assert ready["reason"] == "ready"

    policy["value"] = b"# different\n"
    unmanaged = linux_ssh_status(
        system_name="Linux", connections_factory=connections, runner=runner
    )
    assert unmanaged["managed_policy"] is False
    assert unmanaged["effective_policy"] is False
    assert unmanaged["reason"] == "unmanaged_listener"

    policy["value"] = module._POLICY
    effective["value"] = False
    drifted = linux_ssh_status(
        system_name="Linux", connections_factory=connections, runner=runner
    )
    assert drifted["managed_policy"] is True
    assert drifted["effective_policy"] is False
    assert drifted["reason"] == "policy_drift"


def test_enable_uses_only_fixed_policy_and_systemd_vectors(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_account_available", lambda: True)
    monkeypatch.setattr(module, "_read_managed_policy", lambda: None)
    write = mock.Mock()
    validate = mock.Mock()
    monkeypatch.setattr(module, "_write_policy_atomically", write)
    monkeypatch.setattr(module, "_validate_effective_policy", validate)

    state = {"service": False}

    def runner(argv, **kwargs):
        assert kwargs["shell"] is False
        calls.append(argv)
        if argv[1] == "is-enabled":
            return _completed(argv, 1, "disabled\n")
        if argv[1:3] == ["is-active", "--quiet"]:
            unit = argv[3]
            return _completed(argv, 0 if unit == "ssh.service" and state["service"] else 3)
        if argv[1:3] == ["enable", "--now"]:
            state["service"] = True
        return _completed(argv)

    def connections(**_kwargs):
        return [SimpleNamespace(status=module.psutil.CONN_LISTEN, laddr=("0.0.0.0", 22))] if state["service"] else []

    result = configure_linux_ssh(
        {"enabled": True}, runner=runner, connections_factory=connections,
        sleeper=lambda _seconds: None,
    )

    write.assert_called_once_with()
    validate.assert_called_once_with(runner=runner)
    assert calls == [
        [str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.socket"],
        [str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "is-enabled", "--", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "enable", "--now", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "reload-or-restart", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.socket"],
    ]
    assert result["firewall_changed"] is False
    assert result["access_scope"] == "private_sources_only"


def test_disable_is_fixed_and_does_not_touch_spark(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_read_managed_policy", lambda: module._POLICY)
    monkeypatch.setattr(module, "_validate_effective_policy", mock.Mock())
    state = {"service": True}

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1:3] == ["is-active", "--quiet"]:
            unit = argv[3]
            return _completed(argv, 0 if unit == "ssh.service" and state["service"] else 3)
        if argv[1:3] == ["disable", "--now"]:
            state["service"] = False
        return _completed(argv)

    def connections(**_kwargs):
        return [SimpleNamespace(status=module.psutil.CONN_LISTEN, laddr=("0.0.0.0", 22))] if state["service"] else []

    result = configure_linux_ssh(
        {"enabled": False}, runner=runner, connections_factory=connections,
        sleeper=lambda _seconds: None,
    )

    assert calls == [
        [str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.socket"],
        [str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "disable", "--now", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.service"],
    ]
    assert result == {"enabled": False, "listening": False, "port": 22}


@pytest.mark.parametrize("enabled", [True, False])
def test_different_existing_policy_is_never_overwritten_or_stopped(monkeypatch, enabled):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_account_available", lambda: True)
    monkeypatch.setattr(module, "_read_managed_policy", lambda: b"# user policy\n")
    write = mock.Mock()
    monkeypatch.setattr(module, "_write_policy_atomically", write)

    def runner(argv, **_kwargs):
        return _completed(argv, 3 if argv[-1] == "ssh.socket" else 0)

    with pytest.raises(LinuxSshError, match="拒绝覆盖或关闭"):
        configure_linux_ssh({"enabled": enabled}, runner=runner)
    write.assert_not_called()


def test_active_user_ssh_socket_is_never_modified(monkeypatch):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    write = mock.Mock()
    monkeypatch.setattr(module, "_write_policy_atomically", write)
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        return _completed(argv, 0)

    with pytest.raises(LinuxSshError, match="ssh.socket"):
        configure_linux_ssh({"enabled": True}, runner=runner)
    assert calls == [[str(module.LINUX_SYSTEMCTL), "is-active", "--quiet", "ssh.socket"]]
    write.assert_not_called()


def test_enable_rolls_back_when_port_22_never_listens(monkeypatch):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_account_available", lambda: True)
    monkeypatch.setattr(module, "_read_managed_policy", lambda: None)
    monkeypatch.setattr(module, "_write_policy_atomically", mock.Mock())
    monkeypatch.setattr(module, "_validate_effective_policy", mock.Mock())
    restore = mock.Mock()
    monkeypatch.setattr(module, "_restore_managed_policy", restore)
    state = {"service": False}

    def runner(argv, **_kwargs):
        if argv[1] == "is-enabled":
            return _completed(argv, 1, "disabled\n")
        if argv[1:3] == ["is-active", "--quiet"]:
            return _completed(
                argv,
                0 if argv[3] == "ssh.service" and state["service"] else 3,
            )
        if argv[1:3] == ["enable", "--now"]:
            state["service"] = True
        if argv[1] == "stop":
            state["service"] = False
        return _completed(argv)

    with pytest.raises(LinuxSshError, match="未能稳定运行"):
        configure_linux_ssh(
            {"enabled": True},
            runner=runner,
            connections_factory=lambda **_kwargs: [],
            sleeper=lambda _seconds: None,
        )
    restore.assert_called_once_with(None)
    assert state["service"] is False


def test_command_executor_accepts_only_fixed_linux_ssh_action():
    payload = {"enabled": True}
    command = Command.parse(
        {"id": "ssh-1", "action": LINUX_SSH_CONFIGURE_ACTION, "payload": payload}
    )
    with mock.patch(
        "agent.commands.configure_linux_ssh",
        return_value={"enabled": True, "listening": True, "port": 22},
    ) as configure:
        outcome = CommandExecutor(_config()).execute(command)
    assert outcome.status == "succeeded"
    configure.assert_called_once_with(payload)

    rejected = CommandExecutor(_config()).execute(
        Command.parse(
            {
                "id": "ssh-bad",
                "action": LINUX_SSH_CONFIGURE_ACTION,
                "payload": {"enabled": True, "command": "sh"},
            }
        )
    )
    assert rejected.status == "failed"
    assert rejected.result["rejected"] is True


def test_capability_name_is_versioned_and_separate_from_spark():
    assert LINUX_SSH_MANAGE_CAPABILITY == "remote_access.ssh_manage.v1"
    assert "spark" not in LINUX_SSH_MANAGE_CAPABILITY


@pytest.mark.parametrize("initial_active,initial_enabled", [(False, True), (True, False)])
def test_failed_ssh_enable_restores_activity_and_boot_enablement_independently(monkeypatch, initial_active, initial_enabled):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_account_available", lambda: True)
    monkeypatch.setattr(module, "_read_managed_policy", lambda: module._POLICY)
    monkeypatch.setattr(module, "_validate_effective_policy", mock.Mock())
    restored = mock.Mock()
    monkeypatch.setattr(module, "_restore_managed_policy", restored)
    monkeypatch.setattr(module, "_wait_for_listener_state", lambda *_args, **_kwargs: True)
    state = {"active": initial_active, "enabled": initial_enabled, "fail_reload": True}
    calls = []

    def runner(argv, **kwargs):
        assert kwargs["shell"] is False
        calls.append(argv)
        if argv[1] == "is-active":
            return _completed(argv, 0 if argv[-1] == "ssh.service" and state["active"] else 3)
        if argv[1] == "is-enabled":
            return _completed(argv, 0 if state["enabled"] else 1, "enabled\n" if state["enabled"] else "disabled\n")
        if argv[1] == "enable":
            state["enabled"] = True
            if "--now" in argv:
                state["active"] = True
        elif argv[1] == "disable":
            state["enabled"] = False
            if "--now" in argv:
                state["active"] = False
        elif argv[1] == "stop":
            state["active"] = False
        elif argv[1] == "reload-or-restart":
            if state["fail_reload"]:
                state["fail_reload"] = False
                return _completed(argv, 1)
            state["active"] = True
        return _completed(argv)

    connections = lambda **_kwargs: [SimpleNamespace(status=module.psutil.CONN_LISTEN, laddr=("0.0.0.0", 22))] if state["active"] else []
    with pytest.raises(LinuxSshError, match="未能稳定运行"):
        configure_linux_ssh({"enabled": True}, runner=runner, connections_factory=connections)
    restored.assert_called_once_with(module._POLICY)
    assert (state["active"], state["enabled"]) == (initial_active, initial_enabled)
    assert all(argv[-1] in {"ssh.service", "ssh.socket"} for argv in calls)


@pytest.mark.parametrize("boot_state,returncode", [("masked", 1), ("static", 0), ("", 4), ("enabled-runtime", 0)])
def test_ssh_enable_does_not_mutate_unrestorable_boot_state(monkeypatch, boot_state, returncode):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_account_available", lambda: True)
    monkeypatch.setattr(module, "_read_managed_policy", lambda: module._POLICY)
    write = mock.Mock()
    monkeypatch.setattr(module, "_write_policy_atomically", write)
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "is-enabled":
            return _completed(argv, returncode, boot_state)
        return _completed(argv, 3)

    with pytest.raises(LinuxSshError, match="开机启用状态无法安全确认"):
        configure_linux_ssh({"enabled": True}, runner=runner, connections_factory=lambda **_kwargs: [])
    assert write.call_count == 0
    assert all(argv[1] in {"is-active", "is-enabled"} for argv in calls)


def test_ssh_activity_rollback_is_attempted_even_if_boot_restore_fails(monkeypatch):
    monkeypatch.setattr(module, "linux_ssh_management_supported", lambda: True)
    monkeypatch.setattr(module, "_linux_trusted_system_executable", lambda _path: True)
    monkeypatch.setattr(module, "_account_available", lambda: True)
    monkeypatch.setattr(module, "_read_managed_policy", lambda: module._POLICY)
    monkeypatch.setattr(module, "_validate_effective_policy", mock.Mock())
    monkeypatch.setattr(module, "_restore_managed_policy", mock.Mock())
    monkeypatch.setattr(module, "_wait_for_listener_state", lambda *_args, **_kwargs: True)
    state = {"active": False}
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "is-active":
            return _completed(argv, 0 if argv[-1] == "ssh.service" and state["active"] else 3)
        if argv[1] == "is-enabled":
            return _completed(argv, 1, "disabled\n")
        if argv[1:3] == ["enable", "--now"]:
            state["active"] = True
        elif argv[1] == "reload-or-restart":
            return _completed(argv, 1)
        elif argv[1] == "disable":
            raise OSError("mock boot-state restoration failed")
        elif argv[1] == "stop":
            state["active"] = False
        return _completed(argv)

    with pytest.raises(LinuxSshError, match="未能稳定运行") as error:
        configure_linux_ssh({"enabled": True}, runner=runner, connections_factory=lambda **_kwargs: [])
    assert "mock boot-state" not in str(error.value)
    assert calls[-2:] == [
        [str(module.LINUX_SYSTEMCTL), "disable", "--", "ssh.service"],
        [str(module.LINUX_SYSTEMCTL), "stop", "ssh.service"],
    ]
    assert state["active"] is False
