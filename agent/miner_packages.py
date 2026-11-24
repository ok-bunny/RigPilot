"""Safe download and extraction of miner package archives.

Remote package metadata is treated as untrusted input. Packages are fetched
only from the enrolled controller or a local hostname allowlist, then unpacked
into a data directory with restrictive permissions where the platform supports
them. A caller-supplied SHA-256 value, when present, binds the downloaded
archive before extraction. Records without a digest remain supported as an
explicitly unbound legacy path. Nothing in this module directly starts a
downloaded program.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import time
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

import requests

from server.miner_adapters.retired import retired_miner_type

from .config import AgentConfig
from .fsutil import fsync_directory
from .fsutil import is_link_or_reparse as _is_link_or_reparse
from .state import AgentState


# HiveOS session packages were removed.  The legacy descriptor field stays
# recognized so a stale controller payload fails with one clear error instead
# of an unsupported-field listing; it can never install again.
_RETIRED_HIVEOS_CHANNEL_ERROR = (
    "HiveOS miner packages are no longer supported"
)


class MinerPackageError(RuntimeError):
    """Package metadata, transport, or archive content is unsafe."""


class _UnverifiedMinerPackageCache(MinerPackageError):
    """An otherwise valid cache lacks this archive digest attestation."""


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.+-]+$")
_PACKAGE_REQUIRED_FIELDS = {
    "id",
    "name",
    "version",
    "download_url",
    "archive_type",
}
_PACKAGE_FIELDS = _PACKAGE_REQUIRED_FIELDS | {
    "sha256",
    "managed_os_family",
    "hiveos_custom",
}
# Package records are immutable by ID/version. The absolute controller URL is
# intentionally excluded because a controller may move to a new origin while
# retaining the same database and hosted package IDs.
_PACKAGE_MARKER_FIELDS = _PACKAGE_REQUIRED_FIELDS - {"download_url"}
_ARCHIVE_TYPES = {"zip", "tar.gz"}
_CONTROLLER_DOWNLOAD_PATH = re.compile(
    r"^/api/agent/miner-packages/([A-Za-z0-9_.+-]{1,80})/download$"
)
_CHUNK_SIZE = 64 * 1024
_MAX_MEMBERS = 20_000
_MAX_PATH_DEPTH = 64
_MAX_MEMBER_PATH = 1024
_MARKER_NAME = ".rigpilot-package.json"
_VERIFIED_ARCHIVE_SHA256_FIELD = "verified_archive_sha256_v1"
_VERIFIED_CACHE_DIRECTORY_PREFIX = ".rigpilot-verified-sha256-v1-"
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise MinerPackageError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise MinerPackageError(f"{label} must contain 1-{maximum} characters")
    if any(ord(character) < 32 for character in value):
        raise MinerPackageError(f"{label} contains control characters")
    return value


def _normalized_host(hostname: str) -> str:
    value = hostname.rstrip(".").lower()
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        try:
            return value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise MinerPackageError("download_url contains an invalid hostname") from exc


def _loopback(hostname: str) -> bool:
    if hostname.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_download_url(value: Any, allowed_hosts: frozenset[str]) -> str:
    """Validate scheme and destination before any network operation."""

    url = _text(value, "download_url", 2048)
    if any(character.isspace() for character in url):
        raise MinerPackageError("download_url must not contain whitespace")
    try:
        parsed = urlparse(url)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise MinerPackageError("download_url is malformed") from exc
    if username or password:
        raise MinerPackageError("download_url must not contain credentials")
    if parsed.fragment:
        raise MinerPackageError("download_url must not contain a fragment")
    if not hostname:
        raise MinerPackageError("download_url must contain a hostname")
    if not parsed.path or parsed.path.endswith("/"):
        raise MinerPackageError("download_url must point to a package file")
    host = _normalized_host(hostname)
    normalized_allowlist = frozenset(_normalized_host(item) for item in allowed_hosts)
    if host not in normalized_allowlist:
        raise MinerPackageError("download_url hostname is not in the local allowlist")
    scheme = parsed.scheme.lower()
    if scheme != "https" and not (scheme == "http" and _loopback(hostname)):
        raise MinerPackageError("download_url must use HTTPS (HTTP is loopback-only)")
    return url


def controller_package_download_url(
    value: Any, config: AgentConfig, package_id: str
) -> str | None:
    """Resolve an authenticated controller package URL, if ``value`` is one.

    Controller-hosted packages deliberately use a narrow fixed path. Relative
    URLs are resolved against ``server_url``; absolute URLs are accepted only
    when their origin exactly matches the enrolled controller. This prevents
    credentials from ever being attached to an arbitrary package host.
    """

    url = _text(value, "download_url", 2048)
    if any(character.isspace() for character in url):
        raise MinerPackageError("download_url must not contain whitespace")
    try:
        parsed = urlparse(url)
        server = urlparse(config.server_url)
        parsed_port = parsed.port
        server_port = server.port
    except ValueError as exc:
        raise MinerPackageError("download_url is malformed") from exc
    if parsed.username or parsed.password or parsed.params or parsed.query or parsed.fragment:
        return None
    match = _CONTROLLER_DOWNLOAD_PATH.fullmatch(parsed.path)
    if match is None or match.group(1) != package_id:
        return None

    if not parsed.scheme and not parsed.netloc:
        if not url.startswith("/"):
            return None
        return urljoin(config.server_url.rstrip("/") + "/", url)

    def origin(
        scheme: str, hostname: str | None, port: int | None
    ) -> tuple[str, str, int | None] | None:
        if not scheme or not hostname:
            return None
        normalized_scheme = scheme.lower()
        default_port = 443 if normalized_scheme == "https" else 80
        return (
            normalized_scheme,
            _normalized_host(hostname),
            port if port is not None else default_port,
        )

    if origin(parsed.scheme, parsed.hostname, parsed_port) != origin(
        server.scheme, server.hostname, server_port
    ):
        return None
    return url


def _retired_managed_package_type(value: Any) -> str | None:
    """Only controlled managed package IDs identify a built-in adapter.

    Generic package IDs and display names are user-selected labels; even an
    exact match to a retired miner name cannot claim the built-in namespace.
    """
    if not isinstance(value, str) or not value.strip().lower().startswith(("builtin-", "test-")):
        return None
    return retired_miner_type(value)


def _reject_retired_package(value: Mapping[str, Any]) -> None:
    """Refuse retired managed packages and the removed HiveOS channel."""
    if _retired_managed_package_type(value.get("id")) is not None:
        raise MinerPackageError("miner package adapter is retired")
    if "hiveos_custom" in value or str(value.get("id", "")).startswith("hiveos-"):
        raise MinerPackageError(_RETIRED_HIVEOS_CHANNEL_ERROR)


def validate_package_metadata(
    value: Any, config: AgentConfig, label: str = "miner_package"
) -> dict[str, Any] | None:
    """Validate one nullable package descriptor without downloading it."""

    if value is None:
        return None
    fields = set(value) if isinstance(value, dict) else set()
    if (
        not isinstance(value, dict)
        or not _PACKAGE_REQUIRED_FIELDS <= fields
        or fields - _PACKAGE_FIELDS
    ):
        raise MinerPackageError(
            f"{label} must contain: "
            + ", ".join(sorted(_PACKAGE_REQUIRED_FIELDS))
            + "; sha256 is optional only for unbound legacy records"
        )
    _reject_retired_package(value)
    if not config.allow_miner_downloads:
        raise MinerPackageError("miner package downloads are disabled locally")
    if config.miner_packages_dir is None:
        raise MinerPackageError("miner_packages_dir is not configured locally")
    package_id = _text(value["id"], f"{label}.id", 80)
    version = _text(value["version"], f"{label}.version", 80)
    if not _SAFE_COMPONENT.fullmatch(package_id):
        raise MinerPackageError(f"{label}.id contains unsupported characters")
    if not _SAFE_COMPONENT.fullmatch(version):
        raise MinerPackageError(f"{label}.version contains unsupported characters")
    name = _text(value["name"], f"{label}.name", 120)
    raw_digest = value.get("sha256")
    if raw_digest is None:
        # Historical package records can be unbound until production metadata
        # has been inventoried and backfilled with trusted archive digests.
        digest = None
    elif isinstance(raw_digest, str):
        digest = raw_digest.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise MinerPackageError(
                f"{label}.sha256 must be a 64-character hexadecimal string"
            )
    else:
        raise MinerPackageError(f"{label}.sha256 must be a string or null")
    archive_type = _text(value["archive_type"], f"{label}.archive_type", 16).lower()
    if archive_type not in _ARCHIVE_TYPES:
        raise MinerPackageError(f"{label}.archive_type must be zip or tar.gz")
    controller_url = controller_package_download_url(
        value["download_url"], config, package_id
    )
    managed_os_family = value.get("managed_os_family")
    if managed_os_family is not None:
        managed_os_family = _text(
            managed_os_family,
            f"{label}.managed_os_family",
            16,
        ).lower()
        if managed_os_family not in {"windows", "linux"}:
            raise MinerPackageError(
                f"{label}.managed_os_family must be windows or linux"
            )
    normalized = {
        "id": package_id,
        "name": name,
        "version": version,
        "download_url": controller_url
        or validate_download_url(
            value["download_url"], config.miner_download_host_allowlist
        ),
        "sha256": digest,
        "archive_type": archive_type,
    }
    if managed_os_family is not None:
        normalized["managed_os_family"] = managed_os_family
    if "hiveos_custom" in value or package_id.startswith("hiveos-"):
        raise MinerPackageError(_RETIRED_HIVEOS_CHANNEL_ERROR)
    return normalized


def _safe_member_parts(
    name: str, *, allow_current_directory_prefix: bool = False,
) -> tuple[str, ...]:
    # Backslashes become separators so a ZIP made on Unix cannot traverse when
    # later unpacked on Windows.
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise MinerPackageError("archive contains an unsafe member path")
    normalized = normalized.rstrip("/")
    if not normalized or len(normalized) > _MAX_MEMBER_PATH:
        raise MinerPackageError("archive contains an unsafe member path")
    raw_parts = normalized.split("/")
    if len(raw_parts) > _MAX_PATH_DEPTH:
        raise MinerPackageError("archive member path is too deep")
    if allow_current_directory_prefix:
        # Some tar writers include complete leading './' components. Check
        # the original path budgets first, and strip only that literal prefix;
        # embedded dots, traversal and backslash-dot spellings remain invalid.
        prefix_end = 0
        while name.startswith("./", prefix_end):
            prefix_end += 2
        raw_parts = raw_parts[prefix_end // 2:]
    if not raw_parts:
        raise MinerPackageError("archive contains an unsafe member path")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise MinerPackageError("archive contains an unsafe member path")
    for part in raw_parts:
        if (
            len(part) > 255
            or part.endswith((" ", "."))
            or ":" in part
            or any(ord(character) < 32 for character in part)
        ):
            raise MinerPackageError("archive contains an unsafe member path")
        windows_base = unicodedata.normalize("NFKC", part).split(".", 1)[0].upper()
        if windows_base in _WINDOWS_RESERVED_NAMES:
            raise MinerPackageError("archive contains a Windows reserved path")
    path = PurePosixPath(*raw_parts)
    if path.is_absolute():
        raise MinerPackageError("archive contains an unsafe member path")
    if tuple(raw_parts) == (_MARKER_NAME,):
        raise MinerPackageError("archive contains a reserved marker path")
    return tuple(raw_parts)


def _destination(root: Path, name: str) -> Path:
    return _destination_from_parts(root, _safe_member_parts(name))


def _destination_from_parts(root: Path, parts: tuple[str, ...]) -> Path:
    destination = root.joinpath(*parts)
    try:
        destination.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise MinerPackageError("archive member escapes the package directory") from exc
    return destination




def _require_plain_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise MinerPackageError(f"{label} cannot be inspected") from exc
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise MinerPackageError(f"{label} must be a real local directory")


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _verified_file_identity(info):
    if os.name == "nt":
        # NTFS path and handle stat can disagree on ctime immediately after
        # an in-place write. For ordinary file reads compare stable identity
        # plus reparse attributes; content bytes are attested below.
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns,
                getattr(info, "st_file_attributes", 0))
    return _file_identity(info)


def _same_opened_file(path_info, opened_info, *, windows: bool | None = None) -> bool:
    """Bind a path to its read handle without comparing unlike Windows clocks."""
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return _file_identity(path_info) == _file_identity(opened_info)
    return (
        stat.S_ISREG(path_info.st_mode)
        and stat.S_ISREG(opened_info.st_mode)
        and not _is_link_or_reparse(path_info)
        and not _is_link_or_reparse(opened_info)
        and path_info.st_dev != 0
        and path_info.st_ino != 0
        and path_info.st_dev == opened_info.st_dev
        and path_info.st_ino == opened_info.st_ino
        and path_info.st_nlink == opened_info.st_nlink == 1
        and path_info.st_size == opened_info.st_size
    )


def _tree_fingerprint(root: Path, max_bytes: int, *, include_files: bool = False) -> dict[str, Any]:
    """Attest the complete regular-file graph of an installed package."""
    _require_plain_directory(root, "miner package directory")
    files = []
    entries_seen = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda item: item.name)
        except OSError as exc:
            raise MinerPackageError("installed miner package cannot be inspected") from exc
        for entry in entries:
            relative = Path(entry.path).relative_to(root).as_posix()
            if relative == _MARKER_NAME:
                continue
            entries_seen += 1
            if entries_seen > _MAX_MEMBERS:
                raise MinerPackageError("installed miner package has too many entries")
            try:
                # DirEntry.stat omits inode/link-count fields on Windows. Read
                # the path itself so ordinary NTFS files retain st_nlink=1
                # while the hard-link and reparse checks remain fail-closed.
                info = os.stat(entry.path, follow_symlinks=False)
                if _is_link_or_reparse(info):
                    raise MinerPackageError("installed miner package contains a link")
                elif stat.S_ISDIR(info.st_mode):
                    stack.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files.append((relative, Path(entry.path), info))
                else:
                    raise MinerPackageError("installed miner package contains a special or linked file")
            except OSError as exc:
                raise MinerPackageError("installed miner package entry cannot be inspected") from exc
    total = 0
    file_hashes: dict[str, Any] = {}
    fingerprint = hashlib.sha256()
    for relative, path, original in sorted(files):
        declared_size = original.st_size
        if declared_size < 0 or total + declared_size > max_bytes:
            raise MinerPackageError("installed miner package exceeds the size limit")
        digest = hashlib.sha256()
        actual_size = 0
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
            opened = os.fstat(descriptor)
            if not _same_opened_file(original, opened):
                raise MinerPackageError("installed miner package changed during verification")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                while True:
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    actual_size += len(chunk)
                    if total + actual_size > max_bytes:
                        raise MinerPackageError("installed miner package exceeds the size limit")
                    digest.update(chunk)
            opened_after = os.fstat(descriptor)
            path_after = path.lstat()
            if (_verified_file_identity(opened_after) != _verified_file_identity(opened)
                    or _verified_file_identity(path_after) != _verified_file_identity(original)
                    or not _same_opened_file(path_after, opened_after)
                    or actual_size != declared_size):
                raise MinerPackageError("installed miner package changed during verification")
        except OSError as exc:
            raise MinerPackageError("installed miner package file cannot be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        total += actual_size
        encoded_path = relative.encode("utf-8")
        fingerprint.update(len(encoded_path).to_bytes(4, "big"))
        fingerprint.update(encoded_path)
        fingerprint.update(actual_size.to_bytes(8, "big"))
        fingerprint.update(digest.digest())
        if include_files:
            file_hashes[relative] = digest.hexdigest()
    return {"tree_sha256": fingerprint.hexdigest(), "file_count": len(files),
            "extracted_bytes": total,
            **({"files": file_hashes} if include_files else {})}


def _verify_installed_package(
    target: Path, metadata: Mapping[str, Any], max_bytes: int
) -> dict[str, Any]:
    _require_plain_directory(target, "existing miner package")
    marker = target / _MARKER_NAME
    try:
        marker_info = marker.lstat()
        if (
            _is_link_or_reparse(marker_info)
            or not stat.S_ISREG(marker_info.st_mode)
            or marker_info.st_size > 64 * 1024
        ):
            raise MinerPackageError("existing miner package marker is unsafe")
        installed = json.loads(marker.read_text(encoding="utf-8"))
    except MinerPackageError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise MinerPackageError("existing miner package has no valid marker") from exc
    if not isinstance(installed, dict) or any(
        installed.get(field) != metadata[field] for field in _PACKAGE_MARKER_FIELDS
    ):
        raise MinerPackageError("existing miner package marker does not match")
    if metadata.get("managed_os_family") is not None and installed.get(
        "managed_os_family"
    ) != metadata.get("managed_os_family"):
        raise MinerPackageError("existing miner package OS family does not match")
    expected_digest = metadata.get("sha256")
    if (
        expected_digest is not None
        and installed.get(_VERIFIED_ARCHIVE_SHA256_FIELD) != expected_digest
    ):
        raise _UnverifiedMinerPackageCache(
            "existing miner package has no matching verified archive SHA-256"
        )
    fingerprint = _tree_fingerprint(target, max_bytes)
    if any(installed.get(key) != value for key, value in fingerprint.items()):
        raise MinerPackageError("existing miner package content does not match its marker")
    return installed


def _cache_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MinerPackageError("existing miner package cannot be inspected") from exc
    return True


def _verified_cache_target(
    root: Path, metadata: Mapping[str, Any], digest: str
) -> Path:
    """Return a bounded side-by-side slot for replacing an unverified cache."""

    identity = json.dumps(
        {
            "id": metadata["id"],
            "name": metadata["name"],
            "version": metadata["version"],
            "archive_type": metadata["archive_type"],
            "managed_os_family": metadata.get("managed_os_family"),
            "sha256": digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    slot = hashlib.sha256(identity).hexdigest()
    return root / f"{_VERIFIED_CACHE_DIRECTORY_PREFIX}{slot}"


def _write_marker(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    with path.open("xb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    fsync_directory(path)


def _write_stream(source: Any, destination: Path, remaining: int) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("xb") as output:
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > remaining:
                raise MinerPackageError("expanded archive exceeds the local size limit")
            output.write(chunk)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return written


def _extract_zip(archive: Path, destination: Path, max_bytes: int) -> int:
    total = 0
    seen: set[tuple[str, ...]] = set()
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > _MAX_MEMBERS:
                raise MinerPackageError("archive contains too many members")
            for member in members:
                parts = _safe_member_parts(member.filename)
                if parts in seen:
                    raise MinerPackageError("archive contains duplicate member paths")
                seen.add(parts)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise MinerPackageError("archive symbolic links are not allowed")
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise MinerPackageError("archive contains a special file")
                target = _destination(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    try:
                        target.chmod(0o700)
                    except OSError:
                        pass
                    continue
                if member.file_size < 0 or total + member.file_size > max_bytes:
                    raise MinerPackageError("expanded archive exceeds the local size limit")
                with source.open(member, "r") as input_stream:
                    total += _write_stream(input_stream, target, max_bytes - total)
    except (zipfile.BadZipFile, OSError) as exc:
        raise MinerPackageError("miner ZIP archive is invalid") from exc
    return total


def _extract_tar_gz(archive: Path, destination: Path, max_bytes: int) -> int:
    total = 0
    seen: set[tuple[str, ...]] = set()
    try:
        with tarfile.open(archive, mode="r|gz") as source:
            for member_count, member in enumerate(source, start=1):
                if member_count > _MAX_MEMBERS:
                    raise MinerPackageError("archive contains too many members")
                if member.name in {".", "./"}:
                    if not member.isdir() or member.size != 0:
                        raise MinerPackageError("archive root path must be an empty directory")
                    parts = ()
                else:
                    parts = _safe_member_parts(
                        member.name, allow_current_directory_prefix=True,
                    )
                relative = "/".join(parts)
                if parts in seen:
                    raise MinerPackageError("archive contains duplicate member paths")
                seen.add(parts)
                # A conventional tar root directory creates no destination and
                # changes no permissions. It still consumes the member budget
                # and participates in duplicate detection above.
                if not parts:
                    continue
                if member.islnk() or member.issym():
                    raise MinerPackageError("archive links are not allowed")
                if not (member.isdir() or member.isfile()):
                    raise MinerPackageError("archive contains a special file")
                target = _destination_from_parts(destination, parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    try:
                        target.chmod(0o700)
                    except OSError:
                        pass
                    continue
                if member.size < 0 or total + member.size > max_bytes:
                    raise MinerPackageError("expanded archive exceeds the local size limit")
                input_stream = source.extractfile(member)
                if input_stream is None:
                    raise MinerPackageError("archive file member cannot be read")
                with input_stream:
                    written = _write_stream(input_stream, target, max_bytes - total)
                    if written != member.size:
                        raise MinerPackageError("archive file size changed during extraction")
                    total += written
    except (tarfile.TarError, OSError, ValueError, TypeError) as exc:
        raise MinerPackageError("miner tar.gz archive is invalid") from exc
    return total


def _download(
    metadata: Mapping[str, Any],
    destination: Path,
    config: AgentConfig,
    requester: Callable[..., Any],
    state: AgentState | None,
) -> int:
    max_bytes = int(config.miner_download_max_mb * 1024 * 1024)
    deadline = time.monotonic() + config.miner_download_total_timeout
    response: Any = None
    size = 0
    archive_digest = hashlib.sha256()
    try:
        request_options: dict[str, Any] = {
            "stream": True,
            "allow_redirects": False,
            "timeout": config.request_timeout,
            "verify": config.tls_verify,
        }
        controller_url = controller_package_download_url(
            metadata["download_url"], config, metadata["id"]
        )
        if controller_url is not None:
            if state is None:
                raise MinerPackageError(
                    "controller-hosted miner package requires Agent authentication"
                )
            request_options["headers"] = {
                "X-Agent-ID": state.agent_id,
                "Authorization": f"Bearer {state.agent_secret}",
            }
        response = requester(metadata["download_url"], **request_options)
        if time.monotonic() > deadline:
            raise MinerPackageError("miner package download exceeded the total time limit")
        status = getattr(response, "status_code", None)
        if status != 200:
            if isinstance(status, int) and 300 <= status < 400:
                raise MinerPackageError("miner package redirects are not allowed")
            raise MinerPackageError(f"miner package download returned HTTP {status}")
        headers = getattr(response, "headers", {})
        content_length = headers.get("Content-Length") if headers is not None else None
        declared_size = None
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except (TypeError, ValueError) as exc:
                raise MinerPackageError("miner package has an invalid Content-Length") from exc
            if declared_size < 0 or declared_size > max_bytes:
                raise MinerPackageError("miner package exceeds the local size limit")
        with destination.open("xb") as output:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if time.monotonic() > deadline:
                    raise MinerPackageError(
                        "miner package download exceeded the total time limit"
                    )
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise MinerPackageError("miner package exceeds the local size limit")
                output.write(chunk)
                archive_digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        expected_digest = metadata.get("sha256")
        if expected_digest is not None and archive_digest.hexdigest() != expected_digest:
            raise MinerPackageError(
                "miner package SHA-256 does not match package metadata"
            )
    except requests.RequestException as exc:
        raise MinerPackageError("miner package download failed") from exc
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    return size


def install_miner_package(
    metadata: Mapping[str, Any],
    config: AgentConfig,
    *,
    requester: Callable[..., Any] = requests.get,
    state: AgentState | None = None,
) -> dict[str, Any]:
    """Serialize cache installation against Agent-owned cache cleanup."""
    _reject_retired_package(metadata)
    if config.miner_packages_dir is None:
        raise MinerPackageError("miner_packages_dir is not configured locally")
    root = config.miner_packages_dir
    root.mkdir(parents=True, exist_ok=True)
    _require_plain_directory(root, "miner_packages_dir")
    from .miner_cache_cleanup import cache_operation_lock, MinerCacheCleanupError
    try:
        with cache_operation_lock(root, wait_seconds=30.0):
            return _install_miner_package_unlocked(
                metadata, config, requester=requester, state=state
            )
    except MinerCacheCleanupError as exc:
        raise MinerPackageError("miner package cache is busy or unsafe") from exc


def _install_miner_package_unlocked(
    metadata: Mapping[str, Any],
    config: AgentConfig,
    *,
    requester: Callable[..., Any] = requests.get,
    state: AgentState | None = None,
) -> dict[str, Any]:
    """Download, safely unpack, and atomically install a validated package."""

    _reject_retired_package(metadata)

    if config.miner_packages_dir is None:
        raise MinerPackageError("miner_packages_dir is not configured locally")
    root = config.miner_packages_dir
    root.mkdir(parents=True, exist_ok=True)
    _require_plain_directory(root, "miner_packages_dir")
    max_bytes = int(config.miner_download_max_mb * 1024 * 1024)
    managed_os_family = metadata.get("managed_os_family")
    family_suffix = (
        f"-{managed_os_family}"
        if managed_os_family in {"windows", "linux"}
        else ""
    )
    legacy_digest = metadata.get("sha256")
    if isinstance(legacy_digest, str) and re.fullmatch(r"[0-9a-f]{64}", legacy_digest):
        # Preserve the 0.4.0 digest-based directory name for cache compatibility.
        identity = (
            f"{metadata['id']}-{metadata['version']}{family_suffix}-"
            f"{legacy_digest[:12]}"
        )
    else:
        identity = f"{metadata['id']}-{metadata['version']}{family_suffix}"
    target = root / identity
    if _cache_entry_exists(target):
        try:
            _verify_installed_package(target, metadata, max_bytes)
        except _UnverifiedMinerPackageCache:
            # An old cache may contain a declared digest that was never checked.
            # Keep that directory untouched in case a miner is still using it,
            # and install the verified archive into a deterministic side slot.
            if not isinstance(legacy_digest, str):
                raise
            target = _verified_cache_target(root, metadata, legacy_digest)
            if _cache_entry_exists(target):
                _verify_installed_package(target, metadata, max_bytes)
                return {
                    "package_id": metadata["id"],
                    "version": metadata["version"],
                    "path": str(target),
                    "sha256": metadata.get("sha256"),
                    "downloaded": False,
                }
        else:
            return {
                "package_id": metadata["id"],
                "version": metadata["version"],
                "path": str(target),
                "sha256": metadata.get("sha256"),
                "downloaded": False,
            }
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{metadata['id']}.", dir=root))
    archive = temporary_root / "package.download"
    extracted = temporary_root / "content"
    try:
        downloaded_size = _download(metadata, archive, config, requester, state)
        extracted.mkdir()
        try:
            extracted.chmod(0o700)
        except OSError:
            pass
        if metadata["archive_type"] == "zip":
            extracted_size = _extract_zip(archive, extracted, max_bytes)
        else:
            extracted_size = _extract_tar_gz(archive, extracted, max_bytes)
        fingerprint = _tree_fingerprint(extracted, max_bytes)
        if fingerprint["file_count"] < 1:
            raise MinerPackageError("miner package contains no regular files")
        if fingerprint["extracted_bytes"] != extracted_size:
            raise MinerPackageError("extracted package size verification failed")
        marker_payload = dict(metadata)
        marker_payload.update(
            {
                "downloaded_bytes": downloaded_size,
                **fingerprint,
            }
        )
        verified_digest = metadata.get("sha256")
        if verified_digest is not None:
            # This is written only after _download compared the complete archive.
            marker_payload[_VERIFIED_ARCHIVE_SHA256_FIELD] = verified_digest
        marker_path = extracted / _MARKER_NAME
        _write_marker(marker_path, marker_payload)
        installed_by_this_call = True
        try:
            os.replace(extracted, target)
            _fsync_directory(root)
        except OSError:
            # A concurrent application of the same immutable package may have
            # won. The verifier requires the exact archive attestation for
            # digest-bound packages, so an old canonical cache cannot win here.
            _verify_installed_package(target, metadata, max_bytes)
            installed_by_this_call = False
        return {
            "package_id": metadata["id"],
            "version": metadata["version"],
            "path": str(target),
            "sha256": metadata.get("sha256"),
            "downloaded": installed_by_this_call,
            "downloaded_bytes": downloaded_size,
            "extracted_bytes": extracted_size,
        }
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
