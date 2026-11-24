"""Narrow Windows SRBMiner device probe adapter.

A returned subprocess.run CompletedProcess proves the direct child was waited
for. It makes no claim about descendants, unrelated processes or a Job scope.
Linux retains managed_miners' existing PTY runner and its explicit exit proof.
"""
from __future__ import annotations

import os
import subprocess

_DEFAULT_RUN = subprocess.run


def run_native_srb_probe(
    argv, *, system_name, cwd, timeout, text=True, capture_output=True,
    env=None, shell=False, check=False,
) -> subprocess.CompletedProcess[str]:
    """Run the existing fixed CLI, returning only direct-child exit evidence."""
    if (system_name != "Windows" or not isinstance(argv, (list, tuple))
            or len(argv) != 2 or argv[1] != "--list-devices"
            or not isinstance(argv[0], str) or not argv[0] or "\x00" in argv[0]
            or shell is not False or check is not False
            or text is not True or capture_output is not True):
        error = ValueError("unsupported native SRB direct-child probe invocation")
        error.owned_direct_child_closed = False
        raise error
    runner = subprocess.run
    try:
        completed = runner(
            argv, cwd=os.fspath(cwd), shell=False, check=False,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # CPython's default run kills and waits/communicates before rethrowing
        # its original TimeoutExpired. An injected runner has no such proof.
        exc.owned_direct_child_closed = runner is _DEFAULT_RUN
        raise
    except Exception as exc:
        # Preserve the original error type/timeout output and never rerun an
        # invocation that may already have executed. No affirmative wait
        # receipt is exposed on this failure path.
        exc.owned_direct_child_closed = False
        raise
    if (not isinstance(completed, subprocess.CompletedProcess)
            or type(completed.returncode) is not int):
        error = RuntimeError("native SRB direct-child probe result is invalid")
        error.owned_direct_child_closed = False
        raise error
    completed.owned_direct_child_closed = True
    return completed
