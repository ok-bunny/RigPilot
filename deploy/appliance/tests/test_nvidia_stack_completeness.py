"""Run the actual build gate against a fake chroot and missing stack members."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

import pytest


APPLIANCE = Path(__file__).resolve().parents[1]
VERSION = "615.78.08"
KERNEL = "6.8.0-142-generic"

CHROOT = r'''
chroot() {
    local root="$1" field module target
    shift
    case "$1" in
        modinfo)
            field="$5"; module="$6"
            [[ "$module" != "${MISSING_MODULE:-}" ]] || return 1
            case "$field" in
                version) printf '%s\n' "${MODULE_VERSION:-615.78.08}" ;;
                vermagic) printf '%s SMP\n' "${MODULE_KERNEL:-6.8.0-142-generic}" ;;
                license) printf '%s\n' 'Dual MIT/GPL' ;;
                firmware) printf '%s\n' 'nvidia/615.78.08/gsp_ga10x.bin' ;;
                *) return 1 ;;
            esac ;;
        readlink)
            target="$(readlink -e "$root$3")" || return 1
            printf '%s\n' "${target#"$root"}" ;;
        test) test "$2" "$root$3" ;;
        grep) grep "$2" "$3" "$root$4" ;;
        *) return 1 ;;
    esac
}
'''


def fixture_stack(tmp_path):
    libraries = tmp_path / "usr/lib/x86_64-linux-gnu"
    libraries.mkdir(parents=True)
    for name in ("libnvidia-ml", "libcuda", "libnvidia-opencl", "libnvidia-cfg"):
        (libraries / f"{name}.so.{VERSION}").write_bytes(b"fixture-elf")
        (libraries / f"{name}.so.1").symlink_to(f"{name}.so.{VERSION}")
    firmware = tmp_path / f"lib/firmware/nvidia/{VERSION}/gsp_ga10x.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"fixture-firmware")
    icd = tmp_path / "etc/OpenCL/vendors/nvidia.icd"
    icd.parent.mkdir(parents=True)
    icd.write_text("libnvidia-opencl.so.1\n")


def run_gate(tmp_path, **env):
    source = (APPLIANCE / "build-rigpilotos-target.sh").read_text()
    function = "verify_nvidia_stack() {\n" + source.split("verify_nvidia_stack() {\n", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    script = ("set -Eeuo pipefail\nfail() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
              + f"ROOT_MOUNT={shlex.quote(str(tmp_path))}\n"
              + f"NVIDIA_DRIVER_VERSION={VERSION}\nDEFAULT_KERNEL_ABI={KERNEL}\n"
              + CHROOT + function + "verify_nvidia_stack\n")
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                          env=dict(os.environ, **env), timeout=10)


def test_complete_stack_passes(tmp_path):
    fixture_stack(tmp_path)
    result = run_gate(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module", ["nvidia", "nvidia_uvm", "nvidia_modeset", "nvidia_drm"])
def test_missing_module_cannot_pass_with_base_nvidia_present(tmp_path, module):
    fixture_stack(tmp_path)
    result = run_gate(tmp_path, MISSING_MODULE=module)
    assert result.returncode != 0
    assert f"required NVIDIA module is missing: {module}" in result.stderr


@pytest.mark.parametrize("override", [{"MODULE_VERSION": "615.71.09"}, {"MODULE_KERNEL": "6.8.0-999-generic"}])
def test_wrong_module_version_or_kernel_fails(tmp_path, override):
    fixture_stack(tmp_path)
    result = run_gate(tmp_path, **override)
    assert result.returncode != 0


@pytest.mark.parametrize("missing", [
    f"lib/firmware/nvidia/{VERSION}/gsp_ga10x.bin",
    f"usr/lib/x86_64-linux-gnu/libcuda.so.{VERSION}",
    f"usr/lib/x86_64-linux-gnu/libnvidia-ml.so.{VERSION}",
    f"usr/lib/x86_64-linux-gnu/libnvidia-opencl.so.{VERSION}",
    f"usr/lib/x86_64-linux-gnu/libnvidia-cfg.so.{VERSION}",
    "etc/OpenCL/vendors/nvidia.icd",
])
def test_missing_firmware_library_or_icd_fails(tmp_path, missing):
    fixture_stack(tmp_path)
    (tmp_path / missing).unlink()
    result = run_gate(tmp_path)
    assert result.returncode != 0


def test_wrong_version_library_symlink_is_rejected(tmp_path):
    fixture_stack(tmp_path)
    libraries = tmp_path / "usr/lib/x86_64-linux-gnu"
    (libraries / "libcuda.so.615.71.09").write_bytes(b"wrong-version")
    (libraries / "libcuda.so.1").unlink()
    (libraries / "libcuda.so.1").symlink_to("libcuda.so.615.71.09")
    result = run_gate(tmp_path)
    assert result.returncode != 0
    assert "NVIDIA library version differs: libcuda" in result.stderr

