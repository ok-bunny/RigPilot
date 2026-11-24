from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_batch_agent_update_ui_is_explicit_and_keeps_single_device_flow():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="openAgentBatchUpdateButton"' in html
    assert 'id="agentBatchUpdateDialog"' in html
    assert 'id="agentBatchUpdateRelease"' in html
    assert 'id="agentBatchUpdateAgents"' in html
    assert 'id="agentBatchUpdateSelectedCount">已选择 0 台' in html
    assert "不会默认选择任何设备" in html

    assert "function openAgentBatchUpdateDialog()" in app_js
    assert "form.reset();" in app_js
    assert "默认一台也不选" in app_js
    assert 'data-agent-batch-update="${escapeHtml(agent.id)}"' in app_js
    assert ':checked", $("#agentBatchUpdateAgents")' in app_js
    assert "请明确勾选至少一台设备；系统不会默认全选。" in app_js
    assert "window.confirm(`确认将 RigPilot Agent" in app_js
    assert "/api/agent-releases/${encodeURIComponent(release.id)}/deploy-batch" in app_js
    assert "agent_ids: selected.map((agent) => agent.id)" in app_js
    assert "renderBatchOperationResults(" in app_js

    # The existing one-device path remains available.
    assert "/api/agent-releases/${encodeURIComponent(release.id)}/deploy" in app_js
    assert "async function deployAgentRelease()" in app_js
    assert 'id="agentUpdateDialog"' in html

    assert ".batch-choice-list" in styles
    assert ".batch-choice.is-disabled" in styles
    assert ".batch-result.accepted" in styles
    assert ".batch-result.rejected" in styles


def test_batch_flight_apply_ui_is_explicit_and_keeps_single_device_flow():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="openFlightBatchApplyButton"' in html
    assert 'id="flightBatchApplyDialog"' in html
    assert 'id="flightBatchApplySheet"' in html
    assert 'id="flightBatchApplyAgents"' in html
    assert 'id="flightBatchApplySelectedCount">已选择 0 台' in html
    assert 'id="applyFlightSheetBatchButton"' in html
    assert "不会默认选择任何设备" in html

    assert "function openFlightBatchApplyDialog()" in app_js
    assert "function renderFlightBatchApplyAgents()" in app_js
    assert "function selectedFlightBatchApplyItems()" in app_js
    assert "form.reset();" in app_js
    assert "默认一台也不选" in app_js
    assert 'data-flight-batch-apply="${escapeHtml(agent.id)}"' in app_js
    assert ':checked", $("#flightBatchApplyAgents")' in app_js
    assert "请明确勾选至少一台设备；系统不会默认全选。" in app_js
    assert "window.confirm(`确认将飞行表“${sheet.name}”应用到以下" in app_js
    assert "/api/flight-sheets/${encodeURIComponent(sheet.id)}/apply-batch" in app_js
    assert "agent_ids: selected.map((agent) => agent.id)" in app_js
    assert "renderBatchOperationResults(" in app_js

    # The existing one-device path remains available.
    assert "/api/agents/${encodeURIComponent(agent.id)}/flight-sheets/${encodeURIComponent(profileId)}/apply" in app_js
    assert "async function submitDeploy()" in app_js
    assert 'id="deployDialog"' in html


def test_device_gpu_batch_oc_ui_uses_uuid_selection_and_safe_button_state():
    html = (ROOT / "server" / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "server" / "static" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "server" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="deviceGpuOcBatchDialog"' in html
    assert 'id="deviceGpuOcBatchForm"' in html
    assert 'id="deviceGpuOcBatchTargets"' in html
    assert 'id="deviceGpuOcBatchSelectedCount">已选择 0 张' in html
    assert "只改变你明确勾选的显卡" in html

    assert "function batchDeviceGpuCandidates(agent)" in app_js
    assert "hasSavedOc: Boolean(gpuDesiredOcEntry(agent, gpu))" in app_js
    assert 'data-device-gpu-oc-batch="${escapeHtml(gpu.uuid)}"' in app_js
    assert ':checked", $("#deviceGpuOcBatchTargets")' in app_js
    assert "selected.every((gpu) => gpu.capabilities.includes(field))" in app_js
    assert "selected.every((gpu) => gpu.hasSavedOc)" in app_js
    assert "未勾选显卡绝不会改变" in app_js
    assert "/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/batch-apply" in app_js
    assert "/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/batch-clear" in app_js
    assert "gpu_uuids: selected.map((gpu) => gpu.uuid)" in app_js
    for action in ("applyDeviceGpuOcBatch", "clearDeviceGpuOcBatch"):
        handler = app_js.split(f"async function {action}()", 1)[1].split("\n}", 1)[0]
        assert "if (state.deviceGpuOcBatchTarget !== target) return;" in handler
        assert "setDeviceGpuOcPending(target, false);" in handler

    # The existing one-card path remains available.
    assert "/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/apply" in app_js
    assert 'data-device-gpu-oc="${index}"' in app_js

    assert ".gpu-batch-choice-list" in styles
    assert ".batch-selection-head" in styles
    assert ".batch-result-list" in styles
