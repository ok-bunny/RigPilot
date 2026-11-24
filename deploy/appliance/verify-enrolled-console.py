#!/usr/bin/env python3
"""Verify a disposable enrolled RigPilotOS image through its serial console."""

from __future__ import annotations

import argparse
import base64
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from runpy import run_path


SUCCESS = b"RIGPILOTOS_ENROLLED_VERIFY_OK"
FAILED = b"RIGPILOTOS_ENROLLED_VERIFY_FAILED"
EXPECTED_IMAGE_RELEASE = run_path(str(Path(__file__).with_name("appliance_metadata.py")))["IMAGE_RELEASE"]
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
BUILD_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


CONSOLE = run_path(str(Path(__file__).with_name("appliance_console.py")))
ApplianceSession = run_path(str(Path(__file__).with_name("appliance_session.py")))["ApplianceSession"]
wait_for = CONSOLE["wait_for"]
connect = CONSOLE["connect_serial"]


def wait_for_result(stream: socket.socket, success: bytes, deadline: float) -> bytes:
    return CONSOLE["wait_for_result"](stream, success, FAILED, deadline)


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
        help="QEMU machine used for the enrolled boot verification",
    )
    parser.add_argument("--port", type=int, default=24805)
    parser.add_argument("--timeout", type=int, default=600)
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
    password = os.environ.get("RIGPILOTOS_TEST_PASSWORD")
    if not password:
        raise RuntimeError("RIGPILOTOS_TEST_PASSWORD is required")

    success = (
        "RIGPILOTOS_ENROLLED_VERIFY_OK "
        f"image={args.image_release} revision={args.build_revision} "
        f"agent={args.agent_version}"
    ).encode("ascii")

    verification = r'''set -Eeuo pipefail
trap 'status=$?; echo RIGPILOTOS_ENROLLED_VERIFY_FAILED line=$LINENO status=$status' ERR
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
test -x /usr/libexec/rigpilotos-pci-bar
test -z "$(find /usr/libexec \( -type d -name __pycache__ -o -type f \( -name '*.pyc' -o -name '*.pyo' \) \) -print -quit)"
grep -q -- '--id rigpilotos-pci-realloc' /boot/grub/grub.cfg
grep -q -- '--id rigpilotos-pci-nocrs' /boot/grub/grub.cfg
grep -qx 'GRUB_DEFAULT=0' /etc/default/grub
grub_environment="$(grub-editenv /boot/grub/grubenv list)"
! printf '%s\n' "$grub_environment" | grep -q '^next_entry='
ready=0
# A first boot can spend roughly a minute installing the frozen Agent and
# applying systemd policy on slower USB media or under QEMU TCG.  Wait for the
# complete transaction instead of treating an in-progress install as failure.
for attempt in $(seq 1 60); do
  if test -s /var/lib/rigpilot-agent/state.json \
      && test -s /etc/rigpilot-agent/config.json \
      && test ! -e /run/rigpilot-provision/rigpilot-enroll.json \
      && systemctl is-active --quiet rigpilot-agent.service; then
    ready=1
    break
  fi
  sleep 2
done
if test "$ready" != 1; then
  echo "RIGPILOTOS_STATE_PRESENT=$(test -s /var/lib/rigpilot-agent/state.json && echo 1 || echo 0)"
  echo "RIGPILOTOS_CONFIG_PRESENT=$(test -s /etc/rigpilot-agent/config.json && echo 1 || echo 0)"
  echo "RIGPILOTOS_TOKEN_PRESENT=$(test -e /run/rigpilot-provision/rigpilot-enroll.json && echo 1 || echo 0)"
  echo "RIGPILOTOS_AGENT_ACTIVE=$(systemctl is-active --quiet rigpilot-agent.service && echo 1 || echo 0)"
  systemctl status rigpilot-firstboot-enroll.service --no-pager -l || true
  journalctl -b -u rigpilot-firstboot-enroll.service --no-pager -n 40 || true
fi
test "$ready" = 1
test -s /var/lib/rigpilot-agent/state.json
test -s /etc/rigpilot-agent/config.json
for forbidden_path in \
  /var/lib/rigpilot-agent/.state.json.replacement-pending \
  /var/lib/rigpilot-agent/.state.json.enrollment-pending \
  /var/lib/rigpilot-agent/firstboot-registration.json \
  /etc/netplan/20-rigpilot-wifi.yaml \
  /run/rigpilot-provision/rigpilot-wifi.json \
  /run/rigpilot-provision/.rigpilot-enrollment-retry \
  /run/rigpilot-provision/rigpilot-enroll.json \
  /run/rigpilot-provision/rigpilot-replace.json; do
  test ! -e "$forbidden_path"
  test ! -L "$forbidden_path"
done
test -z "$(find /var/lib/rigpilot-agent -maxdepth 1 \
  -name '.state.json.*.tmp' -print -quit 2>/dev/null)"
test -z "$(find /etc/rigpilot-agent -maxdepth 1 \
  -name '.config.json.appliance-*' -print -quit 2>/dev/null)"
test -z "$(find /var/lib/rigpilot-agent -maxdepth 1 \
  -name '.firstboot-registration.json.appliance-*' -print -quit 2>/dev/null)"
test "$(cat /opt/rigpilot-agent/active-version)" = __AGENT_VERSION__
test "$(/opt/rigpilot-agent/versions/__AGENT_VERSION__/RigPilotAgent --version)" = 'RigPilot Agent __AGENT_VERSION__'
systemctl is-active --quiet rigpilot-agent.service
agent_pid=$(systemctl show rigpilot-agent.service -p MainPID --value)
test -n "$agent_pid" && test "$agent_pid" != 0
test "$(readlink -f /proc/$agent_pid/exe)" = /opt/rigpilot-agent/versions/__AGENT_VERSION__/RigPilotAgent
python3 - <<'PY'
import json
with open('/etc/rigpilot-agent/config.json', encoding='utf-8-sig') as handle:
    value = json.load(handle)
# Retired with the Agent 0.8.58 feature cleanup (image 0.2.11): none of these
# feature keys may be committed by the appliance or reintroduced by Install.sh.
for name in (
    'allow_remote_sessions',
    'allow_remote_root_sessions',
    'allow_local_web',
    'local_web_port',
    'allow_nvidia_driver_updates',
    'allow_hiveos_runtime_preparation',
    'hiveos_max_archive_bytes',
    'hiveos_max_expanded_bytes',
    'hiveos_min_free_bytes',
):
    if name in value:
        raise SystemExit(f'retired feature key must be absent: {name}')
if 'allow_wifi_management' in value:
    raise SystemExit('Wi-Fi management configuration must be absent')
if 'enrollment_token' in value:
    raise SystemExit('enrollment token persisted in configuration')
PY
test ! -e /opt/rigpilot-agent/versions/__AGENT_VERSION__/rigpilot-agent-wifi.conf
test ! -L /opt/rigpilot-agent/versions/__AGENT_VERSION__/rigpilot-agent-wifi.conf
# 0.2.11 removal assertions: the retired Spark remote-session drop-in, the
# HiveOS environment and the driver-update/local-web entry points are gone.
test ! -e /etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf
test ! -L /etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf
test ! -e /opt/rigpilot-agent/Setup-HiveOS-Runtime.sh
test ! -e /var/lib/rigpilot-agent/hiveos-components
test ! -e /var/lib/rigpilot-agent/pending-hiveos-start.json
! getent passwd rigpilot-hiveos >/dev/null
! command -v bwrap >/dev/null
! dpkg-query -W -f='${Status}' ubuntu-drivers-common 2>/dev/null | grep -qx 'install ok installed'
! ss -ltnH '( sport = :4200 )' | grep -q .
# Without the retired remote-sessions drop-in the shipped unit's own sandbox
# applies again (Agent 0.8.58+ base unit hardening).
test "$(systemctl show rigpilot-agent.service -p NoNewPrivileges --value)" = yes
test "$(systemctl show rigpilot-agent.service -p ProtectHome --value)" = yes
test "$(systemctl show rigpilot-agent.service -p ProtectSystem --value)" = strict
remote_dropin=/etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf
supervision_dropin=/etc/systemd/system/rigpilot-agent.service.d/supervision.conf
dropin_paths="$(systemctl show rigpilot-agent.service -p DropInPaths --value)"
case " $dropin_paths " in
  *" $remote_dropin "*) false ;;
esac
case " $dropin_paths " in
  *" $supervision_dropin "*) ;;
  *) false ;;
esac
test -f "$supervision_dropin"
test "$(systemctl show rigpilot-agent.service -p Type --value)" = notify
test "$(systemctl show rigpilot-agent.service -p NotifyAccess --value)" = all
test "$(systemctl show rigpilot-agent.service -p Restart --value)" = on-failure
test "$(systemctl show rigpilot-agent.service -p KillMode --value)" = process
# Repeat the exact sandbox query from the Agent's mount namespace.  This
# catches a service sandbox that looks correct to a login shell but prevents
# the frozen Agent itself from reading PID 1's effective unit properties.
agent_policy="$(nsenter --target "$agent_pid" --mount -- \
  /usr/bin/systemctl show rigpilot-agent.service \
    --property=NoNewPrivileges --property=ProtectHome \
    --property=ProtectSystem --property=DropInPaths --no-pager)"
printf '%s\n' "$agent_policy" | grep -qx 'NoNewPrivileges=yes'
printf '%s\n' "$agent_policy" | grep -qx 'ProtectHome=yes'
printf '%s\n' "$agent_policy" | grep -qx 'ProtectSystem=strict'
printf '%s\n' "$agent_policy" | grep -Fqx "DropInPaths=$dropin_paths"
# Leave the enrolled VM online for one complete heartbeat interval so the
# controller can verify the final stable capability set rather than the
# intentionally minimal registration heartbeat.
sleep 35
# Structured remote access now travels only through the Agent's existing
# outbound HTTPS command channel.  LAN OpenSSH is a separate fixed-policy
# service shipped by the appliance image.
dpkg-query -W -f='${Status}\n' openssh-server | grep -qx 'install ok installed'
systemctl is-enabled --quiet rigpilotos-ssh-host-keys.service
! systemctl is-failed --quiet rigpilotos-ssh-host-keys.service
systemctl is-enabled --quiet ssh.service
! systemctl is-enabled --quiet ssh.socket
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
test "$(timedatectl show -p Timezone --value)" = Asia/Shanghai
systemctl is-active --quiet systemd-timesyncd.service
systemctl is-active --quiet rigpilotos-nvidia-guard.service
systemctl is-enabled --quiet nvidia-persistenced.service
test "$(journalctl -k -b --no-pager | grep -Fc 'NVRM: No NVIDIA GPU found' || true)" = 0
echo RIGPILOTOS_ENROLLED_VERIFY_OK image=__IMAGE_RELEASE__ revision=__BUILD_REVISION__ agent=__AGENT_VERSION__
'''
    verification = verification.replace("__IMAGE_RELEASE__", args.image_release)
    verification = verification.replace("__BUILD_REVISION__", args.build_revision)
    verification = verification.replace("__AGENT_VERSION__", args.agent_version)
    encoded = base64.b64encode(verification.encode("utf-8")).decode("ascii")

    args.transcript.parent.mkdir(parents=True, exist_ok=True)
    stderr_path = args.transcript.with_suffix(args.transcript.suffix + ".qemu-stderr")
    with ApplianceSession() as session:
        stderr_stream = session.open_stderr(stderr_path)
        cpu_model = "host" if args.machine.endswith("accel=kvm") else "max"
        process = session.start_process(
            [
                str(args.qemu.resolve(strict=True)),
                "-machine", args.machine, "-cpu", cpu_model, "-smp", "2", "-m", "2048",
                "-display", "none", "-monitor", "none", "-snapshot", "-no-reboot",
                "-serial", f"tcp:127.0.0.1:{args.port},server=on,wait=off",
                "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
                "-drive", f"file={args.image.resolve(strict=True)},if=virtio,format={args.format}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_stream,
        )
        stream: socket.socket | None = None
        transcript = bytearray()
        deadline = time.monotonic() + args.timeout
        try:
            stream = session.own_client(connect(args.port, deadline))
            for attempt in range(3):
                try:
                    transcript.extend(wait_for(stream, b"rigpilot login:", deadline))
                    break
                except (ConnectionResetError, ConnectionAbortedError):
                    stream.close()
                    session.client = None
                    if attempt == 2:
                        raise
                    # Windows QEMU can briefly reset a TCP serial client while
                    # the installed ext4 journal is replayed.  The serial server
                    # remains available and getty redraws its prompt on newline.
                    stream = session.own_client(connect(args.port, deadline))
                    stream.sendall(b"\n")
            CONSOLE["authenticate"](stream, password, deadline, sudo=True, capture=transcript, wait=wait_for)
            lines = ["rm -f /tmp/rigpilotos-enrolled-verify.b64"]
            for offset in range(0, len(encoded), 3000):
                lines.append(
                    "printf '%s' '" + encoded[offset : offset + 3000]
                    + "' >>/tmp/rigpilotos-enrolled-verify.b64"
                )
            lines.append("base64 -d /tmp/rigpilotos-enrolled-verify.b64 | sudo /bin/bash")
            stream.sendall(("\n".join(lines) + "\n").encode("ascii"))
            result = wait_for_result(stream, success, deadline)
            transcript.extend(result)
            if not CONSOLE["verified_result"](result, success, FAILED):
                tail = result[-8000:].decode("utf-8", errors="replace")
                raise RuntimeError("enrolled appliance verification failed:\n" + tail)
            print("enrolled appliance verification passed")
            return 0
        finally:
            args.transcript.write_bytes(transcript)


if __name__ == "__main__":
    raise SystemExit(main())
