"""Exercise cleanup failure handling without mounting or deleting anything."""

from pathlib import Path
import shutil
import subprocess
import unittest
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "build-rigpilotos-target.sh"


class NativeBuildCleanupTests(unittest.TestCase):
    def run_cleanup(self, stubs: str, expected: int) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("Bash is required to exercise the cleanup function")
        source = SCRIPT.read_text(encoding="utf-8")
        body = source.split("cleanup() {\n", 1)[1].split(
            "\n}\ntrap cleanup EXIT", 1
        )[0]
        work_dir = "/var/tmp/rigpilotos-build.cleanup-harness-" + uuid.uuid4().hex
        self.assertFalse(Path(work_dir).exists())
        harness = (
            "set -e\n"
            f"WORK_DIR={work_dir}\n"
            "ROOT_MOUNT=$WORK_DIR/root\n"
            "CONFIG_MOUNT=$WORK_DIR/provision\n"
            "sync() { :; }\n"
            "rm() { echo UNSAFE_RM_CALLED; return 97; }\n"
            + stubs
            + "cleanup() {\n"
            + body
            + "\n}\ncleanup\n"
        )
        completed = subprocess.run(
            [bash, "-c", harness], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(
            completed.returncode, expected, completed.stdout + completed.stderr
        )
        self.assertNotIn("UNSAFE_RM_CALLED", completed.stdout)
        if expected:
            self.assertIn("retained", completed.stderr)

    def test_both_unmount_attempts_failing_preserves_work_tree(self):
        self.run_cleanup(
            "mountpoint() { return 0; }\n"
            "umount() { return 1; }\n"
            "findmnt() { printf '/\\n'; }\n",
            1,
        )

    def test_unexpected_descendant_mount_preserves_work_tree(self):
        self.run_cleanup(
            "mountpoint() { return 1; }\n"
            "umount() { return 0; }\n"
            'findmnt() { printf "/\\n%s/root/unexpected\\n" "$WORK_DIR"; }\n',
            1,
        )

    def test_unreadable_mount_table_preserves_work_tree(self):
        self.run_cleanup(
            "mountpoint() { return 1; }\n"
            "umount() { return 0; }\n"
            "findmnt() { return 1; }\n",
            1,
        )

    def test_already_detached_cleanup_succeeds(self):
        self.run_cleanup(
            "mountpoint() { return 1; }\n"
            "umount() { return 0; }\n"
            "findmnt() { printf '/\\n'; }\n",
            0,
        )


if __name__ == "__main__":
    unittest.main()
