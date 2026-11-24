from __future__ import annotations

import ast
import importlib.util
import time
from pathlib import Path
import subprocess

import pytest


APPLIANCE = Path(__file__).resolve().parents[1]
VERIFIERS = ("verify-unregistered-console.py", "verify-enrolled-console.py")
SSH_READY_MARKER = "RIGPILOTOS_CHECK_SSH_GUEST_LOCAL_READY_OK"


def verification_script(name: str = "verify-unregistered-console.py") -> str:
    tree = ast.parse((APPLIANCE / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if any(isinstance(target, ast.Name) and target.id == "verification" for target in node.targets):
                return node.value.value
    raise AssertionError("Embedded guest checks missing")


def run_fragment(fragment: str, mock: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", "set -Eeuo pipefail\n" + mock + "\n" + fragment],
                          text=True, capture_output=True, check=False, timeout=5)


def ssh_readiness_fragment(verifier: str) -> str:
    script = verification_script(verifier)
    start = script.index("ssh_ready=0")
    marker = "echo " + SSH_READY_MARKER
    return script[start:script.index(marker, start) + len(marker)]


@pytest.mark.parametrize("failed_unit", [None, "rigpilotos-smoke-ready.service", "rigpilot-firstboot-enroll.service"])
def test_failed_unit_stops_before_success_marker(failed_unit: str | None) -> None:
    script = verification_script()
    start = script.index("for unit in ")
    fragment = script[start:script.index("\ndone", start) + 5]
    mock = "systemctl() { if [[ \"$1\" == is-enabled ]]; then return 0; fi; [[ \"$3\" == \"" + (failed_unit or "none") + "\" ]]; }"
    result = run_fragment(fragment, mock)
    if failed_unit is None:
        assert result.returncode == 0
        assert "RIGPILOTOS_CHECK_UNIT_OK rigpilotos-smoke-ready.service" in result.stdout
    else:
        assert result.returncode == 1
        assert "RIGPILOTOS_CHECK_UNIT_OK " + failed_unit not in result.stdout
        assert "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED" in result.stderr


@pytest.mark.parametrize("enabled", [True, False])
def test_ssh_socket_must_be_disabled(enabled: bool) -> None:
    script = verification_script()
    start = script.index("if systemctl is-enabled --quiet ssh.socket; then")
    fragment = script[start:script.index("\nfi", start) + 3]
    result = run_fragment(fragment, "systemctl() { return " + ("0" if enabled else "1") + "; }")
    assert (result.returncode != 0) is enabled
    if enabled:
        assert "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED" in result.stderr


@pytest.mark.parametrize("output,status,accepted", [
    ("next_entry=rigpilotos-pci-realloc", 0, False),
    ("", 2, False),
    ("saved_entry=0", 0, True),
])
@pytest.mark.parametrize("verifier", VERIFIERS)
def test_grub_pending_override_or_query_failure_blocks_release(output: str, status: int, accepted: bool, verifier: str) -> None:
    script = verification_script(verifier)
    end = "kernel_command_line=" if verifier.startswith("verify-unregistered") else "ready=0"
    fragment = script[script.index("grub_environment="):script.index(end)]
    mock = "grub-editenv() { printf '%s\\n' '" + output + "'; return " + str(status) + "; }"
    result = run_fragment(fragment, mock)
    assert (result.returncode == 0) is accepted
    if output.startswith("next_entry=") and verifier.startswith("verify-unregistered"):
        assert "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED" in result.stderr


@pytest.mark.parametrize("command_line,accepted", [
    ("quiet console=ttyS0", True),
    ("quiet pci=realloc console=ttyS0", False),
    ("quiet pci=nocrs", False),
])
def test_default_kernel_recovery_override_blocks_release(command_line: str, accepted: bool) -> None:
    script = verification_script()
    start = script.index("kernel_command_line=")
    fragment = script[start:script.index("echo RIGPILOTOS_CHECK_PCI_BAR_RECOVERY_OK", start)]
    result = run_fragment(fragment, "cat() { printf '%s\\n' '" + command_line + "'; }")
    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED" in result.stderr


@pytest.mark.parametrize("listener", [
    'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=42,fd=3))',
    'LISTEN 0 128 [::ffff:10.0.2.15]:22 [::]:* users:(("sshd",pid=42,fd=3))',
])
@pytest.mark.parametrize("verifier", VERIFIERS)
def test_ssh_readiness_waits_for_delayed_service_without_starting_it(verifier: str, listener: str) -> None:
    fragment = ssh_readiness_fragment(verifier)
    mock = """ssh_calls=0
systemctl() {
  case "$1" in
    is-active) ssh_calls=$((ssh_calls + 1)); [[ "$ssh_calls" -ge 3 ]] ;;
    is-failed) return 1 ;;
    *) echo "unexpected systemctl action: $1" >&2; return 2 ;;
  esac
}
ss() { printf '%s\n' '__LISTENER__'; }
sleep() { :; }
test() {
  if [[ "$1" == -s && "$2" == /etc/ssh/ssh_host_ed25519_key ]]; then return 0; fi
  builtin test "$@"
}
"""
    result = run_fragment(fragment, mock.replace("__LISTENER__", listener))
    assert result.returncode == 0, result.stderr
    assert SSH_READY_MARKER in result.stdout
    assert "unexpected systemctl action" not in result.stderr


@pytest.mark.parametrize("verifier", VERIFIERS)
@pytest.mark.parametrize("state", ["failed", "inactive"])
def test_ssh_readiness_rejects_unavailable_service_with_diagnostics(verifier: str, state: str) -> None:
    fragment = ssh_readiness_fragment(verifier)
    mock = f"""systemctl() {{
  case "$1" in
    is-active) return 3 ;;
    is-failed) [[ "$3" == ssh.service && '{state}' == failed ]] ;;
    show|status) echo 'SSH_UNIT_STATE={state}' ;;
    *) return 2 ;;
  esac
}}
journalctl() {{ echo 'SSH_JOURNAL_DIAGNOSTIC'; }}
stat() {{ return 1; }}
sleep() {{ echo SSH_WAIT_TICK; }}
"""
    result = run_fragment(fragment, mock)
    assert result.returncode != 0
    assert "RIGPILOTOS_SSH_READY_FAILED" in result.stderr
    assert f"SSH_UNIT_STATE={state}" in result.stdout
    assert "SSH_JOURNAL_DIAGNOSTIC" in result.stdout
    assert SSH_READY_MARKER not in result.stdout
    assert ("SSH_WAIT_TICK" in result.stdout) is (state == "inactive")


@pytest.mark.parametrize("verifier", VERIFIERS)
@pytest.mark.parametrize("listener", [
    'LISTEN 0 128 127.0.0.1:22 0.0.0.0:* users:(("sshd",pid=42,fd=3))',
    'LISTEN 0 128 [::1]:22 [::]:* users:(("sshd",pid=42,fd=3))',
    'LISTEN 0 128 [::ffff:127.0.0.1]:22 [::]:* users:(("sshd",pid=42,fd=3))',
    'LISTEN 0 128 [::ffff:127.12.34.56]:22 [::]:* users:(("sshd",pid=42,fd=3))',
    'LISTEN 0 128 [::FFFF:7F00:1]:22 [::]:* users:(("sshd",pid=42,fd=3))',
    'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("python3",pid=42,fd=3))',
])
def test_ssh_local_readiness_rejects_loopback_or_non_sshd_listener(listener: str, verifier: str) -> None:
    fragment = ssh_readiness_fragment(verifier)
    mock = f"""systemctl() {{
  case "$1" in
    is-active) return 0 ;;
    is-failed) return 1 ;;
    show|status) echo 'SSH_UNIT_STATE=active' ;;
    *) return 2 ;;
  esac
}}
ss() {{ printf '%s\n' '{listener}'; }}
sleep() {{ :; }}
journalctl() {{ :; }}
stat() {{ return 1; }}
test() {{
  if [[ "$1" == -s && "$2" == /etc/ssh/ssh_host_ed25519_key ]]; then return 0; fi
  builtin test "$@"
}}
"""
    result = run_fragment(fragment, mock)
    assert result.returncode != 0
    assert "RIGPILOTOS_SSH_READY_FAILED" in result.stderr
    assert SSH_READY_MARKER not in result.stdout


def verifier_module():
    path = APPLIANCE / "verify-unregistered-console.py"
    spec = importlib.util.spec_from_file_location("rigpilotos_ssh_console_runtime_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSerial:
    def __init__(self, chunks: list[bytes]):
        self.chunks = iter(chunks)
        self.sent: list[bytes] = []

    def sendall(self, value: bytes) -> None:
        self.sent.append(value)

    def recv(self, _: int) -> bytes:
        return next(self.chunks)


@pytest.mark.parametrize("scenario", ["expired", "disconnected", "complete"])
def test_optional_diagnostics_are_bounded_and_preserve_completion(scenario: str) -> None:
    module = verifier_module()
    chunks = {"expired": [], "disconnected": [b"partial diagnostics", b""],
              "complete": [module.POST_RESULT_DONE]}
    stream = FakeSerial(chunks[scenario])
    result = module.collect_optional_post_result_diagnostics(
        stream, b"diagnostics\n", time.monotonic() + (-1 if scenario == "expired" else 10)
    )
    assert stream.sent == ([] if scenario == "expired" else [b"diagnostics\n"])
    if scenario == "expired":
        assert b"DIAGNOSTICS_SKIPPED" in result
    elif scenario == "disconnected":
        assert b"partial diagnostics" in result
        assert b"DIAGNOSTICS_INCOMPLETE" in result
    else:
        assert module.POST_RESULT_DONE in result
        assert b"DIAGNOSTICS_INCOMPLETE" not in result
