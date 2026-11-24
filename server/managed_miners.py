"""Trusted, versioned release catalog for managed miners.

The catalog refreshes from GitHub's official releases API, but it never trusts
release prose, arbitrary asset names, or arbitrary URLs.  Only exact release
tags and the platform archive names produced by the upstream projects are
accepted.  A small built-in catalog keeps flight sheets usable when GitHub is
unavailable.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .miner_adapters import MINER_ADAPTERS, ReleaseSource


# ``miner-adapter.v1`` is now the canonical inventory.  The legacy miner type
# strings remain on the wire and in SQLite during the compatibility phase, but
# adding/removing a built-in adapter no longer requires another independent
# type allowlist in the release catalog.
MINER_TYPES = MINER_ADAPTERS.miner_types
MANAGED_MINER_DISPLAY_NAMES = {
    adapter.legacy_miner_type: adapter.display_name
    for adapter in MINER_ADAPTERS.list()
}
# Version discovery and local archive retention are intentionally separate.
# Operators may select any supported release in this bounded catalog while the
# controller still keeps only a few unreferenced downloaded archives on disk.
MANAGED_CATALOG_RELEASE_LIMIT = 50
MANAGED_CATALOG_TTL_SECONDS = 15 * 60
GITHUB_API_RESPONSE_LIMIT = 2 * 1024 * 1024
from .miner_adapters.validation import SUPPORTED_VERSION as _SUPPORTED_VERSION_RE


@dataclass
class _DNSLookup:
    key: tuple[str, int]
    finished: threading.Event = field(default_factory=threading.Event)
    answers: list[tuple[Any, ...]] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)
    timed_out: bool = False


_DNS_LOOKUP_GUARD = threading.Lock()
_DNS_LOOKUP_IN_FLIGHT: _DNSLookup | None = None


@dataclass(frozen=True)
class ManagedMinerAsset:
    miner_type: str
    display_name: str
    version: str
    os_family: str
    filename: str
    archive_type: str
    download_url: str | None
    variant: str | None = None
    source_type: str = "github"
    expected_sha256: str | None = None

    @property
    def package_id(self) -> str:
        return managed_package_id(
            self.miner_type,
            self.version,
            self.os_family,
            variant=self.variant,
        )

    def public_payload(self) -> dict[str, object]:
        payload = {
            "os_family": self.os_family,
            "filename": self.filename,
            "archive_type": self.archive_type,
            "package_id": self.package_id,
            "source_type": self.source_type,
        }
        if self.download_url is not None:
            payload["download_url"] = self.download_url
        if self.variant:
            payload["variant"] = self.variant
        if self.expected_sha256 is not None:
            payload["expected_sha256"] = self.expected_sha256
        return payload


@dataclass(frozen=True)
class ManagedMinerRelease:
    miner_type: str
    display_name: str
    version: str
    assets: dict[str, ManagedMinerAsset]
    prerelease: bool = False
    source_type: str = "github"


@dataclass(frozen=True)
class _MinerDefinition:
    miner_type: str
    display_name: str
    owner: str
    repo: str
    fallback_versions: tuple[str, ...]
    release_source: ReleaseSource


_DEFINITIONS: dict[str, _MinerDefinition] = {
    adapter.legacy_miner_type: _MinerDefinition(
        miner_type=adapter.legacy_miner_type,
        display_name=adapter.display_name,
        owner=adapter.release.owner,
        repo=adapter.release.repository,
        fallback_versions=adapter.release.fallback_versions,
        release_source=adapter.release,
    )
    for adapter in MINER_ADAPTERS.list()
}


def normalize_version(value: str, miner_type: str | None = None) -> str:
    """Normalize one of the finite release version grammars.

    Callers that know the miner type also enforce that adapter's exact
    grammar.  The type-less path remains for legacy package metadata and
    accepts only the union of the two audited built-in grammars.
    """

    if miner_type is not None:
        adapter = MINER_ADAPTERS.require(miner_type.strip().lower())
        return adapter.release.normalize_version(value)
    if not isinstance(value, str) or not _SUPPORTED_VERSION_RE.fullmatch(value):
        raise ValueError("miner version does not use a supported release format")
    return value


def managed_version_sort_key(miner_type: str, version: str) -> tuple[int, int, int, str]:
    adapter = MINER_ADAPTERS.require(miner_type.strip().lower())
    return adapter.release.version_sort_key(version)


def normalize_os_family(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"ubuntu", "debian"}:
        normalized = "linux"
    if normalized not in {"windows", "linux"}:
        raise ValueError(f"managed miners are unavailable for {normalized or 'unknown'} x64")
    return normalized


def managed_package_id(
    miner_type: str,
    version: str,
    os_family: str,
    *,
    variant: str | None = None,
) -> str:
    normalized_type = miner_type.strip().lower()
    try:
        adapter = MINER_ADAPTERS.require(normalized_type)
    except ValueError as exc:
        # Preserve the established public error for existing API clients.
        raise ValueError("unsupported managed miner type") from exc
    normalized_version = adapter.release.normalize_version(version)
    normalized_os = normalize_os_family(os_family)
    normalized_variant = (variant or "").strip().lower() or None
    try:
        return adapter.package_id(
            normalized_version,
            normalized_os,
            variant=normalized_variant,
        )
    except ValueError as exc:
        if normalized_type == "pearlminer" and normalized_os == "linux":
            raise ValueError(
                "Pearl Miner Linux packages require nvidia or amd variant"
            ) from exc
        if normalized_variant:
            raise ValueError("managed miner package variant is unsupported") from exc
        raise


def managed_test_package_id(
    miner_type: str,
    version: str,
    os_family: str,
    variant: str | None = None,
) -> str:
    """Return a collision-free ID for an operator-supplied test package.

    Delegate all miner, version, platform and variant validation to the
    canonical package-ID builder so test packages cannot bypass the managed
    miner constraints.
    """

    builtin_id = managed_package_id(
        miner_type,
        version,
        os_family,
        variant=variant,
    )
    return f"test-{builtin_id.removeprefix('builtin-')}"


def _asset_filename(
    miner_type: str,
    version: str,
    os_family: str,
    variant: str | None = None,
) -> tuple[str, str]:
    adapter = MINER_ADAPTERS.require(miner_type)
    normalized_version = adapter.release.normalize_version(version)
    asset = adapter.asset(os_family, variant)
    return asset.filename(normalized_version), asset.archive_type


def _asset_specs(
    miner_type: str,
    version: str,
    *,
    fallback: bool = False,
) -> tuple[tuple[str, str | None, str], ...]:
    adapter = MINER_ADAPTERS.require(miner_type)
    return tuple(
        (asset.os_family, asset.variant, asset.key)
        for asset in adapter.assets
        if not fallback
        or not asset.fallback_versions
        or version in asset.fallback_versions
    )


def _official_asset_url(
    definition: _MinerDefinition,
    tag: str,
    filename: str,
) -> str:
    # Tags and filenames have already passed strict allowlists, so this is a
    # canonical URL rather than a value copied from untrusted API content.
    return (
        f"https://github.com/{definition.owner}/{definition.repo}/releases/download/"
        f"{tag}/{filename}"
    )


def _release_from_version(
    definition: _MinerDefinition,
    version: str,
) -> ManagedMinerRelease:
    definition.release_source.normalize_version(version)
    tag = definition.release_source.tag_for_version(version)
    assets: dict[str, ManagedMinerAsset] = {}
    for os_family, variant, asset_key in _asset_specs(
        definition.miner_type,
        version,
        fallback=True,
    ):
        filename, archive_type = _asset_filename(
            definition.miner_type,
            version,
            os_family,
            variant,
        )
        asset_spec = MINER_ADAPTERS.require(definition.miner_type).asset(
            os_family, variant
        )
        assets[asset_key] = ManagedMinerAsset(
            miner_type=definition.miner_type,
            display_name=definition.display_name,
            version=version,
            os_family=os_family,
            filename=filename,
            archive_type=archive_type,
            download_url=_official_asset_url(definition, tag, filename),
            variant=variant,
            expected_sha256=asset_spec.expected_sha256(version),
        )
    return ManagedMinerRelease(
        miner_type=definition.miner_type,
        display_name=definition.display_name,
        version=version,
        assets=assets,
    )


def fallback_catalog() -> dict[str, tuple[ManagedMinerRelease, ...]]:
    return {
        miner_type: tuple(
            _release_from_version(definition, version)
            for version in definition.fallback_versions[
                :MANAGED_CATALOG_RELEASE_LIMIT
            ]
        )
        for miner_type, definition in _DEFINITIONS.items()
    }


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, connect_ip: str, timeout: float):
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self._connect_ip = connect_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_ip, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _bounded_getaddrinfo(
    hostname: str,
    port: int,
    *,
    timeout_seconds: float = 2.0,
) -> list[tuple[Any, ...]]:
    """Resolve DNS without allowing a platform resolver to wedge refreshes.

    ``socket.getaddrinfo`` has no portable timeout and can block forever in a
    daemon worker.  Isolating it in a daemon thread gives the catalog's
    single-flight owner a firm release point even if the OS resolver stalls.
    """

    if timeout_seconds <= 0:
        raise ValueError("DNS timeout must be positive")
    global _DNS_LOOKUP_IN_FLIGHT
    key = (hostname, port)
    start_lookup = False
    with _DNS_LOOKUP_GUARD:
        lookup = _DNS_LOOKUP_IN_FLIGHT
        if lookup is not None and not lookup.finished.is_set():
            if lookup.key != key:
                raise RuntimeError("another managed-miner DNS lookup is still pending")
            if lookup.timed_out:
                raise RuntimeError("GitHub releases API DNS lookup is still pending")
        elif lookup is None or lookup.key != key:
            lookup = _DNSLookup(key=key)
            _DNS_LOOKUP_IN_FLIGHT = lookup
            start_lookup = True

    def resolve() -> None:
        try:
            lookup.answers.extend(
                socket.getaddrinfo(
                    hostname,
                    port,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                )
            )
        except Exception as exc:  # pragma: no branch - reported by caller
            lookup.errors.append(exc)
        finally:
            lookup.finished.set()

    if start_lookup:
        worker = threading.Thread(
            target=resolve,
            name="rigpilot-managed-miner-dns",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            lookup.errors.append(exc)
            lookup.finished.set()
    if not lookup.finished.wait(timeout_seconds):
        with _DNS_LOOKUP_GUARD:
            if _DNS_LOOKUP_IN_FLIGHT is lookup:
                lookup.timed_out = True
        raise RuntimeError("GitHub releases API DNS lookup timed out")
    with _DNS_LOOKUP_GUARD:
        if _DNS_LOOKUP_IN_FLIGHT is lookup:
            _DNS_LOOKUP_IN_FLIGHT = None
    if lookup.errors:
        raise RuntimeError("GitHub releases API could not be resolved") from lookup.errors[0]
    return list(lookup.answers)


def _github_public_addresses() -> list[str]:
    answers = _bounded_getaddrinfo("api.github.com", 443)
    addresses: list[str] = []
    for answer in answers:
        value = answer[4][0].split("%", 1)[0]
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise RuntimeError("GitHub releases API resolved to a non-public address")
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise RuntimeError("GitHub releases API has no usable address")
    return addresses


def _fetch_github_releases(owner: str, repo: str) -> list[dict[str, Any]]:
    """Read a bounded JSON response from the one allowed API host.

    The owner/repository values are internal constants.  DNS is resolved once,
    public addresses are required, redirects are rejected, and TLS keeps the
    original api.github.com hostname for certificate verification and SNI.
    """

    if (owner, repo) not in {
        (definition.owner, definition.repo) for definition in _DEFINITIONS.values()
    }:
        raise ValueError("unsupported GitHub release repository")
    path = (
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/releases"
        f"?per_page={MANAGED_CATALOG_RELEASE_LIMIT}"
    )
    last_error: Exception | None = None
    deadline = time.monotonic() + 6.0
    for address in _github_public_addresses():
        connection: _PinnedHTTPSConnection | None = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = _PinnedHTTPSConnection(
                "api.github.com", address, timeout=min(4.0, remaining)
            )
            connection.request(
                "GET",
                path,
                headers={
                    "Host": "api.github.com",
                    "User-Agent": "RigPilot-Managed-Miner-Catalog/1.0",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise RuntimeError("GitHub releases API redirects are not allowed")
            if response.status != 200:
                raise RuntimeError(
                    f"GitHub releases API returned HTTP {response.status}"
                )
            content_type = (response.getheader("Content-Type") or "").lower()
            if not content_type.startswith("application/json"):
                raise RuntimeError("GitHub releases API did not return JSON")
            if (response.getheader("Content-Encoding") or "identity").lower() != "identity":
                raise RuntimeError("GitHub releases API used content encoding")
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise RuntimeError("invalid GitHub API Content-Length") from exc
                if declared_size < 0 or declared_size > GITHUB_API_RESPONSE_LIMIT:
                    raise RuntimeError("GitHub releases API response is too large")
            body = response.read(GITHUB_API_RESPONSE_LIMIT + 1)
            if len(body) > GITHUB_API_RESPONSE_LIMIT:
                raise RuntimeError("GitHub releases API response is too large")
            parsed = json.loads(body.decode("utf-8"))
            if not isinstance(parsed, list):
                raise RuntimeError("GitHub releases API returned an invalid document")
            return [item for item in parsed if isinstance(item, dict)]
        except (OSError, http.client.HTTPException, ValueError) as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
    raise RuntimeError("could not read GitHub releases API") from last_error


def _parse_releases(
    definition: _MinerDefinition,
    documents: list[dict[str, Any]],
) -> tuple[ManagedMinerRelease, ...]:
    accepted: list[ManagedMinerRelease] = []
    seen: set[str] = set()
    for document in documents[:MANAGED_CATALOG_RELEASE_LIMIT]:
        if document.get("draft") is not False:
            continue
        prerelease = document.get("prerelease")
        if not isinstance(prerelease, bool):
            continue
        if prerelease and not definition.release_source.allows_prereleases:
            continue
        tag_and_version = definition.release_source.parse_tag(document.get("tag_name"))
        if not tag_and_version:
            continue
        tag, version = tag_and_version
        if version in seen:
            continue
        raw_assets = document.get("assets")
        if not isinstance(raw_assets, list):
            continue
        asset_records: dict[str, tuple[str, object]] = {}
        for item in raw_assets[:100]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            url = item.get("browser_download_url")
            if isinstance(name, str) and isinstance(url, str):
                asset_records[name] = (url, item.get("digest"))
        assets: dict[str, ManagedMinerAsset] = {}
        for os_family, variant, asset_key in _asset_specs(
            definition.miner_type,
            version,
        ):
            filename, archive_type = _asset_filename(
                definition.miner_type,
                version,
                os_family,
                variant,
            )
            official_url = _official_asset_url(definition, tag, filename)
            asset_spec = MINER_ADAPTERS.require(definition.miner_type).asset(
                os_family, variant
            )
            expected_sha256 = asset_spec.expected_sha256(version)
            if asset_spec.sha256_by_version and expected_sha256 is None:
                # This adapter opts into source-pinned assets. New upstream
                # releases remain unavailable until their official digest is
                # deliberately added to the trusted adapter manifest.
                continue
            # Merely having the expected name is insufficient: the API URL
            # must also exactly match the canonical repository release URL.
            record = asset_records.get(filename)
            if record is None or record[0] != official_url:
                continue
            if (
                expected_sha256 is not None
                and record[1] != f"sha256:{expected_sha256}"
            ):
                continue
            parsed_url = urlparse(official_url)
            if (
                parsed_url.scheme != "https"
                or parsed_url.hostname != "github.com"
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                continue
            assets[asset_key] = ManagedMinerAsset(
                miner_type=definition.miner_type,
                display_name=definition.display_name,
                version=version,
                os_family=os_family,
                filename=filename,
                archive_type=archive_type,
                download_url=official_url,
                variant=variant,
                expected_sha256=expected_sha256,
            )
        if not assets:
            continue
        seen.add(version)
        accepted.append(
            ManagedMinerRelease(
                miner_type=definition.miner_type,
                display_name=definition.display_name,
                version=version,
                assets=assets,
                prerelease=prerelease,
            )
        )
    if not accepted:
        raise RuntimeError(f"GitHub returned no supported {definition.display_name} releases")
    accepted.sort(
        key=lambda release: definition.release_source.version_sort_key(release.version),
        reverse=True,
    )
    if not definition.release_source.allows_prereleases:
        return tuple(accepted)

    latest_stable = next(
        (release for release in accepted if not release.prerelease),
        None,
    )
    if latest_stable is None:
        raise RuntimeError(
            f"GitHub returned no supported stable {definition.display_name} release"
        )

    # Each of the bounded input documents can add at most one release.
    return tuple(accepted)


CatalogFetcher = Callable[[str, str], list[dict[str, Any]]]
Clock = Callable[[], float]


class ManagedMinerCatalog:
    """Thread-safe stale-while-revalidate release catalog.

    Reads always return the last complete fallback/cache snapshot.  An expired
    snapshot starts at most one daemon refresh and never makes an API request
    wait on GitHub.  ``refresh_sync`` exists for deterministic tests and
    maintenance code; request handlers intentionally do not use it.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = MANAGED_CATALOG_TTL_SECONDS,
        failure_ttl_seconds: float = 60.0,
        fetcher: CatalogFetcher | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._ttl_seconds = max(0.0, ttl_seconds)
        self._failure_ttl_seconds = max(0.0, failure_ttl_seconds)
        self._fetcher = fetcher or _fetch_github_releases
        self._clock = clock
        self._lock = threading.Lock()
        self._refresh_finished = threading.Condition(self._lock)
        self._refresh_in_flight = False
        self._expires_at = 0.0
        self._catalog = fallback_catalog()
        self._sources = {miner_type: "fallback" for miner_type in MINER_TYPES}
        self._refresh_errors: dict[str, str | None] = {
            miner_type: None for miner_type in MINER_TYPES
        }
        self._checked_at = datetime.now(timezone.utc)
        self._refresh_after = self._checked_at

    def invalidate(self) -> None:
        with self._lock:
            self._expires_at = 0.0

    def _perform_refresh(self) -> None:
        updates: dict[str, tuple[ManagedMinerRelease, ...]] = {}
        refresh_errors: dict[str, str | None] = {}
        failed = False
        for miner_type in _DEFINITIONS:
            definition = _DEFINITIONS[miner_type]
            try:
                documents = self._fetcher(definition.owner, definition.repo)
                updates[miner_type] = _parse_releases(definition, documents)
                refresh_errors[miner_type] = None
            except Exception as exc:
                # Preserve the last usable snapshot for this miner.  On first
                # boot that snapshot is the built-in fallback catalog.
                failed = True
                message = str(exc).strip() or exc.__class__.__name__
                refresh_errors[miner_type] = message[:240]
        checked_at = datetime.now(timezone.utc)
        ttl = self._failure_ttl_seconds if failed else self._ttl_seconds
        with self._lock:
            catalog = dict(self._catalog)
            sources = dict(self._sources)
            errors = dict(self._refresh_errors)
            for miner_type, releases in updates.items():
                catalog[miner_type] = releases
                sources[miner_type] = "github"
            errors.update(refresh_errors)
            self._catalog = catalog
            self._sources = sources
            self._refresh_errors = errors
            self._expires_at = self._clock() + ttl
            self._checked_at = checked_at
            self._refresh_after = checked_at + timedelta(seconds=ttl)

    def _finish_refresh(self) -> None:
        try:
            self._perform_refresh()
        finally:
            with self._refresh_finished:
                self._refresh_in_flight = False
                self._refresh_finished.notify_all()

    def _start_background_refresh_if_needed(self) -> None:
        with self._refresh_finished:
            if self._clock() < self._expires_at or self._refresh_in_flight:
                return
            self._refresh_in_flight = True
            worker = threading.Thread(
                target=self._finish_refresh,
                name="rigpilot-managed-miner-catalog-refresh",
                daemon=True,
            )
            try:
                worker.start()
            except Exception:
                self._refresh_in_flight = False
                self._refresh_finished.notify_all()

    def refresh_sync(self, *, force: bool = True, timeout: float | None = None) -> None:
        """Synchronously refresh, intended for tests rather than API reads.

        If a background refresh already owns the single-flight slot this waits
        for that refresh instead of starting a duplicate.  A bounded timeout
        raises ``TimeoutError`` without cancelling the daemon worker.
        """

        with self._refresh_finished:
            if self._refresh_in_flight:
                completed = self._refresh_finished.wait_for(
                    lambda: not self._refresh_in_flight,
                    timeout=timeout,
                )
                if not completed:
                    raise TimeoutError("managed miner catalog refresh timed out")
                return
            if not force and self._clock() < self._expires_at:
                return
            self._refresh_in_flight = True
        self._finish_refresh()

    def releases(self, miner_type: str) -> tuple[ManagedMinerRelease, ...]:
        normalized = miner_type.strip().lower()
        if normalized not in MINER_TYPES:
            raise ValueError("unsupported managed miner type")
        self._start_background_refresh_if_needed()
        with self._lock:
            return self._catalog[normalized]

    def latest_version(self, miner_type: str) -> str:
        for release in self.releases(miner_type):
            if not release.prerelease:
                return release.version
        raise ValueError("managed miner catalog has no stable release")

    def has_version(self, miner_type: str, version: str) -> bool:
        normalize_version(version, miner_type)
        return any(item.version == version for item in self.releases(miner_type))

    def asset(
        self,
        miner_type: str,
        os_family: str,
        version: str | None = None,
        *,
        variant: str | None = None,
    ) -> ManagedMinerAsset:
        normalized_type = miner_type.strip().lower()
        normalized_os = normalize_os_family(os_family)
        releases = self.releases(normalized_type)
        selected = version or next(
            (release.version for release in releases if not release.prerelease), None
        )
        if selected is None:
            raise ValueError("managed miner catalog has no stable release")
        normalize_version(selected, normalized_type)
        normalized_variant = (variant or "").strip().lower() or None
        adapter = MINER_ADAPTERS.require(normalized_type)
        try:
            asset_key = adapter.asset(normalized_os, normalized_variant).key
        except ValueError as exc:
            if normalized_type == "pearlminer" and normalized_os == "linux":
                raise ValueError(
                    "Pearl Miner Linux packages require nvidia or amd variant"
                ) from exc
            if normalized_variant:
                raise ValueError("managed miner package variant is unsupported") from exc
            raise
        for release in releases:
            if release.version == selected:
                try:
                    return release.assets[asset_key]
                except KeyError as exc:
                    raise ValueError(
                        f"{normalized_type} {selected} is unavailable for {normalized_os} x64"
                    ) from exc
        raise ValueError(
            f"{normalized_type} version {selected} is not in the current managed catalog"
        )

    def payload(self) -> list[dict[str, object]]:
        self._start_background_refresh_if_needed()
        with self._lock:
            catalog = self._catalog
            sources = dict(self._sources)
            refresh_errors = dict(self._refresh_errors)
            checked_at = self._checked_at
            refresh_after = self._refresh_after
        result: list[dict[str, object]] = []
        for miner_type in _DEFINITIONS:
            releases = catalog[miner_type]
            latest_version = next(
                (release.version for release in releases if not release.prerelease),
                None,
            )
            if latest_version is None:
                raise ValueError("managed miner catalog has no stable release")
            supported_os = sorted(
                {
                    asset.os_family
                    for release in releases
                    for asset in release.assets.values()
                }
            )
            result.append(
                {
                    "id": miner_type,
                    "name": releases[0].display_name,
                    "version": latest_version,
                    "latest_version": latest_version,
                    "versions": [
                        {
                            "version": release.version,
                            "prerelease": release.prerelease,
                            "source_type": release.source_type,
                            "assets": {
                                os_family: asset.public_payload()
                                for os_family, asset in release.assets.items()
                            },
                        }
                        for release in releases
                    ],
                    "supported_os": supported_os,
                    "catalog_source": sources[miner_type],
                    "refresh_error": refresh_errors[miner_type],
                    "checked_at": checked_at.isoformat(timespec="seconds"),
                    "refresh_after": refresh_after.isoformat(timespec="seconds"),
                    "xmr_default_algorithm": (
                        None if miner_type == "xmrig" else "randomx"
                    ),
                    "pearl_default_algorithm": (
                        "pearlhash" if miner_type == "pearlminer" else None
                    ),
                }
            )
        return result
# Conservative coin defaults. XMRig is intentionally XMR-only in the simple
# workflow. SRBMiner allows a small set of obvious defaults.
SRB_COIN_ALGORITHMS: dict[str, str] = dict(
    MINER_ADAPTERS.require("srbminer").coin_defaults
)

SRB_GPU_ALGORITHMS = frozenset(item.name for item in MINER_ADAPTERS.require("srbminer").algorithm_specs if "gpu" in item.resources)
SRB_CPU_ALGORITHMS = frozenset(item.name for item in MINER_ADAPTERS.require("srbminer").algorithm_specs if "cpu" in item.resources)
SRB_SIMPLE_ALGORITHMS = frozenset(MINER_ADAPTERS.require("srbminer").algorithms)
SRB_DUAL_ALGORITHM_PAIRS = frozenset(MINER_ADAPTERS.require("srbminer").dual_algorithm_pairs)


def normalize_algorithm(miner_type: str, coin: str, algorithm: str | None) -> str | None:
    normalized_type = miner_type.strip().lower()
    normalized_coin = coin.strip().upper()
    try:
        adapter = MINER_ADAPTERS.require(normalized_type)
    except ValueError as exc:
        raise ValueError("unsupported managed miner type") from exc
    if normalized_type == "xmrig":
        if normalized_coin != "XMR":
            raise ValueError("XMRig simple mode currently supports XMR only")
        adapter.normalize_algorithm(None)
        # The legacy XMRig wire format selects RandomX through its generated
        # config and historically stores a null algorithm. Keep that shape
        # until Flight Sheet V2 is the only protocol.
        return None
    if normalized_type == "pearlminer":
        if normalized_coin != "PEARL":
            raise ValueError("Pearl Miner simple mode currently supports PEARL only")
        try:
            value = adapter.normalize_algorithm(algorithm)
        except ValueError as exc:
            raise ValueError("Pearl Miner requires the pearlhash algorithm") from exc
        return value
    if normalized_type == "srbminer":
        requested = algorithm or SRB_COIN_ALGORITHMS.get(normalized_coin, "")
        if not requested:
            raise ValueError("SRBMiner requires an algorithm for this coin")
        try:
            return adapter.normalize_algorithm(requested, coin=normalized_coin)
        except ValueError as exc:
            raise ValueError(
                f"Unsupported SRBMiner algorithm: {str(requested).strip().lower()}"
            ) from exc
    try:
        value = adapter.normalize_algorithm(algorithm, coin=normalized_coin)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported {adapter.display_name} algorithm for {normalized_coin}"
        ) from exc
    return value


def public_adapter_catalog() -> list[dict[str, object]]:
    """Return the audited, data-only adapter manifests for UI/API clients."""

    return MINER_ADAPTERS.public_catalog()
