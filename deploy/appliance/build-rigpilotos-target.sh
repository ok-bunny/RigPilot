#!/usr/bin/env bash
set -Eeuo pipefail

# Build a clean Ubuntu 24.04 RigPilot appliance on an explicitly supplied,
# disposable block device.  Run in the isolated QEMU builder VM, or on a Linux
# host with a separately verified disposable NBD image and amd64 binfmt support.
# Since image 0.2.11 the appliance carries no HiveOS runtime, no Spark
# remote-session drop-in, no NVIDIA driver-update tooling and no private
# bubblewrap bundle (retired with Agent 0.8.58/0.8.59); the build asserts
# their absence instead of installing them.
# Since image 0.2.12 the NVIDIA driver is installed from the official
# XFree86 .run archive (branch-pinned, digest-verified at build time)
# instead of the CUDA network repository, which rotated the 615 branch
# out of ubuntu2404 after 0.2.11; an apt preference keeps distribution
# NVIDIA packages from ever shadowing the run-managed files, and
# pcie_aspm=off is retained in GRUB as a PCIe policy.  It does not establish
# the cause of, or guarantee recovery from, Xid 79 GPU link failures.

readonly UBUNTU_VERSION="24.04"
readonly UBUNTU_CODENAME="noble"
readonly NVIDIA_DRIVER_BRANCH="615"
readonly NVIDIA_DRIVER_VERSION="615.78.08"
readonly NVIDIA_RUN_SHA256="3e3f6dddc2ee767a08185300af7be3cb286ece76698d2dde3524599782d07ffe"
readonly NVIDIA_RUN_BASENAME="NVIDIA-Linux-x86_64-${NVIDIA_DRIVER_VERSION}.run"
readonly DEFAULT_MIRROR="https://archive.ubuntu.com/ubuntu"
readonly MINIMUM_AGENT_VERSION="0.8.22"
readonly MINIMUM_TARGET_BYTES=6000000000
readonly MINIMUM_ROOT_FREE_BYTES=$((768 * 1024 * 1024))
readonly MINIMUM_ROOT_FREE_INODES=20000

TARGET_DEVICE=""
AGENT_PACKAGE=""
AGENT_VERSION=""
NVIDIA_RUN_PATH=""
OVERLAY_DIR=""
BUILD_REVISION=""
UBUNTU_MIRROR="${DEFAULT_MIRROR}"

fail() {
    printf 'RigPilotOS build failed: %s\n' "$*" >&2
    exit 1
}

verify_nvidia_stack() {
    # A successful installer and nvidia.ko alone do not prove that CUDA,
    # the persistence daemon or firmware-dependent GPUs can initialize.
    local module_name value firmware_name library target
    for module_name in nvidia nvidia_uvm nvidia_modeset nvidia_drm; do
        value="$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F version "${module_name}")" ||
            fail "required NVIDIA module is missing: ${module_name}"
        [[ "${value}" == "${NVIDIA_DRIVER_VERSION}" ]] ||
            fail "NVIDIA module version differs: ${module_name}: ${value}"
        value="$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F vermagic "${module_name}")" ||
            fail "NVIDIA module vermagic is unavailable: ${module_name}"
        [[ "${value}" == "${DEFAULT_KERNEL_ABI} "* ]] ||
            fail "NVIDIA module vermagic differs: ${module_name}"
        value="$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F license "${module_name}")" ||
            fail "NVIDIA module license is unavailable: ${module_name}"
        [[ "${value}" == "Dual MIT/GPL" ]] ||
            fail "NVIDIA module is not the open module: ${module_name}"
    done
    value="$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F firmware nvidia)" ||
        fail "NVIDIA firmware declarations are unavailable"
    [[ -n "${value}" ]] || fail "NVIDIA open module declares no firmware"
    while IFS= read -r firmware_name; do
        [[ "${firmware_name}" == nvidia/"${NVIDIA_DRIVER_VERSION}"/* &&
           "${firmware_name##*/}" =~ ^[A-Za-z0-9_-]+[.]bin$ ]] ||
            fail "unexpected NVIDIA firmware declaration: ${firmware_name}"
        chroot "${ROOT_MOUNT}" test -s "/lib/firmware/${firmware_name}" ||
            fail "required NVIDIA firmware is missing: ${firmware_name}"
    done <<<"${value}"
    for library in libnvidia-ml libcuda libnvidia-opencl libnvidia-cfg; do
        target="$(chroot "${ROOT_MOUNT}" readlink -e "/usr/lib/x86_64-linux-gnu/${library}.so.1")" ||
            fail "required NVIDIA library is missing: ${library}"
        [[ "${target##*/}" == "${library}.so.${NVIDIA_DRIVER_VERSION}" ]] ||
            fail "NVIDIA library version differs: ${library}"
        chroot "${ROOT_MOUNT}" test -s "${target}" ||
            fail "NVIDIA library is empty: ${library}"
    done
    chroot "${ROOT_MOUNT}" grep -qx 'libnvidia-opencl.so.1' /etc/OpenCL/vendors/nvidia.icd ||
        fail "NVIDIA OpenCL ICD is missing or differs"
    printf 'NVIDIA module, firmware and userspace completeness verified (no physical GPU claim).\n'
}

usage() {
    cat <<'EOF'
Usage:
  sudo ./build-rigpilotos-target.sh \
    --target-device /dev/vdb \
    --agent-package /path/RigPilotAgent-VERSION-Ubuntu-x86_64.tar.gz \
    --agent-version VERSION \
    --nvidia-run /path/NVIDIA-Linux-x86_64-615.78.08.run \
    --overlay /path/rootfs-overlay \
    --build-revision r11 \
    --erase-confirm /dev/vdb

The target device is completely erased. The confirmation value must exactly
match --target-device. Never point this at the builder VM system disk.
EOF
}

ERASE_CONFIRM=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-device) TARGET_DEVICE="${2:-}"; shift 2 ;;
        --agent-package) AGENT_PACKAGE="${2:-}"; shift 2 ;;
        --agent-version) AGENT_VERSION="${2:-}"; shift 2 ;;
        --nvidia-run) NVIDIA_RUN_PATH="${2:-}"; shift 2 ;;
        --overlay) OVERLAY_DIR="${2:-}"; shift 2 ;;
        --build-revision) BUILD_REVISION="${2:-}"; shift 2 ;;
        --ubuntu-mirror) UBUNTU_MIRROR="${2:-}"; shift 2 ;;
        --erase-confirm) ERASE_CONFIRM="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "unknown argument: $1" ;;
    esac
done

[[ ${EUID} -eq 0 ]] || fail "run as root"
[[ -n "${TARGET_DEVICE}" && -n "${AGENT_PACKAGE}" && -n "${AGENT_VERSION}" && \
   -n "${NVIDIA_RUN_PATH}" && -n "${OVERLAY_DIR}" && -n "${BUILD_REVISION}" ]] || {
    usage >&2
    fail "required arguments are missing"
}
[[ "${ERASE_CONFIRM}" == "${TARGET_DEVICE}" ]] || fail "erase confirmation does not match target"
[[ -b "${TARGET_DEVICE}" && ! -L "${TARGET_DEVICE}" ]] || fail "target is not a plain block device"
[[ -f "${AGENT_PACKAGE}" && ! -L "${AGENT_PACKAGE}" ]] || fail "Agent package is missing or unsafe"
[[ -f "${NVIDIA_RUN_PATH}" && ! -L "${NVIDIA_RUN_PATH}" ]] || fail "NVIDIA .run archive is missing or unsafe"
[[ "$(basename -- "${NVIDIA_RUN_PATH}")" == "${NVIDIA_RUN_BASENAME}" ]] || \
    fail "NVIDIA .run archive must be named ${NVIDIA_RUN_BASENAME}"
printf '%s  %s\n' "${NVIDIA_RUN_SHA256}" "${NVIDIA_RUN_PATH}" | sha256sum --check --strict || \
    fail "NVIDIA .run archive digest mismatch"
[[ -d "${OVERLAY_DIR}" && ! -L "${OVERLAY_DIR}" ]] || fail "rootfs overlay is missing or unsafe"
[[ "${UBUNTU_MIRROR}" =~ ^https?://[^[:space:]]+$ ]] || fail "Ubuntu mirror is invalid"
[[ "${BUILD_REVISION}" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || \
    fail "build revision is invalid"
[[ "${AGENT_VERSION}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || \
    fail "Agent version is invalid"
IFS=. read -r agent_major agent_minor agent_patch <<<"${AGENT_VERSION}"
(( agent_major > 0 || agent_minor > 8 || (agent_minor == 8 && agent_patch >= 22) )) || \
    fail "Ubuntu 24.04/615 image requires Agent ${MINIMUM_AGENT_VERSION} or newer"

for command_name in \
    awk blkid blockdev cat chmod chown chroot cp debootstrap df dumpe2fs e2fsck find findmnt flock grep head install lsblk \
    ln mkdir mktemp mount mountpoint mkfs.ext4 mkfs.vfat partprobe python3 readlink rm sgdisk \
    sha256sum sort stat sync tail tar tr truncate udevadm umount uname wipefs dirname dpkg-deb; do
    command -v "${command_name}" >/dev/null 2>&1 || fail "missing build dependency: ${command_name}"
done

APPLIANCE_SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
IMAGE_RELEASE="$(python3 "${APPLIANCE_SOURCE_DIR}/appliance_metadata.py")"
readonly IMAGE_RELEASE
POLICY_HELPER="${APPLIANCE_SOURCE_DIR}/apply-final-image-policy.py"
RETAINED_DKMS_DEB="${APPLIANCE_SOURCE_DIR}/vendor/dkms_3.4.3-1ubuntu1_all.deb"
[[ -f "${POLICY_HELPER}" && ! -L "${POLICY_HELPER}" ]] || fail "reviewed factory policy helper is missing"
[[ -f "${RETAINED_DKMS_DEB}" && ! -L "${RETAINED_DKMS_DEB}" ]] || fail "frozen DKMS archive is missing"
printf '%s  %s\n' "6cb516fc913f1afbe8387078b357e15619e19291b9398e0b3cb6d0beae99e2b4" \
    "${RETAINED_DKMS_DEB}" | sha256sum --check --strict || fail "frozen DKMS archive SHA256 mismatch"
# The GA 6.8.0-142.142 kernel metapackages were superseded in noble-updates
# (the pocket now carries 6.8.0-147.147) while the concrete 6.8.0-142.142
# kernel binaries remain published.  Both metapackages are vendored from the
# Ubuntu primary archive (launchpad file archive) with pinned digests so the
# build keeps binding the image to the reviewed GA 6.8.0-142 ABI (2026-11-01).
VENDORED_LINUX_IMAGE_GENERIC_DEB="${APPLIANCE_SOURCE_DIR}/vendor/linux-image-generic_6.8.0-142.142_amd64.deb"
VENDORED_LINUX_HEADERS_GENERIC_DEB="${APPLIANCE_SOURCE_DIR}/vendor/linux-headers-generic_6.8.0-142.142_amd64.deb"
[[ -f "${VENDORED_LINUX_IMAGE_GENERIC_DEB}" && ! -L "${VENDORED_LINUX_IMAGE_GENERIC_DEB}" ]] || \
    fail "vendored GA 6.8.0-142.142 linux-image-generic metapackage is missing"
[[ -f "${VENDORED_LINUX_HEADERS_GENERIC_DEB}" && ! -L "${VENDORED_LINUX_HEADERS_GENERIC_DEB}" ]] || \
    fail "vendored GA 6.8.0-142.142 linux-headers-generic metapackage is missing"
printf '%s  %s\n' \
    "41163c3be460f82aaf422c5fd5bcdb062eaf52e20c8990af96a0c5e9b1ab9521" "${VENDORED_LINUX_IMAGE_GENERIC_DEB}" \
    "6ff6f171e65633d9cd313a2dffb0c7780c52ecfcc65a0dbc00a19f72101806c0" "${VENDORED_LINUX_HEADERS_GENERIC_DEB}" \
    | sha256sum --check --strict || fail "vendored GA kernel metapackage SHA256 mismatch"
test "$(dpkg-deb -f "${VENDORED_LINUX_IMAGE_GENERIC_DEB}" Package)" = "linux-image-generic" || \
    fail "vendored linux-image-generic identity differs"
test "$(dpkg-deb -f "${VENDORED_LINUX_IMAGE_GENERIC_DEB}" Version)" = "6.8.0-142.142" || \
    fail "vendored linux-image-generic version differs"
test "$(dpkg-deb -f "${VENDORED_LINUX_HEADERS_GENERIC_DEB}" Package)" = "linux-headers-generic" || \
    fail "vendored linux-headers-generic identity differs"
test "$(dpkg-deb -f "${VENDORED_LINUX_HEADERS_GENERIC_DEB}" Version)" = "6.8.0-142.142" || \
    fail "vendored linux-headers-generic version differs"

exec 9>/run/lock/rigpilotos-image-build.lock
flock -n 9 || fail "another RigPilotOS image build is running"

validate_agent_package() {
    python3 - "${AGENT_PACKAGE}" "${AGENT_VERSION}" <<'PY'
import hashlib
import json
import pathlib
import re
import struct
import sys
import tarfile


def canonical(name):
    path = pathlib.PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or str(path) != name or ".." in path.parts:
        raise SystemExit(f"unsafe Agent archive path: {name!r}")
    return name


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise SystemExit(f"duplicate Agent manifest field: {key!r}")
        value[key] = item
    return value


def validate_package(archive_path, expected_version):
    prefix = "RigPilotAgent-Ubuntu-x64"
    files = {}
    directories = set()
    seen = set()
    total = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        for index, member in enumerate(archive, start=1):
            if index > 4096:
                raise SystemExit("Agent package has too many entries")
            name = canonical(member.name.rstrip("/") if member.isdir() else member.name)
            if name.casefold() in seen or not (name == prefix or name.startswith(prefix + "/")):
                raise SystemExit(f"duplicate or unexpected Agent archive member: {member.name!r}")
            seen.add(name.casefold())
            if member.isdir():
                directories.add(name)
                continue
            if not member.isreg() or not 0 <= member.size <= 512 * 1024 * 1024:
                raise SystemExit(f"unsafe Agent archive member: {member.name!r}")
            total += member.size
            if total > 2 * 1024 * 1024 * 1024:
                raise SystemExit("Agent package expands beyond the release limit")
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit(f"Agent archive member cannot be read: {name!r}")
            relative = name.removeprefix(prefix + "/")
            payload = source.read()
            if relative != "RigPilotAgent" and (b"\x00" in payload or b"\r" in payload):
                raise SystemExit(f"Agent text member must be NUL-free LF text: {name!r}")
            files[relative] = payload
    required = {
        "RigPilotAgent", "Install.sh", "Remove-Systemd.sh", "RigPilotAgentLauncher.sh",
        "RigPilotAgentUpdater.py", "Start-Portable.sh", "SHA256SUMS", "VERSION",
        "agent-release.json", "rigpilot-agent.service",
        "rigpilot-agent-supervision.conf", "rigpilot-agent-update-recovery.service",
        "rigpilot-agent-update-recovery.timer",
    }
    if not required.issubset(files):
        raise SystemExit("Agent package is incomplete")
    # Retired with the Agent 0.8.58 feature-cleanup batch: a seed still
    # carrying either member must never be embedded in image 0.2.11+.
    for retired_member in ("Setup-HiveOS-Runtime.sh", "rigpilot-agent-remote-sessions.conf"):
        if retired_member in files:
            raise SystemExit(f"retired Agent package member is not permitted: {retired_member}")
    if "rigpilot-agent-wifi.conf" in files:
        raise SystemExit("Wi-Fi Agent package is not permitted in a wired-only image")
    if files["VERSION"].decode("ascii").strip() != expected_version:
        raise SystemExit("Agent package VERSION does not match --agent-version")
    header = files["RigPilotAgent"][:64]
    if len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01" or struct.unpack_from("<H", header, 18)[0] != 62:
        raise SystemExit("Agent binary is not an ELF64 x86-64 executable")
    manifest = json.loads(files["agent-release.json"], object_pairs_hook=unique_object)
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "version", "platform", "architecture", "files"}:
        raise SystemExit("Agent manifest fields are invalid")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["version"] != expected_version or manifest["platform"] != "linux"
            or manifest["architecture"] != "x86_64"):
        raise SystemExit("Agent manifest identity does not match the package")
    hashes = manifest["files"]
    if not isinstance(hashes, dict) or set(hashes) != set(files) - {"agent-release.json", "SHA256SUMS"}:
        raise SystemExit("Agent manifest file set does not match the package")
    for name, digest in hashes.items():
        canonical(name)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SystemExit("Agent manifest digest is invalid")
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise SystemExit(f"Agent manifest hash mismatch: {name}")
    sums = {}
    for line in files["SHA256SUMS"].decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise SystemExit("Agent SHA256SUMS entry is invalid")
        digest, name = match.groups()
        canonical(name)
        if name in sums:
            raise SystemExit(f"duplicate Agent SHA256SUMS entry: {name}")
        sums[name] = digest
    if set(sums) != set(files) - {"SHA256SUMS"}:
        raise SystemExit("Agent SHA256SUMS file set does not match the package")
    for name, digest in sums.items():
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise SystemExit(f"Agent SHA256SUMS mismatch: {name}")
    allowed_directories = {prefix}
    for name in files:
        allowed_directories.update(str(parent) for parent in (pathlib.PurePosixPath(prefix) / name).parents if str(parent) != ".")
    if not directories.issubset(allowed_directories):
        raise SystemExit("Agent package contains an unexpected directory")


if __name__ == "__main__":
    validate_package(sys.argv[1], sys.argv[2])
PY
}

# Validate the complete seed before erasing the disposable target; validate
# again immediately before extraction after the long base-system build.
validate_agent_package

# Use kernel device numbers so mapper aliases and nested RAID/LVM/crypt
# ancestry cannot hide the builder root disk. Refuse unreadable topology.
verify_target_device() {
    python3 - "${TARGET_DEVICE}" <<'PY'
import json
from pathlib import Path
import re
import subprocess
import sys


def query(*args):
    return subprocess.check_output(args, text=True).strip()


def device_tree(path, *, inverse=False):
    args = ["lsblk", "--json", "--paths", "--output", "NAME,MAJ:MIN,TYPE,MOUNTPOINTS"]
    if inverse:
        args.append("--inverse")
    value = json.loads(query(*args, "--", path))
    roots = value.get("blockdevices")
    if not isinstance(roots, list) or len(roots) != 1:
        raise SystemExit("cannot identify a unique block device topology")
    pending = list(roots)
    result = []
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or re.fullmatch(r"[0-9]+:[0-9]+", node.get("maj:min", "")) is None:
            raise SystemExit("invalid block device topology")
        result.append(node)
        pending.extend(node.get("children", []))
    return result


def verify_target(target, *, sysfs=Path("/sys/dev/block")):
    root_number = query("findmnt", "--noheadings", "--output", "MAJ:MIN", "--target", "/")
    if re.fullmatch(r"[0-9]+:[0-9]+", root_number) is None:
        raise SystemExit("cannot identify builder root device")
    system = device_tree("/dev/block/" + root_number, inverse=True)
    if system[0]["maj:min"] != root_number or not any(node.get("type") == "disk" for node in system):
        raise SystemExit("cannot identify builder system disk ancestry")
    target_tree = device_tree(target)
    if target_tree[0].get("type") != "disk":
        raise SystemExit("target must be a whole disk")
    if {node["maj:min"] for node in system} & {node["maj:min"] for node in target_tree}:
        raise SystemExit("refusing to erase the builder system disk or its ancestors")
    for node in target_tree:
        mounts = node.get("mountpoints")
        if not isinstance(mounts, list):
            raise SystemExit("cannot read target mount state")
        if any(mount is not None and mount != "" for mount in mounts):
            raise SystemExit("target or one of its descendants is mounted or active swap")
        holders = sysfs / node["maj:min"] / "holders"
        # iterdir raises on missing/unreadable sysfs instead of assuming idle.
        if next(holders.iterdir(), None) is not None:
            raise SystemExit("target or one of its descendants has active device holders")


if __name__ == "__main__":
    verify_target(sys.argv[1])
PY
}

TARGET_DEVICE="$(readlink -f -- "${TARGET_DEVICE}")"
verify_target_device
TARGET_BYTES="$(blockdev --getsize64 "${TARGET_DEVICE}")"
[[ "${TARGET_BYTES}" =~ ^[0-9]+$ && "${TARGET_BYTES}" -ge "${MINIMUM_TARGET_BYTES}" ]] || \
    fail "target must be at least ${MINIMUM_TARGET_BYTES} bytes"

case "${TARGET_DEVICE}" in
    *[0-9]) partition_path() { printf '%sp%s' "${TARGET_DEVICE}" "$1"; } ;;
    *) partition_path() { printf '%s%s' "${TARGET_DEVICE}" "$1"; } ;;
esac

P1="$(partition_path 1)"
P2="$(partition_path 2)"
P3="$(partition_path 3)"
P4="$(partition_path 4)"
WORK_DIR="$(mktemp -d /var/tmp/rigpilotos-build.XXXXXXXXXX)"
ROOT_MOUNT="${WORK_DIR}/root"
CONFIG_MOUNT="${WORK_DIR}/provision"

cleanup() {
    local exit_code=$?
    local unmount_failed=0 mount_table=""
    trap - EXIT INT TERM
    set +e
    sync
    for path in \
        "${ROOT_MOUNT}/run" "${ROOT_MOUNT}/sys" "${ROOT_MOUNT}/proc" \
        "${ROOT_MOUNT}/dev" "${ROOT_MOUNT}/boot/efi" \
        "${ROOT_MOUNT}" "${CONFIG_MOUNT}"; do
        if mountpoint -q -- "${path}"; then
            if ! umount -R -- "${path}" >/dev/null 2>&1 && \
               ! umount -l -- "${path}" >/dev/null 2>&1; then
                unmount_failed=1
            fi
        fi
    done
    # A failed detach must never turn recursive deletion into a walk through
    # a remaining host bind mount. Check all descendants, not just known roots.
    if ! mount_table=$(findmnt -rn -o TARGET); then
        unmount_failed=1
    elif printf '%s\n' "${mount_table}" | awk -v root="${WORK_DIR}" \
        '$0 == root || index($0, root "/") == 1 { found = 1 } END { exit !found }'; then
        unmount_failed=1
    fi
    if [[ "${unmount_failed}" -ne 0 ]]; then
        printf 'Build cleanup retained %s because mounts could not be proven detached.\n' \
            "${WORK_DIR}" >&2
        exit 1
    fi
    if [[ -d "${WORK_DIR}" && ! -L "${WORK_DIR}" ]]; then
        case "${WORK_DIR}" in /var/tmp/rigpilotos-build.*) rm -rf --one-file-system -- "${WORK_DIR}" ;; esac
    fi
    exit "${exit_code}"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Erasing isolated target %s (%s bytes) ...\n' "${TARGET_DEVICE}" "${TARGET_BYTES}"
wipefs --all --force "${TARGET_DEVICE}"
sgdisk --zap-all "${TARGET_DEVICE}"
sgdisk \
    --new=1:2048:+64M --typecode=1:0700 --change-name=1:RIGPILOT \
    --new=2:0:+2M --typecode=2:EF02 --change-name=2:BIOSBOOT \
    --new=3:0:+256M --typecode=3:EF00 --change-name=3:RIGPILOT_EFI \
    --new=4:0:0 --typecode=4:8304 --change-name=4:RIGPILOT_ROOT \
    "${TARGET_DEVICE}"
partprobe "${TARGET_DEVICE}"
udevadm settle --timeout=30
for partition in "${P1}" "${P2}" "${P3}" "${P4}"; do
    [[ -b "${partition}" ]] || fail "partition was not created: ${partition}"
done

mkfs.vfat -F 32 -n RIGPILOT "${P1}"
# FAT volume labels are limited to 11 characters.  Keep the longer,
# descriptive GPT partition name above, but use a valid filesystem label.
mkfs.vfat -F 32 -n RIGP_EFI "${P3}"
# Root filesystem recipe, locked in build-profile.json (rootfs_recipe).
# Disable ext4 features incompatible with online resize on the pinned GA kernel.
ROOTFS_MKFS_OPTIONS='-F -L RIGPILOT_ROOT -m 0 -O ^metadata_csum_seed,^orphan_file,metadata_csum,64bit'
# shellcheck disable=SC2086
mkfs.ext4 ${ROOTFS_MKFS_OPTIONS} "${P4}"
# Record the creating e2fsprogs version explicitly (release values must stay
# single tokens, so keep only the dotted version from "mke2fs X.Y.Z (date)").
ROOTFS_E2FSPROGS_RAW="$(mkfs.ext4 -V 2>&1 | head -n 1)"
ROOTFS_E2FSPROGS="${ROOTFS_E2FSPROGS_RAW#mke2fs }"
ROOTFS_E2FSPROGS="${ROOTFS_E2FSPROGS%% *}"
[[ "${ROOTFS_E2FSPROGS}" =~ ^[0-9][0-9.]*$ ]] || \
    fail "builder e2fsprogs version could not be identified: ${ROOTFS_E2FSPROGS_RAW}"
# Release-file values must be single tokens (no spaces), so bind the recipe
# through its SHA-256 instead of the raw option string.
ROOTFS_MKFS_OPTIONS_SHA256="$(printf '%s' "${ROOTFS_MKFS_OPTIONS}" | sha256sum | awk '{print $1}')"
[[ "${ROOTFS_MKFS_OPTIONS_SHA256}" =~ ^[0-9a-f]{64}$ ]] ||     fail "root filesystem recipe digest could not be computed"
printf 'Root filesystem created with: %s (digest %s, e2fsprogs %s)\n' \
    "${ROOTFS_MKFS_OPTIONS}" "${ROOTFS_MKFS_OPTIONS_SHA256}" "${ROOTFS_E2FSPROGS}"
rootfs_features="$(dumpe2fs -h "${P4}" 2>/dev/null | \
    awk -F': *' '/^Filesystem features:/ { print $2 }')"
[[ -n "${rootfs_features}" ]] || fail "created root filesystem features could not be read"
case " ${rootfs_features} " in
    *" metadata_csum_seed "*)
        fail "root filesystem carries metadata_csum_seed; online resize is impossible on kernel 6.8" ;;
esac
case " ${rootfs_features} " in
    *" orphan_file "*)
        fail "root filesystem carries orphan_file; online resize is impossible on kernel 6.8" ;;
esac

install -d -m 0755 "${ROOT_MOUNT}" "${CONFIG_MOUNT}"
mount "${P4}" "${ROOT_MOUNT}"
install -d -m 0755 "${ROOT_MOUNT}/boot/efi"
mount "${P3}" "${ROOT_MOUNT}/boot/efi"
mount -o rw,nosuid,nodev,noexec,umask=0077 "${P1}" "${CONFIG_MOUNT}"

printf 'Bootstrapping Ubuntu %s (%s) ...\n' "${UBUNTU_VERSION}" "${UBUNTU_CODENAME}"
case "$(uname -m)" in
    aarch64|arm64)
        # Rosetta needs /proc/self/exe before the first amd64 process starts.
        # debootstrap's second stage runs /bin/true before its own setup_proc,
        # so extract first, mount proc, then drive stage two from the native
        # host shell. Keeping that shell outside the chroot also lets its
        # setup_proc safely refresh the target proc mount during bootstrap.
        printf 'Using native ARM host with amd64 binfmt and two-stage debootstrap.\n'
        debootstrap --arch=amd64 --variant=minbase --foreign \
            "${UBUNTU_CODENAME}" "${ROOT_MOUNT}" "${UBUNTU_MIRROR}"
        install -d -m 0755 "${ROOT_MOUNT}/proc"
        mount -t proc -o nosuid,nodev,noexec proc "${ROOT_MOUNT}/proc"
        DEBOOTSTRAP_DIR="${ROOT_MOUNT}/debootstrap" debootstrap \
            --second-stage --second-stage-target="${ROOT_MOUNT}"
        if mountpoint -q -- "${ROOT_MOUNT}/proc"; then
            umount -- "${ROOT_MOUNT}/proc"
        fi
        ;;
    *)
        debootstrap --arch=amd64 --variant=minbase \
            "${UBUNTU_CODENAME}" "${ROOT_MOUNT}" "${UBUNTU_MIRROR}"
        ;;
esac

install -m 0644 /etc/resolv.conf "${ROOT_MOUNT}/etc/resolv.conf"
cat >"${ROOT_MOUNT}/etc/apt/sources.list" <<'EOF'
deb https://archive.ubuntu.com/ubuntu noble main restricted universe multiverse
deb https://archive.ubuntu.com/ubuntu noble-updates main restricted universe multiverse
deb https://security.ubuntu.com/ubuntu noble-security main restricted universe multiverse
EOF
cat >"${ROOT_MOUNT}/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 0755 "${ROOT_MOUNT}/usr/sbin/policy-rc.d"

for path in dev proc sys run; do
    mount --rbind "/${path}" "${ROOT_MOUNT}/${path}"
    mount --make-rslave "${ROOT_MOUNT}/${path}"
done
[[ "$(chroot "${ROOT_MOUNT}" dpkg --print-architecture)" == amd64 ]] || \
    fail "bootstrapped userland is not amd64"
python3 - "${ROOT_MOUNT}/bin/bash" <<'PY'
import pathlib
import struct
import sys

with pathlib.Path(sys.argv[1]).open("rb") as source:
    header = source.read(24)
if header[:6] != b"\x7fELF\x02\x01" or len(header) < 20:
    raise SystemExit("bootstrapped Bash is not an ELF64 little-endian executable")
if struct.unpack_from("<H", header, 18)[0] != 62:
    raise SystemExit("bootstrapped Bash is not x86-64")
print("Bootstrapped architecture verified: dpkg amd64 and Bash ELF x86-64")
PY

printf 'Installing Ubuntu kernel, tools and NVIDIA %s open driver ...\n' "${NVIDIA_DRIVER_VERSION}"
# Stage the vendored GA metapackages where the chroot transaction can pin them.
install -d -m 0755 "${ROOT_MOUNT}/apt-vendored"
install -m 0644 "${VENDORED_LINUX_IMAGE_GENERIC_DEB}" "${VENDORED_LINUX_HEADERS_GENERIC_DEB}" \
    "${RETAINED_DKMS_DEB}" "${ROOT_MOUNT}/apt-vendored/"
# 2026-11-17 builder workaround (0212): the identical assertion executed
# inside the chroot (amd64 coreutils under Rosetta) failed twice on this
# Parallels ARM builder with "computed checksum did NOT match", while every
# isolated reproduction (ext4-on-NBD write+readback, Rosetta chroot hashing,
# sha256sum --check via stdin, hot and cold caches) verified byte-identical
# files and digests.  Run the staged-file assertions from the native aarch64
# host with identical strength instead of trusting Rosetta-translated
# hashing under build load.
sha256sum --check --strict \
    <<<"$(printf '%s  %s\n' \
        "41163c3be460f82aaf422c5fd5bcdb062eaf52e20c8990af96a0c5e9b1ab9521" "${ROOT_MOUNT}/apt-vendored/linux-image-generic_6.8.0-142.142_amd64.deb" \
        "6ff6f171e65633d9cd313a2dffb0c7780c52ecfcc65a0dbc00a19f72101806c0" "${ROOT_MOUNT}/apt-vendored/linux-headers-generic_6.8.0-142.142_amd64.deb" \
        "6cb516fc913f1afbe8387078b357e15619e19291b9398e0b3cb6d0beae99e2b4" "${ROOT_MOUNT}/apt-vendored/dkms_3.4.3-1ubuntu1_all.deb")" \
    || fail "vendored GA metapackage digests changed after staging into the target"
# The dkms deb is vendored for the same reason as the kernel metapackages:
# noble's live archive moved dkms between the 0.2.11 build (3.4.3-1ubuntu1)
# and 2026-11-17 (3.0.11-1ubuntu13), which broke the retained-DKMS policy
# identity gate.  Installing the pinned local deb keeps the retained copy,
# the installed package and the factory policy assertion identical.
# 2026-11-17 builder workaround #3 (root cause of the three "SIGBUS" build
# failures): the 6 GB delivery image leaves only ~2.6 GB free on the root
# filesystem after the kernel/firmware install, while the .run route needs
# 2.6 GB of transient space (staged 530 MB archive plus the ~2.1 GB unpacked
# installer tree).  nvidia-installer then dies with SIGBUS while mmap-copying
# onto a full filesystem (verified: failed build rootfs at 100%, installer
# log shows module compilation already complete, death during DKMS
# registration; 0211's deb route never staged this transient bulk).  Bind a
# host-side directory inside the work dir over the chroot's /tmp so the
# archive, the emulator and the unpack tree consume builder-disk space; the
# root filesystem only receives the final installed driver files.  The EXIT
# cleanup unmounts this bind together with the rootfs (umount -R).
install -d -m 0777 "${WORK_DIR}/chroot-tmp" "${ROOT_MOUNT}/tmp"
mount --bind "${WORK_DIR}/chroot-tmp" "${ROOT_MOUNT}/tmp"
install -d -m 0700 "${ROOT_MOUNT}/tmp/nvidia-run"
install -m 0644 "${NVIDIA_RUN_PATH}" "${ROOT_MOUNT}/tmp/nvidia-run/${NVIDIA_RUN_BASENAME}"
printf '%s  %s\n' "${NVIDIA_RUN_SHA256}" "${ROOT_MOUNT}/tmp/nvidia-run/${NVIDIA_RUN_BASENAME}" | \
    sha256sum --check --strict || \
    fail "staged NVIDIA .run archive digest mismatch (host-side assertion)"
# Builder workaround #2 (kept as defense in depth): run the installer binary
# under the static qemu-x86_64 user emulator (an aarch64 binary that runs
# natively inside the amd64 chroot), addressed through PATH so tests can mock
# it by name; the shell extraction and the gcc/make module build stay on
# binfmt Rosetta.
[[ -x /bin/qemu-x86_64 ]] || \
    fail "static qemu-x86_64 user emulator is required on the builder host (apt install qemu-user-static)"
install -d -m 0700 "${ROOT_MOUNT}/tmp/qemu-x86_64-bin"
install -m 0755 /bin/qemu-x86_64 "${ROOT_MOUNT}/tmp/qemu-x86_64-bin/nvidia-run-qemu"
chroot "${ROOT_MOUNT}" /usr/bin/env \
    PATH="/tmp/qemu-x86_64-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    NVIDIA_DRIVER_VERSION="${NVIDIA_DRIVER_VERSION}" \
    DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    bash -Eeuo pipefail -c '
        apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 \
            -o Acquire::https::Timeout=30 update
        # Configure the target kernel and initramfs completely before NVIDIA.
        # Installing both in one dpkg transaction can make nvidia-kernel-common
        # observe the builder VM kernel before initramfs-tools is configured.
        # Since 0.2.11 the package list deliberately omits the retired HiveOS
        # host toolset (jq is retained for the appliance itself) and the
        # NVIDIA driver-update probe package ubuntu-drivers-common.
        apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 \
            -o Acquire::https::Timeout=30 install -y --no-install-recommends \
            systemd-sysv systemd-resolved systemd-timesyncd dbus kmod initramfs-tools \
            /apt-vendored/linux-image-generic_6.8.0-142.142_amd64.deb \
            /apt-vendored/linux-headers-generic_6.8.0-142.142_amd64.deb \
            linux-image-6.8.0-142-generic=6.8.0-142.142 \
            linux-headers-6.8.0-142-generic=6.8.0-142.142 \
            linux-modules-6.8.0-142-generic=6.8.0-142.142 \
            linux-modules-extra-6.8.0-142-generic=6.8.0-142.142 \
            linux-firmware \
            grub2-common grub-pc-bin \
            grub-efi-amd64-bin efibootmgr netplan.io networkd-dispatcher \
            sudo locales tzdata ca-certificates curl wget git jq python3 \
            libpam0g libnuma1 ocl-icd-libopencl1 bash-completion vim nano \
            fbterm fontconfig fonts-wqy-microhei \
            tmux screen htop iotop sysstat pciutils usbutils dmidecode lshw \
            lm-sensors smartmontools nvme-cli ethtool iproute2 iputils-ping \
            dnsutils rsync unzip p7zip-full xz-utils zstd dosfstools gdisk \
            parted cloud-guest-utils e2fsprogs openssh-server ntfs-3g exfatprogs \
            mokutil build-essential \
            /apt-vendored/dkms_3.4.3-1ubuntu1_all.deb clinfo
        # Retired-feature absence gates (0.2.11): the removed HiveOS runtime,
        # driver-update probe and bubblewrap must not arrive through any
        # dependency path either.
        for forbidden_package in ubuntu-drivers-common bubblewrap; do
            if dpkg-query -W -f="\${Status}" "$forbidden_package" 2>/dev/null | grep -qx "install ok installed"; then
                echo "retired package was pulled into the image: $forbidden_package" >&2
                exit 1
            fi
        done
        test -s /etc/initramfs-tools/initramfs.conf || {
            echo "target initramfs configuration was not created" >&2
            exit 1
        }
        test -e /boot/vmlinuz || {
            echo "target default GA kernel link was not created" >&2
            exit 1
        }
        test "$(readlink -f /boot/vmlinuz)" = /boot/vmlinuz-6.8.0-142-generic
        grep -qx "CONFIG_SMP=y" /boot/config-6.8.0-142-generic
        grep -qx "CONFIG_STRICT_MODULE_RWX=y" /boot/config-6.8.0-142-generic
        if grep -Eq "^(CONFIG_ARCH_HAS_EXECMEM_ROX|CONFIG_EXECMEM)=" /boot/config-6.8.0-142-generic; then
            echo "GA kernel config differs from the reviewed execmem-free source path" >&2
            exit 1
        fi
        # The vendored metapackages must end up installed, not merely resolved.
        test "$(dpkg-query -W -f="\${Version}" linux-image-generic)" = "6.8.0-142.142"
        test "$(dpkg-query -W -f="\${Version}" linux-headers-generic)" = "6.8.0-142.142"
        # The final 6 GB layout has ample runtime headroom, but retaining all
        # downloaded kernel/tool .debs while fetching the NVIDIA stack needlessly
        # increases peak build usage.  Installed files are unaffected.
        apt-get clean
        rm -rf /apt-vendored
        # The driver itself is NOT installed from apt: distribution repos no
        # longer carry the 615 branch (CUDA repo rotated it out after 0.2.11;
        # noble-updates lags at 615.71.09).  Install the digest-verified
        # XFree86 .run archive against the pinned GA kernel instead, and pin
        # apt away from every NVIDIA package so no future upgrade can shadow
        # the run-managed files.  (Digest asserted host-side before this
        # transaction: see the 2026-11-17 builder workaround above.)
        cat >/etc/apt/preferences.d/99-rigpilot-nvidia-run-managed <<'APT_PREFS'
# RigPilotOS 0.2.12+: the NVIDIA driver is managed by the pinned XFree86
# .run installer, not by distribution packages.  Block every NVIDIA deb so
# routine upgrades can never split-file the driver stack.
Package: nvidia-* libnvidia-* cuda-*
Pin: release *
Pin-Priority: -1
APT_PREFS
        # Builder workarounds #2/#3 (see the staging block): the chroot /tmp
        # is a host-side bind (root filesystem headroom) and the installer
        # binary runs under the staged qemu-x86_64 emulator.  Extract with
        # the shell, install from inside the unpacked tree, then drop it.
        cd /tmp
        sh /tmp/nvidia-run/NVIDIA-Linux-x86_64-*.run -x >/dev/null
        cd /tmp/NVIDIA-Linux-x86_64-615.78.08
        nvidia-run-qemu ./nvidia-installer \
            --silent --ui=none --no-questions \
            --no-backup --no-x-check --no-nouveau-check \
            --kernel-module-type=open \
            --kernel-name=6.8.0-142-generic \
            --skip-module-load
        cd /
        rm -rf -- /tmp/NVIDIA-Linux-x86_64-615.78.08 /tmp/nvidia-run \
            /tmp/qemu-x86_64-bin
        apt-get check
        locale-gen en_US.UTF-8 zh_CN.UTF-8
        update-locale LANG=en_US.UTF-8
        ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
        echo Asia/Shanghai >/etc/timezone
        dpkg-reconfigure -f noninteractive tzdata
    '

# Host-side kernel config digest (same 2026-11-17 Rosetta-hashing workaround:
# was sha256sum inside the chroot transaction).
printf '%s  %s\n' "3b8533dd9d235ca634ac58f82c5ce1ee35f12ef620693e17033184d2c9ca5890" \
    "${ROOT_MOUNT}/boot/config-6.8.0-142-generic" | sha256sum --check --strict || \
    fail "installed GA kernel config digest differs"

# Builder workaround #3 epilogue: detach the host-side /tmp bind before any
# later stage walks the target tree.  The EXIT cleanup covers failure paths.
if mountpoint -q -- "${ROOT_MOUNT}/tmp"; then
    umount -- "${ROOT_MOUNT}/tmp" || fail "could not detach the chroot /tmp bind"
fi
chroot "${ROOT_MOUNT}" test -x /usr/bin/nvidia-smi || fail "nvidia-smi was not installed"
# 0.2.11 retired the Agent-driven NVIDIA driver-update feature; the probe
# package it used must be absent, not merely unused.
if chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Status}\n' ubuntu-drivers-common 2>/dev/null | \
    grep -qx 'install ok installed'; then
    fail "ubuntu-drivers-common must not be installed in 0.2.11+"
fi
chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Status}\n' openssh-server | \
    grep -qx 'install ok installed' || fail "openssh-server is not installed"
chroot "${ROOT_MOUNT}" test -x /usr/sbin/sshd || fail "sshd executable is missing"
# Since 0.2.12 the NVIDIA stack is run-managed: no NVIDIA deb may be present,
# and the apt preference file must keep it that way on every later upgrade.
if chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Status}' 'nvidia-*' 2>/dev/null | \
    grep -qx 'install ok installed'; then
    fail "run-managed image must not contain installed NVIDIA deb packages"
fi
chroot "${ROOT_MOUNT}" test -f \
    /etc/apt/preferences.d/99-rigpilot-nvidia-run-managed || \
    fail "NVIDIA apt preference guard is missing"
DEFAULT_KERNEL_IMAGE="$(chroot "${ROOT_MOUNT}" readlink -f -- /boot/vmlinuz)"
case "${DEFAULT_KERNEL_IMAGE}" in
    /boot/vmlinuz-*) ;;
    *) fail "default GA kernel image could not be identified" ;;
esac
DEFAULT_KERNEL_ABI="${DEFAULT_KERNEL_IMAGE##*/vmlinuz-}"
[[ "${DEFAULT_KERNEL_ABI}" == "6.8.0-142-generic" ]] || fail "unexpected GA kernel ABI"
chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Status}\n' linux-firmware | \
    grep -qx 'install ok installed' || fail "linux-firmware is not installed"
# Ubuntu's signed GA modules-extra package hard-depends on this passive
# regulatory database. It provides no appliance Wi-Fi enrollment path.
REGDB_STATUS="$(chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Status}\t${Version}' wireless-regdb)"
[[ "${REGDB_STATUS}" == $'install ok installed\t2026.05.30-0ubuntu1~24.04.1' ]] || \
    fail "GA modules-extra wireless-regdb dependency differs from reviewed signed index"
for module_name in \
    e1000e igb igc ixgbe r8169 r8152 ax88179_178a cdc_ether aqc111; do
    chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" "${module_name}" \
        >/dev/null 2>&1 || fail "required network driver module is missing: ${module_name}"
done
NVIDIA_MODULE_VERSION="$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F version nvidia)"
[[ "${NVIDIA_MODULE_VERSION}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
    fail "default kernel does not contain NVIDIA ${NVIDIA_DRIVER_VERSION}: ${NVIDIA_MODULE_VERSION}"
NVIDIA_VERMAGIC="$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F vermagic nvidia)"
[[ "${NVIDIA_VERMAGIC}" == "${DEFAULT_KERNEL_ABI} "* ]] || \
    fail "NVIDIA module vermagic does not match the default kernel"
[[ "$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F license nvidia | head -n 1)" == \
    "Dual MIT/GPL" ]] || fail "NVIDIA kernel module is not the open module"
printf 'NVIDIA module file: %s\n' \
    "$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_ABI}" -F filename nvidia)"
chroot "${ROOT_MOUNT}" depmod -a "${DEFAULT_KERNEL_ABI}"
verify_nvidia_stack
# nvidia-kernel-common runs inside a chroot and can briefly observe the
# builder VM ABI through uname(2).  Remove only orphan initrds for which this
# target has no matching kernel image; never remove an installed kernel pair.
for candidate in "${ROOT_MOUNT}"/boot/initrd.img-*; do
    [[ -f "${candidate}" ]] || continue
    candidate_abi="${candidate##*/initrd.img-}"
    if [[ "${candidate_abi}" != "${DEFAULT_KERNEL_ABI}" && \
          ! -e "${ROOT_MOUNT}/boot/vmlinuz-${candidate_abi}" ]]; then
        rm -f -- "${candidate}"
    fi
done
DPKG_AUDIT="$(chroot "${ROOT_MOUNT}" dpkg --audit)"
[[ -z "${DPKG_AUDIT}" ]] || fail "target package database audit failed: ${DPKG_AUDIT}"
INITRAMFS_CONTENTS="$(chroot "${ROOT_MOUNT}" lsinitramfs \
    "/boot/initrd.img-${DEFAULT_KERNEL_ABI}")" || fail "default kernel initramfs could not be read"
printf 'NVIDIA entries in default kernel initramfs:\n'
printf '%s\n' "${INITRAMFS_CONTENTS}" | grep -E '/nvidia[^/]*\.ko([.].*)?$' || \
    printf 'No NVIDIA kernel modules embedded; modules are available from the root filesystem.\n'

printf 'Configuring base system ...\n'
printf 'rigpilot\n' >"${ROOT_MOUNT}/etc/hostname"
cat >"${ROOT_MOUNT}/etc/hosts" <<'EOF'
127.0.0.1 localhost
127.0.1.1 rigpilot
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF
install -d -m 0755 "${ROOT_MOUNT}/etc/netplan"
cat >"${ROOT_MOUNT}/etc/netplan/01-rigpilot.yaml" <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    all-ethernet:
      match:
        name: "e*"
      dhcp4: true
      dhcp6: true
      optional: true
      dhcp4-overrides:
        route-metric: 100
      dhcp6-overrides:
        route-metric: 100
EOF
chmod 0600 "${ROOT_MOUNT}/etc/netplan/01-rigpilot.yaml"

if ! chroot "${ROOT_MOUNT}" getent passwd user >/dev/null; then
    chroot "${ROOT_MOUNT}" useradd --create-home --shell /bin/bash user
fi
chroot "${ROOT_MOUNT}" passwd -l user >/dev/null
chroot "${ROOT_MOUNT}" usermod -aG sudo,video,render,plugdev user
chroot "${ROOT_MOUNT}" passwd -l root >/dev/null
# Some minimal debootstrap/useradd combinations can leave the passwd entry
# present without a usable home directory.  login(1) authenticates the user
# and then immediately terminates the session when chdir(2) fails, which looks
# like a password loop on the console.  Materialize and verify the home
# explicitly instead of relying on useradd's distribution defaults.
install -d -m 0750 -o "$(chroot "${ROOT_MOUNT}" id -u user)" \
    -g "$(chroot "${ROOT_MOUNT}" id -g user)" "${ROOT_MOUNT}/home/user"
chmod 0755 "${ROOT_MOUNT}"
chmod 0755 "${ROOT_MOUNT}/home"
cp -a "${ROOT_MOUNT}/etc/skel/." "${ROOT_MOUNT}/home/user/"
chown -R "$(chroot "${ROOT_MOUNT}" id -u user):$(chroot "${ROOT_MOUNT}" id -g user)" \
    "${ROOT_MOUNT}/home/user"
chroot "${ROOT_MOUNT}" runuser -u user -- /bin/sh -c \
    'cd /home/user && test "$(id -u)" = 1000' || fail "default user home is unusable"

cat >"${ROOT_MOUNT}/etc/default/grub" <<'EOF'
# Ubuntu GRUB consumes next_entry before falling back to this normal default.
# Keep normal boots pinned to entry 0; recovery is selected only by grub-reboot.
GRUB_DEFAULT=0
GRUB_TIMEOUT_STYLE=menu
GRUB_TIMEOUT=5
GRUB_DISTRIBUTOR="RigPilotOS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet"
# Preserve the current PCIe policy; it is not proof of the cause or repair of Xid 79.
# Hardware, firmware, link and driver causes still need device evidence.
GRUB_CMDLINE_LINUX="console=tty0 console=ttyS0,115200n8 pcie_aspm=off"
GRUB_TERMINAL_INPUT="console serial"
GRUB_TERMINAL_OUTPUT="gfxterm serial"
GRUB_GFXMODE="1024x768,auto"
GRUB_GFXPAYLOAD_LINUX="keep"
GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"
EOF

ROOT_UUID="$(blkid -s UUID -o value "${P4}")"
EFI_UUID="$(blkid -s UUID -o value "${P3}")"
CONFIG_UUID="$(blkid -s UUID -o value "${P1}")"
[[ -n "${ROOT_UUID}" && -n "${EFI_UUID}" && -n "${CONFIG_UUID}" ]] || fail "filesystem UUID discovery failed"
cat >"${ROOT_MOUNT}/etc/fstab" <<EOF
UUID=${ROOT_UUID} / ext4 defaults,noatime,errors=remount-ro 0 1
UUID=${EFI_UUID} /boot/efi vfat umask=0077 0 1
UUID=${CONFIG_UUID} /run/rigpilot-provision vfat rw,nosuid,nodev,noexec,uid=0,gid=0,fmask=0177,dmask=0077,nofail,x-systemd.device-timeout=15s 0 0
EOF

printf 'Validating and staging RigPilot Agent package ...\n'
validate_agent_package
AGENT_DIR="${ROOT_MOUNT}/usr/lib/rigpilot-bootstrap/package"
install -d -m 0700 -o root -g root "${AGENT_DIR}"
tar --extract --gzip --file "${AGENT_PACKAGE}" --directory "${AGENT_DIR}" \
    --strip-components=1 --no-same-owner --no-same-permissions
find "${AGENT_DIR}" -type d -exec chmod 0755 {} +
find "${AGENT_DIR}" -type f -exec chmod 0644 {} +
for executable in Install.sh Remove-Systemd.sh RigPilotAgent RigPilotAgentLauncher.sh \
    RigPilotAgentUpdater.py Start-Portable.sh; do
    [[ -f "${AGENT_DIR}/${executable}" && ! -L "${AGENT_DIR}/${executable}" ]] || \
        fail "Agent package executable is missing: ${executable}"
    chmod 0755 "${AGENT_DIR}/${executable}"
done
(cd "${AGENT_DIR}" && sha256sum --check --strict SHA256SUMS)
[[ "$(cat "${AGENT_DIR}/VERSION")" == "${AGENT_VERSION}" ]] || \
    fail "staged Agent VERSION changed unexpectedly"
# Execute the seeded binary through the target's own amd64 userland.  On an
# aarch64 + Rosetta builder the host has no amd64 shared libraries, so a bare
# host-side exec cannot resolve libz; the chroot exec measures the binary the
# way the shipped appliance will run it (2026-11-01).
[[ "$(chroot "${ROOT_MOUNT}" /usr/lib/rigpilot-bootstrap/package/RigPilotAgent --version)" == \
    "RigPilot Agent ${AGENT_VERSION}" ]] || fail "Agent binary version check failed"

printf 'Installing RigPilot appliance overlay ...\n'
cp -a --no-preserve=ownership "${OVERLAY_DIR}/." "${ROOT_MOUNT}/"
find "${ROOT_MOUNT}/usr/libexec" -depth \
    \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -o \
       -type d -name '__pycache__' \) -delete
# Files copied from a Windows-hosted overlay can carry a synthetic 0775 mode
# on parent directories.  Restore the fixed root-owned trust chains used by
# Spark session and agent_update.v1 validation instead of weakening either
# runtime policy.
TRUSTED_DIRS=(
    "${ROOT_MOUNT}"
    "${ROOT_MOUNT}/etc"
    "${ROOT_MOUNT}/etc/netplan"
    "${ROOT_MOUNT}/etc/profile.d"
    "${ROOT_MOUNT}/etc/ssh"
    "${ROOT_MOUNT}/etc/ssh/sshd_config.d"
    "${ROOT_MOUNT}/etc/systemd"
    "${ROOT_MOUNT}/etc/systemd/system"
    "${ROOT_MOUNT}/etc/systemd/journald.conf.d"
    "${ROOT_MOUNT}/etc/grub.d"
    "${ROOT_MOUNT}/usr"
    "${ROOT_MOUNT}/usr/bin"
    "${ROOT_MOUNT}/usr/libexec"
    "${ROOT_MOUNT}/usr/local"
    "${ROOT_MOUNT}/usr/local/sbin"
    "${ROOT_MOUNT}/opt"
    "${ROOT_MOUNT}/var"
    "${ROOT_MOUNT}/var/lib"
)
chmod 0755 "${TRUSTED_DIRS[@]}"
chown -R root:root \
    "${ROOT_MOUNT}/usr/lib/rigpilot-bootstrap" \
    "${ROOT_MOUNT}/usr/libexec" \
    "${ROOT_MOUNT}/etc/systemd/system" \
    "${ROOT_MOUNT}/etc/ssh/sshd_config.d" \
    "${ROOT_MOUNT}/usr/local/sbin" 2>/dev/null || true
chown root:root "${TRUSTED_DIRS[@]}"
find "${ROOT_MOUNT}/usr/libexec" "${ROOT_MOUNT}/usr/local/sbin" \
    -maxdepth 1 -type f \( -name 'rigpilot-*' -o -name 'rigpilotos-*' \) \
    -exec chmod 0755 {} + 2>/dev/null || true
# The journal cap is a fixed root-owned policy file; make its identity and
# mode deterministic regardless of the overlay source filesystem.
chown root:root "${ROOT_MOUNT}/etc/systemd/journald.conf.d/rigpilot-journal-cap.conf"
chmod 0644 "${ROOT_MOUNT}/etc/systemd/journald.conf.d/rigpilot-journal-cap.conf"
chmod 0755 "${ROOT_MOUNT}/etc/grub.d/41_rigpilotos_pci_recovery"
chmod 0644 "${ROOT_MOUNT}"/etc/systemd/system/rigpilot-*.service \
    "${ROOT_MOUNT}"/etc/systemd/system/rigpilot-*.timer \
    "${ROOT_MOUNT}"/etc/systemd/system/rigpilot-*.path \
    "${ROOT_MOUNT}"/etc/systemd/system/rigpilotos-*.service \
    "${ROOT_MOUNT}"/etc/systemd/system/rigpilotos-*.timer \
    "${ROOT_MOUNT}"/etc/systemd/system/rigpilotos-*.path 2>/dev/null || true
chmod 0600 "${ROOT_MOUNT}/etc/ssh/sshd_config.d/20-rigpilot-lan.conf"
chown root:root "${ROOT_MOUNT}/etc/netplan/01-rigpilot.yaml"
chmod 0600 "${ROOT_MOUNT}/etc/netplan/01-rigpilot.yaml"
chown root:root "${ROOT_MOUNT}/etc/profile.d/rigpilotos-welcome.sh"
chmod 0644 "${ROOT_MOUNT}/etc/profile.d/rigpilotos-welcome.sh"
for trusted_dir in "${TRUSTED_DIRS[@]}"; do
    [[ "$(stat -c '%a %u:%g' "${trusted_dir}")" == "755 0:0" ]] || \
        fail "trusted directory mode or owner is invalid: ${trusted_dir}"
done
[[ "$(stat -c '%a %u:%g' \
    "${ROOT_MOUNT}/etc/systemd/journald.conf.d/rigpilot-journal-cap.conf")" == \
    "644 0:0" ]] || fail "journal cap file mode or owner is invalid"
[[ "$(stat -c '%a %u:%g' \
    "${ROOT_MOUNT}/etc/netplan/01-rigpilot.yaml")" == "600 0:0" ]] || \
    fail "Netplan file mode or owner is invalid"
[[ "$(stat -c '%a %u:%g' \
    "${ROOT_MOUNT}/etc/profile.d/rigpilotos-welcome.sh")" == "644 0:0" ]] || \
    fail "login summary file mode or owner is invalid"
[[ "$(stat -c '%a %u:%g' \
    "${ROOT_MOUNT}/etc/ssh/sshd_config.d/20-rigpilot-lan.conf")" == \
    "600 0:0" ]] || fail "SSH policy mode or owner is invalid"
[[ -d "${ROOT_MOUNT}/usr/libexec" && ! -L "${ROOT_MOUNT}/usr/libexec" ]] || \
    fail "appliance overlay directory is missing or unsafe"
OVERLAY_BYTECODE="$(find "${ROOT_MOUNT}/usr/libexec" \
    \( -type d -name '__pycache__' -o -type f \
       \( -name '*.pyc' -o -name '*.pyo' \) \) -print -quit)" || \
    fail "appliance overlay bytecode scan failed"
[[ -z "${OVERLAY_BYTECODE}" ]] || \
    fail "Python bytecode cache leaked into the appliance overlay"

DEFAULT_KERNEL_PATH="$(chroot "${ROOT_MOUNT}" readlink -f /boot/vmlinuz)"
case "${DEFAULT_KERNEL_PATH}" in
    /boot/vmlinuz-*) ;;
    *) fail "default kernel link is invalid" ;;
esac
DEFAULT_KERNEL_VERSION="${DEFAULT_KERNEL_PATH#/boot/vmlinuz-}"
[[ -s "${ROOT_MOUNT}${DEFAULT_KERNEL_PATH}" ]] || fail "default kernel is missing"
[[ -s "${ROOT_MOUNT}/boot/initrd.img-${DEFAULT_KERNEL_VERSION}" ]] || \
    fail "matching initrd is missing"
NVIDIA_MODULE_VERSION="$(chroot "${ROOT_MOUNT}" modinfo \
    -k "${DEFAULT_KERNEL_VERSION}" -F version nvidia | head -n 1)"
[[ "${NVIDIA_MODULE_VERSION}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
    fail "NVIDIA module is not the required ${NVIDIA_DRIVER_VERSION} release"
NVIDIA_MODULE_VERMAGIC="$(chroot "${ROOT_MOUNT}" modinfo \
    -k "${DEFAULT_KERNEL_VERSION}" -F vermagic nvidia | head -n 1)"
[[ "${NVIDIA_MODULE_VERMAGIC}" == "${DEFAULT_KERNEL_VERSION} "* ]] || \
    fail "NVIDIA module does not match the default kernel"
[[ "$(chroot "${ROOT_MOUNT}" modinfo -k "${DEFAULT_KERNEL_VERSION}" \
    -F license nvidia | head -n 1)" == "Dual MIT/GPL" ]] || \
    fail "NVIDIA module is not the open kernel module"

cat >"${ROOT_MOUNT}/etc/rigpilotos-release" <<EOF
NAME=RigPilotOS
VERSION=${IMAGE_RELEASE}
BUILD_REVISION=${BUILD_REVISION}
UBUNTU_VERSION=${UBUNTU_VERSION}
AGENT_SEED_VERSION=$(cat "${AGENT_DIR}/VERSION")
WIFI_SUPPORT=disabled
IMAGE_KERNEL=${DEFAULT_KERNEL_VERSION}
NVIDIA_DRIVER_BRANCH=${NVIDIA_DRIVER_BRANCH}
NVIDIA_DRIVER_VERSION=${NVIDIA_MODULE_VERSION}
NVIDIA_KERNEL_MODULE=open
PCI_BAR_RECOVERY_AVAILABLE=true
PCI_BAR_RECOVERY_DEFAULT=false
ROOTFS_MKFS_OPTIONS_SHA256=${ROOTFS_MKFS_OPTIONS_SHA256}
ROOTFS_E2FSPROGS=${ROOTFS_E2FSPROGS}
EOF

install -d -m 0755 "${ROOT_MOUNT}/usr/share/rigpilotos"
chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Package}\t${Version}\n' \
    >"${ROOT_MOUNT}/usr/share/rigpilotos/package-manifest.txt"
if [[ -f "${ROOT_MOUNT}/usr/share/doc/libnvidia-compute/copyright" ]]; then
    install -m 0644 "${ROOT_MOUNT}/usr/share/doc/libnvidia-compute/copyright" \
        "${ROOT_MOUNT}/usr/share/rigpilotos/NVIDIA-DRIVER-LICENSE.txt"
fi

printf 'Installing BIOS and UEFI boot loaders ...\n'
grep -qx 'blacklist nouveau' "${ROOT_MOUNT}/etc/modprobe.d/rigpilotos-nvidia.conf" ||
    fail "Nouveau automatic loading is not disabled"
grep -qx 'options nouveau modeset=0' "${ROOT_MOUNT}/etc/modprobe.d/rigpilotos-nvidia.conf" ||
    fail "Nouveau modesetting policy is missing"
chroot "${ROOT_MOUNT}" update-initramfs -u -k all
install -d -m 0755 "${ROOT_MOUNT}/boot/grub"
chroot "${ROOT_MOUNT}" grub-install --target=i386-pc --boot-directory=/boot \
    --no-floppy --recheck "${TARGET_DEVICE}"
chroot "${ROOT_MOUNT}" grub-install --target=x86_64-efi \
    --efi-directory=/boot/efi --boot-directory=/boot --removable --no-nvram --recheck
validate_generated_grub() {
    python3 - "${ROOT_MOUNT}/boot/grub/grub.cfg" "${ROOT_UUID}" <<'PY'
"""Read-only GRUB kernel root validation against the actual root partition UUID."""
import json
from pathlib import Path
import re
import shlex
import sys


def check_grub_cfg(text, expected_root_uuid):
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', expected_root_uuid):
        raise ValueError('actual root partition UUID is missing or invalid')
    expected = 'root=UUID=' + expected_root_uuid
    entries = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not re.match(r'^\s*(?:linux|linuxefi)(?:\s|$)', line):
            continue
        tokens = shlex.split(line, comments=True, posix=True)
        roots = [token for token in tokens[2:] if token.startswith('root=')]
        if len(tokens) < 3 or roots != [expected]:
            raise ValueError(f'GRUB line {line_number}: {tokens[0]} requires exactly {expected}; found {roots!r}')
        entries.append({'line': line_number, 'command': tokens[0], 'kernel': tokens[1], 'root': expected})
    if not entries:
        raise ValueError('GRUB has no linux/linuxefi kernel entries')
    return {'root_uuid': expected_root_uuid, 'linux_entries': len(entries), 'entries': entries}


if __name__ == '__main__':
    try:
        result = check_grub_cfg(Path(sys.argv[1]).read_text(), sys.argv[2])
    except (ValueError, IndexError, OSError) as error:
        raise SystemExit(str(error))
    print(json.dumps(result))
PY
}
# Reject transient builder /dev names even if a recovery entry has the right UUID.
chroot "${ROOT_MOUNT}" update-grub
validate_generated_grub || fail "generated GRUB kernel root UUID is invalid"
chroot "${ROOT_MOUNT}" grub-editenv /boot/grub/grubenv create
chroot "${ROOT_MOUNT}" grub-reboot rigpilotos-pci-realloc
chroot "${ROOT_MOUNT}" grub-editenv /boot/grub/grubenv list | \
    grep -qx 'next_entry=rigpilotos-pci-realloc' || \
    fail "GRUB one-time recovery entry cannot be scheduled"
chroot "${ROOT_MOUNT}" grub-editenv /boot/grub/grubenv unset next_entry
chroot "${ROOT_MOUNT}" test -s /boot/grub/grub.cfg || fail "GRUB configuration was not generated"
chroot "${ROOT_MOUNT}" grep -q -- \
    "--id rigpilotos-pci-realloc" /boot/grub/grub.cfg || \
    fail "PCI BAR recovery GRUB entry was not generated"
chroot "${ROOT_MOUNT}" grep -q -- \
    "--id rigpilotos-pci-nocrs" /boot/grub/grub.cfg || \
    fail "advanced PCI BAR recovery GRUB entry was not generated"
GRUB_ENV="$(chroot "${ROOT_MOUNT}" grub-editenv /boot/grub/grubenv list)" || \
    fail "GRUB environment could not be read"
if printf '%s\n' "${GRUB_ENV}" | grep -q '^next_entry='; then
    fail "a one-time GRUB entry leaked into the released image"
fi
grep -q 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"' "${ROOT_MOUNT}/etc/default/grub" || \
    fail "normal GRUB entry was unexpectedly changed by PCI BAR recovery support"
chroot "${ROOT_MOUNT}" test -s /boot/efi/EFI/BOOT/BOOTX64.EFI || fail "UEFI removable loader is missing"

chroot "${ROOT_MOUNT}" systemctl enable \
    systemd-networkd.service systemd-resolved.service systemd-timesyncd.service \
    ssh.service
chroot "${ROOT_MOUNT}" systemctl disable ssh.socket 2>/dev/null || true
chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Status} ${Version}\n' systemd-timesyncd | \
    grep -q '^install ok installed ' || fail "systemd-timesyncd is not installed"
chroot "${ROOT_MOUNT}" dpkg-query -W -f='${Status} ${Version}\n' systemd-resolved | \
    grep -q '^install ok installed ' || fail "systemd-resolved is not installed"
chroot "${ROOT_MOUNT}" test -x /lib/systemd/systemd-timesyncd || \
    fail "systemd-timesyncd executable is missing"
chroot "${ROOT_MOUNT}" test -s /lib/systemd/system/systemd-timesyncd.service || \
    fail "systemd-timesyncd unit is missing"
for unit in \
    rigpilotos-nvidia-guard.service \
    rigpilotos-ssh-host-keys.service \
    rigpilot-firstboot-enroll.service \
    rigpilot-firstboot-enroll.path \
    rigpilot-firstboot-enroll.timer \
    rigpilotos-smoke-ready.service \
    rigpilotos-grow-root.service; do
    if [[ -f "${ROOT_MOUNT}/etc/systemd/system/${unit}" ]]; then
        chroot "${ROOT_MOUNT}" systemctl enable "${unit}"
    fi
done
# 0.2.12 run-managed driver parity with 0.2.11: the deb stack shipped and
# enabled nvidia-persistenced; the .run installer ships only the binary.
# The overlay unit restores the service and it MUST be enabled.  Holding
# device state between clients is not evidence that physical Xid 79 faults
# are repaired; actual GPU validation remains a separate acceptance scope.
chroot "${ROOT_MOUNT}" test -x /usr/bin/nvidia-persistenced || \
    fail "nvidia-persistenced binary is missing from the .run install"
chroot "${ROOT_MOUNT}" systemctl enable nvidia-persistenced.service
chroot "${ROOT_MOUNT}" systemctl is-enabled --quiet nvidia-persistenced.service || \
    fail "nvidia-persistenced is not enabled"

# Never clone SSH host identity.  openssh-server creates keys while the image
# is assembled, so remove those build-time keys after validating the policy.
# The ordered one-shot service generates a unique set on each installed host
# before ssh.service starts for the first time.
install -d -m 0755 "${ROOT_MOUNT}/run/sshd"
chroot "${ROOT_MOUNT}" /usr/sbin/sshd -t
SSH_ALLOW_USERS_EXPECTED="$(printf '%s\n' \
    user@10.0.0.0/8 user@172.16.0.0/12 user@192.168.0.0/16 \
    user@100.64.0.0/10 | sort)"
SSH_ALLOW_USERS_ACTUAL="$(chroot "${ROOT_MOUNT}" /usr/sbin/sshd -T | awk \
    '$1 == "allowusers" { for (i = 2; i <= NF; i++) { n = split($i, a, ","); for (j = 1; j <= n; j++) if (a[j] != "") print a[j] } }' | \
    sort -u)"
[[ "${SSH_ALLOW_USERS_ACTUAL}" == "${SSH_ALLOW_USERS_EXPECTED}" ]] || \
    fail "LAN-only SSH AllowUsers policy is invalid"
chroot "${ROOT_MOUNT}" /usr/sbin/sshd -T | \
    grep -x 'kbdinteractiveauthentication no' >/dev/null || \
    fail "SSH keyboard-interactive authentication policy is invalid"
chroot "${ROOT_MOUNT}" /usr/sbin/sshd -T \
    -C user=user,addr=192.168.1.2,host=rigpilot | \
    grep -x 'passwordauthentication yes' >/dev/null || fail "private-source SSH password policy is invalid"
chroot "${ROOT_MOUNT}" /usr/sbin/sshd -T \
    -C user=user,addr=203.0.113.2,host=rigpilot | \
    grep -x 'passwordauthentication no' >/dev/null || fail "public-source SSH password policy is invalid"
chroot "${ROOT_MOUNT}" /usr/sbin/sshd -T \
    -C user=root,addr=192.168.1.2,host=rigpilot | \
    grep -x 'permitrootlogin no' >/dev/null || fail "direct root SSH policy is invalid"
rm -f -- "${ROOT_MOUNT}"/etc/ssh/ssh_host_*_key \
    "${ROOT_MOUNT}"/etc/ssh/ssh_host_*_key.pub
[[ -d "${ROOT_MOUNT}/etc/ssh" && ! -L "${ROOT_MOUNT}/etc/ssh" ]] || \
    fail "SSH directory is missing or unsafe"
SSH_HOST_KEYS="$(find "${ROOT_MOUNT}/etc/ssh" -maxdepth 1 -type f \
    -name 'ssh_host_*_key*' -print -quit)" || fail "SSH host key scan failed"
[[ -z "${SSH_HOST_KEYS}" ]] || fail "build-time SSH host keys were not removed"

cat >"${CONFIG_MOUNT}/README.txt" <<'EOF'
RigPilotOS first boot registration

1. Connect wired Ethernet with DHCP. This image does not configure Wi-Fi.
2. Generate a one-time enrollment code in the RigPilot controller.
3. Copy rigpilot-enroll.example.json to rigpilot-enroll.json.
4. Replace the example token and optionally set name.
5. Safely eject the drive and boot it. Registration waits for verified HTTPS
   connectivity. The enrollment file is deleted only after a successful
   registration and Agent installation.

To replace the disk of an existing device while keeping its controller name,
history and settings, generate a one-time inheritance code for that offline
device. Copy rigpilot-replace.example.json to rigpilot-replace.json and replace
the token. Never keep rigpilot-enroll.json and rigpilot-replace.json together.

Before first use, configure RIGPILOT_CONTROLLER_URL in the installed
/etc/rigpilotos-controller.env and set a unique password for the local user
on the private target disk. The factory user account is password-locked.
NVIDIA BAR diagnosis: sudo rigpilot-control pci-bar
One-time safe recovery: sudo rigpilot-control pci-recovery
Advanced one-time ACPI fallback: sudo rigpilot-control pci-fallback
EOF
cat >"${CONFIG_MOUNT}/rigpilot-enroll.example.json" <<'EOF'
{
  "schema": "rigpilot.enrollment.v1",
  "enrollment_token": "rpe_REPLACE_WITH_CONTROLLER_GENERATED_ONE_TIME_TOKEN"
}
EOF
cat >"${CONFIG_MOUNT}/rigpilot-replace.example.json" <<'EOF'
{
  "schema": "rigpilot.replacement.v1",
  "replacement_token": "rpr_REPLACE_WITH_CONTROLLER_GENERATED_ONE_TIME_TOKEN"
}
EOF

rm -f "${ROOT_MOUNT}/usr/sbin/policy-rc.d"
rm -f "${ROOT_MOUNT}/etc/resolv.conf"
ln -s ../run/systemd/resolve/stub-resolv.conf "${ROOT_MOUNT}/etc/resolv.conf"
chroot "${ROOT_MOUNT}" apt-get clean
python3 "${POLICY_HELPER}" \
    --root "${ROOT_MOUNT}" --provision-root "${CONFIG_MOUNT}" \
    --dkms-deb "${RETAINED_DKMS_DEB}" \
    --agent-version "${AGENT_VERSION}" --build-revision "${BUILD_REVISION}" || \
    fail "factory policy/DKMS archive verification failed"
rm -rf "${ROOT_MOUNT}/var/lib/apt/lists"/*
rm -rf "${ROOT_MOUNT}/tmp"/* "${ROOT_MOUNT}/var/tmp"/*
# DKMS may create a per-builder signing identity during package installation.
# The installed module remains; new machines must generate their own keys.
rm -f -- "${ROOT_MOUNT}/var/lib/dkms/mok.key" \
    "${ROOT_MOUNT}/var/lib/dkms/mok.pub" \
    "${ROOT_MOUNT}/var/lib/shim-signed/mok/MOK.priv" \
    "${ROOT_MOUNT}/var/lib/shim-signed/mok/MOK.der"
rm -f -- \
    "${ROOT_MOUNT}/var/lib/systemd/random-seed" \
    "${ROOT_MOUNT}/var/lib/systemd/timesync/clock" \
    "${ROOT_MOUNT}/var/lib/dbus/machine-id" \
    "${ROOT_MOUNT}/etc/netplan/20-rigpilot-wifi.yaml" \
    "${ROOT_MOUNT}/var/lib/rigpilotos/root-grown" \
    "${CONFIG_MOUNT}/rigpilot-wifi.json" \
    "${CONFIG_MOUNT}/.rigpilot-enrollment-retry" \
    "${CONFIG_MOUNT}/rigpilot-enroll.json" \
    "${CONFIG_MOUNT}/rigpilot-replace.json"
rm -f -- "${ROOT_MOUNT}/etc/machine-id"
: >"${ROOT_MOUNT}/etc/machine-id"
rm -f -- "${ROOT_MOUNT}"/etc/ssh/ssh_host_*_key \
    "${ROOT_MOUNT}"/etc/ssh/ssh_host_*_key.pub
find "${ROOT_MOUNT}/var/log" -type f -exec truncate -s 0 {} +

# The release master must not carry identity, entropy, clock, provisioning,
# network credentials or first-boot completion state from the builder.  Keep
# these assertions immediately before publication so later build changes
# cannot silently reintroduce them after an earlier cleanup step.
[[ -f "${ROOT_MOUNT}/etc/machine-id" && \
   ! -L "${ROOT_MOUNT}/etc/machine-id" && \
   ! -s "${ROOT_MOUNT}/etc/machine-id" ]] || \
    fail "released image contains a machine identity"
for forbidden_path in \
    "${ROOT_MOUNT}/var/lib/dkms/mok.key" \
    "${ROOT_MOUNT}/var/lib/dkms/mok.pub" \
    "${ROOT_MOUNT}/var/lib/shim-signed/mok/MOK.priv" \
    "${ROOT_MOUNT}/var/lib/shim-signed/mok/MOK.der" \
    "${ROOT_MOUNT}/var/lib/dbus/machine-id" \
    "${ROOT_MOUNT}/var/lib/systemd/random-seed" \
    "${ROOT_MOUNT}/var/lib/systemd/timesync/clock" \
    "${ROOT_MOUNT}/var/lib/rigpilot-agent/state.json" \
    "${ROOT_MOUNT}/var/lib/rigpilot-agent/.state.json.replacement-pending" \
    "${ROOT_MOUNT}/var/lib/rigpilot-agent/.state.json.enrollment-pending" \
    "${ROOT_MOUNT}/var/lib/rigpilot-agent/firstboot-registration.json" \
    "${ROOT_MOUNT}/var/lib/rigpilot-agent/pending-hiveos-start.json" \
    "${ROOT_MOUNT}/etc/rigpilot-agent/config.json" \
    "${ROOT_MOUNT}/etc/netplan/20-rigpilot-wifi.yaml" \
    "${ROOT_MOUNT}/var/lib/rigpilotos/root-grown" \
    "${ROOT_MOUNT}/run/rigpilot-provision/rigpilot-wifi.json" \
    "${ROOT_MOUNT}/run/rigpilot-provision/.rigpilot-enrollment-retry" \
    "${ROOT_MOUNT}/run/rigpilot-provision/rigpilot-enroll.json" \
    "${ROOT_MOUNT}/run/rigpilot-provision/rigpilot-replace.json" \
    "${CONFIG_MOUNT}/rigpilot-wifi.json" \
    "${CONFIG_MOUNT}/.rigpilot-enrollment-retry" \
    "${CONFIG_MOUNT}/rigpilot-enroll.json" \
    "${CONFIG_MOUNT}/rigpilot-replace.json" \
    "${ROOT_MOUNT}/var/lib/rigpilot-agent/hiveos-components" \
    "${ROOT_MOUNT}/etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf" \
    "${ROOT_MOUNT}/opt/rigpilot-agent/Setup-HiveOS-Runtime.sh"; do
    [[ ! -e "${forbidden_path}" && ! -L "${forbidden_path}" ]] || \
        fail "released image contains forbidden mutable state: ${forbidden_path}"
done
# Retired HiveOS environment: no dedicated non-root account, no private
# bubblewrap bundle anywhere in the released tree (0.2.11 removal batch).
if chroot "${ROOT_MOUNT}" getent passwd rigpilot-hiveos >/dev/null 2>&1; then
    fail "released image contains the retired rigpilot-hiveos account"
fi
if chroot "${ROOT_MOUNT}" getent group rigpilot-hiveos >/dev/null 2>&1; then
    fail "released image contains the retired rigpilot-hiveos group"
fi
BUBBLEWRAP_RESIDUE="$(find "${ROOT_MOUNT}" -xdev \
    \( -name 'bwrap' -o -name 'bwrap-*' -o -name 'hiveos-runtime' \) -print -quit 2>/dev/null)"
[[ -z "${BUBBLEWRAP_RESIDUE}" ]] || \
    fail "released image contains a private bubblewrap/HiveOS runtime remnant: ${BUBBLEWRAP_RESIDUE}"
# The registration partition is a separate noexec vfat mount, so the root
# filesystem scan above never crosses into it; scan it explicitly for the
# same retired runtime names.
PROVISION_BUBBLEWRAP_RESIDUE="$(find "${CONFIG_MOUNT}" \
    \( -name 'bwrap' -o -name 'bwrap-*' -o -name 'hiveos-runtime' \) -print -quit 2>/dev/null)"
[[ -z "${PROVISION_BUBBLEWRAP_RESIDUE}" ]] || \
    fail "registration partition contains a bubblewrap/HiveOS runtime remnant: ${PROVISION_BUBBLEWRAP_RESIDUE}"
# Fresh images may not yet have Agent directories. Existing entries must be
# real directories, and a failed scan is never evidence of an empty directory.
require_no_transactions() {
    local directory="$1" residue
    shift
    [[ ! -L "${directory}" ]] || fail "Agent transaction directory is unsafe: ${directory}"
    [[ -e "${directory}" ]] || return 0
    [[ -d "${directory}" ]] || fail "Agent transaction directory is not a directory: ${directory}"
    residue="$(find "${directory}" -maxdepth 1 "$@" -print -quit)" || \
        fail "Agent transaction scan failed: ${directory}"
    [[ -z "${residue}" ]] || fail "released image contains an unfinished Agent transaction: ${directory}"
}
require_no_transactions "${ROOT_MOUNT}/var/lib/rigpilot-agent" \
    \( -name '.state.json.*.tmp' -o -name '.firstboot-registration.json.appliance-*' \)
require_no_transactions "${ROOT_MOUNT}/etc/rigpilot-agent" -name '.config.json.appliance-*'
[[ -d "${ROOT_MOUNT}/etc/ssh" && ! -L "${ROOT_MOUNT}/etc/ssh" ]] || \
    fail "SSH directory is missing or unsafe"
SSH_HOST_KEYS="$(find "${ROOT_MOUNT}/etc/ssh" -maxdepth 1 \
    -name 'ssh_host_*_key*' -print -quit)" || fail "SSH host key scan failed"
[[ -z "${SSH_HOST_KEYS}" ]] || fail "released image contains SSH host keys"

# Refuse to publish an image without practical room for first-boot identity,
# Agent state, miner packages, logs and an online Agent upgrade.  This also
# detects when a newer Ubuntu/NVIDIA package set outgrows the fixed layout.
ROOT_FREE_KIB="$(df -Pk "${ROOT_MOUNT}" | awk 'NR == 2 { print $4 }')"
ROOT_FREE_INODES="$(df -Pi "${ROOT_MOUNT}" | awk 'NR == 2 { print $4 }')"
[[ "${ROOT_FREE_KIB}" =~ ^[0-9]+$ ]] || \
    fail "target root free-space reading is invalid: ${ROOT_FREE_KIB:-unknown} KiB"
ROOT_FREE_BYTES=$((ROOT_FREE_KIB * 1024))
[[ "${ROOT_FREE_BYTES}" =~ ^[0-9]+$ && \
   "${ROOT_FREE_BYTES}" -ge "${MINIMUM_ROOT_FREE_BYTES}" ]] || \
    fail "target root has insufficient free space after cleanup: ${ROOT_FREE_BYTES:-unknown} bytes"
[[ "${ROOT_FREE_INODES}" =~ ^[0-9]+$ && \
   "${ROOT_FREE_INODES}" -ge "${MINIMUM_ROOT_FREE_INODES}" ]] || \
    fail "target root has insufficient free inodes after cleanup: ${ROOT_FREE_INODES:-unknown}"
printf 'Post-cleanup root headroom: %s bytes, %s inodes\n' \
    "${ROOT_FREE_BYTES}" "${ROOT_FREE_INODES}"
sync

for path in "${ROOT_MOUNT}/run" "${ROOT_MOUNT}/sys" "${ROOT_MOUNT}/proc" "${ROOT_MOUNT}/dev"; do
    umount -R -- "${path}"
done
umount "${ROOT_MOUNT}/boot/efi"
umount "${CONFIG_MOUNT}"
umount "${ROOT_MOUNT}"
for partition in "${P1}" "${P3}" "${P4}"; do
    if [[ -n "$(findmnt -rn -S "${partition}")" ]]; then
        fail "target partition is still mounted after build cleanup: ${partition}"
    fi
done
sync
udevadm settle --timeout=30
# The Windows orchestrator performs e2fsck in a fresh disposable VM after
# this builder powers off.  Some kernels keep the just-unmounted partition
# exclusively open until the current VM exits, which makes an in-VM e2fsck
# report a false "is in use" failure.
sgdisk --verify "${TARGET_DEVICE}"

printf '\nRigPilotOS target build complete.\n'
printf 'Release: %s\nUbuntu: %s\nNVIDIA: %s open (official NVIDIA APT)\nAgent seed: %s\n' \
    "${IMAGE_RELEASE}" "${UBUNTU_VERSION}" "${NVIDIA_DRIVER_VERSION}" \
    "${AGENT_VERSION}"
printf 'Target: %s (%s bytes)\n' "${TARGET_DEVICE}" "${TARGET_BYTES}"
