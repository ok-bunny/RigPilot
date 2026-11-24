"""Audited built-in adapters for RigPilot managed miners."""

from __future__ import annotations

from .srb_catalog import (
    SRB_ALGORITHM_MINIMUM_VERSIONS,
    SRB_ALGORITHM_ROWS,
    SRB_DUAL_ALGORITHM_PAIRS,
    SRB_DUAL_ANY_SECONDARY,
    SRB_GPU_MINIMUM_VERSIONS,
    SRB_LEGACY_REMOVED,
    SRB_LEGACY_REMOVAL_VERSION,
)
from .contract import (
    AlgorithmSpec,
    AssetSpec,
    MinerAdapter,
    MinerAdapterRegistry,
    OptionSpec,
    PlatformSpec,
    PrereleasePolicy,
    ReleaseSource,
    ReleaseTagStyle,
)


_EXTRA_ARGS_OPTION = OptionSpec(
    "extra_args", "string", "额外参数",
    "附加矿工命令行参数；支持双/单引号和多行，按字面值传入；不可覆盖钱包、矿池、API、设备、OC 或配置路径",
    default="", min_length=0, max_length=4096,
)


_COMMON_CPU_OPTIONS = (
    OptionSpec(
        "cpu_threads", "integer", "CPU Threads", "CPU mining threads", 1, 1024,
        nullable=True,
    ),
    OptionSpec(
        "huge_pages", "boolean", "Huge Pages", "Enable miner huge pages", default=True
    ),
    OptionSpec(
        "randomx_1gb_pages",
        "boolean",
        "RandomX 1 GB Pages",
        "Enable RandomX one-gigabyte pages when supported", default=False,
    ),
    OptionSpec("tls", "boolean", "TLS", "Enable the miner TLS option", default=False),
    OptionSpec("keepalive", "boolean", "Keepalive", "Enable pool keepalive", default=True),
)


XMRIG_ADAPTER = MinerAdapter(
    adapter_id="org.xmrig.xmrig",
    adapter_revision=1,
    legacy_miner_type="xmrig",
    display_name="XMRig",
    runtime_id="builtin.xmrig.v1",
    telemetry_id="builtin.xmrig.http.v1",
    release=ReleaseSource(
        "xmrig",
        "xmrig",
        ("6.26.0", "6.25.0", "6.23.0"),
        "github.xmrig.v1",
        ReleaseTagStyle.V_PREFIX,
        PrereleasePolicy.STABLE_ONLY,
    ),
    platforms=(
        PlatformSpec("windows", ("xmrig.exe",)),
        PlatformSpec("linux", ("xmrig",)),
    ),
    assets=(
        AssetSpec(
            "windows", "windows", "xmrig-{version}-windows-x64.zip", "zip"
        ),
        AssetSpec(
            "linux", "linux", "xmrig-{version}-linux-static-x64.tar.gz", "tar.gz"
        ),
    ),
    algorithm_specs=(AlgorithmSpec("randomx", "cpu", ("XMR",), ("rx", "rx/0")),),
    default_algorithm="randomx",
    coin_defaults=(("XMR", "randomx"),),
    resource_kinds=frozenset({"cpu"}),
    pool_schemes=frozenset({"stratum+tcp", "stratum+ssl", "stratum+tls"}),
    options=_COMMON_CPU_OPTIONS
    + (
        _EXTRA_ARGS_OPTION,
        OptionSpec(
            "cpu_max_threads_hint",
            "integer",
            "Maximum CPU Threads",
            "Limit automatic CPU thread selection by percentage",
            1,
            100,
            default=100,
        ),
        OptionSpec(
            "pause_on_battery",
            "boolean",
            "Pause on Battery",
            "Pause CPU mining while a portable device uses battery power",
            default=False,
        ),
    ),
    capabilities=frozenset({"native", "cpu", "independent_instances", "local_http_api"}),
    package_namespaces=("builtin-xmrig-", "test-xmrig-"),
)


SRBMINER_ADAPTER = MinerAdapter(
    adapter_id="org.doktor83.srbminer-multi",
    adapter_revision=1,
    legacy_miner_type="srbminer",
    display_name="SRBMiner-MULTI",
    runtime_id="builtin.srbminer.v1",
    telemetry_id="builtin.srbminer.http.v1",
    release=ReleaseSource(
        "doktor83",
        "SRBMiner-Multi",
        ("3.7.2", "3.7.1", "3.4.6", "3.4.5", "3.4.4"),
        "github.srbminer.v1",
        ReleaseTagStyle.PLAIN,
        PrereleasePolicy.STABLE_ONLY,
    ),
    platforms=(
        PlatformSpec("windows", ("SRBMiner-MULTI.exe",)),
        PlatformSpec("linux", ("SRBMiner-MULTI",)),
    ),
    assets=(
        AssetSpec(
            "windows",
            "windows",
            "SRBMiner-Multi-{version_dashed}-win64.zip",
            "zip",
        ),
        AssetSpec(
            "linux",
            "linux",
            "SRBMiner-Multi-{version_dashed}-Linux.tar.gz",
            "tar.gz",
        ),
    ),
    algorithm_specs=tuple(
        AlgorithmSpec(name, default, ("XMR",) if name == "randomx" else ("QUANTUS",) if name == "quantus" else (),
                      ("rx", "rx/0") if name == "randomx" else (), resources, vendors,
                      SRB_ALGORITHM_MINIMUM_VERSIONS.get(name),
                      gpu_minimum_versions=SRB_GPU_MINIMUM_VERSIONS.get(name, ()))
        for name, default, resources, vendors in SRB_ALGORITHM_ROWS
    ) + tuple(AlgorithmSpec(name, "gpu", removed_version=SRB_LEGACY_REMOVAL_VERSION, catalogue_status="legacy") for name in SRB_LEGACY_REMOVED),
    default_algorithm="randomx",
    coin_defaults=(
        ("XMR", "randomx"),
        ("ETC", "etchash"),
        ("ETHW", "ethash"),
        ("RVN", "kawpow"),
        ("ERG", "autolykos2"),
        ("PEARL", "pearlhash"),
        ("QUANTUS", "quantus"),
        ("QTC", "qhash"),
    ),
    coin_algorithm_constraints=(("XMR", ("randomx",)),),
    resource_kinds=frozenset({"cpu", "gpu"}),
    pool_schemes=frozenset({"stratum+tcp", "stratum+ssl", "stratum+tls", "quic"}),
    options=_COMMON_CPU_OPTIONS
    + (
        _EXTRA_ARGS_OPTION,
        OptionSpec("resource_kind", "choice", "任务资源", "CPU/GPU 混合算法选择本任务实际使用的资源；留空使用算法默认资源", choices=("cpu", "gpu"), nullable=True),
        OptionSpec("quic_wallet_mode", "choice", "QUIC 连接身份", "公共矿池使用付款钱包（默认）；私有节点明确选择 token，并在私密矿池凭据中输入节点认证 token", choices=("wallet", "token"), nullable=True),
        OptionSpec("tls_cert_sha256", "string", "QUIC 证书指纹", "Quantus QUIC 的 DER certificate SHA256 pin；保持节点提供的编码，不作为付款钱包", nullable=True, min_length=1, max_length=512),
        OptionSpec(
            "cpu_threads_reduce",
            "integer",
            "Reduce CPU Threads",
            "Reduce SRBMiner automatic CPU threads",
            0,
            1024,
            nullable=True,
        ),
        OptionSpec(
            "cpu_threads_intensity",
            "choice",
            "CPU Thread Intensity",
            "SRBMiner CPU thread intensity",
            choices=(1, 2, 4),
            nullable=True,
        ),
        OptionSpec(
            "extended_log", "boolean", "Extended Log", "Enable extended SRBMiner logging",
            default=False,
        ),
    ),
    capabilities=frozenset(
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
        }
    ),
    package_namespaces=("builtin-srbminer-", "test-srbminer-"),
    dual_algorithm_pairs=tuple(sorted(SRB_DUAL_ALGORITHM_PAIRS)),
    dual_any_secondary=tuple(sorted(SRB_DUAL_ANY_SECONDARY)),
)


PEARLMINER_ADAPTER = MinerAdapter(
    adapter_id="org.pearlfortune.pearl-miner",
    adapter_revision=1,
    legacy_miner_type="pearlminer",
    display_name="Pearl Miner",
    runtime_id="builtin.pearlminer.v1",
    telemetry_id="builtin.pearlminer.log.v1",
    release=ReleaseSource(
        "pearlfortune",
        "pearl-miner",
        ("2.0.3", "1.2.4", "1.2.3"),
        "github.pearlminer.v1",
        ReleaseTagStyle.V_PREFIX,
        PrereleasePolicy.ALLOW_WITH_STABLE_DEFAULT,
    ),
    platforms=(
        PlatformSpec("windows", ("miner.exe",)),
        PlatformSpec(
            "linux", ("miner", "miner-cuda12", "miner-cuda13"), ("nvidia", "amd")
        ),
    ),
    assets=(
        AssetSpec(
            "windows",
            "windows",
            "miner-windows-v{version}.zip",
            "zip",
            fallback_versions=("1.2.4", "1.2.3", "1.2.1"),
        ),
        AssetSpec(
            "linux-nvidia",
            "linux",
            "pearlfortune-v{version}.tar.gz",
            "tar.gz",
            "nvidia",
            ("2.0.3", "1.2.4", "1.2.3", "1.2.1"),
        ),
        AssetSpec(
            "linux-amd",
            "linux",
            "pearlfortune-amd-v{version}.tar.gz",
            "tar.gz",
            "amd",
            ("1.2.4", "1.2.1"),
            (("1.2.2", "pearlfortune-amd-v1.2.2.fix.tar.gz"),),
        ),
    ),
    algorithm_specs=(AlgorithmSpec("pearlhash", "gpu", ("PEARL", "PRL")),),
    default_algorithm="pearlhash",
    coin_defaults=(("PEARL", "pearlhash"), ("PRL", "pearlhash")),
    resource_kinds=frozenset({"gpu"}),
    pool_schemes=frozenset({"pearl", "stratum+ssl", "stratum+tcp"}),
    options=(
        _EXTRA_ARGS_OPTION,
        OptionSpec(
            "gpu_backend",
            "choice",
            "GPU Backend",
            "Select the packaged NVIDIA or AMD backend",
            choices=("auto", "nvidia", "amd"),
            default="auto",
        ),
        OptionSpec(
            "cuda_variant",
            "choice",
            "CUDA Variant",
            "Select an audited CUDA binary",
            choices=("auto", "cuda12", "cuda13"),
            default="auto",
        ),
    ),
    capabilities=frozenset(
        {"native", "wsl_gpu", "gpu", "independent_instances", "log_telemetry"}
    ),
    package_namespaces=("builtin-pearlminer-", "test-pearlminer-"),
)


from .seine_catalog import build_seine_adapter

SEINE_ADAPTER = build_seine_adapter()

BUILTIN_ADAPTERS = (
    XMRIG_ADAPTER,
    SRBMINER_ADAPTER,
    PEARLMINER_ADAPTER,
    SEINE_ADAPTER,
)
MINER_ADAPTERS = MinerAdapterRegistry(BUILTIN_ADAPTERS)
