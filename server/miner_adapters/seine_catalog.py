"""Seine CPU/pool metadata pinned to audited upstream releases."""
from .contract import AlgorithmSpec,AssetSpec,MinerAdapter,OptionSpec,PlatformSpec,PrereleasePolicy,ReleaseSource,ReleaseTagStyle

SEINE_ALGORITHM="blocknet-argon2id"
SEINE_LATEST_VERSION="0.2.45"
SEINE_FALLBACK_VERSION="0.2.15"

def build_seine_adapter():
    return MinerAdapter(
        adapter_id="org.zpalmtree.seine",adapter_revision=1,
        legacy_miner_type="seine",display_name="Seine",runtime_id="builtin.seine.v1",
        telemetry_id="builtin.seine.http.v1",
        release=ReleaseSource("zpalmtree","seine",(SEINE_LATEST_VERSION,SEINE_FALLBACK_VERSION),"github.seine.v1",
            ReleaseTagStyle.V_PREFIX,PrereleasePolicy.STABLE_ONLY,minimum_version=SEINE_FALLBACK_VERSION),
        platforms=(PlatformSpec("windows",("seine.exe",)),PlatformSpec("linux",("seine",))),
        assets=(
            AssetSpec("windows","windows","seine-windows-x86_64.zip","zip",sha256_by_version=(
                (SEINE_LATEST_VERSION,"1708e8f0d02426db508a1f75fed73a6ee314c0cc7ec9005143049d78c2ddf3d3"),
                (SEINE_FALLBACK_VERSION,"f779f013ea91f329fc481a7c302e727e2c839ac004dbfd8e4b216f59fd425cfb"))),
            AssetSpec("linux","linux","seine-linux-x86_64.tar.gz","tar.gz",sha256_by_version=(
                (SEINE_LATEST_VERSION,"19e3c979d14f38664bdfb28c2b48e4b0580a1cb711bae2ee68bcbc9ea41bd173"),
                (SEINE_FALLBACK_VERSION,"bcbf7b71a03dbac59953e1615f5bc15fbc6079e8f6252f0bae8ad435f62d06c1"))),
        ),
        algorithm_specs=(AlgorithmSpec(SEINE_ALGORITHM,"cpu",("BNT",),supported_resources=("cpu",)),),
        default_algorithm=SEINE_ALGORITHM,coin_defaults=(("BNT",SEINE_ALGORITHM),),
        resource_kinds=frozenset({"cpu"}),pool_schemes=frozenset({"stratum+tcp"}),
        options=(
            OptionSpec("cpu_threads","integer","CPU 线程","留空由Seine按CPU/RAM/cgroup自动选择；每线程约2GiB，不能盲目使用全部核",minimum=1,nullable=True),
            OptionSpec("cpu_profile","choice","CPU 性能模式","沿用Seine性能预设；留空恢复上游balanced默认",choices=("balanced","throughput","efficiency"),default="balanced",nullable=True),
            OptionSpec("extra_args","string","额外参数","附加字面参数，不得覆盖CPU模式、钱包、矿池、API、设备或私有运行路径",default="",min_length=0,max_length=4096),
        ),
        capabilities=frozenset({"native","cpu","independent_instances","local_http_api"}),
        package_namespaces=("builtin-seine-","test-seine-"),
    )
