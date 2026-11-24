"""Run only directory permission setup/checks with local shell mocks."""
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "build-rigpilotos-target.sh"
EXPECTED_PATHS = (
    "", "etc", "etc/netplan", "etc/profile.d", "etc/ssh", "etc/ssh/sshd_config.d",
    "etc/systemd", "etc/systemd/system", "etc/systemd/journald.conf.d",
    "etc/grub.d", "usr", "usr/bin",
    "usr/libexec", "usr/local", "usr/local/sbin", "opt", "var", "var/lib",
)


def permission_fragment():
    source = SCRIPT.read_text()
    start = source.index("TRUSTED_DIRS=(")
    end = source.index("\nchown -R root:root", start)
    declaration = source[start:end]
    start = source.index('chown root:root "${TRUSTED_DIRS[@]}"', end)
    owner = source[start:source.index("\nfind ", start)]
    start = source.index('for trusted_dir in "${TRUSTED_DIRS[@]}"; do', start)
    guard = source[start:source.index("\ndone", start) + len("\ndone")]
    return declaration + "\n" + owner + "\n" + guard


@pytest.mark.parametrize("failure", ["none", "bad-mode", "bad-owner", "unreadable"])
def test_protected_directory_setup_and_checks_keep_the_full_boundary(tmp_path, failure):
    root = tmp_path / "root with spaces"
    paths = [str(root / path) if path else str(root) for path in EXPECTED_PATHS]
    trace = tmp_path / "calls"
    mocks = r"""set -Eeuo pipefail
ROOT_MOUNT="$1"; TRACE="$2"; FAILURE="$3"
fail() { printf '%s\n' "$*" >&2; exit 1; }
chmod() { printf '%s\0' chmod "$@" >>"$TRACE"; }
chown() { printf '%s\0' chown "$@" >>"$TRACE"; }
stat() {
    [[ "$#" == 3 && "$1" == -c && "$2" == '%a %u:%g' ]] || return 90
    printf '%s\0' stat "$@" >>"$TRACE"
    if [[ "$3" == "$ROOT_MOUNT/etc/netplan" ]]; then
        case "$FAILURE" in
            bad-mode) printf '775 0:0\n'; return ;;
            bad-owner) printf '755 1000:1000\n'; return ;;
            unreadable) return 73 ;;
        esac
    fi
    printf '755 0:0\n'
}
"""
    result = subprocess.run(
        ["bash", "-c", mocks + permission_fragment(), "permission-fixture",
         str(root), str(trace), failure], text=True, capture_output=True, timeout=5,
    )
    calls = trace.read_bytes().decode().rstrip("\0").split("\0")
    starts = [index for index, value in enumerate(calls)
              if value in {"chmod", "chown", "stat"}]
    groups = [calls[start:end] for start, end in zip(starts, starts[1:] + [len(calls)])]
    assert {group[0] for group in groups} == {"chmod", "chown", "stat"}
    for group in groups:
        if group[0] in {"chmod", "chown"}:
            assert group[1] == ("0755" if group[0] == "chmod" else "root:root")
            assert set(group[2:]) == set(paths)
        else:
            assert group[1:3] == ["-c", "%a %u:%g"]
            assert group[3] in paths
    if failure == "none":
        assert result.returncode == 0, result.stderr
        assert {group[3] for group in groups if group[0] == "stat"} == set(paths)
    else:
        assert result.returncode != 0
        assert "trusted directory mode or owner is invalid" in result.stderr
        assert str(root / "etc/netplan") in result.stderr
    assert not root.exists(), "Mocks must not touch any protected directories"
