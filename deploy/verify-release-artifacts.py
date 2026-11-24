"""Verify RigPilot release archives, embedded manifests, and SHA-256 sidecars."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile


# Resolve the pure contract from this trusted repository, independent of cwd
# or caller PYTHONPATH. It has no runtime/Agent/hardware import side effects.
_contract_path = Path(__file__).resolve().parents[1] / "agent" / "release_contract.py"
_contract_spec = importlib.util.spec_from_file_location("rigpilot_release_contract", _contract_path)
_contract = importlib.util.module_from_spec(_contract_spec)
_contract_spec.loader.exec_module(_contract)
WINDOWS_REQUIRED_PAYLOAD = _contract.WINDOWS_REQUIRED_PAYLOAD
# Install/runtime support and Full entry points are necessary. Documentation
# emitted by the current builder is not an extra minimum-installability gate.
LINUX_FULL_REQUIRED_PAYLOAD = _contract.LINUX_FULL_REQUIRED_PAYLOAD
LINUX_FULL_EXECUTABLE_PAYLOAD = _contract.LINUX_FULL_EXECUTABLE_PAYLOAD


HEX64 = re.compile(r"[0-9a-f]{64}")
MAX_ARCHIVE_ENTRIES = 4096
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
)
LINUX_TEXT_NAMES = {"SHA256SUMS", "VERSION"}
LINUX_TEXT_SUFFIXES = {
    ".conf",
    ".json",
    ".md",
    ".py",
    ".service",
    ".sh",
    ".timer",
    ".txt",
}


def is_linux_text_member(name: str) -> bool:
    path = PurePosixPath(name)
    return path.name in LINUX_TEXT_NAMES or path.suffix.casefold() in LINUX_TEXT_SUFFIXES


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_binary_architecture(payload: bytes, platform: str) -> str:
    """Check the main executable header, not every embedded library or runtime."""
    if platform == "windows":
        if len(payload) < 64 or payload[:2] != b"MZ":
            raise RuntimeError("Windows Agent binary is not a PE executable")
        offset = int.from_bytes(payload[60:64], "little")
        if (offset > len(payload) - 26 or payload[offset:offset + 4] != b"PE\x00\x00"
                or int.from_bytes(payload[offset + 4:offset + 6], "little") != 0x8664
                or int.from_bytes(payload[offset + 24:offset + 26], "little") != 0x20B):
            raise RuntimeError("Windows Agent binary must be AMD64 PE32+")
    elif platform == "linux":
        if (len(payload) < 64 or payload[:7] != b"\x7fELF\x02\x01\x01"
                or int.from_bytes(payload[18:20], "little") != 62):
            raise RuntimeError("Linux Agent binary must be ELF64 little-endian x86_64")
    else:
        raise RuntimeError("unsupported binary platform")
    return digest_bytes(payload)


def canonical(name: str) -> str:
    value = name.rstrip("/")
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or "\x00" in value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
    ):
        raise RuntimeError(f"unsafe release member: {name!r}")
    return value


def canonical_windows(name: str) -> str:
    try:
        parts = _contract.canonical_windows_archive_parts(name.rstrip("/"))
    except _contract.WindowsArchivePathError as exc:
        raise RuntimeError(f"unsafe Windows release member: {name!r}") from exc
    value = "/".join(parts)
    if value != name.rstrip("/"):
        raise RuntimeError(f"noncanonical Windows release member: {name!r}")
    return value


def verify_sidecar(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"release artifact is missing or unsafe: {path}")
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file() or sidecar.is_symlink():
        raise RuntimeError(f"SHA-256 sidecar is missing or unsafe: {sidecar}")
    line = sidecar.read_text(encoding="ascii").strip("\n")
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
    if match is None or match.group(2) != path.name:
        raise RuntimeError(f"SHA-256 sidecar format is invalid: {sidecar}")
    actual = digest_file(path)
    if match.group(1) != actual:
        raise RuntimeError(f"outer SHA-256 mismatch: {path}")
    return actual


def verify_manifest_files(files: object) -> dict[str, str]:
    if not isinstance(files, dict) or not files:
        raise RuntimeError("release manifest file map is empty or invalid")
    folded: set[str] = set()
    for name, value in files.items():
        if not isinstance(name, str) or canonical(name) != name:
            raise RuntimeError("release manifest contains an unsafe path")
        if not isinstance(value, str) or HEX64.fullmatch(value) is None:
            raise RuntimeError("release manifest contains an invalid digest")
        if name.casefold() in folded:
            raise RuntimeError("release manifest contains duplicate paths")
        folded.add(name.casefold())
    if any(parent.as_posix() in folded for name in folded for parent in PurePosixPath(name).parents):
        raise RuntimeError("release manifest contains a file used as a parent directory")
    return files.copy()


def verify_manifest(
    manifest: object, *, version: str, platform: str, architecture: str
) -> dict[str, str]:
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest is not an object")
    expected_fields = {"schema_version", "version", "platform", "architecture", "files"}
    if set(manifest) != expected_fields:
        raise RuntimeError("release manifest fields are invalid")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["version"] != version
        or manifest["platform"] != platform
        or manifest["architecture"] != architecture
    ):
        raise RuntimeError("release manifest identity does not match the artifact")
    return verify_manifest_files(manifest["files"])


def verify_windows(path: Path, version: str) -> None:
    verify_sidecar(path)
    prefix = "RigPilotAgent-Portable/"
    with zipfile.ZipFile(path) as archive:
        names: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        expanded_size = 0
        if not 1 <= len(archive.infolist()) <= MAX_ARCHIVE_ENTRIES:
            raise RuntimeError("Windows archive entry count is invalid")
        for info in archive.infolist():
            original = getattr(info, "orig_filename", info.filename)
            if original != info.filename:
                raise RuntimeError("Windows archive contains an unsafe original path")
            name = canonical_windows(original)
            if name in names or name.casefold() in folded or info.is_dir():
                raise RuntimeError("Windows archive contains a duplicate or directory entry")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if info.create_system == 3 and stat.S_IFMT(unix_mode) not in {0, stat.S_IFREG}:
                raise RuntimeError("Windows archive contains a Unix link or special entry")
            if info.file_size < 0 or info.file_size > 512 * 1024 * 1024:
                raise RuntimeError("Windows archive member is too large")
            expanded_size += info.file_size
            if expanded_size > 2 * 1024 * 1024 * 1024:
                raise RuntimeError("Windows archive expands beyond the release limit")
            if not name.startswith(prefix):
                raise RuntimeError("Windows archive has an unexpected top-level path")
            names[name] = info
            folded.add(name.casefold())
        manifest_name = prefix + "agent-release.json"
        if manifest_name not in names:
            raise RuntimeError("Windows archive is missing its release manifest")
        manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
        files = verify_manifest(
            manifest, version=version, platform="windows", architecture="x86_64"
        )
        if not WINDOWS_REQUIRED_PAYLOAD.issubset(files):
            raise RuntimeError("Windows release is missing required update payload")
        for name in files:
            canonical_windows(name)
            if name.split("/", 1)[0].casefold() in {"data", "versions"}:
                raise RuntimeError("Windows release includes reserved runtime data")
        expected = {prefix + name for name in files} | {manifest_name}
        if set(names) != expected:
            raise RuntimeError("Windows archive file set does not match its manifest")
        for name, expected_hash in files.items():
            if digest_bytes(archive.read(prefix + name)) != expected_hash:
                raise RuntimeError(f"Windows payload hash mismatch: {name}")
        verify_binary_architecture(archive.read(prefix + "RigPilotAgent.exe"), "windows")


def regular_tar_members(
    path: Path,
) -> tuple[dict[str, bytes], set[str], dict[str, int]]:
    regular: dict[str, bytes] = {}
    directories: set[str] = set()
    modes: dict[str, int] = {}
    folded: set[str] = set()
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for index, member in enumerate(archive, 1):
            if index > MAX_ARCHIVE_ENTRIES:
                raise RuntimeError("tar archive entry count is invalid")
            name = canonical(member.name)
            if name.casefold() in folded:
                raise RuntimeError("tar archive contains duplicate paths")
            folded.add(name.casefold())
            modes[name] = stat.S_IMODE(member.mode)
            if (not (member.isdir() or member.isreg()) or member.size < 0
                    or member.size > 512 * 1024 * 1024 or bool(getattr(member, "sparse", None))):
                raise RuntimeError("tar archive contains a link, sparse, special, or oversized member")
            if member.isdir():
                if member.size:
                    raise RuntimeError("tar archive directory contains unexpected data")
                directories.add(name)
                continue
            total += member.size
            if total > 2 * 1024 * 1024 * 1024:
                raise RuntimeError("tar archive expands beyond the release limit")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read tar member: {name}")
            regular[name] = source.read()
    return regular, directories, modes


def verify_linux(path: Path, version: str, *, online: bool) -> str:
    verify_sidecar(path)
    root = "RigPilotAgent-Ubuntu-x64-OnlineUpdate" if online else "RigPilotAgent-Ubuntu-x64"
    regular, directories, modes = regular_tar_members(path)
    if any(
        not (name == root or name.startswith(root + "/"))
        for name in set(regular) | directories
    ):
        raise RuntimeError("Linux archive has an unexpected top-level path")
    manifest_name = root + "/agent-release.json"
    if manifest_name not in regular:
        raise RuntimeError("Linux archive is missing its release manifest")
    manifest = json.loads(regular[manifest_name].decode("utf-8"))
    files = verify_manifest(
        manifest, version=version, platform="linux", architecture="x86_64"
    )
    expected = {root + "/" + name for name in files} | {manifest_name}
    if not online:
        expected.add(root + "/SHA256SUMS")
    if set(regular) != expected:
        raise RuntimeError("Linux archive file set does not match its manifest")
    if not online and not LINUX_FULL_REQUIRED_PAYLOAD.issubset(
        {name.removeprefix(root + "/") for name in regular}
    ):
        raise RuntimeError("Linux Full release is missing required install payload")
    invalid_line_endings = sorted(
        name
        for name, payload in regular.items()
        if is_linux_text_member(name) and b"\r" in payload
    )
    if invalid_line_endings:
        raise RuntimeError(
            "Linux archive text files must use LF only: "
            + ", ".join(invalid_line_endings)
        )
    executable_names = LINUX_FULL_EXECUTABLE_PAYLOAD
    for name in directories:
        if modes[name] != 0o755:
            raise RuntimeError(f"Linux archive directory mode is unsafe: {name}")
    for name in regular:
        relative = name.removeprefix(root + "/")
        expected_mode = 0o755 if relative in executable_names else 0o644
        if modes[name] != expected_mode:
            raise RuntimeError(f"Linux archive file mode is unsafe: {name}")
    for name, expected_hash in files.items():
        if digest_bytes(regular[root + "/" + name]) != expected_hash:
            raise RuntimeError(f"Linux payload hash mismatch: {name}")
    binary = regular.get(root + "/RigPilotAgent")
    if binary is None:
        raise RuntimeError("Linux Agent binary is missing")
    binary_sha = verify_binary_architecture(binary, "linux")
    if online:
        if set(files) != {"RigPilotAgent"}:
            raise RuntimeError("Linux online payload is not minimal")
        return binary_sha
    if regular[root + "/VERSION"] != f"{version}\n".encode("ascii"):
        raise RuntimeError("Linux VERSION does not match the release identity")
    sums: dict[str, str] = {}
    for line in regular[root + "/SHA256SUMS"].decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None or canonical(match.group(2)) != match.group(2):
            raise RuntimeError("Linux SHA256SUMS is invalid")
        if match.group(2) in sums:
            raise RuntimeError("Linux SHA256SUMS contains duplicate paths")
        sums[match.group(2)] = match.group(1)
    expected_sums = set(files) | {"agent-release.json"}
    if set(sums) != expected_sums:
        raise RuntimeError("Linux SHA256SUMS file set is invalid")
    for name, expected_hash in sums.items():
        if digest_bytes(regular[root + "/" + name]) != expected_hash:
            raise RuntimeError(f"Linux SHA256SUMS mismatch: {name}")
    return binary_sha


def verify_controller(path: Path, version: str) -> None:
    verify_sidecar(path)
    regular, directories, _ = regular_tar_members(path)
    raw_manifest = regular.get("controller-release.json")
    if raw_manifest is None:
        raise RuntimeError("controller manifest is missing")
    manifest = json.loads(raw_manifest.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("controller manifest is not an object")
    expected_fields = {"schema_version", "version", "files"}
    if set(manifest) != expected_fields:
        raise RuntimeError("controller manifest fields are invalid")
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["version"] != version
        or VERSION.fullmatch(version) is None
    ):
        raise RuntimeError("controller manifest identity is invalid")
    files = verify_manifest_files(manifest["files"])
    if set(regular) != set(files) | {"controller-release.json"}:
        raise RuntimeError("controller archive file set does not match its manifest")
    allowed_directories: set[str] = set()
    for name in set(files) | {"controller-release.json"}:
        parent = PurePosixPath(name).parent
        while str(parent) != ".":
            allowed_directories.add(parent.as_posix())
            parent = parent.parent
    if not directories.issubset(allowed_directories):
        raise RuntimeError("controller archive contains an unexpected directory")
    for name, expected_hash in files.items():
        if digest_bytes(regular[name]) != expected_hash:
            raise RuntimeError(f"controller payload hash mismatch: {name}")


def cleanup_smoke_directory(directory: tempfile.TemporaryDirectory) -> None:
    """Allow bounded Windows image-unload/file-scanner delays after child exit.

    Never ignore a failed cleanup: persistent locks and unrelated filesystem
    errors still fail the release verification.
    """
    for attempt in range(40):
        try:
            directory.cleanup()
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 39:
                raise
            time.sleep(0.25)


def smoke_windows(path: Path, version: str) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows binary smoke test requires a Windows runtime")
    temporary = tempfile.TemporaryDirectory(prefix="rigpilot-win-smoke-")
    try:
        root = Path(temporary.name)
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                original = getattr(info, "orig_filename", info.filename)
                if original != info.filename:
                    raise RuntimeError("Windows smoke has an unsafe original path")
                relative = PurePosixPath(canonical_windows(original))
                target = root.joinpath(*relative.parts)
                if root.resolve() not in target.resolve().parents:
                    raise RuntimeError("Windows smoke extraction escapes its temporary root")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        executable = root / "RigPilotAgent-Portable" / "RigPilotAgent.exe"
        completed = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.stdout.strip() != f"RigPilot Agent {version}":
            raise RuntimeError("Windows binary version smoke test failed")
    finally:
        cleanup_smoke_directory(temporary)


def smoke_linux(path: Path, version: str) -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Linux binary smoke test requires a Linux runtime")
    regular, _, _ = regular_tar_members(path)
    binary = regular.get("RigPilotAgent-Ubuntu-x64/RigPilotAgent")
    if binary is None:
        raise RuntimeError("Linux binary is missing from the full package")
    with tempfile.TemporaryDirectory(prefix="rigpilot-linux-smoke-") as temporary:
        executable = Path(temporary) / "RigPilotAgent"
        executable.write_bytes(binary)
        executable.chmod(0o700)
        completed = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.stdout.strip() != f"RigPilot Agent {version}":
            raise RuntimeError("Linux binary version smoke test failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--agent-version")
    parser.add_argument("--controller-version")
    parser.add_argument("--skip-windows", action="store_true")
    parser.add_argument("--skip-linux", action="store_true")
    parser.add_argument("--skip-controller", action="store_true")
    parser.add_argument("--smoke-native", choices=("none", "windows", "linux"), default="none")
    args = parser.parse_args()
    if args.skip_windows and args.skip_linux and args.skip_controller:
        parser.error("No release target selected")
    for selected, option, version in (
        (not (args.skip_windows and args.skip_linux), "--agent-version", args.agent_version),
        (not args.skip_controller, "--controller-version", args.controller_version),
    ):
        if selected and (version is None or VERSION.fullmatch(version) is None):
            parser.error(f"{option} must use X.Y.Z for the selected target")
    if (args.smoke_native == "windows" and args.skip_windows) or (
        args.smoke_native == "linux" and args.skip_linux
    ):
        parser.error(f"--smoke-native {args.smoke_native} cannot be combined with --skip-{args.smoke_native}")
    output = args.output_dir.resolve(strict=True)
    if not args.skip_windows:
        verify_windows(
            output / f"RigPilotAgent-{args.agent_version}-Windows-x64-Portable.zip",
            args.agent_version,
        )
    if not args.skip_linux:
        full_binary_sha = verify_linux(
            output / f"RigPilotAgent-{args.agent_version}-Ubuntu-x86_64.tar.gz",
            args.agent_version,
            online=False,
        )
        online_binary_sha = verify_linux(
            output / f"RigPilotAgent-{args.agent_version}-Ubuntu-x86_64-OnlineUpdate.tar.gz",
            args.agent_version,
            online=True,
        )
        if full_binary_sha != online_binary_sha:
            raise RuntimeError("Linux Full and OnlineUpdate binaries differ")
    if not args.skip_controller:
        verify_controller(
            output / f"RigPilotController-{args.controller_version}.tar.gz",
            args.controller_version,
        )
    if args.smoke_native == "windows":
        smoke_windows(
            output / f"RigPilotAgent-{args.agent_version}-Windows-x64-Portable.zip",
            args.agent_version,
        )
    elif args.smoke_native == "linux":
        smoke_linux(
            output / f"RigPilotAgent-{args.agent_version}-Ubuntu-x86_64.tar.gz",
            args.agent_version,
        )
    print("RELEASE_VERIFICATION=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
