#!/usr/bin/env python3
"""Verify an unregistered RigPilotOS image through an offline serial console."""

from __future__ import annotations

import argparse
import base64
import os
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path
from runpy import run_path


SUCCESS = b"RIGPILOTOS_UNREGISTERED_VERIFY_OK"
FAILED = b"RIGPILOTOS_UNREGISTERED_VERIFY_FAILED"
EXPECTED_IMAGE_RELEASE = run_path(str(Path(__file__).with_name("appliance_metadata.py")))["IMAGE_RELEASE"]
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
BUILD_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


CONSOLE = run_path(str(Path(__file__).with_name("appliance_console.py")))
ApplianceSession = run_path(str(Path(__file__).with_name("appliance_session.py")))["ApplianceSession"]
wait_for = CONSOLE["wait_for"]
connect = CONSOLE["connect_serial"]


def wait_for_result(stream: socket.socket, success: bytes, deadline: float) -> bytes:
    return CONSOLE["wait_for_result"](stream, success, FAILED, deadline)

POST_RESULT_DONE = b"\nRIGPILOTOS_POST_RESULT_DIAGNOSTICS_DONE"


def collect_optional_post_result_diagnostics(
    stream: socket.socket, command: bytes, deadline: float
) -> bytes:
    """Collect bounded extra evidence without changing a finished boot gate."""
    if deadline - time.monotonic() <= 0.5:
        return b"\nRIGPILOTOS_POST_RESULT_DIAGNOSTICS_SKIPPED\n"
    diagnostics_deadline = min(deadline, time.monotonic() + 5.0)
    try:
        stream.sendall(command)
    except OSError:
        return b"\nRIGPILOTOS_POST_RESULT_DIAGNOSTICS_SEND_FAILED\n"
    received = bytearray()
    while time.monotonic() < diagnostics_deadline:
        try:
            chunk = stream.recv(65536)
        except TimeoutError:
            continue
        except OSError:
            break
        if not chunk:
            break
        received.extend(chunk)
        if POST_RESULT_DONE in received:
            return bytes(received)
        if len(received) > 4 * 1024 * 1024:
            del received[: len(received) - 2 * 1024 * 1024]
    return bytes(received) + b"\nRIGPILOTOS_POST_RESULT_DIAGNOSTICS_INCOMPLETE\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--format", choices=("raw", "qcow2"), required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--image-release", required=True)
    parser.add_argument("--agent-version", required=True)
    parser.add_argument("--build-revision", required=True)
    parser.add_argument(
        "--machine",
        choices=(
            "pc,accel=tcg",
            "q35,accel=tcg",
            "pc,accel=kvm",
            "q35,accel=kvm",
        ),
        default="pc,accel=tcg",
        help="QEMU machine used for the offline boot verification",
    )
    parser.add_argument("--port", type=int, default=24788)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--writable", action="store_true")
    parser.add_argument("--expect-kernel-arg")
    parser.add_argument("--reject-kernel-arg")
    parser.add_argument("--schedule-next-entry")
    args = parser.parse_args()

    if VERSION_RE.fullmatch(args.image_release) is None:
        raise RuntimeError("image release is invalid")
    if args.image_release != EXPECTED_IMAGE_RELEASE:
        raise RuntimeError(
            f"image release must be {EXPECTED_IMAGE_RELEASE} for this verifier"
        )
    if VERSION_RE.fullmatch(args.agent_version) is None:
        raise RuntimeError("Agent version is invalid")
    if tuple(map(int, args.agent_version.split("."))) < (0, 8, 22):
        raise RuntimeError("wired-only image requires Agent 0.8.22 or newer")
    if BUILD_REVISION_RE.fullmatch(args.build_revision) is None:
        raise RuntimeError("build revision is invalid")
    kernel_arg_re = re.compile(r"^[A-Za-z0-9_.=,+-]+$")
    for value in (args.expect_kernel_arg, args.reject_kernel_arg):
        if value and kernel_arg_re.fullmatch(value) is None:
            raise RuntimeError("kernel argument is invalid")
    if args.schedule_next_entry and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", args.schedule_next_entry
    ) is None:
        raise RuntimeError("GRUB entry id is invalid")
    success = (
        "RIGPILOTOS_UNREGISTERED_VERIFY_OK "
        f"image={args.image_release} revision={args.build_revision} "
        f"agent={args.agent_version}"
    ).encode("ascii")

    password = os.environ.get("RIGPILOTOS_TEST_PASSWORD")
    if not password:
        raise RuntimeError("RIGPILOTOS_TEST_PASSWORD is required")
    verification = r'''set -Eeuo pipefail
trap 'status=$?; echo RIGPILOTOS_UNREGISTERED_VERIFY_FAILED line=$LINENO status=$status' ERR
grep -qx 'ID=ubuntu' /etc/os-release
grep -Eq '^VERSION_ID="?24\.04"?$' /etc/os-release
grep -qx 'VERSION_CODENAME=noble' /etc/os-release
grep -qx 'NAME=RigPilotOS' /etc/rigpilotos-release
grep -qx 'VERSION=__IMAGE_RELEASE__' /etc/rigpilotos-release
grep -qx 'BUILD_REVISION=__BUILD_REVISION__' /etc/rigpilotos-release
grep -qx 'UBUNTU_VERSION=24.04' /etc/rigpilotos-release
grep -qx 'AGENT_SEED_VERSION=__AGENT_VERSION__' /etc/rigpilotos-release
grep -qx 'WIFI_SUPPORT=disabled' /etc/rigpilotos-release
retained_dkms=/var/cache/apt/archives/dkms_3.4.3-1ubuntu1_all.deb
test -f "$retained_dkms"
test ! -L "$retained_dkms"
test "$(stat -c '%a %u:%g' "$retained_dkms")" = '600 0:0'
printf '%s  %s\n' '6cb516fc913f1afbe8387078b357e15619e19291b9398e0b3cb6d0beae99e2b4' "$retained_dkms" | sha256sum --check --strict
! grep -q '^WPA_DEFAULT_START=' /etc/rigpilotos-release
grep -qx 'NVIDIA_DRIVER_BRANCH=615' /etc/rigpilotos-release
grep -qx 'NVIDIA_KERNEL_MODULE=open' /etc/rigpilotos-release
grep -qx 'PCI_BAR_RECOVERY_AVAILABLE=true' /etc/rigpilotos-release
grep -qx 'PCI_BAR_RECOVERY_DEFAULT=false' /etc/rigpilotos-release
default_kernel_path="$(readlink -f /boot/vmlinuz)"
case "$default_kernel_path" in
  /boot/vmlinuz-*) ;;
  *) false ;;
esac
default_kernel="${default_kernel_path#/boot/vmlinuz-}"
test -n "$default_kernel"
test "$(uname -r)" = "$default_kernel"
grep -qx "IMAGE_KERNEL=$default_kernel" /etc/rigpilotos-release
test -s "/boot/vmlinuz-$default_kernel"
test -s "/boot/initrd.img-$default_kernel"
nvidia_module_version="$(modinfo -k "$default_kernel" -F version nvidia | head -n 1)"
test "$nvidia_module_version" = '615.78.08'
grep -qx "NVIDIA_DRIVER_VERSION=$nvidia_module_version" /etc/rigpilotos-release
nvidia_vermagic="$(modinfo -k "$default_kernel" -F vermagic nvidia | head -n 1)"
case "$nvidia_vermagic" in
  "$default_kernel "*) ;;
  *) false ;;
esac
test "$(modinfo -k "$default_kernel" -F license nvidia | head -n 1)" = 'Dual MIT/GPL'
# 0.2.12+: the driver is run-managed. No NVIDIA deb may be installed, the
# CUDA repository must not be configured, and the apt preference guard must
# keep distribution packages from shadowing the run-installed files.
for package_name in \
  linux-image-generic \
  linux-headers-generic \
  linux-modules-extra-6.8.0-142-generic \
  dkms \
  systemd-resolved \
  openssh-server; do
  dpkg-query -W -f='${Status}\n' "$package_name" | grep -qx 'install ok installed'
done
for nvidia_deb in \
  nvidia-driver-615 nvidia-headless-open nvidia-headless-no-dkms-open \
  nvidia-dkms-open nvidia-dkms-615 nvidia-kernel-common \
  nvidia-kernel-source-open libnvidia-compute libnvidia-cfg1; do
  if dpkg-query -W -f='${Status}\n' "$nvidia_deb" 2>/dev/null | grep -qx 'install ok installed'; then
    echo "run-managed image must not carry NVIDIA deb: $nvidia_deb" >&2
    exit 1
  fi
done
test ! -e /etc/apt/sources.list.d/cuda-ubuntu2404-x86_64.list
test -f /etc/apt/preferences.d/99-rigpilot-nvidia-run-managed
grep -q 'Pin-Priority: -1' /etc/apt/preferences.d/99-rigpilot-nvidia-run-managed
grep -Fqw pcie_aspm=off /proc/cmdline
for package_name in wpasupplicant iw rfkill; do
  if dpkg-query -W -f='${Status}\n' "$package_name" 2>/dev/null | grep -qx 'install ok installed'; then
    echo "forbidden Wi-Fi package is installed: $package_name" >&2
    exit 1
  fi
done
test "$(dpkg-query -W -f='${Status}\t${Version}' wireless-regdb)" = $'install ok installed\t2026.05.30-0ubuntu1~24.04.1'
for forbidden_wifi_runtime in /usr/libexec/rigpilotos-wifi-bootstrap /usr/lib/systemd/system/wpa_supplicant.service /etc/systemd/system/rigpilotos-wifi-bootstrap.service /etc/systemd/system/rigpilotos-wifi-bootstrap.path /etc/systemd/system/rigpilotos-wifi-bootstrap.timer; do
  test ! -e "$forbidden_wifi_runtime"
  test ! -L "$forbidden_wifi_runtime"
done
echo RIGPILOTOS_CHECK_PLATFORM_BASELINE_OK
seed=/usr/lib/rigpilot-bootstrap/package
test "$(cat "$seed/VERSION")" = __AGENT_VERSION__
(cd "$seed" && sha256sum --check --strict SHA256SUMS)
test "$("$seed/RigPilotAgent" --version)" = 'RigPilot Agent __AGENT_VERSION__'
echo RIGPILOTOS_CHECK_AGENT_SEED_OK
for forbidden_path in \
  /var/lib/rigpilot-agent/state.json \
  /var/lib/rigpilot-agent/.state.json.replacement-pending \
  /var/lib/rigpilot-agent/.state.json.enrollment-pending \
  /var/lib/rigpilot-agent/firstboot-registration.json \
  /etc/rigpilot-agent/config.json \
  /etc/netplan/20-rigpilot-wifi.yaml \
  /run/rigpilot-provision/rigpilot-wifi.json \
  /run/rigpilot-provision/.rigpilot-enrollment-retry \
  /run/rigpilot-provision/rigpilot-enroll.json \
  /run/rigpilot-provision/rigpilot-replace.json; do
  test ! -e "$forbidden_path"
  test ! -L "$forbidden_path"
done
test ! -L /var/lib/rigpilot-agent
test ! -e /var/lib/rigpilot-agent || test -d /var/lib/rigpilot-agent
unfinished_state_transaction=""
if test -d /var/lib/rigpilot-agent; then
  unfinished_state_transaction="$(find /var/lib/rigpilot-agent -maxdepth 1 \
    -name '.state.json.*.tmp' -print -quit)"
fi
printf 'RIGPILOTOS_UNFINISHED_STATE_TRANSACTION=%q\n' "$unfinished_state_transaction"
test -z "$unfinished_state_transaction"
test ! -L /etc/rigpilot-agent
test ! -e /etc/rigpilot-agent || test -d /etc/rigpilot-agent
unfinished_config_transaction=""
if test -d /etc/rigpilot-agent; then
  unfinished_config_transaction="$(find /etc/rigpilot-agent -maxdepth 1 \
    -name '.config.json.appliance-*' -print -quit)"
fi
printf 'RIGPILOTOS_UNFINISHED_CONFIG_TRANSACTION=%q\n' "$unfinished_config_transaction"
test -z "$unfinished_config_transaction"
unfinished_journal_transaction=""
if test -d /var/lib/rigpilot-agent; then
  unfinished_journal_transaction="$(find /var/lib/rigpilot-agent -maxdepth 1 \
    -name '.firstboot-registration.json.appliance-*' -print -quit)"
fi
printf 'RIGPILOTOS_UNFINISHED_JOURNAL_TRANSACTION=%q\n' "$unfinished_journal_transaction"
test -z "$unfinished_journal_transaction"
echo RIGPILOTOS_CHECK_IDENTITY_CLEAN_OK
for unit in \
  rigpilotos-nvidia-guard.service \
  rigpilotos-ssh-host-keys.service \
  rigpilot-firstboot-enroll.service \
  rigpilot-firstboot-enroll.path \
  rigpilot-firstboot-enroll.timer \
  rigpilotos-smoke-ready.service \
  rigpilotos-grow-root.service; do
  systemctl is-enabled --quiet "$unit"
  if systemctl is-failed --quiet "$unit"; then
    echo "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED line=$LINENO status=1 reason=RIGPILOTOS_FAILED_UNIT $unit" >&2
    exit 1
  fi
  echo "RIGPILOTOS_CHECK_UNIT_OK $unit"
done
dpkg-query -W -f='${Status}\n' openssh-server | grep -qx 'install ok installed'
systemctl is-enabled --quiet ssh.service
if systemctl is-enabled --quiet ssh.socket; then
  echo "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED line=$LINENO status=1 reason=RIGPILOTOS_UNEXPECTED_SSH_SOCKET" >&2
  exit 1
fi
# This oneshot only guarantees that keys exist before ssh.service starts.
# Depending on whether keys already existed at boot, systemd may report the
# helper as active/exited or inactive/dead; neither is an error.
if systemctl is-failed --quiet rigpilotos-ssh-host-keys.service; then
  echo "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED line=$LINENO status=1 reason=RIGPILOTOS_FAILED_SSH_HOST_KEYS" >&2
  exit 1
fi
# Getty can accept a console login before multi-user startup finishes. Wait
# only for the already-enabled SSH service and first-boot keys; never start or
# repair a broken service from an acceptance test.
ssh_ready=0
# This is guest-local evidence only. Require sshd itself to own a non-loopback
# bind; physical LAN reachability needs a separate host-to-guest SSH gate.
ssh_local_listener_ready() {
  ss -ltnpH '( sport = :22 )' | awk '
    index($0, "\"sshd\"") > 0 &&
    $4 ~ /:22$/ &&
    $4 !~ /^127\./ &&
    $4 !~ /^\[?::1\]?:22$/ &&
    tolower($4) !~ /^\[?::ffff:127\./ &&
    tolower($4) !~ /^\[?::ffff:7f[0-9a-f][0-9a-f]:/ { found = 1 }
    END { exit !found }
  '
}
for attempt in {1..45}; do
  if systemctl is-active --quiet ssh.service && \
     ssh_local_listener_ready && \
     test -s /etc/ssh/ssh_host_ed25519_key; then
    ssh_ready=1
    break
  fi
  if systemctl is-failed --quiet ssh.service || \
     systemctl is-failed --quiet rigpilotos-ssh-host-keys.service; then
    break
  fi
  sleep 2
done
if test "$ssh_ready" != 1; then
  echo RIGPILOTOS_SSH_READY_FAILED >&2
  ss -ltnpH '( sport = :22 )' || true
  systemctl show -p Id -p LoadState -p UnitFileState -p ActiveState \
    -p SubState -p Result -p ConditionResult -p ExecMainStatus \
    ssh.service ssh.socket rigpilotos-ssh-host-keys.service --no-pager || true
  systemctl status ssh.service ssh.socket rigpilotos-ssh-host-keys.service \
    --no-pager -l || true
  journalctl -b --no-pager -o short-monotonic -n 100 \
    -u ssh.service -u ssh.socket -u rigpilotos-ssh-host-keys.service || true
  stat -c '%a %u:%g %s %n' /etc/ssh/ssh_host_ed25519_key 2>&1 || true
  exit 1
fi
echo RIGPILOTOS_CHECK_SSH_GUEST_LOCAL_READY_OK
test "$(stat -c '%a %U:%G' /etc/ssh/sshd_config.d/20-rigpilot-lan.conf)" = '600 root:root'
sshd -t
ssh_allow_users_expected="$(printf '%s\n' \
  user@10.0.0.0/8 user@172.16.0.0/12 user@192.168.0.0/16 \
  user@100.64.0.0/10 | sort)"
ssh_allow_users_actual="$(sshd -T | awk \
  '$1 == "allowusers" { for (i = 2; i <= NF; i++) { n = split($i, a, ","); for (j = 1; j <= n; j++) if (a[j] != "") print a[j] } }' | \
  sort -u)"
printf 'RIGPILOTOS_SSH_ALLOWUSERS_ACTUAL=%q\n' "$ssh_allow_users_actual"
test "$ssh_allow_users_actual" = "$ssh_allow_users_expected"
sshd -T | grep -x 'kbdinteractiveauthentication no' >/dev/null
sshd -T -C user=user,addr=192.168.1.2,host=rigpilot | grep -x 'passwordauthentication yes' >/dev/null
sshd -T -C user=user,addr=203.0.113.2,host=rigpilot | grep -x 'passwordauthentication no' >/dev/null
sshd -T -C user=root,addr=192.168.1.2,host=rigpilot | grep -x 'permitrootlogin no' >/dev/null
echo RIGPILOTOS_CHECK_SSH_CONFIG_POLICY_OK
systemctl is-active --quiet systemd-networkd.service
echo RIGPILOTOS_CHECK_NETWORKD_LOCAL_OK
test -L /usr/sbin/netplan
test "$(readlink /usr/sbin/netplan)" = '../share/netplan/netplan.script'
test "$(readlink -f /usr/sbin/netplan)" = /usr/share/netplan/netplan.script
test -f /usr/share/netplan/netplan.script
test -x /usr/share/netplan/netplan.script
test "$(stat -Lc '%u' /usr/sbin/netplan)" = 0
test "$(stat -c '%u' /usr/share/netplan/netplan.script)" = 0
test -z "$(find /usr/share/netplan/netplan.script -maxdepth 0 -perm /022 -print)"
echo RIGPILOTOS_CHECK_NETPLAN_TRUST_OK
test "$(systemctl show -p Result --value rigpilotos-smoke-ready.service)" = success
echo RIGPILOTOS_CHECK_SMOKE_READY_OK
test "$(systemctl show -p Result --value rigpilotos-grow-root.service)" = success
echo RIGPILOTOS_CHECK_GROW_ROOT_OK
systemctl is-active --quiet rigpilotos-nvidia-guard.service
echo RIGPILOTOS_CHECK_NVIDIA_GUARD_OK
systemctl is-enabled --quiet nvidia-persistenced.service
echo RIGPILOTOS_CHECK_NVIDIA_PERSISTENCED_OK
test -x /usr/libexec/rigpilotos-pci-bar
test -z "$(find /usr/libexec \( -type d -name __pycache__ -o -type f \( -name '*.pyc' -o -name '*.pyo' \) \) -print -quit)"
test -x /etc/grub.d/41_rigpilotos_pci_recovery
grep -q -- '--id rigpilotos-pci-realloc' /boot/grub/grub.cfg
grep -q -- '--id rigpilotos-pci-nocrs' /boot/grub/grub.cfg
grep -qx 'GRUB_DEFAULT=0' /etc/default/grub
grub_environment="$(grub-editenv /boot/grub/grubenv list)"
if printf '%s\n' "$grub_environment" | grep -q '^next_entry='; then
  echo "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED line=$LINENO status=1 reason=RIGPILOTOS_UNEXPECTED_NEXT_BOOT_ENTRY" >&2
  exit 1
fi
kernel_command_line="$(cat /proc/cmdline)"
if printf '%s\n' "$kernel_command_line" | grep -qE '(^| )pci=(realloc|nocrs)'; then
  echo "RIGPILOTOS_UNREGISTERED_VERIFY_FAILED line=$LINENO status=1 reason=RIGPILOTOS_UNEXPECTED_DEFAULT_PCI_OVERRIDE" >&2
  exit 1
fi
echo RIGPILOTOS_CHECK_PCI_BAR_RECOVERY_OK
test "$(timedatectl show -p Timezone --value)" = Asia/Shanghai
echo RIGPILOTOS_CHECK_TIMEZONE_OK
__KERNEL_ARG_CHECKS__
__SCHEDULE_CHECK__
sync
echo RIGPILOTOS_UNREGISTERED_VERIFY_OK image=__IMAGE_RELEASE__ revision=__BUILD_REVISION__ agent=__AGENT_VERSION__
'''
    verification = verification.replace("__IMAGE_RELEASE__", args.image_release)
    verification = verification.replace("__BUILD_REVISION__", args.build_revision)
    verification = verification.replace("__AGENT_VERSION__", args.agent_version)
    kernel_arg_checks: list[str] = []
    if args.expect_kernel_arg:
        quoted = shlex.quote(args.expect_kernel_arg)
        kernel_arg_checks.append(
            f"grep -Fqw -- {quoted} /proc/cmdline"
        )
    if args.reject_kernel_arg:
        quoted = shlex.quote(args.reject_kernel_arg)
        kernel_arg_checks.append(
            f"! grep -Fqw -- {quoted} /proc/cmdline"
        )
    verification = verification.replace(
        "__KERNEL_ARG_CHECKS__", "\n".join(kernel_arg_checks)
    )
    schedule_check = ""
    if args.schedule_next_entry:
        quoted_entry = shlex.quote(args.schedule_next_entry)
        schedule_check = (
            f"grub-reboot {quoted_entry}\n"
            "grub-editenv /boot/grub/grubenv list | "
            f"grep -Fx -- 'next_entry={args.schedule_next_entry}'"
        )
    verification = verification.replace("__SCHEDULE_CHECK__", schedule_check)
    encoded = base64.b64encode(verification.encode("utf-8")).decode("ascii")
    transcript = bytearray()
    stderr_path = args.transcript.with_suffix(args.transcript.suffix + ".qemu-stderr")
    args.transcript.parent.mkdir(parents=True, exist_ok=True)
    with ApplianceSession() as session:
        stderr_stream = session.open_stderr(stderr_path)
        cpu_model = "host" if args.machine.endswith("accel=kvm") else "max"
        qemu_arguments = [
            str(args.qemu.resolve(strict=True)),
            "-machine", args.machine, "-cpu", cpu_model, "-smp", "2", "-m", "2048",
            "-display", "none", "-monitor", "none",
            "-netdev", "user,restrict=on,id=offline0",
            "-device", "virtio-net-pci,netdev=offline0",
            "-no-reboot", "-serial", f"tcp:127.0.0.1:{args.port},server=on,wait=off",
            "-drive", f"file={args.image.resolve(strict=True)},if=virtio,format={args.format}",
        ]
        if not args.writable:
            qemu_arguments.insert(qemu_arguments.index("-netdev"), "-snapshot")
        process = session.start_process(
            qemu_arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_stream,
        )
        stream: socket.socket | None = None
        deadline = time.monotonic() + args.timeout
        try:
            stream = session.own_client(connect(args.port, deadline))
            transcript.extend(wait_for(stream, b"rigpilot login:", deadline))
            CONSOLE["authenticate"](stream, password, deadline, sudo=True, capture=transcript, wait=wait_for)
            lines = ["rm -f /tmp/rigpilotos-unregistered-verify.b64"]
            for offset in range(0, len(encoded), 3000):
                lines.append(
                    "printf '%s' '" + encoded[offset : offset + 3000]
                    + "' >>/tmp/rigpilotos-unregistered-verify.b64"
                )
            lines.append("base64 -d /tmp/rigpilotos-unregistered-verify.b64 | sudo /bin/bash")
            stream.sendall(("\n".join(lines) + "\n").encode("ascii"))
            result = wait_for_result(stream, success, deadline)
            transcript.extend(result)
            diagnostic_command = (b"\nsudo systemctl show -p Id -p Result -p ActiveState -p SubState -p ExecMainCode -p ExecMainStatus -p ExecMainStartTimestampMonotonic -p ExecMainExitTimestampMonotonic rigpilotos-smoke-ready.service rigpilotos-grow-root.service systemd-networkd.service systemd-resolved.service ssh.service ssh.socket rigpilotos-ssh-host-keys.service; sudo journalctl -b --no-pager -o short-monotonic -n 140 -u rigpilotos-smoke-ready.service -u rigpilotos-grow-root.service -u systemd-networkd.service -u systemd-resolved.service -u ssh.service -u ssh.socket -u rigpilotos-ssh-host-keys.service -u serial-getty@ttyS0.service; echo RIGPILOTOS_POST_RESULT_DIAGNOSTICS_DONE\n")
            transcript.extend(collect_optional_post_result_diagnostics(
                stream, diagnostic_command, deadline
            ))
            if not CONSOLE["verified_result"](result, success, FAILED):
                tail = result[-8000:].decode("utf-8", errors="replace")
                raise RuntimeError("offline unregistered verification failed:\n" + tail)
            print("offline unregistered appliance verification passed")
            return 0
        finally:
            args.transcript.write_bytes(transcript)


if __name__ == "__main__":
    raise SystemExit(main())
