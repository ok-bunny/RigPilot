#!/usr/bin/env python3
"""Exercise the installed appliance's serial-console login in QEMU.

The password is supplied through ``RIGPILOTOS_TEST_PASSWORD`` so it is not
placed in argv or persisted in a transcript.  This is a build/diagnostic tool;
it never modifies the image because QEMU runs it with ``-snapshot``.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from runpy import run_path


VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
EXPECTED_IMAGE_RELEASE = run_path(str(Path(__file__).with_name("appliance_metadata.py")))["IMAGE_RELEASE"]
BASELINE_SUCCESS = b"RIGPILOTOS_BASELINE_VERIFY_OK"
BASELINE_FAILED = b"RIGPILOTOS_BASELINE_VERIFY_FAILED"


CONSOLE = run_path(str(Path(__file__).with_name("appliance_console.py")))
ApplianceSession = run_path(str(Path(__file__).with_name("appliance_session.py")))["ApplianceSession"]
contains_exact_line = CONSOLE["contains_exact_line"]
wait_for = CONSOLE["wait_for"]
connect_serial = CONSOLE["connect_serial"]


def wait_for_baseline_result(stream: socket.socket, deadline: float) -> bytes:
    return CONSOLE["wait_for_result"](stream, BASELINE_SUCCESS, BASELINE_FAILED, deadline)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--port", type=int, default=24444)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--username", default="user")
    parser.add_argument("--format", choices=("raw", "qcow2"), default="raw")
    parser.add_argument("--diagnostics-output", type=Path)
    parser.add_argument("--image-release", required=True)
    parser.add_argument("--agent-version", required=True)
    parser.add_argument(
        "--machine",
        choices=(
            "pc,accel=tcg",
            "q35,accel=tcg",
            "pc,accel=kvm",
            "q35,accel=kvm",
        ),
        default="pc,accel=tcg",
        help="QEMU machine used for the console-login verification",
    )
    parser.add_argument(
        "--firmware",
        type=Path,
        help="optional read-only EDK2 code image for a UEFI boot check",
    )
    parser.add_argument(
        "--firmware-vars",
        type=Path,
        help="writable EDK2 variables image paired with --firmware",
    )
    parser.add_argument("--writable", action="store_true")
    parser.add_argument("--expect-enrolled", action="store_true")
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

    qemu = args.qemu.resolve(strict=True)
    image = args.image.resolve(strict=True)
    password = os.environ.get("RIGPILOTOS_TEST_PASSWORD")
    if not password:
        raise RuntimeError("RIGPILOTOS_TEST_PASSWORD is required")

    cpu_model = "host" if args.machine.endswith("accel=kvm") else "max"
    qemu_arguments = [
            str(qemu),
            "-machine",
            args.machine,
            "-cpu",
            cpu_model,
            "-smp",
            "2",
            "-m",
            "2048",
            "-display",
            "none",
            "-monitor",
            "none",
            "-no-reboot",
            "-serial",
            f"tcp:127.0.0.1:{args.port},server=on,wait=off",
            "-netdev",
            "user,id=net0",
            "-device",
            "virtio-net-pci,netdev=net0",
            "-drive",
            f"file={image},if=virtio,format={args.format}",
        ]
    if not args.writable:
        qemu_arguments.insert(qemu_arguments.index("-no-reboot"), "-snapshot")
    if (args.firmware is None) != (args.firmware_vars is None):
        parser.error("--firmware and --firmware-vars must be supplied together")
    if args.firmware is not None and args.firmware_vars is not None:
        firmware = args.firmware.resolve(strict=True)
        firmware_vars = args.firmware_vars.resolve(strict=True)
        qemu_arguments[1:1] = [
            "-drive",
            f"if=pflash,format=raw,unit=0,readonly=on,file={firmware}",
            "-drive",
            f"if=pflash,format=raw,unit=1,file={firmware_vars}",
        ]
    process = None
    try:
        with ApplianceSession() as session:
            process = session.start_process(
                qemu_arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            stream: socket.socket | None = None
            deadline = time.monotonic() + args.timeout
            stream = session.own_client(connect_serial(args.port, deadline))
            wait_for(stream, b"rigpilot login:", deadline)
            CONSOLE["authenticate"](stream, password, deadline, username=args.username, wait=wait_for)
            stream.sendall(b"id\n")
            result = wait_for(stream, b"uid=1000(user)", deadline)
            if b"Login incorrect" in result:
                raise RuntimeError("serial-console authentication was rejected")

            baseline_script = r'''set -Eeuo pipefail
trap 'status=$?; echo RIGPILOTOS_BASELINE_VERIFY_FAILED line=$LINENO status=$status' ERR
grep -qx 'ID=ubuntu' /etc/os-release
grep -Eq '^VERSION_ID="?24\.04"?$' /etc/os-release
grep -qx 'VERSION_CODENAME=noble' /etc/os-release
grep -qx 'NAME=RigPilotOS' /etc/rigpilotos-release
grep -qx 'VERSION=__IMAGE_RELEASE__' /etc/rigpilotos-release
grep -qx 'UBUNTU_VERSION=24.04' /etc/rigpilotos-release
grep -qx 'WIFI_SUPPORT=disabled' /etc/rigpilotos-release
grep -qx 'NVIDIA_DRIVER_BRANCH=615' /etc/rigpilotos-release
grep -qx 'NVIDIA_KERNEL_MODULE=open' /etc/rigpilotos-release
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
# 0.2.12+: run-managed driver — no NVIDIA debs, no CUDA repo, guard present.
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
echo RIGPILOTOS_BASELINE_VERIFY_OK
'''.replace("__IMAGE_RELEASE__", args.image_release)
            encoded_baseline = base64.b64encode(
                baseline_script.encode("utf-8")
            ).decode("ascii")
            baseline_lines = [
                "rm -f /tmp/rigpilotos-smoke-baseline.b64 "
                "/tmp/rigpilotos-smoke-baseline.sh"
            ]
            for offset in range(0, len(encoded_baseline), 3000):
                baseline_lines.append(
                    "printf '%s' '"
                    + encoded_baseline[offset : offset + 3000]
                    + "' >>/tmp/rigpilotos-smoke-baseline.b64"
                )
            baseline_lines.extend(
                (
                    "base64 -d /tmp/rigpilotos-smoke-baseline.b64 "
                    ">/tmp/rigpilotos-smoke-baseline.sh",
                    "sudo -k",
                    "sudo -S /bin/bash /tmp/rigpilotos-smoke-baseline.sh",
                )
            )
            stream.sendall(("\n".join(baseline_lines) + "\n").encode("ascii"))
            wait_for(stream, b"password for user:", deadline)
            stream.sendall(password.encode("utf-8") + b"\n")
            baseline_result = wait_for_baseline_result(stream, deadline)
            if not CONSOLE["verified_result"](baseline_result, BASELINE_SUCCESS, BASELINE_FAILED):
                visible = baseline_result[-8000:].decode("utf-8", errors="replace")
                raise RuntimeError("platform baseline verification failed:\n" + visible)
            print("serial-console login passed")
            if args.diagnostics_output is not None:
                diagnostic_body = (
                    (
                        "enrolled=0; seen_firstboot=0; for n in $(seq 1 90); do "
                        "systemctl is-active --quiet rigpilot-agent.service && "
                        "test -s /var/lib/rigpilot-agent/state.json && "
                        "test ! -e /run/rigpilot-provision/rigpilot-enroll.json && "
                        "enrolled=1 && break; "
                        "if systemctl is-active --quiet rigpilot-firstboot-enroll.service; then "
                        "seen_firstboot=1; "
                        "elif test \"$seen_firstboot\" = 1 && "
                        "test -f /run/rigpilot-provision/.rigpilot-enrollment-retry; then break; fi; "
                        "sleep 2; done; "
                        if args.expect_enrolled
                        else ""
                    )
                    +
                    "retired_features_clean=0; clock_ready=0; nvidia_guard_ready=0; "
                    "agent_version_ready=0; agent_seed_ready=0; "
                    "test ! -e /etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf && "
                    "test ! -e /var/lib/rigpilot-agent/hiveos-components && "
                    "test ! -e /opt/rigpilot-agent/Setup-HiveOS-Runtime.sh && "
                    "! getent passwd rigpilot-hiveos >/dev/null && "
                    "! command -v bwrap >/dev/null && "
                    "! dpkg-query -W -f='${Status}' ubuntu-drivers-common 2>/dev/null | grep -qx 'install ok installed' && "
                    "! grep -Eq '\"(allow_local_web|allow_remote_sessions|allow_remote_root_sessions|local_web_port|allow_nvidia_driver_updates)\"' /etc/rigpilot-agent/config.json && "
                    "retired_features_clean=1; "
                    "test \"$(timedatectl show -p Timezone --value)\" = Asia/Shanghai && "
                    "systemctl is-active --quiet systemd-timesyncd.service && clock_ready=1; "
                    "systemctl is-active --quiet rigpilotos-nvidia-guard.service && "
                    "systemctl is-enabled --quiet nvidia-persistenced.service && "
                    "test -f /run/modprobe.d/rigpilotos-no-nvidia.conf && "
                    "test \"$(journalctl -k -b --no-pager | grep -Fc \"NVRM: No NVIDIA GPU found\" || true)\" = 0 && "
                    "nvidia_guard_ready=1; "
                    f"grep -qx \"VERSION={args.image_release}\" /etc/rigpilotos-release && "
                    f"grep -qx \"AGENT_SEED_VERSION={args.agent_version}\" /etc/rigpilotos-release && "
                    f"test \"$(cat /opt/rigpilot-agent/active-version)\" = {args.agent_version} && "
                    f"test \"$(/opt/rigpilot-agent/versions/{args.agent_version}/RigPilotAgent --version)\" "
                    f"= \"RigPilot Agent {args.agent_version}\" && "
                    "agent_pid=$(systemctl show rigpilot-agent.service -p MainPID --value); "
                    "test -n \"$agent_pid\" && test \"$agent_pid\" != 0 && "
                    f"test \"$(readlink -f /proc/$agent_pid/exe)\" = "
                    f"/opt/rigpilot-agent/versions/{args.agent_version}/RigPilotAgent && "
                    "agent_version_ready=1; "
                    "seed=/usr/lib/rigpilot-bootstrap/package; "
                    f"test \"$(cat \"$seed/VERSION\" 2>/dev/null)\" = {args.agent_version} && "
                    "(cd \"$seed\" && sha256sum --check --strict SHA256SUMS >/dev/null) && "
                    f"test \"$(\"$seed/RigPilotAgent\" --version 2>/dev/null)\" "
                    f"= \"RigPilot Agent {args.agent_version}\" && "
                    "agent_seed_ready=1; "
                    "echo ===APPLIANCE_POLICY===; "
                    "echo RIGPILOTOS_RETIRED_FEATURES_STATUS=$retired_features_clean; "
                    "echo RIGPILOTOS_CLOCK_STATUS=$clock_ready; "
                    "echo RIGPILOTOS_NVIDIA_GUARD_STATUS=$nvidia_guard_ready; "
                    "echo RIGPILOTOS_AGENT_VERSION_STATUS=$agent_version_ready; "
                    "echo RIGPILOTOS_AGENT_SEED_STATUS=$agent_seed_ready; "
                    "echo ===AGENT_LAYOUT===; "
                    "for path in /opt/rigpilot-agent /opt/rigpilot-agent/versions "
                    f"/opt/rigpilot-agent/versions/{args.agent_version} "
                    f"/opt/rigpilot-agent/versions/{args.agent_version}/RigPilotAgent "
                    "/opt/rigpilot-agent/RigPilotAgentLauncher.sh "
                    "/opt/rigpilot-agent/RigPilotAgentUpdater.py "
                    "/opt/rigpilot-agent/active-version /var/lib/rigpilot-agent "
                    "/var/lib/rigpilot-agent/state.json /etc/rigpilot-agent "
                    "/etc/rigpilot-agent/config.json "
                    "/etc/systemd/system/rigpilot-agent.service "
                    "/etc/systemd/system/rigpilot-agent.service.d "
                    "/etc/systemd/system/rigpilot-agent.service.d/supervision.conf "
                    "/usr/bin/systemd-run /usr/bin/systemctl /usr/bin/python3; "
                    "do stat -c \"%F %U:%G %a %h %s %n\" \"$path\" 2>&1 || true; done; "
                    "printf \"active-version=\"; cat /opt/rigpilot-agent/active-version 2>/dev/null || true; "
                    "pid=$(systemctl show rigpilot-agent.service -p MainPID --value); "
                    "printf \"agent-executable=\"; readlink -f \"/proc/$pid/exe\" 2>/dev/null || true; "
                    "echo ===TRUST_CHAINS===; "
                    f"namei -l /opt/rigpilot-agent/versions/{args.agent_version}/RigPilotAgent || true; "
                    "namei -l /var/lib/rigpilot-agent/state.json || true; "
                    "namei -l /etc/rigpilot-agent/config.json || true; "
                    "namei -l /etc/systemd/system/rigpilot-agent.service.d/supervision.conf || true; "
                    "namei -l /usr/bin/systemd-run || true; namei -l /usr/bin/systemctl || true; "
                    "namei -l /usr/bin/python3 || true; "
                    "echo ===EFFECTIVE_AGENT_POLICY===; "
                    "systemctl show rigpilot-agent.service -p NoNewPrivileges -p ProtectHome "
                    "-p ProtectSystem -p DropInPaths -p MainPID --no-pager || true; "
                    "echo ===TIME===; date --iso-8601=seconds; date --utc --iso-8601=seconds; "
                    "timedatectl status || true; timedatectl show || true; "
                    "systemctl is-enabled systemd-timesyncd.service || true; "
                    "systemctl is-active systemd-timesyncd.service || true; "
                    "systemctl status systemd-timesyncd.service --no-pager -l || true; "
                    "journalctl -b -u systemd-timesyncd.service --no-pager -n 120 || true; "
                    "echo ===FAILED===; systemctl --failed --no-pager; "
                    "echo ===GRUB2_COMMON===; systemctl status grub2-common.service --no-pager -l || true; "
                    "journalctl -b -u grub2-common.service --no-pager -n 120 || true; "
                    "echo ===GROWROOT===; systemctl status rigpilotos-grow-root.service --no-pager -l || true; "
                    "journalctl -b -u rigpilotos-grow-root.service --no-pager -n 120 || true; "
                    "echo ===NETWORKD===; systemctl status systemd-networkd --no-pager -l || true; "
                    "journalctl -b -u systemd-networkd --no-pager -n 120 || true; "
                    "echo ===RESOLVED===; systemctl status systemd-resolved --no-pager -l || true; "
                    "journalctl -b -u systemd-resolved --no-pager -n 80 || true; "
                    "echo ===FIRSTBOOT===; systemctl status rigpilot-firstboot-enroll.service --no-pager -l || true; "
                    "journalctl -b -u rigpilot-firstboot-enroll.service --no-pager -n 120 || true; "
                    "echo ===SMOKE_READY===; systemctl is-enabled rigpilotos-smoke-ready.service || true; "
                    "systemctl status rigpilotos-smoke-ready.service --no-pager -l || true; "
                    "journalctl -b -u rigpilotos-smoke-ready.service --no-pager -n 120 || true; "
                    "echo ===AGENT===; systemctl status rigpilot-agent.service --no-pager -l || true; "
                    "journalctl -b -u rigpilot-agent.service --no-pager -n 120 || true; "
                    "echo ===NETWORK===; ip -br link; ip -br address; ip route; "
                    "echo ===MOUNTS===; findmnt /run/rigpilot-provision || true; "
                    + (
                        "echo RIGPILOTOS_ENROLLMENT_STATUS=$enrolled; "
                        if args.expect_enrolled
                        else ""
                    )
                    + "printf 'RIGPILOTOS_DIAG_V1\\n'\n"
                )
                encoded_diagnostics = base64.b64encode(
                    diagnostic_body.encode("ascii")
                ).decode("ascii")
                command = (
                    "printf '%s' '"
                    + encoded_diagnostics
                    + "' | base64 -d >/tmp/rigpilotos-diagnostics.sh; "
                    "chmod 0700 /tmp/rigpilotos-diagnostics.sh; "
                    "sudo -k; sudo -S /bin/bash /tmp/rigpilotos-diagnostics.sh\n"
                )
                stream.sendall(command.encode("ascii"))
                sudo_prompt = wait_for(stream, b"password for user:", deadline)
                stream.sendall(password.encode("utf-8") + b"\n")
                diagnostics = sudo_prompt + wait_for(
                    stream, b"RIGPILOTOS_DIAG_V1", deadline
                )
                args.diagnostics_output.parent.mkdir(parents=True, exist_ok=True)
                args.diagnostics_output.write_bytes(diagnostics)
                print(f"diagnostics: {args.diagnostics_output}")
                if (
                    args.expect_enrolled
                    and not contains_exact_line(diagnostics, b"RIGPILOTOS_ENROLLMENT_STATUS=1")
                ):
                    raise RuntimeError("first-boot enrollment did not complete")
                required_markers = [
                    b"RIGPILOTOS_CLOCK_STATUS=1",
                    b"RIGPILOTOS_NVIDIA_GUARD_STATUS=1",
                ]
                if args.expect_enrolled:
                    required_markers.extend(
                        (
                            b"RIGPILOTOS_RETIRED_FEATURES_STATUS=1",
                            b"RIGPILOTOS_AGENT_VERSION_STATUS=1",
                        )
                    )
                else:
                    required_markers.append(b"RIGPILOTOS_AGENT_SEED_STATUS=1")
                missing = [
                    marker.decode("ascii")
                    for marker in required_markers
                    if not contains_exact_line(diagnostics, marker)
                ]
                if missing:
                    raise RuntimeError(
                        "appliance policy checks failed: " + ", ".join(missing)
                    )
            return 0
    finally:
        if process is not None and process.stderr is not None:
            try:
                if process.returncode not in (None, 0, 1):
                    print(process.stderr.read().decode("utf-8", errors="replace"), file=sys.stderr)
            finally:
                process.stderr.close()


if __name__ == "__main__":
    raise SystemExit(main())
