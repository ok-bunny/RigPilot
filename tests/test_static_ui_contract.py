import base64
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, parse_qs
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Assets(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "script" and fields.get("src"):
            self.urls.append(fields["src"])
        elif tag == "link" and fields.get("rel") == "stylesheet":
            self.urls.append(fields["href"])


def _cache_busted_assets(html, required):
    urls = _Assets(html).urls
    paths = [urlsplit(url).path for url in urls]
    for path in required:
        assert paths.count(path) == 1  # One authoritative asset, not a fixed page-wide total.
        query = parse_qs(urlsplit(urls[paths.index(path)]).query)
        assert len(query.get("v", [])) == 1 and query["v"][0].strip()
    assert paths.index("/static/theme-init.js") < paths.index(required[-1])


def test_login_forms_start_empty_and_keep_password_manager_sections_distinct():
    index_html = (ROOT / "server" / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    controller_inputs = (
        index_html.split('<form id="loginForm"', 1)[1].split("</form>", 1)[0],
    )
    for form in controller_inputs:
        assert 'value="admin"' not in form
        assert 'autocomplete="section-rigpilot-controller username"' in form
        assert 'autocomplete="section-rigpilot-controller current-password"' in form
        assert 'autocapitalize="none"' in form
        assert 'spellcheck="false"' in form
    assert 'id="currentUsername">admin<' not in index_html
    assert 'id="currentUsername">—<' in index_html
    assert "只有 admin 可以" not in index_html


def test_credential_inputs_keep_native_password_manager_rendering_and_narrow_size():
    index_html = (ROOT / "server" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    styles = (ROOT / "server" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )

    for field_id, document in (
        ("password", index_html),
        ("newPassword", index_html),
    ):
        field = re.search(rf'<input[^>]*id="{field_id}"[^>]*>', document)
        assert field is not None
        assert 'type="password"' in field.group(0)

    narrow_rules = styles.split("/* Single adaptive layout:", 1)[1]
    assert "input, select, textarea { font-size: 16px; }" in narrow_rules
    assert 'input[type="password"] {' not in styles
    assert "font: -webkit-small-control" not in styles
    assert "@supports (-webkit-touch-callout: none)" not in styles
    autofill_rule = styles.split(".login-panel input:-webkit-autofill,", 1)[1].split(
        "}", 1
    )[0]
    assert "-webkit-text-fill-color" in autofill_rule
    assert "caret-color" in autofill_rule
    assert "font:" not in autofill_rule
    assert "font-size" not in autofill_rule
    assert "letter-spacing" not in autofill_rule
    assert "transition: font" not in styles
    assert "transition: letter-spacing" not in styles


def test_successful_login_clears_password_before_entering_application():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    login = app_js.split("async function login(event)", 1)[1].split(
        "function logout", 1
    )[0]

    request_end = login.index('$("#password").value = "";')
    enter_app = login.index("await enterApp();")
    assert request_end < enter_app


def test_add_wallet_button_does_not_pass_click_event_as_wallet():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert '$("#addWalletButton").addEventListener("click", () => openWalletDialog());' in app_js
    assert '$("#addWalletButton").addEventListener("click", openWalletDialog);' not in app_js


def test_srbminer_gpu_oc_fields_stay_inside_advanced_settings():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    advanced_start = html.index('<details id="flightAdvanced"')
    advanced_end = html.index("</details>", advanced_start)
    oc_start = html.index('<section id="flightGpuOcSection"', advanced_start)

    assert advanced_start < oc_start < advanced_end
    assert '<option value="pearlhash">PearlHash</option>' in html
    assert "二选一" not in html[oc_start:advanced_end]


def test_srbminer_flight_oc_uses_model_rules_instead_of_per_row_indices():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    row_builder = app_js.split("function addFlightGpuOcRow(values = {})", 1)[1].split(
        "function collectFlightGpuOc", 1
    )[0]
    collector = app_js.split("function collectFlightGpuOc()", 1)[1].split(
        "function setFlightAlgorithmValue", 1
    )[0]
    index_hints = app_js.split("function updateFlightGpuOcIndexHints()", 1)[1].split(
        "function syncFlightGpuOcVisibility", 1
    )[0]

    assert "SRBMiner 按型号 GPU OC" in html
    assert "5080、5070、5060 Ti" in index_hints
    assert "同型号多张卡自动共用本行参数" in index_hints
    assert 'data-flight-gpu-field="model_match"' in row_builder
    assert "显卡型号匹配" in row_builder
    assert 'data-flight-gpu-field="index"' not in row_builder
    assert "model_match: modelMatch" in collector
    assert "旧索引 OC 与型号匹配 OC 不能混用" in collector


def test_device_gpu_oc_replaces_standalone_page_and_targets_one_nvml_index():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'data-page="gpu"' not in html
    assert 'id="gpuPage"' not in html
    assert 'id="deviceGpuOcDialog"' in html
    assert 'data-device-gpu-oc="${index}"' in app_js
    assert "gpuTableCellMarkup(agent)" not in app_js
    assert "agent.status === \"online\"" in app_js
    assert "state.user?.role === \"operator\"" in app_js
    assert "if (!window.confirm(question)) return;" in app_js
    assert "/gpu-oc/apply" in app_js
    assert 'id="deleteDeviceGpuOcButton"' in html
    assert "function gpuDesiredOcEntry(agent, gpu)" in app_js
    assert "agent?.desired_gpu_oc" in app_js
    assert "/gpu-oc/${target.index}?gpu_uuid=" in app_js
    assert 'method: "DELETE"' in app_js
    assert "控制端已保存" in app_js
    assert 'body: { vendor: "nvidia", gpu_uuid: target.uuid, setting }' in app_js
    table_row = app_js.split("function deviceTableRow(agent)", 1)[1].split(
        "function renderDevices", 1
    )[0]
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]
    assert "data-device-gpu-oc" not in table_row
    assert "gpuUtilizationTableMarkup(agent)" in table_row
    assert "deviceDetailGpuRow(agent, gpu, canOperate)" in drawer
    assert "device-gpu-full-detail" not in drawer
    assert "查看逐卡完整硬件与 OC 记录" not in drawer
    assert 'class="device-gpu-oc-note device-gpu-oc-common-note"' in drawer
    assert drawer.count("不等同于当前生效参数") == 1
    assert "openDeviceGpuOc(Number(gpuButton.dataset.deviceGpuOc))" in app_js
    assert "selectedGpuOcPanel" not in app_js
    assert "inlineGpuOcForm" not in app_js
    assert "device-gpu-workspace" not in drawer
    assert drawer.index('class="device-hashrate-panel"') < drawer.index('<h4>GPU 硬件监控</h4>')
    assert "selectedGpuUuid" not in app_js
    assert "selectDeviceDetailGpu" not in app_js
    assert "data-select-gpu-uuid" not in app_js
    submitter = app_js.split("async function submitDeviceGpuOc(", 1)[1].split(
        "async function applyDeviceGpuOc()", 1
    )[0]
    assert "state.agents.find((item) => item.id === target.agentId)" in submitter
    assert "gpuMetricUuid(currentGpu) !== target.uuid" in submitter
    assert "encodeURIComponent(agent.id)" in submitter
    assert "agentVersionAtLeast(agent.agent_version, [0, 6, 16])" in app_js
    assert "逐卡累计 OC 需要先将该设备升级到 Agent 0.6.16" in app_js
    assert "停止矿工后会尝试把设备上的 NVML OC 恢复为硬件默认值" in app_js
    assert "控制端保存的 OC 配置不会删除。恢复失败时任务会明确标记失败" in app_js
    for field in (
        "power_limit_w",
        "core_clock_offset_mhz",
        "core_clock_lock_mhz",
        "memory_clock_offset_mhz",
        "memory_clock_lock_mhz",
        "fan_percent",
    ):
        assert f'data-device-gpu-field="{field}"' in html


def test_device_gpu_oc_missing_saved_records_require_explicit_uuid_only_forget():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    matcher = app_js.split("function gpuMissingDesiredOcEntries(agent)", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "gpuMetricUuid" in matcher
    assert "reportedUuids.has(uuid)" in matcher
    assert "gpuMetricIndex" not in matcher
    assert "entry?.setting" not in matcher

    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]
    assert "已缺席 OC 记录" in app_js
    assert "gpuMissingDesiredOcMarkup(agent, canOperate)" in drawer
    assert "data-forget-missing-gpu-oc" in drawer

    forgetter = app_js.split("async function forgetMissingDeviceGpuOc", 1)[1].split(
        "async function deleteDeviceGpuOc", 1
    )[0]
    assert forgetter.count("window.confirm(") == 2
    assert "&forget_stale=true" in forgetter
    assert 'result?.status !== "deleted" || result?.reset_command !== null' in forgetter
    assert "不会向任何显卡下发恢复默认命令" in forgetter

    normal_delete = app_js.split("async function deleteDeviceGpuOc()", 1)[1].split(
        "async function forgetMissingDeviceGpuOc", 1
    )[0]
    assert 'result?.status !== "reset_pending" || !result?.reset_command' in normal_delete
    assert "恢复默认任务已排队，成功后才删除保存记录" in normal_delete


def test_miner_log_poll_cancels_abandoned_or_timed_out_command():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    # The one-shot "last 60 seconds" view is gone; the live loop is the only
    # collector and must still cancel abandoned or timed-out commands.
    assert "async function collectMinerLogs()" not in app_js
    assert "data-collect-miner-logs" not in app_js
    assert "最近 60 秒矿工日志" not in app_js
    live_poller = app_js.split(
        "async function requestLiveMinerLogSnapshot(session)", 1
    )[1].split("async function runLiveMinerLogLoop", 1)[0]

    assert "async function cancelMinerLogCommand(commandId)" in app_js
    assert live_poller.count("await cancelMinerLogCommand(queued.id);") >= 2
    assert "Date.now() + 45_000" in live_poller
    stopper = app_js.split("function stopLiveMinerLogs()", 1)[1].split(
        "function openLiveMinerLogs", 1
    )[0]
    assert "void cancelMinerLogCommand(commandId);" in stopper


def test_remote_task_controls_distinguish_cancel_from_offline_abandon():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    # deviceActivityMarkup command operations are exercised by the actual
    # Node command/audit behavior tests; retired global renderers are not gates.
    deleter = app_js.split("async function deleteOfflineAgent", 1)[1].split(
        "async function controlMining", 1
    )[0]
    assert "?abandon_unfinished=true" in deleter
    assert "无法保证设备端动作尚未执行" in deleter
    assert "/api/commands?limit=100&include_all_active=true" in app_js


def test_live_miner_log_dialog_is_on_demand_abortable_and_highlighted():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="liveMinerLogDialog"' in html
    assert 'id="liveMinerLogOutput"' in html
    assert "关闭后立即停止" in html
    assert "data-live-miner-logs" in app_js
    assert "/commands?live=true" in app_js
    assert 'body: { action: "collect_miner_logs", payload: {} }' in app_js
    assert "controller: new AbortController()" in app_js
    assert "session.controller.abort();" in app_js
    assert 'addEventListener("close", stopLiveMinerLogs)' in app_js
    assert "previousBySource: new Map()" in app_js
    assert "liveMinerLogOverlap(previous, current)" in app_js
    for category in ("error", "warning", "success", "share", "hashrate", "pool"):
        assert f".miner-live-line.is-{category}" in styles
    # Dual mining: one stream per (role, source); legs never collide in the
    # dedup map and get leg tabs plus a numeric-token highlight.
    assert "const dedupKey = `${role}:${source}`;" in app_js
    assert 'id="liveMinerLogTabs"' in html
    assert 'data-tab="all"' in app_js
    assert ".miner-live-tab" in styles
    assert ".miner-live-role.is-primary" in styles
    assert ".miner-live-role.is-secondary" in styles
    assert ".miner-live-num" in styles


def test_live_miner_log_output_escapes_remote_lines_and_stays_bounded():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    renderer = app_js.split("function liveMinerLogLineMarkup(entry, options)", 1)[1].split(
        "function renderLiveMinerLogs", 1
    )[0]
    merger = app_js.split("function appendLiveMinerLogSnapshot(session, result)", 1)[1].split(
        "function liveMinerLogLineMarkup", 1
    )[0]

    assert "escapeHtml(entry.source)" in renderer
    assert "escapeHtml(entry.line)" in renderer
    assert "session.lines.length - 2000" in merger
    assert "entry.lines.slice(0, 500)" in merger


def test_device_list_has_guarded_mining_control_buttons():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'data-mining-action="${action}"' in app_js
    assert 'button("stop_mining", "停止挖矿"' in app_js
    assert 'button("restart_mining", "重启挖矿"' in app_js
    assert "miningDisplayState(miningStatus, agent)" in app_js
    assert "!presentation.stopActive ? presentation.stopReason" in app_js
    assert 'stale: ["未挖矿 · 状态残留"' in app_js
    assert 'error: ["未挖矿 · 状态异常"' in app_js
    assert 'class="status-pill mining-list-state ${presentation.className}"' in app_js
    assert ".mining-list-state" in styles
    assert "agentSupportsMiningControl(agent.agent_version)" in app_js
    assert "return agentVersionAtLeast(version, [0, 5, 2]);" in app_js
    assert "event.stopPropagation();" in app_js
    assert "if (!window.confirm(question)) return;" in app_js
    assert "body: { action, payload: {} }" in app_js


def test_device_list_marks_confirmed_non_running_states_without_hashrate():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    renderer = app_js.split("function deviceTableMiningSnapshot(agent)", 1)[1].split(
        "function deviceMiningColumnMarkup", 1
    )[0]
    columns = app_js.split("function deviceMiningColumnMarkup", 1)[1].split(
        "function agentReleaseMatchesDevice", 1
    )[0]

    assert '["stopped", "inactive", "stale", "failed", "error"].includes(miningState)' in renderer
    assert 'placeholder: "未挖矿"' in renderer
    assert 'placeholder: "未配置"' in renderer
    assert "escapeHtml(snapshot.placeholder)" in columns


def test_mining_status_keeps_dual_algorithms_separate_and_scales_si_units():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'const HASHRATE_UNITS = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s", "EH/s"];' in app_js
    assert "amount >= 1000 ||" in app_js
    assert "Number(amount.toFixed(2)) >= 1000" in app_js
    assert 'const identity = [detail.coin, detail.algorithm]' in app_js
    assert 'detail.pool_url || "矿池未上报"' in app_js
    assert 'Pool A/R(St) ${shares.accepted ?? "—"}/${shares.rejected ?? "—"}(${shares.stale ?? "—"})' in app_js
    assert 'Proof S/R/D ${proofs.s ?? "—"}/${proofs.r ?? "—"}/${proofs.d ?? "—"}' in app_js
    assert 'class="mining-hashrate-panel"' in app_js
    # Even an older Agent that still sends a total must render primary and
    # secondary first instead of hiding them behind the invalid mixed total.
    formatter = app_js.split("function formatHashrate(value", 1)[1].split(
        "function miningHashrateMarkup", 1
    )[0]
    assert formatter.index("const parts") < formatter.index("const total")


def test_device_tables_split_mining_identity_and_put_last_seen_under_status():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    header = html.split('<table class="device-table fleet-device-table">', 1)[1].split("</thead>", 1)[0]
    expected_headers = [
        ("fleet-col-device", "设备"), ("fleet-col-status", "状态/<wbr>最后在线时间"),
        ("fleet-col-coin", "币种"), ("fleet-col-miner", "挖矿软件"),
        ("fleet-col-hashrate", "算力"), ("fleet-col-cpu", "CPU"),
        ("fleet-col-memory", "内存"), ("fleet-col-gpu", "GPU"),
        ("fleet-col-agent", "被控端版本"), ("fleet-col-actions", "设备状态/操作"),
    ]
    assert [header.index(f'<th class="{name}">{label}</th>') for name, label in expected_headers] == sorted(
        header.index(f'<th class="{name}">{label}</th>') for name, label in expected_headers
    )
    assert [header.index(f'<col class="{name}">') for name, _ in expected_headers] == sorted(
        header.index(f'<col class="{name}">') for name, _ in expected_headers
    )
    assert "fleet-col-mining" not in header and "fleet-col-last-seen" not in header
    assert header.count(">GPU</th>") == 1

    snapshot = app_js.split("function deviceTableMiningSnapshot(agent)", 1)[1].split(
        "function deviceMiningColumnMarkup", 1
    )[0]
    rate_details = app_js.split("function miningHashrateDetails(status)", 1)[1].split(
        "function miningHashrateRole", 1
    )[0]
    identity = app_js.split("function deviceMiningMinerIdentity", 1)[1].split(
        "function hashrateToHps", 1
    )[0]
    columns = app_js.split("function deviceMiningColumnMarkup", 1)[1].split(
        "function agentReleaseMatchesDevice", 1
    )[0]
    status = app_js.split("function deviceStatusCellMarkup(agent", 1)[1].split(
        "function deviceTableRow(agent)", 1
    )[0]
    row = app_js.split("function deviceTableRow(agent)", 1)[1].split(
        "function setTableVisible", 1
    )[0]
    assert "miningStatusForAgent(agent)" in snapshot
    assert 'agent?.status !== "online"' in snapshot
    assert '!["running", "active", "degraded", "running_zero_hashrate", "running_partial_hashrate"].includes(miningState)' in snapshot
    assert "miningHashrateDetails(status)" in snapshot
    assert "miningHashrateRole(detail, index)" in snapshot
    assert "miningHashrateDetailIsFresh(status, detail, agent)" in snapshot
    assert "miningHashrateObservedValue(detail.hashrate)" in snapshot
    assert "status.hashrate_details" in rate_details
    assert '["primary", "secondary"]' in rate_details
    assert "rows.push({ role, hashrate: rates[role] })" in rate_details
    assert 'placeholder: "--"' in snapshot
    assert "minerPackage.version" in app_js
    assert identity.index("|| reportedPackage?.name") < identity.index(
        "|| minerPackageTypeLabel(type)"
    )
    assert '!hasSecondary && role !== "secondary"' in identity
    assert 'detail?.version_source === "hiveos_stats"' in identity
    assert 'column === "coin"' in columns and 'column === "miner"' in columns
    assert "device-mining-miner-version" in columns and "hiveos-badge" in columns
    assert "escapeHtml(versionTitle)" in columns
    assert "包版本：" not in columns
    assert "deviceMiningTaskMarkup" not in app_js
    assert 'deviceMiningColumnMarkup(miningSnapshot, "coin")' in row
    assert 'deviceMiningColumnMarkup(miningSnapshot, "miner")' in row
    assert 'deviceMiningColumnMarkup(miningSnapshot, "hashrate")' in row
    assert row.index('deviceMiningColumnMarkup(miningSnapshot, "coin")') < row.index(
        'deviceMiningColumnMarkup(miningSnapshot, "miner")'
    ) < row.index('deviceMiningColumnMarkup(miningSnapshot, "hashrate")')
    assert 'deviceStatusCellMarkup(agent, true)' in row
    assert 'formatTime(agent.last_seen, false)' in status
    assert 'class="device-last-seen"' in status
    assert 'title="最后在线：' in status
    assert "configurationRefreshBadge(agent)" in status
    assert "agentUpdateStatusMarkup(agent)" in status
    assert ":is(.fleet-device-table, .overview-device-table) .device-status-stack" in styles
    assert "grid-auto-rows: max-content; justify-items: center" in styles
    assert ":is(.fleet-device-table :is(th, td).fleet-col-status, .overview-device-table :is(th, td):nth-child(2)) { text-align: center; }" in styles
    assert ":is(.fleet-device-table th.fleet-col-status, .overview-device-table th:nth-child(2)) { white-space: normal; line-height: 1.3; }" in styles
    assert ".device-status-stack .device-last-seen { display: block; min-width: 0; max-width: 100%; color: var(--muted); font-family: inherit; font-size: 11px; font-weight: 500; line-height: 1.4;" in styles
    assert "overflow-wrap: anywhere; text-align: center; white-space: normal;" in styles
    assert "agent.hostname" not in row and "agent.note" not in row
    assert "escapeHtml(agent.name)" in row
    assert '<small>' not in row.split('class="fleet-col-device"', 1)[1].split("</td>", 1)[0]
    assert 'const resourcesOnline = agent.status === "online";' in row
    assert 'const cpuCell = resourcesOnline ? usage(metrics.cpu_percent, cpuTemperatureLabel(metrics)) : "-";' in row
    assert 'const memoryCell = resourcesOnline ? usage(metrics.memory_percent) : "-";' in row
    assert 'const gpuCell = resourcesOnline ? gpuUtilizationTableMarkup(agent)' in row
    assert 'gpuUtilizationPlaceholderMarkup("设备离线，GPU状态未确认")' in row
    assert "agentVersionCellMarkup(agent)" in row
    assert "miningControlButtons(agent)" in row and 'data-delete-agent' in row
    for name, _ in expected_headers:
        assert f'<td class="{name}">' in row

    table_css = styles.split(".fleet-device-table { min-width: 940px; table-layout: fixed; }", 1)[1].split(
        ".device-network {", 1
    )[0]
    widths = [float(value) for value in re.findall(
        r"\.fleet-device-table col\.fleet-col-[\w-]+ \{ width: ([\d.]+)%; \}", table_css
    )]
    assert len(widths) == 8 and 58 <= sum(widths) <= 62
    assert ".fleet-device-table col.fleet-col-device { width: 10%; }" in table_css
    assert ".fleet-device-table col.fleet-col-miner { width: 8%; }" in table_css
    assert ".fleet-device-table col.fleet-col-gpu { width: auto; }" in table_css
    assert ".fleet-device-table col.fleet-col-agent { width: 9.3%; }" in table_css
    assert ".fleet-device-table col.fleet-col-actions { width: 170px; }" in table_css
    assert ".device-table.fleet-device-table .gpu-utilization-list { width: 100%; flex-wrap: wrap; column-gap: 4px; row-gap: 8px; }" in table_css
    assert ".fleet-device-table col.fleet-col-status { width: 8%; }" in table_css
    assert ".fleet-device-table col.fleet-col-coin { width: 5.5%; }" in table_css
    assert ".device-table:is(.fleet-device-table, .overview-device-table) :is(th, td) { padding-right: 6px; padding-left: 6px; text-align: left; }" in table_css
    assert ".fleet-device-table .device-status-stack .status-pill { white-space: nowrap; }" in table_css
    assert "fleet-col-last-seen" not in table_css and "device-mining-task" not in table_css
    assert "height: 48px; min-height: 48px" in table_css
    assert ".device-mining-coin-list, .device-mining-miner-list, .device-mining-hashrate-list" in table_css
    assert "grid-template-columns: 50px 50px minmax(0, 1fr) 12px" in table_css
    assert '[data-delete-agent] { grid-column: 1 / 3; grid-row: 3; justify-self: start; }' in table_css
    assert ".gpu-utilization-row { width: 72px; flex-basis: 72px; }" in table_css
    assert ".fleet-device-table .fleet-col-actions .mining-control-button { min-width: 56px; }" not in styles
    assert ".fleet-device-table .gpu-temperature-hashrate { width: 100%; max-width: 100%; overflow-wrap: anywhere;" in table_css
    assert "font-size: 9px; line-height: 1.25; text-align: center; white-space: normal;" in table_css

    assert "min-width: 1500px;" not in styles
    assert ".device-list-page.active {" in styles

    overview = html.split('<table class="device-table overview-device-table">', 1)[1].split("</thead>", 1)[0]
    for before, after in (("挖矿软件", "CPU/GPU 概览"), ("CPU/GPU 概览", "算力"), ("算力", "GPU 功耗")):
        assert overview.index(before) < overview.index(after)
    assert "状态/<wbr>最后在线时间</th>" in overview
    assert "<th>最后在线</th>" not in overview
    overview_row = app_js.split("function overviewDeviceRow(agent)", 1)[1].split(
        "function renderOverviewDevices", 1
    )[0]
    assert 'deviceStatusCellMarkup(agent)' in overview_row
    assert "agent.hostname" not in overview_row and "agent.note" not in overview_row
    assert overview_row.index("overviewTemperatureMarkup(agent)") < overview_row.index(
        'deviceMiningColumnMarkup(mining, "hashrate")'
    ) < overview_row.index("${power}</td>")
    assert "overviewTemperatureMarkup(agent)" in overview_row
    assert '<small>' not in overview_row.split('class="device-ident"', 1)[1].split("</td>", 1)[0]
    assert ".overview-device-table :is(th, td):nth-child(7)" in styles
    assert ".overview-device-table :is(th, td):nth-child(8)" not in styles
    assert ".overview-device-table :is(th, td):nth-child(5) { width: 27%; }" in styles
    assert ".overview-temperature-list { display: flex; flex-wrap: wrap; align-items: center; gap: 6px;" in styles
    assert ".overview-device-table :is(th, td):last-child { text-align: left; white-space: nowrap; }" in styles


def test_device_list_gpu_efficiency_requires_complete_per_gpu_hashrate_and_power():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    efficiency = app_js.split("function deviceHashrateEfficiency", 1)[1].split(
        "function deviceMiningTableEntry", 1
    )[0]
    entry = app_js.split("function deviceMiningTableEntry", 1)[1].split(
        "function deviceTableMiningSnapshot", 1
    )[0]
    columns = app_js.split("function deviceMiningColumnMarkup", 1)[1].split(
        "function agentReleaseMatchesDevice", 1
    )[0]
    assert "detail?.gpu_hashrates" in efficiency
    assert "new Set(indices).size !== indices.length" in efficiency
    assert "detailSharesGpuIndices(status, detail, indices)" in efficiency
    assert "gpus.filter((gpu) => gpuMetricIndex(gpu) === indices[position])" in efficiency
    assert "matches.length !== 1" in efficiency
    assert "powerW <= 0" in efficiency
    assert "detailHashrateHps === null" in efficiency
    assert "hashrateDifference > hashrateTolerance" in efficiency
    assert "Math.abs(detailHashrateHps) * 0.02" in efficiency
    assert "totalHashrateHps / totalPowerW" in efficiency
    assert 'formatted.replace(/H\\/s$/, "H/s/W")' in app_js
    assert "efficiency: hasHashrateAmount(value) ? deviceHashrateEfficiency(agent, status, detail) : null" in entry
    assert 'row.efficiency?.label || "—"' in columns
    assert "缺少可核验的逐卡功耗，暂不估算能效" in columns
    assert 'class="device-mining-row device-mining-hashrate"' in columns
    assert "能效 ${escapeHtml(efficiencyLabel)}" in columns
    assert ".device-mining-hashrate" in styles
    assert ".device-mining-hashrate em" in styles
    aligned_rule = styles.split(".fleet-device-table :is(.device-mining-coin, .device-mining-miner, .device-mining-hashrate) {", 1)[1].split("}", 1)[0]
    assert "height: 48px; min-height: 48px" in aligned_rule
    assert "grid-template-rows: 18px 18px" in aligned_rule
    assert ".device-table:is(.fleet-device-table, .overview-device-table) :is(th, td) { padding-right: 6px; padding-left: 6px; text-align: left; }" in styles
    assert ".fleet-device-table :is(.device-mining-coin-list, .device-mining-miner-list, .device-mining-hashrate-list) { width: 100%; min-width: 0;" in styles
    mining_row_rule = styles.split(".device-mining-row {", 1)[1].split("}", 1)[0]
    assert "align-content: start" in mining_row_rule


def test_device_drawer_exposes_guarded_diagnostics_without_service_restart_shortcut():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]
    mining_section = drawer.split('class="drawer-section device-detail-mining"', 1)[1].split(
        "function formatUptime", 1
    )[0]
    guard = app_js.split("function deviceDiagnosticUnavailableReason(agent)", 1)[1].split(
        "function minerLogUnavailableReason", 1
    )[0]
    opener = app_js.split("function openAction(action)", 1)[1].split(
        "async function submitAction", 1
    )[0]
    submitter = app_js.split("async function submitAction()", 1)[1].split(
        "function openEnroll", 1
    )[0]

    assert 'data-agent-action="collect_diagnostics"' in mining_section
    assert '${canOperate ?' in mining_section
    assert 'agent.status === "online"' in mining_section
    assert 'disabled title="设备离线，无法采集诊断信息"' in mining_section
    assert '["admin", "operator"].includes(state.user?.role)' in guard
    assert 'agent.status !== "online"' in guard
    assert 'action === "collect_diagnostics"' in opener
    assert "deviceDiagnosticUnavailableReason(state.selectedAgent)" in opener
    assert 'action === "collect_diagnostics"' in submitter
    assert "deviceDiagnosticUnavailableReason(agent)" in submitter
    assert 'data-agent-action="restart_service"' not in drawer
    assert 'data-agent-action="reboot"' in drawer
    assert 'data-agent-action="shutdown"' in drawer



def test_agent_bootstrap_and_gpu_driver_versions_are_distinguished_with_legacy_fallback():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    helper = app_js.split("function agentBootstrapLabel(agent)", 1)[1].split(
        "function agentGpuDriverLabel", 1
    )[0]
    driver_helper = app_js.split("function agentGpuDriverLabel(agent)", 1)[1].split(
        "function agentVersionCellMarkup", 1
    )[0]
    renderer = app_js.split("function agentVersionCellMarkup(agent)", 1)[1].split(
        "function gpuUtilizationTableMarkup", 1
    )[0]

    assert "agent?.inventory?.agent_update_readiness?.bootstrap" in helper
    assert 'bootstrap.protocol !== "agent_bootstrap.v1"' in helper
    assert 'return ""' in helper
    assert "agent?.metrics?.gpus" in driver_helper
    assert "gpu?.driver_version" in driver_helper
    assert "new Set()" in driver_helper
    assert 'versions.join(" / ")' in driver_helper
    assert "gpuReportedVendor(gpu)" in driver_helper
    assert '"NVIDIA"' in driver_helper and '"AMD"' in driver_helper
    assert "厂商未确认" in driver_helper
    assert "Agent ${version}" in renderer
    assert "agent-bootstrap-version" in renderer
    assert "agent-gpu-driver-version" in renderer
    assert "bootstrap || gpuDriver" in renderer


def test_disk_usage_is_replaced_by_gpu_utilization_in_preview_table_and_drawer():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "async function saveAgentDetails", 1
    )[0]

    assert "GPU 利用率" in drawer
    assert "gpuUtilizationMetric(gpus)" in drawer
    assert "metrics.disk_percent" not in drawer
    assert ".gpu-utilization-list" in styles
    assert ".gpu-utilization-row > .gpu-temperature-badge" in styles
    assert "temperatureLabel" in app_js
    assert "gpu-table-oc" not in styles


def test_device_list_gpu_temperatures_use_accessible_threshold_badges():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    hashrate_helper = app_js.split("function gpuMiningHashrateSnapshot(agent, gpu)", 1)[1].split(
        "function gpuUtilizationTableMarkup", 1
    )[0]
    tile_selector = app_js.split("function gpuMiningHashrateTileReading(agent, gpu)", 1)[1].split(
        "function gpuUtilizationTableMarkup", 1
    )[0]
    model_helper = app_js.split("function gpuTileModelLabel(name, index)", 1)[1].split(
        "function gpuUtilizationTableMarkup", 1
    )[0]
    renderer = app_js.split("function gpuUtilizationTableMarkup(agent)", 1)[1].split(
        "const configurationRefreshDismissKey", 1
    )[0]

    assert "miningStatusForAgent(agent)" in hashrate_helper
    assert 'agent?.status === "online"' in hashrate_helper
    assert '["running", "active", "degraded", "running_zero_hashrate", "running_partial_hashrate"].includes(miningDisplayState(status, agent))' in hashrate_helper
    assert "miningHashrateDetailIsFresh(status, detail, agent)" in hashrate_helper
    assert "status.hashrate_details" in hashrate_helper
    assert "detail.gpu_hashrates" in hashrate_helper
    assert "readingIndex !== index" in hashrate_helper
    assert "[detail.coin, detail.algorithm]" in hashrate_helper
    assert "roleLabel" not in hashrate_helper
    assert "formatHashrateAmount(" in hashrate_helper
    assert '["srbminer", "pearlminer"].includes(minerType)' in hashrate_helper
    assert "minerType ? 0 : 1" in hashrate_helper
    assert '["randomx", "rx", "rx/0", "rx0"].includes(algorithm)' in hashrate_helper
    assert "readingTimestamp(reading)" in hashrate_helper
    assert "gpuMiningHashrateSnapshot(agent, gpu).readings.reduce" in tile_selector
    assert "reading.authority > selected.authority" in tile_selector
    assert "reading.observedAt > selected.observedAt" in tile_selector
    assert "reading.sequence > selected.sequence" in tile_selector
    assert "Number.isFinite(temperature)" in renderer
    assert '.replace(/^RTX\\s+/i, "")' in model_helper
    assert "`${Math.round(temperature)}°C`" in renderer
    assert 'temperature > 75 ? "is-hot" : "is-normal"' in renderer
    assert '"is-missing"' in renderer
    assert 'class="gpu-temperature-badge ' in renderer
    assert "utilization_percent" not in renderer
    assert "utilizationLabel" not in renderer
    assert "gpu.fan_percent ?? gpu.fan_speed_percent ?? gpu.fan_speed" in renderer
    assert "GPU ${index" in renderer
    assert "型号未上报" in renderer
    assert "风扇 ${fanSpeedLabel}" in renderer
    assert "aria-label=" in renderer
    assert "gpuMiningHashrateTileReading(agent, gpu)" in renderer
    assert 'class="gpu-temperature-hashrates"' in renderer
    assert 'class="gpu-temperature-hashrate"' in renderer
    assert "gpuTileHashrateAmountMarkup(hashrate.amount)" in renderer
    amount_markup = app_js.split("function gpuTileHashrateAmountMarkup(amount)", 1)[1].split(
        "function gpuUtilizationTableMarkup", 1
    )[0]
    assert 'class="gpu-temperature-hashrate-unit"' in amount_markup
    assert "[kMGTPE]?H" in amount_markup
    assert "escapeHtml(parts[1])" in amount_markup
    assert "escapeHtml(parts[2])" in amount_markup
    assert "reading.role" not in renderer
    assert "hashrates.map" not in renderer
    assert "${hashrate.identity} ${hashrate.amount}" in renderer
    assert ".gpu-utilization-list { display: flex; flex-wrap: nowrap;" in styles
    assert "align-items: flex-start; gap: 4px;" in styles
    assert (
        ".gpu-utilization-row { display: inline-grid; width: 56px; "
        "flex: 0 0 56px;"
    ) in styles
    assert ".gpu-temperature-hashrates { display: grid; width: 100%;" in styles
    assert ".gpu-temperature-hashrate" in styles
    assert "<small>GPU " not in renderer
    assert 'class="usage"' not in renderer
    assert 'class="usage-line"' not in renderer

    assert "min-width: 34px; height: 22px; padding: 0 3px;" in styles
    assert ".gpu-temperature-hashrate { color: var(--lime); font-size: 8px; font-weight: 700;" in styles
    assert ".gpu-temperature-hashrate-unit { margin-left: 2px; font-size: 7px;" in styles
    assert ".gpu-temperature-badge.is-normal { color: var(--accent-ink); background: var(--gpu-normal); }" in styles
    assert ".gpu-temperature-badge.is-hot { color: #0a0d10; background: var(--amber); }" in styles
    assert ".gpu-temperature-badge.is-missing { color: var(--muted); background: transparent; }" in styles


def test_cpu_temperature_is_visible_without_fabricating_values_for_older_agents():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    temperature = app_js.split("function cpuTemperatureC(metrics)", 1)[1].split(
        "function cpuTemperatureLabel", 1
    )[0]
    row = app_js.split("function deviceTableRow(agent)", 1)[1].split(
        "function renderDevices", 1
    )[0]
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "async function saveAgentDetails", 1
    )[0]

    # 0 °C must only be rendered when an Agent explicitly reports it. Missing,
    # boolean, non-finite, and impossible sensor values are ignored.
    assert "cpu.temperature_c ?? source.cpu_temperature_c" in temperature
    assert 'value === null || value === undefined || value === ""' in temperature
    assert 'typeof value === "boolean"' in temperature
    assert "Number.isFinite(number) && number >= -40 && number <= 150" in temperature
    assert "usage(metrics.cpu_percent, cpuTemperatureLabel(metrics))" in row
    assert "cpuTemperatureLabel(metrics, \"温度未上报\")" in drawer
    assert "cpu-resource-summary" in drawer
    assert ".usage .usage-summary" in styles
    assert ".metric-box .cpu-resource-summary" in styles


def test_scrolling_data_pages_pin_controls_and_remove_redundant_banners():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )

    for banner in (
        "按单台设备灰度更新",
        "这里只保存公开收款地址",
        "上传文件，或从可信 HTTPS 地址导入",
        "支持单矿工、SRBMiner 内置双挖和第二独立矿工",
    ):
        assert banner not in html

    assert 'id="devicesPage" class="page pinned-page device-list-page"' in html
    assert 'id="walletsPage" class="page pinned-page resource-library-page"' in html
    assert 'id="minersPage" class="page pinned-page resource-library-page"' in html
    assert 'id="flightsPage" class="page pinned-page resource-library-page"' in html
    assert 'id="poolsPage" class="page pinned-page resource-library-page"' in html
    assert 'id="workloadPage" class="page workload-page"' in html
    assert 'id="activityPage"' not in html
    assert "pinned-scroll-panel agent-release-library-panel" in html
    assert html.count('class="panel activity-scroll-panel"') == 0
    assert 'class="agent-release-platform-filter"' in html

    update_history = html.split("agent-update-history-panel", 1)[1].split(
        "</section>", 1
    )[0]
    assert "pinned-scroll-panel" not in update_history

    assert "@media (min-width: 901px)" in styles
    assert ".pinned-page.active" in styles
    assert ".pinned-scroll-panel > .table-wrap" in styles
    assert "overscroll-behavior-inline: contain" in styles
    assert ".pinned-scroll-panel .device-table thead th" in styles
    assert "position: sticky" in styles
    assert "#flightsPage .pinned-scroll-panel > .flight-card-wrap { overflow: auto; }" in styles
    assert ".device-activity-list" in styles
    assert ".agent-release-platform-filter select { width: 148px; }" in styles
    assert ".device-list-page > .toolbar { align-items: stretch; flex-wrap: wrap;" in styles
    assert ".device-list-page > .toolbar { justify-content: flex-start; }" in styles
    assert ".device-toolbar-actions { display: flex;" in styles
    assert ".device-list-page > .toolbar .device-toolbar-actions { margin-left: auto; }" in styles
    assert "grid-template-columns: minmax(148px, 1fr) auto auto;" in styles
    assert ".agent-release-platform-filter { width: 148px; justify-self: end; justify-items: end; text-align: right; }" in styles
    assert 'table.closest(".table-wrap")?.classList.toggle("hidden", !visible);' in app_js


def test_data_pages_flow_naturally_below_the_desktop_breakpoint():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )

    # One adaptive layout: the fixed-viewport scroller chrome is a desktop-only
    # enhancement, and narrow viewports flow with the document instead of a
    # separate phone form with its own media-query driven JS.
    assert "--mobile-pinned-page-height" not in styles
    assert "#agentReleasesPage.active" not in styles
    assert "max-height: 560px;" not in styles
    assert "(orientation: landscape)" not in styles
    assert "pinned-page-content" not in styles
    assert "MOBILE_NAV_MEDIA_QUERY" not in app_js
    assert "setMobileSidebarOpen" not in app_js

    assert ".device-list-page > .toolbar .search-box { width: 100%; grid-column: 1 / -1; }" in styles
    assert ".device-list-page > .toolbar .device-toolbar-actions { width: 100%; grid-column: 1 / -1; margin: 0; }" in styles
    assert ".resource-library-page .resource-head" in styles
    assert "#flightsPage .pinned-scroll-panel > .flight-card-wrap { overflow: auto; }" in styles
    assert ".agent-release-head > .table-row-actions" in styles
    assert "grid-column: 1 / -1;" in styles
    assert "justify-self: end;" in styles

    assert "@media (max-width: 900px)," not in styles
    # The activity-list 10px variant retired with that dead panel; the
    # live flight form keeps the safe-area contract.
    assert "max(12px, env(safe-area-inset-bottom))" in styles
    assert "@media (min-width: 901px)" in styles
    assert "min-height: min(420px, calc(100dvh - 182px));" in styles
    assert ".app-shell { min-height: 100vh; min-height: 100svh; min-height: 100dvh;" in styles
    assert ".sidebar { height: 100vh; height: 100svh; height: 100dvh;" in styles
    assert "overflow-y: auto; overscroll-behavior: contain; background: var(--sidebar-bg);" in styles
    assert ".drawer { position: fixed;" in styles and "height: 100svh; height: 100dvh;" in styles
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert "overscroll-behavior: contain;" in styles
    assert "-webkit-overflow-scrolling: touch;" in styles


def test_agent_release_page_supports_explicit_single_device_updates():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'class="nav-item admin-only" data-page="agentReleases"' in html
    assert 'id="agentReleasesPage" class="page admin-only"' in html
    assert "按单台设备灰度更新" not in html
    assert "绝不默认批量更新" in html
    assert "一次只更新一台设备" in html
    assert "自动停止该设备正在运行的受管矿工" in html
    assert "确认停矿并只向上面这一台设备下发" in html
    assert "稳定回连后自动恢复原飞行表" in html
    assert "agent_update.v1" in html
    assert 'id="agentReleaseDialog"' in html
    assert 'id="agentReleaseForm"' in html
    assert 'id="agentReleaseFile" type="file"' in html
    assert 'id="agentReleasePlatform" required' in html
    assert '<option value="windows">Windows</option>' in html
    assert '<option value="linux">Linux / Ubuntu</option>' in html
    assert 'id="agentReleasePlatformFilter"' in html
    assert 'value="x86_64（x64）" readonly' in html
    assert 'id="agentReleaseArchiveLabel" value="ZIP（Windows 便携版）" readonly' in html
    assert "Linux x64 在线更新 TAR.GZ" in html
    assert 'id="agentUpdateDialog"' in html
    assert 'id="agentUpdateForm"' in html
    assert 'id="agentUpdateAgent" required' in html
    assert 'id="agentUpdateConfirm" type="checkbox" required' in html
    assert 'id="agentUpdateTableBody"' in html

    assert "agentReleases: []" in app_js
    assert 'state.user?.role === "admin"' in app_js
    assert 'request("agentReleases", "/api/agent-releases")' in app_js
    assert "state[result.key] = result.data" in app_js
    assert "renderAgentReleases();" in app_js
    assert "agentUpdates: []" in app_js
    assert 'request("agentUpdates", "/api/agent-updates?limit=100")' in app_js
    assert "renderAgentUpdates();" in app_js
    assert 'agent.agent_version || "未知"' in app_js
    assert 'const archiveType = release.platform === "linux" ? "application/gzip" : "application/zip"' in app_js
    assert 'headers: { Accept: archiveType, Authorization: `Bearer ${state.token}` }' in app_js
    assert 'method: "DELETE"' in app_js

    uploader = app_js.split("async function uploadAgentRelease()", 1)[1].split(
        "function syncMinerPackageSource", 1
    )[0]
    assert "const platform = $(\"#agentReleasePlatform\").value" in uploader
    assert "platform," in uploader
    assert 'architecture: "x86_64"' in uploader
    assert "file_name: file.name" in uploader
    assert 'headers: { "Content-Type": "application/octet-stream" }' in uploader
    assert "body: file" in uploader
    assert "timeoutMs: LARGE_UPLOAD_TIMEOUT_MS" in uploader
    assert "未向设备下发" in uploader
    assert 'expectedSuffix = platform === "linux" ? ".tar.gz" : ".zip"' in uploader
    assert '"agent_update.v1": "更新被控端"' in app_js
    assert "agentUpdateEligibility(agent, release)" in app_js
    assert "release.deployable !== true" in app_js
    assert "未报告 agent_update.v1；请在设备检查受保护安装目录、allow_agent_updates，并以管理员身份重新运行 Install-Autostart.bat" in app_js
    assert "未报告 agent_update.v1；请在设备手动安装最新版 Ubuntu 完整包一次" in app_js
    assert "function agentUpdateReadinessDetail(agent, platform)" in app_js
    assert "agent?.inventory?.agent_update_readiness" in app_js
    assert "acl_or_owner_mismatch" in app_js
    assert "受保护安装目录的 ACL 或所有者不安全" in app_js
    assert ".agent-release-table { min-width:" in styles
    assert ".agent-update-table { min-width:" in styles
    assert ".release-digest-full" in styles


def test_controller_static_assets_are_cache_busted_for_current_device_ui():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    _cache_busted_assets(html, ["/static/theme-init.js", "/static/styles.css", "/static/app.js"])


def test_refresh_animation_rotates_only_the_icon_and_keeps_the_button_frame_still():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert '<button id="refreshButton" class="icon-button" type="button" title="刷新" aria-label="刷新设备数据"><span class="refresh-icon" aria-hidden="true">↻</span></button>' in html
    icon_rule = styles.split(".refresh-icon {", 1)[1].split("}", 1)[0]
    animation_rule = styles.split("#refreshButton.spinning .refresh-icon {", 1)[1].split("}", 1)[0]
    assert "display: inline-grid" in icon_rule
    assert "place-items: center" in icon_rule
    assert "transform-origin: center" in icon_rule
    assert "animation: control-spin .7s linear infinite" in animation_rule
    assert ".spinning { animation:" not in styles
    assert '$("#refreshButton").classList.add("spinning")' in app_js
    assert '$("#refreshButton").classList.remove("spinning")' in app_js
    assert '$("#refreshButton").setAttribute("aria-busy", "true")' in app_js
    assert '$("#refreshButton").removeAttribute("aria-busy")' in app_js


def test_controller_theme_is_fixed_dark_and_retains_css_driven_chart_colors():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    theme_init_js = (ROOT / "server" / "static" / "theme-init.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    head = html.split("</head>", 1)[0]

    assert 'id="themeButton"' not in html
    assert 'data-theme-toggle' not in html
    assert 'login-theme-button' not in html
    _cache_busted_assets(head, ["/static/theme-init.js", "/static/styles.css"])
    assert head.index('src="/static/theme-init.js') < head.index('href="/static/styles.css')
    assert 'document.documentElement.dataset.theme = "dark"' in theme_init_js
    assert 'document.documentElement.style.colorScheme = "dark"' in theme_init_js
    assert 'localStorage' not in theme_init_js
    assert 'matchMedia' not in theme_init_js
    for forbidden in ('THEME_STORAGE_KEY', 'THEME_PREFERENCES', 'applyThemePreference',
                      'cycleThemePreference', 'initializeTheme', 'data-theme-toggle'):
        assert forbidden not in app_js
    assert ':root[data-theme="light"]' not in styles
    assert ':root[data-theme="system"]' not in styles
    assert 'prefers-color-scheme' not in styles
    assert '.theme-button' not in styles
    assert 'window.getComputedStyle(document.documentElement)' in app_js
    assert app_js.index("function themeColor(name, fallback)") < app_js.index("function hashrateChartColors()")
    for variable in ("--chart-grid", "--chart-muted", "--chart-cpu", "--chart-memory",
                     "--chart-series-1", "--chart-series-8"):
        assert variable in app_js
    assert 'renderHashrateHistory();' in app_js
    assert 'drawTelemetry(state.selectedAgentTelemetry)' in app_js



def test_dashboard_fetches_and_renders_bounded_24_hour_hashrate_series():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="hashrateHistoryChart"' in html
    assert "24 小时币种算力" in html
    assert 'request("hashrateHistory", "/api/hashrate-history?hours=24&bucket_minutes=5")' in app_js
    assert "Date.now() - state.hashrateHistoryFetchedAt >= 60000" in app_js
    renderer = app_js.split("function renderHashrateHistory()", 1)[1].split(
        "function fleetRow", 1
    )[0]
    assert "document.series" in renderer
    assert "entry.item.unit || \"H/s\"" in renderer
    assert "formatHashrateAmount" in renderer
    assert "过去 24 小时暂无算力数据" in renderer
    assert "算力历史加载失败" in renderer
    assert 'class="hashrate-history-line"' in renderer
    assert "hashrateHistorySegments" in renderer
    assert 'class="hashrate-series-card"' in renderer
    assert 'class="hashrate-history-point"' in renderer
    assert "function hashrateHistorySeriesLabel(series)" in app_js
    series_label = app_js.split("function hashrateHistorySeriesLabel(series)", 1)[1].split(
        "function hashrateHistoryPoints", 1
    )[0]
    assert "series?.role" not in series_label
    assert 'querySelectorAll(".hashrate-history-svg")' in renderer
    assert 'svg.addEventListener("pointermove", pointer)' in renderer
    assert "new Date(point.timestamp).toLocaleString" in renderer
    assert 'formatHashrateAmount(point.value, entry.item.unit || "H/s")' in renderer
    assert 'class="hashrate-history-tooltip"' in renderer
    assert ".hashrate-series-grid" in styles
    assert ".hashrate-history-line" in styles
    assert ".hashrate-history-tooltip" in styles
    assert ".hashrate-history-hover-line" in styles


def test_workload_is_one_visible_page_and_fetches_on_entry_or_when_due():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert '<section id="workloadPage" class="page workload-page">' in html
    assert '<section id="workloadPanel" class="panel workload-panel">' in html
    assert html.count('id="workloadRangeForm"') == 1
    assert html.index('id="workloadPage"') < html.index('id="workloadPanel"')
    assert 'id="workloadPanel"' not in html.split('id="dashboardPage"', 1)[1].split('id="devicesPage"', 1)[0]
    assert 'id="workloadRangeForm"' in html
    assert 'id="workloadFrom" type="datetime-local"' in html
    assert 'id="workloadTo" type="datetime-local"' in html
    assert 'id="workloadAgent"' in html
    assert 'id="workloadAlgorithm"' in html
    assert 'data-workload-preset="24"' in html
    assert 'data-workload-preset="168"' in html
    assert 'data-workload-preset="336"' in html
    assert "最长可查询 14 天" in html
    assert "估算工作量/算力积分（H）" in html
    assert "不等同于矿池难度确认" in html

    refresh_all = app_js.split("async function refreshAll(silent = false)", 1)[1].split(
        "function renderAll()", 1
    )[0]
    assert "/api/workload" not in refresh_all
    assert "state.workload" not in refresh_all

    range_builder = app_js.split("function workloadRangeForRequest()", 1)[1].split(
        "function renderWorkloadFilterOptions()", 1
    )[0]
    assert "WORKLOAD_MAX_RANGE_MS" in range_builder
    assert 'include_points: "false"' in range_builder
    assert 'query.set("agent_id", agentId)' in range_builder
    assert "`/api/workload?${query.toString()}`" in range_builder

    refresh_gate = app_js.split("function workloadRefreshDue()", 1)[1].split(
        "function settlementLocalValue", 1
    )[0]
    assert 'state.page === "workload"' in refresh_gate
    assert '!state.workloadLoading' in refresh_gate
    assert 'Date.now() - state.workloadLastRequestAt >= 60000' in refresh_gate
    assert 'if (workloadRefreshDue()) refreshWorkloadSummary();' in app_js
    assert 'if (shouldFetch && state.page === "workload") refreshWorkloadSummary();' in app_js
    assert "setWorkloadPreset(Number(button.dataset.workloadPreset), true)" in app_js
    assert '["#workloadFrom", "#workloadTo"]' in app_js
    assert 'button.classList.remove("active")' in app_js
    assert 'addEventListener("submit"' in app_js

    assert ".workload-panel" in styles
    assert ".workload-range-form" in styles
    assert ".workload-device-row" in styles
    assert ".workload-measure" in styles
    assert ".workload-percentage" in styles
    assert ".workload-basis.is-estimated" in styles





def test_device_version_update_hint_opens_existing_dialog_with_preselected_device():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert "function latestCompatibleAgentRelease(agent)" in app_js
    assert "release?.deployable !== true" in app_js
    assert 'release?.update_protocol !== "agent_update.v1"' in app_js
    assert 'class="text-button agent-update-needed"' in app_js
    assert "agentUpdateEligibility(agent, release)" in app_js
    assert "需修复本地更新能力" in app_js
    assert "需安装完整包" in app_js
    assert "Linux Agent 0.6.6 或更早版本可能仍含旧更新桥；请手动安装 0.6.8 完整包一次" in app_js
    assert 'compareAgentVersions(agent.agent_version, "0.6.7")' in app_js
    assert "agent-update-needed is-disabled" in app_js
    assert "需要更新至 " in app_js
    assert "openAgentUpdateDialog(button.dataset.agentUpdateRelease, button.dataset.agentUpdateAgent)" in app_js
    assert 'function openAgentUpdateDialog(releaseId, preselectedAgentId = "")' in app_js
    dialog = app_js.split('function openAgentUpdateDialog(releaseId, preselectedAgentId = "")', 1)[1].split(
        "function syncAgentUpdateSelection", 1
    )[0]
    assert 'agent.id === preselectedAgentId ? "selected" : ""' in dialog
    assert "syncAgentUpdateSelection();" in dialog


def test_device_logs_replace_global_history_page_without_losing_update_cleanup():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="activityPage"' not in html
    assert 'id="clearOperationsHistoryButton"' not in html
    assert 'id="clearAgentUpdateHistoryButton"' in html
    assert "admin-only" in html.split('id="clearAgentUpdateHistoryButton"', 1)[1].split(">", 1)[0]
    assert '/api/commands?agent_id=${query}&limit=100&include_all_active=true' in app_js
    assert '/api/audit?agent_id=${query}&limit=100' in app_js
    assert 'entry?.related_agent_id === agentId' in app_js
    function_body = app_js.split("async function clearAgentUpdateHistory()", 1)[1].split("\n}\n", 1)[0]
    assert "window.confirm" in function_body
    assert 'api("/api/agent-updates/history", { method: "DELETE" })' in function_body
    assert "await refreshAll(true);" in function_body


def test_disabled_users_are_defensively_hidden_from_normal_user_list():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    renderer = app_js.split("function renderUsers()", 1)[1].split(
        "async function deleteUser", 1
    )[0]

    assert "state.users.filter((user) => user?.is_active !== false)" in renderer
    assert "已停用" not in renderer


def test_legacy_running_status_requires_fresh_local_api_statistics():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    resolver = app_js.split("function miningDisplayState(status, agent = null)", 1)[1].split(
        "const HASHRATE_UNITS", 1
    )[0]

    assert "status?.process_verified === false" in resolver
    assert "status?.process_error" in resolver
    assert "status?.error" in resolver
    assert "status?.degraded === true" in resolver
    assert 'running_roles' in resolver
    assert 'if (degraded) return "degraded"' in resolver
    assert 'return "running_zero_hashrate"' in resolver
    assert 'return "running_partial_hashrate"' in resolver
    assert 'return "unconfirmed"' in resolver
    assert 'return "stale"' in resolver
    assert "status?.stats_updated_at" in resolver
    assert "checkedAt - statsAt <= 30000" in resolver
    assert 'unconfirmed: "状态异常/未确认"' in app_js
    assert 'stale: "状态过期/进程已不存在"' in app_js
    assert 'offline: "设备离线/状态未确认"' in app_js
    assert 'degraded: "部分矿工运行"' in app_js
    assert '["running", "active", "degraded", "running_zero_hashrate", "running_partial_hashrate"].includes(stateValue)' in app_js
    assert 'const isOnline = agent.status === "online"' in app_js
    assert "const confirmedRunning = isOnline" in app_js
    assert "const controllerHeartbeatAt = Date.parse(agent?.last_seen)" in resolver
    assert 'agent?.status === "online"' in resolver
    assert "controllerHeartbeatIsFresh" in resolver
    assert "Math.abs(Date.now() - controllerHeartbeatAt) <= 120000" in resolver
    assert "Math.abs(controllerHeartbeatAt - checkedAt) <= 60000" in resolver
    assert 'const supervision = agentSupervisionLabel(agent, inventory);' in app_js
    assert 'inventory.agent_supervision' in app_js
    assert '未上报监管信息' in app_js
    assert '旧版 Agent 未上报监管能力' not in app_js
    assert 'systemd Watchdog' in app_js
    assert 'status.service_name' not in app_js.split(
        "function miningStatusMarkup(agent)", 1
    )[1].split("async function refreshAgentStatus", 1)[0]
    assert "设备当前离线；下列配置来自最后一次心跳" in app_js
    assert "status.error," in app_js
    assert "status.process_error," in app_js
    assert "const miningState = miningDisplayState(status, agent);" in app_js
    assert "statsAt >= bootAt" in resolver

def test_controller_status_panel_version_and_dashboard_order_are_explicit():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="controllerVersion"' in html
    assert 'id="controllerStatusGrid"' in html
    assert 'id="controllerStatusMeta"' in html
    assert html.index('class="summary-grid overview-summary-grid"') < html.index('class="panel hashrate-history-panel"') < html.index('class="panel overview-device-panel"')
    assert html.index('id="accessPage"') < html.index('class="panel controller-status-panel"')
    assert "<h2>设备状态</h2>" not in html
    assert "<h2>机群健康度</h2>" not in html
    assert "<h2>最近任务</h2>" not in html
    assert 'id="fleetPreview"' not in html
    assert 'id="healthRing"' not in html
    assert 'id="recentCommands"' not in html
    assert 'versionElement.textContent = `控制端 v${version}`' in app_js
    assert 'network.receive_bytes_per_second' in app_js
    assert 'network.send_bytes_per_second' in app_js
    assert 'grid-template-columns: repeat(6, minmax(0, 1fr))' in styles
    assert 'grid-template-columns: repeat(3, minmax(0, 1fr))' in styles
    assert 'grid-template-columns: repeat(2, minmax(0, 1fr))' in styles


def test_device_gpu_oc_hides_unconfirmed_nvml_controls_and_shows_active_source():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    matcher = app_js.split("function gpuOcSettingMatches", 1)[1].split(
        "function gpuActiveOcMarkup", 1
    )[0]

    for field in (
        "power_limit_w",
        "core_clock_offset_mhz",
        "core_clock_lock_mhz",
        "memory_clock_offset_mhz",
        "memory_clock_lock_mhz",
        "fan_percent",
    ):
        assert f'data-device-gpu-control="{field}"' in html
    assert "gpu.oc_capabilities.filter" in app_js
    assert 'label.classList.toggle("hidden", !supported)' in app_js
    assert 'source === "srbminer_cli" ? "SRBMiner 参数声明"' in app_js
    assert 'source === "nvml" ? "NVML 最近成功下发"' in app_js
    assert "gpuOcClockObservation" not in app_js
    assert "采样观测" not in app_js
    assert "graphics_clock_mhz" in app_js
    assert "memory_clock_mhz" in app_js
    assert "NVML 选择最接近的硬件频率" in html
    assert "保护降频仍优先" in html
    assert "status?.gpu_oc_profiles" in app_js
    assert 'profileState !== "declared"' in app_js
    assert 'profileState !== "applied_record"' in app_js
    assert '["running", "active", "starting"].includes(miningState)' in app_js
    assert "gpuActiveOcMarkup(agent, gpu)" in app_js
    assert "function gpuStandaloneOcSetting(agent, gpu)" in app_js
    assert "const currentSetting = gpuStandaloneOcSetting(agent, gpu);" in app_js
    assert "currentSetting?.[field]" in app_js
    assert "function fillDeviceGpuOcControls(form, target, gpu, currentSetting)" in app_js
    assert "清空已填字段会恢复该项默认" in app_js
    assert "else if (target.initialSetting?.[field] != null) setting[field] = null;" in app_js
    assert "gpu.fan_min_percent" in app_js
    assert "gpu.fan_max_percent" in app_js
    assert "const settingUuid = gpuMetricUuid(setting);" in matcher
    assert "const currentUuid = gpuMetricUuid(gpu);" in matcher
    assert "if (settingUuid || profile?.identity_complete === true)" in matcher
    assert (
        "return Boolean(settingUuid && currentUuid && settingUuid === currentUuid);"
        in matcher
    )
    assert matcher.index("profile?.identity_complete === true") < matcher.index(
        "return Number(setting?.index) === index;"
    )
    assert "停止挖矿或删除已保存 OC 后恢复驱动默认策略" in html


def test_flight_sheet_list_shows_configured_device_count_and_auto_reapply_feedback():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert '<th>使用设备</th>' in html
    assert 'Number(sheet.configured_device_count ?? sheet.device_count) || 0' in app_js
    assert 'class="table-subline">已配置设备</small>' in app_js
    assert 'class="flight-sheet-card ${secondary ? "has-secondary" : "single-miner"}"' in app_js
    assert 'class="flight-card-devices" data-label="使用设备"' in app_js
    assert 'class="flight-card-actions"' in app_js
    assert 'configurationSaveMessage("钱包", result.reapply)' in app_js
    assert 'configurationSaveMessage("飞行表", result.reapply)' in app_js
    assert "Number(reapply.marked)" in app_js
    assert "Number(reapply.pending)" in app_js
    assert '已下发 ${queued} 台' in app_js
    assert '${pending} 台待设备上线并开始挖矿后下发' in app_js


def test_device_configuration_refresh_markers_cover_pending_progress_and_failure():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    badge = app_js.split("function configurationRefreshBadge", 1)[1].split(
        "function deviceTableRow", 1
    )[0]
    row = app_js.split("function deviceTableRow(agent)", 1)[1].split(
        "function renderDevices", 1
    )[0]
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]
    assert 'pending: ["配置待更新", "pending"]' in badge
    assert 'preparing: ["配置下发中", "progress"]' in badge
    assert 'queued: ["配置下发中", "progress"]' in badge
    assert 'succeeded: ["配置已更新", "success"]' in badge
    assert 'failed: ["配置更新失败", "failed"]' in badge
    assert "deviceStatusCellMarkup(agent, true)" in row
    assert "showConfigurationRefresh ? configurationRefreshBadge(agent)" in badge
    assert "configurationRefreshBadge(agent, true)" in drawer
    assert ".configuration-refresh-badge.failed" in styles
    assert 'data-dismiss-configuration-refresh=' in badge
    assert "configurationRefreshIsDismissed(agent)" in badge
    assert "bindConfigurationRefreshDismiss" in app_js
    assert 'class="device-toolbar-actions"' in html
    assert 'id="clearConfigurationRefreshBadgesButton"' in html
    bulk = app_js.split("function dismissAllConfigurationRefreshes()", 1)[1].split(
        "function bindConfigurationRefreshDismiss", 1
    )[0]
    candidates = app_js.split(
        "function dismissibleConfigurationRefreshAgents()", 1
    )[1].split("function syncConfigurationRefreshClearButton", 1)[0]
    assert "state.agents.filter" in candidates
    assert 'toLowerCase() === "succeeded"' in candidates
    assert "configurationRefreshSignature(refresh)" in candidates
    assert "persistConfigurationRefreshDismissals(dismissed);" in bulk
    assert "renderDevices();" in bulk
    assert "renderDrawer(state.selectedAgent);" in bulk
    assert "api(" not in bulk
    assert '$("#clearConfigurationRefreshBadgesButton").addEventListener("click", dismissAllConfigurationRefreshes);' in app_js


def test_historical_agent_release_notes_are_presented_in_chinese():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert "Linux 0.6.8：修复独立更新器就绪检测" in app_js
    assert "Agent 0.6.7：全设备 IP 上报" in app_js
    assert "agentReleaseNotesLabel(release.notes)" in app_js


def test_device_drawer_shows_resource_identity_and_remove_flight():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")

    assert "inventory.cpu_model" in app_js
    assert "inventory.processor" in app_js
    assert "memory.total_bytes" in app_js
    assert 'class="metric-box resource-identity"' in app_js
    # Web terminal / file workspace UI was removed in 0.8.48.
    assert "openRemoteWorkspace" not in app_js
    assert "linuxRemoteAccessMarkup" not in app_js
    assert "localWebWorkspaceUrl" not in app_js
    assert 'data-open-remote-workspace' not in app_js
    assert 'id="localConnectionDialog"' not in index_html
    assert 'id="remoteSessionDialog"' not in index_html
    assert "本地 WebShell" not in index_html
    assert 'data-agent-action="remove_flight_sheet"' in app_js
    assert 'agentVersionAtLeast(agent.agent_version, [0, 6, 1])' in app_js
    assert '["reboot", "shutdown", "remove_flight_sheet"].includes(action)' in app_js
    assert 'body: { action, payload }' in app_js



def test_device_list_hides_network_column_but_drawer_keeps_observed_and_lan_ip_addresses():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    row = app_js.split("function deviceTableRow(agent)", 1)[1].split(
        "function renderDevices", 1
    )[0]
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]
    assert "function deviceNetworkMarkup(agent, detailed = false)" in app_js
    assert "agent.public_ip" in app_js
    assert "agent.local_ip_addresses" in app_js
    assert "deviceNetworkMarkup(agent)" not in row
    assert "deviceNetworkMarkup(agent, true)" in drawer
    assert "<th>网络</th>" not in index_html
    assert ".device-network.detailed" in styles


def test_device_gpu_single_row_uses_evidence_backed_memory_identity_fallbacks():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    hardware = app_js.split("function deviceDetailGpuRow(", 1)[1].split(
        "function deviceFlightSummaryMarkup", 1
    )[0]
    board = app_js.split("function gpuBoardLabel(gpu)", 1)[1].split(
        "function gpuMiningHashrateMarkup", 1
    )[0]
    identity = app_js.split("function gpuMemoryIdentityParts(gpu)", 1)[1].split(
        "function gpuBoardLabel", 1
    )[0]
    assert "memory_total_mib" in app_js
    assert "board_vendor" in board
    dataset = json.loads((ROOT / "vendor" / "pciids" / "gpu-identities.json").read_text(encoding="utf-8"))
    pci_script = (ROOT / "server" / "static" / "pci-identity-data.js").read_text(encoding="utf-8")
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    assert dataset["vendors"] and set(dataset["brand_aliases"]).issubset(dataset["vendors"])
    assert "global.RigPilotPciIdentity" in pci_script
    assert "globalThis.RigPilotPciIdentity" in board
    assert ".normalizeId(" in board and ".boardVendorLabel(" in board
    assert "gpuBoardVendors" not in app_js
    assert html.index('/static/pci-identity-data.js') < html.index('/static/app.js')
    assert "未知品牌 (ID ${boardId.toUpperCase()})" in board
    assert "gpuTextValue(gpu?.board_vendor_id)" not in board
    assert "memory_vendor" in identity
    assert "memory_type" in identity
    assert "gpuMemoryIdentityParts(gpu)" in hardware
    assert "gpuBoardLabel(gpu)" in hardware
    assert "device-gpu-hardware-band" not in hardware
    assert "device-gpu-detail-row" not in hardware
    for cell in ("model", "hashrate", "thermals", "utilization", "clocks", "power", "identity", "oc", "status"):
        assert f'class="device-gpu-cell-{cell}"' in hardware
    assert 'class="device-gpu-identifier" title="${escapeHtml(value)}" aria-label="${escapeHtml(value)}"' in hardware
    assert "function gpuHardwareMarkup" not in app_js
    assert "function gpuCardMarkup" not in app_js
    assert "gpuVerifiedMemoryIdentities" not in app_js
    assert '"rtx 5060 ti": "GDDR7"' in app_js
    assert '"rtx 5080": "GDDR7"' in app_js
    assert "`${memoryType}（颗粒厂商未上报）`" in identity
    assert "Linux NVIDIA 驱动未提供显存颗粒" not in hardware
    assert '"显存颗粒未上报"' in app_js
    assert "vbios_version" in hardware
    assert "power_min_limit_w" in app_js
    assert "power_default_limit_w" in app_js
    assert "power_max_limit_w" in app_js
    assert 'class="gpu-hardware"' in app_js
    assert "graphics_clock_mhz" in app_js
    assert "memory_clock_mhz" in app_js
    assert "核心频率" in app_js
    assert "显存频率" in app_js
    assert ".gpu-hardware" in styles


def test_driver_unavailable_gpu_is_not_rendered_as_zero_or_offered_oc():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    metric_renderer = app_js.split("function metric(value, suffix = \"%\")", 1)[1].split(
        "function clampPercent", 1
    )[0]
    table_renderer = app_js.split("function gpuUtilizationTableMarkup(agent)", 1)[1].split(
        "const configurationRefreshDismissKey", 1
    )[0]
    gpu_renderer = app_js.split("function deviceDetailGpuRow(", 1)[1].split(
        "function deviceFlightSummaryMarkup", 1
    )[0]
    oc_button = app_js.split("function gpuOcButtonMarkup(", 1)[1].split(
        "function gpuMissingDesiredOcMarkup", 1
    )[0]
    utilization = app_js.split("function averageGpuUtilization(gpus)", 1)[1].split(
        "function gpuUtilizationMetric", 1
    )[0]

    assert 'value === null || value === undefined || value === ""' in metric_renderer
    assert "gpu?.driver_available !== false" in utilization
    assert "function gpuDriverUnavailable(gpu)" in app_js
    assert "gpuDriverUnavailable(gpu)" in table_renderer
    assert "驱动异常" in table_renderer
    assert "gpu.driver_available === false" in gpu_renderer
    assert 'driverUnavailable ? null : gpuNumberValue(gpu?.power_limit_w)' in gpu_renderer
    assert "Above 4G Decoding" in gpu_renderer
    assert "gpu.driver_available === false" in oc_button
    assert "gpuOcButtonMarkup(agent, gpu, canOperate)" in gpu_renderer
    assert ".gpu-temperature-badge.is-error" in styles
    assert ".gpu-card.driver-error" in styles


def test_activity_translates_known_agent_errors_and_keeps_original_diagnostics():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    formatter = app_js.split("function commandErrorMessage(command)", 1)[1].split(
        "function metric", 1
    )[0]

    assert "previous managed miner exited or restarted itself" in formatter
    assert "无法唯一确认替代进程" in formatter
    assert "previous managed miner API is still listening" in formatter
    assert "旧矿工的本地 API 仍在监听" in formatter
    assert "identity became ambiguous during orphan confirmation" in formatter
    assert "SRBMiner 没有报告任何可用的 GPU" in formatter
    assert "temporary SRBMiner GPU OC reset process did not become ready:" in formatter
    assert "SRBMiner 临时 OC 复位进程未能完成就绪确认" in formatter
    assert "原始错误：${error}" in formatter
    assert "return translated ?" in formatter


def test_device_ui_no_longer_exposes_legacy_tags_or_grouping():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    device_filter = app_js.split("function filteredAgents()", 1)[1].split(
        "function miningStatusForAgent", 1
    )[0]
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]

    assert "agent.tags" not in device_filter
    assert "<h4>标签</h4>" not in drawer
    assert "未分组" not in drawer
    assert "系统或标签" not in html
    assert "系统或网络地址" in html
    assert ".tag-list" not in styles


def test_device_detail_gpu_rows_show_only_real_indexed_miner_hashrates():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    helper = app_js.split("function gpuMiningHashrateSnapshot(agent, gpu)", 1)[1].split(
        "function gpuUtilizationTableMarkup", 1
    )[0]
    renderer = app_js.split("function gpuMiningHashrateMarkup(agent, gpu)", 1)[1].split(
        "function gpuOcButtonMarkup", 1
    )[0]
    gpu_row = app_js.split("function deviceDetailGpuRow(", 1)[1].split(
        "function deviceFlightSummaryMarkup", 1
    )[0]
    assert "status.hashrate_details" in helper
    assert "detail.gpu_hashrates" in helper
    assert "readingIndex !== index" in helper
    assert 'agent?.status === "online"' in helper
    assert '["running", "active", "degraded", "running_zero_hashrate", "running_partial_hashrate"].includes(miningDisplayState(status, agent))' in helper
    assert "miningHashrateDetailIsFresh(status, detail, agent)" in helper
    assert "formatHashrateAmount(" in helper
    assert "[detail.coin, detail.algorithm]" in helper
    assert "roleLabel" not in helper
    assert "gpuMiningHashrateSnapshot(agent, gpu)" in renderer
    assert "snapshot.status.gpu_hashrate_status" in renderer
    assert "矿工显卡身份与 NVML 未匹配" in renderer
    assert "SRBMiner 显卡身份与 NVML 未匹配" not in renderer
    assert "XMRig 当前仅上报 CPU 总算力" in renderer
    assert "gpuMiningHashrateMarkup(agent, gpu)" in gpu_row
    assert "gpuMiningHashrateTileReading(agent, gpu)" in app_js.split(
        "function gpuUtilizationTableMarkup(agent)", 1
    )[1].split("const configurationRefreshDismissKey", 1)[0]
    assert ".gpu-mining-hashrate-row" in styles
    assert ".gpu-mining-hashrate-unavailable" in styles





def test_device_details_and_controller_remove_wifi_management():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    main_py = (ROOT / "server" / "main.py").read_text(encoding="utf-8")

    for removed_ui_token in (
        "function wifiManagementMarkup",
        "function setAgentWifiStatus",
        "function waitForWifiOperation",
        "function populateWifiNetworks",
        "function scanAgentWifi",
        "function connectAgentWifi",
        "function disconnectAgentWifi",
        "device-detail-wifi",
        "agentWifiPanel",
        "data-wifi-scan",
    ):
        assert removed_ui_token not in app_js
    assert ".wifi-management" not in styles
    assert ".wifi-connect-form" not in styles

    assert 'wifi_actions' not in main_py
    assert '/wifi' not in main_py
    assert not (ROOT / "server" / "wifi_actions.py").exists()




def test_controller_status_treats_first_cpu_sample_as_unavailable():
    app = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    assert "value != null && Number.isFinite(numeric)" in app


def test_controller_release_requires_system_metrics_module():
    deploy = (ROOT / "deploy" / "deploy-controller-release.sh").read_text(
        encoding="utf-8"
    )
    required = (
        "__init__.py",
        "auth.py",
        "config.py",
        "database.py",
        "main.py",
        "managed_miners.py",
        "requirements.txt",
        "schemas.py",
        "system_metrics.py",
    )
    for name in required:
        assert f'"server/{name}"' in deploy
    assert '"server/static/app.js"' in deploy
    assert '"server/static/index.html"' in deploy
    assert '"server/static/styles.css"' in deploy
    assert "set(declared) != allowed_files" in deploy
    assert "regular_files != allowed_files | {manifest_name}" in deploy


def test_controller_api_timeout_refresh_isolation_and_drawer_telemetry_are_explicit():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    api_body = app_js.split("async function api(path, options = {})", 1)[1].split(
        "function toast", 1
    )[0]
    refresh = app_js.split("async function refreshAll(silent = false)", 1)[1].split(
        "function renderAll", 1
    )[0]
    assert "const API_TIMEOUT_MS = 20_000" in app_js
    assert "const controller = new AbortController()" in api_body
    assert "signal: controller.signal" in api_body
    assert "requestOptions.timeoutMs" in api_body
    # Miner, Agent, controller and remote-workspace file uploads all use the
    # bounded large-transfer timeout rather than the default API timeout.
    # HiveOS quick creation and package-only updates download before returning.
    assert "/api/agent-releases/upload?" in app_js
    assert "/api/miner-packages/upload?" in app_js
    miner_import = app_js.split('await api("/api/miner-packages/import"', 1)[1].split(
        ");", 1
    )[0]
    assert "timeoutMs: LARGE_UPLOAD_TIMEOUT_MS" in miner_import
    assert "state.token === tokenAtRequest" in api_body
    assert "state.authEpoch += 1" in app_js
    assert "state.refreshController?.abort()" in app_js
    assert "const settled = (key, promise)" in refresh
    assert 'request("agents", "/api/agents")' in refresh
    assert 'request("summary", "/api/summary")' in refresh
    assert "state[result.key] = result.data" in refresh
    assert "refreshId === state.refreshId" in refresh
    assert "authEpoch === state.authEpoch" in refresh
    assert '"selectedAgentTelemetry"' in refresh
    assert "refreshSelectedAgentDrawer(true)" in refresh
    assert "state.selectedAgentTelemetry" in app_js
    assert 'state.selectedAgent?.id !== agentId || state.deviceDetailTab !== "overview"' in app_js


def test_miner_package_test_classification_is_generic_and_keeps_custom_upload():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    metadata = app_js.split("function minerPackageManagedMetadata()", 1)[1].split(
        "function syncMinerPackageClassification", 1
    )[0]
    upload = app_js.split("async function createMinerPackage()", 1)[1].split(
        "function walletOptions()", 1
    )[0]
    renderer = app_js.split("function renderMinerPackages()", 1)[1].split(
        "function agentReleaseTargetLabel", 1
    )[0]

    assert 'id="minerPackageManagedType"' in html
    assert '<option value="">未归类（普通自定义包）</option>' in html
    assert '<option value="xmrig">XMRig</option>' in html
    assert '<option value="srbminer">SRBMiner-MULTI</option>' in html
    assert '<option value="pearlminer">Pearl Miner</option>' in html
    assert 'id="minerPackageManagedOsFamily"' in html
    assert '<option value="windows">Windows x64</option>' in html
    assert '<option value="linux">Linux x64</option>' in html
    assert 'id="minerPackageManagedVariant"' in html
    assert '<option value="nvidia">NVIDIA</option>' in html
    assert '<option value="amd">AMD</option>' in html
    assert "归类包固定登记为测试版" in html
    assert "minerPackageControlledPearlBeta" not in html
    package_form = re.search(r'<form[^>]*id="minerPackageForm".*?</form>', html, re.DOTALL)
    assert package_form is not None
    # Registry metadata may retain historical asset identities; the upload
    # form and classifier must not contain a bespoke Pearl beta/version gate.
    assert "2.0.3" not in package_form.group(0)

    assert 'if (!managedType) return null' in metadata
    assert 'managed_miner_type: managedType' in metadata
    assert 'managed_os_family: managedOsFamily' in metadata
    assert 'managed_channel: "test",' in metadata
    assert 'metadata.managed_variant = $("#minerPackageManagedVariant").value' in metadata
    assert 'minerPackageTypeLabel(metadata.managed_miner_type)' in app_js
    assert 'new URLSearchParams({ name, version, archive_type: archiveType, note, filename: file.name })' in upload
    assert 'Object.entries(metadata || {}).forEach(([key, value]) => query.set(key, value))' in upload
    assert '...(metadata || {})' in upload
    assert 'await api("/api/miner-packages/import"' in upload
    assert "2.0.3" not in upload
    assert "managed_miner_type" in renderer
    assert "managed_os_family" in renderer
    assert "managed_variant" in renderer
    assert "managed_channel" in renderer
    assert "未归类" in renderer
    assert "测试版" in renderer

def test_srb_dual_gpu_order_matches_backend():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    comparator = app_js.split("function sameGpuIndices(left, right)", 1)[1].split(
        "function validateSrbDualFlight", 1
    )[0]

    assert "index === right[position]" in comparator
    assert "new Set(right)" not in comparator
    assert "相同顺序的 GPU 索引" in app_js



def test_pearl_miner_flight_ui_supports_proxy_and_versioned_direct_stratum():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    sync = app_js.split("function syncManagedLeg(prefix", 1)[1].split(
        "function setManagedExtra", 1
    )[0]
    builder = app_js.split("function buildManagedLeg(prefix", 1)[1].split(
        "async function saveFlightSheet", 1
    )[0]
    assert '<option value="pearlminer">Pearl Miner（PEARL / GPU）</option>' in html
    assert 'id="flightGpuBackend"' in html
    assert 'id="flightCudaVariant"' in html
    assert "Auto（按设备自动选择）" in html
    assert 'value="nvidia"' in html
    assert 'value="amd"' in html
    assert 'value="cuda12"' in html
    assert 'value="cuda13"' in html
    assert "pearl://global.pearlfortune.org:443" in sync
    assert "pearl://jp.pearlfortune.org:443" in sync
    assert "stratum+tcp://host:port" in sync
    assert "stratum+ssl://host:port" in sync
    assert sync.count("也可直接填写") == 2
    assert "function pearlMinerPoolUrlSupported(value)" in app_js
    assert "/^(?:pearl|stratum\\+(?:tcp|ssl)):\\/\\//i" in app_js
    assert app_js.count("pearlMinerPoolUrlSupported(pool.value)") == 2
    assert "function pearlMinerUsesDirectStratum(value)" not in app_js
    assert "pearlMinerUsesDirectStratum(poolUrl)" not in builder
    assert "agentVersionAtLeast(minerVersion, [2, 1, 1])" not in builder
    assert "pool_url: poolUrl" in builder
    assert 'const requiredCoin = isXmrig ? "XMR" : (isPearl ? "PEARL" : "")' in sync
    assert 'elements.algorithm.value = "pearlhash"' in sync
    assert 'elements.devices.disabled = !minerAdapterHasCapability(minerType, "device_selection")' in sync
    assert 'extraConfig.gpu_backend = elements.gpuBackend.value' in builder
    assert 'extraConfig.cuda_variant = adapterOptions.has("gpu_backend") && elements.gpuBackend.value === "amd" ? "auto" : elements.cudaVariant.value' in builder
    assert "minerAdapterDefaultAlgorithm(minerType, coin)" in builder
    assert 'device_indices: minerAdapterHasCapability(minerType, "device_selection")' in builder




def test_miner_catalog_shows_stable_and_test_release_roles_without_changing_the_default():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    versions = app_js.split("function managedMinerVersions(definition)", 1)[1].split(
        "function managedVersionAsset", 1
    )[0]
    option_label = app_js.split("function managedVersionOptionLabel", 1)[1].split(
        "function managedMinerLabel", 1
    )[0]
    selector = app_js.split("function populateManagedVersionSelect", 1)[1].split(
        "function syncSecondaryMinerVersion", 1
    )[0]
    compatibility = app_js.split("function renderManagedVersionCompatibility", 1)[1].split(
        "function syncManagedLeg", 1
    )[0]

    assert 'right.version.split(".")' in versions
    assert "return unique;" in versions
    assert "return unique.slice(0, 3)" not in versions
    assert "unique.filter(isPreview)" not in versions
    assert "function managedVersionIsPreview(version)" in versions
    assert 'releaseLabel = "默认稳定版"' in option_label
    assert 'releaseLabel = latestPreview?.version === version.version ? "最新测试版" : "历史测试版"' in option_label
    assert 'releaseLabel = "历史稳定版"' in option_label
    assert 'version.version === "2.0.3"' not in option_label
    assert 'version.version === "2.0.2"' not in option_label
    assert 'const latest = String(definition?.latest_version' in selector
    assert 'const selected = current || latest' in selector
    assert '["windows", "Windows"]' in compatibility
    assert '["linux-nvidia", "Linux NVIDIA"]' in compatibility
    assert '["linux-amd", "Linux AMD"]' in compatibility
    assert "测试版需显式选择" in app_js
    assert "managedVersionContainerAsset" not in app_js
    assert "最近 3 版" not in app_js
    assert "2.0.3" not in app_js
    assert "2.0.2" not in app_js


def test_independent_second_miner_autostart_and_normal_mining_summary_contract():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    save_flight = app_js.split("async function saveFlightSheet()", 1)[1].split(
        "async function deleteResource", 1
    )[0]
    autostart = app_js.split("function miningAutostartMarkup", 1)[1].split(
        "function renderDrawer", 1
    )[0]
    managed_leg = app_js.split("function syncManagedLeg", 1)[1].split(
        "function setManagedExtra", 1
    )[0]
    process_label = app_js.split('function miningProcessLabel(process, minerType = "")', 1)[1].split(
        "function agentSupervisionLabel", 1
    )[0]

    assert 'id="summaryNormalMining"' in html
    assert "有算力设备" in html
    assert "各已配置矿工均已上报新鲜的正值算力" in html
    assert '$("#summaryNormalMining").textContent = state.summary.normal_mining || 0' in app_js
    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in styles

    assert 'id="flightSecondaryMode"' in html
    assert '<option value="independent">第二独立矿工（推荐）</option>' in html
    assert '<option value="integrated">SRBMiner 内置双挖（同一进程）</option>' in html
    assert html.count('<option value="xmrig">XMRig（XMR / CPU 推荐）</option>') >= 2
    assert html.count('<option value="pearlminer">Pearl Miner（PEARL / GPU）</option>') >= 2
    assert 'let secondaryMode = "integrated"' in save_flight
    assert "secondary_mode: secondaryMode" in save_flight
    assert 'flightSecondaryMode() === "independent"' in app_js
    assert "syncCpuRandomxFields(prefix)" in managed_leg
    assert 'adapterOptions.has("cpu_threads")' in app_js
    assert 'elements.cpuHint.disabled = !usesCpuRandomx || !adapterOptions.has("cpu_max_threads_hint")' in app_js

    assert "/mining-autostart" in autostart
    assert "method: \"PATCH\"" in autostart
    assert "body: { enabled }" in autostart
    assert "managed_mining.autostart.v1" not in app_js
    assert "function miningShareLabel(status, confirmedRunning)" in app_js
    share_label = app_js.split(
        "function miningShareLabel(status, confirmedRunning)", 1
    )[1].split("function miningProcessLabel", 1)[0]
    assert "status?.shares" in share_label
    assert "status?.hashrate_details" in share_label
    assert "detail.shares" in share_label
    assert 'detail.role === "primary"' in share_label
    assert 'detail.role === "secondary"' in share_label
    assert 'roleCounters.join("；")' in share_label
    assert 'profile.toLowerCase() === "default"' in process_label
    assert "return packageLabel;" in process_label
    assert 'return `${profile} · ${packageLabel}`;' in process_label
    assert process_label.index('profile.toLowerCase() === "default"') < process_label.index('return `${profile} · ${packageLabel}`;')


def test_managed_flight_wsl_execution_mode_is_explicit_gpu_only_and_round_trips():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    builder = app_js.split("function buildManagedLeg(prefix", 1)[1].split(
        "async function saveFlightSheet", 1
    )[0]
    editor = app_js.split("function openFlightDialog(sheetId", 1)[1].split(
        "function parseDeviceIndices", 1
    )[0]
    renderer = app_js.split("function renderFlightSheets()", 1)[1].split(
        "function gpuOcEditSummary", 1
    )[0]

    assert 'id="flightExecutionMode"' in html
    assert 'id="flightSecondaryExecutionMode"' in html
    assert html.count('<option value="native">本机原生（推荐）</option>') == 2
    assert html.count('<option value="wsl">Windows WSL（Linux NVIDIA 包）</option>') == 2
    assert "目标设备已启用可用的 WSL2" in html

    # Eligibility and control changes: tests/controller_release20_ui_behavior.test.cjs,
    # "WSL eligibility and synchronization follow capabilities and actual selected resource".
    # Keep the form/wire/editor contract here without requiring a helper's branch spelling.
    assert '"WSL · Linux NVIDIA"' in app_js

    assert "execution_mode: executionMode" in builder
    assert "只有 Windows GPU 矿工可以使用 WSL；XMRig、CPU 任务和内置双挖必须使用本机原生模式" in builder
    assert "没有可供 WSL 使用的 Linux NVIDIA 包" in builder
    assert "function managedVersionWslAsset(minerType, version)" in app_js
    assert "WSL 模式当前只支持 Linux NVIDIA 矿工包" in builder
    assert 'sheet.execution_mode === "wsl"' in editor
    assert "sheet.secondary.execution_mode || sheet.secondary_execution_mode" in editor
    assert "primaryWsl" not in renderer
    assert "secondaryWsl" not in renderer
    assert '<span class="tag">WSL</span>' not in renderer


def test_device_residue_repair_uses_safe_scan_select_cleanup_workflow():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="miningResidueDialog"' in html
    assert 'id="miningResidueConfirmName"' in html
    assert 'id="cleanupMiningResidueButton"' in html
    assert 'id="rescanMiningResidueButton"' in html
    assert 'id="miningResidueRestart"' in html
    assert "不会仅凭旧 PID 结束进程" in html
    assert "data-mining-residue-repair" in app_js
    assert "function openMiningResidueRepair" in app_js
    assert "async function scanMiningResidue" in app_js
    assert "async function cleanupMiningResidue" in app_js
    assert "function renderMiningResidueRepair" in app_js
    assert "miningResidueRepair: null" in app_js
    assert "miningResidueRepairToken: 0" in app_js
    assert "data-mining-residue-target" in app_js
    assert "data-candidate-fingerprint" in app_js
    assert "manual_candidates" in app_js
    assert "manualCandidates.length === 1" in app_js
    assert "已识别矿工，等待人工确认" in app_js
    assert "候选 PID" in app_js
    assert "本次扫描未提供可复核的唯一候选，不能人工清理" in app_js
    assert 'const selectionState = automaticallySelectable ? " checked" : selectable ? "" : " disabled"' in app_js
    assert "mining-residue-candidates" not in app_js
    assert "managed_mining.residue_repair.explicit_candidate.v1" in app_js
    assert "candidate_fingerprint: input.dataset.candidateFingerprint" in app_js
    assert '"same-name process is not an exact replacement"' in app_js
    assert "同名矿工，但其完整启动参数与旧记录不一致" in app_js
    assert 'body: { action: "restart_mining", payload: {} }' in app_js
    assert 'status === "ambiguous"' in app_js
    assert '"scan_mining_residue.v1"' in app_js
    assert '"cleanup_mining_residue.v1"' in app_js
    assert '"scan_mining_residue.v1": "扫描矿工残留"' in app_js
    assert '"cleanup_mining_residue.v1": "清理矿工残留"' in app_js
    cleanup_flow = app_js.split("async function cleanupMiningResidue(event)", 1)[1].split(
        "function minerLogLineKind", 1
    )[0]
    assert cleanup_flow.index('action: "cleanup_mining_residue.v1"') < cleanup_flow.index(
        'action: "scan_mining_residue.v1"'
    ) < cleanup_flow.index('action: "restart_mining"')
    assert 'finding?.status !== "no_record"' in cleanup_flow
    assert "确认无残留才允许重启挖矿" in html
    assert ".mining-residue-finding.is-ambiguous" in styles
    assert ".mining-residue-finding.is-manual-candidate" in styles
    assert ".mining-residue-candidate" not in styles
    assert ".mining-residue-options" in styles


def test_adaptive_navigation_uses_one_dom_without_drawer_toggles():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    # The single adaptive layout keeps one navigation DOM: no phone drawer,
    # backdrop, hamburger button, or viewport-conditional JS branches.
    for removed in ("sidebarBackdrop", "menuButton", "mobile-menu", "sidebar-backdrop"):
        assert removed not in html
        assert removed not in styles
    assert "setMobileSidebarOpen" not in app_js
    assert "mobile-open" not in styles
    assert "mobile-menu-open" not in styles
    assert "matchMedia" not in app_js

    nav_buttons = re.findall(
        r'<button class="nav-item(?: admin-only| active)?" data-page="[^"]+"[^>]*>.*?</button>',
        html,
    )
    assert len(nav_buttons) >= 8
    for button in nav_buttons:
        assert 'type="button"' in button
        assert 'aria-label="' in button
        assert 'title="' in button
        assert 'class="nav-label"' in button

    # Escape still closes the device drawer for keyboard users at any width.
    assert 'event.key === "Escape"' in app_js
    assert "closeDrawer(); setMobileSidebarOpen(false);" not in app_js


def test_desktop_sidebar_hover_expands_as_overlay_over_the_adaptive_strip():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="appView" class="app-shell hidden"' in html
    assert "desktopSidebarToggle" not in html
    assert "desktopSidebarExpanded" not in app_js
    assert "syncDesktopSidebar" not in app_js
    assert "setDesktopSidebarExpanded" not in app_js
    assert "sidebar-expanded" not in styles
    nav_buttons = re.findall(
        r'<button class="nav-item(?: admin-only| active)?" data-page="[^"]+"[^>]*>.*?</button>',
        html,
    )
    nav_pages = [re.search(r'data-page="([^"]+)"', button).group(1) for button in nav_buttons]
    assert len(nav_pages) == len(set(nav_pages))
    assert {"dashboard", "devices", "workload", "flights", "wallets", "pools", "miners", "agentReleases", "access"} <= set(nav_pages)
    for button in nav_buttons:
        assert 'type="button"' in button
        assert 'aria-label="' in button
        assert 'title="' in button
        assert 'class="nav-icon" aria-hidden="true"' in button
        assert 'class="nav-label"' in button

    assert "rigpilot_sidebar" not in app_js

    base_shell = styles.split(".app-shell {", 1)[1].split("}", 1)[0]
    assert "--sidebar-width: 238px" in base_shell
    assert "grid-template-columns: var(--sidebar-width) minmax(0, 1fr)" in base_shell
    assert "transition:" not in base_shell
    base_icon = styles.split(".nav-icon {", 1)[1].split("}", 1)[0]
    assert "width: 18px" in base_icon
    assert "text-align: center" in base_icon
    assert "display: grid" not in base_icon
    desktop = styles.split("/* Fine-pointer desktops", 1)[1].split(
        ".topbar {", 1
    )[0]
    assert "(hover: hover) and (pointer: fine) and (width > 960px)" in desktop
    assert "(hover: hover) and (pointer: fine) and (width > 900px) and (height > 520px)" in desktop
    assert ".app-shell { --sidebar-width: 76px; }" in desktop
    assert ".sidebar { box-sizing: border-box; width: 76px;" in desktop
    assert "overflow-x: hidden" in desktop
    assert ".sidebar:is(:hover, :has(:focus-visible)) { width: 238px;" in desktop
    assert ".app-shell { --sidebar-width: 230px; }" not in styles
    assert "#deviceDrawer { width: min(1540px, calc(100vw - 76px)); }" in styles
    assert ".sidebar .nav-label" in desktop
    assert "white-space: nowrap" in desktop
    assert ".sidebar .secure-label," in desktop
    assert ".sidebar .user-copy { min-width: 0; white-space: nowrap; }" in desktop
    assert ".sidebar .nav-icon { width: 20px; height: 20px; display: grid;" in desktop
    assert "legacy" not in desktop
    assert "max-width: calc(1700px + 238px - var(--sidebar-width))" in styles

    narrow = styles.split("/* Single adaptive layout:", 1)[1]
    assert ".app-shell { grid-template-columns: minmax(0, 1fr); }" in narrow
    assert ".nav-list {" in narrow
    assert "overflow-x: auto;" in narrow
    assert "flex: 1 0 100%;" in narrow
    assert "sidebar.mobile-open" not in styles
    assert "setMobileSidebarOpen" not in app_js


def test_adaptive_sidebar_and_modal_motion_survive_fixed_theme():
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    normal_motion = styles.rsplit("@media (prefers-reduced-motion: reduce)", 1)[0]
    reduced_motion = styles.rsplit("@media (prefers-reduced-motion: reduce)", 1)[1]

    def last_transition_for(selector: str) -> str:
        value = ""
        for match in re.finditer(
            r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}",
            normal_motion,
            flags=re.DOTALL,
        ):
            selectors = [part.strip().splitlines()[-1] for part in match.group("selectors").split(",")]
            if selector not in selectors:
                continue
            transition = re.search(r"(?<!-)transition\s*:\s*(?P<value>[^;]+);", match.group("body"))
            if transition:
                value = transition.group("value").strip()
        return value

    sidebar_transition = last_transition_for(".sidebar")
    drawer_transition = last_transition_for(".drawer")
    assert "width" in sidebar_transition
    assert "box-shadow" in sidebar_transition
    assert "background-color" in sidebar_transition
    assert "transform" in drawer_transition
    assert "background-color" in drawer_transition

    modal_open = re.search(
        r"\.modal\[open\]\s+\.modal-card\s*\{(?P<body>[^{}]+)\}",
        normal_motion,
        flags=re.DOTALL,
    )
    assert modal_open
    assert "animation:" in modal_open.group("body") or "transition:" in modal_open.group("body")
    assert "opacity" in normal_motion
    assert "transform" in normal_motion

    assert ".sidebar" in reduced_motion
    assert ".drawer" in reduced_motion
    assert ".modal-card" in reduced_motion
    assert "transition: none" in reduced_motion
    assert "animation: none" in reduced_motion


def test_narrow_layout_prevents_login_zoom_and_keeps_topbar_actions_visible():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    narrow = styles.split("/* Single adaptive layout:", 1)[1]

    viewport = html.split('name="viewport"', 1)[1].split(">", 1)[0]
    assert "width=device-width" in viewport
    assert "initial-scale=1" in viewport
    assert "user-scalable=no" not in viewport
    assert "maximum-scale" not in viewport

    assert "-webkit-text-size-adjust: 100%" in styles
    assert "input, select, textarea { font-size: 16px; }" in narrow
    # One adaptive layout: narrow viewports re-wrap the topbar instead of
    # hiding its controls behind phone-only overrides.
    assert "[data-open-enroll] { display: none" not in styles
    assert ".topbar-actions .icon-button { display: none; }" not in styles
    assert ".topbar-actions #refreshButton { display: grid; }" not in styles
    assert "menuButton" not in styles


def test_narrow_tables_keep_horizontal_scrolling_and_larger_touch_actions():
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    narrow = styles.split("/* Single adaptive layout:", 1)[1]

    assert "-webkit-overflow-scrolling: touch" in narrow
    assert "overscroll-behavior-inline: contain" in narrow
    assert ".device-table th { padding: 0 14px; font-size: 10px; }" in narrow
    assert ".device-table td { padding: 0 14px; font-size: 12px; }" in narrow
    assert ".device-table .text-button" in narrow
    assert "min-height: 40px" in narrow
    assert "font-size: 13px" in narrow


def test_device_os_badges_use_platform_icons_instead_of_abbreviations():
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert ".os-badge.windows::before" in styles
    assert ".os-badge.ubuntu::before" in styles
    assert 'background: url("data:image/svg+xml;base64,' in styles
    assert "Tux © Larry Ewing" in styles
    assert "position: absolute" in styles
    assert "inset: 0" in styles
    assert "margin: auto" in styles
    assert ".os-badge.windows," in styles
    assert ".os-badge.ubuntu { font-size: 0; }" in styles

    encoded_tux = styles.split(
        'background: url("data:image/svg+xml;base64,', 1
    )[1].split('") center / contain no-repeat', 1)[0]
    tux_svg = base64.b64decode(encoded_tux, validate=True).decode("utf-8")
    assert 'viewBox="0 0 600 700"' in tux_svg
    assert "<script" not in tux_svg
    assert "<foreignObject" not in tux_svg
    assert "javascript:" not in tux_svg


def test_device_details_use_wide_desktop_grid_without_changing_mobile_drawer():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    desktop = styles.split("@media (min-width: 1000px)", 1)[1].split(".modal {", 1)[0]
    assert "grid-template-columns: repeat(12, minmax(0, 1fr))" in desktop
    assert "grid-auto-rows: max-content" in desktop
    assert "width: min(1350px, calc(100vw - 230px))" in styles
    assert ".device-detail-gpus { grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); }" in styles
    assert ".device-detail-logs { grid-column: 1 / -1 !important; }" in styles
    assert "repeat(auto-fit, minmax(360px, 1fr))" in desktop
    assert '.drawer-content[data-active-tab="overview"] { grid-template-columns: repeat(2, minmax(0, 1fr)); }' in styles
    assert '.drawer-content[data-active-tab="overview"] > .drawer-section { height: auto' in styles
    full_rule = styles.split(
        '.drawer-content[data-active-tab="overview"] > :is(', 1
    )[1].split(') { grid-column: 1 / -1; }', 1)[0]
    half_rule = styles.split(
        '.drawer-content[data-active-tab="overview"] > :is(', 2
    )[2].split(') { grid-column: span 1; }', 1)[0]
    assert ".self-heal-list-pill" not in styles and ".device-detail-self-heal" not in styles
    assert set(re.findall(r"\.device-detail-[\w-]+", full_rule)) == {
        ".device-detail-mining",
        ".device-detail-resources",
        ".device-detail-network", ".device-detail-telemetry",
        ".device-detail-identity", ".device-detail-info",
    }
    assert set(re.findall(r"\.device-detail-[\w-]+", half_rule)) == {
        ".device-detail-config", ".device-detail-autostart",
        ".device-detail-actions",
    }
    assert '$("#drawerContent").dataset.activeTab = tab;' in app_js

    for class_name in (
        "device-detail-mining",
        "device-detail-logs",
        "device-detail-resources",
        "device-detail-network",
        "device-detail-telemetry",
        "device-detail-gpus",
        "device-detail-config",
        "device-detail-autostart",
        "device-detail-actions",
        "device-detail-identity",
        "device-detail-info",
    ):
        assert class_name in app_js

    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]
    # Helper calls establish section order; each helper must still emit its
    # expected layout class. Desktop and mobile CSS constraints remain above.
    logs = app_js.split("function minerLogSectionMarkup(agent)", 1)[1].split(
        "\nfunction ", 1
    )[0]
    assert 'class="drawer-section device-detail-logs"' in logs
    assert 'data-device-tab="workload"' not in drawer
    assert "function openDeviceWorkload" not in app_js
    ordered_sections = (
        "device-kpi-strip",
        "alert-list",
        "device-detail-gpus",
        "device-detail-mining",
        "minerLogSectionMarkup(agent)",
        "device-detail-resources",
        "device-detail-network",
        "device-detail-telemetry",
        "device-detail-config",
        "device-detail-autostart",
        "device-detail-actions",
        "device-detail-identity",
        "device-detail-info",
    )
    offsets = [drawer.index(class_name) for class_name in ordered_sections]
    assert offsets == sorted(offsets)
    assert "device-detail-wifi" not in drawer

    # The compact drawer remains the default and therefore remains unchanged
    # for mobile/tablet widths below the explicit desktop breakpoint.
    assert ".drawer { position: fixed" in styles
    assert "width: min(560px, 100vw)" in styles




def test_device_note_editor_is_not_rendered_in_device_details():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="agentNote"' not in app_js
    assert "设备备注" not in app_js
    assert ".agent-details-form #agentNote" not in styles
    assert ".device-note-card" not in styles
    save_handler = app_js.split("async function saveAgentDetails(event)", 1)[1].split(
        "function agentVersionAtLeast", 1
    )[0]
    assert 'body: { name }' in save_handler
    assert 'body: { name, note }' not in save_handler


def test_flight_sheet_miner_selects_use_authenticated_adapter_catalog_with_fallback():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert "minerAdapters: []" in app_js
    assert 'request("minerAdapters", "/api/miner-adapters")' in app_js
    bootstrap_match = re.search(
        r'<script id="minerAdapterBootstrap" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    assert bootstrap_match is not None
    bootstrap = json.loads(bootstrap_match.group(1))
    from server.miner_adapters import MINER_ADAPTERS

    assert bootstrap == MINER_ADAPTERS.public_catalog()
    by_type = {item["miner_type"]: item for item in bootstrap}
    expected_ids = {
        "xmrig": "org.xmrig.xmrig",
        "srbminer": "org.doktor83.srbminer-multi",
        "pearlminer": "org.pearlfortune.pearl-miner",
        "seine": "org.zpalmtree.seine",
    }
    assert {name: by_type[name]["adapter_id"] for name in expected_ids} == expected_ids
    assert [item["miner_type"] for item in bootstrap] == list(expected_ids)
    # Fallback consumption: tests/hiveos_flight_ui.test.cjs,
    # "the built-in catalog retains current miners and excludes retired miners from new choices".
    # Bootstrap/API-registry equality above verifies data instead of its JS parser spelling.
    assert "function availableMinerAdapters()" in app_js
    assert "function populateManagedMinerTypeSelect(prefix, selectedType = \"\")" in app_js
    assert 'data-adapter-id="${escapeHtml(adapter.adapter_id || "")}"' in app_js
    assert 'populateManagedMinerTypeSelect("flight", "xmrig")' in app_js
    assert 'populateManagedMinerTypeSelect("flightSecondary", "pearlminer")' in app_js
    assert 'const primaryType = sheet.miner_type || "generic";' in app_js
    assert 'const secondaryType = sheet.secondary.miner_type || "srbminer";' in app_js

    # Static options remain usable before login and when the catalog endpoint
    # is unavailable; JavaScript replaces them only after trusted data arrives.
    assert html.count('<option value="xmrig">XMRig（XMR / CPU 推荐）</option>') >= 2
    assert html.count('<option value="srbminer">SRBMiner-MULTI（CPU / GPU）</option>') >= 2
    assert html.count('<option value="pearlminer">Pearl Miner（PEARL / GPU）</option>') >= 2
    assert '<option value="lolminer">' not in html
    assert '<option value="forgeminer">' not in html
    _cache_busted_assets(html, ["/static/theme-init.js", "/static/app.js"])


def test_retired_miners_are_display_only_and_have_no_execution_ui():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    for miner_type in ("lolminer", "forgeminer", "synthetic-unsupported-miner"):
        assert f'<option value="{miner_type}">' not in html
        assert f'if (minerType === "{miner_type}")' not in app_js
    assert "lolMiner Stratum" not in app_js
    assert "ForgeMiner Stratum" not in app_js
    assert "const RETIRED_MINER_LABELS" in app_js
    assert " · 已退休" in app_js
    assert "function populateMinerPackageManagedTypeSelect(selectedType = \"\")" in app_js
    assert "populateMinerPackageManagedTypeSelect();" in app_js


def test_common_extra_args_are_in_each_native_miner_software_configuration():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    for prefix in ("flight", "flightSecondary"):
        assert f'id="{prefix}ExtraArgs" maxlength="4096"' in html
        assert f'id="{prefix}ExtraArgsField"' in html
    assert "elements.extraArgs.value = typeof extra.extra_args" in app_js
    assert "extraConfig.extra_args = elements.extraArgs.value" in app_js
    assert '"cuda_variant", "tls", "extra_args"' in app_js
    assert html.count("非空额外参数需要目标设备运行 Agent 0.8.31 或更新版本") == 2
    assert html.count("managed_mining.extra_args.v1") == 2
    assert html.count("按主挖、第二矿工的配置顺序各追加一次额外参数") == 2


def test_flight_sheet_algorithm_options_and_defaults_prefer_adapter_metadata():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    helpers = app_js.split("function minerAdapterAlgorithms", 1)[1].split(
        "function flightSecondaryMode", 1
    )[0]
    effective = app_js.split("function effectiveFlightAlgorithm(prefix)", 1)[1].split(
        "function managedLegSupportsWsl", 1
    )[0]
    synchronizer = app_js.split("function syncManagedLeg(prefix", 1)[1].split(
        "function setManagedExtra", 1
    )[0]

    assert "minerAdapterDefinition(minerType)?.algorithms" in helpers
    assert "adapter.coin_defaults" in helpers
    assert "adapter.default_algorithm" in helpers
    assert "function populateManagedAlgorithmSelect(prefix, selectedAlgorithm = \"\")" in app_js
    assert 'elements.algorithm.innerHTML = \'<option value="">自动</option>\'' in helpers
    assert "data-history-algorithm" in helpers
    assert "minerAdapterDefaultAlgorithm(elements.type.value, coin)" in effective
    assert "populateManagedAlgorithmSelect(prefix, preserveAlgorithm ? elements.algorithm.value : \"\")" in synchronizer
    assert "minerAdapterDefaultAlgorithm(minerType, coin)" in synchronizer

    # Pair outcomes: tests/controller_release20_ui_behavior.test.cjs,
    # "ordered GPU pairs preserve order while periodic and CPU plus GPU remain supported".
    # OC eligibility is checked there by its actual resource/execution-mode case.


def test_legacy_and_unknown_flight_sheets_are_read_only_until_explicit_copy_migration():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    opener = app_js.split("function openFlightDialog(sheetId = null)", 1)[1].split(
        "function parseDeviceIndices", 1
    )[0]
    saver = app_js.split("async function saveFlightSheet()", 1)[1].split(
        "async function deleteResource", 1
    )[0]

    assert 'id="flightLegacyNotice"' in html
    assert 'id="migrateFlightButton"' in html
    assert "function flightSheetMigrationReason(sheet)" in app_js
    assert 'sheet.miner_type === "generic"' in app_js
    assert "!minerAdapterDefinition(minerType)" in app_js
    assert "function setFlightFormHistoricalReadOnly(readOnly)" in app_js
    assert "function beginFlightMigrationCopy()" in app_js
    assert 'state.editingFlightSheetId = null' in app_js.split(
        "function beginFlightMigrationCopy()", 1
    )[1].split("function openFlightDialog", 1)[0]
    assert "迁移为副本" in html
    assert "原记录不会被覆盖" in opener
    assert "setFlightFormHistoricalReadOnly(true)" in opener
    assert "if (state.flightHistoricalReadOnly)" in saver
    assert "请先明确点击“迁移为副本”" in saver
    assert 'method: editing ? "PUT" : "POST"' in saver
    assert "迁移副本已创建；原飞行表保持不变" in saver
    assert "保存后会转换为 XMRig / SRBMiner / Pearl Miner" not in app_js


def test_adapter_option_schema_generates_only_unhandled_typed_job_controls():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    renderer = app_js.split("function renderAdapterOptionControls(prefix)", 1)[1].split(
        "function collectAdapterSchemaOptions", 1
    )[0]
    collector = app_js.split("function collectAdapterSchemaOptions(prefix)", 1)[1].split(
        "function flightSecondaryMode", 1
    )[0]
    builder = app_js.split("function buildManagedLeg(prefix", 1)[1].split(
        "async function saveFlightSheet", 1
    )[0]

    assert 'id="flightAdapterOptions"' in html
    assert 'id="flightSecondaryAdapterOptions"' in html
    assert "const specializedAdapterOptions = new Set([" in app_js
    assert '"cpu_threads", "cpu_max_threads_hint", "huge_pages", "gpu_backend"' in app_js
    assert '&& (option.scope || "job") === "job"' in renderer
    assert 'option.type === "boolean"' in renderer
    assert 'option.type === "choice"' in renderer
    assert 'option.type === "integer"' in renderer
    assert 'data-adapter-option="${escapeHtml(name)}"' in renderer
    assert "numericChoice" in collector
    assert "Object.assign(extraConfig, collectAdapterSchemaOptions(prefix))" in builder
    assert 'minerAdapterHasCapability(minerType, "device_selection")' in builder
    assert "function resetAdapterSchemaOptionValues(prefix, extra = {})" in app_js
    assert "resetAdapterSchemaOptionValues(prefix, extra)" in app_js
    assert 'resetAdapterSchemaOptionValues("flight")' in app_js
    assert 'resetAdapterSchemaOptionValues("flightSecondary")' in app_js
    assert "function flightGpuOcIsSupported()" in app_js
    # Pair outcomes: tests/controller_release20_ui_behavior.test.cjs,
    # "ordered GPU pairs preserve order while periodic and CPU plus GPU remain supported".
    # Generic typed controls do not require a second literal dual-pair list.


def test_workload_settlement_uses_a_persisted_five_minute_timestamp():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="workloadSettlementNote" type="datetime-local" step="300"' in html
    assert 'id="workloadSettlementUseAsStart"' in html
    assert "settlement_at: settlementAt" in app
    assert "expected_revision: Number(state.settlementNote?.revision) || 0" in app
    assert "Number(match[5]) % 5 !== 0" in app
    assert "return date.toISOString();" in app
    assert "from.value = settlementLocalValue(saved);" in app
    assert "legacy_note" in app
    save = app.split("async function saveWorkloadSettlementNote()", 1)[1].split(
        "function averageGpuUtilization", 1
    )[0]
    assert "note," not in save
    assert "input.value = draft" in save


def test_v1_flight_editor_uses_modern_task_cards_without_changing_transport():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    saver = app.split("async function saveFlightSheet()", 1)[1].split(
        "async function deleteResource", 1
    )[0]

    assert html.count("设置挖矿软件配置") == 2
    assert 'id="flightForm" novalidate' in html
    assert 'data-flight-configure="flight"' in html
    assert 'data-flight-configure="flightSecondary"' in html
    assert 'id="flightPrimaryFields" class="flight-section flight-editor-job" data-flight-config-fieldset="flight"' in html
    assert 'id="flightSecondaryFields" class="flight-section flight-editor-job secondary-section hidden" data-flight-config-fieldset="flightSecondary"' in html
    assert html.count("flight-miner-config-close-top") == 2
    assert 'class="inline-grid flight-software-grid flight-job-core-only"' in html
    assert html.count('class="inline-grid flight-software-grid flight-job-core-only"') == 2
    assert 'flightMinerSummaryTitle' not in html
    assert 'flightSecondaryMinerSummaryTitle' not in html
    assert 'id="flightExecutionModeField" class="flight-miner-config-only"' in html
    assert 'id="flightSecondaryExecutionModeField" class="flight-miner-config-only"' in html
    assert 'id="flightAdvanced" class="flight-help flight-advanced flight-miner-config-only"' in html
    assert 'id="flightSecondaryAdvanced" class="flight-help flight-advanced flight-miner-config-only"' in html
    primary = html.split('id="flightPrimaryFields"', 1)[1].split("</fieldset>", 1)[0]
    secondary = html.split('id="flightSecondaryFields"', 1)[1].split("</fieldset>", 1)[0]
    assert 'id="flightWorkerField" class="flight-worker-config flight-miner-config-only"' in primary
    assert primary.index('id="flightExecutionModeField"') < primary.index('id="flightWorkerField"') < primary.index('id="flightAdvanced"')
    assert html.count('id="flightWorker"') == 1
    assert 'id="flightWorker"' not in secondary
    assert primary.index('id="flightMinerType"') < primary.index('id="flightExecutionModeField"') < primary.index('id="flightAdvanced"')
    assert secondary.index('id="flightSecondaryMinerType"') < secondary.index('id="flightSecondaryExecutionModeField"') < secondary.index('id="flightSecondaryAdvanced"')
    assert 'class="flight-job-core-only">钱包<select id="flightWallet"' in primary
    assert 'class="flight-job-core-only">钱包<select id="flightSecondaryWallet"' in secondary
    assert "function openFlightMinerConfig(prefix)" in app
    assert "function resetActiveFlightMinerConfig()" in app
    assert "function validateFlightFormForSave(form)" in app
    assert "function validateActiveFlightMinerConfig(form)" in app
    assert 'form.querySelector(":invalid")' in app
    assert 'openFlightMinerConfig(fieldset.dataset.flightConfigFieldset)' in app
    assert "if (validateFlightFormForSave(event.currentTarget)) saveFlightSheet();" in app
    assert 'closeFlightMinerConfig({ restoreFocus: false })' in app
    assert 'field.classList.remove("hidden")' in app
    assert ".flight-editor-job" in css
    assert ".flight-miner-config-open" in css
    assert "#flightsPage .flight-table tbody { display: grid" in css
    assert "position: static; inset: auto; display: grid !important" in css
    assert "overflow-x: hidden; overflow-y: auto" in css
    assert ".flight-miner-config-only" in css
    assert "#flightForm:not(.flight-miner-config-open) > .flight-editor-job > .flight-miner-config-only { display: none !important; }" in css
    assert "height: calc(100dvh - 30px); max-height: calc(100vh - 30px)" in css
    assert "#flightForm.flight-miner-config-open > .flight-editor-job.is-config-target > .flight-software-grid {" not in css
    assert "#flightForm.flight-miner-config-open > .flight-editor-job.is-config-target > .miner-version-compat {" not in css

    assert 'api(editing ? `/api/flight-sheets/${encodeURIComponent(editing)}` : "/api/flight-sheets"' in saver
    # Card title/column/secondary projection and layout requirements live in
    # test_flight_card_alignment_contract.py; this test owns editor/v1 transport.
    assert 'worker_name_template: validateWorkerNameTemplate($("#flightWorker").value)' in saver
    opener = app.split("function openFlightMinerConfig(prefix)", 1)[1].split("function reportFlightInvalidControl", 1)[0]
    assert '${prefix}MinerType' not in opener
    assert "form.scrollTop = 0" in opener
    assert "fieldset.scrollTop = 0" not in opener
    assert 'method: editing ? "PUT" : "POST"' in saver
    assert "/api/flight-sheets-v2" not in saver
    assert "/api/flight-v2" not in saver
    assert "runtime_declaration" not in saver
    assert "result_binding" not in saver
    assert "adapter_config_hash" not in saver


def test_v1_xmrig_keeps_null_algorithm_while_other_miners_fail_closed():
    app = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    builder = app.split("function buildManagedLeg(prefix", 1)[1].split(
        "async function saveFlightSheet", 1
    )[0]

    assert 'let algorithm = minerType === "xmrig"' in builder
    assert "? null" in builder
    assert 'if (minerType !== "xmrig" && !algorithm)' in builder
    assert "币种没有内置推荐算法，请在高级设置中选择算法" in builder
    assert "algorithm," in builder
    assert "runtime_declaration" not in builder
    assert "adapter_config_hash" not in builder


def test_r8_coin_summary_and_history_keep_each_coin_visible():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    summary = html.split('class="summary-card summary-hashrate"', 1)[1].split("</article>", 1)[0]
    renderer = app_js.split("function renderOverviewAggregateSummary()", 1)[1].split(
        "function overviewFilteredAgents", 1
    )[0]
    assert 'id="summaryHashrateGroups"' in summary
    assert 'role="list"' in summary
    assert 'id="summaryHashrate"' not in summary
    assert 'id="summaryHashrateNote"' not in summary
    assert "groups.map((group)" in renderer
    assert 'formatHashrateAmount(group.hps, "H/s")' in renderer
    assert 'role="listitem"' in renderer
    assert "groups.length} 币种" not in renderer
    assert "note.textContent" not in renderer
    assert "summary-hashrate-empty" in renderer

    assert 'id="hashrateHistoryChart"' in html
    assert 'id="hashrateHistoryMeta"' in html
    # Rendered charts: tests/hashrate_coin_chart.test.cjs,
    # "three API coin series keep independent cards and unit-aware axes"
    # and "missing and invalid historical readings are not converted to zero".
    # Shared renderer extraction must not require particular formatter call strings.
    assert ".hashrate-series-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in styles
    assert ".hashrate-series-grid { grid-template-columns: minmax(0, 1fr); }" in styles


def test_r8_device_flight_summary_grows_before_its_tab_row():
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")
    drawer = app_js.split("function renderDrawer(", 1)[1].split(
        "function formatUptime", 1
    )[0]
    assert drawer.index("deviceFlightSummaryMarkup(agent") < drawer.index('class="device-detail-tabs"')
    summary = styles.split(".device-flight-summary {", 1)[1].split("}", 1)[0]
    summary_cell = styles.split(".device-flight-summary > div {", 1)[1].split("}", 1)[0]
    title = styles.split(".device-flight-summary strong {", 1)[1].split("}", 1)[0]
    detail = styles.split(".device-flight-summary small {", 1)[1].split("}", 1)[0]
    tabs = styles.split(".device-detail-tabs {", 1)[1].split("}", 1)[0]
    assert "min-height: 62px" in summary
    assert "min-height: 64px" in summary_cell
    assert "align-self: stretch" in summary_cell
    assert "overflow-wrap: anywhere" in title
    assert "white-space: nowrap" not in title
    assert "flex-wrap: wrap" in detail
    assert "min-height: 42px" in tabs
    assert "grid-column: 1 / -1" in tabs


def test_r9_charcoal_controls_login_and_detail_cta_share_their_intended_cascade():
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert '--bg: #22252a' in styles
    assert '--panel: #2d3137' in styles
    assert '--gpu-normal: #7fb643' in styles
    assert '.gpu-temperature-badge.is-normal { color: var(--accent-ink); background: var(--gpu-normal); }' in styles
    assert '.overview-temperature-chip.is-normal { color: var(--accent-ink); background: var(--gpu-normal); }' in styles
    assert ':where(input:not([type="checkbox"]):not([type="radio"]):not([type="file"]):not([type="hidden"]), select, textarea)' in styles
    assert ':where(input, select, textarea) { min-height: 40px' not in styles
    assert 'input[type="file"]:not([hidden]) { display: block; box-sizing: border-box; width: 100%; min-width: 0; max-width: 100%;' in styles
    assert '.modal-card { background: var(--modal-bg); border: 1px solid var(--line); border-radius: var(--radius);' in styles
    assert '.security-inline { display: grid;' in styles
    assert 'border-radius: var(--radius); background: var(--panel-2);' in styles

    login_base = styles.index('.login-shell { min-height: 100vh;')
    login_mobile = styles.index('.login-shell { grid-template-columns: 1fr; }')
    compact_workspace = styles.index('/* Compact charcoal and amber workspace.')
    assert login_base < login_mobile < compact_workspace
    assert '.login-shell { grid-template-columns: minmax(420px, 1.15fr)' not in styles[compact_workspace:]
    assert '.device-flight-summary > button { min-height: 38px; grid-column: 1 / -1; justify-self: end;' in styles
    assert '.device-flight-summary > button { width: calc(100% - 24px); justify-self: stretch; }' in styles
    # The single adaptive layout keeps summary content (icons, captions)
    # visible at every width instead of dropping them on phones.
    assert 'display: none; }' in styles
    assert '.overview-summary-grid .summary-hashrate .summary-icon { display: none; }' not in styles
    assert '.summary-card small { display: none; }' not in styles
    assert 'grid-auto-rows: max-content' in styles
