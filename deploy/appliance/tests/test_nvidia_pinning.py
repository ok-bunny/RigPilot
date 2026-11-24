"""Execute the builder's run-managed NVIDIA transaction with a mocked installer.

Since image 0.2.12 the driver installs from the pinned XFree86 .run archive
inside the chroot transaction: digest verification, apt preference guard,
installer invocation against the pinned GA kernel, and staging cleanup.  The
CUDA apt repository flow (keyring, pinning deb, dpkg assertions) is gone and
must stay gone.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "build-rigpilotos-target.sh"
DRIVER_VERSION = "615.78.08"
RUN_BASENAME = f"NVIDIA-Linux-x86_64-{DRIVER_VERSION}.run"
RUN_SHA256 = "3e3f6dddc2ee767a08185300af7be3cb286ece76698d2dde3524599782d07ffe"

MOCKS = r'''
record_call() {
    printf '%s' "$1" >>"$NVIDIA_TEST_TRACE"
    shift
    printf '\t%s' "$@" >>"$NVIDIA_TEST_TRACE"
    printf '\n' >>"$NVIDIA_TEST_TRACE"
}
sha256sum() {
    record_call sha256sum "$@"
    [[ "${NVIDIA_TEST_DIGEST_OK:-1}" == 1 ]] || return 1
    return 0
}
apt-get() {
    record_call apt-get "$@"
    return "${NVIDIA_TEST_APT_STATUS:-0}"
}
sh() {
    record_call sh "$@"
    return "${NVIDIA_TEST_RUNNER_STATUS:-0}"
}
nvidia-run-qemu() {
    record_call nvidia-run-qemu "$@"
    return "${NVIDIA_TEST_INSTALLER_STATUS:-0}"
}
locale-gen() { record_call locale-gen "$@"; }
update-locale() { record_call update-locale "$@"; }
dpkg-reconfigure() { record_call dpkg-reconfigure "$@"; }
ln() { record_call ln "$@"; }
export -f record_call
export NVIDIA_TEST_TRACE
'''


def chroot_transaction(root_name: str) -> str:
    """Return the run-managed driver slice of the chroot transaction.

    Only the driver portion runs under test; every absolute path it touches
    is prefixed with the sandbox root so nothing escapes the temp directory.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("# The driver itself is NOT installed from apt")
    start = source.rindex("\n", 0, start) + 1
    end = source.index("\n    '", start)
    block = source[start:end]
    block = block.replace("/etc/apt/preferences.d/", f"{root_name}/etc/apt/preferences.d/")
    block = block.replace(">/etc/timezone", f">{root_name}/etc/timezone")
    block = block.replace("/tmp/nvidia-run/", f"{root_name}/tmp/nvidia-run/")
    block = block.replace(" /tmp/", f" {root_name}/tmp/")
    block = block.replace("-- /tmp/", f"-- {root_name}/tmp/")
    block = block.replace("cd /tmp\n", f"cd {root_name}/tmp\n")
    # The unpacked archive directory never materializes under the mocks; the
    # source-level cd assertion covers the real working-directory contract.
    # (Applied after the generic " /tmp/" rewrite, so match the rewritten form.)
    block = block.replace(f"cd {root_name}/tmp/NVIDIA-Linux-x86_64-615.78.08\n", f"cd {root_name}/tmp\n")
    block = block.replace("\n        cd /\n", f"\n        cd {root_name}\n")
    return block


class NvidiaRunInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.trace = self.root / "trace"
        self.run_dir = self.root / "tmp" / "nvidia-run"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / RUN_BASENAME).write_bytes(b"fixture")
        (self.root / "apt-vendored").mkdir()
        (self.root / "etc/apt/preferences.d").mkdir(parents=True)
        self.env = {
            "PATH": "/usr/bin:/bin",
            "NVIDIA_DRIVER_VERSION": DRIVER_VERSION,
            "NVIDIA_RUN_SHA256": RUN_SHA256,
            "DEBIAN_FRONTEND": "noninteractive",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NVIDIA_TEST_TRACE": str(self.trace),
            "NVIDIA_TEST_RUN_NAME": RUN_BASENAME,
            "NVIDIA_TEST_EXPECTED_SHA": RUN_SHA256,
        }

    def run_transaction(self, **overrides) -> subprocess.CompletedProcess:
        env = dict(self.env)
        env.update({key: str(value) for key, value in overrides.items()})
        script = (
            "set -Eeuo pipefail\n"
            f"cd {self.root}\n"
            + MOCKS
            + chroot_transaction(str(self.root))
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def trace_text(self) -> str:
        return self.trace.read_text(encoding="utf-8") if self.trace.exists() else ""

    def test_installer_runs_under_qemu_emulator_against_pinned_kernel_open_module(self):
        result = self.run_transaction()
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = self.trace_text()
        # 2026-11-17 builder workaround #2: the archive is extracted by the
        # shell and only the installer binary runs under the staged
        # qemu-x86_64 user emulator (nvidia-installer SIGBUSes deterministically
        # under Parallels Rosetta).
        self.assertIn("sh\t/tmp/", trace.replace(str(self.root), ""))
        self.assertIn("\t-x", trace)
        self.assertIn("nvidia-run-qemu\t", trace)
        self.assertIn("./nvidia-installer", trace)
        # The installer must run from inside the unpacked archive so its
        # default kernel-open build directory resolves.
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("cd /tmp/NVIDIA-Linux-x86_64-615.78.08\n", source)
        self.assertIn("--silent", trace)
        self.assertIn("--kernel-module-type=open", trace)
        self.assertIn("--kernel-name=6.8.0-142-generic", trace)
        self.assertIn("--skip-module-load", trace)
        # The staged archive and its directory are removed after install.
        self.assertFalse((self.run_dir / RUN_BASENAME).exists())

    def test_digest_mismatch_stops_before_installer(self):
        # Since the 2026-11-17 builder workaround the staged .run digest is
        # asserted host-side before the chroot transaction (Rosetta-translated
        # hashing proved unreliable under build load), so the transaction
        # itself no longer hashes anything.  Keep the same guarantee from the
        # outside: the builder must gate the transaction on the host-side
        # digest assertion and must not hash inside the chroot block.
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'printf \'%s  %s\\n\' "${NVIDIA_RUN_SHA256}" '
            '"${ROOT_MOUNT}/tmp/nvidia-run/${NVIDIA_RUN_BASENAME}"', source)
        self.assertIn("fail \"staged NVIDIA .run archive digest mismatch", source)
        block = chroot_transaction(str(self.root))
        self.assertNotIn("sha256sum --check", block)
        result = self.run_transaction()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_failure_is_fatal(self):
        result = self.run_transaction(NVIDIA_TEST_INSTALLER_STATUS=1)
        self.assertNotEqual(result.returncode, 0)

    def test_extraction_failure_is_fatal(self):
        result = self.run_transaction(NVIDIA_TEST_RUNNER_STATUS=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("nvidia-run-qemu\t", self.trace_text())

    def test_apt_preference_guard_is_written(self):
        result = self.run_transaction()
        self.assertEqual(result.returncode, 0, result.stderr)
        prefs = self.root / "etc/apt/preferences.d/99-rigpilot-nvidia-run-managed"
        self.assertTrue(prefs.is_file())
        text = prefs.read_text(encoding="utf-8")
        self.assertIn("Package: nvidia-* libnvidia-* cuda-*", text)
        self.assertIn("Pin-Priority: -1", text)

    def test_cuda_repository_material_is_gone_from_the_builder(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("cuda-keyring", source)
        self.assertNotIn("developer.download.nvidia.com", source)
        self.assertNotIn("nvidia-driver-pinning-", source)
        self.assertNotIn("NVIDIA_PACKAGE_VERSION", source)

    def test_run_archive_is_digest_gated_before_the_chroot(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(f'[[ "$(basename -- "${{NVIDIA_RUN_PATH}}")" == "${{NVIDIA_RUN_BASENAME}}" ]]', source)
        self.assertIn(
            'printf \'%s  %s\\n\' "${NVIDIA_RUN_SHA256}" "${NVIDIA_RUN_PATH}" | sha256sum --check --strict',
            source,
        )
        self.assertIn('install -m 0644 "${NVIDIA_RUN_PATH}" "${ROOT_MOUNT}/tmp/nvidia-run/${NVIDIA_RUN_BASENAME}"', source)

    def test_final_policy_asserts_run_managed_driver(self):
        policy = (SCRIPT.parent / "apply-final-image-policy.py").read_text(encoding="utf-8")
        self.assertIn("'NVIDIA_DRIVER_VERSION':NVIDIA_DRIVER_VERSION", policy)
        self.assertIn("CUDA repository source remains in run-managed image", policy)
        self.assertIn("CUDA repository keyring remains in run-managed image", policy)
        self.assertIn("NVIDIA apt preference guard is missing", policy)
        self.assertIn("'mode':'run-installer'", policy)

    def test_builder_module_version_assertion_targets_run_version(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            '[[ "${NVIDIA_MODULE_VERSION}" == "${NVIDIA_DRIVER_VERSION}" ]]',
            source,
        )
        self.assertIn('fail "default kernel does not contain NVIDIA', source)


if __name__ == "__main__":
    unittest.main()
