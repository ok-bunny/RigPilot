"""Shared fail-closed filesystem helpers for the Agent package.

Until 0.8.60 these predicates lived as fourteen private copies across the
product modules; semantics were maintained by copy discipline.  The canonical
implementations live here and every module keeps its original private name as
a thin alias or delegating wrapper, so call sites and raised exceptions are
byte-for-byte unchanged.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_link_or_reparse(info: os.stat_result) -> bool:
    """True when a ``lstat`` result is a symlink or Windows reparse point."""

    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse
    )


def normalized_path(value: str | os.PathLike[str]) -> str:
    """Case-normalized absolute path for identity comparisons."""

    return os.path.normcase(os.path.realpath(os.fspath(value)))


def fsync_directory(path: Path, *, strict: bool = False) -> None:
    """Durably publish directory changes on POSIX filesystems.

    Non-strict callers treat an un-fsyncable directory as best effort
    (matching the historical private copies); strict callers -- the enrolled
    state file -- propagate the failure because an un-durable credential
    swap must not be reported as complete.  Opening a directory fails on
    Windows, so no explicit platform guard is required.
    """

    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        if strict:
            raise
        return
    try:
        os.fsync(descriptor)
    except OSError:
        if strict:
            raise
    finally:
        os.close(descriptor)
