"""SRBMiner catalogue facts pinned to upstream release documentation.

CPU/GPU capability does not mean one job runs on both. Old entries stay
addressable for stored flights; known upstream removals are version-scoped.
"""
from collections.abc import Mapping

SRB_CATALOG_VERSION = "3.7.2"
SRB_CATALOG_COMMIT = "2f84e09039102ac4fc2b53e392c3901833b3d792"
SRB_CATALOG_SOURCE = "https://github.com/doktor83/SRBMiner-Multi/blob/" + SRB_CATALOG_COMMIT + "/README.md"
# Release notes, rather than the tag's older Parameters file, introduce K1.
# These are documented hardware facts; no inventory name implies support.
SRB_PEARL_K1_MINIMUM_VERSION = "3.7.2"
SRB_PEARL_K1_SOURCE = "https://github.com/doktor83/SRBMiner-Multi/releases/tag/3.7.2"
SRB_PEARL_K1_GPU_ARCHITECTURES = ("SM86", "SM89", "SM120")
# Only release-note-proven introduction floors; unknown floors stay unknown.
SRB_ALGORITHM_MINIMUM_VERSIONS = {
    "argon2id_exfer": "3.2.9",
    "blake3_an": "3.5.7",
    "noid": "3.6.3",
    "oggpow": "3.3.6",
    "qhash": "3.1.6",
    "quantus": "3.6.4",
    "randomc64": "3.1.5",
    "randomdrgx": "3.5.7",
    "randomjuno": "3.0.8",
    "randompcn": "3.5.6",
    "randomsnap": "3.1.2",
    "randomzqvx": "3.5.6",
    "sha256d_csd": "3.5.8",
    "sha3t": "3.5.5",
    "xelishashv3": "3.0.6",
    "yespowermwc": "3.1.1",
}

# Vendor-specific floors are checked only after actual GPU discovery.
# Quantus Polaris/RX4xx/RX5xx has the additional 3.6.8 requirement below.
SRB_GPU_MINIMUM_VERSIONS = {
    "quantus": (("amd", "3.6.4"), ("nvidia", "3.6.4"), ("intel", "3.7.0")),
    "xelishashv3": (("amd", "3.1.4"), ("nvidia", "3.1.2"), ("intel", "3.1.4")),
}

SRB_ALGORITHM_ROWS = (('argon2d_16000', 'cpu', ('cpu', 'gpu'), ('amd',)),
 ('argon2d_dynamic', 'cpu', ('cpu', 'gpu'), ('amd',)),
 ('argon2id_exfer', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia')),
 ('autolykos2', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('blake3_an', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('blake3_decred', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('cpupower', 'cpu', ('cpu',), ()),
 ('cryptonight_gpu', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia', 'intel')),
 ('cryptonight_turtle', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia')),
 ('curvehash', 'cpu', ('cpu', 'gpu'), ('amd',)),
 ('etchash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('ethash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('evrprogpow', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('firopow', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('fishhash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('flex', 'cpu', ('cpu',), ()),
 ('ghostrider', 'cpu', ('cpu',), ()),
 ('heavyhash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('kawpow', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('lyra2v2_webchain', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia')),
 ('meowpow', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('mike', 'cpu', ('cpu',), ()),
 ('minotaurx', 'cpu', ('cpu',), ()),
 ('noid', 'gpu', ('gpu',), ('amd', 'nvidia')),
 ('oggpow', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('panthera', 'cpu', ('cpu',), ()),
 ('pearlhash', 'gpu', ('gpu',), ('amd', 'nvidia')),
 ('phihash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('progpow_epic', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('progpow_zano', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('qhash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('quantus', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('randomalpha', 'cpu', ('cpu',), ()),
 ('randomarq', 'cpu', ('cpu',), ()),
 ('randomc64', 'cpu', ('cpu',), ()),
 ('randomdrgx', 'cpu', ('cpu',), ()),
 ('randomepic', 'cpu', ('cpu',), ()),
 ('randomjuno', 'cpu', ('cpu',), ()),
 ('randompcn', 'cpu', ('cpu',), ()),
 ('randomscash', 'cpu', ('cpu',), ()),
 ('randomsnap', 'cpu', ('cpu',), ()),
 ('randomvirel', 'cpu', ('cpu',), ()),
 ('randomx', 'cpu', ('cpu',), ()),
 ('randomy', 'cpu', ('cpu',), ()),
 ('randomyada', 'cpu', ('cpu',), ()),
 ('randomzqvx', 'cpu', ('cpu',), ()),
 ('rinhash', 'cpu', ('cpu',), ()),
 ('sha256d_csd', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('sha3t', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('verthash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('verushash', 'cpu', ('cpu',), ()),
 ('xelishashv3', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia', 'intel')),
 ('xhash', 'gpu', ('gpu',), ('amd', 'nvidia', 'intel')),
 ('yescrypt', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia')),
 ('yescryptr16', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia')),
 ('yescryptr32', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia')),
 ('yescryptr8', 'cpu', ('cpu', 'gpu'), ('amd', 'nvidia')),
 ('yespower', 'cpu', ('cpu', 'gpu'), ('nvidia',)),
 ('yespower2b', 'cpu', ('cpu',), ()),
 ('yespoweradvc', 'cpu', ('cpu',), ()),
 ('yespowereqpay', 'cpu', ('cpu',), ()),
 ('yespowerinterchained', 'cpu', ('cpu',), ()),
 ('yespowerltncg', 'cpu', ('cpu',), ()),
 ('yespowermgpc', 'cpu', ('cpu',), ()),
 ('yespowermwc', 'cpu', ('cpu',), ()),
 ('yespowerr16', 'cpu', ('cpu',), ()),
 ('yespowersugar', 'cpu', ('cpu',), ()),
 ('yespowertide', 'cpu', ('cpu', 'gpu'), ('nvidia',)),
 ('yespowerurx', 'cpu', ('cpu',), ()))
SRB_LEGACY_REMOVED = ("walahash", "karlsenhashv2")
SRB_LEGACY_REMOVAL_VERSION = "3.5.8"
SRB_LEGACY_REMOVAL_SOURCE = "https://github.com/doktor83/SRBMiner-Multi/releases/tag/3.5.8"
SRB_DUAL_ALGORITHM_PAIRS = frozenset({
    ("fishhash", "qhash"), ("fishhash", "walahash"), ("fishhash", "blake3_decred"),
    ("autolykos2", "qhash"), ("autolykos2", "walahash"), ("autolykos2", "blake3_decred"),
    ("autolykos2", "heavyhash"), ("etchash", "blake3_decred"), ("etchash", "heavyhash"),
    ("ethash", "blake3_decred"), ("ethash", "heavyhash"),
    ("karlsenhashv2", "blake3_decred"), ("xhash", "qhash"), ("xhash", "blake3_decred"),
})
SRB_DUAL_ANY_SECONDARY = frozenset({"randomepic", "progpow_epic"})

def srb_dual_pair_supported(primary: str, secondary: str, primary_resource: str, secondary_resource: str) -> bool:
    if primary == secondary == "randomx": return False
    if primary in SRB_DUAL_ANY_SECONDARY: return True
    if {primary_resource, secondary_resource} == {"cpu", "gpu"}: return True
    return (primary, secondary) in SRB_DUAL_ALGORITHM_PAIRS


SRB_PERIODIC_CPU_THREADS_ERROR = (
    "CPU periodic dual threads must be automatic on both algorithms or explicitly set for both; "
    "the current launcher cannot accurately represent mixed automatic and fixed thread slots."
)
SRB_PERIODIC_CPU_THREADS_HINT = (
    "当前启动器无法准确表达周期 CPU 双任务混合自动/固定线程数；"
    "请两腿线程数全部留空，或分别明确设置。"
)


def validate_periodic_cpu_threads(
    primary_options: Mapping[str, object], secondary_options: Mapping[str, object]
) -> None:
    """Validate thread-slot expression after typed options and CPU-periodic context.

    Missing and None both select automatic threads. Numeric option validation
    remains with callers. This checks the launcher's expression limit, not
    upstream algorithm support or miner-version/capability requirements.
    """
    if (primary_options.get("cpu_threads") is None) != (
        secondary_options.get("cpu_threads") is None
    ):
        raise ValueError(SRB_PERIODIC_CPU_THREADS_ERROR)
