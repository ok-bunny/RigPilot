"""Pure release payload contracts shared by the Agent and offline verifiers.

This module has only standard-library dependencies and performs no filesystem,
environment, hardware or network work at import time. It can be loaded directly
with ``importlib.util.spec_from_file_location`` from any working directory.

The member sets are minimum required payloads, not fixed archive counts. Callers
still validate regular-file types, links, manifest membership/hashes, budgets and
target binary architecture. Agent 0.8.58 removed the HiveOS runtime and the
NVIDIA driver update manager; their package members are no longer required.
"""

from __future__ import annotations

from pathlib import PurePosixPath


WINDOWS_REQUIRED_PAYLOAD = frozenset({
    "RigPilotAgent.exe",
    "RigPilotAgentUpdater.ps1",
    "portable-agent.ps1",
    "Start-RigPilotAgent.bat",
})

# Required by the current Linux Full installer before it verifies SHA256SUMS.
LINUX_INSTALLER_REQUIRED_PAYLOAD = frozenset({
    "RigPilotAgent",
    "RigPilotAgentLauncher.sh",
    "RigPilotAgentUpdater.py",
    "rigpilot-agent.service",
    "rigpilot-agent-supervision.conf",
    "rigpilot-agent-update-recovery.service",
    "rigpilot-agent-update-recovery.timer",
    "VERSION",
    "SHA256SUMS",
    "agent-release.json",
})

# Executable members emitted by build-ubuntu-portable.sh, including its entry
# scripts. Check modes only for members present in the selected payload: Linux
# OnlineUpdate carries the Agent binary, not the Full entry scripts.
LINUX_FULL_EXECUTABLE_PAYLOAD = frozenset({
    "RigPilotAgent",
    "Install.sh",
    "Start-Portable.sh",
    "Remove-Systemd.sh",
    "RigPilotAgentLauncher.sh",
    "RigPilotAgentUpdater.py",
})
LINUX_FULL_REQUIRED_PAYLOAD = (
    LINUX_INSTALLER_REQUIRED_PAYLOAD | LINUX_FULL_EXECUTABLE_PAYLOAD
)

_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{index}" for index in "123456789¹²³"),
    *(f"LPT{index}" for index in "123456789¹²³"),
})


class WindowsArchivePathError(ValueError):
    """A Windows payload path cannot be interpreted safely."""


def canonical_windows_archive_parts(name: str) -> tuple[str, ...]:
    """Return the existing Agent's canonical ZIP path components.

    ZIP directory callers strip their trailing slash first. Repeated slashes
    and harmless ``.`` components are normalized by PurePosixPath as before;
    manifest callers must also compare ``'/'.join(parts) == name`` when their
    contract requires the original spelling to be canonical.
    """

    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise WindowsArchivePathError("Windows archive contains an unsafe path")
    path = PurePosixPath(name)
    parts = path.parts
    if (
        path.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            part.endswith((" ", "."))
            or sum(2 if ord(character) > 0xFFFF else 1 for character in part) > 255
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
            or any(
                character in ':?*|<>"' or ord(character) < 32 or ord(character) == 127
                for character in part
            )
            for part in parts
        )
    ):
        raise WindowsArchivePathError("Windows archive contains an unsafe path")
    return tuple(parts)
