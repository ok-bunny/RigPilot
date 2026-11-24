"""Bounded controller management for the host's local OpenSSH listener.

This module deliberately does not provide a generic package manager, command
runner, firewall editor, or tunnel.  A controller administrator may only ask
the root Linux Agent to enable or disable one fixed ``ssh.service`` listener
on port 22.  Enabling also installs a fixed, private-source password policy
for the pre-existing ``user`` account; missing ``openssh-server`` is reported
with an actionable error instead of being installed remotely.
"""

from __future__ import annotations

import os
import platform
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import psutil

try:  # Static import keeps the Linux PyInstaller build aware of this module.
    import pwd as _pwd
except ImportError:  # pragma: no cover - Windows has no pwd module
    _pwd = None

from .agent_updates import (
    LINUX_SYSTEMCTL,
    _linux_plain_root_owned,
    _linux_protected_directory_chain,
    _linux_trusted_system_executable,
)
from .external_process import external_process_environment


LINUX_SSH_MANAGE_CAPABILITY = "remote_access.ssh_manage.v1"
LINUX_SSH_CONFIGURE_ACTION = "configure_linux_ssh.v1"
LINUX_SSH_PORT = 22
LINUX_SSH_ACCOUNT = "user"
LINUX_SSH_SERVICE = "ssh.service"
LINUX_SSH_SOCKET = "ssh.socket"
SSHD = Path("/usr/sbin/sshd")
SSH_CONFIG = Path("/etc/ssh/sshd_config")
SSH_CONFIG_DIRECTORY = Path("/etc/ssh/sshd_config.d")
SSH_MANAGED_DROPIN = SSH_CONFIG_DIRECTORY / "20-rigpilot-lan.conf"

_POLICY = (
    "# Managed by RigPilot: password SSH is LAN-only; Spark does not use port 22.\n"
    "PermitRootLogin no\n"
    "PasswordAuthentication no\n"
    "KbdInteractiveAuthentication no\n"
    "UsePAM yes\n"
    "AllowUsers user@10.0.0.0/8 user@172.16.0.0/12 "
    "user@192.168.0.0/16 user@100.64.0.0/10\n"
    "Match User user Address 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10\n"
    "    PasswordAuthentication yes\n"
    "Match all\n"
).encode("ascii")
_PRIVATE_ALLOW_USERS = frozenset(
    {
        "user@10.0.0.0/8",
        "user@172.16.0.0/12",
        "user@192.168.0.0/16",
        "user@100.64.0.0/10",
        "user@fc00::/7",
        "user@fe80::/10",
    }
)


class LinuxSshError(RuntimeError):
    """A fixed Linux SSH operation could not be completed safely."""


def validate_linux_ssh_payload(payload: Any) -> dict[str, bool]:
    if not isinstance(payload, dict) or set(payload) != {"enabled"}:
        raise LinuxSshError("SSH 设置只能包含 enabled 字段")
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise LinuxSshError("SSH enabled 必须是布尔值")
    return {"enabled": enabled}


def _base_management_boundary_trusted() -> bool:
    return bool(
        _linux_trusted_system_executable(LINUX_SYSTEMCTL)
        and _linux_plain_root_owned(Path("/etc"), directory=True, enforce_owner=True)
        and _linux_protected_directory_chain(Path("/etc"))
    )


def linux_ssh_management_supported(*, system_name: str | None = None) -> bool:
    """Advertise the fixed action even when openssh-server is not installed."""

    return bool(
        (system_name or platform.system()) == "Linux"
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
        and _base_management_boundary_trusted()
    )


def _fixed_ssh_boundary_trusted(*, require_dropin: bool) -> bool:
    if not all(
        _linux_plain_root_owned(path, directory=True, enforce_owner=True)
        and _linux_protected_directory_chain(path)
        for path in (SSH_CONFIG.parent, SSH_CONFIG_DIRECTORY)
    ):
        return False
    if not (
        _linux_plain_root_owned(SSH_CONFIG, directory=False, enforce_owner=True)
        and int(getattr(SSH_CONFIG.lstat(), "st_nlink", 0)) == 1
    ):
        return False
    try:
        info = SSH_MANAGED_DROPIN.lstat()
    except FileNotFoundError:
        return not require_dropin
    except OSError:
        return False
    return bool(
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and int(getattr(info, "st_uid", -1)) == 0
        and int(getattr(info, "st_nlink", 0)) == 1
        and info.st_mode & 0o022 == 0
        and info.st_size <= 16 * 1024
    )


def _listener_available(
    *, connections_factory: Callable[..., Any] = psutil.net_connections
) -> bool:
    try:
        connections = connections_factory(kind="tcp")
    except (OSError, psutil.Error):
        return False
    for connection in connections:
        if getattr(connection, "status", None) != psutil.CONN_LISTEN:
            continue
        local = getattr(connection, "laddr", None)
        port = getattr(local, "port", None)
        if port is None and isinstance(local, (tuple, list)) and len(local) >= 2:
            port = local[1]
        if port == LINUX_SSH_PORT:
            return True
    return False


def _account_available() -> bool:
    if _pwd is None:
        return False
    try:
        account = _pwd.getpwnam(LINUX_SSH_ACCOUNT)
    except (KeyError, OSError):
        return False
    return bool(int(account.pw_uid) > 0 and str(account.pw_dir).startswith("/"))


def linux_ssh_status(
    *,
    system_name: str | None = None,
    connections_factory: Callable[..., Any] = psutil.net_connections,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Return bounded, secret-free SSH readiness for heartbeat/UI display."""

    if (system_name or platform.system()) != "Linux":
        return {
            "schema_version": 1,
            "manageable": False,
            "installed": False,
            "account_available": False,
            "listening": False,
            "service_active": False,
            "socket_active": False,
            "managed_policy": False,
            "effective_policy": False,
            "port": LINUX_SSH_PORT,
            "account": LINUX_SSH_ACCOUNT,
            "access_scope": "private_sources_only",
            "reason": "linux_only",
        }
    manageable = linux_ssh_management_supported(system_name="Linux")
    installed = _linux_trusted_system_executable(SSHD)
    account_available = _account_available()
    managed_policy = installed and _managed_policy_content_matches()
    listening = bool(installed and _listener_available(connections_factory=connections_factory))
    service_active = bool(manageable and installed and _unit_active(LINUX_SSH_SERVICE, runner=runner))
    socket_active = bool(manageable and installed and _unit_active(LINUX_SSH_SOCKET, runner=runner))
    effective_policy = bool(
        managed_policy
        and listening
        and service_active
        and not socket_active
        and _effective_policy_matches(runner=runner)
    )
    if not manageable:
        reason = "management_boundary_unavailable"
    elif not installed:
        reason = "openssh_server_missing"
    elif not account_available:
        reason = "login_account_missing"
    elif socket_active:
        reason = "unmanaged_socket_activation"
    elif listening and not managed_policy:
        reason = "unmanaged_listener"
    elif listening and managed_policy and service_active and not effective_policy:
        reason = "policy_drift"
    elif listening and effective_policy:
        reason = "ready"
    elif listening:
        reason = "unverified_listener"
    else:
        reason = "service_stopped"
    return {
        "schema_version": 1,
        "manageable": manageable,
        "installed": installed,
        "account_available": account_available,
        "listening": listening,
        "service_active": service_active,
        "socket_active": socket_active,
        "managed_policy": managed_policy,
        "effective_policy": effective_policy,
        "port": LINUX_SSH_PORT,
        "account": LINUX_SSH_ACCOUNT,
        "access_scope": "private_sources_only",
        "reason": reason,
    }


def _write_policy_atomically(content: bytes = _POLICY) -> None:
    if not isinstance(content, bytes) or not content or len(content) > 16 * 1024:
        raise LinuxSshError("SSH 托管策略内容无效")
    if not _fixed_ssh_boundary_trusted(require_dropin=False):
        raise LinuxSshError("SSH 固定配置目录不安全，已拒绝修改")
    directory_fd = os.open(
        SSH_CONFIG_DIRECTORY,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    temporary_name = f".20-rigpilot-lan.{os.getpid()}"
    temporary_fd: int | None = None
    temporary_created = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        temporary_created = True
        view = memoryview(content)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fchmod(temporary_fd, 0o600)
        os.fchown(temporary_fd, 0, 0)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        if not _fixed_ssh_boundary_trusted(require_dropin=False):
            raise LinuxSshError("SSH 固定配置边界在写入时发生变化")
        os.replace(
            temporary_name,
            SSH_MANAGED_DROPIN.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_created = False
        os.fsync(directory_fd)
    finally:
        try:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                if temporary_created:
                    os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(directory_fd)


def _read_managed_policy() -> bytes | None:
    try:
        SSH_MANAGED_DROPIN.lstat()
    except FileNotFoundError:
        return None
    if not _fixed_ssh_boundary_trusted(require_dropin=True):
        raise LinuxSshError("现有 SSH 托管策略文件不安全，已拒绝覆盖")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(SSH_MANAGED_DROPIN, flags)
    try:
        info = os.fstat(fd)
        if info.st_size > 16 * 1024:
            raise LinuxSshError("现有 SSH 托管策略文件过大")
        chunks: list[bytes] = []
        remaining = 16 * 1024 + 1
        while remaining:
            chunk = os.read(fd, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > 16 * 1024:
            raise LinuxSshError("现有 SSH 托管策略文件过大")
        return value
    finally:
        os.close(fd)


def _managed_policy_content_matches() -> bool:
    """Return true only for RigPilot's exact root-owned policy bytes."""

    try:
        return _read_managed_policy() == _POLICY
    except (LinuxSshError, OSError):
        return False


def _restore_managed_policy(previous: bytes | None) -> None:
    """Best-effort transactional rollback confined to the fixed drop-in."""

    if previous is not None:
        _write_policy_atomically(previous)
        return
    try:
        SSH_MANAGED_DROPIN.lstat()
    except FileNotFoundError:
        return
    if not _fixed_ssh_boundary_trusted(require_dropin=True):
        raise LinuxSshError("SSH 托管策略回滚边界不安全")
    directory_fd = os.open(
        SSH_CONFIG_DIRECTORY,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.unlink(SSH_MANAGED_DROPIN.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _run_fixed(
    argv: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            argv,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=external_process_environment("Linux"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LinuxSshError("SSH 系统操作无法执行") from exc


def _unit_active(
    unit: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    if unit not in {LINUX_SSH_SERVICE, LINUX_SSH_SOCKET}:
        raise LinuxSshError("SSH systemd 单元不受支持")
    try:
        result = _run_fixed(
            [str(LINUX_SYSTEMCTL), "is-active", "--quiet", unit],
            runner=runner,
            timeout=10,
        )
    except LinuxSshError:
        return False
    return result.returncode == 0


def _service_boot_enabled(
    *, runner: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    result = _run_fixed(
        [str(LINUX_SYSTEMCTL), "is-enabled", "--", LINUX_SSH_SERVICE],
        runner=runner,
        timeout=10,
    )
    state = (result.stdout or "").strip()
    if state == "enabled" and result.returncode == 0:
        return True
    if state == "disabled" and result.returncode == 1:
        return False
    # Runtime-only, masked, static or unavailable units cannot be restored by
    # the fixed boot-enable vectors below. Do not guess their earlier state.
    raise LinuxSshError("SSH 服务开机启用状态无法安全确认，已拒绝更改")


def _wait_for_listener_state(
    expected: bool,
    *,
    connections_factory: Callable[..., Any],
    sleeper: Callable[[float], None],
    attempts: int = 10,
) -> bool:
    for attempt in range(attempts):
        if _listener_available(connections_factory=connections_factory) is expected:
            return True
        if attempt + 1 < attempts:
            sleeper(0.2)
    return False


def _validate_effective_policy(
    *, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> None:
    syntax = _run_fixed(
        [str(SSHD), "-t", "-f", str(SSH_CONFIG)], runner=runner
    )
    if syntax.returncode != 0:
        raise LinuxSshError("OpenSSH 配置校验失败，未启动 22 端口")
    checks = (
        (
            "user=user,addr=192.168.1.2,host=rigpilot",
            {"port": "22", "passwordauthentication": "yes"},
        ),
        (
            "user=user,addr=203.0.113.2,host=rigpilot",
            {"port": "22", "passwordauthentication": "no"},
        ),
        (
            "user=root,addr=192.168.1.2,host=rigpilot",
            {"port": "22", "permitrootlogin": "no"},
        ),
    )
    for connection, expected_values in checks:
        result = _run_fixed(
            [str(SSHD), "-T", "-C", connection, "-f", str(SSH_CONFIG)],
            runner=runner,
        )
        values: dict[str, list[str]] = {}
        for line in (result.stdout or "").splitlines():
            name, separator, value = line.partition(" ")
            normalized_name = name.casefold()
            if separator:
                values.setdefault(normalized_name, []).append(
                    value.strip().casefold()
                )
        if result.returncode != 0 or any(
            set(values.get(key, [])) != {expected}
            for key, expected in expected_values.items()
        ):
            raise LinuxSshError("OpenSSH 安全策略校验失败，未启动 22 端口")
        allow_users = {
            item
            for line in values.get("allowusers", [])
            for item in line.split()
        }
        if (
            not allow_users
            or not allow_users.issubset(_PRIVATE_ALLOW_USERS)
            or not {
                "user@10.0.0.0/8",
                "user@172.16.0.0/12",
                "user@192.168.0.0/16",
                "user@100.64.0.0/10",
            }.issubset(allow_users)
        ):
            raise LinuxSshError("OpenSSH 来源地址限制未生效，未启动 22 端口")


def _effective_policy_matches(
    *, runner: Callable[..., subprocess.CompletedProcess[str]]
) -> bool:
    """Probe the daemon's effective policy without trusting drop-in bytes alone."""

    try:
        _validate_effective_policy(runner=runner)
    except LinuxSshError:
        return False
    return True


def configure_linux_ssh(
    payload: Any,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    connections_factory: Callable[..., Any] = psutil.net_connections,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    values = validate_linux_ssh_payload(payload)
    if not linux_ssh_management_supported():
        raise LinuxSshError("当前 Linux Agent 不具备安全管理 SSH 22 端口的权限")
    if not _linux_trusted_system_executable(SSHD):
        if not values["enabled"]:
            return {
                "enabled": False,
                "listening": False,
                "already_absent": True,
                "port": LINUX_SSH_PORT,
            }
        raise LinuxSshError(
            "未安装 openssh-server。请在设备本地执行：sudo apt-get update && "
            "sudo apt-get install --no-install-recommends openssh-server，然后重试。"
        )
    socket_active = _run_fixed(
        [str(LINUX_SYSTEMCTL), "is-active", "--quiet", LINUX_SSH_SOCKET],
        runner=runner,
        timeout=10,
    ).returncode == 0
    if socket_active:
        raise LinuxSshError(
            "检测到用户管理的 ssh.socket 正在运行；RigPilot 不会修改该 socket，请先在设备本地处理"
        )
    previous = _read_managed_policy()
    if previous is not None and previous != _POLICY:
        raise LinuxSshError("现有 SSH 托管配置不是 RigPilot 固定策略，已拒绝覆盖或关闭")

    service_active = _run_fixed(
        [str(LINUX_SYSTEMCTL), "is-active", "--quiet", LINUX_SSH_SERVICE],
        runner=runner,
        timeout=10,
    ).returncode == 0
    listener_active = _listener_available(connections_factory=connections_factory)
    if listener_active and not service_active:
        raise LinuxSshError("检测到非 ssh.service 的 22 端口监听，已拒绝接管或关闭")

    if not values["enabled"]:
        if previous != _POLICY:
            raise LinuxSshError("当前 SSH 22 不是由 RigPilot 精确策略管理，已拒绝关闭")
        _validate_effective_policy(runner=runner)
        stopped = _run_fixed(
            [str(LINUX_SYSTEMCTL), "disable", "--now", LINUX_SSH_SERVICE],
            runner=runner,
        )
        if stopped.returncode != 0:
            raise LinuxSshError("无法停止并禁用 RigPilot 管理的 Linux SSH 服务")
        still_active = _run_fixed(
            [str(LINUX_SYSTEMCTL), "is-active", "--quiet", LINUX_SSH_SERVICE],
            runner=runner,
            timeout=10,
        ).returncode == 0
        listener_stopped = _wait_for_listener_state(
            False,
            connections_factory=connections_factory,
            sleeper=sleeper,
        )
        if still_active or not listener_stopped:
            raise LinuxSshError("SSH 服务已执行关闭，但实际 22 端口仍在监听，请本地检查")
        return {"enabled": False, "listening": False, "port": LINUX_SSH_PORT}

    if not _account_available():
        raise LinuxSshError("设备不存在可用的非 root 本地 user 账号，无法安全启用 SSH")
    if previous is None and (service_active or listener_active):
        raise LinuxSshError("检测到未受 RigPilot 精确策略管理的 SSH 服务或 22 端口，已拒绝接管")
    was_active = service_active
    was_enabled = _service_boot_enabled(runner=runner)
    try:
        if previous is None:
            _write_policy_atomically()
        _validate_effective_policy(runner=runner)
        started = _run_fixed(
            [str(LINUX_SYSTEMCTL), "enable", "--now", LINUX_SSH_SERVICE],
            runner=runner,
        )
        if started.returncode != 0:
            raise LinuxSshError("OpenSSH 安全策略已写入，但 SSH 服务启动失败")
        reloaded = _run_fixed(
            [str(LINUX_SYSTEMCTL), "reload-or-restart", LINUX_SSH_SERVICE],
            runner=runner,
        )
        active = _run_fixed(
            [str(LINUX_SYSTEMCTL), "is-active", "--quiet", LINUX_SSH_SERVICE],
            runner=runner,
        )
        socket_became_active = _run_fixed(
            [str(LINUX_SYSTEMCTL), "is-active", "--quiet", LINUX_SSH_SOCKET],
            runner=runner,
            timeout=10,
        ).returncode == 0
        listener_ready = _wait_for_listener_state(
            True,
            connections_factory=connections_factory,
            sleeper=sleeper,
        )
        if (
            reloaded.returncode != 0
            or active.returncode != 0
            or socket_became_active
            or not listener_ready
        ):
            raise LinuxSshError("OpenSSH 安全策略已写入，但 SSH 服务未能稳定运行")
    except Exception:
        try:
            _restore_managed_policy(previous)
        except Exception as rollback_exc:
            raise LinuxSshError("SSH 启用失败，且固定策略无法安全回滚") from rollback_exc
        # Boot enablement and current activity are independent. Restore both
        # with separate fixed vectors; one rollback failure must not skip the
        # other, and the original enable error remains the reported cause.
        rollback_commands = (
            [str(LINUX_SYSTEMCTL), "enable" if was_enabled else "disable", "--", LINUX_SSH_SERVICE],
            [str(LINUX_SYSTEMCTL), "reload-or-restart" if was_active else "stop", LINUX_SSH_SERVICE],
        )
        for rollback_argv in rollback_commands:
            try:
                _run_fixed(rollback_argv, runner=runner)
            except LinuxSshError:
                pass
        raise
    return {
        "enabled": True,
        "listening": True,
        "port": LINUX_SSH_PORT,
        "account": LINUX_SSH_ACCOUNT,
        "access_scope": "private_sources_only",
        "firewall_changed": False,
    }
