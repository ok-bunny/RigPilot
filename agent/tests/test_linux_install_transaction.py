"""Exercise real installer cleanup and policy dispatch in temporary directories."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest


INSTALLER = Path(__file__).resolve().parents[2] / "deploy/linux/portable/Install.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is unavailable")


def _function(source: str, name: str) -> str:
    match = re.search(r"(?ms)^" + re.escape(name) + r"\(\) \{\n.*?^\}", source)
    assert match is not None, name
    return match.group(0)


def _lan_policy_case(source: str, *, post_commit: bool = False) -> tuple[int, str]:
    entry = 'case "${RIGPILOT_LAN_SSH:-enable}" in'
    start = source.rindex(entry) if post_commit else source.index(entry)
    end = source.index("\nesac", start) + len("\nesac")
    return start, source[start:end]


def _run(script: str, policy: str | None = None) -> subprocess.CompletedProcess[str]:
    # Do not inherit BASH_ENV, credentials, or the real host's installer policy.
    env = {"PATH": os.defpath, "LC_ALL": "C"}
    if policy is not None:
        env["RIGPILOT_LAN_SSH"] = policy
    return subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, env=env, timeout=5
    )


@pytest.mark.parametrize(
    "committed,slot_created,exit_code,slot_retained,timer_was_active",
    [(1, 1, 17, True, 1), (0, 1, 17, False, 1),
     (1, 1, 0, True, 1), (0, 0, 17, True, 0)],
)
def test_finish_install_retains_only_committed_or_existing_slots(
    tmp_path: Path, committed: int, slot_created: int, exit_code: int,
    slot_retained: bool, timer_was_active: int
):
    source = INSTALLER.read_text(encoding="utf-8")
    app = tmp_path / "app"
    slot = app / "versions" / "9.9.9"
    slot.mkdir(parents=True)
    binary = slot / "RigPilotAgent"
    binary.write_bytes(b"installed agent")
    rollback = app / ".install-rollback.test"
    rollback.mkdir()
    config_stage = tmp_path / "config-stage"
    supervision_stage = tmp_path / "supervision-stage"
    config_stage.write_text("staged config")
    supervision_stage.write_text("staged supervision")
    active = app / "active-version"
    active.write_text("9.9.9\n")
    log = tmp_path / "mock-calls"
    values = {
        "APP_DIR": app,
        "SLOT": slot,
        "ROLLBACK_DIR": rollback,
        "CONFIG_STAGE": config_stage,
        "SUPERVISION_STAGE": supervision_stage,
        "ACTIVE_VERSION_FILE": active,
        "MOCK_LOG": log,
        "SERVICE_DROPIN_DIR": tmp_path / "service.d",
        **{name: tmp_path / name for name in (
            "SERVICE_FILE", "SUPERVISION_DROPIN", "REMOTE_SESSION_DROPIN",
            "LEGACY_WIFI_DROPIN", "RECOVERY_SERVICE", "RECOVERY_TIMER", "CONFIG_FILE",
        )},
        "COMMITTED": committed,
        "SLOT_CREATED": slot_created,
        "CHANGES_STARTED": 1,
        "SERVICE_WAS_ACTIVE": 1,
        "SERVICE_WAS_ENABLED": 1,
        "TIMER_WAS_ENABLED": 1,
        "TIMER_WAS_ACTIVE": timer_was_active,
    }
    assignments = "\n".join(
        f"{name}={shlex.quote(str(value))}" for name, value in values.items()
    )
    script = (
        "set -Eeuo pipefail\n" + assignments + "\n"
        + 'systemctl() { printf "systemctl %s\\n" "$*" >>"${MOCK_LOG}"; }\n'
        + 'restore_file() { printf "restore %s\\n" "$*" >>"${MOCK_LOG}"; }\n'
        + 'install() { printf "install %s\\n" "$*" >>"${MOCK_LOG}"; }\n'
        + _function(source, "warn_registered_agent_offline") + "\n"
        + _function(source, "finish_install") + "\n"
        + f"trap finish_install EXIT\nexit {exit_code}\n"
    )
    result = _run(script)
    assert result.returncode == exit_code, result.stderr
    assert slot.exists() is slot_retained
    if slot_retained:
        assert binary.read_bytes() == b"installed agent"
        assert active.read_text() == "9.9.9\n"
    assert not rollback.exists()
    assert not config_stage.exists() and not supervision_stage.exists()
    calls = log.read_text() if log.exists() else ""
    if not committed and exit_code:
        assert "systemctl stop rigpilot-agent.service" in calls
        assert "systemctl start rigpilot-agent.service" in calls
        timer_action = "start" if timer_was_active else "stop"
        assert f"systemctl {timer_action} rigpilot-agent-update-recovery.timer" in calls
        assert f"restore {app}/Setup-HiveOS-Runtime.sh hiveos-setup 0755" in calls
    else:
        assert calls == ""


@pytest.mark.parametrize("policy", [None, "", "enable", "preserve", "disable", "typo"])
def test_lan_ssh_policy_is_checked_before_installation_writes(tmp_path: Path, policy):
    source = INSTALLER.read_text(encoding="utf-8")
    start, preflight = _lan_policy_case(source)
    assert start < source.index("\nexec 9>/run/lock/rigpilot-agent-install.lock")
    assert start < source.index('install -d -m 0755 -o root -g root "${APP_DIR}"')
    mutation = tmp_path / "installation-write"
    result = _run(
        "set -Eeuo pipefail\n" + _function(source, "fail") + "\n" + preflight
        + f"\nprintf started >{shlex.quote(str(mutation))}\n",
        policy,
    )
    valid = policy in (None, "", "enable", "preserve")
    assert result.returncode == (0 if valid else 1), result.stderr
    assert mutation.exists() is valid
    if not valid:
        assert "RIGPILOT_LAN_SSH must be enable or preserve" in result.stderr


@pytest.mark.parametrize("policy", [None, "", "enable", "preserve"])
def test_post_commit_optional_ssh_failure_does_not_fail_installation(tmp_path: Path, policy):
    source = INSTALLER.read_text(encoding="utf-8")
    start, dispatch = _lan_policy_case(source, post_commit=True)
    assert start > source.index("\nCOMMITTED=1\n")
    calls = tmp_path / "ssh-calls"
    result = _run(
        "set -Eeuo pipefail\n" + _function(source, "fail") + "\n"
        + f"SSH_CALLS={shlex.quote(str(calls))}\n"
        + 'configure_lan_openssh() { printf called >"${SSH_CALLS}"; return 23; }\n'
        + dispatch + "\nprintf complete\n",
        policy,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "complete"
    assert calls.exists() is (policy != "preserve")
