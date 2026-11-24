"""Immutable contract for trusted RigPilot miner adapters.

The adapter ABI is deliberately declarative.  A manifest may select a
runtime and telemetry implementation that is already bundled with the Agent,
but it cannot provide Python, PowerShell, shell fragments, executable paths,
or shell command lines. Literal extra CLI arguments remain data. This keeps future miner integration small while
preserving the Agent's local trust boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from string import Formatter
from typing import Iterable
from .retired import is_retired_miner, retired_miner_type


MINER_ADAPTER_ABI = "miner-adapter.v1"
from .validation import (
    THREE_PART_VERSION as _THREE_PART_NUMERIC_VERSION,
    TWO_PART_VERSION as _TWO_PART_OPTIONAL_LETTER_VERSION,
    SUPPORTED_VERSION as _SUPPORTED_RELEASE_VERSION,
    validate_option_value,
)
_IMPLEMENTATION_ID = re.compile(
    r"^builtin(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)+\.v[1-9][0-9]*$"
)
_PACKAGE_NAMESPACE = re.compile(r"^(?:builtin|test)-[a-z0-9][a-z0-9-]*-$")
_ADAPTER_ID = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_MINER_TYPE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOKEN = re.compile(r"^[a-z0-9]+(?:[_.-][a-z0-9]+)*$")
_ASSET_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OPTION_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_COIN = re.compile(r"^[A-Z0-9](?:[A-Z0-9._+-]*[A-Z0-9])?$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_OS = frozenset({"windows", "linux"})
_ALLOWED_ARCHIVES = frozenset({"zip", "tar.gz"})
_ALLOWED_RESOURCES = frozenset({"cpu", "gpu"})
_ALLOWED_POOL_SCHEMES = frozenset(
    {"pearl", "stratum+tcp", "stratum+ssl", "stratum+tls", "quic"}
)
_ALLOWED_CAPABILITIES = frozenset(
    {
        "native",
        "wsl_gpu",
        "cpu",
        "gpu",
        "integrated_dual",
        "independent_instances",
        "device_probe",
        "device_selection",
        "miner_cli_oc",
        "local_http_api",
        "log_telemetry",
    }
)


class ReleaseTagStyle(str, Enum):
    """Strict upstream release-tag encodings understood by the controller."""

    V_PREFIX = "v_prefix"
    PLAIN = "plain"


class ReleaseVersionStyle(str, Enum):
    """Finite upstream version grammars understood without executable hooks."""

    THREE_PART_NUMERIC = "three_part_numeric"
    TWO_PART_OPTIONAL_SUFFIX = "two_part_optional_suffix"


class PrereleasePolicy(str, Enum):
    """Whether preview releases may be listed and how defaults are selected."""

    STABLE_ONLY = "stable_only"
    ALLOW_WITH_STABLE_DEFAULT = "allow_with_stable_default"


@dataclass(frozen=True, slots=True)
class ReleaseSource:
    """One allowlisted upstream release source."""

    owner: str
    repository: str
    fallback_versions: tuple[str, ...]
    resolver_id: str
    tag_style: ReleaseTagStyle
    prerelease_policy: PrereleasePolicy
    version_style: ReleaseVersionStyle = ReleaseVersionStyle.THREE_PART_NUMERIC
    minimum_version: str | None = None

    def accepts_version(self, version: object) -> bool:
        pattern = (
            _THREE_PART_NUMERIC_VERSION
            if self.version_style is ReleaseVersionStyle.THREE_PART_NUMERIC
            else _TWO_PART_OPTIONAL_LETTER_VERSION
        )
        return isinstance(version, str) and pattern.fullmatch(version) is not None

    def normalize_version(self, version: object) -> str:
        if not self.accepts_version(version):
            expected = (
                "numeric MAJOR.MINOR.PATCH"
            if self.version_style is ReleaseVersionStyle.THREE_PART_NUMERIC
                else "numeric MAJOR.MINOR with an optional lowercase suffix"
            )
            raise ValueError(f"miner version must use {expected} format")
        normalized = str(version)
        if self.minimum_version is not None:
            if not self.accepts_version(self.minimum_version):
                raise ValueError("miner adapter minimum release version is invalid")
            if self._normalized_sort_key(normalized) < self._normalized_sort_key(
                self.minimum_version
            ):
                raise ValueError(
                    f"miner version must be {self.minimum_version} or newer"
                )
        return normalized

    def _normalized_sort_key(self, normalized: str) -> tuple[int, int, int, str]:
        """Sort a value that already passed this source's finite grammar."""

        if self.version_style is ReleaseVersionStyle.THREE_PART_NUMERIC:
            major, minor, patch = (int(part) for part in normalized.split("."))
            return major, minor, patch, ""
        match = _TWO_PART_OPTIONAL_LETTER_VERSION.fullmatch(normalized)
        assert match is not None
        suffix = match.group(3)
        return int(match.group(1)), int(match.group(2)), 0, suffix or ""

    def version_sort_key(self, version: object) -> tuple[int, int, int, str]:
        normalized = self.normalize_version(version)
        return self._normalized_sort_key(normalized)

    def tag_for_version(self, version: str) -> str:
        version = self.normalize_version(version)
        return f"v{version}" if self.tag_style is ReleaseTagStyle.V_PREFIX else version

    def parse_tag(self, value: object) -> tuple[str, str] | None:
        if not isinstance(value, str):
            return None
        version = value[1:] if self.tag_style is ReleaseTagStyle.V_PREFIX else value
        try:
            normalized = self.normalize_version(version)
        except ValueError:
            return None
        expected_tag = f"v{normalized}" if self.tag_style is ReleaseTagStyle.V_PREFIX else normalized
        if value != expected_tag:
            return None
        return value, normalized

    @property
    def allows_prereleases(self) -> bool:
        return self.prerelease_policy is PrereleasePolicy.ALLOW_WITH_STABLE_DEFAULT

    def public_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider": "github",
            "owner": self.owner,
            "repository": self.repository,
            "fallback_versions": list(self.fallback_versions),
            "resolver_id": self.resolver_id,
            "tag_style": self.tag_style.value,
            "prerelease_policy": self.prerelease_policy.value,
            "version_style": self.version_style.value,
        }
        if self.minimum_version is not None:
            payload["minimum_version"] = self.minimum_version
        return payload


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    """Trusted package/executable metadata for one operating system."""

    os_family: str
    executable_candidates: tuple[str, ...]
    asset_variants: tuple[str, ...] = ()

    def public_payload(self) -> dict[str, object]:
        return {
            "os_family": self.os_family,
            "executable_candidates": list(self.executable_candidates),
            "asset_variants": list(self.asset_variants),
        }


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """One deterministic upstream package asset accepted by the adapter."""

    key: str
    os_family: str
    filename_template: str
    archive_type: str
    variant: str | None = None
    fallback_versions: tuple[str, ...] = ()
    filename_overrides: tuple[tuple[str, str], ...] = ()
    sha256_by_version: tuple[tuple[str, str], ...] = ()

    def filename(self, version: str) -> str:
        if not isinstance(version, str) or _SUPPORTED_RELEASE_VERSION.fullmatch(version) is None:
            raise ValueError("miner version does not use a supported release format")
        override = dict(self.filename_overrides).get(version)
        if override is not None:
            return override
        return self.filename_template.format(
            version=version,
            version_dashed=version.replace(".", "-"),
        )

    def expected_sha256(self, version: str) -> str | None:
        return dict(self.sha256_by_version).get(version)

    def public_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key": self.key,
            "os_family": self.os_family,
            "archive_type": self.archive_type,
            "variant": self.variant,
        }
        if self.fallback_versions:
            payload["fallback_versions"] = list(self.fallback_versions)
        if self.sha256_by_version:
            payload["sha256_pinned_versions"] = [
                version for version, _digest in self.sha256_by_version
            ]
        return payload


@dataclass(frozen=True, slots=True)
class AlgorithmSpec:
    """One canonical algorithm and its resource ownership."""

    name: str
    resource_kind: str
    coins: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    supported_resources: tuple[str, ...] = ()
    gpu_vendors: tuple[str, ...] = ()
    minimum_version: str | None = None
    removed_version: str | None = None
    catalogue_status: str = "current"
    gpu_minimum_versions: tuple[tuple[str, str], ...] = ()

    @property
    def resources(self) -> tuple[str, ...]:
        return self.supported_resources or (self.resource_kind,)

    def supports_version(self, version: str | None) -> bool:
        if not version: return True
        parts = tuple(int(p) for p in version.split(".")) if _THREE_PART_NUMERIC_VERSION.fullmatch(version) else None
        if parts is None: return self.minimum_version is None and self.removed_version is None
        if self.minimum_version and parts < tuple(int(p) for p in self.minimum_version.split(".")): return False
        if self.removed_version and parts >= tuple(int(p) for p in self.removed_version.split(".")): return False
        return True

    def matches(self, value: str) -> bool:
        return value == self.name or value in self.aliases

    def public_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "resource_kind": self.resource_kind,
            "coins": list(self.coins),
            "aliases": list(self.aliases),
            "supported_resources": list(self.resources),
            "gpu_vendors": list(self.gpu_vendors),
            "gpu_minimum_versions": dict(self.gpu_minimum_versions),
            "minimum_version": self.minimum_version,
            "removed_version": self.removed_version,
            "catalogue_status": self.catalogue_status,
        }


@dataclass(frozen=True, slots=True)
class OptionSpec:
    """A typed adapter option suitable for validation and generated UI."""

    name: str
    value_type: str
    label: str
    description: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str | int, ...] = ()
    default: str | int | bool | None = None
    nullable: bool = False
    scope: str = "job"
    min_length: int = 0
    max_length: int | None = None

    def public_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "type": self.value_type,
            "label": self.label,
            "description": self.description,
            "default": self.default,
            "nullable": self.nullable,
            "scope": self.scope,
        }
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.value_type == "string":
            payload["min_length"] = self.min_length
            payload["max_length"] = self.max_length
            if self.name == "extra_args":
                payload["multiline"] = True
        return payload


@dataclass(frozen=True, slots=True)
class MinerAdapter:
    """A trusted, immutable miner adapter manifest."""

    adapter_id: str
    adapter_revision: int
    legacy_miner_type: str
    display_name: str
    runtime_id: str
    telemetry_id: str
    release: ReleaseSource
    platforms: tuple[PlatformSpec, ...]
    assets: tuple[AssetSpec, ...]
    algorithm_specs: tuple[AlgorithmSpec, ...]
    default_algorithm: str
    coin_defaults: tuple[tuple[str, str], ...]
    resource_kinds: frozenset[str]
    pool_schemes: frozenset[str]
    options: tuple[OptionSpec, ...]
    capabilities: frozenset[str]
    package_namespaces: tuple[str, ...]
    abi_version: str = MINER_ADAPTER_ABI
    dual_algorithm_pairs: tuple[tuple[str, str], ...] = ()
    dual_any_secondary: tuple[str, ...] = ()
    # Explicit existing coin policies are separate from default-algorithm hints.
    coin_algorithm_constraints: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def option_keys(self) -> frozenset[str]:
        return frozenset(item.name for item in self.options)

    @property
    def algorithms(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.algorithm_specs)

    @property
    def executables_by_system(self) -> dict[str, tuple[str, ...]]:
        return {
            item.os_family: item.executable_candidates for item in self.platforms
        }

    def platform(self, system_name: str) -> PlatformSpec | None:
        normalized = system_name.strip().lower()
        normalized = {
            "windows": "windows",
            "linux": "linux",
            "ubuntu": "linux",
            "debian": "linux",
        }.get(normalized, normalized)
        return next(
            (item for item in self.platforms if item.os_family == normalized),
            None,
        )

    def executable_candidates(self, system_name: str) -> tuple[str, ...]:
        platform = self.platform(system_name)
        return platform.executable_candidates if platform is not None else ()

    def matches_package_id(self, package_id: object) -> bool:
        return isinstance(package_id, str) and package_id.startswith(
            self.package_namespaces
        )

    def asset(self, os_family: str, variant: str | None = None) -> AssetSpec:
        normalized_os = {
            "ubuntu": "linux",
            "debian": "linux",
        }.get(os_family.strip().lower(), os_family.strip().lower())
        normalized_variant = variant.strip().lower() if isinstance(variant, str) else None
        result = next(
            (
                item
                for item in self.assets
                if item.os_family == normalized_os and item.variant == normalized_variant
            ),
            None,
        )
        if result is None:
            raise ValueError("miner adapter platform variant is unsupported")
        return result

    def package_id(
        self,
        version: str,
        os_family: str,
        *,
        variant: str | None = None,
        test: bool = False,
    ) -> str:
        version = self.release.normalize_version(version)
        asset = self.asset(os_family, variant)
        suffix = f"-{asset.variant}" if asset.variant else ""
        prefix = "test" if test else "builtin"
        return (
            f"{prefix}-{self.legacy_miner_type}-{version}-"
            f"{asset.os_family}-x64{suffix}"
        )

    def normalize_algorithm(self, value: object, *, coin: object = None) -> str:
        normalized_coin = str(coin or "").strip().upper()
        coin_default = dict(self.coin_defaults).get(normalized_coin)
        normalized = str(value or coin_default or self.default_algorithm).strip().lower()
        normalized = normalized or self.default_algorithm
        match = next(
            (item for item in self.algorithm_specs if item.matches(normalized)),
            None,
        )
        if match is None:
            raise ValueError("miner adapter algorithm is unsupported")
        allowed = dict(self.coin_algorithm_constraints).get(normalized_coin)
        if allowed is not None and match.name not in allowed:
            raise ValueError(f"{normalized_coin} managed mining requires " + ", ".join(allowed))
        if normalized_coin and match.coins and normalized_coin not in match.coins:
            raise ValueError("miner adapter algorithm does not support this coin")
        return match.name

    def algorithm_spec(self, value: object, *, coin: object = None) -> AlgorithmSpec:
        normalized = self.normalize_algorithm(value, coin=coin)
        return next(item for item in self.algorithm_specs if item.name == normalized)

    def algorithm_resource(self, value: object, *, coin: object = None, resource_kind: object = None) -> str:
        spec = self.algorithm_spec(value, coin=coin)
        selected = str(resource_kind or spec.resource_kind).strip().lower()
        if selected not in spec.resources:
            raise ValueError("selected algorithm does not support this task resource")
        return selected

    def validate_algorithm_version(self, value: object, version: str | None, *, coin: object = None) -> str:
        spec = self.algorithm_spec(value, coin=coin)
        if not spec.supports_version(version):
            if spec.minimum_version:
                raise ValueError(f"{spec.name} requires miner version {spec.minimum_version} or newer")
            raise ValueError(f"{spec.name} was removed in miner version {spec.removed_version}; keep historical data or select a compatible older binary")
        return spec.name

    def validate_execution_context(
        self, algorithm: object, version: str | None, options: dict[str, object],
        *, coin: object = None,
    ) -> str:
        """Validate known software floors before replacing a running miner.

        An unresolved version is checked again after package selection, like
        algorithm version bounds. Unknown future flags have no guessed floor.
        Extra flags never select a resource or infer a device identity here.
        """
        from .srb_catalog import SRB_PEARL_K1_MINIMUM_VERSION
        from .validation import parse_extra_args

        normalized = self.validate_algorithm_version(algorithm, version, coin=coin)
        arguments = parse_extra_args(options.get("extra_args", ""), miner_type=self.legacy_miner_type)
        if self.legacy_miner_type == "srbminer" and version and any(
            argument.split("=", 1)[0].casefold() == "--pearl-k1" for argument in arguments
        ):
            if self.release.version_sort_key(version) < self.release.version_sort_key(SRB_PEARL_K1_MINIMUM_VERSION):
                raise ValueError(f"--pearl-k1 requires SRBMiner {SRB_PEARL_K1_MINIMUM_VERSION} or newer")
        return normalized

    def validate_pool_transport(self, value: str, algorithm: str | None, version: str | None,
                                options: dict[str, object], *, pool_password: str | None = None) -> None:
        from .validation import parse_pool_endpoint
        endpoint = parse_pool_endpoint(value, self.pool_schemes)
        normalized = self.validate_algorithm_version(algorithm, version)
        if endpoint.scheme == "quic":
            if self.legacy_miner_type != "srbminer" or normalized != "quantus":
                raise ValueError("QUIC is supported only for SRBMiner Quantus")
            pin = options.get("tls_cert_sha256")
            if not isinstance(pin, str) or not pin.strip():
                raise ValueError("Quantus QUIC requires tls_cert_sha256 (DER certificate SHA256 pin)")
            if len(pin) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in pin):
                raise ValueError("tls_cert_sha256 contains an invalid length or control characters")
            self.quic_wallet_value("", options, pool_password=pool_password)

    def quic_wallet_value(self, wallet_address: str, options: dict[str, object], *, pool_password: str | None = None) -> str:
        """Exact QUIC --wallet value: public payout address or explicitly selected node token.

        Callers must not append a worker suffix to this value. Optional/omitted
        mode remains compatible with public pools and never infers a node token.
        """
        mode = options.get("quic_wallet_mode") or "wallet"
        if mode == "wallet":
            return wallet_address
        if mode != "token":
            raise ValueError("quic_wallet_mode must be wallet or token")
        if not isinstance(pool_password, str) or not pool_password.strip():
            raise ValueError("Quantus node QUIC requires an explicitly supplied authentication token in the secret pool credential field")
        if len(pool_password) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in pool_password):
            raise ValueError("Quantus node authentication token contains an invalid length or control characters")
        return pool_password

    def validate_hardware_version(self, algorithm: str, version: str, *, vendor: str, name: str = "") -> None:
        """Check documented GPU floors after discovery; unknown hardware stays unknown."""
        if self.legacy_miner_type != "srbminer":
            return
        spec = self.algorithm_spec(algorithm)
        self.validate_algorithm_version(algorithm, version)
        if not spec.gpu_minimum_versions:
            return
        parts = tuple(int(p) for p in version.split("."))
        vendor = vendor.lower()
        floor = dict(spec.gpu_minimum_versions).get(vendor)
        if floor and parts < tuple(int(p) for p in floor.split(".")):
            raise ValueError(f"{spec.name} on {vendor} requires SRBMiner {floor} or newer")
        if spec.name == "quantus" and vendor == "amd" and re.search(r"\bRX\s*[45]\d{2}\b", name, re.I) and parts < (3, 6, 8):
            raise ValueError("Quantus on AMD RX4xx/RX5xx requires SRBMiner 3.6.8 or newer")

    def runtime_descriptor_v1(self) -> dict[str, object]:
        """Return this adapter's content-bound Flight V2 runtime declaration.

        The local import avoids a contract/built-in registry cycle.  Missing
        or incomplete declarations fail closed in ``runtime_v1``; callers must
        not substitute a miner-name-specific Agent handler.
        """

        from .runtime_v1 import runtime_descriptor_for

        return runtime_descriptor_for(self)

    def runtime_public_payload_v1(self) -> dict[str, object]:
        """Return the exact runtime payload embedded in authenticated jobs."""

        from .runtime_v1 import public_runtime_payload_for

        return public_runtime_payload_for(self)

    def public_payload(self) -> dict[str, object]:
        from .srb_catalog import (
            SRB_CATALOG_COMMIT,
            SRB_CATALOG_VERSION,
            SRB_ALGORITHM_MINIMUM_VERSIONS,
            SRB_PERIODIC_CPU_THREADS_ERROR,
            SRB_PERIODIC_CPU_THREADS_HINT,
        )

        return {
            "abi_version": self.abi_version,
            "adapter_id": self.adapter_id,
            "adapter_revision": self.adapter_revision,
            "miner_type": self.legacy_miner_type,
            "display_name": self.display_name,
            "runtime_id": self.runtime_id,
            "telemetry_id": self.telemetry_id,
            "release": self.release.public_payload(),
            "platforms": [item.public_payload() for item in self.platforms],
            "assets": [item.public_payload() for item in self.assets],
            "algorithms": [item.public_payload() for item in self.algorithm_specs],
            "default_algorithm": self.default_algorithm,
            "coin_defaults": dict(self.coin_defaults),
            "resource_kinds": sorted(self.resource_kinds),
            "pool_schemes": sorted(self.pool_schemes),
            "options": [item.public_payload() for item in self.options],
            "capabilities": sorted(self.capabilities),
            "package_namespaces": list(self.package_namespaces),
            "dual_algorithm_pairs": [list(pair) for pair in self.dual_algorithm_pairs],
            "dual_any_secondary": list(self.dual_any_secondary),
            "catalogue": ({"version": SRB_CATALOG_VERSION, "commit": SRB_CATALOG_COMMIT} if self.legacy_miner_type == "srbminer" else None),
            "periodic_cpu_threads_rule": ({
                "secondary_mode": "integrated",
                "primary_algorithms": list(self.dual_any_secondary),
                "resources": ["cpu", "cpu"],
                "option": "cpu_threads",
                "requires_matching_automatic_status": True,
                "automatic": "missing_or_null",
                "message": SRB_PERIODIC_CPU_THREADS_ERROR,
                "hint": SRB_PERIODIC_CPU_THREADS_HINT,
            } if self.legacy_miner_type == "srbminer" else {}),
            "protocol_requirements": ({"quic": {"algorithms": ["quantus"], "minimum_version": SRB_ALGORITHM_MINIMUM_VERSIONS["quantus"], "required_options": ["tls_cert_sha256"], "wallet_mode_option": "quic_wallet_mode", "default_wallet_mode": "wallet", "wallet_fields": {"wallet": "wallet_address", "token": "pool_password"}, "worker_suffix": False}} if self.legacy_miner_type == "srbminer" else {}),
        }


def _is_canonical(value: object, pattern: re.Pattern[str], *, lower: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    expected = value.strip().lower() if lower else value.strip()
    return value == expected and pattern.fullmatch(value) is not None


def _validate_release_source(source: ReleaseSource) -> None:
    if not isinstance(source, ReleaseSource):
        raise ValueError("miner adapter release source is invalid")
    github_name = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$")
    if (
        not _is_canonical(source.owner, github_name, lower=False)
        or not _is_canonical(source.repository, github_name, lower=False)
    ):
        raise ValueError("miner adapter release repository is invalid")
    trusted_resolvers = {
        "github.xmrig.v1": ("xmrig", "xmrig"),
        "github.srbminer.v1": ("doktor83", "SRBMiner-Multi"),
        "github.pearlminer.v1": ("pearlfortune", "pearl-miner"),
        "github.seine.v1": ("zpalmtree", "seine"),
    }
    if source.resolver_id not in trusted_resolvers:
        raise ValueError("miner adapter release resolver is unsupported")
    if (source.owner, source.repository) != trusted_resolvers[source.resolver_id]:
        raise ValueError("miner adapter release resolver repository disagrees")
    if not isinstance(source.tag_style, ReleaseTagStyle):
        raise ValueError("miner adapter release tag style is invalid")
    if not isinstance(source.prerelease_policy, PrereleasePolicy):
        raise ValueError("miner adapter prerelease policy is invalid")
    if not isinstance(source.version_style, ReleaseVersionStyle):
        raise ValueError("miner adapter release version style is invalid")
    if (
        not isinstance(source.fallback_versions, tuple)
        or
        not source.fallback_versions
        or len(source.fallback_versions) != len(set(source.fallback_versions))
        or any(not source.accepts_version(item) for item in source.fallback_versions)
    ):
        raise ValueError("miner adapter fallback release versions are invalid")
    if source.minimum_version is not None:
        if not source.accepts_version(source.minimum_version):
            raise ValueError("miner adapter minimum release version is invalid")
        minimum_key = source._normalized_sort_key(source.minimum_version)
        if any(
            source._normalized_sort_key(version) < minimum_key
            for version in source.fallback_versions
        ):
            raise ValueError("miner adapter fallback release predates its minimum")


def _validate_platforms_and_assets(adapter: MinerAdapter) -> None:
    if not isinstance(adapter.platforms, tuple) or not isinstance(adapter.assets, tuple):
        raise ValueError("miner adapter platform or asset collection is invalid")
    if not adapter.platforms or not adapter.assets:
        raise ValueError("miner adapter platform or asset list is empty")
    platforms: dict[str, PlatformSpec] = {}
    for platform in adapter.platforms:
        if not isinstance(platform, PlatformSpec):
            raise ValueError("miner adapter platform is invalid or duplicated")
        if platform.os_family not in _ALLOWED_OS or platform.os_family in platforms:
            raise ValueError("miner adapter platform is invalid or duplicated")
        if (
            not isinstance(platform.executable_candidates, tuple)
            or not isinstance(platform.asset_variants, tuple)
            or
            not platform.executable_candidates
            or len(platform.executable_candidates)
            != len({item.casefold() for item in platform.executable_candidates})
            or any(_SAFE_FILENAME.fullmatch(item) is None for item in platform.executable_candidates)
        ):
            raise ValueError("miner adapter executable candidates are invalid")
        if (
            len(platform.asset_variants) != len(set(platform.asset_variants))
            or any(not _is_canonical(item, _TOKEN, lower=True) for item in platform.asset_variants)
        ):
            raise ValueError("miner adapter platform variants are invalid")
        platforms[platform.os_family] = platform

    asset_keys: set[str] = set()
    asset_targets: set[tuple[str, str | None]] = set()
    variants_by_os: dict[str, set[str]] = {key: set() for key in platforms}
    has_unvariant_asset_by_os: dict[str, bool] = {key: False for key in platforms}
    for asset in adapter.assets:
        if not isinstance(asset, AssetSpec):
            raise ValueError("miner adapter asset is invalid")
        if not _is_canonical(asset.key, _ASSET_KEY, lower=True) or asset.key in asset_keys:
            raise ValueError("miner adapter asset key is invalid or duplicated")
        target = (asset.os_family, asset.variant)
        if asset.os_family not in platforms or target in asset_targets:
            raise ValueError("miner adapter asset target is invalid or duplicated")
        if asset.archive_type not in _ALLOWED_ARCHIVES:
            raise ValueError("miner adapter archive type is unsupported")
        if asset.variant is not None:
            if not _is_canonical(asset.variant, _TOKEN, lower=True):
                raise ValueError("miner adapter asset variant is invalid")
            variants_by_os[asset.os_family].add(asset.variant)
        else:
            has_unvariant_asset_by_os[asset.os_family] = True
        if not isinstance(asset.filename_template, str):
            raise ValueError("miner adapter asset filename template is invalid")
        try:
            for _literal, field_name, format_spec, conversion in Formatter().parse(
                asset.filename_template
            ):
                if field_name is not None:
                    if format_spec or conversion or field_name not in {
                        "version",
                        "version_dashed",
                    }:
                        raise ValueError
            rendered = asset.filename(adapter.release.fallback_versions[0])
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError("miner adapter asset filename template is invalid") from exc
        # Some upstreams keep a constant asset name under a versioned release
        # tag. The trusted source/tag still binds that filename to its version.
        if _SAFE_FILENAME.fullmatch(rendered) is None:
            raise ValueError("miner adapter asset filename template is invalid")
        expected_suffix = ".zip" if asset.archive_type == "zip" else ".tar.gz"
        if not rendered.endswith(expected_suffix):
            raise ValueError("miner adapter asset filename/archive mismatch")
        if (
            not isinstance(asset.fallback_versions, tuple)
            or
            len(asset.fallback_versions) != len(set(asset.fallback_versions))
            or any(
                not adapter.release.accepts_version(item)
                for item in asset.fallback_versions
            )
        ):
            raise ValueError("miner adapter asset fallback versions are invalid")
        if (
            not isinstance(asset.filename_overrides, tuple)
            or any(
                not isinstance(item, tuple) or len(item) != 2
                for item in asset.filename_overrides
            )
        ):
            raise ValueError("miner adapter asset filename override is invalid")
        override_versions = [item[0] for item in asset.filename_overrides]
        if len(override_versions) != len(set(override_versions)):
            raise ValueError("miner adapter asset filename override is duplicated")
        for version, filename in asset.filename_overrides:
            if (
                not adapter.release.accepts_version(version)
                or _SAFE_FILENAME.fullmatch(filename) is None
                or not filename.endswith(expected_suffix)
            ):
                raise ValueError("miner adapter asset filename override is invalid")
        if (
            not isinstance(asset.sha256_by_version, tuple)
            or any(
                not isinstance(item, tuple) or len(item) != 2
                for item in asset.sha256_by_version
            )
        ):
            raise ValueError("miner adapter asset SHA-256 pins are invalid")
        pinned_versions = [item[0] for item in asset.sha256_by_version]
        if len(pinned_versions) != len(set(pinned_versions)):
            raise ValueError("miner adapter asset SHA-256 pin is duplicated")
        for version, digest in asset.sha256_by_version:
            if (
                not adapter.release.accepts_version(version)
                or _SHA256.fullmatch(digest) is None
            ):
                raise ValueError("miner adapter asset SHA-256 pin is invalid")
        if asset.sha256_by_version:
            fallback_versions = (
                asset.fallback_versions or adapter.release.fallback_versions
            )
            if any(version not in pinned_versions for version in fallback_versions):
                raise ValueError(
                    "miner adapter pinned asset has an unpinned fallback release"
                )
        asset_keys.add(asset.key)
        asset_targets.add(target)

    if set(platforms) != {item.os_family for item in adapter.assets}:
        raise ValueError("miner adapter platform has no package asset")
    for os_family, platform in platforms.items():
        if variants_by_os[os_family] != set(platform.asset_variants):
            raise ValueError("miner adapter platform variant declarations disagree with assets")
        if platform.asset_variants and has_unvariant_asset_by_os[os_family]:
            raise ValueError("miner adapter platform mixes variant and unvariant assets")


def _validate_algorithms(adapter: MinerAdapter) -> None:
    if (
        not isinstance(adapter.resource_kinds, frozenset)
        or not isinstance(adapter.algorithm_specs, tuple)
        or not isinstance(adapter.coin_defaults, tuple)
        or not isinstance(adapter.coin_algorithm_constraints, tuple)
        or
        not adapter.resource_kinds
        or not adapter.resource_kinds.issubset(_ALLOWED_RESOURCES)
    ):
        raise ValueError("miner adapter resource kinds are invalid")
    if not adapter.algorithm_specs:
        raise ValueError("miner adapter algorithm list is empty")
    names: set[str] = set()
    accepted_tokens: set[str] = set()
    used_resources: set[str] = set()
    algorithm_coins: dict[str, set[str]] = {}
    algorithm_pattern = re.compile(r"^[a-z0-9](?:[a-z0-9_.+/-]*[a-z0-9])?$")
    for algorithm in adapter.algorithm_specs:
        if not isinstance(algorithm, AlgorithmSpec):
            raise ValueError("miner adapter algorithm is invalid")
        if not isinstance(algorithm.aliases, tuple) or not isinstance(algorithm.coins, tuple):
            raise ValueError("miner adapter algorithm collections are invalid")
        if not _is_canonical(algorithm.name, algorithm_pattern, lower=True):
            raise ValueError("miner adapter algorithm name is invalid")
        if algorithm.name in names or algorithm.resource_kind not in adapter.resource_kinds:
            raise ValueError("miner adapter algorithm is duplicated or has an invalid resource")
        tokens = (algorithm.name, *algorithm.aliases)
        if (
            len(tokens) != len(set(tokens))
            or any(not _is_canonical(item, algorithm_pattern, lower=True) for item in tokens)
            or any(item in accepted_tokens for item in tokens)
        ):
            raise ValueError("miner adapter algorithm aliases overlap")
        if (
            len(algorithm.coins) != len(set(algorithm.coins))
            or any(not _is_canonical(item, _COIN, lower=False) for item in algorithm.coins)
        ):
            raise ValueError("miner adapter algorithm coins are invalid")
        names.add(algorithm.name)
        accepted_tokens.update(tokens)
        if (not isinstance(algorithm.supported_resources, tuple) or not set(algorithm.resources).issubset(adapter.resource_kinds)
            or algorithm.resource_kind not in algorithm.resources):
            raise ValueError("miner adapter algorithm supported resources are invalid")
        if not isinstance(algorithm.gpu_vendors, tuple) or not set(algorithm.gpu_vendors).issubset({"amd", "nvidia", "intel"}):
            raise ValueError("miner adapter algorithm GPU vendors are invalid")
        for version in (algorithm.minimum_version, algorithm.removed_version):
            if version is not None and not _THREE_PART_NUMERIC_VERSION.fullmatch(version):
                raise ValueError("miner adapter algorithm version bound is invalid")
        if not isinstance(algorithm.gpu_minimum_versions, tuple):
            raise ValueError("miner adapter GPU version floors are invalid")
        gpu_floor_vendors: set[str] = set()
        for entry in algorithm.gpu_minimum_versions:
            if (not isinstance(entry, tuple) or len(entry) != 2
                or entry[0] not in algorithm.gpu_vendors or entry[0] in gpu_floor_vendors
                or not isinstance(entry[1], str) or not _THREE_PART_NUMERIC_VERSION.fullmatch(entry[1])):
                raise ValueError("miner adapter GPU version floor is invalid")
            gpu_floor_vendors.add(entry[0])
        used_resources.update(algorithm.resources)
        algorithm_coins[algorithm.name] = set(algorithm.coins)
    if adapter.default_algorithm not in names:
        raise ValueError("miner adapter default algorithm is undeclared")
    if used_resources != set(adapter.resource_kinds):
        raise ValueError("miner adapter resource kind has no algorithm")
    defaults: dict[str, str] = {}
    for item in adapter.coin_defaults:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("miner adapter coin default is invalid")
        coin, algorithm = item
        if (
            not _is_canonical(coin, _COIN, lower=False)
            or coin in defaults
            or algorithm not in names
            or (algorithm_coins[algorithm] and coin not in algorithm_coins[algorithm])
        ):
            raise ValueError("miner adapter coin default is invalid")
        defaults[coin] = algorithm
    constrained_coins: set[str] = set()
    for entry in adapter.coin_algorithm_constraints:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError("miner adapter coin algorithm constraint is invalid")
        coin, allowed = entry
        if (not _is_canonical(coin, _COIN, lower=False) or coin in constrained_coins
            or not isinstance(allowed, tuple) or not allowed
            or any(not isinstance(name, str) or name not in names for name in allowed)
            or len(allowed) != len(set(allowed))):
            raise ValueError("miner adapter coin algorithm constraint is invalid")
        constrained_coins.add(coin)
    declared_coins = set().union(*algorithm_coins.values())
    if declared_coins - set(defaults):
        raise ValueError("miner adapter algorithm coin has no default")


def _validate_options(adapter: MinerAdapter) -> None:
    if not isinstance(adapter.options, tuple):
        raise ValueError("miner adapter option collection is invalid")
    if len(adapter.option_keys) != len(adapter.options):
        raise ValueError("miner adapter option name is duplicated")
    for option in adapter.options:
        if not isinstance(option, OptionSpec):
            raise ValueError("miner adapter option is invalid")
        if not _is_canonical(option.name, _OPTION_NAME, lower=True):
            raise ValueError("miner adapter option name is invalid")
        if (
            not isinstance(option.label, str)
            or not isinstance(option.description, str)
            or not isinstance(option.nullable, bool)
            or not isinstance(option.choices, tuple)
            or not option.label.strip()
            or option.label != option.label.strip()
            or not option.description.strip()
            or option.description != option.description.strip()
            or any(ord(character) < 32 for character in option.label + option.description)
        ):
            raise ValueError("miner adapter option text is invalid")
        if option.scope != "job":
            raise ValueError("miner adapter option scope is unsupported")
        if option.value_type == "boolean":
            if option.minimum is not None or option.maximum is not None or option.choices:
                raise ValueError("miner adapter boolean option constraints are invalid")
        elif option.value_type == "integer":
            if (
                option.minimum is None
                or isinstance(option.minimum, bool)
                or not isinstance(option.minimum, int)
                or (option.maximum is not None and (
                    isinstance(option.maximum, bool)
                    or not isinstance(option.maximum, int)
                    or option.minimum > option.maximum
                ))
                or option.choices
            ):
                raise ValueError("miner adapter integer option constraints are invalid")
        elif option.value_type == "choice":
            if (
                option.minimum is not None
                or option.maximum is not None
                or not option.choices
                or len(option.choices) != len(set(option.choices))
                or any(isinstance(item, bool) or not isinstance(item, (str, int)) for item in option.choices)
                or len({type(item) for item in option.choices}) != 1
                or any(
                    isinstance(item, str)
                    and not _is_canonical(item, _TOKEN, lower=True)
                    for item in option.choices
                )
            ):
                raise ValueError("miner adapter choice option constraints are invalid")
        elif option.value_type == "string":
            if (isinstance(option.min_length, bool) or not isinstance(option.min_length, int) or option.min_length < 0
                or isinstance(option.max_length, bool) or not isinstance(option.max_length, int)
                or not option.min_length <= option.max_length <= 4096 or option.choices):
                raise ValueError("miner adapter string option bounds are invalid")
        else:
            raise ValueError("miner adapter option type is unsupported")
        if option.default is None and not option.nullable:
            raise ValueError("miner adapter option requires a default or nullable=true")
        if option.default is not None:
            try:
                validate_option_value(option, option.default)
            except ValueError as exc:
                if option.value_type == "string":
                    raise
                raise ValueError(
                    f"miner adapter {option.value_type} option default is invalid"
                ) from exc


class MinerAdapterRegistry:
    """Closed registry of audited adapters bundled with this release."""

    def __init__(self, adapters: Iterable[MinerAdapter]) -> None:
        by_type: dict[str, MinerAdapter] = {}
        by_id: dict[str, MinerAdapter] = {}
        package_namespaces: set[str] = set()
        for adapter in adapters:
            if not isinstance(adapter, MinerAdapter):
                raise ValueError("miner adapter declaration is invalid")
            if adapter.abi_version != MINER_ADAPTER_ABI:
                raise ValueError("miner adapter ABI is unsupported")
            miner_type = adapter.legacy_miner_type
            adapter_id = adapter.adapter_id
            if is_retired_miner(miner_type) or is_retired_miner(adapter_id):
                raise ValueError("retired managed miner adapter cannot be registered")
            if (
                not _is_canonical(miner_type, _MINER_TYPE, lower=True)
                or not _is_canonical(adapter_id, _ADAPTER_ID, lower=True)
                or not adapter.display_name.strip()
                or adapter.display_name != adapter.display_name.strip()
                or len(adapter.display_name) > 80
                or any(ord(character) < 32 for character in adapter.display_name)
            ):
                raise ValueError("miner adapter identity is not canonical")
            if (
                isinstance(adapter.adapter_revision, bool)
                or not isinstance(adapter.adapter_revision, int)
                or adapter.adapter_revision < 1
            ):
                raise ValueError("miner adapter revision is invalid")
            if _IMPLEMENTATION_ID.fullmatch(adapter.runtime_id) is None:
                raise ValueError("miner adapter runtime identity is invalid")
            if _IMPLEMENTATION_ID.fullmatch(adapter.telemetry_id) is None:
                raise ValueError("miner adapter telemetry identity is invalid")
            if miner_type in by_type or adapter_id in by_id:
                raise ValueError("miner adapter identity is duplicated")
            _validate_release_source(adapter.release)
            _validate_platforms_and_assets(adapter)
            _validate_algorithms(adapter)
            _validate_options(adapter)
            if (
                not isinstance(adapter.pool_schemes, frozenset)
                or not adapter.pool_schemes
                or not adapter.pool_schemes.issubset(_ALLOWED_POOL_SCHEMES)
                or any(value != value.strip().lower() for value in adapter.pool_schemes)
            ):
                raise ValueError("miner adapter pool schemes are invalid")
            if (
                not isinstance(adapter.capabilities, frozenset)
                or not adapter.capabilities
                or not adapter.capabilities.issubset(_ALLOWED_CAPABILITIES)
                or not adapter.resource_kinds.issubset(adapter.capabilities)
            ):
                raise ValueError("miner adapter capabilities are invalid")
            expected_namespaces = (
                f"builtin-{miner_type}-",
                f"test-{miner_type}-",
            )
            if not isinstance(adapter.package_namespaces, tuple):
                raise ValueError("miner adapter package namespace collection is invalid")
            if adapter.package_namespaces != expected_namespaces:
                if not adapter.package_namespaces:
                    raise ValueError("miner adapter package namespace is empty")
                raise ValueError(
                    "miner adapter package namespace is invalid or non-canonical"
                )
            for namespace in adapter.package_namespaces:
                if (
                    _PACKAGE_NAMESPACE.fullmatch(namespace) is None
                    or any(
                        namespace.startswith(existing)
                        or existing.startswith(namespace)
                        for existing in package_namespaces
                    )
                ):
                    raise ValueError("miner adapter package namespace overlaps another adapter")
                package_namespaces.add(namespace)
            by_type[miner_type] = adapter
            by_id[adapter_id] = adapter
        all_names = set(by_type) | set(by_id)
        if len(all_names) != len(by_type) + len(by_id):
            raise ValueError("miner adapter type conflicts with an adapter ID")
        # Preserve the audited declaration order.  It is part of the existing
        # controller catalog/UI contract (XMRig, SRBMiner, Pearl Miner).
        self._adapters = tuple(by_type.values())

    @property
    def miner_types(self) -> frozenset[str]:
        return frozenset(item.legacy_miner_type for item in self._adapters)

    def list(self) -> tuple[MinerAdapter, ...]:
        return self._adapters

    def get(self, value: object) -> MinerAdapter | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        return next(
            (
                item
                for item in self._adapters
                if normalized in {item.legacy_miner_type, item.adapter_id.lower()}
            ),
            None,
        )

    def require(self, value: object) -> MinerAdapter:
        adapter = self.get(value)
        if adapter is None:
            retired = retired_miner_type(value)
            if retired is not None:
                raise ValueError(f"retired managed miner adapter {retired}; reconfigure the flight sheet")
            raise ValueError("unsupported managed miner adapter")
        return adapter

    def public_catalog(self) -> list[dict[str, object]]:
        return [adapter.public_payload() for adapter in self.list()]
