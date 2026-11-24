"""Exclusive writable SRBMiner runs copied from a sealed package cache.

Each preparation creates a never-before-existing directory while holding the
cache transaction lock. Existing runs are never reused or inspected to permit
starting. Callers keep leases until a process record takes over and separately
provide positive closure evidence before disposal. This module neither starts
nor stops a process and never mutates the sealed source cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Mapping, Any

from .config import AgentConfig
from .miner_packages import (
    MinerPackageError,
    _CHUNK_SIZE,
    _MARKER_NAME,
    _MAX_MEMBERS,
    _is_link_or_reparse,
    _safe_member_parts,
    _same_opened_file,
    _tree_fingerprint,
    _verified_file_identity,
    _verify_installed_package,
)


LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY = "rigpilot-native-srb-runtime"
NATIVE_SRB_RUNTIME_DIRECTORY = "rigpilot-native-srb-runtime-v2"
_RUNTIME_DIRECTORIES = frozenset({LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY, NATIVE_SRB_RUNTIME_DIRECTORY})
_ROLES = frozenset({"single", "primary", "secondary"})
_CONTROL = "control.json"
_BUNDLE = "bundle"
_PROBE_PENDING = "probe-pending"
_KIND = "rigpilot-native-srb-runtime.v1"
_GENERATION_KIND = "rigpilot-native-srb-runtime.v2"
_GENERATION = re.compile(r"generation-[0-9a-f]{32}\Z")
_OWNER = uuid.uuid4().hex
_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_CONTROL_BYTES = 64 * 1024


class NativeMinerRuntimeError(MinerPackageError):
    """The baseline, runtime namespace or reference inventory is unsafe."""


def _fail(message: str) -> None:
    raise NativeMinerRuntimeError(message)


def _directory(path: Path) -> os.stat_result:
    info = path.lstat()
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        _fail("native SRBMiner runtime requires plain directories")
    if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
        _fail("native SRBMiner runtime directory ownership is unsafe")
    return info


def _directory_identity(info: os.stat_result) -> tuple[int, ...]:
    attributes = getattr(info, "st_file_attributes", 0)
    if _windows_directory_metadata_is_lazy():
        # Native Windows probes observed 0x10000010 -> 0x10 while enumerating
        # the same directory. Normalize only this observed transient bit;
        # retain every other attribute, reparse tag and device/inode/mode.
        return (info.st_dev, info.st_ino, info.st_mode,
                attributes & ~0x10000000,
                getattr(info, "st_reparse_tag", 0))
    return (info.st_dev, info.st_ino, info.st_mode,
            attributes)


def _windows_directory_metadata_is_lazy() -> bool:
    return os.name == "nt"


def _entry_identity(info: os.stat_result) -> tuple[int, ...]:
    # Windows directory changes are bound by stable identity plus entries.
    # Files retain the complete existing verification identity on every OS.
    if stat.S_ISDIR(info.st_mode) and _windows_directory_metadata_is_lazy():
        return _directory_identity(info)
    return _verified_file_identity(info)


def _chain(root: Path, path: Path) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        _fail("native SRBMiner path is outside its configured root")
    if ".." in parts:
        _fail("native SRBMiner path contains an unsafe parent component")
    _directory(root)
    current = root
    for part in parts:
        current /= part
        _directory(current)


def _read_regular(path: Path, maximum: int) -> bytes:
    before = path.lstat()
    if (_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1 or before.st_size > maximum):
        _fail("native SRBMiner control file is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(descriptor)
        if not _same_opened_file(before, opened):
            _fail("native SRBMiner control file changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        if (len(data) != before.st_size or len(data) > maximum
                or _verified_file_identity(after) != _verified_file_identity(opened)
                or _verified_file_identity(path_after) != _verified_file_identity(before)
                or not _same_opened_file(path_after, after)):
            _fail("native SRBMiner control file changed while reading")
        return data
    finally:
        os.close(descriptor)


def _snapshot(root: Path, *, allow_marker: bool = False) -> dict[str, os.stat_result]:
    """Bind plain entries and every directory identity to a complete inventory."""
    result = {"": _directory(root)}
    lazy_directories = _windows_directory_metadata_is_lazy()
    pending = [(root, None)]
    while pending:
        directory, previous = pending.pop()
        before = _directory(directory)
        name = directory.relative_to(root).as_posix()
        if _entry_identity(before) != _entry_identity(result["" if name == "." else name]):
            _fail("native SRBMiner directory changed before inspecting")
        with os.scandir(directory) as stream:
            entries = sorted(stream, key=lambda entry: entry.name)
        inventory = {}
        children = []
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if allow_marker and relative == _MARKER_NAME:
                # The root marker is checked separately with its read handle.
                continue
            if _safe_member_parts(relative) != Path(relative).parts:
                _fail("native SRBMiner bundle contains an unsafe path")
            info = path.lstat()
            if lazy_directories:
                inventory[entry.name] = _entry_identity(info)
            if _is_link_or_reparse(info) or info.st_dev != result[""].st_dev:
                _fail("native SRBMiner bundle contains a link or external entry")
            if stat.S_ISDIR(info.st_mode):
                children.append(path)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                _fail("native SRBMiner bundle contains a special or linked file")
            if previous is None:
                result[relative] = info
            if len(result) > _MAX_MEMBERS + 1:
                _fail("native SRBMiner bundle has too many entries")
            if len(inventory) > _MAX_MEMBERS:
                _fail("native SRBMiner directory has too many entries")
        if _entry_identity(_directory(directory)) != _entry_identity(before):
            _fail("native SRBMiner directory changed while inspecting")
        if previous is not None:
            if inventory != previous:
                _fail("native SRBMiner directory entries changed while inspecting")
        else:
            # Re-read names and fresh path stats after checking the complete
            # subtree, catching same-name replacements and additions without
            # depending on unstable Windows directory timestamp metadata.
            if lazy_directories:
                pending.append((directory, inventory))
            pending.extend((child, None) for child in (reversed(children) if lazy_directories else children))
    return result


def _unchanged(before: Mapping[str, os.stat_result], after: Mapping[str, os.stat_result]) -> bool:
    return (before.keys() == after.keys() and all(
        _entry_identity(info) == _entry_identity(after[name])
        for name, info in before.items()
    ))


def _create_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _directory(path)


def _children(path: Path, allowed: set[str]) -> None:
    with os.scandir(path) as stream:
        if any(entry.name not in allowed for entry in stream):
            _fail("native SRBMiner runtime namespace has unknown entries")


def _control(slot: Path, role: str, runtime: Path, packages_root: Path) -> dict[str, Any]:
    _directory(slot)
    _children(slot, {_CONTROL, _BUNDLE, _PROBE_PENDING})
    try:
        value = json.loads(_read_regular(slot / _CONTROL, _MAX_CONTROL_BYTES).decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise NativeMinerRuntimeError("native SRBMiner ownership record is invalid") from exc
    fields = {
            "kind", "role", "runtime", "source", "source_archive_sha256", "tree_sha256",
            "file_count", "extracted_bytes", "state"}
    generation = isinstance(value, dict) and value.get("kind") == _GENERATION_KIND
    if generation:
        fields |= {"generation", "owner", "directory_identity"}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get("kind") not in {_KIND, _GENERATION_KIND} or value.get("role") != role
            or value.get("runtime") != str(runtime) or value.get("state") != "owned"
            or not isinstance(value.get("source"), str)
            or not _HEX.fullmatch(str(value.get("source_archive_sha256", "")))
            or not _HEX.fullmatch(str(value.get("tree_sha256", "")))
            or type(value.get("file_count")) is not int or value["file_count"] < 1
            or type(value.get("extracted_bytes")) is not int or value["extracted_bytes"] < 0):
        _fail("native SRBMiner runtime slot has no valid ownership record")
    if generation and (
        not _GENERATION.fullmatch(slot.name) or value["generation"] != slot.name
        or not isinstance(value["owner"], str) or re.fullmatch(r"[0-9a-f]{32}", value["owner"]) is None
        or value["directory_identity"] != list(_directory_identity(_directory(slot)))
    ):
        _fail("native SRBMiner generation identity changed")
    source = Path(value["source"])
    try:
        relative = source.relative_to(packages_root)
    except ValueError:
        _fail("native SRBMiner runtime slot has an external baseline")
    if not relative.parts or relative.parts[0] in _RUNTIME_DIRECTORIES or ".." in relative.parts:
        _fail("native SRBMiner runtime slot has an invalid baseline")
    return value


def _delete_generation(slot: Path, expected: tuple[int, ...], packages_root: Path) -> None:
    """Bind the moved directory before deleting; never delete a substituted path."""
    _chain(packages_root, slot)
    _snapshot(slot)
    if _directory_identity(_directory(slot)) != expected:
        _fail("native SRBMiner generation changed before disposal")
    container = slot.parent / (".retiring-" + uuid.uuid4().hex)
    container.mkdir(mode=0o700)
    container_identity = _directory_identity(_directory(container))
    moved = container / "run"
    os.rename(slot, moved)
    if (_directory_identity(_directory(moved)) != expected
            or _directory_identity(_directory(container)) != container_identity):
        _fail("native SRBMiner generation identity changed during disposal")
    _snapshot(moved)
    shutil.rmtree(moved)
    container.rmdir()


def discard_native_srb_bundle(bundle: Path, config: AgentConfig, runtime: Path, *, role: str,
                             confirmed_stop: bool = False, prior_boot_closed: bool = False) -> bool:
    """Discard this Agent session's run only after the caller proves it closed.

    Lease release alone is not that proof. Legacy slots and generations left
    by another Agent instance are deliberately retained. A changed tree or
    directory identity is also retained for review.
    """
    packages_root = Path(config.miner_packages_dir).absolute()
    slot = Path(bundle).absolute().parent
    if (Path(bundle).name != _BUNDLE or not _GENERATION.fullmatch(slot.name)
            or slot.parent not in {packages_root / name / role for name in _RUNTIME_DIRECTORIES}):
        return False
    try:
        slot.lstat()
    except FileNotFoundError:
        return False
    from .miner_cache_cleanup import cache_operation_lock
    with cache_operation_lock(packages_root):
        _chain(packages_root, slot)
        value = _control(slot, role, Path(runtime).absolute(), packages_root)
        if (value.get("kind") != _GENERATION_KIND
                or (value.get("owner") != _OWNER and not confirmed_stop)):
            return False
        if (slot / _PROBE_PENDING).exists() and not prior_boot_closed:
            return False
        _delete_generation(slot, tuple(value["directory_identity"]), packages_root)
        return True


def mark_native_srb_probe(bundle: Path, config: AgentConfig, runtime: Path, *, role: str,
                          closed: bool) -> None:
    """Persist uncertainty; positive fixed-probe direct-child exit clears it."""
    root = Path(config.miner_packages_dir).absolute()
    slot = Path(bundle).parent
    _chain(root, slot)
    value = _control(slot, role, runtime, root)
    if value.get("kind") != _GENERATION_KIND or value.get("owner") != _OWNER:
        _fail("native SRBMiner probe run is not owned by this operation")
    pending = slot / _PROBE_PENDING
    if closed:
        try:
            _read_regular(pending, 0)
        except FileNotFoundError:
            return
        pending.unlink()
    else:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)


def _copy_file(source: Path, destination: Path, expected: str, before: os.stat_result,
               parent_before: os.stat_result) -> None:
    if _verified_file_identity(source.lstat()) != _verified_file_identity(before):
        _fail("native SRBMiner source changed before copying")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    parent_descriptor = None
    try:
        opened = os.fstat(descriptor)
        if not _same_opened_file(before, opened):
            _fail("native SRBMiner source changed while opening")
        digest = hashlib.sha256()
        total = 0
        if _directory_identity(_directory(destination.parent)) != _directory_identity(parent_before):
            _fail("native SRBMiner destination directory changed before copying")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        permissions = 0o600 | (stat.S_IMODE(before.st_mode) & 0o111)
        if os.open in os.supports_dir_fd:
            parent_descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                        | getattr(os, "O_NOFOLLOW", 0))
            if _directory_identity(os.fstat(parent_descriptor)) != _directory_identity(parent_before):
                _fail("native SRBMiner destination directory changed while opening")
            output_descriptor = os.open(destination.name, flags, permissions, dir_fd=parent_descriptor)
        else:
            # Windows has no Python directory-relative open. Check the parent
            # before opening and bind the output handle to its path afterwards.
            output_descriptor = os.open(destination, flags, permissions)
        with os.fdopen(descriptor, "rb", closefd=False) as stream, os.fdopen(output_descriptor, "wb") as output:
            output_before = os.fstat(output.fileno())
            if not _same_opened_file(destination.lstat(), output_before):
                _fail("native SRBMiner destination changed while opening")
            while True:
                chunk = stream.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > before.st_size:
                    _fail("native SRBMiner source grew while copying")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            # This exclusively created run is disposable and never reused.
            # Flush for handle/path and final tree verification; persistent
            # control records and the sealed source retain their durability.
            if hasattr(os, "fchmod"):
                os.fchmod(output.fileno(), permissions)
            if not _same_opened_file(destination.lstat(), os.fstat(output.fileno())):
                _fail("native SRBMiner destination changed while copying")
        after = os.fstat(descriptor)
        path_after = source.lstat()
        if (total != before.st_size or digest.hexdigest() != expected
                or _verified_file_identity(after) != _verified_file_identity(opened)
                or _verified_file_identity(path_after) != _verified_file_identity(before)
                or not _same_opened_file(path_after, after)):
            _fail("native SRBMiner source changed while copying")
        copied = destination.lstat()
        if (_is_link_or_reparse(copied) or not stat.S_ISREG(copied.st_mode)
                or copied.st_nlink != 1 or (copied.st_dev, copied.st_ino) == (before.st_dev, before.st_ino)):
            _fail("native SRBMiner copied file is not independent")
        if _directory_identity(_directory(destination.parent)) != _directory_identity(parent_before):
            _fail("native SRBMiner destination directory changed during copying")
    finally:
        os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def prepare_native_srb_bundle(
    package_root: Path,
    metadata: Mapping[str, Any],
    config: AgentConfig,
    runtime: Path,
    *,
    role: str,
) -> Path:
    """Create a never-before-existing run and return its writable package.

    ``role`` is ``single`` for the configured runtime itself, or ``primary`` /
    ``secondary`` for its role subdirectory. A role manager's configuration may
    already point to that named role subdirectory. Use the returned root for executable
    selection, device discovery, transient OC reset and the final launch; keep
    the original ``installed_path`` in the flight package metadata. An unrelated
    unreadable process cannot refer to the previously nonexistent run. Existing
    slots, uncertain generations and interrupted reclamation are left alone.

    No root marker is copied. The control record outside ``bundle`` only proves
    slot ownership and its initial baseline; it never attests runtime mutations.
    A failed copy discards only its own unchanged, unexposed directory. Callers
    dispose completed runs separately, after positive lifecycle confirmation.
    Allocation does not inspect or reclaim any existing run.
    """
    from .miner_cache_cleanup import MinerCacheCleanupError

    try:
        return _prepare(package_root, metadata, config, runtime, role=role)
    except NativeMinerRuntimeError:
        raise
    except (OSError, ValueError, TypeError, KeyError, MinerPackageError, MinerCacheCleanupError) as exc:
        raise NativeMinerRuntimeError("native SRBMiner writable bundle could not be verified") from exc


def _prepare(package_root: Path, metadata: Mapping[str, Any], config: AgentConfig,
             runtime: Path, *, role: str) -> Path:
    from .miner_cache_cleanup import cache_operation_lock

    if role not in _ROLES or config.miner_packages_dir is None or config.managed_miner_runtime_dir is None:
        _fail("native SRBMiner runtime configuration is invalid")
    digest = metadata.get("sha256")
    if not isinstance(digest, str) or not _HEX.fullmatch(digest) or metadata.get("hiveos_custom") is not None:
        _fail("native SRBMiner requires an archive SHA-attested native package")
    packages_root = Path(config.miner_packages_dir).absolute()
    _directory(packages_root)
    packages_root = packages_root.resolve(strict=True)
    source = Path(package_root).absolute()
    _chain(packages_root, source)
    if source != source.resolve(strict=True):
        _fail("native SRBMiner baseline path is not canonical")
    relative = source.relative_to(packages_root)
    if not relative.parts or relative.parts[0] in _RUNTIME_DIRECTORIES:
        _fail("native SRBMiner baseline must be a sealed package cache")
    runtime_base = Path(config.managed_miner_runtime_dir).absolute()
    _directory(runtime_base)
    runtime_base = runtime_base.resolve(strict=True)
    runtime = Path(runtime).absolute()
    expected_runtime = runtime_base if role == "single" else runtime_base / role
    # Match the actual child path first: the legitimate configured base may
    # itself be named primary/secondary. Localized configuration is accepted
    # only when the supplied runtime is exactly that configured role path.
    localized = role != "single" and runtime == runtime_base and runtime_base.name == role
    if runtime != expected_runtime and not localized:
        _fail("native SRBMiner runtime role does not match the configured path")
    _chain(runtime_base, runtime)
    maximum = int(config.miner_download_max_mb) * 1024 * 1024
    if maximum <= 0:
        _fail("native SRBMiner package size limit is invalid")

    with cache_operation_lock(packages_root):
        marker_info = (source / _MARKER_NAME).lstat()
        marker = _read_regular(source / _MARKER_NAME, _MAX_CONTROL_BYTES)
        before = _snapshot(source, allow_marker=True)
        installed = _verify_installed_package(source, metadata, maximum)
        if installed != json.loads(marker.decode("utf-8")):
            _fail("native SRBMiner source marker changed during verification")
        baseline = _tree_fingerprint(source, maximum, include_files=True)
        if any(baseline[key] != installed.get(key) for key in ("tree_sha256", "file_count", "extracted_bytes")):
            _fail("native SRBMiner source changed while preparing its baseline")
        files = {name for name, info in before.items() if stat.S_ISREG(info.st_mode)}
        if files != set(baseline["files"]) or not _unchanged(before, _snapshot(source, allow_marker=True)):
            _fail("native SRBMiner source inventory changed while preparing")
        if (_read_regular(source / _MARKER_NAME, _MAX_CONTROL_BYTES) != marker
                or _verified_file_identity((source / _MARKER_NAME).lstat()) != _verified_file_identity(marker_info)):
            _fail("native SRBMiner source marker changed while preparing")

        namespace = packages_root / NATIVE_SRB_RUNTIME_DIRECTORY
        _create_directory(namespace)
        namespace_identity = _directory_identity(namespace.lstat())
        role_root = namespace / role
        _create_directory(role_root)
        role_identity = _directory_identity(role_root.lstat())
        available_bytes = shutil.disk_usage(packages_root).free
        if available_bytes < baseline["extracted_bytes"] + _MAX_CONTROL_BYTES:
            _fail(f"native SRBMiner runtime has insufficient disk space "
                  f"[required_bytes={baseline['extracted_bytes'] + _MAX_CONTROL_BYTES}; "
                  f"available_bytes={available_bytes}]")
        slot = role_root / ("generation-" + uuid.uuid4().hex)
        try:
            slot.mkdir(mode=0o700)
        except FileExistsError:
            _fail("native SRBMiner generation was not exclusively created")
        slot_identity = _directory_identity(_directory(slot))
        try:
            return _copy_generation(source, marker_info, marker, before, baseline, maximum,
                                    packages_root, namespace, namespace_identity, role_root,
                                    role_identity, slot, slot_identity, role, runtime, digest)
        except BaseException as exc:
            # The path has never been handed to any executable runner. Only
            # this mkdir's identity is deletion authority, never its name.
            try:
                _delete_generation(slot, slot_identity, packages_root)
            except (OSError, NativeMinerRuntimeError):
                add_note = getattr(exc, "add_note", None)
                if callable(add_note):
                    add_note("Task-owned partial run retained: cleanup identity could not be verified")
            raise


def _copy_generation(source, marker_info, marker, before, baseline, maximum,
                     packages_root, namespace, namespace_identity, role_root,
                     role_identity, slot, slot_identity, role, runtime, digest):
    control = {"kind": _GENERATION_KIND, "role": role, "runtime": str(runtime), "source": str(source),
               "source_archive_sha256": digest, "tree_sha256": baseline["tree_sha256"],
               "file_count": baseline["file_count"], "extracted_bytes": baseline["extracted_bytes"],
               "state": "owned", "generation": slot.name, "owner": _OWNER,
               "directory_identity": list(slot_identity)}
    control_descriptor = os.open(slot / _CONTROL, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(control_descriptor, "wb") as output:
        output.write((json.dumps(control, sort_keys=True) + "\n").encode("utf-8"))
        output.flush()
        os.fsync(output.fileno())
    bundle = slot / _BUNDLE
    _create_directory(bundle)
    for name, info in sorted(before.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
        if name and stat.S_ISDIR(info.st_mode):
            _create_directory(bundle / name)
    destination_directories = _snapshot(bundle)
    for name, expected in sorted(baseline["files"].items()):
        parent = Path(name).parent.as_posix()
        _copy_file(source / name, bundle / name, expected, before[name],
                   destination_directories["" if parent == "." else parent])
    if not _unchanged(before, _snapshot(source, allow_marker=True)):
        _fail("native SRBMiner source changed during its bundle copy")
    if (_read_regular(source / _MARKER_NAME, _MAX_CONTROL_BYTES) != marker
            or _verified_file_identity((source / _MARKER_NAME).lstat()) != _verified_file_identity(marker_info)):
        _fail("native SRBMiner source marker changed during its bundle copy")
    after = _tree_fingerprint(bundle, maximum, include_files=True)
    if after != baseline:
        _fail("native SRBMiner copied bundle disagrees with its verified baseline")
    destination_after = _snapshot(bundle)
    if any(_directory_identity(info) != _directory_identity(destination_after[name])
           for name, info in destination_directories.items()):
        _fail("native SRBMiner copied directory identity changed")
    if (_directory_identity(namespace.lstat()) != namespace_identity
            or _directory_identity(role_root.lstat()) != role_identity
            or _directory_identity(_directory(slot)) != slot_identity):
        _fail("native SRBMiner runtime changed during its bundle copy")
    return bundle
