from __future__ import annotations

from dataclasses import replace
import json

import pytest

from server.miner_adapters import BUILTIN_ADAPTERS, MINER_ADAPTERS, MINER_ADAPTER_ABI
from server.miner_adapters.contract import (
    MinerAdapter,
    MinerAdapterRegistry,
    PrereleasePolicy,
    ReleaseTagStyle,
)


def test_builtin_adapter_registry_covers_remaining_native_miners():
    assert MINER_ADAPTERS.list() == BUILTIN_ADAPTERS
    assert MINER_ADAPTERS.miner_types == {
        item.legacy_miner_type for item in BUILTIN_ADAPTERS
    }
    assert all(item.abi_version == MINER_ADAPTER_ABI for item in MINER_ADAPTERS.list())


@pytest.mark.parametrize(
    ("miner_type", "adapter_id", "runtime_id", "telemetry_id"),
    [
        ("xmrig", "org.xmrig.xmrig", "builtin.xmrig.v1", "builtin.xmrig.http.v1"),
        ("srbminer", "org.doktor83.srbminer-multi", "builtin.srbminer.v1", "builtin.srbminer.http.v1"),
        ("pearlminer", "org.pearlfortune.pearl-miner", "builtin.pearlminer.v1", "builtin.pearlminer.log.v1"),
        ("seine", "org.zpalmtree.seine", "builtin.seine.v1", "builtin.seine.http.v1"),
    ],
)
def test_builtin_adapter_identity_and_runtime_are_stable(miner_type, adapter_id, runtime_id, telemetry_id):
    adapter = MINER_ADAPTERS.require(miner_type)
    assert MINER_ADAPTERS.require(adapter_id) is adapter
    assert (adapter.adapter_id, adapter.runtime_id, adapter.telemetry_id) == (adapter_id, runtime_id, telemetry_id)


def test_adapter_contract_preserves_package_and_executable_namespaces():
    xmrig = MINER_ADAPTERS.require("xmrig")
    srb = MINER_ADAPTERS.require("srbminer")
    pearl = MINER_ADAPTERS.require("pearlminer")
    assert xmrig.matches_package_id("builtin-xmrig-6.26.0-linux-x64")
    assert srb.matches_package_id("test-srbminer-3.4.6-windows-x64")
    assert pearl.matches_package_id("builtin-pearlminer-2.0.3-linux-x64-nvidia")
    assert not pearl.matches_package_id("builtin-xmrig-6.26.0-linux-x64")
    assert xmrig.executable_candidates("Windows") == ("xmrig.exe",)
    assert srb.executable_candidates("Linux") == ("SRBMiner-MULTI",)
    assert pearl.executable_candidates("Ubuntu") == ("miner", "miner-cuda12", "miner-cuda13")
    assert xmrig.package_id("6.26.0", "windows") == "builtin-xmrig-6.26.0-windows-x64"
    assert srb.package_id("3.4.6", "linux", test=True) == "test-srbminer-3.4.6-linux-x64"
    assert pearl.package_id("2.0.3", "linux", variant="nvidia") == "builtin-pearlminer-2.0.3-linux-x64-nvidia"


def test_adapter_assets_preserve_current_release_filename_rules():
    xmrig = MINER_ADAPTERS.require("xmrig")
    srb = MINER_ADAPTERS.require("srbminer")
    pearl = MINER_ADAPTERS.require("pearlminer")
    assert xmrig.asset("windows").filename("6.26.0") == "xmrig-6.26.0-windows-x64.zip"
    assert srb.asset("linux").filename("3.4.6") == "SRBMiner-Multi-3-4-6-Linux.tar.gz"
    assert pearl.asset("linux", "amd").filename("1.2.2") == "pearlfortune-amd-v1.2.2.fix.tar.gz"


def test_seine_constant_asset_names_keep_release_tag_and_version_specific_sha_pins():
    seine = MINER_ADAPTERS.require("seine")
    release = seine.release
    assert (release.owner, release.repository, release.resolver_id) == (
        "zpalmtree", "seine", "github.seine.v1",
    )
    assert release.fallback_versions == ("0.2.45", "0.2.15")
    assert release.minimum_version == "0.2.15"
    assert release.tag_for_version("0.2.15") == "v0.2.15"
    assert release.parse_tag("v0.2.15") == ("v0.2.15", "0.2.15")
    assert release.parse_tag("0.2.15") is None
    assert release.parse_tag("v0.2.14") is None
    assert release.prerelease_policy is PrereleasePolicy.STABLE_ONLY
    for system, filename, digests in (
        ("windows", "seine-windows-x86_64.zip", {
            "0.2.45": "1708e8f0d02426db508a1f75fed73a6ee314c0cc7ec9005143049d78c2ddf3d3",
            "0.2.15": "f779f013ea91f329fc481a7c302e727e2c839ac004dbfd8e4b216f59fd425cfb"}),
        ("linux", "seine-linux-x86_64.tar.gz", {
            "0.2.45": "19e3c979d14f38664bdfb28c2b48e4b0580a1cb711bae2ee68bcbc9ea41bd173",
            "0.2.15": "bcbf7b71a03dbac59953e1615f5bc15fbc6079e8f6252f0bae8ad435f62d06c1"}),
    ):
        asset = seine.asset(system)
        assert asset.filename("0.2.15") == asset.filename("0.2.45") == filename
        assert {version: asset.expected_sha256(version) for version in digests} == digests
        assert asset.expected_sha256("0.2.16") is None
    assert release.tag_for_version("0.2.45") == "v0.2.45"
    assert seine.executable_candidates("Windows") == ("seine.exe",)
    assert seine.executable_candidates("Linux") == ("seine",)
    assert seine.resource_kinds == {"cpu"}
    assert seine.normalize_algorithm(None, coin="BNT") == "blocknet-argon2id"
    assert seine.algorithm_resource("blocknet-argon2id", coin="BNT") == "cpu"
    threads = next(option for option in seine.options if option.name == "cpu_threads")
    assert threads.minimum == 1 and threads.maximum is None and threads.nullable


def test_adapter_algorithm_aliases_defaults_and_resource_ownership():
    xmrig = MINER_ADAPTERS.require("xmrig")
    srb = MINER_ADAPTERS.require("srbminer")
    pearl = MINER_ADAPTERS.require("pearlminer")
    assert xmrig.normalize_algorithm("rx/0", coin="XMR") == "randomx"
    assert srb.normalize_algorithm(None, coin="ETC") == "etchash"
    assert srb.algorithm_resource("randomx", coin="XMR") == "cpu"
    assert srb.algorithm_resource("pearlhash", coin="PEARL") == "gpu"
    assert pearl.normalize_algorithm(None, coin="PRL") == "pearlhash"
    with pytest.raises(ValueError, match="algorithm is unsupported"):
        pearl.normalize_algorithm("randomx", coin="PEARL")


def test_public_adapter_catalog_is_json_safe_and_contains_common_extra_args():
    catalog = MINER_ADAPTERS.public_catalog()
    assert [item["miner_type"] for item in catalog] == [
        item.legacy_miner_type for item in BUILTIN_ADAPTERS
    ]
    pearl = next(item for item in catalog if item["miner_type"] == "pearlminer")
    assert pearl["abi_version"] == "miner-adapter.v1"
    assert pearl["pool_schemes"] == ["pearl", "stratum+ssl", "stratum+tcp"]
    assert [item["name"] for item in pearl["options"]] == ["extra_args", "gpu_backend", "cuda_variant"]
    assert pearl["package_namespaces"] == ["builtin-pearlminer-", "test-pearlminer-"]
    assert pearl["assets"][1]["key"] == "linux-nvidia"
    for adapter in catalog:
        option = next(o for o in adapter["options"] if o["name"] == "extra_args")
        assert (option["type"], option["multiline"], option["default"], option["max_length"]) == ("string", True, "", 4096)
    json.dumps(catalog)


def test_adapter_option_defaults_match_the_existing_flight_sheet_runtime():
    for miner_type in ("xmrig", "srbminer"):
        options = {item.name: item for item in MINER_ADAPTERS.require(miner_type).options}
        assert options["huge_pages"].default is True
        assert options["keepalive"].default is True
        assert options["extra_args"].default == ""
    pearl = {item.name: item for item in MINER_ADAPTERS.require("pearlminer").options}
    assert pearl["gpu_backend"].default == "auto"
    assert pearl["cuda_variant"].default == "auto"
    assert pearl["cuda_variant"].choices == ("auto", "cuda12", "cuda13")


def test_release_tag_and_prerelease_rules_are_declared_by_each_adapter():
    xmrig = MINER_ADAPTERS.require("xmrig").release
    srb = MINER_ADAPTERS.require("srbminer").release
    pearl = MINER_ADAPTERS.require("pearlminer").release
    assert xmrig.tag_style is ReleaseTagStyle.V_PREFIX
    assert xmrig.prerelease_policy is PrereleasePolicy.STABLE_ONLY
    assert xmrig.tag_for_version("6.26.0") == "v6.26.0"
    assert xmrig.parse_tag("6.26.0") is None
    assert srb.tag_style is ReleaseTagStyle.PLAIN
    assert srb.tag_for_version("3.4.6") == "3.4.6"
    assert srb.parse_tag("v3.4.6") is None
    assert pearl.allows_prereleases is True
    assert pearl.parse_tag("v2.0.4") == ("v2.0.4", "2.0.4")


def test_unknown_adapter_is_rejected_and_description_words_are_not_runtime_hooks():
    assert MINER_ADAPTERS.get("unknown") is None
    with pytest.raises(ValueError, match="unsupported managed miner adapter"):
        MINER_ADAPTERS.require("unknown")
    source = MINER_ADAPTERS.require("xmrig")
    described = replace(source, display_name="Python shell PowerShell description")
    registry = MinerAdapterRegistry((described,))
    payload = registry.require("xmrig").public_payload()
    assert payload["display_name"] == described.display_name
    assert payload.keys() == source.public_payload().keys()
    assert payload["runtime_id"] == source.runtime_id
    assert payload["telemetry_id"] == source.telemetry_id
    # The separate untrusted-runtime/telemetry tests enforce the actual IDs.
    json.dumps(payload)


def test_registry_is_deeply_immutable_at_its_public_boundaries():
    adapters = MINER_ADAPTERS.list()
    assert isinstance(adapters, tuple)
    with pytest.raises(AttributeError):
        adapters[0].display_name = "changed"  # type: ignore[misc]
    executable_map = adapters[0].executables_by_system
    executable_map["windows"] = ("untrusted.exe",)
    assert adapters[0].executable_candidates("Windows") != ("untrusted.exe",)


def test_registry_rejects_empty_or_overlapping_package_namespaces():
    source = MINER_ADAPTERS.require("xmrig")

    def changed(**values):
        payload = {
            field: getattr(source, field)
            for field in source.__dataclass_fields__
        }
        payload.update(values)
        return MinerAdapter(**payload)

    with pytest.raises(ValueError, match="package namespace is empty"):
        MinerAdapterRegistry((changed(package_namespaces=()),))
    with pytest.raises(ValueError, match="package namespace is invalid"):
        MinerAdapterRegistry((changed(package_namespaces=("",)),))
    with pytest.raises(ValueError, match="package namespace is invalid"):
        MinerAdapterRegistry(
            (
                source,
                changed(
                    adapter_id="org.example.other",
                    legacy_miner_type="other",
                ),
            )
        )


def test_registry_rejects_untrusted_runtime_and_telemetry_identifiers():
    source = MINER_ADAPTERS.require("xmrig")
    payload = {
        field: getattr(source, field)
        for field in source.__dataclass_fields__
    }
    payload["runtime_id"] = "python.import.untrusted"
    with pytest.raises(ValueError, match="runtime identity is invalid"):
        MinerAdapterRegistry((MinerAdapter(**payload),))
    payload["runtime_id"] = source.runtime_id
    payload["telemetry_id"] = "shell.callback.untrusted"
    with pytest.raises(ValueError, match="telemetry identity is invalid"):
        MinerAdapterRegistry((MinerAdapter(**payload),))


def test_registry_rejects_noncanonical_identity_and_prefix_overlap():
    source = MINER_ADAPTERS.require("xmrig")
    with pytest.raises(ValueError, match="identity is not canonical"):
        MinerAdapterRegistry((replace(source, adapter_id="Org.Example.Miner"),))

    first = replace(
        source,
        adapter_id="org.example.foo",
        legacy_miner_type="foo",
        package_namespaces=("builtin-foo-", "test-foo-"),
    )
    second = replace(
        source,
        adapter_id="org.example.foo-bar",
        legacy_miner_type="foo-bar",
        package_namespaces=("builtin-foo-bar-", "test-foo-bar-"),
    )
    with pytest.raises(ValueError, match="namespace overlaps"):
        MinerAdapterRegistry((first, second))


def test_registry_rejects_invalid_release_platform_and_asset_contracts():
    source = MINER_ADAPTERS.require("xmrig")
    with pytest.raises(ValueError, match="release resolver is unsupported"):
        MinerAdapterRegistry(
            (replace(source, release=replace(source.release, resolver_id="github.any.v1")),)
        )
    with pytest.raises(ValueError, match="resolver repository disagrees"):
        MinerAdapterRegistry(
            (
                replace(
                    source,
                    release=replace(source.release, owner="not-xmrig"),
                ),
            )
        )
    with pytest.raises(ValueError, match="platform is invalid or duplicated"):
        MinerAdapterRegistry(
            (replace(source, platforms=(source.platforms[0], source.platforms[0])),)
        )
    unsafe_asset = replace(
        source.assets[0], filename_template="../xmrig-{version}.zip"
    )
    with pytest.raises(ValueError, match="filename template is invalid"):
        MinerAdapterRegistry(
            (replace(source, assets=(unsafe_asset, source.assets[1])),)
        )
    wrong_archive = replace(source.assets[0], archive_type="tar.gz")
    with pytest.raises(ValueError, match="filename/archive mismatch"):
        MinerAdapterRegistry(
            (replace(source, assets=(wrong_archive, source.assets[1])),)
        )

    pearl = MINER_ADAPTERS.require("pearlminer")
    mixed_linux_asset = replace(
        pearl.assets[1], key="linux-default", variant=None
    )
    with pytest.raises(ValueError, match="mixes variant and unvariant assets"):
        MinerAdapterRegistry(
            (replace(pearl, assets=(*pearl.assets, mixed_linux_asset)),)
        )


def test_registry_rejects_invalid_algorithm_coin_resource_and_option_contracts():
    source = MINER_ADAPTERS.require("xmrig")
    overlapping_algorithm = replace(
        source.algorithm_specs[0], aliases=("randomx",)
    )
    with pytest.raises(ValueError, match="algorithm aliases overlap"):
        MinerAdapterRegistry(
            (replace(source, algorithm_specs=(overlapping_algorithm,)),)
        )
    with pytest.raises(ValueError, match="coin default is invalid"):
        MinerAdapterRegistry(
            (replace(source, coin_defaults=(("XMR", "unknown"),)),)
        )
    with pytest.raises(ValueError, match="algorithm coin has no default"):
        MinerAdapterRegistry((replace(source, coin_defaults=()),))
    with pytest.raises(ValueError, match="invalid resource"):
        MinerAdapterRegistry((replace(source, resource_kinds=frozenset({"gpu"})),))

    huge_pages = next(item for item in source.options if item.name == "huge_pages")
    invalid_default = replace(huge_pages, default="yes")
    options = tuple(
        invalid_default if item.name == "huge_pages" else item
        for item in source.options
    )
    with pytest.raises(ValueError, match="boolean option default is invalid"):
        MinerAdapterRegistry((replace(source, options=options),))

    pearl = MINER_ADAPTERS.require("pearlminer")
    cuda = next(item for item in pearl.options if item.name == "cuda_variant")
    invalid_choice = replace(cuda, default="cuda99")
    pearl_options = tuple(
        invalid_choice if item.name == "cuda_variant" else item
        for item in pearl.options
    )
    with pytest.raises(ValueError, match="choice option default is invalid"):
        MinerAdapterRegistry((replace(pearl, options=pearl_options),))

    noncanonical_choice = replace(cuda, choices=("auto", "CUDA13"))
    pearl_options = tuple(
        noncanonical_choice if item.name == "cuda_variant" else item
        for item in pearl.options
    )
    with pytest.raises(ValueError, match="choice option constraints are invalid"):
        MinerAdapterRegistry((replace(pearl, options=pearl_options),))


def test_registry_requires_deeply_immutable_manifest_collections():
    source = MINER_ADAPTERS.require("xmrig")
    with pytest.raises(ValueError, match="revision is invalid"):
        MinerAdapterRegistry((replace(source, adapter_revision="1"),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="platform or asset collection is invalid"):
        MinerAdapterRegistry((replace(source, assets=list(source.assets)),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="option collection is invalid"):
        MinerAdapterRegistry((replace(source, options=list(source.options)),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="package namespace collection is invalid"):
        MinerAdapterRegistry(
            (replace(source, package_namespaces=list(source.package_namespaces)),)  # type: ignore[arg-type]
        )


@pytest.mark.parametrize('identity,expected', [
    ('lolminer', 'lolminer'), (' ForgeMiner ', 'forgeminer'),
    ('org.lolliedieb.lolminer', 'lolminer'), ('org.hashraptor.forgeminer', 'forgeminer'),
    ('builtin.lolminer.v1', 'lolminer'), ('managed_mining.forgeminer.v1', 'forgeminer'),
    ('builtin-lolminer-1.98a-linux-x64', 'lolminer'), ('test-forgeminer-1.5.16-windows-x64', 'forgeminer'),
    ('lolminer-etc', 'lolminer'), ('forgeminer-pearl', 'forgeminer'),
    ('quantusminer', 'quantusminer'),
])
def test_retired_history_identity_has_no_executable_adapter(identity, expected):
    from server.miner_adapters import is_retired_miner, retired_miner_type
    assert is_retired_miner(identity)
    assert retired_miner_type(identity) == expected
    assert MINER_ADAPTERS.get(identity) is None
    with pytest.raises(ValueError, match='retired managed miner adapter'):
        MINER_ADAPTERS.require(identity)


@pytest.mark.parametrize('identity', ['srbminer', 'xmrig', 'pearlminer', 'quantus', 'lolminerplus', 'some lolminer text', None])
def test_retired_history_helper_keeps_unrelated_identities_and_descriptions_distinct(identity):
    from server.miner_adapters import is_retired_miner
    assert not is_retired_miner(identity)


@pytest.mark.parametrize('retired_type', ['lolminer', 'forgeminer'])
def test_retired_adapter_cannot_be_reregistered_under_a_current_resolver(retired_type):
    source = MINER_ADAPTERS.require('xmrig')
    with pytest.raises(ValueError, match='retired'):
        MinerAdapterRegistry((replace(source, legacy_miner_type=retired_type,
                                     package_namespaces=(f'builtin-{retired_type}-', f'test-{retired_type}-')),))


@pytest.mark.parametrize('resolver,owner,repository', [
    ('github.lolminer.v1', 'Lolliedieb', 'lolMiner-releases'),
    ('github.forgeminer.v1', '0xHashRaptor', 'ForgeMiner'),
])
def test_retired_release_resolvers_are_not_trusted_for_new_adapters(resolver, owner, repository):
    source = MINER_ADAPTERS.require('xmrig')
    with pytest.raises(ValueError, match='release resolver is unsupported'):
        MinerAdapterRegistry((replace(source, release=replace(source.release,
            resolver_id=resolver, owner=owner, repository=repository)),))
