"""Environment isolation for trusted executables outside the frozen Agent.

PyInstaller one-file builds prepend their extraction directory to
``LD_LIBRARY_PATH`` for the embedded Python process.  A child such as
``systemctl`` or ``systemd-run`` must not inherit those private libraries:
newer distributions can otherwise load an incompatible bundled library and
fail before the command itself starts.  Restore the loader environment saved
by PyInstaller, and never leak the Agent's systemd notification contract to a
child process.

This restores the host environment; it is not an environment allowlist. User
Python settings such as ``PYTHONPATH`` and ``PYTHONHOME`` remain intact so an
external Python command keeps its configured import/interpreter semantics.
Callers needing a private worker environment must define that boundary
separately, as the NVIDIA memory worker does.
"""

from __future__ import annotations

import os
import platform
import sys


def external_process_environment(
    system_name: str | None = None,
) -> dict[str, str]:
    """Restore Linux loader state and remove inherited systemd notifications.

    Preserve ordinary user settings, including Python module/interpreter
    paths. Non-Linux callers receive an unmodified environment copy.
    """

    environment = os.environ.copy()
    if (system_name or platform.system()).strip().casefold() != "linux":
        return environment

    environment.pop("NOTIFY_SOCKET", None)
    environment.pop("WATCHDOG_USEC", None)
    environment.pop("WATCHDOG_PID", None)

    if "LD_LIBRARY_PATH_ORIG" in environment:
        original = environment.pop("LD_LIBRARY_PATH_ORIG")
        if original:
            environment["LD_LIBRARY_PATH"] = original
        else:
            environment.pop("LD_LIBRARY_PATH", None)
    elif bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS"):
        environment.pop("LD_LIBRARY_PATH", None)
    return environment
