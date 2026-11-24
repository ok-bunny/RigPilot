"""Tests for the shared fail-closed filesystem helpers (0.8.60 dedup)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from agent import fsutil
from agent import managed_miners
from agent import miner_logs
from agent import miner_packages
from agent import process_identity
from agent import wsl_mining
from agent.agent_updates import _is_link_or_reparse
from agent.commands import _is_link_or_reparse as commands_is_link


def test_predicate_detects_posix_symlink(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable here: {exc}")

    assert fsutil.is_link_or_reparse(link.lstat()) is True
    assert fsutil.is_link_or_reparse(target.lstat()) is False


def test_predicate_ignores_missing_windows_attributes(tmp_path: Path):
    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    info = regular.lstat()
    # On POSIX a stat_result carries no st_file_attributes at all; on
    # Windows a regular file carries attribute flags (e.g. ARCHIVE) with
    # the reparse bit unset.  Either way the predicate must stay False.
    assert fsutil.is_link_or_reparse(info) is False
    if sys.platform != "win32":
        assert getattr(info, "st_file_attributes", 0) == 0


def test_fsync_directory_non_strict_swallows_failures(tmp_path: Path):
    # A missing directory is best effort for non-strict callers.
    fsutil.fsync_directory(tmp_path / "missing")
    # A real directory fsyncs without raising.
    fsutil.fsync_directory(tmp_path)


def test_fsync_directory_strict_propagates_failures(tmp_path: Path):
    with pytest.raises(OSError):
        fsutil.fsync_directory(tmp_path / "missing", strict=True)
    if sys.platform == "win32":
        # Opening a directory is impossible on Windows: strict mode must
        # surface that instead of pretending the entry was made durable.
        with pytest.raises(OSError):
            fsutil.fsync_directory(tmp_path, strict=True)
    else:
        fsutil.fsync_directory(tmp_path, strict=True)


def test_normalized_path_is_absolute_and_case_normalized(tmp_path: Path):
    target = tmp_path / "nested" / "file.txt"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    result = fsutil.normalized_path(str(target))
    assert os.path.isabs(result)
    assert fsutil.normalized_path(target) == fsutil.normalized_path(str(target))
    assert result == os.path.normcase(os.path.realpath(str(target)))


def test_aliases_keep_module_names_behaviour_identical():
    # The dedup keeps each module's private name bound to the canonical
    # implementation (or a delegating wrapper with identical semantics).
    assert managed_miners._link_or_reparse is fsutil.is_link_or_reparse
    assert miner_logs._link_or_reparse is fsutil.is_link_or_reparse
    assert wsl_mining._link_or_reparse is fsutil.is_link_or_reparse
    assert process_identity._link_or_reparse is fsutil.is_link_or_reparse
    assert commands_is_link is fsutil.is_link_or_reparse
    assert miner_packages._is_link_or_reparse is fsutil.is_link_or_reparse
    assert managed_miners._normalized_path is fsutil.normalized_path
    assert process_identity._normalized_path is fsutil.normalized_path


def test_agent_updates_wrapper_keeps_fail_closed_lstat(tmp_path: Path):
    assert _is_link_or_reparse(tmp_path / "not-there") is True
    regular = tmp_path / "regular"
    regular.write_text("x", encoding="utf-8")
    assert _is_link_or_reparse(regular) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fsync delegation")
def test_module_fsync_wrappers_delegate_to_fsutil(tmp_path: Path):
    miner_packages._fsync_directory(tmp_path)
    # state.py's strict wrapper still raises for a missing directory.
    from agent.state import _fsync_directory as state_fsync

    with pytest.raises(OSError):
        state_fsync(tmp_path / "missing")


def test_state_identity_check_reads_regular_private_file(tmp_path: Path):
    # The enrolled-state hardening path accepts exactly what save_state
    # writes: a regular, owner-private file (POSIX 0600).
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
        assert path.stat().st_uid == os.geteuid()
        assert stat.S_ISREG(path.stat().st_mode)
