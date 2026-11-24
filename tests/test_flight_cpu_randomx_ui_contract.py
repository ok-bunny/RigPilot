import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def bootstrap_adapters():
    html = (ROOT / "server/static/index.html").read_text(encoding="utf-8")
    match = re.search(
        r'<script\b[^>]*\bid="minerAdapterBootstrap"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match is not None
    return {item["miner_type"]: item for item in json.loads(match.group(1))}


def test_v1_cpu_randomx_options_are_structured_visible_and_algorithm_scoped():
    html = (ROOT / "server/static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "server/static/app.js").read_text(encoding="utf-8")

    for prefix in ("flight", "flightSecondary"):
        for suffix in ("CpuRandomxSection", "CpuHintField", "CpuThreadsField", "HugePages"):
            assert f'id="{prefix}{suffix}"' in html
    assert html.count("CPU / RandomX") == 2
    assert html.count("启用 Huge Pages") == 2
    assert "Windows 首次可能需要管理员权限并重启" in html
    assert "启用 RandomX 1GB Pages" in app
    assert "受支持的 Linux 原生" in app

    # Current function behavior is exercised independently in
    # cpu_controls_ui_behavior.test.cjs: resource choices, typed hydration,
    # collection/build, thread limits and stored-value/reset isolation.
    # This Python contract needs no Node process or retired JS declarations.


def test_cpu_bootstrap_options_are_typed_job_values_without_raw_or_msr_controls():
    adapters = bootstrap_adapters()
    expected = {
        "xmrig": {
            "cpu_threads": "integer", "huge_pages": "boolean",
            "randomx_1gb_pages": "boolean", "keepalive": "boolean",
            "cpu_max_threads_hint": "integer", "pause_on_battery": "boolean",
        },
        "srbminer": {
            "cpu_threads": "integer", "huge_pages": "boolean",
            "randomx_1gb_pages": "boolean", "keepalive": "boolean",
            "cpu_threads_reduce": "integer", "cpu_threads_intensity": "choice",
            "extended_log": "boolean", "resource_kind": "choice",
        },
    }
    for miner_type, types in expected.items():
        adapter = adapters[miner_type]
        options = {option["name"]: option for option in adapter["options"]}
        assert {"native", "cpu", "independent_instances", "local_http_api"} <= set(adapter["capabilities"])
        assert not {"raw_args", "rdmsr", "wrmsr"} & options.keys()
        for name, option_type in types.items():
            assert options[name]["type"] == option_type
            assert options[name]["scope"] == "job"
        assert options["cpu_threads"]["minimum"] == 1
        assert options["cpu_threads"]["maximum"] == 1024
        assert options["cpu_threads"]["nullable"] is True
        assert options["huge_pages"]["default"] is True
        assert options["randomx_1gb_pages"]["default"] is False
    srb_options = {option["name"]: option for option in adapters["srbminer"]["options"]}
    assert srb_options["cpu_threads_reduce"]["minimum"] == 0
    assert srb_options["cpu_threads_reduce"]["maximum"] == 1024
    assert srb_options["cpu_threads_intensity"]["choices"] == [1, 2, 4]
    assert srb_options["resource_kind"]["choices"] == ["cpu", "gpu"]
    assert {"gpu", "integrated_dual", "device_probe", "device_selection"} <= set(adapters["srbminer"]["capabilities"])


def test_cpu_bootstrap_distinguishes_cpu_algorithms_from_mixed_resource_choices():
    adapters = bootstrap_adapters()
    for miner_type in ("xmrig", "srbminer"):
        algorithms = {item["name"]: item for item in adapters[miner_type]["algorithms"]}
        assert algorithms["randomx"]["resource_kind"] == "cpu"
        assert algorithms["randomx"]["supported_resources"] == ["cpu"]
    srb_algorithms = {item["name"]: item for item in adapters["srbminer"]["algorithms"]}
    assert srb_algorithms["randomepic"]["supported_resources"] == ["cpu"]
    assert srb_algorithms["yespower"]["resource_kind"] == "cpu"
    assert set(srb_algorithms["yespower"]["supported_resources"]) == {"cpu", "gpu"}
    assert srb_algorithms["pearlhash"]["supported_resources"] == ["gpu"]
